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
const lp = @import("lightpanda");

const Frame = @import("../../../Frame.zig");
const Factory = @import("../../../Factory.zig");

const Blob = @import("../../Blob.zig");
const Node = @import("../../Node.zig");
const Element = @import("../../Element.zig");

const OffscreenCanvas = @import("../../canvas/OffscreenCanvas.zig");
const WebGLRenderingContext = @import("../../canvas/WebGLRenderingContext.zig");
const CanvasRenderingContext2D = @import("../../canvas/CanvasRenderingContext2D.zig");
const context2d = @import("../../canvas/context2d.zig");
const png_encoder = @import("../../../png.zig");

const HtmlElement = @import("../Html.zig");

const js = lp.js;
const log = lp.log;
const Execution = js.Execution;

const Canvas = @This();

pub const Proto = HtmlElement;
_proto_canary: if (lp.IS_DEBUG) *HtmlElement else void = undefined,
_cached: ?DrawingContext = null,
_origin_clean: bool = true,
_surface: ?*context2d.Surface = null,
_transferred: bool = false,

pub fn checkDrawing(self: *const Canvas) !void {
    if (self._transferred) return error.InvalidStateError;
}
pub fn originClean(self: *const Canvas) bool {
    return if (self._surface) |s| s.originClean() else self._origin_clean;
}
pub fn surface(self: *Canvas, exec: *Execution) !*context2d.Surface {
    if (self._surface) |s| return s;
    const s = try context2d.Surface.init(self.getWidth(), self.getHeight(), exec);
    s.observe(switch (exec.js.global) {
        .frame => |frame| frame,
        .worker => null,
    });
    self._surface = s;
    return s;
}

pub fn asElement(self: *Canvas) *Element {
    return Factory.protoOf(self).asElement();
}
pub fn asConstElement(self: *const Canvas) *const Element {
    return Factory.protoOf(self).asElement();
}
pub fn asNode(self: *Canvas) *Node {
    return self.asElement().asNode();
}

pub fn getWidth(self: *const Canvas) u32 {
    if (self._transferred) if (self._surface) |s| return s.size().width;
    const attr = self.asConstElement().getAttributeInterned("width") orelse return 300;
    return std.fmt.parseUnsigned(u32, attr, 10) catch 300;
}

pub fn getHeight(self: *const Canvas) u32 {
    if (self._transferred) if (self._surface) |s| return s.size().height;
    const attr = self.asConstElement().getAttributeInterned("height") orelse return 150;
    return std.fmt.parseUnsigned(u32, attr, 10) catch 150;
}

fn setWidth(self: *Canvas, value: u32, frame: *Frame) !void {
    try self.setDimension("width", value, frame);
}
fn setHeight(self: *Canvas, value: u32, frame: *Frame) !void {
    try self.setDimension("height", value, frame);
}
fn setDimension(self: *Canvas, comptime name: []const u8, value: u32, frame: *Frame) !void {
    try self.checkDrawing();
    var buffer: [24]u8 = undefined;
    const text = std.mem.print(&buffer, "{d}", .{value}) catch unreachable;
    try self.asElement().setAttributeSafe(.wrap(name), .wrap(text), frame);
}

/// Since there's no base class rendering contexts inherit from,
/// we're using tagged union.
const DrawingContext = union(enum) {
    @"2d": *CanvasRenderingContext2D,
    webgl: *WebGLRenderingContext,
};

fn getContext(self: *Canvas, context_type: []const u8, exec: *Execution) !?DrawingContext {
    const frame = switch (exec.js.global) {
        .frame => |frame| frame,
        .worker => return error.TypeError,
    };
    try self.checkDrawing();
    if (self._cached) |cached| {
        const matches = switch (cached) {
            .@"2d" => std.mem.eql(u8, context_type, "2d"),
            .webgl => std.mem.eql(u8, context_type, "webgl") or std.mem.eql(u8, context_type, "experimental-webgl"),
        };
        return if (matches) cached else null;
    }

    const drawing_context: DrawingContext = blk: {
        if (std.mem.eql(u8, context_type, "2d")) {
            const ctx = try CanvasRenderingContext2D.init(self, exec);
            break :blk .{ .@"2d" = ctx };
        }

        if (std.mem.eql(u8, context_type, "webgl") or std.mem.eql(u8, context_type, "experimental-webgl")) {
            const ctx = (try WebGLRenderingContext.init(self, exec)) orelse return null;
            break :blk .{ .webgl = ctx };
        }
        if (isWebGL(context_type)) frame.page.status.noteUnsupported(.webgl);
        return null;
    };
    self._cached = drawing_context;
    return drawing_context;
}

pub fn isWebGL(context_type: []const u8) bool {
    inline for (&.{ "webgl", "webgl2", "experimental-webgl" }) |needle| {
        if (std.mem.eql(u8, context_type, needle)) {
            return true;
        }
    }
    return false;
}

/// Capture dimensions, origin state and pixels under one shared-surface lock.
pub fn pixels(self: *const Canvas, arena: std.mem.Allocator) !context2d.PixelSource {
    if (self._surface) |s| return s.snapshot(arena);
    const width = self.getWidth();
    const height = self.getHeight();
    if (width > 16384 or height > 16384 or @as(u64, width) * height * 4 > 268435456) return error.InvalidStateError;
    const bytes = try arena.alloc(u8, @as(usize, width) * height * 4);
    @memset(bytes, 0);
    return .{ .width = width, .height = height, .bytes = bytes, .origin_clean = self._origin_clean };
}
fn png(self: *const Canvas, arena: std.mem.Allocator) !?[]const u8 {
    var writer: std.Io.Writer.Allocating = .init(arena);
    if (self._surface) |s| {
        if (!try s.png(&writer.writer)) return null;
        return writer.written();
    }
    const image = try self.pixels(arena);
    if (!image.origin_clean) return error.SecurityError;
    if (image.width == 0 or image.height == 0) return null;
    if (!png_encoder.write(image.bytes.ptr, image.width, image.height, &writer.writer, write)) return error.InvalidStateError;
    return writer.written();
}
fn write(context: *anyopaque, bytes: [*]const u8, len: usize) callconv(.c) bool {
    const writer: *std.Io.Writer = @ptrCast(@alignCast(context));
    writer.writeAll(bytes[0..len]) catch return false;
    return true;
}
fn toDataURL(self: *const Canvas, _: ?[]const u8, _: ?f64, exec: *Execution) ![]const u8 {
    const bytes = (try self.png(exec.local_arena)) orelse return "data:,";
    const codec = std.base64.standard.Encoder;
    const out = try exec.local_arena.alloc(u8, "data:image/png;base64,".len + codec.calcSize(bytes.len));
    @memcpy(out[0.."data:image/png;base64,".len], "data:image/png;base64,");
    _ = codec.encode(out["data:image/png;base64,".len..], bytes);
    return out;
}

/// Same image as `toDataURL`, handed to `callback` as a Blob from a task.
/// A canvas with no pixels calls back with null, per spec.
pub fn toBlob(self: *const Canvas, callback: js.Function.Global, _: ?[]const u8, _: ?f64, exec: *Execution) !void {
    const encoded = try self.png(exec.local_arena);
    const task = try exec._factory.create(ToBlobCallback{
        .exec = exec.*,
        // The spec checks the bitmap now, not when the task runs.
        .has_bitmap = encoded != null,
        .bytes = if (encoded) |bytes| try exec.page_arena.dupe(u8, bytes) else &.{},
        .callback = callback,
    });
    errdefer exec._factory.destroy(task);

    try exec._scheduler.add(task, ToBlobCallback.run, 0, .{
        .name = "canvas.toBlob",
        .finalizer = ToBlobCallback.cancelled,
    });
}

const ToBlobCallback = struct {
    exec: Execution,
    has_bitmap: bool,
    bytes: []const u8,
    callback: js.Function.Global,

    fn cancelled(ctx: *anyopaque) void {
        const self: *ToBlobCallback = @ptrCast(@alignCast(ctx));
        self.deinit();
    }

    fn deinit(self: *ToBlobCallback) void {
        self.callback.release();
        self.exec._factory.destroy(self);
    }

    fn run(ctx: *anyopaque) !?u32 {
        const self: *ToBlobCallback = @ptrCast(@alignCast(ctx));
        defer self.deinit();
        const exec = &self.exec;

        var blob: ?*Blob = null;
        if (self.has_bitmap) {
            const b = try Blob.initFromBytes(self.bytes, "image/png", exec);
            // The page can hold on to the Blob, in which case the JS wrapper
            // takes its own ref; ours only has to cover the call.
            b.acquireRef();
            blob = b;
        }
        defer if (blob) |b| {
            b.releaseRef(exec.page);
        };

        var ls: js.Local.Scope = undefined;
        exec.js.localScope(&ls);
        defer ls.deinit();

        ls.toLocal(self.callback).call(void, .{blob}) catch |err| {
            exec.page.recordJsError(err);
            log.debug(.js, "canvas.toBlob", .{ .err = err });
        };
        ls.local.runMicrotasks();
        return null;
    }
};

pub const Build = struct {
    pub fn attributeChange(element: *Element, name: lp.String, _: lp.String, frame: *Frame) !void {
        try changed(element, name, frame);
    }
    pub fn attributeRemove(element: *Element, name: lp.String, frame: *Frame) !void {
        try changed(element, name, frame);
    }
    fn changed(element: *Element, name: lp.String, frame: *Frame) !void {
        if (!name.eql(comptime .wrap("width")) and !name.eql(comptime .wrap("height"))) return;
        const canvas = element.as(Canvas);
        try canvas.checkDrawing();
        if (canvas._cached) |context| switch (context) {
            .@"2d" => |ctx| try ctx.resize(),
            .webgl => |ctx| try ctx.resize(),
        };
        canvas._origin_clean = true;
        _ = frame._render_resource_version.fetchAdd(1, .release);
        frame.page.visualChanged();
    }
};

/// The DOM canvas becomes a placeholder observing this same native surface.
fn transferControlToOffscreen(self: *Canvas, exec: *Execution) !*OffscreenCanvas {
    if (self._transferred or self._cached != null) return error.InvalidStateError;
    const s = try self.surface(exec);
    const result = try OffscreenCanvas.receiveTransfer(s, exec);
    self._transferred = true;
    return result;
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(Canvas);

    pub const Meta = struct {
        pub const name = "HTMLCanvasElement";
        pub var class_id: bridge.ClassId = undefined;
    };

    pub const width = bridge.accessor(Canvas.getWidth, Canvas.setWidth, .{ .ce_reactions = true });
    pub const height = bridge.accessor(Canvas.getHeight, Canvas.setHeight, .{ .ce_reactions = true });
    pub const getContext = bridge.function(Canvas.getContext, .{});
    pub const toDataURL = bridge.function(Canvas.toDataURL, .{});
    pub const toBlob = bridge.function(Canvas.toBlob, .{});
    pub const transferControlToOffscreen = bridge.function(Canvas.transferControlToOffscreen, .{});
};

const testing = @import("../../../../testing.zig");
test "WebApi: HTMLCanvasElement serialization" {
    try testing.htmlRunner("canvas/canvas_serialization.html", .{});
}
