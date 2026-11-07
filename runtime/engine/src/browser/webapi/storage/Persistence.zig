// Profile storage uses SQLite's crash-recovery journal, never a fresh fallback.
const std = @import("std");
const lp = @import("lightpanda");
const Sqlite = @import("../../../storage/sqlite/Sqlite.zig");

pub const Opened = struct { conn: Sqlite.Conn, existing: bool };
pub const Kind = enum { local, idb, cookies };

pub fn open(path: [:0]const u8) !Opened {
    return openChecked(path) catch |err| switch (err) {
        error.Corrupt, error.NotADB, error.Format, error.CorruptVTab, error.CorruptSequence, error.CorruptIndex => error.incompatible_profile,
        else => err,
    };
}

fn openChecked(path: [:0]const u8) !Opened {
    const existing = if (std.mem.eql(u8, path, ":memory:")) false else blk: {
        const stat = std.Io.Dir.cwd().statFile(lp.io, path, .{}) catch |err| switch (err) {
            error.FileNotFound => break :blk false,
            else => return err,
        };
        if (stat.kind != .file or stat.size == 0) return error.incompatible_profile;
        break :blk true;
    };
    const conn = try Sqlite.Conn.open(path);
    errdefer conn.close();
    try conn.busyTimeout(1000);
    if (existing) try check(conn);
    // FULL syncs every WAL commit before the mutation/transaction is acknowledged.
    // fullfsync requests the stronger flush on platforms (notably macOS) that support it.
    try conn.exec("pragma journal_mode=wal", .{});
    try conn.exec("pragma synchronous=full", .{});
    try conn.exec("pragma fullfsync=on", .{});
    try conn.exec("pragma checkpoint_fullfsync=on", .{});
    try conn.exec("pragma foreign_keys=on", .{});
    return .{ .conn = conn, .existing = existing };
}

pub fn check(conn: Sqlite.Conn) !void {
    var rows = try conn.rows("pragma integrity_check", .{});
    defer rows.deinit();
    var ok = false;
    while (try rows.next()) |row| {
        if (!std.mem.eql(u8, row.get([]const u8, 0), "ok")) return error.incompatible_profile;
        ok = true;
    }
    if (!ok) return error.incompatible_profile;
    var foreign_keys = try conn.rows("pragma foreign_key_check", .{});
    defer foreign_keys.deinit();
    if (try foreign_keys.next() != null) return error.incompatible_profile;
}

pub fn validateSchema(conn: Sqlite.Conn, kind: Kind) !void {
    // Prepare against every persisted column before accepting the profile. Missing or
    // incompatible tables must not be repaired into an apparently empty origin.
    const query: []const u8 = switch (kind) {
        .local => "select key,value from entries limit 0",
        .idb => "select d.id,d.name,d.version,s.id,s.database_id,s.name,s.key_path,s.key_path_component_length,s.auto_increment,s.key_generator,r.object_store_id,r.key,r.value,i.id,i.object_store_id,i.name,i.key_path,i.key_path_component_length,i.is_unique,i.multi_entry,x.index_id,x.key,x.primary_key,x.is_unique from idb_databases d,idb_object_stores s,idb_records r,idb_indexes i,idb_index_records x limit 0",
        .cookies => "select name,value,domain,path,expires,secure,http_only,same_site,same_site_default,creation_time from cookies limit 0",
    };
    var rows = conn.rows(query, .{}) catch |err| switch (err) {
        error.Error => return error.incompatible_profile,
        else => return err,
    };
    defer rows.deinit();
    _ = try rows.next();
    if (kind == .local) {
        const invalid = (try conn.scalar(i64, "select count(*) from entries where typeof(key)!='text' or typeof(value)!='text'", .{})).?;
        const bytes = (try conn.scalar(i64, "select coalesce(sum(length(cast(value as blob))),0) from entries", .{})).?;
        if (invalid != 0 or bytes > 5 * 1024 * 1024) return error.incompatible_profile;
    }
}

/// Called before publishing ready/getInfo, including when no origin has yet opened.
pub fn preflight(allocator: std.mem.Allocator, path: []const u8) !void {
    if (!std.fs.path.isAbsolute(path)) return error.invalid_config;
    try std.Io.Dir.cwd().createDirPath(lp.io, path);
    var dir = try std.Io.Dir.cwd().openDir(lp.io, path, .{ .iterate = true });
    defer dir.close(lp.io);
    var it = dir.iterate();
    while (try it.next(lp.io)) |entry| {
        // An orphaned recovery journal is not an empty/new origin.
        const suffix: []const u8 = if (std.mem.endsWith(u8, entry.name, ".sqlite-wal")) "-wal" else if (std.mem.endsWith(u8, entry.name, ".sqlite-shm")) "-shm" else if (std.mem.endsWith(u8, entry.name, ".sqlite-journal")) "-journal" else "";
        if (suffix.len != 0) {
            const base = entry.name[0 .. entry.name.len - suffix.len];
            if (std.mem.eql(u8, base, "cookies.sqlite") or std.mem.startsWith(u8, base, "local-") or std.mem.startsWith(u8, base, "idb-")) {
                _ = dir.statFile(lp.io, base, .{}) catch |err| switch (err) {
                    error.FileNotFound => return error.incompatible_profile,
                    else => return err,
                };
            }
            continue;
        }
        if (!std.mem.endsWith(u8, entry.name, ".sqlite")) continue;
        const kind: Kind = if (std.mem.eql(u8, entry.name, "cookies.sqlite")) .cookies else if (std.mem.startsWith(u8, entry.name, "local-")) .local else if (std.mem.startsWith(u8, entry.name, "idb-")) .idb else continue;
        if (entry.kind != .file) return error.incompatible_profile;
        const filename = try std.fs.path.joinZ(allocator, &.{ path, entry.name });
        defer allocator.free(filename);
        const opened = try open(filename);
        defer opened.conn.close();
        try validateSchema(opened.conn, kind);
    }
}

test "profile storage rejects corrupt and empty databases instead of recreating them" {
    const testing = @import("../../../testing.zig");
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(testing.io, .{ .sub_path = "local-bad.sqlite", .data = "not a database" });
    const bad_path = try testing.tmpPath(&tmp, "local-bad.sqlite");
    try testing.expectError(error.incompatible_profile, open(bad_path));
    try tmp.dir.writeFile(testing.io, .{ .sub_path = "local-empty.sqlite", .data = "" });
    const empty_path = try testing.tmpPath(&tmp, "local-empty.sqlite");
    try testing.expectError(error.incompatible_profile, open(empty_path));
}

test "profile storage rejects orphaned recovery journals" {
    const testing = @import("../../../testing.zig");
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(testing.io, .{ .sub_path = "local-lost.sqlite-wal", .data = "orphaned" });
    const path = try tmp.dir.realPathFileAlloc(testing.io, ".", testing.allocator);
    defer testing.allocator.free(path);
    try testing.expectError(error.incompatible_profile, preflight(testing.allocator, path));
}
