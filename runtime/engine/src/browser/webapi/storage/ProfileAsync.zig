// Independent, authenticated Profile channel for IndexedDB. All methods and
// callbacks run on the Session's event-loop thread.
const std = @import("std");
const lp = @import("lightpanda");
const Allocator = std.mem.Allocator;

const max_message = 1024 * 1024;
const max_buffered = 4 * 1024 * 1024;
const max_requests = 64;
const timeout_ms = 30_000;

pub const Result = union(enum) {
    reply: std.json.Parsed(std.json.Value),
    failure: anyerror,
};
pub const Callback = *const fn (*anyopaque, Result) void;

const Request = struct {
    id: u64,
    bytes: ?[]u8,
    offset: usize = 0,
    dispatched: bool = false,
    deadline: u64,
    context: *anyopaque,
    callback: ?Callback,
};

pub const Client = struct {
    fd: std.posix.fd_t,
    allocator: Allocator,
    next_id: u64 = 1,
    failed: bool = false,
    requests: [max_requests]?Request = @splat(null),
    buffered: usize = 0,
    prefix: [4]u8 = undefined,
    prefix_used: usize = 0,
    response: ?[]u8 = null,
    response_used: usize = 0,

    pub fn init(fd: std.posix.fd_t, allocator: Allocator) Client {
        return .{ .fd = fd, .allocator = allocator };
    }

    pub fn submit(self: *Client, operation: []const u8, payload: anytype, context: *anyopaque, callback: Callback) !u64 {
        if (self.failed or self.fd < 0) return error.ProfileServiceLost;
        if (self.next_id >= std.math.maxInt(i64)) return error.ProfileRequestExhausted;
        var slot: ?usize = null;
        for (self.requests, 0..) |request, i| {
            if (request == null) {
                slot = i;
                break;
            }
        }
        const index = slot orelse return error.ProfileBackpressure;
        const id = self.next_id;
        const message = try std.json.Stringify.valueAlloc(self.allocator, .{
            .version = @as(u8, 1),
            .id = id,
            .operation = operation,
            .payload = payload,
        }, .{});
        defer self.allocator.free(message);
        if (message.len == 0 or message.len > max_message) return error.ProfileMessageTooLarge;
        const length = message.len + 4;
        if (length > max_buffered - self.buffered) return error.ProfileBackpressure;
        const frame = try self.allocator.alloc(u8, length);
        std.mem.writeInt(u32, frame[0..4], @intCast(message.len), .big);
        @memcpy(frame[4..], message);
        self.requests[index] = .{ .id = id, .bytes = frame, .deadline = lp.datetime.milliTimestamp(.boot) + timeout_ms, .context = context, .callback = callback };
        self.buffered += length;
        self.next_id += 1;
        return id;
    }

    pub fn hasPending(self: *const Client) bool {
        for (self.requests) |request| if (request != null) {
            return true;
        };
        return false;
    }

    // Wire tombstones have no remaining owner callback.
    pub fn hasCallbacks(self: *const Client) bool {
        for (self.requests) |request| if (request) |r| {
            if (r.callback != null) return true;
        };
        return false;
    }

    pub fn wantsWrite(self: *const Client) bool {
        if (self.failed) return false;
        for (self.requests) |request| if (request) |r| {
            if (r.bytes != null) return true;
        };
        return false;
    }

    fn take(self: *Client, index: usize) Request {
        const request = self.requests[index].?;
        self.requests[index] = null;
        if (request.bytes) |bytes| {
            self.buffered -= bytes.len;
            self.allocator.free(bytes);
        }
        return request;
    }

    fn fail(self: *Client, reason: anyerror) void {
        if (self.failed) return;
        self.failed = true;
        if (self.fd >= 0) _ = std.c.close(self.fd);
        self.fd = -1;
        if (self.response) |bytes| self.allocator.free(bytes);
        self.response = null;
        self.prefix_used = 0;
        self.response_used = 0;
        for (0..max_requests) |i| {
            if (self.requests[i] == null) continue;
            const request = self.take(i);
            if (request.callback) |callback| callback(request.context, .{ .failure = if (request.dispatched) error.ProfileSideEffectUnknown else reason });
        }
    }

    pub fn deinit(self: *Client) void {
        self.fail(error.ProfileServiceLost);
    }

    pub fn cancel(self: *Client, id: u64) void {
        for (self.requests, 0..) |maybe_request, i| {
            const request = maybe_request orelse continue;
            if (request.id != id) continue;
            const callback = request.callback orelse return;
            if (request.dispatched) {
                // Keep the frame and ID until the reply is drained. The owner
                // may disappear now, but the stream must remain synchronized.
                self.requests[i].?.callback = null;
                callback(request.context, .{ .failure = error.ProfileSideEffectUnknown });
            } else {
                const removed = self.take(i);
                callback(removed.context, .{ .failure = error.Abort });
            }
            return;
        }
    }

    pub fn timeoutMillis(self: *const Client) ?u32 {
        const now = lp.datetime.milliTimestamp(.boot);
        var remaining: ?u64 = null;
        for (self.requests) |maybe_request| {
            const request = maybe_request orelse continue;
            const left = request.deadline -| now;
            remaining = @min(remaining orelse left, left);
        }
        return if (remaining) |ms| @intCast(@min(ms, std.math.maxInt(u32))) else null;
    }

    pub fn onTimeout(self: *Client) void {
        @import("../../TaskGate.zig").enterOrExit();
        defer @import("../../TaskGate.zig").exit();
        const now = lp.datetime.milliTimestamp(.boot);
        for (0..max_requests) |i| {
            const request = self.requests[i] orelse continue;
            if (request.deadline > now) continue;
            if (request.dispatched) {
                self.fail(error.ProfileTimeout);
                return;
            }
            const removed = self.take(i);
            if (removed.callback) |callback| callback(removed.context, .{ .failure = error.ProfileTimeout });
        }
    }

    pub fn onWritable(self: *Client) void {
        @import("../../TaskGate.zig").enterOrExit();
        defer @import("../../TaskGate.zig").exit();
        var attempts: usize = 0;
        while (!self.failed and attempts < max_requests) : (attempts += 1) {
            var index: ?usize = null;
            var first: u64 = std.math.maxInt(u64);
            for (self.requests, 0..) |maybe_request, i| {
                const request = maybe_request orelse continue;
                if (request.bytes != null and request.id < first) {
                    first = request.id;
                    index = i;
                }
            }
            const i = index orelse return;
            const request = &self.requests[i].?;
            const bytes = request.bytes.?;
            const n = std.c.send(self.fd, bytes.ptr + request.offset, bytes.len - request.offset, std.c.MSG.DONTWAIT | std.c.MSG.NOSIGNAL);
            if (n < 0) switch (std.c.errno(n)) {
                .AGAIN => return,
                .INTR => continue,
                else => {
                    self.fail(error.ProfileServiceLost);
                    return;
                },
            };
            if (n == 0) {
                self.fail(error.ProfileServiceLost);
                return;
            }
            request.dispatched = true;
            request.offset += @intCast(n);
            if (request.offset == bytes.len) {
                self.buffered -= bytes.len;
                self.allocator.free(bytes);
                request.bytes = null;
            }
        }
    }

    pub fn onReadable(self: *Client) void {
        @import("../../TaskGate.zig").enterOrExit();
        defer @import("../../TaskGate.zig").exit();
        var attempts: usize = 0;
        while (!self.failed and attempts < max_requests) : (attempts += 1) {
            const target = if (self.response) |body| body[self.response_used..] else self.prefix[self.prefix_used..];
            const n = std.c.recv(self.fd, target.ptr, target.len, std.c.MSG.DONTWAIT);
            if (n < 0) switch (std.c.errno(n)) {
                .AGAIN => return,
                .INTR => continue,
                else => {
                    self.fail(error.ProfileServiceLost);
                    return;
                },
            };
            if (n == 0) {
                self.fail(error.ProfileServiceLost);
                return;
            }
            if (self.response == null) {
                self.prefix_used += @intCast(n);
                if (self.prefix_used != 4) continue;
                const size = std.mem.readInt(u32, &self.prefix, .big);
                if (size == 0 or size > max_message) {
                    self.fail(error.ProfileProtocolError);
                    return;
                }
                self.response = self.allocator.alloc(u8, size) catch {
                    self.fail(error.OutOfMemory);
                    return;
                };
                self.response_used = 0;
            } else {
                self.response_used += @intCast(n);
                if (self.response_used != self.response.?.len) continue;
                const bytes = self.response.?;
                self.response = null;
                self.response_used = 0;
                self.prefix_used = 0;
                const parsed = std.json.parseFromSlice(std.json.Value, self.allocator, bytes, .{ .allocate = .alloc_always }) catch {
                    self.allocator.free(bytes);
                    self.fail(error.ProfileProtocolError);
                    return;
                };
                self.allocator.free(bytes);
                self.deliver(parsed);
            }
        }
    }

    fn deliver(self: *Client, parsed: std.json.Parsed(std.json.Value)) void {
        if (parsed.value != .object) {
            parsed.deinit();
            self.fail(error.ProfileProtocolError);
            return;
        }
        const fields = parsed.value.object;
        const id = fields.get("id") orelse {
            parsed.deinit();
            self.fail(error.ProfileProtocolError);
            return;
        };
        const ok = fields.get("ok") orelse {
            parsed.deinit();
            self.fail(error.ProfileProtocolError);
            return;
        };
        if (id != .integer or id.integer < 0 or ok != .bool or (ok.bool and fields.get("result") == null)) {
            parsed.deinit();
            self.fail(error.ProfileProtocolError);
            return;
        }
        for (self.requests, 0..) |maybe_request, i| {
            const request = maybe_request orelse continue;
            if (request.id != @as(u64, @intCast(id.integer))) continue;
            if (!request.dispatched or request.bytes != null) {
                parsed.deinit();
                self.fail(error.ProfileProtocolError);
                return;
            }
            const removed = self.take(i);
            const callback = removed.callback orelse {
                parsed.deinit();
                return;
            };
            if (ok.bool) {
                callback(removed.context, .{ .reply = parsed });
            } else {
                const reason = responseError(fields);
                parsed.deinit();
                callback(removed.context, .{ .failure = reason });
            }
            return;
        }
        parsed.deinit();
        self.fail(error.ProfileProtocolError);
    }
};

fn responseError(fields: std.json.ObjectMap) anyerror {
    if (fields.get("effect")) |effect| {
        if (effect == .string and std.mem.eql(u8, effect.string, "unknown")) return error.ProfileSideEffectUnknown;
    }
    if (fields.get("error_code")) |code| {
        if (code == .string) {
            inline for (.{
                error.Constraint,          error.NotFound,             error.Abort,                error.Auth,                error.Busy,                   error.QuotaExceeded,
                error.CantOpen,            error.Corrupt,              error.Empty,                error.Error,               error.Format,                 error.Full,
                error.Internal,            error.Interrupt,            error.IoErr,                error.Locked,              error.Mismatch,               error.Misuse,
                error.NoLFS,               error.NoMem,                error.NotADB,               error.Notfound,            error.Notice,                 error.Perm,
                error.Protocol,            error.Range,                error.ReadOnly,             error.Schema,              error.TooBig,                 error.Warning,
                error.ConstraintCheck,     error.ConstraintCommithook, error.ConstraintForeignKey, error.ConstraintFunction,  error.ConstraintNotNull,      error.ConstraintPrimaryKey,
                error.ConstraintTrigger,   error.ConstraintUnique,     error.ConstraintVTab,       error.ConstraintRowId,     error.ConstraintPinned,       error.ConstraintDatatype,
                error.ErrorMissingCollseq, error.ErrorRetry,           error.ErrorSnapshot,        error.IoerrRead,           error.IoerrShortRead,         error.IoerrWrite,
                error.IoerrFsync,          error.IoerrDir_fsync,       error.IoerrTruncate,        error.IoerrFstat,          error.IoerrUnlock,            error.IoerrRdlock,
                error.IoerrDelete,         error.IoerrBlocked,         error.IoerrNomem,           error.IoerrAccess,         error.IoerrCheckreservedlock, error.IoerrLock,
                error.IoerrClose,          error.IoerrDirClose,        error.IoerrShmopen,         error.IoerrShmsize,        error.IoerrShmlock,           error.IoerrShmmap,
                error.IoerrSeek,           error.IoerrDeleteNoent,     error.IoerrMmap,            error.IoerrGetTempPath,    error.IoerrConvPath,          error.IoerrVnode,
                error.IoerrAuth,           error.IoerrBeginAtomic,     error.IoerrCommitAtomic,    error.IoerrRollbackAtomic, error.IoerrData,              error.IoerrCorruptFS,
                error.LockedSharedCache,   error.LockedVTab,           error.BusyRecovery,         error.BusySnapshot,        error.BusyTimeout,            error.CantOpenNoTempDir,
                error.CantOpenIsDir,       error.CantOpenFullPath,     error.CantOpenConvPath,     error.CantOpenDirtyWal,    error.CantOpenSymlink,        error.CorruptVTab,
                error.CorruptSequence,     error.CorruptIndex,         error.ReadonlyRecovery,     error.ReadonlyCantlock,    error.ReadonlyRollback,       error.ReadonlyDbMoved,
                error.ReadonlyCantInit,    error.ReadonlyDirectory,    error.AbortRollback,        error.NoticeRecoverWal,    error.NoticeRecoverRollback,  error.WarningAutoIndex,
                error.AuthUser,            error.OkLoadPermanently,    error.Unknown,              error.OutOfMemory,         error.InvalidArgument,        error.UnknownError,
            }) |known| {
                if (std.mem.eql(u8, code.string, @errorName(known))) return known;
            }
        }
    }
    return error.ProfileRequestRejected;
}

test "cancellation and teardown synchronously resolve live callbacks" {
    const State = struct {
        count: usize = 0,
        failure: ?anyerror = null,

        fn complete(ctx: *anyopaque, result: Result) void {
            const state: *@This() = @ptrCast(@alignCast(ctx));
            state.count += 1;
            switch (result) {
                .failure => |err| state.failure = err,
                .reply => |reply| reply.deinit(),
            }
        }
    };
    var client = Client.init(-1, std.testing.allocator);
    var unsent = State{};
    var sent = State{};
    var remaining = State{};
    client.requests[0] = .{ .id = 1, .bytes = null, .deadline = 0, .context = &unsent, .callback = State.complete };
    client.requests[1] = .{ .id = 2, .bytes = null, .dispatched = true, .deadline = 0, .context = &sent, .callback = State.complete };
    client.requests[2] = .{ .id = 3, .bytes = null, .deadline = 0, .context = &remaining, .callback = State.complete };
    client.cancel(1);
    try std.testing.expectEqual(@as(usize, 1), unsent.count);
    try std.testing.expectEqual(error.Abort, unsent.failure.?);
    client.cancel(2);
    try std.testing.expectEqual(@as(usize, 1), sent.count);
    try std.testing.expectEqual(error.ProfileSideEffectUnknown, sent.failure.?);
    try std.testing.expect(client.hasCallbacks());
    client.deinit();
    try std.testing.expectEqual(@as(usize, 1), remaining.count);
    try std.testing.expectEqual(error.ProfileServiceLost, remaining.failure.?);
    try std.testing.expectEqual(@as(usize, 1), sent.count);
    try std.testing.expect(!client.hasCallbacks());
    try std.testing.expect(!client.hasPending());
}
