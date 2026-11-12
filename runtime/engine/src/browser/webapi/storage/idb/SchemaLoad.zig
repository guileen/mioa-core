// Stream a committed schema while the caller holds the origin grant. The heap
// state owns all in-flight protocol resources and survives a detached caller.
const std = @import("std");
const ProfileAsync = @import("../ProfileAsync.zig");
const RuntimeMemory = @import("../RuntimeMemory.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const Gate = @import("RemoteGate.zig");
const Key = @import("Key.zig");
const Snapshot = @import("SchemaSnapshot.zig").Snapshot;
const Allocator = std.mem.Allocator;
const BlobRef = RemoteAsync.BlobRef;

const WirePath = struct { data: BlobRef, component_length: []const u8 };
const WireStore = struct { name: BlobRef, id: []const u8, key_path: ?WirePath, auto_increment: bool };
const WireIndex = struct { name: BlobRef, id: []const u8, key_path: WirePath, unique: bool, multi_entry: bool };
const WireRow = struct { store: WireStore, index: ?WireIndex };
const Header = struct { rows_id: []const u8, next_store_id: []const u8, next_index_id: []const u8 };

pub const Result = union(enum) { success: Snapshot, failure: anyerror };
pub const Handle = @This();

allocator: Allocator,
snapshot_allocator: Allocator,
client: *ProfileAsync.Client,
origin: []u8,
ticket: []u8,
ctx: ?*anyopaque,
callback: ?*const fn (*anyopaque, Result) void,
rows_id: ?[]u8 = null,
row: ?std.json.Parsed(WireRow) = null,
refs: [4]?BlobRef = .{ null, null, null, null },
values: [4]?[]u8 = .{ null, null, null, null },
position: usize = 0,
child: ?*RemoteAsync.Handle = null,
snapshot: Snapshot = .{},
failure: ?anyerror = null,
cancelled: bool = false,

// The caller's context must survive until the completion callback. Keep the
// state alive to drain owner handles, without touching the snapshot allocator.
pub fn cancel(self: *Handle) void {
    self.cancelled = true;
}
pub fn start(allocator: Allocator, snapshot_allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ticket: []const u8, db_id: i64, ctx: *anyopaque, callback: *const fn (*anyopaque, Result) void) !*Handle {
    const bounded = try RuntimeMemory.allocator(allocator);
    const self = try bounded.create(Handle);
    errdefer bounded.destroy(self);
    const owned_origin = try bounded.dupe(u8, origin);
    errdefer bounded.free(owned_origin);
    const owned_ticket = try bounded.dupe(u8, ticket);
    errdefer bounded.free(owned_ticket);
    self.* = .{ .allocator = bounded, .snapshot_allocator = snapshot_allocator, .client = client, .origin = owned_origin, .ticket = owned_ticket, .ctx = ctx, .callback = callback };
    var buf: [32]u8 = undefined;
    const id = try std.fmt.bufPrint(&buf, "{d}", .{db_id});
    _ = try RemoteAsync.request(bounded, client, owned_origin, owned_ticket, "idb.schemaSnapshot", .{ .database_id = id }, self, headerDone);
    return self;
}

fn integer(text: []const u8) !i64 {
    const value = std.fmt.parseInt(i64, text, 10) catch return error.ProfileProtocolError;
    if (value <= 0) return error.ProfileProtocolError;
    return value;
}
fn resultValue(reply: std.json.Value) !std.json.Value {
    if (Gate.responseError(reply)) |err| return err;
    return reply.object.get("result") orelse error.ProfileProtocolError;
}
fn failed(self: *Handle, err: anyerror) void {
    if (self.failure == null) self.failure = err;
    if (err == error.ProfileSideEffectUnknown) self.client.deinit();
}
fn headerDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const self: *Handle = @ptrCast(@alignCast(ctx));
    const reply = switch (result) {
        .failure => |err| {
            self.failed(err);
            self.finish();
            return;
        },
        .reply => |value| value,
    };
    defer reply.deinit();
    const value = resultValue(reply.value) catch |err| {
        self.failed(err);
        self.finish();
        return;
    };
    const parsed = std.json.parseFromValue(Header, self.allocator, value, .{ .allocate = .alloc_always }) catch |err| {
        self.client.deinit();
        self.failed(if (err == error.OutOfMemory) error.OutOfMemory else error.ProfileProtocolError);
        self.finish();
        return;
    };
    defer parsed.deinit();
    self.rows_id = self.allocator.dupe(u8, parsed.value.rows_id) catch {
        self.client.deinit();
        self.failed(error.OutOfMemory);
        self.finish();
        return;
    };
    self.snapshot.next_store_id = integer(parsed.value.next_store_id) catch |err| {
        self.failed(err);
        self.close();
        return;
    };
    self.snapshot.next_index_id = integer(parsed.value.next_index_id) catch |err| {
        self.failed(err);
        self.close();
        return;
    };
    if (self.cancelled) self.close() else self.next();
}
fn next(self: *Handle) void {
    _ = RemoteAsync.request(self.allocator, self.client, self.origin, self.ticket, "idb.rows_next", .{ .rows_id = self.rows_id.? }, self, rowDone) catch |err| {
        self.failed(err);
        self.close();
        return;
    };
}
fn rowDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const self: *Handle = @ptrCast(@alignCast(ctx));
    const reply = switch (result) {
        .failure => |err| {
            self.failed(err);
            self.close();
            return;
        },
        .reply => |value| value,
    };
    defer reply.deinit();
    const value = resultValue(reply.value) catch |err| {
        self.failed(err);
        self.close();
        return;
    };
    if (value == .null) {
        self.close();
        return;
    }
    self.row = std.json.parseFromValue(WireRow, self.allocator, value, .{ .allocate = .alloc_always }) catch |err| {
        // The owner may already have allocated blob handles that we cannot identify.
        self.client.deinit();
        self.failed(if (err == error.OutOfMemory) error.OutOfMemory else error.ProfileProtocolError);
        self.close();
        return;
    };
    const row = self.row.?.value;
    self.refs = .{ row.store.name, if (row.store.key_path) |p| p.data else null, if (row.index) |i| i.name else null, if (row.index) |i| i.key_path.data else null };
    self.position = 0;
    if (self.cancelled) self.dropNext() else self.readNext();
}
fn readNext(self: *Handle) void {
    while (self.position < self.refs.len and self.refs[self.position] == null) self.position += 1;
    if (self.position == self.refs.len) {
        if (!self.cancelled) self.addRow() catch |err| self.failed(err);
        self.position = 0;
        self.dropNext();
        return;
    }
    self.child = RemoteAsync.download(self.allocator, self.client, self.origin, self.refs[self.position].?, self, readDone) catch |err| {
        self.failed(err);
        self.position = 0;
        self.dropNext();
        return;
    };
}
fn readDone(ctx: *anyopaque, result: RemoteAsync.BytesResult) void {
    const self: *Handle = @ptrCast(@alignCast(ctx));
    self.child = null;
    switch (result) {
        .failure => |err| {
            self.failed(err);
            self.position = 0;
            self.dropNext();
        },
        .value => |bytes| {
            self.values[self.position] = bytes;
            self.position += 1;
            if (self.cancelled) {
                self.position = 0;
                self.dropNext();
            } else self.readNext();
        },
    }
}
fn path(allocator: Allocator, wire: WirePath, bytes: []const u8) !Key.KeyPath {
    const length = std.fmt.parseInt(usize, wire.component_length, 10) catch return error.ProfileProtocolError;
    return Key.decodeKeyPathColumn(allocator, bytes, length) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => error.ProfileProtocolError,
    };
}
fn samePath(a: ?Key.KeyPath, b: ?Key.KeyPath) bool {
    const x = a orelse return b == null;
    const y = b orelse return false;
    return switch (x) {
        .string => |s| switch (y) {
            .string => |t| std.mem.eql(u8, s, t),
            else => false,
        },
        .list => |xs| switch (y) {
            .list => |ys| blk: {
                if (xs.len != ys.len) break :blk false;
                for (xs, ys) |s, t| if (!std.mem.eql(u8, s, t)) break :blk false;
                break :blk true;
            },
            else => false,
        },
    };
}
fn addRow(self: *Handle) !void {
    const row = self.row.?.value;
    var arena = std.heap.ArenaAllocator.init(self.allocator);
    defer arena.deinit();
    const store_id = try integer(row.store.id);
    if (store_id >= self.snapshot.next_store_id) return error.ProfileProtocolError;
    const store_path = if (row.store.key_path) |p| try path(arena.allocator(), p, self.values[1].?) else null;
    const name = self.values[0].?;
    if (self.snapshot.storeById(store_id)) |store| {
        if (!std.mem.eql(u8, store.name, name) or store.info.auto_increment != row.store.auto_increment or !samePath(store.info.key_path, store_path)) return error.ProfileProtocolError;
    } else {
        try self.snapshot.addStore(self.snapshot_allocator, name, .{ .id = store_id, .key_path = store_path, .auto_increment = row.store.auto_increment });
    }
    if (row.index) |index| {
        const index_id = try integer(index.id);
        if (index_id >= self.snapshot.next_index_id) return error.ProfileProtocolError;
        try self.snapshot.addIndex(self.snapshot_allocator, store_id, self.values[2].?, .{ .id = index_id, .key_path = try path(arena.allocator(), index.key_path, self.values[3].?), .unique = index.unique, .multi_entry = index.multi_entry });
    }
}
fn dropNext(self: *Handle) void {
    while (self.position < self.refs.len and self.refs[self.position] == null) self.position += 1;
    if (self.position == self.refs.len) {
        for (&self.values) |*v| {
            if (v.*) |bytes| self.allocator.free(bytes);
            v.* = null;
        }
        if (self.row) |row| row.deinit();
        self.row = null;
        self.refs = .{ null, null, null, null };
        if (self.cancelled or self.failure != null) self.close() else self.next();
        return;
    }
    self.child = RemoteAsync.drop(self.allocator, self.client, self.origin, self.refs[self.position].?, self, dropDone) catch |err| {
        self.failed(err);
        // Remaining handles are reclaimed by closing the dedicated channel.
        self.client.deinit();
        self.position = self.refs.len;
        self.dropNext();
        return;
    };
}
fn dropDone(ctx: *anyopaque, err: ?anyerror) void {
    const self: *Handle = @ptrCast(@alignCast(ctx));
    self.child = null;
    if (err) |failure| {
        self.failed(failure);
        self.client.deinit();
        self.position = self.refs.len;
    } else self.position += 1;
    self.dropNext();
}
fn close(self: *Handle) void {
    const id = self.rows_id orelse {
        self.finish();
        return;
    };
    _ = RemoteAsync.request(self.allocator, self.client, self.origin, self.ticket, "idb.rows_close", .{ .rows_id = id }, self, closed) catch |err| {
        self.failed(err);
        self.client.deinit();
        self.finish();
        return;
    };
}
fn closed(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const self: *Handle = @ptrCast(@alignCast(ctx));
    switch (result) {
        .failure => |err| {
            self.failed(err);
            self.client.deinit();
        },
        .reply => |reply| {
            defer reply.deinit();
            const value = resultValue(reply.value) catch |err| {
                self.failed(err);
                self.client.deinit();
                self.finish();
                return;
            };
            if (value != .null) {
                self.failed(error.ProfileProtocolError);
                self.client.deinit();
            }
        },
    }
    self.finish();
}
fn finish(self: *Handle) void {
    const allocator = self.allocator;
    const callback = self.callback;
    const ctx = self.ctx;
    const result: Result = if (self.cancelled) .{ .failure = error.Abort } else if (self.failure) |err| .{ .failure = err } else .{ .success = self.snapshot };
    if (self.rows_id) |id| allocator.free(id);
    allocator.free(self.origin);
    allocator.free(self.ticket);
    allocator.destroy(self);
    if (callback) |done| if (ctx) |target| done(target, result);
}
