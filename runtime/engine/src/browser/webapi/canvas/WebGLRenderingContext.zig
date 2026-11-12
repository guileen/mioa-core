const std = @import("std");
const lp = @import("lightpanda");
const js = @import("../../js/js.zig");
const Canvas = @import("../element/html/Canvas.zig");
const OffscreenCanvas = @import("OffscreenCanvas.zig");
const Surface = @import("context2d.zig").Surface;
const Execution = js.Execution;
const Bytes = @import("../../graphics_bytes.zig").Bytes;
const WebGLRenderingContext = @This();
pub const Backend = opaque {};
_canvas: CanvasOwner,
_surface: *Surface,
_error: u32 = 0,
_unpack_flip_y: bool = false,
_unpack_premultiply: bool = false,
_unpack_colorspace: u32 = 0x9244,
_unpack_alignment: usize = 4,
_objects: std.AutoHashMapUnmanaged(u64, NativeObject) = .empty,
const CanvasOwner = union(enum) {
    html: *Canvas,
    offscreen: *OffscreenCanvas,
    fn width(self: CanvasOwner) u32 {
        return switch (self) {
            .html => |canvas| canvas.getWidth(),
            .offscreen => |canvas| canvas.getWidth(),
        };
    }
    fn height(self: CanvasOwner) u32 {
        return switch (self) {
            .html => |canvas| canvas.getHeight(),
            .offscreen => |canvas| canvas.getHeight(),
        };
    }
};

fn Object(comptime class_name: []const u8) type {
    return struct {
        const Self = @This();
        id: u32,
        delete_requested: bool = false,
        owner: *WebGLRenderingContext,
        pub const JsApi = struct {
            pub const bridge = js.Bridge(Self);
            pub const Meta = struct {
                pub const name = class_name;
                pub var class_id: bridge.ClassId = undefined;
            };
        };
    };
}
pub const Shader = Object("WebGLShader");
pub const Program = Object("WebGLProgram");
pub const Buffer = Object("WebGLBuffer");
pub const Uniform = Object("WebGLUniformLocation");
pub const Texture = Object("WebGLTexture");
pub const Framebuffer = Object("WebGLFramebuffer");
pub const Renderbuffer = Object("WebGLRenderbuffer");
const Kind = enum { program, buffer, texture, framebuffer, renderbuffer, shader };
const NativeObject = union(Kind) { program: *Program, buffer: *Buffer, texture: *Texture, framebuffer: *Framebuffer, renderbuffer: *Renderbuffer, shader: *Shader };
fn remember(self: *WebGLRenderingContext, value: anytype, comptime kind: Kind, exec: *Execution) !@TypeOf(value) {
    const key = (@as(u64, @backingInt(kind)) << 32) | value.id;
    try self._objects.put(exec.page_arena, key, @unionInit(NativeObject, @tagName(kind), value));
    return value;
}
fn matchesIdentity(self: *WebGLRenderingContext, object: anytype, comptime kind: Kind) bool {
    const key = (@as(u64, @backingInt(kind)) << 32) | object.id;
    const registered = self._objects.get(key) orelse return false;
    return registered == kind and @field(registered, @tagName(kind)) == object;
}
const DebugInfo = struct {
    pub const JsApi = struct {
        pub const bridge = js.Bridge(DebugInfo);
        pub const Meta = struct {
            pub const name = "WEBGL_debug_renderer_info";
            pub var class_id: bridge.ClassId = undefined;
        };
        pub const UNMASKED_VENDOR_WEBGL = bridge.property(@as(u32, 0x9245), .{ .template = true });
        pub const UNMASKED_RENDERER_WEBGL = bridge.property(@as(u32, 0x9246), .{ .template = true });
    };
};
const PrecisionValues = struct { rangeMin: i32, rangeMax: i32, precision: i32 };
pub const ShaderPrecision = struct {
    value: PrecisionValues,
    fn rangeMin(self: *const ShaderPrecision) i32 {
        return self.value.rangeMin;
    }
    fn rangeMax(self: *const ShaderPrecision) i32 {
        return self.value.rangeMax;
    }
    fn precision(self: *const ShaderPrecision) i32 {
        return self.value.precision;
    }
    pub const JsApi = struct {
        pub const bridge = js.Bridge(ShaderPrecision);
        pub const Meta = struct {
            pub const name = "WebGLShaderPrecisionFormat";
            pub var class_id: bridge.ClassId = undefined;
        };
        pub const rangeMin = bridge.accessor(ShaderPrecision.rangeMin, null, .{});
        pub const rangeMax = bridge.accessor(ShaderPrecision.rangeMax, null, .{});
        pub const precision = bridge.accessor(ShaderPrecision.precision, null, .{});
    };
};
pub fn registerTypes() []const type {
    return &.{ WebGLRenderingContext, Shader, Program, Buffer, Uniform, Texture, Framebuffer, Renderbuffer, DebugInfo, ShaderPrecision };
}
pub fn init(canvas: *Canvas, exec: *Execution) !?*WebGLRenderingContext {
    return initialize(.{ .html = canvas }, exec);
}
pub fn initOffscreen(canvas: *OffscreenCanvas, exec: *Execution) !?*WebGLRenderingContext {
    return initialize(.{ .offscreen = canvas }, exec);
}
fn initialize(owner: CanvasOwner, exec: *Execution) !?*WebGLRenderingContext {
    const surface = try switch (owner) {
        .html => |canvas| canvas.surface(exec),
        .offscreen => |canvas| canvas.surface(exec),
    };
    const context = try exec._factory.create(WebGLRenderingContext{ ._canvas = owner, ._surface = surface });
    _ = (try surface.initializeGl()) orelse return null;
    return context;
}
pub fn release(backend: *Backend) void {
    lp_gl_free(backend);
}
fn write(ctx: *anyopaque, data: [*]const u8, len: usize) callconv(.c) bool {
    const writer: *std.Io.Writer = @ptrCast(@alignCast(ctx));
    writer.writeAll(data[0..len]) catch return false;
    return true;
}
fn commandMutates(comptime method: []const u8) bool {
    return !(std.mem.startsWith(u8, method, "get") or
        std.mem.startsWith(u8, method, "is") or
        std.mem.eql(u8, method, "checkFramebufferStatus") or
        std.mem.eql(u8, method, "contextStatus") or
        std.mem.eql(u8, method, "finish") or
        std.mem.eql(u8, method, "flush"));
}
fn rawCommand(self: *WebGLRenderingContext, comptime method: []const u8, args: anytype, exec: *Execution) ![]const u8 {
    const request = try std.json.Stringify.valueAlloc(exec.local_arena, .{ .method = method, .args = args }, .{});
    var result: std.Io.Writer.Allocating = .init(exec.local_arena);
    const access = try self._surface.beginGlAccess(comptime commandMutates(method));
    defer access.end();
    if (!lp_gl_command(access.backend, request.ptr, request.len, &result.writer, write)) return error.InvalidOperationError;
    return result.written();
}
fn command(self: *WebGLRenderingContext, comptime method: []const u8, args: anytype, exec: *Execution) !js.Value {
    return exec.js.local.?.parseJSON(try self.rawCommand(method, args, exec));
}
fn objectId(self: *WebGLRenderingContext, value: anytype) !u32 {
    if (value) |object| {
        if (object.owner != self) return error.InvalidOperationError;
        if (object.delete_requested) {
            if (comptime @TypeOf(object) == *Shader) {
                if (!self.matchesIdentity(object, .shader)) return error.InvalidOperationError;
            } else if (comptime @TypeOf(object) == *Program) {
                if (!self.matchesIdentity(object, .program)) return error.InvalidOperationError;
            } else return error.InvalidOperationError;
        }
        return object.id;
    }
    return 0;
}
fn isLiveObject(self: *WebGLRenderingContext, value: anytype, comptime kind: Kind, comptime operation: []const u8, exec: *Execution) !bool {
    const object = value orelse return false;
    // A native name can be reused after deletion, so validate the original
    // wrapper and its context before querying the driver's current object.
    if (object.owner != self or !self.matchesIdentity(object, kind)) return false;
    if (kind != .shader and kind != .program and object.delete_requested) return false;
    return (try self.command(operation, .{object.id}, exec)).toBool();
}
fn deleteObject(self: *WebGLRenderingContext, value: anytype, comptime operation: []const u8, exec: *Execution) !void {
    const object = value orelse return;
    if (object.owner != self) {
        self._error = 0x0502;
        return;
    }
    if (object.delete_requested) return;
    _ = try self.command(operation, .{object.id}, exec);
    object.delete_requested = true;
}
fn isShader(self: *WebGLRenderingContext, value: ?*Shader, exec: *Execution) !bool {
    return self.isLiveObject(value, .shader, "isShader", exec);
}
fn isProgram(self: *WebGLRenderingContext, value: ?*Program, exec: *Execution) !bool {
    return self.isLiveObject(value, .program, "isProgram", exec);
}
fn isBuffer(self: *WebGLRenderingContext, value: ?*Buffer, exec: *Execution) !bool {
    return self.isLiveObject(value, .buffer, "isBuffer", exec);
}
fn isTexture(self: *WebGLRenderingContext, value: ?*Texture, exec: *Execution) !bool {
    return self.isLiveObject(value, .texture, "isTexture", exec);
}
fn isFramebuffer(self: *WebGLRenderingContext, value: ?*Framebuffer, exec: *Execution) !bool {
    return self.isLiveObject(value, .framebuffer, "isFramebuffer", exec);
}
fn isRenderbuffer(self: *WebGLRenderingContext, value: ?*Renderbuffer, exec: *Execution) !bool {
    return self.isLiveObject(value, .renderbuffer, "isRenderbuffer", exec);
}
fn getCanvas(self: *WebGLRenderingContext) CanvasOwner {
    return self._canvas;
}
fn getDrawingBufferWidth(self: *WebGLRenderingContext) u32 {
    return self._canvas.width();
}
fn getDrawingBufferHeight(self: *WebGLRenderingContext) u32 {
    return self._canvas.height();
}
fn getSupportedExtensions(_: *WebGLRenderingContext) []const []const u8 {
    return &.{"WEBGL_debug_renderer_info"};
}
fn getExtension(_: *WebGLRenderingContext, name: []const u8, exec: *Execution) !?*DebugInfo {
    if (!std.mem.eql(u8, name, "WEBGL_debug_renderer_info")) return null;
    return exec._factory.create(DebugInfo{});
}
fn getParameter(self: *WebGLRenderingContext, pname: u32, exec: *Execution) !js.Value {
    if (pname == 0x9240) return exec.js.local.?.zigValueToJs(self._unpack_flip_y, .{});
    if (pname == 0x9241) return exec.js.local.?.zigValueToJs(self._unpack_premultiply, .{});
    if (pname == 0x9243) return exec.js.local.?.zigValueToJs(self._unpack_colorspace, .{});
    const result = try self.rawCommand("getParameter", .{switch (pname) {
        0x9245 => @as(u32, 0x1F00),
        0x9246 => @as(u32, 0x1F01),
        else => pname,
    }}, exec);
    switch (pname) {
        0x8894, 0x8895, 0x8B8D, 0x8069, 0x8514, 0x8CA7, 0x8CA6 => {
            const kind: Kind = switch (pname) {
                0x8894, 0x8895 => .buffer,
                0x8B8D => .program,
                0x8069, 0x8514 => .texture,
                0x8CA7 => .renderbuffer,
                else => .framebuffer,
            };
            const id = try std.json.parseFromSliceLeaky(u32, exec.local_arena, result, .{});
            const key = (@as(u64, @backingInt(kind)) << 32) | id;
            if (self._objects.get(key)) |object| return exec.js.local.?.zigValueToJs(object, .{});
            return exec.js.local.?.zigValueToJs(@as(?*Buffer, null), .{});
        },
        0x0BA2, 0x0C10, 0x0D3A, 0x86A3 => {
            const values = try std.json.parseFromSliceLeaky([]const i32, exec.local_arena, result, .{});
            const array = exec.js.local.?.createTypedArray(.int32, values.len);
            @memcpy(array.slice(), values);
            return .{ .local = array.local, .handle = array.handle };
        },
        0x846D, 0x846E, 0x0B70, 0x8005, 0x0C22 => {
            const values = try std.json.parseFromSliceLeaky([]const f32, exec.local_arena, result, .{});
            const array = exec.js.local.?.createTypedArray(.float32, values.len);
            @memcpy(array.slice(), values);
            return .{ .local = array.local, .handle = array.handle };
        },
        else => return exec.js.local.?.parseJSON(result),
    }
}
fn readPixels(self: *WebGLRenderingContext, x: i32, y: i32, width: i32, height: i32, format: u32, kind: u32, pixels: js.Value, exec: *Execution) !void {
    if (!pixels.isArrayBufferView()) return error.TypeError;
    _ = exec;
    const target = @constCast(try pixels.toStringSmart());
    const access = try self._surface.beginGlAccess(false);
    defer access.end();
    if (!lp_gl_read_pixels(access.backend, x, y, width, height, format, kind, target.ptr, target.len)) self._error = 0x0502;
}
fn bufferData(self: *WebGLRenderingContext, target: u32, data: js.Value, usage: u32, exec: *Execution) !void {
    if (data.isArrayBuffer() or data.isArrayBufferView()) {
        _ = try self.command("bufferData", .{ target, Bytes{ .data = try data.toStringSmart() }, usage }, exec);
    } else {
        _ = try self.command("bufferData", .{ target, try data.toU32(), usage }, exec);
    }
}
pub extern "c" fn lp_gl_new(u32, u32) ?*Backend;
extern "c" fn lp_gl_free(*Backend) void;
extern "c" fn lp_gl_command(*Backend, [*]const u8, usize, *anyopaque, *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) bool;
pub extern "c" fn lp_gl_readback(*Backend, [*]u8, usize) bool;
pub extern "c" fn lp_gl_finish(*Backend) bool;
pub fn resize(self: *WebGLRenderingContext) !void {
    try self._surface.resize(self._canvas.width(), self._canvas.height());
}
pub extern "c" fn lp_gl_resize(*Backend, u32, u32) bool;
pub fn clearBitmap(backend: *Backend) !void {
    if (!lp_gl_clear_bitmap(backend)) return error.InvalidStateError;
}
extern "c" fn lp_gl_clear_bitmap(*Backend) bool;
fn createShader(self: *WebGLRenderingContext, kind: u32, exec: *Execution) !*Shader {
    const id = try (try self.command("createShader", .{kind}, exec)).toU32();
    return self.remember(try exec._factory.create(Shader{ .id = id, .owner = self }), .shader, exec);
}
fn createProgram(self: *WebGLRenderingContext, exec: *Execution) !*Program {
    const id = try (try self.command("createProgram", .{}, exec)).toU32();
    return self.remember(try exec._factory.create(Program{ .id = id, .owner = self }), .program, exec);
}
fn createBuffer(self: *WebGLRenderingContext, exec: *Execution) !*Buffer {
    const id = try (try self.command("createBuffer", .{}, exec)).toU32();
    return self.remember(try exec._factory.create(Buffer{ .id = id, .owner = self }), .buffer, exec);
}
fn createTexture(self: *WebGLRenderingContext, exec: *Execution) !*Texture {
    const id = try (try self.command("createTexture", .{}, exec)).toU32();
    return self.remember(try exec._factory.create(Texture{ .id = id, .owner = self }), .texture, exec);
}
fn createFramebuffer(self: *WebGLRenderingContext, exec: *Execution) !*Framebuffer {
    const id = try (try self.command("createFramebuffer", .{}, exec)).toU32();
    return self.remember(try exec._factory.create(Framebuffer{ .id = id, .owner = self }), .framebuffer, exec);
}
fn createRenderbuffer(self: *WebGLRenderingContext, exec: *Execution) !*Renderbuffer {
    const id = try (try self.command("createRenderbuffer", .{}, exec)).toU32();
    return self.remember(try exec._factory.create(Renderbuffer{ .id = id, .owner = self }), .renderbuffer, exec);
}
fn shaderSource(self: *WebGLRenderingContext, shader: *Shader, source: []const u8, exec: *Execution) !void {
    _ = try self.command("shaderSource", .{ try self.objectId(@as(?*Shader, shader)), source }, exec);
}
fn compileShader(self: *WebGLRenderingContext, shader: *Shader, exec: *Execution) !void {
    _ = try self.command("compileShader", .{try self.objectId(@as(?*Shader, shader))}, exec);
}
fn attachShader(self: *WebGLRenderingContext, program: *Program, shader: *Shader, exec: *Execution) !void {
    _ = try self.command("attachShader", .{ try self.objectId(@as(?*Program, program)), try self.objectId(@as(?*Shader, shader)) }, exec);
}
fn linkProgram(self: *WebGLRenderingContext, program: *Program, exec: *Execution) !void {
    _ = try self.command("linkProgram", .{try self.objectId(@as(?*Program, program))}, exec);
}
fn useProgram(self: *WebGLRenderingContext, program: ?*Program, exec: *Execution) !void {
    _ = try self.command("useProgram", .{try self.objectId(program)}, exec);
}
fn bindBuffer(self: *WebGLRenderingContext, target: u32, buffer: ?*Buffer, exec: *Execution) !void {
    _ = try self.command("bindBuffer", .{ target, try self.objectId(buffer) }, exec);
}
fn bindTexture(self: *WebGLRenderingContext, target: u32, texture: ?*Texture, exec: *Execution) !void {
    _ = try self.command("bindTexture", .{ target, try self.objectId(texture) }, exec);
}
fn bindFramebuffer(self: *WebGLRenderingContext, target: u32, buffer: ?*Framebuffer, exec: *Execution) !void {
    _ = try self.command("bindFramebuffer", .{ target, try self.objectId(buffer) }, exec);
}
fn bindRenderbuffer(self: *WebGLRenderingContext, target: u32, buffer: ?*Renderbuffer, exec: *Execution) !void {
    _ = try self.command("bindRenderbuffer", .{ target, try self.objectId(buffer) }, exec);
}
fn getShaderParameter(self: *WebGLRenderingContext, shader: *Shader, pname: u32, exec: *Execution) !js.Value {
    return self.command("getShaderParameter", .{ try self.objectId(@as(?*Shader, shader)), pname }, exec);
}
fn getShaderInfoLog(self: *WebGLRenderingContext, shader: *Shader, exec: *Execution) !js.Value {
    return self.command("getShaderInfoLog", .{try self.objectId(@as(?*Shader, shader))}, exec);
}
fn getProgramParameter(self: *WebGLRenderingContext, program: *Program, pname: u32, exec: *Execution) !js.Value {
    return self.command("getProgramParameter", .{ try self.objectId(@as(?*Program, program)), pname }, exec);
}
fn getProgramInfoLog(self: *WebGLRenderingContext, program: *Program, exec: *Execution) !js.Value {
    return self.command("getProgramInfoLog", .{try self.objectId(@as(?*Program, program))}, exec);
}
fn getAttribLocation(self: *WebGLRenderingContext, program: *Program, name: []const u8, exec: *Execution) !js.Value {
    return self.command("getAttribLocation", .{ try self.objectId(@as(?*Program, program)), name }, exec);
}
fn getUniformLocation(self: *WebGLRenderingContext, program: *Program, name: []const u8, exec: *Execution) !?*Uniform {
    const value = try self.command("getUniformLocation", .{ try self.objectId(@as(?*Program, program)), name }, exec);
    if (value.isNull()) return null;
    return exec._factory.create(Uniform{ .id = try value.toU32(), .owner = self });
}
fn clearColor(self: *WebGLRenderingContext, a0: f64, a1: f64, a2: f64, a3: f64, exec: *Execution) !void {
    _ = try self.command("clearColor", .{ a0, a1, a2, a3 }, exec);
}
fn clear(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("clear", .{a0}, exec);
}
fn clearDepth(self: *WebGLRenderingContext, a0: f64, exec: *Execution) !void {
    _ = try self.command("clearDepth", .{a0}, exec);
}
fn clearStencil(self: *WebGLRenderingContext, a0: i32, exec: *Execution) !void {
    _ = try self.command("clearStencil", .{a0}, exec);
}
fn viewport(self: *WebGLRenderingContext, a0: i32, a1: i32, a2: i32, a3: i32, exec: *Execution) !void {
    _ = try self.command("viewport", .{ a0, a1, a2, a3 }, exec);
}
fn scissor(self: *WebGLRenderingContext, a0: i32, a1: i32, a2: i32, a3: i32, exec: *Execution) !void {
    _ = try self.command("scissor", .{ a0, a1, a2, a3 }, exec);
}
fn enable(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("enable", .{a0}, exec);
}
fn disable(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("disable", .{a0}, exec);
}
fn drawArrays(self: *WebGLRenderingContext, a0: u32, a1: i32, a2: i32, exec: *Execution) !void {
    _ = try self.command("drawArrays", .{ a0, a1, a2 }, exec);
}
fn drawElements(self: *WebGLRenderingContext, a0: u32, a1: i32, a2: u32, a3: i32, exec: *Execution) !void {
    _ = try self.command("drawElements", .{ a0, a1, a2, a3 }, exec);
}
fn vertexAttribPointer(self: *WebGLRenderingContext, a0: u32, a1: i32, a2: u32, a3: bool, a4: i32, a5: i32, exec: *Execution) !void {
    _ = try self.command("vertexAttribPointer", .{ a0, a1, a2, a3, a4, a5 }, exec);
}
fn enableVertexAttribArray(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("enableVertexAttribArray", .{a0}, exec);
}
fn disableVertexAttribArray(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("disableVertexAttribArray", .{a0}, exec);
}
fn finish(self: *WebGLRenderingContext, exec: *Execution) !void {
    _ = try self.command("finish", .{}, exec);
}
fn flush(self: *WebGLRenderingContext, exec: *Execution) !void {
    _ = try self.command("flush", .{}, exec);
}
fn blendFunc(self: *WebGLRenderingContext, a0: u32, a1: u32, exec: *Execution) !void {
    _ = try self.command("blendFunc", .{ a0, a1 }, exec);
}
fn blendEquation(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("blendEquation", .{a0}, exec);
}
fn activeTexture(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("activeTexture", .{a0}, exec);
}
fn texParameteri(self: *WebGLRenderingContext, a0: u32, a1: u32, a2: i32, exec: *Execution) !void {
    _ = try self.command("texParameteri", .{ a0, a1, a2 }, exec);
}
fn pixelStorei(self: *WebGLRenderingContext, pname: u32, value: i32, exec: *Execution) !void {
    switch (pname) {
        0x9240 => self._unpack_flip_y = value != 0,
        0x9241 => self._unpack_premultiply = value != 0,
        0x9243 => {
            if (value != 0 and value != 0x9244) {
                self._error = 0x0501;
                return;
            }
            self._unpack_colorspace = @intCast(value);
        },
        0x0CF5 => {
            if (value != 1 and value != 2 and value != 4 and value != 8) {
                self._error = 0x0501;
                return;
            }
            self._unpack_alignment = @intCast(value);
            _ = try self.command("pixelStorei", .{ pname, value }, exec);
        },
        else => _ = try self.command("pixelStorei", .{ pname, value }, exec),
    }
}
fn depthFunc(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("depthFunc", .{a0}, exec);
}
fn depthMask(self: *WebGLRenderingContext, a0: bool, exec: *Execution) !void {
    _ = try self.command("depthMask", .{a0}, exec);
}
fn colorMask(self: *WebGLRenderingContext, a0: bool, a1: bool, a2: bool, a3: bool, exec: *Execution) !void {
    _ = try self.command("colorMask", .{ a0, a1, a2, a3 }, exec);
}
fn frontFace(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("frontFace", .{a0}, exec);
}
fn cullFace(self: *WebGLRenderingContext, a0: u32, exec: *Execution) !void {
    _ = try self.command("cullFace", .{a0}, exec);
}
fn lineWidth(self: *WebGLRenderingContext, a0: f64, exec: *Execution) !void {
    _ = try self.command("lineWidth", .{a0}, exec);
}
fn polygonOffset(self: *WebGLRenderingContext, a0: f64, a1: f64, exec: *Execution) !void {
    _ = try self.command("polygonOffset", .{ a0, a1 }, exec);
}
fn uniform1f(self: *WebGLRenderingContext, location: ?*Uniform, a0: f64, exec: *Execution) !void {
    _ = try self.command("uniform1f", .{ try self.objectId(location), a0 }, exec);
}
fn uniform2f(self: *WebGLRenderingContext, location: ?*Uniform, a0: f64, a1: f64, exec: *Execution) !void {
    _ = try self.command("uniform2f", .{ try self.objectId(location), a0, a1 }, exec);
}
fn uniform3f(self: *WebGLRenderingContext, location: ?*Uniform, a0: f64, a1: f64, a2: f64, exec: *Execution) !void {
    _ = try self.command("uniform3f", .{ try self.objectId(location), a0, a1, a2 }, exec);
}
fn uniform4f(self: *WebGLRenderingContext, location: ?*Uniform, a0: f64, a1: f64, a2: f64, a3: f64, exec: *Execution) !void {
    _ = try self.command("uniform4f", .{ try self.objectId(location), a0, a1, a2, a3 }, exec);
}
fn getError(self: *WebGLRenderingContext, exec: *Execution) !u32 {
    if (self._error != 0) {
        const error_code = self._error;
        self._error = 0;
        return error_code;
    }
    return (try self.command("getError", .{}, exec)).toU32();
}
const FloatList = union(enum) {
    typed: js.TypedArray(f32),
    list: []const f32,
    fn values(self: FloatList) []const f32 {
        return switch (self) {
            .typed => |v| v.values,
            .list => |v| v,
        };
    }
};
const IntList = union(enum) {
    typed: js.TypedArray(i32),
    list: []const i32,
    fn values(self: IntList) []const i32 {
        return switch (self) {
            .typed => |v| v.values,
            .list => |v| v,
        };
    }
};
fn getShaderPrecisionFormat(self: *WebGLRenderingContext, shader: u32, precision: u32, exec: *Execution) !?*ShaderPrecision {
    const value = try self.command("getShaderPrecisionFormat", .{ shader, precision }, exec);
    if (value.isNull()) return null;
    return exec._factory.create(ShaderPrecision{ .value = try std.json.parseFromSliceLeaky(PrecisionValues, exec.local_arena, try value.toJson(exec.local_arena), .{}) });
}
const TextureParameters = extern struct { target: u32, level: i32, internal: i32, width: i32, height: i32, border: i32, format: u32, kind: u32 };
fn texImage2D(self: *WebGLRenderingContext, target: u32, level: i32, internal: i32, width_or_format: u32, height_or_kind: u32, border_or_source: js.Value, format: ?u32, kind: ?u32, pixels: ?js.Value, exec: *Execution) !void {
    var parameters: TextureParameters = undefined;
    var data: ?[]const u8 = null;
    if (format != null and kind != null) {
        parameters = .{ .target = target, .level = level, .internal = internal, .width = @bitCast(width_or_format), .height = @bitCast(height_or_kind), .border = try border_or_source.toI32(), .format = format.?, .kind = kind.? };
        if (pixels) |value| if (!value.isNull() and !value.isUndefined()) {
            if (!value.isArrayBufferView()) {
                self._error = 0x0502;
                return;
            }
            data = try value.toStringSmart();
        };
    } else {
        const image = try @import("context2d.zig").pixelSource(border_or_source, exec);
        if (!image.origin_clean) return error.SecurityError;
        parameters = .{ .target = target, .level = level, .internal = internal, .width = @intCast(image.width), .height = @intCast(image.height), .border = 0, .format = width_or_format, .kind = height_or_kind };
        data = self.convertImage(image.width, image.height, parameters.format, parameters.kind, image.bytes, exec.local_arena) catch {
            self._error = 0x0502;
            return;
        };
    }
    if (data) |bytes| data = self.unpack(parameters.width, parameters.height, parameters.format, parameters.kind, bytes, exec.local_arena) catch {
        self._error = 0x0502;
        return;
    };
    const access = try self._surface.beginGlAccess(true);
    defer access.end();
    if (!lp_gl_tex_image(access.backend, &parameters, if (data) |v| v.ptr else null, if (data) |v| v.len else 0)) self._error = 0x0502;
}
fn texSubImage2D(self: *WebGLRenderingContext, target: u32, level: i32, x: i32, y: i32, width_or_format: u32, height_or_kind: u32, format_or_source: js.Value, kind: ?u32, pixels: ?js.Value, exec: *Execution) !void {
    var width: i32 = undefined;
    var height: i32 = undefined;
    var format: u32 = undefined;
    var pixel_kind: u32 = undefined;
    var data: []const u8 = undefined;
    if (kind) |pixel_type| {
        width = @bitCast(width_or_format);
        height = @bitCast(height_or_kind);
        format = try format_or_source.toU32();
        pixel_kind = pixel_type;
        const value = pixels orelse return error.TypeError;
        if (!value.isArrayBufferView()) {
            self._error = 0x0502;
            return;
        }
        data = try value.toStringSmart();
    } else {
        const image = try @import("context2d.zig").pixelSource(format_or_source, exec);
        if (!image.origin_clean) return error.SecurityError;
        width = @intCast(image.width);
        height = @intCast(image.height);
        format = width_or_format;
        pixel_kind = height_or_kind;
        data = self.convertImage(image.width, image.height, format, pixel_kind, image.bytes, exec.local_arena) catch {
            self._error = 0x0502;
            return;
        };
    }
    data = self.unpack(width, height, format, pixel_kind, data, exec.local_arena) catch {
        self._error = 0x0502;
        return;
    };
    const access = try self._surface.beginGlAccess(true);
    defer access.end();
    if (!lp_gl_tex_sub_image(access.backend, target, level, x, y, width, height, format, pixel_kind, data.ptr, data.len)) self._error = 0x0502;
}
extern "c" fn lp_gl_tex_image(*Backend, *const TextureParameters, ?[*]const u8, usize) bool;
extern "c" fn lp_gl_tex_sub_image(*Backend, u32, i32, i32, i32, i32, i32, u32, u32, [*]const u8, usize) bool;
extern "c" fn lp_gl_read_pixels(*Backend, i32, i32, i32, i32, u32, u32, [*]u8, usize) bool;
fn bufferSubData(self: *WebGLRenderingContext, target: u32, offset: i32, data: js.Value, exec: *Execution) !void {
    if (!data.isArrayBufferView() and !data.isArrayBuffer()) {
        self._error = 0x0502;
        return;
    }
    _ = try self.command("bufferSubData", .{ target, offset, Bytes{ .data = try data.toStringSmart() } }, exec);
}
fn uniform1fv(self: *WebGLRenderingContext, location: ?*Uniform, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 1 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform1fv", .{ try self.objectId(location), values }, exec);
}
fn uniform1iv(self: *WebGLRenderingContext, location: ?*Uniform, data: IntList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 1 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform1iv", .{ try self.objectId(location), values }, exec);
}
fn uniform1i(self: *WebGLRenderingContext, location: ?*Uniform, a0: i32, exec: *Execution) !void {
    if (location == null) return;
    _ = try self.command("uniform1i", .{ try self.objectId(location), a0 }, exec);
}
fn uniform2fv(self: *WebGLRenderingContext, location: ?*Uniform, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 2 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform2fv", .{ try self.objectId(location), values }, exec);
}
fn uniform2iv(self: *WebGLRenderingContext, location: ?*Uniform, data: IntList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 2 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform2iv", .{ try self.objectId(location), values }, exec);
}
fn uniform2i(self: *WebGLRenderingContext, location: ?*Uniform, a0: i32, a1: i32, exec: *Execution) !void {
    if (location == null) return;
    _ = try self.command("uniform2i", .{ try self.objectId(location), a0, a1 }, exec);
}
fn uniform3fv(self: *WebGLRenderingContext, location: ?*Uniform, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 3 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform3fv", .{ try self.objectId(location), values }, exec);
}
fn uniform3iv(self: *WebGLRenderingContext, location: ?*Uniform, data: IntList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 3 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform3iv", .{ try self.objectId(location), values }, exec);
}
fn uniform3i(self: *WebGLRenderingContext, location: ?*Uniform, a0: i32, a1: i32, a2: i32, exec: *Execution) !void {
    if (location == null) return;
    _ = try self.command("uniform3i", .{ try self.objectId(location), a0, a1, a2 }, exec);
}
fn uniform4fv(self: *WebGLRenderingContext, location: ?*Uniform, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 4 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform4fv", .{ try self.objectId(location), values }, exec);
}
fn uniform4iv(self: *WebGLRenderingContext, location: ?*Uniform, data: IntList, exec: *Execution) !void {
    if (location == null) return;
    const values = data.values();
    if (values.len % 4 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniform4iv", .{ try self.objectId(location), values }, exec);
}
fn uniform4i(self: *WebGLRenderingContext, location: ?*Uniform, a0: i32, a1: i32, a2: i32, a3: i32, exec: *Execution) !void {
    if (location == null) return;
    _ = try self.command("uniform4i", .{ try self.objectId(location), a0, a1, a2, a3 }, exec);
}
fn uniformMatrix2fv(self: *WebGLRenderingContext, location: ?*Uniform, transpose: bool, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    if (transpose or data.values().len % 4 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniformMatrix2fv", .{ try self.objectId(location), false, data.values() }, exec);
}
fn uniformMatrix3fv(self: *WebGLRenderingContext, location: ?*Uniform, transpose: bool, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    if (transpose or data.values().len % 9 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniformMatrix3fv", .{ try self.objectId(location), false, data.values() }, exec);
}
fn uniformMatrix4fv(self: *WebGLRenderingContext, location: ?*Uniform, transpose: bool, data: FloatList, exec: *Execution) !void {
    if (location == null) return;
    if (transpose or data.values().len % 16 != 0) {
        self._error = 0x0501;
        return;
    }
    _ = try self.command("uniformMatrix4fv", .{ try self.objectId(location), false, data.values() }, exec);
}
fn deleteShader(self: *WebGLRenderingContext, value: ?*Shader, exec: *Execution) !void {
    return self.deleteObject(value, "deleteShader", exec);
}
fn deleteProgram(self: *WebGLRenderingContext, value: ?*Program, exec: *Execution) !void {
    return self.deleteObject(value, "deleteProgram", exec);
}
fn deleteBuffer(self: *WebGLRenderingContext, value: ?*Buffer, exec: *Execution) !void {
    return self.deleteObject(value, "deleteBuffer", exec);
}
fn deleteTexture(self: *WebGLRenderingContext, value: ?*Texture, exec: *Execution) !void {
    return self.deleteObject(value, "deleteTexture", exec);
}
fn deleteFramebuffer(self: *WebGLRenderingContext, value: ?*Framebuffer, exec: *Execution) !void {
    return self.deleteObject(value, "deleteFramebuffer", exec);
}
fn deleteRenderbuffer(self: *WebGLRenderingContext, value: ?*Renderbuffer, exec: *Execution) !void {
    return self.deleteObject(value, "deleteRenderbuffer", exec);
}
fn generateMipmap(self: *WebGLRenderingContext, target: u32, exec: *Execution) !void {
    _ = try self.command("generateMipmap", .{target}, exec);
}
fn checkFramebufferStatus(self: *WebGLRenderingContext, target: u32, exec: *Execution) !js.Value {
    return self.command("checkFramebufferStatus", .{target}, exec);
}
fn framebufferTexture2D(self: *WebGLRenderingContext, target: u32, attachment: u32, texture_target: u32, texture: ?*Texture, level: i32, exec: *Execution) !void {
    _ = try self.command("framebufferTexture2D", .{ target, attachment, texture_target, try self.objectId(texture), level }, exec);
}
fn framebufferRenderbuffer(self: *WebGLRenderingContext, target: u32, attachment: u32, renderbuffer_target: u32, renderbuffer: ?*Renderbuffer, exec: *Execution) !void {
    _ = try self.command("framebufferRenderbuffer", .{ target, attachment, renderbuffer_target, try self.objectId(renderbuffer) }, exec);
}
fn renderbufferStorage(self: *WebGLRenderingContext, target: u32, format: u32, width: i32, height: i32, exec: *Execution) !void {
    _ = try self.command("renderbufferStorage", .{ target, format, width, height }, exec);
}
fn convertImage(self: *WebGLRenderingContext, width: u32, height: u32, format: u32, kind: u32, input: []const u8, arena: std.mem.Allocator) ![]const u8 {
    const pixels = try std.math.mul(usize, width, height);
    if (input.len != try std.math.mul(usize, pixels, 4)) return error.InvalidOperationError;
    const channels: usize = switch (format) {
        0x1908 => 4,
        0x1907 => 3,
        0x190A => 2,
        0x1909, 0x1906 => 1,
        else => return error.InvalidOperationError,
    };
    const packed_pixels = kind == 0x8033 or kind == 0x8034 or kind == 0x8363;
    if (kind != 0x1401 and !packed_pixels) return error.InvalidOperationError;
    if ((kind == 0x8363 and format != 0x1907) or ((kind == 0x8033 or kind == 0x8034) and format != 0x1908)) return error.InvalidOperationError;
    const row = try std.math.mul(usize, width, if (packed_pixels) @as(usize, 2) else channels);
    const stride = try std.math.add(usize, row, self._unpack_alignment - 1) / self._unpack_alignment * self._unpack_alignment;
    if (format == 0x1908 and kind == 0x1401 and stride == row) return input;
    const size = if (height == 0) 0 else try std.math.add(usize, try std.math.mul(usize, stride, height - 1), row);
    const output = try arena.alloc(u8, size);
    @memset(output, 0);
    for (0..height) |y| for (0..width) |x| {
        const rgba = input[(y * width + x) * 4 ..][0..4];
        const offset = y * stride + x * (if (packed_pixels) @as(usize, 2) else channels);
        if (packed_pixels) {
            const value: u16 = switch (kind) {
                0x8363 => (@as(u16, rgba[0] >> 3) << 11) | (@as(u16, rgba[1] >> 2) << 5) | @as(u16, rgba[2] >> 3),
                0x8033 => (@as(u16, rgba[0] >> 4) << 12) | (@as(u16, rgba[1] >> 4) << 8) | (@as(u16, rgba[2] >> 4) << 4) | @as(u16, rgba[3] >> 4),
                else => (@as(u16, rgba[0] >> 3) << 11) | (@as(u16, rgba[1] >> 3) << 6) | (@as(u16, rgba[2] >> 3) << 1) | @as(u16, rgba[3] >> 7),
            };
            std.mem.writeInt(u16, output[offset..][0..2], value, @import("builtin").cpu.arch.endian());
        } else switch (format) {
            0x1908 => @memcpy(output[offset..][0..4], rgba),
            0x1907 => @memcpy(output[offset..][0..3], rgba[0..3]),
            0x1906 => output[offset] = rgba[3],
            0x190A => {
                output[offset] = rgba[0];
                output[offset + 1] = rgba[3];
            },
            else => output[offset] = rgba[0],
        }
    };
    return output;
}
fn unpack(self: *WebGLRenderingContext, width: i32, height: i32, format: u32, kind: u32, data: []const u8, arena: std.mem.Allocator) ![]const u8 {
    if (!self._unpack_flip_y and !self._unpack_premultiply) return data;
    if (width < 0 or height < 0) return error.InvalidOperationError;
    const channels: usize = switch (format) {
        0x1908 => 4,
        0x1907 => 3,
        0x190A => 2,
        0x1909, 0x1906 => 1,
        else => return error.InvalidOperationError,
    };
    const bytes_per_pixel: usize = switch (kind) {
        0x1401 => channels,
        0x8033, 0x8034, 0x8363 => 2,
        else => return error.InvalidOperationError,
    };
    const row = try std.math.mul(usize, @intCast(width), bytes_per_pixel);
    const stride = try std.math.add(usize, row, self._unpack_alignment - 1) / self._unpack_alignment * self._unpack_alignment;
    const rows: usize = @intCast(height);
    const required = if (rows == 0) 0 else try std.math.add(usize, try std.math.mul(usize, stride, rows - 1), row);
    if (data.len < required) return error.InvalidOperationError;
    const out = try arena.alloc(u8, required);
    @memset(out, 0);
    for (0..rows) |y| {
        const source_y = if (self._unpack_flip_y) rows - 1 - y else y;
        @memcpy(out[y * stride ..][0..row], data[source_y * stride ..][0..row]);
        if (self._unpack_premultiply and (format == 0x1908 or format == 0x190A)) {
            if (kind != 0x1401) return error.InvalidOperationError;
            for (0..@as(usize, @intCast(width))) |x| {
                const pixel = out[y * stride + x * channels ..][0..channels];
                const alpha = pixel[channels - 1];
                for (0..channels - 1) |c| pixel[c] = @intCast((@as(u16, pixel[c]) * alpha + 127) / 255);
            }
        }
    }
    return out;
}
pub const JsApi = struct {
    pub const bridge = js.Bridge(WebGLRenderingContext);
    pub const Meta = struct {
        pub const name = "WebGLRenderingContext";
        pub var class_id: bridge.ClassId = undefined;
    };
    pub const canvas = bridge.accessor(getCanvas, null, .{});
    pub const drawingBufferWidth = bridge.accessor(getDrawingBufferWidth, null, .{});
    pub const drawingBufferHeight = bridge.accessor(getDrawingBufferHeight, null, .{});
    pub const getShaderPrecisionFormat = bridge.function(WebGLRenderingContext.getShaderPrecisionFormat, .{});
    pub const texImage2D = bridge.function(WebGLRenderingContext.texImage2D, .{});
    pub const texSubImage2D = bridge.function(WebGLRenderingContext.texSubImage2D, .{});
    pub const bufferSubData = bridge.function(WebGLRenderingContext.bufferSubData, .{});
    pub const uniform1fv = bridge.function(WebGLRenderingContext.uniform1fv, .{});
    pub const uniform1iv = bridge.function(WebGLRenderingContext.uniform1iv, .{});
    pub const uniform1i = bridge.function(WebGLRenderingContext.uniform1i, .{});
    pub const uniform2fv = bridge.function(WebGLRenderingContext.uniform2fv, .{});
    pub const uniform2iv = bridge.function(WebGLRenderingContext.uniform2iv, .{});
    pub const uniform2i = bridge.function(WebGLRenderingContext.uniform2i, .{});
    pub const uniform3fv = bridge.function(WebGLRenderingContext.uniform3fv, .{});
    pub const uniform3iv = bridge.function(WebGLRenderingContext.uniform3iv, .{});
    pub const uniform3i = bridge.function(WebGLRenderingContext.uniform3i, .{});
    pub const uniform4fv = bridge.function(WebGLRenderingContext.uniform4fv, .{});
    pub const uniform4iv = bridge.function(WebGLRenderingContext.uniform4iv, .{});
    pub const uniform4i = bridge.function(WebGLRenderingContext.uniform4i, .{});
    pub const uniformMatrix2fv = bridge.function(WebGLRenderingContext.uniformMatrix2fv, .{});
    pub const uniformMatrix3fv = bridge.function(WebGLRenderingContext.uniformMatrix3fv, .{});
    pub const uniformMatrix4fv = bridge.function(WebGLRenderingContext.uniformMatrix4fv, .{});
    pub const deleteShader = bridge.function(WebGLRenderingContext.deleteShader, .{});
    pub const deleteProgram = bridge.function(WebGLRenderingContext.deleteProgram, .{});
    pub const deleteBuffer = bridge.function(WebGLRenderingContext.deleteBuffer, .{});
    pub const deleteTexture = bridge.function(WebGLRenderingContext.deleteTexture, .{});
    pub const deleteFramebuffer = bridge.function(WebGLRenderingContext.deleteFramebuffer, .{});
    pub const deleteRenderbuffer = bridge.function(WebGLRenderingContext.deleteRenderbuffer, .{});
    pub const isShader = bridge.function(WebGLRenderingContext.isShader, .{});
    pub const isProgram = bridge.function(WebGLRenderingContext.isProgram, .{});
    pub const isBuffer = bridge.function(WebGLRenderingContext.isBuffer, .{});
    pub const isTexture = bridge.function(WebGLRenderingContext.isTexture, .{});
    pub const isFramebuffer = bridge.function(WebGLRenderingContext.isFramebuffer, .{});
    pub const isRenderbuffer = bridge.function(WebGLRenderingContext.isRenderbuffer, .{});
    pub const generateMipmap = bridge.function(WebGLRenderingContext.generateMipmap, .{});
    pub const checkFramebufferStatus = bridge.function(WebGLRenderingContext.checkFramebufferStatus, .{});
    pub const framebufferTexture2D = bridge.function(WebGLRenderingContext.framebufferTexture2D, .{});
    pub const framebufferRenderbuffer = bridge.function(WebGLRenderingContext.framebufferRenderbuffer, .{});
    pub const renderbufferStorage = bridge.function(WebGLRenderingContext.renderbufferStorage, .{});
    pub const LOW_FLOAT = bridge.property(@as(u32, 36336), .{ .template = true });
    pub const MEDIUM_FLOAT = bridge.property(@as(u32, 36337), .{ .template = true });
    pub const HIGH_FLOAT = bridge.property(@as(u32, 36338), .{ .template = true });
    pub const LOW_INT = bridge.property(@as(u32, 36339), .{ .template = true });
    pub const MEDIUM_INT = bridge.property(@as(u32, 36340), .{ .template = true });
    pub const HIGH_INT = bridge.property(@as(u32, 36341), .{ .template = true });
    pub const INVALID_ENUM = bridge.property(@as(u32, 1280), .{ .template = true });
    pub const INVALID_VALUE = bridge.property(@as(u32, 1281), .{ .template = true });
    pub const INVALID_OPERATION = bridge.property(@as(u32, 1282), .{ .template = true });
    pub const UNSIGNED_SHORT_4_4_4_4 = bridge.property(@as(u32, 32819), .{ .template = true });
    pub const UNSIGNED_SHORT_5_5_5_1 = bridge.property(@as(u32, 32820), .{ .template = true });
    pub const UNSIGNED_SHORT_5_6_5 = bridge.property(@as(u32, 33635), .{ .template = true });
    pub const TEXTURE_WRAP_S = bridge.property(@as(u32, 10242), .{ .template = true });
    pub const TEXTURE_WRAP_T = bridge.property(@as(u32, 10243), .{ .template = true });
    pub const CLAMP_TO_EDGE = bridge.property(@as(u32, 33071), .{ .template = true });
    pub const REPEAT = bridge.property(@as(u32, 10497), .{ .template = true });
    pub const MIRRORED_REPEAT = bridge.property(@as(u32, 33648), .{ .template = true });
    pub const COLOR_ATTACHMENT0 = bridge.property(@as(u32, 36064), .{ .template = true });
    pub const DEPTH_ATTACHMENT = bridge.property(@as(u32, 36096), .{ .template = true });
    pub const STENCIL_ATTACHMENT = bridge.property(@as(u32, 36128), .{ .template = true });
    pub const DEPTH_COMPONENT16 = bridge.property(@as(u32, 33189), .{ .template = true });
    pub const RGBA4 = bridge.property(@as(u32, 32854), .{ .template = true });
    pub const RGB565 = bridge.property(@as(u32, 36194), .{ .template = true });
    pub const RGB5_A1 = bridge.property(@as(u32, 32855), .{ .template = true });
    pub const STENCIL_INDEX8 = bridge.property(@as(u32, 36168), .{ .template = true });
    pub const DEPTH_STENCIL = bridge.property(@as(u32, 34041), .{ .template = true });
    pub const UNPACK_FLIP_Y_WEBGL = bridge.property(@as(u32, 37440), .{ .template = true });
    pub const UNPACK_PREMULTIPLY_ALPHA_WEBGL = bridge.property(@as(u32, 37441), .{ .template = true });
    pub const UNPACK_COLORSPACE_CONVERSION_WEBGL = bridge.property(@as(u32, 37443), .{ .template = true });
    pub const BROWSER_DEFAULT_WEBGL = bridge.property(@as(u32, 37444), .{ .template = true });
    pub const getParameter = bridge.function(WebGLRenderingContext.getParameter, .{});
    pub const getSupportedExtensions = bridge.function(WebGLRenderingContext.getSupportedExtensions, .{});
    pub const getExtension = bridge.function(WebGLRenderingContext.getExtension, .{});
    pub const readPixels = bridge.function(WebGLRenderingContext.readPixels, .{});
    pub const bufferData = bridge.function(WebGLRenderingContext.bufferData, .{});
    pub const createShader = bridge.function(WebGLRenderingContext.createShader, .{});
    pub const createProgram = bridge.function(WebGLRenderingContext.createProgram, .{});
    pub const createBuffer = bridge.function(WebGLRenderingContext.createBuffer, .{});
    pub const createTexture = bridge.function(WebGLRenderingContext.createTexture, .{});
    pub const createFramebuffer = bridge.function(WebGLRenderingContext.createFramebuffer, .{});
    pub const createRenderbuffer = bridge.function(WebGLRenderingContext.createRenderbuffer, .{});
    pub const shaderSource = bridge.function(WebGLRenderingContext.shaderSource, .{});
    pub const compileShader = bridge.function(WebGLRenderingContext.compileShader, .{});
    pub const attachShader = bridge.function(WebGLRenderingContext.attachShader, .{});
    pub const linkProgram = bridge.function(WebGLRenderingContext.linkProgram, .{});
    pub const useProgram = bridge.function(WebGLRenderingContext.useProgram, .{});
    pub const bindBuffer = bridge.function(WebGLRenderingContext.bindBuffer, .{});
    pub const bindTexture = bridge.function(WebGLRenderingContext.bindTexture, .{});
    pub const bindFramebuffer = bridge.function(WebGLRenderingContext.bindFramebuffer, .{});
    pub const bindRenderbuffer = bridge.function(WebGLRenderingContext.bindRenderbuffer, .{});
    pub const getShaderParameter = bridge.function(WebGLRenderingContext.getShaderParameter, .{});
    pub const getShaderInfoLog = bridge.function(WebGLRenderingContext.getShaderInfoLog, .{});
    pub const getProgramParameter = bridge.function(WebGLRenderingContext.getProgramParameter, .{});
    pub const getProgramInfoLog = bridge.function(WebGLRenderingContext.getProgramInfoLog, .{});
    pub const getAttribLocation = bridge.function(WebGLRenderingContext.getAttribLocation, .{});
    pub const getUniformLocation = bridge.function(WebGLRenderingContext.getUniformLocation, .{});
    pub const clearColor = bridge.function(WebGLRenderingContext.clearColor, .{});
    pub const clear = bridge.function(WebGLRenderingContext.clear, .{});
    pub const clearDepth = bridge.function(WebGLRenderingContext.clearDepth, .{});
    pub const clearStencil = bridge.function(WebGLRenderingContext.clearStencil, .{});
    pub const viewport = bridge.function(WebGLRenderingContext.viewport, .{});
    pub const scissor = bridge.function(WebGLRenderingContext.scissor, .{});
    pub const enable = bridge.function(WebGLRenderingContext.enable, .{});
    pub const disable = bridge.function(WebGLRenderingContext.disable, .{});
    pub const drawArrays = bridge.function(WebGLRenderingContext.drawArrays, .{});
    pub const drawElements = bridge.function(WebGLRenderingContext.drawElements, .{});
    pub const vertexAttribPointer = bridge.function(WebGLRenderingContext.vertexAttribPointer, .{});
    pub const enableVertexAttribArray = bridge.function(WebGLRenderingContext.enableVertexAttribArray, .{});
    pub const disableVertexAttribArray = bridge.function(WebGLRenderingContext.disableVertexAttribArray, .{});
    pub const finish = bridge.function(WebGLRenderingContext.finish, .{});
    pub const flush = bridge.function(WebGLRenderingContext.flush, .{});
    pub const blendFunc = bridge.function(WebGLRenderingContext.blendFunc, .{});
    pub const blendEquation = bridge.function(WebGLRenderingContext.blendEquation, .{});
    pub const activeTexture = bridge.function(WebGLRenderingContext.activeTexture, .{});
    pub const texParameteri = bridge.function(WebGLRenderingContext.texParameteri, .{});
    pub const pixelStorei = bridge.function(WebGLRenderingContext.pixelStorei, .{});
    pub const depthFunc = bridge.function(WebGLRenderingContext.depthFunc, .{});
    pub const depthMask = bridge.function(WebGLRenderingContext.depthMask, .{});
    pub const colorMask = bridge.function(WebGLRenderingContext.colorMask, .{});
    pub const frontFace = bridge.function(WebGLRenderingContext.frontFace, .{});
    pub const cullFace = bridge.function(WebGLRenderingContext.cullFace, .{});
    pub const lineWidth = bridge.function(WebGLRenderingContext.lineWidth, .{});
    pub const polygonOffset = bridge.function(WebGLRenderingContext.polygonOffset, .{});
    pub const uniform1f = bridge.function(WebGLRenderingContext.uniform1f, .{});
    pub const uniform2f = bridge.function(WebGLRenderingContext.uniform2f, .{});
    pub const uniform3f = bridge.function(WebGLRenderingContext.uniform3f, .{});
    pub const uniform4f = bridge.function(WebGLRenderingContext.uniform4f, .{});
    pub const getError = bridge.function(WebGLRenderingContext.getError, .{});
    pub const COLOR_BUFFER_BIT = bridge.property(@as(u32, 16384), .{ .template = true });
    pub const DEPTH_BUFFER_BIT = bridge.property(@as(u32, 256), .{ .template = true });
    pub const STENCIL_BUFFER_BIT = bridge.property(@as(u32, 1024), .{ .template = true });
    pub const VERTEX_SHADER = bridge.property(@as(u32, 35633), .{ .template = true });
    pub const FRAGMENT_SHADER = bridge.property(@as(u32, 35632), .{ .template = true });
    pub const COMPILE_STATUS = bridge.property(@as(u32, 35713), .{ .template = true });
    pub const LINK_STATUS = bridge.property(@as(u32, 35714), .{ .template = true });
    pub const VALIDATE_STATUS = bridge.property(@as(u32, 35715), .{ .template = true });
    pub const ARRAY_BUFFER = bridge.property(@as(u32, 34962), .{ .template = true });
    pub const ELEMENT_ARRAY_BUFFER = bridge.property(@as(u32, 34963), .{ .template = true });
    pub const STATIC_DRAW = bridge.property(@as(u32, 35044), .{ .template = true });
    pub const DYNAMIC_DRAW = bridge.property(@as(u32, 35048), .{ .template = true });
    pub const FLOAT = bridge.property(@as(u32, 5126), .{ .template = true });
    pub const UNSIGNED_BYTE = bridge.property(@as(u32, 5121), .{ .template = true });
    pub const UNSIGNED_SHORT = bridge.property(@as(u32, 5123), .{ .template = true });
    pub const RGBA = bridge.property(@as(u32, 6408), .{ .template = true });
    pub const RGB = bridge.property(@as(u32, 6407), .{ .template = true });
    pub const TRIANGLES = bridge.property(@as(u32, 4), .{ .template = true });
    pub const TRIANGLE_STRIP = bridge.property(@as(u32, 5), .{ .template = true });
    pub const TRIANGLE_FAN = bridge.property(@as(u32, 6), .{ .template = true });
    pub const POINTS = bridge.property(@as(u32, 0), .{ .template = true });
    pub const LINES = bridge.property(@as(u32, 1), .{ .template = true });
    pub const VENDOR = bridge.property(@as(u32, 7936), .{ .template = true });
    pub const RENDERER = bridge.property(@as(u32, 7937), .{ .template = true });
    pub const VERSION = bridge.property(@as(u32, 7938), .{ .template = true });
    pub const SHADING_LANGUAGE_VERSION = bridge.property(@as(u32, 35724), .{ .template = true });
    pub const MAX_TEXTURE_SIZE = bridge.property(@as(u32, 3379), .{ .template = true });
    pub const MAX_VERTEX_ATTRIBS = bridge.property(@as(u32, 34921), .{ .template = true });
    pub const SUBPIXEL_BITS = bridge.property(@as(u32, 0x0D50), .{ .template = true });
    pub const VIEWPORT = bridge.property(@as(u32, 2978), .{ .template = true });
    pub const SCISSOR_BOX = bridge.property(@as(u32, 0x0C10), .{ .template = true });
    pub const MAX_VIEWPORT_DIMS = bridge.property(@as(u32, 0x0D3A), .{ .template = true });
    pub const ALIASED_POINT_SIZE_RANGE = bridge.property(@as(u32, 0x846D), .{ .template = true });
    pub const ALIASED_LINE_WIDTH_RANGE = bridge.property(@as(u32, 0x846E), .{ .template = true });
    pub const DEPTH_RANGE = bridge.property(@as(u32, 0x0B70), .{ .template = true });
    pub const COLOR_CLEAR_VALUE = bridge.property(@as(u32, 0x0C22), .{ .template = true });
    pub const COLOR_WRITEMASK = bridge.property(@as(u32, 0x0C23), .{ .template = true });
    pub const BLEND_COLOR = bridge.property(@as(u32, 0x8005), .{ .template = true });
    pub const COMPRESSED_TEXTURE_FORMATS = bridge.property(@as(u32, 0x86A3), .{ .template = true });
    pub const ARRAY_BUFFER_BINDING = bridge.property(@as(u32, 0x8894), .{ .template = true });
    pub const ELEMENT_ARRAY_BUFFER_BINDING = bridge.property(@as(u32, 0x8895), .{ .template = true });
    pub const CURRENT_PROGRAM = bridge.property(@as(u32, 0x8B8D), .{ .template = true });
    pub const TEXTURE_BINDING_2D = bridge.property(@as(u32, 0x8069), .{ .template = true });
    pub const TEXTURE_BINDING_CUBE_MAP = bridge.property(@as(u32, 0x8514), .{ .template = true });
    pub const FRAMEBUFFER_BINDING = bridge.property(@as(u32, 0x8CA6), .{ .template = true });
    pub const RENDERBUFFER_BINDING = bridge.property(@as(u32, 0x8CA7), .{ .template = true });
    pub const BLEND = bridge.property(@as(u32, 3042), .{ .template = true });
    pub const DITHER = bridge.property(@as(u32, 0x0BD0), .{ .template = true });
    pub const DEPTH_TEST = bridge.property(@as(u32, 2929), .{ .template = true });
    pub const SCISSOR_TEST = bridge.property(@as(u32, 3089), .{ .template = true });
    pub const TEXTURE_2D = bridge.property(@as(u32, 3553), .{ .template = true });
    pub const TEXTURE0 = bridge.property(@as(u32, 33984), .{ .template = true });
    pub const TEXTURE_MIN_FILTER = bridge.property(@as(u32, 10241), .{ .template = true });
    pub const TEXTURE_MAG_FILTER = bridge.property(@as(u32, 10240), .{ .template = true });
    pub const NEAREST = bridge.property(@as(u32, 9728), .{ .template = true });
    pub const LINEAR = bridge.property(@as(u32, 9729), .{ .template = true });
    pub const FRAMEBUFFER = bridge.property(@as(u32, 36160), .{ .template = true });
    pub const RENDERBUFFER = bridge.property(@as(u32, 36161), .{ .template = true });
    pub const FRAMEBUFFER_COMPLETE = bridge.property(@as(u32, 36053), .{ .template = true });
    pub const UNPACK_ALIGNMENT = bridge.property(@as(u32, 3317), .{ .template = true });
    pub const PACK_ALIGNMENT = bridge.property(@as(u32, 3333), .{ .template = true });
    pub const NO_ERROR = bridge.property(@as(u32, 0), .{ .template = true });
    pub const SRC_ALPHA = bridge.property(@as(u32, 770), .{ .template = true });
    pub const ONE_MINUS_SRC_ALPHA = bridge.property(@as(u32, 771), .{ .template = true });
    pub const ONE = bridge.property(@as(u32, 1), .{ .template = true });
    pub const ZERO = bridge.property(@as(u32, 0), .{ .template = true });
};

const testing = @import("../../../testing.zig");
test "WebApi: native WebGL shader arrays textures and readback" {
    try testing.htmlRunner("canvas/native_webgl.html", .{});
}
test "WebApi: native GL identity extensions and separate contexts" {
    try testing.htmlRunner("canvas/webgl_rendering_context.html", .{});
}

test "WebGL backing commands acquire writers while queries only lock" {
    inline for (.{ "clear", "drawArrays", "drawElements", "copyTexImage2D", "copyTexSubImage2D", "texImage2D", "texSubImage2D", "bufferData", "bufferSubData", "renderbufferStorage", "generateMipmap", "deleteTexture", "bindFramebuffer" }) |method| {
        try std.testing.expect(commandMutates(method));
    }
    inline for (.{ "getParameter", "getError", "getShaderInfoLog", "isTexture", "checkFramebufferStatus", "contextStatus", "finish", "flush" }) |method| {
        try std.testing.expect(!commandMutates(method));
    }
}
