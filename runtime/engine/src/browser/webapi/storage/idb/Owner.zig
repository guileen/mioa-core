// Trusted IndexedDB owner. The profile service holds the lease and is the only
// peer permitted to supply connection_id. No SQL crosses this boundary.
const std = @import("std");
const Engine = @import("Engine.zig");
const Sqlite = @import("../../../../storage/sqlite/Sqlite.zig");
const Dispatch = @import("OwnerDispatch.zig");
const Allocator = std.mem.Allocator;
const Owner = @This();

extern "c" fn tmpfile() ?*anyopaque;
extern "c" fn fileno(*anyopaque) c_int;
extern "c" fn fclose(*anyopaque) c_int;
extern "c" fn ftruncate(c_int, i64) c_int;
extern "c" fn pread(c_int, [*]u8, usize, i64) isize;
extern "c" fn pwrite(c_int, [*]const u8, usize, i64) isize;

const max_message = 1024 * 1024;
const chunk_size = 128 * 1024;
const max_handles = 64;
// Frame bounds are transport bounds, not value/name limits. Blobs are spooled
// on disk; materialization gets a separate parent-ledger grant before SQLite.
const max_spooled = 64 * @as(usize, 1_000_000_000);
const max_open_rows = 2;
const owner_handle_grant = 96;
pub const BlobRef = struct { blob_id: []const u8, size: []const u8 };
pub const RowsRef = struct { rows_id: []const u8 };
const Blob = struct {
    principal: u64,
    file: *anyopaque,
    size: usize,
    written: usize,
    id_text: []const u8,
    size_text: []const u8,
    disk_grant: u64,
    mapped: ?[]align(std.heap.page_size_min) u8 = null,
};
pub const MetadataKind = enum { databases, names, indexes, schema };
const SchemaPath = struct { data: BlobRef, component_length: []const u8 };
const SchemaIndex = struct { name: BlobRef, id: []const u8, key_path: SchemaPath, unique: bool, multi_entry: bool };
const Rows = struct {
    principal: u64,
    ticket: u64,
    origin: []const u8,
    rows: ?Engine.Rows = null,
    metadata: ?Sqlite.Rows = null,
    kind: ?MetadataKind = null,
    columns: u8 = 0,
    id_text: []const u8,
    finished: bool = false,
};
const Ticket = struct { id: u64, principal: u64, active: bool = false };
const Origin = struct { engine: Engine, reader: Engine, row_upper: usize = 0, tickets: std.ArrayList(Ticket) = .empty };
const Grant = struct { principal: u64, origin: []const u8, ticket: u64 };
const GrantWire = struct { type: []const u8 = "idb_gate", connection_id: u64, origin: []const u8, ticket: []const u8, state: []const u8 = "granted" };

allocator: Allocator,
directory: []const u8,
max_value: usize,
request_reservation: usize,
origins: std.StringHashMapUnmanaged(*Origin) = .empty,
blobs: std.AutoHashMapUnmanaged(u64, Blob) = .empty,
rows: std.AutoHashMapUnmanaged(u64, Rows) = .empty,
spooled: usize = 0,
next_id: u64 = 1,
grants: [max_handles]Grant = undefined,
grant_count: usize = 0,
current_origin: ?[]const u8 = null,
lifetime_grant: u64 = 0,
grant_fd: std.posix.fd_t = -1,
grant_request_id: u64 = 2,
memory_reserved: usize = 64 * 1024 * 1024,
memory_grants: std.ArrayList(u64) = .empty,

fn requestGrant(self: *Owner, operation: u32, token: u64, bytes: u64, handles: u64) !u64 {
    const id = self.grant_request_id;
    self.grant_request_id = std.math.add(u64, id, 1) catch return error.ProfileGrantProtocol;
    return memoryGrant(self.grant_fd, operation, id, token, bytes, handles);
}

fn admitMaterialization(self: *Owner, demand: usize) !void {
    const envelope = std.math.add(usize, 64 * 1024 * 1024, try std.math.mul(usize, demand, 16)) catch return error.QuotaExceeded;
    if (envelope > self.memory_reserved) {
        try self.memory_grants.ensureUnusedCapacity(self.allocator, 1);
        const extra = envelope - self.memory_reserved;
        const id = self.grant_request_id;
        self.grant_request_id += 1;
        const grant = try memoryGrant(self.grant_fd, 4, id, self.lifetime_grant, extra, 0);
        self.memory_grants.appendAssumeCapacity(grant);
        self.memory_reserved = envelope;
    }
    self.request_reservation = @max(8 * 1024 * 1024, try std.math.mul(usize, demand, 4));
}

fn prepareMaterialization(self: *Owner, op: []const u8, payload: std.json.Value) !void {
    self.request_reservation = 8 * 1024 * 1024;
    if (std.mem.startsWith(u8, op, "idb.blob_") or std.mem.eql(u8, op, "idb.disconnect") or std.mem.eql(u8, op, "idb.capabilities")) return;
    for ([_][]const u8{ "idb.acquire", "idb.poll", "idb.cancel", "idb.begin", "idb.commit", "idb.rollback", "idb.rows_close" }) |control| {
        if (std.mem.eql(u8, op, control)) return;
    }
    const principal = try principalOf(payload);
    const origin = try string(try field(payload, "origin"));
    const o = try self.originFor(origin);
    var demand: usize = 0;
    var blobs = self.blobs.valueIterator();
    while (blobs.next()) |b| {
        if (b.principal == principal) demand = std.math.add(usize, demand, b.size) catch return error.QuotaExceeded;
    }
    o.row_upper = @max(o.row_upper, demand);
    try self.admitMaterialization(o.row_upper);
}

fn nextId(self: *Owner) !u64 {
    const id = self.next_id;
    self.next_id = std.math.add(u64, id, 1) catch return error.ProfileHandleExhausted;
    return id;
}
fn text(self: *Owner, id: anytype) ![]const u8 {
    return std.fmt.allocPrint(self.allocator, "{d}", .{id});
}
fn object(value: std.json.Value) !std.json.ObjectMap {
    if (value != .object) return error.ProfileProtocolError;
    return value.object;
}
fn field(value: std.json.Value, name: []const u8) !std.json.Value {
    return (try object(value)).get(name) orelse error.ProfileProtocolError;
}
fn string(value: std.json.Value) ![]const u8 {
    if (value != .string) return error.ProfileProtocolError;
    return value.string;
}
fn number(value: std.json.Value) !u64 {
    return std.fmt.parseInt(u64, try string(value), 10) catch error.ProfileProtocolError;
}
fn principalOf(payload: std.json.Value) !u64 {
    const value = try field(payload, "connection_id");
    if (value != .integer or value.integer < 0) return error.ProfileProtocolError;
    return @intCast(value.integer);
}
fn originFor(self: *Owner, name: []const u8) !*Origin {
    if (self.origins.get(name)) |o| return o;
    if (name.len == 0 or name.len > 8192) return error.ProfileLimitExceeded;
    if (self.origins.count() >= 4) {
        // Idle origins need not consume a permanent connection budget.
        var victim: ?[]const u8 = null;
        if (self.rows.count() == 0) {
            var it = self.origins.iterator();
            while (it.next()) |entry| {
                if (entry.value_ptr.*.tickets.items.len == 0) {
                    victim = entry.key_ptr.*;
                    break;
                }
            }
        }
        const key = victim orelse return error.QuotaExceeded;
        const entry = self.origins.fetchRemove(key).?;
        const old = entry.value;
        old.reader.close();
        old.engine.close();
        old.tickets.deinit(self.allocator);
        self.allocator.destroy(old);
        self.allocator.free(entry.key);
    }
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(name, &digest, .{});
    const path = try self.allocator.printSentinel("{s}/idb-{s}.sqlite", .{ self.directory, std.fmt.bytesToHex(digest, .lower) }, 0);
    defer self.allocator.free(path);
    const o = try self.allocator.create(Origin);
    errdefer self.allocator.destroy(o);
    o.* = .{ .engine = try Engine.open(path), .reader = undefined };
    errdefer o.engine.close();
    o.reader = .{ .conn = try Sqlite.Conn.openReadOnly(path) };
    errdefer o.reader.close();
    o.engine.conn.copy_rows = true;
    o.reader.conn.copy_rows = true;
    // length(CAST(... AS BLOB)) reads byte lengths without returning large
    // values to the owner. This initial high-water bound also covers existing
    // databases, not just values uploaded during this owner lifetime.
    const row_upper = (try o.engine.conn.scalar(i64,
        \\ SELECT
        \\ coalesce((SELECT max(length(key)+length(value)) FROM idb_records),0) +
        \\ coalesce((SELECT max(length(key)+length(primary_key)) FROM idb_index_records),0) +
        \\ coalesce((SELECT max(length(cast(name AS blob))) FROM idb_databases),0) +
        \\ coalesce((SELECT max(length(cast(name AS blob))+coalesce(length(cast(key_path AS blob)),0)) FROM idb_object_stores),0) +
        \\ coalesce((SELECT max(length(cast(name AS blob))+length(cast(key_path AS blob))) FROM idb_indexes),0)
    , .{})) orelse 0;
    o.row_upper = std.math.cast(usize, row_upper) orelse return error.QuotaExceeded;
    try o.engine.conn.exec("pragma cache_size=-256", .{});
    try o.engine.conn.exec("pragma mmap_size=0", .{});
    const key = try self.allocator.dupe(u8, name);
    errdefer self.allocator.free(key);
    try o.reader.conn.exec("pragma cache_size=-256", .{});
    try o.reader.conn.exec("pragma mmap_size=0", .{});
    try self.origins.put(self.allocator, key, o);
    return o;
}
fn refreshReader(self: *Owner, origin: []const u8, o: *Origin) !void {
    // An outstanding reader statement pins its original WAL snapshot. Do not
    // let an unrelated committed-metadata request inherit that stale view.
    var it = self.rows.valueIterator();
    var pinned = false;
    while (it.next()) |row| {
        if (row.ticket == 0 and std.mem.eql(u8, row.origin, origin)) {
            pinned = true;
            break;
        }
    }
    if (!pinned) return;
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(origin, &digest, .{});
    const path = try self.allocator.printSentinel("{s}/idb-{s}.sqlite", .{ self.directory, std.fmt.bytesToHex(digest, .lower) }, 0);
    defer self.allocator.free(path);
    var reader: Engine = .{ .conn = try Sqlite.Conn.openReadOnly(path) };
    errdefer reader.close();
    reader.conn.copy_rows = true;
    try reader.conn.exec("pragma cache_size=-256", .{});
    try reader.conn.exec("pragma mmap_size=0", .{});
    // sqlite3_close_v2 defers finalization until any outstanding statements
    // from the previous connection have been closed.
    o.reader.close();
    o.reader = reader;
}
fn ticketIndex(o: *Origin, principal: u64, id: u64) !usize {
    for (o.tickets.items, 0..) |ticket, i| {
        if (ticket.id == id and ticket.principal == principal) return i;
    }
    return error.ProfileInvalidHandle;
}
fn closeRows(self: *Owner, id: u64) void {
    if (self.rows.fetchRemove(id)) |entry| {
        const r = entry.value;
        if (r.rows) |rows| {
            var mutable = rows;
            mutable.deinit();
        }
        if (r.metadata) |rows| rows.deinit();
        self.allocator.free(r.origin);
        self.allocator.free(r.id_text);
    }
}
fn closeTicketRows(self: *Owner, ticket: u64) void {
    while (true) {
        var found: ?u64 = null;
        var it = self.rows.iterator();
        while (it.next()) |e| if (e.value_ptr.ticket == ticket) {
            found = e.key_ptr.*;
            break;
        };
        if (found) |id| self.closeRows(id) else break;
    }
}
fn dropBlob(self: *Owner, id: u64) void {
    if (self.blobs.fetchRemove(id)) |entry| {
        const b = entry.value;
        self.spooled -= b.size;
        if (b.mapped) |mapped| std.posix.munmap(mapped);
        if (fclose(b.file) == 0) _ = self.requestGrant(2, b.disk_grant, 0, 0) catch 0;
        self.allocator.free(b.id_text);
        self.allocator.free(b.size_text);
    }
}
fn cancel(self: *Owner, origin: []const u8, o: *Origin, i: usize) void {
    const ticket = o.tickets.items[i];
    self.closeTicketRows(ticket.id);
    if (ticket.active) o.engine.rollback(null);
    _ = o.tickets.orderedRemove(i);
    if (i == 0 and o.tickets.items.len != 0) {
        const next = o.tickets.items[0];
        self.grants[self.grant_count] = .{ .principal = next.principal, .origin = origin, .ticket = next.id };
        self.grant_count += 1;
    }
}
fn disconnect(self: *Owner, principal: u64) void {
    while (true) {
        var id: ?u64 = null;
        var it = self.rows.iterator();
        while (it.next()) |e| if (e.value_ptr.principal == principal) {
            id = e.key_ptr.*;
            break;
        };
        if (id) |v| self.closeRows(v) else break;
    }
    var origins = self.origins.iterator();
    while (origins.next()) |entry| {
        const o = entry.value_ptr.*;
        var i = o.tickets.items.len;
        while (i > 0) {
            i -= 1;
            if (o.tickets.items[i].principal == principal) self.cancel(entry.key_ptr.*, o, i);
        }
    }
    while (true) {
        var id: ?u64 = null;
        var it = self.blobs.iterator();
        while (it.next()) |e| if (e.value_ptr.principal == principal) {
            id = e.key_ptr.*;
            break;
        };
        if (id) |v| self.dropBlob(v) else break;
    }
}
fn deinit(self: *Owner) void {
    while (self.rows.count() != 0) {
        var it = self.rows.iterator();
        self.closeRows(it.next().?.key_ptr.*);
    }
    while (self.blobs.count() != 0) {
        var it = self.blobs.iterator();
        self.dropBlob(it.next().?.key_ptr.*);
    }
    self.rows.deinit(self.allocator);
    self.blobs.deinit(self.allocator);
    var it = self.origins.iterator();
    while (it.next()) |entry| {
        const o = entry.value_ptr.*;
        for (o.tickets.items) |ticket| if (ticket.active) {
            o.engine.rollback(null);
            break;
        };
        o.tickets.deinit(self.allocator);
        o.reader.close();
        o.engine.close();
        self.allocator.destroy(o);
        self.allocator.free(entry.key_ptr.*);
    }
    self.origins.deinit(self.allocator);
    for (self.memory_grants.items) |grant| {
        _ = memoryGrant(self.grant_fd, 2, self.grant_request_id, grant, 0, 0) catch 0;
        self.grant_request_id += 1;
    }
    self.memory_grants.deinit(self.allocator);
}
fn newBlob(self: *Owner, principal: u64, size: usize) !BlobRef {
    if (self.blobs.count() >= max_handles or size > self.max_value or size > max_spooled - self.spooled) return error.QuotaExceeded;
    const disk_grant = try self.requestGrant(3, 0, size, 0);
    var release_grant = true;
    errdefer if (release_grant) {
        _ = self.requestGrant(2, disk_grant, 0, 0) catch 0;
    };
    const file = tmpfile() orelse return error.ProfileStorageError;
    errdefer if (fclose(file) != 0) {
        release_grant = false;
    };
    if (ftruncate(fileno(file), @intCast(size)) != 0) return error.ProfileStorageError;
    const id = try self.nextId();
    const id_text = try self.text(id);
    errdefer self.allocator.free(id_text);
    const size_text = try self.text(size);
    errdefer self.allocator.free(size_text);
    try self.blobs.put(self.allocator, id, .{ .principal = principal, .file = file, .size = size, .written = 0, .id_text = id_text, .size_text = size_text, .disk_grant = disk_grant });
    self.spooled += size;
    return .{ .blob_id = id_text, .size = size_text };
}
fn blob(self: *Owner, principal: u64, value: std.json.Value) !*Blob {
    const id = try number(try field(value, "blob_id"));
    const b = self.blobs.getPtr(id) orelse return error.ProfileInvalidHandle;
    if (b.principal != principal) return error.ProfileInvalidHandle;
    return b;
}
fn writeAt(b: *Blob, offset: usize, bytes: []const u8) !void {
    var n: usize = 0;
    while (n < bytes.len) {
        const rc = pwrite(fileno(b.file), bytes.ptr + n, bytes.len - n, @intCast(offset + n));
        if (rc <= 0) return error.ProfileStorageError;
        n += @intCast(rc);
    }
}
pub fn inputBlob(self: *Owner, principal: u64, value: std.json.Value) ![]const u8 {
    const b = try self.blob(principal, value);
    if (b.written != b.size) return error.ProfileIncompleteBlob;
    if (b.size == 0) return "";
    if (b.mapped == null) b.mapped = try std.posix.mmap(null, b.size, .{ .READ = true }, .{ .TYPE = .PRIVATE }, fileno(b.file), 0);
    return b.mapped.?;
}
fn unmapInputs(self: *Owner) void {
    var it = self.blobs.valueIterator();
    while (it.next()) |b| {
        if (b.mapped) |mapping| {
            std.posix.munmap(mapping);
            b.mapped = null;
        }
    }
}
pub fn outputBlob(self: *Owner, principal: u64, bytes: []const u8) !BlobRef {
    const ref = try self.newBlob(principal, bytes.len);
    errdefer self.dropBlob(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable);
    const b = self.blobs.getPtr(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable).?;
    // Avoid retaining a second in-memory copy of a result.
    try writeAt(b, 0, bytes);
    b.written = bytes.len;
    return ref;
}
pub fn addRows(self: *Owner, principal: u64, ticket: u64, rows: Engine.Rows, columns: u8) !RowsRef {
    if (self.rows.count() >= max_open_rows or columns < 1 or columns > 3) return error.QuotaExceeded;
    const id = try self.nextId();
    const id_text = try self.text(id);
    errdefer self.allocator.free(id_text);
    const origin = try self.allocator.dupe(u8, self.current_origin orelse return error.ProfileInvalidHandle);
    errdefer self.allocator.free(origin);
    try self.rows.put(self.allocator, id, .{ .principal = principal, .ticket = ticket, .origin = origin, .rows = rows, .columns = columns, .id_text = id_text });
    return .{ .rows_id = id_text };
}
pub fn addMetadataRows(self: *Owner, principal: u64, ticket: u64, rows: Sqlite.Rows, kind: MetadataKind) !RowsRef {
    if (self.rows.count() >= max_open_rows) return error.QuotaExceeded;
    const id = try self.nextId();
    const id_text = try self.text(id);
    errdefer self.allocator.free(id_text);
    const origin = try self.allocator.dupe(u8, self.current_origin orelse return error.ProfileInvalidHandle);
    errdefer self.allocator.free(origin);
    try self.rows.put(self.allocator, id, .{ .principal = principal, .ticket = ticket, .origin = origin, .metadata = rows, .kind = kind, .id_text = id_text });
    return .{ .rows_id = id_text };
}
fn schemaRow(self: *Owner, a: Allocator, principal: u64, row: Sqlite.Row) ![]u8 {
    var refs: [4]BlobRef = undefined;
    var count: usize = 0;
    errdefer for (refs[0..count]) |ref| self.dropBlob(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable);
    refs[count] = try self.outputBlob(principal, row.get([]const u8, 0));
    const store_name = refs[count];
    count += 1;
    var store_path: ?SchemaPath = null;
    if (row.get(?[]const u8, 2)) |path| {
        refs[count] = try self.outputBlob(principal, path);
        const data = refs[count];
        count += 1;
        store_path = .{ .data = data, .component_length = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 3)}) };
    }
    var index: ?SchemaIndex = null;
    if (row.get(?[]const u8, 5)) |name| {
        refs[count] = try self.outputBlob(principal, name);
        const index_name = refs[count];
        count += 1;
        refs[count] = try self.outputBlob(principal, row.get([]const u8, 7));
        const index_path = refs[count];
        count += 1;
        index = .{
            .name = index_name,
            .id = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 6)}),
            .key_path = .{ .data = index_path, .component_length = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 8)}) },
            .unique = row.get(bool, 9),
            .multi_entry = row.get(bool, 10),
        };
    }
    return encode(a, .{
        .store = .{ .name = store_name, .id = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 1)}), .key_path = store_path, .auto_increment = row.get(bool, 4) },
        .index = index,
    });
}
fn metadataOperation(op: []const u8) bool {
    for ([_][]const u8{
        "idb.databaseId",      "idb.databaseVersion", "idb.databases",
        "idb.objectStoreId",   "idb.objectStoreInfo", "idb.objectStoreNames",
        "idb.indexExists",     "idb.indexInfo",       "idb.indexNames",
        "idb.indexesForStore",
    }) |name| if (std.mem.eql(u8, op, name)) return true;
    return false;
}
fn encode(a: Allocator, value: anytype) ![]u8 {
    return std.json.Stringify.valueAlloc(a, value, .{});
}
fn execute(self: *Owner, a: Allocator, op: []const u8, payload: std.json.Value) ![]u8 {
    defer self.unmapInputs();
    const principal = try principalOf(payload);
    if (std.mem.eql(u8, op, "idb.capabilities")) return encode(a, .{
        .max_value_bytes = try std.fmt.allocPrint(a, "{d}", .{self.max_value}),
        .request_arena_bytes = try std.fmt.allocPrint(a, "{d}", .{self.request_reservation}),
    });
    if (std.mem.eql(u8, op, "idb.disconnect")) {
        self.disconnect(principal);
        return a.dupe(u8, "null");
    }
    if (std.mem.eql(u8, op, "idb.blob_begin")) {
        const size = std.math.cast(usize, try number(try field(payload, "size"))) orelse return error.ProfileLimitExceeded;
        return encode(a, try self.newBlob(principal, size));
    }
    if (std.mem.eql(u8, op, "idb.blob_append")) {
        const b = try self.blob(principal, payload);
        const offset = std.math.cast(usize, try number(try field(payload, "offset"))) orelse return error.ProfileProtocolError;
        const encoded = try string(try field(payload, "data"));
        const decoder = std.base64.standard.Decoder;
        const len = decoder.calcSizeForSlice(encoded) catch return error.ProfileProtocolError;
        if (len > chunk_size or offset != b.written or len > b.size - b.written) return error.ProfileProtocolError;
        const bytes = try a.alloc(u8, len);
        try decoder.decode(bytes, encoded);
        try writeAt(b, offset, bytes);
        b.written += len;
        return a.dupe(u8, "null");
    }
    if (std.mem.eql(u8, op, "idb.blob_read")) {
        const b = try self.blob(principal, payload);
        const offset = std.math.cast(usize, try number(try field(payload, "offset"))) orelse return error.ProfileProtocolError;
        const length = try field(payload, "length");
        if (length != .integer or length.integer < 0 or length.integer > chunk_size or offset > b.written) return error.ProfileProtocolError;
        const len = @min(@as(usize, @intCast(length.integer)), b.written - offset);
        const bytes = try a.alloc(u8, len);
        var n: usize = 0;
        while (n < len) {
            const rc = pread(fileno(b.file), bytes.ptr + n, len - n, @intCast(offset + n));
            if (rc <= 0) return error.ProfileStorageError;
            n += @intCast(rc);
        }
        const buf = try a.alloc(u8, std.base64.standard.Encoder.calcSize(len));
        return encode(a, .{ .data = std.base64.standard.Encoder.encode(buf, bytes), .size = b.size_text });
    }
    if (std.mem.eql(u8, op, "idb.blob_drop")) {
        _ = try self.blob(principal, payload);
        self.dropBlob(try number(try field(payload, "blob_id")));
        return a.dupe(u8, "null");
    }
    if (std.mem.eql(u8, op, "idb.rows_next") or std.mem.eql(u8, op, "idb.rows_close")) {
        const id = try number(try field(payload, "rows_id"));
        const r = self.rows.getPtr(id) orelse return error.ProfileInvalidHandle;
        const ticket_value = try field(payload, "ticket");
        const ticket = if (ticket_value == .null) 0 else try number(ticket_value);
        if (r.principal != principal or r.ticket != ticket or !std.mem.eql(u8, r.origin, try string(try field(payload, "origin")))) return error.ProfileInvalidHandle;
        if (std.mem.eql(u8, op, "idb.rows_close")) {
            self.closeRows(id);
            return a.dupe(u8, "null");
        }
        if (r.finished) return a.dupe(u8, "null");
        if (r.metadata) |*rows| {
            const row = (try rows.next()) orelse {
                r.finished = true;
                return a.dupe(u8, "null");
            };
            return switch (r.kind.?) {
                .names => blk: {
                    const ref = try self.outputBlob(principal, row.get([]const u8, 0));
                    errdefer self.dropBlob(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable);
                    break :blk encode(a, ref);
                },
                .databases => blk: {
                    const ref = try self.outputBlob(principal, row.get([]const u8, 0));
                    errdefer self.dropBlob(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable);
                    break :blk encode(a, .{ .name = ref, .version = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 1)}) });
                },
                .indexes => blk: {
                    const ref = try self.outputBlob(principal, row.get([]const u8, 1));
                    errdefer self.dropBlob(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable);
                    break :blk encode(a, .{ .id = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 0)}), .key_path = SchemaPath{ .data = ref, .component_length = try std.fmt.allocPrint(a, "{d}", .{row.get(i64, 2)}) }, .unique = row.get(bool, 3), .multi_entry = row.get(bool, 4) });
                },
                .schema => self.schemaRow(a, principal, row),
            };
        }
        const row = (try r.rows.?.next()) orelse {
            r.finished = true;
            return a.dupe(u8, "null");
        };
        var refs: [3]BlobRef = undefined;
        var count: usize = 0;
        errdefer for (refs[0..count]) |ref| self.dropBlob(std.fmt.parseInt(u64, ref.blob_id, 10) catch unreachable);
        if (r.columns > 0) {
            refs[0] = try self.outputBlob(principal, row.get([]const u8, 0));
            count += 1;
        }
        if (r.columns > 1) {
            refs[1] = try self.outputBlob(principal, row.get([]const u8, 1));
            count += 1;
        }
        if (r.columns > 2) {
            refs[2] = try self.outputBlob(principal, row.get([]const u8, 2));
            count += 1;
        }
        return encode(a, refs[0..r.columns]);
    }
    const origin = try string(try field(payload, "origin"));
    self.current_origin = origin;
    defer self.current_origin = null;
    if (std.mem.eql(u8, op, "idb.acquire")) {
        const o = try self.originFor(origin);
        if (o.tickets.items.len >= max_handles) return error.ProfileLimitExceeded;
        const id = try self.nextId();
        try o.tickets.append(self.allocator, .{ .id = id, .principal = principal });
        return encode(a, .{ .ticket = try std.fmt.allocPrint(a, "{d}", .{id}), .granted = o.tickets.items.len == 1 });
    }
    const ticket_value = try field(payload, "ticket");
    if (ticket_value == .null) {
        if (!metadataOperation(op)) return error.ProfileInvalidTransaction;
        const o = try self.originFor(origin);
        try self.refreshReader(origin, o);
        return Dispatch.dispatch(self, &o.reader, principal, 0, a, op, payload);
    }
    const o = self.origins.get(origin) orelse return error.ProfileInvalidHandle;
    const id = try number(ticket_value);
    const i = try ticketIndex(o, principal, id);
    if (std.mem.eql(u8, op, "idb.poll")) return encode(a, .{ .granted = i == 0 });
    if (std.mem.eql(u8, op, "idb.cancel")) {
        self.cancel(origin, o, i);
        return a.dupe(u8, "null");
    }
    if (i != 0) return error.ProfileNotGranted;
    if (std.mem.eql(u8, op, "idb.begin")) {
        if (o.tickets.items[i].active) return error.ProfileInvalidTransaction;
        try o.engine.begin(null);
        o.tickets.items[i].active = true;
        return a.dupe(u8, "null");
    }
    if (std.mem.eql(u8, op, "idb.commit") or std.mem.eql(u8, op, "idb.rollback")) {
        if (!o.tickets.items[i].active) return error.ProfileInvalidTransaction;
        self.closeTicketRows(id);
        if (std.mem.eql(u8, op, "idb.commit")) try o.engine.commit(null) else o.engine.rollback(null);
        o.tickets.items[i].active = false;
        return a.dupe(u8, "null");
    }
    // Reopening a committed database streams its schema under the origin grant
    // without beginning a SQL transaction. It still requires the authenticated
    // queue-head ticket above; unlike metadataOperation, no null ticket is valid.
    if (!o.tickets.items[i].active and !metadataOperation(op) and !std.mem.eql(u8, op, "idb.schemaSnapshot")) return error.ProfileInvalidTransaction;
    return Dispatch.dispatch(self, &o.engine, principal, id, a, op, payload);
}
fn readExact(fd: std.posix.fd_t, bytes: []u8) !bool {
    var offset: usize = 0;
    while (offset < bytes.len) {
        const n = std.c.read(fd, bytes.ptr + offset, bytes.len - offset);
        if (n == 0 and offset == 0) return false;
        if (n <= 0) return error.ProfileServiceLost;
        offset += @intCast(n);
    }
    return true;
}
fn sendFrame(fd: std.posix.fd_t, bytes: []const u8) !void {
    if (bytes.len == 0 or bytes.len > max_message) return error.ProfileMessageTooLarge;
    var prefix: [4]u8 = undefined;
    std.mem.writeInt(u32, &prefix, @intCast(bytes.len), .big);
    for ([_][]const u8{ &prefix, bytes }) |part| {
        var offset: usize = 0;
        while (offset < part.len) {
            const n = std.c.send(fd, part.ptr + offset, part.len - offset, std.c.MSG.NOSIGNAL);
            if (n <= 0) return error.ProfileServiceLost;
            offset += @intCast(n);
        }
    }
}
fn memoryGrant(fd: std.posix.fd_t, operation: u32, request_id: u64, grant_id: u64, bytes: u64, handles: u64) !u64 {
    var request: [40]u8 = undefined;
    @memcpy(request[0..4], "MGR1");
    std.mem.writeInt(u32, request[4..8], operation, .big);
    std.mem.writeInt(u64, request[8..16], request_id, .big);
    std.mem.writeInt(u64, request[16..24], grant_id, .big);
    std.mem.writeInt(u64, request[24..32], bytes, .big);
    std.mem.writeInt(u64, request[32..40], handles, .big);
    var offset: usize = 0;
    while (offset < request.len) {
        const n = std.c.send(fd, request[offset..].ptr, request.len - offset, std.c.MSG.NOSIGNAL);
        if (n <= 0) return error.ProfileGrantLost;
        offset += @intCast(n);
    }
    var reply: [32]u8 = undefined;
    if (!try readExact(fd, &reply)) return error.ProfileGrantLost;
    if (!std.mem.eql(u8, reply[0..4], "MGR1") or std.mem.readInt(u64, reply[8..16], .big) != request_id or std.mem.readInt(u64, reply[24..32], .big) != 0) return error.ProfileGrantProtocol;
    switch (std.mem.readInt(u32, reply[4..8], .big)) {
        0 => {},
        4 => return error.QuotaExceeded,
        else => return error.ProfileGrantRejected,
    }
    const result = std.mem.readInt(u64, reply[16..24], .big);
    if ((operation == 1 or operation == 3 or operation == 4) and result == 0) return error.ProfileGrantProtocol;
    return result;
}
fn grantEvents(self: *Owner, a: Allocator) ![]GrantWire {
    const events = try a.alloc(GrantWire, self.grant_count);
    for (self.grants[0..self.grant_count], 0..) |grant, i| {
        events[i] = .{ .connection_id = grant.principal, .origin = grant.origin, .ticket = try std.fmt.allocPrint(a, "{d}", .{grant.ticket}) };
    }
    return events;
}
pub fn serve(allocator: Allocator, fd: std.posix.fd_t, grant_fd: std.posix.fd_t, directory: []const u8) !void {
    defer _ = std.c.close(fd);
    defer _ = std.c.close(grant_fd);
    if (fd == grant_fd or !std.fs.path.isAbsolute(directory)) return error.InvalidProfileOwner;
    // Reserve the fixed owner/process envelope first. Additional value/schema
    // memory is admitted on demand, never converted into a smaller SQLite ABI.
    const selected_grant = try memoryGrant(grant_fd, 1, 1, 0, 64 * 1024 * 1024, owner_handle_grant);
    var owner = Owner{ .allocator = allocator, .directory = directory, .max_value = 1_000_000_000, .request_reservation = 8 * 1024 * 1024, .grant_fd = grant_fd, .lifetime_grant = selected_grant };
    defer _ = memoryGrant(grant_fd, 2, owner.grant_request_id, selected_grant, 0, 0) catch 0;
    defer owner.deinit();
    try sendFrame(fd, "{\"ready\":true}");
    var last_id: i64 = 0;
    while (true) {
        // One request at a time; reserve a finite clone/metadata budget before
        // executing SQLite or allocating Engine result values.
        const reservation = try std.posix.mmap(null, 8 * 1024 * 1024, .{ .READ = true, .WRITE = true }, .{ .TYPE = .PRIVATE, .ANONYMOUS = true }, -1, 0);
        defer std.posix.munmap(reservation);
        var fixed = std.heap.FixedBufferAllocator.init(reservation);
        var arena = std.heap.ArenaAllocator.init(fixed.allocator());
        defer arena.deinit();
        const a = arena.allocator();
        var reply_arena = std.heap.ArenaAllocator.init(allocator);
        defer reply_arena.deinit();
        const reply_allocator = reply_arena.allocator();
        owner.grant_count = 0;
        var prefix: [4]u8 = undefined;
        if (!try readExact(fd, &prefix)) return;
        const size = std.mem.readInt(u32, &prefix, .big);
        if (size == 0 or size > max_message) return error.ProfileProtocolError;
        const bytes = try a.alloc(u8, size);
        if (!try readExact(fd, bytes)) return error.ProfileProtocolError;
        const request = try std.json.parseFromSlice(std.json.Value, a, bytes, .{});
        const obj = try object(request.value);
        if (obj.count() != 4) return error.ProfileProtocolError;
        const version = obj.get("version") orelse return error.ProfileProtocolError;
        const id = obj.get("id") orelse return error.ProfileProtocolError;
        const op = obj.get("operation") orelse return error.ProfileProtocolError;
        const payload = obj.get("payload") orelse return error.ProfileProtocolError;
        if (version != .integer or version.integer != 1 or id != .integer or id.integer <= last_id or op != .string or payload != .object) return error.ProfileProtocolError;
        last_id = id.integer;
        owner.prepareMaterialization(op.string, payload) catch |err| {
            try sendFrame(fd, try encode(reply_allocator, .{ .id = id.integer, .ok = false, .@"error" = if (err == error.OutOfMemory or err == error.Overflow) "QuotaExceeded" else @errorName(err), .events = try owner.grantEvents(reply_allocator) }));
            continue;
        };
        const operation_memory = try std.posix.mmap(null, owner.request_reservation, .{ .READ = true, .WRITE = true }, .{ .TYPE = .PRIVATE, .ANONYMOUS = true }, -1, 0);
        defer std.posix.munmap(operation_memory);
        var operation_fixed = std.heap.FixedBufferAllocator.init(operation_memory);
        var operation_arena = std.heap.ArenaAllocator.init(operation_fixed.allocator());
        defer operation_arena.deinit();
        const result = owner.execute(operation_arena.allocator(), op.string, payload) catch |err| {
            const events = try owner.grantEvents(reply_allocator);
            const reply = try encode(reply_allocator, .{ .id = id.integer, .ok = false, .@"error" = if (err == error.OutOfMemory or err == error.TooBig) "QuotaExceeded" else @errorName(err), .events = events });
            try sendFrame(fd, reply);
            continue;
        };
        const events = try owner.grantEvents(reply_allocator);
        if (result.len > max_message) {
            try sendFrame(fd, try encode(reply_allocator, .{ .id = id.integer, .ok = false, .@"error" = "ProfileMessageTooLarge", .events = events }));
            continue;
        }
        const parsed = try std.json.parseFromSlice(std.json.Value, reply_allocator, result, .{});
        const reply = try encode(reply_allocator, .{ .id = id.integer, .ok = true, .result = parsed.value, .events = events });
        if (reply.len > max_message) {
            try sendFrame(fd, try encode(reply_allocator, .{ .id = id.integer, .ok = false, .@"error" = "ProfileMessageTooLarge", .events = events }));
            continue;
        }
        try sendFrame(fd, reply);
    }
}

test "committed schema reopens under an inactive grant without weakening ticket authority" {
    const testing = @import("../../../../testing.zig");
    const Call = struct {
        fn run(owner: *Owner, a: Allocator, op: []const u8, payload: anytype) ![]u8 {
            const encoded = try encode(a, payload);
            defer a.free(encoded);
            const parsed = try std.json.parseFromSlice(std.json.Value, a, encoded, .{ .allocate = .alloc_always });
            defer parsed.deinit();
            return owner.execute(a, op, parsed.value);
        }
    };
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const directory = try tmp.dir.realPathFileAlloc(testing.io, ".", testing.allocator);
    defer testing.allocator.free(directory);
    var owner = Owner{ .allocator = testing.allocator, .directory = directory, .max_value = 1_000_000_000, .request_reservation = 8 * 1024 * 1024 };
    defer owner.deinit();
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const origin = "https://shared.example";
    const o = try owner.originFor(origin);
    try o.engine.begin(null);
    const database_id = try o.engine.upsertDatabase(null, "committed", 1);
    const store_id = try o.engine.createObjectStore(null, a, database_id, "values", null, false);
    try o.engine.commit(null);
    const db = try std.fmt.allocPrint(a, "{d}", .{database_id});
    const store = try std.fmt.allocPrint(a, "{d}", .{store_id});

    const Granted = struct { ticket: []const u8, granted: bool };
    const first = try std.json.parseFromSlice(Granted, a, try Call.run(&owner, a, "idb.acquire", .{ .connection_id = 7, .origin = origin }), .{});
    defer first.deinit();
    const queued = try std.json.parseFromSlice(Granted, a, try Call.run(&owner, a, "idb.acquire", .{ .connection_id = 8, .origin = origin }), .{});
    defer queued.deinit();
    try testing.expect(first.value.granted);
    try testing.expect(!queued.value.granted);
    try testing.expectError(error.ProfileInvalidTransaction, Call.run(&owner, a, "idb.schemaSnapshot", .{ .connection_id = 7, .origin = origin, .ticket = @as(?[]const u8, null), .database_id = db }));
    try testing.expectError(error.ProfileInvalidHandle, Call.run(&owner, a, "idb.schemaSnapshot", .{ .connection_id = 8, .origin = origin, .ticket = first.value.ticket, .database_id = db }));
    try testing.expectError(error.ProfileInvalidHandle, Call.run(&owner, a, "idb.schemaSnapshot", .{ .connection_id = 7, .origin = "https://other.example", .ticket = first.value.ticket, .database_id = db }));
    try testing.expectError(error.ProfileInvalidHandle, Call.run(&owner, a, "idb.schemaSnapshot", .{ .connection_id = 7, .origin = origin, .ticket = "999999", .database_id = db }));
    try testing.expectError(error.ProfileNotGranted, Call.run(&owner, a, "idb.schemaSnapshot", .{ .connection_id = 8, .origin = origin, .ticket = queued.value.ticket, .database_id = db }));
    try testing.expectError(error.ProfileInvalidTransaction, Call.run(&owner, a, "idb.clear", .{ .connection_id = 7, .origin = origin, .ticket = first.value.ticket, .object_store_id = store }));

    const Header = struct { rows_id: []const u8, next_store_id: []const u8, next_index_id: []const u8 };
    const snapshot = try std.json.parseFromSlice(Header, a, try Call.run(&owner, a, "idb.schemaSnapshot", .{ .connection_id = 7, .origin = origin, .ticket = first.value.ticket, .database_id = db }), .{});
    defer snapshot.deinit();
    try testing.expect(!o.tickets.items[0].active);
    try testing.expect((try std.fmt.parseInt(i64, snapshot.value.next_store_id, 10)) > store_id);
    const rows_id = try std.fmt.parseInt(u64, snapshot.value.rows_id, 10);
    const rows = owner.rows.getPtr(rows_id) orelse return error.TestExpectedRows;
    try testing.expectEqual(@as(u64, 7), rows.principal);
    try testing.expectEqual(try std.fmt.parseInt(u64, first.value.ticket, 10), rows.ticket);
    try testing.expectString(origin, rows.origin);
    // Inspect the real SQLite snapshot, not an echoed request or synthetic row.
    const row = (try rows.metadata.?.next()) orelse return error.TestExpectedStore;
    try testing.expectString("values", row.get([]const u8, 0));
    try testing.expectEqual(store_id, row.get(i64, 1));
    try testing.expectEqual(null, try rows.metadata.?.next());
    try testing.expectString("null", try Call.run(&owner, a, "idb.rows_close", .{ .connection_id = 7, .origin = origin, .ticket = first.value.ticket, .rows_id = snapshot.value.rows_id }));
    try testing.expectEqual(@as(usize, 0), owner.rows.count());
}
