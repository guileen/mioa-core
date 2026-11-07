const context2d = @import("context2d.zig");
const Canvas = @import("../element/html/Canvas.zig");
const Self = @This();
_canvas: *Canvas,
_drawing: context2d.Drawing,
const Impl = context2d.Context(Self, Canvas, "CanvasRenderingContext2D");
pub const init = Impl.init;
pub const resize = Impl.resize;
pub const JsApi = Impl.JsApi;

const testing = @import("../../../testing.zig");
test "WebApi: native CanvasRenderingContext2D" {
    try testing.htmlRunner("canvas/canvas_rendering_context_2d.html", .{});
}
