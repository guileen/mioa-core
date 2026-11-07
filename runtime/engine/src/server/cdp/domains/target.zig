// Copyright (C) 2023-2024  Lightpanda (Selecy SAS)
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

const id = @import("../id.zig");
const CDP = @import("../CDP.zig");

const URL = @import("../../../browser/URL.zig");
const js = @import("../../../browser/js/js.zig");

const log = lp.log;

pub fn processMessage(cmd: *CDP.Command) !void {
    const action = std.meta.stringToEnum(enum {
        getTargets,
        attachToTarget,
        attachToBrowserTarget,
        closeTarget,
        createBrowserContext,
        createTarget,
        detachFromTarget,
        disposeBrowserContext,
        getBrowserContexts,
        getTargetInfo,
        sendMessageToTarget,
        setAutoAttach,
        setDiscoverTargets,
        activateTarget,
    }, cmd.input.action) orelse return error.UnknownMethod;

    switch (action) {
        .getTargets => return getTargets(cmd),
        .attachToTarget => return attachToTarget(cmd),
        .attachToBrowserTarget => return attachToBrowserTarget(cmd),
        .closeTarget => return closeTarget(cmd),
        .createBrowserContext => return createBrowserContext(cmd),
        .createTarget => return createTarget(cmd),
        .detachFromTarget => return detachFromTarget(cmd),
        .disposeBrowserContext => return disposeBrowserContext(cmd),
        .getBrowserContexts => return getBrowserContexts(cmd),
        .getTargetInfo => return getTargetInfo(cmd),
        .sendMessageToTarget => return sendMessageToTarget(cmd),
        .setAutoAttach => return setAutoAttach(cmd),
        .setDiscoverTargets => return setDiscoverTargets(cmd),
        .activateTarget => return activateTarget(cmd),
    }
}

fn getTargets(cmd: *CDP.Command) !void {
    var infos: std.ArrayList(TargetInfo) = .empty;
    if (cmd.cdp.browser_context) |*bc| {
        if (pageTargetInfo(bc)) |info| try infos.append(cmd.arena, info);
    }
    for (cmd.cdp.targets.items) |target| {
        if (pageTargetInfo(target)) |info| try infos.append(cmd.arena, info);
    }
    return cmd.sendResult(.{ .targetInfos = infos.items }, .{});
}

fn pageTargetInfo(bc: *const CDP.BrowserContext) ?TargetInfo {
    const target_id = if (bc.target_id) |*tid| tid else return null;
    return .{
        .targetId = target_id,
        .title = bc.getTitle() orelse "",
        .url = bc.getURL() orelse "about:blank",
        .browserContextId = bc.id,
        .attached = bc.attached_sessions.count() != 0,
    };
}

fn activateTarget(cmd: *CDP.Command) !void {
    const p = (try cmd.params(struct { targetId: []const u8 })) orelse return error.InvalidParams;
    cmd.cdp.active_target = cmd.cdp.targetById(p.targetId) orelse return error.UnknownTargetId;
    return cmd.sendResult(null, .{});
}

/// Clients such as Browser Use track a tab's URL and title only through this
/// event, as Chrome sends it whenever either changes.
pub fn sendTargetInfoChanged(bc: *const CDP.BrowserContext, frame_id: u32) !void {
    if (!bc.cdp.target_discover) return;
    const main = bc.mainFrame() orelse return;
    if (main._frame_id != frame_id) return;
    const info = pageTargetInfo(bc) orelse return;
    try bc.cdp.sendEvent("Target.targetInfoChanged", .{ .targetInfo = info }, .{});
}

fn getBrowserContexts(cmd: *CDP.Command) !void {
    var browser_context_ids: []const []const u8 = undefined;
    if (cmd.browser_context) |bc| {
        browser_context_ids = &.{bc.id};
    } else {
        browser_context_ids = &.{};
    }

    return cmd.sendResult(.{
        .browserContextIds = browser_context_ids,
    }, .{});
}

fn createBrowserContext(cmd: *CDP.Command) !void {
    const params = try cmd.params(struct {
        disposeOnDetach: bool = false,
        proxyServer: ?[:0]const u8 = null,
        proxyBypassList: ?[]const u8 = null,
        originsWithUniversalNetworkAccess: ?[]const []const u8 = null,
    });
    if (params) |p| {
        if (p.disposeOnDetach or p.proxyBypassList != null or p.originsWithUniversalNetworkAccess != null) {
            log.debug(.not_implemented, "Target.createBrowserContext", .{ .disposeOnDetach = p.disposeOnDetach, .has_proxyBypassList = p.proxyBypassList != null, .has_originsWithUniversalNetworkAccess = p.originsWithUniversalNetworkAccess != null });
        }
    }

    const bc = cmd.createBrowserContext() catch |err| switch (err) {
        error.AlreadyExists => return cmd.sendError(-32000, "Cannot have more than one browser context at a time", .{}),
        else => return err,
    };

    if (params) |p| {
        if (p.proxyServer) |proxy| {
            // For now the http client is not in the browser context so we assume there is just 1.
            try cmd.cdp.browser.http_client.changeProxy(proxy);
            bc.http_proxy_changed = true;
        }
    }

    return cmd.sendResult(.{
        .browserContextId = bc.id,
    }, .{});
}

fn disposeBrowserContext(cmd: *CDP.Command) !void {
    const params = (try cmd.params(struct {
        browserContextId: []const u8,
    })) orelse return error.InvalidParams;

    const bc = cmd.browser_context orelse {
        return cmd.sendError(-32602, "No browser context with the given id found", .{});
    };
    if (std.mem.eql(u8, bc.id, params.browserContextId) == false) {
        return cmd.sendError(-32602, "No browser context with the given id found", .{});
    }

    for (cmd.cdp.targets.items) |target| try target.closeTarget();
    try bc.closeTarget();
    cmd.cdp.disposeBrowserContext();
    try cmd.sendResult(null, .{});
}

fn createTarget(cmd: *CDP.Command) !void {
    const params = (try cmd.params(struct {
        url: [:0]const u8 = "about:blank",
        // width: ?u64 = null,
        // height: ?u64 = null,
        browserContextId: ?[]const u8 = null,
        // enableBeginFrameControl: bool = false,
        // newWindow: bool = false,
        // background: bool = false,
        // forTab: ?bool = null,
    })) orelse return error.InvalidParams;

    const root = if (cmd.cdp.browser_context) |*bc| bc else try cmd.createBrowserContext();
    if (params.browserContextId) |param_browser_context_id| {
        if (std.mem.eql(u8, param_browser_context_id, root.id) == false) {
            return error.UnknownBrowserContextId;
        }
    }

    const bc = try cmd.cdp.newTarget();
    const page = try createNativePage(cmd, bc);
    const frame = page.frame().?;

    const target_id = &bc.target_id.?;

    // change CDP state
    bc.security_origin = "://";
    bc.secure_context_type = "InsecureScheme";

    // send targetCreated event
    // TODO: should this only be sent when Target.setDiscoverTargets
    // has been enabled?
    try cmd.sendEvent("Target.targetCreated", .{
        .targetInfo = TargetInfo{
            .attached = false,
            .targetId = target_id,
            .title = "",
            .browserContextId = bc.id,
            .url = "about:blank",
        },
    }, .{});

    // attach to the target only if auto attach is set.
    if (cmd.cdp.target_auto_attach) {
        try doAttachtoTarget(cmd, bc);
    }

    if (URL.isAboutBlank(params.url) == false) {
        const encoded_url = try URL.resolveNavigation(frame.call_arena, params.url, .{});
        try frame.navigate(
            encoded_url,
            .{ .reason = .address_bar, .kind = .{ .push = null } },
        );
    }

    try cmd.sendResult(.{
        .targetId = target_id,
    }, .{});
}

fn createNativePage(cmd: *CDP.Command, bc: *CDP.BrowserContext) !@import("../../../browser/Session.zig").PageHandle {
    cmd.cdp.creating_target = bc;
    defer cmd.cdp.creating_target = null;
    const page = try bc.session.createPage();
    errdefer page.close();
    bc.target_id = id.toFrameId(page.frame_id);
    try registerInitialContext(bc, page.frame().?);
    cmd.cdp.active_target = bc;
    return page;
}

pub fn registerInitialContext(bc: *CDP.BrowserContext, frame: *@import("../../../browser/Frame.zig")) !void {
    var ls: js.Local.Scope = undefined;
    frame.js.localScope(&ls);
    defer ls.deinit();
    if (bc.inspector().getContextId(&ls.local) != 0) return;
    const aux_data = try bc.arena.print("{{\"isDefault\":true,\"type\":\"default\",\"frameId\":\"{s}\"}}", .{&bc.target_id.?});
    bc.inspector().contextCreatedInGroup(&ls.local, "", frame.origin orelse "", aux_data, true, bc.context_group_id);
}

// Session publishes script-created tabs through the same notification as
// CDP-created tabs. Keep those native pages in the registry as well.
pub fn nativePageOpened(cdp: *CDP, frame: *@import("../../../browser/Frame.zig")) !void {
    const bc = try cdp.newTarget();
    try @import("page.zig").frameCreated(bc, frame);
    bc.target_id = id.toFrameId(frame._frame_id);
    try registerInitialContext(bc, frame);
    try cdp.sendEvent("Target.targetCreated", .{ .targetInfo = pageTargetInfo(bc).? }, .{});
    if (cdp.target_auto_attach) {
        const attached = try bc.attachPrimarySession(cdp.session_id_gen.next());
        try cdp.sendEvent("Target.attachedToTarget", AttachToTarget{
            .sessionId = attached.id,
            .targetInfo = pageTargetInfo(bc).?,
        }, .{});
    }
}

fn attachToTarget(cmd: *CDP.Command) !void {
    const params = (try cmd.params(struct {
        targetId: []const u8,
        flatten: bool = true,
    })) orelse return error.InvalidParams;
    if (!params.flatten) return error.UnsupportedNonFlattenedSession;

    const bc = cmd.cdp.targetById(params.targetId) orelse return error.UnknownTargetId;
    const target_id = &bc.target_id.?;

    // The first attach becomes the page's primary session, which all
    // Page/Network/... events are emitted to. Later attaches — e.g.
    // Playwright's browserContext.newCDPSession(page), sent through its
    // browser session while the page session is live — get a distinct
    // auxiliary session so they don't collide with the primary one.
    if (bc.session_id == null) {
        try doAttachtoTarget(cmd, bc);
        return cmd.sendResult(.{ .sessionId = bc.session_id }, .{});
    }

    const parent_id = if (cmd.input.session_id) |session_id|
        cmd.cdp.resolveSessionId(session_id) orelse return error.UnknownSessionId
    else
        null;
    const session = try bc.attachSession(cmd.cdp.session_id_gen.next(), parent_id);

    try cmd.sendEvent("Target.attachedToTarget", AttachToTarget{
        .sessionId = session.id,
        .targetInfo = TargetInfo{
            .targetId = target_id,
            .title = bc.getTitle() orelse "",
            .url = bc.getURL() orelse "about:blank",
            .browserContextId = bc.id,
        },
    }, .{ .session_id = parent_id });

    return cmd.sendResult(.{ .sessionId = session.id }, .{});
}

fn attachToBrowserTarget(cmd: *CDP.Command) !void {
    const cdp = cmd.cdp;
    // Browser-target sessions are independent from page-target sessions and
    // can be created before a browser context exists.
    const session_id = cdp.browser_session_id orelse cdp.browser_session_id_gen.next();

    try cmd.sendEvent("Target.attachedToTarget", AttachToTarget{
        .sessionId = session_id,
        .targetInfo = TargetInfo{
            .targetId = "browser",
            .title = "",
            .url = "",
            .type = "browser",
            // Chrome doesn't send a browserContextId in this case.
            .browserContextId = null,
        },
    }, .{});

    cdp.browser_session_id = session_id;

    return cmd.sendResult(.{ .sessionId = session_id }, .{});
}

fn closeTarget(cmd: *CDP.Command) !void {
    const params = (try cmd.params(struct {
        targetId: []const u8,
    })) orelse return error.InvalidParams;

    const bc = cmd.cdp.targetById(params.targetId) orelse return error.UnknownTargetId;

    try cmd.sendResult(.{ .success = true }, .{});
    try bc.closeTarget();
}

fn getTargetInfo(cmd: *CDP.Command) !void {
    const Params = struct {
        targetId: ?[]const u8 = null,
    };
    const params = (try cmd.params(Params)) orelse Params{};

    if (params.targetId) |param_target_id| {
        const bc = cmd.cdp.targetById(param_target_id) orelse return error.UnknownTargetId;
        const info = pageTargetInfo(bc) orelse return error.TargetNotLoaded;
        return cmd.sendResult(.{ .targetInfo = info }, .{});
    }
    if (cmd.browser_context) |bc| {
        if (pageTargetInfo(bc)) |info| return cmd.sendResult(.{ .targetInfo = info }, .{});
    }

    return cmd.sendResult(.{
        .targetInfo = TargetInfo{
            .targetId = "TID-STARTUP",
            .type = "browser",
            .title = "",
            .url = "about:blank",
            .attached = true,
            .canAccessOpener = false,
        },
    }, .{});
}

fn sendMessageToTarget(cmd: *CDP.Command) !void {
    return cmd.sendError(-32000, "Non-flattened target transport is unsupported; attach with flatten:true", .{});
}

fn detachFromTarget(cmd: *CDP.Command) !void {
    const Params = struct {
        sessionId: ?[]const u8 = null,
        targetId: ?[]const u8 = null,
    };
    const params = (try cmd.params(Params)) orelse Params{};

    const bc = if (params.sessionId) |sid|
        cmd.cdp.targetForSession(sid) orelse return error.UnknownSessionId
    else if (params.targetId) |tid|
        cmd.cdp.targetById(tid) orelse return error.UnknownTargetId
    else
        cmd.browser_context orelse return error.BrowserContextNotLoaded;
    if (params.sessionId orelse bc.session_id) |sid| {
        const session = bc.attached_sessions.get(sid) orelse return error.UnknownSessionId;
        bc.fetchDisableForSession(sid);
        try cmd.sendEvent("Target.detachedFromTarget", .{ .sessionId = sid }, .{ .session_id = session.parent_id });
        _ = bc.detachSession(sid);
    }

    return cmd.sendResult(null, .{});
}

fn setDiscoverTargets(cmd: *CDP.Command) !void {
    const params = (try cmd.params(struct {
        discover: bool,
        filter: ?[]const TargetFilter = null,
    })) orelse return error.InvalidParams;

    cmd.cdp.target_discover = params.discover and filterIncludesPage(params.filter);
    if (cmd.cdp.target_discover) {
        if (cmd.cdp.browser_context) |*bc| {
            if (pageTargetInfo(bc)) |info| try cmd.sendEvent("Target.targetCreated", .{ .targetInfo = info }, .{});
        }
        for (cmd.cdp.targets.items) |target| {
            if (pageTargetInfo(target)) |info| try cmd.sendEvent("Target.targetCreated", .{ .targetInfo = info }, .{});
        }
    }
    return cmd.sendResult(null, .{});
}

/// One entry of Target.setAutoAttach's `filter`: `type`
/// selects the target type it matches (absent matches any
/// type), `exclude` turns the entry into an exclusion.
const TargetFilter = struct {
    type: ?[]const u8 = null,
    exclude: bool = false,
};

/// Does the client's target filter include page targets?
/// Entries are checked in order: the first entry matching the
/// target type decides; an absent type matches any type.
fn filterIncludesPage(filter: ?[]const TargetFilter) bool {
    for (filter orelse return true) |f| {
        const t = f.type orelse return !f.exclude;
        if (std.mem.eql(u8, t, "page")) return !f.exclude;
    }
    return false;
}

fn setAutoAttach(cmd: *CDP.Command) !void {
    const params = (try cmd.params(struct {
        autoAttach: bool,
        waitForDebuggerOnStart: bool,
        flatten: bool = true,
        filter: ?[]const TargetFilter = null,
    })) orelse return error.InvalidParams;
    if (!params.flatten) return error.UnsupportedNonFlattenedSession;

    cmd.cdp.target_auto_attach = params.autoAttach and filterIncludesPage(params.filter);
    if (!cmd.cdp.target_auto_attach) {
        if (cmd.cdp.browser_context) |*root| try detachPrimary(cmd, root);
        for (cmd.cdp.targets.items) |target| try detachPrimary(cmd, target);
    } else if (cmd.cdp.browser_context) |*root| {
        if (root.target_id != null and root.session_id == null) try doAttachtoTarget(cmd, root);
        for (cmd.cdp.targets.items) |target| {
            if (target.target_id != null and target.session_id == null) try doAttachtoTarget(cmd, target);
        }
    } else {
        // Preserve the legacy driver handshake. This is not a native page;
        // actual page operations require Target.createTarget and a real SID.
        try cmd.sendEvent("Target.attachedToTarget", AttachToTarget{
            .sessionId = "STARTUP",
            .targetInfo = TargetInfo{
                .type = "page",
                .targetId = "TID-STARTUP",
                .title = "",
                .url = "about:blank",
                .browserContextId = "BID-STARTUP",
            },
        }, .{});
    }
    return cmd.sendResult(null, .{});
}

fn detachPrimary(cmd: *CDP.Command, bc: *CDP.BrowserContext) !void {
    if (bc.session_id) |sid| {
        const attached = bc.attached_sessions.get(sid).?;
        bc.fetchDisableForSession(sid);
        try cmd.sendEvent("Target.detachedFromTarget", .{ .sessionId = sid }, .{ .session_id = attached.parent_id });
        _ = bc.detachSession(sid);
    }
}

fn doAttachtoTarget(cmd: *CDP.Command, bc: *CDP.BrowserContext) !void {
    const parent_id = if (cmd.input.session_id) |sid| cmd.cdp.resolveSessionId(sid) else null;
    const attached = try bc.attachSession(cmd.cdp.session_id_gen.next(), parent_id);
    bc.session_id = attached.id;
    try cmd.sendEvent("Target.attachedToTarget", AttachToTarget{
        .sessionId = attached.id,
        .targetInfo = pageTargetInfo(bc).?,
    }, .{ .session_id = parent_id });
}

const AttachToTarget = struct {
    sessionId: []const u8,
    targetInfo: TargetInfo,
    waitingForDebugger: bool = false,
};

const TargetInfo = struct {
    url: []const u8,
    title: []const u8,
    targetId: []const u8,
    attached: bool = true,
    type: []const u8 = "page",
    canAccessOpener: bool = false,
    browserContextId: ?[]const u8 = null,
};

const testing = @import("../testing.zig");
test "cdp.target: getBrowserContexts" {
    var ctx = try testing.context();
    defer ctx.deinit();

    // {
    //     // no browser context
    //     try ctx.processMessage(.{.id = 4, .method = "Target.getBrowserContexts"});

    //     try ctx.expectSentResult(.{
    //         .browserContextIds = &.{},
    //     }, .{ .id = 4, .session_id = null });
    // }

    {
        // with a browser context
        _ = try ctx.loadBrowserContext(.{ .id = "BID-X" });
        try ctx.processMessage(.{ .id = 5, .method = "Target.getBrowserContexts" });

        try ctx.expectSentResult(.{
            .browserContextIds = &.{"BID-X"},
        }, .{ .id = 5, .session_id = null });
    }
}

test "cdp.target: createBrowserContext" {
    var ctx = try testing.context();
    defer ctx.deinit();

    {
        try ctx.processMessage(.{ .id = 4, .method = "Target.createBrowserContext" });
        try ctx.expectSentResult(.{
            .browserContextId = ctx.cdp().browser_context.?.id,
        }, .{ .id = 4, .session_id = null });
    }

    {
        // we already have one now
        try ctx.processMessage(.{ .id = 5, .method = "Target.createBrowserContext" });
        try ctx.expectSentError(-32000, "Cannot have more than one browser context at a time", .{ .id = 5 });
    }
}

test "cdp.target: disposeBrowserContext" {
    var ctx = try testing.context();
    defer ctx.deinit();

    {
        try ctx.processMessage(.{ .id = 7, .method = "Target.disposeBrowserContext" });
        try ctx.expectSentError(-31998, "InvalidParams", .{ .id = 7 });
    }

    {
        try ctx.processMessage(.{
            .id = 8,
            .method = "Target.disposeBrowserContext",
            .params = .{ .browserContextId = "BID-10" },
        });
        try ctx.expectSentError(-32602, "No browser context with the given id found", .{ .id = 8 });
    }

    {
        _ = try ctx.loadBrowserContext(.{ .id = "BID-20" });
        try ctx.processMessage(.{
            .id = 9,
            .method = "Target.disposeBrowserContext",
            .params = .{ .browserContextId = "BID-20" },
        });
        try ctx.expectSentResult(null, .{ .id = 9 });
        try testing.expectEqual(null, ctx.cdp().browser_context);
    }
}

test "cdp.target: disposeBrowserContext detaches target sessions" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 1, .method = "Target.setAutoAttach", .params = .{ .autoAttach = true, .waitForDebuggerOnStart = false } });
    try ctx.processMessage(.{ .id = 2, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const bc = &ctx.cdp().browser_context.?;
    const target_id = bc.target_id.?;
    const context_id = try testing.arena_allocator.dupe(u8, bc.id);
    const primary_id = try testing.arena_allocator.dupe(u8, bc.session_id.?);
    try ctx.processMessage(.{ .id = 3, .method = "Target.attachToBrowserTarget" });
    try ctx.processMessage(.{
        .id = 4,
        .method = "Target.attachToTarget",
        .sessionId = "BSID-1",
        .params = .{ .targetId = target_id },
    });
    // the primary is attached first, the auxiliary session after it
    try testing.expectEqual(2, bc.attached_sessions.count());
    const auxiliary_id = try testing.arena_allocator.dupe(u8, bc.attached_sessions.keys()[1]);

    try ctx.processMessage(.{
        .id = 5,
        .method = "Target.disposeBrowserContext",
        .params = .{ .browserContextId = context_id },
    });
    // Playwright waits for detachedFromTarget to resolve Page.closedPromise,
    // even if Target.disposeBrowserContext itself has already replied.
    try ctx.expectSentEvent("Target.detachedFromTarget", .{
        .targetId = target_id,
        .sessionId = auxiliary_id,
        .reason = "Render process gone.",
    }, .{ .session_id = "BSID-1" });
    try ctx.expectSentEvent("Target.detachedFromTarget", .{
        .targetId = target_id,
        .sessionId = primary_id,
        .reason = "Render process gone.",
    }, .{});
    try ctx.expectSentEvent("Target.targetDestroyed", .{ .targetId = target_id }, .{});
    try ctx.expectSentResult(null, .{ .id = 5 });
    try testing.expectEqual(null, ctx.cdp().browser_context);

    // The connection remains usable; stale target/session IDs are not reused.
    try ctx.processMessage(.{ .id = 6, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    try testing.expect(!std.mem.eql(u8, &target_id, &ctx.cdp().browser_context.?.target_id.?));
    try testing.expect(!std.mem.eql(u8, primary_id, ctx.cdp().browser_context.?.session_id.?));
}

test "cdp.target: disposeBrowserContext destroys an unattached target" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 1, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const bc = &ctx.cdp().browser_context.?;
    const target_id = bc.target_id.?;
    const context_id = try testing.arena_allocator.dupe(u8, bc.id);

    // An invalid context ID must leave the live target untouched.
    try ctx.processMessage(.{ .id = 2, .method = "Target.disposeBrowserContext", .params = .{ .browserContextId = "BID-UNKNOWN" } });
    try ctx.expectSentError(-32602, "No browser context with the given id found", .{ .id = 2 });
    try ctx.expectSentCount(3);
    try testing.expectEqual(&target_id, bc.target_id.?);

    try ctx.processMessage(.{ .id = 3, .method = "Target.disposeBrowserContext", .params = .{ .browserContextId = context_id } });
    try ctx.expectSentEvent("Target.targetDestroyed", .{ .targetId = target_id }, .{ .index = 3 });
    try ctx.expectSentResult(null, .{ .id = 3, .index = 4 });
    try ctx.expectSentCount(5);
}

test "cdp.target: disposeBrowserContext after closeTarget does not repeat events" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 1, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const bc = &ctx.cdp().browser_context.?;
    const target_id = bc.target_id.?;
    const context_id = try testing.arena_allocator.dupe(u8, bc.id);

    try ctx.processMessage(.{ .id = 2, .method = "Target.closeTarget", .params = .{ .targetId = target_id } });
    try ctx.expectSentEvent("Target.targetDestroyed", .{ .targetId = target_id }, .{ .index = 3 });
    try ctx.expectSentCount(4);
    try ctx.processMessage(.{ .id = 3, .method = "Target.disposeBrowserContext", .params = .{ .browserContextId = context_id } });
    try ctx.expectSentResult(null, .{ .id = 3, .index = 4 });
    try ctx.expectSentCount(5);
}

// Issue #2472: CDP target IDs (`FID-{d:0>10}`) must stay unique for the
// lifetime of a CDP connection. Before the fix, `Session.frame_id_gen`
// reset to 0 on `tearDownActivePage` AND fresh sessions also started
// from 0, so the second `Target.createTarget` after a dispose re-issued
// the same `FID-0000000001` and Playwright clients tripped on
// `Duplicate target FID-...`. The counter now lives on `Browser` and is
// monotonic across BrowserContexts.
test "cdp.target: createTarget assigns unique IDs across BrowserContexts (issue #2472)" {
    var ctx = try testing.context();
    defer ctx.deinit();

    // Cycle 1: create context + target, capture the assigned target_id.
    try ctx.processMessage(.{
        .id = 1,
        .method = "Target.createTarget",
        .params = .{ .url = "about:blank" },
    });
    const target_id_1 = ctx.cdp().browser_context.?.target_id.?;
    const bc_id_1 = ctx.cdp().browser_context.?.id;

    // Dispose the first context.
    try ctx.processMessage(.{
        .id = 2,
        .method = "Target.disposeBrowserContext",
        .params = .{ .browserContextId = bc_id_1 },
    });
    try testing.expectEqual(null, ctx.cdp().browser_context);

    // Cycle 2: create another context + target. The new target_id must
    // differ from cycle 1's -- duplicates here are exactly what Playwright
    // asserts on with `Duplicate target FID-...`.
    try ctx.processMessage(.{
        .id = 3,
        .method = "Target.createTarget",
        .params = .{ .url = "about:blank" },
    });
    const target_id_2 = ctx.cdp().browser_context.?.target_id.?;

    try testing.expect(!std.mem.eql(u8, &target_id_1, &target_id_2));
}

test "cdp.target: createTarget" {
    {
        var ctx = try testing.context();
        defer ctx.deinit();
        try ctx.processMessage(.{ .id = 10, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });

        // should create a browser context
        const bc = ctx.cdp().browser_context.?;
        try ctx.expectSentEvent("Target.targetCreated", .{ .targetInfo = .{ .url = "about:blank", .title = "", .attached = false, .type = "page", .canAccessOpener = false, .browserContextId = bc.id, .targetId = bc.target_id.? } }, .{});
    }

    {
        var ctx = try testing.context();
        defer ctx.deinit();
        // active auto attach to get the Target.attachedToTarget event.
        try ctx.processMessage(.{ .id = 9, .method = "Target.setAutoAttach", .params = .{ .autoAttach = true, .waitForDebuggerOnStart = false } });
        try ctx.processMessage(.{ .id = 10, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });

        // should create a browser context
        const bc = ctx.cdp().browser_context.?;
        try ctx.expectSentEvent("Target.targetCreated", .{ .targetInfo = .{ .url = "about:blank", .title = "", .attached = false, .type = "page", .canAccessOpener = false, .browserContextId = bc.id, .targetId = bc.target_id.? } }, .{});
        try ctx.expectSentEvent("Target.attachedToTarget", .{ .sessionId = bc.session_id.?, .targetInfo = .{ .url = "about:blank", .title = "", .attached = true, .type = "page", .canAccessOpener = false, .browserContextId = bc.id, .targetId = bc.target_id.? } }, .{});
    }

    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.createTarget", .params = .{ .browserContextId = "BID-8" } });
        try ctx.expectSentError(-31998, "UnknownBrowserContextId", .{ .id = 10 });
    }

    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.createTarget", .params = .{ .browserContextId = "BID-9" } });
        try testing.expectEqual(true, bc.target_id != null);
        try ctx.expectSentResult(.{ .targetId = bc.target_id.? }, .{ .id = 10 });
        try ctx.expectSentEvent("Target.targetCreated", .{ .targetInfo = .{ .url = "about:blank", .title = "", .attached = false, .type = "page", .canAccessOpener = false, .browserContextId = "BID-9", .targetId = bc.target_id.? } }, .{});
    }
}

test "cdp.target: native pages share storage but isolate targets and inspector groups" {
    var ctx = try testing.context();
    defer ctx.deinit();
    try ctx.processMessage(.{ .id = 1, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const first = &ctx.cdp().browser_context.?;
    const first_id = first.target_id.?;
    try ctx.processMessage(.{ .id = 2, .method = "Target.attachToTarget", .params = .{ .targetId = first_id, .flatten = true } });
    const first_sid = first.session_id.?;
    try ctx.processMessage(.{ .id = 3, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    try testing.expectEqual(1, ctx.cdp().targets.items.len);
    const second = ctx.cdp().targets.items[0];
    const second_id = second.target_id.?;
    try ctx.processMessage(.{ .id = 4, .method = "Target.attachToTarget", .params = .{ .targetId = second_id, .flatten = true } });
    const second_sid = second.session_id.?;
    try testing.expect(first.session == second.session);
    try testing.expect(first.mainPage().? != second.mainPage().?);
    try testing.expect(first.context_group_id != second.context_group_id);
    try testing.expect(!std.mem.eql(u8, &first_id, &second_id));
    try testing.expect(!std.mem.eql(u8, first_sid, second_sid));
    try ctx.processMessage(.{ .id = 5, .method = "Target.getTargets" });
    try ctx.expectSentResult(.{ .targetInfos = .{ pageTargetInfo(first).?, pageTargetInfo(second).? } }, .{ .id = 5 });
    try ctx.processMessage(.{ .id = 6, .method = "Page.getFrameTree", .sessionId = first_sid });
    try ctx.expectSentResult(.{ .frameTree = .{ .frame = .{ .id = first_id } } }, .{ .id = 6, .session_id = first_sid });
    try ctx.processMessage(.{ .id = 7, .method = "Page.getFrameTree", .sessionId = second_sid });
    try ctx.expectSentResult(.{ .frameTree = .{ .frame = .{ .id = second_id } } }, .{ .id = 7, .session_id = second_sid });
    try ctx.processMessage(.{ .id = 8, .method = "Runtime.enable", .sessionId = first_sid });
    try ctx.processMessage(.{ .id = 9, .method = "Runtime.enable", .sessionId = second_sid });
    try ctx.processMessage(.{ .id = 10, .method = "Runtime.evaluate", .sessionId = first_sid, .params = .{ .expression = "globalThis.tabMarker = 'first'", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "first" } }, .{ .id = 10, .session_id = first_sid });
    try ctx.processMessage(.{ .id = 11, .method = "Runtime.evaluate", .sessionId = second_sid, .params = .{ .expression = "typeof tabMarker", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "undefined" } }, .{ .id = 11, .session_id = second_sid });
    try ctx.processMessage(.{ .id = 12, .method = "Runtime.evaluate", .sessionId = second_sid, .params = .{ .expression = "({value:6})", .returnByValue = false } });
    try ctx.expectSentResult(.{ .result = .{ .type = "object" } }, .{ .id = 12, .session_id = second_sid });
    const object_id = blk: {
        for (ctx.received.items) |msg| {
            const input_id = msg.object.get("id") orelse continue;
            if (input_id.integer == 12) break :blk msg.object.get("result").?.object.get("result").?.object.get("objectId").?.string;
        }
        return error.MissingRemoteObject;
    };
    try ctx.processMessage(.{ .id = 13, .method = "Runtime.callFunctionOn", .sessionId = second_sid, .params = .{
        .objectId = object_id,
        .functionDeclaration = "function(arg, other) { return Promise.resolve(this.value + arg + other.value); }",
        .arguments = .{ .{ .value = 3 }, .{ .objectId = object_id } },
        .awaitPromise = true,
        .returnByValue = true,
        .userGesture = true,
    } });
    try ctx.expectSentResult(.{ .result = .{ .type = "number", .value = 15 } }, .{ .id = 13, .session_id = second_sid });
    try ctx.processMessage(.{ .id = 14, .method = "Runtime.releaseObject", .sessionId = second_sid, .params = .{ .objectId = object_id } });
    _ = try second.attachSession("SID-NAV-AUX", null);
    try ctx.processMessage(.{ .id = 15, .method = "Page.navigate", .sessionId = "SID-NAV-AUX", .params = .{ .url = "about:blank" } });
    try ctx.expectSentResult(.{ .frameId = second_id }, .{ .id = 15, .session_id = "SID-NAV-AUX" });
    try ctx.processMessage(.{ .id = 16, .method = "Runtime.evaluate", .sessionId = first_sid, .params = .{ .expression = "tabMarker", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "first" } }, .{ .id = 16, .session_id = first_sid });
    try ctx.processMessage(.{ .id = 17, .method = "Target.activateTarget", .params = .{ .targetId = first_id } });
    try testing.expect(ctx.cdp().defaultTarget().? == first);
    try ctx.processMessage(.{ .id = 18, .method = "Target.closeTarget", .params = .{ .targetId = second_id } });
    try testing.expect(first.mainPage() != null);
    try testing.expect(second.mainPage() == null);
    try testing.expect(ctx.cdp().targetById(&second_id) == null);
    try ctx.processMessage(.{ .id = 19, .method = "Runtime.evaluate", .sessionId = first_sid, .params = .{ .expression = "tabMarker", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "first" } }, .{ .id = 19, .session_id = first_sid });
}

test "cdp.target: tabs share native origin storage and route network events" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const url = "http://127.0.0.1:9582/src/browser/tests/hi.html";
    try ctx.processMessage(.{ .id = 1, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const first = &ctx.cdp().browser_context.?;
    try ctx.processMessage(.{ .id = 2, .method = "Target.attachToTarget", .params = .{ .targetId = first.target_id.? } });
    const first_sid = first.session_id.?;
    try ctx.processMessage(.{ .id = 3, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const second = ctx.cdp().targets.items[0];
    try ctx.processMessage(.{ .id = 4, .method = "Target.attachToTarget", .params = .{ .targetId = second.target_id.? } });
    const second_sid = second.session_id.?;
    try ctx.processMessage(.{ .id = 5, .method = "Network.enable", .sessionId = first_sid });
    try ctx.processMessage(.{ .id = 6, .method = "Network.enable", .sessionId = second_sid });
    try ctx.processMessage(.{ .id = 7, .method = "Page.navigate", .sessionId = first_sid, .params = .{ .url = url } });
    try testing.waitForPage(first);
    try ctx.expectSentEvent("Network.requestWillBeSent", .{ .frameId = first.target_id.?, .request = .{ .url = url } }, .{ .session_id = first_sid });
    try ctx.processMessage(.{ .id = 8, .method = "Runtime.evaluate", .sessionId = first_sid, .params = .{
        .expression = "localStorage.setItem('native-tabs', 'shared'); document.cookie = 'native_tab=shared; Path=/'; 'written'",
        .returnByValue = true,
    } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "written" } }, .{ .id = 8, .session_id = first_sid });
    try ctx.processMessage(.{ .id = 9, .method = "Page.navigate", .sessionId = second_sid, .params = .{ .url = url } });
    try testing.waitForPage(second);
    try ctx.expectSentEvent("Network.requestWillBeSent", .{ .frameId = second.target_id.?, .request = .{ .url = url } }, .{ .session_id = second_sid });
    try ctx.processMessage(.{ .id = 10, .method = "Runtime.evaluate", .sessionId = second_sid, .params = .{
        .expression = "localStorage.getItem('native-tabs') + '|' + String(document.cookie.includes('native_tab=shared'))",
        .returnByValue = true,
    } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "shared|true" } }, .{ .id = 10, .session_id = second_sid });
}

// A freshly created blank target (the default CDP Target.createTarget) is
// left unnavigated until a real navigation happens, so Navigation._entries
// is empty. Reading history.state/navigation.currentEntry there used to hit
// the hard `lp.assert(len > 0, ...)` in Navigation.getCurrentEntry and crash
// the whole process instead of just failing the one command.
test "cdp.target: history.state on freshly created blank target doesn't crash" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 40, .method = "Target.setAutoAttach", .params = .{ .autoAttach = true, .waitForDebuggerOnStart = false } });
    try ctx.processMessage(.{ .id = 41, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const bc = &ctx.cdp().browser_context.?;
    const session_id = bc.session_id.?;

    try ctx.processMessage(.{ .id = 42, .method = "Runtime.enable", .sessionId = session_id });
    try ctx.processMessage(.{ .id = 43, .method = "Runtime.evaluate", .sessionId = session_id, .params = .{
        .expression = "history.state",
    } });
    try ctx.expectSentResult(.{ .result = .{ .type = "object", .subtype = "null", .value = null } }, .{ .id = 43 });
}

test "cdp.target: navigation.currentEntry on freshly created blank target doesn't crash" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 40, .method = "Target.setAutoAttach", .params = .{ .autoAttach = true, .waitForDebuggerOnStart = false } });
    try ctx.processMessage(.{ .id = 41, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const bc = &ctx.cdp().browser_context.?;
    const session_id = bc.session_id.?;

    try ctx.processMessage(.{ .id = 42, .method = "Runtime.enable", .sessionId = session_id });
    try ctx.processMessage(.{ .id = 43, .method = "Runtime.evaluate", .sessionId = session_id, .params = .{
        .expression = "navigation.currentEntry.url",
    } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "about:blank" } }, .{ .id = 43 });
}

// A browser-target session (Target.attachToBrowserTarget) is distinct from
// the page-target session. It used to be stored in bc.session_id, which broke
// the "no target => no session_id" invariant asserted in createTarget.
test "cdp.target: attachToBrowserTarget then createTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 1, .method = "Target.attachToBrowserTarget" });
    try ctx.expectSentEvent("Target.attachedToTarget", .{ .sessionId = "BSID-1", .targetInfo = .{ .targetId = "browser", .title = "", .url = "", .attached = true, .type = "browser", .canAccessOpener = false } }, .{});
    try ctx.expectSentResult(.{ .sessionId = "BSID-1" }, .{ .id = 1 });
    try testing.expectEqual(null, ctx.cdp().browser_context);

    try ctx.processMessage(.{ .id = 2, .method = "Target.createBrowserContext" });
    const bc = &ctx.cdp().browser_context.?;
    try ctx.expectSentResult(.{ .browserContextId = bc.id }, .{ .id = 2 });
    try testing.expectEqual(null, bc.session_id);

    {
        try ctx.processMessage(.{ .id = 3, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
        try ctx.expectSentResult(.{ .targetId = bc.target_id.? }, .{ .id = 3 });
    }

    {
        // the browser session id is valid on subsequent commands
        try ctx.processMessage(.{ .id = 4, .method = "Target.setDiscoverTargets", .sessionId = "BSID-1", .params = .{ .discover = true } });
        try ctx.expectSentResult(null, .{ .id = 4, .session_id = "BSID-1" });
    }

    {
        // unknown session ids are still rejected
        try ctx.processMessage(.{ .id = 5, .method = "Target.setDiscoverTargets", .sessionId = "SID-NOPE", .params = .{ .discover = true } });
        try ctx.expectSentError(-32001, "Unknown sessionId", .{ .id = 5 });
    }
}

test "cdp.target: closeTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();

    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.closeTarget", .params = .{ .targetId = "X" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.closeTarget", .params = .{ .targetId = "TID-8" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    // pretend we createdTarget first
    _ = try bc.session.createPage();
    bc.target_id = "TID-000000000A".*;
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.closeTarget", .params = .{ .targetId = "TID-8" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    {
        try ctx.processMessage(.{ .id = 11, .method = "Target.closeTarget", .params = .{ .targetId = "TID-000000000A" } });
        try ctx.expectSentResult(.{ .success = true }, .{ .id = 11 });
        try testing.expectEqual(false, bc.session.hasPage());
        try testing.expectEqual(null, bc.target_id);
    }
}

test "cdp.target: attachToTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();

    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.attachToTarget", .params = .{ .targetId = "X" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.attachToTarget", .params = .{ .targetId = "TID-8" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    // pretend we createdTarget first
    _ = try bc.session.createPage();
    bc.target_id = "TID-000000000B".*;
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.attachToTarget", .params = .{ .targetId = "TID-8" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    {
        // the first attach becomes the primary session
        try ctx.processMessage(.{ .id = 11, .method = "Target.attachToTarget", .params = .{ .targetId = "TID-000000000B" } });
        const session_id = bc.session_id.?;
        try ctx.expectSentResult(.{ .sessionId = session_id }, .{ .id = 11 });
        try ctx.expectSentEvent("Target.attachedToTarget", .{ .sessionId = session_id, .targetInfo = .{ .url = "about:blank", .title = "", .attached = true, .type = "page", .canAccessOpener = false, .browserContextId = "BID-9", .targetId = bc.target_id.? } }, .{});
        try testing.expectEqual(1, bc.attached_sessions.count());
        try testing.expect(bc.attached_sessions.contains(session_id));
    }
}

test "cdp.target: auxiliary session is unique and routed through its parent" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{
        .id = "BID-9",
        .session_id = "SID-PRIMARY",
        .target_id = "TID-000000000B".*,
    });

    try ctx.processMessage(.{ .id = 1, .method = "Target.attachToBrowserTarget" });
    try ctx.processMessage(.{
        .id = 2,
        .method = "Target.attachToTarget",
        .sessionId = "BSID-1",
        .params = .{ .targetId = "TID-000000000B" },
    });

    // the primary is attached first, the auxiliary session after it
    try testing.expectEqual(2, bc.attached_sessions.count());
    const session_id = bc.attached_sessions.keys()[1];
    try testing.expect(!std.mem.eql(u8, session_id, bc.session_id.?));
    try ctx.expectSentEvent("Target.attachedToTarget", .{
        .sessionId = session_id,
        .targetInfo = .{
            .url = "about:blank",
            .title = "",
            .attached = true,
            .type = "page",
            .canAccessOpener = false,
            .browserContextId = "BID-9",
            .targetId = "TID-000000000B",
        },
    }, .{ .index = 2, .session_id = "BSID-1" });
    try ctx.expectSentResult(.{ .sessionId = session_id }, .{
        .id = 2,
        .session_id = "BSID-1",
    });
}

test "cdp.target: getTargetInfo" {
    var ctx = try testing.context();
    defer ctx.deinit();

    {
        try ctx.processMessage(.{ .id = 9, .method = "Target.getTargetInfo" });
        try ctx.expectSentResult(.{
            .targetInfo = .{
                .type = "browser",
                .title = "",
                .url = "about:blank",
                .attached = true,
                .canAccessOpener = false,
            },
        }, .{ .id = 9 });
    }

    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.getTargetInfo", .params = .{ .targetId = "X" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.getTargetInfo", .params = .{ .targetId = "TID-8" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    // pretend we createdTarget first
    _ = try bc.session.createPage();
    bc.target_id = "TID-000000000C".*;
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.getTargetInfo", .params = .{ .targetId = "TID-8" } });
        try ctx.expectSentError(-31998, "UnknownTargetId", .{ .id = 10 });
    }

    {
        try ctx.processMessage(.{ .id = 11, .method = "Target.getTargetInfo", .params = .{ .targetId = "TID-000000000C" } });
        try ctx.expectSentResult(.{
            .targetInfo = .{
                .targetId = "TID-000000000C",
                .type = "page",
                .title = "",
                .url = "about:blank",
                .attached = false,
                .canAccessOpener = false,
            },
        }, .{ .id = 11 });
    }
}

test "cdp.target: issue#474: attach to just created target" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    {
        try ctx.processMessage(.{ .id = 10, .method = "Target.createTarget", .params = .{ .browserContextId = "BID-9" } });
        try testing.expectEqual(true, bc.target_id != null);
        try ctx.expectSentResult(.{ .targetId = bc.target_id.? }, .{ .id = 10 });

        try ctx.processMessage(.{ .id = 11, .method = "Target.attachToTarget", .params = .{ .targetId = bc.target_id.? } });
        const session_id = bc.session_id.?;
        try ctx.expectSentResult(.{ .sessionId = session_id }, .{ .id = 11 });
    }
}

test "cdp.target: detachFromTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    try ctx.processMessage(.{ .id = 10, .method = "Target.createTarget", .params = .{ .browserContextId = "BID-9" } });
    try ctx.processMessage(.{ .id = 11, .method = "Target.attachToTarget", .params = .{ .targetId = bc.target_id.? } });
    const session_id = bc.session_id.?;

    try ctx.processMessage(.{ .id = 12, .method = "Target.detachFromTarget", .params = .{ .sessionId = session_id } });

    try ctx.expectSentEvent("Target.detachedFromTarget", .{ .sessionId = session_id }, .{});
    try testing.expectEqual(null, bc.session_id);
    try ctx.expectSentResult(null, .{ .id = 12 });

    try ctx.processMessage(.{ .id = 13, .method = "Target.attachToTarget", .params = .{ .targetId = bc.target_id.? } });
    try testing.expect(!std.mem.eql(u8, session_id, bc.session_id.?));
    try ctx.expectSentResult(.{ .sessionId = bc.session_id.? }, .{ .id = 13 });
}

test "cdp.target: detachFromTarget auxiliary session" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{
        .id = "BID-9",
        .session_id = "SID-PRIMARY",
        .target_id = "TID-000000000B".*,
    });

    try ctx.processMessage(.{ .id = 10, .method = "Target.attachToTarget", .params = .{ .targetId = "TID-000000000B" } });
    try testing.expectEqual(2, bc.attached_sessions.count());
    const session_id = bc.attached_sessions.keys()[1];
    try testing.expect(!std.mem.eql(u8, session_id, bc.session_id.?));

    try ctx.processMessage(.{ .id = 11, .method = "Target.detachFromTarget", .params = .{ .sessionId = session_id } });
    try ctx.expectSentEvent("Target.detachedFromTarget", .{ .sessionId = session_id }, .{});
    try testing.expectEqual(1, bc.attached_sessions.count());
    try testing.expectEqual(true, bc.session_id != null);
    try ctx.expectSentResult(null, .{ .id = 11 });
}

// A detached session's inspector session goes with it: commands on the old
// id are rejected up front and the primary is unaffected.
test "cdp.target: detachFromTarget releases the auxiliary session's inspector session" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{
        .id = "BID-9",
        .url = "hi.html",
        .session_id = "SID-PRIMARY",
        .target_id = "TID-000000000B".*,
    });
    _ = try bc.attachSession("SID-AUX", null);

    try ctx.processMessage(.{ .id = 10, .method = "Runtime.evaluate", .sessionId = "SID-AUX", .params = .{ .expression = "1 + 1", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "number", .value = 2 } }, .{ .id = 10, .session_id = "SID-AUX" });

    try ctx.processMessage(.{ .id = 11, .method = "Target.detachFromTarget", .params = .{ .sessionId = "SID-AUX" } });
    try ctx.expectSentEvent("Target.detachedFromTarget", .{ .sessionId = "SID-AUX" }, .{});
    try ctx.expectSentResult(null, .{ .id = 11 });
    try testing.expectEqual(1, bc.attached_sessions.count());

    try ctx.processMessage(.{ .id = 12, .method = "Runtime.evaluate", .sessionId = "SID-AUX", .params = .{ .expression = "1 + 1", .returnByValue = true } });
    try ctx.expectSentError(-32001, "Unknown sessionId", .{ .id = 12 });

    try ctx.processMessage(.{ .id = 13, .method = "Runtime.evaluate", .sessionId = "SID-PRIMARY", .params = .{ .expression = "2 + 2", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "number", .value = 4 } }, .{ .id = 13, .session_id = "SID-PRIMARY" });
}

// Stopping an inspector session fails its pending evaluations. The client was
// already told the session is detached, so, like Chrome, nothing more is sent.
test "cdp.target: detachFromTarget drops the auxiliary session's pending responses" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{
        .id = "BID-9",
        .url = "hi.html",
        .session_id = "SID-PRIMARY",
        .target_id = "TID-000000000B".*,
    });
    _ = try bc.attachSession("SID-AUX", null);

    try ctx.processMessage(.{ .id = 10, .method = "Runtime.enable", .sessionId = "SID-AUX" });
    try ctx.expectSentResult(null, .{ .id = 10, .session_id = "SID-AUX" });
    try ctx.processMessage(.{ .id = 11, .method = "Runtime.evaluate", .sessionId = "SID-AUX", .params = .{ .expression = "({a: 1})" } });
    try ctx.expectSentResult(.{ .result = .{ .type = "object" } }, .{ .id = 11, .session_id = "SID-AUX" });
    try ctx.processMessage(.{ .id = 12, .method = "Runtime.evaluate", .sessionId = "SID-AUX", .params = .{ .expression = "new Promise(() => {})", .awaitPromise = true } });

    try ctx.processMessage(.{ .id = 13, .method = "Target.detachFromTarget", .params = .{ .sessionId = "SID-AUX" } });
    try ctx.expectSentEvent("Target.detachedFromTarget", .{ .sessionId = "SID-AUX" }, .{});
    try ctx.expectSentResult(null, .{ .id = 13 });

    var i: usize = 0;
    while (try ctx.getSentMessage(i)) |msg| : (i += 1) {
        const msg_id = msg.object.get("id") orelse continue;
        try testing.expect(msg_id != .integer or msg_id.integer != 12);
    }

    try ctx.processMessage(.{ .id = 14, .method = "Runtime.evaluate", .sessionId = "SID-PRIMARY", .params = .{ .expression = "2 + 2", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "number", .value = 4 } }, .{ .id = 14, .session_id = "SID-PRIMARY" });
}

test "cdp.target: detachFromTarget without session" {
    var ctx = try testing.context();
    defer ctx.deinit();
    _ = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    {
        // detach when no session is attached should not send event
        try ctx.processMessage(.{ .id = 10, .method = "Target.detachFromTarget" });
        try ctx.expectSentResult(null, .{ .id = 10 });
        try ctx.expectSentCount(1);
    }
}

test "cdp.target: setAutoAttach false sends detachedFromTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();
    const bc = try ctx.loadBrowserContext(.{ .id = "BID-9" });
    try ctx.processMessage(.{ .id = 10, .method = "Target.setAutoAttach", .params = .{ .autoAttach = true, .waitForDebuggerOnStart = false } });
    try ctx.processMessage(.{ .id = 11, .method = "Target.createTarget", .params = .{ .browserContextId = "BID-9" } });
    const session_id = bc.session_id.?;

    try ctx.processMessage(.{ .id = 12, .method = "Target.setAutoAttach", .params = .{ .autoAttach = false, .waitForDebuggerOnStart = false } });

    try ctx.expectSentEvent("Target.detachedFromTarget", .{ .sessionId = session_id }, .{});
    try testing.expectEqual(null, bc.session_id);
    try ctx.expectSentResult(null, .{ .id = 12 });
}

test "cdp.target: targetInfoChanged reports the page's URL and title" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{ .id = 1, .method = "Target.setDiscoverTargets", .params = .{ .discover = true } });
    try ctx.expectSentResult(null, .{ .id = 1 });

    const bc = try ctx.loadBrowserContext(.{ .id = "BID-TIC", .url = "cdp/accname.html", .target_id = "FID-000000000I".* });
    try ctx.expectSentEvent("Target.targetInfoChanged", .{ .targetInfo = .{
        .targetId = &bc.target_id.?,
        .url = bc.getURL().?,
        .title = "AccName Fixture",
        .browserContextId = "BID-TIC",
    } }, .{});
}

test "cdp.target: targetInfoChanged requires setDiscoverTargets" {
    var ctx = try testing.context();
    defer ctx.deinit();

    _ = try ctx.loadBrowserContext(.{ .url = "cdp/accname.html" });
    var i: usize = 0;
    while (try ctx.getSentMessage(i)) |msg| : (i += 1) {
        const method = msg.object.get("method") orelse continue;
        try testing.expect(!std.mem.eql(u8, method.string, "Target.targetInfoChanged"));
    }
    try testing.expect(i > 0);
}

test "cdp.target: setAutoAttach with page-exclude filter sends no attachedToTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();

    // Puppeteer's filter: exclude page targets, include the rest.
    try ctx.processMessage(.{
        .id = 1,
        .method = "Target.setAutoAttach",
        .params = .{
            .autoAttach = true,
            .waitForDebuggerOnStart = false,
            .filter = [_]TargetFilter{
                .{ .type = "page", .exclude = true },
                .{},
            },
        },
    });
    try ctx.expectSentResult(null, .{ .id = 1 });
    // The STARTUP placeholder is a page target: with pages
    // excluded, only the result may be sent.
    try ctx.expectSentCount(1);
}

test "cdp.target: setAutoAttach without filter still sends startup attachedToTarget" {
    var ctx = try testing.context();
    defer ctx.deinit();

    try ctx.processMessage(.{
        .id = 1,
        .method = "Target.setAutoAttach",
        .params = .{ .autoAttach = true, .waitForDebuggerOnStart = false },
    });
    try ctx.expectSentEvent("Target.attachedToTarget", .{
        .sessionId = "STARTUP",
        .targetInfo = .{
            .targetId = "TID-STARTUP",
            .type = "page",
            .title = "",
            .url = "about:blank",
            .browserContextId = "BID-STARTUP",
        },
    }, .{ .index = 0 });
    try ctx.expectSentResult(null, .{ .id = 1, .index = 1 });
    try ctx.expectSentCount(2);
}

test "cdp.target: native page notifications register tabs and preserve their target on navigation" {
    var ctx = try testing.context();
    defer ctx.deinit();
    try ctx.processMessage(.{ .id = 1, .method = "Target.createTarget", .params = .{ .url = "about:blank" } });
    const root = &ctx.cdp().browser_context.?;
    const first_frame = root.mainFrame().?;
    const native_page = try root.session.createPage();
    const native_id = id.toFrameId(native_page.frame_id);
    const target = ctx.cdp().targetById(&native_id) orelse return error.NativeTargetMissing;
    try testing.expect(target != root);
    try testing.expect(root.mainFrame().? == first_frame);
    try testing.expect(target.session == root.session);
    try ctx.processMessage(.{ .id = 2, .method = "Target.attachToTarget", .params = .{ .targetId = native_id, .flatten = true } });
    const sid = target.session_id.?;
    try ctx.processMessage(.{ .id = 3, .method = "Runtime.evaluate", .sessionId = sid, .params = .{ .expression = "globalThis.nativeMarker = 'tab'", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "tab" } }, .{ .id = 3, .session_id = sid });
    try ctx.processMessage(.{ .id = 4, .method = "Page.navigate", .sessionId = sid, .params = .{ .url = "about:blank" } });
    try ctx.expectSentResult(.{ .frameId = native_id }, .{ .id = 4, .session_id = sid });
    try testing.expectEqual(1, ctx.cdp().targets.items.len);
    try testing.expect(ctx.cdp().targetById(&native_id).? == target);
    try ctx.processMessage(.{ .id = 5, .method = "Target.closeTarget", .params = .{ .targetId = root.target_id.? } });
    try testing.expect(ctx.cdp().defaultTarget().? == target);
    try ctx.processMessage(.{ .id = 6, .method = "Runtime.evaluate", .sessionId = sid, .params = .{ .expression = "typeof nativeMarker", .returnByValue = true } });
    try ctx.expectSentResult(.{ .result = .{ .type = "string", .value = "undefined" } }, .{ .id = 6, .session_id = sid });
}
