const std = @import("std");

test {
    std.testing.refAllDecls(@import("browser/entities.zig"));
    std.testing.refAllDecls(@import("browser/png.zig"));
}
