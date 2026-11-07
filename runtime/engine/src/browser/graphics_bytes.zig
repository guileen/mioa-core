const std = @import("std");
/// Binary buffers are numeric arrays at the native graphics JSON boundary,
/// never UTF-8 strings (arbitrary RGBA and typed-array bytes need not be UTF-8).
pub const Bytes = struct {
    data: []const u8,
    pub fn jsonStringify(self: Bytes, writer: *std.json.Stringify) !void {
        try writer.beginArray();
        for (self.data) |byte| try writer.write(byte);
        try writer.endArray();
    }
};
