// Resumable, chunked IndexedDB codec over the dedicated Profile channel.
const std = @import("std");
const ProfileAsync = @import("../ProfileAsync.zig");
const Gate = @import("RemoteGate.zig");
const Allocator = std.mem.Allocator;
pub const BlobRef = @import("Remote.zig").BlobRef;
pub const BlobResult = union(enum) { value: BlobRef, failure: anyerror };
pub const BytesResult = union(enum) { value: []u8, failure: anyerror };
const chunk_size = 128 * 1024;

// Valid until the completion callback starts. cancel() may invoke that callback
// synchronously; the callback must clear any stored handle before returning.
pub const Handle = struct {
    client: *ProfileAsync.Client,
    id: u64 = 0,
    child: ?*Handle = null,
    pub fn cancel(self: *Handle) void {
        if (self.child) |child| child.cancel() else self.client.cancel(self.id);
    }
};

pub fn request(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ticket: ?[]const u8, operation: []const u8, payload: anytype, ctx: *anyopaque, callback: ProfileAsync.Callback) !u64 {
    const encoded = try std.json.Stringify.valueAlloc(allocator, payload, .{});
    defer allocator.free(encoded);
    var args = try std.json.parseFromSlice(std.json.Value, allocator, encoded, .{ .allocate = .alloc_always });
    defer args.deinit();
    if (args.value != .object) return error.ProfileProtocolError;
    try args.value.object.put(args.arena.allocator(), "origin", .{ .string = origin });
    try args.value.object.put(args.arena.allocator(), "ticket", if (ticket) |t| .{ .string = t } else .null);
    return client.submit(operation, args.value, ctx, callback);
}

fn resultValue(reply: std.json.Value) !std.json.Value {
    if (Gate.responseError(reply)) |err| return err;
    return reply.object.get("result") orelse error.ProfileProtocolError;
}

const Upload = struct {
    allocator: Allocator,
    client: *ProfileAsync.Client,
    handle: Handle,
    origin: []u8,
    bytes: []u8,
    offset: usize = 0,
    ref: ?BlobRef = null,
    ctx: *anyopaque,
    done: *const fn (*anyopaque, BlobResult) void,
    failure: ?anyerror = null,

    fn destroy(self: *Upload) void {
        self.allocator.free(self.origin);
        self.allocator.free(self.bytes);
        self.allocator.destroy(self);
    }
    fn fail(self: *Upload, err: anyerror) void {
        self.failure = err;
        if (self.ref) |ref| {
            self.handle.id = request(self.allocator, self.client, self.origin, null, "idb.blob_drop", .{ .blob_id = ref.blob_id }, self, dropped) catch {
                self.client.deinit();
                self.finishFailure();
                return;
            };
        } else self.finishFailure();
    }
    fn finishFailure(self: *Upload) void {
        if (self.ref) |ref| {
            self.allocator.free(ref.blob_id);
            self.allocator.free(ref.size);
        }
        const callback = self.done;
        const ctx = self.ctx;
        const err = self.failure orelse error.ProfileProtocolError;
        self.destroy();
        callback(ctx, .{ .failure = err });
    }
    fn dropped(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *Upload = @ptrCast(@alignCast(ctx));
        switch (result) {
            .reply => |reply| reply.deinit(),
            .failure => self.client.deinit(),
        }
        self.finishFailure();
    }
    fn begun(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *Upload = @ptrCast(@alignCast(ctx));
        const reply = switch (result) {
            .failure => |err| {
                if (err == error.ProfileSideEffectUnknown) self.client.deinit();
                self.fail(err);
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        const value = resultValue(reply.value) catch |err| {
            self.fail(err);
            return;
        };
        const parsed = std.json.parseFromValue(BlobRef, self.allocator, value, .{ .allocate = .alloc_always }) catch {
            self.client.deinit();
            self.fail(error.ProfileProtocolError);
            return;
        };
        defer parsed.deinit();
        const id = self.allocator.dupe(u8, parsed.value.blob_id) catch {
            self.client.deinit();
            self.fail(error.OutOfMemory);
            return;
        };
        const size = self.allocator.dupe(u8, parsed.value.size) catch {
            self.allocator.free(id);
            self.client.deinit();
            self.fail(error.OutOfMemory);
            return;
        };
        self.ref = .{ .blob_id = id, .size = size };
        const actual = std.fmt.parseInt(usize, size, 10) catch {
            self.fail(error.ProfileProtocolError);
            return;
        };
        if (actual != self.bytes.len) {
            self.fail(error.ProfileProtocolError);
            return;
        }
        self.advance();
    }
    fn advance(self: *Upload) void {
        if (self.offset == self.bytes.len) {
            const ref = self.ref.?;
            const callback = self.done;
            const ctx = self.ctx;
            self.destroy();
            callback(ctx, .{ .value = ref });
            return;
        }
        const part = self.bytes[self.offset .. self.offset + @min(chunk_size, self.bytes.len - self.offset)];
        const encoded = self.allocator.alloc(u8, std.base64.standard.Encoder.calcSize(part.len)) catch {
            self.fail(error.OutOfMemory);
            return;
        };
        defer self.allocator.free(encoded);
        _ = std.base64.standard.Encoder.encode(encoded, part);
        var buf: [32]u8 = undefined;
        const offset = std.fmt.bufPrint(&buf, "{d}", .{self.offset}) catch unreachable;
        self.handle.id = request(self.allocator, self.client, self.origin, null, "idb.blob_append", .{ .blob_id = self.ref.?.blob_id, .offset = offset, .data = encoded }, self, appended) catch |err| {
            self.fail(err);
            return;
        };
        self.offset += part.len;
    }
    fn appended(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *Upload = @ptrCast(@alignCast(ctx));
        const reply = switch (result) {
            .failure => |err| {
                self.fail(err);
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        const value = resultValue(reply.value) catch |err| {
            self.fail(err);
            return;
        };
        if (value != .null) {
            self.fail(error.ProfileProtocolError);
            return;
        }
        self.advance();
    }
};

pub fn upload(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, bytes: []const u8, ctx: *anyopaque, done: *const fn (*anyopaque, BlobResult) void) !*Handle {
    const self = try allocator.create(Upload);
    errdefer allocator.destroy(self);
    const owned_origin = try allocator.dupe(u8, origin);
    errdefer allocator.free(owned_origin);
    const owned_bytes = try allocator.dupe(u8, bytes);
    errdefer allocator.free(owned_bytes);
    self.* = .{ .allocator = allocator, .client = client, .handle = .{ .client = client }, .origin = owned_origin, .bytes = owned_bytes, .ctx = ctx, .done = done };
    var buf: [32]u8 = undefined;
    const size = try std.fmt.bufPrint(&buf, "{d}", .{bytes.len});
    self.handle.id = try request(allocator, client, origin, null, "idb.blob_begin", .{ .size = size }, self, Upload.begun);
    return &self.handle;
}

const Download = struct {
    allocator: Allocator,
    client: *ProfileAsync.Client,
    handle: Handle,
    origin: []u8,
    id: []u8,
    size: []u8,
    bytes: []u8,
    offset: usize = 0,
    ctx: *anyopaque,
    done: *const fn (*anyopaque, BytesResult) void,

    fn finish(self: *Download, err: ?anyerror) void {
        const callback = self.done;
        const ctx = self.ctx;
        const bytes = self.bytes;
        const allocator = self.allocator;
        allocator.free(self.origin);
        allocator.free(self.id);
        allocator.free(self.size);
        allocator.destroy(self);
        if (err) |failure| {
            allocator.free(bytes);
            callback(ctx, .{ .failure = failure });
        } else callback(ctx, .{ .value = bytes });
    }
    fn advance(self: *Download) void {
        if (self.offset == self.bytes.len) {
            self.finish(null);
            return;
        }
        var buf: [32]u8 = undefined;
        const offset = std.fmt.bufPrint(&buf, "{d}", .{self.offset}) catch unreachable;
        const len = @min(chunk_size, self.bytes.len - self.offset);
        self.handle.id = request(self.allocator, self.client, self.origin, null, "idb.blob_read", .{ .blob_id = self.id, .offset = offset, .length = len }, self, readDone) catch |err| {
            self.finish(err);
            return;
        };
    }
    fn readDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *Download = @ptrCast(@alignCast(ctx));
        const reply = switch (result) {
            .failure => |err| {
                self.finish(err);
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        const value = resultValue(reply.value) catch |err| {
            self.finish(err);
            return;
        };
        const parsed = std.json.parseFromValue(struct { data: []const u8, size: []const u8 }, self.allocator, value, .{ .allocate = .alloc_always }) catch {
            self.finish(error.ProfileProtocolError);
            return;
        };
        defer parsed.deinit();
        const len = @min(chunk_size, self.bytes.len - self.offset);
        const decoded = std.base64.standard.Decoder.calcSizeForSlice(parsed.value.data) catch {
            self.finish(error.ProfileProtocolError);
            return;
        };
        if (decoded != len or !std.mem.eql(u8, self.size, parsed.value.size)) {
            self.finish(error.ProfileProtocolError);
            return;
        }
        std.base64.standard.Decoder.decode(self.bytes[self.offset .. self.offset + len], parsed.value.data) catch {
            self.finish(error.ProfileProtocolError);
            return;
        };
        self.offset += len;
        self.advance();
    }
};

pub fn download(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ref: BlobRef, ctx: *anyopaque, done: *const fn (*anyopaque, BytesResult) void) !*Handle {
    const length = std.fmt.parseInt(usize, ref.size, 10) catch return error.ProfileProtocolError;
    const self = try allocator.create(Download);
    errdefer allocator.destroy(self);
    const own_origin = try allocator.dupe(u8, origin);
    errdefer allocator.free(own_origin);
    const id = try allocator.dupe(u8, ref.blob_id);
    errdefer allocator.free(id);
    const size = try allocator.dupe(u8, ref.size);
    errdefer allocator.free(size);
    const bytes = try allocator.alloc(u8, length);
    errdefer allocator.free(bytes);
    self.* = .{ .allocator = allocator, .client = client, .handle = .{ .client = client }, .origin = own_origin, .id = id, .size = size, .bytes = bytes, .ctx = ctx, .done = done };
    // Even empty blobs must complete asynchronously; use a zero-length read.
    var buf: [32]u8 = undefined;
    const offset = try std.fmt.bufPrint(&buf, "{d}", .{@as(usize, 0)});
    self.handle.id = try request(allocator, client, origin, null, "idb.blob_read", .{ .blob_id = id, .offset = offset, .length = @min(chunk_size, length) }, self, Download.readDone);
    return &self.handle;
}

const Drop = struct { allocator: Allocator, handle: Handle, ctx: *anyopaque, done: *const fn (*anyopaque, ?anyerror) void };
pub fn drop(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ref: BlobRef, ctx: *anyopaque, done: *const fn (*anyopaque, ?anyerror) void) !*Handle {
    const state = try allocator.create(Drop);
    errdefer allocator.destroy(state);
    state.* = .{ .allocator = allocator, .handle = .{ .client = client }, .ctx = ctx, .done = done };
    state.handle.id = try request(allocator, client, origin, null, "idb.blob_drop", .{ .blob_id = ref.blob_id }, state, dropDone);
    return &state.handle;
}
fn dropDone(ctx: *anyopaque, result: ProfileAsync.Result) void {
    const state: *Drop = @ptrCast(@alignCast(ctx));
    var err: ?anyerror = null;
    switch (result) {
        .failure => |failure| err = failure,
        .reply => |reply| {
            defer reply.deinit();
            const value = resultValue(reply.value) catch |failure| blk: {
                err = failure;
                break :blk @as(std.json.Value, .null);
            };
            if (value != .null) err = error.ProfileProtocolError;
        },
    }
    const callback = state.done;
    const target = state.ctx;
    const allocator = state.allocator;
    allocator.destroy(state);
    callback(target, err);
}

pub const RowResult = union(enum) { value: ?[][]u8, failure: anyerror };
const RowRead = struct {
    allocator: Allocator,
    client: *ProfileAsync.Client,
    handle: Handle,
    origin: []u8,
    ctx: *anyopaque,
    done: *const fn (*anyopaque, RowResult) void,
    refs: ?std.json.Parsed([]BlobRef) = null,
    values: ?[][]u8 = null,
    index: usize = 0,
    count: usize = 0,
    failure: ?anyerror = null,

    fn finish(self: *RowRead, eof: bool) void {
        const allocator = self.allocator;
        const callback = self.done;
        const ctx = self.ctx;
        const failure = self.failure;
        const values = self.values;
        if (failure != null) if (values) |columns| {
            for (columns[0..self.count]) |column| allocator.free(column);
            allocator.free(columns);
        };
        if (self.refs) |refs| refs.deinit();
        allocator.free(self.origin);
        allocator.destroy(self);
        if (failure) |err| callback(ctx, .{ .failure = err }) else callback(ctx, .{ .value = if (eof) null else values });
    }
    fn start(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *RowRead = @ptrCast(@alignCast(ctx));
        const reply = switch (result) {
            .failure => |err| {
                if (err == error.ProfileSideEffectUnknown) self.client.deinit();
                self.failure = err;
                self.finish(false);
                return;
            },
            .reply => |value| value,
        };
        defer reply.deinit();
        const value = resultValue(reply.value) catch |err| {
            self.failure = err;
            self.finish(false);
            return;
        };
        if (value == .null) {
            self.finish(true);
            return;
        }
        self.refs = std.json.parseFromValue([]BlobRef, self.allocator, value, .{ .allocate = .alloc_always }) catch {
            // The owner may have allocated handles that could not be decoded.
            self.client.deinit();
            self.failure = error.ProfileProtocolError;
            self.finish(false);
            return;
        };
        self.values = self.allocator.alloc([]u8, self.refs.?.value.len) catch {
            self.failure = error.OutOfMemory;
            self.cleanup();
            return;
        };
        self.readNext();
    }
    fn readNext(self: *RowRead) void {
        const refs = self.refs.?.value;
        if (self.index == refs.len) {
            self.index = 0;
            self.cleanup();
            return;
        }
        self.handle.child = download(self.allocator, self.client, self.origin, refs[self.index], self, readDone) catch |err| {
            self.failure = err;
            self.index = 0;
            self.cleanup();
            return;
        };
    }
    fn readDone(ctx: *anyopaque, result: BytesResult) void {
        const self: *RowRead = @ptrCast(@alignCast(ctx));
        self.handle.child = null;
        switch (result) {
            .failure => |err| {
                self.failure = err;
                self.index = 0;
                self.cleanup();
            },
            .value => |bytes| {
                self.values.?[self.index] = bytes;
                self.count += 1;
                self.index += 1;
                self.readNext();
            },
        }
    }
    fn cleanup(self: *RowRead) void {
        const refs = self.refs.?.value;
        while (self.index < refs.len) {
            self.handle.child = drop(self.allocator, self.client, self.origin, refs[self.index], self, rowDropDone) catch |err| {
                if (self.failure == null) self.failure = err;
                // The remaining resources cannot safely be reclaimed on a
                // channel that refuses cleanup.
                self.client.deinit();
                self.finish(false);
                return;
            };
            return;
        }
        self.finish(false);
    }
    fn rowDropDone(ctx: *anyopaque, err: ?anyerror) void {
        const self: *RowRead = @ptrCast(@alignCast(ctx));
        self.handle.child = null;
        if (err) |failure| {
            if (self.failure == null) self.failure = failure;
            self.client.deinit();
            self.finish(false);
            return;
        }
        self.index += 1;
        self.cleanup();
    }
};

pub fn rowsNext(allocator: Allocator, client: *ProfileAsync.Client, origin: []const u8, ticket: ?[]const u8, rows_id: []const u8, ctx: *anyopaque, done: *const fn (*anyopaque, RowResult) void) !*Handle {
    const self = try allocator.create(RowRead);
    errdefer allocator.destroy(self);
    const own_origin = try allocator.dupe(u8, origin);
    errdefer allocator.free(own_origin);
    self.* = .{ .allocator = allocator, .client = client, .handle = .{ .client = client }, .origin = own_origin, .ctx = ctx, .done = done };
    self.handle.id = try request(allocator, client, origin, ticket, "idb.rows_next", .{ .rows_id = rows_id }, self, RowRead.start);
    return &self.handle;
}
