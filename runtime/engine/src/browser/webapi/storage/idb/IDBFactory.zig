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

const Key = @import("Key.zig");
const Engine = @import("Engine.zig");
const RemoteAsync = @import("RemoteAsync.zig");
const RemoteGate = @import("RemoteGate.zig");
const ProfileAsync = @import("../ProfileAsync.zig");
const RuntimeMemory = @import("../RuntimeMemory.zig");
const SchemaSnapshot = @import("SchemaSnapshot.zig");
const SchemaLoad = @import("SchemaLoad.zig");
const IDBRequest = @import("IDBRequest.zig");
const IDBDatabase = @import("IDBDatabase.zig");
const IDBTransaction = @import("IDBTransaction.zig");

const log = lp.log;
const Execution = js.Execution;

const IDBFactory = @This();

_pad: bool = false,

pub fn open(_: *IDBFactory, name: []const u8, version: ?u64, exec: *Execution) !*IDBRequest {
    if (exec.origin() == null) {
        // unavailable for opaque origins, e.g. about:blank
        return error.SecurityError;
    }

    // A supplied version must be >= 1; open(name, 0) is a TypeError per spec.
    if (version) |v| {
        if (v == 0) return error.TypeError;
    }

    const request = try IDBRequest.initOpen(exec);

    const ctx = try exec._factory.create(OpenContext{
        .request = request,
        .name = try exec.dupeString(name),
        .version = version,
        .exec = exec,
        ._gate_waiter = .{ .ctx = exec.js, .wake = OpenContext.wakeUp, .cancel = OpenContext.cancelParked },
    });
    errdefer exec._factory.destroy(ctx);

    try exec.js.scheduler.add(ctx, OpenContext.run, 0, .{
        .name = "IDBFactory.open",
        .repeat_blocks_done = true,
        .repeat_failure = OpenContext.repeatFailed,
        .finalizer = OpenContext.cancelled,
    });
    return request;
}

const State = enum {
    scheduled, // The scheduler's finalizer
    parked, // cancelParked
    waiting_upgrade, // async transaction operation owns the continuation
    waiting_operation, // async owner request owns the continuation
    running, // on the stack, will finalize when done
};

const OpenContext = struct {
    request: *IDBRequest,
    name: []const u8,
    version: ?u64,
    exec: *Execution,
    // Our node in the engine's connection gate wait-list. See Engine.acquireGate.
    _gate_waiter: Engine.GateWaiter,
    _engine: ?*Engine = null,
    _parked_reset: u32 = 0,
    _state: State = .scheduled,
    _open_job: ?*OpenJob = null,
    _open_result: ?OpenResult = null,

    // If an callback queued more requests, we need to process those requests
    // on the next tick, and thus need to hold onto the transaction (which pins
    // that transaction (_rc++) so that it does't get cleaned up from under us).
    _upgrade: ?*IDBTransaction = null,

    fn cancelled(ctx: *anyopaque) void {
        // What if we're gated? Well, A scheduled task is only canceled on
        // teardown, which would have already called Engine.detach(js_ctx).
        const self: *OpenContext = @ptrCast(@alignCast(ctx));
        self.discardResult();
        if (self._open_job) |job| {
            self._open_job = null;
            job.detach();
        }
        if (self._upgrade) |txn| {
            txn.detachAsync();
            if (txn._begun and txn._engine.remote == null) {
                txn._engine.rollback(txn.gateWaiter());
            }
            txn._begun = false;
            txn._settled = true;
            self.request._txn = .none;
            txn._db._txn = null;
            txn._owner_waiter = null;
            txn.releaseRef(self.exec.page);
        }
        if (self._engine) |engine| engine.cancelGate(&self._gate_waiter);
        self.exec._factory.destroy(self);
    }

    fn repeatFailed(ctx: *anyopaque, err: anyerror) void {
        const self: *OpenContext = @ptrCast(@alignCast(ctx));
        self.discardResult();
        if (self._open_job) |job| {
            self._open_job = null;
            job.detach();
        }
        if (self._upgrade) |txn| {
            txn._upgrade_wake = null;
            txn._upgrade_wake_ctx = null;
            txn._committing = false;
            txn.abortWith(self.exec, err) catch {};
            txn.detachAsync();
            txn._begun = false;
            txn._settled = true;
            self.request._txn = .none;
            txn._db._txn = null;
            txn._owner_waiter = null;
            txn.releaseRef(self.exec.page);
            self._upgrade = null;
        }
        if (self._engine) |engine| engine.cancelGate(&self._gate_waiter);
        self.request.setError(err);
        self.request.deliver(self.exec) catch {};
        self.exec._factory.destroy(self);
    }

    // Engine.detach cancel: our context is going away. Only a parked context
    // owns its destruction here; otherwise a task finalizer or `run` does.
    fn cancelParked(waiter: *Engine.GateWaiter) void {
        const self: *OpenContext = @fieldParentPtr("_gate_waiter", waiter);
        if (self._upgrade) |txn| {
            txn.detachAsync();
            // Engine.detach rolls back before handing admission on.
            txn._begun = false;
            txn._settled = true;
            if (self._state == .waiting_upgrade) {
                if (self._parked_reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
                self.request._txn = .none;
                txn._db._txn = null;
                txn._owner_waiter = null;
                txn.releaseRef(self.exec.page);
                self.exec._factory.destroy(self);
            }
            return;
        }
        if (self._state == .parked or self._state == .waiting_operation) {
            if (self._parked_reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
            if (self._open_job) |job| {
                self._open_job = null;
                job.detach();
            }
            self.exec._factory.destroy(self);
        }
    }

    fn run(ctx: *anyopaque) !?u32 {
        const self: *OpenContext = @ptrCast(@alignCast(ctx));
        self._state = .running;

        if (self._upgrade != null) {
            return self.drainUpgrade();
        }

        const engine = self._engine orelse self.resolveEngine() catch |err| {
            defer self.exec._factory.destroy(self);
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            return null;
        };
        self._engine = engine;

        // Both local handoffs and remote notifications wake the parked task.
        const acquired = engine.acquireGate(&self._gate_waiter) catch |err| {
            engine.cancelGate(&self._gate_waiter);
            defer self.exec._factory.destroy(self);
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            return null;
        };
        if (!acquired) {
            self._state = .parked;
            self._parked_reset = self.exec.js.scheduler.resets;
            self.exec.js.scheduler.blocking += 1;
            return null;
        }

        if (engine.remote) |remote| {
            if (self._open_result) |result| {
                self._open_result = null;
                const upgrading = self.finishRemoteOpen(engine, result) catch |err| blk: {
                    self.request.setError(err);
                    self.request.deliver(self.exec) catch {};
                    break :blk false;
                };
                if (upgrading) {
                    self._state = .scheduled;
                    return 1;
                }
                _ = engine.releaseGate(&self._gate_waiter);
                self.exec._factory.destroy(self);
                return null;
            }
            const client = engine.profile_async orelse {
                self.request.setError(error.ProfileServiceLost);
                self.request.deliver(self.exec) catch {};
                _ = engine.releaseGate(&self._gate_waiter);
                self.exec._factory.destroy(self);
                return null;
            };
            const ticket = self._gate_waiter.ticket orelse {
                self.request.setError(error.ProfileProtocolError);
                self.request.deliver(self.exec) catch {};
                _ = engine.releaseGate(&self._gate_waiter);
                self.exec._factory.destroy(self);
                return null;
            };
            self._open_job = OpenJob.start(remote.allocator, client, remote.origin, ticket, self.name, self.version, self) catch |err| {
                self.request.setError(err);
                self.request.deliver(self.exec) catch {};
                _ = engine.releaseGate(&self._gate_waiter);
                self.exec._factory.destroy(self);
                return null;
            };
            self._state = .waiting_operation;
            self._parked_reset = self.exec.js.scheduler.resets;
            self.exec.js.scheduler.blocking += 1;
            return null;
        }

        const upgrading = self.runOpen(engine) catch |err| blk: {
            log.warn(.storage, "idb open", .{ .err = err, .name = self.name, .sqlite = engine.lastError() });
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            break :blk false;
        };
        if (upgrading) {
            self._state = .scheduled;
            return 1; // the versionchange drain continues next turn; keep the gate
        }

        _ = engine.releaseGate(&self._gate_waiter);
        self.exec._factory.destroy(self);
        return null;
    }

    // One turn of the versionchange drain: deliver a batch of request events;
    // handlers may enqueue more. Once the transaction settles — the queue
    // stayed empty (committed, `complete` fired) or a handler aborted —
    // deliver the open request's outcome and clean up.
    fn drainUpgrade(self: *OpenContext) !?u32 {
        const txn = self._upgrade.?;
        switch (txn.settleStep(self.exec)) {
            .pending => {
                self._state = .waiting_upgrade;
                self._parked_reset = self.exec.js.scheduler.resets;
                self.exec.js.scheduler.blocking += 1;
                return null;
            },
            .again => {
                self._state = .scheduled;
                return 1;
            },
            .done => {},
        }
        if (txn.abortDeliveryPending()) {
            self._state = .scheduled;
            return 1;
        }

        self._upgrade = null;
        const engine = txn._engine;
        defer self.exec._factory.destroy(self);
        defer _ = engine.releaseGate(&self._gate_waiter);
        try self.finishUpgrade(txn);
        return null;
    }

    fn upgradeWake(ctx: *anyopaque) void {
        const self: *OpenContext = @ptrCast(@alignCast(ctx));
        if (self._state != .waiting_upgrade) return;
        if (self._parked_reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
        self._state = .scheduled;
        self.exec.js.scheduler.add(self, run, 0, .{
            .name = "IDBFactory.open",
            .repeat_blocks_done = true,
            .repeat_failure = repeatFailed,
            .finalizer = cancelled,
        }) catch |err| repeatFailed(self, err);
    }

    // The versionchange transaction settled (committed or aborted): sever the
    // upgrade wiring, drop our pin (may free the transaction), and deliver the
    // open request's outcome.
    fn finishUpgrade(self: *OpenContext, txn: *IDBTransaction) !void {
        const exec = self.exec;
        const db = txn._db;
        // Our pin is often the last one, so releasing it frees `txn` (its
        // arena goes back to the pool). Read everything we need first.
        const failed = txn.aborted() or db._closed;
        self.request._txn = .none;
        db._txn = null;
        txn._owner_waiter = null;
        txn._upgrade_wake = null;
        txn._upgrade_wake_ctx = null;
        txn.releaseRef(exec.page);

        if (failed) {
            self.request._result = .{ .none = js.Undefined{} };
            self.request.setError(error.AbortError);
            return self.request.deliver(exec);
        }
        return self.request.fireSuccess(exec);
    }

    // Scheduler wake-up: the connection gate was handed to us, so re-run.
    fn wakeUp(waiter: *Engine.GateWaiter) void {
        const self: *OpenContext = @fieldParentPtr("_gate_waiter", waiter);
        if (self._parked_reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
        self._state = .scheduled;
        self.exec.js.scheduler.add(self, run, 0, .{
            .name = "IDBFactory.open",
            .finalizer = cancelled,
            .repeat_blocks_done = true,
            .repeat_failure = repeatFailed,
        }) catch |err| {
            // We were handed the gate; if we can't reschedule, hand it off so the
            // waiters behind us aren't stranded.
            log.debug(.storage, "idb resume open", .{ .err = err });
            if (self._engine) |engine| engine.cancelGate(&self._gate_waiter);
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            self.exec._factory.destroy(self);
        };
    }

    fn resolveEngine(self: *OpenContext) !*Engine {
        // origin being null was already guarded against, so this should be
        // unreachable, but this is safer.
        const origin = self.exec.origin() orelse return error.SecurityError;
        return self.exec.session.idb.engineForOrigin(origin);
    }

    fn discardResult(self: *OpenContext) void {
        if (self._open_result) |*result| switch (result.*) {
            .success => |*data| data.arena.deinit(),
            .failure => {},
        };
        self._open_result = null;
    }

    fn finishRemoteOpen(self: *OpenContext, engine: *Engine, result: OpenResult) !bool {
        var data = switch (result) {
            .failure => |err| return err,
            .success => |value| value,
        };
        errdefer data.arena.deinit();
        const exec = self.exec;
        const db = try IDBDatabase.init(exec, engine, data.database_id, self.name, data.version);
        db.replaceSnapshot(data.arena, data.snapshot);
        // Ownership was transferred; subsequent errors are handled by db.
        data.arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        self.request.setDatabaseResult(db);
        if (!data.upgrade) {
            try self.request.fireSuccess(exec);
            return false;
        }
        const txn = try IDBTransaction.initVersionChange(db, exec);
        txn._owner_waiter = &self._gate_waiter;
        txn._upgrade_wake = upgradeWake;
        txn._upgrade_wake_ctx = self;
        txn._old_version = data.old_version orelse 0;
        txn.acquireRef();
        errdefer {
            self.request._txn = .none;
            db._txn = null;
            txn.detachAsync();
            txn._owner_waiter = null;
            txn._begun = false;
            txn._settled = true;
            txn.releaseRef(exec.page);
        }
        self.request._txn = .{ .borrowed = txn };
        db._txn = txn;
        try self.request.fireUpgradeNeeded(exec, @intCast(data.old_version orelse 0), @intCast(data.version));
        self._upgrade = txn;
        return true;
    }

    // Returns true when an upgrade drain is now pending: the versionchange
    // transaction has queued requests, the gate stays held and drainUpgrade
    // takes over on the next turns.
    fn runOpen(self: *OpenContext, engine: *Engine) !bool {
        const exec = self.exec;
        const existing = try engine.databaseVersion(&self._gate_waiter, self.name);

        // No explicit version means "open at the current version" (or 1 for a
        // brand-new database).
        const requested: i64 = if (self.version) |v| @intCast(v) else existing orelse 1;

        if (existing) |current| {
            if (requested < current) {
                self.request.setError(error.VersionError);
                self.request.deliver(exec) catch {};
                return false;
            }

            if (requested == current) {
                const database_id = (try engine.databaseId(&self._gate_waiter, self.name)).?;
                const db = try IDBDatabase.init(exec, engine, database_id, self.name, current);
                self.request.setDatabaseResult(db);
                try self.request.fireSuccess(exec);
                return false;
            }
        }

        // New database or an upgrade to a higher version. Run a versionchange
        // transaction so user JS can evolve the schema during `upgradeneeded`;
        // it's exposed as `request.transaction` and committed once its request
        // queue stays empty.
        try engine.begin(&self._gate_waiter);

        var closed = false;
        errdefer if (closed == false) {
            engine.rollback(&self._gate_waiter);
        };

        const database_id = try engine.upsertDatabase(&self._gate_waiter, self.name, requested);
        const db = try IDBDatabase.init(exec, engine, database_id, self.name, requested);
        self.request.setDatabaseResult(db);

        const txn = try IDBTransaction.initVersionChange(db, exec);
        txn._owner_waiter = &self._gate_waiter;
        txn._upgrade_wake = upgradeWake;
        txn._upgrade_wake_ctx = self;
        txn._old_version = existing orelse 0;
        txn.acquireRef();

        {
            // The wiring below outlives this call on the drain path; on an
            // error it must be severed here, with our pin.
            errdefer {
                self.request._txn = .none;
                db._txn = null;
                txn._upgrade_wake = null;
                txn._upgrade_wake_ctx = null;
                txn._begun = false;
                txn._settled = true;
                txn._owner_waiter = null;
                txn.releaseRef(exec.page);
            }
            self.request._txn = .{ .borrowed = txn };
            db._txn = txn;
            const old_version: u64 = @intCast(existing orelse 0);
            try self.request.fireUpgradeNeeded(exec, old_version, @intCast(requested));
        }

        // All upgrades use the asynchronous drain, including an empty one:
        // committing a shared Profile transaction may itself be pending.
        closed = true;
        self._upgrade = txn;
        return true;
    }
};

pub fn deleteDatabase(_: *IDBFactory, name: []const u8, exec: *Execution) !*IDBRequest {
    if (exec.origin() == null) {
        // unavailable for opaque origins, e.g. about:blank
        return error.SecurityError;
    }

    const request = try IDBRequest.initOpen(exec);

    const ctx = try exec._factory.create(DeleteContext{
        .request = request,
        .name = try exec.dupeString(name),
        .exec = exec,
        ._gate_waiter = .{ .ctx = exec.js, .wake = DeleteContext.wakeUp, .cancel = DeleteContext.cancelParked },
    });
    errdefer exec._factory.destroy(ctx);

    try exec.js.scheduler.add(ctx, DeleteContext.run, 0, .{
        .name = "IDBFactory.deleteDatabase",
        .repeat_blocks_done = true,
        .repeat_failure = DeleteContext.repeatFailed,
        .finalizer = DeleteContext.cancelled,
    });
    return request;
}

const OpenResult = union(enum) {
    failure: anyerror,
    success: struct {
        database_id: i64,
        version: i64,
        old_version: ?i64,
        upgrade: bool,
        snapshot: SchemaSnapshot.Snapshot,
        arena: std.heap.ArenaAllocator,
    },
};

// Remote operation state is heap-owned so cancellation can outlive the page.
const OpenJob = struct {
    allocator: std.mem.Allocator,
    snapshot_arena: std.heap.ArenaAllocator,
    client: *ProfileAsync.Client,
    origin: []u8,
    ticket: []u8,
    target: ?*OpenContext,
    version: ?u64,
    stage: enum { version, id, begin, upsert } = .version,
    handle: ?*RemoteAsync.Handle = null,
    schema_handle: ?*SchemaLoad.Handle = null,
    request_id: ?u64 = null,
    blob: ?RemoteAsync.BlobRef = null,
    failure: ?anyerror = null,
    cleaning: bool = false,
    old_version: ?i64 = null,
    requested: i64 = 0,
    database_id: i64 = 0,
    upgrade: bool = false,
    snapshot: ?SchemaSnapshot.Snapshot = null,

    fn start(allocator: std.mem.Allocator, client: *ProfileAsync.Client, origin: []const u8, ticket: []const u8, name: []const u8, version: ?u64, target: *OpenContext) !*OpenJob {
        const self = try allocator.create(OpenJob);
        errdefer allocator.destroy(self);
        const own_origin = try allocator.dupe(u8, origin);
        errdefer allocator.free(own_origin);
        const own_ticket = try allocator.dupe(u8, ticket);
        errdefer allocator.free(own_ticket);
        self.* = .{ .allocator = allocator, .snapshot_arena = std.heap.ArenaAllocator.init(try RuntimeMemory.allocator(allocator)), .client = client, .origin = own_origin, .ticket = own_ticket, .target = target, .version = version };
        errdefer self.snapshot_arena.deinit();
        self.handle = try RemoteAsync.upload(allocator, client, origin, name, self, uploaded);
        return self;
    }

    fn detach(self: *OpenJob) void {
        self.target = null;
        self.failure = error.Abort;
        if (self.cleaning) return;
        if (self.schema_handle) |handle| {
            self.schema_handle = null;
            handle.cancel();
        } else if (self.handle) |handle| {
            self.handle = null;
            handle.cancel();
        } else if (self.request_id) |id| {
            self.request_id = null;
            self.client.cancel(id);
        }
    }

    fn uploaded(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *OpenJob = @ptrCast(@alignCast(ctx));
        self.handle = null;
        switch (result) {
            .failure => |err| {
                self.failure = err;
                self.finish();
            },
            .value => |ref| {
                self.blob = ref;
                if (self.target == null) return self.cleanup();
                self.submit("idb.databaseVersion", .{ .name = ref }) catch |err| {
                    self.failure = err;
                    self.cleanup();
                };
            },
        }
    }

    fn submit(self: *OpenJob, op: []const u8, payload: anytype) !void {
        self.request_id = try RemoteAsync.request(self.allocator, self.client, self.origin, self.ticket, op, payload, self, completed);
    }

    fn completed(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *OpenJob = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        const reply = switch (result) {
            .failure => |err| {
                self.failure = err;
                self.cleanup();
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        if (RemoteGate.responseError(reply.value)) |err| {
            self.failure = err;
            self.cleanup();
            return;
        }
        const value = reply.value.object.get("result") orelse {
            self.failure = error.ProfileProtocolError;
            self.cleanup();
            return;
        };
        if (self.target == null) return self.cleanup();
        self.advance(value) catch |err| {
            self.failure = err;
            self.cleanup();
        };
    }

    fn number(value: std.json.Value) !i64 {
        if (value != .string) return error.ProfileProtocolError;
        return std.fmt.parseInt(i64, value.string, 10) catch error.ProfileProtocolError;
    }

    fn advance(self: *OpenJob, value: std.json.Value) !void {
        switch (self.stage) {
            .version => {
                self.old_version = if (value == .null) null else try number(value);
                self.requested = if (self.version) |version| std.math.cast(i64, version) orelse return error.TypeError else self.old_version orelse 1;
                if (self.old_version) |current| if (self.requested < current) return error.VersionError;
                self.upgrade = self.old_version == null or self.requested != self.old_version.?;
                if (self.upgrade) {
                    self.stage = .begin;
                    try self.submit("idb.begin", struct {}{});
                } else {
                    self.stage = .id;
                    try self.submit("idb.databaseId", .{ .name = self.blob.? });
                }
            },
            .begin => {
                if (value != .null) return error.ProfileProtocolError;
                self.stage = .upsert;
                var buffer: [32]u8 = undefined;
                const version = try std.fmt.bufPrint(&buffer, "{d}", .{self.requested});
                try self.submit("idb.upsertDatabase", .{ .name = self.blob.?, .version = version });
            },
            .id, .upsert => {
                self.database_id = try number(value);
                self.schema_handle = try SchemaLoad.start(self.allocator, self.snapshot_arena.allocator(), self.client, self.origin, self.ticket, self.database_id, self, schemaDone);
            },
        }
    }

    fn schemaDone(ctx: *anyopaque, result: SchemaLoad.Result) void {
        const self: *OpenJob = @ptrCast(@alignCast(ctx));
        self.schema_handle = null;
        switch (result) {
            .failure => |err| self.failure = err,
            .success => |snapshot| self.snapshot = snapshot,
        }
        self.cleanup();
    }

    fn cleanup(self: *OpenJob) void {
        const blob = self.blob orelse return self.finish();
        self.cleaning = true;
        self.handle = RemoteAsync.drop(self.allocator, self.client, self.origin, blob, self, dropped) catch |err| {
            if (self.failure == null) self.failure = err;
            self.finish();
            return;
        };
    }

    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *OpenJob = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (err) |failure| if (self.failure == null) {
            self.failure = failure;
        };
        self.finish();
    }

    fn finish(self: *OpenJob) void {
        const target = self.target;
        const result: OpenResult = if (self.failure) |err| .{ .failure = err } else .{ .success = .{
            .database_id = self.database_id,
            .version = self.requested,
            .old_version = self.old_version,
            .upgrade = self.upgrade,
            .snapshot = self.snapshot.?,
            .arena = self.snapshot_arena,
        } };
        if (self.failure != null or target == null) self.snapshot_arena.deinit();
        const allocator = self.allocator;
        if (self.blob) |blob| {
            allocator.free(blob.blob_id);
            allocator.free(blob.size);
        }
        allocator.free(self.origin);
        allocator.free(self.ticket);
        allocator.destroy(self);
        if (target) |context| {
            context._open_job = null;
            context._open_result = result;
            OpenContext.wakeUp(&context._gate_waiter);
        }
    }
};

const DeleteContext = struct {
    request: *IDBRequest,
    name: []const u8,
    exec: *Execution,
    _gate_waiter: Engine.GateWaiter,
    _engine: ?*Engine = null,
    // See OpenContext._state.
    _parked_reset: u32 = 0,
    _state: State = .scheduled,
    _job: ?*DeleteJob = null,
    _delete_done: bool = false,
    _delete_error: ?anyerror = null,

    fn cancelled(ctx: *anyopaque) void {
        // What if we're gated? Well, A scheduled task is only canceled on
        // teardown, which would have already called Engine.detach(js_ctx).
        const self: *DeleteContext = @ptrCast(@alignCast(ctx));
        self.cancelRequest();
        if (self._engine) |engine| engine.cancelGate(&self._gate_waiter);
        self.exec._factory.destroy(self);
    }

    fn repeatFailed(ctx: *anyopaque, err: anyerror) void {
        const self: *DeleteContext = @ptrCast(@alignCast(ctx));
        self.cancelRequest();
        if (self._engine) |engine| engine.cancelGate(&self._gate_waiter);
        self.request.setError(err);
        self.request.deliver(self.exec) catch {};
        self.exec._factory.destroy(self);
    }

    // See OpenContext.cancelParked.
    fn cancelParked(waiter: *Engine.GateWaiter) void {
        const self: *DeleteContext = @fieldParentPtr("_gate_waiter", waiter);
        if (self._state == .parked or self._state == .waiting_operation) {
            if (self._parked_reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
            self.cancelRequest();
            self.exec._factory.destroy(self);
        }
    }

    fn run(ctx: *anyopaque) !?u32 {
        const self: *DeleteContext = @ptrCast(@alignCast(ctx));
        self._state = .running;

        const engine = self._engine orelse self.resolveEngine() catch |err| {
            defer self.exec._factory.destroy(self);
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            return null;
        };
        self._engine = engine;

        const acquired = engine.acquireGate(&self._gate_waiter) catch |err| {
            engine.cancelGate(&self._gate_waiter);
            defer self.exec._factory.destroy(self);
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            return null;
        };
        if (!acquired) {
            self._state = .parked;
            self._parked_reset = self.exec.js.scheduler.resets;
            self.exec.js.scheduler.blocking += 1;
            return null;
        }
        if (engine.remote) |remote| {
            if (!self._delete_done) {
                const client = engine.profile_async orelse {
                    self.request.setError(error.ProfileServiceLost);
                    self.request.deliver(self.exec) catch {};
                    _ = engine.releaseGate(&self._gate_waiter);
                    self.exec._factory.destroy(self);
                    return null;
                };
                const ticket = self._gate_waiter.ticket orelse {
                    self.request.setError(error.ProfileProtocolError);
                    self.request.deliver(self.exec) catch {};
                    _ = engine.releaseGate(&self._gate_waiter);
                    self.exec._factory.destroy(self);
                    return null;
                };
                self._job = DeleteJob.start(remote.allocator, client, remote.origin, ticket, self.name, self) catch |err| {
                    self.request.setError(err);
                    self.request.deliver(self.exec) catch {};
                    _ = engine.releaseGate(&self._gate_waiter);
                    self.exec._factory.destroy(self);
                    return null;
                };
                self._state = .waiting_operation;
                self._parked_reset = self.exec.js.scheduler.resets;
                self.exec.js.scheduler.blocking += 1;
                return null;
            }
            defer self.exec._factory.destroy(self);
            defer _ = engine.releaseGate(&self._gate_waiter);
            if (self._delete_error) |err| {
                self.request.setError(err);
                self.request.deliver(self.exec) catch {};
            } else self.request.fireSuccess(self.exec) catch {};
            return null;
        }
        defer self.exec._factory.destroy(self);
        defer _ = engine.releaseGate(&self._gate_waiter);

        self.runDelete(engine) catch |err| {
            log.warn(.storage, "idb deleteDatabase", .{ .err = err, .name = self.name, .sqlite = engine.lastError() });
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
        };
        return null;
    }

    fn cancelRequest(self: *DeleteContext) void {
        const job = self._job orelse return;
        self._job = null;
        job.detach();
    }

    // Scheduler wake-up: the connection gate was handed to us, so re-run.
    fn wakeUp(waiter: *Engine.GateWaiter) void {
        const self: *DeleteContext = @fieldParentPtr("_gate_waiter", waiter);
        if (self._parked_reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
        self._state = .scheduled;
        self.exec.js.scheduler.add(self, run, 0, .{
            .name = "IDBFactory.deleteDatabase",
            .finalizer = cancelled,
            .repeat_blocks_done = true,
            .repeat_failure = repeatFailed,
        }) catch |err| {
            // We were handed the gate; if we can't reschedule, hand it off so the
            // waiters behind us aren't stranded.
            log.debug(.storage, "idb resume delete", .{ .err = err });
            if (self._engine) |engine| engine.cancelGate(&self._gate_waiter);
            self.request.setError(err);
            self.request.deliver(self.exec) catch {};
            self.exec._factory.destroy(self);
        };
    }

    fn resolveEngine(self: *DeleteContext) !*Engine {
        const origin = self.exec.origin() orelse return error.SecurityError;
        return self.exec.session.idb.engineForOrigin(origin);
    }

    fn runDelete(self: *DeleteContext, engine: *Engine) !void {
        try engine.deleteDatabase(&self._gate_waiter, self.name);
        return self.request.fireSuccess(self.exec);
    }
};

// Owner RPC and blob cleanup may outlive a detached JS context.
const DeleteJob = struct {
    allocator: std.mem.Allocator,
    client: *ProfileAsync.Client,
    origin: []u8,
    ticket: []u8,
    target: ?*DeleteContext,
    handle: ?*RemoteAsync.Handle = null,
    request_id: ?u64 = null,
    blob: ?RemoteAsync.BlobRef = null,
    failure: ?anyerror = null,
    cleaning: bool = false,

    fn start(allocator: std.mem.Allocator, client: *ProfileAsync.Client, origin: []const u8, ticket: []const u8, name: []const u8, target: *DeleteContext) !*DeleteJob {
        const self = try allocator.create(DeleteJob);
        errdefer allocator.destroy(self);
        const owned_origin = try allocator.dupe(u8, origin);
        errdefer allocator.free(owned_origin);
        const owned_ticket = try allocator.dupe(u8, ticket);
        errdefer allocator.free(owned_ticket);
        self.* = .{ .allocator = allocator, .client = client, .origin = owned_origin, .ticket = owned_ticket, .target = target };
        self.handle = try RemoteAsync.upload(allocator, client, origin, name, self, uploaded);
        return self;
    }

    fn detach(self: *DeleteJob) void {
        self.target = null;
        if (self.cleaning) return;
        if (self.handle) |handle| {
            self.handle = null;
            handle.cancel();
        } else if (self.request_id) |id| {
            self.request_id = null;
            self.client.cancel(id);
        }
    }

    fn uploaded(ctx: *anyopaque, result: RemoteAsync.BlobResult) void {
        const self: *DeleteJob = @ptrCast(@alignCast(ctx));
        self.handle = null;
        switch (result) {
            .failure => |err| {
                self.failure = err;
                self.finish();
            },
            .value => |ref| {
                self.blob = ref;
                if (self.target == null) return self.cleanup();
                self.request_id = RemoteAsync.request(self.allocator, self.client, self.origin, self.ticket, "idb.deleteDatabase", .{ .name = ref }, self, deleted) catch |err| {
                    self.failure = err;
                    self.cleanup();
                    return;
                };
            },
        }
    }

    fn deleted(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *DeleteJob = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        switch (result) {
            .failure => |err| self.failure = err,
            .reply => |reply| {
                defer reply.deinit();
                if (RemoteGate.responseError(reply.value)) |err| self.failure = err else {
                    const value: std.json.Value = reply.value.object.get("result") orelse .{ .bool = false };
                    if (value != .null) self.failure = error.ProfileProtocolError;
                }
            },
        }
        self.cleanup();
    }

    fn cleanup(self: *DeleteJob) void {
        const blob = self.blob orelse return self.finish();
        self.cleaning = true;
        self.handle = RemoteAsync.drop(self.allocator, self.client, self.origin, blob, self, dropped) catch |err| {
            if (self.failure == null) self.failure = err;
            self.finish();
            return;
        };
    }

    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *DeleteJob = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (err) |failure| if (self.failure == null) {
            self.failure = failure;
        };
        self.finish();
    }

    fn finish(self: *DeleteJob) void {
        const target = self.target;
        const failure = self.failure;
        const allocator = self.allocator;
        if (self.blob) |blob| {
            allocator.free(blob.blob_id);
            allocator.free(blob.size);
        }
        allocator.free(self.origin);
        allocator.free(self.ticket);
        allocator.destroy(self);
        if (target) |context| {
            context._job = null;
            context._delete_error = failure;
            context._delete_done = true;
            DeleteContext.wakeUp(&context._gate_waiter);
        }
    }
};

fn databases(_: *IDBFactory, exec: *Execution) !js.Promise {
    const local = exec.js.local.?;
    // unavailable for opaque origins, e.g. about:blank
    const origin = exec.origin() orelse return error.SecurityError;
    const engine = try exec.session.idb.engineForOrigin(origin);
    if (engine.remote) |remote| {
        const resolver = js.PromiseResolver.init(local);
        const promise = resolver.promise();
        const global = try resolver.persist();
        errdefer global.deinit();
        try DatabaseList.start(remote.allocator, engine, exec, global);
        return promise;
    }
    return local.resolvePromise(try engine.databases(null, exec.call_arena));
}

// The listing and its cursor/blobs may outlive a detached page. Its resource
// registration severs the JS resolver before any later transport callbacks.
const DatabaseList = struct {
    allocator: std.mem.Allocator,
    engine: *Engine,
    exec: *Execution,
    client: *ProfileAsync.Client,
    origin: []u8,
    resolver: js.PromiseResolver.Global,
    resource: Engine.ContextResource,
    reset: u32,
    detached: bool = false,
    scheduled: bool = false,
    cleaning: bool = false,
    failure: ?anyerror = null,
    request_id: ?u64 = null,
    handle: ?*RemoteAsync.Handle = null,
    rows_id: ?[]u8 = null,
    blob: ?RemoteAsync.BlobRef = null,
    pending_version: i64 = 0,
    items: std.ArrayList(Engine.DatabaseInfo) = .empty,

    fn start(allocator_: std.mem.Allocator, engine: *Engine, exec: *Execution, resolver: js.PromiseResolver.Global) !void {
        const allocator = try RuntimeMemory.allocator(allocator_);
        const self = try allocator.create(DatabaseList);
        errdefer allocator.destroy(self);
        const origin = try allocator.dupe(u8, engine.remote.?.origin);
        errdefer allocator.free(origin);
        const client = engine.profile_async orelse return error.ProfileServiceLost;
        self.* = .{
            .allocator = allocator,
            .engine = engine,
            .exec = exec,
            .client = client,
            .origin = origin,
            .resolver = resolver,
            .reset = exec.js.scheduler.resets,
            .resource = .{ .ctx = exec.js, .release = detach },
        };
        self.request_id = try RemoteAsync.request(allocator, client, origin, null, "idb.databases", struct {}{}, self, opened);
        engine.registerContextResource(&self.resource);
        exec.js.scheduler.blocking += 1;
    }

    fn detach(resource: *Engine.ContextResource) void {
        const self: *DatabaseList = @fieldParentPtr("resource", resource);
        self.detached = true;
        self.resolver.deinit();
        if (self.scheduled) return;
        if (self.reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
        self.failure = error.Abort;
        if (self.cleaning) return;
        if (self.handle) |handle| {
            self.handle = null;
            handle.cancel();
        } else if (self.request_id) |id| {
            self.request_id = null;
            self.client.cancel(id);
        }
    }

    fn opened(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        const reply = switch (result) {
            .failure => |err| {
                self.failure = err;
                self.cleanup();
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        if (RemoteGate.responseError(reply.value)) |err| {
            self.failure = err;
            self.cleanup();
            return;
        }
        const value = reply.value.object.get("result") orelse {
            self.failure = error.ProfileProtocolError;
            self.cleanup();
            return;
        };
        if (value != .object) {
            self.failure = error.ProfileProtocolError;
            self.cleanup();
            return;
        }
        const id = value.object.get("rows_id") orelse {
            self.failure = error.ProfileProtocolError;
            self.cleanup();
            return;
        };
        if (id != .string) {
            self.failure = error.ProfileProtocolError;
            self.cleanup();
            return;
        }
        self.rows_id = self.allocator.dupe(u8, id.string) catch |err| {
            self.failure = err;
            self.client.deinit();
            self.cleanup();
            return;
        };
        if (self.detached) return self.cleanup();
        self.next();
    }

    fn next(self: *DatabaseList) void {
        self.request_id = RemoteAsync.request(self.allocator, self.client, self.origin, null, "idb.rows_next", .{ .rows_id = self.rows_id.? }, self, row) catch |err| {
            self.failure = err;
            self.cleanup();
            return;
        };
    }

    fn row(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        const reply = switch (result) {
            .failure => |err| {
                self.failure = err;
                self.cleanup();
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        if (RemoteGate.responseError(reply.value)) |err| {
            self.failure = err;
            self.cleanup();
            return;
        }
        const value = reply.value.object.get("result") orelse {
            self.failure = error.ProfileProtocolError;
            self.cleanup();
            return;
        };
        if (value == .null or self.detached) return self.cleanup();
        const parsed = std.json.parseFromValue(struct { name: RemoteAsync.BlobRef, version: []const u8 }, self.allocator, value, .{ .allocate = .alloc_always }) catch |err| {
            self.failure = err;
            self.client.deinit();
            self.cleanup();
            return;
        };
        defer parsed.deinit();
        self.pending_version = std.fmt.parseInt(i64, parsed.value.version, 10) catch {
            self.failure = error.ProfileProtocolError;
            self.client.deinit();
            self.cleanup();
            return;
        };
        const id = self.allocator.dupe(u8, parsed.value.name.blob_id) catch |err| {
            self.failure = err;
            self.client.deinit();
            self.cleanup();
            return;
        };
        const size = self.allocator.dupe(u8, parsed.value.name.size) catch |err| {
            self.allocator.free(id);
            self.failure = err;
            self.client.deinit();
            self.cleanup();
            return;
        };
        self.blob = .{ .blob_id = id, .size = size };
        self.handle = RemoteAsync.download(self.allocator, self.client, self.origin, self.blob.?, self, downloaded) catch |err| {
            self.failure = err;
            self.cleanup();
            return;
        };
    }

    fn downloaded(ctx: *anyopaque, result: RemoteAsync.BytesResult) void {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        self.handle = null;
        switch (result) {
            .failure => |err| self.failure = err,
            .value => |bytes| {
                if (self.detached) self.allocator.free(bytes) else self.items.append(self.allocator, .{ .name = bytes, .version = self.pending_version }) catch |err| {
                    self.allocator.free(bytes);
                    self.failure = err;
                };
            },
        }
        self.dropBlob();
    }

    fn dropBlob(self: *DatabaseList) void {
        const blob = self.blob orelse {
            if (self.failure != null or self.detached) self.cleanup() else self.next();
            return;
        };
        self.handle = RemoteAsync.drop(self.allocator, self.client, self.origin, blob, self, dropped) catch |err| {
            if (self.failure == null) self.failure = err;
            self.freeBlob();
            self.cleanup();
            return;
        };
    }

    fn dropped(ctx: *anyopaque, err: ?anyerror) void {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        self.handle = null;
        if (err) |e| if (self.failure == null) {
            self.failure = e;
        };
        self.freeBlob();
        if (self.failure != null or self.detached) self.cleanup() else self.next();
    }

    fn freeBlob(self: *DatabaseList) void {
        if (self.blob) |blob| {
            self.allocator.free(blob.blob_id);
            self.allocator.free(blob.size);
            self.blob = null;
        }
    }

    fn cleanup(self: *DatabaseList) void {
        if (self.blob != null) return self.dropBlob();
        if (self.rows_id) |id| {
            self.cleaning = true;
            self.request_id = RemoteAsync.request(self.allocator, self.client, self.origin, null, "idb.rows_close", .{ .rows_id = id }, self, closed) catch {
                self.client.deinit();
                self.finish();
                return;
            };
            return;
        }
        self.finish();
    }

    fn closed(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        self.request_id = null;
        switch (result) {
            .failure => |err| if (self.failure == null) {
                self.failure = err;
            },
            .reply => |reply| {
                defer reply.deinit();
                if (RemoteGate.responseError(reply.value)) |err| if (self.failure == null) {
                    self.failure = err;
                };
            },
        }
        self.finish();
    }

    fn finish(self: *DatabaseList) void {
        if (self.detached) return self.destroy();
        if (self.reset == self.exec.js.scheduler.resets) self.exec.js.scheduler.blocking -= 1;
        self.scheduled = true;
        self.exec.js.scheduler.add(self, deliver, 0, .{ .name = "IDBFactory.databases", .finalizer = finalize }) catch {
            self.engine.unregisterContextResource(&self.resource);
            self.resolver.deinit();
            self.destroy();
        };
    }

    fn deliver(ctx: *anyopaque) !?u32 {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        if (!self.detached) {
            const exec = self.exec;
            const previous = exec.js.local;
            defer exec.js.local = previous;
            var scope: js.Local.Scope = undefined;
            exec.js.localScope(&scope);
            defer scope.deinit();
            exec.js.local = &scope.local;
            const resolver = self.resolver.local(&scope.local);
            if (self.failure != null) resolver.rejectError("IDBFactory.databases", .{ .generic_error = "IndexedDB listing failed" }) else resolver.resolve("IDBFactory.databases", self.items.items);
            self.resolver.deinit();
            self.engine.unregisterContextResource(&self.resource);
        }
        self.destroy();
        return null;
    }

    fn finalize(ctx: *anyopaque) void {
        const self: *DatabaseList = @ptrCast(@alignCast(ctx));
        if (!self.detached) {
            self.engine.unregisterContextResource(&self.resource);
            self.resolver.deinit();
        }
        self.destroy();
    }

    fn destroy(self: *DatabaseList) void {
        const allocator = self.allocator;
        for (self.items.items) |item| allocator.free(item.name);
        self.items.deinit(allocator);
        self.freeBlob();
        if (self.rows_id) |id| allocator.free(id);
        allocator.free(self.origin);
        allocator.destroy(self);
    }
};

pub fn cmp(_: *IDBFactory, first: js.Value, second: js.Value, exec: *Execution) !i32 {
    const a = try Key.encodeValue(exec.call_arena, first);
    const b = try Key.encodeValue(exec.call_arena, second);
    return switch (std.mem.order(u8, a, b)) {
        .lt => -1,
        .eq => 0,
        .gt => 1,
    };
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(IDBFactory);

    pub const Meta = struct {
        pub const name = "IDBFactory";
        pub var class_id: bridge.ClassId = undefined;
        pub const empty_with_no_proto = true;
    };

    pub const open = bridge.function(IDBFactory.open, .{});
    pub const deleteDatabase = bridge.function(IDBFactory.deleteDatabase, .{});
    pub const databases = bridge.function(IDBFactory.databases, .{});
    pub const cmp = bridge.function(IDBFactory.cmp, .{});
};
