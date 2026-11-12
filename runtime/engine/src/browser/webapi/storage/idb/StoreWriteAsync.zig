// Asynchronous record mutations on the Profile IndexedDB connection.
const std = @import("std");
const js = @import("../../../js/js.zig");
const ProfileAsync = @import("../ProfileAsync.zig");
const RuntimeMemory = @import("../RuntimeMemory.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const RemoteGate = @import("RemoteGate.zig");
const IDBObjectStore = @import("IDBObjectStore.zig");
const IDBRequest = @import("IDBRequest.zig");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const Execution = js.Execution;
const Blob = RemoteAsync.BlobRef;

const Entry = struct { id: i64, key: []const u8, unique: bool };
const Phase = enum { savepoint, generator, bump, key_upload, value_upload, record, delete_indexes, index_upload, index_add, index_drop, delete_record, release, result, rollback, cleanup };
const State = struct {
    allocator: std.mem.Allocator,
    arena: std.heap.ArenaAllocator,
    store: *IDBObjectStore,
    request: *IDBRequest,
    exec: *Execution,
    ctx: *anyopaque,
    done: IDBRequest.AsyncDone,
    kind: IDBObjectStore.WriteKind,
    prepared: IDBObjectStore.PreparedKey,
    clone: ?usize,
    indexes: []const Engine.IndexInfo = &.{},
    deleting: bool = false,
    phase: Phase = .savepoint,
    savepoint: bool = false,
    failure: ?anyerror = null,
    key: []const u8 = &.{},
    bytes: []const u8 = &.{},
    generated: i64 = 0,
    entries: std.ArrayList(Entry) = .empty,
    entry: usize = 0,
    key_blob: ?Blob = null,
    value_blob: ?Blob = null,
    index_blob: ?Blob = null,
    handle: ?*RemoteAsync.Handle = null,
    rpc: ?u64 = null,
    cancelled: bool = false,

    fn client(self: *State) *ProfileAsync.Client {
        return self.store._engine.profile_async.?;
    }
    fn origin(self: *State) []const u8 {
        return self.store._engine.remote.?.origin;
    }
    fn ticket(self: *State) ?[]const u8 {
        return self.store._txn.gateWaiter().ticket;
    }
    fn number(buf: *[32]u8, n: i64) []const u8 {
        return std.fmt.bufPrint(buf, "{d}", .{n}) catch unreachable;
    }
    fn send(self: *State, operation: []const u8, payload: anytype) !void {
        self.rpc = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), operation, payload, self, replied);
    }
    fn upload(self: *State, data: []const u8) !void {
        self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), data, self, uploaded);
    }
    fn drop(self: *State, ref: Blob) !void {
        self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), ref, self, dropped);
    }
    fn freeBlob(self: *State, ref: Blob) void {
        self.allocator.free(ref.blob_id);
        self.allocator.free(ref.size);
    }
    fn cancel(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.cancelled = true;
        if (self.phase == .cleanup or self.phase == .rollback or self.phase == .index_drop) return;
        if (self.handle) |handle| {
            handle.cancel();
        } else if (self.rpc) |id| {
            self.client().cancel(id);
        }
    }
    fn finish(self: *State) void {
        const done = self.done;
        const ctx = self.ctx;
        const failure = self.failure;
        self.request.clearAsyncCancel();
        if (self.index_blob) |ref| self.freeBlob(ref);
        if (self.value_blob) |ref| self.freeBlob(ref);
        if (self.key_blob) |ref| self.freeBlob(ref);
        if (self.clone) |clone| self.store._txn.releaseClone(clone);
        self.arena.deinit();
        const allocator = self.allocator;
        allocator.destroy(self);
        done(ctx, failure);
    }
    fn fail(self: *State, err: anyerror) void {
        if (self.failure == null) self.failure = err;
        if (self.phase == .rollback) {
            self.client().deinit();
            self.finish();
            return;
        }
        self.phase = .cleanup;
        self.advance();
    }
    fn advance(self: *State) void {
        self.step() catch |err| {
            if (self.phase == .cleanup or self.phase == .rollback) {
                if (self.failure == null) self.failure = err;
                self.client().deinit();
                self.finish();
            } else self.fail(err);
        };
    }
    fn step(self: *State) !void {
        var id: [32]u8 = undefined;
        if ((self.cancelled or self.store._txn._settled or self.store._txn._detached) and self.phase != .cleanup and self.phase != .rollback) {
            self.failure = error.AbortError;
            self.phase = .cleanup;
        }
        switch (self.phase) {
            .savepoint => try self.send("idb.savepoint", struct {}{}),
            .generator => try self.send("idb.nextGeneratedKey", .{ .store_id = number(&id, self.store._store_id) }),
            .bump => try self.send("idb.maybeBumpGenerator", .{ .store_id = number(&id, self.store._store_id), .key = @min(self.prepared.explicit.bump.?, 9007199254740992) }),
            .key_upload => try self.upload(self.key),
            .value_upload => try self.upload(self.bytes),
            .record => try self.send(if (self.kind == .add) "idb.add" else "idb.put", .{ .object_store_id = number(&id, self.store._store_id), .key = self.key_blob.?, .value = self.value_blob.? }),
            .delete_indexes => try self.send("idb.deleteIndexRecordsForKey", .{ .object_store_id = number(&id, self.store._store_id), .primary_key = self.key_blob.? }),
            .index_upload => try self.upload(self.entries.items[self.entry].key),
            .index_add => {
                const entry = self.entries.items[self.entry];
                try self.send("idb.addIndexRecord", .{ .index_id = number(&id, entry.id), .key = self.index_blob.?, .primary_key = self.key_blob.?, .unique = entry.unique });
            },
            .index_drop => try self.drop(self.index_blob.?),
            .delete_record => try self.send("idb.deleteRange", .{ .object_store_id = number(&id, self.store._store_id), .b = .{ .lower = self.key_blob.?, .upper = self.key_blob.?, .lower_open = false, .upper_open = false, .is_point = true } }),
            .result => unreachable,
            .release => try self.send("idb.releaseSavepoint", struct {}{}),
            .rollback => try self.send("idb.rollbackSavepoint", struct {}{}),
            .cleanup => {
                if (self.index_blob) |ref| return self.drop(ref);
                if (self.value_blob) |ref| return self.drop(ref);
                if (self.key_blob) |ref| return self.drop(ref);
                if (self.savepoint and !self.store._txn._settled and !self.store._txn._detached) {
                    self.phase = .rollback;
                    return self.step();
                }
                self.finish();
            },
        }
    }
    fn scheduleJs(self: *State) void {
        self.exec.js.scheduler.add(self, runJs, 0, .{ .name = "IDBObjectStore.write", .finalizer = finalizeJs }) catch |err| self.fail(err);
    }
    fn finalizeJs(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        // The scheduler is being reset. Only the transport-owned drop helper
        // may survive this finalizer; it must not retain the page or txn.
        const transport = self.client();
        const remote_origin = self.origin();
        for ([_]?Blob{ self.index_blob, self.value_blob, self.key_blob }) |maybe_ref| {
            if (maybe_ref) |ref| {
                _ = RemoteAsync.drop(self.allocator, transport, remote_origin, ref, transport, detachedDrop) catch {
                    transport.deinit();
                    break;
                };
            }
        }
        self.failure = error.AbortError;
        self.finish();
    }
    fn detachedDrop(ctx: *anyopaque, err: ?anyerror) void {
        if (err != null) {
            const transport: *ProfileAsync.Client = @ptrCast(@alignCast(ctx));
            transport.deinit();
        }
    }
    fn runJs(ctx: *anyopaque) !?u32 {
        const self: *State = @ptrCast(@alignCast(ctx));
        if (self.cancelled or self.store._txn._settled or self.store._txn._detached) {
            self.fail(error.AbortError);
            return null;
        }
        const exec = self.exec;
        const previous = exec.js.local;
        defer exec.js.local = previous;
        var scope: js.Local.Scope = undefined;
        exec.js.localScope(&scope);
        defer scope.deinit();
        exec.js.local = &scope.local;
        if (self.phase == .result) {
            const value = Key.decodeToJs(self.arena.allocator(), &scope.local, self.key) catch |err| {
                self.fail(err);
                return null;
            };
            self.request.setValue(value) catch |err| {
                self.fail(err);
                return null;
            };
            self.phase = .cleanup;
        } else {
            self.prepareJs() catch |err| {
                self.fail(err);
                return null;
            };
            self.phase = .key_upload;
        }
        self.advance();
        return null;
    }
    fn prepareJs(self: *State) !void {
        const arena = self.arena.allocator();
        const local = self.exec.js.local.?;
        var value: ?js.Value = null;
        if (!self.deleting) {
            self.bytes = self.store._txn.cloneBytes(self.clone.?);
            switch (self.prepared) {
                .explicit => |e| self.key = e.encoded,
                .generate_out_of_line, .generate_in_line => {
                    const k = try local.newNumber(@floatFromInt(self.generated));
                    self.key = try Key.encodeValue(arena, k);
                    if (std.meta.activeTag(self.prepared) == .generate_in_line) {
                        const v = try js.Value.deserialize(local, self.bytes);
                        try Key.injectKey(local, v, self.store._key_path.?.string, k);
                        const serialized = try v.serialize();
                        defer serialized.deinit();
                        self.bytes = try arena.dupe(u8, serialized.bytes());
                        value = v;
                    }
                },
            }
            const indexes = self.indexes;
            if (indexes.len != 0) {
                const v = value orelse try js.Value.deserialize(local, self.bytes);
                for (indexes) |index| {
                    const extracted = (try Key.extractKeyPath(local, v, index.key_path)) orelse continue;
                    if (index.multi_entry and extracted.isArray()) {
                        const start = self.entries.items.len;
                        const array = extracted.toArray();
                        for (0..array.len()) |i| {
                            const element = try array.get(@intCast(i));
                            const encoded = Key.encodeValue(arena, element) catch |err| switch (err) {
                                error.DataError => continue,
                                else => return err,
                            };
                            var duplicate = false;
                            for (self.entries.items[start..]) |entry| if (std.mem.eql(u8, entry.key, encoded)) {
                                duplicate = true;
                                break;
                            };
                            if (!duplicate) try self.entries.append(arena, .{ .id = index.id, .key = encoded, .unique = index.unique });
                        }
                    } else {
                        const encoded = Key.encodeValue(arena, extracted) catch |err| switch (err) {
                            error.DataError => continue,
                            else => return err,
                        };
                        try self.entries.append(arena, .{ .id = index.id, .key = encoded, .unique = index.unique });
                    }
                }
            }
        }
    }
    fn replied(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.rpc = null;
        const reply = switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .reply => |r| r,
        };
        defer reply.deinit();
        if (RemoteGate.responseError(reply.value)) |err| {
            self.fail(err);
            return;
        }
        const value = reply.value.object.get("result") orelse {
            self.fail(error.ProfileProtocolError);
            return;
        };
        if (self.phase == .generator) {
            if (value != .string) {
                self.fail(error.ProfileProtocolError);
                return;
            }
            self.generated = std.fmt.parseInt(i64, value.string, 10) catch {
                self.fail(error.ProfileProtocolError);
                return;
            };
            self.scheduleJs();
            return;
        }
        if (value != .null) {
            self.fail(error.ProfileProtocolError);
            return;
        }
        switch (self.phase) {
            .savepoint => {
                self.savepoint = true;
                if (self.deleting) {
                    self.phase = .key_upload;
                } else switch (self.prepared) {
                    .generate_in_line, .generate_out_of_line => self.phase = .generator,
                    .explicit => |e| if (e.bump != null and e.bump.? >= 1) {
                        self.phase = .bump;
                    } else {
                        self.scheduleJs();
                        return;
                    },
                }
            },
            .bump => {
                self.scheduleJs();
                return;
            },
            .record => self.phase = .delete_indexes,
            .delete_indexes => self.phase = if (self.deleting) .delete_record else if (self.entries.items.len == 0) .release else .index_upload,
            .index_add => self.phase = .index_drop,
            .delete_record => self.phase = .release,
            .release => {
                self.savepoint = false;
                if (!self.deleting) {
                    self.phase = .result;
                    self.scheduleJs();
                    return;
                }
                self.phase = .cleanup;
            },
            .rollback => {
                self.savepoint = false;
                self.phase = .cleanup;
            },
            else => {
                self.fail(error.ProfileProtocolError);
                return;
            },
        }
        self.advance();
    }
    fn uploaded(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        const ref = switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .value => |r| r,
        };
        switch (self.phase) {
            .key_upload => {
                self.key_blob = ref;
                self.phase = if (self.deleting) .delete_indexes else .value_upload;
            },
            .value_upload => {
                self.value_blob = ref;
                self.phase = .record;
            },
            .index_upload => {
                self.index_blob = ref;
                self.phase = .index_add;
            },
            else => {
                self.freeBlob(ref);
                self.fail(error.ProfileProtocolError);
                return;
            },
        }
        self.advance();
    }
    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (self.index_blob) |ref| {
            self.freeBlob(ref);
            self.index_blob = null;
        } else if (self.value_blob) |ref| {
            self.freeBlob(ref);
            self.value_blob = null;
        } else if (self.key_blob) |ref| {
            self.freeBlob(ref);
            self.key_blob = null;
        }
        if (err) |failure| {
            if (self.failure == null) self.failure = failure;
            self.client().deinit();
            self.finish();
            return;
        }
        if (self.phase == .index_drop) {
            self.entry += 1;
            self.phase = if (self.entry == self.entries.items.len) .release else .index_upload;
        }
        self.advance();
    }
};

pub fn run(store: *IDBObjectStore, request: *IDBRequest, kind: IDBObjectStore.WriteKind, clone: usize, prepared: IDBObjectStore.PreparedKey, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone, indexes: []const Engine.IndexInfo) !IDBRequest.AsyncStatus {
    const allocator = RuntimeMemory.allocator(store._engine.remote.?.allocator) catch |err| {
        store._txn.releaseClone(clone);
        return err;
    };
    const self = allocator.create(State) catch |err| {
        store._txn.releaseClone(clone);
        return err;
    };
    self.* = .{ .allocator = allocator, .arena = std.heap.ArenaAllocator.init(allocator), .store = store, .request = request, .exec = exec, .ctx = ctx, .done = done, .kind = kind, .prepared = prepared, .clone = clone, .indexes = indexes };
    self.step() catch |err| {
        self.arena.deinit();
        allocator.destroy(self);
        store._txn.releaseClone(clone);
        return err;
    };
    request.setAsyncCancel(self, State.cancel);
    return .pending;
}

pub fn deleteAt(store: *IDBObjectStore, request: *IDBRequest, key: []const u8, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    const allocator = try RuntimeMemory.allocator(store._engine.remote.?.allocator);
    const self = try allocator.create(State);
    self.* = .{ .allocator = allocator, .arena = std.heap.ArenaAllocator.init(allocator), .store = store, .request = request, .exec = exec, .ctx = ctx, .done = done, .kind = .put, .prepared = .{ .explicit = .{ .encoded = key, .bump = null } }, .clone = null, .deleting = true, .key = key };
    self.step() catch |err| {
        self.arena.deinit();
        allocator.destroy(self);
        return err;
    };
    request.setAsyncCancel(self, State.cancel);
    return .pending;
}
