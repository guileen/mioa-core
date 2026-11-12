// Copyright (C) 2023-2025  Lightpanda (Selecy SAS)
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

const js = @import("../js/js.zig");
const Frame = @import("../Frame.zig");

const PopStateEvent = @import("event/PopStateEvent.zig");

const History = @This();

const ScrollRestoration = enum { auto, manual };

_frame: *Frame,
_scroll_restoration: ScrollRestoration = .auto,

pub fn getLength(self: *const History) u32 {
    const target = self._frame;
    return @intCast(target._session.navigationFor(target)._entries.items.len);
}

pub fn getState(self: *const History, frame: *Frame) !?js.Value {
    const target = self._frame;
    if (target._session.navigationFor(target).getCurrentEntry()._state.value) |state| {
        const value = try frame.js.local.?.parseJSON(state);
        return value;
    } else return null;
}

fn getScrollRestoration(self: *History) []const u8 {
    return @tagName(self._scroll_restoration);
}

fn setScrollRestoration(self: *History, str: []const u8) void {
    if (std.meta.stringToEnum(ScrollRestoration, str)) |sr| {
        self._scroll_restoration = sr;
    }
}

fn prefixUntil(url: []const u8, delimiters: []const u8) []const u8 {
    for (url, 0..) |c, i| {
        if (std.mem.findScalar(u8, delimiters, c) != null) return url[0..i];
    }
    return url;
}

// Both inputs are serialized URLs. Compare authority including credentials;
// opaque URLs may change only their fragment, file URLs only query/fragment.
fn canRewriteUrl(current: []const u8, next: []const u8) bool {
    const colon = std.mem.findScalar(u8, current, ':') orelse return false;
    if (!std.mem.startsWith(u8, next, current[0 .. colon + 1])) return false;
    const scheme = current[0..colon];
    if (std.mem.eql(u8, scheme, "http") or std.mem.eql(u8, scheme, "https")) {
        const start = colon + 3;
        if (current.len < start or next.len < start) return false;
        return std.mem.eql(u8, current[0 .. start + prefixUntil(current[start..], "/?#").len], next[0 .. start + prefixUntil(next[start..], "/?#").len]);
    }
    const delimiters = if (std.mem.eql(u8, scheme, "file")) "?#" else "#";
    return std.mem.eql(u8, prefixUntil(current, delimiters), prefixUntil(next, delimiters));
}

fn resolveStateUrl(target: *Frame, input: ?[]const u8) ![:0]const u8 {
    const arena = target._session.arena.allocator();
    const value = input orelse return arena.dupeSentinel(u8, target.url, 0);
    if (value.len == 0) return arena.dupeSentinel(u8, target.url, 0);
    const url = @import("../URL.zig").resolve(arena, target.navigationBase(), value, .{}) catch |err| switch (err) {
        error.TypeError => return error.SecurityError,
        else => return err,
    };
    if (!canRewriteUrl(target.url, url)) return error.SecurityError;
    return url;
}

pub fn pushState(self: *History, state: js.Value, _: ?[]const u8, _url: ?[]const u8) !void {
    // The URL resolves against this history's document, not the caller's.
    const target = self._frame;
    const session = target._session;
    const arena = session.arena;
    const url = try resolveStateUrl(target, _url);

    const json = state.toJson(arena.allocator()) catch return error.DataClone;
    _ = try session.navigationFor(target).pushEntry(url, .{ .source = .history, .value = json }, target, true);

    target.url = url;
    // setHref == reinitializing.
    try target.window._location._url.setHref(url, &target.js.execution);
    // `:target` matches off the fragment, which the new URL can change.
    target.styleChanged();

    session.notification.dispatch(.frame_navigated_within_document, &.{
        .url = url,
        .frame_id = target._frame_id,
        .navigation_type = .historyApi,
    });
}

fn replaceState(self: *History, state: js.Value, _: ?[]const u8, _url: ?[]const u8) !void {
    // The URL resolves against this history's document, not the caller's.
    const target = self._frame;
    const session = target._session;
    const arena = session.arena;
    const url = try resolveStateUrl(target, _url);

    const json = state.toJson(arena.allocator()) catch return error.DataClone;
    _ = try session.navigationFor(target).replaceEntry(url, .{ .source = .history, .value = json }, target, true);

    target.url = url;
    // setHref == reinitializing.
    try target.window._location._url.setHref(url, &target.js.execution);
    // `:target` matches off the fragment, which the new URL can change.
    target.styleChanged();

    session.notification.dispatch(.frame_navigated_within_document, &.{
        .url = url,
        .frame_id = target._frame_id,
        .navigation_type = .historyApi,
    });
}

fn goInner(delta: i32, frame: *Frame) !void {
    // 0 behaves the same as no argument, both reloading the frame.

    const navigation = frame._session.navigationFor(frame);
    const current = navigation._index;
    const index_s: i64 = @intCast(@as(i64, @intCast(current)) + @as(i64, @intCast(delta)));
    if (index_s < 0 or index_s > navigation._entries.items.len - 1) {
        return;
    }

    const index = @as(usize, @intCast(index_s));
    const entry = navigation._entries.items[index];
    // popstate fires on the document that stays; a traversal to another
    // document replaces this one instead.
    const same_document = entry.sameDocument(frame);

    _ = try navigation.navigateInner(entry._url, .{ .traverse = index }, frame);

    if (same_document) {
        try firePopState(frame, entry._state.value);
        // hashchange is queued by navigateInner.
    }
}

// Fires popstate at the frame's window, synchronously: a same-document
// traversal or a fragment navigation updating the document for its new
// history entry. `state` is the entry's serialized state.
pub fn firePopState(frame: *Frame, state: ?[]const u8) !void {
    const target = frame.window.asEventTarget();
    if (frame._event_manager.hasDirectListeners(target, "popstate")) {
        const event = (try PopStateEvent.initTrusted(comptime .wrap("popstate"), .{ .state = state }, frame)).asEvent();
        try frame._event_manager.dispatchDirect(target, event, .{ .context = "Pop State" });
    }
}

// Traversals still act on the injected frame, not self._frame.
pub fn back(_: *History, frame: *Frame) !void {
    try goInner(-1, frame);
}

pub fn forward(_: *History, frame: *Frame) !void {
    try goInner(1, frame);
}

pub fn go(_: *History, delta: ?i32, frame: *Frame) !void {
    try goInner(delta orelse 0, frame);
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(History);

    pub const Meta = struct {
        pub const name = "History";
        pub var class_id: bridge.ClassId = 0;
    };

    pub const length = bridge.accessor(History.getLength, null, .{});
    pub const scrollRestoration = bridge.accessor(History.getScrollRestoration, History.setScrollRestoration, .{});
    pub const state = bridge.accessor(History.getState, null, .{});
    pub const pushState = bridge.function(History.pushState, .{});
    pub const replaceState = bridge.function(History.replaceState, .{});
    pub const back = bridge.function(History.back, .{});
    pub const forward = bridge.function(History.forward, .{});
    pub const go = bridge.function(History.go, .{});
};

const testing = @import("../../testing.zig");
test "WebApi: History" {
    try testing.htmlRunner("history.html", .{});
    try testing.htmlRunner("history_url_update.html", .{});
    try testing.htmlRunner("history_traverse.html", .{});
}

test "History: URL rewrite authority" {
    const cases = [_]struct { current: []const u8, next: []const u8, allowed: bool }{
        .{ .current = "https://a.test/a", .next = "https://a.test/b?q=1#x", .allowed = true },
        .{ .current = "https://a.test/a", .next = "https://b.test/a", .allowed = false },
        .{ .current = "https://a.test/a", .next = "http://a.test/a", .allowed = false },
        .{ .current = "https://a.test/a", .next = "https://a.test:8443/a", .allowed = false },
        .{ .current = "https://a.test/a", .next = "https://user@a.test/a", .allowed = false },
        .{ .current = "https://user:one@a.test/a", .next = "https://user:two@a.test/a", .allowed = false },
        .{ .current = "file:///a", .next = "file:///a?q=1#x", .allowed = true },
        .{ .current = "file:///a", .next = "file:///b", .allowed = false },
        .{ .current = "about:blank", .next = "about:blank#x", .allowed = true },
        .{ .current = "about:blank", .next = "about:blank?q=1", .allowed = false },
        .{ .current = "about:blank", .next = "https://a.test/a", .allowed = false },
        .{ .current = "data:text/html,one", .next = "data:text/html,one#x", .allowed = true },
        .{ .current = "data:text/html,one", .next = "data:text/html,two", .allowed = false },
    };
    for (cases) |case| try testing.expectEqual(case.allowed, canRewriteUrl(case.current, case.next));
}
