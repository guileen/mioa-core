// Private connected Profile service protocol.
const std = @import("std");

pub const max_message = 1024 * 1024;
const Allocator = std.mem.Allocator;

pub const Client = struct {
    fd: std.posix.fd_t,
    next_id: u64 = 1,
    failed: bool = false,
    mutex: std.Io.Mutex = .init,
    // Accessed by the Session's serialized JS-thread event pump only.
    cookie_events_subscribed: bool = false,
    cookie_events_next_sequence: u64 = 0,
    event_fd: std.posix.fd_t = -1,
    cookie_event_buffer: std.ArrayList(u8) = .empty,
    cookie_event_allocator: ?Allocator = null,
    cookie_event_used: usize = 0,
    cookie_event_watcher_ready: bool = false,

    // The connected descriptor is an inherited capability; it is never derived
    // from a payload principal or from a pathname supplied by page code.
    pub fn init(fd: std.posix.fd_t) Client {
        return .{ .fd = fd };
    }

    pub fn deinit(self: *Client) void {
        if (self.cookie_event_allocator) |allocator| self.cookie_event_buffer.deinit(allocator);
        self.cookie_event_allocator = null;
        if (self.fd >= 0) _ = std.c.close(self.fd);
        if (self.event_fd >= 0) _ = std.c.close(self.event_fd);
        self.event_fd = -1;
        self.fd = -1;
        self.failed = true;
    }

    fn writeAll(self: *Client, bytes: []const u8) !void {
        const suspended = @import("../../TaskGate.zig").pauseForWait();
        defer @import("../../TaskGate.zig").resumeAfterWait(suspended);
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = std.c.send(self.fd, bytes.ptr + offset, bytes.len - offset, std.c.MSG.NOSIGNAL);
            if (n <= 0) return error.ProfileServiceLost;
            offset += @intCast(n);
        }
    }

    fn readAll(self: *Client, bytes: []u8) !void {
        const suspended = @import("../../TaskGate.zig").pauseForWait();
        defer @import("../../TaskGate.zig").resumeAfterWait(suspended);
        var offset: usize = 0;
        while (offset < bytes.len) {
            const n = std.c.read(self.fd, bytes.ptr + offset, bytes.len - offset);
            if (n <= 0) return error.ProfileServiceLost;
            offset += @intCast(n);
        }
    }

    pub fn request(self: *Client, allocator: Allocator, operation: []const u8, payload: anytype) !std.json.Parsed(std.json.Value) {
        const suspended = @import("../../TaskGate.zig").pauseForWait();
        self.mutex.lockUncancelable(@import("lightpanda").io);
        @import("../../TaskGate.zig").resumeAfterWait(suspended);
        defer self.mutex.unlock(@import("lightpanda").io);
        if (self.failed or self.fd < 0) return error.ProfileServiceLost;
        if (self.next_id >= std.math.maxInt(i64)) return error.ProfileRequestExhausted;
        const id = self.next_id;
        self.next_id += 1;
        const message = try std.json.Stringify.valueAlloc(allocator, .{
            .version = @as(u8, 1),
            .id = id,
            .operation = operation,
            .payload = payload,
        }, .{});
        defer allocator.free(message);
        if (message.len == 0 or message.len > max_message) return error.ProfileMessageTooLarge;
        var prefix: [4]u8 = undefined;
        std.mem.writeInt(u32, &prefix, @intCast(message.len), .big);
        self.writeAll(&prefix) catch |err| {
            self.failed = true;
            return err;
        };
        self.writeAll(message) catch |err| {
            self.failed = true;
            return err;
        };
        self.readAll(&prefix) catch |err| {
            self.failed = true;
            return err;
        };
        const size = std.mem.readInt(u32, &prefix, .big);
        if (size == 0 or size > max_message) {
            self.failed = true;
            return error.ProfileProtocolError;
        }
        const bytes = allocator.alloc(u8, size) catch |err| {
            // A response has already started; the stream cannot be reused.
            self.failed = true;
            return err;
        };
        defer allocator.free(bytes);
        self.readAll(bytes) catch |err| {
            self.failed = true;
            return err;
        };
        const response = std.json.parseFromSlice(std.json.Value, allocator, bytes, .{ .allocate = .alloc_always }) catch {
            self.failed = true;
            return error.ProfileProtocolError;
        };
        errdefer response.deinit();
        if (response.value != .object) {
            self.failed = true;
            return error.ProfileProtocolError;
        }
        const fields = response.value.object;
        const reply_id = fields.get("id") orelse {
            self.failed = true;
            return error.ProfileProtocolError;
        };
        const ok = fields.get("ok") orelse {
            self.failed = true;
            return error.ProfileProtocolError;
        };
        if (reply_id != .integer or reply_id.integer < 0 or @as(u64, @intCast(reply_id.integer)) != id or ok != .bool) {
            self.failed = true;
            return error.ProfileProtocolError;
        }
        if (!ok.bool) {
            if (fields.get("effect")) |effect| {
                if (effect == .string and std.mem.eql(u8, effect.string, "unknown")) return error.ProfileSideEffectUnknown;
            }
            if (fields.get("error_code")) |code| {
                if (code == .string) {
                    inline for (.{
                        error.Constraint,           error.NotFound,             error.Abort,                  error.Auth,              error.Busy,
                        error.CantOpen,             error.Corrupt,              error.Empty,                  error.Error,             error.Format,
                        error.Full,                 error.Internal,             error.Interrupt,              error.IoErr,             error.Locked,
                        error.Mismatch,             error.Misuse,               error.NoLFS,                  error.NoMem,             error.NotADB,
                        error.Notfound,             error.Notice,               error.Perm,                   error.Protocol,          error.Range,
                        error.ReadOnly,             error.Schema,               error.TooBig,                 error.Warning,           error.ConstraintCheck,
                        error.ConstraintCommithook, error.ConstraintForeignKey, error.ConstraintFunction,     error.ConstraintNotNull, error.ConstraintPrimaryKey,
                        error.ConstraintTrigger,    error.ConstraintUnique,     error.ConstraintVTab,         error.ConstraintRowId,   error.ConstraintPinned,
                        error.ConstraintDatatype,   error.ErrorMissingCollseq,  error.ErrorRetry,             error.ErrorSnapshot,     error.IoerrRead,
                        error.IoerrShortRead,       error.IoerrWrite,           error.IoerrFsync,             error.IoerrDir_fsync,    error.IoerrTruncate,
                        error.IoerrFstat,           error.IoerrUnlock,          error.IoerrRdlock,            error.IoerrDelete,       error.IoerrBlocked,
                        error.IoerrNomem,           error.IoerrAccess,          error.IoerrCheckreservedlock, error.IoerrLock,         error.IoerrClose,
                        error.IoerrDirClose,        error.IoerrShmopen,         error.IoerrShmsize,           error.IoerrShmlock,      error.IoerrShmmap,
                        error.IoerrSeek,            error.IoerrDeleteNoent,     error.IoerrMmap,              error.IoerrGetTempPath,  error.IoerrConvPath,
                        error.IoerrVnode,           error.IoerrAuth,            error.IoerrBeginAtomic,       error.IoerrCommitAtomic, error.IoerrRollbackAtomic,
                        error.IoerrData,            error.IoerrCorruptFS,       error.LockedSharedCache,      error.LockedVTab,        error.BusyRecovery,
                        error.BusySnapshot,         error.BusyTimeout,          error.CantOpenNoTempDir,      error.CantOpenIsDir,     error.CantOpenFullPath,
                        error.CantOpenConvPath,     error.CantOpenDirtyWal,     error.CantOpenSymlink,        error.CorruptVTab,       error.CorruptSequence,
                        error.CorruptIndex,         error.ReadonlyRecovery,     error.ReadonlyCantlock,       error.ReadonlyRollback,  error.ReadonlyDbMoved,
                        error.ReadonlyCantInit,     error.ReadonlyDirectory,    error.AbortRollback,          error.NoticeRecoverWal,  error.NoticeRecoverRollback,
                        error.WarningAutoIndex,     error.AuthUser,             error.OkLoadPermanently,      error.Unknown,           error.OutOfMemory,
                        error.InvalidArgument,      error.UnknownError,         error.QuotaExceeded,
                    }) |known| {
                        if (std.mem.eql(u8, code.string, @errorName(known))) return known;
                    }
                }
            }
            return error.ProfileRequestRejected;
        }
        if (fields.get("result") == null) {
            self.failed = true;
            return error.ProfileProtocolError;
        }
        return response;
    }
};
