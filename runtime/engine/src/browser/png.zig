//! Straight RGBA8 canvas PNG encoding. The provider is selected at compile time;
//! the Zig provider also supplies the C symbol used by Rust's canvas backend.
const std = @import("std");
const build_config = @import("build_config");
const flate = std.compress.flate;

pub const Write = *const fn (*anyopaque, [*]const u8, usize) callconv(.c) bool;
pub const Error = error{ InvalidDimensions, InvalidRgbaLength, OutOfMemory };
const signature = "\x89PNG\r\n\x1a\n";
// Match the existing raster ABI's capacity limit, including callers outside Zig.
const max_rgba_bytes = 268435456;

fn pixelLength(width: u32, height: u32) Error!usize {
    if (width == 0 or height == 0 or width > 0x7fffffff or height > 0x7fffffff)
        return error.InvalidDimensions;
    const pixels = std.math.mul(usize, width, height) catch return error.InvalidDimensions;
    const length = std.math.mul(usize, pixels, 4) catch return error.InvalidDimensions;
    if (length > max_rgba_bytes) return error.InvalidDimensions;
    return length;
}

/// Returns an allocator-owned PNG8 RGBA image without premultiplying or copying
/// the input image. Rows stream directly into the standard zlib compressor's
/// history buffer; only the required PNG filter byte is added to each row.
pub fn encode(allocator: std.mem.Allocator, rgba: []const u8, width: u32, height: u32) Error![]u8 {
    const length = try pixelLength(width, height);
    if (rgba.len != length) return error.InvalidRgbaLength;
    var output = try std.Io.Writer.Allocating.initCapacity(allocator, 4096);
    defer output.deinit();
    // Keep the compressor's large lookup tables off callers' stacks.
    const compressor = try allocator.create(flate.Compress);
    defer allocator.destroy(compressor);
    const history = try allocator.alloc(u8, flate.max_window_len);
    defer allocator.free(history);
    encodeInto(&output, compressor, history, rgba, width, height) catch return error.OutOfMemory;
    return output.toOwnedSlice();
}

fn chunk(writer: *std.Io.Writer, kind: *const [4]u8, data: []const u8) std.Io.Writer.Error!void {
    try writer.writeInt(u32, @intCast(data.len), .big);
    try writer.writeAll(kind);
    try writer.writeAll(data);
    var crc = std.hash.Crc32.init();
    crc.update(kind);
    crc.update(data);
    try writer.writeInt(u32, crc.final(), .big);
}

fn encodeInto(output: *std.Io.Writer.Allocating, compressor: *flate.Compress, history: []u8, rgba: []const u8, width: u32, height: u32) std.Io.Writer.Error!void {
    const writer = &output.writer;
    try writer.writeAll(signature);
    var header: [13]u8 = @splat(0);
    std.mem.writeInt(u32, header[0..4], width, .big);
    std.mem.writeInt(u32, header[4..8], height, .big);
    header[8] = 8; // bit depth
    header[9] = 6; // truecolor with alpha; compression/filter/interlace stay zero
    try chunk(writer, "IHDR", &header);
    const idat_offset = output.written().len;
    try writer.writeAll("\x00\x00\x00\x00IDAT");
    compressor.* = try .init(writer, history, .zlib, .default);
    const row_length = @as(usize, width) * 4;
    var offset: usize = 0;
    for (0..height) |_| {
        try compressor.writer.writeByte(0); // PNG None filter
        try compressor.writer.writeAll(rgba[offset..][0..row_length]);
        offset += row_length;
    }
    try compressor.finish();
    // The 256 MiB input limit keeps even incompressible DEFLATE below PNG's
    // 2^31-1 chunk limit. Reserve the checksum before taking this slice because
    // growing the allocating writer may invalidate it.
    try writer.ensureUnusedCapacity(4);
    const bytes = output.written();
    std.mem.writeInt(u32, bytes[idat_offset..][0..4], @intCast(bytes.len - idat_offset - 8), .big);
    const crc = std.hash.Crc32.hash(bytes[idat_offset + 4 ..]);
    try writer.writeInt(u32, crc, .big);
    try chunk(writer, "IEND", &.{});
}

const Rust = struct {
    extern "c" fn lp_rgba_png(?[*]const u8, u32, u32, *anyopaque, Write) bool;
};

/// Callback is invoked once, only after successful complete encoding; its false
/// return is propagated. The C ABI cannot carry an allocator, so use the same
/// process allocator as the other native canvas scratch buffers.
pub fn write(rgba: [*]const u8, width: u32, height: u32, context: *anyopaque, callback: Write) bool {
    return switch (build_config.png_provider) {
        .rust => Rust.lp_rgba_png(rgba, width, height, context, callback),
        .zig => rgbaPng(rgba, width, height, context, callback),
    };
}

// Set once by native heap startup, before renderer or V8 threads can encode.
// Standalone component probes retain their ordinary page allocator.
pub var native_allocator: std.mem.Allocator = std.heap.page_allocator;

fn rgbaPng(rgba: ?[*]const u8, width: u32, height: u32, context: *anyopaque, callback: Write) callconv(.c) bool {
    return writeWithAllocator(native_allocator, rgba, width, height, context, callback);
}

fn writeWithAllocator(allocator: std.mem.Allocator, rgba: ?[*]const u8, width: u32, height: u32, context: *anyopaque, callback: Write) bool {
    const length = pixelLength(width, height) catch return false;
    const pointer = rgba orelse return false;
    const bytes = encode(allocator, pointer[0..length], width, height) catch return false;
    defer allocator.free(bytes);
    return callback(context, bytes.ptr, bytes.len);
}

comptime {
    if (build_config.png_provider == .zig) @export(&rgbaPng, .{ .name = "lp_rgba_png" });
}

// This parser deliberately checks framing/CRC separately from encoder helpers;
// a separate DEFLATE decoder and reference Adler calculation check the payload.
fn expectImage(png: []const u8, width: u32, height: u32, rgba: []const u8) !void {
    const testing = std.testing;
    try testing.expectEqualSlices(u8, signature, png[0..8]);
    var offset: usize = 8;
    var seen_header = false;
    var seen_end = false;
    var compressed: std.Io.Writer.Allocating = .init(testing.allocator);
    defer compressed.deinit();
    while (offset < png.len) {
        try testing.expect(png.len - offset >= 12);
        const length = std.mem.readInt(u32, png[offset..][0..4], .big);
        try testing.expect(length <= png.len - offset - 12);
        const kind = png[offset + 4 ..][0..4];
        const data = png[offset + 8 ..][0..length];
        // Bitwise reference CRC avoids sharing the encoder's table/hardware path.
        var crc: u32 = 0xffffffff;
        for (png[offset + 4 ..][0 .. length + 4]) |byte| {
            crc ^= byte;
            for (0..8) |_| crc = (crc >> 1) ^ (if (crc & 1 != 0) @as(u32, 0xedb88320) else 0);
        }
        try testing.expectEqual(~crc, std.mem.readInt(u32, png[offset + 8 + length ..][0..4], .big));
        if (std.mem.eql(u8, kind, "IHDR")) {
            try testing.expect(!seen_header and offset == 8);
            try testing.expectEqual(13, data.len);
            try testing.expectEqual(width, std.mem.readInt(u32, data[0..4], .big));
            try testing.expectEqual(height, std.mem.readInt(u32, data[4..8], .big));
            try testing.expectEqualSlices(u8, &.{ 8, 6, 0, 0, 0 }, data[8..]);
            seen_header = true;
        } else if (std.mem.eql(u8, kind, "IDAT")) {
            try testing.expect(seen_header and !seen_end);
            try compressed.writer.writeAll(data);
        } else if (std.mem.eql(u8, kind, "IEND")) {
            try testing.expect(!seen_end);
            try testing.expectEqual(0, data.len);
            try testing.expectEqual(png.len, offset + 12);
            seen_end = true;
        } else {
            try testing.expect(kind[0] & 32 != 0); // unknown critical chunk
        }
        offset += length + 12;
    }
    try testing.expect(seen_header and seen_end and compressed.written().len >= 6);
    const idat = compressed.written();
    var input: std.Io.Reader = .fixed(idat);
    var history: [flate.max_window_len]u8 = undefined;
    var decompressor: flate.Decompress = .init(&input, .zlib, &history);
    const row_length = @as(usize, width) * 4;
    const row = try testing.allocator.alloc(u8, row_length + 1);
    defer testing.allocator.free(row);
    const pixels = try testing.allocator.alloc(u8, rgba.len);
    defer testing.allocator.free(pixels);
    var a: u32 = 1;
    var b: u32 = 0;
    for (0..height) |y| {
        try decompressor.reader.readSliceAll(row);
        try testing.expect(row[0] <= 4);
        // The standard decoder exposes but does not validate the zlib checksum.
        for (row) |byte| {
            a = (a + byte) % 65521;
            b = (b + a) % 65521;
        }
        for (row[1..], 0..) |byte, x| {
            const i = y * row_length + x;
            const left: u8 = if (x >= 4) pixels[i - 4] else 0;
            const above: u8 = if (y != 0) pixels[i - row_length] else 0;
            const upper_left: u8 = if (y != 0 and x >= 4) pixels[i - row_length - 4] else 0;
            const prediction: u8 = switch (row[0]) {
                0 => 0,
                1 => left,
                2 => above,
                3 => @intCast((@as(u16, left) + above) / 2),
                4 => paeth(left, above, upper_left),
                else => unreachable,
            };
            pixels[i] = byte +% prediction;
        }
    }
    try testing.expectEqualSlices(u8, rgba, pixels);
    try testing.expectError(error.EndOfStream, decompressor.reader.takeByte());
    try testing.expectEqual(null, decompressor.err);
    try testing.expectEqual((b << 16) | a, std.mem.readInt(u32, idat[idat.len - 4 ..][0..4], .big));
}

fn paeth(left: u8, above: u8, upper_left: u8) u8 {
    const p = @as(i16, left) + above - upper_left;
    const dl = @abs(p - left);
    const da = @abs(p - above);
    const du = @abs(p - upper_left);
    return if (dl <= da and dl <= du) left else if (da <= du) above else upper_left;
}

test "PNG preserves straight alpha, row boundaries, dimensions and checksums" {
    const pixels = [_]u8{
        255, 0, 17, 255, 32,  200, 64,  128, 123, 45, 67, 0,
        1,   2, 3,  1,   255, 254, 253, 254, 9,   8,  7,  64,
    };
    const png = try encode(std.testing.allocator, &pixels, 3, 2);
    defer std.testing.allocator.free(png);
    try expectImage(png, 3, 2, &pixels);
    const single = try encode(std.testing.allocator, pixels[0..4], 1, 1);
    defer std.testing.allocator.free(single);
    try expectImage(single, 1, 1, pixels[0..4]);
}

test "PNG compression remains effective across multiple history windows" {
    const pixels = try std.testing.allocator.alloc(u8, 257 * 131 * 4);
    defer std.testing.allocator.free(pixels);
    for (pixels, 0..) |*byte, i| byte.* = @intCast(i % 251);
    const png = try encode(std.testing.allocator, pixels, 257, 131);
    defer std.testing.allocator.free(png);
    try std.testing.expect(png.len < pixels.len / 4);
    try expectImage(png, 257, 131, pixels);
}

test "PNG rejects empty, oversized and mismatched RGBA inputs before allocating" {
    const allocator = std.testing.failing_allocator;
    try std.testing.expectError(error.InvalidDimensions, encode(allocator, &.{}, 0, 1));
    try std.testing.expectError(error.InvalidDimensions, encode(allocator, &.{}, 1, 0));
    try std.testing.expectError(error.InvalidDimensions, encode(allocator, &.{}, 0xffffffff, 0xffffffff));
    try std.testing.expectError(error.InvalidDimensions, encode(allocator, &.{}, 67108865, 1));
    try std.testing.expectError(error.InvalidRgbaLength, encode(allocator, &.{}, 67108864, 1));
    try std.testing.expectError(error.InvalidRgbaLength, encode(allocator, &.{ 1, 2, 3 }, 1, 1));
    try std.testing.expectError(error.InvalidRgbaLength, encode(allocator, &.{ 1, 2, 3, 4, 5 }, 1, 1));
}

fn allocationFailures(allocator: std.mem.Allocator) !void {
    // Incompressible data makes the allocating output grow beyond its initial
    // capacity, covering both compression writes and final ownership transfer.
    var pixels: [97 * 41 * 4]u8 = undefined;
    var random = std.Random.DefaultPrng.init(0x504e47);
    random.random().bytes(&pixels);
    const png = try encode(allocator, &pixels, 97, 41);
    defer allocator.free(png);
}

test "PNG propagates every allocation failure without leaking" {
    try std.testing.checkAllAllocationFailures(std.testing.allocator, allocationFailures, .{});
}

const CallbackProbe = struct {
    called: bool = false,
    accept: bool = true,
    valid: bool = false,
    fn callback(context: *anyopaque, bytes: [*]const u8, length: usize) callconv(.c) bool {
        const self: *CallbackProbe = @ptrCast(@alignCast(context));
        self.called = true;
        self.valid = length > 8 and std.mem.eql(u8, bytes[0..8], signature);
        return self.accept;
    }
};

test "PNG callback ABI propagates rejection and encoding errors" {
    const rgba = [_]u8{ 42, 43, 44, 128 };
    var probe: CallbackProbe = .{};
    try std.testing.expect(rgbaPng(&rgba, 1, 1, &probe, CallbackProbe.callback));
    try std.testing.expect(probe.valid);
    probe = .{ .accept = false };
    try std.testing.expect(!rgbaPng(&rgba, 1, 1, &probe, CallbackProbe.callback));
    try std.testing.expect(probe.valid);
    probe = .{};
    try std.testing.expect(!rgbaPng(null, 1, 1, &probe, CallbackProbe.callback));
    try std.testing.expect(!rgbaPng(&rgba, 0, 1, &probe, CallbackProbe.callback));
    try std.testing.expect(!rgbaPng(&rgba, 0xffffffff, 0xffffffff, &probe, CallbackProbe.callback));
    try std.testing.expect(!writeWithAllocator(std.testing.failing_allocator, &rgba, 1, 1, &probe, CallbackProbe.callback));
    try std.testing.expect(!probe.called);
}
