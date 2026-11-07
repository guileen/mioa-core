// One native declaration shared by HTTP, Window and Worker. Custom UA metadata
// describes the configured declaration, not an assertion of Chrome equivalence.
const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const Identity = @This();

pub const Brand = struct { brand: []const u8, version: []const u8 };
pub const default_user_agent = "Lightpanda/" ++ @import("lightpanda").build_config.version;

user_agent: [:0]const u8,
brands: [1]Brand,
full_version_list: [1]Brand,
platform: []const u8,
navigator_platform: []const u8,
mobile: bool,
architecture: []const u8,
bitness: []const u8,
wow64: bool,
sec_ch_ua: [:0]const u8,
sec_ch_ua_full_version_list: [:0]const u8,
sec_ch_ua_platform: [:0]const u8,

pub fn init(allocator: Allocator, ua: []const u8, configured_platform: ?[]const u8) !Identity {
    try @import("Config.zig").validateUserAgent(ua);
    const owned = try allocator.dupeSentinel(u8, ua, 0);
    errdefer allocator.free(owned);
    const product = parseProduct(owned) orelse return error.invalid_config;
    const major = product.version[0 .. std.mem.findScalar(u8, product.version, '.') orelse product.version.len];
    const declared_platform = platformFromUA(owned);
    const navigator_platform = try allocator.dupe(u8, configured_platform orelse declared_platform orelse hostPlatform());
    errdefer allocator.free(navigator_platform);
    const platform = platformName(navigator_platform) orelse return error.invalid_config;
    if (declared_platform) |declared| {
        if (!std.mem.eql(u8, platform, platformName(declared).?)) return error.invalid_config;
    }
    const low = try allocator.printSentinel("\"{s}\";v=\"{s}\"", .{ product.brand, major }, 0);
    errdefer allocator.free(low);
    const full = try allocator.printSentinel("\"{s}\";v=\"{s}\"", .{ product.brand, product.version }, 0);
    errdefer allocator.free(full);
    const wire_platform = try allocator.printSentinel("\"{s}\"", .{platform}, 0);
    const arm = std.mem.find(u8, owned, "aarch64") != null or std.mem.find(u8, owned, "arm64") != null or std.mem.find(u8, owned, "ARM") != null;
    const wow64 = std.mem.find(u8, owned, "WOW64") != null;
    const x86 = wow64 or std.mem.find(u8, owned, "x86") != null or std.mem.find(u8, owned, "x64") != null or std.mem.find(u8, owned, "Win64") != null or std.mem.find(u8, owned, "Intel") != null;
    return .{
        .user_agent = owned,
        .brands = .{.{ .brand = product.brand, .version = major }},
        .full_version_list = .{.{ .brand = product.brand, .version = product.version }},
        .platform = platform,
        .navigator_platform = navigator_platform,
        .mobile = std.mem.find(u8, owned, "Mobile") != null or std.mem.eql(u8, platform, "Android") or std.mem.eql(u8, platform, "iOS"),
        .architecture = if (arm) "arm" else if (x86) "x86" else if (declared_platform != null) "" else hostArchitecture(),
        .bitness = if (arm or x86) (if (std.mem.find(u8, owned, "64") != null or std.mem.find(u8, owned, "Intel Mac") != null) "64" else "32") else if (declared_platform != null) "" else hostBitness(),
        .wow64 = wow64,
        .sec_ch_ua = low,
        .sec_ch_ua_full_version_list = full,
        .sec_ch_ua_platform = wire_platform,
    };
}

pub fn deinit(self: *const Identity, allocator: Allocator) void {
    allocator.free(self.sec_ch_ua_platform);
    allocator.free(self.sec_ch_ua_full_version_list);
    allocator.free(self.sec_ch_ua);
    allocator.free(self.navigator_platform);
    allocator.free(self.user_agent);
}

const Product = struct { brand: []const u8, version: []const u8 };
fn parseProduct(ua: []const u8) ?Product {
    // Compatibility tokens (Mozilla/AppleWebKit) are not the selected browser.
    const known = [_]struct { token: []const u8, brand: []const u8 }{
        .{ .token = "Edg/", .brand = "Microsoft Edge" },
        .{ .token = "OPR/", .brand = "Opera" },
        .{ .token = "Lightpanda/", .brand = "Lightpanda" },
        .{ .token = "Chrome/", .brand = "Google Chrome" },
        .{ .token = "Chromium/", .brand = "Chromium" },
        .{ .token = "Firefox/", .brand = "Firefox" },
        .{ .token = "Version/", .brand = "Safari" },
    };
    for (known) |candidate| {
        if (std.mem.eql(u8, candidate.token, "Version/") and std.mem.find(u8, ua, "Safari/") == null) continue;
        if (std.mem.find(u8, ua, candidate.token)) |start| {
            if (start > 0 and ua[start - 1] != ' ' and ua[start - 1] != ')') continue;
            const rest = ua[start + candidate.token.len ..];
            const version = rest[0 .. std.mem.findAny(u8, rest, " ;)\t") orelse rest.len];
            if (!safeToken(version)) return null;
            return .{ .brand = candidate.brand, .version = version };
        }
    }
    var tokens = std.mem.tokenizeScalar(u8, ua, ' ');
    const first = tokens.next() orelse return null;
    const slash = std.mem.findScalar(u8, first, '/') orelse first.len;
    const brand = first[0..slash];
    const version = if (slash < first.len) first[slash + 1 ..] else "";
    if (!safeToken(brand) or (slash < first.len and !safeToken(version))) return null;
    return .{ .brand = brand, .version = version };
}
fn safeToken(value: []const u8) bool {
    if (value.len == 0) return false;
    for (value) |c| if (!(std.ascii.isAlphanumeric(c) or c == '.' or c == '-' or c == '_' or c == '+')) return false;
    return true;
}
fn platformFromUA(ua: []const u8) ?[]const u8 {
    if (std.mem.find(u8, ua, "Android") != null) return "Android";
    if (std.mem.find(u8, ua, "iPhone") != null) return "iPhone";
    if (std.mem.find(u8, ua, "iPad") != null) return "iPad";
    if (std.mem.find(u8, ua, "Windows") != null) return "Win32";
    if (std.mem.find(u8, ua, "Macintosh") != null or std.mem.find(u8, ua, "Mac OS X") != null) return "MacIntel";
    if (std.mem.find(u8, ua, "Linux") != null) return if (std.mem.find(u8, ua, "aarch64") != null) "Linux aarch64" else "Linux x86_64";
    if (std.mem.find(u8, ua, "FreeBSD") != null) return "FreeBSD";
    return null;
}
fn platformName(platform: []const u8) ?[]const u8 {
    if (std.mem.startsWith(u8, platform, "Mac")) return "macOS";
    if (std.mem.startsWith(u8, platform, "Win")) return "Windows";
    if (std.mem.startsWith(u8, platform, "Linux")) return "Linux";
    if (std.mem.eql(u8, platform, "Android")) return "Android";
    if (std.mem.eql(u8, platform, "iPhone") or std.mem.eql(u8, platform, "iPad")) return "iOS";
    if (std.mem.eql(u8, platform, "FreeBSD")) return "FreeBSD";
    if (std.mem.eql(u8, platform, "Unknown")) return "Unknown";
    return null;
}
fn hostPlatform() []const u8 {
    return switch (builtin.target.os.tag) {
        .macos => "MacIntel",
        .windows => "Win32",
        .linux => if (builtin.target.cpu.arch == .aarch64) "Linux aarch64" else "Linux x86_64",
        .freebsd => "FreeBSD",
        else => "Unknown",
    };
}
fn hostArchitecture() []const u8 {
    return switch (builtin.target.cpu.arch) {
        .x86, .x86_64 => "x86",
        .aarch64, .aarch64_be, .arm, .armeb => "arm",
        else => "",
    };
}
fn hostBitness() []const u8 {
    return switch (builtin.target.cpu.arch) {
        .x86_64, .aarch64, .aarch64_be, .powerpc64, .powerpc64le, .riscv64 => "64",
        else => "32",
    };
}

test "Identity: native and explicit UA share low/full brands and wire declarations" {
    const allocator = std.testing.allocator;
    var native = try Identity.init(allocator, default_user_agent, null);
    defer native.deinit(allocator);
    try std.testing.expectEqualStrings("Lightpanda", native.brands[0].brand);
    try std.testing.expectEqualStrings("1", native.brands[0].version);
    var chrome = try Identity.init(allocator, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36", "Win32");
    defer chrome.deinit(allocator);
    try std.testing.expectEqualStrings("Google Chrome", chrome.brands[0].brand);
    try std.testing.expectEqualStrings("124", chrome.brands[0].version);
    try std.testing.expectEqualStrings("124.0.0.0", chrome.full_version_list[0].version);
    try std.testing.expectEqualStrings("\"Google Chrome\";v=\"124\"", chrome.sec_ch_ua);
    try std.testing.expectEqualStrings("Windows", chrome.platform);
    try std.testing.expectError(error.invalid_config, Identity.init(allocator, "Chrome/124.0 (Windows NT 10.0)", "MacIntel"));
    var custom = try Identity.init(allocator, "CustomBot/2.7", null);
    defer custom.deinit(allocator);
    try std.testing.expectEqualStrings("CustomBot", custom.brands[0].brand);
    try std.testing.expectEqualStrings("2", custom.brands[0].version);
}
