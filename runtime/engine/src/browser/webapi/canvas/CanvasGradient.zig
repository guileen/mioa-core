const std = @import("std");
const js = @import("../../js/js.zig");
const color = @import("../../color.zig");
const CanvasGradient = @This();
pub const Stop = struct {
    offset: f64,
    color: [4]u8,
    pub fn jsonStringify(self: Stop, writer: *std.json.Stringify) !void {
        try writer.write(.{ .offset = self.offset, .color = @import("../../graphics_bytes.zig").Bytes{ .data = &self.color } });
    }
};
kind: []const u8 = "linear",
coords: [6]f64 = @splat(0),
transform: [6]f64 = .{ 1, 0, 0, 1, 0, 0 },
stops: std.ArrayList(Stop) = .empty,
pub fn init(exec: *const js.Execution) !*CanvasGradient {
    return exec._factory.create(CanvasGradient{});
}
pub fn jsonStringify(self: *const CanvasGradient, writer: *std.json.Stringify) !void {
    try writer.write(.{ .kind = self.kind, .coords = self.coords, .transform = self.transform, .stops = self.stops.items });
}
fn addColorStop(self: *CanvasGradient, offset: f64, value: []const u8, exec: *js.Execution) !void {
    if (!std.math.isFinite(offset) or offset < 0 or offset > 1) return error.IndexSizeError;
    const rgba = color.RGBA.parse(value) catch return error.SyntaxError;
    try self.stops.append(exec.page_arena, .{ .offset = offset, .color = .{ rgba.r, rgba.g, rgba.b, rgba.a } });
}
pub const JsApi = struct {
    pub const bridge = js.Bridge(CanvasGradient);
    pub const Meta = struct {
        pub const name = "CanvasGradient";
        pub var class_id: bridge.ClassId = undefined;
    };
    pub const addColorStop = bridge.function(CanvasGradient.addColorStop, .{});
};
