// Copyright (C) 2023-2026  Lightpanda (Selecy SAS)
//
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

const std = @import("std");
const lp = @import("lightpanda");
const EnvironmentApi = @import("environment_api");

const CookieOwnerBootstrap = struct {
    fd: std.posix.fd_t,
    database: [:0]const u8,
};

const IndexedDbOwnerBootstrap = struct {
    fd: std.posix.fd_t,
    grant_fd: std.posix.fd_t,
    directory: []const u8,
};

fn environmentSnapshot(name: [*:0]const u8) !?EnvironmentApi.mioa_environment_value_v1 {
    var value: EnvironmentApi.mioa_environment_value_v1 = .{ .value = null, .length = 0 };
    switch (EnvironmentApi.mioa_environment_get_v1(name, &value)) {
        EnvironmentApi.MIOA_ENVIRONMENT_OK => return value,
        EnvironmentApi.MIOA_ENVIRONMENT_MISSING => return null,
        EnvironmentApi.MIOA_ENVIRONMENT_NO_MEMORY => return error.OutOfMemory,
        else => return error.InvalidEnvironment,
    }
}

fn releaseEnvironmentSnapshot(value: *?EnvironmentApi.mioa_environment_value_v1) void {
    if (value.*) |*snapshot| EnvironmentApi.mioa_environment_value_release_v1(snapshot);
    value.* = null;
}

fn environmentPresent(name: [*:0]const u8) !bool {
    var present: c_int = 0;
    const status = EnvironmentApi.mioa_environment_has_v1(name, &present);
    if (status != EnvironmentApi.MIOA_ENVIRONMENT_OK) return error.InvalidEnvironment;
    return present != 0;
}

fn ownerDescriptor(raw: []const u8) !std.posix.fd_t {
    const fd = std.fmt.parseInt(std.posix.fd_t, raw, 10) catch return error.InvalidProfileOwner;
    if (fd < 0) return error.InvalidProfileOwner;
    return fd;
}

fn cookieOwnerBootstrap(fd_raw: ?[]const u8, database: ?[:0]const u8) !?CookieOwnerBootstrap {
    const raw_fd = fd_raw orelse return null;
    const path = database orelse return error.InvalidProfileOwner;
    return .{ .fd = try ownerDescriptor(raw_fd), .database = path };
}

fn indexedDbOwnerBootstrap(
    fd_raw: ?[]const u8,
    directory: ?[]const u8,
    grant_raw: ?[]const u8,
) !?IndexedDbOwnerBootstrap {
    const raw_fd = fd_raw orelse return null;
    const raw_directory = directory orelse return error.InvalidProfileOwner;
    const raw_grant = grant_raw orelse return error.InvalidProfileOwner;
    const fd = try ownerDescriptor(raw_fd);
    const grant_fd = try ownerDescriptor(raw_grant);
    if (fd == grant_fd) return error.InvalidProfileOwner;
    return .{ .fd = fd, .grant_fd = grant_fd, .directory = raw_directory };
}

fn profileOwnerConflictsWithCookieFiles(owner_present: bool, cookie_file: bool, cookie_jar_file: bool) bool {
    return owner_present and (cookie_file or cookie_jar_file);
}
const Allocator = std.mem.Allocator;

const log = lp.log;
const App = lp.App;
const Config = lp.Config;
const SigHandler = @import("Sighandler.zig");
pub const panic = lp.crash_handler.panic;

pub const std_options: std.Options = .{
    .signal_stack_size = null,
};

pub fn main(init: std.process.Init) !void {
    // Standalone CLI keeps its original debug/C allocator. Managed application
    // objects use the shared native allocator over admitted page backing.
    var gpa_instance: std.heap.SafeAllocator = .init(std.heap.page_allocator, .{ .stack_trace_frames = 10 });
    const initial_gpa = if (lp.IS_DEBUG) gpa_instance.allocator() else std.heap.c_allocator;
    // Install admitted backing before any application arena or native engine.
    const gpa = try lp.RuntimeMemory.allocator(initial_gpa);

    defer if (lp.IS_DEBUG) {
        if (gpa_instance.deinit() != 0) std.process.exit(1);
    };
    defer lp.RuntimeMemory.shutdown();
    // SQLite configuration must precede every connection and owner-mode branch.
    // Ungoverned owner helpers keep their existing default SQLite allocator.
    try lp.Sqlite.installRuntimeAllocator();

    // arena for main-specific allocations
    var main_arena_instance = std.heap.ArenaAllocator.init(gpa);
    const main_arena = main_arena_instance.allocator();
    defer main_arena_instance.deinit();

    // Only the Profile lease owner supplies this connected capability to its
    // dedicated child. Ordinary runtime processes must never inherit it.
    var cookie_fd_env = try environmentSnapshot("MIOA_COOKIE_OWNER_FD");
    defer releaseEnvironmentSnapshot(&cookie_fd_env);
    if (cookie_fd_env) |fd_value| {
        var cookie_db_env = try environmentSnapshot("MIOA_COOKIE_OWNER_DB");
        defer releaseEnvironmentSnapshot(&cookie_db_env);
        const raw_db = if (cookie_db_env) |value| value.value[0..value.length :0] else null;
        const owner = (try cookieOwnerBootstrap(fd_value.value[0..fd_value.length], raw_db)) orelse unreachable;
        try lp.CookieOwner.serve(gpa, owner.fd, owner.database);
        return;
    }

    var idb_fd_env = try environmentSnapshot("MIOA_IDB_OWNER_FD");
    defer releaseEnvironmentSnapshot(&idb_fd_env);
    if (idb_fd_env) |fd_value| {
        var idb_dir_env = try environmentSnapshot("MIOA_IDB_OWNER_DIR");
        defer releaseEnvironmentSnapshot(&idb_dir_env);
        var idb_grant_env = try environmentSnapshot("MIOA_IDB_GRANT_FD");
        defer releaseEnvironmentSnapshot(&idb_grant_env);
        const raw_directory = if (idb_dir_env) |value| value.value[0..value.length] else null;
        const raw_grant = if (idb_grant_env) |value| value.value[0..value.length] else null;
        const owner = (try indexedDbOwnerBootstrap(fd_value.value[0..fd_value.length], raw_directory, raw_grant)) orelse unreachable;
        try lp.IndexedDBOwner.serve(gpa, owner.fd, owner.grant_fd, owner.directory);
        return;
    }

    run(gpa, main_arena, init.minimal.args) catch |err| {
        if (err == error.UserCancelled) std.process.exit(130);
        // error.AgentFailed: the agent thread reported the failure in-context.
        // error.PageFailed: fetch already logged every failing url.
        // lp.Agent.UserError: a user-facing message was already printed.
        if (err == error.AgentFailed or err == error.PageFailed or lp.Agent.isUserError(err)) std.process.exit(1);
        // curl's code for --fail on an HTTP error, also already reported per url.
        if (err == error.HttpError) std.process.exit(22);
        log.fatal(.app, "exit", .{ .err = err });
        std.process.exit(1);
    };
}

fn run(allocator: Allocator, main_arena: Allocator, proc_args: std.process.Args) !void {
    lp.core_dump.disableIfRequested();
    lp.malloc_tuning.apply();
    lp.crash_handler.attachSignalHandlers();

    const args = Config.parseArgs(main_arena, proc_args) catch |err| switch (err) {
        // Already logged where they were found.
        error.UnknownCommand,
        error.UnknownOption,
        error.InvalidArgument,
        error.MissingArgument,
        error.TooManyPositionalArguments,
        => std.process.exit(1),
        else => return err,
    };
    defer args.deinit(main_arena);

    // Cookie-file imports/exports operate on a local jar and must never be
    // combined with the authoritative shared Profile connection.
    const profile_owner_present = try environmentPresent("MIOA_PROFILE_SERVICE_FD");
    if (profileOwnerConflictsWithCookieFiles(profile_owner_present, args.cookieFile() != null, args.cookieJarFile() != null)) {
        return error.InvalidProfileOwner;
    }

    switch (args.mode) {
        .help => |tag| return args.printUsageAndExit(main_arena, tag, true),
        .version => |opts| {
            if (opts.check) {
                try lp.checkVersion(allocator, &args);
            } else {
                var stdout = std.Io.File.stdout().writerStreaming(lp.io, &.{});
                try stdout.interface.print("{s}\n", .{lp.build_config.version});
            }
            return std.process.cleanExit(lp.io);
        },
        .completion => |opts| {
            const shell = opts.shell orelse return args.printUsageAndExit(main_arena, .completion, false);
            var buf: [4096]u8 = undefined;
            var stdout = std.Io.File.stdout().writerStreaming(lp.io, &buf);
            try lp.completion.write(&stdout.interface, shell, args.exec_name);
            try stdout.interface.flush();
            return std.process.cleanExit(lp.io);
        },
        .agent => |opts| if (opts.list_models) {
            try lp.Agent.listModels(allocator, opts);
            return std.process.cleanExit(lp.io);
        },
        else => {},
    }

    // must be installed before any other threads
    const sighandler = try main_arena.create(SigHandler);
    sighandler.* = .{ .arena = main_arena };
    try sighandler.install();

    // _app is global to handle graceful shutdown.
    var app = try App.init(allocator, &args);
    defer app.deinit();

    app.telemetry.record(.{ .run = {} });

    logConfigTips(app.config);

    defer if (app.config.dumpMetricsOnExit()) {
        var writer = std.Io.File.stdout().writerStreaming(lp.io, &.{});
        lp.metrics.write(&writer.interface);
    };

    switch (args.mode) {
        .serve => |opts| {
            log.debug(.app, "startup", .{ .mode = "serve", .snapshot = app.snapshot.fromEmbedded() });
            const address: lp.Server.Address = if (opts.unix_socket) |path| .{ .unix = path } else .{
                .ip = std.Io.net.IpAddress.parse(args.serveHost(), args.port()) catch |err| {
                    log.fatal(.app, "invalid server address", .{ .err = err, .host = args.serveHost(), .port = args.port() });
                    return args.printUsageAndExit(main_arena, .serve, false);
                },
            };

            var server = lp.Server.init(app, address) catch |err| {
                if (err != error.AddressInUse) {
                    log.fatal(.app, "server run error", .{ .err = err });
                } else switch (address) {
                    .ip => log.fatal(.app, "address already in use", .{
                        .host = args.serveHost(),
                        .port = args.port(),
                        .hint = "Another process is already listening on this address. " ++
                            "Stop the other process or use --port to choose a different port.",
                    }),
                    .unix => |path| log.fatal(.app, "address already in use", .{
                        .path = path,
                        .hint = "The path already exists. If no other process is listening on it, delete it.",
                    }),
                }
                return err;
            };
            defer server.deinit();

            try sighandler.on(lp.Server.shutdown, .{server});

            server.run();
        },
        .fetch => |opts| {
            const urls = opts.url.items;

            // Plain (non-JSON) dump writes one document to stdout with no
            // framing, so it can't disambiguate more than one page.
            if (urls.len == 1) {
                log.debug(.app, "startup", .{
                    .mode = "fetch",
                    .dump_mode = opts.dump,
                    .url = urls[0],
                    .snapshot = app.snapshot.fromEmbedded(),
                });
            } else {
                if (opts.json == false) {
                    log.fatal(.app, "multiple URLs require --json", .{});
                    return error.InvalidArgument;
                }
                log.debug(.app, "startup", .{
                    .mode = "fetch",
                    .dump_mode = opts.dump,
                    .url_count = urls.len,
                    .snapshot = app.snapshot.fromEmbedded(),
                });
            }

            if (opts.dump == null and opts.dump_selector != null) {
                log.fatal(.app, "--dump-selector needs --dump", .{});
                return error.InvalidArgument;
            }
            if (opts.dump == null and opts.dump_max_bytes != null) {
                log.fatal(.app, "--dump-max-bytes needs --dump", .{});
                return error.InvalidArgument;
            }
            if (opts.dump_max_bytes != null and opts.dump != .html and opts.dump != .markdown) {
                log.fatal(.app, "--dump-max-bytes needs text", .{ .dump = opts.dump, .allowed = "html, markdown" });
                return error.InvalidArgument;
            }
            if (opts.markdown_alternate and opts.dump != .markdown) {
                log.fatal(.app, "invalid --markdown-alternate", .{ .needs = "--dump markdown" });
                return error.InvalidArgument;
            }
            if (opts.markdown_alternate and opts.dump_selector != null) {
                log.fatal(.app, "invalid --markdown-alternate", .{ .conflicts_with = "--dump-selector" });
                return error.InvalidArgument;
            }

            var fetch_opts = lp.FetchOpts{
                .wait_ms = opts.wait_ms,
                .wait_until = opts.wait_until,
                .wait_script = opts.wait_script,
                .inject_script = opts.inject_script,
                .wait_selector = opts.wait_selector,
                .dump_mode = opts.dump,
                .selector = opts.dump_selector,
                .fail_on_http_error = opts.fail_on_http_error,
                .markdown_alternate = opts.markdown_alternate,
                .dump = .{
                    .strip = opts.strip_mode,
                    .with_base = opts.with_base,
                    .with_frames = opts.with_frames,
                    .max_bytes = opts.dump_max_bytes,
                },
                .json = opts.json,
            };

            var writer = std.Io.File.stdout().writerStreaming(lp.io, &.{});
            if (opts.dump != null or opts.json) {
                fetch_opts.writer = &writer.interface;
            }

            // Browser owns a V8 isolate, which has thread affinity — it must
            // be init/used/deinit on the same thread (fetchThread, below). So
            // we can't treat Browser like the above serve path treats Server.
            // We need Browser to be createdin fetchThread and to get a reference
            // to it here.
            var ft: FetchTerminator = .{};
            try sighandler.on(FetchTerminator.terminate, .{&ft});
            if (opts.terminate_ms) |ms| {
                try sighandler.deadline(ms);
            }

            var fetch_err: ?anyerror = null;
            var worker_thread = try std.Thread.spawn(.{}, fetchThread, .{ app, &ft, urls, fetch_opts, &fetch_err });
            worker_thread.join();
            if (fetch_err) |err| return err;
        },
        .mcp => |opts| {
            log.info(.mcp, "starting server", .{});

            // --port serves MCP over HTTP instead of stdio. It and --cdp-port
            // each run their accept loop on this thread, so they can't combine.
            if (opts.port) |port| {
                if (opts.cdp_port != null) {
                    log.fatal(.mcp, "port conflicts with cdp-port", .{ .hint = "both need the main thread for their accept loop" });
                    return error.InvalidArgument;
                }
                const address = std.Io.net.IpAddress.parse(opts.host, port) catch |err| {
                    log.fatal(.mcp, "invalid address", .{ .err = err, .host = opts.host, .port = port });
                    return err;
                };
                const http_server = try lp.mcp.HttpServer.init(allocator, app);
                defer http_server.deinit();
                // A signal stops the accept loop, run() returns, deinit joins.
                try sighandler.on(lp.mcp.HttpServer.stop, .{http_server});
                http_server.run(address) catch |err| {
                    log.fatal(.mcp, "mcp http error", .{ .err = err });
                    return err;
                };
                return;
            }

            var cdp_server: ?*lp.Server = null;
            if (opts.cdp_port) |port| {
                const address = std.Io.net.IpAddress.parse("127.0.0.1", port) catch |err| {
                    log.fatal(.mcp, "invalid cdp address", .{ .err = err, .port = port });
                    return;
                };
                cdp_server = try lp.Server.init(app, .{ .ip = address });
                try sighandler.on(lp.Server.shutdown, .{cdp_server.?});
            }
            defer if (cdp_server) |s| s.deinit();

            var mcp_err: ?anyerror = null;
            {
                var worker_thread = try std.Thread.spawn(.{}, mcpThread, .{ allocator, app, cdp_server, &mcp_err });
                defer worker_thread.join();

                // mcp talks over stdio on mcpThread. Only run the CDP accept/read
                // loop when an optional CDP server was started; otherwise the main
                // thread just waits for the worker.
                if (cdp_server) |s| {
                    s.run();
                }
            }
            if (mcp_err) |err| return err;
        },
        .agent => |opts| {
            log.info(.app, "starting agent", .{});

            // Ctrl-C cancels the current turn; signals never kill the
            // process. `/quit` (or Ctrl-D on an empty prompt) exits.
            sighandler.no_hard_exit = true;

            var sig_bridge: lp.Agent.SigBridge = .{};
            try sighandler.on(lp.Agent.SigBridge.onSignal, .{&sig_bridge});

            var failed: bool = false;
            var cancelled: bool = false;
            {
                var worker_thread = try std.Thread.spawn(.{}, agentThread, .{
                    allocator,
                    app,
                    opts,
                    &failed,
                    &cancelled,
                    &sig_bridge,
                });
                worker_thread.join();
            }

            if (cancelled) return error.UserCancelled;
            if (failed) return error.AgentFailed;
        },
        else => unreachable,
    }
}

fn agentThread(
    allocator: std.mem.Allocator,
    app: *App,
    opts: Config.Agent,
    failed: *bool,
    cancelled: *bool,
    sig_bridge: *lp.Agent.SigBridge,
) void {
    var agent_instance = lp.Agent.init(allocator, app, opts) catch |err| {
        if (err == error.UserCancelled) {
            cancelled.* = true;
        } else {
            // UserError: message already printed inside Agent.init.
            if (!lp.Agent.isUserError(err)) log.fatal(.app, "agent init error", .{ .err = err });
            failed.* = true;
        }
        return;
    };
    sig_bridge.attach(agent_instance);
    defer agent_instance.deinit();
    defer sig_bridge.detach();

    if (agent_instance.ai_client) |cli| {
        app.telemetry.record(.{
            .llm = app.telemetry.llm_init(@tagName(cli), agent_instance.model),
        });
    } else {
        app.telemetry.record(.{
            .llm = app.telemetry.llm_init("nollm", null),
        });
    }

    if (!agent_instance.run()) {
        failed.* = true;
    }
}

const FetchTerminator = struct {
    mutex: std.Io.Mutex = .init,
    browser: ?*lp.Browser = null,

    fn storeBrowser(self: *FetchTerminator, browser: *lp.Browser) void {
        self.mutex.lockUncancelable(lp.io);
        defer self.mutex.unlock(lp.io);
        self.browser = browser;
    }

    fn releaseBrowser(self: *FetchTerminator) void {
        self.mutex.lockUncancelable(lp.io);
        defer self.mutex.unlock(lp.io);
        const b = self.browser orelse return;
        b.env.cancelTerminate();
        self.browser = null;
    }

    fn terminate(self: *FetchTerminator) void {
        self.mutex.lockUncancelable(lp.io);
        defer self.mutex.unlock(lp.io);
        const b = self.browser orelse return;
        b.env.terminate();
        self.browser = null;
    }
};

fn fetchThread(app: *App, ft: *FetchTerminator, urls: []const [:0]const u8, fetch_opts: lp.FetchOpts, err_out: *?anyerror) void {
    var browser: lp.Browser = undefined;
    browser.init(app, .{}) catch |err| {
        err_out.* = err;
        log.fatal(.app, "browser init error", .{ .err = err });
        return;
    };
    defer browser.deinit();

    ft.storeBrowser(&browser);
    // if this exits normally, we want to disarm the FetchTerminator so that
    // any subsequent sighandlers don't try to shutdown an already (or in-the-
    // process-of) shutting down browser/env
    defer ft.releaseBrowser();

    lp.fetch(app, &browser, urls, fetch_opts) catch |err| {
        err_out.* = err;
        // Both are already reported per url by fetch itself.
        if (err == error.PageFailed or err == error.HttpError) return;
        log.fatal(.app, "fetch error", .{ .err = err, .url_count = urls.len });
    };
}

fn mcpThread(allocator: std.mem.Allocator, app: *App, cdp_server: ?*lp.Server, err_out: *?anyerror) void {
    defer if (cdp_server) |s| {
        s.shutdown();
    };

    var stdout = std.Io.File.stdout().writerStreaming(lp.io, &.{});
    var mcp_server: *lp.mcp.Server = lp.mcp.Server.init(allocator, app, &stdout.interface) catch |err| {
        err_out.* = err;
        log.fatal(.mcp, "mcp init error", .{ .err = err });
        return;
    };
    defer mcp_server.deinit();

    var stdin_buf: [64 * 1024]u8 = undefined;
    var stdin = std.Io.File.stdin().readerStreaming(lp.io, &stdin_buf);
    lp.mcp.router.processRequests(mcp_server, &stdin.interface, std.Io.File.stdin()) catch |err| {
        err_out.* = err;
        log.fatal(.mcp, "mcp error", .{ .err = err });
    };
}

fn logConfigTips(config: *const Config) void {
    // Only for a person reading the terminal, not for scripts capturing stderr.
    if (!Config.stderrIsTty()) return;

    var count: usize = 0;
    var tips: [2]log.KV = undefined;
    if (config.obeyRobots() == false) {
        tips[count] = .init("robots", "use '--obey-robots' to use a sites robots.txt");
        count += 1;
    }

    if (count > 0) {
        tips[count] = .init("meta", "use '--log-filter note' to silence this message");
        log.logKVs(.note, .note, "config tips", tips[0 .. count + 1]);
    }
}
test "cookie owner bootstrap validates presence and descriptor" {
    try std.testing.expect(try cookieOwnerBootstrap(null, null) == null);
    try std.testing.expectError(error.InvalidProfileOwner, cookieOwnerBootstrap("5", null));
    try std.testing.expectError(error.InvalidProfileOwner, cookieOwnerBootstrap("", "/profile/cookies.db"));
    try std.testing.expectError(error.InvalidProfileOwner, cookieOwnerBootstrap("bad", "/profile/cookies.db"));
    try std.testing.expectError(error.InvalidProfileOwner, cookieOwnerBootstrap("-1", "/profile/cookies.db"));

    const owner = (try cookieOwnerBootstrap("0", "/profile/cookies.db")).?;
    try std.testing.expectEqual(@as(std.posix.fd_t, 0), owner.fd);
    try std.testing.expectEqualStrings("/profile/cookies.db", owner.database);
}

test "indexed db owner bootstrap requires distinct valid descriptors and fields" {
    try std.testing.expect(try indexedDbOwnerBootstrap(null, null, null) == null);
    try std.testing.expectError(error.InvalidProfileOwner, indexedDbOwnerBootstrap("5", null, "6"));
    try std.testing.expectError(error.InvalidProfileOwner, indexedDbOwnerBootstrap("5", "/profile", null));
    try std.testing.expectError(error.InvalidProfileOwner, indexedDbOwnerBootstrap("", "/profile", "6"));
    try std.testing.expectError(error.InvalidProfileOwner, indexedDbOwnerBootstrap("-1", "/profile", "6"));
    try std.testing.expectError(error.InvalidProfileOwner, indexedDbOwnerBootstrap("5", "/profile", "bad"));
    try std.testing.expectError(error.InvalidProfileOwner, indexedDbOwnerBootstrap("5", "/profile", "5"));

    const owner = (try indexedDbOwnerBootstrap("5", "/profile", "6")).?;
    try std.testing.expectEqual(@as(std.posix.fd_t, 5), owner.fd);
    try std.testing.expectEqual(@as(std.posix.fd_t, 6), owner.grant_fd);
    try std.testing.expectEqualStrings("/profile", owner.directory);
}

test "profile owner conflicts only with cookie file options" {
    try std.testing.expect(!profileOwnerConflictsWithCookieFiles(false, true, true));
    try std.testing.expect(!profileOwnerConflictsWithCookieFiles(true, false, false));
    try std.testing.expect(profileOwnerConflictsWithCookieFiles(true, true, false));
    try std.testing.expect(profileOwnerConflictsWithCookieFiles(true, false, true));
}
