const std = @import("std");
const js = @import("../../js/js.zig");
const Frame = @import("../../Frame.zig");
const CSSRule = @import("CSSRule.zig");
const CSSStyleProperties = @import("CSSStyleProperties.zig");
const Element = @import("../Element.zig");

const CSSStyleRule = @This();

pub const Proto = CSSRule;

_proto: *CSSRule,
_selector_text: []const u8 = "",
_style: ?*CSSStyleProperties = null,
_owner_element: ?*Element = null,

pub fn init(frame: *Frame) !*CSSStyleRule {
    const style_rule = try frame._factory.chained(.{
        CSSRule{ ._type = undefined },
        CSSStyleRule{ ._proto = undefined },
    });
    style_rule._proto._type = .{ .style = style_rule };
    return style_rule;
}

fn getSelectorText(self: *const CSSStyleRule) []const u8 {
    return self._selector_text;
}

pub fn setSelectorText(self: *CSSStyleRule, text: []const u8, frame: *Frame) !void {
    self._selector_text = try frame.dupeString(text);
    // Rule selectors do not belong to an element whose attribute mutation
    // would otherwise invalidate the page's visual state.
    if (self._owner_element) |owner| {
        if (owner.ownerFrame(frame)) |owner_frame| {
            owner_frame.styleChanged();
            return;
        }
    }
    frame.styleChanged();
}

pub fn getStyle(self: *CSSStyleRule, frame: *Frame) !*CSSStyleProperties {
    if (self._style) |style| {
        return style;
    }
    const style = try CSSStyleProperties.init(null, false, frame);
    style.asCSSStyleDeclaration()._rule_owner = self._owner_element;
    self._style = style;
    return style;
}

pub fn getCssText(self: *CSSStyleRule, frame: *Frame) ![]const u8 {
    const style_props = try self.getStyle(frame);
    const style = style_props.asCSSStyleDeclaration();
    var buf = std.Io.Writer.Allocating.init(frame.local_arena);
    try buf.writer.print("{s} {{ ", .{self._selector_text});
    try style.format(&buf.writer);
    try buf.writer.writeAll(" }");
    return buf.written();
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(CSSStyleRule);

    pub const Meta = struct {
        pub const name = "CSSStyleRule";
        pub var class_id: bridge.ClassId = undefined;
    };

    pub const selectorText = bridge.accessor(CSSStyleRule.getSelectorText, CSSStyleRule.setSelectorText, .{});
    pub const style = bridge.accessor(CSSStyleRule.getStyle, null, .{});
    pub const cssText = bridge.accessor(CSSStyleRule.getCssText, null, .{});
};
