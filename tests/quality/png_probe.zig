const std = @import("std");
const png = @import("browser/png.zig");
const io = @import("probe_io.zig");

pub fn main(init: std.process.Init) !void {
    var args = std.process.Args.Iterator.init(init.minimal.args);
    _ = args.next();
    const width_text = args.next() orelse return error.Usage;
    const height_text = args.next() orelse return error.Usage;
    const input = args.next() orelse return error.Usage;
    const output = args.next() orelse return error.Usage;
    if (args.next() != null) return error.Usage;
    const width = try std.fmt.parseInt(u32, width_text, 10);
    const height = try std.fmt.parseInt(u32, height_text, 10);
    const allocator = std.heap.page_allocator;
    const rgba = try io.readFile(allocator, input, 268435456);
    defer allocator.free(rgba);
    const encoded = try png.encode(allocator, rgba, width, height);
    defer allocator.free(encoded);
    try io.writeFile(allocator, output, encoded);
}
