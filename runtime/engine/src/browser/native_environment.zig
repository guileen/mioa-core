// Native profile identity is committed only after configuration, assets and storage restore.
const std = @import("std");
const lp = @import("lightpanda");
const Persistence = @import("webapi/storage/Persistence.zig");
const Sqlite = @import("../storage/sqlite/Sqlite.zig");

const KeyOrder = struct {
    keys: []const []const u8,

    pub fn lessThan(self: KeyOrder, a: usize, b: usize) bool {
        return std.mem.lessThan(u8, self.keys[a], self.keys[b]);
    }
};

fn sortValue(value: *std.json.Value) void {
    switch (value.*) {
        .object => |*object| {
            object.sortUnstable(KeyOrder{ .keys = object.keys() });
            for (object.values()) |*child| sortValue(child);
        },
        .array => |*array| {
            for (array.items) |*child| sortValue(child);
        },
        else => {},
    }
}

fn sha256(bytes: []const u8) [64]u8 {
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(bytes, &digest, .{});
    return std.fmt.bytesToHex(digest, .lower);
}

fn isSha256(value: []const u8) bool {
    if (value.len != 64) return false;
    for (value) |c| {
        if ((c < '0' or c > '9') and (c < 'a' or c > 'f')) return false;
    }
    return true;
}

// Return whether the durable record needs updating. Every check happens before
// any update, under the same IMMEDIATE transaction as the eventual commit.
fn admitExisting(conn: Sqlite.Conn, environment: *const lp.Config.Environment, semantic: []const u8, raw: []const u8) !bool {
    const count = conn.scalar(i64, "select count(*) from environment", .{}) catch |err| switch (err) {
        error.Error => return error.incompatible_profile,
        else => return err,
    };
    if (count != 1) return error.incompatible_profile;
    const row = (conn.row(
        \\select schema,id,revision,semantic_sha256,raw_sha256 from environment
        \\where singleton=1 and typeof(singleton)='integer' and typeof(schema)='integer'
        \\and typeof(id)='text' and length(id)>0 and typeof(revision)='integer'
        \\and revision between 1 and 4294967295
        \\and typeof(semantic_sha256)='text' and typeof(raw_sha256)='text'
    , .{}) catch |err| switch (err) {
        error.Error => return error.incompatible_profile,
        else => return err,
    }) orelse return error.incompatible_profile;
    defer row.deinit();

    const schema = row.get(i64, 0);
    const id = row.get([]const u8, 1);
    const revision = row.get(i64, 2);
    const old_semantic = row.get([]const u8, 3);
    const old_raw = row.get([]const u8, 4);
    if (schema != 3 or !isSha256(old_semantic) or !isSha256(old_raw)) return error.incompatible_profile;
    if (!std.mem.eql(u8, id, environment.id)) return error.identity_collision;
    if (environment.revision < revision) return error.incompatible_profile;
    if (environment.revision == revision) {
        if (!std.mem.eql(u8, old_semantic, semantic)) return error.incompatible_profile;
    } else {
        if (@as(i64, environment.revision) != revision + 1) return error.incompatible_profile;
        const previous = environment.previous_environment_sha256 orelse return error.incompatible_profile;
        if (!std.mem.eql(u8, previous, old_raw)) return error.incompatible_profile;
    }
    return environment.revision != revision or !std.mem.eql(u8, old_raw, raw);
}

/// Called by App after asset validation and Session.preflightStorage, before
/// publishing ready/getInfo. No configuration is generated or rewritten here.
pub fn admit(allocator: std.mem.Allocator, config: *const lp.Config, raw: []const u8) !void {
    const environment = if (config.environment) |*value| value else return error.invalid_config;
    if (environment.schema != 3 or environment.id.len == 0 or environment.revision == 0) return error.invalid_config;

    // Preserve the host's exact JSON number spellings: parsing through f64
    // would change e.g. 1.0 to 1 and invalidate the resolved digest.
    var parsed = std.json.parseFromSlice(std.json.Value, allocator, raw, .{ .parse_numbers = false }) catch |err| switch (err) {
        error.OutOfMemory => return err,
        else => return error.invalid_config,
    };
    defer parsed.deinit();
    if (parsed.value != .object) return error.invalid_config;
    sortValue(&parsed.value);
    const root = &parsed.value.object;
    var canonical: std.Io.Writer.Allocating = .init(allocator);
    defer canonical.deinit();
    if (environment.resolved_sha256) |expected| {
        if (!isSha256(expected)) return error.invalid_config;
        const resolved = root.get("resolved") orelse return error.invalid_config;
        if (resolved != .object) return error.invalid_config;
        std.json.Stringify.value(resolved, .{}, &canonical.writer) catch return error.OutOfMemory;
        const actual = sha256(canonical.written());
        if (!std.mem.eql(u8, expected, &actual)) return error.incompatible_profile;
        canonical.clearRetainingCapacity();
    }

    // Management proofs and physical relocations are not a new identity.
    for ([_][]const u8{ "name", "mode", "created_at", "revision", "resolved_sha256", "previous_environment_sha256", "asset_paths" }) |key| {
        _ = root.orderedRemove(key);
    }
    const storage = root.getPtr("storage") orelse return error.invalid_config;
    if (storage.* != .object) return error.invalid_config;
    _ = storage.object.orderedRemove("path");
    const runtime = root.getPtr("runtime") orelse return error.invalid_config;
    if (runtime.* != .object) return error.invalid_config;
    _ = runtime.object.orderedRemove("binary");
    std.json.Stringify.value(parsed.value, .{}, &canonical.writer) catch return error.OutOfMemory;
    const semantic_digest = sha256(canonical.written());
    const raw_digest = sha256(raw);

    const path = try allocator.printSentinel("{s}/environment.sqlite", .{environment.storage.path}, 0);
    defer allocator.free(path);
    // Lost admission metadata must not be silently regenerated over its WAL.
    if (std.Io.Dir.cwd().statFile(lp.io, path, .{})) |_| {} else |err| switch (err) {
        error.FileNotFound => {
            const dir = try std.Io.Dir.cwd().openDir(lp.io, environment.storage.path, .{});
            defer dir.close(lp.io);
            for ([_][]const u8{ "environment.sqlite-wal", "environment.sqlite-shm", "environment.sqlite-journal" }) |name| {
                if (dir.statFile(lp.io, name, .{})) |_| {
                    return error.incompatible_profile;
                } else |recovery_err| switch (recovery_err) {
                    error.FileNotFound => {},
                    else => return recovery_err,
                }
            }
        },
        else => return err,
    }
    const opened = try Persistence.open(path);
    const conn = opened.conn;
    defer conn.close();
    try conn.begin(.immediate);
    errdefer conn.rollback() catch {};
    if (opened.existing) {
        if (try admitExisting(conn, environment, &semantic_digest, &raw_digest)) {
            try conn.exec("update environment set schema=?1,id=?2,revision=?3,semantic_sha256=?4,raw_sha256=?5 where singleton=1", .{
                environment.schema, environment.id, environment.revision, @as([]const u8, &semantic_digest), @as([]const u8, &raw_digest),
            });
        }
    } else {
        try conn.exec(
            \\create table environment (
            \\ singleton integer primary key check(singleton=1),
            \\ schema integer not null check(schema=3), id text not null check(length(id)>0),
            \\ revision integer not null check(revision>=1),
            \\ semantic_sha256 text not null check(length(semantic_sha256)=64),
            \\ raw_sha256 text not null check(length(raw_sha256)=64)
            \\) strict
        , .{});
        try conn.exec("insert into environment(singleton,schema,id,revision,semantic_sha256,raw_sha256) values(1,?1,?2,?3,?4,?5)", .{
            environment.schema, environment.id, environment.revision, @as([]const u8, &semantic_digest), @as([]const u8, &raw_digest),
        });
    }
    try conn.commit();
}
