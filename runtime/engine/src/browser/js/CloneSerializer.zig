// Keep the public structured-clone delegate semantics while replacing V8's
// malloc-only serializer backing in the qualified, header-bound native build.
const std = @import("std");
const v8 = @import("js.zig").v8;
pub const enabled = @import("build_config").accounted_clone;
// Referenced only by the is_test branch below; no override path is emitted in production.
pub const TestAllocator = struct {
    pub var override: ?std.mem.Allocator = null;
};

pub const Memory = struct {
    allocator: std.mem.Allocator,
    failed: bool = false,

    pub fn init() !Memory {
        if (!enabled and std.c.getenv("MIOA_PROFILE_SERVICE_FD") != null) return error.QualificationRequired;
        if (comptime @import("builtin").is_test) {
            if (TestAllocator.override) |allocator| return .{ .allocator = allocator };
        }
        return .{ .allocator = if (enabled)
            try @import("../webapi/storage/RuntimeMemory.zig").allocator(std.heap.c_allocator)
        else
            std.heap.c_allocator };
    }

    fn allocate(context: ?*anyopaque, size: usize) callconv(.c) ?*anyopaque {
        const self: *Memory = @ptrCast(@alignCast(context.?));
        const bytes = self.allocator.alignedAlloc(u8, .@"16", size) catch {
            self.failed = true;
            return null;
        };
        return bytes.ptr;
    }

    fn dispose(context: ?*anyopaque, pointer: ?*anyopaque, size: usize) callconv(.c) void {
        const self: *Memory = @ptrCast(@alignCast(context.?));
        const bytes: [*]align(16) u8 = @ptrCast(@alignCast(pointer.?));
        self.allocator.free(bytes[0..size]);
    }
};

pub fn create(isolate: *v8.Isolate, callbacks: *const v8.ValueSerializerDelegateCallbacks, memory: *Memory) ?*v8.ValueSerializer {
    if (enabled) return lp_accounted_clone_new(isolate, callbacks, memory, Memory.allocate, Memory.dispose);
    return v8.v8__ValueSerializer__New(isolate, callbacks);
}
pub fn destroy(serializer: *v8.ValueSerializer) void {
    if (enabled) lp_accounted_clone_delete(serializer) else v8.v8__ValueSerializer__DELETE(serializer);
}
pub fn header(serializer: *v8.ValueSerializer) void {
    if (enabled) lp_accounted_clone_header(serializer) else v8.v8__ValueSerializer__WriteHeader(serializer);
}
pub fn write(serializer: *v8.ValueSerializer, context: *const v8.Context, value: *const v8.Value, out: *v8.MaybeBool) void {
    if (enabled) lp_accounted_clone_write(serializer, context, value, out) else v8.v8__ValueSerializer__WriteValue(serializer, context, value, out);
}
pub fn release(serializer: *v8.ValueSerializer, size: *usize, capacity: *usize) [*c]u8 {
    if (enabled) return lp_accounted_clone_release(serializer, size, capacity);
    capacity.* = 0;
    return v8.v8__ValueSerializer__Release(serializer, size);
}
pub fn writeUint32(serializer: *v8.ValueSerializer, value: u32) void {
    if (enabled) lp_accounted_clone_u32(serializer, value) else v8.v8__ValueSerializer__WriteUint32(serializer, value);
}
pub fn writeUint64(serializer: *v8.ValueSerializer, value: u64) void {
    if (enabled) lp_accounted_clone_u64(serializer, value) else v8.v8__ValueSerializer__WriteUint64(serializer, value);
}
pub fn writeBytes(serializer: *v8.ValueSerializer, bytes: []const u8) void {
    if (enabled) lp_accounted_clone_bytes(serializer, bytes.ptr, bytes.len) else v8.v8__ValueSerializer__WriteRawBytes(serializer, bytes.ptr, bytes.len);
}

extern "c" fn lp_accounted_clone_new(*v8.Isolate, *const v8.ValueSerializerDelegateCallbacks, ?*anyopaque, *const fn (?*anyopaque, usize) callconv(.c) ?*anyopaque, *const fn (?*anyopaque, ?*anyopaque, usize) callconv(.c) void) ?*v8.ValueSerializer;
extern "c" fn lp_accounted_clone_delete(*v8.ValueSerializer) void;
extern "c" fn lp_accounted_clone_header(*v8.ValueSerializer) void;
extern "c" fn lp_accounted_clone_write(*v8.ValueSerializer, *const v8.Context, *const v8.Value, *v8.MaybeBool) void;
extern "c" fn lp_accounted_clone_release(*v8.ValueSerializer, *usize, *usize) [*c]u8;
extern "c" fn lp_accounted_clone_u32(*v8.ValueSerializer, u32) void;
extern "c" fn lp_accounted_clone_u64(*v8.ValueSerializer, u64) void;
extern "c" fn lp_accounted_clone_bytes(*v8.ValueSerializer, [*]const u8, usize) void;
