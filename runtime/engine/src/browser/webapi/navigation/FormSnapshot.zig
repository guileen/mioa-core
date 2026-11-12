const std = @import("std");
const Frame = @import("../../Frame.zig");
const Element = @import("../Element.zig");
const Node = @import("../Node.zig");
const TreeWalker = @import("../TreeWalker.zig");

const FormSnapshot = @This();
const Input = Element.Html.Input;
const TextArea = Element.Html.TextArea;
const Option = Element.Html.Option;
const max_controls = 4096;
const max_bytes = 1024 * 1024;

arena: std.heap.ArenaAllocator,
url: []const u8,
controls: []Control,

const Control = struct {
    ordinal: usize,
    id: ?[]const u8,
    name: ?[]const u8,
    form: ?[]const u8,
    identity: u64,
    kind: Kind,
    value: []const u8,
    checked: bool,
};
const Kind = union(enum) { input: Input.Type, textarea, option };

fn copyAttr(a: std.mem.Allocator, el: *Element, comptime name: []const u8) !?[]const u8 {
    const value = el.getAttributeSafe(comptime .wrap(name)) orelse return null;
    return try a.dupe(u8, value);
}

fn sameAttr(saved: ?[]const u8, current: ?[]const u8) bool {
    if (saved) |s| return if (current) |c| std.mem.eql(u8, s, c) else false;
    return current == null;
}

// Include the enclosing form/select structure and authored control identity,
// not just a control's position among other controls.
fn identity(node: *Node) u64 {
    var hash = std.hash.Wyhash.init(0);
    var current: ?*Node = node;
    while (current) |n| : (current = n.parentNode()) {
        if (n.is(Element)) |el| {
            const tag = el.getTagNameLower();
            hash.update(std.mem.asBytes(&tag.len));
            hash.update(tag);
            inline for (.{ "id", "name", "form", "type", "value", "multiple", "action" }) |attr| {
                if (el.getAttributeSafe(comptime .wrap(attr))) |value| {
                    hash.update(&.{1});
                    hash.update(std.mem.asBytes(&value.len));
                    hash.update(value);
                } else hash.update(&.{0});
            }
        }
    }
    return hash.final();
}
pub fn capture(frame: *Frame) !*FormSnapshot {
    const allocator = frame._session.browser.app.allocator;
    const snapshot = try allocator.create(FormSnapshot);
    snapshot.* = .{ .arena = std.heap.ArenaAllocator.init(allocator), .url = undefined, .controls = undefined };
    errdefer snapshot.destroy();
    const a = snapshot.arena.allocator();
    if (frame.url.len > max_bytes) return error.OutOfMemory;
    snapshot.url = try a.dupe(u8, frame.url);
    var controls: std.ArrayList(Control) = .empty;
    var walker = TreeWalker.Full.init(frame.document.asNode(), .{});
    var ordinal: usize = 0;
    var bytes: usize = frame.url.len;
    while (walker.next()) |node| : (ordinal += 1) {
        var kind: Kind = undefined;
        var value: []const u8 = "";
        var checked = false;
        if (node.is(Input)) |input| {
            switch (input._input_type) {
                .password, .file => continue,
                .checkbox, .radio => {
                    kind = .{ .input = input._input_type };
                    checked = input.getChecked();
                    value = input.getValue();
                },
                else => {
                    if (input._input_type.valueMode() != .value) continue;
                    kind = .{ .input = input._input_type };
                    value = input.getValue();
                },
            }
        } else if (node.is(TextArea)) |textarea| {
            kind = .textarea;
            value = textarea.getValue();
        } else if (node.is(Option)) |option| {
            kind = .option;
            value = option.getValue(frame);
            checked = option.getSelected();
        } else continue;
        const el = node.as(Element);
        var size = value.len;
        inline for (.{ "id", "name", "form" }) |attr| {
            if (el.getAttributeSafe(comptime .wrap(attr))) |v| {
                size = std.math.add(usize, size, v.len) catch return error.OutOfMemory;
            }
        }
        bytes = std.math.add(usize, bytes, size) catch return error.OutOfMemory;
        if (controls.items.len >= max_controls or bytes > max_bytes) return error.OutOfMemory;
        try controls.append(a, .{
            .ordinal = ordinal,
            .id = try copyAttr(a, el, "id"),
            .name = try copyAttr(a, el, "name"),
            .form = try copyAttr(a, el, "form"),
            .kind = kind,
            .value = try a.dupe(u8, value),
            .checked = checked,
            .identity = identity(node),
        });
    }
    snapshot.controls = try controls.toOwnedSlice(a);
    return snapshot;
}

pub fn destroy(self: *FormSnapshot) void {
    const allocator = self.arena.child_allocator;
    self.arena.deinit();
    allocator.destroy(self);
}

pub fn restore(self: *const FormSnapshot, frame: *Frame) void {
    if (!std.mem.eql(u8, self.url, frame.url)) return;
    var walker = TreeWalker.Full.init(frame.document.asNode(), .{});
    var ordinal: usize = 0;
    var index: usize = 0;
    while (walker.next()) |node| : (ordinal += 1) {
        if (index == self.controls.len) break;
        const saved = self.controls[index];
        if (ordinal != saved.ordinal) continue;
        index += 1;
        const el = node.is(Element) orelse continue;
        if (!sameAttr(saved.id, el.getAttributeSafe(comptime .wrap("id"))) or
            !sameAttr(saved.name, el.getAttributeSafe(comptime .wrap("name"))) or
            !sameAttr(saved.form, el.getAttributeSafe(comptime .wrap("form")))) continue;
        if (identity(node) != saved.identity) continue;
        switch (saved.kind) {
            .input => |ty| {
                const input = node.is(Input) orelse continue;
                if (input._input_type != ty) continue;
                if (ty == .checkbox or ty == .radio) {
                    if (!std.mem.eql(u8, saved.value, input.getValue())) continue;
                    input.setChecked(saved.checked, frame) catch {};
                } else input.setValue(saved.value, frame) catch {};
            },
            .textarea => {
                const textarea = node.is(TextArea) orelse continue;
                textarea.setValue(saved.value, frame) catch {};
            },
            .option => {
                const option = node.is(Option) orelse continue;
                if (!std.mem.eql(u8, saved.value, option.getValue(frame))) continue;
                option._selected = saved.checked;
            },
        }
    }
    frame.domChanged();
}
