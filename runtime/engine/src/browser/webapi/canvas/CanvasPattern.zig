// Copyright (C) 2023-2026 Lightpanda (Selecy SAS)
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
// SPDX-License-Identifier: AGPL-3.0-or-later

const std = @import("std");
const js = @import("../../js/js.zig");
const context2d = @import("context2d.zig");
const CanvasPattern = @This();
width: u32,
height: u32,
pixels: []const u8,
repetition: []const u8,
transform: context2d.Matrix = context2d.identity,
origin_clean: bool,
pub fn init(source: context2d.PixelSource, repetition: []const u8, exec: *const js.Execution) !*CanvasPattern {
    return exec._factory.create(CanvasPattern{ .width = source.width, .height = source.height, .pixels = try exec.page_arena.dupe(u8, source.bytes), .repetition = try exec.page_arena.dupe(u8, repetition), .origin_clean = source.origin_clean });
}
fn setTransform(self: *CanvasPattern, value: ?@import("../DOMMatrixReadOnly.zig").DOMMatrixInit) !void {
    self.transform = try context2d.matrix(value orelse .{});
}
pub fn jsonStringify(self: *const CanvasPattern, writer: *std.json.Stringify) !void {
    try writer.write(.{ .width = self.width, .height = self.height, .pixels = @import("../../graphics_bytes.zig").Bytes{ .data = self.pixels }, .repetition = self.repetition, .transform = self.transform });
}
pub const JsApi = struct {
    pub const bridge = js.Bridge(CanvasPattern);
    pub const Meta = struct {
        pub const name = "CanvasPattern";
        pub var class_id: bridge.ClassId = undefined;
    };
    pub const setTransform = bridge.function(CanvasPattern.setTransform, .{});
};
