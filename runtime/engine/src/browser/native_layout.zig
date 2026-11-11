const std = @import("std");
const lp = @import("lightpanda");
const Frame = @import("Frame.zig");
const Node = @import("webapi/Node.zig");
const Element = @import("webapi/Element.zig");
const DOMRect = @import("webapi/DOMRect.zig");
const Allocator = std.mem.Allocator;

pub const Document = opaque {};
const Rect = extern struct { x: f64 = 0, y: f64 = 0, width: f64 = 0, height: f64 = 0 };
/// Env-gated frame-path probes (`MIOA_ENGINE_TIMING=<path>`).
///
/// Inlined here on purpose: the desktop bootstrap rebuilds its MANIFEST from
/// `git ls-files`, so a probe in an untracked file makes every staged bundle
/// fail as `bootstrap_invalid_bundle` before a single frame is measured.
///
/// One `fopen`+`fwrite` per line, so concurrent writers interleave whole lines;
/// inert unless the variable is set.
pub const perf = struct {
    var resolved = false;
    var destination: ?[*:0]u8 = null;

    pub fn path() ?[*:0]u8 {
        if (!resolved) {
            destination = std.c.getenv("MIOA_ENGINE_TIMING");
            resolved = true;
        }
        return destination;
    }

    pub fn enabled() bool {
        return path() != null;
    }

    pub fn now() u64 {
        return @import("../datetime.zig").microTimestamp(.awake);
    }

    pub fn log(comptime tag: []const u8, comptime fmt: []const u8, args: anytype) void {
        const file_path = path() orelse return;
        var buf: [256]u8 = undefined;
        const line = std.fmt.bufPrint(&buf, tag ++ " " ++ fmt ++ "\n", args) catch return;
        const file = std.c.fopen(file_path, "a") orelse return;
        defer _ = std.c.fclose(file);
        _ = std.c.fwrite(line.ptr, 1, line.len, file);
    }
};

const Projection = struct {
    key: usize,
    parent: ?usize,
    tag: ?[]const u8 = null,
    text: ?[]const u8 = null,
    namespace: ?[]const u8 = null,
    attrs: []const [2][]const u8 = &.{},
    scroll_x: f32 = 0,
    scroll_y: f32 = 0,
    sheet: ?[]const u8 = null,
    shadow_host: ?usize = null,
    shadow_closed: bool = false,
    focused: bool = false,
    hovered: bool = false,
    active: bool = false,
    focus_visible: bool = false,
    selection_focus: ?usize = null,
    selection_node: ?usize = null,
};

/// Env-gated layout timing (`MIOA_ENGINE_TIMING=<path>`). One appended line per
/// layout call splits a 30-95 ms frame into projection, JSON serialisation and
/// the native renderer call, instead of inferring the split from aggregate CPU.
/// Inert unless the variable is set.
fn engineTime() u64 {
    return @import("../datetime.zig").microTimestamp(.awake);
}

fn appendTiming(path: [*:0]const u8, line: []const u8) void {
    // The path comes from getenv and is already NUL-terminated; 0.17 has no
    // std.fs.cwd() and no std.fmt.bufPrintZ, so append through libc.
    const file = std.c.fopen(path, "a") orelse return;
    defer _ = std.c.fclose(file);
    _ = std.c.fwrite(line.ptr, 1, line.len, file);
}

pub fn viewport(frame: *Frame) @import("Viewport.zig") {
    const result = frame.page.getViewport();
    if (frame.iframe) |iframe| {
        if (frame._embedding_viewport) |embedded| return embedded;
        const element = iframe.asElement();
        const owner = element.ownerFrame(frame) orelse return result;
        _ = document(owner) catch return result;
        return frame._embedding_viewport orelse result;
    }
    return result;
}
fn inputTarget(frame: *Frame, target: ?*Element) ?*Element {
    var element = target orelse return null;
    var owner = element.ownerFrame(frame) orelse return null;
    while (owner != frame) {
        const iframe = owner.iframe orelse return null;
        element = iframe.asElement();
        owner = element.ownerFrame(owner) orelse return null;
    }
    return element;
}

pub fn document(frame: *Frame) !*Document {
    // Zig 0.17 dropped std.posix.getenv; the libc binding is the stable one.
    const timing_env: ?[*:0]u8 = std.c.getenv("MIOA_ENGINE_TIMING");
    const t0: u64 = if (timing_env != null) engineTime() else 0;
    const lease = try frame._session.getArena(.large, "native-layout");
    defer lease.release();
    const arena = lease.allocator();
    var projected: std.ArrayList(Projection) = .empty;
    var stack: std.ArrayList(*Node) = .empty;
    try stack.append(arena, frame.document.asNode());
    const hovered = inputTarget(frame, frame.page.input_hover_target);
    const active = inputTarget(frame, frame.page.input_pointer.down_target);
    while (stack.pop()) |node| {
        var item: Projection = .{ .key = @intFromPtr(node), .parent = if (node.parentNode()) |parent| @intFromPtr(parent) else null };
        if (node.is(Element)) |el| {
            item.tag = el.getTagNameLower();
            item.namespace = el.getNamespaceURI();
            item.focused = frame.document._active_element == el;
            item.focus_visible = el.hasVisibleFocus(frame);
            item.hovered = hovered == el;
            item.active = active == el;
            var attrs: std.ArrayList([2][]const u8) = .empty;
            const inline_style = el.inlineStyle(frame);
            const input = el.is(Element.Html.Input);
            for (el.attributeEntries()) |*attr| {
                if (inline_style != null and std.mem.eql(u8, attr.name(), "style")) continue;
                if (input != null and std.mem.eql(u8, attr.name(), "value")) continue;
                try attrs.append(arena, .{ attr.name(), attr.value() });
            }
            if (inline_style) |style| {
                var writer: std.Io.Writer.Allocating = .init(arena);
                try style.asCSSStyleDeclaration().format(&writer.writer);
                try attrs.append(arena, .{ "style", writer.written() });
            }
            if (input) |control| {
                try attrs.append(arena, .{ "value", control.getValue() });
                if (control.tracksSelection()) item.selection_focus = controlSelectionFocus(control);
            }
            if (el.is(Element.Html.TextArea)) |control| {
                item.text = control.getValue();
                item.selection_focus = controlSelectionFocus(control);
            }
            const sheet = if (el.is(Element.Html.Link)) |link| link._sheet else if (el.is(Element.Html.Style)) |style| style._sheet else null;
            if (sheet) |css| if (!css._disabled) {
                if (css._css_rules) |rules| {
                    var writer: std.Io.Writer.Allocating = .init(arena);
                    for (rules._rules.items) |rule| {
                        try writer.writer.writeAll(rule.getCssText(frame));
                        try writer.writer.writeByte('\n');
                    }
                    item.sheet = writer.written();
                }
            };
            item.attrs = attrs.items;
            if (frame.page.element_scroll_positions.get(el)) |position| {
                item.scroll_x = @floatFromInt(position.x);
                item.scroll_y = @floatFromInt(position.y);
            }
        } else if (node.is(Node.CData.Text) != null) {
            item.text = try node.getTextContentAlloc(arena);
        } else if (node.is(Node.ShadowRoot)) |shadow| {
            item.shadow_host = @intFromPtr(shadow._host.asNode());
            item.shadow_closed = std.mem.eql(u8, shadow.getMode(), "closed");
        } else if (node == frame.document.asNode()) {
            if (selectionFocus(frame)) |focus| {
                item.selection_node = @intFromPtr(focus.node);
                item.selection_focus = focus.offset;
            }
        } else if (node._type != .document and node._type != .document_fragment) continue;
        try projected.append(arena, item);
        if (item.tag == null or item.text == null) {
            var child = node.lastChild();
            while (child) |current| : (child = current.previousSibling()) try stack.append(arena, current);
        }
        if (node.is(Element)) |el| {
            if (el.hostedShadowRoot(frame)) |shadow| try stack.append(arena, shadow.asNode());
        }
    }
    const t_proj: u64 = if (timing_env != null) engineTime() else 0;
    const json = try std.json.Stringify.valueAlloc(arena, projected.items, .{});
    const t_json: u64 = if (timing_env != null) engineTime() else 0;
    const view = viewport(frame);
    const position = [4]f32{ @floatFromInt(view.width), @floatFromInt(view.height), @floatFromInt(frame.window.getScrollX()), @floatFromInt(frame.window.getScrollY()) };
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    const resource_version = frame._render_resource_version.load(.acquire);
    hasher.update(std.mem.asBytes(&resource_version));
    hasher.update(json);
    hasher.update(frame.base());
    hasher.update(std.mem.asBytes(&position));
    var digest: [32]u8 = undefined;
    hasher.final(&digest);
    if (frame._layout_document != null and std.mem.eql(u8, &digest, &frame._layout_digest)) {
        updateFrameViewports(frame, frame._layout_document.?, projected.items);
        if (timing_env) |path| {
            var buf: [192]u8 = undefined;
            const t_end = engineTime();
            if (std.fmt.bufPrint(&buf, "layout cached=1 nodes={d} proj_us={d} json_us={d} native_us=0 total_us={d}\n", .{
                projected.items.len,
                t_proj - t0,
                t_json - t_proj,
                t_end - t0,
            })) |line| appendTiming(path, line) else |_| {}
        }
        return frame._layout_document.?;
    }
    const next = lp_document_new(json.ptr, json.len, frame.base().ptr, frame.base().len, position[0], position[1], position[2], position[3], frame, load, surface) orelse return error.LayoutFailed;
    if (frame._layout_document) |old| lp_document_free(old);
    frame._layout_document = next;
    frame._layout_digest = digest;
    if (timing_env) |path| {
        var buf: [208]u8 = undefined;
        const t_end = engineTime();
        if (std.fmt.bufPrint(&buf, "layout cached=0 nodes={d} proj_us={d} json_us={d} native_us={d} total_us={d}\n", .{
            projected.items.len,
            t_proj - t0,
            t_json - t_proj,
            t_end - t_json,
            t_end - t0,
        })) |line| appendTiming(path, line) else |_| {}
    }
    updateFrameViewports(frame, next, projected.items);
    return next;
}
fn updateFrameViewports(frame: *Frame, value: *Document, nodes: []const Projection) void {
    for (nodes) |item| {
        const node: *Node = @ptrFromInt(item.key);
        const iframe = node.is(Element.Html.IFrame) orelse continue;
        const window = iframe._window orelse continue;
        var box: Metrics = .{};
        if (!lp_document_metrics(value, item.key, &box)) continue;
        var embedded = frame.page.getViewport();
        embedded.width = @intFromFloat(std.math.clamp(@floor(box.client_width), 0, 32767));
        embedded.height = @intFromFloat(std.math.clamp(@floor(box.client_height), 0, 32767));
        window._frame._embedding_viewport = embedded;
    }
}
fn load(context: *anyopaque, url: [*]const u8, len: usize, byte_count: *usize) callconv(.c) ?[*]const u8 {
    const frame: *Frame = @ptrCast(@alignCast(context));
    const bytes = frame._render_resources.get(url[0..len]) orelse return null;
    byte_count.* = bytes.len;
    return bytes.ptr;
}
fn surface(context: *anyopaque, key: usize, pixel_width: *u32, pixel_height: *u32, byte_count: *usize) callconv(.c) ?[*]const u8 {
    const frame: *Frame = @ptrCast(@alignCast(context));
    const node: *Node = @ptrFromInt(key);
    const bytes = if (node.is(Element.Html.Canvas)) |canvas| blk: {
        const image = canvas.pixels(frame.local_arena) catch return null;
        pixel_width.* = image.width;
        pixel_height.* = image.height;
        break :blk image.bytes;
    } else if (node.is(Element.Html.IFrame)) |iframe| blk: {
        const window = iframe._window orelse return null;
        const view = viewport(window._frame);
        pixel_width.* = view.width;
        pixel_height.* = view.height;
        break :blk rgba(window._frame) catch return null;
    } else return null;
    byte_count.* = bytes.len;
    return bytes.ptr;
}

fn controlSelectionFocus(control: anytype) usize {
    return @min(control.getValue().len, if (control._selection_direction == .backward) control._selection_start else control._selection_end);
}
const SelectionFocus = struct { node: *Node, offset: usize };
fn selectionFocus(frame: *Frame) ?SelectionFocus {
    const selection = &frame.document._selection;
    const range = (selection._range orelse return null).asAbstractRange();
    const backward = selection._direction == .backward;
    const node = if (backward) range.getStartContainer() else range.getEndContainer();
    if (!node.isConnected() or node.ownerFrame(frame) != frame) return null;
    return .{ .node = node, .offset = if (backward) range.getStartOffset() else range.getEndOffset() };
}

pub const Caret = struct {
    rect: [4]f64 = .{ 0, 0, 0, 0 },
    kind: []const u8 = "none",
};
/// Queries only retained documents after the raw paint. Re-preparing here could
/// replace the exact shaped buffer whose pixels were just captured.
pub fn focusedCaret(root: *Frame, focused: *Frame) Caret {
    const element = focused.document._active_element;
    const value = focused._layout_document orelse return .{};
    var values: Rect = .{};
    var exact = false;
    var editable = false;
    if (element) |el| {
        if (el.is(Element.Html.Input)) |control| {
            editable = control.tracksSelection();
            if (editable) exact = lp_document_caret(value, @intFromPtr(el.asNode()), 0, true, &values);
        } else if (el.is(Element.Html.TextArea) != null) {
            editable = true;
            exact = lp_document_caret(value, @intFromPtr(el.asNode()), 0, true, &values);
        } else {
            editable = el.isEditable();
            if (selectionFocus(focused)) |focus| {
                exact = lp_document_caret(value, @intFromPtr(focus.node), focus.offset, false, &values);
            }
        }
    } else if (selectionFocus(focused)) |focus| {
        exact = lp_document_caret(value, @intFromPtr(focus.node), focus.offset, false, &values);
    }
    if (exact) {
        if (rectInRoot(root, focused, values)) |mapped| return caretResult(mapped, "native-shaped-insertion");
    }
    if (!editable) return .{};
    const el = element orelse return .{};
    if (!lp_document_rect(value, @intFromPtr(el.asNode()), &values)) return .{};
    if (rectInRoot(root, focused, values)) |mapped| return caretResult(mapped, "focused-element-bounds");
    // If a transformed embedding cannot be mapped exactly, use the actual
    // ancestor iframe bounds in the root instead of labelling an estimate exact.
    var current = focused;
    while (current != root) {
        const iframe = current.iframe orelse return .{};
        const host = iframe.asElement();
        const parent = host.ownerFrame(current) orelse return .{};
        const parent_doc = parent._layout_document orelse return .{};
        if (lp_document_rect(parent_doc, @intFromPtr(host.asNode()), &values)) {
            if (rectInRoot(root, parent, values)) |mapped| return caretResult(mapped, "focused-element-bounds");
        }
        current = parent;
    }
    return .{};
}
fn caretResult(value: Rect, kind: []const u8) Caret {
    if (!std.math.isFinite(value.x) or !std.math.isFinite(value.y) or
        !std.math.isFinite(value.width) or !std.math.isFinite(value.height) or
        value.width < 0 or value.height <= 0) return .{};
    return .{ .rect = .{ value.x, value.y, value.width, value.height }, .kind = kind };
}
fn rectInRoot(root: *Frame, owner: *Frame, value: Rect) ?Rect {
    var result = value;
    var current = owner;
    while (current != root) {
        const iframe = current.iframe orelse return null;
        const host = iframe.asElement();
        const parent = host.ownerFrame(current) orelse return null;
        const parent_doc = parent._layout_document orelse return null;
        const embedded = current._embedding_viewport orelse return null;
        if (!lp_document_embed_rect(parent_doc, @intFromPtr(host.asNode()), embedded.width, embedded.height, &result)) return null;
        current = parent;
    }
    return result;
}
extern "c" fn lp_document_caret(*Document, usize, usize, bool, *Rect) bool;
extern "c" fn lp_document_embed_rect(*Document, usize, u32, u32, *Rect) bool;
pub fn rect(el: *Element, frame: *Frame) !DOMRect.Data {
    var values: Rect = .{};
    _ = lp_document_rect(try document(frame), @intFromPtr(el.asNode()), &values);
    return .{ .x = values.x, .y = values.y, .width = values.width, .height = values.height };
}
pub const Metrics = extern struct {
    x: f64 = 0,
    y: f64 = 0,
    width: f64 = 0,
    height: f64 = 0,
    client_width: f64 = 0,
    client_height: f64 = 0,
    scroll_width: f64 = 0,
    scroll_height: f64 = 0,
    scroll_x: f64 = 0,
    scroll_y: f64 = 0,
    max_x: f64 = 0,
    max_y: f64 = 0,
    border_left: f64 = 0,
    border_top: f64 = 0,
};
pub fn metrics(el: *Element, frame: *Frame) !Metrics {
    var value: Metrics = .{};
    _ = lp_document_metrics(try document(frame), @intFromPtr(el.asNode()), &value);
    return value;
}
pub fn size(frame: *Frame) !@import("webapi/Document.zig").Extent {
    var value: Rect = .{};
    _ = lp_document_size(try document(frame), &value);
    return .{ .width = value.width, .height = value.height };
}
extern "c" fn lp_document_metrics(*Document, usize, *Metrics) bool;
extern "c" fn lp_document_size(*Document, *Rect) bool;
pub fn hit(frame: *Frame, x: f64, y: f64) !?*Element {
    const key = lp_document_hit(try document(frame), @floatCast(x), @floatCast(y));
    if (key == 0) return null;
    const node: *Node = @ptrFromInt(key);
    return node.is(Element);
}
pub fn height(frame: *Frame) !f32 {
    return lp_document_height(try document(frame));
}
pub fn deinit(frame: *Frame) void {
    if (frame._layout_document) |value| lp_document_free(value);
    frame._layout_document = null;
}
pub const RenderOpts = extern struct {
    width: u32,
    height: u32,
    clip_x: f32,
    clip_y: f32,
    clip_w: f32,
    clip_h: f32,
    scale: f32,
    flags: u32,
};
pub fn render(frame: *Frame, opts: RenderOpts, content_height: *u32, context: *anyopaque, callback: *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) !i32 {
    const value = try document(frame);
    content_height.* = @intFromFloat(@ceil(lp_document_height(value)));
    if (opts.flags & 1 != 0) return 0;
    return lp_document_png(value, opts, context, callback);
}
extern "c" fn lp_document_new([*]const u8, usize, [*]const u8, usize, f32, f32, f32, f32, *anyopaque, *const fn (*anyopaque, [*]const u8, usize, *usize) callconv(.c) ?[*]const u8, *const fn (*anyopaque, usize, *u32, *u32, *usize) callconv(.c) ?[*]const u8) ?*Document;
extern "c" fn lp_document_free(*Document) void;
extern "c" fn lp_document_rect(*Document, usize, *Rect) bool;
extern "c" fn lp_document_hit(*Document, f32, f32) usize;
extern "c" fn lp_document_height(*Document) f32;
extern "c" fn lp_document_png(*Document, RenderOpts, *anyopaque, *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) i32;

pub fn rgba(frame: *Frame) ![]const u8 {
    var writer: std.Io.Writer.Allocating = .init(frame.local_arena);
    if (!lp_document_rgba(try document(frame), &writer.writer, writeBytes)) return error.WriteFailed;
    return writer.written();
}
fn writeBytes(context: *anyopaque, data: [*]const u8, len: usize) callconv(.c) bool {
    const writer: *std.Io.Writer = @ptrCast(@alignCast(context));
    writer.writeAll(data[0..len]) catch return false;
    return true;
}
extern "c" fn lp_document_rgba(*Document, *anyopaque, *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) bool;

pub const RawFrame = extern struct { width: u32 = 0, height: u32 = 0, stride: u32 = 0, theme: u32 = 0, fingerprint: [32]u8 = undefined };

extern "c" fn lp_document_set_dirty_rect(*Document, f32, f32, f32, f32) bool;
extern "c" fn lp_document_set_frame_unchanged(*Document, bool) bool;

/// Record a document-space rect that the next capture must repaint. Only the
/// engine knows what it actually changed; a rect list that misses a change
/// leaves stale pixels, so callers must describe *everything* they touched.
pub fn markDirtyRect(frame: *Frame, x: f32, y: f32, w: f32, h: f32) void {
    if (!std.math.isFinite(x) or !std.math.isFinite(y) or !(w > 0) or !(h > 0)) return;
    if (frame._paint_dirty) |prev| {
        const x0 = @min(prev[0], x);
        const y0 = @min(prev[1], y);
        const x1 = @max(prev[0] + prev[2], x + w);
        const y1 = @max(prev[1] + prev[3], y + h);
        frame._paint_dirty = .{ x0, y0, x1 - x0, y1 - y0 };
    } else {
        frame._paint_dirty = .{ x, y, w, h };
    }
}

pub fn markElementDirty(frame: *Frame, element: ?*Element) void {
    const el = element orelse return;
    const owner = el.ownerFrame(frame) orelse return;
    if (owner != frame) return; // child documents repaint through their own capture
    const bounds = rect(el, frame) catch return;
    // Inflate: outlines, shadows and rounded-corner antialiasing paint outside
    // the border box.
    const pad: f64 = 8;
    markDirtyRect(frame, @floatCast(bounds.x - pad), @floatCast(bounds.y - pad), @floatCast(bounds.width + pad * 2), @floatCast(bounds.height + pad * 2));
}

pub fn rawFrame(frame: *Frame, path: []const u8, slot: u32) !RawFrame {
    const view = frame.page.getViewport();
    const probe = perf.enabled();
    const t_begin = if (probe) perf.now() else 0;
    clearPaint(frame);
    const t_clear = if (probe) perf.now() else 0;
    var result: RawFrame = .{};
    const doc = try document(frame);
    if (frame._paint_dirty) |d| {
        _ = lp_document_set_dirty_rect(doc, d[0], d[1], d[2], d[3]);
        frame._paint_dirty = null;
    } else {
        _ = lp_document_set_dirty_rect(doc, 0, 0, 0, 0); // drop any stale request
    }
    // A capture whose projection digest equals the published one cannot change a
    // pixel; the renderer republishes its shadow instead of re-rasterising.
    const unchanged = if (frame._captured_digest) |digest| std.mem.eql(u8, &digest, &frame._layout_digest) else false;
    _ = lp_document_set_frame_unchanged(doc, unchanged);
    if (!lp_document_raw_frame(doc, path.ptr, path.len, slot, view.width, view.height, view.scale, &result)) return error.RawFrameFailed;
    const t_paint = if (probe) perf.now() else 0;
    result.fingerprint = try scopedFingerprint(frame, result.fingerprint);
    frame._captured_fingerprint = result.fingerprint;
    frame._captured_digest = frame._layout_digest;
    if (probe) {
        const t_end = perf.now();
        perf.log("frame", "clear_us={d} paint_us={d} fp_us={d} total_us={d} w={d} h={d}", .{
            t_clear - t_begin, t_paint - t_clear, t_end - t_paint, t_end - t_begin, result.width, result.height,
        });
    }
    return result;
}
extern "c" fn lp_document_raw_frame(*Document, [*]const u8, usize, u32, u32, u32, f32, *RawFrame) bool;
fn clearPaint(frame: *Frame) void {
    if (frame._layout_document) |value| lp_document_clear_paint(value);
    for (frame.child_frames.items) |child| clearPaint(child);
}
extern "c" fn lp_document_clear_paint(*Document) void;

pub fn frameFingerprint(frame: *Frame) ![32]u8 {
    const view = frame.page.getViewport();
    var pixels: [32]u8 = undefined;
    if (!lp_document_frame_fingerprint(try document(frame), view.width, view.height, view.scale, &pixels)) return error.RawFrameFailed;
    return scopedFingerprint(frame, pixels);
}
/// Fingerprint of the frame the client has on screen, reused while the document
/// digest still matches the capture.
///
/// `document()` here is ~0.1-1.2 ms and refreshes `_layout_digest`, so the reuse
/// key can never claim a stale frame; a miss falls back to the authoritative
/// repaint. This removes the full-viewport raster that every native input used
/// to pay just to recompute the fingerprint it was handed.
pub fn frameFingerprintCached(frame: *Frame) ![32]u8 {
    _ = try document(frame);
    if (frame._captured_fingerprint) |cached| {
        if (frame._captured_digest) |digest| {
            if (std.mem.eql(u8, &digest, &frame._layout_digest)) return cached;
        }
    }
    return frameFingerprint(frame);
}

fn scopedFingerprint(frame: *Frame, pixels: [32]u8) ![32]u8 {
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    hasher.update(&pixels);
    try hashDocumentTree(frame, &hasher, true);
    var result: [32]u8 = undefined;
    hasher.final(&result);
    return result;
}
fn hashDocumentTree(frame: *Frame, hasher: *std.crypto.hash.sha2.Sha256, refreshed: bool) !void {
    // The root was projected by the caller (rawFrame / frameFingerprint), so a
    // second full projection there is pure waste; child frames still need it or
    // their `_layout_digest` would be stale.
    if (!refreshed) _ = try document(frame);
    hasher.update(std.mem.asBytes(&frame._frame_id));
    hasher.update(std.mem.asBytes(&frame._loader_id));
    hasher.update(&frame._layout_digest);
    hasher.update(std.mem.asBytes(&frame.page.style_version));
    for (frame.child_frames.items) |child| try hashDocumentTree(child, hasher, false);
}
extern "c" fn lp_document_frame_fingerprint(*Document, u32, u32, f32, *[32]u8) bool;

/// Cheap renderer-owned dirtiness; only scanned while a native client subscribes.
pub fn revision(frame: *Frame) u64 {
    var hasher = std.hash.Wyhash.init(0);
    hasher.update(std.mem.asBytes(&frame.page.dom_version));
    hasher.update(std.mem.asBytes(&frame.page.style_version));
    const view = frame.page.getViewport();
    hasher.update(std.mem.asBytes(&view.width));
    hasher.update(std.mem.asBytes(&view.height));
    hasher.update(std.mem.asBytes(&view.scale));
    hashRevision(frame, &hasher);
    return hasher.final();
}
fn hashRevision(frame: *Frame, hasher: *std.hash.Wyhash) void {
    hasher.update(std.mem.asBytes(&frame._frame_id));
    hasher.update(std.mem.asBytes(&frame._loader_id));
    const resource = frame._render_resource_version.load(.acquire);
    hasher.update(std.mem.asBytes(&resource));
    const x = frame.window.getScrollX();
    const y = frame.window.getScrollY();
    hasher.update(std.mem.asBytes(&x));
    hasher.update(std.mem.asBytes(&y));
    if (frame.document._active_element) |element| {
        const key = @intFromPtr(element.asNode());
        hasher.update(std.mem.asBytes(&key));
        if (element.is(Element.Html.Input)) |control| {
            const focus = controlSelectionFocus(control);
            hasher.update(std.mem.asBytes(&focus));
        } else if (element.is(Element.Html.TextArea)) |control| {
            const focus = controlSelectionFocus(control);
            hasher.update(std.mem.asBytes(&focus));
        }
    }
    if (selectionFocus(frame)) |focus| {
        const key = @intFromPtr(focus.node);
        hasher.update(std.mem.asBytes(&key));
        hasher.update(std.mem.asBytes(&focus.offset));
    }
    for (frame.child_frames.items) |child| hashRevision(child, hasher);
}

test "browser.native_layout: authoritative focus and dynamic CSS" {
    try @import("../testing.zig").htmlRunner("canvas/native_layout_focus.html", .{});
}

test "browser.native_layout: selection focus uses live UTF-8 direction" {
    const Control = struct {
        _selection_start: u32,
        _selection_end: u32,
        _selection_direction: @import("webapi/Selection.zig").SelectionDirection,
        fn getValue(_: @This()) []const u8 {
            return "A😀中";
        }
    };
    var control: Control = .{ ._selection_start = 1, ._selection_end = 5, ._selection_direction = .backward };
    try std.testing.expectEqual(@as(usize, 1), controlSelectionFocus(control));
    control._selection_direction = .forward;
    try std.testing.expectEqual(@as(usize, 5), controlSelectionFocus(control));
    control._selection_direction = .none;
    control._selection_end = 100;
    try std.testing.expectEqual(@as(usize, 8), controlSelectionFocus(control));
}
