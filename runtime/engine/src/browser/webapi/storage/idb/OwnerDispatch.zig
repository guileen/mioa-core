// Copyright (C) 2023-2026 Lightpanda (Selecy SAS)
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The owner accepts a closed set of Engine operations, never SQL. The host
// authenticates the origin and ticket before calling dispatch and owns blob and
// row handles. All input byte slices are borrowed from that host for this call.
const std = @import("std");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const Allocator = std.mem.Allocator;
const Value = std.json.Value;

fn field(v: Value, name: []const u8) !Value {
    if (v != .object) return error.InvalidRequest;
    return v.object.get(name) orelse error.InvalidRequest;
}
fn string(v: Value) ![]const u8 {
    if (v != .string) return error.InvalidRequest;
    return v.string;
}
fn text(v: Value, name: []const u8) ![]const u8 {
    return string(try field(v, name));
}
fn integer(v: Value, name: []const u8) !i64 {
    return std.fmt.parseInt(i64, try text(v, name), 10) catch error.InvalidRequest;
}
fn boolean(v: Value, name: []const u8) !bool {
    const x = try field(v, name);
    if (x != .bool) return error.InvalidRequest;
    return x.bool;
}
fn unsigned(v: Value, name: []const u8) !u32 {
    const x = try field(v, name);
    if (x != .integer or x.integer < 0 or x.integer > std.math.maxInt(u32)) return error.InvalidRequest;
    return @intCast(x.integer);
}
fn limit(v: Value) !?u32 {
    const x = try field(v, "limit_");
    if (x == .null) return null;
    return try unsigned(v, "limit_");
}
fn number(v: Value, name: []const u8) !f64 {
    const x = try field(v, name);
    const n: f64 = switch (x) {
        .float => x.float,
        .integer => @floatFromInt(x.integer),
        else => return error.InvalidRequest,
    };
    if (!std.math.isFinite(n)) return error.InvalidRequest;
    return n;
}
fn keyPath(host: anytype, principal: u64, a: Allocator, v: Value) !?Key.KeyPath {
    if (v == .null) return null;
    const text_bytes = try host.inputBlob(principal, try field(v, "data"));
    const length = std.fmt.parseInt(usize, try text(v, "component_length"), 10) catch return error.InvalidRequest;
    if (length > 0 and length - 1 > text_bytes.len) return error.InvalidRequest;
    if (length > 0 and std.mem.count(u8, text_bytes, ",") != length - 1) return error.InvalidRequest;
    return try Key.decodeKeyPathColumn(a, text_bytes, length);
}
fn pathValue(host: anytype, principal: u64, a: Allocator, path: ?Key.KeyPath) !?struct {
    data: @typeInfo(@TypeOf(host.outputBlob(principal, ""))).error_union.payload,
    component_length: []const u8,
} {
    const p = path orelse return null;
    const column = try Key.encodeKeyPathColumn(a, p);
    const component_length = try std.fmt.allocPrint(a, "{d}", .{column.component_length});
    return .{
        .data = try host.outputBlob(principal, column.text),
        .component_length = component_length,
    };
}
fn bounds(host: anytype, principal: u64, payload: Value) !Engine.Bounds {
    const b = try field(payload, "b");
    const lower = try host.inputBlob(principal, try field(b, "lower"));
    const upper = try host.inputBlob(principal, try field(b, "upper"));
    const lower_open = try boolean(b, "lower_open");
    const upper_open = try boolean(b, "upper_open");
    const point = try boolean(b, "is_point");
    if (point and (lower_open or upper_open or !std.mem.eql(u8, lower, upper))) return error.InvalidRequest;
    return .{ .lower = lower, .upper = upper, .lower_op = if (lower_open) "> " else ">= ", .upper_op = if (upper_open) "< " else "<= ", .is_point = point };
}
fn blob(host: anytype, principal: u64, v: Value, name: []const u8) ![]const u8 {
    return host.inputBlob(principal, try field(v, name));
}
fn output(a: Allocator, v: anytype) ![]u8 {
    return std.json.Stringify.valueAlloc(a, v, .{});
}
fn decimal(a: Allocator, n: i64) ![]const u8 {
    return std.fmt.allocPrint(a, "{d}", .{n});
}
fn optionalDecimal(a: Allocator, n: ?i64) !?[]const u8 {
    return if (n) |x| try decimal(a, x) else null;
}
fn outputBlob(host: anytype, principal: u64, value: ?[]const u8) !?(@typeInfo(@TypeOf(host.outputBlob(principal, ""))).error_union.payload) {
    return if (value) |v| try host.outputBlob(principal, v) else null;
}

// The host holds the origin transaction gate throughout the streamed snapshot.
fn schemaSnapshot(host: anytype, engine: *Engine, principal: u64, ticket: u64, a: Allocator, database_id: i64) ![]u8 {
    const next_store_id = try decimal(a, try engine.nextStoreId());
    const next_index_id = try decimal(a, try engine.nextIndexId());
    const rows = try engine.schemaSnapshotRows(database_id);
    const ref = host.addMetadataRows(principal, ticket, rows, .schema) catch |err| {
        rows.deinit();
        return err;
    };
    return output(a, .{ .rows_id = ref.rows_id, .next_store_id = next_store_id, .next_index_id = next_index_id });
}

// The host must hold the origin's transaction gate for the authenticated ticket.
// Generic host typing avoids an import cycle with the host implementation.
pub fn dispatch(host: anytype, engine: *Engine, principal: u64, ticket: u64, a: Allocator, operation: []const u8, p: Value) ![]u8 {
    const op = if (std.mem.startsWith(u8, operation, "idb.")) operation[4..] else return error.UnknownOperation;
    const eq = std.mem.eql;
    if (eq(u8, op, "schemaSnapshot")) {
        if (ticket == 0) return error.ProfileInvalidTransaction;
        return schemaSnapshot(host, engine, principal, ticket, a, try integer(p, "database_id"));
    }
    if (eq(u8, op, "begin")) {
        try engine.begin(null);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "commit")) {
        try engine.commit(null);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "rollback")) {
        engine.rollback(null);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "savepoint")) {
        try engine.savepoint(null);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "releaseSavepoint")) {
        try engine.releaseSavepoint(null);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "rollbackSavepoint")) {
        engine.rollbackSavepoint(null);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "databaseId")) return output(a, try optionalDecimal(a, try engine.databaseId(null, try blob(host, principal, p, "name"))));
    if (eq(u8, op, "databaseVersion")) return output(a, try optionalDecimal(a, try engine.databaseVersion(null, try blob(host, principal, p, "name"))));
    if (eq(u8, op, "databases")) {
        const rows = try engine.databaseMetadataRows();
        const ref = host.addMetadataRows(principal, ticket, rows, .databases) catch |err| {
            rows.deinit();
            return err;
        };
        return output(a, ref);
    }
    if (eq(u8, op, "upsertDatabase")) return output(a, try decimal(a, try engine.upsertDatabase(null, try blob(host, principal, p, "name"), try integer(p, "version"))));
    if (eq(u8, op, "deleteDatabase")) {
        try engine.deleteDatabase(null, try blob(host, principal, p, "name"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "objectStoreId")) return output(a, try optionalDecimal(a, try engine.objectStoreId(null, try integer(p, "database_id"), try blob(host, principal, p, "name"))));
    if (eq(u8, op, "objectStoreInfo")) {
        const info = try engine.objectStoreInfo(null, a, try integer(p, "database_id"), try blob(host, principal, p, "name")) orelse return output(a, @as(?u8, null));
        return output(a, .{ .id = try decimal(a, info.id), .key_path = try pathValue(host, principal, a, info.key_path), .auto_increment = info.auto_increment });
    }
    if (eq(u8, op, "objectStoreNames")) {
        const rows = try engine.objectStoreNameRows(try integer(p, "database_id"));
        const ref = host.addMetadataRows(principal, ticket, rows, .names) catch |err| {
            rows.deinit();
            return err;
        };
        return output(a, ref);
    }
    if (eq(u8, op, "createObjectStore")) return output(a, try decimal(a, try engine.createObjectStore(null, a, try integer(p, "database_id"), try blob(host, principal, p, "name"), try keyPath(host, principal, a, try field(p, "key_path")), try boolean(p, "auto_increment"))));
    if (eq(u8, op, "createObjectStoreWithId")) {
        if (ticket == 0) return error.ProfileInvalidTransaction;
        const id = try integer(p, "id");
        if (id <= 0) return error.InvalidRequest;
        return output(a, try decimal(a, try engine.createObjectStoreWithId(id, a, try integer(p, "database_id"), try blob(host, principal, p, "name"), try keyPath(host, principal, a, try field(p, "key_path")), try boolean(p, "auto_increment"))));
    }
    if (eq(u8, op, "renameObjectStore")) {
        try engine.renameObjectStore(null, try integer(p, "object_store_id"), try blob(host, principal, p, "name"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "deleteObjectStore")) {
        try engine.deleteObjectStore(null, try integer(p, "database_id"), try blob(host, principal, p, "name"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "nextGeneratedKey")) return output(a, try decimal(a, try engine.nextGeneratedKey(null, try integer(p, "store_id"))));
    if (eq(u8, op, "maybeBumpGenerator")) {
        try engine.maybeBumpGenerator(null, try integer(p, "store_id"), try number(p, "key"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "indexExists")) return output(a, try engine.indexExists(null, try integer(p, "object_store_id"), try blob(host, principal, p, "name")));
    if (eq(u8, op, "createIndexRow")) {
        const kp = (try keyPath(host, principal, a, try field(p, "key_path"))) orelse return error.InvalidRequest;
        return output(a, try decimal(a, try engine.createIndexRow(null, a, try integer(p, "object_store_id"), try blob(host, principal, p, "name"), kp, try boolean(p, "unique"), try boolean(p, "multi_entry"))));
    }
    if (eq(u8, op, "createIndexRowWithId")) {
        if (ticket == 0) return error.ProfileInvalidTransaction;
        const id = try integer(p, "id");
        if (id <= 0) return error.InvalidRequest;
        const kp = (try keyPath(host, principal, a, try field(p, "key_path"))) orelse return error.InvalidRequest;
        return output(a, try decimal(a, try engine.createIndexRowWithId(id, a, try integer(p, "object_store_id"), try blob(host, principal, p, "name"), kp, try boolean(p, "unique"), try boolean(p, "multi_entry"))));
    }
    if (eq(u8, op, "renameIndex")) {
        try engine.renameIndex(null, try integer(p, "index_id"), try blob(host, principal, p, "name"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "deleteIndexRow")) {
        try engine.deleteIndexRow(null, try integer(p, "object_store_id"), try blob(host, principal, p, "name"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "indexNames")) {
        const rows = try engine.indexNameRows(try integer(p, "object_store_id"));
        const ref = host.addMetadataRows(principal, ticket, rows, .names) catch |err| {
            rows.deinit();
            return err;
        };
        return output(a, ref);
    }
    if (eq(u8, op, "indexInfo")) {
        const info = try engine.indexInfo(null, a, try integer(p, "object_store_id"), try blob(host, principal, p, "name")) orelse return output(a, @as(?u8, null));
        return output(a, .{ .id = try decimal(a, info.id), .key_path = try pathValue(host, principal, a, info.key_path), .unique = info.unique, .multi_entry = info.multi_entry });
    }
    if (eq(u8, op, "indexesForStore")) {
        const rows = try engine.indexMetadataRows(try integer(p, "object_store_id"));
        const ref = host.addMetadataRows(principal, ticket, rows, .indexes) catch |err| {
            rows.deinit();
            return err;
        };
        return output(a, ref);
    }
    if (eq(u8, op, "add") or eq(u8, op, "put")) {
        const id = try integer(p, "object_store_id");
        const key = try blob(host, principal, p, "key");
        const value = try blob(host, principal, p, "value");
        if (eq(u8, op, "add")) try engine.add(null, id, key, value) else try engine.put(null, id, key, value);
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "get")) return output(a, try outputBlob(host, principal, try engine.get(null, a, try integer(p, "object_store_id"), try blob(host, principal, p, "key"))));
    if (eq(u8, op, "clear")) {
        try engine.clear(null, try integer(p, "object_store_id"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "addIndexRecord")) {
        try engine.addIndexRecord(null, try integer(p, "index_id"), try blob(host, principal, p, "key"), try blob(host, principal, p, "primary_key"), try boolean(p, "unique"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "deleteIndexRecordsForKey")) {
        try engine.deleteIndexRecordsForKey(null, try integer(p, "object_store_id"), try blob(host, principal, p, "primary_key"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "clearIndexRecordsForStore")) {
        try engine.clearIndexRecordsForStore(null, try integer(p, "object_store_id"));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "deleteIndexRecordsForRange")) {
        try engine.deleteIndexRecordsForRange(null, try integer(p, "object_store_id"), try bounds(host, principal, p));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "deleteRange")) {
        try engine.deleteRange(null, try integer(p, "object_store_id"), try bounds(host, principal, p));
        return output(a, @as(?u8, null));
    }
    if (eq(u8, op, "countRange")) return output(a, try decimal(a, try engine.countRange(null, try integer(p, "object_store_id"), try bounds(host, principal, p))));
    if (eq(u8, op, "indexCountRange")) return output(a, try decimal(a, try engine.indexCountRange(null, try integer(p, "index_id"), try bounds(host, principal, p))));
    if (eq(u8, op, "getRange")) return output(a, try outputBlob(host, principal, try engine.getRange(null, a, try integer(p, "object_store_id"), try bounds(host, principal, p))));
    if (eq(u8, op, "getKeyRange")) return output(a, try outputBlob(host, principal, try engine.getKeyRange(null, a, try integer(p, "object_store_id"), try bounds(host, principal, p))));
    if (eq(u8, op, "indexGetRange")) return output(a, try outputBlob(host, principal, try engine.indexGetRange(null, a, try integer(p, "object_store_id"), try integer(p, "index_id"), try bounds(host, principal, p))));
    if (eq(u8, op, "indexGetKeyRange")) return output(a, try outputBlob(host, principal, try engine.indexGetKeyRange(null, a, try integer(p, "index_id"), try bounds(host, principal, p))));
    if (eq(u8, op, "getAllRows")) {
        var rows = try engine.getAllRows(null, try integer(p, "object_store_id"), try bounds(host, principal, p), try boolean(p, "reverse"), try limit(p));
        const ref = host.addRows(principal, ticket, rows, 2) catch |err| {
            rows.deinit();
            return err;
        };
        return output(a, ref);
    }
    if (eq(u8, op, "indexGetAllRows")) {
        var rows = try engine.indexGetAllRows(null, try integer(p, "object_store_id"), try integer(p, "index_id"), try bounds(host, principal, p), try boolean(p, "reverse"), try boolean(p, "unique"), try limit(p));
        const ref = host.addRows(principal, ticket, rows, 3) catch |err| {
            rows.deinit();
            return err;
        };
        return output(a, ref);
    }
    if (eq(u8, op, "cursorSeek")) {
        const name = try text(p, "from_op");
        const from_op = if (eq(u8, name, "gt")) "> " else if (eq(u8, name, "gte")) ">= " else if (eq(u8, name, "lt")) "< " else if (eq(u8, name, "lte")) "<= " else return error.InvalidRequest;
        const row = try engine.cursorSeek(null, a, try integer(p, "object_store_id"), try bounds(host, principal, p), try boolean(p, "reverse"), from_op, try blob(host, principal, p, "from_key"), try unsigned(p, "offset"), try boolean(p, "with_value")) orelse return output(a, @as(?u8, null));
        return output(a, .{ .key = try host.outputBlob(principal, row.key), .value = try outputBlob(host, principal, row.value) });
    }
    if (eq(u8, op, "indexCursorSeek")) {
        const row = try engine.indexCursorSeek(null, a, try integer(p, "object_store_id"), try integer(p, "index_id"), try bounds(host, principal, p), try boolean(p, "reverse"), try boolean(p, "unique"), try blob(host, principal, p, "from_key"), try blob(host, principal, p, "from_pk"), try boolean(p, "pk_inclusive"), try boolean(p, "with_value"), try unsigned(p, "offset")) orelse return output(a, @as(?u8, null));
        return output(a, .{ .key = try host.outputBlob(principal, row.key), .primary_key = try host.outputBlob(principal, row.primary_key), .value = try outputBlob(host, principal, row.value) });
    }
    return error.UnknownOperation;
}
