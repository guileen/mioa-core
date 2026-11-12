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

const js = @import("../../../js/js.zig");
const Page = @import("../../../Page.zig");
const Factory = @import("../../../Factory.zig");

const Key = @import("Key.zig");
const Engine = @import("Engine.zig");
const IDBIndex = @import("IDBIndex.zig");
const IDBRequest = @import("IDBRequest.zig");
const IDBObjectStore = @import("IDBObjectStore.zig");
const IDBTransaction = @import("IDBTransaction.zig");
const IDBCursorWithValue = @import("IDBCursorWithValue.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const ProfileAsync = @import("../ProfileAsync.zig");

const log = lp.log;
const Execution = js.Execution;

const IDBCursor = @This();

_engine: *Engine,
_store: *IDBObjectStore,
_txn: *IDBTransaction,
_request: *IDBRequest,
_bounds: Engine.Bounds,
_direction: Direction,
_key_only: bool,
_source: Source,
_got_value: bool = false,

// null for an object-store cursor
_index_id: ?i64 = null,

// the JS value of this Cursor, pre-converted and cached as an optimization
// since this cursor will be the request value on every iteration.
_js: *js.GlobalSlot,

// Encoded current key; null before iteration and at the end. For an index cursor
// this is the index key; for an object store it equals the primary key.
_key: ?[]const u8 = null,

// Encoded primary (store) key. Equals _key for an object-store cursor.
_primary_key: ?[]const u8 = null,
// Current record's serialized value bytes (null when key-only or exhausted).
_value: ?[]const u8 = null,
// The deserialized JS value, cached so repeated `.value` reads return the same
// object (and observe mutations to it). Reset whenever the cursor repositions.
_value_js: ?*js.GlobalSlot = null,

// Backing storage for _key/_primary_key/_value, reused across positions so a
// long scan holds one record's worth of memory, not the whole traversal.
// Anything that must survive a reposition (update/delete/continue snapshots)
// is duped onto the transaction's arena instead.
_key_buf: std.ArrayList(u8) = .empty,
_pk_buf: std.ArrayList(u8) = .empty,
_val_buf: std.ArrayList(u8) = .empty,

pub const Direction = enum {
    next,
    nextunique,
    prev,
    prevunique,

    pub const js_enum_from_string = true;

    pub fn toString(self: Direction) []const u8 {
        return @tagName(self);
    }

    fn reverse(self: Direction) bool {
        return self == .prev or self == .prevunique;
    }

    fn unique(self: Direction) bool {
        return self == .nextunique or self == .prevunique;
    }
};

// What this cursor iterates — used only by the `source` accessor.
const Source = union(enum) {
    store: *IDBObjectStore,
    index: *IDBIndex,
};

// How the next iterate() positions the cursor.
pub const Seek = union(enum) {
    first, // start of the range, in the iteration direction
    next, // strictly past the current position
    to: []const u8, // continue(key): first record at/after an index/store key
    to_primary: struct { key: []const u8, primary_key: []const u8 }, // continuePrimaryKey
};

fn startSentinel(reverse: bool) []const u8 {
    return if (reverse) Engine.Bounds.max_sentinel else Engine.Bounds.min_sentinel;
}

// Cursor over an object store.
pub fn init(store: *IDBObjectStore, bounds: Engine.Bounds, direction: Direction, key_only: bool, exec: *Execution) !*IDBRequest {
    return _init(store, store._txn, null, .{ .store = store }, bounds, direction, key_only, exec);
}

// Cursor over an index (key = index key, primaryKey = store key).
pub fn initIndex(index: *IDBIndex, bounds: Engine.Bounds, direction: Direction, key_only: bool, exec: *Execution) !*IDBRequest {
    const store = index._store;
    return _init(store, store._txn, index._index_id, .{ .index = index }, bounds, direction, key_only, exec);
}

fn _init(store: *IDBObjectStore, txn: *IDBTransaction, index_id: ?i64, source: Source, bounds: Engine.Bounds, direction: Direction, key_only: bool, exec: *Execution) !*IDBRequest {
    try txn.assertActive();

    const request = try txn.newRequest();
    const cursor_value = IDBCursor{
        ._engine = store._engine,
        ._store = store,
        ._txn = txn,
        ._request = request,
        ._bounds = bounds,
        ._direction = direction,
        ._key_only = key_only,
        ._index_id = index_id,
        ._js = undefined,
        ._source = source,
    };

    const local = exec.js.local.?;
    var self: *IDBCursor = undefined;
    var public: js.Value = undefined;
    if (key_only) {
        self = try txn._arena.create(IDBCursor);
        self.* = cursor_value;
        public = try local.zigValueToJs(self, .{});
    } else {
        const with_value = try Factory.chainedWithAllocator(txn._arena.allocator(), .{
            cursor_value,
            IDBCursorWithValue{ ._proto = undefined },
        });
        self = with_value._proto;
        public = try local.zigValueToJs(with_value, .{});
    }
    request._cursor = self;

    // Pre-converted and cached because it's the request value on every iteration,
    // avoiding the bridge's pointer -> js.Value lookup each time.
    self._js = try txn.persist(public);

    // The first seek runs in the drain (or immediately, for a versionchange txn),
    // not here — so the connection is only touched while the txn owns it.
    return request.submit(.{ .cursor_iterate = .{ .cursor = self, .seek = .first, .offset = 0 } }, exec);
}

pub fn acquireRef(self: *IDBCursor) void {
    self._txn.acquireRef();
}

pub fn releaseRef(self: *IDBCursor, page: *Page) void {
    self._txn.releaseRef(page);
}

// Run the deferred seek, staging the positioned cursor (or null) on the request.
pub fn runIterate(self: *IDBCursor, seek: Seek, offset: u32, exec: *Execution) !void {
    self.iterate(seek, offset, exec) catch |err| {
        log.warn(.storage, "idb cursor iterate", .{ .err = err, .sqlite = self._engine.lastError() });
        self._request.setError(err);
    };
}

// Called by IDBRequest.deliver right before a cursor's success handler runs.
pub fn beforeDeliver(self: *IDBCursor) void {
    self._got_value = self._key != null;
}

pub fn @"continue"(self: *IDBCursor, key_arg: ?js.Value, exec: *Execution) !void {
    try self.assertIterable();

    // An explicit `undefined` is an absent optional argument, not a key.
    if (key_arg != null and !key_arg.?.isUndefined()) {
        const k = key_arg.?;
        // Key conversion (DataError) must run before the got-value flag is
        // cleared, so a failing continue() leaves the cursor re-iterable.
        const encoded = try Key.encodeValue(self._txn._arena.allocator(), k);
        // The target must move past the current key in the iteration direction.
        const order = std.mem.order(u8, encoded, self._key.?);
        if (if (self._direction.reverse()) order != .lt else order != .gt) {
            return error.DataError;
        }
        self._got_value = false;
        try self.reiterate(.{ .to = encoded }, 0, exec);
    } else {
        self._got_value = false;
        try self.reiterate(.next, 0, exec);
    }
}

fn continuePrimaryKey(self: *IDBCursor, key_arg: js.Value, primary_key_arg: js.Value, exec: *Execution) !void {
    // Only meaningful on an index cursor with a directed (non-unique) direction.
    if (self._index_id == null or self._direction.unique()) {
        return error.InvalidAccessError;
    }
    try self.assertIterable();

    const reverse = self._direction.reverse();
    // Key conversion (DataError) precedes clearing the got-value flag; see continue().
    const key = try Key.encodeValue(self._txn._arena.allocator(), key_arg);
    const primary_key = try Key.encodeValue(self._txn._arena.allocator(), primary_key_arg);

    // The (key, primaryKey) pair must move past the current position.
    const ok = switch (std.mem.order(u8, key, self._key.?)) {
        .gt => !reverse,
        .lt => reverse,
        .eq => blk: {
            const pk_order = std.mem.order(u8, primary_key, self._primary_key.?);
            break :blk if (reverse) pk_order == .lt else pk_order == .gt;
        },
    };
    if (!ok) return error.DataError;

    self._got_value = false;
    try self.reiterate(.{ .to_primary = .{ .key = key, .primary_key = primary_key } }, 0, exec);
}

pub fn advance(self: *IDBCursor, count: u32, exec: *Execution) !void {
    if (count == 0) {
        return error.TypeError;
    }

    try self.assertIterable();
    self._got_value = false;
    // `count` records forward = skip count-1 past the immediate next.
    try self.reiterate(.next, count - 1, exec);
}

pub fn update(self: *IDBCursor, value: js.Value, exec: *Execution) !*IDBRequest {
    try self.assertCanUpdate();

    // The record sits at the primary (store) key, even for an index cursor.
    const current_key = self._primary_key orelse return error.InvalidStateError;

    // Structured-clone the value now, synchronously: an unserializable value must
    // throw DataCloneError from update() itself, not fail later in the drain. The
    // stored clone also decouples the record from any later mutation of the arg.
    const serialized = blk: {
        self._txn._cloning = true;
        defer self._txn._cloning = false;
        break :blk value.serialize() catch |err| return if (err == error.JsException) error.TryCatchRethrow else err;
    };
    const clone = try self._txn.holdClone(serialized);
    errdefer self._txn.releaseClone(clone);

    // For an in-line store, the value's own key must match the record's key.
    if (self._store._key_path) |kp| {
        const extracted = (try Key.extractKeyPath(exec.js.local.?, value, kp)) orelse return error.DataError;
        const encoded = try Key.encodeValue(exec.call_arena, extracted);
        if (!std.mem.eql(u8, encoded, current_key)) {
            return error.DataError;
        }
    }

    // Snapshot the record key onto the transaction arena: the write runs in the
    // drain, by which point a `continue` could have moved the cursor's live
    // position (and reused its key buffer).
    const key = try self._txn._arena.dupe(u8, current_key);
    const request = try self._txn.newRequest();
    return request.submit(.{ .cursor_update = .{ .cursor = self, .key = key, .value = clone } }, exec);
}

pub fn runUpdate(self: *IDBCursor, request: *IDBRequest, key: []const u8, clone: usize, exec: *Execution) !void {
    defer self._txn.releaseClone(clone);
    self._store.writeAt(key, self._txn.cloneBytes(clone), exec) catch |err| {
        log.warn(.storage, "idb cursor update", .{ .err = err, .sqlite = self._engine.lastError() });
        request.setError(err);
        return;
    };
    try request.setValue(try Key.decodeToJs(exec.call_arena, exec.js.local.?, key));
}

pub fn runUpdateAsync(self: *IDBCursor, request: *IDBRequest, key: []const u8, clone: usize, indexes: []const Engine.IndexInfo, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    return @import("StoreWriteAsync.zig").run(self._store, request, .put, clone, .{ .explicit = .{ .encoded = key, .bump = null } }, exec, ctx, done, indexes);
}

pub fn delete(self: *IDBCursor, exec: *Execution) !*IDBRequest {
    try self.assertCanUpdate();

    // Snapshot the record key (see update): the delete runs later, in the drain.
    const current_key = self._primary_key orelse return error.InvalidStateError;
    const key = try self._txn._arena.dupe(u8, current_key);
    const request = try self._txn.newRequest();
    return request.submit(.{ .cursor_delete = .{ .cursor = self, .key = key } }, exec);
}

pub fn runDelete(self: *IDBCursor, request: *IDBRequest, key: []const u8) !void {
    self._store.deleteAt(key) catch |err| {
        log.warn(.storage, "idb cursor delete", .{ .err = err, .sqlite = self._engine.lastError() });
        request.setError(err);
    };
}

pub fn runDeleteAsync(self: *IDBCursor, request: *IDBRequest, key: []const u8, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    return self._store.runDeleteAsync(request, Engine.Bounds.point(key), exec, ctx, done);
}

pub fn getKey(self: *const IDBCursor, exec: *Execution) !?js.Value {
    const encoded = self._key orelse return null;
    return try Key.decodeToJs(exec.call_arena, exec.js.local.?, encoded);
}

fn getPrimaryKey(self: *const IDBCursor, exec: *Execution) !?js.Value {
    const encoded = self._primary_key orelse return null;
    return try Key.decodeToJs(exec.call_arena, exec.js.local.?, encoded);
}

fn getDirection(self: *const IDBCursor) Direction {
    return self._direction;
}

// The bridge converts the active union variant (the IDBObjectStore/IDBIndex).
fn getSource(self: *const IDBCursor) Source {
    return self._source;
}

const SeekPlan = union(enum) {
    store: struct { from_op: []const u8, from_key: []const u8 },
    index: struct { from_key: []const u8, from_pk: []const u8, pk_inclusive: bool },
};

// Both the local and owner-backed paths must use exactly the same seek rules.
fn seekPlan(self: *const IDBCursor, seek: Seek) !SeekPlan {
    const reverse = self._direction.reverse();
    if (self._index_id != null) {
        const unique = self._direction.unique();
        const from_key, const from_pk, const inclusive = switch (seek) {
            .first => .{ startSentinel(reverse), startSentinel(reverse), false },
            .next => .{ self._key.?, if (unique) startSentinel(!reverse) else self._primary_key.?, false },
            .to => |key| .{ key, startSentinel(reverse), false },
            .to_primary => |pair| .{ pair.key, pair.primary_key, true },
        };
        return .{ .index = .{ .from_key = from_key, .from_pk = from_pk, .pk_inclusive = inclusive } };
    }
    const from_op, const from_key = switch (seek) {
        .first => .{ if (reverse) "<= " else ">= ", startSentinel(reverse) },
        .next => .{ if (reverse) "< " else "> ", self._key.? },
        .to => |key| .{ if (reverse) "<= " else ">= ", key },
        .to_primary => return error.InvalidAccessError,
    };
    return .{ .store = .{ .from_op = from_op, .from_key = from_key } };
}

// Position copies the row into buffers that survive event-loop turns.
fn iterate(self: *IDBCursor, seek: Seek, offset: u32, exec: *Execution) !void {
    const reverse = self._direction.reverse();
    const arena = exec.call_arena;
    const store_id = self._store._store_id;
    switch (try self.seekPlan(seek)) {
        .index => |plan| {
            const rec = try self._engine.indexCursorSeek(self._txn.gateWaiter(), arena, store_id, self._index_id.?, self._bounds, reverse, self._direction.unique(), plan.from_key, plan.from_pk, plan.pk_inclusive, !self._key_only, offset);
            if (rec) |r| try self.position(r.key, r.primary_key, r.value) else self.exhaust();
        },
        .store => |plan| {
            const rec = try self._engine.cursorSeek(self._txn.gateWaiter(), arena, store_id, self._bounds, reverse, plan.from_op, plan.from_key, offset, !self._key_only);
            if (rec) |r| try self.position(r.key, r.key, r.value) else self.exhaust();
        },
    }
}

// Owner operations never touch the JS context on the profile transport callback.
const AsyncSeek = struct {
    cursor: *IDBCursor,
    exec: *Execution,
    ctx: *anyopaque,
    done: IDBRequest.AsyncDone,
    plan: SeekPlan,
    offset: u32,
    allocator: std.mem.Allocator,
    refs: [7]?RemoteAsync.BlobRef = @splat(null),
    values: [3]?[]u8 = @splat(null),
    step: usize = 0,
    transport: *ProfileAsync.Client,
    owned_origin: []u8,
    active: ?*RemoteAsync.Handle = null,
    request_id: ?u64 = null,
    detached: bool = false,
    scheduled: bool = false,
    cleanup_index: usize = 0,
    failure: ?anyerror = null,
    found: bool = false,

    fn client(self: *AsyncSeek) *ProfileAsync.Client {
        return self.transport;
    }
    fn origin(self: *AsyncSeek) []const u8 {
        return self.owned_origin;
    }

    fn start(self: *AsyncSeek) !void {
        self.active = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.cursor._bounds.lower, self, uploaded);
    }

    fn uploaded(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        self.active = null;
        switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .value => |ref| self.refs[self.step] = ref,
        }
        if (self.detached) {
            self.cleanup();
            return;
        }
        self.step += 1;
        const bytes: ?[]const u8 = switch (self.step) {
            1 => self.cursor._bounds.upper,
            2 => switch (self.plan) {
                .store => |p| p.from_key,
                .index => |p| p.from_key,
            },
            3 => switch (self.plan) {
                .store => null,
                .index => |p| p.from_pk,
            },
            else => null,
        };
        if (bytes) |data| {
            self.active = RemoteAsync.upload(self.allocator, self.client(), self.origin(), data, self, uploaded) catch |err| {
                self.fail(err);
                return;
            };
        } else self.submitSeek() catch |err| self.fail(err);
    }

    fn submitSeek(self: *AsyncSeek) !void {
        const cursor = self.cursor;
        const ticket = cursor._txn.gateWaiter().ticket orelse return error.InvalidState;
        var store_buf: [32]u8 = undefined;
        const store = try std.fmt.bufPrint(&store_buf, "{d}", .{cursor._store._store_id});
        const b = cursor._bounds;
        const bounds = .{ .lower = self.refs[0].?, .upper = self.refs[1].?, .lower_open = !b.is_point and std.mem.eql(u8, b.lower_op, "> "), .upper_open = !b.is_point and std.mem.eql(u8, b.upper_op, "< "), .is_point = b.is_point };
        switch (self.plan) {
            .store => |plan| {
                const op = if (std.mem.eql(u8, plan.from_op, "> ")) "gt" else if (std.mem.eql(u8, plan.from_op, ">= ")) "gte" else if (std.mem.eql(u8, plan.from_op, "< ")) "lt" else "lte";
                self.request_id = try RemoteAsync.request(self.allocator, self.client(), self.origin(), ticket, "idb.cursorSeek", .{ .object_store_id = store, .b = bounds, .reverse = cursor._direction.reverse(), .from_op = op, .from_key = self.refs[2].?, .offset = self.offset, .with_value = !cursor._key_only }, self, sought);
            },
            .index => |plan| {
                var index_buf: [32]u8 = undefined;
                const index = try std.fmt.bufPrint(&index_buf, "{d}", .{cursor._index_id.?});
                self.request_id = try RemoteAsync.request(self.allocator, self.client(), self.origin(), ticket, "idb.indexCursorSeek", .{ .object_store_id = store, .index_id = index, .b = bounds, .reverse = cursor._direction.reverse(), .unique = cursor._direction.unique(), .from_key = self.refs[2].?, .from_pk = self.refs[3].?, .pk_inclusive = plan.pk_inclusive, .with_value = !cursor._key_only, .offset = self.offset }, self, sought);
            },
        }
    }

    fn copyRef(self: *AsyncSeek, value: std.json.Value) !RemoteAsync.BlobRef {
        const parsed = std.json.parseFromValue(RemoteAsync.BlobRef, self.allocator, value, .{ .allocate = .alloc_always }) catch return error.ProfileProtocolError;
        defer parsed.deinit();
        const id = try self.allocator.dupe(u8, parsed.value.blob_id);
        errdefer self.allocator.free(id);
        return .{ .blob_id = id, .size = try self.allocator.dupe(u8, parsed.value.size) };
    }

    fn sought(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        const reply = switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .reply => |r| r,
        };
        defer reply.deinit();
        if (self.detached) {
            self.cleanup();
            return;
        }
        const value = reply.value.object.get("result") orelse {
            self.fail(error.ProfileProtocolError);
            return;
        };
        if (value == .null) {
            self.cleanup();
            return;
        }
        if (value != .object) {
            self.badReply(error.ProfileProtocolError);
            return;
        }
        self.found = true;
        const key = value.object.get("key") orelse {
            self.badReply(error.ProfileProtocolError);
            return;
        };
        self.refs[4] = self.copyRef(key) catch |err| {
            self.badReply(err);
            return;
        };
        if (self.cursor._index_id != null) {
            const primary = value.object.get("primary_key") orelse {
                self.badReply(error.ProfileProtocolError);
                return;
            };
            self.refs[5] = self.copyRef(primary) catch |err| {
                self.badReply(err);
                return;
            };
        }
        const v = value.object.get("value") orelse {
            self.badReply(error.ProfileProtocolError);
            return;
        };
        if (v != .null) {
            self.refs[6] = self.copyRef(v) catch |err| {
                self.badReply(err);
                return;
            };
        } else if (!self.cursor._key_only) {
            self.badReply(error.ProfileProtocolError);
            return;
        }
        self.step = 0;
        self.downloadNext();
    }

    // A malformed or uncopyable reply may contain blob leases we cannot name.
    // Closing the channel is the only way to release those owner resources.
    fn badReply(self: *AsyncSeek, err: anyerror) void {
        self.client().deinit();
        self.fail(err);
    }

    fn downloadNext(self: *AsyncSeek) void {
        while (self.step < 3) : (self.step += 1) {
            if (self.refs[4 + self.step]) |ref| {
                self.active = RemoteAsync.download(self.allocator, self.client(), self.origin(), ref, self, downloaded) catch |err| {
                    self.fail(err);
                    return;
                };
                return;
            }
        }
        self.cleanup();
    }

    fn downloaded(ctx: *anyopaque, result: RemoteAsync.BytesResult) void {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        self.active = null;
        switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .value => |bytes| self.values[self.step] = bytes,
        }
        if (self.detached) {
            self.cleanup();
            return;
        }
        self.step += 1;
        self.downloadNext();
    }

    fn fail(self: *AsyncSeek, err: anyerror) void {
        if (self.failure == null) self.failure = err;
        self.cleanup();
    }

    fn cleanup(self: *AsyncSeek) void {
        while (self.cleanup_index < self.refs.len) {
            const index = self.cleanup_index;
            self.cleanup_index += 1;
            if (self.refs[index]) |ref| {
                self.active = RemoteAsync.drop(self.allocator, self.client(), self.origin(), ref, self, dropped) catch |err| {
                    if (self.failure == null) self.failure = err;
                    self.client().deinit();
                    continue;
                };
                return;
            }
        }
        if (self.detached) {
            self.finish(error.Abort);
            return;
        }
        self.scheduled = true;
        self.exec.js.scheduler.add(self, resumeSeek, 0, .{ .name = "IDBCursor.ownerSeek", .finalizer = cancelled }) catch |err| self.finish(err);
    }

    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        self.active = null;
        if (err) |e| if (self.failure == null) {
            self.failure = e;
        };
        self.cleanup();
    }

    fn resumeSeek(ctx: *anyopaque) !?u32 {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        if (self.detached) {
            self.finish(error.Abort);
            return null;
        }
        var result = self.failure;
        if (result == null and !self.cursor._txn._settled) {
            const exec = self.exec;
            const previous = exec.js.local;
            defer exec.js.local = previous;
            var scope: js.Local.Scope = undefined;
            exec.js.localScope(&scope);
            defer scope.deinit();
            exec.js.local = &scope.local;
            if (self.found) {
                if (self.values[0]) |key| {
                    const primary = if (self.cursor._index_id != null) self.values[1] else key;
                    if (primary) |pk| {
                        self.cursor.position(key, pk, self.values[2]) catch |err| {
                            result = err;
                        };
                    } else result = error.ProfileProtocolError;
                } else result = error.ProfileProtocolError;
            } else self.cursor.exhaust();
        }
        self.finish(result);
        return null;
    }

    fn cancelled(ctx: *anyopaque) void {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        self.finish(error.Abort);
    }

    fn cancel(ctx: *anyopaque) void {
        const self: *AsyncSeek = @ptrCast(@alignCast(ctx));
        self.detached = true;
        // Release the transaction pin before teardown. Remaining cleanup owns
        // only the profile client, copied origin and detached buffers.
        self.done(self.ctx, error.Abort);
        if (self.active) |handle| {
            handle.cancel();
        } else if (self.request_id) |id| {
            self.client().cancel(id);
        } else if (!self.scheduled) self.cleanup();
    }

    fn finish(self: *AsyncSeek, err: ?anyerror) void {
        const done = self.done;
        const ctx = self.ctx;
        const allocator = self.allocator;
        const detached = self.detached;
        if (!detached) self.cursor._request.clearAsyncCancel();
        for (self.refs) |maybe_ref| if (maybe_ref) |ref| {
            allocator.free(ref.blob_id);
            allocator.free(ref.size);
        };
        for (self.values) |maybe_value| if (maybe_value) |value| allocator.free(value);
        allocator.free(self.owned_origin);
        allocator.destroy(self);
        if (!detached) done(ctx, err);
    }
};

pub fn runIterateAsync(self: *IDBCursor, seek: Seek, offset: u32, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    const allocator = self._engine.remote.?.allocator;
    const state = try allocator.create(AsyncSeek);
    errdefer allocator.destroy(state);
    const origin = try allocator.dupe(u8, self._engine.remote.?.origin);
    errdefer allocator.free(origin);
    state.* = .{ .cursor = self, .exec = exec, .ctx = ctx, .done = done, .plan = try self.seekPlan(seek), .offset = offset, .allocator = allocator, .transport = self._engine.profile_async.?, .owned_origin = origin };
    try state.start();
    self._request.setAsyncCancel(state, AsyncSeek.cancel);
    return .pending;
}

// Re-arm the cursor's existing request with its next seek and requeue it. Called by
// continue/advance from within a success handler; the drain loop picks the requeued
// request up in the same pass (a versionchange cursor runs it now).
fn reiterate(self: *IDBCursor, seek: Seek, offset: u32, exec: *Execution) !void {
    _ = try self._request.submit(.{ .cursor_iterate = .{ .cursor = self, .seek = seek, .offset = offset } }, exec);
}

fn position(self: *IDBCursor, key: []const u8, primary_key: []const u8, value: ?[]const u8) !void {
    const arena = self._txn._arena;
    self.invalidateValue();

    self._key_buf.clearRetainingCapacity();
    try self._key_buf.appendSlice(arena.allocator(), key);
    self._key = self._key_buf.items;

    self._pk_buf.clearRetainingCapacity();
    try self._pk_buf.appendSlice(arena.allocator(), primary_key);
    self._primary_key = self._pk_buf.items;

    if (value) |v| {
        self._val_buf.clearRetainingCapacity();
        try self._val_buf.appendSlice(arena.allocator(), v);
        self._value = self._val_buf.items;
    } else {
        self._value = null;
    }
    self._request.setValueGlobal(self._js);
}

fn exhaust(self: *IDBCursor) void {
    self.invalidateValue();
    self._key = null;
    self._primary_key = null;
    self._value = null;
    self._request.setNull();
}

// Drop any cached `.value` object; the next read re-deserializes at the new
// position. The persisted slot's handle is reset here so it doesn't pin the old
// value until transaction teardown.
fn invalidateValue(self: *IDBCursor) void {
    if (self._value_js) |slot| {
        slot.reset();
        self._value_js = null;
    }
}

// The deserialized current value, created on first read and cached so repeated
// `.value` accesses return the same JS object (see IDBCursorWithValue.getValue).
pub fn getValueJs(self: *IDBCursor, exec: *Execution) !?js.Value {
    const bytes = self._value orelse return null;
    const local = exec.js.local.?;
    if (self._value_js) |slot| {
        return slot.local(local);
    }
    const value = try js.Value.deserialize(local, bytes);
    self._value_js = try self._txn.persist(value);
    return value;
}

fn assertIterable(self: *IDBCursor) !void {
    if (self._txn._settled == true) {
        return error.TransactionInactiveError;
    }

    if (self._got_value == false) {
        return error.InvalidStateError;
    }
}

fn assertCanUpdate(self: *IDBCursor) !void {
    try self._txn.assertActive();

    if (self._txn._mode == .readonly) {
        return error.ReadOnlyError;
    }
    if (self._store._deleted) {
        return error.InvalidStateError;
    }
    if (self._got_value == false) {
        return error.InvalidStateError;
    }
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(IDBCursor);

    pub const Meta = struct {
        pub const name = "IDBCursor";
        pub var class_id: bridge.ClassId = undefined;
    };

    pub const key = bridge.accessor(IDBCursor.getKey, null, .{ .null_as_undefined = true });
    pub const primaryKey = bridge.accessor(IDBCursor.getPrimaryKey, null, .{ .null_as_undefined = true });
    pub const direction = bridge.accessor(IDBCursor.getDirection, null, .{});
    pub const source = bridge.accessor(IDBCursor.getSource, null, .{});
    pub const @"continue" = bridge.function(IDBCursor.@"continue", .{});
    pub const continuePrimaryKey = bridge.function(IDBCursor.continuePrimaryKey, .{});
    pub const advance = bridge.function(IDBCursor.advance, .{});
    pub const update = bridge.function(IDBCursor.update, .{});
    pub const delete = bridge.function(IDBCursor.delete, .{});
};
