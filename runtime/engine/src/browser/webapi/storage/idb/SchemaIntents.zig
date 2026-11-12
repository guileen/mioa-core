// Internal versionchange requests. They share the transaction request queue so
// schema changes cannot overtake ordinary record operations.
const std = @import("std");
const js = @import("../../../js/js.zig");
const Key = @import("Key.zig");
const Engine = @import("Engine.zig");
const Transaction = @import("IDBTransaction.zig");
const Request = @import("IDBRequest.zig");
const ProfileAsync = @import("../ProfileAsync.zig");
const RuntimeMemory = @import("../RuntimeMemory.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const Gate = @import("RemoteGate.zig");
const Backfill = @import("SchemaIndexBackfill.zig");
const Allocator = std.mem.Allocator;

pub const Intent = union(enum) {
    create_store: struct { id: i64, database_id: i64, name: []const u8, key_path: ?Key.KeyPath, auto_increment: bool },
    delete_store: struct { database_id: i64, name: []const u8 },
    rename_store: struct { id: i64, name: []const u8 },
    create_index: struct { id: i64, object_store_id: i64, name: []const u8, key_path: Key.KeyPath, unique: bool, multi_entry: bool },
    delete_index: struct { object_store_id: i64, name: []const u8 },
    rename_index: struct { id: i64, name: []const u8 },

    fn name(self: Intent) []const u8 {
        return switch (self) {
            inline else => |value| value.name,
        };
    }
    fn path(self: Intent) ?Key.KeyPath {
        return switch (self) {
            .create_store => |v| v.key_path,
            .create_index => |v| v.key_path,
            else => null,
        };
    }
};

const State = struct {
    allocator: Allocator,
    scratch: std.heap.ArenaAllocator,
    txn: *Transaction,
    request: *Request,
    intent: Intent,
    exec: *js.Execution,
    ctx: *anyopaque,
    done: Request.AsyncDone,
    waiter: *Engine.GateWaiter,
    ticket: []u8,
    name_ref: ?RemoteAsync.BlobRef = null,
    path_ref: ?RemoteAsync.BlobRef = null,
    path_components: usize = 0,
    handle: ?*RemoteAsync.Handle = null,
    raw_id: ?u64 = null,
    err: ?anyerror = null,
    cancelled: bool = false,

    fn client(self: *State) *ProfileAsync.Client {
        return self.txn._engine.profile_async.?;
    }
    fn origin(self: *State) []const u8 {
        return self.txn._engine.remote.?.origin;
    }
    fn cancel(ctx: *anyopaque) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.cancelled = true;
        // A detached transaction cannot be retained by a cleanup RPC. Closing
        // the channel synchronously fails the in-flight operation and lets the
        // owner reclaim all blobs and transaction resources.
        self.client().deinit();
    }
    fn start(self: *State) !void {
        self.handle = try RemoteAsync.upload(self.allocator, self.client(), self.origin(), self.intent.name(), self, uploadedName);
    }
    fn uploadedName(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        self.name_ref = switch (result) {
            .value => |ref| ref,
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        if (self.cancelled) {
            self.finish(error.Abort);
            return;
        }
        if (self.intent.path()) |path| {
            const column = Key.encodeKeyPathColumn(self.scratch.allocator(), path) catch |err| {
                self.finish(err);
                return;
            };
            self.path_components = column.component_length;
            self.handle = RemoteAsync.upload(self.allocator, self.client(), self.origin(), column.text, self, uploadedPath) catch |err| {
                self.finish(err);
                return;
            };
        } else self.command() catch |err| self.finish(err);
    }
    fn uploadedPath(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        self.path_ref = switch (result) {
            .value => |ref| ref,
            .failure => |err| {
                self.finish(err);
                return;
            },
        };
        if (self.cancelled) {
            self.finish(error.Abort);
            return;
        }
        self.command() catch |err| self.finish(err);
    }
    fn command(self: *State) !void {
        if (self.txn.gateWaiter() != self.waiter or self.waiter.ticket == null or !std.mem.eql(u8, self.waiter.ticket.?, self.ticket)) return error.ProfileServiceLost;
        var a: [32]u8 = undefined;
        var b: [32]u8 = undefined;
        var c: [32]u8 = undefined;
        const components = std.fmt.bufPrint(&c, "{d}", .{self.path_components}) catch unreachable;
        const path = if (self.path_ref) |ref| .{ .data = ref, .component_length = components } else null;
        const client_ = self.client();
        const ticket = self.ticket;
        const alloc = self.allocator;
        const origin_ = self.origin();
        const name = self.name_ref.?;
        self.raw_id = switch (self.intent) {
            .create_store => |v| try RemoteAsync.request(alloc, client_, origin_, ticket, "idb.createObjectStoreWithId", .{ .id = number(&a, v.id), .database_id = number(&b, v.database_id), .name = name, .key_path = path, .auto_increment = v.auto_increment }, self, commandDone),
            .delete_store => |v| try RemoteAsync.request(alloc, client_, origin_, ticket, "idb.deleteObjectStore", .{ .database_id = number(&a, v.database_id), .name = name }, self, commandDone),
            .rename_store => |v| try RemoteAsync.request(alloc, client_, origin_, ticket, "idb.renameObjectStore", .{ .object_store_id = number(&a, v.id), .name = name }, self, commandDone),
            .create_index => |v| try RemoteAsync.request(alloc, client_, origin_, ticket, "idb.createIndexRowWithId", .{ .id = number(&a, v.id), .object_store_id = number(&b, v.object_store_id), .name = name, .key_path = path, .unique = v.unique, .multi_entry = v.multi_entry }, self, commandDone),
            .delete_index => |v| try RemoteAsync.request(alloc, client_, origin_, ticket, "idb.deleteIndexRow", .{ .object_store_id = number(&a, v.object_store_id), .name = name }, self, commandDone),
            .rename_index => |v| try RemoteAsync.request(alloc, client_, origin_, ticket, "idb.renameIndex", .{ .index_id = number(&a, v.id), .name = name }, self, commandDone),
        };
    }
    fn commandDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.raw_id = null;
        const reply = switch (result) {
            .failure => |err| {
                self.finish(err);
                return;
            },
            .reply => |r| r,
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
        switch (self.intent) {
            .create_store => |v| if (!matchesId(value, v.id)) {
                self.finish(error.ProfileProtocolError);
                return;
            },
            .create_index => |v| if (!matchesId(value, v.id)) {
                self.finish(error.ProfileProtocolError);
                return;
            },
            else => if (value != .null) {
                self.finish(error.ProfileProtocolError);
                return;
            },
        }
        self.finish(null);
    }
    fn finish(self: *State, err: ?anyerror) void {
        if (self.err == null) self.err = err;
        if (self.cancelled) {
            if (self.path_ref) |ref| {
                self.freeRef(ref);
                self.path_ref = null;
            }
            if (self.name_ref) |ref| {
                self.freeRef(ref);
                self.name_ref = null;
            }
            self.cleanup();
            return;
        }
        if (self.path_ref) |ref| {
            self.path_ref = null;
            self.handle = RemoteAsync.drop(self.allocator, self.client(), self.origin(), ref, self, dropped) catch |failure| {
                self.freeRef(ref);
                if (self.name_ref) |name| {
                    self.freeRef(name);
                    self.name_ref = null;
                }
                if (self.err == null) self.err = failure;
                self.client().deinit();
                self.cleanup();
                return;
            };
            self.freeRef(ref);
            return;
        }
        if (self.name_ref) |ref| {
            self.name_ref = null;
            self.handle = RemoteAsync.drop(self.allocator, self.client(), self.origin(), ref, self, dropped) catch |failure| {
                self.freeRef(ref);
                if (self.err == null) self.err = failure;
                self.client().deinit();
                self.cleanup();
                return;
            };
            self.freeRef(ref);
            return;
        }
        self.cleanup();
    }
    fn freeRef(self: *State, ref: RemoteAsync.BlobRef) void {
        self.allocator.free(ref.blob_id);
        self.allocator.free(ref.size);
    }
    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *State = @ptrCast(@alignCast(ctx));
        self.handle = null;
        self.finish(err);
    }
    fn cleanup(self: *State) void {
        self.request.clearAsyncCancel();
        const err: ?anyerror = if (self.err) |failure| failure else if (self.cancelled) error.Abort else null;
        const txn = self.txn;
        const request = self.request;
        const exec = self.exec;
        const ctx = self.ctx;
        const done = self.done;
        const intent = self.intent;
        const allocator = self.allocator;
        self.scratch.deinit();
        allocator.free(self.ticket);
        allocator.destroy(self);
        if (err) |failure| {
            done(ctx, failure);
            return;
        }
        if (intent == .create_index) {
            const v = intent.create_index;
            const status = Backfill.start(txn, v.object_store_id, .{ .id = v.id, .key_path = v.key_path, .unique = v.unique, .multi_entry = v.multi_entry }, request, exec, ctx, done) catch |failure| {
                done(ctx, failure);
                return;
            };
            if (status == .completed) done(ctx, null);
        } else done(ctx, null);
    }
};
fn number(buf: *[32]u8, id: i64) []const u8 {
    return std.fmt.bufPrint(buf, "{d}", .{id}) catch unreachable;
}
fn matchesId(value: std.json.Value, id: i64) bool {
    if (value != .string) return false;
    return (std.fmt.parseInt(i64, value.string, 10) catch return false) == id;
}

pub fn execute(txn: *Transaction, request: *Request, intent: Intent, exec: *js.Execution, ctx: *anyopaque, done: Request.AsyncDone) !Request.AsyncStatus {
    if (txn._mode != .versionchange or txn._settled) return error.InvalidStateError;
    const waiter = txn.gateWaiter();
    const ticket = waiter.ticket orelse return error.ProfileServiceLost;
    const allocator = try RuntimeMemory.allocator(txn._engine.remote.?.allocator);
    const self = try allocator.create(State);
    errdefer allocator.destroy(self);
    const owned_ticket = try allocator.dupe(u8, ticket);
    errdefer allocator.free(owned_ticket);
    self.* = .{ .allocator = allocator, .scratch = std.heap.ArenaAllocator.init(allocator), .txn = txn, .request = request, .intent = intent, .exec = exec, .ctx = ctx, .done = done, .waiter = waiter, .ticket = owned_ticket };
    errdefer self.scratch.deinit();
    try self.start();
    request.setAsyncCancel(self, State.cancel);
    return .pending;
}
