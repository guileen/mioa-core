// CookieStore operations executed synchronously by the Profile cookie owner.
const std = @import("std");
const Cookie = @import("Cookie.zig");
const CookieStore = @import("CookieStore.zig");
const Allocator = std.mem.Allocator;

const Query = struct {
    url: []const u8,
    site: ?[]const u8,
    name: ?[]const u8,
    first_only: bool,
};
const Mutation = struct {
    url: []const u8,
    site: ?[]const u8,
    init: CookieStore.CookieInit,
    is_delete: bool,
};

pub fn dispatch(jar: *Cookie.Jar, allocator: Allocator, operation: []const u8, payload: std.json.Value) ![]u8 {
    if (jar.profile_client != null) return error.ProfileCookieOperationUnsupported;
    if (std.mem.eql(u8, operation, "cookies.store_get")) {
        const parsed = try std.json.parseFromValue(Query, allocator, payload, .{});
        defer parsed.deinit();
        const p = parsed.value;
        const url = try allocator.dupeSentinel(u8, p.url, 0);
        defer allocator.free(url);
        const site = if (p.site) |s| try allocator.dupeSentinel(u8, s, 0) else null;
        defer if (site) |s| allocator.free(s);
        const target = Cookie.PreparedUri.init(url);
        if (target.host.len == 0) return error.SecurityError;
        const same_site = Cookie.areSameSite(if (site) |s| .{ .url = s } else .none, target.host);
        const name = if (p.name) |n| std.mem.trim(u8, n, " \t") else null;
        try jar.removeExpired(null);
        var rows: std.ArrayList(CookieStore.StoreRow) = .empty;
        defer rows.deinit(allocator);
        for (jar.cookies.items) |*cookie| {
            if (!cookie.appliesTo(&target, .{ .same_site = same_site, .is_http = false })) continue;
            if (name) |n| if (!std.mem.eql(u8, n, cookie.name)) continue;
            try rows.append(allocator, .{
                .name = cookie.name,
                .value = cookie.value,
                .domain = if (cookie.domain.len > 0 and cookie.domain[0] == '.') cookie.domain[1..] else null,
                .path = cookie.path,
                .expires = if (cookie.expires) |e| e * 1000.0 else null,
                .secure = cookie.secure,
                .sameSite = @tagName(cookie.same_site),
            });
            if (p.first_only) break;
        }
        return std.json.Stringify.valueAlloc(allocator, rows.items, .{});
    }
    if (std.mem.eql(u8, operation, "cookies.store_set")) {
        const parsed = try std.json.parseFromValue(Mutation, allocator, payload, .{});
        defer parsed.deinit();
        const p = parsed.value;
        if (p.is_delete and (p.init.value.len != 0 or p.init.expires == null or p.init.expires.? != 0 or p.init.maxAge != null or p.init.sameSite != .strict)) return error.InvalidArgument;
        const url = try allocator.dupeSentinel(u8, p.url, 0);
        defer allocator.free(url);
        const site = if (p.site) |s| try allocator.dupeSentinel(u8, s, 0) else null;
        defer if (site) |s| allocator.free(s);
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        try CookieStore.storeForJar(jar, arena.allocator(), url, if (site) |s| .{ .url = s } else .none, p.init, p.is_delete);
        return allocator.dupe(u8, "null");
    }
    return error.UnsupportedCookieOperation;
}
