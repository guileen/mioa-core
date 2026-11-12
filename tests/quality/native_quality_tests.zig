const std = @import("std");
const png = @import("browser/png.zig");

// Force discovery even when --test-filter excludes the unnamed root test.
comptime {
    _ = @import("native_tests.zig");
    _ = @import("browser/entities.zig");
    _ = @import("browser/png.zig");
}

// Import the maintained test root, rather than copying its assertions here.
test {
    std.testing.refAllDecls(@import("native_tests.zig"));
}

const Publication = struct {
    calls: usize = 0,
    accept: bool = true,
    valid: bool = false,
    fn receive(context: *anyopaque, bytes: [*]const u8, length: usize) callconv(.c) bool {
        const self: *Publication = @ptrCast(@alignCast(context));
        self.calls += 1;
        self.valid = length >= 20 and std.mem.eql(u8, bytes[0..8], "\x89PNG\r\n\x1a\n") and
            std.mem.eql(u8, bytes[length - 8 ..][0..4], "IEND");
        return self.accept;
    }
};

test "quality: public PNG callback publishes one complete image or nothing" {
    const pixels = [_]u8{ 11, 29, 87, 0, 101, 7, 33, 128 };
    var publication: Publication = .{};
    try std.testing.expect(png.write(&pixels, 2, 1, &publication, Publication.receive));
    try std.testing.expectEqual(@as(usize, 1), publication.calls);
    try std.testing.expect(publication.valid);
    publication = .{ .accept = false };
    try std.testing.expect(!png.write(&pixels, 2, 1, &publication, Publication.receive));
    try std.testing.expectEqual(@as(usize, 1), publication.calls);
    try std.testing.expect(publication.valid);
    publication = .{};
    try std.testing.expect(!png.write(&pixels, 0, 1, &publication, Publication.receive));
    try std.testing.expectEqual(@as(usize, 0), publication.calls);
}
