// Copyright (C) 2023-2025  Lightpanda (Selecy SAS)
//
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

const std = @import("std");
const js = @import("../../js/js.zig");
const Sqlite = @import("../../../storage/sqlite/Sqlite.zig");
const Persistence = @import("Persistence.zig");
const ProfileIpc = @import("ProfileIpc.zig");

const Allocator = std.mem.Allocator;

pub fn registerTypes() []const type {
    return &.{Lookup};
}

pub const Cookie = @import("Cookie.zig");

pub const Shed = struct {
    _origins: std.StringHashMapUnmanaged(*Bucket) = .empty,
    path: ?[]const u8 = null,
    // Installed only by a qualified shared-Profile runtime. Session storage
    // remains local to its top-level browsing context.
    profile_client: ?*ProfileIpc.Client = null,

    pub fn deinit(self: *Shed, allocator: Allocator) void {
        var it = self._origins.iterator();
        while (it.next()) |kv| {
            allocator.free(kv.key_ptr.*);
            kv.value_ptr.*.deinit();
            allocator.destroy(kv.value_ptr.*);
        }
        self._origins.deinit(allocator);
    }

    pub fn getOrPut(self: *Shed, allocator: Allocator, origin: []const u8) !*Bucket {
        const gop = try self._origins.getOrPut(allocator, origin);
        if (gop.found_existing) return gop.value_ptr.*;
        errdefer std.debug.assert(self._origins.remove(origin));

        const bucket = try allocator.create(Bucket);
        errdefer allocator.destroy(bucket);
        bucket.* = .init(allocator);
        errdefer bucket.deinit();
        if (self.profile_client) |client| {
            bucket.local._profile_origin = try allocator.dupe(u8, origin);
            bucket.local._profile_client = client;
            try bucket.local.refreshProfile();
        } else if (self.path) |dir| {
            var digest: [32]u8 = undefined;
            std.crypto.hash.sha2.Sha256.hash(origin, &digest, .{});
            const path = try allocator.printSentinel("{s}/local-{s}.sqlite", .{ dir, std.fmt.bytesToHex(digest, .lower) }, 0);
            defer allocator.free(path);
            const opened = try Persistence.open(path);
            const conn = opened.conn;
            errdefer conn.close();
            if (opened.existing) {
                try Persistence.validateSchema(conn, .local);
            } else {
                try conn.exec("create table entries (key text primary key, value text not null)", .{});
            }
            var rows = try conn.rows("select key, value from entries", .{});
            defer rows.deinit();
            while (try rows.next()) |row| {
                try bucket.local.setItem(row.get([]const u8, 0), row.get([]const u8, 1));
            }
            bucket.local._conn = conn;
        }

        gop.key_ptr.* = try allocator.dupe(u8, origin);
        gop.value_ptr.* = bucket;
        return bucket;
    }
};

pub const Bucket = struct {
    local: Lookup,

    pub fn init(allocator: Allocator) Bucket {
        return .{
            .local = .{ ._allocator = allocator },
        };
    }

    pub fn deinit(self: *Bucket) void {
        self.local.deinit();
    }
};

// One live top-level browsing context's sessionStorage, separated by origin.
// Unlike Shed, this namespace has no persistence path or SQLite connection.
// Session owns it across replacement Pages until the tab is closed.
pub const SessionNamespace = struct {
    _origins: std.StringHashMapUnmanaged(*Lookup) = .empty,

    pub fn deinit(self: *SessionNamespace, allocator: Allocator) void {
        var it = self._origins.iterator();
        while (it.next()) |kv| {
            allocator.free(kv.key_ptr.*);
            kv.value_ptr.*.deinit();
            allocator.destroy(kv.value_ptr.*);
        }
        self._origins.deinit(allocator);
    }

    pub fn getOrPut(self: *SessionNamespace, allocator: Allocator, origin: []const u8) !*Lookup {
        const gop = try self._origins.getOrPut(allocator, origin);
        if (gop.found_existing) return gop.value_ptr.*;
        errdefer std.debug.assert(self._origins.remove(origin));

        const lookup = try allocator.create(Lookup);
        errdefer allocator.destroy(lookup);
        lookup.* = .{ ._allocator = allocator };
        gop.key_ptr.* = try allocator.dupe(u8, origin);
        gop.value_ptr.* = lookup;
        return lookup;
    }
};

pub const Lookup = struct {
    _data: std.StringHashMapUnmanaged([]const u8) = .empty,
    _size: usize = 0,
    _allocator: Allocator,
    _conn: ?Sqlite.Conn = null,
    _profile_client: ?*ProfileIpc.Client = null,
    _profile_origin: ?[]const u8 = null,

    const max_size = 5 * 1024 * 1024;

    pub fn deinit(self: *Lookup) void {
        if (self._conn) |conn| conn.close();
        if (self._profile_origin) |origin| self._allocator.free(origin);
        var it = self._data.iterator();
        while (it.next()) |entry| {
            self._allocator.free(entry.key_ptr.*);
            self._allocator.free(entry.value_ptr.*);
        }
        self._data.deinit(self._allocator);
        self._size = 0;
    }

    const profile_chunk_size = 192 * 1024;

    fn resultObject(response: std.json.Value) !std.json.ObjectMap {
        if (response != .object) return error.ProfileProtocolError;
        const result = response.object.get("result") orelse return error.ProfileProtocolError;
        if (result != .object) return error.ProfileProtocolError;
        return result.object;
    }

    fn profileToken(response: std.json.Value) ![]const u8 {
        const fields = try resultObject(response);
        const token = fields.get("token") orelse return error.ProfileProtocolError;
        if (token != .string or token.string.len == 0) return error.ProfileProtocolError;
        return token.string;
    }

    // A snapshot is frozen by the service before it is transferred. Only a
    // complete, validated replacement becomes visible to the JS bindings.
    fn refreshProfile(self: *Lookup) !void {
        const client = self._profile_client orelse return;
        const origin = self._profile_origin.?;
        const begin = try client.request(self._allocator, "local.snapshot.begin", .{ .origin = origin });
        defer begin.deinit();
        const token = try profileToken(begin.value);
        defer {
            const end = client.request(self._allocator, "local.snapshot.end", .{ .origin = origin, .token = token }) catch null;
            if (end) |response| response.deinit();
        }
        const fields = try resultObject(begin.value);
        const count_value = fields.get("count") orelse return error.ProfileProtocolError;
        if (count_value != .integer or count_value.integer < 0) return error.ProfileProtocolError;
        const record_count = std.math.cast(usize, count_value.integer) orelse return error.ProfileProtocolError;
        var replacement = Lookup{ ._allocator = self._allocator };
        errdefer replacement.deinit();
        for (0..record_count) |_| {
            const next = try client.request(self._allocator, "local.snapshot.next", .{ .origin = origin, .token = token });
            defer next.deinit();
            const record = try resultObject(next.value);
            const key_length = record.get("key_length") orelse return error.ProfileProtocolError;
            const value_length = record.get("value_length") orelse return error.ProfileProtocolError;
            if (key_length != .integer or value_length != .integer or key_length.integer < 0 or value_length.integer < 0) return error.ProfileProtocolError;
            const key_len = std.math.cast(usize, key_length.integer) orelse return error.ProfileProtocolError;
            const value_len = std.math.cast(usize, value_length.integer) orelse return error.ProfileProtocolError;
            const total = std.math.add(usize, key_len, value_len) catch return error.ProfileProtocolError;
            if (value_len > max_size - replacement._size) return error.ProfileProtocolError;
            try replacement._data.ensureUnusedCapacity(self._allocator, 1);
            const key_bytes = try self._allocator.alloc(u8, key_len);
            errdefer self._allocator.free(key_bytes);
            const value = try self._allocator.alloc(u8, value_len);
            errdefer self._allocator.free(value);
            var offset: usize = 0;
            while (offset < total) {
                const part_response = try client.request(self._allocator, "local.snapshot.read", .{ .origin = origin, .token = token, .offset = offset });
                defer part_response.deinit();
                const part = (try resultObject(part_response.value)).get("data") orelse return error.ProfileProtocolError;
                if (part != .string) return error.ProfileProtocolError;
                const decoder = std.base64.standard.Decoder;
                const size = decoder.calcSizeForSlice(part.string) catch return error.ProfileProtocolError;
                if (size == 0 or size > profile_chunk_size or size > total - offset) return error.ProfileProtocolError;
                const decoded = try self._allocator.alloc(u8, size);
                defer self._allocator.free(decoded);
                decoder.decode(decoded, part.string) catch return error.ProfileProtocolError;
                const key_part = if (offset < key_len) @min(size, key_len - offset) else 0;
                if (key_part != 0) @memcpy(key_bytes[offset .. offset + key_part], decoded[0..key_part]);
                if (key_part < size) {
                    const value_offset = offset + key_part - key_len;
                    @memcpy(value[value_offset .. value_offset + size - key_part], decoded[key_part..]);
                }
                offset += size;
            }
            if (replacement._data.contains(key_bytes)) return error.ProfileProtocolError;
            replacement._data.putAssumeCapacity(key_bytes, value);
            replacement._size += value_len;
        }
        var old = Lookup{ ._allocator = self._allocator, ._data = self._data, ._size = self._size };
        self._data = replacement._data;
        self._size = replacement._size;
        old.deinit();
    }

    // Stage bytes under a principal-bound token; only commit changes storage.
    // The key and value are streamed together without constructing a second
    // full-sized copy, and even a failed transfer is explicitly discarded.
    fn mutateProfile(self: *Lookup, kind: []const u8, key_bytes: []const u8, value: []const u8) !void {
        const client = self._profile_client.?;
        const origin = self._profile_origin.?;
        const begin = try client.request(self._allocator, "local.mutation.begin", .{
            .origin = origin,
            .kind = kind,
            .key_length = key_bytes.len,
            .value_length = value.len,
        });
        defer begin.deinit();
        const token = try profileToken(begin.value);
        var committed = false;
        defer if (!committed) {
            const abort = client.request(self._allocator, "local.mutation.abort", .{ .origin = origin, .token = token }) catch null;
            if (abort) |response| response.deinit();
        };
        var offset: usize = 0;
        for ([_][]const u8{ key_bytes, value }) |segment| {
            var pos: usize = 0;
            while (pos < segment.len) {
                const end = pos + @min(profile_chunk_size, segment.len - pos);
                const part = segment[pos..end];
                const encoded = try self._allocator.alloc(u8, std.base64.standard.Encoder.calcSize(part.len));
                defer self._allocator.free(encoded);
                _ = std.base64.standard.Encoder.encode(encoded, part);
                const response = try client.request(self._allocator, "local.mutation.chunk", .{
                    .origin = origin,
                    .token = token,
                    .offset = offset,
                    .data = encoded,
                });
                response.deinit();
                offset += part.len;
                pos = end;
            }
        }
        const response = try client.request(self._allocator, "local.mutation.commit", .{ .origin = origin, .token = token });
        response.deinit();
        committed = true;
    }

    pub fn getItem(self: *const Lookup, k: []const u8) ?[]const u8 {
        return self._data.get(k);
    }

    pub fn setItem(self: *Lookup, k: []const u8, value: []const u8) !void {
        // The service makes the quota decision atomically with the mutation.
        if (self._profile_client != null) {
            return self.mutateProfile("set", k, value);
        }
        const old_len = if (self._data.get(k)) |old| old.len else 0;
        std.debug.assert(old_len <= self._size);
        if (self._size - old_len + value.len > max_size) {
            return error.QuotaExceeded;
        }

        if (self._data.getPtr(k)) |value_ptr| {
            const value_owned = try self._allocator.dupe(u8, value);
            errdefer self._allocator.free(value_owned);
            if (self._conn) |conn| try conn.exec("insert into entries(key,value) values (?1,?2) on conflict(key) do update set value=excluded.value", .{ k, value });
            self._size -= value_ptr.*.len;
            self._allocator.free(value_ptr.*);
            value_ptr.* = value_owned;
            self._size += value.len;
        } else {
            const key_owned = try self._allocator.dupe(u8, k);
            errdefer self._allocator.free(key_owned);
            const value_owned = try self._allocator.dupe(u8, value);
            errdefer self._allocator.free(value_owned);

            try self._data.ensureUnusedCapacity(self._allocator, 1);
            if (self._conn) |conn| try conn.exec("insert into entries(key,value) values (?1,?2) on conflict(key) do update set value=excluded.value", .{ k, value });
            self._data.putAssumeCapacity(key_owned, value_owned);
            self._size += value.len;
        }
    }

    pub fn removeItem(self: *Lookup, k: []const u8) !void {
        if (self._profile_client != null) {
            return self.mutateProfile("remove", k, "");
        }
        if (self._conn) |conn| try conn.exec("delete from entries where key=?1", .{k});
        const kv = self._data.fetchRemove(k) orelse return;
        self._size -= kv.value.len;
        self._allocator.free(kv.key);
        self._allocator.free(kv.value);
    }

    pub fn clear(self: *Lookup) !void {
        if (self._profile_client) |client| {
            const response = try client.request(self._allocator, "local.clear", .{ .origin = self._profile_origin.? });
            response.deinit();
            return;
        }
        if (self._conn) |conn| try conn.exec("delete from entries", .{});
        var it = self._data.iterator();
        while (it.next()) |entry| {
            self._allocator.free(entry.key_ptr.*);
            self._allocator.free(entry.value_ptr.*);
        }
        self._data.clearRetainingCapacity();
        self._size = 0;
    }

    pub fn key(self: *const Lookup, index: u32) ?[]const u8 {
        var it = self._data.keyIterator();
        var i: u32 = 0;
        while (it.next()) |k| {
            if (i == index) {
                return k.*;
            }
            i += 1;
        }
        return null;
    }

    pub fn getLength(self: *const Lookup) u32 {
        return @intCast(self._data.count());
    }

    fn hasItem(self: *const Lookup, k: []const u8) bool {
        return self._data.contains(k);
    }

    pub fn keys(self: *Lookup, exec: *const js.Execution) !js.Array {
        try self.refreshProfile();
        const local = exec.js.local.?;
        var arr = local.newArray(self.getLength());
        var it = self._data.keyIterator();
        var i: u32 = 0;
        while (it.next()) |k| : (i += 1) {
            _ = try arr.set(i, k.*, .{});
        }
        return arr;
    }

    pub const JsApi = struct {
        pub const bridge = js.Bridge(Lookup);

        pub const Meta = struct {
            pub const name = "Storage";
            pub var class_id: bridge.ClassId = undefined;
        };

        fn get(self: *Lookup, k: []const u8) !?[]const u8 {
            try self.refreshProfile();
            return self.getItem(k);
        }
        fn getLengthRemote(self: *Lookup) !u32 {
            try self.refreshProfile();
            return self.getLength();
        }
        fn getKeyRemote(self: *Lookup, index: u32) !?[]const u8 {
            try self.refreshProfile();
            return self.key(index);
        }
        pub const length = bridge.accessor(Lookup.JsApi.getLengthRemote, null, .{});
        pub const getItem = bridge.function(Lookup.JsApi.get, .{});
        pub const setItem = bridge.function(Lookup.setItem, .{});
        pub const removeItem = bridge.function(Lookup.removeItem, .{});
        pub const clear = bridge.function(Lookup.clear, .{});
        pub const key = bridge.function(Lookup.JsApi.getKeyRemote, .{});
        pub const @"[str]" = bridge.namedIndexed(Lookup.JsApi.get, Lookup.setItem, Lookup.removeItem, Lookup.keys, Lookup.JsApi.has, .{ .null_as_undefined = true });
        fn has(self: *Lookup, k: []const u8) !bool {
            try self.refreshProfile();
            return self.hasItem(k);
        }
        pub const @"[int]" = bridge.indexedReadWrite(_getByIndex, _setByIndex, _removeByIndex, _indexHas, null, .{ .null_as_undefined = true });

        // v8 routes storage[9] to the indexed interceptor, so we need to do the
        // int -> string conversion
        fn _getByIndex(self: *Lookup, idx: u32) !?[]const u8 {
            try self.refreshProfile();
            var buf: [10]u8 = undefined;
            return self._data.get(std.mem.print(&buf, "{d}", .{idx}) catch unreachable);
        }

        fn _setByIndex(self: *Lookup, idx: u32, value: []const u8) !void {
            var buf: [10]u8 = undefined;
            return self.setItem(std.mem.print(&buf, "{d}", .{idx}) catch unreachable, value);
        }

        fn _removeByIndex(self: *Lookup, idx: u32) !void {
            var buf: [10]u8 = undefined;
            try self.removeItem(std.mem.print(&buf, "{d}", .{idx}) catch unreachable);
        }

        fn _indexHas(self: *Lookup, idx: u32) !bool {
            try self.refreshProfile();
            var buf: [10]u8 = undefined;
            return self._data.contains(std.mem.print(&buf, "{d}", .{idx}) catch unreachable);
        }
    };
};

const testing = @import("../../../testing.zig");
test "WebApi: Storage" {
    try testing.htmlRunner("storage.html", .{});
}

test "local storage commits live mutations and restores isolated origins" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const path = try tmp.dir.realPathFileAlloc(testing.io, ".", testing.allocator);
    defer testing.allocator.free(path);
    var shed = Shed{ .path = path };
    defer shed.deinit(testing.allocator);
    const a = try shed.getOrPut(testing.allocator, "https://a.example");
    try a.local.setItem("key", "value");
    try testing.expectEqual(@as(i64, 2), (try a.local._conn.?.scalar(i64, "pragma synchronous", .{})).?);
    var restored = Shed{ .path = path };
    defer restored.deinit(testing.allocator);
    const a_again = try restored.getOrPut(testing.allocator, "https://a.example");
    const b = try restored.getOrPut(testing.allocator, "https://b.example");
    try testing.expectString("value", a_again.local.getItem("key").?);
    try testing.expectEqual(null, b.local.getItem("key"));
    try a.local.removeItem("key");
    try testing.expectEqual(@as(i64, 0), (try a_again.local._conn.?.scalar(i64, "select count(*) from entries", .{})).?);
    try a.local.setItem("other", "value");
    try a.local.clear();
    try testing.expectEqual(@as(i64, 0), (try a_again.local._conn.?.scalar(i64, "select count(*) from entries", .{})).?);
}
