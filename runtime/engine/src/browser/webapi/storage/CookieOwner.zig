// Cookie operations executed by the Profile owner, against its one native Jar.
// Dispatch is synchronous: the owner must serialize calls and must not expose
// the Jar to another writer. Results are JSON values, not protocol envelopes.
const std = @import("std");
const lp = @import("lightpanda");
const Cookie = @import("Cookie.zig");
const CdpStorage = @import("../../../server/cdp/domains/storage.zig");
const BidiStorage = @import("../../../server/bidi/storage.zig");
const CookieStoreOwner = @import("CookieStoreOwner.zig");
const URL = @import("../../URL.zig");
const Allocator = std.mem.Allocator;

const Header = struct {
    url: []const u8,
    origin_url: ?[]const u8,
    is_http: bool,
    kind: Cookie.RequestKind = .subresource,
};
const Set = struct {
    url: []const u8,
    header: []const u8,
    is_http: bool,
    // A null site is the cross-site ancestor-chain sentinel used by Document.
    origin_url: ?[]const u8,
};
const Delete = struct {
    name: []const u8,
    domain: ?[]const u8 = null,
    path: ?[]const u8 = null,
    url: ?[]const u8 = null,
};
const Row = struct {
    name: []const u8,
    value: []const u8,
    domain: []const u8,
    path: []const u8,
    expires: ?f64,
    secure: bool,
    http_only: bool,
    same_site: Cookie.SameSite,
    same_site_default: bool,
    creation_time: u64,

    pub fn jsonStringify(self: Row, writer: anytype) !void {
        try writer.beginArray();
        try writer.write(self.name);
        try writer.write(self.value);
        try writer.write(self.domain);
        try writer.write(self.path);
        try writer.write(self.expires);
        try writer.write(@as(u8, @intFromBool(self.secure)));
        try writer.write(@as(u8, @intFromBool(self.http_only)));
        try writer.write(@tagName(self.same_site));
        try writer.write(@as(u8, @intFromBool(self.same_site_default)));
        try writer.write(self.creation_time);
        try writer.endArray();
    }
};

const EventCollector = struct {
    arena: std.heap.ArenaAllocator,
    events: std.ArrayList(lp.Notification.CookieChanged) = .empty,
    failed: bool = false,

    fn reset(self: *EventCollector) void {
        self.events = .empty;
        _ = self.arena.reset(.retain_capacity);
        self.failed = false;
    }

    fn onChange(ctx: *anyopaque, event: *const lp.Notification.CookieChanged) !void {
        const self: *EventCollector = @ptrCast(@alignCast(ctx));
        if (self.failed) return;
        if (self.events.items.len >= 1024) {
            self.failed = true;
            return;
        }
        const a = self.arena.allocator();
        const name = a.dupe(u8, event.name) catch {
            self.failed = true;
            return;
        };
        const value = a.dupe(u8, event.value) catch {
            self.failed = true;
            return;
        };
        const domain = a.dupe(u8, event.domain) catch {
            self.failed = true;
            return;
        };
        const path = a.dupe(u8, event.path) catch {
            self.failed = true;
            return;
        };
        var copy = event.*;
        copy.name = name;
        copy.value = value;
        copy.domain = domain;
        copy.path = path;
        self.events.append(a, copy) catch {
            self.failed = true;
        };
    }
};

const max_message = @import("ProfileIpc.zig").max_message;

fn readExact(fd: std.posix.fd_t, bytes: []u8) !bool {
    var offset: usize = 0;
    while (offset < bytes.len) {
        const n = std.c.read(fd, bytes.ptr + offset, bytes.len - offset);
        if (n == 0 and offset == 0) return false;
        if (n <= 0) return error.ProfileServiceLost;
        offset += @intCast(n);
    }
    return true;
}

fn sendFrame(fd: std.posix.fd_t, bytes: []const u8) !void {
    if (bytes.len == 0 or bytes.len > max_message) return error.ProfileMessageTooLarge;
    var prefix: [4]u8 = undefined;
    std.mem.writeInt(u32, &prefix, @intCast(bytes.len), .big);
    for ([_][]const u8{ &prefix, bytes }) |part| {
        var offset: usize = 0;
        while (offset < part.len) {
            const n = std.c.send(fd, part.ptr + offset, part.len - offset, std.c.MSG.NOSIGNAL);
            if (n <= 0) return error.ProfileServiceLost;
            offset += @intCast(n);
        }
    }
}

// Events have independent frames: a legal bulk mutation cannot overflow its
// reply merely because the aggregate event payload exceeds the RPC ceiling.
fn sendEvents(fd: std.posix.fd_t, id: i64, allocator: Allocator, events: []const lp.Notification.CookieChanged) !void {
    for (events) |event| {
        const frame = try std.json.Stringify.valueAlloc(allocator, .{ .id = id, .cookie_event = event }, .{});
        defer allocator.free(frame);
        try sendFrame(fd, frame);
    }
}

/// The descriptor is a private socket inherited from the lease owner. The
/// lease owner must keep the exclusive Profile lease for this host's lifetime.
pub fn serve(allocator: Allocator, fd: std.posix.fd_t, database: [:0]const u8) !void {
    defer _ = std.c.close(fd);
    var jar = Cookie.Jar.init(allocator, null);
    defer jar.deinit();
    if (!std.mem.eql(u8, std.fs.path.basename(database), "cookies.sqlite")) return error.InvalidProfileOwner;
    try lp.cookies.restoreProfile(&jar, std.fs.path.dirname(database) orelse return error.InvalidProfileOwner);
    const notification = try lp.Notification.init(allocator);
    defer notification.deinit();
    var collector = EventCollector{ .arena = std.heap.ArenaAllocator.init(allocator) };
    defer collector.arena.deinit();
    try notification.register(.cookie_changed, &collector, EventCollector.onChange);
    jar.notification = notification;
    try sendFrame(fd, "{\"ready\":true}");
    var last_id: i64 = 0;
    while (true) {
        collector.reset();
        var prefix: [4]u8 = undefined;
        if (!try readExact(fd, &prefix)) return;
        const size = std.mem.readInt(u32, &prefix, .big);
        if (size == 0 or size > max_message) return error.ProfileProtocolError;
        const bytes = try allocator.alloc(u8, size);
        defer allocator.free(bytes);
        if (!try readExact(fd, bytes)) return error.ProfileProtocolError;
        const request = try std.json.parseFromSlice(std.json.Value, allocator, bytes, .{});
        defer request.deinit();
        if (request.value != .object) return error.ProfileProtocolError;
        const obj = request.value.object;
        if (obj.count() != 4) return error.ProfileProtocolError;
        const version = obj.get("version") orelse return error.ProfileProtocolError;
        const id = obj.get("id") orelse return error.ProfileProtocolError;
        const operation = obj.get("operation") orelse return error.ProfileProtocolError;
        const payload = obj.get("payload") orelse return error.ProfileProtocolError;
        if (version != .integer or version.integer != 1 or id != .integer or id.integer <= last_id or
            operation != .string or payload != .object) return error.ProfileProtocolError;
        last_id = id.integer;
        const result = dispatch(&jar, allocator, operation.string, payload) catch |err| {
            if (collector.failed) return error.ProfileCookieEventOverflow;
            try sendEvents(fd, id.integer, allocator, collector.events.items);
            const reply = try std.json.Stringify.valueAlloc(allocator, .{
                .id = id.integer,
                .ok = false,
                .@"error" = @errorName(err),
            }, .{});
            defer allocator.free(reply);
            try sendFrame(fd, reply);
            continue;
        };
        defer allocator.free(result);
        if (collector.failed) return error.ProfileCookieEventOverflow;
        try sendEvents(fd, id.integer, allocator, collector.events.items);
        const parsed = try std.json.parseFromSlice(std.json.Value, allocator, result, .{});
        defer parsed.deinit();
        const reply = try std.json.Stringify.valueAlloc(allocator, .{
            .id = id.integer,
            .ok = true,
            .result = parsed.value,
        }, .{});
        defer allocator.free(reply);
        try sendFrame(fd, reply);
    }
}

/// Caller owns the returned JSON bytes. An error must produce an error reply;
/// it must never cause the caller to fall back to a local writable jar.
pub fn dispatch(jar: *Cookie.Jar, allocator: Allocator, operation: []const u8, payload: std.json.Value) ![]u8 {
    if (std.mem.eql(u8, operation, "cookies.store_get") or std.mem.eql(u8, operation, "cookies.store_set")) {
        return CookieStoreOwner.dispatch(jar, allocator, operation, payload);
    }
    if (std.mem.eql(u8, operation, "cookies.header")) {
        const p = try std.json.parseFromValue(Header, allocator, payload, .{});
        defer p.deinit();
        const url = try allocator.dupeSentinel(u8, p.value.url, 0);
        defer allocator.free(url);
        const origin = if (p.value.origin_url) |v| try allocator.dupeSentinel(u8, v, 0) else null;
        defer if (origin) |v| allocator.free(v);
        var output: std.Io.Writer.Allocating = .init(allocator);
        defer output.deinit();
        try jar.forRequest(url, &output.writer, .{
            .is_http = p.value.is_http,
            .kind = p.value.kind,
            .origin_url = if (origin) |v| .{ .url = v } else .none,
        });
        return std.json.Stringify.valueAlloc(allocator, output.written(), .{});
    }
    if (std.mem.eql(u8, operation, "cookies.set")) {
        const p = try std.json.parseFromValue(Set, allocator, payload, .{});
        defer p.deinit();
        const url = try allocator.dupeSentinel(u8, p.value.url, 0);
        defer allocator.free(url);
        const cookie = Cookie.parse(jar.allocator, url, p.value.header) catch |err| switch (err) {
            error.OutOfMemory => return err,
            else => return std.json.Stringify.valueAlloc(allocator, false, .{}),
        };
        if (!p.value.is_http and (cookie.http_only or (cookie.same_site != .none and p.value.origin_url == null))) {
            cookie.deinit();
            return std.json.Stringify.valueAlloc(allocator, false, .{});
        }
        if (p.value.is_http) {
            try jar.add(cookie, lp.datetime.timestamp(.real), true);
        } else {
            try jar.add(cookie, lp.datetime.timestamp(.real), false);
        }
        return std.json.Stringify.valueAlloc(allocator, true, .{});
    }
    if (std.mem.eql(u8, operation, "cookies.cdp_set")) {
        const p = try std.json.parseFromValue(struct { cookies: []const CdpStorage.CdpCookie }, allocator, payload, .{});
        defer p.deinit();
        if (p.value.cookies.len > 1024) return error.CookieBatchSizeExceeded;
        const count = try CdpStorage.setCdpCookies(jar, p.value.cookies);
        return std.json.Stringify.valueAlloc(allocator, count, .{});
    }
    if (std.mem.eql(u8, operation, "cookies.cdp_get")) {
        const p = try std.json.parseFromValue(struct { urls: ?[]const []const u8 = null }, allocator, payload, .{});
        defer p.deinit();
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        const a = arena.allocator();
        var prepared: ?[]Cookie.PreparedUri = null;
        if (p.value.urls) |urls| {
            const values = try a.alloc(Cookie.PreparedUri, urls.len);
            for (urls, 0..) |url, i| values[i] = Cookie.PreparedUri.init(try a.dupeSentinel(u8, url, 0));
            prepared = values;
        }
        try jar.removeExpired(null);
        const writer = CdpStorage.CookieWriter{ .cookies = jar.cookies.items, .urls = prepared };
        return std.json.Stringify.valueAlloc(allocator, writer, .{});
    }
    if (std.mem.eql(u8, operation, "cookies.fields_set")) {
        const p = try std.json.parseFromValue(struct { fields: Cookie.Fields, url: ?[:0]const u8 }, allocator, payload, .{});
        defer p.deinit();
        try BidiStorage.add(jar, p.value.fields, p.value.url);
        return allocator.dupe(u8, "null");
    }
    if (std.mem.eql(u8, operation, "cookies.bidi_get") or std.mem.eql(u8, operation, "cookies.bidi_delete")) {
        const p = try std.json.parseFromValue(struct { filter: BidiStorage.Filter }, allocator, payload, .{});
        defer p.deinit();
        if (std.mem.eql(u8, operation, "cookies.bidi_delete")) {
            try BidiStorage.remove(jar, &p.value.filter, BidiStorage.Filter.matches);
            return allocator.dupe(u8, "null");
        }
        try jar.removeExpired(null);
        var cookies: std.ArrayList(BidiStorage.BiDiCookie) = .empty;
        defer cookies.deinit(allocator);
        for (jar.cookies.items) |*cookie| {
            if (p.value.filter.matches(cookie)) try cookies.append(allocator, .{ .cookie = cookie });
        }
        return std.json.Stringify.valueAlloc(allocator, cookies.items, .{});
    }
    if (std.mem.eql(u8, operation, "cookies.associated_get") or std.mem.eql(u8, operation, "cookies.associated_delete")) {
        const p = try std.json.parseFromValue(struct { url: [:0]const u8, name: ?[]const u8 }, allocator, payload, .{});
        defer p.deinit();
        const associated = BidiStorage.Associated.init(p.value.url, p.value.name);
        if (std.mem.eql(u8, operation, "cookies.associated_delete")) {
            try BidiStorage.remove(jar, &associated, BidiStorage.Associated.has);
            return allocator.dupe(u8, "null");
        }
        try jar.removeExpired(null);
        var cookies: std.ArrayList(BidiStorage.HttpCookie) = .empty;
        defer cookies.deinit(allocator);
        for (jar.cookies.items) |*cookie| {
            if (associated.has(cookie)) try cookies.append(allocator, .{ .cookie = cookie });
        }
        return std.json.Stringify.valueAlloc(allocator, cookies.items, .{});
    }
    if (std.mem.eql(u8, operation, "cookies.tool_get")) {
        const p = try std.json.parseFromValue(struct { url: ?[]const u8, all: bool }, allocator, payload, .{});
        defer p.deinit();
        if (jar.cookies.items.len == 0) return std.json.Stringify.valueAlloc(allocator, "No cookies.", .{});
        if (!p.value.all and p.value.url == null) return std.json.Stringify.valueAlloc(allocator, "No current page. Pass `url` to filter by host or `all=true` to list every cookie.", .{});
        const host = if (!p.value.all) URL.getHostname(p.value.url.?) else null;
        var output: std.Io.Writer.Allocating = .init(allocator);
        defer output.deinit();
        var count: usize = 0;
        for (jar.cookies.items) |*cookie| {
            if (host) |h| if (!cookie.matchesHost(h)) continue;
            try output.writer.print("{s}={s}; domain={s}; path={s}", .{ cookie.name, cookie.value, cookie.domain, cookie.path });
            if (cookie.secure) try output.writer.writeAll("; Secure");
            if (cookie.http_only) try output.writer.writeAll("; HttpOnly");
            try output.writer.writeAll("\n");
            count += 1;
        }
        if (count == 0) {
            const text = try std.fmt.allocPrint(allocator, "No cookies for {s}.", .{p.value.url orelse "(unfiltered)"});
            defer allocator.free(text);
            return std.json.Stringify.valueAlloc(allocator, text, .{});
        }
        return std.json.Stringify.valueAlloc(allocator, output.written(), .{});
    }
    if (std.mem.eql(u8, operation, "cookies.clear")) {
        const p = try std.json.parseFromValue(struct {}, allocator, payload, .{});
        defer p.deinit();
        try jar.clearRetainingCapacity();
        return allocator.dupe(u8, "null");
    }
    if (std.mem.eql(u8, operation, "cookies.delete")) {
        const p = try std.json.parseFromValue(Delete, allocator, payload, .{});
        defer p.deinit();
        if (jar.cookies.items.len == 0) return allocator.dupe(u8, "null");
        const url = if (p.value.url) |v| try allocator.dupeSentinel(u8, v, 0) else null;
        defer if (url) |v| allocator.free(v);
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        const domain = try Cookie.parseDomain(arena.allocator(), url, p.value.domain);
        const path = try Cookie.parsePath(arena.allocator(), url, p.value.path);
        const wanted = std.mem.trimStart(u8, domain, ".");
        var i = jar.cookies.items.len;
        while (i > 0) {
            i -= 1;
            const c = &jar.cookies.items[i];
            if (!std.mem.eql(u8, c.name, p.value.name)) continue;
            const actual = std.mem.trimStart(u8, c.domain, ".");
            if (!std.mem.eql(u8, actual, wanted) or !std.mem.eql(u8, c.path, path)) continue;
            try jar.removeAt(i);
        }
        return allocator.dupe(u8, "null");
    }
    if (std.mem.eql(u8, operation, "cookies.snapshot")) {
        const p = try std.json.parseFromValue(struct {}, allocator, payload, .{});
        defer p.deinit();
        try jar.removeExpired(null);
        var rows: std.ArrayList(Row) = .empty;
        defer rows.deinit(allocator);
        for (jar.cookies.items) |c| try rows.append(allocator, .{
            .name = c.name,
            .value = c.value,
            .domain = c.domain,
            .path = c.path,
            .expires = c.expires,
            .secure = c.secure,
            .http_only = c.http_only,
            .same_site = c.same_site,
            .same_site_default = c.same_site_default,
            .creation_time = c.creation_time,
        });
        return std.json.Stringify.valueAlloc(allocator, rows.items, .{});
    }
    return error.UnsupportedCookieOperation;
}

test "owner serializes HTTP and script cookie semantics" {
    const a = std.testing.allocator;
    var jar = Cookie.Jar.init(a, null);
    defer jar.deinit();
    const http = try std.json.parseFromSlice(std.json.Value, a,
        \\{"url":"https://example.test/a","header":"sid=secret; HttpOnly; Path=/","is_http":true,"origin_url":null}
    , .{});
    defer http.deinit();
    const first = try dispatch(&jar, a, "cookies.set", http.value);
    defer a.free(first);
    try std.testing.expectEqualStrings("true", first);

    const script = try std.json.parseFromSlice(std.json.Value, a,
        \\{"url":"https://example.test/a","header":"sid=stolen; Path=/","is_http":false,"origin_url":"https://example.test/"}
    , .{});
    defer script.deinit();
    const second = try dispatch(&jar, a, "cookies.set", script.value);
    defer a.free(second);
    try std.testing.expectEqualStrings("true", second);

    const header = try std.json.parseFromSlice(std.json.Value, a,
        \\{"url":"https://example.test/a","origin_url":"https://example.test/","is_http":true,"kind":"subresource"}
    , .{});
    defer header.deinit();
    const result = try dispatch(&jar, a, "cookies.header", header.value);
    defer a.free(result);
    try std.testing.expectEqualStrings("\"sid=secret\"", result);
}

test "owner observes ordered changes and suppresses identical replacements" {
    const a = std.testing.allocator;
    const notification = try lp.Notification.init(a);
    defer notification.deinit();
    var collector = EventCollector{ .arena = std.heap.ArenaAllocator.init(a) };
    defer collector.arena.deinit();
    try notification.register(.cookie_changed, &collector, EventCollector.onChange);
    var jar = Cookie.Jar.init(a, notification);
    defer jar.deinit();
    try jar.populateFromResponse("https://example.test/", "a=1; Path=/");
    try jar.populateFromResponse("https://example.test/", "a=1; Path=/");
    try jar.populateFromResponse("https://example.test/", "a=2; Path=/");
    try jar.clearRetainingCapacity();
    try std.testing.expect(!collector.failed);
    try std.testing.expectEqual(@as(usize, 3), collector.events.items.len);
    try std.testing.expectEqual(lp.Notification.CookieChanged.Kind.changed, collector.events.items[0].kind);
    try std.testing.expectEqualStrings("1", collector.events.items[0].value);
    try std.testing.expectEqualStrings("2", collector.events.items[1].value);
    try std.testing.expectEqual(lp.Notification.CookieChanged.Kind.deleted, collector.events.items[2].kind);
}
