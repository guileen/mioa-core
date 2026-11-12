// Backfill a newly created index on the owning versionchange transaction.
const std = @import("std");
const js = @import("../../../js/js.zig");
const ProfileAsync = @import("../ProfileAsync.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const Gate = @import("RemoteGate.zig");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const IDBTransaction = @import("IDBTransaction.zig");
const IDBRequest = @import("IDBRequest.zig");
const Blob = RemoteAsync.BlobRef;

const Phase = enum { lower, upper, open, next, primary, key, insert, key_drop, primary_drop, close, cleanup };
const State = struct {
    allocator: std.mem.Allocator,
    arena: std.heap.ArenaAllocator,
    transport: *ProfileAsync.Client,
    owned_origin: []u8,
    txn: *IDBTransaction,
    request: *IDBRequest,
    store_id: i64,
    index: Engine.IndexInfo,
    exec: *js.Execution,
    ctx: *anyopaque,
    done: IDBRequest.AsyncDone,
    phase: Phase = .lower,
    failure: ?anyerror = null,
    lower: ?Blob = null,
    upper: ?Blob = null,
    primary: ?Blob = null,
    key: ?Blob = null,
    rows_id: ?[]u8 = null,
    row: ?[][]u8 = null,
    keys: std.ArrayList([]const u8) = .empty,
    position: usize = 0,
    handle: ?*RemoteAsync.Handle = null,
    rpc: ?u64 = null,
    is_cancelled: bool = false,
    detached: bool = false,
    scheduled: bool = false,

    fn client(self: *State) *ProfileAsync.Client {
        return self.transport;
    }
    fn origin(self: *State) []const u8 {
        return self.owned_origin;
    }
    fn ticket(self: *State) ?[]const u8 {
        return self.txn.gateWaiter().ticket;
    }
    fn cancel(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.is_cancelled = true;
        if (self.failure == null) self.failure = error.AbortError;
        if (self.txn._detached) {
            self.detached = true;
            self.request.clearAsyncCancel();
            const transport = self.transport;
            const pending = self.handle != null or self.rpc != null;
            const scheduled = self.scheduled;
            self.done(self.ctx, error.AbortError);
            transport.deinit();
            if (!pending and !scheduled) self.finish();
            return;
        }
        if (self.phase == .cleanup or self.phase == .close or self.phase == .key_drop or self.phase == .primary_drop) return;
        if (self.handle) |handle| handle.cancel() else if (self.rpc) |id| self.client().cancel(id);
    }
    fn freeBlob(self: *State, ref: Blob) void {
        self.allocator.free(ref.blob_id);
        self.allocator.free(ref.size);
    }
    fn freeRow(self: *State) void {
        if (self.row) |row| {
            for (row) |v| self.allocator.free(v);
            self.allocator.free(row);
            self.row = null;
        }
        self.arena.deinit();
        self.arena = std.heap.ArenaAllocator.init(self.allocator);
        self.keys = .empty;
        self.position = 0;
    }
    fn finish(self: *State) void {
        const callback = self.done;
        const ctx = self.ctx;
        const err = self.failure;
        const allocator = self.allocator;
        const detached = self.detached;
        if (!detached) self.request.clearAsyncCancel();
        self.freeRow();
        self.arena.deinit();
        if (self.rows_id) |id| allocator.free(id);
        if (self.lower) |v| self.freeBlob(v);
        if (self.upper) |v| self.freeBlob(v);
        if (self.primary) |v| self.freeBlob(v);
        if (self.key) |v| self.freeBlob(v);
        allocator.free(self.owned_origin);
        allocator.destroy(self);
        if (!detached) callback(ctx, err);
    }
    fn fail(self: *State, err: anyerror) void {
        if (self.failure == null) self.failure = switch (err) {
            error.Constraint => error.ConstraintError,
            else => err,
        };
        if (self.detached) {
            self.finish();
            return;
        }
        if (err == error.ProfileSideEffectUnknown) {
            self.client().deinit();
            self.finish();
            return;
        }
        self.phase = .cleanup;
        self.advance();
    }
    fn advance(self: *State) void {
        self.step() catch |err| {
            if (self.phase == .cleanup or self.phase == .close) {
                if (self.failure == null) self.failure = err;
                self.client().deinit();
                self.finish();
            } else self.fail(err);
        };
    }
    fn step(self: *State) !void {
        if ((self.is_cancelled or self.txn._settled) and self.phase != .cleanup and self.phase != .close) {
            if (self.failure == null) self.failure = error.AbortError;
            self.phase = .cleanup;
        }
        const bounds = Engine.Bounds.unbounded();
        var number: [32]u8 = undefined;
        switch (self.phase) {
            .lower => self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), bounds.lower, self, uploaded),
            .upper => self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), bounds.upper, self, uploaded),
            .open => {
                const id = try std.fmt.bufPrint(&number, "{d}", .{self.store_id});
                self.rpc = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.getAllRows", .{ .object_store_id = id, .b = .{ .lower = self.lower.?, .upper = self.upper.?, .lower_open = false, .upper_open = false, .is_point = false }, .reverse = false, .limit_ = @as(?u32, null) }, self, replied);
            },
            .next => self.handle = try RemoteAsync.rowsNext(self.allocator, self.client(), self.origin(), self.ticket(), self.rows_id.?, self, rowDone),
            .primary => self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.row.?[0], self, uploaded),
            .key => self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.keys.items[self.position], self, uploaded),
            .insert => {
                const id = try std.fmt.bufPrint(&number, "{d}", .{self.index.id});
                self.rpc = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.addIndexRecord", .{ .index_id = id, .key = self.key.?, .primary_key = self.primary.?, .unique = self.index.unique }, self, replied);
            },
            .key_drop => self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), self.key.?, self, dropped),
            .primary_drop => self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), self.primary.?, self, dropped),
            .close => self.rpc = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.rows_close", .{ .rows_id = self.rows_id.? }, self, replied),
            .cleanup => {
                if (self.rows_id != null) {
                    self.phase = .close;
                    return self.step();
                }
                if (self.key) |v| {
                    self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), v, self, dropped);
                    return;
                }
                if (self.primary) |v| {
                    self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), v, self, dropped);
                    return;
                }
                if (self.lower) |v| {
                    self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), v, self, dropped);
                    return;
                }
                if (self.upper) |v| {
                    self.handle = try RemoteAsync.drop(self.allocator, self.client(), self.origin(), v, self, dropped);
                    return;
                }
                self.finish();
            },
        }
    }
    fn uploaded(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (self.detached) {
            if (result == .value) self.freeBlob(result.value);
            self.finish();
            return;
        }
        const ref = switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .value => |v| v,
        };
        switch (self.phase) {
            .lower => {
                self.lower = ref;
                self.phase = .upper;
            },
            .upper => {
                self.upper = ref;
                self.phase = .open;
            },
            .primary => {
                self.primary = ref;
                self.phase = .key;
            },
            .key => {
                self.key = ref;
                self.phase = .insert;
            },
            else => {
                self.freeBlob(ref);
                self.fail(error.ProfileProtocolError);
                return;
            },
        }
        self.advance();
    }
    fn replied(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.rpc = null;
        if (self.detached) {
            if (result == .reply) result.reply.deinit();
            self.finish();
            return;
        }
        const reply = switch (result) {
            .failure => |err| {
                if (self.phase == .close) {
                    self.client().deinit();
                    if (self.failure == null) self.failure = err;
                    self.finish();
                } else self.fail(err);
                return;
            },
            .reply => |v| v,
        };
        defer reply.deinit();
        if (Gate.responseError(reply.value)) |err| {
            if (self.phase == .close) {
                self.client().deinit();
                if (self.failure == null) self.failure = err;
                self.finish();
            } else self.fail(err);
            return;
        }
        const value = reply.value.object.get("result") orelse {
            if (self.phase == .close or self.phase == .open) {
                self.client().deinit();
                if (self.failure == null) self.failure = error.ProfileProtocolError;
                self.finish();
            } else self.fail(error.ProfileProtocolError);
            return;
        };
        switch (self.phase) {
            .open => {
                if (value != .object) {
                    self.client().deinit();
                    if (self.failure == null) self.failure = error.ProfileProtocolError;
                    self.finish();
                    return;
                }
                const id = value.object.get("rows_id") orelse {
                    self.client().deinit();
                    if (self.failure == null) self.failure = error.ProfileProtocolError;
                    self.finish();
                    return;
                };
                if (id != .string) {
                    self.client().deinit();
                    if (self.failure == null) self.failure = error.ProfileProtocolError;
                    self.finish();
                    return;
                }
                self.rows_id = self.allocator.dupe(u8, id.string) catch {
                    self.client().deinit();
                    if (self.failure == null) self.failure = error.OutOfMemory;
                    self.finish();
                    return;
                };
                self.phase = .next;
            },
            .insert => {
                if (value != .null) {
                    self.fail(error.ProfileProtocolError);
                    return;
                }
                self.phase = .key_drop;
            },
            .close => {
                if (value != .null) {
                    self.client().deinit();
                    if (self.failure == null) self.failure = error.ProfileProtocolError;
                    self.finish();
                    return;
                }
                self.allocator.free(self.rows_id.?);
                self.rows_id = null;
                self.phase = .cleanup;
            },
            else => {
                self.fail(error.ProfileProtocolError);
                return;
            },
        }
        self.advance();
    }
    fn rowDone(ctx: *anyopaque, result: RemoteAsync.RowResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (self.detached) {
            if (result == .value) if (result.value) |row| {
                for (row) |v| self.allocator.free(v);
                self.allocator.free(row);
            };
            self.finish();
            return;
        }
        const row = switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .value => |v| v orelse {
                self.phase = .cleanup;
                self.advance();
                return;
            },
        };
        self.row = row;
        if (row.len != 2) {
            self.fail(error.ProfileProtocolError);
            return;
        }
        self.scheduled = true;
        self.exec.js.scheduler.add(self, runJs, 0, .{ .name = "IDBIndex.backfill", .finalizer = cancelled }) catch |err| {
            self.scheduled = false;
            self.fail(err);
        };
    }
    fn cancelled(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.scheduled = false;
        self.fail(error.AbortError);
    }
    fn runJs(ctx: *anyopaque) !?u32 {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.scheduled = false;
        if (self.detached) {
            self.finish();
            return null;
        }
        if (self.is_cancelled or self.txn._settled) {
            self.fail(error.AbortError);
            return null;
        }
        const extraction: ?anyerror = blk: {
            const old = self.exec.js.local;
            var scope: js.Local.Scope = undefined;
            self.exec.js.localScope(&scope);
            defer scope.deinit();
            self.exec.js.local = &scope.local;
            defer self.exec.js.local = old;
            self.extract() catch |err| break :blk err;
            break :blk null;
        };
        if (extraction) |err| {
            self.fail(err);
            return null;
        }
        if (self.keys.items.len == 0) {
            self.freeRow();
            self.phase = .next;
        } else self.phase = .primary;
        self.advance();
        return null;
    }
    fn extract(self: *State) !void {
        const local = self.exec.js.local.?;
        const value = try js.Value.deserialize(local, self.row.?[1]);
        const extracted = (try Key.extractKeyPath(local, value, self.index.key_path)) orelse return;
        const arena = self.arena.allocator();
        if (self.index.multi_entry and extracted.isArray()) {
            const array = extracted.toArray();
            for (0..array.len()) |i| {
                const element = try array.get(@intCast(i));
                const encoded = Key.encodeValue(arena, element) catch |err| switch (err) {
                    error.OutOfMemory => return err,
                    else => continue,
                };
                var duplicate = false;
                for (self.keys.items) |key| if (std.mem.eql(u8, key, encoded)) {
                    duplicate = true;
                    break;
                };
                if (!duplicate) try self.keys.append(arena, encoded);
            }
        } else {
            const encoded = Key.encodeValue(arena, extracted) catch |err| switch (err) {
                error.OutOfMemory => return err,
                else => return,
            };
            try self.keys.append(arena, encoded);
        }
    }
    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (self.detached) {
            self.finish();
            return;
        }
        if (self.key) |ref| {
            self.freeBlob(ref);
            self.key = null;
        } else if (self.primary) |ref| {
            self.freeBlob(ref);
            self.primary = null;
        } else if (self.lower) |ref| {
            self.freeBlob(ref);
            self.lower = null;
        } else if (self.upper) |ref| {
            self.freeBlob(ref);
            self.upper = null;
        }
        if (err) |failure| {
            if (self.failure == null) self.failure = failure;
            self.client().deinit();
            self.finish();
            return;
        }
        switch (self.phase) {
            .key_drop => {
                self.position += 1;
                self.phase = if (self.position == self.keys.items.len) .primary_drop else .key;
            },
            .primary_drop => {
                self.freeRow();
                self.phase = .next;
            },
            else => {},
        }
        self.advance();
    }
};

pub fn start(txn: *IDBTransaction, store_id: i64, index: Engine.IndexInfo, request: *IDBRequest, exec: *js.Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    const allocator = txn._engine.remote.?.allocator;
    const self = try allocator.create(State);
    errdefer allocator.destroy(self);
    const origin = try allocator.dupe(u8, txn._engine.remote.?.origin);
    errdefer allocator.free(origin);
    self.* = .{ .allocator = allocator, .arena = std.heap.ArenaAllocator.init(allocator), .transport = txn._engine.profile_async.?, .owned_origin = origin, .txn = txn, .request = request, .store_id = store_id, .index = index, .exec = exec, .ctx = ctx, .done = done };
    errdefer self.arena.deinit();
    try self.step();
    request.setAsyncCancel(self, State.cancel);
    return .pending;
}
