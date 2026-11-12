// Asynchronous index reads over the bounded Profile IndexedDB codec.
const std = @import("std");
const js = @import("../../../js/js.zig");
const ProfileAsync = @import("../ProfileAsync.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const Gate = @import("RemoteGate.zig");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const IDBRecord = @import("IDBRecord.zig");
const IDBIndex = @import("IDBIndex.zig");
const IDBObjectStore = @import("IDBObjectStore.zig");
const IDBRequest = @import("IDBRequest.zig");
const Allocator = std.mem.Allocator;
const Blob = RemoteAsync.BlobRef;
const Execution = js.Execution;

pub const Kind = enum { get, get_key, count, all };

const State = struct {
    allocator: Allocator,
    remote_client: *ProfileAsync.Client,
    remote_origin: []u8,
    remote_ticket: ?[]u8,
    index: *IDBIndex,
    request: *IDBRequest,
    exec: *Execution,
    ctx: *anyopaque,
    done: IDBRequest.AsyncDone,
    kind: Kind,
    bounds: Engine.Bounds,
    mode: IDBObjectStore.GetAllMode,
    reverse: bool,
    unique: bool,
    limit: ?u32,
    lower: ?Blob = null,
    upper: ?Blob = null,
    returned: ?Blob = null,
    bytes: ?[]u8 = null,
    count: i64 = 0,
    rows_id: ?[]u8 = null,
    rows: std.ArrayList([][]u8) = .empty,
    err: ?anyerror = null,
    handle: ?*RemoteAsync.Handle = null,
    command_id: ?u64 = null,
    cleaning: bool = false,
    cancelled: bool = false,

    fn client(self: *State) *ProfileAsync.Client {
        return self.remote_client;
    }
    fn origin(self: *State) []const u8 {
        return self.remote_origin;
    }
    fn ticket(self: *State) ?[]const u8 {
        return self.remote_ticket;
    }
    fn start(self: *State) !void {
        self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.bounds.lower, self, lowerDone);
    }
    fn cancel(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.cancelled = true;
        if (self.err == null) self.err = error.AbortError;
        const callback = self.done;
        const target = self.ctx;
        // Cancel may synchronously finish cleanup and destroy the state.
        if (!self.cleaning) {
            if (self.handle) |handle| {
                handle.cancel();
            } else if (self.command_id) |id| {
                self.client().cancel(id);
            }
        }
        callback(target, error.AbortError);
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
        if (self.cancelled) {
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
        if (self.cancelled) {
            self.finish(error.AbortError);
            return;
        }
        self.command() catch |err| self.finish(err);
    }
    fn command(self: *State) !void {
        var store_buf: [32]u8 = undefined;
        var index_buf: [32]u8 = undefined;
        const store_id = std.fmt.bufPrint(&store_buf, "{d}", .{self.index._store._store_id}) catch unreachable;
        const index_id = std.fmt.bufPrint(&index_buf, "{d}", .{self.index._index_id}) catch unreachable;
        const b = .{ .lower = self.lower.?, .upper = self.upper.?, .lower_open = !self.bounds.is_point and std.mem.eql(u8, self.bounds.lower_op, "> "), .upper_open = !self.bounds.is_point and std.mem.eql(u8, self.bounds.upper_op, "< "), .is_point = self.bounds.is_point };
        self.command_id = switch (self.kind) {
            .get => try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.indexGetRange", .{ .object_store_id = store_id, .index_id = index_id, .b = b }, self, commandDone),
            .get_key => try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.indexGetKeyRange", .{ .index_id = index_id, .b = b }, self, commandDone),
            .count => try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.indexCountRange", .{ .index_id = index_id, .b = b }, self, commandDone),
            .all => try RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.indexGetAllRows", .{ .object_store_id = store_id, .index_id = index_id, .b = b, .reverse = self.reverse, .unique = self.unique, .limit_ = self.limit }, self, commandDone),
        };
    }
    fn commandDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.command_id = null;
        const reply = switch (result) {
            .reply => |v| v,
            .failure => |err| {
                if (err == error.ProfileSideEffectUnknown) self.client().deinit();
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
        switch (self.kind) {
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
                const parsed = std.json.parseFromValue(Blob, self.allocator, value, .{}) catch {
                    self.client().deinit();
                    self.finish(error.ProfileProtocolError);
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
                if (self.cancelled) {
                    self.finish(error.AbortError);
                    return;
                }
                self.handle = RemoteAsync.download(self.allocator, self.client(), self.origin(), self.returned.?, self, downloaded) catch |err| {
                    self.finish(err);
                    return;
                };
            },
            .all => {
                if (value != .object) {
                    self.client().deinit();
                    self.finish(error.ProfileProtocolError);
                    return;
                }
                const id = value.object.get("rows_id") orelse {
                    self.client().deinit();
                    self.finish(error.ProfileProtocolError);
                    return;
                };
                if (id != .string) {
                    self.client().deinit();
                    self.finish(error.ProfileProtocolError);
                    return;
                }
                self.rows_id = self.allocator.dupe(u8, id.string) catch {
                    self.client().deinit();
                    self.finish(error.OutOfMemory);
                    return;
                };
                if (self.cancelled) {
                    self.finish(error.AbortError);
                    return;
                }
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
        if (row.len != 3 or self.cancelled) {
            for (row) |v| self.allocator.free(v);
            self.allocator.free(row);
            self.finish(if (self.cancelled) error.AbortError else error.ProfileProtocolError);
            return;
        }
        self.rows.append(self.allocator, row) catch |err| {
            for (row) |v| self.allocator.free(v);
            self.allocator.free(row);
            self.finish(err);
            return;
        };
        self.nextRow();
    }
    fn finish(self: *State, err: ?anyerror) void {
        if (self.err == null) self.err = err;
        self.cleaning = true;
        if (self.rows_id) |id| {
            self.rows_id = null;
            self.command_id = RemoteAsync.request(self.allocator, self.client(), self.origin(), self.ticket(), "idb.rows_close", .{ .rows_id = id }, self, rowsClosed) catch |e| {
                self.allocator.free(id);
                if (self.err == null) self.err = e;
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
        self.command_id = null;
        switch (result) {
            .reply => |reply| {
                defer reply.deinit();
                if (Gate.responseError(reply.value)) |err| {
                    if (self.err == null) self.err = err;
                    self.client().deinit();
                } else if (reply.value.object.get("result") == null or reply.value.object.get("result").? != .null) {
                    if (self.err == null) self.err = error.ProfileProtocolError;
                    self.client().deinit();
                }
            },
            .failure => |err| {
                if (self.err == null) self.err = err;
                self.client().deinit();
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
                self.allocator.free(ref.blob_id);
                self.allocator.free(ref.size);
                self.client().deinit();
                self.cleanupBlobs();
                return;
            };
            self.allocator.free(ref.blob_id);
            self.allocator.free(ref.size);
            return;
        }
        if (self.err != null) {
            self.complete();
            return;
        }
        self.exec.js.scheduler.add(self, deliver, 0, .{ .name = "IDBIndex.remote", .finalizer = schedulerCancelled }) catch |err| {
            self.err = err;
            self.complete();
        };
    }
    fn blobDropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (err) |e| {
            if (self.err == null) self.err = e;
            self.client().deinit();
        }
        self.cleanupBlobs();
    }
    fn schedulerCancelled(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.err = error.AbortError;
        self.complete();
    }
    fn deliver(ctx: *anyopaque) !?u32 {
        const self: *State = @ptrCast(@alignCast(ctx));
        if (self.cancelled) {
            self.complete();
            return null;
        }
        const exec = self.exec;
        const old = exec.js.local;
        var scope: js.Local.Scope = undefined;
        exec.js.localScope(&scope);
        defer scope.deinit();
        exec.js.local = &scope.local;
        self.deliverValue() catch |err| {
            self.err = err;
        };
        exec.js.local = old;
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
                        .value => try js.Value.deserialize(local, row[2]),
                        .key => try Key.decodeToJs(self.exec.call_arena, local, row[1]),
                        .record => try IDBRecord.initValue(self.index._store._txn, local, row[0], row[1], row[2]),
                    };
                    _ = try arr.set(@intCast(i), out, .{});
                }
                try self.request.setValue(arr.toValue());
            },
        }
    }
    fn complete(self: *State) void {
        const callback = self.done;
        const ctx = self.ctx;
        const err = self.err;
        const allocator = self.allocator;
        const cancelled = self.cancelled;
        if (!cancelled) self.request.clearAsyncCancel();
        if (self.bytes) |b| allocator.free(b);
        for (self.rows.items) |row| {
            for (row) |v| allocator.free(v);
            allocator.free(row);
        }
        self.rows.deinit(allocator);
        allocator.free(self.remote_origin);
        if (self.remote_ticket) |owned_ticket| allocator.free(owned_ticket);
        allocator.destroy(self);
        if (!cancelled) callback(ctx, err);
    }
};

pub fn start(index: *IDBIndex, request: *IDBRequest, kind: Kind, bounds: Engine.Bounds, mode: IDBObjectStore.GetAllMode, reverse: bool, unique: bool, limit: ?u32, exec: *Execution, ctx: *anyopaque, done: IDBRequest.AsyncDone) !IDBRequest.AsyncStatus {
    const allocator = index._engine.remote.?.allocator;
    const origin = try allocator.dupe(u8, index._engine.remote.?.origin);
    errdefer allocator.free(origin);
    const ticket = if (index._store._txn.gateWaiter().ticket) |t| try allocator.dupe(u8, t) else null;
    errdefer if (ticket) |t| allocator.free(t);
    const self = try allocator.create(State);
    errdefer allocator.destroy(self);
    self.* = .{ .allocator = allocator, .remote_client = index._engine.profile_async.?, .remote_origin = origin, .remote_ticket = ticket, .index = index, .request = request, .exec = exec, .ctx = ctx, .done = done, .kind = kind, .bounds = bounds, .mode = mode, .reverse = reverse, .unique = unique, .limit = limit };
    try self.start();
    request.setAsyncCancel(self, State.cancel);
    return .pending;
}
