const std = @import("std");
const js = @import("../../js/js.zig");
const context2d = @import("context2d.zig");
const PathData = @import("../svg/PathData.zig");
const Path2D = @This();
pub const _prototype_root = true;
pub const Point = struct { x: f64 = 0, y: f64 = 0 };
pub const Command = struct { t: []const u8, x: f64 = 0, y: f64 = 0, x1: f64 = 0, y1: f64 = 0, x2: f64 = 0, y2: f64 = 0 };
commands: std.ArrayList(Command) = .empty,
current: ?Point = null,
start: ?Point = null,
_allocator: ?std.mem.Allocator = null,
fn allocator(self: *const Path2D, e: *js.Execution) std.mem.Allocator {
    return self._allocator orelse e.page_arena;
}
pub const Input = union(enum) { path: *Path2D, svg: []const u8 };
pub fn constructor(input: ?Input, e: *js.Execution) !*Path2D {
    const path = try e._factory.create(Path2D{});
    if (input) |value| switch (value) {
        .path => |other| {
            try path.commands.appendSlice(e.page_arena, other.commands.items);
            path.current = other.current;
            path.start = other.start;
        },
        .svg => |svg| try path.parse(svg, e),
    };
    return path;
}
fn parse(self: *Path2D, svg: []const u8, e: *js.Execution) !void {
    const parsed = try PathData.parseWithEvents(svg, e.local_arena);
    // Reuse LP's SVG curve parser while preserving explicit move/close events.
    var boundary: usize = 0;
    for (0..parsed.segments.items.len + 1) |index| {
        while (boundary < parsed.boundaries.items.len and parsed.boundaries.items[boundary].segment_index == index) {
            const event = parsed.boundaries.items[boundary];
            boundary += 1;
            switch (event.kind) {
                .move => try self.moveTo(event.point.x, event.point.y, e),
                .close => try self.closePath(e),
            }
        }
        if (index == parsed.segments.items.len) break;
        switch (parsed.segments.items[index]) {
            .line => |s| try self.lineTo(s.end.x, s.end.y, e),
            .quadratic => |s| try self.quadraticCurveTo(s.control.x, s.control.y, s.end.x, s.end.y, e),
            .cubic => |s| try self.bezierCurveTo(s.control1.x, s.control1.y, s.control2.x, s.control2.y, s.end.x, s.end.y, e),
            .arc => |s| try self.ellipse(s.center.x, s.center.y, s.rx, s.ry, s.rotation, s.theta, s.theta + s.delta, s.delta < 0, e),
        }
    }
}
fn finite(values: []const f64) bool {
    for (values) |v| if (!std.math.isFinite(v)) return false;
    return true;
}
pub fn moveTo(self: *Path2D, x: f64, y: f64, e: *js.Execution) !void {
    if (!finite(&.{ x, y })) return;
    try self.commands.append(self.allocator(e), .{ .t = "M", .x = x, .y = y });
    self.current = .{ .x = x, .y = y };
    self.start = self.current;
}
pub fn lineTo(self: *Path2D, x: f64, y: f64, e: *js.Execution) !void {
    if (!finite(&.{ x, y })) return;
    if (self.current == null) return self.moveTo(x, y, e);
    try self.commands.append(self.allocator(e), .{ .t = "L", .x = x, .y = y });
    self.current = .{ .x = x, .y = y };
}
pub fn closePath(self: *Path2D, e: *js.Execution) !void {
    if (self.start == null) return;
    try self.commands.append(self.allocator(e), .{ .t = "Z" });
    self.current = self.start;
}
pub fn quadraticCurveTo(self: *Path2D, x1: f64, y1: f64, x: f64, y: f64, e: *js.Execution) !void {
    if (!finite(&.{ x1, y1, x, y })) return;
    if (self.current == null) try self.moveTo(x1, y1, e);
    try self.commands.append(self.allocator(e), .{ .t = "Q", .x1 = x1, .y1 = y1, .x = x, .y = y });
    self.current = .{ .x = x, .y = y };
}
pub fn bezierCurveTo(self: *Path2D, x1: f64, y1: f64, x2: f64, y2: f64, x: f64, y: f64, e: *js.Execution) !void {
    if (!finite(&.{ x1, y1, x2, y2, x, y })) return;
    if (self.current == null) try self.moveTo(x1, y1, e);
    try self.commands.append(self.allocator(e), .{ .t = "C", .x1 = x1, .y1 = y1, .x2 = x2, .y2 = y2, .x = x, .y = y });
    self.current = .{ .x = x, .y = y };
}
pub fn rect(self: *Path2D, x: f64, y: f64, w: f64, h: f64, e: *js.Execution) !void {
    if (!finite(&.{ x, y, w, h })) return;
    try self.moveTo(x, y, e);
    try self.lineTo(x + w, y, e);
    try self.lineTo(x + w, y + h, e);
    try self.lineTo(x, y + h, e);
    try self.closePath(e);
}
pub fn arc(self: *Path2D, x: f64, y: f64, r: f64, start_: f64, end: f64, ccw: ?bool, e: *js.Execution) !void {
    try self.ellipse(x, y, r, r, 0, start_, end, ccw, e);
}
fn ellipsePoint(cx: f64, cy: f64, rx: f64, ry: f64, rotation: f64, t: f64) Point {
    return .{ .x = cx + rx * @cos(t) * @cos(rotation) - ry * @sin(t) * @sin(rotation), .y = cy + rx * @cos(t) * @sin(rotation) + ry * @sin(t) * @cos(rotation) };
}
fn ellipseTangent(rx: f64, ry: f64, rotation: f64, t: f64) Point {
    return .{ .x = -rx * @sin(t) * @cos(rotation) - ry * @cos(t) * @sin(rotation), .y = -rx * @sin(t) * @sin(rotation) + ry * @cos(t) * @cos(rotation) };
}
pub fn ellipse(self: *Path2D, cx: f64, cy: f64, rx: f64, ry: f64, rotation: f64, start_: f64, end: f64, ccw: ?bool, e: *js.Execution) !void {
    if (rx < 0 or ry < 0) return error.IndexSizeError;
    if (!finite(&.{ cx, cy, rx, ry, rotation, start_, end })) return;
    const tau = 2 * std.math.pi;
    var delta = end - start_;
    if (ccw orelse false) {
        if (-delta >= tau) delta = -tau else {
            delta = @mod(delta, tau);
            if (delta > 0) delta -= tau;
        }
    } else {
        if (delta >= tau) delta = tau else delta = @mod(delta, tau);
    }
    const first = ellipsePoint(cx, cy, rx, ry, rotation, start_);
    if (self.current == null) try self.moveTo(first.x, first.y, e) else try self.lineTo(first.x, first.y, e);
    if (delta == 0) return;
    const count: usize = @intFromFloat(@ceil(@abs(delta) / (std.math.pi / @as(f64, 2))));
    const step = delta / @as(f64, @floatFromInt(count));
    for (0..count) |i| {
        const a = start_ + @as(f64, @floatFromInt(i)) * step;
        const b = a + step;
        const p0 = ellipsePoint(cx, cy, rx, ry, rotation, a);
        const p1 = ellipsePoint(cx, cy, rx, ry, rotation, b);
        const v0 = ellipseTangent(rx, ry, rotation, a);
        const v1 = ellipseTangent(rx, ry, rotation, b);
        const k = 4.0 / 3.0 * @tan(step / 4);
        try self.bezierCurveTo(p0.x + k * v0.x, p0.y + k * v0.y, p1.x - k * v1.x, p1.y - k * v1.y, p1.x, p1.y, e);
    }
}
pub fn arcTo(self: *Path2D, x1: f64, y1: f64, x2: f64, y2: f64, r: f64, e: *js.Execution) !void {
    if (r < 0) return error.IndexSizeError;
    if (!finite(&.{ x1, y1, x2, y2, r })) return;
    const p = self.current orelse return self.moveTo(x1, y1, e);
    var ux = p.x - x1;
    var uy = p.y - y1;
    var vx = x2 - x1;
    var vy = y2 - y1;
    const ul = @sqrt(ux * ux + uy * uy);
    const vl = @sqrt(vx * vx + vy * vy);
    const cross = ux * vy - uy * vx;
    if (ul == 0 or vl == 0 or cross == 0 or r == 0) return self.lineTo(x1, y1, e);
    ux /= ul;
    uy /= ul;
    vx /= vl;
    vy /= vl;
    const angle = std.math.acos(std.math.clamp(ux * vx + uy * vy, -1, 1));
    const dist = r / @tan(angle / 2);
    const t0 = Point{ .x = x1 + ux * dist, .y = y1 + uy * dist };
    const t1 = Point{ .x = x1 + vx * dist, .y = y1 + vy * dist };
    const sign: f64 = if (cross < 0) -1 else 1;
    const cx = t0.x - sign * uy * r;
    const cy = t0.y + sign * ux * r;
    try self.lineTo(t0.x, t0.y, e);
    try self.arc(cx, cy, r, std.math.atan2(t0.y - cy, t0.x - cx), std.math.atan2(t1.y - cy, t1.x - cx), cross > 0, e);
}
pub const Radius = union(enum) { number: f64, point: Point };
pub const Radii = union(enum) { number: f64, list: []const Radius, point: Point };
fn radius(value: Radius) !Point {
    const p = switch (value) {
        .number => |n| Point{ .x = n, .y = n },
        .point => |point| point,
    };
    if (p.x < 0 or p.y < 0) return error.RangeError;
    return p;
}
pub fn normalizeRadii(input: ?Radii) ![4]Point {
    var points: [4]Point = undefined;
    var n: usize = 1;
    points[0] = .{ .x = 0, .y = 0 };
    if (input) |value| switch (value) {
        .number => |v| points[0] = try radius(.{ .number = v }),
        .point => |p| points[0] = try radius(.{ .point = p }),
        .list => |values| {
            n = values.len;
            if (n == 0 or n > 4) return error.RangeError;
            for (values, 0..) |v, i| points[i] = try radius(v);
        },
    };
    return switch (n) {
        1 => .{ points[0], points[0], points[0], points[0] },
        2 => .{ points[0], points[1], points[0], points[1] },
        3 => .{ points[0], points[1], points[2], points[1] },
        else => points,
    };
}
pub fn roundRect(self: *Path2D, x_: f64, y_: f64, w_: f64, h_: f64, input: ?Radii, e: *js.Execution) !void {
    var r = try normalizeRadii(input);
    if (!finite(&.{ x_, y_, w_, h_ })) return;
    for (r) |p| if (!finite(&.{ p.x, p.y })) return;
    var x = x_;
    var y = y_;
    var w = w_;
    var h = h_;
    const reverse = w * h < 0;
    if (w < 0) {
        x += w;
        w = -w;
        std.mem.swap(Point, &r[0], &r[1]);
        std.mem.swap(Point, &r[3], &r[2]);
    }
    if (h < 0) {
        y += h;
        h = -h;
        std.mem.swap(Point, &r[0], &r[3]);
        std.mem.swap(Point, &r[1], &r[2]);
    }
    var factor: f64 = 1;
    const sums = [4]f64{ r[0].x + r[1].x, r[3].x + r[2].x, r[0].y + r[3].y, r[1].y + r[2].y };
    for (sums, 0..) |sum, i| if (sum > 0) {
        factor = @min(factor, (if (i < 2) w else h) / sum);
    };
    for (&r) |*p| {
        p.x *= factor;
        p.y *= factor;
    }
    const begin = self.commands.items.len;
    try self.moveTo(x + r[0].x, y, e);
    try self.lineTo(x + w - r[1].x, y, e);
    try self.ellipse(x + w - r[1].x, y + r[1].y, r[1].x, r[1].y, 0, -std.math.pi / @as(f64, 2), 0, false, e);
    try self.lineTo(x + w, y + h - r[2].y, e);
    try self.ellipse(x + w - r[2].x, y + h - r[2].y, r[2].x, r[2].y, 0, 0, std.math.pi / @as(f64, 2), false, e);
    try self.lineTo(x + r[3].x, y + h, e);
    try self.ellipse(x + r[3].x, y + h - r[3].y, r[3].x, r[3].y, 0, std.math.pi / @as(f64, 2), std.math.pi, false, e);
    try self.lineTo(x, y + r[0].y, e);
    try self.ellipse(x + r[0].x, y + r[0].y, r[0].x, r[0].y, 0, std.math.pi, 3 * std.math.pi / @as(f64, 2), false, e);
    try self.closePath(e);
    if (reverse) {
        // Reverse this subpath's winding, including cubic control directions.
        const old = try e.local_arena.dupe(Command, self.commands.items[begin..]);
        self.commands.shrinkRetainingCapacity(begin);
        const endpoints = try e.local_arena.alloc(Point, old.len);
        var current_: Point = .{ .x = old[0].x, .y = old[0].y };
        for (old, 0..) |cmd, i| {
            endpoints[i] = current_;
            if (!std.mem.eql(u8, cmd.t, "Z")) current_ = .{ .x = cmd.x, .y = cmd.y };
        }
        try self.moveTo(current_.x, current_.y, e);
        var i = old.len;
        while (i > 1) {
            i -= 1;
            const cmd = old[i];
            if (std.mem.eql(u8, cmd.t, "Z")) continue;
            const p = endpoints[i];
            if (std.mem.eql(u8, cmd.t, "C")) try self.bezierCurveTo(cmd.x2, cmd.y2, cmd.x1, cmd.y1, p.x, p.y, e) else try self.lineTo(p.x, p.y, e);
        }
        try self.closePath(e);
    }
}
pub fn addPath(self: *Path2D, other: *Path2D, init: ?@import("../DOMMatrixReadOnly.zig").DOMMatrixInit, e: *js.Execution) !void {
    const m = try context2d.matrix(init orelse .{});
    for (m) |v| if (!std.math.isFinite(v)) return;
    const count = other.commands.items.len;
    const source = if (self == other) try e.local_arena.dupe(Command, other.commands.items[0..count]) else other.commands.items[0..count];
    for (source) |cmd| {
        const p = apply(m, .{ .x = cmd.x, .y = cmd.y });
        const p1 = apply(m, .{ .x = cmd.x1, .y = cmd.y1 });
        const p2 = apply(m, .{ .x = cmd.x2, .y = cmd.y2 });
        if (std.mem.eql(u8, cmd.t, "M")) try self.moveTo(p.x, p.y, e) else if (std.mem.eql(u8, cmd.t, "L")) try self.lineTo(p.x, p.y, e) else if (std.mem.eql(u8, cmd.t, "Q")) try self.quadraticCurveTo(p1.x, p1.y, p.x, p.y, e) else if (std.mem.eql(u8, cmd.t, "C")) try self.bezierCurveTo(p1.x, p1.y, p2.x, p2.y, p.x, p.y, e) else try self.closePath(e);
    }
}
fn apply(m: context2d.Matrix, p: Point) Point {
    return .{ .x = m[0] * p.x + m[2] * p.y + m[4], .y = m[1] * p.x + m[3] * p.y + m[5] };
}
pub const JsApi = struct {
    pub const bridge = js.Bridge(Path2D);
    pub const Meta = struct {
        pub const name = "Path2D";
        pub var class_id: bridge.ClassId = undefined;
    };
    pub const constructor = bridge.constructor(Path2D.constructor, .{});
    pub const moveTo = bridge.function(Path2D.moveTo, .{});
    pub const lineTo = bridge.function(Path2D.lineTo, .{});
    pub const closePath = bridge.function(Path2D.closePath, .{});
    pub const quadraticCurveTo = bridge.function(Path2D.quadraticCurveTo, .{});
    pub const bezierCurveTo = bridge.function(Path2D.bezierCurveTo, .{});
    pub const rect = bridge.function(Path2D.rect, .{});
    pub const arc = bridge.function(Path2D.arc, .{});
    pub const ellipse = bridge.function(Path2D.ellipse, .{});
    pub const arcTo = bridge.function(Path2D.arcTo, .{});
    pub const roundRect = bridge.function(Path2D.roundRect, .{});
    pub const addPath = bridge.function(Path2D.addPath, .{});
};
