// Copyright (C) 2023-2026  Lightpanda (Selecy SAS)
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
const lp = @import("lightpanda");

const Key = @import("Key.zig");
const Sqlite = @import("../../../../storage/sqlite/Sqlite.zig");
const Persistence = @import("../Persistence.zig");
const Remote = @import("Remote.zig");
const RemoteMetadata = @import("RemoteMetadata.zig");
const RemoteRecords = @import("RemoteRecords.zig");
const RemoteGate = @import("RemoteGate.zig");

const log = lp.log;
const Allocator = std.mem.Allocator;

const Engine = @This();

conn: Sqlite.Conn,
remote: ?*Remote = null,
profile_async: ?*@import("../ProfileAsync.zig").Client = null,
_remote_gate: ?*RemoteGate = null,
_gate_handshake: bool = false,
_gate_repoll: bool = false,

// A single sqlite connection backs every database of an origin, so only one
// transaction (or open/delete) may hold it in a `begin`/`commit` bracket at a
// time. `_gate_owner` is whoever currently holds it; contenders park their
// (intrusive, caller-owned) node on `_gate_waiters` and are handed ownership in
// FIFO order as it's released.
//
// The Engine is session-scoped; waiters and registered resources are owned by
// individual JS contexts. detach() must run before a context's scheduler or
// page storage is torn down, to cancel waiters and release its resources.
_gate_owner: ?*GateWaiter = null,
_gate_waiters: std.DoublyLinkedList = .{},
_context_resources: std.DoublyLinkedList = .{},
pub const Row = union(enum) {
    local: Sqlite.Row,
    remote: Remote.Row,

    pub fn get(self: Row, comptime T: type, index: usize) T {
        return switch (self) {
            .local => |row| row.get(T, index),
            .remote => |row| row.get(T, index),
        };
    }
};

pub const Rows = union(enum) {
    local: Sqlite.Rows,
    remote: Remote.Rows,

    pub fn next(self: *Rows) !?Row {
        return switch (self.*) {
            .local => |*rows| if (try rows.next()) |row| .{ .local = row } else null,
            .remote => |*rows| if (try rows.next()) |row| .{ .remote = row } else null,
        };
    }

    pub fn deinit(self: *Rows) void {
        switch (self.*) {
            .local => |rows| rows.deinit(),
            .remote => |*rows| rows.deinit(),
        }
    }
};

pub const ContextResource = struct {
    node: std.DoublyLinkedList.Node = .{},
    ctx: *anyopaque,
    release: *const fn (*ContextResource) void,
    registered: bool = false,
};

pub fn registerContextResource(self: *Engine, resource: *ContextResource) void {
    std.debug.assert(!resource.registered);
    self._context_resources.append(&resource.node);
    resource.registered = true;
}

pub fn unregisterContextResource(self: *Engine, resource: *ContextResource) void {
    if (!resource.registered) return;
    self._context_resources.remove(&resource.node);
    resource.registered = false;
}

pub const GateWaiter = struct {
    node: std.DoublyLinkedList.Node = .{},
    queued: bool = false,
    remote_pending: bool = false,
    ticket: ?[]const u8 = null,
    failure: ?anyerror = null,
    // A bit unusual. Normally, if we need to tear something down when the frame
    // is destroyed, we track that on the frame (see Frame._file_lists). And
    // we _could_ do that here too, but the Engine still needs the list, so that
    // would require book-keeping in 2 lists. Instead, Engine owns the list and
    // we track the *js.Context (because it needs to work for both WGS and
    // frames). On context teardown, we can iterate the list and remove any
    // waiters associated with that context.
    ctx: *anyopaque,

    // Called when this waiter is handed the gate; typically reschedules the
    // owner's task so it re-runs and finds itself the owner.
    wake: *const fn (waiter: *GateWaiter) void,

    // Called when detach() removes this waiter because its context is going away.
    cancel: *const fn (waiter: *GateWaiter) void,
};

pub fn acquireGate(self: *Engine, waiter: *GateWaiter) !bool {
    if (waiter.failure) |err| return err;
    if (self._gate_owner == null) self._gate_owner = waiter;
    if (self._gate_owner != waiter) {
        if (!waiter.queued) {
            self._gate_waiters.append(&waiter.node);
            waiter.queued = true;
        }
        return false;
    }
    const remote = self.remote orelse return true;
    const gate = self._remote_gate orelse blk: {
        const created = try RemoteGate.create(remote.allocator, self.profile_async.?, remote.origin, self, remoteGateWake);
        errdefer created.deinit();
        try created.start();
        self._remote_gate = created;
        self._gate_handshake = true;
        self._gate_repoll = false;
        break :blk created;
    };
    if (gate.failure) |err| return err;
    waiter.ticket = gate.ticket;
    waiter.remote_pending = !gate.granted;
    return gate.granted;
}

// Release the gate held by `waiter`, handing it directly to the next parked
// waiter (no window where an unrelated contender can grab it) and waking it.
pub fn releaseGate(self: *Engine, waiter: *GateWaiter) bool {
    if (self._gate_owner != waiter) {
        return false;
    }
    if (self._remote_gate) |gate| {
        self._remote_gate = null;
        waiter.ticket = null;
        waiter.remote_pending = false;
        gate.cancel();
    }
    if (self._gate_waiters.popFirst()) |node| {
        const next: *GateWaiter = @fieldParentPtr("node", node);
        next.queued = false;
        self._gate_owner = next;
        next.wake(next);
        return true;
    }
    self._gate_owner = null;
    return true;
}

pub fn cancelGate(self: *Engine, waiter: *GateWaiter) void {
    if (waiter.queued) {
        self._gate_waiters.remove(&waiter.node);
        waiter.queued = false;
    } else {
        _ = self.releaseGate(waiter);
    }
}

// A js Context is being torn down: remove every waiter associated with it.
pub fn detach(self: *Engine, ctx: *anyopaque) void {
    var node = self._gate_waiters.first;
    while (node) |n| {
        const next = n.next;
        const waiter: *GateWaiter = @fieldParentPtr("node", n);
        if (waiter.ctx == ctx) {
            self._gate_waiters.remove(n);
            waiter.queued = false;
            waiter.cancel(waiter);
        }
        node = next;
    }

    if (self._gate_owner) |owner| {
        if (owner.ctx == ctx) {
            if (self.remote == null) self.conn.exec("rollback", .{}) catch {};
            _ = self.releaseGate(owner);
            owner.cancel(owner);
        }
    }
    while (true) {
        var resource_node = self._context_resources.first;
        while (resource_node) |n| {
            const resource: *ContextResource = @fieldParentPtr("node", n);
            if (resource.ctx == ctx) break;
            resource_node = n.next;
        }
        const n = resource_node orelse break;
        const resource: *ContextResource = @fieldParentPtr("node", n);
        self.unregisterContextResource(resource);
        resource.release(resource);
    }
}

fn remoteGateWake(ctx: *anyopaque) void {
    const self: *Engine = @ptrCast(@alignCast(ctx));
    const gate = self._remote_gate orelse return;
    const owner = self._gate_owner orelse return;
    if (gate.failure) |err| {
        owner.failure = err;
        owner.remote_pending = false;
        owner.wake(owner);
        return;
    }
    owner.ticket = gate.ticket;
    if (gate.granted) {
        owner.remote_pending = false;
        owner.wake(owner);
        return;
    }
    if (self._gate_handshake or self._gate_repoll) {
        self._gate_handshake = false;
        self._gate_repoll = false;
        gate.poll() catch |err| {
            owner.failure = err;
            owner.remote_pending = false;
            owner.wake(owner);
        };
    }
}

pub fn receiveGateEvent(self: *Engine, ticket: []const u8, cancelled: bool) void {
    const gate = self._remote_gate orelse return;
    const owner = self._gate_owner orelse return;
    if (!owner.remote_pending) return;
    if (gate.ticket) |active| {
        if (!std.mem.eql(u8, active, ticket)) return;
    }
    if (cancelled) {
        owner.failure = error.ProfileServiceLost;
        owner.remote_pending = false;
        owner.wake(owner);
    } else if (gate.pending) {
        self._gate_repoll = true;
    } else {
        gate.poll() catch |err| {
            owner.failure = err;
            owner.remote_pending = false;
            owner.wake(owner);
        };
    }
}

pub fn failAll(self: *Engine, err: anyerror) void {
    if (self._gate_owner) |owner| {
        if (owner.remote_pending) {
            owner.failure = err;
            owner.remote_pending = false;
            owner.wake(owner);
        }
    }
    while (self._gate_waiters.popFirst()) |node| {
        const waiter: *GateWaiter = @fieldParentPtr("node", node);
        waiter.queued = false;
        waiter.failure = err;
        waiter.wake(waiter);
    }
}

// A lost or indeterminate transaction outcome cannot be followed by another
// mutation on the same principal. Closing the event channel revokes the owner
// principal and causes its outstanding transaction to be rolled back.
pub fn failRemote(self: *Engine, err: anyerror) void {
    if (self.remote) |remote| remote.client.deinit();
    if (self.profile_async) |client| client.deinit();
    self.failAll(err);
}

pub fn open(path: [:0]const u8) !Engine {
    const opened = try Persistence.open(path);
    const conn = opened.conn;
    errdefer conn.close();
    if (opened.existing) {
        try Persistence.validateSchema(conn, .idb);
        return .{ .conn = conn };
    }
    try conn.begin(.immediate);
    errdefer conn.rollback() catch {};

    try conn.exec(
        \\ create table if not exists idb_databases (
        \\   id integer primary key,
        \\   name text not null unique,
        \\   version integer not null
        \\ );
        \\
        \\ create table if not exists idb_object_stores (
        \\   id integer primary key,
        \\   database_id integer not null references idb_databases(id) on delete cascade,
        \\   name text not null,
        \\   key_path text,
        \\   key_path_component_length integer not null default 0,
        \\   auto_increment integer not null default 0,
        \\   key_generator integer not null default 1,
        \\   unique(database_id, name)
        \\ );
        \\
        \\ create table if not exists idb_records (
        \\   object_store_id integer not null references idb_object_stores(id) on delete cascade,
        \\   key blob not null,
        \\   value blob not null,
        \\   primary key (object_store_id, key)
        \\ ) without rowid;
        \\
        \\ create table if not exists idb_indexes (
        \\   id integer primary key,
        \\   object_store_id integer not null references idb_object_stores(id) on delete cascade,
        \\   name text not null,
        \\   key_path text not null,
        \\   key_path_component_length integer not null default 0,
        \\   is_unique integer not null default 0,
        \\   multi_entry integer not null default 0,
        \\   unique(object_store_id, name)
        \\ );
        \\
        \\ create table if not exists idb_index_records (
        \\   index_id integer not null references idb_indexes(id) on delete cascade,
        \\   key blob not null,
        \\   primary_key blob not null,
        \\   is_unique integer not null,
        \\   primary key (index_id, key, primary_key)
        \\ ) without rowid;
        \\ create unique index if not exists idb_index_unique
        \\   on idb_index_records(index_id, key) where is_unique = 1;
    , .{});
    try conn.commit();
    return .{ .conn = conn };
}

pub fn openRemote(allocator: Allocator, client: *@import("../ProfileIpc.zig").Client, async_client: *@import("../ProfileAsync.zig").Client, origin: []const u8) !Engine {
    const remote = try allocator.create(Remote);
    errdefer allocator.destroy(remote);
    remote.* = try Remote.init(allocator, client, origin);
    return .{ .conn = undefined, .remote = remote, .profile_async = async_client };
}

fn remoteTicket(self: *const Engine, waiter: ?*GateWaiter, required: bool) !?[]const u8 {
    if (waiter) |holder| {
        if (self._gate_owner != holder or holder.remote_pending) return error.InvalidState;
        return holder.ticket orelse error.InvalidState;
    }
    if (required) return error.InvalidState;
    return null;
}

fn remoteCommand(self: *const Engine, waiter: ?*GateWaiter, operation: []const u8) !void {
    const remote = self.remote.?;
    const response = try remote.callWithTicket(?bool, try self.remoteTicket(waiter, true), operation, struct {}{});
    response.deinit();
}

pub const AsyncState = enum { completed, pending };
pub const AsyncDone = *const fn (*anyopaque, ?anyerror) void;

fn submitCommand(self: *Engine, waiter: ?*GateWaiter, operation: []const u8, ctx: *anyopaque, done: AsyncDone, request_id: *?u64) !AsyncState {
    const remote = self.remote.?;
    const ticket = (try self.remoteTicket(waiter, true)).?;
    request_id.* = try @import("RemoteCommand.zig").submit(remote.allocator, self.profile_async.?, remote.origin, ticket, operation, ctx, done);
    return .pending;
}

pub fn beginAsync(self: *Engine, waiter: ?*GateWaiter, ctx: *anyopaque, done: AsyncDone, request_id: *?u64) !AsyncState {
    request_id.* = null;
    if (self.remote != null) return self.submitCommand(waiter, "idb.begin", ctx, done, request_id);
    try self.begin(waiter);
    return .completed;
}

pub fn commitAsync(self: *Engine, waiter: ?*GateWaiter, ctx: *anyopaque, done: AsyncDone, request_id: *?u64) !AsyncState {
    request_id.* = null;
    if (self.remote != null) return self.submitCommand(waiter, "idb.commit", ctx, done, request_id);
    try self.commit(waiter);
    return .completed;
}

pub fn rollbackAsync(self: *Engine, waiter: ?*GateWaiter, ctx: *anyopaque, done: AsyncDone, request_id: *?u64) !AsyncState {
    request_id.* = null;
    if (self.remote != null) return self.submitCommand(waiter, "idb.rollback", ctx, done, request_id);
    self.rollback(waiter);
    return .completed;
}

pub fn close(self: *Engine) void {
    if (self._remote_gate) |gate| {
        self._remote_gate = null;
        gate.deinit();
    }
    if (self.remote) |remote| {
        const allocator = remote.allocator;
        remote.deinit();
        allocator.destroy(remote);
    } else self.conn.close();
}

pub fn lastError(self: *const Engine) [:0]const u8 {
    if (self.remote != null) return "Profile IndexedDB owner";
    return self.conn.lastError();
}

// Owner-side streaming queries. Their SQL and projection are fixed here rather
// than exposed through the Profile protocol.
pub fn databaseMetadataRows(self: *const Engine) !Sqlite.Rows {
    return self.conn.rows("select name, version from idb_databases order by name", .{});
}

pub fn objectStoreNameRows(self: *const Engine, database_id: i64) !Sqlite.Rows {
    return self.conn.rows("select name from idb_object_stores where database_id = ?1 order by name", .{database_id});
}

pub fn indexNameRows(self: *const Engine, object_store_id: i64) !Sqlite.Rows {
    return self.conn.rows("select name from idb_indexes where object_store_id = ?1 order by name", .{object_store_id});
}

pub fn indexMetadataRows(self: *const Engine, object_store_id: i64) !Sqlite.Rows {
    return self.conn.rows("select id, key_path, key_path_component_length, is_unique, multi_entry from idb_indexes where object_store_id = ?1", .{object_store_id});
}

pub fn schemaSnapshotRows(self: *const Engine, database_id: i64) !Sqlite.Rows {
    return self.conn.rows(
        \\ select s.name, s.id, s.key_path, s.key_path_component_length, s.auto_increment,
        \\        i.name, i.id, i.key_path, i.key_path_component_length, i.is_unique, i.multi_entry
        \\ from idb_object_stores s left join idb_indexes i on i.object_store_id = s.id
        \\ where s.database_id = ?1 order by s.name, i.name
    , .{database_id});
}

pub fn begin(self: *Engine, waiter: ?*GateWaiter) !void {
    if (self.remote != null) return self.remoteCommand(waiter, "idb.begin");
    return self.conn.exec("begin immediate", .{});
}

pub fn commit(self: *Engine, waiter: ?*GateWaiter) !void {
    if (self.remote != null) return self.remoteCommand(waiter, "idb.commit");
    return self.conn.exec("commit", .{});
}

pub fn rollback(self: *Engine, waiter: ?*GateWaiter) void {
    if (self.remote) |remote| {
        self.remoteCommand(waiter, "idb.rollback") catch {
            remote.client.failed = true;
        };
        return;
    }
    self.conn.exec("rollback", .{}) catch |err| {
        log.warn(.storage, "idb rollback", .{ .err = err, .sqlite = self.conn.lastError() });
    };
}
pub fn databaseId(self: *const Engine, waiter: ?*GateWaiter, name: []const u8) !?i64 {
    if (self.remote) |remote| return RemoteMetadata.databaseId(remote, try self.remoteTicket(waiter, false), name);
    return self.conn.scalar(i64, "select id from idb_databases where name = ?1", .{name});
}

pub fn databaseVersion(self: *const Engine, waiter: ?*GateWaiter, name: []const u8) !?i64 {
    if (self.remote) |remote| return RemoteMetadata.databaseVersion(remote, try self.remoteTicket(waiter, false), name);
    return self.conn.scalar(i64, "select version from idb_databases where name = ?1", .{name});
}

pub const DatabaseInfo = struct {
    name: []const u8,
    version: i64,
};

// Every database of the origin, by name (IDBFactory.databases).
pub fn databases(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator) ![]const DatabaseInfo {
    if (self.remote) |remote| return RemoteMetadata.databases(remote, try self.remoteTicket(waiter, false), arena);
    var rows = try self.conn.rows("select name, version from idb_databases order by name", .{});
    defer rows.deinit();

    var list: std.ArrayList(DatabaseInfo) = .empty;
    while (try rows.next()) |row| {
        try list.append(arena, .{
            .name = try arena.dupe(u8, row.get([]const u8, 0)),
            .version = row.get(i64, 1),
        });
    }
    return list.items;
}

pub fn indexExists(self: *const Engine, waiter: ?*GateWaiter, object_store_id: i64, name: []const u8) !bool {
    if (self.remote) |remote| return RemoteMetadata.indexExists(remote, try self.remoteTicket(waiter, false), object_store_id, name);
    const id = try self.conn.scalar(i64, "select id from idb_indexes where object_store_id = ?1 and name = ?2", .{ object_store_id, name });
    return id != null;
}

pub fn upsertDatabase(self: *Engine, waiter: ?*GateWaiter, name: []const u8, version: i64) !i64 {
    if (self.remote) |remote| return RemoteMetadata.upsertDatabase(remote, try self.remoteTicket(waiter, true), name, version);
    try self.conn.exec(
        \\ insert into idb_databases (name, version) values (?1, ?2)
        \\ on conflict(name) do update set version = ?2
    , .{ name, version });

    return (try self.databaseId(waiter, name)).?;
}

pub fn deleteDatabase(self: *Engine, waiter: ?*GateWaiter, name: []const u8) !void {
    if (self.remote) |remote| return RemoteMetadata.deleteDatabase(remote, try self.remoteTicket(waiter, true), name);
    return self.conn.exec("delete from idb_databases where name = ?1", .{name});
}

pub fn objectStoreId(self: *const Engine, waiter: ?*GateWaiter, database_id: i64, name: []const u8) !?i64 {
    if (self.remote) |remote| return RemoteMetadata.objectStoreId(remote, try self.remoteTicket(waiter, false), database_id, name);
    return self.conn.scalar(
        i64,
        "select id from idb_object_stores where database_id = ?1 and name = ?2",
        .{ database_id, name },
    );
}

pub const StoreInfo = struct {
    id: i64,
    key_path: ?Key.KeyPath,
    auto_increment: bool,
};

pub fn objectStoreInfo(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, database_id: i64, name: []const u8) !?StoreInfo {
    if (self.remote) |remote| return RemoteMetadata.objectStoreInfo(remote, try self.remoteTicket(waiter, false), arena, database_id, name);
    var row = (try self.conn.row(
        "select id, key_path, key_path_component_length, auto_increment from idb_object_stores where database_id = ?1 and name = ?2",
        .{ database_id, name },
    )) orelse return null;
    defer row.deinit();

    const key_path = row.get(?[]const u8, 1);
    return .{
        .id = row.get(i64, 0),
        .key_path = if (key_path) |kp| try Key.decodeKeyPathColumn(arena, kp, @intCast(row.get(i64, 2))) else null,
        .auto_increment = row.get(bool, 3),
    };
}

// key_generator starts at 1, we increment it, but want to return the
// previous value, hence the - 1.
pub fn nextGeneratedKey(self: *Engine, waiter: ?*GateWaiter, store_id: i64) !i64 {
    if (self.remote) |remote| return RemoteMetadata.nextGeneratedKey(remote, try self.remoteTicket(waiter, true), store_id);
    return (try self.conn.scalar(
        i64,
        "update idb_object_stores set key_generator = key_generator + 1 where id = ?1 returning key_generator - 1",
        .{store_id},
    )) orelse error.NotFound;
}

// If an explicit key was given, we need to bump the generator so that a future
// generated key doesn't collide
pub fn maybeBumpGenerator(self: *Engine, waiter: ?*GateWaiter, store_id: i64, key: f64) !void {
    if (self.remote) |remote| return RemoteMetadata.maybeBumpGenerator(remote, try self.remoteTicket(waiter, true), store_id, key);
    if (key < 1) {
        return;
    }

    // Cap at 2^53, the largest integer the generator tracks per spec.
    const capped = @min(@floor(key), 9007199254740992);
    const want: i64 = @trunc(capped + 1);
    try self.conn.exec(
        "update idb_object_stores set key_generator = ?2 where id = ?1 and key_generator < ?2",
        .{ store_id, want },
    );
}

pub fn createObjectStore(
    self: *Engine,
    waiter: ?*GateWaiter,
    arena: Allocator,
    database_id: i64,
    name: []const u8,
    key_path: ?Key.KeyPath,
    auto_increment: bool,
) !i64 {
    if (self.remote) |remote| return RemoteMetadata.createObjectStore(remote, try self.remoteTicket(waiter, true), arena, database_id, name, key_path, auto_increment);
    const column = if (key_path) |kp| try Key.encodeKeyPathColumn(arena, kp) else null;
    try self.conn.exec(
        \\ insert into idb_object_stores (database_id, name, key_path, key_path_component_length, auto_increment)
        \\ values (?1, ?2, ?3, ?4, ?5)
    , .{
        database_id,
        name,
        if (column) |c| c.text else null,
        if (column) |c| c.component_length else @as(usize, 0),
        auto_increment,
    });
    return (try self.objectStoreId(waiter, database_id, name)).?;
}

// The owner reserves these IDs while holding the origin's exclusive upgrade
// gate; no other transaction can insert rows before the intents are applied.
pub fn nextStoreId(self: *const Engine) !i64 {
    const current = (try self.conn.scalar(i64, "select coalesce(max(id), 0) from idb_object_stores", .{})).?;
    return std.math.add(i64, @max(current, 0), 1) catch error.TooBig;
}

pub fn nextIndexId(self: *const Engine) !i64 {
    const current = (try self.conn.scalar(i64, "select coalesce(max(id), 0) from idb_indexes", .{})).?;
    return std.math.add(i64, @max(current, 0), 1) catch error.TooBig;
}

pub fn createObjectStoreWithId(self: *Engine, id: i64, arena: Allocator, database_id: i64, name: []const u8, key_path: ?Key.KeyPath, auto_increment: bool) !i64 {
    if (id <= 0) return error.InvalidArgument;
    const column = if (key_path) |kp| try Key.encodeKeyPathColumn(arena, kp) else null;
    try self.conn.exec(
        \\ insert into idb_object_stores (id, database_id, name, key_path, key_path_component_length, auto_increment)
        \\ values (?1, ?2, ?3, ?4, ?5, ?6)
    , .{ id, database_id, name, if (column) |c| c.text else null, if (column) |c| c.component_length else @as(usize, 0), auto_increment });
    return id;
}

pub fn createIndexRowWithId(self: *Engine, id: i64, arena: Allocator, object_store_id: i64, name: []const u8, key_path: Key.KeyPath, unique: bool, multi_entry: bool) !i64 {
    if (id <= 0) return error.InvalidArgument;
    const column = try Key.encodeKeyPathColumn(arena, key_path);
    try self.conn.exec(
        \\ insert into idb_indexes (id, object_store_id, name, key_path, key_path_component_length, is_unique, multi_entry)
        \\ values (?1, ?2, ?3, ?4, ?5, ?6, ?7)
    , .{ id, object_store_id, name, column.text, column.component_length, unique, multi_entry });
    return id;
}

// A duplicate name surfaces as error.Constraint.
pub fn renameObjectStore(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, name: []const u8) !void {
    if (self.remote) |remote| return RemoteMetadata.renameObjectStore(remote, try self.remoteTicket(waiter, true), object_store_id, name);
    try self.conn.exec("update idb_object_stores set name = ?2 where id = ?1", .{ object_store_id, name });
}

pub fn renameIndex(self: *Engine, waiter: ?*GateWaiter, index_id: i64, name: []const u8) !void {
    if (self.remote) |remote| return RemoteMetadata.renameIndex(remote, try self.remoteTicket(waiter, true), index_id, name);
    try self.conn.exec("update idb_indexes set name = ?2 where id = ?1", .{ index_id, name });
}

pub fn deleteObjectStore(self: *Engine, waiter: ?*GateWaiter, database_id: i64, name: []const u8) !void {
    if (self.remote) |remote| return RemoteMetadata.deleteObjectStore(remote, try self.remoteTicket(waiter, true), database_id, name);
    // caller has a transaction open; cascade drops records, indexes and index
    // records.
    const deleted = try self.conn.scalar(i64,
        \\ delete from idb_object_stores where database_id = ?1 and name = ?2
        \\ returning id
    , .{ database_id, name });
    if (deleted == null) {
        return error.NotFound;
    }
}

pub fn add(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, key: []const u8, value: []const u8) !void {
    if (self.remote) |remote| return RemoteRecords.add(remote, try self.remoteTicket(waiter, true), object_store_id, key, value);
    return self.conn.exec(
        "insert into idb_records (object_store_id, key, value) values (?1, ?2, ?3)",
        .{ object_store_id, key, value },
    );
}

pub fn put(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, key: []const u8, value: []const u8) !void {
    if (self.remote) |remote| return RemoteRecords.put(remote, try self.remoteTicket(waiter, true), object_store_id, key, value);
    return self.conn.exec(
        \\ insert into idb_records (object_store_id, key, value) values (?1, ?2, ?3)
        \\ on conflict(object_store_id, key) do update set value = ?3
    , .{ object_store_id, key, value });
}

pub fn get(self: *const Engine, waiter: ?*GateWaiter, allocator: Allocator, object_store_id: i64, key: []const u8) !?[]u8 {
    if (self.remote) |remote| return RemoteRecords.get(remote, try self.remoteTicket(waiter, true), allocator, object_store_id, key);
    var row = (try self.conn.row(
        "select value from idb_records where object_store_id = ?1 and key = ?2",
        .{ object_store_id, key },
    )) orelse return null;
    defer row.deinit();
    return try allocator.dupe(u8, row.get([]const u8, 0));
}

pub fn clear(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64) !void {
    if (self.remote) |remote| return RemoteRecords.clear(remote, try self.remoteTicket(waiter, true), object_store_id);
    return self.conn.exec("delete from idb_records where object_store_id = ?1", .{object_store_id});
}

pub const IndexInfo = struct {
    id: i64,
    key_path: Key.KeyPath,
    unique: bool,
    multi_entry: bool,
};

pub fn createIndexRow(self: *Engine, waiter: ?*GateWaiter, arena: Allocator, object_store_id: i64, name: []const u8, key_path: Key.KeyPath, unique: bool, multi_entry: bool) !i64 {
    if (self.remote) |remote| return RemoteMetadata.createIndexRow(remote, try self.remoteTicket(waiter, true), arena, object_store_id, name, key_path, unique, multi_entry);
    const column = try Key.encodeKeyPathColumn(arena, key_path);
    return (try self.conn.scalar(
        i64,
        \\ insert into idb_indexes (object_store_id, name, key_path, key_path_component_length, is_unique, multi_entry)
        \\ values (?1, ?2, ?3, ?4, ?5, ?6) returning id
    ,
        .{ object_store_id, name, column.text, column.component_length, unique, multi_entry },
    )) orelse error.UnknownError;
}

pub fn deleteIndexRow(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, name: []const u8) !void {
    if (self.remote) |remote| return RemoteMetadata.deleteIndexRow(remote, try self.remoteTicket(waiter, true), object_store_id, name);
    const deleted = try self.conn.scalar(
        i64,
        "delete from idb_indexes where object_store_id = ?1 and name = ?2 returning id",
        .{ object_store_id, name },
    );
    if (deleted == null) {
        return error.NotFound;
    }
}

pub fn indexInfo(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, object_store_id: i64, name: []const u8) !?IndexInfo {
    if (self.remote) |remote| return RemoteMetadata.indexInfo(remote, try self.remoteTicket(waiter, false), arena, object_store_id, name);
    var row = (try self.conn.row(
        "select id, key_path, key_path_component_length, is_unique, multi_entry from idb_indexes where object_store_id = ?1 and name = ?2",
        .{ object_store_id, name },
    )) orelse return null;
    defer row.deinit();

    return .{
        .id = row.get(i64, 0),
        .key_path = try Key.decodeKeyPathColumn(arena, row.get([]const u8, 1), @intCast(row.get(i64, 2))),
        .unique = row.get(bool, 3),
        .multi_entry = row.get(bool, 4),
    };
}

pub fn indexesForStore(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, object_store_id: i64) ![]IndexInfo {
    if (self.remote) |remote| return RemoteMetadata.indexesForStore(remote, try self.remoteTicket(waiter, false), arena, object_store_id);
    var rows = try self.conn.rows(
        "select id, key_path, key_path_component_length, is_unique, multi_entry from idb_indexes where object_store_id = ?1",
        .{object_store_id},
    );
    defer rows.deinit();

    var list: std.ArrayList(IndexInfo) = .empty;
    while (try rows.next()) |row| {
        try list.append(arena, .{
            .id = row.get(i64, 0),
            .key_path = try Key.decodeKeyPathColumn(arena, row.get([]const u8, 1), @intCast(row.get(i64, 2))),
            .unique = row.get(bool, 3),
            .multi_entry = row.get(bool, 4),
        });
    }
    return list.items;
}

pub fn indexNames(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, object_store_id: i64) ![]const []const u8 {
    if (self.remote) |remote| return RemoteMetadata.indexNames(remote, try self.remoteTicket(waiter, false), arena, object_store_id);
    var rows = try self.conn.rows(
        "select name from idb_indexes where object_store_id = ?1 order by name",
        .{object_store_id},
    );
    defer rows.deinit();

    var list: std.ArrayList([]const u8) = .empty;
    while (try rows.next()) |row| {
        try list.append(arena, try arena.dupe(u8, row.get([]const u8, 0)));
    }
    return list.items;
}

pub fn objectStoreNames(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, database_id: i64) ![]const []const u8 {
    if (self.remote) |remote| return RemoteMetadata.objectStoreNames(remote, try self.remoteTicket(waiter, false), arena, database_id);
    var rows = try self.conn.rows(
        "select name from idb_object_stores where database_id = ?1 order by name",
        .{database_id},
    );
    defer rows.deinit();

    var list: std.ArrayList([]const u8) = .empty;
    while (try rows.next()) |row| {
        try list.append(arena, try arena.dupe(u8, row.get([]const u8, 0)));
    }
    return list.items;
}

pub fn addIndexRecord(self: *Engine, waiter: ?*GateWaiter, index_id: i64, key: []const u8, primary_key: []const u8, unique: bool) !void {
    if (self.remote) |remote| return RemoteRecords.addIndexRecord(remote, try self.remoteTicket(waiter, true), index_id, key, primary_key, unique);
    return self.conn.exec(
        "insert into idb_index_records (index_id, key, primary_key, is_unique) values (?1, ?2, ?3, ?4)",
        .{ index_id, key, primary_key, unique },
    );
}

pub fn deleteIndexRecordsForKey(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, primary_key: []const u8) !void {
    if (self.remote) |remote| return RemoteRecords.deleteIndexRecordsForKey(remote, try self.remoteTicket(waiter, true), object_store_id, primary_key);
    return self.conn.exec(
        \\ delete from idb_index_records
        \\ where primary_key = ?2 and index_id in (
        \\   select id from idb_indexes where object_store_id = ?1
        \\ )
    , .{ object_store_id, primary_key });
}

pub fn clearIndexRecordsForStore(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64) !void {
    if (self.remote) |remote| return RemoteRecords.clearIndexRecordsForStore(remote, try self.remoteTicket(waiter, true), object_store_id);
    return self.conn.exec(
        "delete from idb_index_records where index_id in (select id from idb_indexes where object_store_id = ?1)",
        .{object_store_id},
    );
}

// Drop index entries for every record about to be deleted by a ranged delete.
pub fn deleteIndexRecordsForRange(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, b: Bounds) !void {
    if (self.remote) |remote| return RemoteRecords.deleteIndexRecordsForRange(remote, try self.remoteTicket(waiter, true), object_store_id, b);
    const ops = rangeOps(b);
    var buf: [400]u8 = undefined;
    const sql = try std.mem.printSentinel(&buf,
        \\ delete from idb_index_records where index_id in (
        \\   select id from idb_indexes where object_store_id = ?1
        \\ ) and primary_key in (
        \\   select key from idb_records where object_store_id = ?1 and key {s} ?2 and key {s} ?3
        \\)
    , .{ ops.lo, ops.hi }, 0);

    return self.conn.exec(sql, .{ object_store_id, b.lower, b.upper });
}

pub fn savepoint(self: *Engine, waiter: ?*GateWaiter) !void {
    if (self.remote != null) return self.remoteCommand(waiter, "idb.savepoint");
    return self.conn.exec("savepoint idb_op", .{});
}

pub fn releaseSavepoint(self: *Engine, waiter: ?*GateWaiter) !void {
    if (self.remote != null) return self.remoteCommand(waiter, "idb.releaseSavepoint");
    return self.conn.exec("release idb_op", .{});
}

pub fn rollbackSavepoint(self: *Engine, waiter: ?*GateWaiter) void {
    if (self.remote) |remote| {
        self.remoteCommand(waiter, "idb.rollbackSavepoint") catch {
            remote.client.failed = true;
        };
        return;
    }
    self.conn.exec("rollback to idb_op", .{}) catch |err| {
        log.warn(.storage, "idb savepoint rollback", .{ .err = err, .sqlite = self.conn.lastError() });
    };
    self.conn.exec("release idb_op", .{}) catch |err| {
        log.warn(.storage, "idb savepoint release", .{ .err = err });
    };
}

// First record value in an index range (joins back to the store by primary key).
pub fn indexGetRange(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, object_store_id: i64, index_id: i64, b: Bounds) !?[]u8 {
    if (self.remote) |remote| return RemoteRecords.indexGetRange(remote, try self.remoteTicket(waiter, true), arena, object_store_id, index_id, b);
    const ops = rangeOps(b);

    var buf: [512]u8 = undefined;
    const sql = try std.mem.print(&buf,
        \\ select r.value
        \\ from idb_index_records ir
        \\ join idb_records r on r.object_store_id = ?1 and r.key = ir.primary_key
        \\ where ir.index_id = ?2 and ir.key {s} ?3 and ir.key {s} ?4 order by ir.key, ir.primary_key
        \\ limit 1
    , .{ ops.lo, ops.hi });

    var row = (try self.conn.row(sql, .{ object_store_id, index_id, b.lower, b.upper })) orelse return null;
    defer row.deinit();

    return try arena.dupe(u8, row.get([]const u8, 0));
}

// First primary key in an index range.
pub fn indexGetKeyRange(self: *const Engine, waiter: ?*GateWaiter, arena: Allocator, index_id: i64, b: Bounds) !?[]u8 {
    if (self.remote) |remote| return RemoteRecords.indexGetKeyRange(remote, try self.remoteTicket(waiter, true), arena, index_id, b);
    const ops = rangeOps(b);
    var buf: [320]u8 = undefined;
    const sql = try std.mem.print(&buf,
        \\ select primary_key
        \\ from idb_index_records
        \\ where index_id = ?1 and key {s} ?2 and key {s} ?3
        \\ order by key, primary_key limit 1
    , .{ ops.lo, ops.hi });

    var row = (try self.conn.row(sql, .{ index_id, b.lower, b.upper })) orelse return null;
    defer row.deinit();

    return try arena.dupe(u8, row.get([]const u8, 0));
}

pub fn indexCountRange(self: *const Engine, waiter: ?*GateWaiter, index_id: i64, b: Bounds) !i64 {
    if (self.remote) |remote| return RemoteRecords.indexCountRange(remote, try self.remoteTicket(waiter, true), index_id, b);
    const ops = rangeOps(b);
    var buf: [320]u8 = undefined;
    const sql = try std.mem.print(&buf,
        \\ select count(*)
        \\ from idb_index_records
        \\ where index_id = ?1 and key {s} ?2 and key {s} ?3
    , .{ ops.lo, ops.hi });

    return (try self.conn.scalar(i64, sql, .{ index_id, b.lower, b.upper })) orelse 0;
}

// Full (index key, primary key, value) rows over an index range, ordered by index
// key (then primary key), ascending or — for a reverse direction — descending, and
// limited to `limit_` rows (null = no limit).
//
// When unique is true, we use sqlite3 bare aggregates to resolve the uniqueness
// Note that min(ir.primary_key) is used regardless of `reverse` because
// uniqueness is always based on ascending order.
pub fn indexGetAllRows(self: *const Engine, waiter: ?*GateWaiter, object_store_id: i64, index_id: i64, b: Bounds, reverse: bool, unique: bool, limit_: ?u32) !Rows {
    if (self.remote) |remote| return .{ .remote = try RemoteRecords.indexGetAllRows(remote, try self.remoteTicket(waiter, true), object_store_id, index_id, b, reverse, unique, limit_) };
    const ops = rangeOps(b);
    const order = if (reverse) "desc" else "asc";

    var buf: [640]u8 = undefined;
    const sql = try std.mem.print(&buf,
        \\ select ir.key, {s}, r.value
        \\ from idb_index_records ir
        \\ join idb_records r on r.object_store_id = ?1 and r.key = ir.primary_key
        \\ where ir.index_id = ?2 and ir.key {s} ?3 and ir.key {s} ?4
        \\ {s}
        \\ order by ir.key {s}{s}
        \\ limit ?5
    , .{
        if (unique) "min(ir.primary_key)" else "ir.primary_key",
        ops.lo,
        ops.hi,
        if (unique) "group by ir.key" else "",
        order,
        if (unique) "" else if (reverse) ", ir.primary_key desc" else ", ir.primary_key asc",
    });

    // SQLite treats a negative LIMIT as "no limit".
    const limit: i64 = if (limit_) |c| @intCast(c) else -1;
    return .{ .local = try self.conn.rows(sql, .{ object_store_id, index_id, b.lower, b.upper, limit }) };
}

pub const IndexCursorRecord = struct {
    key: []u8,
    primary_key: []u8,
    value: ?[]u8,
};

pub fn indexCursorSeek(
    self: *const Engine,
    waiter: ?*GateWaiter,
    arena: Allocator,
    object_store_id: i64,
    index_id: i64,
    b: Bounds,
    reverse: bool,
    unique: bool,
    from_key: []const u8,
    from_pk: []const u8,
    pk_inclusive: bool,
    with_value: bool,
    offset: u32,
) !?IndexCursorRecord {
    if (self.remote) |remote| return RemoteRecords.indexCursorSeek(remote, try self.remoteTicket(waiter, true), arena, object_store_id, index_id, b, reverse, unique, from_key, from_pk, pk_inclusive, with_value, offset);
    const ops = rangeOps(b);
    const order = if (reverse) "desc" else "asc";
    // Position past the current (key, primary_key): a strictly-greater key, or an
    // equal key with a greater (or, for continuePrimaryKey, >=) primary key.
    const key_op = if (reverse) "< " else "> ";
    const pk_op = if (reverse) (if (pk_inclusive) "<= " else "< ") else (if (pk_inclusive) ">= " else "> ");

    var buf: [768]u8 = undefined;
    const sql = try std.mem.print(
        &buf,
        \\ select ir.key, {s}{s} from idb_index_records ir {s}
        \\ where ir.index_id = ?2 and ir.key {s} ?3 and ir.key {s} ?4 and (ir.key {s}?5 or (ir.key = ?5 and ir.primary_key {s}?6))
        \\ {s} order by ir.key {s}, ir.primary_key {s}
        \\ limit 1 offset ?7
    ,
        .{
            if (unique) "min(ir.primary_key)" else "ir.primary_key",
            if (with_value) ", r.value" else "",
            if (with_value) "join idb_records r on r.object_store_id = ?1 and r.key = ir.primary_key" else "",
            ops.lo,
            ops.hi,
            key_op,
            pk_op,
            if (unique) "group by ir.key" else "",
            order,
            if (unique) "asc" else order,
        },
    );

    var row = (try self.conn.row(sql, .{ object_store_id, index_id, b.lower, b.upper, from_key, from_pk, @as(i64, offset) })) orelse return null;
    defer row.deinit();

    return .{
        .key = try arena.dupe(u8, row.get([]const u8, 0)),
        .primary_key = try arena.dupe(u8, row.get([]const u8, 1)),
        .value = if (with_value) try arena.dupe(u8, row.get([]const u8, 2)) else null,
    };
}

pub const Bounds = struct {
    lower: []const u8,
    upper: []const u8,

    // These are built-time string literal operators, e.g. ">= ". Not worried
    // about some SQL injection.
    lower_op: []const u8,
    upper_op: []const u8,

    // optimization flag for point queries.
    is_point: bool,

    // Every encoded key begins with a type tag from 10 to 50]...
    // 0 sorts below, and...
    pub const min_sentinel: []const u8 = &.{0x00};
    // 255 sorts above
    pub const max_sentinel: []const u8 = &.{0xFF};

    pub fn unbounded() Bounds {
        return .{ .lower = min_sentinel, .upper = max_sentinel, .lower_op = ">= ", .upper_op = "<= ", .is_point = false };
    }

    pub fn point(encoded: []const u8) Bounds {
        return .{ .lower = encoded, .upper = encoded, .lower_op = "", .upper_op = "", .is_point = true };
    }
};

pub fn getRange(self: *const Engine, waiter: ?*GateWaiter, allocator: Allocator, object_store_id: i64, b: Bounds) !?[]u8 {
    if (self.remote) |remote| return RemoteRecords.getRange(remote, try self.remoteTicket(waiter, true), allocator, object_store_id, b);
    var buf: [256]u8 = undefined;
    const sql = try rangeSql(&buf, "select value", b, " order by key limit 1");
    var row = (try self.conn.row(sql, .{ object_store_id, b.lower, b.upper })) orelse return null;
    defer row.deinit();
    return try allocator.dupe(u8, row.get([]const u8, 0));
}

pub fn getKeyRange(self: *const Engine, waiter: ?*GateWaiter, allocator: Allocator, object_store_id: i64, b: Bounds) !?[]u8 {
    if (self.remote) |remote| return RemoteRecords.getKeyRange(remote, try self.remoteTicket(waiter, true), allocator, object_store_id, b);
    var buf: [256]u8 = undefined;
    const sql = try rangeSql(&buf, "select key", b, " order by key limit 1");
    var row = (try self.conn.row(sql, .{ object_store_id, b.lower, b.upper })) orelse return null;
    defer row.deinit();
    return try allocator.dupe(u8, row.get([]const u8, 0));
}

pub fn countRange(self: *const Engine, waiter: ?*GateWaiter, object_store_id: i64, b: Bounds) !i64 {
    if (self.remote) |remote| return RemoteRecords.countRange(remote, try self.remoteTicket(waiter, true), object_store_id, b);
    var buf: [256]u8 = undefined;
    const sql = try rangeSql(&buf, "select count(*)", b, "");
    return (try self.conn.scalar(i64, sql, .{ object_store_id, b.lower, b.upper })) orelse 0;
}

pub fn deleteRange(self: *Engine, waiter: ?*GateWaiter, object_store_id: i64, b: Bounds) !void {
    if (self.remote) |remote| return RemoteRecords.deleteRange(remote, try self.remoteTicket(waiter, true), object_store_id, b);
    var buf: [256]u8 = undefined;
    const sql = try rangeSql(&buf, "delete", b, "");
    return self.conn.exec(sql, .{ object_store_id, b.lower, b.upper });
}

// Every injected string is a compile-time known/safe value.
fn rangeSql(buf: []u8, head: []const u8, b: Bounds, tail: []const u8) ![:0]u8 {
    if (b.is_point) {
        // optimized query for [common] point query. (key = ?2 or key = ?3)
        // to keep the param list the side for the caller.
        return std.mem.printSentinel(
            buf,
            "{s} from idb_records where object_store_id = ?1 and (key = ?2 or key = ?3) {s}",
            .{ head, tail },
            0,
        );
    }

    return std.mem.printSentinel(
        buf,
        "{s} from idb_records where object_store_id = ?1 and key {s} ?2 and key {s} ?3{s}",
        .{ head, b.lower_op, b.upper_op, tail },
        0,
    );
}

// A point range stores empty operators; index queries want a closed range.
fn rangeOps(b: Bounds) struct { lo: []const u8, hi: []const u8 } {
    return .{
        .lo = if (b.is_point) ">= " else b.lower_op,
        .hi = if (b.is_point) "<= " else b.upper_op,
    };
}

// What a ranged getAll/getAllKeys returns: the value or key column.
const Column = enum {
    value,
    key,
};

// Open a getAll/getAllKeys cursor. The JS layer streams rows straight into a JS
// array, avoiding a copy of the whole result set out of sqlite. (The SQL text is
// copied into the prepared statement, so the stack `buf` can be discarded.)
fn getAllRangeRows(self: *const Engine, object_store_id: i64, b: Bounds, column: Column, limit_: ?u32) !Sqlite.Rows {
    var buf: [256]u8 = undefined;
    const head = if (column == .value) "select value" else "select key";
    const sql = try rangeSql(&buf, head, b, " order by key limit ?4");

    // SQLite treats a negative LIMIT as "no limit".
    const limit: i64 = if (limit_) |c| @intCast(c) else -1;
    return self.conn.rows(sql, .{ object_store_id, b.lower, b.upper, limit });
}

// Full (key, value) rows over a store range, ordered by key
pub fn getAllRows(self: *const Engine, waiter: ?*GateWaiter, object_store_id: i64, b: Bounds, reverse: bool, limit_: ?u32) !Rows {
    if (self.remote) |remote| return .{ .remote = try RemoteRecords.getAllRows(remote, try self.remoteTicket(waiter, true), object_store_id, b, reverse, limit_) };
    var buf: [256]u8 = undefined;
    const sql = try rangeSql(&buf, "select key, value", b, if (reverse) " order by key desc limit ?4" else " order by key limit ?4");
    // SQLite treats a negative LIMIT as "no limit".
    const limit: i64 = if (limit_) |c| @intCast(c) else -1;
    return .{ .local = try self.conn.rows(sql, .{ object_store_id, b.lower, b.upper, limit }) };
}

fn getAllRange(self: *const Engine, arena: Allocator, object_store_id: i64, b: Bounds, column: Column, limit_: ?u32) ![]const []u8 {
    var rows = try self.getAllRangeRows(object_store_id, b, column, limit_);
    defer rows.deinit();

    var list: std.ArrayList([]u8) = .empty;
    while (try rows.next()) |row| {
        try list.append(arena, try arena.dupe(u8, row.get([]const u8, 0)));
    }
    return list.items;
}

pub const CursorRecord = struct {
    key: []u8,
    // null for a key-only cursor (the value column isn't selected or duped).
    value: ?[]u8,
};

// Seek a single record for a cursor: within `b`, in `reverse` order or not,
// positioned by `from_op ?4` (e.g. "> " last_key) and skipping `offset` matches
// (for advance). `from_key` of the min/max sentinel makes the position clause a
// no-op for the first step. `with_value` is false for openKeyCursor, which skips
// the (potentially large) value column entirely.
pub fn cursorSeek(
    self: *const Engine,
    waiter: ?*GateWaiter,
    arena: Allocator,
    object_store_id: i64,
    b: Bounds,
    reverse: bool,
    from_op: []const u8,
    from_key: []const u8,
    offset: u32,
    with_value: bool,
) !?CursorRecord {
    if (self.remote) |remote| return RemoteRecords.cursorSeek(remote, try self.remoteTicket(waiter, true), arena, object_store_id, b, reverse, from_op, from_key, offset, with_value);
    // A point range stores its operators empty; a cursor still wants a closed
    // range over the single key.
    const lower_op = if (b.is_point) ">= " else b.lower_op;
    const upper_op = if (b.is_point) "<= " else b.upper_op;
    const order = if (reverse) "desc" else "asc";

    var buf: [320]u8 = undefined;
    const sql = try std.mem.print(
        &buf,
        "{s} from idb_records where object_store_id = ?1 and key {s} ?2 and key {s} ?3 and key {s} ?4 order by key {s} limit 1 offset ?5",
        .{ if (with_value) "select key, value" else "select key", lower_op, upper_op, from_op, order },
    );

    var row = (try self.conn.row(sql, .{ object_store_id, b.lower, b.upper, from_key, @as(i64, offset) })) orelse return null;
    defer row.deinit();
    return .{
        .key = try arena.dupe(u8, row.get([]const u8, 0)),
        .value = if (with_value) try arena.dupe(u8, row.get([]const u8, 1)) else null,
    };
}

const testing = @import("../../../../testing.zig");
test "IDB - Engine: ranged getAll/count honour open and closed bounds" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var engine = try Engine.open(":memory:");
    defer engine.close();
    const db_id = try engine.upsertDatabase(null, "app", 1);
    const store_id = try engine.createObjectStore(null, testing.allocator, db_id, "s", null, false);
    try seedNumbers(&engine, store_id, arena, &.{ 3, 1, 5, 2, 4 });

    const k2 = try numKey(arena, 2);
    const k4 = try numKey(arena, 4);

    // Closed [2,4]: keys 2,3,4 in sorted order.
    {
        const b = boundsFor(k2, false, k4, false);
        const vals = try engine.getAllRange(arena, store_id, b, .value, null);
        try testing.expectEqual(3, vals.len);
        try testing.expectEqualSlices(u8, "v2", vals[0]);
        try testing.expectEqualSlices(u8, "v4", vals[2]);
        try testing.expectEqual(3, try engine.countRange(null, store_id, b));
    }

    // Open lower (2,4]: keys 3,4.
    {
        const b = boundsFor(k2, true, k4, false);
        try testing.expectEqual(2, try engine.countRange(null, store_id, b));
        const vals = try engine.getAllRange(arena, store_id, b, .value, null);
        try testing.expectEqualSlices(u8, "v3", vals[0]);
    }

    // Open both (2,4): only key 3.
    {
        const b = boundsFor(k2, true, k4, true);
        try testing.expectEqual(1, try engine.countRange(null, store_id, b));
    }

    // Unbounded and point ranges.
    try testing.expectEqual(5, try engine.countRange(null, store_id, Engine.Bounds.unbounded()));
    try testing.expectEqual(1, try engine.countRange(null, store_id, Engine.Bounds.point(try numKey(arena, 3))));

    // A limit caps the result count.
    const limited = try engine.getAllRange(arena, store_id, Engine.Bounds.unbounded(), .value, 2);
    try testing.expectEqual(2, limited.len);

    // The .key column returns encoded keys, sorted.
    const keys = try engine.getAllRange(arena, store_id, Engine.Bounds.unbounded(), .key, null);
    try testing.expectEqualSlices(u8, try numKey(arena, 1), keys[0]);
    try testing.expectEqualSlices(u8, try numKey(arena, 5), keys[4]);
}

test "IDB - Engine: getRange/getKeyRange first-in-range and deleteRange" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var engine = try Engine.open(":memory:");
    defer engine.close();
    const db_id = try engine.upsertDatabase(null, "app", 1);
    const store_id = try engine.createObjectStore(null, testing.allocator, db_id, "s", null, false);
    try seedNumbers(&engine, store_id, arena, &.{ 1, 2, 3, 4, 5 });

    const k2 = try numKey(arena, 2);
    const k4 = try numKey(arena, 4);

    // First value/key in [2,4] is for key 2.
    try testing.expectEqualSlices(u8, "v2", (try engine.getRange(null, arena, store_id, boundsFor(k2, false, k4, false))).?);
    try testing.expectEqualSlices(u8, k2, (try engine.getKeyRange(null, arena, store_id, boundsFor(k2, false, k4, false))).?);

    // Open lower skips key 2.
    try testing.expectEqualSlices(u8, "v3", (try engine.getRange(null, arena, store_id, boundsFor(k2, true, k4, false))).?);

    // Empty range yields null.
    try testing.expectEqual(null, try engine.getRange(null, arena, store_id, Engine.Bounds.point(try numKey(arena, 99))));

    // deleteRange removes [4,5], leaving 1,2,3.
    try engine.deleteRange(null, store_id, boundsFor(k4, false, try numKey(arena, 5), false));
    try testing.expectEqual(3, try engine.countRange(null, store_id, Engine.Bounds.unbounded()));
}

test "IDB - Engine: detach unlinks parked waiters and hands off an owned gate" {
    var engine = try Engine.open(":memory:");
    defer engine.close();

    const TestWaiter = struct {
        waiter: Engine.GateWaiter,
        woken: u32 = 0,
        cancelled: u32 = 0,

        fn init(ctx: *anyopaque) @This() {
            return .{ .waiter = .{ .ctx = ctx, .wake = wake, .cancel = cancel } };
        }
        fn wake(w: *Engine.GateWaiter) void {
            const self: *@This() = @fieldParentPtr("waiter", w);
            self.woken += 1;
        }
        fn cancel(w: *Engine.GateWaiter) void {
            const self: *@This() = @fieldParentPtr("waiter", w);
            self.cancelled += 1;
        }
    };

    var ctx_a: u8 = 0;
    var ctx_b: u8 = 0;

    // a1 owns; a2 and b1 park behind it.
    var a1 = TestWaiter.init(&ctx_a);
    var a2 = TestWaiter.init(&ctx_a);
    var b1 = TestWaiter.init(&ctx_b);
    try testing.expectEqual(true, try engine.acquireGate(&a1.waiter));
    try testing.expectEqual(false, try engine.acquireGate(&a2.waiter));
    try testing.expectEqual(false, try engine.acquireGate(&b1.waiter));

    // Detaching context A cancels both its waiters (owner + parked, no wakes
    // for either) and hands the gate straight to b1.
    engine.detach(&ctx_a);
    try testing.expectEqual(1, a1.cancelled);
    try testing.expectEqual(0, a1.woken);
    try testing.expectEqual(1, a2.cancelled);
    try testing.expectEqual(0, a2.woken);
    try testing.expectEqual(0, b1.cancelled);
    try testing.expectEqual(1, b1.woken);
    try testing.expect(engine._gate_owner == &b1.waiter);

    // Detaching a context with no participants is a no-op.
    engine.detach(&ctx_a);
    try testing.expect(engine._gate_owner == &b1.waiter);

    // releaseGate reports whether the caller actually owned the gate.
    try testing.expectEqual(false, engine.releaseGate(&a1.waiter));
    try testing.expectEqual(true, engine.releaseGate(&b1.waiter));
    try testing.expectEqual(null, engine._gate_owner);
}

test "IDB - Engine: open creates schema" {
    var engine = try Engine.open(":memory:");
    defer engine.close();
    try testing.expectEqual(null, try engine.databaseVersion(null, "missing"));
}

test "IDB - Engine: database + object store + add/get round-trip" {
    var engine = try Engine.open(":memory:");
    defer engine.close();

    const db_id = try engine.upsertDatabase(null, "app", 1);
    try testing.expectEqual(1, (try engine.databaseVersion(null, "app")).?);

    const store_id = try engine.createObjectStore(null, testing.allocator, db_id, "books", null, false);

    // Value bytes deliberately include an embedded NUL and high bytes to prove
    // the BLOB path is binary-safe (serialized values are arbitrary bytes).
    const value = "a\x00b\xffc";
    try engine.add(null, store_id, "key1", value);

    const got = (try engine.get(null, testing.allocator, store_id, "key1")).?;
    defer testing.allocator.free(got);
    try testing.expectEqualSlices(u8, value, got);

    try testing.expectEqual(null, try engine.get(null, testing.allocator, store_id, "absent"));
}

test "IDB - Engine: add rejects duplicate key, put overwrites" {
    var engine = try Engine.open(":memory:");
    defer engine.close();

    const db_id = try engine.upsertDatabase(null, "app", 1);
    const store_id = try engine.createObjectStore(null, testing.allocator, db_id, "s", null, false);

    try engine.add(null, store_id, "k", "v1");
    try testing.expectError(error.Constraint, engine.add(null, store_id, "k", "v2"));

    try engine.put(null, store_id, "k", "v2");
    const got = (try engine.get(null, testing.allocator, store_id, "k")).?;
    defer testing.allocator.free(got);
    try testing.expectEqualSlices(u8, "v2", got);
}

test "IDB - Engine: createObjectStore rejects duplicate name" {
    var engine = try Engine.open(":memory:");
    defer engine.close();

    const db_id = try engine.upsertDatabase(null, "app", 1);
    _ = try engine.createObjectStore(null, testing.allocator, db_id, "s", null, false);
    try testing.expectError(error.Constraint, engine.createObjectStore(null, testing.allocator, db_id, "s", null, false));
}

test "IDB - Engine: rollback discards uncommitted writes" {
    var engine = try Engine.open(":memory:");
    defer engine.close();

    const db_id = try engine.upsertDatabase(null, "app", 1);
    const store_id = try engine.createObjectStore(null, testing.allocator, db_id, "s", null, false);

    try engine.begin(null);
    try engine.add(null, store_id, "k", "v");
    engine.rollback(null);

    try testing.expectEqual(null, try engine.get(null, testing.allocator, store_id, "k"));
}

// Seed a store with numeric keys n and values "v<n>", inserted out of order so
// the range tests prove ORDER BY rather than insertion order.
fn seedNumbers(engine: *Engine, store_id: i64, arena: Allocator, ns: []const u8) !void {
    for (ns) |n| {
        const enc = try Key.number(@floatFromInt(n)).encode(arena);
        var buf: [8]u8 = undefined;
        try engine.add(null, store_id, enc, try std.mem.print(&buf, "v{d}", .{n}));
    }
}

fn numKey(arena: Allocator, n: u8) ![]u8 {
    return Key.number(@floatFromInt(n)).encode(arena);
}

fn boundsFor(lower: ?[]const u8, lower_open: bool, upper: ?[]const u8, upper_open: bool) Engine.Bounds {
    return .{
        .is_point = false,
        .lower = lower orelse Engine.Bounds.min_sentinel,
        .upper = upper orelse Engine.Bounds.max_sentinel,
        .lower_op = if (lower_open) "> " else ">= ",
        .upper_op = if (upper_open) "< " else "<= ",
    };
}
