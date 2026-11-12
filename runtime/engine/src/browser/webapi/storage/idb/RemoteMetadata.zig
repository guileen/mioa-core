// Typed metadata operations over the Profile IndexedDB protocol.
const std = @import("std");
const Remote = @import("Remote.zig");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const Allocator = std.mem.Allocator;

fn number(buf: *[32]u8, value: i64) []const u8 {
    return std.fmt.bufPrint(buf, "{d}", .{value}) catch unreachable;
}
fn integer(value: []const u8) !i64 {
    return std.fmt.parseInt(i64, value, 10) catch error.ProfileProtocolError;
}
fn scalar(remote: *Remote, ticket: ?[]const u8, operation: []const u8, payload: anytype) !?i64 {
    var response = try remote.callWithTicket(?[]const u8, ticket, operation, payload);
    defer response.deinit();
    return if (response.value) |v| try integer(v) else null;
}
fn command(remote: *Remote, ticket: ?[]const u8, operation: []const u8, payload: anytype) !void {
    var response = try remote.callWithTicket(?bool, ticket, operation, payload);
    defer response.deinit();
    if (response.value != null) return error.ProfileProtocolError;
}

pub fn databaseId(remote: *Remote, ticket: ?[]const u8, name: []const u8) !?i64 {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    return scalar(remote, ticket, "idb.databaseId", .{ .name = ref });
}
pub fn databaseVersion(remote: *Remote, ticket: ?[]const u8, name: []const u8) !?i64 {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    return scalar(remote, ticket, "idb.databaseVersion", .{ .name = ref });
}
pub fn upsertDatabase(remote: *Remote, ticket: ?[]const u8, name: []const u8, version: i64) !i64 {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var v: [32]u8 = undefined;
    return (try scalar(remote, ticket, "idb.upsertDatabase", .{ .name = ref, .version = number(&v, version) })) orelse error.ProfileProtocolError;
}
pub fn deleteDatabase(remote: *Remote, ticket: ?[]const u8, name: []const u8) !void {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    return command(remote, ticket, "idb.deleteDatabase", .{ .name = ref });
}
pub fn objectStoreId(remote: *Remote, ticket: ?[]const u8, database_id: i64, name: []const u8) !?i64 {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var id: [32]u8 = undefined;
    return scalar(remote, ticket, "idb.objectStoreId", .{ .database_id = number(&id, database_id), .name = ref });
}
pub fn indexExists(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, name: []const u8) !bool {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var id: [32]u8 = undefined;
    var response = try remote.callWithTicket(bool, ticket, "idb.indexExists", .{ .object_store_id = number(&id, object_store_id), .name = ref });
    defer response.deinit();
    return response.value;
}
pub fn renameObjectStore(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, name: []const u8) !void {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.renameObjectStore", .{ .object_store_id = number(&id, object_store_id), .name = ref });
}
pub fn renameIndex(remote: *Remote, ticket: ?[]const u8, index_id: i64, name: []const u8) !void {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.renameIndex", .{ .index_id = number(&id, index_id), .name = ref });
}
pub fn deleteObjectStore(remote: *Remote, ticket: ?[]const u8, database_id: i64, name: []const u8) !void {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.deleteObjectStore", .{ .database_id = number(&id, database_id), .name = ref });
}
pub fn deleteIndexRow(remote: *Remote, ticket: ?[]const u8, object_store_id: i64, name: []const u8) !void {
    const ref = try remote.upload(name);
    defer remote.drop(ref);
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.deleteIndexRow", .{ .object_store_id = number(&id, object_store_id), .name = ref });
}
pub fn nextGeneratedKey(remote: *Remote, ticket: ?[]const u8, store_id: i64) !i64 {
    var id: [32]u8 = undefined;
    return (try scalar(remote, ticket, "idb.nextGeneratedKey", .{ .store_id = number(&id, store_id) })) orelse error.ProfileProtocolError;
}
pub fn maybeBumpGenerator(remote: *Remote, ticket: ?[]const u8, store_id: i64, key: f64) !void {
    // Infinity is a valid IndexedDB numeric key but is not representable in JSON.
    // The local engine ignores keys below one and caps the rest at 2^53.
    if (key < 1) return;
    var id: [32]u8 = undefined;
    return command(remote, ticket, "idb.maybeBumpGenerator", .{ .store_id = number(&id, store_id), .key = @min(key, 9007199254740992) });
}

const WireDatabase = struct { name: Remote.BlobRef, version: []const u8 };
const WirePath = struct { data: Remote.BlobRef, component_length: []const u8 };
const WireStore = struct { id: []const u8, key_path: ?WirePath, auto_increment: bool };
const WireIndex = struct { id: []const u8, key_path: WirePath, unique: bool, multi_entry: bool };

fn uploadPath(remote: *Remote, arena: Allocator, path: ?Key.KeyPath, length_buf: *[32]u8) !?WirePath {
    const p = path orelse return null;
    const column = try Key.encodeKeyPathColumn(arena, p);
    const length = try std.fmt.bufPrint(length_buf, "{d}", .{column.component_length});
    return .{ .data = try remote.upload(column.text), .component_length = length };
}
fn decodePath(remote: *Remote, arena: Allocator, path: WirePath) !Key.KeyPath {
    defer remote.dropBorrowed(path.data);
    const length = std.fmt.parseInt(usize, path.component_length, 10) catch return error.ProfileProtocolError;
    const text = try remote.download(remote.allocator, path.data);
    defer remote.allocator.free(text);
    return Key.decodeKeyPathColumn(arena, text, length) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => error.ProfileProtocolError,
    };
}
fn decodeIndex(remote: *Remote, arena: Allocator, value: WireIndex) !Engine.IndexInfo {
    const path = try decodePath(remote, arena, value.key_path);
    return .{ .id = try integer(value.id), .key_path = path, .unique = value.unique, .multi_entry = value.multi_entry };
}
pub fn databases(remote: *Remote, ticket: ?[]const u8, arena: Allocator) ![]const Engine.DatabaseInfo {
    var response = try remote.callWithTicket(struct { rows_id: []const u8 }, ticket, "idb.databases", struct {}{});
    defer response.deinit();
    const id = response.value.rows_id;
    defer command(remote, ticket, "idb.rows_close", .{ .rows_id = id }) catch {};
    var result: std.ArrayList(Engine.DatabaseInfo) = .empty;
    while (true) {
        var row = try remote.callWithTicket(?WireDatabase, ticket, "idb.rows_next", .{ .rows_id = id });
        defer row.deinit();
        const item = row.value orelse break;
        defer remote.dropBorrowed(item.name);
        try result.append(arena, .{ .name = try remote.download(arena, item.name), .version = try integer(item.version) });
    }
    return result.items;
}
pub fn objectStoreInfo(remote: *Remote, ticket: ?[]const u8, arena: Allocator, database_id: i64, name: []const u8) !?Engine.StoreInfo {
    const name_ref = try remote.upload(name);
    defer remote.drop(name_ref);
    var id: [32]u8 = undefined;
    var response = try remote.callWithTicket(?WireStore, ticket, "idb.objectStoreInfo", .{ .database_id = number(&id, database_id), .name = name_ref });
    defer response.deinit();
    const info = response.value orelse return null;
    const path = if (info.key_path) |p| try decodePath(remote, arena, p) else null;
    return .{ .id = try integer(info.id), .key_path = path, .auto_increment = info.auto_increment };
}
pub fn createObjectStore(remote: *Remote, ticket: ?[]const u8, arena: Allocator, database_id: i64, name: []const u8, key_path: ?Key.KeyPath, auto_increment: bool) !i64 {
    const name_ref = try remote.upload(name);
    defer remote.drop(name_ref);
    var length: [32]u8 = undefined;
    const path = try uploadPath(remote, arena, key_path, &length);
    defer if (path) |p| remote.drop(p.data);
    var id: [32]u8 = undefined;
    return (try scalar(remote, ticket, "idb.createObjectStore", .{ .database_id = number(&id, database_id), .name = name_ref, .key_path = path, .auto_increment = auto_increment })) orelse error.ProfileProtocolError;
}
pub fn createIndexRow(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64, name: []const u8, key_path: Key.KeyPath, unique: bool, multi_entry: bool) !i64 {
    const name_ref = try remote.upload(name);
    defer remote.drop(name_ref);
    var length: [32]u8 = undefined;
    const path = (try uploadPath(remote, arena, key_path, &length)).?;
    defer remote.drop(path.data);
    var id: [32]u8 = undefined;
    return (try scalar(remote, ticket, "idb.createIndexRow", .{ .object_store_id = number(&id, object_store_id), .name = name_ref, .key_path = path, .unique = unique, .multi_entry = multi_entry })) orelse error.ProfileProtocolError;
}
pub fn indexInfo(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64, name: []const u8) !?Engine.IndexInfo {
    const name_ref = try remote.upload(name);
    defer remote.drop(name_ref);
    var id: [32]u8 = undefined;
    var response = try remote.callWithTicket(?WireIndex, ticket, "idb.indexInfo", .{ .object_store_id = number(&id, object_store_id), .name = name_ref });
    defer response.deinit();
    return if (response.value) |value| try decodeIndex(remote, arena, value) else null;
}
pub fn indexesForStore(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64) ![]Engine.IndexInfo {
    var id_buf: [32]u8 = undefined;
    var response = try remote.callWithTicket(struct { rows_id: []const u8 }, ticket, "idb.indexesForStore", .{ .object_store_id = number(&id_buf, object_store_id) });
    defer response.deinit();
    const id = response.value.rows_id;
    defer command(remote, ticket, "idb.rows_close", .{ .rows_id = id }) catch {};
    var result: std.ArrayList(Engine.IndexInfo) = .empty;
    while (true) {
        var row = try remote.callWithTicket(?WireIndex, ticket, "idb.rows_next", .{ .rows_id = id });
        defer row.deinit();
        const item = row.value orelse break;
        try result.append(arena, try decodeIndex(remote, arena, item));
    }
    return result.items;
}
fn names(remote: *Remote, ticket: ?[]const u8, arena: Allocator, operation: []const u8, payload: anytype) ![]const []const u8 {
    var response = try remote.callWithTicket(struct { rows_id: []const u8 }, ticket, operation, payload);
    defer response.deinit();
    const id = response.value.rows_id;
    defer command(remote, ticket, "idb.rows_close", .{ .rows_id = id }) catch {};
    var result: std.ArrayList([]const u8) = .empty;
    while (true) {
        var row = try remote.callWithTicket(?Remote.BlobRef, ticket, "idb.rows_next", .{ .rows_id = id });
        defer row.deinit();
        const item = row.value orelse break;
        defer remote.dropBorrowed(item);
        try result.append(arena, try remote.download(arena, item));
    }
    return result.items;
}
pub fn indexNames(remote: *Remote, ticket: ?[]const u8, arena: Allocator, object_store_id: i64) ![]const []const u8 {
    var id: [32]u8 = undefined;
    return names(remote, ticket, arena, "idb.indexNames", .{ .object_store_id = number(&id, object_store_id) });
}
pub fn objectStoreNames(remote: *Remote, ticket: ?[]const u8, arena: Allocator, database_id: i64) ![]const []const u8 {
    var id: [32]u8 = undefined;
    return names(remote, ticket, arena, "idb.objectStoreNames", .{ .database_id = number(&id, database_id) });
}
