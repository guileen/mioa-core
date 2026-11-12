// Native heap admission counts page backing, including allocator metadata and
// retained buckets, not logical object lengths. Only physically disposed (or
// never acquired) backing can return trusted growth credit; baseline stays live.
const std = @import("std");
const lp = @import("lightpanda");
const Gate = @import("../../TaskGate.zig");
const Allocator = std.mem.Allocator;
const Alignment = std.mem.Alignment;
const chunk = 4 * 1024 * 1024;
const unused_limit = 2 * chunk;

// Both byte counts fit in u32 because the owner ceiling is validated before
// managed publication. One CAS couples admission and credit retirement: a
// reservation based on a pre-retirement snapshot cannot spend returned credit.
const Admission = struct {
    value: std.atomic.Value(u64) = .init(0),

    const Snapshot = struct { used: usize, credit: usize };

    fn pack(live: usize, admitted: usize) u64 {
        std.debug.assert(live <= admitted and admitted <= std.math.maxInt(u32));
        return (@as(u64, @intCast(admitted)) << 32) | @as(u32, @intCast(live));
    }
    fn unpack(word: u64) Snapshot {
        return .{ .used = @as(u32, @truncate(word)), .credit = @intCast(word >> 32) };
    }
    fn load(self: *const Admission) Snapshot {
        return unpack(self.value.load(.acquire));
    }
    fn tryReserve(self: *Admission, bytes: usize) bool {
        return self.reserveFrom(bytes, self.value.load(.acquire));
    }
    fn reserveFrom(self: *Admission, bytes: usize, observed: u64) bool {
        var current = observed;
        while (true) {
            const state = unpack(current);
            if (bytes > state.credit - state.used) return false;
            current = self.value.cmpxchgWeak(current, pack(state.used + bytes, state.credit), .acq_rel, .acquire) orelse return true;
        }
    }
    // Growth and retirement are serialized by the stream mutex, but reserve
    // and release remain concurrent. Never overwrite either half with a store.
    fn addCredit(self: *Admission, bytes: usize) void {
        var current = self.value.load(.acquire);
        while (true) {
            const state = unpack(current);
            current = self.value.cmpxchgWeak(current, pack(state.used, state.credit + bytes), .acq_rel, .acquire) orelse return;
        }
    }
    fn releaseUsed(self: *Admission, bytes: usize) Snapshot {
        var current = self.value.load(.acquire);
        while (true) {
            const state = unpack(current);
            std.debug.assert(bytes <= state.used);
            const next = pack(state.used - bytes, state.credit);
            current = self.value.cmpxchgWeak(current, next, .acq_rel, .acquire) orelse return unpack(next);
        }
    }
    fn retireUnused(self: *Admission) usize {
        return self.retireFrom(self.value.load(.acquire));
    }
    fn retireFrom(self: *Admission, observed: u64) usize {
        var current = observed;
        while (true) {
            const state = unpack(current);
            const unused = state.credit - state.used;
            if (unused <= unused_limit) return 0;
            // Retain one complete block plus the partially used block (if
            // any). This is less than 8MiB unused and never returns baseline.
            const bytes = (unused - chunk) / chunk * chunk;
            current = self.value.cmpxchgWeak(current, pack(state.used, state.credit - bytes), .acq_rel, .acquire) orelse return bytes;
        }
    }
};

var mutex: std.Io.Mutex = .init;
var initialized = false;
var managed = std.atomic.Value(bool).init(false);
var admission: Admission = .{};
var ceiling: usize = 0;
var grant_fd: std.posix.fd_t = -1;
var grant_id: u64 = 0;
var sequence: u64 = 0;
var broken = false;
var heap: std.heap.SafeAllocator = .init(page_backing, .{
    .stack_trace_frames = 0,
    .check_write_after_free = false,
});
const page_backing: Allocator = .{ .ptr = &admission, .vtable = &.{
    .alloc = allocPages,
    .resize = Allocator.noResize,
    .remap = Allocator.noRemap,
    .free = freePages,
} };

fn exchange(op: u32, grant: u64, bytes: u64) !u64 {
    if (broken) return error.RuntimeGrantLost;
    sequence = std.math.add(u64, sequence, 1) catch return error.RuntimeGrantProtocol;
    var req: [40]u8 = @splat(0);
    @memcpy(req[0..4], "MGR1");
    std.mem.writeInt(u32, req[4..8], op, .big);
    std.mem.writeInt(u64, req[8..16], sequence, .big);
    std.mem.writeInt(u64, req[16..24], grant, .big);
    std.mem.writeInt(u64, req[24..32], bytes, .big);
    var offset: usize = 0;
    while (offset < req.len) {
        const n = std.c.send(grant_fd, req[offset..].ptr, req.len - offset, std.c.MSG.NOSIGNAL);
        if (n < 0 and std.c.errno(n) == .INTR) continue;
        if (n <= 0) {
            broken = true;
            return error.RuntimeGrantLost;
        }
        offset += @intCast(n);
    }
    var reply: [32]u8 = undefined;
    offset = 0;
    while (offset < reply.len) {
        const n = std.c.read(grant_fd, reply[offset..].ptr, reply.len - offset);
        if (n < 0 and std.c.errno(n) == .INTR) continue;
        if (n <= 0) {
            broken = true;
            return error.RuntimeGrantLost;
        }
        offset += @intCast(n);
    }
    if (!std.mem.eql(u8, reply[0..4], "MGR1") or
        std.mem.readInt(u64, reply[8..16], .big) != sequence or
        std.mem.readInt(u64, reply[24..32], .big) != 0)
    {
        broken = true;
        return error.RuntimeGrantProtocol;
    }
    const result = std.mem.readInt(u32, reply[4..8], .big);
    if (result == 4) return error.QuotaExceeded;
    if (result != 0) return error.RuntimeGrantRejected;
    const value = std.mem.readInt(u64, reply[16..24], .big);
    if ((op == 1 or op == 4) and value == 0) {
        broken = true;
        return error.RuntimeGrantProtocol;
    }
    return value;
}

// Called before application arenas, Rust rendering, or V8 startup. The ordinary
// ungoverned CLI retains its supplied allocator and needs no grant connection.
pub fn allocator(backing: Allocator) !Allocator {
    if (managed.load(.acquire)) return heap.allocator();
    const suspended = Gate.pauseForWait();
    defer Gate.resumeAfterWait(suspended);
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    if (managed.load(.acquire)) return heap.allocator();
    if (initialized) return error.RuntimeGrantLost;
    const raw_fd = std.c.getenv("MIOA_RUNTIME_GRANT_FD") orelse {
        if (std.c.getenv("MIOA_PROFILE_SERVICE_FD") != null) return error.RuntimeGrantMissing;
        return backing;
    };
    initialized = true; // A partial stream exchange must never be retried.
    grant_fd = std.fmt.parseInt(std.posix.fd_t, std.mem.span(raw_fd), 10) catch return error.RuntimeGrantProtocol;
    if (grant_fd <= 2) return error.RuntimeGrantProtocol;
    ceiling = std.math.cast(usize, try exchange(5, 0, 0)) orelse return error.RuntimeGrantProtocol;
    // Exact byte accounting uses two u32 halves, not lossy page units. The
    // pinned owner ceiling (504MiB) fits; larger policies must change the state
    // representation rather than silently truncate admission.
    if (!validCeiling(ceiling)) return error.RuntimeGrantProtocol;
    grant_id = try exchange(1, 0, chunk);
    admission.value.store(Admission.pack(0, chunk), .release);
    @import("../../png.zig").native_allocator = heap.allocator();
    managed.store(true, .release);
    return heap.allocator();
}

pub fn isManaged() bool {
    return managed.load(.acquire);
}

export fn lp_runtime_heap_ceiling() usize {
    return if (isManaged()) ceiling else std.math.maxInt(usize);
}

fn validCeiling(bytes: usize) bool {
    return bytes >= chunk and bytes <= std.math.maxInt(u32) and bytes % chunk == 0;
}

// Called with the slow mutex held, after backing disposal or failed acquisition.
// Retire locally BEFORE sending op6. EOF, rejection, and an uncertain ACK never
// restore credit: the parent may conservatively retain it until process exit.
fn returnUnusedLocked() void {
    if (broken or grant_fd < 0) return;
    const bytes = admission.retireUnused();
    if (bytes == 0) return;
    const acknowledged = exchange(6, grant_id, bytes) catch {
        broken = true;
        return;
    };
    if (acknowledged != grant_id) broken = true;
}

// Atomic fast path per backing allocation, not per Vec/Box/Zig object. Threads
// suspend their own CPG fragment before waiting for the slow-path mutex or IPC.
pub fn reserve(bytes: usize) bool {
    if (!isManaged()) return true;
    if (bytes == 0) return true;
    if (admission.tryReserve(bytes)) return true;
    const suspended = Gate.pauseForWait();
    defer Gate.resumeAfterWait(suspended);
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    while (!admission.tryReserve(bytes)) {
        const state = admission.load();
        if (bytes > ceiling or state.used > ceiling - bytes or state.credit >= ceiling or broken or grant_fd < 0) {
            returnUnusedLocked();
            return false;
        }
        _ = exchange(4, grant_id, chunk) catch {
            // Previous successful growth in this attempt acquired no backing.
            returnUnusedLocked();
            return false;
        };
        admission.addCredit(chunk);
    }
    return true;
}
pub fn release(bytes: usize) void {
    if (!isManaged() or bytes == 0) return;
    // The caller has already unmapped/decommitted the backing, or proved the
    // backing acquisition failed. Live reservations (including realloc overlap
    // and temporary alignment mappings) remain in used until that point.
    const state = admission.releaseUsed(bytes);
    if (state.credit - state.used <= unused_limit) return;
    const suspended = Gate.pauseForWait();
    defer Gate.resumeAfterWait(suspended);
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    returnUnusedLocked();
}
fn pageBytes(len: usize) ?usize {
    const page = std.heap.pageSize();
    const rounded = std.math.add(usize, len, page - 1) catch return null;
    return rounded & ~(page - 1);
}
fn allocPages(_: *anyopaque, len: usize, alignment: Alignment, ret_addr: usize) ?[*]u8 {
    const bytes = pageBytes(len) orelse return null;
    // PageAllocator temporarily maps alignment slack before unmapping it.
    const slack = alignment.toByteUnits() -| std.heap.pageSize();
    const transient = std.math.add(usize, bytes, slack) catch return null;
    if (!reserve(transient)) return null;
    const result = std.heap.page_allocator.rawAlloc(len, alignment, ret_addr) orelse {
        release(transient);
        return null;
    };
    release(slack);
    return result;
}
fn freePages(_: *anyopaque, memory: []u8, alignment: Alignment, ret_addr: usize) void {
    std.heap.page_allocator.rawFree(memory, alignment, ret_addr);
    release(pageBytes(memory.len).?);
}

// Rust, V8 buffers and C-library callbacks share this same physical heap.
// A header remembers allocator provenance even if an early ungoverned object
// survives managed initialization. Realloc must allocate/copy/free, charging
// both old and new backing until the old allocation is actually released.
const Header = extern struct { size: usize, alignment: usize, governed: bool };
export fn lp_runtime_alloc(size: usize, requested_alignment: usize) ?*anyopaque {
    if (requested_alignment == 0 or !std.math.isPowerOfTwo(requested_alignment)) return null;
    const alignment = @max(requested_alignment, @alignOf(Header));
    const prefix = std.mem.alignForward(usize, @sizeOf(Header), alignment);
    const total = std.math.add(usize, prefix, @max(size, 1)) catch return null;
    const governed = isManaged();
    const a = if (governed) heap.allocator() else std.heap.c_allocator;
    const memory = a.rawAlloc(total, Alignment.fromByteUnits(alignment), @returnAddress()) orelse return null;
    const result = memory + prefix;
    const header: *Header = @ptrCast(@alignCast(result - @sizeOf(Header)));
    header.* = .{ .size = total, .alignment = alignment, .governed = governed };
    return result;
}
export fn lp_runtime_free(pointer: ?*anyopaque) void {
    const result: [*]u8 = @ptrCast(pointer orelse return);
    const header: *const Header = @ptrCast(@alignCast(result - @sizeOf(Header)));
    const saved = header.*;
    const prefix = std.mem.alignForward(usize, @sizeOf(Header), saved.alignment);
    const a = if (saved.governed) heap.allocator() else std.heap.c_allocator;
    a.rawFree((result - prefix)[0..saved.size], Alignment.fromByteUnits(saved.alignment), @returnAddress());
}

// C callbacks may run inside an outer network wait. Charge their allocator,
// zeroing and copy work as CPU, and restore the caller's paused state afterward.
fn enterCallback() ?bool {
    return if (isManaged()) Gate.callbackEnter() else null;
}
fn exitCallback(state: ?bool) void {
    if (state) |paused| Gate.callbackExit(paused);
}
export fn lp_runtime_malloc(size: usize) ?*anyopaque {
    const state = enterCallback();
    defer exitCallback(state);
    return lp_runtime_alloc(size, @alignOf(std.c.max_align_t));
}
export fn lp_runtime_size(pointer: *const anyopaque) usize {
    const result: [*]const u8 = @ptrCast(pointer);
    const header: *const Header = @ptrCast(@alignCast(result - @sizeOf(Header)));
    const prefix = std.mem.alignForward(usize, @sizeOf(Header), header.alignment);
    return header.size - prefix;
}
export fn lp_runtime_c_free(pointer: ?*anyopaque) void {
    // Terminal teardown must release backing without another application permit.
    lp_runtime_free(pointer);
}
export fn lp_runtime_calloc(count: usize, size: usize) ?*anyopaque {
    const bytes = std.math.mul(usize, count, size) catch return null;
    const state = enterCallback();
    defer exitCallback(state);
    const result = lp_runtime_alloc(bytes, @alignOf(std.c.max_align_t)) orelse return null;
    @memset(@as([*]u8, @ptrCast(result))[0..bytes], 0);
    return result;
}
export fn lp_runtime_realloc(pointer: ?*anyopaque, size: usize) ?*anyopaque {
    if (size == 0) {
        lp_runtime_free(pointer);
        return null;
    }
    const state = enterCallback();
    defer exitCallback(state);
    const result = lp_runtime_alloc(size, @alignOf(std.c.max_align_t)) orelse return null;
    if (pointer) |old| {
        const bytes = @min(lp_runtime_size(old), size);
        @memcpy(@as([*]u8, @ptrCast(result))[0..bytes], @as([*]const u8, @ptrCast(old))[0..bytes]);
        lp_runtime_free(old);
    }
    return result;
}
export fn lp_runtime_strdup(text: [*:0]const u8) ?[*:0]u8 {
    const state = enterCallback();
    defer exitCallback(state);
    const bytes = std.math.add(usize, std.mem.len(text), 1) catch return null;
    const result: [*]u8 = @ptrCast(lp_runtime_alloc(bytes, @alignOf(std.c.max_align_t)) orelse return null);
    @memcpy(result[0..bytes], text[0..bytes]);
    return @ptrCast(result);
}
export fn lp_runtime_reserve(bytes: usize) bool {
    return reserve(bytes);
}
export fn lp_runtime_release(bytes: usize) void {
    release(bytes);
}
export fn lp_runtime_pause() bool {
    return Gate.pauseForWait();
}
export fn lp_runtime_resume(suspended: bool) void {
    Gate.resumeAfterWait(suspended);
}

// Called only at final process teardown, after application arenas, Rust values,
// and V8 platforms are gone. Caches and process-wide pages may remain physically
// backed: do not invent disposal or force-free anything static teardown can
// observe. Closure retains baseline and all unreturned growth until exact exit.
pub fn shutdown() void {
    if (!isManaged() or grant_fd < 0) return;
    const suspended = Gate.pauseForWait();
    defer Gate.resumeAfterWait(suspended);
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    _ = exchange(2, grant_id, 0) catch return;
    broken = true; // No growth or trusted return may follow baseline closure.
    _ = std.c.close(grant_fd);
    grant_fd = -1;
}

test "packed admission bounds exact bytes by the validated owner ceiling" {
    try std.testing.expect(validCeiling(504 * 1024 * 1024));
    try std.testing.expect(!validCeiling(chunk - 1));
    try std.testing.expect(!validCeiling(chunk + 1));
    try std.testing.expect(!validCeiling(std.math.maxInt(u32)));
    if (comptime @sizeOf(usize) > 4) {
        try std.testing.expect(!validCeiling(@as(usize, std.math.maxInt(u32)) + 1));
    }
    const maximum: usize = std.math.maxInt(u32) / chunk * chunk;
    try std.testing.expect(validCeiling(maximum));
    var state: Admission = .{};
    state.value.store(Admission.pack(0, maximum), .release);
    try std.testing.expect(state.tryReserve(maximum));
    try std.testing.expect(!state.tryReserve(1));
    try std.testing.expect(!state.tryReserve(std.math.maxInt(usize)));
    _ = state.releaseUsed(1);
    try std.testing.expect(state.tryReserve(1));
    try std.testing.expectEqual(maximum, state.load().used);
    try std.testing.expectEqual(maximum, state.load().credit);
}

test "alignment disposal and failed acquisition retire only whole growth blocks" {
    const live = 60 * 1024 * 1024;
    const slack = 256 * 1024 * 1024;
    var state: Admission = .{};
    state.value.store(Admission.pack(live + slack, live + slack), .release);
    _ = state.releaseUsed(slack);
    try std.testing.expectEqual(@as(usize, 252 * 1024 * 1024), state.retireUnused());
    try std.testing.expectEqual(@as(usize, live), state.load().used);
    try std.testing.expectEqual(@as(usize, 64 * 1024 * 1024), state.load().credit);
    // Retiring again cannot duplicate returned credit; disposing the remaining
    // backing leaves the original baseline, not zero admitted bytes.
    try std.testing.expectEqual(@as(usize, 0), state.retireUnused());
    _ = state.releaseUsed(live);
    try std.testing.expectEqual(@as(usize, 60 * 1024 * 1024), state.retireUnused());
    try std.testing.expectEqual(@as(usize, 0), state.load().used);
    try std.testing.expectEqual(@as(usize, chunk), state.load().credit);
    try std.testing.expectEqual(@as(usize, 0), state.retireUnused());
    // A failed backing acquisition releases its entire provisional reservation,
    // not merely the alignment slack. Only baseline remains afterward.
    state.value.store(Admission.pack(slack, slack), .release);
    _ = state.releaseUsed(slack);
    try std.testing.expectEqual(@as(usize, slack - chunk), state.retireUnused());
    try std.testing.expectEqual(@as(usize, 0), state.load().used);
    try std.testing.expectEqual(@as(usize, chunk), state.load().credit);
}

test "retirement boundary preserves baseline and at most eight MiB unused" {
    var state: Admission = .{};
    state.value.store(Admission.pack(0, chunk), .release);
    try std.testing.expectEqual(@as(usize, 0), state.retireUnused());
    state.value.store(Admission.pack(chunk, 3 * chunk), .release);
    try std.testing.expectEqual(@as(usize, 0), state.retireUnused());
    _ = state.releaseUsed(1);
    try std.testing.expectEqual(@as(usize, chunk), state.retireUnused());
    try std.testing.expectEqual(@as(usize, chunk - 1), state.load().used);
    try std.testing.expectEqual(@as(usize, 2 * chunk), state.load().credit);
    try std.testing.expect(state.load().credit - state.load().used <= unused_limit);
    // Arbitrary byte reservations, including an in-flight backing acquisition,
    // remain charged; rounding retirement must never borrow their final byte.
    state.value.store(Admission.pack(3 * chunk + 1, 8 * chunk), .release);
    try std.testing.expectEqual(@as(usize, 3 * chunk), state.retireUnused());
    try std.testing.expectEqual(@as(usize, 3 * chunk + 1), state.load().used);
    try std.testing.expectEqual(@as(usize, 5 * chunk), state.load().credit);
}

test "a concurrent stale reservation cannot spend retired credit" {
    const Race = struct {
        state: Admission = .{},
        ready: std.atomic.Value(bool) = .init(false),
        proceed: std.atomic.Value(bool) = .init(false),
        reserved: bool = false,

        fn run(self: *@This()) void {
            const snapshot = self.state.value.load(.acquire);
            self.ready.store(true, .release);
            while (!self.proceed.load(.acquire)) std.atomic.spinLoopHint();
            self.reserved = self.state.reserveFrom(3 * chunk, snapshot);
        }
    };
    var race: Race = .{};
    race.state.value.store(Admission.pack(chunk, 5 * chunk), .release);
    const thread = try std.Thread.spawn(.{}, Race.run, .{&race});
    while (!race.ready.load(.acquire)) std.atomic.spinLoopHint();
    // Worker holds a snapshot that could admit its request. Physically disposed
    // credit is retired before its actual CAS, forcing it to recheck both halves.
    const returned = race.state.retireUnused();
    race.proceed.store(true, .release);
    thread.join();
    try std.testing.expectEqual(@as(usize, 3 * chunk), returned);
    try std.testing.expect(!race.reserved);
    try std.testing.expectEqual(@as(usize, chunk), race.state.load().used);
    try std.testing.expectEqual(@as(usize, 2 * chunk), race.state.load().credit);
}

test "a concurrent stale retirement cannot return active backing credit" {
    const Race = struct {
        state: Admission = .{},
        ready: std.atomic.Value(bool) = .init(false),
        proceed: std.atomic.Value(bool) = .init(false),
        returned: usize = 0,

        fn run(self: *@This()) void {
            const snapshot = self.state.value.load(.acquire);
            self.ready.store(true, .release);
            while (!self.proceed.load(.acquire)) std.atomic.spinLoopHint();
            self.returned = self.state.retireFrom(snapshot);
        }
    };
    var race: Race = .{};
    race.state.value.store(Admission.pack(chunk, 5 * chunk), .release);
    const thread = try std.Thread.spawn(.{}, Race.run, .{&race});
    while (!race.ready.load(.acquire)) std.atomic.spinLoopHint();
    const reserved = race.state.tryReserve(3 * chunk);
    race.proceed.store(true, .release);
    thread.join();
    try std.testing.expect(reserved);
    try std.testing.expectEqual(@as(usize, 0), race.returned);
    try std.testing.expectEqual(@as(usize, 4 * chunk), race.state.load().used);
    try std.testing.expectEqual(@as(usize, 5 * chunk), race.state.load().credit);
}

test "C callbacks preserve bytes and ownership when realloc fails" {
    const original = lp_runtime_malloc(32) orelse return error.OutOfMemory;
    var live = original;
    defer lp_runtime_c_free(live);
    const bytes: [*]u8 = @ptrCast(live);
    for (bytes[0..32], 0..) |*byte, index| byte.* = @intCast(index);
    try std.testing.expect(lp_runtime_realloc(live, std.math.maxInt(usize)) == null);
    try std.testing.expectEqual(@as(usize, 32), lp_runtime_size(live));
    for (bytes[0..32], 0..) |byte, index| try std.testing.expectEqual(@as(u8, @intCast(index)), byte);
    live = lp_runtime_realloc(live, 64) orelse return error.OutOfMemory;
    try std.testing.expectEqual(@as(usize, 64), lp_runtime_size(live));
    live = lp_runtime_realloc(live, 7) orelse return error.OutOfMemory;
    const shrunk: [*]const u8 = @ptrCast(live);
    for (shrunk[0..7], 0..) |byte, index| try std.testing.expectEqual(@as(u8, @intCast(index)), byte);
}

test "C callbacks check calloc overflow and copy the strdup terminator" {
    try std.testing.expect(lp_runtime_calloc(std.math.maxInt(usize), 2) == null);
    const zeroed = lp_runtime_calloc(4, 13) orelse return error.OutOfMemory;
    defer lp_runtime_c_free(zeroed);
    try std.testing.expectEqualSlices(u8, &@as([52]u8, @splat(0)), @as([*]const u8, @ptrCast(zeroed))[0..52]);
    const copied = lp_runtime_strdup("native heap") orelse return error.OutOfMemory;
    defer lp_runtime_c_free(copied);
    try std.testing.expectEqualStrings("native heap", std.mem.span(copied));
    try std.testing.expectEqual(@as(u8, 0), copied[11]);
    const released = lp_runtime_malloc(1) orelse return error.OutOfMemory;
    try std.testing.expect(lp_runtime_realloc(released, 0) == null);
}

test "an early C allocation retains its libc owner after managed cutover" {
    if (isManaged()) return error.SkipZigTest;
    const early = lp_runtime_malloc(37) orelse return error.OutOfMemory;
    managed.store(true, .release);
    defer managed.store(false, .release);
    // The managed heap never allocated this pointer. Dispatching free by the
    // current mode rather than the saved header would corrupt that heap.
    lp_runtime_free(early);
}

test "pinned BoringSSL keeps its prefix and failed realloc ownership" {
    const tls = struct {
        extern "c" fn OPENSSL_malloc(usize) ?*anyopaque;
        extern "c" fn OPENSSL_realloc(?*anyopaque, usize) ?*anyopaque;
        extern "c" fn OPENSSL_free(?*anyopaque) void;
    };
    const pointer = tls.OPENSSL_malloc(33) orelse return error.OutOfMemory;
    defer tls.OPENSSL_free(pointer);
    const bytes: [*]u8 = @ptrCast(pointer);
    @memset(bytes[0..33], 0xa7);
    // The pinned implementation owns an eight-byte prefix above our shared
    // heap header. This fails if its mem.cc silently reverts to libc malloc.
    try std.testing.expectEqual(@as(usize, 41), lp_runtime_size(@ptrCast(bytes - 8)));
    try std.testing.expect(tls.OPENSSL_realloc(pointer, std.math.maxInt(usize)) == null);
    for (bytes[0..33]) |byte| try std.testing.expectEqual(@as(u8, 0xa7), byte);
}

test "compression heap denial preserves native realloc ownership" {
    if (isManaged()) return error.SkipZigTest;
    const native = struct {
        const Mem = opaque {};
        extern "c" fn zcalloc(?*anyopaque, c_uint, c_uint) ?*anyopaque;
        extern "c" fn zcfree(?*anyopaque, ?*anyopaque) void;
        extern "c" fn BrotliDefaultAllocFunc(?*anyopaque, usize) ?*anyopaque;
        extern "c" fn BrotliDefaultFreeFunc(?*anyopaque, ?*anyopaque) void;
        extern "c" fn ZSTD_createDDict(*const anyopaque, usize) ?*anyopaque;
        extern "c" fn ZSTD_freeDDict(?*anyopaque) usize;
        extern "c" fn nghttp2_mem_default() *Mem;
        extern "c" fn nghttp2_mem_calloc(*Mem, usize, usize) ?*anyopaque;
        extern "c" fn nghttp2_mem_realloc(*Mem, ?*anyopaque, usize) ?*anyopaque;
        extern "c" fn nghttp2_mem_free(*Mem, ?*anyopaque) void;
    };
    const mem = native.nghttp2_mem_default();
    var original: ?*anyopaque = native.nghttp2_mem_calloc(mem, 8, 4) orelse return error.OutOfMemory;
    defer native.nghttp2_mem_free(mem, original);
    const bytes: [*]u8 = @ptrCast(original.?);
    try std.testing.expectEqualSlices(u8, &@as([32]u8, @splat(0)), bytes[0..32]);
    @memset(bytes[0..32], 0x6b);

    // No new backing is admitted. Large requests cannot reuse a small-object
    // bucket; denial must precede allocation, not charge logical bytes later.
    const previous_admission = admission.value.load(.acquire);
    const previous_ceiling = ceiling;
    const previous_used = Admission.unpack(previous_admission).used;
    admission.value.store(Admission.pack(previous_used, previous_used), .release);
    ceiling = previous_used;
    managed.store(true, .release);
    defer {
        managed.store(false, .release);
        admission.value.store(previous_admission, .release);
        ceiling = previous_ceiling;
    }
    const large = 64 * 1024 * 1024;
    const zallocation = native.zcalloc(null, 1, large);
    if (zallocation) |pointer| native.zcfree(null, pointer);
    try std.testing.expect(zallocation == null);
    const ballocation = native.BrotliDefaultAllocFunc(null, large);
    if (ballocation) |pointer| native.BrotliDefaultFreeFunc(null, pointer);
    try std.testing.expect(ballocation == null);
    const grown = native.nghttp2_mem_realloc(mem, original, large);
    if (grown) |pointer| original = pointer;
    try std.testing.expect(grown == null);
    for (bytes[0..32]) |byte| try std.testing.expectEqual(@as(u8, 0x6b), byte);
    // Raw dictionaries are valid without a zstd dictionary header. A fallback
    // to libc would copy and retain this buffer despite the backing denial.
    const dictionary: [256 * 1024]u8 = @splat(0x5a);
    const ddict = native.ZSTD_createDDict(&dictionary, dictionary.len);
    if (ddict) |pointer| _ = native.ZSTD_freeDDict(pointer);
    try std.testing.expect(ddict == null);
    native.nghttp2_mem_free(mem, original);
    original = null;
    try std.testing.expectEqual(previous_used, admission.load().used);
}

// Test callbacks retain their own ownership even when the built-in fallback
// allocator is redirected. Failure after one success exercises partial cleanup.
const CompressionAllocatorProbe = struct {
    calls: usize = 0,
    live: usize = 0,

    fn allow(self: *@This()) bool {
        self.calls += 1;
        return self.calls == 1;
    }
    fn alloc(context: ?*anyopaque, size: usize) callconv(.c) ?*anyopaque {
        const self: *@This() = @ptrCast(@alignCast(context.?));
        if (!self.allow()) return null;
        const pointer = lp_runtime_malloc(size) orelse return null;
        self.live += 1;
        return pointer;
    }
    fn free(context: ?*anyopaque, pointer: ?*anyopaque) callconv(.c) void {
        if (pointer == null) return;
        const self: *@This() = @ptrCast(@alignCast(context.?));
        self.live -= 1;
        lp_runtime_c_free(pointer);
    }
    fn zalloc(context: ?*anyopaque, count: c_uint, size: c_uint) callconv(.c) ?*anyopaque {
        return calloc(count, size, context);
    }
    fn zfree(context: ?*anyopaque, pointer: ?*anyopaque) callconv(.c) void {
        free(context, pointer);
    }
    fn malloc(size: usize, context: ?*anyopaque) callconv(.c) ?*anyopaque {
        return alloc(context, size);
    }
    fn cfree(pointer: ?*anyopaque, context: ?*anyopaque) callconv(.c) void {
        free(context, pointer);
    }
    fn calloc(count: usize, size: usize, context: ?*anyopaque) callconv(.c) ?*anyopaque {
        const length = std.math.mul(usize, count, size) catch return null;
        const pointer = alloc(context, length) orelse return null;
        @memset(@as([*]u8, @ptrCast(pointer))[0..length], 0);
        return pointer;
    }
    fn realloc(pointer: ?*anyopaque, size: usize, context: ?*anyopaque) callconv(.c) ?*anyopaque {
        if (pointer == null) return alloc(context, size);
        if (size == 0) {
            free(context, pointer);
            return null;
        }
        const self: *@This() = @ptrCast(@alignCast(context.?));
        if (!self.allow()) return null;
        return lp_runtime_realloc(pointer, size);
    }
};

test "zlib and Brotli preserve supplied allocators on partial initialization failure" {
    const zlib = @import("../../../sys/zlib.zig");
    const brotli = @import("../../../sys/brotli.zig");
    var zprobe: CompressionAllocatorProbe = .{};
    var stream: zlib.z_stream = .{
        .zalloc = CompressionAllocatorProbe.zalloc,
        .zfree = CompressionAllocatorProbe.zfree,
        .@"opaque" = &zprobe,
    };
    const result = zlib.deflateInit2_(&stream, -1, zlib.Z_DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY, zlib.zlibVersion(), @sizeOf(zlib.z_stream));
    if (result == zlib.Z_OK) _ = zlib.deflateEnd(&stream);
    try std.testing.expectEqual(zlib.Z_MEM_ERROR, result);
    try std.testing.expect(zprobe.calls > 1);
    try std.testing.expectEqual(@as(usize, 0), zprobe.live);

    var bprobe: CompressionAllocatorProbe = .{};
    const decoder = brotli.BrotliDecoderCreateInstance(CompressionAllocatorProbe.alloc, CompressionAllocatorProbe.free, &bprobe);
    if (decoder) |state| brotli.BrotliDecoderDestroyInstance(state);
    try std.testing.expect(decoder == null);
    try std.testing.expect(bprobe.calls > 1);
    try std.testing.expectEqual(@as(usize, 0), bprobe.live);

    var eprobe: CompressionAllocatorProbe = .{};
    const encoder = brotli.BrotliEncoderCreateInstance(CompressionAllocatorProbe.alloc, CompressionAllocatorProbe.free, &eprobe) orelse return error.OutOfMemory;
    const input_text = "encoder allocation boundary";
    var input: [*c]const u8 = input_text;
    var input_left: usize = input_text.len;
    var output: [128]u8 = undefined;
    var next_output: [*c]u8 = &output;
    var output_left: usize = output.len;
    const encoded = brotli.BrotliEncoderCompressStream(encoder, .finish, &input_left, &input, &output_left, &next_output, null);
    brotli.BrotliEncoderDestroyInstance(encoder);
    try std.testing.expectEqual(brotli.BROTLI_FALSE, encoded);
    try std.testing.expect(eprobe.calls > 1);
    try std.testing.expectEqual(@as(usize, 0), eprobe.live);
}

test "zstd and nghttp2 preserve custom allocator cleanup after denial" {
    const native = struct {
        const CustomMem = extern struct {
            alloc: *const fn (?*anyopaque, usize) callconv(.c) ?*anyopaque,
            free: *const fn (?*anyopaque, ?*anyopaque) callconv(.c) void,
            context: ?*anyopaque,
        };
        const HttpMem = extern struct {
            context: ?*anyopaque,
            malloc: *const fn (usize, ?*anyopaque) callconv(.c) ?*anyopaque,
            free: *const fn (?*anyopaque, ?*anyopaque) callconv(.c) void,
            calloc: *const fn (usize, usize, ?*anyopaque) callconv(.c) ?*anyopaque,
            realloc: *const fn (?*anyopaque, usize, ?*anyopaque) callconv(.c) ?*anyopaque,
        };
        extern "c" fn ZSTD_createDDict_advanced(*const anyopaque, usize, c_int, c_int, CustomMem) ?*anyopaque;
        extern "c" fn ZSTD_freeDDict(?*anyopaque) usize;
        extern "c" fn nghttp2_session_callbacks_new(*?*anyopaque) c_int;
        extern "c" fn nghttp2_session_callbacks_del(?*anyopaque) void;
        extern "c" fn nghttp2_session_client_new3(*?*anyopaque, *const anyopaque, ?*anyopaque, ?*const anyopaque, *HttpMem) c_int;
        extern "c" fn nghttp2_session_del(?*anyopaque) void;
    };
    var zprobe: CompressionAllocatorProbe = .{};
    const dictionary = "raw dictionary allocation boundary";
    const ddict = native.ZSTD_createDDict_advanced(dictionary, dictionary.len, 0, 1, .{
        .alloc = CompressionAllocatorProbe.alloc,
        .free = CompressionAllocatorProbe.free,
        .context = &zprobe,
    });
    if (ddict) |pointer| _ = native.ZSTD_freeDDict(pointer);
    try std.testing.expect(ddict == null);
    try std.testing.expect(zprobe.calls > 1);
    try std.testing.expectEqual(@as(usize, 0), zprobe.live);

    var callbacks: ?*anyopaque = null;
    try std.testing.expectEqual(@as(c_int, 0), native.nghttp2_session_callbacks_new(&callbacks));
    defer native.nghttp2_session_callbacks_del(callbacks);
    var hprobe: CompressionAllocatorProbe = .{};
    var mem: native.HttpMem = .{
        .context = &hprobe,
        .malloc = CompressionAllocatorProbe.malloc,
        .free = CompressionAllocatorProbe.cfree,
        .calloc = CompressionAllocatorProbe.calloc,
        .realloc = CompressionAllocatorProbe.realloc,
    };
    var session: ?*anyopaque = null;
    const result = native.nghttp2_session_client_new3(&session, callbacks.?, null, null, &mem);
    if (result == 0) native.nghttp2_session_del(session);
    try std.testing.expectEqual(@as(c_int, -901), result); // NGHTTP2_ERR_NOMEM
    try std.testing.expect(hprobe.calls > 1);
    try std.testing.expectEqual(@as(usize, 0), hprobe.live);
}
