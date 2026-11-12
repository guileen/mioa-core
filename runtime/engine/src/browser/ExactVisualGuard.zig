// Exact renderer state for governed authority and internal fingerprint reuse.
// Validation performs no layout, canvas readback, pixel hashing or allocation.
const std = @import("std");
const Frame = @import("Frame.zig");
const Page = @import("Page.zig");
const Viewport = @import("Viewport.zig");
const Node = @import("webapi/Node.zig");
const Element = @import("webapi/Element.zig");

pub const history_length = 8;
pub const Key = struct { owner: usize = 0, generation: u64 = 0 };
const Model = struct { visual: u64 = 0, canvas: u64 = 0 };
const State = struct {
    page: usize = 0,
    frame: usize = 0,
    document: usize = 0,
    parent: usize = 0,
    position: usize = 0,
    frame_id: u32 = 0,
    loader_id: u32 = 0,
    model: Model = .{},
    resource_epoch: u64 = 0,
    children: usize = 0,
    scroll_x: u32 = 0,
    scroll_y: u32 = 0,
    focused_element: usize = 0,
    control_focus: usize = 0,
    control_anchor: usize = 0,
    control_direction: usize = 0,
    selection_node: usize = 0,
    selection_offset: usize = 0,
    selection_anchor_node: usize = 0,
    selection_anchor_offset: usize = 0,
    selection_direction: usize = 0,
    hovered: usize = 0,
    pressed: usize = 0,
    keyboard_modality: bool = false,
};
const Record = struct { key: Key = .{}, state: State = .{} };
// Lazily allocated when exact state is first captured. Displayed authority has
// its own retained record: preparing newer candidates cannot overwrite it.
pub const History = struct {
    candidates: [history_length]Record = @splat(.{}),
    displayed: Record = .{},
};
pub const Capture = struct {
    key: Key = .{},
    page: usize = 0,
    document: usize = 0,
    frame_id: u32 = 0,
    loader_id: u32 = 0,
    model_stamp: u64 = 0,
    canvas_epoch: u64 = 0,
    viewport: Viewport = .{ .width = 0, .height = 0 },
    frames: usize = 0,
};

pub fn controlSelectionFocus(control: anytype) usize {
    return @min(control.getValue().len, if (control._selection_direction == .backward) control._selection_start else control._selection_end);
}
pub const SelectionFocus = struct { node: *Node, offset: usize };
pub fn selectionFocus(frame: *Frame) ?SelectionFocus {
    const selection = &frame.document._selection;
    const range = (selection._range orelse return null).asAbstractRange();
    const backward = selection._direction == .backward;
    const node = if (backward) range.getStartContainer() else range.getEndContainer();
    if (!node.isConnected() or node.ownerFrame(frame) != frame) return null;
    return .{ .node = node, .offset = if (backward) range.getStartOffset() else range.getEndOffset() };
}
fn address(pointer: anytype) usize {
    return if (pointer) |value| @intFromPtr(value) else 0;
}
fn model(page: *Page) !Model {
    const visual = page.visual_generation.load(.acquire);
    const canvas = page.canvas_epoch.load(.acquire);
    if (visual == std.math.maxInt(u64) or canvas == std.math.maxInt(u64) or
        page.canvas_writers.load(.acquire) != 0 or
        visual != page.visual_generation.load(.acquire) or
        canvas != page.canvas_epoch.load(.acquire)) return error.VisualUnstable;
    return .{ .visual = visual, .canvas = canvas };
}
fn snapshotState(frame: *Frame, parent: usize, position: usize) !State {
    var result: State = .{
        .page = @intFromPtr(frame.page),
        .frame = @intFromPtr(frame),
        .document = @intFromPtr(frame.document),
        .parent = parent,
        .position = position,
        .frame_id = frame._frame_id,
        .loader_id = frame._loader_id,
        .model = try model(frame.page),
        .resource_epoch = frame._render_resource_version.load(.acquire),
        .children = frame.child_frames.items.len,
        .scroll_x = frame.window.getScrollX(),
        .scroll_y = frame.window.getScrollY(),
        .focused_element = address(frame.document._active_element),
        .hovered = address(frame.page.input_hover_target),
        .pressed = address(frame.page.input_pointer.down_target),
        .keyboard_modality = frame.page.input_keyboard_modality,
    };
    if (result.resource_epoch == std.math.maxInt(u64)) return error.VisualUnstable;
    if (frame.document._active_element) |element| {
        if (element.is(Element.Html.Input)) |control| {
            result.control_focus = control._selection_end;
            result.control_anchor = control._selection_start;
            result.control_direction = @backingInt(control._selection_direction);
        } else if (element.is(Element.Html.TextArea)) |control| {
            result.control_focus = control._selection_end;
            result.control_anchor = control._selection_start;
            result.control_direction = @backingInt(control._selection_direction);
        }
    }
    if (frame.document._selection._range) |selected| {
        const range = selected.asAbstractRange();
        result.selection_node = @intFromPtr(range.getEndContainer());
        result.selection_offset = range.getEndOffset();
        result.selection_anchor_node = @intFromPtr(range.getStartContainer());
        result.selection_anchor_offset = range.getStartOffset();
        result.selection_direction = @backingInt(frame.document._selection._direction);
    }
    return result;
}
fn settleTree(frame: *Frame, page: *Page) !void {
    if (frame.page != page) return error.VisualOwnerMismatch;
    for (frame._canvas_surfaces.items) |surface| try surface.settleObservedWrites();
    for (frame.child_frames.items) |child| try settleTree(child, page);
}
fn saveTree(frame: *Frame, page: usize, key: Key, parent: usize, position: usize, count: *usize) !void {
    // Rendered iframe Frames share their Page owner. Never silently qualify an
    // independently mutating page through this coupled-frame guard.
    if (@intFromPtr(frame.page) != page) return error.VisualOwnerMismatch;
    const history = frame._native_guard_history orelse blk: {
        const created = try frame.page_arena.create(History);
        created.* = .{};
        frame._native_guard_history = created;
        break :blk created;
    };
    history.candidates[@intCast(key.generation % history_length)] = .{
        .key = key,
        .state = try snapshotState(frame, parent, position),
    };
    count.* += 1;
    for (frame.child_frames.items, 0..) |child, child_index| {
        try saveTree(child, page, key, @intFromPtr(frame), child_index, count);
    }
}
fn matchesTree(frame: *Frame, capture: *const Capture, parent: usize, position: usize, displayed: bool, count: *usize) bool {
    if (@intFromPtr(frame.page) != capture.page) return false;
    const history = frame._native_guard_history orelse return false;
    const record = if (displayed) &history.displayed else &history.candidates[@intCast(capture.key.generation % history_length)];
    if (!std.meta.eql(record.key, capture.key)) return false;
    const current = snapshotState(frame, parent, position) catch return false;
    if (!std.meta.eql(record.state, current)) return false;
    count.* += 1;
    for (frame.child_frames.items, 0..) |child, child_index| {
        if (!matchesTree(child, capture, @intFromPtr(frame), child_index, displayed, count)) return false;
    }
    return true;
}
fn pinTree(frame: *Frame, key: Key) void {
    const history = frame._native_guard_history.?;
    history.displayed = history.candidates[@intCast(key.generation % history_length)];
    for (frame.child_frames.items) |child| pinTree(child, key);
}

// Complete only already-observed GPU writes before taking the model snapshot.
// New concurrent commands make the subsequent before/after guard fail closed.
pub fn prepare(frame: *Frame, key: Key) !Capture {
    if (key.owner == 0 or key.generation == 0) return error.InvalidVisualCapture;
    try settleTree(frame, frame.page);
    const initial = try model(frame.page);
    const viewport = frame.page.getViewport();
    if (viewport.width == 0 or viewport.height == 0 or !std.math.isFinite(viewport.scale) or viewport.scale <= 0) return error.InvalidVisualCapture;
    var capture: Capture = .{
        .key = key,
        .page = @intFromPtr(frame.page),
        .document = @intFromPtr(frame.document),
        .frame_id = frame._frame_id,
        .loader_id = frame._loader_id,
        .model_stamp = initial.visual,
        .canvas_epoch = initial.canvas,
        .viewport = viewport,
    };
    try saveTree(frame, capture.page, key, 0, 0, &capture.frames);
    if (!validate(frame, capture, false)) return error.VisualUnstable;
    return capture;
}
pub fn validate(frame: *Frame, capture: Capture, displayed: bool) bool {
    if (capture.key.owner == 0 or capture.key.generation == 0 or capture.frames == 0 or
        capture.page != @intFromPtr(frame.page) or capture.document != @intFromPtr(frame.document) or
        capture.frame_id != frame._frame_id or capture.loader_id != frame._loader_id or
        !std.meta.eql(capture.viewport, frame.page.getViewport())) return false;
    const before = model(frame.page) catch return false;
    if (before.visual != capture.model_stamp or before.canvas != capture.canvas_epoch) return false;
    var count: usize = 0;
    if (!matchesTree(frame, &capture, 0, 0, displayed, &count) or count != capture.frames) return false;
    const after = model(frame.page) catch return false;
    return std.meta.eql(before, after);
}
pub fn pinDisplayed(frame: *Frame, capture: Capture) bool {
    if (!validate(frame, capture, false)) return false;
    pinTree(frame, capture.key);
    return validate(frame, capture, true);
}

const testing = @import("../testing.zig");
test "exact visual guard validates without layout and retains displayed authority" {
    const frame = try testing.createFrame();
    defer testing.test_session.closeAllPages();
    const capture = try prepare(frame, .{ .owner = 1, .generation = 1 });
    try testing.expect(frame._layout_document == null);
    try testing.expect(pinDisplayed(frame, capture));
    for (2..12) |generation| _ = try prepare(frame, .{ .owner = 1, .generation = generation });
    try testing.expect(validate(frame, capture, true));
    try testing.expect(!validate(frame, capture, false));
    frame.page.visualChanged();
    try testing.expect(!validate(frame, capture, true));
}
test "exact visual guard rejects in-flight, changed and poisoned canvas epochs" {
    const frame = try testing.createFrame();
    defer testing.test_session.closeAllPages();
    const capture = try prepare(frame, .{ .owner = 1, .generation = 1 });
    const writer = frame.page.beginCanvasWrite();
    try testing.expect(!validate(frame, capture, false));
    try testing.expectError(error.VisualUnstable, prepare(frame, .{ .owner = 1, .generation = 2 }));
    writer.end();
    try testing.expect(!validate(frame, capture, false));
    const next = try prepare(frame, .{ .owner = 1, .generation = 3 });
    try testing.expect(validate(frame, next, false));
    frame.page.invalidateCanvasWrites();
    try testing.expect(!validate(frame, next, false));
    try testing.expectError(error.VisualUnstable, prepare(frame, .{ .owner = 1, .generation = 4 }));
}
test "exact visual guard compares resource and loader identities without relying on notifications" {
    const frame = try testing.createFrame();
    defer testing.test_session.closeAllPages();
    const capture = try prepare(frame, .{ .owner = 1, .generation = 1 });
    const old_loader = frame._loader_id;
    frame._loader_id +%= 1;
    try testing.expect(!validate(frame, capture, false));
    frame._loader_id = old_loader;
    try testing.expect(validate(frame, capture, false));
    _ = frame._render_resource_version.fetchAdd(1, .release);
    try testing.expect(!validate(frame, capture, false));
}
