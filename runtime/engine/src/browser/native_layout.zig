const std = @import("std");
const lp = @import("lightpanda");
const Frame = @import("Frame.zig");
const ExactVisualGuard = @import("ExactVisualGuard.zig");
const Node = @import("webapi/Node.zig");
const Element = @import("webapi/Element.zig");
const DOMRect = @import("webapi/DOMRect.zig");
const Allocator = std.mem.Allocator;

pub const Document = opaque {};
/// Mirrors `screenshot.LpFontFile`; the engine's `--render-font` list.
const FontFile = extern struct { data: [*]const u8, data_len: usize };
const Rect = extern struct { x: f64 = 0, y: f64 = 0, width: f64 = 0, height: f64 = 0 };
const Projection = struct {
    key: usize,
    parent: ?usize,
    tag: ?[]const u8 = null,
    text: ?[]const u8 = null,
    namespace: ?[]const u8 = null,
    attrs: []const [2][]const u8 = &.{},
    scroll_x: f32 = 0,
    scroll_y: f32 = 0,
    shadow_host: ?usize = null,
    shadow_closed: bool = false,
    focused: bool = false,
    hovered: bool = false,
    active: bool = false,
    focus_visible: bool = false,
    selection_focus: ?usize = null,
    selection_node: ?usize = null,
};

/// The declarations the Zig kernel models, as the engine's cascade resolved
/// them (author rules, inline style, inheritance). The kernel applies these
/// values; it is not a second CSS engine.
const style_properties = [_][]const u8{
    "display",          "font-size",          "font-weight",         "text-decoration",
    "margin-top",       "margin-bottom",      "margin-left",         "width",
    "height",
    // Shorthands come first: the kernel applies declarations in order, so the
    // longhands below win.
              "padding",            "padding-top",         "padding-right",
    "padding-bottom",   "padding-left",       "border",              "border-width",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "box-sizing",
    // Shorthand first so background-color wins over a background color token.
          "background",         "background-color",    "color",
};

/// CSS-wide keywords leave the kernel's own default (UA style, or the value
/// inherited down the tree) in place.
fn cssWideKeyword(value: []const u8) bool {
    return std.mem.eql(u8, value, "initial") or std.mem.eql(u8, value, "inherit") or
        std.mem.eql(u8, value, "unset") or std.mem.eql(u8, value, "revert") or
        std.mem.eql(u8, value, "revert-layer");
}

fn projectStyle(writer: *std.Io.Writer, el: *Element, manager: anytype) !void {
    for (style_properties) |name| {
        const raw = manager.computedPropertyValue(el, lp.String.wrap(name)) orelse continue;
        const value = std.mem.trim(u8, raw, " \t\r\n");
        if (value.len == 0 or cssWideKeyword(value)) continue;
        try writer.writeAll(name);
        try writer.writeByte(':');
        try writer.writeAll(value);
        try writer.writeByte(';');
    }
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
    return documentScoped(frame, null, 0);
}

/// Projects `root` (or the whole document) and lays it out at `width` layout
/// px. `width == 0` means the viewport width. Scoping is what the screenshot
/// tests use: they render a detached `<div>`.
pub fn documentScoped(frame: *Frame, root: ?*Node, width: f32) !*Document {
    const lease = try frame._session.getArena(.large, "native-layout");
    defer lease.release();
    const arena = lease.allocator();
    var projected: std.ArrayList(Projection) = .empty;
    var stack: std.ArrayList(*Node) = .empty;
    const start = root orelse frame.document.asNode();
    try stack.append(arena, start);
    const hovered = inputTarget(frame, frame.page.input_hover_target);
    const active = inputTarget(frame, frame.page.input_pointer.down_target);
    while (stack.pop()) |node| {
        var item: Projection = .{ .key = @intFromPtr(node), .parent = if (node == start) null else if (node.parentNode()) |parent| @intFromPtr(parent) else null };
        if (node.is(Element)) |el| {
            item.tag = el.getTagNameLower();
            item.namespace = el.getNamespaceURI();
            item.focused = frame.document._active_element == el;
            item.focus_visible = el.hasVisibleFocus(frame);
            item.hovered = hovered == el;
            item.active = active == el;
            var attrs: std.ArrayList([2][]const u8) = .empty;
            const input = el.is(Element.Html.Input);
            for (el.attributeEntries()) |*attr| {
                if (std.mem.eql(u8, attr.name(), "style")) continue;
                if (input != null and std.mem.eql(u8, attr.name(), "value")) continue;
                try attrs.append(arena, .{ attr.name(), attr.value() });
            }
            {
                var writer: std.Io.Writer.Allocating = .init(arena);
                try projectStyle(&writer.writer, el, &frame._style_manager);
                if (writer.written().len != 0) {
                    try attrs.append(arena, .{ "style", writer.written() });
                }
            }
            if (input) |control| {
                try attrs.append(arena, .{ "value", control.getValue() });
                if (control.tracksSelection()) item.selection_focus = controlSelectionFocus(control);
            }
            if (el.is(Element.Html.TextArea)) |control| {
                item.text = control.getValue();
                item.selection_focus = controlSelectionFocus(control);
            }
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
    const json = try std.json.Stringify.valueAlloc(arena, projected.items, .{});
    const view = viewport(frame);
    const layout_width: f32 = if (width > 0) width else @floatFromInt(view.width);
    const position = [4]f32{ layout_width, @floatFromInt(view.height), @floatFromInt(frame.window.getScrollX()), @floatFromInt(frame.window.getScrollY()) };
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    const resource_version = frame._render_resource_version.load(.acquire);
    hasher.update(std.mem.asBytes(&resource_version));
    hasher.update(json);
    hasher.update(frame.base());
    hasher.update(std.mem.asBytes(&position));
    const scope: usize = if (root) |r| @intFromPtr(r) else 0;
    hasher.update(std.mem.asBytes(&scope));
    hasher.update(std.mem.asBytes(&layout_width));
    var digest: [32]u8 = undefined;
    hasher.final(&digest);
    if (frame._layout_document != null and std.mem.eql(u8, &digest, &frame._layout_digest)) {
        updateFrameViewports(frame, frame._layout_document.?, projected.items);
        return frame._layout_document.?;
    }
    const fonts = frame._session.browser.app.render_fonts.files;
    const next = nl_new(json.ptr, json.len, frame.base().ptr, frame.base().len, @ptrCast(fonts.ptr), fonts.len, position[0], position[1], position[2], position[3], frame, load, surface) orelse return error.LayoutFailed;
    if (frame._layout_document) |old| nl_free(old);
    frame._layout_document = next;
    frame._layout_digest = digest;
    updateFrameViewports(frame, next, projected.items);
    return next;
}
fn updateFrameViewports(frame: *Frame, value: *Document, nodes: []const Projection) void {
    for (nodes) |item| {
        const node: *Node = @ptrFromInt(item.key);
        const iframe = node.is(Element.Html.IFrame) orelse continue;
        const window = iframe._window orelse continue;
        var box: Metrics = .{};
        if (!nl_metrics(value, item.key, &box)) continue;
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

const controlSelectionFocus = ExactVisualGuard.controlSelectionFocus;
const selectionFocus = ExactVisualGuard.selectionFocus;

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
            if (editable) exact = nl_caret(value, @intFromPtr(el.asNode()), 0, true, &values);
        } else if (el.is(Element.Html.TextArea) != null) {
            editable = true;
            exact = nl_caret(value, @intFromPtr(el.asNode()), 0, true, &values);
        } else {
            editable = el.isEditable();
            if (selectionFocus(focused)) |focus| {
                exact = nl_caret(value, @intFromPtr(focus.node), focus.offset, false, &values);
            }
        }
    } else if (selectionFocus(focused)) |focus| {
        exact = nl_caret(value, @intFromPtr(focus.node), focus.offset, false, &values);
    }
    if (exact) {
        if (rectInRoot(root, focused, values)) |mapped| return caretResult(mapped, "native-shaped-insertion");
    }
    if (!editable) return .{};
    const el = element orelse return .{};
    if (!nl_rect(value, @intFromPtr(el.asNode()), &values)) return .{};
    if (rectInRoot(root, focused, values)) |mapped| return caretResult(mapped, "focused-element-bounds");
    // If a transformed embedding cannot be mapped exactly, use the actual
    // ancestor iframe bounds in the root instead of labelling an estimate exact.
    var current = focused;
    while (current != root) {
        const iframe = current.iframe orelse return .{};
        const host = iframe.asElement();
        const parent = host.ownerFrame(current) orelse return .{};
        const parent_doc = parent._layout_document orelse return .{};
        if (nl_rect(parent_doc, @intFromPtr(host.asNode()), &values)) {
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
        if (!nl_embed_rect(parent_doc, @intFromPtr(host.asNode()), embedded.width, embedded.height, &result)) return null;
        current = parent;
    }
    return result;
}
pub fn rect(el: *Element, frame: *Frame) !DOMRect.Data {
    var values: Rect = .{};
    _ = nl_rect(try document(frame), @intFromPtr(el.asNode()), &values);
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
    _ = nl_metrics(try document(frame), @intFromPtr(el.asNode()), &value);
    return value;
}
pub fn size(frame: *Frame) !@import("webapi/Document.zig").Extent {
    var value: Rect = .{};
    _ = nl_size(try document(frame), &value);
    return .{ .width = value.width, .height = value.height };
}
pub fn hit(frame: *Frame, x: f64, y: f64) !?*Element {
    const key = nl_hit(try document(frame), @floatCast(x), @floatCast(y));
    if (key == 0) return null;
    const node: *Node = @ptrFromInt(key);
    return node.is(Element);
}
pub fn height(frame: *Frame) !f32 {
    return nl_height(try document(frame));
}

/// Content height of `root` laid out at `width` layout px.
pub fn heightAt(frame: *Frame, root: *Node, width: f32) !f32 {
    return nl_height(try documentScoped(frame, root, width));
}
pub fn deinit(frame: *Frame) void {
    if (frame._layout_document) |value| nl_free(value);
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
    return renderScoped(frame, null, opts, content_height, context, callback);
}

/// Screenshot render, laid out at `opts.width` and scoped to `root` when given.
pub fn renderScoped(frame: *Frame, root: ?*Node, opts: RenderOpts, content_height: *u32, context: *anyopaque, callback: *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) !i32 {
    const value = try documentScoped(frame, root, @floatFromInt(opts.width));
    content_height.* = @intFromFloat(@ceil(nl_height(value)));
    if (opts.flags & 1 != 0) return 0;
    return nl_png(value, opts, context, callback);
}

pub fn rgba(frame: *Frame) ![]const u8 {
    var writer: std.Io.Writer.Allocating = .init(frame.local_arena);
    if (!nl_rgba(try document(frame), &writer.writer, writeBytes)) return error.WriteFailed;
    return writer.written();
}
fn writeBytes(context: *anyopaque, data: [*]const u8, len: usize) callconv(.c) bool {
    const writer: *std.Io.Writer = @ptrCast(@alignCast(context));
    writer.writeAll(data[0..len]) catch return false;
    return true;
}

const BorrowedPaint = struct {
    frame: *Frame,
    width: u32,
    height: u32,
    scale: f32,
    key: ExactVisualGuard.Key,
    guard: ExactVisualGuard.Capture = .{},
    stable: bool = false,
    failure: ?anyerror = null,
};
fn paintBorrowed(context: *anyopaque, pixels: [*]u8, len: usize, stride: usize) callconv(.c) bool {
    const was_paused = @import("TaskGate.zig").callbackEnter();
    defer @import("TaskGate.zig").callbackExit(was_paused);
    const paint: *BorrowedPaint = @ptrCast(@alignCast(context));
    paint.guard = ExactVisualGuard.prepare(paint.frame, paint.key) catch |err| {
        paint.failure = err;
        return false;
    };
    if (paint.guard.viewport.width != paint.width or paint.guard.viewport.height != paint.height or
        paint.guard.viewport.scale != paint.scale)
    {
        paint.failure = error.VisualUnstable;
        return false;
    }
    clearPaint(paint.frame);
    const value = document(paint.frame) catch |err| {
        paint.failure = err;
        return false;
    };
    if (!nl_paint_borrowed(value, paint.width, paint.height, paint.scale, pixels, len, stride)) {
        paint.failure = error.RawFrameFailed;
        return false;
    }
    paint.stable = ExactVisualGuard.validate(paint.frame, paint.guard, false);
    return true;
}
const BackingMetadata = extern struct { row_stride: u64 = 0, byte_length: u64 = 0, allocation_bytes: u64 = 0 };
// Private result bits: 1 = all native aliases released, 2 = paint completed.
extern "c" fn lp_broker_paint([*:0]const u8, u64, i32, u32, i32, u32, u32, u32, *anyopaque, *const fn (*anyopaque, [*]u8, usize, usize) callconv(.c) bool, *BackingMetadata) u32;

pub const BrokerFrame = struct {
    width: u32,
    height: u32,
    stride: u32,
    byte_length: u64,
    allocation_bytes: u64,
    guard: ExactVisualGuard.Capture,
    painted: bool,
    capture_stable: bool,
    failure: ?anyerror,
};
/// Trusted cooperative CPU-only writer. The broker retains its writer use until
/// its own trusted-profile terminal protocol completes; this is not publication.
pub fn brokerFrame(frame: *Frame, name: [:0]const u8, epoch: u64, host: i32, uid: u32, version: i32, timeout: u32, key: ExactVisualGuard.Key) !BrokerFrame {
    if (comptime @import("builtin").os.tag != .macos) return error.Unsupported;
    const view = frame.page.getViewport();
    if (view.width == 0 or view.height == 0 or !std.math.isFinite(view.scale) or view.scale < 0.25 or view.scale > 8) return error.InvalidExtent;
    const physical_width = @ceil(@as(f64, @floatFromInt(view.width)) * @as(f64, view.scale));
    const physical_height = @ceil(@as(f64, @floatFromInt(view.height)) * @as(f64, view.scale));
    if (physical_width < 1 or physical_height < 1 or physical_width > 3840 * 2160 or physical_height > 3840 * 2160 or physical_width * physical_height > 3840 * 2160) return error.InvalidExtent;
    const width: u32 = @intFromFloat(physical_width);
    const raster_height: u32 = @intFromFloat(physical_height);
    var paint: BorrowedPaint = .{ .frame = frame, .width = view.width, .height = view.height, .scale = view.scale, .key = key };
    var metadata: BackingMetadata = .{};
    const suspended = @import("TaskGate.zig").pauseForWait();
    const status = lp_broker_paint(name.ptr, epoch, host, uid, version, timeout, width, raster_height, &paint, paintBorrowed, &metadata);
    @import("TaskGate.zig").resumeAfterWait(suspended);
    if ((status & 1) == 0) return error.RawFrameFailed;
    const painted = (status & 2) != 0;
    return .{ .width = width, .height = raster_height, .stride = @intCast(metadata.row_stride), .byte_length = metadata.byte_length, .allocation_bytes = metadata.allocation_bytes, .guard = paint.guard, .painted = painted, .failure = paint.failure, .capture_stable = painted and paint.stable and ExactVisualGuard.validate(frame, paint.guard, false) };
}

pub const RawFrame = extern struct { width: u32 = 0, height: u32 = 0, stride: u32 = 0, theme: u32 = 0, fingerprint: [32]u8 = undefined };
pub const FingerprintCache = struct { guard: ExactVisualGuard.Capture, pixels: [32]u8, scoped: [32]u8 };
pub fn rawFrame(frame: *Frame, path: []const u8, slot: u32) !RawFrame {
    // Clear before prepare can replace a history entry, including failed paints.
    frame._native_fingerprint_cache = null;
    const guard: ?ExactVisualGuard.Capture = ExactVisualGuard.prepare(frame, .{ .owner = @intFromPtr(frame), .generation = std.math.maxInt(u64) }) catch null;
    const view = frame.page.getViewport();
    clearPaint(frame);
    var result: RawFrame = .{};
    if (!nl_raw_frame(try document(frame), path.ptr, path.len, slot, view.width, view.height, view.scale, hashPixels, &result)) return error.RawFrameFailed;
    const pixels = result.fingerprint;
    result.fingerprint = try scopedFingerprint(frame, result.fingerprint);
    if (guard) |stable| {
        if (ExactVisualGuard.validate(frame, stable, false)) frame._native_fingerprint_cache = .{ .guard = stable, .pixels = pixels, .scoped = result.fingerprint };
    }
    return result;
}
// Keep raw capture and atomic input on the same SHA-256 implementation; Zig's
// target-selected backend uses the host SHA instructions without an asm dep.
fn hashPixels(bytes: [*]const u8, len: usize, out: *[32]u8) callconv(.c) void {
    std.crypto.hash.sha2.Sha256.hash(bytes[0..len], out, .{});
}
fn clearPaint(frame: *Frame) void {
    if (frame._layout_document) |value| nl_clear_paint(value);
    for (frame.child_frames.items) |child| clearPaint(child);
}

pub fn frameFingerprint(frame: *Frame) ![32]u8 {
    if (frame._native_fingerprint_cache) |cached| {
        if (ExactVisualGuard.validate(frame, cached.guard, false)) {
            // Reproject every document: don't substitute a notification counter
            // for current DOM/style/resource semantics. Only pixels are reused.
            const scoped = try scopedFingerprint(frame, cached.pixels);
            if (std.mem.eql(u8, &scoped, &cached.scoped) and ExactVisualGuard.validate(frame, cached.guard, false)) return scoped;
        }
    }
    const view = frame.page.getViewport();
    var pixels: [32]u8 = undefined;
    if (!nl_frame_fingerprint(try document(frame), view.width, view.height, view.scale, hashPixels, &pixels)) return error.RawFrameFailed;
    return scopedFingerprint(frame, pixels);
}
fn scopedFingerprint(frame: *Frame, pixels: [32]u8) ![32]u8 {
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    hasher.update(&pixels);
    try hashDocumentTree(frame, &hasher);
    var result: [32]u8 = undefined;
    hasher.final(&result);
    return result;
}
fn hashDocumentTree(frame: *Frame, hasher: *std.crypto.hash.sha2.Sha256) !void {
    _ = try document(frame);
    hasher.update(std.mem.asBytes(&frame._frame_id));
    hasher.update(std.mem.asBytes(&frame._loader_id));
    hasher.update(&frame._layout_digest);
    hasher.update(std.mem.asBytes(&frame.page.style_version));
    for (frame.child_frames.items) |child| try hashDocumentTree(child, hasher);
}

// ---------------------------------------------------------------------------
// Renderer dispatch
//
// The Desktop-owned Zig kernel (`mioa-desktop/src/render_kernel`) is the only
// renderer: a plain module call, no C entry points, no link-order games.
// `Document` stays opaque here so the engine never names a kernel type.
// ---------------------------------------------------------------------------
const render_kernel = @import("render_kernel");

fn nl_new(json: [*]const u8, json_len: usize, base: [*]const u8, base_len: usize, fonts: [*]const FontFile, fonts_len: usize, width: f32, vh: f32, scroll_x: f32, scroll_y: f32, ctx: *anyopaque, load_cb: *const fn (*anyopaque, [*]const u8, usize, *usize) callconv(.c) ?[*]const u8, surface_cb: *const fn (*anyopaque, usize, *u32, *u32, *usize) callconv(.c) ?[*]const u8) ?*Document {
    return @ptrCast(render_kernel.lp_document_new(json, json_len, base, base_len, @ptrCast(fonts), fonts_len, width, vh, scroll_x, scroll_y, ctx, load_cb, surface_cb));
}
fn nl_free(doc: *Document) void {
    render_kernel.lp_document_free(@ptrCast(@alignCast(doc)));
}
fn nl_height(doc: *Document) f32 {
    return render_kernel.lp_document_height(@ptrCast(@alignCast(doc)));
}
fn nl_png(doc: *Document, opts: RenderOpts, ctx: *anyopaque, write: *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) i32 {
    return render_kernel.lp_document_png(@ptrCast(@alignCast(doc)), .{
        .width = opts.width,
        .height = opts.height,
        .clip_x = opts.clip_x,
        .clip_y = opts.clip_y,
        .clip_w = opts.clip_w,
        .clip_h = opts.clip_h,
        .scale = opts.scale,
        .flags = opts.flags,
    }, ctx, write);
}
fn nl_rect(doc: *Document, key: usize, out: *Rect) bool {
    return render_kernel.lp_document_rect(@ptrCast(@alignCast(doc)), key, @ptrCast(@alignCast(out)));
}
fn nl_metrics(doc: *Document, key: usize, out: *Metrics) bool {
    return render_kernel.lp_document_metrics(@ptrCast(@alignCast(doc)), key, @ptrCast(@alignCast(out)));
}
fn nl_size(doc: *Document, out: *Rect) bool {
    return render_kernel.lp_document_size(@ptrCast(@alignCast(doc)), @ptrCast(@alignCast(out)));
}
fn nl_hit(doc: *Document, x: f32, y: f32) usize {
    return render_kernel.lp_document_hit(@ptrCast(@alignCast(doc)), x, y);
}
fn nl_caret(doc: *Document, key: usize, offset: usize, control: bool, out: *Rect) bool {
    return render_kernel.lp_document_caret(@ptrCast(@alignCast(doc)), key, offset, control, @ptrCast(@alignCast(out)));
}
fn nl_embed_rect(doc: *Document, key: usize, width: u32, vh: u32, out: *Rect) bool {
    return render_kernel.lp_document_embed_rect(@ptrCast(@alignCast(doc)), key, width, vh, @ptrCast(@alignCast(out)));
}
fn nl_clear_paint(doc: *Document) void {
    render_kernel.lp_document_clear_paint(@ptrCast(@alignCast(doc)));
}
fn nl_rgba(doc: *Document, ctx: *anyopaque, write: *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) bool {
    return render_kernel.lp_document_rgba(@ptrCast(@alignCast(doc)), ctx, write);
}
fn nl_paint_borrowed(doc: *Document, width: u32, vh: u32, scale: f32, pixels: [*]u8, len: usize, stride: usize) bool {
    return render_kernel.lp_document_paint_borrowed(@ptrCast(@alignCast(doc)), width, vh, scale, pixels, len, stride);
}
fn nl_raw_frame(doc: *Document, path: [*]const u8, path_len: usize, slot: u32, width: u32, vh: u32, scale: f32, hash: *const fn ([*]const u8, usize, *[32]u8) callconv(.c) void, out: *RawFrame) bool {
    return render_kernel.lp_document_raw_frame(@ptrCast(@alignCast(doc)), path, path_len, slot, width, vh, scale, hash, @ptrCast(@alignCast(out)));
}
fn nl_frame_fingerprint(doc: *Document, width: u32, vh: u32, scale: f32, hash: *const fn ([*]const u8, usize, *[32]u8) callconv(.c) void, out: *[32]u8) bool {
    return render_kernel.lp_document_frame_fingerprint(@ptrCast(@alignCast(doc)), width, vh, scale, hash, out);
}

/// Cheap renderer-owned dirtiness; only scanned while a native client subscribes.
pub fn revision(frame: *Frame) u64 {
    var hasher = std.hash.Wyhash.init(0);
    hasher.update(std.mem.asBytes(&frame.page.dom_version));
    hasher.update(std.mem.asBytes(&frame.page.style_version));
    const visual_generation = frame.page.visual_generation.load(.acquire);
    hasher.update(std.mem.asBytes(&visual_generation));
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

test "browser.native_layout: delayed native borrow rejects changed viewport and DPR before paint" {
    const testing = @import("../testing.zig");
    const frame = try testing.createFrame();
    defer testing.test_session.closeAllPages();
    const view = frame.page.getViewport();
    var bytes: [64]u8 = @splat(0xa5);
    for ([_]bool{ false, true }) |dpr| {
        var paint: BorrowedPaint = .{
            .frame = frame,
            .width = if (dpr) view.width else view.width + 1,
            .height = view.height,
            .scale = if (dpr) view.scale + 0.5 else view.scale,
            .key = .{ .owner = 1, .generation = if (dpr) 2 else 1 },
        };
        try testing.expect(!paintBorrowed(&paint, &bytes, bytes.len, 16));
        try testing.expect(paint.failure != null);
        try testing.expectEqual(error.VisualUnstable, paint.failure.?);
        try testing.expect(!paint.stable);
        for (bytes) |byte| try testing.expect(byte == 0xa5);
    }
}

test "browser.native_layout: authoritative focus and dynamic CSS" {
    try @import("../testing.zig").htmlRunner("canvas/native_layout_focus.html", .{});
}

test "browser.native_layout: selection focus uses live UTF-8 direction" {
    const Control = struct {
        _selection_start: u32,
        _selection_end: u32,
        _selection_direction: @import("webapi/Selection.zig").SelectionDirection,
        pub fn getValue(_: @This()) []const u8 {
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
