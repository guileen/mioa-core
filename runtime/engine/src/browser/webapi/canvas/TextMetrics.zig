// Copyright (C) 2023-2026 Lightpanda (Selecy SAS)
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
// SPDX-License-Identifier: AGPL-3.0-or-later

const std = @import("std");
const js = @import("../../js/js.zig");
const TextMetrics = @This();
/// Values are produced by the same native shaper that renders this context.
metrics: Native,
pub const Native = struct {
    width: f64,
    actualBoundingBoxLeft: f64,
    actualBoundingBoxRight: f64,
    actualBoundingBoxAscent: f64,
    actualBoundingBoxDescent: f64,
    fontBoundingBoxAscent: f64,
    fontBoundingBoxDescent: f64,
    emHeightAscent: f64,
    emHeightDescent: f64,
    alphabeticBaseline: f64,
    hangingBaseline: f64,
    ideographicBaseline: f64,
};
pub fn fromNative(bytes: []const u8, exec: *const js.Execution) !*TextMetrics {
    return exec._factory.create(TextMetrics{ .metrics = try std.json.parseFromSliceLeaky(Native, exec.local_arena, bytes, .{}) });
}
fn metric(comptime field: []const u8) fn (*const TextMetrics) f64 {
    return struct {
        fn get(self: *const TextMetrics) f64 {
            return @field(self.metrics, field);
        }
    }.get;
}
pub const JsApi = struct {
    pub const bridge = js.Bridge(TextMetrics);
    pub const Meta = struct {
        pub const name = "TextMetrics";
        pub var class_id: bridge.ClassId = undefined;
    };
    pub const width = bridge.accessor(metric("width"), null, .{});
    pub const actualBoundingBoxLeft = bridge.accessor(metric("actualBoundingBoxLeft"), null, .{});
    pub const actualBoundingBoxRight = bridge.accessor(metric("actualBoundingBoxRight"), null, .{});
    pub const actualBoundingBoxAscent = bridge.accessor(metric("actualBoundingBoxAscent"), null, .{});
    pub const actualBoundingBoxDescent = bridge.accessor(metric("actualBoundingBoxDescent"), null, .{});
    pub const fontBoundingBoxAscent = bridge.accessor(metric("fontBoundingBoxAscent"), null, .{});
    pub const fontBoundingBoxDescent = bridge.accessor(metric("fontBoundingBoxDescent"), null, .{});
    pub const emHeightAscent = bridge.accessor(metric("emHeightAscent"), null, .{});
    pub const emHeightDescent = bridge.accessor(metric("emHeightDescent"), null, .{});
    pub const alphabeticBaseline = bridge.accessor(metric("alphabeticBaseline"), null, .{});
    pub const hangingBaseline = bridge.accessor(metric("hangingBaseline"), null, .{});
    pub const ideographicBaseline = bridge.accessor(metric("ideographicBaseline"), null, .{});
};
