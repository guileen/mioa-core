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

const js = @import("../../js/js.zig");
const URL = @import("../../URL.zig");
const Session = @import("../../Session.zig");
const Notification = @import("../../../Notification.zig");

const Cookie = @import("Cookie.zig");
const ProfileAsync = @import("ProfileAsync.zig");
const EventTarget = @import("../EventTarget.zig");
const CookieChangeEvent = @import("../event/CookieChangeEvent.zig");

const Execution = js.Execution;
const String = lp.String;

// https://developer.mozilla.org/en-US/docs/Web/API/CookieStore
const CookieStore = @This();

pub const Proto = EventTarget;

_proto: *EventTarget,
_exec: ?*Execution = null,
_profile_event_start: u64 = 0,
_pending_profile: std.DoublyLinkedList = .{},

pub fn asEventTarget(self: *CookieStore) *EventTarget {
    return self._proto;
}

/// Registers this CookieStore as a listener for jar-change notifications on
/// the given execution's session. Must be called once after construction by
/// the owning global (Window today; WorkerGlobalScope once wired up).
/// Idempotent on re-call.
pub fn attach(self: *CookieStore, exec: *Execution) !void {
    if (self._exec != null) return;
    if (exec.session.cookie_jar.profile_client) |client| {
        if (!client.cookie_event_watcher_ready or client.event_fd < 0 or client.failed) return error.ProfileCookieEventsUnavailable;
        if (!client.cookie_events_subscribed) {
            const reply = try client.request(exec.local_arena, "events.subscribe", struct {}{});
            defer reply.deinit();
            const result = reply.value.object.get("result") orelse {
                client.failed = true;
                return error.ProfileProtocolError;
            };
            const parsed = std.json.parseFromValue(struct { next_sequence: u64 }, exec.local_arena, result, .{}) catch {
                client.failed = true;
                return error.ProfileProtocolError;
            };
            defer parsed.deinit();
            client.cookie_events_next_sequence = parsed.value.next_sequence;
            self._profile_event_start = parsed.value.next_sequence;
            client.cookie_events_subscribed = true;
        } else {
            // A service-side watermark excludes events already published,
            // including complete or partial frames still queued on the FD.
            const reply = try client.request(exec.local_arena, "events.cursor", struct {}{});
            defer reply.deinit();
            const result = reply.value.object.get("result") orelse {
                client.failed = true;
                return error.ProfileProtocolError;
            };
            const parsed = std.json.parseFromValue(struct { next_sequence: u64 }, exec.local_arena, result, .{}) catch {
                client.failed = true;
                return error.ProfileProtocolError;
            };
            defer parsed.deinit();
            if (parsed.value.next_sequence < client.cookie_events_next_sequence) {
                client.failed = true;
                return error.ProfileProtocolError;
            }
            self._profile_event_start = parsed.value.next_sequence;
        }
    }
    try exec.session.notification.register(.cookie_changed, self, onCookieChanged);
    self._exec = exec;
}

/// Removes this CookieStore from the notification list.
pub fn detach(self: *CookieStore) void {
    const exec = self._exec orelse return;
    while (self._pending_profile.first) |node| {
        const request: *ProfileRequest = @fieldParentPtr("node", node);
        self._pending_profile.remove(node);
        request.store = null;
        if (request.resolver) |resolver| resolver.release();
        request.resolver = null;
        if (!request.scheduled) {
            if (exec.session.profile_async_client) |*client| client.cancel(request.id);
        }
    }
    exec.session.notification.unregisterAll(self);
    self._exec = null;
}
const ProfileEvent = struct {
    sequence: u64,
    type: enum { cookie },
    kind: Notification.CookieChanged.Kind,
    name: []const u8,
    value: []const u8,
    domain: []const u8,
    path: []const u8,
    secure: bool,
    http_only: bool,
    same_site: Cookie.SameSite,
};

const ProfileGateEvent = struct {
    sequence: u64,
    type: enum { idb_gate },
    origin: []const u8,
    ticket: []const u8,
    state: enum { granted, cancelled },
};

// Called on the session's JS thread when the event descriptor is readable.
// Partial frames stay in the client-owned buffer across readiness callbacks.
pub fn consumeProfileEvents(session: *Session) !void {
    try @import("../../TaskGate.zig").enter();
    defer @import("../../TaskGate.zig").exit();
    const client = session.cookie_jar.profile_client orelse return;
    errdefer {
        client.failed = true;
        session.idb.failAll(error.ProfileServiceLost);
    }
    if (client.failed) return error.ProfileServiceLost;
    if (client.event_fd < 0) return error.ProfileCookieEventsUnavailable;
    if (client.cookie_event_allocator == null) client.cookie_event_allocator = session.cookie_jar.allocator;
    try client.cookie_event_buffer.ensureTotalCapacity(session.cookie_jar.allocator, 4);
    var delivered: usize = 0;
    while (true) {
        while (client.cookie_event_used >= 4) {
            const length = std.mem.readInt(u32, client.cookie_event_buffer.items[0..4], .big);
            if (length == 0 or length > @import("ProfileIpc.zig").max_message) return error.ProfileProtocolError;
            const frame_size: usize = 4 + @as(usize, length);
            try client.cookie_event_buffer.ensureTotalCapacity(session.cookie_jar.allocator, frame_size);
            if (client.cookie_event_used < frame_size) break;
            if (!client.cookie_events_subscribed) return error.ProfileProtocolError;
            const allocator = session.cookie_jar.allocator;
            const raw = std.json.parseFromSlice(std.json.Value, allocator, client.cookie_event_buffer.items[4..frame_size], .{}) catch return error.ProfileProtocolError;
            defer raw.deinit();
            const envelope = std.json.parseFromValue(struct {
                sequence: u64,
                type: enum { cookie, idb_gate },
            }, allocator, raw.value, .{ .ignore_unknown_fields = true }) catch return error.ProfileProtocolError;
            defer envelope.deinit();
            if (envelope.value.sequence != client.cookie_events_next_sequence or envelope.value.sequence == std.math.maxInt(u64)) return error.ProfileProtocolError;
            switch (envelope.value.type) {
                .cookie => {
                    const event = std.json.parseFromValue(ProfileEvent, allocator, raw.value, .{}) catch return error.ProfileProtocolError;
                    defer event.deinit();
                    client.cookie_events_next_sequence += 1;
                    session.notification.dispatch(.cookie_changed, &.{
                        .kind = event.value.kind,
                        .name = event.value.name,
                        .value = event.value.value,
                        .domain = event.value.domain,
                        .path = event.value.path,
                        .secure = event.value.secure,
                        .http_only = event.value.http_only,
                        .same_site = event.value.same_site,
                    });
                },
                .idb_gate => {
                    const event = std.json.parseFromValue(ProfileGateEvent, allocator, raw.value, .{}) catch return error.ProfileProtocolError;
                    defer event.deinit();
                    client.cookie_events_next_sequence += 1;
                    switch (event.value.state) {
                        .granted => session.idb.receiveGrant(event.value.origin, event.value.ticket),
                        .cancelled => session.idb.receiveCancellation(event.value.origin, event.value.ticket),
                    }
                },
            }
            const remaining = client.cookie_event_used - frame_size;
            std.mem.copyForwards(u8, client.cookie_event_buffer.items[0..remaining], client.cookie_event_buffer.items[frame_size..client.cookie_event_used]);
            client.cookie_event_used = remaining;
            client.cookie_event_buffer.items.len = remaining;
            delivered += 1;
            if (delivered == 64) return;
        }
        // Read at most one frame at a time. After the work budget is spent,
        // any further frame remains in the socket and keeps readiness asserted.
        const end: usize = if (client.cookie_event_used < 4)
            4
        else
            4 + @as(usize, std.mem.readInt(u32, client.cookie_event_buffer.items[0..4], .big));
        const available = client.cookie_event_buffer.allocatedSlice()[client.cookie_event_used..end];
        if (available.len == 0) return error.ProfileProtocolError;
        const count = std.c.recv(client.event_fd, available.ptr, available.len, std.c.MSG.DONTWAIT);
        if (count == 0) return error.ProfileServiceLost;
        if (count < 0) switch (std.c.errno(count)) {
            .AGAIN => return,
            .INTR => continue,
            else => return error.ProfileServiceLost,
        };
        client.cookie_event_used += @intCast(count);
        client.cookie_event_buffer.items.len = client.cookie_event_used;
    }
}

fn onCookieChanged(ctx: *anyopaque, data: *const Notification.CookieChanged) !void {
    const self: *CookieStore = @ptrCast(@alignCast(ctx));
    const exec = self._exec orelse return;
    if (exec.session.cookie_jar.profile_client) |client| {
        // The receiver increments the cursor immediately before dispatch.
        if (client.cookie_events_next_sequence <= self._profile_event_start) return;
    }

    // CookieStore exposes only cookies that script would see for the
    // current document — same filter as `match` (HttpOnly hidden,
    // SameSite judged against the global's site-for-cookies).
    const doc_url = exec.url.*;
    const target = Cookie.PreparedUri.init(doc_url);
    if (target.host.len == 0) {
        return;
    }

    const probe = Cookie{
        .arena = undefined,
        .name = data.name,
        .value = data.value,
        .domain = data.domain,
        .path = data.path,
        .expires = null,
        .secure = data.secure,
        .http_only = data.http_only,
        .same_site = data.same_site,
    };
    const same_site = Cookie.areSameSite(exec.siteForCookies(), target.host);
    if (!probe.appliesTo(&target, .{ .same_site = same_site, .is_http = false })) {
        return;
    }

    // Rechecked in ChangeCallback.run, but between now and then, a listener
    // could be added which should NOT get this event (because it wasn't addded
    // at this point).
    if (!exec.hasDirectListeners(self.asEventTarget(), "change")) {
        return;
    }

    // Per spec, `change` is dispatched as a queued task — never synchronously
    // from the mutation site. We snapshot the notification fields onto a
    // small page arena that the scheduled callback releases after dispatch.
    const arena = try exec.getArena(.tiny, "CookieStore.change");
    errdefer arena.release();

    const cb = try arena.create(ChangeCallback);
    cb.* = .{
        .cookie_store = self,
        .exec = exec,
        .arena = arena,
        .kind = data.kind,
        .name = try arena.dupe(u8, data.name),
        .value = try arena.dupe(u8, data.value),
        .domain = try arena.dupe(u8, data.domain),
        .path = try arena.dupe(u8, data.path),
        .secure = data.secure,
        .same_site = data.same_site,
    };

    try exec.js.scheduler.add(cb, ChangeCallback.run, 0, .{
        .name = "CookieStore.change",
        .finalizer = ChangeCallback.cancelled,
    });
}

const ChangeCallback = struct {
    cookie_store: *CookieStore,
    // Execution is stored only to ensure we can release the arena.
    // The CookieStore could have been detached in the meantime, and so its
    // _exec pointer could have been reset.
    exec: *Execution,
    arena: *lp.Arena,
    kind: Notification.CookieChanged.Kind,
    name: []const u8,
    value: []const u8,
    domain: []const u8,
    path: []const u8,
    secure: bool,
    same_site: Cookie.SameSite,

    fn cancelled(ctx: *anyopaque) void {
        const self: *ChangeCallback = @ptrCast(@alignCast(ctx));
        self.releaseArena();
    }

    fn releaseArena(self: *ChangeCallback) void {
        self.arena.release();
    }

    fn run(ctx: *anyopaque) !?u32 {
        const self: *ChangeCallback = @ptrCast(@alignCast(ctx));
        defer self.releaseArena();

        const cs = self.cookie_store;
        // We use the CookieStore's exec here instead of self.exec to detect if
        // the store has been detached. In this case, we don't dispatch the
        // event.
        const exec = cs._exec orelse return null;
        const target = cs.asEventTarget();

        // Skip event construction when nobody is listening.
        if (!exec.hasDirectListeners(target, "change")) {
            return null;
        }

        const event = try CookieChangeEvent.initSingle(self.kind, .{
            .kind = self.kind,
            .name = self.name,
            .value = self.value,
            .domain = self.domain,
            .path = self.path,
            .secure = self.secure,
            .http_only = false,
            .same_site = self.same_site,
        }, exec);

        try exec.dispatch(target, event.asEvent(), .{
            .context = "CookieStore.change",
        });

        return null;
    }
};

// https://developer.mozilla.org/en-US/docs/Web/API/CookieStore/get
const GetOptions = struct {
    name: ?[]const u8 = null,
    url: ?[]const u8 = null,
};

const GetInput = union(enum) {
    name: []const u8,
    options: GetOptions,
};

// https://developer.mozilla.org/en-US/docs/Web/API/CookieStore/set
pub const CookieInit = struct {
    domain: ?[]const u8 = null,
    expires: ?f64 = null,
    maxAge: ?f64 = null,
    name: []const u8,
    partitioned: bool = false,
    path: []const u8 = "/",
    sameSite: SameSite = .strict,
    value: []const u8,
};

const SetInput = union(enum) {
    name: []const u8,
    options: CookieInit,
};

// https://developer.mozilla.org/en-US/docs/Web/API/CookieStore/delete
const DeleteOptions = struct {
    domain: ?[]const u8 = null,
    name: []const u8,
    partitioned: bool = false,
    path: []const u8 = "/",
};

const DeleteInput = union(enum) {
    name: []const u8,
    options: DeleteOptions,
};

pub const SameSite = enum {
    strict,
    lax,
    none,
    pub const js_enum_from_string = true;
};

pub fn get(self: *CookieStore, input: ?GetInput, exec: *const Execution) !js.Promise {
    const local = exec.js.local.?;

    const name: ?[]const u8, const url: ?[]const u8 = if (input) |inp| switch (inp) {
        .name => |n| .{ n, null },
        .options => |o| .{ o.name, o.url },
    } else .{ null, null };

    if (name == null and url == null) {
        // Unlike getAll(), get() requires a name or url
        return local.typeError("get requires a name or url");
    }
    if (exec.session.cookie_jar.profile_client != null) return self.profileGet(exec, name, url, true);

    const items = matchCookies(exec, name, url, true) catch |err| {
        return local.typeError(@errorName(err));
    };

    if (items.len == 0) {
        return local.resolvePromise(@as(?CookieListItem, null));
    }
    return local.resolvePromise(items[0]);
}

pub fn getAll(self: *CookieStore, input: ?GetInput, exec: *const Execution) !js.Promise {
    const local = exec.js.local.?;

    const name: ?[]const u8, const url: ?[]const u8 = if (input) |inp| switch (inp) {
        .name => |n| .{ n, null },
        .options => |o| .{ o.name, o.url },
    } else .{ null, null };
    if (exec.session.cookie_jar.profile_client != null) return self.profileGet(exec, name, url, false);

    const items = matchCookies(exec, name, url, false) catch |err| {
        return local.typeError(@errorName(err));
    };
    return local.resolvePromise(items);
}

pub fn set(self: *CookieStore, input: SetInput, value: ?[]const u8, exec: *const Execution) !js.Promise {
    const local = exec.js.local.?;

    const init: CookieInit = switch (input) {
        .options => |o| o,
        .name => |n| .{
            .name = n,
            .value = value orelse return local.typeError("value is required"),
        },
    };
    if (exec.session.cookie_jar.profile_client != null) return self.profileSet(exec, init, false);

    storeCookie(exec, init, false) catch |err| {
        return local.typeError(@errorName(err));
    };

    return local.resolvePromise({});
}

pub fn delete(self: *CookieStore, input: DeleteInput, exec: *const Execution) !js.Promise {
    const local = exec.js.local.?;

    const opts: DeleteOptions = switch (input) {
        .options => |o| o,
        .name => |n| .{ .name = n },
    };

    // Deletion per spec is an expired set: write a cookie with the same
    // name/path/domain but with Expires in the past, and the Jar will drop
    // any existing match (or no-op if none).
    const init: CookieInit = .{
        .name = opts.name,
        .value = "",
        .expires = 0,
        .domain = opts.domain,
        .path = opts.path,
        .sameSite = .strict,
        .partitioned = opts.partitioned,
    };
    if (exec.session.cookie_jar.profile_client != null) return self.profileSet(exec, init, true);
    storeCookie(exec, init, true) catch |err| {
        return local.typeError(@errorName(err));
    };

    return local.resolvePromise({});
}

fn profileGet(self: *CookieStore, exec: *const Execution, name: ?[]const u8, url: ?[]const u8, first: bool) !js.Promise {
    const local = exec.js.local.?;
    const resolved = resolveQueryUrl(exec, url) catch |err| return local.typeError(@errorName(err));
    if (Cookie.PreparedUri.init(resolved).host.len == 0) return local.typeError("SecurityError");
    const site: ?[]const u8 = switch (exec.siteForCookies()) {
        .none => null,
        .url => |value| value,
    };
    return self.profileRequest(exec, if (first) .get else .get_all, "cookies.store_get", .{
        .url = resolved,
        .site = site,
        .name = name,
        .first_only = first,
    });
}

fn profileSet(self: *CookieStore, exec: *const Execution, init: CookieInit, is_delete: bool) !js.Promise {
    const site: ?[]const u8 = switch (exec.siteForCookies()) {
        .none => null,
        .url => |value| value,
    };
    return self.profileRequest(exec, .write, "cookies.store_set", .{
        .url = exec.url.*,
        .site = site,
        .init = init,
        .is_delete = is_delete,
    });
}

fn profileRequest(self: *CookieStore, exec: *const Execution, kind: ProfileRequest.Kind, operation: []const u8, payload: anytype) !js.Promise {
    const local = exec.js.local.?;
    const event_client = exec.session.cookie_jar.profile_client orelse return local.typeError("ProfileServiceLost");
    if (event_client.failed or !event_client.cookie_event_watcher_ready) return local.typeError("ProfileServiceLost");
    const client = if (exec.session.profile_async_client) |*value| value else return local.typeError("ProfileServiceLost");
    const allocator = exec.session.cookie_jar.allocator;
    const request = try allocator.create(ProfileRequest);
    const resolver = local.createPromiseResolver();
    const global = resolver.persist() catch |err| {
        allocator.destroy(request);
        return err;
    };
    request.* = .{
        .store = self,
        .exec = @constCast(exec),
        .allocator = allocator,
        .resolver = global,
        .kind = kind,
    };
    request.id = client.submit(operation, payload, request, ProfileRequest.completed) catch |err| {
        global.release();
        allocator.destroy(request);
        return local.typeError(@errorName(err));
    };
    self._pending_profile.append(&request.node);
    return resolver.promise();
}

const ProfileRequest = struct {
    const Kind = enum { get, get_all, write };
    node: std.DoublyLinkedList.Node = .{},
    store: ?*CookieStore,
    exec: *Execution,
    allocator: std.mem.Allocator,
    resolver: ?js.PromiseResolver.Global,
    kind: Kind,
    id: u64 = 0,
    scheduled: bool = false,
    result: ?ProfileAsync.Result = null,

    fn release(self: *ProfileRequest) void {
        if (self.store) |store| store._pending_profile.remove(&self.node);
        if (self.result) |result| switch (result) {
            .reply => |reply| reply.deinit(),
            .failure => {},
        };
        if (self.resolver) |resolver| resolver.release();
        self.allocator.destroy(self);
    }

    fn completed(ctx: *anyopaque, result: ProfileAsync.Result) void {
        const self: *ProfileRequest = @ptrCast(@alignCast(ctx));
        self.result = result;
        if (self.store == null) {
            self.release();
            return;
        }
        self.scheduled = true;
        self.exec.js.scheduler.add(self, run, 0, .{
            .name = "CookieStore.profileReply",
            .finalizer = cancelled,
        }) catch self.release();
    }

    fn cancelled(ctx: *anyopaque) void {
        const self: *ProfileRequest = @ptrCast(@alignCast(ctx));
        self.release();
    }

    fn run(ctx: *anyopaque) !?u32 {
        const self: *ProfileRequest = @ptrCast(@alignCast(ctx));
        defer self.release();
        if (self.store == null) return null;
        var scope: js.Local.Scope = undefined;
        self.exec.js.localScope(&scope);
        defer scope.deinit();
        const resolver = self.resolver.?.local(&scope.local);
        const event_client = self.exec.session.cookie_jar.profile_client orelse {
            resolver.rejectError("CookieStore", .{ .type_error = "ProfileServiceLost" });
            return null;
        };
        if (event_client.failed or !event_client.cookie_event_watcher_ready) {
            resolver.rejectError("CookieStore", .{ .type_error = "ProfileServiceLost" });
            return null;
        }
        switch (self.result.?) {
            .failure => |err| resolver.rejectError("CookieStore", .{ .type_error = @errorName(err) }),
            .reply => |reply| {
                const result = reply.value.object.get("result") orelse {
                    resolver.rejectError("CookieStore", .{ .type_error = "ProfileProtocolError" });
                    return null;
                };
                if (self.kind == .write) {
                    if (result != .null) {
                        resolver.rejectError("CookieStore", .{ .type_error = "ProfileProtocolError" });
                    } else {
                        resolver.resolve("CookieStore", {});
                    }
                    return null;
                }
                const rows = std.json.parseFromValue([]StoreRow, self.allocator, result, .{}) catch {
                    resolver.rejectError("CookieStore", .{ .type_error = "ProfileProtocolError" });
                    return null;
                };
                defer rows.deinit();
                const items = self.allocator.alloc(CookieListItem, rows.value.len) catch {
                    resolver.rejectError("CookieStore", .{ .type_error = "OutOfMemory" });
                    return null;
                };
                defer self.allocator.free(items);
                for (rows.value, 0..) |row, i| items[i] = .{
                    .name = String.wrap(row.name),
                    .value = String.wrap(row.value),
                    .domain = if (row.domain) |domain| String.wrap(domain) else null,
                    .path = String.wrap(row.path),
                    .expires = row.expires,
                    .secure = row.secure,
                    .sameSite = row.sameSite,
                };
                if (self.kind == .get) {
                    if (items.len == 0) resolver.resolve("CookieStore.get", @as(?CookieListItem, null)) else resolver.resolve("CookieStore.get", items[0]);
                } else {
                    resolver.resolve("CookieStore.getAll", items);
                }
            },
        }
        return null;
    }
};

// Resolve the optional `url` per CookieStore.get/getAll spec. In a Window
// context, only the document's own URL is allowed (matches the cookie scope
// script already sees). In a Worker context, any same-origin URL is allowed.
fn resolveQueryUrl(exec: *const Execution, _override: ?[]const u8) ![:0]const u8 {
    const current = exec.url.*;
    const override = _override orelse return current;

    const resolved = try URL.resolve(exec.local_arena, exec.base(), override, .{});
    if (!exec.isSameOrigin(resolved)) {
        return error.SecurityError;
    }

    switch (exec.js.global) {
        .frame => {
            // URL that differs from document only by #hash is the same document URL
            if (!std.mem.eql(u8, URL.stripFragment(resolved), URL.stripFragment(current))) {
                return error.InvalidUrl;
            }
        },
        .worker => {},
    }
    return resolved;
}

fn matchCookies(
    exec: *const Execution,
    name: ?[]const u8,
    url: ?[]const u8,
    first_only: bool,
) ![]CookieListItem {
    const session = exec.session;
    const url_resolved = try resolveQueryUrl(exec, url);

    const target: Cookie.PreparedUri = .init(url_resolved);
    if (target.host.len == 0) {
        return error.SecurityError;
    }

    if (session.cookie_jar.profile_client != null) return error.ProfileCookieOperationUnsupported;

    try session.cookie_jar.removeExpired(null);

    // Cookie names are normalized. Apply the same normalization to the input
    // we're matching
    const normalized_name: ?[]const u8 = if (name) |n| std.mem.trim(u8, n, " \t") else null;

    const same_site = Cookie.areSameSite(exec.siteForCookies(), target.host);
    var items: std.ArrayList(CookieListItem) = .empty;
    for (session.cookie_jar.cookies.items) |*cookie| {
        // CookieStore exposes only cookies that script would see for the
        // current document. HttpOnly cookies stay hidden.
        if (cookie.appliesTo(&target, .{ .same_site = same_site, .is_http = false }) == false) {
            continue;
        }
        if (normalized_name) |n| {
            if (std.mem.eql(u8, cookie.name, n) == false) {
                continue;
            }
        }

        try items.append(exec.local_arena, .{
            .name = String.wrap(cookie.name),
            .value = String.wrap(cookie.value),
            .domain = if (cookie.domain.len > 0 and cookie.domain[0] == '.')
                String.wrap(cookie.domain[1..])
            else
                null,
            .path = String.wrap(cookie.path),
            .expires = if (cookie.expires) |e| e * 1000.0 else null,
            .secure = cookie.secure,
            .sameSite = switch (cookie.same_site) {
                .strict => "strict",
                .lax => "lax",
                .none => "none",
            },
            .partitioned = false,
        });
        if (first_only) {
            break;
        }
    }

    return items.items;
}
pub const StoreRow = struct {
    name: []const u8,
    value: []const u8,
    domain: ?[]const u8,
    path: []const u8,
    expires: ?f64,
    secure: bool,
    sameSite: []const u8,
};

fn storeCookie(exec: *const Execution, init_: CookieInit, is_delete: bool) !void {
    const jar = &exec.session.cookie_jar;
    const site = exec.siteForCookies();
    if (jar.profile_client != null) return error.ProfileCookieOperationUnsupported;
    return storeForJar(jar, exec.local_arena, exec.url.*, site, init_, is_delete);
}

// Shared by the in-process API and the Profile owner, so the owner enforces
// the same validation and non-HTTP cookie protections as a local store.
pub fn storeForJar(jar: *Cookie.Jar, scratch: std.mem.Allocator, url: [:0]const u8, site: Cookie.SiteForCookies, init_: CookieInit, is_delete: bool) !void {
    if (jar.profile_client != null) return error.ProfileCookieOperationUnsupported;

    var init = init_;

    init.name = std.mem.trim(u8, init.name, " \t");
    init.value = std.mem.trim(u8, init.value, " \t");
    if (init.name.len + init.value.len > Cookie.max_cookie_size) return error.CookieSizeExceeded;

    if (init.maxAge) |max_age| {
        if (init.expires != null) {
            // maxAge and expires are mutually exclusive
            return error.InvalidArgument;
        }
        // convert to absolute time, so the rest of the code is shared for
        // maxAge and expires.
        init.expires = (@as(f64, @floatFromInt(lp.datetime.timestamp(.real))) + max_age) * 1000.0;
    }

    if (init.expires) |ms| {
        // 400 day expiry limit, per spec.
        const cap_ms = (@as(f64, @floatFromInt(lp.datetime.timestamp(.real))) + 400 * std.time.s_per_day) * 1000.0;
        init.expires = @min(ms, cap_ms);
    }

    // delete() may legitimately target a nameless cookie — its value is always empty.
    if (!is_delete and init.name.len == 0) {
        if (init.value.len == 0) {
            return error.InvalidCookieName;
        }
        if (std.mem.findScalar(u8, init.value, '=') != null) {
            return error.InvalidCookieName;
        }
        if (Cookie.hasHiddenPrefix(init.value)) {
            return error.InvalidCookieName;
        }
    }

    // Reject inputs the cookie model can't represent. `=` is allowed in
    // values but not in names; `;` and the control characters (U+0000–U+001F,
    // U+007F) break the cookie wire format and so are forbidden in both.
    if (std.mem.findScalar(u8, init.name, '=') != null) {
        return error.InvalidCookieName;
    }
    if (hasForbiddenChar(init.name)) {
        return error.InvalidCookieName;
    }
    if (hasForbiddenChar(init.value)) {
        return error.InvalidCookieValue;
    }

    // A path attribute, when given, must be absolute. The Cookie path/domain
    // attribute values are also capped at 1024 bytes per spec.
    // https://cookiestore.spec.whatwg.org/#cookie-maximum-attribute-value-size
    if (init.path.len > 0 and init.path[0] != '/') {
        return error.InvalidCookiePath;
    }
    if (init.path.len > 1024) {
        return error.InvalidCookiePath;
    }
    if (std.mem.findAny(u8, init.path, ";\r\n\x00") != null) {
        return error.InvalidCookiePath;
    }
    if (init.domain) |d| {
        // CookieStore (unlike the HTTP cookie syntax) rejects a leading dot.
        if (d.len > 0 and d[0] == '.') {
            return error.InvalidCookieDomain;
        }
        if (d.len > 1024) {
            return error.InvalidCookieDomain;
        }
        if (std.mem.findAny(u8, d, ";\r\n\x00") != null) {
            return error.InvalidCookieDomain;
        }
    }

    if (init.sameSite != .none and Cookie.areSameSite(site, URL.getHostname(url)) == false) {
        return error.SameSiteBlocked;
    }

    const trustworthy = URL.isPotentiallyTrustworthy(url);
    // Per spec, SameSite=None requires Secure. CookieStore additionally
    // marks any cookie written from a trustworthy origin as Secure.
    const secure = trustworthy or init.sameSite == .none;

    // Script can't set HttpOnly, so __Http- and __Host-Http- never pass.
    if (Cookie.prefixOf(init.name)) |prefix| {
        const resolved_path = try Cookie.parsePath(scratch, url, init.path);
        if (!prefix.allows(trustworthy, false, if (init.domain) |d| d.len > 0 else false, resolved_path)) {
            return error.InvalidPrefixedCookie;
        }
    }

    // The errdefer only protects construction failures. Once we `break :blk`
    // with the Cookie value, `Jar.add` owns its lifetime.
    const cookie: Cookie = blk: {
        var arena = std.heap.ArenaAllocator.init(jar.allocator);
        errdefer arena.deinit();
        const aa = arena.allocator();

        const owned_name = try aa.dupe(u8, init.name);
        const owned_value = try aa.dupe(u8, init.value);
        const owned_path = try Cookie.parsePath(aa, url, init.path);
        const owned_domain = try Cookie.parseDomain(aa, url, init.domain);

        break :blk .{
            .arena = arena,
            .name = owned_name,
            .value = owned_value,
            .path = owned_path,
            .domain = owned_domain,

            // CookieStore.expires is a unix timestamp in milliseconds; Cookie tracks
            // expiry in seconds. A timestamp at or before "now" deletes the cookie via
            // the Jar's expiry path.
            .expires = if (init.expires) |ms| ms / 1000.0 else null,

            .secure = secure,
            .http_only = false,
            .same_site = switch (init.sameSite) {
                .strict => .strict,
                .lax => .lax,
                .none => .none,
            },
        };
    };

    // CookieStore is a script API, so is_http = false.
    try jar.add(cookie, lp.datetime.timestamp(.real), false);
}

// Control characters (U+0000–U+001F and U+007F DEL) and `;` cannot appear in
// a cookie name or value. The whitespace chars TAB and SPACE are trimmed
// before this check, so the surviving controls are all genuinely invalid.
fn hasForbiddenChar(s: []const u8) bool {
    for (s) |c| {
        if (c <= 0x1F or c == 0x7F or c == ';') {
            return true;
        }
    }
    return false;
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(CookieStore);

    pub const Meta = struct {
        pub const name = "CookieStore";
        pub var class_id: bridge.ClassId = undefined;
    };

    pub const get = bridge.function(CookieStore.get, .{});
    pub const getAll = bridge.function(CookieStore.getAll, .{});
    pub const set = bridge.function(CookieStore.set, .{});
    pub const delete = bridge.function(CookieStore.delete, .{});
    pub const onchange = EventTarget.handlerAccessor(CookieStore, "change");
};

// CookieListItem is an plain JavaScript object, not an interface. The bridge
// automatically translate a Zig struct -> JS Object This should _not_ have a
// JsApi.
//
// NOTE: Per spec, name and value are the only valid fields. All other fields
// are experimental. Chrome exposes them all, so we do too.
pub const CookieListItem = struct {
    domain: ?String = null,
    expires: ?f64 = null,
    name: String,
    partitioned: bool = false,
    path: String = String.wrap("/"),
    sameSite: []const u8 = "strict",
    secure: bool = false,
    // Optional because a deletion change-event reports the removed cookie with
    // `value` omitted (serialized as undefined via the `deleted` accessor's
    // null_as_undefined). For get/getAll and `changed` items it is always set.
    value: ?String = null,
};

const testing = @import("../../../testing.zig");
const HttpClient = @import("../../../network/HttpClient.zig");

test "WebApi: CookieStore" {
    try testing.htmlRunner("cookie_store.html", .{});
}

test "CookieStore: cross-site frame" {
    defer testing.test_session.closeAllPages();
    const frame = try testing.createFrame();
    const exec = &frame.js.execution;
    const jar = &frame._session.cookie_jar;
    defer jar.clearRetainingCapacity() catch unreachable;

    // victim.example embedded by attacker.example: the ancestor chain is
    // cross-site, so the frame has no site for cookies.
    var top_url: [:0]const u8 = "https://attacker.example/";
    var top: HttpClient.Owner = undefined;
    top.url = &top_url;
    top.parent = null;
    frame.url = "https://victim.example/inner";
    frame._http_owner.parent = &top;
    defer frame._http_owner.parent = null;

    try jar.populateFromResponse("https://victim.example/", "strict=1; SameSite=Strict");
    try jar.populateFromResponse("https://victim.example/", "lax=2; SameSite=Lax");
    try jar.populateFromResponse("https://victim.example/", "none=3; SameSite=None; Secure");

    // getAll(): only SameSite=None is visible from a cross-site context.
    {
        const items = try matchCookies(exec, null, null, false);
        try testing.expectEqual(1, items.len);
        try testing.expectEqual("none", items[0].name.str());
    }

    // set(): the default (Strict) and Lax are rejected, None is stored.
    // delete() is an expiring Strict set, so it is rejected too.
    try std.testing.expectError(error.SameSiteBlocked, storeCookie(exec, .{ .name = "set_strict", .value = "4" }, false));
    try std.testing.expectError(error.SameSiteBlocked, storeCookie(exec, .{ .name = "set_lax", .value = "5", .sameSite = .lax }, false));
    try storeCookie(exec, .{ .name = "set_none", .value = "6", .sameSite = .none }, false);
    try std.testing.expectError(error.SameSiteBlocked, storeCookie(exec, .{ .name = "none", .value = "", .expires = 0 }, true));
    {
        const items = try matchCookies(exec, null, null, false);
        try testing.expectEqual(2, items.len);
        try testing.expectEqual("none", items[0].name.str());
        try testing.expectEqual("set_none", items[1].name.str());
    }

    // The same jar seen from a same-site chain: everything applies, and the
    // rejected writes really were rejected rather than hidden.
    top_url = "https://victim.example/";
    {
        const items = try matchCookies(exec, null, null, false);
        try testing.expectEqual(4, items.len);
        try testing.expectEqual("strict", items[0].name.str());
        try testing.expectEqual("lax", items[1].name.str());
        try testing.expectEqual("none", items[2].name.str());
        try testing.expectEqual("set_none", items[3].name.str());
    }
    try storeCookie(exec, .{ .name = "set_strict", .value = "4" }, false);
    try storeCookie(exec, .{ .name = "none", .value = "", .expires = 0 }, true);
    {
        // The jar swap-removes on delete, so only check membership here.
        const items = try matchCookies(exec, null, null, false);
        try testing.expectEqual(4, items.len);
        var has_set_strict = false;
        for (items) |item| {
            try testing.expectEqual(false, std.mem.eql(u8, "none", item.name.str()));
            if (std.mem.eql(u8, "set_strict", item.name.str())) has_set_strict = true;
        }
        try testing.expectEqual(true, has_set_strict);
    }

    // Back in the cross-site context, the Strict cookie written same-site is
    // hidden again.
    top_url = "https://attacker.example/";
    {
        const items = try matchCookies(exec, null, null, false);
        try testing.expectEqual(1, items.len);
        try testing.expectEqual("set_none", items[0].name.str());
    }
}
