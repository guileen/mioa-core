//! Small libc file adapter, deliberately independent of the browser and V8.
const std = @import("std");
extern "c" fn fopen([*:0]const u8, [*:0]const u8) ?*anyopaque;
extern "c" fn fclose(*anyopaque) c_int;
extern "c" fn fseek(*anyopaque, c_long, c_int) c_int;
extern "c" fn ftell(*anyopaque) c_long;
extern "c" fn fread([*]u8, usize, usize, *anyopaque) usize;
extern "c" fn fwrite([*]const u8, usize, usize, *anyopaque) usize;
pub extern "c" fn printf([*:0]const u8, ...) c_int;

pub fn readFile(allocator: std.mem.Allocator, path: []const u8, maximum: usize) ![]u8 {
    const z = try allocator.dupeSentinel(u8, path, 0);
    defer allocator.free(z);
    const file = fopen(z, "rb") orelse return error.OpenFailed;
    defer _ = fclose(file);
    if (fseek(file, 0, 2) != 0) return error.SeekFailed;
    const signed_length = ftell(file);
    if (signed_length < 0) return error.SeekFailed;
    const length: usize = @intCast(signed_length);
    if (length > maximum) return error.InputTooLarge;
    if (fseek(file, 0, 0) != 0) return error.SeekFailed;
    const bytes = try allocator.alloc(u8, length);
    errdefer allocator.free(bytes);
    if (fread(bytes.ptr, 1, bytes.len, file) != bytes.len) return error.ReadFailed;
    return bytes;
}

pub fn writeFile(allocator: std.mem.Allocator, path: []const u8, bytes: []const u8) !void {
    const z = try allocator.dupeSentinel(u8, path, 0);
    defer allocator.free(z);
    const file = fopen(z, "wb") orelse return error.OpenFailed;
    const count = fwrite(bytes.ptr, 1, bytes.len, file);
    const closed = fclose(file);
    if (count != bytes.len or closed != 0) return error.WriteFailed;
}
