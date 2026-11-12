// Private IndexedDB client over the inherited, authenticated Profile capability.
const std = @import("std");
const ProfileIpc = @import("../ProfileIpc.zig");
const Allocator = std.mem.Allocator;
const Remote = @This();

client: *ProfileIpc.Client,
allocator: Allocator,
origin: []const u8,
ticket: ?[]const u8 = null,

pub const BlobRef = struct { blob_id: []const u8, size: []const u8 };
const chunk_size = 128 * 1024;

pub fn init(allocator: Allocator, client: *ProfileIpc.Client, origin: []const u8) !Remote {
    return .{ .allocator = allocator, .client = client, .origin = try allocator.dupe(u8, origin) };
}

pub fn deinit(self: *Remote) void {
    self.cancel() catch {};
    if (self.ticket) |ticket| self.allocator.free(ticket);
    self.ticket = null;
    self.allocator.free(self.origin);
}

pub fn acquire(self: *Remote) !bool {
    if (self.ticket != null) return error.InvalidArgument;
    const reply = try self.call(struct { ticket: []const u8, granted: bool }, "idb.acquire", struct {}{});
    defer reply.deinit();
    const ticket = self.allocator.dupe(u8, reply.value.ticket) catch |err| {
        self.ticket = reply.value.ticket;
        const cancellation = self.call(?bool, "idb.cancel", struct {}{}) catch null;
        if (cancellation) |parsed| parsed.deinit();
        self.ticket = null;
        return err;
    };
    self.ticket = ticket;
    return reply.value.granted;
}

pub fn poll(self: *Remote) !bool {
    if (self.ticket == null) return error.InvalidArgument;
    const reply = try self.call(struct { granted: bool }, "idb.poll", struct {}{});
    defer reply.deinit();
    return reply.value.granted;
}

pub fn cancel(self: *Remote) !void {
    const ticket = self.ticket orelse return;
    const reply = try self.call(?bool, "idb.cancel", struct {}{});
    reply.deinit();
    self.ticket = null;
    self.allocator.free(ticket);
}

// The returned parsed value owns its arena. Payload cannot override the trusted
// origin or transaction ticket supplied by this Remote.
pub fn call(self: *Remote, comptime T: type, operation: []const u8, payload: anytype) !std.json.Parsed(T) {
    return self.callWithTicket(T, self.ticket, operation, payload);
}

pub fn callWithTicket(self: *Remote, comptime T: type, ticket: ?[]const u8, operation: []const u8, payload: anytype) !std.json.Parsed(T) {
    const encoded = try std.json.Stringify.valueAlloc(self.allocator, payload, .{});
    defer self.allocator.free(encoded);
    var args = try std.json.parseFromSlice(std.json.Value, self.allocator, encoded, .{ .allocate = .alloc_always });
    defer args.deinit();
    if (args.value != .object) return error.ProfileProtocolError;
    try args.value.object.put(args.arena.allocator(), "origin", .{ .string = self.origin });
    try args.value.object.put(args.arena.allocator(), "ticket", if (ticket) |value| .{ .string = value } else .null);
    const reply = try self.client.request(self.allocator, operation, args.value);
    defer reply.deinit();
    const result = reply.value.object.get("result") orelse return error.ProfileProtocolError;
    return std.json.parseFromValue(T, self.allocator, result, .{ .allocate = .alloc_always }) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => error.ProfileProtocolError,
    };
}

pub fn upload(self: *Remote, bytes: []const u8) !BlobRef {
    var size_buf: [32]u8 = undefined;
    const size = try std.fmt.bufPrint(&size_buf, "{d}", .{bytes.len});
    const created = try self.callWithTicket(BlobRef, null, "idb.blob_begin", .{ .size = size });
    defer created.deinit();
    errdefer self.dropBorrowed(created.value);
    const id = try self.allocator.dupe(u8, created.value.blob_id);
    errdefer self.allocator.free(id);
    const owned_size = try self.allocator.dupe(u8, created.value.size);
    errdefer self.allocator.free(owned_size);
    const ref: BlobRef = .{ .blob_id = id, .size = owned_size };
    if (!std.mem.eql(u8, size, ref.size)) return error.ProfileProtocolError;
    var offset: usize = 0;
    while (offset < bytes.len) {
        const part = bytes[offset..@min(bytes.len, offset + @min(chunk_size, bytes.len - offset))];
        const encoded_len = std.base64.standard.Encoder.calcSize(part.len);
        const encoded = try self.allocator.alloc(u8, encoded_len);
        defer self.allocator.free(encoded);
        _ = std.base64.standard.Encoder.encode(encoded, part);
        var offset_buf: [32]u8 = undefined;
        const offset_text = try std.fmt.bufPrint(&offset_buf, "{d}", .{offset});
        const reply = try self.callWithTicket(?bool, null, "idb.blob_append", .{ .blob_id = ref.blob_id, .offset = offset_text, .data = encoded });
        reply.deinit();
        offset += part.len;
    }
    return ref;
}

pub fn download(self: *Remote, allocator: Allocator, ref: BlobRef) ![]u8 {
    const size = std.fmt.parseInt(usize, ref.size, 10) catch return error.ProfileProtocolError;
    const output = try allocator.alloc(u8, size);
    errdefer allocator.free(output);
    var offset: usize = 0;
    while (offset < size) {
        const length = @min(chunk_size, size - offset);
        var offset_buf: [32]u8 = undefined;
        const offset_text = try std.fmt.bufPrint(&offset_buf, "{d}", .{offset});
        const reply = try self.callWithTicket(struct { data: []const u8, size: []const u8 }, null, "idb.blob_read", .{ .blob_id = ref.blob_id, .offset = offset_text, .length = length });
        defer reply.deinit();
        if (!std.mem.eql(u8, reply.value.size, ref.size)) return error.ProfileProtocolError;
        const decoded_len = std.base64.standard.Decoder.calcSizeForSlice(reply.value.data) catch return error.ProfileProtocolError;
        if (decoded_len != length) return error.ProfileProtocolError;
        std.base64.standard.Decoder.decode(output[offset .. offset + length], reply.value.data) catch return error.ProfileProtocolError;
        offset += length;
    }
    return output;
}

pub fn dropBorrowed(self: *Remote, ref: BlobRef) void {
    const reply = self.callWithTicket(?bool, null, "idb.blob_drop", .{ .blob_id = ref.blob_id }) catch return;
    reply.deinit();
}

pub fn drop(self: *Remote, ref: BlobRef) void {
    self.dropBorrowed(ref);
    self.allocator.free(ref.blob_id);
    self.allocator.free(ref.size);
}

pub const Row = struct {
    values: []const []u8,
    pub fn get(self: Row, comptime T: type, index: usize) T {
        if (T != []const u8) @compileError("remote rows only contain blobs");
        return self.values[index];
    }
};

pub const Rows = struct {
    remote: *Remote,
    id: []const u8,
    ticket: ?[]const u8,
    finished: bool = false,
    values: ?[][]u8 = null,

    fn clear(self: *Rows) void {
        if (self.values) |values| {
            for (values) |value| self.remote.allocator.free(value);
            self.remote.allocator.free(values);
            self.values = null;
        }
    }
    pub fn next(self: *Rows) !?Row {
        self.clear();
        if (self.finished) return null;
        const reply = try self.remote.callWithTicket(?[]BlobRef, self.ticket, "idb.rows_next", .{ .rows_id = self.id });
        defer reply.deinit();
        const refs = reply.value orelse {
            self.finished = true;
            return null;
        };
        defer for (refs) |ref| self.remote.dropBorrowed(ref);
        const values = try self.remote.allocator.alloc([]u8, refs.len);
        var count: usize = 0;
        errdefer {
            for (values[0..count]) |value| self.remote.allocator.free(value);
            self.remote.allocator.free(values);
        }
        for (refs, 0..) |ref, i| {
            values[i] = try self.remote.download(self.remote.allocator, ref);
            count += 1;
        }
        self.values = values;
        return .{ .values = values };
    }
    pub fn deinit(self: *Rows) void {
        self.clear();
        const reply = self.remote.callWithTicket(?bool, self.ticket, "idb.rows_close", .{ .rows_id = self.id }) catch null;
        if (reply) |parsed| parsed.deinit();
        if (self.ticket) |ticket| self.remote.allocator.free(ticket);
        self.remote.allocator.free(self.id);
    }
};

pub fn rows(self: *Remote, ticket: ?[]const u8, id: []const u8) !Rows {
    errdefer {
        const reply = self.callWithTicket(?bool, ticket, "idb.rows_close", .{ .rows_id = id }) catch null;
        if (reply) |parsed| parsed.deinit();
    }
    const owned_id = try self.allocator.dupe(u8, id);
    errdefer self.allocator.free(owned_id);
    return .{ .remote = self, .id = owned_id, .ticket = if (ticket) |value| try self.allocator.dupe(u8, value) else null };
}
