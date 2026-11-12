const std = @import("std");

var io_threaded: std.Io.Threaded = .init_single_threaded;
pub const io: std.Io = io_threaded.io();
