// Browser document resource metadata. No desktop/UI ownership lives here.
const std = @import("std");
const Frame = @import("../Frame.zig");
const Element = @import("../webapi/Element.zig");
const URL = @import("../URL.zig");
const HttpClient = @import("../../network/HttpClient.zig");
const dimensions = @import("../image_dimensions.zig");

pub const max_bytes = 512 * 1024;
pub const State = struct {
    source: ?*Element.Html.Link = null,
    href: []const u8 = "",
    revision: u64 = 0,
    status: enum { missing, loading, ready, failed, unsupported } = .missing,
    bytes: []const u8 = "",
    mime: []const u8 = "",
    digest: [64]u8 = @splat('0'),

    pub fn deinit(self: *State, allocator: std.mem.Allocator) void {
        if (self.bytes.len != 0) allocator.free(self.bytes);
        self.href = "";
        self.bytes = "";
    }
};

pub fn hasIconRel(rel: []const u8) bool {
    var tokens = std.mem.tokenizeAny(u8, rel, " \t\r\n\x0c");
    while (tokens.next()) |token| {
        if (std.ascii.eqlIgnoreCase(token, "icon")) return true;
    }
    return false;
}

fn changed(frame: *Frame) void {
    frame._site_icon.revision +%= 1;
    frame._session.notification.dispatch(.frame_site_icon_changed, frame);
}

pub fn disconnected(frame: *Frame, link: *Element.Html.Link) void {
    if (frame._site_icon.source != link) return;
    const revision = frame._site_icon.revision;
    frame._site_icon.deinit(frame._session.browser.allocator);
    frame._site_icon = .{ .revision = revision };
    changed(frame);
}

pub fn linkChanged(frame: *Frame, link: *Element.Html.Link) !void {
    if (frame.parent != null or frame._parse_mode == .fragment or frame.isGoingAway()) return;
    const element = link.asElement();
    if (element.getDocument(frame) != frame.document) return;
    const href = element.getAttributeInterned("href") orelse "";
    if (!hasIconRel(element.getAttributeInterned("rel") orelse "") or href.len == 0) {
        disconnected(frame, link);
        return;
    }
    const state = &frame._site_icon;
    // Parser completion, relList reflection and metadata reads must not restart
    // an unchanged request (including failed and unsupported outcomes).
    if (state.source == link and std.mem.eql(u8, state.href, href)) return;
    const allocator = frame._session.browser.allocator;
    const revision = state.revision;
    state.deinit(allocator);
    state.* = .{ .source = link, .href = href, .revision = revision, .status = .loading };
    changed(frame);
    errdefer {
        state.status = .failed;
        changed(frame);
    }
    if (href.len > max_bytes * 3) return error.IconTooLarge;
    const arena = try frame._session.getArena(.small, "site_icon");
    defer arena.release();
    const resolved = try URL.resolve(arena.allocator(), frame.base(), href, .{ .encoding = frame.charset });
    const load = try frame._factory.create(Load{ .frame = frame, .revision = state.revision });
    errdefer frame._factory.destroy(load);
    const transfer = try frame._session.browser.http_client.newRequest(.{
        .ctx = load,
        .url = resolved,
        .method = .GET,
        .origin = frame.origin,
        .request_mode = .no_cors,
        .credentials_mode = .include,
        .resource_type = .image,
        .header_callback = Load.header,
        .data_callback = Load.data,
        .done_callback = Load.done,
        .error_callback = Load.failed,
        .shutdown_callback = Load.shutdown,
    }, &frame._http_owner);
    {
        errdefer transfer.deinit();
        try transfer.setHeader("Accept", "image/png,image/x-icon,image/vnd.microsoft.icon,image/*;q=0.8,*/*;q=0.5", .{});
        try frame.headersForRequest(transfer, .{});
    }
    // HttpClient owns synthetic data:/blob: handling as well as proxy, cookies,
    // referrer and Network.* events. submit failure already calls failed().
    transfer.submit() catch {};
}

const Load = struct {
    frame: *Frame,
    revision: u64,
    status: u16 = 0,
    bytes: std.ArrayList(u8) = .empty,

    fn current(self: *const Load) bool {
        return !self.frame.isGoingAway() and self.frame._site_icon.revision == self.revision;
    }
    fn header(transfer: *HttpClient.Transfer) !HttpClient.Transfer.HeaderResult {
        const self: *Load = @ptrCast(@alignCast(transfer.req.ctx));
        self.status = transfer.responseStatus() orelse 0;
        if (!self.current()) return error.StaleIcon;
        if (transfer.getContentLength()) |size| {
            if (size > max_bytes) return error.IconTooLarge;
        }
        return .proceed;
    }
    fn data(transfer: *HttpClient.Transfer, chunk: []const u8) !void {
        const self: *Load = @ptrCast(@alignCast(transfer.req.ctx));
        if (!self.current()) return error.StaleIcon;
        if (chunk.len > max_bytes - self.bytes.items.len) return error.IconTooLarge;
        try self.bytes.appendSlice(self.frame._session.browser.allocator, chunk);
    }
    fn done(ctx: *anyopaque) !void {
        const self: *Load = @ptrCast(@alignCast(ctx));
        defer self.destroy();
        if (!self.current()) return;
        const state = &self.frame._site_icon;
        if (self.status < 200 or self.status >= 300 or self.bytes.items.len == 0) {
            state.status = .failed;
        } else if (imageMime(self.bytes.items)) |mime| {
            state.bytes = self.bytes.toOwnedSlice(self.frame._session.browser.allocator) catch {
                state.status = .failed;
                changed(self.frame);
                return;
            };
            state.mime = mime;
            var digest: [32]u8 = undefined;
            std.crypto.hash.sha2.Sha256.hash(state.bytes, &digest, .{});
            state.digest = std.fmt.bytesToHex(digest, .lower);
            state.status = .ready;
        } else {
            state.status = .unsupported;
        }
        changed(self.frame);
    }
    fn failed(ctx: *anyopaque, _: anyerror) void {
        const self: *Load = @ptrCast(@alignCast(ctx));
        if (self.current()) {
            self.frame._site_icon.status = .failed;
            changed(self.frame);
        }
        self.destroy();
    }
    fn shutdown(ctx: *anyopaque) void {
        const self: *Load = @ptrCast(@alignCast(ctx));
        self.destroy();
    }
    fn destroy(self: *Load) void {
        self.bytes.deinit(self.frame._session.browser.allocator);
        self.frame._factory.destroy(self);
    }
};

fn imageMime(bytes: []const u8) ?[]const u8 {
    if (bytes.len >= 24 and std.mem.startsWith(u8, bytes, "\x89PNG\r\n\x1a\n")) {
        const size = dimensions.parse(bytes) orelse return null;
        if (size.width > 4096 or size.height > 4096) return null;
        return "image/png";
    }
    if (bytes.len >= 22 and std.mem.eql(u8, bytes[0..4], "\x00\x00\x01\x00")) {
        const count = std.mem.readInt(u16, bytes[4..6], .little);
        if (count == 0 or count > 256 or 6 + @as(usize, count) * 16 > bytes.len) return null;
        var index: usize = 0;
        while (index < count) : (index += 1) {
            const entry = bytes[6 + index * 16 ..][0..16];
            const size: usize = std.mem.readInt(u32, entry[8..12], .little);
            const offset: usize = std.mem.readInt(u32, entry[12..16], .little);
            if (size == 0 or offset < 6 + @as(usize, count) * 16 or offset > bytes.len or size > bytes.len - offset) return null;
        }
        return "image/x-icon";
    }
    return null;
}

test "site icon relation tokens and bounded format identification" {
    try std.testing.expect(hasIconRel("shortcut\tICON"));
    try std.testing.expect(!hasIconRel("apple-touch-icon iconography"));
    try std.testing.expect(imageMime("not an icon") == null);
    const malformed_ico = [6]u8{ 0, 0, 1, 0, 1, 0 } ++ @as([16]u8, @splat(0));
    try std.testing.expect(imageMime(&malformed_ico) == null);
}
