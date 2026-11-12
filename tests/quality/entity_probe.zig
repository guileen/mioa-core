const std = @import("std");
const entities = @import("browser/entities.zig");
const entity_data = @import("browser/entities_data.zig");
const io = @import("probe_io.zig");

pub fn main(init: std.process.Init) !void {
    var args = std.process.Args.Iterator.init(init.minimal.args);
    _ = args.next();
    const path = args.next() orelse return error.Usage;
    if (args.next() != null) return error.Usage;
    if (std.mem.eql(u8, path, "--table-count")) {
        if (io.printf("{\"entries\":%zu}\n", @as(usize, entity_data.entries.len)) < 0) return error.WriteFailed;
        return;
    }
    const allocator = std.heap.page_allocator;
    const source = try io.readFile(allocator, path, 16 * 1024 * 1024);
    defer allocator.free(source);
    const parsed = try std.json.parseFromSlice([]const []const u8, allocator, source, .{});
    defer parsed.deinit();
    if (io.printf("[") < 0) return error.WriteFailed;
    for (parsed.value, 0..) |name, i| {
        var out: [2]u32 = .{ 0xDEADBEEF, 0xCAFEBABE };
        const status = entities.lookup(name, &out);
        if (io.printf("%s{\"status\":%u,\"codepoints\":[%u,%u]}", @as([*:0]const u8, if (i == 0) "" else ","), @as(c_uint, status), @as(c_uint, out[0]), @as(c_uint, out[1])) < 0) return error.WriteFailed;
    }
    if (io.printf("]\n") < 0) return error.WriteFailed;
}
