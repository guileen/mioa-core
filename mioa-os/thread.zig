const std = @import("std");

pub const MIOA_THREAD_OK: c_int = 0;
pub const MIOA_THREAD_INVALID: c_int = 1;
pub const MIOA_THREAD_NO_MEMORY: c_int = 2;
pub const MIOA_THREAD_START_FAILED: c_int = 3;

const Entry = *const fn (?*anyopaque) callconv(.c) void;
const Handle = struct { thread: std.Thread };

fn invoke(entry: Entry, context: ?*anyopaque) void {
    entry(context);
}

pub export fn mioa_thread_start_v1(
    entry: ?Entry,
    context: ?*anyopaque,
    thread_handle: ?*?*anyopaque,
) c_int {
    const out = thread_handle orelse return MIOA_THREAD_INVALID;
    out.* = null;
    const callback = entry orelse return MIOA_THREAD_INVALID;

    const handle = std.heap.page_allocator.create(Handle) catch return MIOA_THREAD_NO_MEMORY;
    handle.thread = std.Thread.spawn(.{}, invoke, .{ callback, context }) catch {
        std.heap.page_allocator.destroy(handle);
        return MIOA_THREAD_START_FAILED;
    };
    out.* = @ptrCast(handle);
    return MIOA_THREAD_OK;
}
pub export fn mioa_thread_start_detached_v1(
    entry: ?Entry,
    context: ?*anyopaque,
) c_int {
    const callback = entry orelse return MIOA_THREAD_INVALID;
    const thread = std.Thread.spawn(.{}, invoke, .{ callback, context }) catch return MIOA_THREAD_START_FAILED;
    thread.detach();
    return MIOA_THREAD_OK;
}


pub export fn mioa_thread_join_v1(thread_handle: ?*?*anyopaque) void {
    const out = thread_handle orelse return;
    const opaque_handle = out.* orelse return;
    const handle: *Handle = @ptrCast(@alignCast(opaque_handle));
    handle.thread.join();
    std.heap.page_allocator.destroy(handle);
    out.* = null;
}

pub export fn mioa_thread_detach_v1(thread_handle: ?*?*anyopaque) void {
    const out = thread_handle orelse return;
    const opaque_handle = out.* orelse return;
    const handle: *Handle = @ptrCast(@alignCast(opaque_handle));
    handle.thread.detach();
    std.heap.page_allocator.destroy(handle);
    out.* = null;
}

pub export fn mioa_thread_yield_v1() void {
    std.Thread.yield() catch std.atomic.spinLoopHint();
}

const DetachContext = struct {
    started: std.atomic.Value(bool) = .init(false),
    release: std.atomic.Value(bool) = .init(false),
    done: std.atomic.Value(bool) = .init(false),
};

fn blockedUntilRelease(context: ?*anyopaque) callconv(.c) void {
    const state: *DetachContext = @ptrCast(@alignCast(context.?));
    state.started.store(true, .release);
    while (!state.release.load(.acquire)) std.atomic.spinLoopHint();
    state.done.store(true, .release);
}

fn waitFor(flag: *std.atomic.Value(bool)) bool {
    var attempts: usize = 0;
    while (!flag.load(.acquire) and attempts < 100_000) : (attempts += 1) {
        std.Thread.yield() catch std.atomic.spinLoopHint();
    }
    return flag.load(.acquire);
}

test "detach consumes token without waiting and callback retains borrowed context" {
    var state: DetachContext = .{};
    var handle: ?*anyopaque = null;
    try std.testing.expectEqual(
        MIOA_THREAD_OK,
        mioa_thread_start_v1(blockedUntilRelease, &state, &handle),
    );
    try std.testing.expect(handle != null);
    try std.testing.expect(waitFor(&state.started));
    mioa_thread_detach_v1(&handle);
    try std.testing.expect(handle == null);
    try std.testing.expect(!state.done.load(.acquire));

    state.release.store(true, .release);
    try std.testing.expect(waitFor(&state.done));
    mioa_thread_detach_v1(&handle);
    mioa_thread_detach_v1(null);
}
test "detached start runs callback to completion with borrowed context" {
    var state: DetachContext = .{};
    try std.testing.expectEqual(
        MIOA_THREAD_OK,
        mioa_thread_start_detached_v1(blockedUntilRelease, &state),
    );
    try std.testing.expect(waitFor(&state.started));
    state.release.store(true, .release);
    try std.testing.expect(waitFor(&state.done));
}

test "detached start rejects a missing callback" {
    try std.testing.expectEqual(
        MIOA_THREAD_INVALID,
        mioa_thread_start_detached_v1(null, null),
    );
}

fn increment(context: ?*anyopaque) callconv(.c) void {
    const count: *std.atomic.Value(u32) = @ptrCast(@alignCast(context.?));
    _ = count.fetchAdd(1, .monotonic);
}

test "join waits for callback and consumes provider handle" {
    var count: std.atomic.Value(u32) = .init(0);
    var handle: ?*anyopaque = null;
    try std.testing.expectEqual(
        MIOA_THREAD_OK,
        mioa_thread_start_v1(increment, &count, &handle),
    );
    try std.testing.expect(handle != null);
    mioa_thread_join_v1(&handle);
    try std.testing.expect(handle == null);
    try std.testing.expectEqual(@as(u32, 1), count.load(.acquire));
}

test "start rejects invalid output and callback and join accepts empty token" {
    try std.testing.expectEqual(
        MIOA_THREAD_INVALID,
        mioa_thread_start_v1(null, null, null),
    );
    var handle: ?*anyopaque = @ptrFromInt(1);
    try std.testing.expectEqual(
        MIOA_THREAD_INVALID,
        mioa_thread_start_v1(null, null, &handle),
    );
    try std.testing.expect(handle == null);
    mioa_thread_join_v1(&handle);
    mioa_thread_join_v1(null);
    try std.testing.expect(handle == null);
}
