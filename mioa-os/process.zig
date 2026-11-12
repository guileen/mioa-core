const std = @import("std");
const lightpanda = @import("lightpanda");

pub const MIOA_PROCESS_OK: c_int = 0;
pub const MIOA_PROCESS_INVALID: c_int = 1;
pub const MIOA_PROCESS_NO_MEMORY: c_int = 2;
pub const MIOA_PROCESS_START_FAILED: c_int = 3;
pub const MIOA_PROCESS_WAIT_FAILED: c_int = 4;
pub const MIOA_PROCESS_CHILD_FAILED: c_int = 5;
pub const MIOA_PROCESS_NOT_FOUND: c_int = 6;
pub const MIOA_PROCESS_CAPTURE_FAILED: c_int = 7;

pub const MioaProcessCaptureResultV1 = extern struct {
    stdout_length: usize,
    stderr_length: usize,
    exit_code: i32,
    exited: u8,
};

fn hostEnvironment() std.process.Environ {
    return .{ .block = .{ .slice = std.mem.span(std.c.environ) } };
}

pub export fn mioa_process_run_with_stdin_v1(
    argv: [*c]const [*c]const u8,
    argc: usize,
    input: [*c]const u8,
    input_length: usize,
) c_int {
    if (argv == null or argc == 0 or (input == null and input_length != 0)) {
        return MIOA_PROCESS_INVALID;
    }

    const allocator = std.heap.page_allocator;
    const args = allocator.alloc([]const u8, argc) catch return MIOA_PROCESS_NO_MEMORY;
    defer allocator.free(args);
    for (0..argc) |index| {
        if (argv[index] == null) return MIOA_PROCESS_INVALID;
        args[index] = std.mem.span(argv[index]);
    }
    if (args[0].len == 0) return MIOA_PROCESS_INVALID;

    const environment = hostEnvironment();
    var environment_map = environment.createMap(allocator) catch return MIOA_PROCESS_NO_MEMORY;
    defer environment_map.deinit();

    var threaded: std.Io.Threaded = .init(allocator, .{ .environ = environment });
    defer threaded.deinit();
    const io = threaded.io();

    var child = std.process.spawn(io, .{
        .argv = args,
        .environ_map = &environment_map,
        .stdin = .pipe,
    }) catch |err| {
        return if (err == error.OutOfMemory) MIOA_PROCESS_NO_MEMORY else MIOA_PROCESS_START_FAILED;
    };

    if (child.stdin) |stdin| {
        var writer = stdin.writerStreaming(io, &.{});
        const bytes: []const u8 = if (input_length == 0) "" else input[0..input_length];
        writer.interface.writeAll(bytes) catch {};
        stdin.close(io);
        child.stdin = null;
    }

    const termination = child.wait(io) catch {
        child.kill(io);
        _ = child.wait(io) catch {};
        return MIOA_PROCESS_WAIT_FAILED;
    };
    return if (termination == .exited and termination.exited == 0)
        MIOA_PROCESS_OK
    else
        MIOA_PROCESS_CHILD_FAILED;
}
fn buffersOverlap(a: [*c]u8, a_len: usize, b: [*c]u8, b_len: usize) bool {
    if (a_len == 0 or b_len == 0) return false;
    const a_start = @intFromPtr(a);
    const b_start = @intFromPtr(b);
    return if (a_start <= b_start)
        b_start - a_start < a_len
    else
        a_start - b_start < b_len;
}

pub export fn mioa_process_capture_v1(
    argv: [*c]const [*c]const u8,
    argc: usize,
    stdout_buffer: [*c]u8,
    stdout_capacity: usize,
    stderr_buffer: [*c]u8,
    stderr_capacity: usize,
    result: [*c]MioaProcessCaptureResultV1,
) c_int {
    if (result == null) return MIOA_PROCESS_INVALID;
    result.* = .{
        .stdout_length = 0,
        .stderr_length = 0,
        .exit_code = 0,
        .exited = 0,
    };
    if (argv == null or argc == 0 or
        (stdout_buffer == null and stdout_capacity != 0) or
        (stderr_buffer == null and stderr_capacity != 0) or
        buffersOverlap(stdout_buffer, stdout_capacity, stderr_buffer, stderr_capacity))
    {
        return MIOA_PROCESS_INVALID;
    }

    const allocator = std.heap.page_allocator;
    const args = allocator.alloc([]const u8, argc) catch return MIOA_PROCESS_NO_MEMORY;
    defer allocator.free(args);
    for (0..argc) |index| {
        if (argv[index] == null) return MIOA_PROCESS_INVALID;
        args[index] = std.mem.span(argv[index]);
    }
    if (args[0].len == 0) return MIOA_PROCESS_INVALID;

    const environment = hostEnvironment();
    var environment_map = environment.createMap(allocator) catch return MIOA_PROCESS_NO_MEMORY;
    defer environment_map.deinit();

    var threaded: std.Io.Threaded = .init(allocator, .{ .environ = environment });
    defer threaded.deinit();
    const io = threaded.io();
    const captured = std.process.run(allocator, io, .{
        .argv = args,
        .environ_map = &environment_map,
        .stdout_limit = .limited(stdout_capacity),
        .stderr_limit = .limited(stderr_capacity),
    }) catch |err| {
        if (err == error.OutOfMemory) return MIOA_PROCESS_NO_MEMORY;
        if (err == error.FileNotFound) return MIOA_PROCESS_NOT_FOUND;
        return MIOA_PROCESS_CAPTURE_FAILED;
    };
    defer allocator.free(captured.stdout);
    defer allocator.free(captured.stderr);

    if (captured.stdout.len != 0) @memcpy(stdout_buffer[0..captured.stdout.len], captured.stdout);
    if (captured.stderr.len != 0) @memcpy(stderr_buffer[0..captured.stderr.len], captured.stderr);
    result.*.stdout_length = captured.stdout.len;
    result.*.stderr_length = captured.stderr.len;

    return switch (captured.term) {
        .exited => |code| blk: {
            result.*.exited = 1;
            result.*.exit_code = @intCast(code);
            break :blk if (code == 0) MIOA_PROCESS_OK else MIOA_PROCESS_CHILD_FAILED;
        },
        else => MIOA_PROCESS_CHILD_FAILED,
    };
}


pub export fn mioa_process_flush_v1() void {
    std.process.cleanExit(lightpanda.io);
}

pub export fn mioa_process_exit_v1(status: u8) noreturn {
    std.process.exit(status);
}

pub export fn mioa_process_pid_v1() i64 {
    const builtin = @import("builtin");
    if (builtin.os.tag == .windows) {
        return @intCast(std.os.windows.kernel32.GetCurrentProcessId());
    } else {
        return @intCast(std.c.getpid());
    }
}

pub export fn mioa_process_kill_v1(pid: i64, signal: i32) c_int {
    const builtin = @import("builtin");
    if (builtin.os.tag == .windows) {
        return MIOA_PROCESS_INVALID;
    } else {
        const rc = std.c.kill(@intCast(pid), @enumFromInt(@as(u32, @intCast(signal))));
        return if (rc == 0) MIOA_PROCESS_OK else MIOA_PROCESS_CHILD_FAILED;
    }
}

fn processExitProbeMode() ?[]const u8 {
    const prefix = "MIOA_PROCESS_EXIT_PROBE=";
    for (hostEnvironment().block.slice) |entry| {
        const entry_z = entry orelse continue;
        const value = std.mem.span(entry_z);
        if (std.mem.startsWith(u8, value, prefix)) return value[prefix.len..];
    }
    return null;
}

test "process API exit probe" {
    const mode = processExitProbeMode() orelse return;
    if (std.mem.eql(u8, mode, "flush")) {
        var stdout = std.Io.File.stdout().writerStreaming(lightpanda.io, &.{});
        try stdout.interface.writeAll("MIOA_PROCESS_CLEAN_EXIT_FLUSHED\n");
        mioa_process_flush_v1();
        return;
    }
    if (std.mem.eql(u8, mode, "failure")) mioa_process_exit_v1(37);
    return error.InvalidProcessExitProbe;
}

test "process API flush and immediate exit preserve status semantics" {
    if (processExitProbeMode() != null) return;
    const allocator = std.testing.allocator;
    var threaded: std.Io.Threaded = .init(std.heap.page_allocator, .{
        .environ = hostEnvironment(),
    });
    defer threaded.deinit();
    const io = threaded.io();
    const exe = try std.process.executablePathAlloc(io, allocator);
    defer allocator.free(exe);
    const argv = [_][]const u8{exe};

    var clean_environment = try hostEnvironment().createMap(allocator);
    defer clean_environment.deinit();
    try clean_environment.put("MIOA_PROCESS_EXIT_PROBE", "flush");
    var clean_child = try std.process.spawn(io, .{
        .argv = &argv,
        .environ_map = &clean_environment,
    });
    switch (try clean_child.wait(io)) {
        .exited => |status| try std.testing.expectEqual(@as(u8, 0), status),
        else => return error.CleanExitFailed,
    }

    var failure_environment = try hostEnvironment().createMap(allocator);
    defer failure_environment.deinit();
    try failure_environment.put("MIOA_PROCESS_EXIT_PROBE", "failure");
    var failure_child = try std.process.spawn(io, .{
        .argv = &argv,
        .environ_map = &failure_environment,
    });
    switch (try failure_child.wait(io)) {
        .exited => |status| try std.testing.expectEqual(@as(u8, 37), status),
        else => return error.ImmediateExitFailed,
    }
}

test "process API streams stdin and inherits the Host environment" {
    if (@import("builtin").os.tag == .windows) return error.SkipZigTest;
    const argv = [_][*c]const u8{
        "/bin/sh",
        "-c",
        "IFS= read -r line && [ \"$line\" = payload ] && [ \"$MIOA_PROCESS_API_TEST\" = inherited ]",
    };
    const input = "payload\n";
    const argv_ptr: [*c]const [*c]const u8 = @ptrCast(&argv);
    try std.testing.expectEqual(
        MIOA_PROCESS_OK,
        mioa_process_run_with_stdin_v1(argv_ptr, argv.len, input.ptr, input.len),
    );
}

test "process API reports invalid, missing, and failed children" {
    if (@import("builtin").os.tag == .windows) return error.SkipZigTest;
    try std.testing.expectEqual(
        MIOA_PROCESS_INVALID,
        mioa_process_run_with_stdin_v1(null, 0, null, 0),
    );
    const missing = [_][*c]const u8{"mioa-process-test-command-that-does-not-exist"};
    const missing_ptr: [*c]const [*c]const u8 = @ptrCast(&missing);
    try std.testing.expectEqual(
        MIOA_PROCESS_START_FAILED,
        mioa_process_run_with_stdin_v1(missing_ptr, missing.len, null, 0),
    );
    const failure = [_][*c]const u8{"/bin/sh", "-c", "exit 9"};
    const failure_ptr: [*c]const [*c]const u8 = @ptrCast(&failure);
    try std.testing.expectEqual(
        MIOA_PROCESS_CHILD_FAILED,
        mioa_process_run_with_stdin_v1(failure_ptr, failure.len, null, 0),
    );
}

test "process capture inherits Host environment and captures both streams" {
    if (@import("builtin").os.tag == .windows) return error.SkipZigTest;
    const argv = [_][*c]const u8{
        "/bin/sh",
        "-c",
        "printf '%s' \"$MIOA_PROCESS_API_TEST\"; printf 'warning' >&2",
    };
    var stdout_buffer: [64]u8 = undefined;
    var stderr_buffer: [64]u8 = undefined;
    var result: MioaProcessCaptureResultV1 = undefined;
    const argv_ptr: [*c]const [*c]const u8 = @ptrCast(&argv);
    try std.testing.expectEqual(
        MIOA_PROCESS_OK,
        mioa_process_capture_v1(
            argv_ptr,
            argv.len,
            &stdout_buffer,
            stdout_buffer.len,
            &stderr_buffer,
            stderr_buffer.len,
            &result,
        ),
    );
    try std.testing.expectEqual(@as(u8, 1), result.exited);
    try std.testing.expectEqual(@as(i32, 0), result.exit_code);
    try std.testing.expectEqualStrings("inherited", stdout_buffer[0..result.stdout_length]);
    try std.testing.expectEqualStrings("warning", stderr_buffer[0..result.stderr_length]);
}

test "process capture reports nonzero child status and bounds output" {
    if (@import("builtin").os.tag == .windows) return error.SkipZigTest;
    const failure = [_][*c]const u8{
        "/bin/sh",
        "-c",
        "printf 'token\\n'; printf 'detail\\n' >&2; exit 23",
    };
    var stdout_buffer: [64]u8 = undefined;
    var stderr_buffer: [64]u8 = undefined;
    var result: MioaProcessCaptureResultV1 = undefined;
    const failure_ptr: [*c]const [*c]const u8 = @ptrCast(&failure);
    try std.testing.expectEqual(
        MIOA_PROCESS_CHILD_FAILED,
        mioa_process_capture_v1(
            failure_ptr,
            failure.len,
            &stdout_buffer,
            stdout_buffer.len,
            &stderr_buffer,
            stderr_buffer.len,
            &result,
        ),
    );
    try std.testing.expectEqual(@as(u8, 1), result.exited);
    try std.testing.expectEqual(@as(i32, 23), result.exit_code);
    try std.testing.expectEqualStrings("token\n", stdout_buffer[0..result.stdout_length]);
    try std.testing.expectEqualStrings("detail\n", stderr_buffer[0..result.stderr_length]);

    const oversized = [_][*c]const u8{ "/bin/sh", "-c", "printf 123456" };
    const oversized_ptr: [*c]const [*c]const u8 = @ptrCast(&oversized);
    try std.testing.expectEqual(
        MIOA_PROCESS_CAPTURE_FAILED,
        mioa_process_capture_v1(
            oversized_ptr,
            oversized.len,
            &stdout_buffer,
            4,
            &stderr_buffer,
            stderr_buffer.len,
            &result,
        ),
    );
    try std.testing.expectEqual(@as(usize, 0), result.stdout_length);
    try std.testing.expectEqual(@as(usize, 0), result.stderr_length);
}

test "process capture rejects overlapping buffers and reports missing command" {
    if (@import("builtin").os.tag == .windows) return error.SkipZigTest;
    var buffer: [32]u8 = undefined;
    var result: MioaProcessCaptureResultV1 = undefined;
    const command = [_][*c]const u8{"/bin/true"};
    const command_ptr: [*c]const [*c]const u8 = @ptrCast(&command);
    try std.testing.expectEqual(
        MIOA_PROCESS_INVALID,
        mioa_process_capture_v1(
            command_ptr,
            command.len,
            &buffer,
            buffer.len,
            &buffer,
            buffer.len,
            &result,
        ),
    );

    const missing = [_][*c]const u8{"mioa-process-capture-command-that-does-not-exist"};
    const missing_ptr: [*c]const [*c]const u8 = @ptrCast(&missing);
    try std.testing.expectEqual(
        MIOA_PROCESS_NOT_FOUND,
        mioa_process_capture_v1(
            missing_ptr,
            missing.len,
            &buffer,
            buffer.len,
            null,
            0,
            &result,
        ),
    );
    try std.testing.expectEqual(@as(usize, 0), result.stdout_length);
    try std.testing.expectEqual(@as(usize, 0), result.stderr_length);
}
