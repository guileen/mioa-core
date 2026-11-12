// Async gate requests outlive page-owned waiters during cancellation.
const std = @import("std");
const ProfileAsync = @import("../ProfileAsync.zig");
const Allocator = std.mem.Allocator;
const Gate = @This();

allocator: Allocator,
client: *ProfileAsync.Client,
origin: []const u8,
ticket: ?[]const u8 = null,
granted: bool = false,
failure: ?anyerror = null,
pending: bool = false,
closing: bool = false,
wake_ctx: ?*anyopaque,
wake: ?*const fn (*anyopaque) void,

pub fn create(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ctx: *anyopaque, wake: *const fn (*anyopaque) void) !*Gate {
    const self = try allocator.create(Gate);
    errdefer allocator.destroy(self);
    self.* = .{ .allocator = allocator, .client = client, .origin = try allocator.dupe(u8, origin), .wake_ctx = ctx, .wake = wake };
    return self;
}

pub fn start(self: *Gate) !void {
    if (self.pending or self.ticket != null or self.closing) return error.InvalidArgument;
    _ = try self.client.submit("idb.acquire", .{ .origin = self.origin, .ticket = @as(?[]const u8, null) }, self, acquired);
    self.pending = true;
}

pub fn poll(self: *Gate) !void {
    if (self.pending or self.closing or self.granted) return error.InvalidArgument;
    const ticket = self.ticket orelse return error.InvalidArgument;
    _ = try self.client.submit("idb.poll", .{ .origin = self.origin, .ticket = ticket }, self, polled);
    self.pending = true;
}

// Detach before releasing the caller's waiter. A late acquire/poll response is
// consumed by this heap state and followed by cancellation, never by a wake.
pub fn cancel(self: *Gate) void {
    self.closing = true;
    self.wake = null;
    self.wake_ctx = null;
    if (!self.pending) self.cancelOrDestroy();
}

pub fn deinit(self: *Gate) void {
    self.cancel();
}

fn destroy(self: *Gate) void {
    if (self.ticket) |ticket| self.allocator.free(ticket);
    self.allocator.free(self.origin);
    self.allocator.destroy(self);
}

fn notify(self: *Gate) void {
    if (self.wake) |wake| if (self.wake_ctx) |ctx| wake(ctx);
}

fn cancelOrDestroy(self: *Gate) void {
    const ticket = self.ticket orelse {
        self.destroy();
        return;
    };
    _ = self.client.submit("idb.cancel", .{ .origin = self.origin, .ticket = ticket }, self, cancelled) catch {
        // Closing the dedicated channel makes the owner release all resources
        // for this principal when cancellation cannot be queued.
        self.client.deinit();
        self.destroy();
        return;
    };
    self.pending = true;
}

fn resultValue(result: ProfileAsync.Result) !std.json.Parsed(std.json.Value) {
    switch (result) {
        .failure => |err| return err,
        .reply => |reply| return reply,
    }
}

pub fn responseError(value: std.json.Value) ?anyerror {
    if (value != .object) return error.ProfileProtocolError;
    const ok = value.object.get("ok") orelse return error.ProfileProtocolError;
    if (ok != .bool) return error.ProfileProtocolError;
    if (ok.bool) return null;
    if (value.object.get("effect")) |effect| {
        if (effect == .string and std.mem.eql(u8, effect.string, "unknown")) return error.ProfileSideEffectUnknown;
    }
    if (value.object.get("error_code")) |code| {
        if (code == .string) {
            inline for (.{ error.Constraint, error.NotFound, error.Busy, error.Locked, error.Abort, error.Interrupt, error.OutOfMemory, error.QuotaExceeded, error.InvalidArgument, error.Full, error.IoErr, error.Corrupt, error.ReadOnly, error.TooBig, error.UnknownError }) |known| {
                if (std.mem.eql(u8, code.string, @errorName(known))) return known;
            }
        }
    }
    return error.ProfileRequestRejected;
}

fn acquired(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const self: *Gate = @ptrCast(@alignCast(ctx));
    self.pending = false;
    const reply = resultValue(result) catch |err| {
        self.failure = err;
        if (self.closing) self.destroy() else self.notify();
        return;
    };
    defer reply.deinit();
    if (responseError(reply.value)) |err| {
        self.failure = err;
    } else if (reply.value.object.get("result")) |value| {
        const parsed = std.json.parseFromValue(struct { ticket: []const u8, granted: bool }, self.allocator, value, .{ .allocate = .alloc_always }) catch null;
        if (parsed) |p| {
            defer p.deinit();
            self.ticket = self.allocator.dupe(u8, p.value.ticket) catch null;
            self.granted = p.value.granted;
            if (self.ticket == null) {
                self.failure = error.OutOfMemory;
                self.client.deinit();
            }
        } else {
            self.failure = error.ProfileProtocolError;
            self.client.deinit();
        }
    } else {
        self.failure = error.ProfileProtocolError;
        self.client.deinit();
    }
    if (self.closing) self.cancelOrDestroy() else self.notify();
}

fn polled(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const self: *Gate = @ptrCast(@alignCast(ctx));
    self.pending = false;
    const reply = resultValue(result) catch |err| {
        self.failure = err;
        if (self.closing) self.cancelOrDestroy() else self.notify();
        return;
    };
    defer reply.deinit();
    if (responseError(reply.value)) |err| {
        self.failure = err;
    } else if (reply.value.object.get("result")) |value| {
        const parsed = std.json.parseFromValue(struct { granted: bool }, self.allocator, value, .{}) catch null;
        if (parsed) |p| {
            defer p.deinit();
            self.granted = p.value.granted;
        } else self.failure = error.ProfileProtocolError;
    } else self.failure = error.ProfileProtocolError;
    if (self.closing) self.cancelOrDestroy() else self.notify();
}

fn cancelled(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const self: *Gate = @ptrCast(@alignCast(ctx));
    self.pending = false;
    switch (result) {
        .failure => self.client.deinit(),
        .reply => |reply| {
            defer reply.deinit();
            if (responseError(reply.value) != null) self.client.deinit();
        },
    }
    self.destroy();
}

test "owner resource denial preserves IndexedDB quota semantics" {
    const parsed = try std.json.parseFromSlice(std.json.Value, std.testing.allocator, "{\"ok\":false,\"error_code\":\"QuotaExceeded\"}", .{});
    defer parsed.deinit();
    const failure = responseError(parsed.value) orelse return error.TestExpectedError;
    try std.testing.expect(failure == error.QuotaExceeded);
}
