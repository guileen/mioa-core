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

// A preload scanner: a lightweight tokenizer-only pass over a document that
// reports fetchable script resources, so downloads can start before the tree
// builder reaches them. Purely a hint source — builds no tree.
// Native Zig implementation replacing legacy Rust prescan.rs.

const std = @import("std");
const entities = @import("../entities.zig");

pub const PrescanResource = enum(u32) {
    script = 0,
    module = 1,
    base = 2,
    _,
};

pub const PrescanCallback = *const fn (ctx: *anyopaque, kind: PrescanResource, url: [*c]const u8, url_len: usize) callconv(.c) void;

fn isTagWhitespace(c: u8) bool {
    return c == ' ' or c == '\t' or c == '\n' or c == '\r' or c == 0x0C;
}

fn skipUntilEndTag(html: []const u8, idx: *usize, comptime tag: []const u8) void {
    var i = idx.*;
    while (i < html.len) {
        if (html[i] == '<' and i + 1 < html.len and html[i + 1] == '/') {
            const after_slash = i + 2;
            if (after_slash + tag.len <= html.len) {
                if (std.ascii.eqlIgnoreCase(html[after_slash .. after_slash + tag.len], tag)) {
                    const next_idx = after_slash + tag.len;
                    if (next_idx == html.len or html[next_idx] == '>' or html[next_idx] == '/' or isTagWhitespace(html[next_idx])) {
                        var close_idx = next_idx;
                        while (close_idx < html.len and html[close_idx] != '>') {
                            close_idx += 1;
                        }
                        if (close_idx < html.len and html[close_idx] == '>') {
                            close_idx += 1;
                        }
                        idx.* = close_idx;
                        return;
                    }
                }
            }
        }
        i += 1;
    }
    idx.* = html.len;
}

fn unescapeEntities(input: []const u8, out: []u8) []const u8 {
    var in_idx: usize = 0;
    var out_idx: usize = 0;
    while (in_idx < input.len and out_idx < out.len) {
        if (input[in_idx] == '&') {
            const rest = input[in_idx + 1 ..];
            if (rest.len > 0 and rest[0] == '#') {
                if (rest.len > 1 and (rest[1] == 'x' or rest[1] == 'X')) {
                    var hex_end: usize = 2;
                    while (hex_end < rest.len and hex_end < 10 and rest[hex_end] != ';') : (hex_end += 1) {}
                    if (hex_end < rest.len and rest[hex_end] == ';') {
                        if (std.fmt.parseInt(u21, rest[2..hex_end], 16)) |cp| {
                            var utf8_buf: [4]u8 = undefined;
                            const len = std.unicode.utf8Encode(cp, &utf8_buf) catch 0;
                            if (len > 0 and out_idx + len <= out.len) {
                                @memcpy(out[out_idx .. out_idx + len], utf8_buf[0..len]);
                                out_idx += len;
                                in_idx += 1 + hex_end + 1;
                                continue;
                            }
                        } else |_| {}
                    }
                } else {
                    var dec_end: usize = 1;
                    while (dec_end < rest.len and dec_end < 10 and rest[dec_end] != ';') : (dec_end += 1) {}
                    if (dec_end < rest.len and rest[dec_end] == ';') {
                        if (std.fmt.parseInt(u21, rest[1..dec_end], 10)) |cp| {
                            var utf8_buf: [4]u8 = undefined;
                            const len = std.unicode.utf8Encode(cp, &utf8_buf) catch 0;
                            if (len > 0 and out_idx + len <= out.len) {
                                @memcpy(out[out_idx .. out_idx + len], utf8_buf[0..len]);
                                out_idx += len;
                                in_idx += 1 + dec_end + 1;
                                continue;
                            }
                        } else |_| {}
                    }
                }
            } else {
                var semi_idx: usize = 0;
                while (semi_idx < rest.len and semi_idx < 32 and rest[semi_idx] != ';') : (semi_idx += 1) {}
                if (semi_idx < rest.len and rest[semi_idx] == ';') {
                    const candidate_name = rest[0 .. semi_idx + 1];
                    var codepoints: [2]u32 = undefined;
                    if (entities.lookup(candidate_name, &codepoints) == 2) {
                        var utf8_buf: [8]u8 = undefined;
                        var ulen: usize = 0;
                        if (std.unicode.utf8Encode(@intCast(codepoints[0]), utf8_buf[0..4])) |l1| {
                            ulen += l1;
                            if (codepoints[1] != 0) {
                                if (std.unicode.utf8Encode(@intCast(codepoints[1]), utf8_buf[ulen .. ulen + 4])) |l2| {
                                    ulen += l2;
                                } else |_| {}
                            }
                            if (out_idx + ulen <= out.len) {
                                @memcpy(out[out_idx .. out_idx + ulen], utf8_buf[0..ulen]);
                                out_idx += ulen;
                                in_idx += 1 + semi_idx + 1;
                                continue;
                            }
                        } else |_| {}
                    }
                }
            }
        }
        out[out_idx] = input[in_idx];
        out_idx += 1;
        in_idx += 1;
    }
    return out[0..out_idx];
}

fn emitResource(ctx: *anyopaque, callback: PrescanCallback, kind: PrescanResource, raw_url: []const u8) void {
    if (std.mem.indexOfScalar(u8, raw_url, '&') == null) {
        callback(ctx, kind, raw_url.ptr, raw_url.len);
        return;
    }
    var buf: [2048]u8 = undefined;
    const decoded = unescapeEntities(raw_url, &buf);
    callback(ctx, kind, decoded.ptr, decoded.len);
}

pub fn prescan(html: []const u8, charset: []const u8, ctx: *anyopaque, callback: PrescanCallback) void {
    _ = charset;
    if (html.len == 0) return;

    var base_seen = false;
    var template_depth: u32 = 0;
    var i: usize = 0;

    while (i < html.len) {
        // Fast-forward to next '<'
        while (i < html.len and html[i] != '<') : (i += 1) {}
        if (i >= html.len) break;
        if (i + 1 >= html.len) break;

        const next_c = html[i + 1];

        // Comment or declaration: <! ... >
        if (next_c == '!') {
            if (i + 3 < html.len and html[i + 2] == '-' and html[i + 3] == '-') {
                // Comment <!-- ... -->
                if (std.mem.indexOf(u8, html[i + 4 ..], "-->")) |c_off| {
                    i = i + 4 + c_off + 3;
                } else {
                    i = html.len;
                }
            } else if (std.mem.startsWith(u8, html[i + 2 ..], "[CDATA[")) {
                if (std.mem.indexOf(u8, html[i + 9 ..], "]]>")) |c_off| {
                    i = i + 9 + c_off + 3;
                } else {
                    i = html.len;
                }
            } else {
                // DOCTYPE or other declaration: scan to '>'
                while (i < html.len and html[i] != '>') : (i += 1) {}
                if (i < html.len and html[i] == '>') i += 1;
            }
            continue;
        }

        // Processing instruction / bogus comment: <? ... >
        if (next_c == '?') {
            while (i < html.len and html[i] != '>') : (i += 1) {}
            if (i < html.len and html[i] == '>') i += 1;
            continue;
        }

        // End tag: </tag>
        if (next_c == '/') {
            i += 2;
            while (i < html.len and isTagWhitespace(html[i])) : (i += 1) {}
            const name_start = i;
            while (i < html.len and !isTagWhitespace(html[i]) and html[i] != '>' and html[i] != '/') : (i += 1) {}
            const tag_name = html[name_start..i];
            while (i < html.len and html[i] != '>') : (i += 1) {}
            if (i < html.len and html[i] == '>') i += 1;

            if (std.ascii.eqlIgnoreCase(tag_name, "template")) {
                if (template_depth > 0) template_depth -= 1;
            }
            continue;
        }

        // Start tag: <tag ...>
        if (std.ascii.isAlphabetic(next_c)) {
            i += 1;
            const name_start = i;
            while (i < html.len and !isTagWhitespace(html[i]) and html[i] != '>' and html[i] != '/') : (i += 1) {}
            const tag_name = html[name_start..i];

            var has_nomodule = false;
            var src_val: ?[]const u8 = null;
            var type_val: ?[]const u8 = null;
            var href_val: ?[]const u8 = null;

            while (i < html.len) {
                while (i < html.len and isTagWhitespace(html[i])) : (i += 1) {}
                if (i >= html.len) break;
                if (html[i] == '>') {
                    i += 1;
                    break;
                }
                if (html[i] == '/') {
                    i += 1;
                    continue;
                }

                const attr_start = i;
                while (i < html.len and !isTagWhitespace(html[i]) and html[i] != '=' and html[i] != '/' and html[i] != '>') : (i += 1) {}
                const attr_name = html[attr_start..i];

                while (i < html.len and isTagWhitespace(html[i])) : (i += 1) {}

                var val: []const u8 = "";
                if (i < html.len and html[i] == '=') {
                    i += 1;
                    while (i < html.len and isTagWhitespace(html[i])) : (i += 1) {}
                    if (i < html.len) {
                        if (html[i] == '"' or html[i] == '\'') {
                            const quote = html[i];
                            i += 1;
                            const v_start = i;
                            while (i < html.len and html[i] != quote) : (i += 1) {}
                            val = html[v_start..i];
                            if (i < html.len and html[i] == quote) i += 1;
                        } else {
                            const v_start = i;
                            while (i < html.len and !isTagWhitespace(html[i]) and html[i] != '>') : (i += 1) {}
                            val = html[v_start..i];
                        }
                    }
                }

                if (std.ascii.eqlIgnoreCase(attr_name, "nomodule")) {
                    has_nomodule = true;
                } else if (std.ascii.eqlIgnoreCase(attr_name, "src")) {
                    if (src_val == null) src_val = val;
                } else if (std.ascii.eqlIgnoreCase(attr_name, "type")) {
                    if (type_val == null) type_val = val;
                } else if (std.ascii.eqlIgnoreCase(attr_name, "href")) {
                    if (href_val == null) href_val = val;
                }
            }

            if (std.ascii.eqlIgnoreCase(tag_name, "template")) {
                template_depth += 1;
            } else if (std.ascii.eqlIgnoreCase(tag_name, "base")) {
                if (!base_seen) {
                    if (href_val) |href| {
                        base_seen = true;
                        emitResource(ctx, callback, .base, href);
                    }
                }
            } else if (std.ascii.eqlIgnoreCase(tag_name, "script")) {
                if (template_depth == 0 and !has_nomodule) {
                    if (src_val) |src| {
                        if (src.len > 0) {
                            var kind: ?PrescanResource = null;
                            if (type_val) |t| {
                                const trimmed = std.mem.trim(u8, t, " \t\r\n\x0C");
                                if (trimmed.len == 0 or
                                    std.ascii.eqlIgnoreCase(trimmed, "application/javascript") or
                                    std.ascii.eqlIgnoreCase(trimmed, "text/javascript"))
                                {
                                    kind = .script;
                                } else if (std.ascii.eqlIgnoreCase(trimmed, "module")) {
                                    kind = .module;
                                }
                            } else {
                                kind = .script;
                            }
                            if (kind) |k| {
                                emitResource(ctx, callback, k, src);
                            }
                        }
                    }
                }
                skipUntilEndTag(html, &i, "script");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "style")) {
                skipUntilEndTag(html, &i, "style");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "noscript")) {
                skipUntilEndTag(html, &i, "noscript");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "iframe")) {
                skipUntilEndTag(html, &i, "iframe");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "xmp")) {
                skipUntilEndTag(html, &i, "xmp");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "noembed")) {
                skipUntilEndTag(html, &i, "noembed");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "noframes")) {
                skipUntilEndTag(html, &i, "noframes");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "title")) {
                skipUntilEndTag(html, &i, "title");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "textarea")) {
                skipUntilEndTag(html, &i, "textarea");
            } else if (std.ascii.eqlIgnoreCase(tag_name, "plaintext")) {
                i = html.len;
            }
            continue;
        }

        // Lone '<' or non-tag markup, advance
        i += 1;
    }
}

const testing = std.testing;

test "Prescan: comprehensive script, base, template, and rawtext scanning" {
    const TestRecorder = struct {
        const Entry = struct {
            kind: PrescanResource,
            url: [128]u8,
            url_len: usize,
        };
        entries: [16]Entry = undefined,
        count: usize = 0,

        fn record(ctx: *anyopaque, kind: PrescanResource, url: [*c]const u8, url_len: usize) callconv(.c) void {
            const self: *@This() = @ptrCast(@alignCast(ctx));
            if (self.count < self.entries.len) {
                var entry = &self.entries[self.count];
                entry.kind = kind;
                const len = @min(url_len, entry.url.len);
                @memcpy(entry.url[0..len], url[0..len]);
                entry.url_len = len;
                self.count += 1;
            }
        }
    };

    var rec = TestRecorder{};
    const sample =
        \\<html><head>
        \\<script src="/serve-count/unit_a.js"></script>
        \\<script src="/serve-count/unit_a.js"></script>
        \\<script type="module" src="/serve-count/unit_m.js"></script>
        \\<script src="/serve-count/unit_skipped.js" nomodule></script>
        \\<script type="application/json" src="/serve-count/unit_data.js"></script>
        \\<script src="data:text/javascript,window.unit_data_url = 1;"></script>
        \\<script type="module" src="data:text/javascript,window.unit_data_module = 1;"></script>
        \\<script>var trap = "</scr" + "ipt><script src=/serve-count/unit_inline.js>";</script>
        \\<!-- <script src="/serve-count/unit_commented.js"></script> -->
        \\<template><script src="/serve-count/unit_templated.js"></script></template>
        \\<noscript><script src="/serve-count/unit_noscripted.js"></script></noscript>
        \\<style>p:before { content: "<script src=/serve-count/unit_styled.js></script>" }</style>
        \\<base href="/serve-count/sub/">
        \\<base href="/serve-count/ignored/">
        \\<script src="based.js"></script>
        \\<script src="escaped.js?a=1&amp;b=2"></script>
        \\</head><body></body></html>
    ;

    prescan(sample, "utf-8", &rec, TestRecorder.record);

    try testing.expectEqual(@as(usize, 8), rec.count);
    try testing.expectEqual(PrescanResource.script, rec.entries[0].kind);
    try testing.expectEqualStrings("/serve-count/unit_a.js", rec.entries[0].url[0..rec.entries[0].url_len]);

    try testing.expectEqual(PrescanResource.script, rec.entries[1].kind);
    try testing.expectEqualStrings("/serve-count/unit_a.js", rec.entries[1].url[0..rec.entries[1].url_len]);

    try testing.expectEqual(PrescanResource.module, rec.entries[2].kind);
    try testing.expectEqualStrings("/serve-count/unit_m.js", rec.entries[2].url[0..rec.entries[2].url_len]);

    try testing.expectEqual(PrescanResource.script, rec.entries[3].kind);
    try testing.expectEqualStrings("data:text/javascript,window.unit_data_url = 1;", rec.entries[3].url[0..rec.entries[3].url_len]);

    try testing.expectEqual(PrescanResource.module, rec.entries[4].kind);
    try testing.expectEqualStrings("data:text/javascript,window.unit_data_module = 1;", rec.entries[4].url[0..rec.entries[4].url_len]);

    try testing.expectEqual(PrescanResource.base, rec.entries[5].kind);
    try testing.expectEqualStrings("/serve-count/sub/", rec.entries[5].url[0..rec.entries[5].url_len]);

    try testing.expectEqual(PrescanResource.script, rec.entries[6].kind);
    try testing.expectEqualStrings("based.js", rec.entries[6].url[0..rec.entries[6].url_len]);

    try testing.expectEqual(PrescanResource.script, rec.entries[7].kind);
    try testing.expectEqualStrings("escaped.js?a=1&b=2", rec.entries[7].url[0..rec.entries[7].url_len]);
}
