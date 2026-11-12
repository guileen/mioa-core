const std = @import("std");
const ProfileAsync = @import("../ProfileAsync.zig");
const Gate = @import("RemoteGate.zig");
const Allocator = std.mem.Allocator;

const Command = struct {
    allocator: Allocator,
    ctx: *anyopaque,
    done: *const fn (*anyopaque, ?anyerror) void,
};

// A successful submission completes exactly once, asynchronously.
pub fn submit(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ticket: []const u8, operation: []const u8, ctx: *anyopaque, done: *const fn (*anyopaque, ?anyerror) void) !u64 {
    const command = try allocator.create(Command);
    errdefer allocator.destroy(command);
    command.* = .{ .allocator = allocator, .ctx = ctx, .done = done };
    return try client.submit(operation, .{ .origin = origin, .ticket = ticket }, command, complete);
}

fn complete(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const command: *Command = @ptrCast(@alignCast(ctx));
    const callback = command.done;
    const target = command.ctx;
    const allocator = command.allocator;
    var err: ?anyerror = null;
    switch (result) {
        .failure => |failure| err = failure,
        .reply => |reply| {
            defer reply.deinit();
            err = Gate.responseError(reply.value);
            if (err == null) {
                const value = reply.value.object.get("result") orelse {
                    err = error.ProfileProtocolError;
                    allocator.destroy(command);
                    callback(target, err);
                    return;
                };
                if (value != .null) err = error.ProfileProtocolError;
            }
        },
    }
    allocator.destroy(command);
    callback(target, err);
}
