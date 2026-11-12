pub const mioa_environment_value_v1 = extern struct {
    value: [*c]u8,
    length: usize,
};

pub const MIOA_ENVIRONMENT_OK: c_int = 0;
pub const MIOA_ENVIRONMENT_MISSING: c_int = 1;
pub const MIOA_ENVIRONMENT_NO_MEMORY: c_int = 3;

pub extern "c" fn mioa_environment_get_v1([*c]const u8, *mioa_environment_value_v1) c_int;
pub extern "c" fn mioa_environment_has_v1([*c]const u8, *c_int) c_int;
pub extern "c" fn mioa_environment_value_release_v1(*mioa_environment_value_v1) void;

pub const Value = struct {
    raw: mioa_environment_value_v1,

    pub fn slice(self: Value) []const u8 {
        if (self.raw.length == 0 or self.raw.value == null) return "";
        return self.raw.value[0..self.raw.length];
    }

    pub fn deinit(self: *Value) void {
        mioa_environment_value_release_v1(&self.raw);
    }
};

pub fn get(name: [*:0]const u8) ?Value {
    var raw: mioa_environment_value_v1 = .{ .value = null, .length = 0 };
    if (mioa_environment_get_v1(name, &raw) == MIOA_ENVIRONMENT_OK) {
        return .{ .raw = raw };
    }
    return null;
}

pub fn has(name: [*:0]const u8) bool {
    var present: c_int = 0;
    if (mioa_environment_has_v1(name, &present) == MIOA_ENVIRONMENT_OK) {
        return present != 0;
    }
    return false;
}

test "environment requires mioa_os to link" {
    _ = has("PATH");
}
