// Typed record and cursor operations over the Profile IndexedDB protocol.
const std = @import("std");
const Engine = @import("Engine.zig");
const Remote = @import("Remote.zig");
const Allocator = std.mem.Allocator;
const Blob = Remote.BlobRef;

fn number(buf: *[32]u8, value: i64) []const u8 {
    return std.fmt.bufPrint(buf, "{d}", .{value}) catch unreachable;
}
fn command(remote: *Remote, ticket: ?[]const u8, operation: []const u8, payload: anytype) !void {
    var response = try remote.callWithTicket(?bool, ticket, operation, payload);
    defer response.deinit();
    if (response.value != null) return error.ProfileProtocolError;
}
fn integer(remote: *Remote, ticket: ?[]const u8, operation: []const u8, payload: anytype) !i64 {
    var response = try remote.callWithTicket([]const u8, ticket, operation, payload);
    defer response.deinit();
    return std.fmt.parseInt(i64, response.value, 10) catch error.ProfileProtocolError;
}
fn bytes(remote: *Remote, ticket: ?[]const u8, allocator: Allocator, operation: []const u8, payload: anytype) !?[]u8 {
    var response = try remote.callWithTicket(?Blob, ticket, operation, payload);
    defer response.deinit();
    const ref = response.value orelse return null;
    defer remote.dropBorrowed(ref);
    return try remote.download(allocator, ref);
}
const WireBounds = struct { lower: Blob, upper: Blob, lower_open: bool, upper_open: bool, is_point: bool };
const UploadedBounds = struct {
    remote: *Remote,
    value: WireBounds,
    fn init(remote: *Remote, b: Engine.Bounds) !UploadedBounds {
        const lower = try remote.upload(b.lower);
        errdefer remote.drop(lower);
        const upper = try remote.upload(b.upper);
        return .{ .remote = remote, .value = .{
            .lower = lower,
            .upper = upper,
            .lower_open = !b.is_point and std.mem.eql(u8, b.lower_op, "> "),
            .upper_open = !b.is_point and std.mem.eql(u8, b.upper_op, "< "),
            .is_point = b.is_point,
        } };
    }
    fn deinit(self: *UploadedBounds) void {
        self.remote.drop(self.value.lower);
        self.remote.drop(self.value.upper);
    }
};
fn rows(remote: *Remote, ticket: ?[]const u8, operation: []const u8, payload: anytype) !Remote.Rows {
    var response = try remote.callWithTicket(struct { rows_id: []const u8 }, ticket, operation, payload);
    defer response.deinit();
    return remote.rows(ticket, response.value.rows_id) catch |err| {
        command(remote, ticket, "idb.rows_close", .{ .rows_id = response.value.rows_id }) catch {};
        return err;
    };
}

pub fn add(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, key: []const u8, value: []const u8) !void {
    return writeRecord(remote, ticket, "idb.add", object_store_id, key, value);
}
pub fn put(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, key: []const u8, value: []const u8) !void {
    return writeRecord(remote, ticket, "idb.put", object_store_id, key, value);
}
fn writeRecord(remote: *Remote, ticket: ?[]const u8, operation: []const u8, object_store_id: i64, key: []const u8, value: []const u8) !void {
    const k = try remote.upload(key);
    defer remote.drop(k);
    const v = try remote.upload(value);
    defer remote.drop(v);
    var id: [32]u8 = undefined;
    return command(remote, ticket, operation, .{ .object_store_id = number(&id, object_store_id), .key = k, .value = v });
}
pub fn get(remote: *Remote, ticket: ?[]const u8, allocator: Allocator, object_store_id: i64, key: []const u8) !?[]u8 {
    const k = try remote.upload(key);
    defer remote.drop(k);
    var id: [32]u8 = undefined;
    return bytes(remote, ticket, allocator, "idb.get", .{ .object_store_id = number(&id, object_store_id), .key = k });
}
pub fn clear(remote: *Remote, ticket: ?[]const u8, object_store_id: i64) !void {
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.clear", .{ .object_store_id = number(&id, object_store_id) });
}
pub fn addIndexRecord(remote: *Remote, ticket: ?[]const u8, index_id: i64, key: []const u8, primary_key: []const u8, unique: bool) !void {
    const k = try remote.upload(key);
    defer remote.drop(k);
    const pk = try remote.upload(primary_key);
    defer remote.drop(pk);
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.addIndexRecord", .{ .index_id = number(&id, index_id), .key = k, .primary_key = pk, .unique = unique });
}
pub fn deleteIndexRecordsForKey(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, primary_key: []const u8) !void {
    const pk = try remote.upload(primary_key);
    defer remote.drop(pk);
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.deleteIndexRecordsForKey", .{ .object_store_id = number(&id, object_store_id), .primary_key = pk });
}
pub fn clearIndexRecordsForStore(remote: *Remote, ticket: ?[]const u8, object_store_id: i64) !void {
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.clearIndexRecordsForStore", .{ .object_store_id = number(&id, object_store_id) });
}
pub fn deleteIndexRecordsForRange(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, b: Engine.Bounds) !void {
    return rangeCommand(remote, ticket, "idb.deleteIndexRecordsForRange", object_store_id, b);
}
pub fn deleteRange(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, b: Engine.Bounds) !void {
    return rangeCommand(remote, ticket, "idb.deleteRange", object_store_id, b);
}
fn rangeCommand(remote: *Remote, ticket: ?[]const u8, operation: []const u8, object_store_id: i64, b: Engine.Bounds) !void {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var id: [32]u8 = undefined;
    return command(remote, ticket, operation, .{ .object_store_id = number(&id, object_store_id), .b = bounds.value });
}
pub fn getRange(remote: *Remote, ticket: ?[]const u8, allocator: Allocator, object_store_id: i64, b: Engine.Bounds) !?[]u8 {
    return rangeBytes(remote, ticket, allocator, "idb.getRange", object_store_id, b);
}
pub fn getKeyRange(remote: *Remote, ticket: ?[]const u8, allocator: Allocator, object_store_id: i64, b: Engine.Bounds) !?[]u8 {
    return rangeBytes(remote, ticket, allocator, "idb.getKeyRange", object_store_id, b);
}
fn rangeBytes(remote: *Remote, ticket: ?[]const u8, allocator: Allocator, operation: []const u8, object_store_id: i64, b: Engine.Bounds) !?[]u8 {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var id: [32]u8 = undefined;
    return bytes(remote, ticket, allocator, operation, .{ .object_store_id = number(&id, object_store_id), .b = bounds.value });
}
pub fn countRange(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, b: Engine.Bounds) !i64 {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var id: [32]u8 = undefined;
    return integer(remote, ticket, "idb.countRange", .{ .object_store_id = number(&id, object_store_id), .b = bounds.value });
}
pub fn indexGetRange(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64, index_id: i64, b: Engine.Bounds) !?[]u8 {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var store: [32]u8 = undefined;
    var index: [32]u8 = undefined;
    return bytes(remote, ticket, arena, "idb.indexGetRange", .{ .object_store_id = number(&store, object_store_id), .index_id = number(&index, index_id), .b = bounds.value });
}
pub fn indexGetKeyRange(remote: *Remote, ticket: ?[]const u8, arena: Allocator, index_id: i64, b: Engine.Bounds) !?[]u8 {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var index: [32]u8 = undefined;
    return bytes(remote, ticket, arena, "idb.indexGetKeyRange", .{ .index_id = number(&index, index_id), .b = bounds.value });
}
pub fn indexCountRange(remote: *Remote, ticket: ?[]const u8, index_id: i64, b: Engine.Bounds) !i64 {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var index: [32]u8 = undefined;
    return integer(remote, ticket, "idb.indexCountRange", .{ .index_id = number(&index, index_id), .b = bounds.value });
}
pub fn getAllRows(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, b: Engine.Bounds, reverse: bool, limit_: ?u32) !Remote.Rows {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var id: [32]u8 = undefined;
    return rows(remote, ticket, "idb.getAllRows", .{ .object_store_id = number(&id, object_store_id), .b = bounds.value, .reverse = reverse, .limit_ = limit_ });
}
pub fn indexGetAllRows(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, index_id: i64, b: Engine.Bounds, reverse: bool, unique: bool, limit_: ?u32) !Remote.Rows {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    var store: [32]u8 = undefined;
    var index: [32]u8 = undefined;
    return rows(remote, ticket, "idb.indexGetAllRows", .{ .object_store_id = number(&store, object_store_id), .index_id = number(&index, index_id), .b = bounds.value, .reverse = reverse, .unique = unique, .limit_ = limit_ });
}

const WireCursor = struct { key: Blob, value: ?Blob };
const WireIndexCursor = struct { key: Blob, primary_key: Blob, value: ?Blob };

pub fn cursorSeek(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64, b: Engine.Bounds, reverse: bool, from_op: []const u8, from_key: []const u8, offset: u32, with_value: bool) !?Engine.CursorRecord {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    const key = try remote.upload(from_key);
    defer remote.drop(key);
    const op = if (std.mem.eql(u8, from_op, "> ")) "gt" else if (std.mem.eql(u8, from_op, ">= ")) "gte" else if (std.mem.eql(u8, from_op, "< ")) "lt" else if (std.mem.eql(u8, from_op, "<= ")) "lte" else return error.InvalidArgument;
    var id: [32]u8 = undefined;
    var response = try remote.callWithTicket(?WireCursor, ticket, "idb.cursorSeek", .{ .object_store_id = number(&id, object_store_id), .b = bounds.value, .reverse = reverse, .from_op = op, .from_key = key, .offset = offset, .with_value = with_value });
    defer response.deinit();
    const result = response.value orelse return null;
    defer remote.dropBorrowed(result.key);
    defer if (result.value) |v| remote.dropBorrowed(v);
    return .{ .key = try remote.download(arena, result.key), .value = if (result.value) |v| try remote.download(arena, v) else null };
}

pub fn indexCursorSeek(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64, index_id: i64, b: Engine.Bounds, reverse: bool, unique: bool, from_key: []const u8, from_pk: []const u8, pk_inclusive: bool, with_value: bool, offset: u32) !?Engine.IndexCursorRecord {
    var bounds = try UploadedBounds.init(remote, b);
    defer bounds.deinit();
    const key = try remote.upload(from_key);
    defer remote.drop(key);
    const pk = try remote.upload(from_pk);
    defer remote.drop(pk);
    var store: [32]u8 = undefined;
    var index: [32]u8 = undefined;
    var response = try remote.callWithTicket(?WireIndexCursor, ticket, "idb.indexCursorSeek", .{ .object_store_id = number(&store, object_store_id), .index_id = number(&index, index_id), .b = bounds.value, .reverse = reverse, .unique = unique, .from_key = key, .from_pk = pk, .pk_inclusive = pk_inclusive, .with_value = with_value, .offset = offset });
    defer response.deinit();
    const result = response.value orelse return null;
    defer remote.dropBorrowed(result.key);
    defer remote.dropBorrowed(result.primary_key);
    defer if (result.value) |v| remote.dropBorrowed(v);
    return .{ .key = try remote.download(arena, result.key), .primary_key = try remote.download(arena, result.primary_key), .value = if (result.value) |v| try remote.download(arena, v) else null };
}
