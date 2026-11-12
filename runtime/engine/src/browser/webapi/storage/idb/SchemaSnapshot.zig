// A connection-local, committed schema view. Upgrade transactions clone this
// into their own arena so an abort cannot mutate a previously published view.
const std = @import("std");
const Engine = @import("Engine.zig");
const Key = @import("Key.zig");
const Allocator = std.mem.Allocator;

pub const Index = struct {
    name: []const u8,
    info: Engine.IndexInfo,
};
pub const Store = struct {
    name: []const u8,
    info: Engine.StoreInfo,
    indexes: std.ArrayList(Index) = .empty,
};

pub const Snapshot = struct {
    stores: std.ArrayList(Store) = .empty,
    next_store_id: i64 = 1,
    next_index_id: i64 = 1,

    pub fn allocStoreId(self: *Snapshot) !i64 {
        if (self.next_store_id <= 0 or self.next_store_id == std.math.maxInt(i64)) return error.ProfileRequestExhausted;
        const result = self.next_store_id;
        self.next_store_id += 1;
        return result;
    }
    pub fn allocIndexId(self: *Snapshot) !i64 {
        if (self.next_index_id <= 0 or self.next_index_id == std.math.maxInt(i64)) return error.ProfileRequestExhausted;
        const result = self.next_index_id;
        self.next_index_id += 1;
        return result;
    }

    pub fn findStore(self: *const Snapshot, name: []const u8) ?*const Store {
        for (self.stores.items) |*store| if (std.mem.eql(u8, store.name, name)) return store;
        return null;
    }
    pub fn storeById(self: *const Snapshot, id: i64) ?*const Store {
        for (self.stores.items) |*store| if (store.info.id == id) return store;
        return null;
    }
    fn mutableStore(self: *Snapshot, id: i64) ?*Store {
        for (self.stores.items) |*store| if (store.info.id == id) return store;
        return null;
    }
    pub fn indexByName(self: *const Snapshot, store_id: i64, name: []const u8) ?*const Index {
        const store = self.storeById(store_id) orelse return null;
        for (store.indexes.items) |*index| if (std.mem.eql(u8, index.name, name)) return index;
        return null;
    }
    pub fn indexById(self: *const Snapshot, id: i64) ?*const Index {
        for (self.stores.items) |*store| for (store.indexes.items) |*index| {
            if (index.info.id == id) return index;
        };
        return null;
    }
    pub fn addStore(self: *Snapshot, allocator: Allocator, name: []const u8, info: Engine.StoreInfo) !void {
        if (self.findStore(name) != null or self.storeById(info.id) != null) return error.Constraint;
        const owned_name = try allocator.dupe(u8, name);
        const path = if (info.key_path) |p| try Key.dupeKeyPath(allocator, p) else null;
        try self.stores.append(allocator, .{ .name = owned_name, .info = .{ .id = info.id, .key_path = path, .auto_increment = info.auto_increment } });
    }
    pub fn addIndex(self: *Snapshot, allocator: Allocator, store_id: i64, name: []const u8, info: Engine.IndexInfo) !void {
        if (self.indexByName(store_id, name) != null or self.indexById(info.id) != null) return error.Constraint;
        if (self.storeById(store_id) == null) return error.NotFound;
        const owned_name = try allocator.dupe(u8, name);
        const path = try Key.dupeKeyPath(allocator, info.key_path);
        const store = self.mutableStore(store_id).?;
        try store.indexes.append(allocator, .{ .name = owned_name, .info = .{ .id = info.id, .key_path = path, .unique = info.unique, .multi_entry = info.multi_entry } });
    }
    pub fn removeStore(self: *Snapshot, name: []const u8) !void {
        for (self.stores.items, 0..) |store, i| if (std.mem.eql(u8, store.name, name)) {
            _ = self.stores.orderedRemove(i);
            return;
        };
        return error.NotFound;
    }
    pub fn removeIndex(self: *Snapshot, store_id: i64, name: []const u8) !void {
        const store = self.mutableStore(store_id) orelse return error.NotFound;
        for (store.indexes.items, 0..) |index, i| if (std.mem.eql(u8, index.name, name)) {
            _ = store.indexes.orderedRemove(i);
            return;
        };
        return error.NotFound;
    }
    pub fn renameStore(self: *Snapshot, allocator: Allocator, id: i64, name: []const u8) !void {
        if (self.findStore(name)) |existing| if (existing.info.id != id) return error.Constraint;
        const store = self.mutableStore(id) orelse return error.NotFound;
        store.name = try allocator.dupe(u8, name);
    }
    pub fn renameIndex(self: *Snapshot, allocator: Allocator, id: i64, name: []const u8) !void {
        for (self.stores.items) |*store| {
            var target: ?*Index = null;
            for (store.indexes.items) |*index| if (index.info.id == id) {
                target = index;
                break;
            };
            if (target == null) continue;
            for (store.indexes.items) |index| if (index.info.id != id and std.mem.eql(u8, index.name, name)) return error.Constraint;
            if (target) |index| {
                index.name = try allocator.dupe(u8, name);
                return;
            }
        }
        return error.NotFound;
    }
    pub fn clone(self: *const Snapshot, allocator: Allocator) !Snapshot {
        var copy: Snapshot = .{ .next_store_id = self.next_store_id, .next_index_id = self.next_index_id };
        for (self.stores.items) |store| {
            try copy.addStore(allocator, store.name, store.info);
            for (store.indexes.items) |index| try copy.addIndex(allocator, store.info.id, index.name, index.info);
        }
        return copy;
    }
    pub fn storeNames(self: *const Snapshot, allocator: Allocator) ![]const []const u8 {
        const names = try allocator.alloc([]const u8, self.stores.items.len);
        for (self.stores.items, 0..) |store, i| names[i] = try allocator.dupe(u8, store.name);
        std.mem.sort([]const u8, names, {}, lessThan);
        return names;
    }
    pub fn indexNames(self: *const Snapshot, allocator: Allocator, store_id: i64) ![]const []const u8 {
        const store = self.storeById(store_id) orelse return error.NotFound;
        const names = try allocator.alloc([]const u8, store.indexes.items.len);
        for (store.indexes.items, 0..) |index, i| names[i] = try allocator.dupe(u8, index.name);
        std.mem.sort([]const u8, names, {}, lessThan);
        return names;
    }
    pub fn indexesForStore(self: *const Snapshot, allocator: Allocator, store_id: i64) ![]Engine.IndexInfo {
        const store = self.storeById(store_id) orelse return error.NotFound;
        const result = try allocator.alloc(Engine.IndexInfo, store.indexes.items.len);
        for (store.indexes.items, 0..) |index, i| result[i] = index.info;
        return result;
    }
};

fn lessThan(_: void, a: []const u8, b: []const u8) bool {
    return std.mem.lessThan(u8, a, b);
}
