// Private trusted-runtime CPU admission. One serialized I/O owner and one
// aggregate process fragment; overlapping Worker CPU is summed by the host.
const std = @import("std");
const lp = @import("lightpanda");
const ProcessApi = @import("process_api");

pub const Error = error{
    RuntimeTaskGateLost,
    RuntimeTaskGateMissing,
    RuntimeTaskGateInvalid,
    RuntimeTaskGateExhausted,
};

var mutex: std.Io.Mutex = .init;
var initialized = false;
var failed = false;
var fd: std.posix.fd_t = -1;
var sequence: u64 = 0;
var active: usize = 0;
var task: u64 = 0;
threadlocal var depth: usize = 0;
threadlocal var paused = false;

fn initialize() !void {
    if (failed) return error.RuntimeTaskGateLost;
    if (initialized) return;
    initialized = true;
    const text = std.c.getenv("MIOA_RUNTIME_TASK_FD") orelse {
        if (std.c.getenv("MIOA_PROFILE_SERVICE_FD") != null) {
            failed = true;
            return error.RuntimeTaskGateMissing;
        }
        return;
    };
    fd = std.fmt.parseInt(std.posix.fd_t, std.mem.span(text), 10) catch {
        failed = true;
        return error.RuntimeTaskGateInvalid;
    };
    if (fd <= 2) {
        failed = true;
        return error.RuntimeTaskGateInvalid;
    }
}

fn exchange(operation: u32, identity: u64) !void {
    if (failed) return error.RuntimeTaskGateLost;
    sequence = std.math.add(u64, sequence, 1) catch return error.RuntimeTaskGateExhausted;
    var request: [32]u8 = @splat(0);
    @memcpy(request[0..4], "CPG1");
    std.mem.writeInt(u32, request[4..8], operation, .little);
    std.mem.writeInt(u64, request[8..16], sequence, .little);
    std.mem.writeInt(u64, request[16..24], identity, .little);
    // A hint, not measured consumption. The host independently samples CPU.
    std.mem.writeInt(u64, request[24..32], if (operation == 1) 1_000_000 else 0, .little);
    var offset: usize = 0;
    while (offset < request.len) {
        const n = std.c.send(fd, request[offset..].ptr, request.len - offset, std.c.MSG.NOSIGNAL);
        if (n < 0 and std.c.errno(n) == .INTR) continue;
        if (n <= 0) return fail();
        offset += @intCast(n);
    }
    var reply: [32]u8 = undefined;
    offset = 0;
    while (offset < reply.len) {
        const n = std.c.read(fd, reply[offset..].ptr, reply.len - offset);
        if (n < 0 and std.c.errno(n) == .INTR) continue;
        if (n <= 0) return fail();
        offset += @intCast(n);
    }
    if (!std.mem.eql(u8, reply[0..4], "CPG1") or
        std.mem.readInt(u32, reply[4..8], .little) != 0 or
        std.mem.readInt(u64, reply[8..16], .little) != sequence or
        std.mem.readInt(u64, reply[16..24], .little) != identity or
        std.mem.readInt(u64, reply[24..32], .little) != 0) return fail();
}

fn fail() error{RuntimeTaskGateLost} {
    failed = true;
    if (fd >= 0) _ = std.c.close(fd);
    fd = -1;
    return error.RuntimeTaskGateLost;
}

fn join() !void {
    // The mutex remains held during first admission: no joining thread can
    // execute before that specific authorization reply has arrived.
    if (active == 0) {
        task = std.math.add(u64, task, 1) catch return error.RuntimeTaskGateExhausted;
        try exchange(1, task);
    }
    active += 1;
}

fn leave() !void {
    std.debug.assert(active != 0);
    active -= 1;
    if (active == 0) try exchange(2, task);
}

pub fn enter() !void {
    if (depth != 0) {
        std.debug.assert(!paused);
        depth += 1;
        return;
    }
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    try initialize();
    if (fd >= 0) try join();
    depth = 1;
}

pub fn exit() void {
    std.debug.assert(depth != 0 and !paused);
    depth -= 1;
    if (depth != 0) return;
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    if (fd >= 0) leave() catch ProcessApi.mioa_process_exit_v1(1);
}

// Blocking Profile/network waits are outside the running CPU fragment. The
// caller must resume before parsing results or executing application callbacks.
pub fn pauseForWait() bool {
    if (depth == 0 or paused) return false;
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    if (fd < 0) return false;
    leave() catch ProcessApi.mioa_process_exit_v1(1);
    paused = true;
    return true;
}

pub fn resumeAfterWait(was_suspended: bool) void {
    if (!was_suspended) return;
    mutex.lockUncancelable(lp.io);
    defer mutex.unlock(lp.io);
    if (failed or fd < 0) ProcessApi.mioa_process_exit_v1(1);
    join() catch ProcessApi.mioa_process_exit_v1(1);
    paused = false;
}

pub fn enterOrExit() void {
    enter() catch ProcessApi.mioa_process_exit_v1(1);
}

// A trusted synchronous native I/O callback (the broker's CPU paint) may arrive
// while its outer command yielded for I/O. Reacquire only for the callback.
pub fn callbackEnter() bool {
    const was_paused = paused;
    if (was_paused) resumeAfterWait(true);
    enterOrExit();
    return was_paused;
}

pub fn callbackExit(was_paused: bool) void {
    exit();
    if (was_paused) _ = pauseForWait();
}
