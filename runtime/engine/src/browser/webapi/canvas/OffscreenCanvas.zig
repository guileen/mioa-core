// Copyright (C) 2023-2026 Lightpanda (Selecy SAS)
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
// SPDX-License-Identifier: AGPL-3.0-or-later

const std = @import("std");
const js = @import("../../js/js.zig");
const context2d = @import("context2d.zig");
const Context = @import("OffscreenCanvasRenderingContext2D.zig");
const WebGL = @import("WebGLRenderingContext.zig");
const Blob = @import("../Blob.zig");
const Execution = js.Execution;
const OffscreenCanvas = @This();
pub const _prototype_root = true;
_width: u32,
_height: u32,
_surface: ?*context2d.Surface = null,
_cached: ?DrawingContext = null,
_detached: bool = false,
pub const DrawingContext = union(enum) { @"2d": *Context, webgl: *WebGL };
pub fn constructor(width: u32, height: u32, exec: *Execution) !*OffscreenCanvas {
    return exec._factory.create(OffscreenCanvas{ ._width = width, ._height = height, ._surface = try context2d.Surface.init(width, height, exec) });
}
pub fn getWidth(self: *const OffscreenCanvas) u32 {
    return if (self._detached) 0 else self._width;
}
pub fn getHeight(self: *const OffscreenCanvas) u32 {
    return if (self._detached) 0 else self._height;
}
fn setWidth(self: *OffscreenCanvas, value: u32) !void {
    try self.checkDrawing();
    self._width = value;
    try self.resize();
}
fn setHeight(self: *OffscreenCanvas, value: u32) !void {
    try self.checkDrawing();
    self._height = value;
    try self.resize();
}
fn resize(self: *OffscreenCanvas) !void {
    if (self._cached) |ctx| switch (ctx) {
        .@"2d" => |context| try context.resize(),
        .webgl => |context| try context.resize(),
    } else if (self._surface) |s| try s.resize(self._width, self._height);
}
pub fn checkDrawing(self: *const OffscreenCanvas) !void {
    if (self._detached) return error.InvalidStateError;
}
pub fn surface(self: *OffscreenCanvas, exec: *Execution) !*context2d.Surface {
    try self.checkDrawing();
    if (self._surface) |s| return s;
    const s = try context2d.Surface.init(self._width, self._height, exec);
    self._surface = s;
    return s;
}
fn getContext(self: *OffscreenCanvas, kind: []const u8, exec: *Execution) !?DrawingContext {
    try self.checkDrawing();
    if (self._cached) |ctx| return switch (ctx) {
        .@"2d" => if (std.mem.eql(u8, kind, "2d")) ctx else null,
        .webgl => if (std.mem.eql(u8, kind, "webgl") or std.mem.eql(u8, kind, "experimental-webgl")) ctx else null,
    };
    const ctx: DrawingContext = if (std.mem.eql(u8, kind, "2d")) .{ .@"2d" = try Context.init(self, exec) } else if (std.mem.eql(u8, kind, "webgl") or std.mem.eql(u8, kind, "experimental-webgl")) .{ .webgl = (try WebGL.initOffscreen(self, exec)) orelse return null } else return null;
    self._cached = ctx;
    return ctx;
}
const BlobOptions = struct { quality: ?f64 = null, type: []const u8 = "image/png" };
fn convertToBlob(self: *OffscreenCanvas, _: ?BlobOptions, exec: *Execution) !js.Promise {
    const local = exec.js.local.?;
    if (self._detached) return local.rejectPromise(.{ .dom_exception = .{ .err = error.InvalidStateError } });
    const s = try self.surface(exec);
    var writer: std.Io.Writer.Allocating = .init(exec.local_arena);
    const encoded = s.png(&writer.writer) catch |err| return local.rejectPromise(.{ .dom_exception = .{ .err = err } });
    if (!encoded) return local.rejectPromise(.{ .dom_exception = .{ .err = error.IndexSizeError } });
    return local.resolvePromise(try Blob.initFromBytes(writer.written(), "image/png", exec));
}
fn transferToImageBitmap(self: *OffscreenCanvas, exec: *Execution) !*ImageBitmap {
    const s = try self.surface(exec);
    if (self._cached == null) return error.InvalidStateError;
    const image = try s.takeBitmap(exec.page_arena, exec.local_arena);
    return exec._factory.create(ImageBitmap{ ._width = image.width, ._height = image.height, ._pixels = image.bytes, ._origin_clean = image.origin_clean });
}
pub fn validateTransfer(self: *OffscreenCanvas) !void {
    if (self._detached or self._cached != null) return error.DataClone;
}
pub fn transferSurface(self: *OffscreenCanvas) !*context2d.Surface {
    try self.validateTransfer();
    return self._surface orelse return error.InvalidStateError;
}
pub fn commitTransfer(self: *OffscreenCanvas) void {
    self._detached = true;
}
pub fn receiveTransfer(s: *context2d.Surface, exec: *Execution) !*OffscreenCanvas {
    const dimensions = s.size();
    const result = try exec._factory.create(OffscreenCanvas{ ._width = dimensions.width, ._height = dimensions.height, ._surface = s });
    try s.retainIn(exec);
    return result;
}
pub const ImageBitmap = struct {
    const Self = @This();
    pub const _prototype_root = true;
    _width: u32,
    _height: u32,
    _pixels: []const u8,
    _origin_clean: bool = true,
    _closed: bool = false,
    fn width(self: *const Self) u32 {
        return if (self._closed) 0 else self._width;
    }
    fn height(self: *const Self) u32 {
        return if (self._closed) 0 else self._height;
    }
    fn close(self: *Self) void {
        self._closed = true;
        self._width = 0;
        self._height = 0;
        self._pixels = &.{};
    }
    pub const JsApi = struct {
        pub const bridge = js.Bridge(Self);
        pub const Meta = struct {
            pub const name = "ImageBitmap";
            pub var class_id: bridge.ClassId = undefined;
        };
        pub const width = bridge.accessor(Self.width, null, .{});
        pub const height = bridge.accessor(Self.height, null, .{});
        pub const close = bridge.function(Self.close, .{});
    };
};
pub const JsApi = struct {
    pub const bridge = js.Bridge(OffscreenCanvas);
    pub const Meta = struct {
        pub const name = "OffscreenCanvas";
        pub var class_id: bridge.ClassId = undefined;
    };
    pub const constructor = bridge.constructor(OffscreenCanvas.constructor, .{});
    pub const width = bridge.accessor(OffscreenCanvas.getWidth, OffscreenCanvas.setWidth, .{});
    pub const height = bridge.accessor(OffscreenCanvas.getHeight, OffscreenCanvas.setHeight, .{});
    pub const getContext = bridge.function(OffscreenCanvas.getContext, .{});
    pub const convertToBlob = bridge.function(OffscreenCanvas.convertToBlob, .{});
    pub const transferToImageBitmap = bridge.function(OffscreenCanvas.transferToImageBitmap, .{});
};
const testing = @import("../../../testing.zig");
test "WebApi: OffscreenCanvas" {
    try testing.htmlRunner("canvas/offscreen_canvas.html", .{});
}
