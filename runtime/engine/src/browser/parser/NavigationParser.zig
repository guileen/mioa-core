// Copyright (C) 2026 Lightpanda (Selecy SAS)
// SPDX-License-Identifier: AGPL-3.0-or-later

const std = @import("std");
const Frame = @import("../Frame.zig");
const Mime = @import("../Mime.zig");
const Parser = @import("Parser.zig");
const h5e = @import("html5ever.zig");
const NavigationParser = @This();

// Allocated in the navigation arena before start(): html5ever retains this
// parser's address across network callbacks. Never retains borrowed HTTP bytes.
parser: Parser.Streaming,
allocator: std.mem.Allocator,
prefix: std.ArrayList(u8) = .empty,
decoded: std.ArrayList(u8) = .empty,
decoder: ?*anyopaque = null,
sniff_charset: bool,
started: bool = false,

pub fn init(allocator: std.mem.Allocator, frame: *Frame, sniff_charset: bool) NavigationParser {
    return .{
        .parser = Parser.Streaming.init(allocator, frame.document.asNode(), frame, .{ .allow_declarative_shadow = true }),
        .allocator = allocator,
        .sniff_charset = sniff_charset,
    };
}

pub fn deinit(self: *NavigationParser) void {
    self.parser.deinit();
    if (self.decoder) |decoder| {
        h5e.encoding_decoder_free(decoder);
        self.decoder = null;
    }
}

pub fn read(self: *NavigationParser, data: []const u8) !void {
    if (self.started) return self.feed(data, false);
    // Explicit encodings need only BOM lookahead; undecided encodings retain at
    // most the HTML prescan window, not the rest of a slow response.
    var rest = data;
    const limit: usize = if (self.sniff_charset) 1024 else 3;
    const count = @min(limit - self.prefix.items.len, rest.len);
    try self.prefix.appendSlice(self.allocator, rest[0..count]);
    rest = rest[count..];
    if (try self.start(false)) {
        try self.feed(rest, false);
    }
}

pub fn done(self: *NavigationParser) !void {
    _ = try self.start(true);
    try self.feed("", true);
    try self.parser.done();
}

fn start(self: *NavigationParser, last: bool) !bool {
    if (self.started) return true;
    const bytes = self.prefix.items;
    if (!last and bytes.len < 3) return false;
    var skip: usize = 0;
    const frame = self.parser.parser.frame;
    if (std.mem.startsWith(u8, bytes, "\xEF\xBB\xBF")) {
        frame.charset = "UTF-8";
        skip = 3;
    } else if (std.mem.startsWith(u8, bytes, "\xFF\xFE")) {
        frame.charset = "UTF-16LE";
        skip = 2;
    } else if (std.mem.startsWith(u8, bytes, "\xFE\xFF")) {
        frame.charset = "UTF-16BE";
        skip = 2;
    } else if (self.sniff_charset) {
        if (Mime.prescanCharset(bytes)) |label| {
            if (h5e.EncodingInfo.forLabel(label)) |info| {
                frame.charset = if (info.isUtf16()) "UTF-8" else info.name();
            }
        } else if (!last and bytes.len < 1024) {
            return false;
        }
    }
    try self.parser.start();
    self.started = true;
    try self.feed(bytes[skip..], false);
    self.prefix.deinit(self.allocator);
    self.prefix = .empty;
    return true;
}

fn feed(self: *NavigationParser, data: []const u8, last: bool) !void {
    const frame = self.parser.parser.frame;
    if (self.decoder == null) {
        // Common UTF-8 chunks need neither a decoding buffer nor a copy. Once
        // a split/invalid sequence occurs, keep the decoder through EOF.
        if (std.mem.eql(u8, frame.charset, "UTF-8") and std.unicode.utf8ValidateSlice(data)) {
            return self.parser.read(data);
        }
        const encoding = h5e.EncodingInfo.forLabel(frame.charset) orelse h5e.EncodingInfo.utf8();
        self.decoder = h5e.encoding_decoder_new(encoding.handle.?) orelse return error.OutOfMemory;
    }
    // Three UTF-8 bytes per source byte plus decoder carry covers all supported
    // encodings. Reuse capacity across chunks; do not retain decoded pages.
    const capacity = try std.math.add(usize, try std.math.mul(usize, data.len, 3), 16);
    try self.decoded.resize(self.allocator, capacity);
    const result = h5e.encoding_decoder_decode(
        self.decoder.?,
        data.ptr,
        data.len,
        self.decoded.items.ptr,
        self.decoded.items.len,
        @intFromBool(last),
    );
    if (result.bytes_read != data.len) return error.IncompleteDecode;
    try self.parser.read(self.decoded.items[0..result.bytes_written]);
}
