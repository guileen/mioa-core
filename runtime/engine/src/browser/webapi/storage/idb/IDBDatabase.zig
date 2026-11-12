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

const js = @import("../../../js/js.zig");

const EventTarget = @import("../../EventTarget.zig");

const Key = @import("Key.zig");
const Engine = @import("Engine.zig");
const SchemaSnapshot = @import("SchemaSnapshot.zig");
const IDBTransaction = @import("IDBTransaction.zig");
const IDBObjectStore = @import("IDBObjectStore.zig");
const DOMStringList = @import("../../collections.zig").DOMStringList;

const Execution = js.Execution;
const Allocator = std.mem.Allocator;

const IDBDatabase = @This();

pub const Proto = EventTarget;

_proto: *EventTarget,
_exec: *Execution,
_engine: *Engine,
_database_id: i64,
_name: []const u8,
_version: i64,
_snapshot: SchemaSnapshot.Snapshot = .{},
_snapshot_arena: ?std.heap.ArenaAllocator = null,
_snapshot_resource: Engine.ContextResource,
_txn: ?*IDBTransaction = null, // only set during upgradeneeded
_closed: bool = false,

pub fn init(exec: *Execution, engine: *Engine, database_id: i64, name: []const u8, version: i64) !*IDBDatabase {
    const self = try exec._factory.eventTarget(IDBDatabase{
        ._proto = undefined,
        ._exec = exec,
        ._engine = engine,
        ._database_id = database_id,
        ._name = name,
        ._snapshot_resource = .{ .ctx = exec.js, .release = releaseSnapshot },
        ._version = version,
    });
    if (engine.profile_async != null) engine.registerContextResource(&self._snapshot_resource);
    return self;
}

pub fn asEventTarget(self: *IDBDatabase) *EventTarget {
    return self._proto;
}

// Takes ownership of an arena allocated against the negotiated Profile grant.
// The replacement is fully prepared before commit, so publication cannot fail.
pub fn replaceSnapshot(self: *IDBDatabase, arena: std.heap.ArenaAllocator, snapshot: SchemaSnapshot.Snapshot) void {
    if (self._snapshot_arena) |*old| old.deinit();
    self._snapshot_arena = arena;
    self._snapshot = snapshot;
}

fn releaseSnapshot(resource: *Engine.ContextResource) void {
    const self: *IDBDatabase = @fieldParentPtr("_snapshot_resource", resource);
    if (self._snapshot_arena) |*arena| arena.deinit();
    self._snapshot_arena = null;
    self._snapshot = .{};
}

const CreateObjectStoreOptions = struct {
    autoIncrement: bool = false,
    keyPath: ?Key.KeyPath = null,
};

// Only callable while the upgrade transaction is live and active, hence the checks
pub fn createObjectStore(
    self: *IDBDatabase,
    name: []const u8,
    options: ?CreateObjectStoreOptions,
) !*IDBObjectStore {
    const txn = self._txn orelse return error.InvalidStateError;
    if (txn._settled) {
        return error.InvalidStateError;
    }
    try txn.assertActive();

    const opts = options orelse CreateObjectStoreOptions{};

    // Spec order: key path syntax, then the name, then autoIncrement vs. key path.
    if (opts.keyPath) |kp| {
        if (Key.isValidKeyPathSpec(kp) == false) {
            return error.SyntaxError;
        }
    }
    const exists = if (self._engine.profile_async != null)
        txn.schemaSnapshot().findStore(name) != null
    else
        (try self._engine.objectStoreId(txn.gateWaiter(), self._database_id, name)) != null;
    if (exists) {
        return error.ConstraintError;
    }

    // Copy the key path onto the transaction arena so it outlives the call.
    // autoIncrement is incompatible with an empty or compound key path.
    const key_path: ?Key.KeyPath = if (opts.keyPath) |kp| blk: {
        if (opts.autoIncrement and keyPathBlocksAutoIncrement(kp)) {
            return error.InvalidAccessError;
        }
        break :blk try Key.dupeKeyPath(txn._arena.allocator(), kp);
    } else null;

    const owned_name = try txn.dupe(name);
    if (self._engine.profile_async != null) {
        const snapshot = txn.schemaSnapshot();
        const request = try txn.reserveSchemaRequest();
        const id = try snapshot.allocStoreId();
        try snapshot.addStore(txn.schemaAllocator(), owned_name, .{
            .id = id,
            .key_path = key_path,
            .auto_increment = opts.autoIncrement,
        });
        errdefer snapshot.removeStore(owned_name) catch {};
        const store = try IDBObjectStore.init(txn, id, owned_name, key_path, opts.autoIncrement);
        store._created = true;
        try txn.cacheStore(store);
        errdefer txn.uncacheStore(owned_name);
        try request.submitSchema(.{ .create_store = .{
            .id = id,
            .database_id = self._database_id,
            .name = owned_name,
            .key_path = key_path,
            .auto_increment = opts.autoIncrement,
        } });
        return store;
    }
    const store_id = self._engine.createObjectStore(
        txn.gateWaiter(),
        txn._arena.allocator(),
        self._database_id,
        name,
        key_path,
        opts.autoIncrement,
    ) catch |err| switch (err) {
        error.Constraint => return error.ConstraintError,
        else => return err,
    };
    const store = try IDBObjectStore.init(txn, store_id, owned_name, key_path, opts.autoIncrement);
    store._created = true;
    try txn.cacheStore(store);
    return store;
}

// autoIncrement requires an out-of-line or single-property in-line key; an empty
// or compound key path can't carry a generated key.
fn keyPathBlocksAutoIncrement(kp: Key.KeyPath) bool {
    return switch (kp) {
        .string => |s| s.len == 0,
        .list => true,
    };
}

// Only callable while the upgrade transaction is live and active, hence the checks
fn deleteObjectStore(self: *IDBDatabase, name: []const u8, _: *Execution) !void {
    const txn = self._txn orelse return error.InvalidStateError;
    if (txn._settled) {
        return error.InvalidStateError;
    }
    try txn.assertActive();
    if (self._engine.profile_async != null) {
        const snapshot = txn.schemaSnapshot();
        if (snapshot.findStore(name) == null) return error.NotFoundError;
        const owned_name = try txn.dupe(name);
        const request = try txn.reserveSchemaRequest();
        try request.submitSchema(.{ .delete_store = .{
            .database_id = self._database_id,
            .name = owned_name,
        } });
        try snapshot.removeStore(name);
    } else {
        try self._engine.deleteObjectStore(txn.gateWaiter(), self._database_id, name);
    }
    txn.uncacheStore(name);
}

const TransactionMode = enum {
    readonly,
    readwrite,
    pub const js_enum_from_string = true;
};

const StoreNames = union(enum) {
    list: *DOMStringList,
    name: []const u8,
    names: []const []const u8,

    fn slice(self: *const StoreNames) []const []const u8 {
        return switch (self.*) {
            .names => |names| names,
            .list => |l| l._items,
            .name => |*n| (n)[0..1],
        };
    }
};

const TransactionOptions = struct {
    durability: IDBTransaction.Durability = .default,
};

pub fn transaction(
    self: *IDBDatabase,
    store_names: StoreNames,
    mode: ?TransactionMode,
    options: ?TransactionOptions,
    exec: *Execution,
) !*IDBTransaction {
    if (self._closed) {
        return error.InvalidStateError;
    }
    if (self._txn) |upgrade| {
        // An upgrade still running owns the connection; once it settled, new
        // transactions may start even before the open request's success fires.
        if (!upgrade._settled) {
            return error.InvalidStateError;
        }
    }

    const names = store_names.slice();
    if (names.len == 0) {
        return error.InvalidAccessError;
    }
    for (names) |name| {
        try self.assertStoreExists(name);
    }

    const opts = options orelse TransactionOptions{};
    const txn = try IDBTransaction.init(self, switch (mode orelse .readonly) {
        .readonly => .readonly,
        .readwrite => .readwrite,
    }, opts.durability, exec);
    txn._scope = try normalizeStoreNames(txn._arena.allocator(), store_names);
    return txn;
}

fn assertStoreExists(self: *const IDBDatabase, name: []const u8) !void {
    if (self._engine.profile_async != null) {
        if (self._snapshot.findStore(name) == null) return error.NotFound;
        return;
    }
    if ((try self._engine.objectStoreId(null, self._database_id, name)) == null) {
        return error.NotFound;
    }
}

// The transaction's scope: the requested store names, sorted with duplicates
// removed (per the IndexedDB spec's "transaction scope" steps).
fn normalizeStoreNames(arena: Allocator, store_names: StoreNames) ![]const []const u8 {
    const names = store_names.slice();
    var list: std.ArrayList([]const u8) = try .initCapacity(arena, names.len);
    for (names) |name| {
        list.appendAssumeCapacity(try arena.dupe(u8, name));
    }

    std.mem.sort([]const u8, list.items, {}, struct {
        fn lessThan(_: void, a: []const u8, b: []const u8) bool {
            return std.mem.lessThan(u8, a, b);
        }
    }.lessThan);

    // Drop duplicates now that equal names are adjacent. The first name always
    // survives, so only the rest need comparing.
    if (list.items.len <= 1) {
        return list.items;
    }

    var write: usize = 1;
    for (list.items[1..]) |name| {
        if (!std.mem.eql(u8, name, list.items[write - 1])) {
            list.items[write] = name;
            write += 1;
        }
    }
    return list.items[0..write];
}

pub fn close(self: *IDBDatabase) void {
    self._closed = true;
}

pub fn getName(self: *const IDBDatabase) []const u8 {
    return self._name;
}

pub fn getVersion(self: *const IDBDatabase) i64 {
    return self._version;
}

pub fn getObjectStoreNames(self: *IDBDatabase, exec: *Execution) !*DOMStringList {
    const arena = try exec.getArena(.small, "IDB.getObjectStoreNames");
    errdefer arena.release();

    const names = if (self._engine.profile_async != null)
        try (if (self._txn) |txn| txn.schemaSnapshot() else &self._snapshot).storeNames(arena.allocator())
    else blk: {
        const waiter = if (self._txn) |txn| txn.metadataWaiter() else null;
        break :blk try self._engine.objectStoreNames(waiter, arena.allocator(), self._database_id);
    };
    const list = try arena.create(DOMStringList);
    list.* = .{ ._items = names, ._arena = arena };
    return list;
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(IDBDatabase);

    pub const Meta = struct {
        pub const name = "IDBDatabase";
        pub var class_id: bridge.ClassId = undefined;
    };

    pub const name = bridge.accessor(IDBDatabase.getName, null, .{});
    pub const version = bridge.accessor(IDBDatabase.getVersion, null, .{});
    pub const objectStoreNames = bridge.accessor(IDBDatabase.getObjectStoreNames, null, .{});
    pub const createObjectStore = bridge.function(IDBDatabase.createObjectStore, .{});
    pub const deleteObjectStore = bridge.function(IDBDatabase.deleteObjectStore, .{});
    pub const transaction = bridge.function(IDBDatabase.transaction, .{});
    pub const close = bridge.function(IDBDatabase.close, .{});
    pub const onerror = EventTarget.handlerAccessor(IDBDatabase, "error");
    pub const onabort = EventTarget.handlerAccessor(IDBDatabase, "abort");
};
