// Copyright (C) 2023-2026  Lightpanda (Selecy SAS)
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

//! State and behaviour shared by the on-screen and offscreen 2D contexts.

const std = @import("std");

const js = @import("../../js/js.zig");
const color = @import("../../color.zig");
const png_encoder = @import("../../png.zig");
const TextMetrics = @import("TextMetrics.zig");
const CanvasGradient = @import("CanvasGradient.zig");
const CanvasPattern = @import("CanvasPattern.zig");

const Execution = js.Execution;

const default_font = "10px sans-serif";

// Chart libraries flip font and dash per label per animation frame, so these
// are stored inline: a page-arena copy per change never comes back. Only a
// value that doesn't fit falls back to the arena. 256 covers the long
// framework font stacks (Bootstrap's is ~200 bytes); 8 covers any real dash
// pattern after the odd-count doubling.
const font_capacity = 256;
const dash_capacity = 8;

/// Paint styles shared by both native 2D context brands.
pub const Style = union(enum) {
    color: color.RGBA,
    gradient: *CanvasGradient,
    pattern: *CanvasPattern,
};

/// Pointer fields first: the bridge probes them for an exact match before
/// falling back to string coercion.
pub const StyleInput = union(enum) {
    gradient: *CanvasGradient,
    pattern: *CanvasPattern,
    color: []const u8,
};

pub const StyleOutput = union(enum) {
    color: []const u8,
    gradient: *CanvasGradient,
    pattern: *CanvasPattern,
};

pub const State = struct {
    fill_style: Style = .{ .color = color.RGBA.Named.black },
    stroke_style: Style = .{ .color = color.RGBA.Named.black },

    font_size: f64 = 10,
    font_buf: [font_capacity]u8 = default_font.* ++ @as([font_capacity - default_font.len]u8, @splat(0)),
    font_len: u16 = default_font.len,
    font_overflow: ?[]const u8 = null,

    dash_buf: [dash_capacity]f64 = undefined,
    dash_len: u8 = 0,
    dash_overflow: ?[]const f64 = null,

    pub fn font(self: *const State) []const u8 {
        return self.font_overflow orelse self.font_buf[0..self.font_len];
    }

    /// Invalid font strings are ignored, per the spec.
    pub fn setFont(self: *State, value: []const u8, exec: *const Execution) !void {
        if (std.mem.eql(u8, value, self.font())) return;
        const size = parseFontSize(value) orelse return;
        if (value.len <= font_capacity) {
            @memcpy(self.font_buf[0..value.len], value);
            self.font_len = @intCast(value.len);
            self.font_overflow = null;
        } else {
            self.font_overflow = try exec.page_arena.dupe(u8, value);
        }
        self.font_size = size;
    }

    pub fn lineDash(self: *const State) []const f64 {
        return self.dash_overflow orelse self.dash_buf[0..self.dash_len];
    }

    /// Negative or non-finite segments leave the list unchanged; an odd count
    /// is repeated, per the spec.
    pub fn setLineDash(self: *State, segments: []const f64, exec: *const Execution) !void {
        for (segments) |s| {
            if (!(s >= 0) or std.math.isInf(s)) return;
        }
        if (std.mem.eql(f64, segments, self.lineDash())) return;
        const repeat: usize = if (segments.len % 2 == 0) 1 else 2;
        const len = segments.len * repeat;
        if (len <= dash_capacity) {
            for (0..repeat) |i| @memcpy(self.dash_buf[i * segments.len ..][0..segments.len], segments);
            self.dash_len = @intCast(len);
            self.dash_overflow = null;
            return;
        }
        const dash = try exec.page_arena.alloc(f64, len);
        for (0..repeat) |i| @memcpy(dash[i * segments.len ..][0..segments.len], segments);
        self.dash_overflow = dash;
    }

    pub fn setStyle(self: *State, comptime field: enum { fill, stroke }, value: StyleInput) void {
        const style: *Style = if (field == .fill) &self.fill_style else &self.stroke_style;
        switch (value) {
            .gradient => |g| style.* = .{ .gradient = g },
            .pattern => |p| style.* = .{ .pattern = p },
            .color => |c| {
                // An unparseable color keeps the current style, per the spec.
                // Parsed into a local on purpose: assigning the literal
                // straight through `style` writes the tag before the payload,
                // so an early return from inside it leaves a `.color` with
                // no colour.
                const parsed = color.RGBA.parse(c) catch return;
                style.* = .{ .color = parsed };
            },
        }
    }

    pub fn getStyle(self: *const State, comptime field: enum { fill, stroke }, exec: *const Execution) !StyleOutput {
        const style = if (field == .fill) self.fill_style else self.stroke_style;
        switch (style) {
            .gradient => |g| return .{ .gradient = g },
            .pattern => |p| return .{ .pattern = p },
            .color => |c| {
                var w = std.Io.Writer.Allocating.init(exec.local_arena);
                try c.format(&w.writer);
                return .{ .color = w.written() };
            },
        }
    }
};

/// The size in px out of a CSS font shorthand, or null when there is none.
fn parseFontSize(font: []const u8) ?f64 {
    var it = std.mem.tokenizeAny(u8, font, " \t\n\r");
    while (it.next()) |raw| {
        // "16px/1.5" carries a line-height.
        const token = raw[0 .. std.mem.findScalar(u8, raw, '/') orelse raw.len];
        const units = [_]struct { suffix: []const u8, px: f64 }{
            .{ .suffix = "px", .px = 1 },
            .{ .suffix = "pt", .px = 4.0 / 3.0 },
            .{ .suffix = "rem", .px = 16 },
            .{ .suffix = "em", .px = 16 },
            .{ .suffix = "%", .px = 0.16 },
        };
        for (units) |unit| {
            if (!std.ascii.endsWithIgnoreCase(token, unit.suffix)) continue;
            const number = std.fmt.parseFloat(f64, token[0 .. token.len - unit.suffix.len]) catch continue;
            if (number < 0 or std.math.isInf(number) or std.math.isNan(number)) return null;
            return number * unit.px;
        }
    }
    return null;
}

const testing = std.testing;
test "context2d: parseFontSize" {
    try testing.expectEqual(10, parseFontSize("10px sans-serif").?);
    try testing.expectEqual(20, parseFontSize("italic bold 20px/1.5 Arial, sans-serif").?);
    try testing.expectEqual(16, parseFontSize("12pt serif").?);
    try testing.expectEqual(24, parseFontSize("1.5em serif").?);
    try testing.expectEqual(null, parseFontSize("bold serif"));
    try testing.expectEqual(null, parseFontSize(""));
}

pub const Backend = opaque {};
pub const Matrix = [6]f64;
pub const identity: Matrix = .{ 1, 0, 0, 1, 0, 0 };
pub fn multiply(a: Matrix, b: Matrix) Matrix {
    return .{ a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3], a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5] };
}
pub fn matrix(init: @import("../DOMMatrixReadOnly.zig").DOMMatrixInit) !Matrix {
    const parsed = try @import("../DOMMatrixReadOnly.zig").fixupDict(init);
    if (!parsed.is_2d) return error.TypeError;
    const m = parsed.m;
    return .{ m[0], m[1], m[4], m[5], m[12], m[13] };
}
fn sink(context: *anyopaque, bytes: [*]const u8, len: usize) callconv(.c) bool {
    const writer: *std.Io.Writer = @ptrCast(@alignCast(context));
    writer.writeAll(bytes[0..len]) catch return false;
    return true;
}
/// A real native bitmap. Realm registries own references, never wrappers or an
/// ephemeral Execution. The HTML placeholder is an observer, not a second page.
/// A lifecycle reference must cover each operation. The mutex protects backing,
/// dimensions, origin state and observer lifetime; only the refcount is atomic.
/// Locked helpers never reacquire the mutex. Readback returns dimensions and
/// bytes from one capture. Observed mutations begin a Page writer before backing
/// work; queued GL work retains one writer until capture/teardown synchronizes it.
pub const Surface = struct {
    const GL = @import("WebGLRenderingContext.zig");
    const Backing = union(enum) { empty, raster: *Backend, gl: *GL.Backend };
    pub const Size = struct { width: u32, height: u32 };
    pub const PaintState = struct { alpha: f64, composite: []const u8, smooth: bool };
    mutex: std.Io.Mutex = .init,
    refs: std.atomic.Value(usize) = .init(1),
    allocator: std.mem.Allocator = std.heap.page_allocator,
    _backing: Backing = .empty,
    fonts: []const u8,
    _width: u32,
    _height: u32,
    _origin_clean: bool = true,
    _observer: ?*@import("../../Frame.zig") = null,
    _pending_gl_write: ?@import("../../Page.zig").CanvasWrite = null,
    // Only construct/end this envelope while the surface lock pins its observer.
    const Write = struct {
        surface: *Surface,
        token: ?@import("../../Page.zig").CanvasWrite,

        fn end(self: Write) void {
            if (self.token) |token| {
                // Submission is not GPU completion. Acquire one retained writer
                // before releasing the immediate writer, without a stable gap.
                if (self.surface._backing == .gl and self.surface._pending_gl_write == null) {
                    self.surface._pending_gl_write = token.page.beginCanvasWrite();
                }
                token.end();
            }
        }
    };
    fn beginWriteLocked(self: *Surface) Write {
        return .{ .surface = self, .token = if (self._observer) |frame| frame.page.beginCanvasWrite() else null };
    }
    fn settleObservedWritesLocked(self: *Surface) !void {
        const token = self._pending_gl_write orelse return;
        self._pending_gl_write = null;
        defer token.end();
        if (self._backing != .gl or !GL.lp_gl_finish(self._backing.gl)) {
            token.page.invalidateCanvasWrites();
            return error.InvalidStateError;
        }
    }
    /// Capture calls this once before sampling the exact model guard. Later
    /// submissions acquire a new pending writer and invalidate qualification.
    pub fn settleObservedWrites(self: *Surface) !void {
        self.lock();
        defer self.unlock();
        try self.settleObservedWritesLocked();
    }
    pub const GlAccess = struct {
        surface: *Surface,
        backend: *GL.Backend,
        write: ?Write,

        pub fn end(self: GlAccess) void {
            if (self.write) |write| {
                self.surface.dirtyLocked();
                write.end();
            }
            self.surface.unlock();
        }
    };
    pub fn beginGlAccess(self: *Surface, mutating: bool) !GlAccess {
        self.lock();
        if (self._backing != .gl) {
            self.unlock();
            return error.InvalidStateError;
        }
        return .{ .surface = self, .backend = self._backing.gl, .write = if (mutating) self.beginWriteLocked() else null };
    }
    pub fn init(width: u32, height: u32, exec: *const Execution) !*Surface {
        if (width > 16384 or height > 16384 or @as(u64, width) * height * 4 > 268435456) return error.InvalidStateError;
        const allocator = try @import("../storage/RuntimeMemory.zig").allocator(std.heap.page_allocator);
        const fonts = try std.json.Stringify.valueAlloc(allocator, exec.session.browser.app.config.renderFonts(), .{});
        errdefer allocator.free(fonts);
        const value = try allocator.create(Surface);
        value.* = .{ .allocator = allocator, .fonts = fonts, ._width = width, ._height = height };
        errdefer allocator.destroy(value);
        try value.track(exec);
        return value;
    }
    fn lock(self: *Surface) void {
        self.mutex.lockUncancelable(@import("lightpanda").io);
    }
    fn unlock(self: *Surface) void {
        self.mutex.unlock(@import("lightpanda").io);
    }
    fn track(self: *Surface, exec: *const Execution) !void {
        switch (exec.js.global) {
            .frame => |frame| try frame._canvas_surfaces.append(frame.page_arena, self),
            .worker => |worker| try worker._canvas_surfaces.append(exec.page_arena, self),
        }
    }
    pub fn retainIn(self: *Surface, exec: *const Execution) !void {
        self.retain();
        errdefer self.release();
        try self.track(exec);
    }
    pub fn retain(self: *Surface) void {
        _ = self.refs.fetchAdd(1, .monotonic);
    }
    pub fn release(self: *Surface) void {
        if (self.refs.fetchSub(1, .acq_rel) != 1) return;
        self.lock();
        self.settleObservedWritesLocked() catch {};
        switch (self._backing) {
            .raster => |backend| lp_canvas_free(backend),
            .gl => |backend| GL.release(backend),
            .empty => {},
        }
        self.unlock();
        const allocator = self.allocator;
        allocator.free(self.fonts);
        allocator.destroy(self);
    }
    pub fn size(self: *Surface) Size {
        self.lock();
        defer self.unlock();
        return .{ .width = self._width, .height = self._height };
    }
    pub fn originClean(self: *Surface) bool {
        self.lock();
        defer self.unlock();
        return self._origin_clean;
    }
    pub fn observe(self: *Surface, frame: ?*@import("../../Frame.zig")) void {
        self.lock();
        defer self.unlock();
        if (self._observer == frame) return;
        self.settleObservedWritesLocked() catch {};
        self._observer = frame;
    }
    pub fn detachObserver(self: *Surface, frame: *@import("../../Frame.zig")) void {
        self.lock();
        defer self.unlock();
        if (self._observer == frame) {
            self.settleObservedWritesLocked() catch {};
            self._observer = null;
        }
    }
    fn rasterLocked(self: *Surface) !*Backend {
        switch (self._backing) {
            .raster => |backend| return backend,
            .gl => return error.InvalidStateError,
            .empty => {
                const backend = lp_canvas_new(self._width, self._height, self.fonts.ptr, self.fonts.len) orelse return error.InvalidStateError;
                self._backing = .{ .raster = backend };
                return backend;
            },
        }
    }
    pub fn initializeRaster(self: *Surface) !void {
        self.lock();
        defer self.unlock();
        const write = self.beginWriteLocked();
        defer write.end();
        _ = try self.rasterLocked();
    }
    pub fn initializeGl(self: *Surface) !?*GL.Backend {
        self.lock();
        defer self.unlock();
        if (self._backing != .empty) return error.InvalidStateError;
        const write = self.beginWriteLocked();
        defer write.end();
        const backend = GL.lp_gl_new(self._width, self._height) orelse return null;
        self._backing = .{ .gl = backend };
        return backend;
    }
    fn dirtyLocked(self: *Surface) void {
        if (self._observer) |frame| {
            _ = frame._render_resource_version.fetchAdd(1, .release);
            frame.page.visualChanged();
        }
    }
    pub fn dirty(self: *Surface) void {
        self.lock();
        defer self.unlock();
        self.dirtyLocked();
    }
    pub fn resize(self: *Surface, width: u32, height: u32) !void {
        if (width > 16384 or height > 16384 or @as(u64, width) * height * 4 > 268435456) return error.InvalidStateError;
        self.lock();
        defer self.unlock();
        const write = self.beginWriteLocked();
        defer write.end();
        const ok = switch (self._backing) {
            .raster => |backend| lp_canvas_resize(backend, width, height),
            .gl => |backend| GL.lp_gl_resize(backend, width, height),
            .empty => true,
        };
        if (!ok) return error.InvalidStateError;
        self._width = width;
        self._height = height;
        self._origin_clean = true;
        self.dirtyLocked();
    }
    fn snapshotLocked(self: *Surface, arena: std.mem.Allocator) !PixelSource {
        const bytes = try arena.alloc(u8, @as(usize, self._width) * self._height * 4);
        const ok = switch (self._backing) {
            .empty => blk: {
                @memset(bytes, 0);
                break :blk true;
            },
            .raster => |backend| lp_canvas_pixels(backend, bytes.ptr, bytes.len),
            .gl => |backend| GL.lp_gl_readback(backend, bytes.ptr, bytes.len),
        };
        if (!ok) return error.InvalidStateError;
        return .{ .width = self._width, .height = self._height, .bytes = bytes, .origin_clean = self._origin_clean };
    }
    pub fn snapshot(self: *Surface, arena: std.mem.Allocator) !PixelSource {
        self.lock();
        defer self.unlock();
        return self.snapshotLocked(arena);
    }
    fn clearBitmapLocked(self: *Surface, arena: std.mem.Allocator) !void {
        const write = self.beginWriteLocked();
        defer write.end();
        switch (self._backing) {
            .empty => {},
            .raster => |backend| {
                const bytes = try arena.alloc(u8, @as(usize, self._width) * self._height * 4);
                @memset(bytes, 0);
                if (!lp_canvas_put(backend, bytes.ptr, self._width, self._height, 0, 0)) return error.InvalidStateError;
            },
            .gl => |backend| try GL.clearBitmap(backend),
        }
        self._origin_clean = true;
        self.dirtyLocked();
    }
    pub fn takeBitmap(self: *Surface, arena: std.mem.Allocator, scratch: std.mem.Allocator) !PixelSource {
        self.lock();
        defer self.unlock();
        if (self._width == 0 or self._height == 0) return error.InvalidStateError;
        const result = try self.snapshotLocked(arena);
        try self.clearBitmapLocked(scratch);
        return result;
    }
    pub fn png(self: *Surface, writer: *std.Io.Writer) !bool {
        self.lock();
        defer self.unlock();
        if (!self._origin_clean) return error.SecurityError;
        if (self._width == 0 or self._height == 0) return false;
        if (self._backing == .raster) {
            if (!lp_canvas_png(self._backing.raster, writer, sink)) return error.InvalidStateError;
            return true;
        }
        var scratch = std.heap.ArenaAllocator.init(self.allocator);
        defer scratch.deinit();
        const image = try self.snapshotLocked(scratch.allocator());
        if (!png_encoder.write(image.bytes.ptr, image.width, image.height, writer, sink)) return error.InvalidStateError;
        return true;
    }
    pub fn request(self: *Surface, value: anytype, arena: std.mem.Allocator, origin_clean: bool) ![]const u8 {
        const bytes = try std.json.Stringify.valueAlloc(arena, value, .{});
        var out: std.Io.Writer.Allocating = .init(arena);
        self.lock();
        defer self.unlock();
        const write = self.beginWriteLocked();
        defer write.end();
        const backend = try self.rasterLocked();
        if (!origin_clean) self._origin_clean = false;
        defer self.dirtyLocked();
        if (!lp_canvas_command(backend, bytes.ptr, bytes.len, &out.writer, sink)) return error.NotSupportedError;
        return out.written();
    }
    pub fn drawPixels(self: *Surface, state: PaintState, image: PixelSource, coords: *const [8]f64, arena: std.mem.Allocator) !void {
        const command = try std.json.Stringify.valueAlloc(arena, .{ .method = "drawState", .alpha = state.alpha, .composite = state.composite, .smooth = state.smooth }, .{});
        var out: std.Io.Writer.Allocating = .init(arena);
        self.lock();
        defer self.unlock();
        const write = self.beginWriteLocked();
        defer write.end();
        const backend = try self.rasterLocked();
        if (!lp_canvas_command(backend, command.ptr, command.len, &out.writer, sink)) return error.NotSupportedError;
        if (!image.origin_clean) self._origin_clean = false;
        defer self.dirtyLocked();
        if (!lp_canvas_draw_pixels(backend, image.bytes.ptr, image.width, image.height, coords)) return error.InvalidStateError;
    }
    pub fn putPixels(self: *Surface, bytes: []const u8, image_width: u32, x: i32, y: i32, dirty_rect: [4]u32) !void {
        self.lock();
        defer self.unlock();
        const write = self.beginWriteLocked();
        defer write.end();
        const backend = try self.rasterLocked();
        defer self.dirtyLocked();
        const x0 = dirty_rect[0];
        const y0 = dirty_rect[1];
        const x1 = dirty_rect[2];
        const y1 = dirty_rect[3];
        for (y0..y1) |row| {
            const dx = @as(i64, x) + x0;
            const dy = @as(i64, y) + @as(i64, @intCast(row));
            if (dx > std.math.maxInt(i32) or dy > std.math.maxInt(i32)) continue;
            const offset = (row * image_width + x0) * 4;
            if (!lp_canvas_put(backend, bytes[offset..].ptr, x1 - x0, 1, @intCast(dx), @intCast(dy))) return error.InvalidStateError;
        }
    }
};
pub const PixelSource = struct { width: u32, height: u32, bytes: []const u8, origin_clean: bool };
pub fn pixelSource(source: js.Value, exec: *Execution) !PixelSource {
    const local = exec.js.local.?;
    const Canvas = @import("../element/html/Canvas.zig");
    const Offscreen = @import("OffscreenCanvas.zig");
    if (local.jsValueToZig(*Canvas, source)) |canvas| {
        return canvas.pixels(exec.local_arena);
    } else |_| {}
    if (local.jsValueToZig(*Offscreen, source)) |canvas| {
        return (try canvas.surface(exec)).snapshot(exec.local_arena);
    } else |_| {}
    if (local.jsValueToZig(*Offscreen.ImageBitmap, source)) |bitmap| {
        if (bitmap._closed) return error.InvalidStateError;
        return .{ .width = bitmap._width, .height = bitmap._height, .bytes = bitmap._pixels, .origin_clean = bitmap._origin_clean };
    } else |_| {}
    if (local.jsValueToZig(*@import("../ImageData.zig"), source)) |image| {
        return .{ .width = image._width, .height = image._height, .bytes = image._data.local(local).slice(), .origin_clean = true };
    } else |_| {}
    const image = local.jsValueToZig(*@import("../element/html/Image.zig"), source) catch return error.TypeError;
    const frame = switch (exec.js.global) {
        .frame => |frame| frame,
        .worker => return error.TypeError,
    };
    const url = try image.getSrc(frame);
    const encoded = frame._render_resources.get(url) orelse return error.InvalidStateError;
    const w = image._natural_width;
    const h = image._natural_height;
    if (w == 0 or h == 0) return error.InvalidStateError;
    // Decode authoritative LP image bytes through the same native renderer.
    const fonts = "[]";
    const backend = lp_canvas_new(w, h, fonts.ptr, fonts.len) orelse return error.InvalidStateError;
    defer lp_canvas_free(backend);
    const coords = [8]f64{ 0, 0, @floatFromInt(w), @floatFromInt(h), 0, 0, @floatFromInt(w), @floatFromInt(h) };
    if (!lp_canvas_draw_encoded(backend, encoded.ptr, encoded.len, &coords)) return error.InvalidStateError;
    const bytes = try exec.local_arena.alloc(u8, @as(usize, w) * h * 4);
    if (!lp_canvas_pixels(backend, bytes.ptr, bytes.len)) return error.InvalidStateError;
    return .{ .width = w, .height = h, .bytes = bytes, .origin_clean = std.mem.startsWith(u8, url, "data:") or @import("../../URL.zig").isSameOrigin(url, frame.origin orelse "") };
}

pub const Drawing = struct {
    surface: *Surface,
    state: State = .{},
    alpha: f64 = 1,
    line_width: f64 = 1,
    @"align": []const u8 = "start",
    baseline: []const u8 = "alphabetic",
    composite: []const u8 = "source-over",
    line_cap: []const u8 = "butt",
    line_join: []const u8 = "miter",
    miter_limit: f64 = 10,
    dash_offset: f64 = 0,
    smooth: bool = true,
    ctm: Matrix = identity,
    saved: std.ArrayList(Saved) = .empty,
    const Saved = struct { state: State, alpha: f64, line_width: f64, @"align": []const u8, baseline: []const u8, composite: []const u8, line_cap: []const u8, line_join: []const u8, miter_limit: f64, dash_offset: f64, smooth: bool, ctm: Matrix };
};

/// One implementation, two native brands. All transient bytes use the calling
/// realm's Execution, supplied on every call and never retained.
///
/// Native command ABI (canvas2d.rs): method/args, numeric RGBA color,
/// alpha/composite/smooth, line_width/line_cap/line_join/miter_limit,
/// dash/dash_offset, evenodd, and optional path M/L/Q/C/Z commands.
/// Gradients carry kind, six coords, RGBA stops, and creation-time transform.
/// Patterns carry width/height, RGBA pixels, repetition, and pattern transform.
/// Text carries shaped font inputs and alignment under `text`; drawImage sends
/// drawState before the unchanged pixel/encoded C entrypoints. Surface owns
/// either the raster handle or the real GL handle, never both, across transfer.
pub fn Context(comptime T: type, comptime C: type, comptime class_name: []const u8) type {
    return struct {
        const ThisContext = @This();
        const Path2D = @import("Path2D.zig");
        const ImageData = @import("../ImageData.zig");
        const Bytes = @import("../../graphics_bytes.zig").Bytes;
        pub fn init(canvas: *C, exec: *Execution) !*T {
            const s = try canvas.surface(exec);
            try s.initializeRaster();
            return exec._factory.create(T{ ._canvas = canvas, ._drawing = .{ .surface = s } });
        }
        fn check(self: *const T) !void {
            try self._canvas.checkDrawing();
        }
        fn paint(self: *T, method: []const u8, args: anytype, stroke_style: bool, path: ?*Path2D, rule: []const u8, arena: std.mem.Allocator) ![]const u8 {
            try check(self);
            const d = &self._drawing;
            const style = if (stroke_style) d.state.stroke_style else d.state.fill_style;
            const origin_clean = !(style == .pattern and !style.pattern.origin_clean and (std.mem.eql(u8, method, "fill") or std.mem.eql(u8, method, "stroke") or std.mem.eql(u8, method, "fillRect") or std.mem.eql(u8, method, "strokeRect")));
            const rgba: [4]u8 = if (style == .color) .{ style.color.r, style.color.g, style.color.b, style.color.a } else .{ 0, 0, 0, 255 };
            return d.surface.request(.{ .method = method, .args = args, .color = Bytes{ .data = &rgba }, .alpha = d.alpha, .composite = d.composite, .smooth = d.smooth, .line_width = d.line_width, .line_cap = d.line_cap, .line_join = d.line_join, .miter_limit = d.miter_limit, .dash = d.state.lineDash(), .dash_offset = d.dash_offset, .gradient = if (style == .gradient) style.gradient else null, .pattern = if (style == .pattern) style.pattern else null, .path = if (path) |p| p.commands.items else null, .evenodd = std.mem.eql(u8, rule, "evenodd") }, arena, origin_clean);
        }
        fn draw(self: *T, method: []const u8, args: anytype, stroke_style: bool, exec: *Execution) !void {
            _ = try paint(self, method, args, stroke_style, null, "nonzero", exec.local_arena);
        }
        pub fn resize(self: *T) !void {
            const s = self._drawing.surface;
            try s.resize(self._canvas.getWidth(), self._canvas.getHeight());
            self._drawing.saved.clearRetainingCapacity();
            const saved = self._drawing.saved;
            self._drawing = .{ .surface = s, .saved = saved };
        }
        pub fn getCanvas(self: *const T) *C {
            return self._canvas;
        }
        pub fn getFont(self: *const T) []const u8 {
            return self._drawing.state.font();
        }
        pub fn setFont(self: *T, v: []const u8, e: *Execution) !void {
            try self._drawing.state.setFont(v, e);
        }
        pub fn getFillStyle(self: *const T, e: *Execution) !StyleOutput {
            return self._drawing.state.getStyle(.fill, e);
        }
        pub fn setFillStyle(self: *T, v: StyleInput) void {
            self._drawing.state.setStyle(.fill, v);
        }
        pub fn getStrokeStyle(self: *const T, e: *Execution) !StyleOutput {
            return self._drawing.state.getStyle(.stroke, e);
        }
        pub fn setStrokeStyle(self: *T, v: StyleInput) void {
            self._drawing.state.setStyle(.stroke, v);
        }
        pub fn getAlpha(self: *const T) f64 {
            return self._drawing.alpha;
        }
        pub fn setAlpha(self: *T, v: f64) void {
            if (std.math.isFinite(v) and v >= 0 and v <= 1) self._drawing.alpha = v;
        }
        pub fn getLineWidth(self: *const T) f64 {
            return self._drawing.line_width;
        }
        pub fn setLineWidth(self: *T, v: f64) void {
            if (std.math.isFinite(v) and v > 0) self._drawing.line_width = v;
        }
        pub fn getAlign(self: *const T) []const u8 {
            return self._drawing.@"align";
        }
        pub fn setAlign(self: *T, v: []const u8) void {
            for ([_][]const u8{ "start", "end", "left", "right", "center" }) |a| if (std.mem.eql(u8, a, v)) {
                self._drawing.@"align" = a;
                return;
            };
        }
        pub fn getBaseline(self: *const T) []const u8 {
            return self._drawing.baseline;
        }
        pub fn setBaseline(self: *T, v: []const u8) void {
            for ([_][]const u8{ "top", "hanging", "middle", "alphabetic", "ideographic", "bottom" }) |a| if (std.mem.eql(u8, a, v)) {
                self._drawing.baseline = a;
                return;
            };
        }
        pub fn getComposite(self: *const T) []const u8 {
            return self._drawing.composite;
        }
        pub fn setComposite(self: *T, v: []const u8) void {
            for ([_][]const u8{ "source-over", "source-in", "source-out", "source-atop", "destination-over", "destination-in", "destination-out", "destination-atop", "lighter", "copy", "xor", "multiply", "screen", "overlay", "darken", "lighten", "color-dodge", "color-burn", "hard-light", "soft-light", "difference", "exclusion", "hue", "saturation", "color", "luminosity" }) |a| if (std.mem.eql(u8, a, v)) {
                self._drawing.composite = a;
                return;
            };
        }
        pub fn getCap(self: *const T) []const u8 {
            return self._drawing.line_cap;
        }
        pub fn setCap(self: *T, v: []const u8) void {
            for ([_][]const u8{ "butt", "round", "square" }) |a| if (std.mem.eql(u8, a, v)) {
                self._drawing.line_cap = a;
                return;
            };
        }
        pub fn getJoin(self: *const T) []const u8 {
            return self._drawing.line_join;
        }
        pub fn setJoin(self: *T, v: []const u8) void {
            for ([_][]const u8{ "round", "bevel", "miter" }) |a| if (std.mem.eql(u8, a, v)) {
                self._drawing.line_join = a;
                return;
            };
        }
        pub fn getMiter(self: *const T) f64 {
            return self._drawing.miter_limit;
        }
        pub fn setMiter(self: *T, v: f64) void {
            if (std.math.isFinite(v) and v > 0) self._drawing.miter_limit = v;
        }
        pub fn getDashOffset(self: *const T) f64 {
            return self._drawing.dash_offset;
        }
        pub fn setDashOffset(self: *T, v: f64) void {
            if (std.math.isFinite(v)) self._drawing.dash_offset = v;
        }
        pub fn getSmooth(self: *const T) bool {
            return self._drawing.smooth;
        }
        pub fn setSmooth(self: *T, v: bool) void {
            self._drawing.smooth = v;
        }
        pub fn getLineDash(self: *const T) []const f64 {
            return self._drawing.state.lineDash();
        }
        pub fn setLineDash(self: *T, v: []const f64, e: *Execution) !void {
            try self._drawing.state.setLineDash(v, e);
        }
        pub fn save(self: *T, e: *Execution) !void {
            const d = &self._drawing;
            try d.saved.append(e.page_arena, .{ .state = d.state, .alpha = d.alpha, .line_width = d.line_width, .@"align" = d.@"align", .baseline = d.baseline, .composite = d.composite, .line_cap = d.line_cap, .line_join = d.line_join, .miter_limit = d.miter_limit, .dash_offset = d.dash_offset, .smooth = d.smooth, .ctm = d.ctm });
            try draw(self, "save", .{}, false, e);
        }
        pub fn restore(self: *T, e: *Execution) !void {
            const d = &self._drawing;
            if (d.saved.pop()) |s| {
                inline for (@typeInfo(Drawing.Saved).@"struct".field_names) |field_name| @field(d, field_name) = @field(s, field_name);
            }
            try draw(self, "restore", .{}, false, e);
        }
        pub fn reset(self: *T) !void {
            try resize(self);
        }
        pub fn transform(self: *T, a: f64, b: f64, c: f64, d: f64, x: f64, y: f64, e: *Execution) !void {
            const m = Matrix{ a, b, c, d, x, y };
            for (m) |v| if (!std.math.isFinite(v)) return;
            try draw(self, "transform", m, false, e);
            self._drawing.ctm = multiply(self._drawing.ctm, m);
        }
        pub const TransformInput = union(enum) { number: f64, dict: @import("../DOMMatrixReadOnly.zig").DOMMatrixInit };
        pub fn setTransform(self: *T, a: ?TransformInput, b: ?f64, c: ?f64, d: ?f64, x: ?f64, y: ?f64, e: *Execution) !void {
            const m: Matrix = if (a) |v| switch (v) {
                .dict => |dict| try matrix(dict),
                .number => |n| .{ n, b orelse return error.TypeError, c orelse return error.TypeError, d orelse return error.TypeError, x orelse return error.TypeError, y orelse return error.TypeError },
            } else identity;
            for (m) |v| if (!std.math.isFinite(v)) return;
            try draw(self, "setTransform", m, false, e);
            self._drawing.ctm = m;
        }
        pub fn resetTransform(self: *T, e: *Execution) !void {
            try draw(self, "resetTransform", .{}, false, e);
            self._drawing.ctm = identity;
        }
        pub fn getTransform(self: *T, e: *Execution) !*@import("../DOMMatrix.zig") {
            const m = self._drawing.ctm;
            return @import("../DOMMatrix.zig").create(.{ m[0], m[1], 0, 0, m[2], m[3], 0, 0, 0, 0, 1, 0, m[4], m[5], 0, 1 }, true, e.page);
        }
        pub fn scale(self: *T, x: f64, y: f64, e: *Execution) !void {
            try transform(self, x, 0, 0, y, 0, 0, e);
        }
        pub fn translate(self: *T, x: f64, y: f64, e: *Execution) !void {
            try transform(self, 1, 0, 0, 1, x, y, e);
        }
        pub fn rotate(self: *T, a: f64, e: *Execution) !void {
            try transform(self, @cos(a), @sin(a), -@sin(a), @cos(a), 0, 0, e);
        }
        pub fn createLinearGradient(self: *T, x0: f64, y0: f64, x1: f64, y1: f64, e: *Execution) !*CanvasGradient {
            return gradient(self, "linear", .{ x0, y0, x1, y1, 0, 0 }, e);
        }
        pub fn createRadialGradient(self: *T, x0: f64, y0: f64, r0: f64, x1: f64, y1: f64, r1: f64, e: *Execution) !*CanvasGradient {
            if (r0 < 0 or r1 < 0) return error.IndexSizeError;
            return gradient(self, "radial", .{ x0, y0, r0, x1, y1, r1 }, e);
        }
        pub fn createConicGradient(self: *T, angle: f64, x: f64, y: f64, e: *Execution) !*CanvasGradient {
            return gradient(self, "conic", .{ angle, x, y, 0, 0, 0 }, e);
        }
        fn gradient(self: *T, kind: []const u8, coords: [6]f64, e: *Execution) !*CanvasGradient {
            for (coords) |v| if (!std.math.isFinite(v)) return error.NotSupportedError;
            const g = try CanvasGradient.init(e);
            g.kind = kind;
            g.coords = coords;
            g.transform = self._drawing.ctm;
            return g;
        }
        pub fn createPattern(self: *T, source: js.Value, repetition: ?[]const u8, e: *Execution) !?*CanvasPattern {
            const rep = repetition orelse "repeat";
            var valid = false;
            for ([_][]const u8{ "", "repeat", "repeat-x", "repeat-y", "no-repeat" }) |a| {
                if (std.mem.eql(u8, a, rep)) valid = true;
            }
            if (!valid) return error.SyntaxError;
            const p = pixelSource(source, e) catch |err| {
                if (err == error.InvalidStateError) return null;
                return err;
            };
            if (p.width == 0 or p.height == 0) return null;
            _ = self;
            return CanvasPattern.init(p, if (rep.len == 0) "repeat" else rep, e);
        }
        fn fontFamily(font: []const u8) []const u8 {
            var tokens = std.mem.tokenizeAny(u8, font, " \t\r\n");
            while (tokens.next()) |token| {
                if (parseFontSize(token) == null) continue;
                const end = @intFromPtr(token.ptr) + token.len - @intFromPtr(font.ptr);
                return std.mem.trim(u8, font[end..], " \t\r\n\"'");
            }
            return "sans-serif";
        }
        fn text(self: *T, method: []const u8, value: []const u8, x: f64, y: f64, max_width: ?f64, e: *Execution) ![]const u8 {
            try check(self);
            const d = &self._drawing;
            const font = d.state.font();
            const family = fontFamily(font);
            const style = if (std.mem.eql(u8, method, "strokeText")) d.state.stroke_style else d.state.fill_style;
            const origin_clean = !(style == .pattern and !style.pattern.origin_clean and !std.mem.eql(u8, method, "measureText"));
            const rgba: [4]u8 = if (style == .color) .{ style.color.r, style.color.g, style.color.b, style.color.a } else .{ 0, 0, 0, 255 };
            return d.surface.request(.{ .method = method, .args = .{}, .color = Bytes{ .data = &rgba }, .alpha = d.alpha, .composite = d.composite, .line_width = d.line_width, .line_cap = d.line_cap, .line_join = d.line_join, .miter_limit = d.miter_limit, .dash = d.state.lineDash(), .dash_offset = d.dash_offset, .gradient = if (style == .gradient) style.gradient else null, .pattern = if (style == .pattern) style.pattern else null, .text = .{ .text = value, .font_size = d.state.font_size, .family = family, .weight = @as(u16, if (std.mem.findPos(u8, font, 0, "bold") != null) 700 else 400), .italic = std.mem.findPos(u8, font, 0, "italic") != null, .x = x, .y = y, .@"align" = d.@"align", .baseline = d.baseline, .max_width = max_width, .composite = d.composite } }, e.local_arena, origin_clean);
        }
        pub fn measureText(self: *T, value: []const u8, e: *Execution) !*TextMetrics {
            return TextMetrics.fromNative(try text(self, "measureText", value, 0, 0, null, e), e);
        }
        pub fn fillText(self: *T, value: []const u8, x: f64, y: f64, max_width: ?f64, e: *Execution) !void {
            _ = try text(self, "fillText", value, x, y, max_width, e);
        }
        pub fn strokeText(self: *T, value: []const u8, x: f64, y: f64, max_width: ?f64, e: *Execution) !void {
            _ = try text(self, "strokeText", value, x, y, max_width, e);
        }
        pub const ImageDataInput = union(enum) { image: *ImageData, width: i32 };
        pub fn createImageData(_: *T, input: ImageDataInput, height: ?i32, settings: ?ImageData.ConstructorSettings, e: *Execution) !*ImageData {
            switch (input) {
                .image => |image| return ImageData.init(image._width, image._height, settings, e),
                .width => |width| {
                    const h = height orelse return error.TypeError;
                    if (width == 0 or h == 0) return error.IndexSizeError;
                    return ImageData.init(@intCast(@abs(width)), @intCast(@abs(h)), settings, e);
                },
            }
        }
        pub fn getImageData(self: *T, sx: i32, sy: i32, sw: i32, sh: i32, e: *Execution) !*ImageData {
            try check(self);
            if (sw == 0 or sh == 0) return error.IndexSizeError;
            const source = try self._drawing.surface.snapshot(e.local_arena);
            if (!source.origin_clean) return error.SecurityError;
            const w: u32 = @intCast(@abs(sw));
            const h: u32 = @intCast(@abs(sh));
            const image = try ImageData.init(w, h, null, e);
            const out = image._data.local(e.js.local.?).slice();
            @memset(out, 0);
            const x0 = @as(i64, sx) + @min(@as(i64, sw), 0);
            const y0 = @as(i64, sy) + @min(@as(i64, sh), 0);
            for (0..h) |y| {
                for (0..w) |x| {
                    const tx = x0 + @as(i64, @intCast(x));
                    const ty = y0 + @as(i64, @intCast(y));
                    if (tx < 0 or ty < 0 or tx >= source.width or ty >= source.height) continue;
                    const src = (@as(usize, @intCast(ty)) * source.width + @as(usize, @intCast(tx))) * 4;
                    const dst = (y * w + x) * 4;
                    @memcpy(out[dst..][0..4], source.bytes[src..][0..4]);
                }
            }
            return image;
        }
        pub fn putImageData(self: *T, image: *ImageData, x: f64, y: f64, dirty_x: ?f64, dirty_y: ?f64, dirty_w: ?f64, dirty_h: ?f64, e: *Execution) !void {
            if (dirty_x != null or dirty_y != null or dirty_w != null or dirty_h != null) {
                if (dirty_x == null or dirty_y == null or dirty_w == null or dirty_h == null) return error.TypeError;
            }
            try check(self);
            const bytes = image._data.local(e.js.local.?).slice();
            if (!std.math.isFinite(x) or !std.math.isFinite(y)) return error.NotSupportedError;
            if (bytes.len != @as(usize, image._width) * image._height * 4) return error.InvalidStateError;
            var left = dirty_x orelse 0;
            var top = dirty_y orelse 0;
            var width = dirty_w orelse @as(f64, @floatFromInt(image._width));
            var height = dirty_h orelse @as(f64, @floatFromInt(image._height));
            if (!std.math.isFinite(left) or !std.math.isFinite(top) or !std.math.isFinite(width) or !std.math.isFinite(height)) return error.NotSupportedError;
            if (width < 0) {
                left += width;
                width = -width;
            }
            if (height < 0) {
                top += height;
                height = -height;
            }
            const x0: u32 = @intFromFloat(@min(@as(f64, @floatFromInt(image._width)), @max(0, left)));
            const y0: u32 = @intFromFloat(@min(@as(f64, @floatFromInt(image._height)), @max(0, top)));
            const x1: u32 = @intFromFloat(@max(0, @min(@as(f64, @floatFromInt(image._width)), left + width)));
            const y1: u32 = @intFromFloat(@max(0, @min(@as(f64, @floatFromInt(image._height)), top + height)));
            if (x1 <= x0 or y1 <= y0) return;
            if (x < @as(f64, @floatFromInt(std.math.minInt(i32))) or x > @as(f64, @floatFromInt(std.math.maxInt(i32))) or y < @as(f64, @floatFromInt(std.math.minInt(i32))) or y > @as(f64, @floatFromInt(std.math.maxInt(i32)))) return;
            try self._drawing.surface.putPixels(bytes, image._width, @intFromFloat(x), @intFromFloat(y), .{ x0, y0, x1, y1 });
        }
        pub fn drawImage(self: *T, source: js.Value, a: f64, b: f64, c: ?f64, d: ?f64, x: ?f64, y: ?f64, g: ?f64, h: ?f64, e: *Execution) !void {
            if ((c == null) != (d == null)) return error.TypeError;
            if (x != null or y != null or g != null or h != null) {
                if (c == null or d == null or x == null or y == null or g == null or h == null) return error.TypeError;
            }
            try check(self);
            const p = try pixelSource(source, e);
            if (p.width == 0 or p.height == 0) return error.InvalidStateError;
            var coords: [8]f64 = if (g != null and h != null) .{ a, b, c orelse return error.TypeError, d orelse return error.TypeError, x orelse return error.TypeError, y orelse return error.TypeError, g.?, h.? } else .{ 0, 0, @floatFromInt(p.width), @floatFromInt(p.height), a, b, c orelse @as(f64, @floatFromInt(p.width)), d orelse @as(f64, @floatFromInt(p.height)) };
            for (coords) |v| if (!std.math.isFinite(v)) return;
            for ([_]usize{ 0, 4 }) |i| {
                if (coords[i + 2] < 0) {
                    coords[i] += coords[i + 2];
                    coords[i + 2] = -coords[i + 2];
                }
                if (coords[i + 3] < 0) {
                    coords[i + 1] += coords[i + 3];
                    coords[i + 3] = -coords[i + 3];
                }
            }
            const state = &self._drawing;
            try state.surface.drawPixels(.{ .alpha = state.alpha, .composite = state.composite, .smooth = state.smooth }, p, &coords, e.local_arena);
        }
        pub const FillInput = union(enum) { path: *Path2D, rule: []const u8 };
        fn fillArgs(input: ?FillInput, rule: ?[]const u8) !struct { path: ?*Path2D, rule: []const u8 } {
            const p = if (input) |v| switch (v) {
                .path => |path| path,
                .rule => null,
            } else null;
            const r_ = if (input) |v| switch (v) {
                .path => rule orelse "nonzero",
                .rule => |value| value,
            } else rule orelse "nonzero";
            if (!std.mem.eql(u8, r_, "nonzero") and !std.mem.eql(u8, r_, "evenodd")) return error.TypeError;
            return .{ .path = p, .rule = r_ };
        }
        pub fn fill(self: *T, input: ?FillInput, rule: ?[]const u8, e: *Execution) !void {
            const a = try fillArgs(input, rule);
            _ = try paint(self, "fill", .{}, false, a.path, a.rule, e.local_arena);
        }
        pub fn stroke(self: *T, path: ?*Path2D, e: *Execution) !void {
            _ = try paint(self, "stroke", .{}, true, path, "nonzero", e.local_arena);
        }
        pub fn clip(self: *T, input: ?FillInput, rule: ?[]const u8, e: *Execution) !void {
            const a = try fillArgs(input, rule);
            _ = try paint(self, "clip", .{}, false, a.path, a.rule, e.local_arena);
        }
        pub const HitInput = union(enum) { path: *Path2D, x: f64 };
        pub const HitRule = union(enum) { number: f64, rule: []const u8 };
        pub fn isPointInPath(self: *T, input: HitInput, y: f64, z: ?HitRule, rule: ?[]const u8, e: *Execution) !bool {
            return hit(self, "isPointInPath", input, y, z, rule, e);
        }
        pub fn isPointInStroke(self: *T, input: HitInput, y: f64, z: ?HitRule, e: *Execution) !bool {
            return hit(self, "isPointInStroke", input, y, z, null, e);
        }
        fn hit(self: *T, method: []const u8, input: HitInput, y: f64, z: ?HitRule, rule: ?[]const u8, e: *Execution) !bool {
            const path = if (input == .path) input.path else null;
            const x = if (input == .x) input.x else y;
            const yy = if (input == .x) y else if (z) |v| switch (v) {
                .number => |n| n,
                .rule => return error.TypeError,
            } else return error.TypeError;
            const rr = if (input == .x) if (z) |v| switch (v) {
                .rule => |r_| r_,
                .number => "nonzero",
            } else "nonzero" else rule orelse "nonzero";
            const result = try paint(self, method, .{ x, yy }, std.mem.eql(u8, method, "isPointInStroke"), path, rr, e.local_arena);
            return std.mem.eql(u8, result, "true");
        }
        pub fn clearRect(self: *T, a0: f64, a1: f64, a2: f64, a3: f64, e: *Execution) !void {
            try draw(self, "clearRect", .{ a0, a1, a2, a3 }, false, e);
        }
        pub fn fillRect(self: *T, a0: f64, a1: f64, a2: f64, a3: f64, e: *Execution) !void {
            try draw(self, "fillRect", .{ a0, a1, a2, a3 }, false, e);
        }
        pub fn strokeRect(self: *T, a0: f64, a1: f64, a2: f64, a3: f64, e: *Execution) !void {
            try draw(self, "strokeRect", .{ a0, a1, a2, a3 }, true, e);
        }
        pub fn beginPath(self: *T, e: *Execution) !void {
            try draw(self, "beginPath", .{}, false, e);
        }
        pub fn closePath(self: *T, e: *Execution) !void {
            try draw(self, "closePath", .{}, false, e);
        }
        pub fn moveTo(self: *T, a0: f64, a1: f64, e: *Execution) !void {
            try draw(self, "moveTo", .{ a0, a1 }, false, e);
        }
        pub fn lineTo(self: *T, a0: f64, a1: f64, e: *Execution) !void {
            try draw(self, "lineTo", .{ a0, a1 }, false, e);
        }
        pub fn quadraticCurveTo(self: *T, a0: f64, a1: f64, a2: f64, a3: f64, e: *Execution) !void {
            try draw(self, "quadraticCurveTo", .{ a0, a1, a2, a3 }, false, e);
        }
        pub fn bezierCurveTo(self: *T, a0: f64, a1: f64, a2: f64, a3: f64, a4: f64, a5: f64, e: *Execution) !void {
            try draw(self, "bezierCurveTo", .{ a0, a1, a2, a3, a4, a5 }, false, e);
        }
        pub fn rect(self: *T, a0: f64, a1: f64, a2: f64, a3: f64, e: *Execution) !void {
            try draw(self, "rect", .{ a0, a1, a2, a3 }, false, e);
        }

        pub fn arc(self: *T, x: f64, y: f64, r: f64, start: f64, end: f64, ccw: ?bool, e: *Execution) !void {
            if (r < 0) return error.IndexSizeError;
            try draw(self, "arc", .{ x, y, r, start, end, ccw orelse false }, false, e);
        }
        pub fn ellipse(self: *T, x: f64, y: f64, rx: f64, ry: f64, rotation: f64, start: f64, end: f64, ccw: ?bool, e: *Execution) !void {
            if (rx < 0 or ry < 0) return error.IndexSizeError;
            try draw(self, "ellipse", .{ x, y, rx, ry, rotation, start, end, ccw orelse false }, false, e);
        }
        pub fn arcTo(self: *T, x1: f64, y1: f64, x2: f64, y2: f64, r: f64, e: *Execution) !void {
            if (r < 0) return error.IndexSizeError;
            try draw(self, "arcTo", .{ x1, y1, x2, y2, r }, false, e);
        }
        pub fn roundRect(self: *T, x: f64, y: f64, w: f64, h: f64, radii: ?Path2D.Radii, e: *Execution) !void {
            var path: Path2D = .{ ._allocator = e.local_arena };
            try path.roundRect(x, y, w, h, radii, e);
            for (path.commands.items) |command| {
                if (std.mem.eql(u8, command.t, "M")) try moveTo(self, command.x, command.y, e) else if (std.mem.eql(u8, command.t, "L")) try lineTo(self, command.x, command.y, e) else if (std.mem.eql(u8, command.t, "C")) try bezierCurveTo(self, command.x1, command.y1, command.x2, command.y2, command.x, command.y, e) else if (std.mem.eql(u8, command.t, "Q")) try quadraticCurveTo(self, command.x1, command.y1, command.x, command.y, e) else try closePath(self, e);
            }
        }
        pub const JsApi = struct {
            pub const bridge = js.Bridge(T);
            pub const Meta = struct {
                pub const name = class_name;
                pub var class_id: bridge.ClassId = undefined;
            };
            pub const canvas = bridge.accessor(ThisContext.getCanvas, null, .{});
            pub const font = bridge.accessor(ThisContext.getFont, ThisContext.setFont, .{});
            pub const fillStyle = bridge.accessor(ThisContext.getFillStyle, ThisContext.setFillStyle, .{});
            pub const strokeStyle = bridge.accessor(ThisContext.getStrokeStyle, ThisContext.setStrokeStyle, .{});
            pub const globalAlpha = bridge.accessor(ThisContext.getAlpha, ThisContext.setAlpha, .{});
            pub const globalCompositeOperation = bridge.accessor(ThisContext.getComposite, ThisContext.setComposite, .{});
            pub const lineWidth = bridge.accessor(ThisContext.getLineWidth, ThisContext.setLineWidth, .{});
            pub const lineCap = bridge.accessor(ThisContext.getCap, ThisContext.setCap, .{});
            pub const lineJoin = bridge.accessor(ThisContext.getJoin, ThisContext.setJoin, .{});
            pub const miterLimit = bridge.accessor(ThisContext.getMiter, ThisContext.setMiter, .{});
            pub const lineDashOffset = bridge.accessor(ThisContext.getDashOffset, ThisContext.setDashOffset, .{});
            pub const textAlign = bridge.accessor(ThisContext.getAlign, ThisContext.setAlign, .{});
            pub const textBaseline = bridge.accessor(ThisContext.getBaseline, ThisContext.setBaseline, .{});
            pub const imageSmoothingEnabled = bridge.accessor(ThisContext.getSmooth, ThisContext.setSmooth, .{});
            pub const clearRect = bridge.function(ThisContext.clearRect, .{});
            pub const fillRect = bridge.function(ThisContext.fillRect, .{});
            pub const strokeRect = bridge.function(ThisContext.strokeRect, .{});
            pub const beginPath = bridge.function(ThisContext.beginPath, .{});
            pub const closePath = bridge.function(ThisContext.closePath, .{});
            pub const moveTo = bridge.function(ThisContext.moveTo, .{});
            pub const lineTo = bridge.function(ThisContext.lineTo, .{});
            pub const quadraticCurveTo = bridge.function(ThisContext.quadraticCurveTo, .{});
            pub const bezierCurveTo = bridge.function(ThisContext.bezierCurveTo, .{});
            pub const rect = bridge.function(ThisContext.rect, .{});
            pub const arc = bridge.function(ThisContext.arc, .{});
            pub const ellipse = bridge.function(ThisContext.ellipse, .{});
            pub const arcTo = bridge.function(ThisContext.arcTo, .{});
            pub const roundRect = bridge.function(ThisContext.roundRect, .{});
            pub const fill = bridge.function(ThisContext.fill, .{});
            pub const stroke = bridge.function(ThisContext.stroke, .{});
            pub const clip = bridge.function(ThisContext.clip, .{});
            pub const isPointInPath = bridge.function(ThisContext.isPointInPath, .{});
            pub const isPointInStroke = bridge.function(ThisContext.isPointInStroke, .{});
            pub const save = bridge.function(ThisContext.save, .{});
            pub const restore = bridge.function(ThisContext.restore, .{});
            pub const reset = bridge.function(ThisContext.reset, .{});
            pub const transform = bridge.function(ThisContext.transform, .{});
            pub const setTransform = bridge.function(ThisContext.setTransform, .{});
            pub const resetTransform = bridge.function(ThisContext.resetTransform, .{});
            pub const getTransform = bridge.function(ThisContext.getTransform, .{});
            pub const scale = bridge.function(ThisContext.scale, .{});
            pub const translate = bridge.function(ThisContext.translate, .{});
            pub const rotate = bridge.function(ThisContext.rotate, .{});
            pub const createLinearGradient = bridge.function(ThisContext.createLinearGradient, .{});
            pub const createRadialGradient = bridge.function(ThisContext.createRadialGradient, .{});
            pub const createConicGradient = bridge.function(ThisContext.createConicGradient, .{});
            pub const createPattern = bridge.function(ThisContext.createPattern, .{});
            pub const getLineDash = bridge.function(ThisContext.getLineDash, .{});
            pub const setLineDash = bridge.function(ThisContext.setLineDash, .{});
            pub const fillText = bridge.function(ThisContext.fillText, .{});
            pub const strokeText = bridge.function(ThisContext.strokeText, .{});
            pub const measureText = bridge.function(ThisContext.measureText, .{});
            pub const createImageData = bridge.function(ThisContext.createImageData, .{});
            pub const getImageData = bridge.function(ThisContext.getImageData, .{});
            pub const putImageData = bridge.function(ThisContext.putImageData, .{});
            pub const drawImage = bridge.function(ThisContext.drawImage, .{});
        };
    };
}

extern "c" fn lp_canvas_new(u32, u32, [*]const u8, usize) ?*Backend;
extern "c" fn lp_canvas_free(*Backend) void;
extern "c" fn lp_canvas_resize(*Backend, u32, u32) bool;
extern "c" fn lp_canvas_command(*Backend, [*]const u8, usize, *anyopaque, *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) bool;
extern "c" fn lp_canvas_pixels(*Backend, [*]u8, usize) bool;
extern "c" fn lp_canvas_png(*Backend, *anyopaque, *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool) bool;
extern "c" fn lp_canvas_put(*Backend, [*]const u8, u32, u32, i32, i32) bool;
extern "c" fn lp_canvas_draw_pixels(*Backend, [*]const u8, u32, u32, *const [8]f64) bool;
extern "c" fn lp_canvas_draw_encoded(*Backend, [*]const u8, usize, *const [8]f64) bool;

test "Surface snapshots stay coherent during resize and cross-thread retain" {
    const fonts = try std.heap.page_allocator.dupe(u8, "[]");
    const s = std.heap.page_allocator.create(Surface) catch |err| {
        std.heap.page_allocator.free(fonts);
        return err;
    };
    s.* = .{ .fonts = fonts, ._width = 1, ._height = 2 };
    defer s.release();
    var failed: std.atomic.Value(bool) = .init(false);
    const Concurrent = struct {
        fn resizeLoop(surface: *Surface, failure: *std.atomic.Value(bool)) void {
            for (0..256) |i| {
                surface.retain();
                const width: u32 = @intCast(i % 8 + 1);
                surface.resize(width, width + 1) catch {
                    failure.store(true, .release);
                };
                surface.release();
            }
        }
        fn snapshotLoop(surface: *Surface, failure: *std.atomic.Value(bool)) void {
            for (0..256) |_| {
                surface.retain();
                defer surface.release();
                const image = surface.snapshot(std.heap.page_allocator) catch {
                    failure.store(true, .release);
                    return;
                };
                defer std.heap.page_allocator.free(image.bytes);
                if (image.height != image.width + 1 or image.bytes.len != @as(usize, image.width) * image.height * 4) failure.store(true, .release);
                if (!image.origin_clean) failure.store(true, .release);
            }
        }
    };
    const writer = try std.Thread.spawn(.{}, Concurrent.resizeLoop, .{ s, &failed });
    const reader = std.Thread.spawn(.{}, Concurrent.snapshotLoop, .{ s, &failed }) catch |err| {
        writer.join();
        return err;
    };
    writer.join();
    reader.join();
    try std.testing.expect(!failed.load(.acquire));
    try std.testing.expectEqual(@as(usize, 1), s.refs.load(.acquire));
}

test "Surface observed writer starts before mutation and ends on failure" {
    var page: @import("../../Page.zig") = undefined;
    page.canvas_epoch = .init(0);
    page.canvas_writers = .init(0);
    page.visual_generation = .init(0);
    var frame: @import("../../Frame.zig") = undefined;
    frame.page = &page;
    frame._render_resource_version = .init(0);
    var surface: Surface = .{ .fonts = "[]", ._width = 1, ._height = 1, ._observer = &frame };
    const Failure = struct {
        fn mutate(s: *Surface, owner: *@import("../../Page.zig")) !void {
            s.lock();
            defer s.unlock();
            const write = s.beginWriteLocked();
            defer write.end();
            try testing.expectEqual(@as(u64, 1), owner.canvas_writers.load(.acquire));
            try testing.expectEqual(@as(u64, 1), owner.canvas_epoch.load(.acquire));
            s._width = 2;
            return error.InvalidStateError;
        }
    };
    try testing.expectError(error.InvalidStateError, Failure.mutate(&surface, &page));
    try testing.expectEqual(@as(u64, 0), page.canvas_writers.load(.acquire));
    try testing.expectEqual(@as(u64, 2), page.canvas_epoch.load(.acquire));
    try surface.resize(3, 4);
    try testing.expectEqual(@as(u64, 0), page.canvas_writers.load(.acquire));
    try testing.expectEqual(@as(u64, 4), page.canvas_epoch.load(.acquire));
    try testing.expectEqual(@as(u32, 3), surface.size().width);
    try testing.expectEqual(@as(u32, 4), surface.size().height);
    try testing.expectEqual(@as(u64, 1), frame._render_resource_version.load(.acquire));
}

test "Surface observed GL retains one writer until real GPU completion" {
    var page: @import("../../Page.zig") = undefined;
    page.canvas_epoch = .init(0);
    page.canvas_writers = .init(0);
    page.visual_generation = .init(0);
    var frame: @import("../../Frame.zig") = undefined;
    frame.page = &page;
    frame._render_resource_version = .init(0);
    var surface: Surface = .{ .fonts = "[]", ._width = 1, ._height = 1, ._observer = &frame };
    _ = (try surface.initializeGl()) orelse return error.SkipZigTest;
    defer {
        surface.observe(null);
        Surface.GL.release(surface._backing.gl);
    }
    try testing.expectEqual(@as(u64, 1), page.canvas_writers.load(.acquire));
    for (0..2) |_| {
        const access = try surface.beginGlAccess(true);
        defer access.end();
        try testing.expectEqual(@as(u64, 2), page.canvas_writers.load(.acquire));
        try Surface.GL.clearBitmap(access.backend);
    }
    try testing.expectEqual(@as(u64, 1), page.canvas_writers.load(.acquire));
    try surface.settleObservedWrites();
    try testing.expectEqual(@as(u64, 0), page.canvas_writers.load(.acquire));
    const settled_epoch = page.canvas_epoch.load(.acquire);
    try surface.settleObservedWrites();
    try testing.expectEqual(settled_epoch, page.canvas_epoch.load(.acquire));
    try surface.resize(2, 2);
    try testing.expectEqual(@as(u64, 1), page.canvas_writers.load(.acquire));
    surface.detachObserver(&frame);
    try testing.expectEqual(@as(u64, 0), page.canvas_writers.load(.acquire));
}
