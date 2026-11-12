// Asynchronous object-store reads and range mutations over the Profile channel.
const std = @import("std");
const js = @import("../../../js/js.zig");
const ProfileAsync = @import("../ProfileAsync.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const Gate = @import("RemoteGate.zig");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const IDBRecord = @import("IDBRecord.zig");
const IDBObjectStore = @import("IDBObjectStore.zig");
const IDBRequest = @import("IDBRequest.zig");
const IDBKeyRange = @import("IDBKeyRange.zig");
const Allocator = std.mem.Allocator;
const Blob = RemoteAsync.BlobRef;
const Execution = js.Execution;

pub const Kind = enum { get, get_key, count, delete, clear, all };

const State = struct {
    allocator: Allocator,
    client_ptr: *ProfileAsync.Client,
    owned_origin: []u8,
    owned_ticket: []u8,
    store_id: i64,
    store: *IDBObjectStore,
    request: *IDBRequest,
    exec: *Execution,
    ctx: *anyopaque,
    done: IDBRequest.AsyncDone,
    kind: Kind,
    bounds: Engine.Bounds,
    mode: IDBObjectStore.GetAllMode,
    reverse: bool,
    limit: ?u32,
    lower: ?Blob = null,
    upper: ?Blob = null,
    returned: ?Blob = null,
    bytes: ?[]u8 = null,
    count: i64 = 0,
    rows_id: ?[]u8 = null,
    rows: std.ArrayList([][]u8) = .empty,
    err: ?anyerror = null,
    phase: enum { first, second } = .first,
    handle: ?*RemoteAsync.Handle = null,
    request_id: ?u64 = null,
    canceled: bool = false,
    detached: bool = false,

    fn client(self: *State) *ProfileAsync.Client {
        return self.client_ptr;
    }
    fn origin(self: *State) []const u8 {
        return self.owned_origin;
    }
    fn ticket(self: *State) ?[]const u8 {
        return self.owned_ticket;
    }
    fn start(self: *State) !void {
        if (self.kind == .clear) return self.command();
        self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.bounds.lower, self, lowerDone);
    }
    fn lowerDone(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        self.lower = switch (result) {
            .value => |v| v,
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        if (self.canceled) {
            self.finish(error.AbortError);
            return;
        }
        self.handle = RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.bounds.upper, self, upperDone) catch |err| {
            self.finish(err);
            return;
        };
    }
    fn upperDone(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        self.upper = switch (result) {
            .value => |v| v,
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        if (self.canceled) {
            self.finish(error.AbortError);
            return;
        }
        self.command() catch |err| self.finish(err);
    }
    fn command(self: *State) !void {
        var buf: [32]u8 = undefined;
        const id = std.fmt.bufPrint(&buf, "{d}", .{self.store_id}) catch unreachable;
        if (self.kind == .clear) {
            const operation: []const u8 = if (self.phase == .first) "idb.clearIndexRecordsForStore" else "idb.clear";
            self.request_id = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), operation, .{ .object_store_id = id }, self, commandDone);
            return;
        }
        const b = .{ .lower = self.lower.?, .upper = self.upper.?, .lower_open = !self.bounds.is_point and std.mem.eql(u8, self.bounds.lower_op, "> "), .upper_open = !self.bounds.is_point and std.mem.eql(u8, self.bounds.upper_op, "< "), .is_point = self.bounds.is_point };
        if (self.kind == .all) {
            self.request_id = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.getAllRows", .{ .object_store_id = id, .b = b, .reverse = self.reverse, .limit_ = self.limit }, self, commandDone);
            return;
        }
        const operation: []const u8 = switch (self.kind) {
            .get => "idb.getRange",
            .get_key => "idb.getKeyRange",
            .count => "idb.countRange",
            .delete => if (self.phase == .first) "idb.deleteIndexRecordsForRange" else "idb.deleteRange",
            else => unreachable,
        };
        self.request_id = try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), operation, .{ .object_store_id = id, .b = b }, self, commandDone);
    }
    fn commandDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        const reply = switch (result) {
            .reply => |v| v,
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        defer reply.deinit();
        if (Gate.responseError(reply.value)) |err| {
            self.finish(err);
            return;
        }
        const value = reply.value.object.get("result") orelse {
            self.finish(error.ProfileProtocolError);
            return;
        };
        if (self.canceled) {
            self.finish(error.AbortError);
            return;
        }
        switch (self.kind) {
            .clear, .delete => {
                if (value != .null) {
                    self.finish(error.ProfileProtocolError);
                    return;
                }
                if (self.phase == .first) {
                    self.phase = .second;
                    self.command() catch |err| self.finish(err);
                } else self.finish(null);
            },
            .count => {
                if (value != .string) {
                    self.finish(error.ProfileProtocolError);
                    return;
                }
                self.count = std.fmt.parseInt(i64, value.string, 10) catch {
                    self.finish(error.ProfileProtocolError);
                    return;
                };
                self.finish(null);
            },
            .get, .get_key => {
                if (value == .null) {
                    self.finish(null);
                    return;
                }
                const parsed = std.json.parseFromValue(Blob, self.allocator, value, .{}) catch |err| {
                    self.client().deinit();
                    self.finish(if (err == error.OutOfMemory) err else error.ProfileProtocolError);
                    return;
                };
                defer parsed.deinit();
                const id = self.allocator.dupe(u8, parsed.value.blob_id) catch {
                    self.client().deinit();
                    self.finish(error.OutOfMemory);
                    return;
                };
                const size = self.allocator.dupe(u8, parsed.value.size) catch {
                    self.allocator.free(id);
                    self.client().deinit();
                    self.finish(error.OutOfMemory);
                    return;
                };
                self.returned = .{ .blob_id = id, .size = size };
                self.handle = RemoteAsync.download(self.allocator, self.client(), self.origin(), self.returned.?, self, downloaded) catch |err| {
                    self.finish(err);
                    return;
                };
            },
            .all => {
                if (value != .object) {
                    self.finish(error.ProfileProtocolError);
                    return;
                }
                const id = value.object.get("rows_id") orelse {
                    self.finish(error.ProfileProtocolError);
                    return;
                };
                if (id != .string) {
                    self.finish(error.ProfileProtocolError);
                    return;
                }
                self.rows_id = self.allocator.dupe(u8, id.string) catch {
                    self.err = error.OutOfMemory;
                    self.request_id = RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.rows_close", .{ .rows_id = id.string }, self, rowsClosed) catch {
                        self.client().deinit();
                        self.cleanupBlobs();
                        return;
                    };
                    return;
                };
                self.nextRow();
            },
        }
    }
    fn downloaded(ctx: *anyopaque, result: RemoteAsync.BytesResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        self.bytes = switch (result) {
            .value => |v| v,
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        self.finish(null);
    }
    fn nextRow(self: *State) void {
        self.handle = RemoteAsync.rowsNext(self.allocator, self.client(), self.origin(), self.ticket(), self.rows_id.?, self, rowDone) catch |err| {
            self.finish(err);
            return;
        };
    }
    fn rowDone(ctx: *anyopaque, result: RemoteAsync.RowResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        const row = switch (result) {
            .value => |v| v orelse {
                self.finish(null);
                return;
            },
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        if (row.len != 2) {
            for (row) |v| self.allocator.free(v);
            self.allocator.free(row);
            self.finish(error.ProfileProtocolError);
            return;
        }
        self.rows.append(self.allocator, row) catch |err| {
            for (row) |v| self.allocator.free(v);
            self.allocator.free(row);
            self.finish(err);
            return;
        };
        if (self.canceled) {
            self.finish(error.AbortError);
            return;
        }
        self.nextRow();
    }
    fn finish(self: *State, err: ?anyerror) void {
        if (self.err == null) self.err = err;
        if (self.rows_id) |id| {
            self.rows_id = null;
            self.request_id = RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.rows_close", .{ .rows_id = id }, self, rowsClosed) catch |close_err| {
                if (self.err == null) self.err = close_err;
                self.allocator.free(id);
                self.client().deinit();
                self.cleanupBlobs();
                return;
            };
            self.allocator.free(id);
            return;
        }
        self.cleanupBlobs();
    }
    fn rowsClosed(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        switch (result) {
            .reply => |reply| {
                defer reply.deinit();
                if (Gate.responseError(reply.value)) |err| {
                    if (self.err == null) self.err = err;
                }
            },
            .failure => |err| {
                if (self.err == null) self.err = err;
            },
        }
        self.cleanupBlobs();
    }
    fn cleanupBlobs(self: *State) void {
        const slot: ?*?Blob = if (self.lower != null) &self.lower else if (self.upper != null) &self.upper else if (self.returned != null) &self.returned else null;
        if (slot) |s| {
            const ref = s.*.?;
            s.* = null;
            self.handle = RemoteAsync.drop(self.allocator, self.client(), self.origin(), ref, self, blobDropped) catch |err| {
                if (self.err == null) self.err = err;
                self.client().deinit();
                self.allocator.free(ref.blob_id);
                self.allocator.free(ref.size);
                self.cleanupBlobs();
                return;
            };
            self.allocator.free(ref.blob_id);
            self.allocator.free(ref.size);
            return;
        }
        if (self.err != null or self.kind == .delete or self.kind == .clear) {
            self.complete();
            return;
        }
        self.exec.js.scheduler.add(self, deliver, 0, .{ .name = "IDBObjectStore.remote", .finalizer = cancelled }) catch |err| {
            self.err = err;
            self.complete();
        };
    }
    fn blobDropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (self.err == null) self.err = err;
        self.cleanupBlobs();
    }
    fn cancelled(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.err = error.AbortError;
        self.complete();
    }
    fn deliver(ctx: *anyopaque) !?u32 {
        const self: *State = @ptrCast(@alignCast(ctx));
        if (self.canceled) {
            self.complete();
            return null;
        }
        {
            const exec = self.exec;
            const old = exec.js.local;
            var scope: js.Local.Scope = undefined;
            exec.js.localScope(&scope);
            defer scope.deinit();
            exec.js.local = &scope.local;
            defer exec.js.local = old;
            if (!self.canceled) self.deliverValue() catch |err| {
                self.err = err;
            };
        }
        self.complete();
        return null;
    }
    fn deliverValue(self: *State) !void {
        const local = self.exec.js.local.?;
        switch (self.kind) {
            .get => if (self.bytes) |b| try self.request.setValue(try js.Value.deserialize(local, b)),
            .get_key => if (self.bytes) |b| try self.request.setValue(try Key.decodeToJs(self.exec.call_arena, local, b)),
            .count => try self.request.setValue(try local.zigValueToJs(self.count, .{})),
            .all => {
                const arr = local.newArray(0);
                for (self.rows.items, 0..) |row, i| {
                    const out: js.Value = switch (self.mode) {
                        .value => try js.Value.deserialize(local, row[1]),
                        .key => try Key.decodeToJs(self.exec.call_arena, local, row[0]),
                        .record => try IDBRecord.initValue(self.store._txn, local, row[0], row[0], row[1]),
                    };
                    _ = try arr.set(@intCast(i), out, .{});
                }
                try self.request.setValue(arr.toValue());
            },
            .delete, .clear => unreachable,
        }
    }
    fn cancel(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.canceled = true;
        self.detached = true;
        self.err = error.AbortError;
        const callback = self.done;
        const target = self.ctx;
        const handle = self.handle;
        const id = self.request_id;
        const client_ptr = self.client_ptr;
        // Cancellation may synchronously complete and destroy the state.
        if (handle) |h| h.cancel() else if (id) |n| client_ptr.cancel(n);
        callback(target, error.AbortError);
    }
    fn complete(self: *State) void {
        const detached = self.detached;
        if (!detached) self.request.clearAsyncCancel();
        const callback = self.done;
        const ctx = self.ctx;
        const err = self.err;
        const allocator = self.allocator;
        if (self.bytes) |b| allocator.free(b);
        for (self.rows.items) |row| {
            for (row) |v| allocator.free(v);
            allocator.free(row);
        }
        self.rows.deinit(allocator);
        allocator.free(self.owned_origin);
        allocator.free(self.owned_ticket);
        allocator.destroy(self);
        if (!detached) callback(ctx, err);
    }
};

pub fn start(store: *IDBObjectStore, request: *IDBRequest, kind: Kind, bounds: Engine.Bounds, mode: IDBObjectStore.GetAllMode, reverse: bool, limit: ?u32, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    const allocator = store._engine.remote.?.allocator;
    const self = try allocator.create(State);
    errdefer allocator.destroy(self);
    const origin = try allocator.dupe(u8, store._engine.remote.?.origin);
    errdefer allocator.free(origin);
    const ticket = try allocator.dupe(u8, store._txn.gateWaiter().ticket orelse return error.InvalidState);
    errdefer allocator.free(ticket);
    self.* = .{ .allocator = allocator, .client_ptr = store._engine.profile_async.?, .owned_origin = origin, .owned_ticket = ticket, .store_id = store._store_id, .store = store, .request = request, .exec = exec, .ctx = ctx, .done = done, .kind = kind, .bounds = bounds, .mode = mode, .reverse = reverse, .limit = limit };
    try self.start();
    request.setAsyncCancel(self, State.cancel);
    return .pending;
}
