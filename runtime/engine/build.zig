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
const builtin = @import("builtin");
const Translator = @import("translate_c").Translator;

const lightpanda_version = std.SemanticVersion.parse(@import("build.zig.zon").version) catch unreachable;
const min_zig_version = std.SemanticVersion.parse(@import("build.zig.zon").minimum_zig_version) catch unreachable;

const NativeProvider = enum { rust, zig };

const Build = blk: {
    if (builtin.zig_version.order(min_zig_version) == .lt) {
        @compileError(std.fmt.comptimePrint(
            \\Zig version is too old:
            \\  current Zig version: {f}
            \\  minimum Zig version: {f}
        , .{ builtin.zig_version, min_zig_version }));
    }
    break :blk std.Build;
};

pub fn build(b: *Build) !void {
    const optimize = b.standardOptimizeOption(.{});
    const requested_target = b.standardTargetOptions(.{});

    const enable_tsan = b.option(bool, "tsan", "Enable Thread Sanitizer") orelse false;
    const enable_asan = b.option(bool, "asan", "Enable Address Sanitizer") orelse false;
    const enable_csan = b.option(std.zig.SanitizeC, "csan", "Enable C Sanitizers");

    const prebuilt_v8_path_option = b.option([]const u8, "prebuilt_v8_path", "Path to a prebuilt libc_v8.a or libc_v8.so");

    const dev_fast = b.option(bool, "dev_fast", "Linux debug builds: shared V8 + self-hosted backend. Implies -Dshared_v8, -Duse_llvm=false and a bundled-CRT target") orelse
        (builtin.target.os.tag == .linux and builtin.target.cpu.arch == .x86_64 and
            optimize == .debug and requested_target.query.isNative() and
            !enable_tsan and !enable_asan and
            (prebuilt_v8_path_option == null or std.mem.endsWith(u8, prebuilt_v8_path_option.?, ".so")));

    if (dev_fast) {
        if (builtin.target.os.tag != .linux) {
            std.debug.print("-Ddev_fast is Linux-only (host is {s})\n", .{@tagName(builtin.target.os.tag)});
            return error.DevFastUnsupportedHost;
        }
        if (optimize != .debug) {
            std.debug.print("-Ddev_fast is debug-only (optimize is {s})\n", .{@tagName(optimize)});
            return error.DevFastRequiresDebug;
        }
        if (!requested_target.query.isNative()) {
            std.debug.print("-Ddev_fast builds for the host; drop -Dtarget/-Dcpu\n", .{});
            return error.DevFastRequiresNativeTarget;
        }
    }

    const target = if (dev_fast) b.resolveTargetQuery(.{
        .cpu_arch = .x86_64,
        .os_tag = .linux,
        .abi = .gnu,
        // Explicit version => bundled CRT, https://codeberg.org/ziglang/zig/issues/31272
        .glibc_version = devFastGlibcVersion(b),
    }) else requested_target;

    // Dependencies never follow -Doptimize, and they build for the requested
    // target rather than the dev_fast bundled-CRT query, so debug and release
    // builds share one set of dependency objects in the cache.
    const debug_deps = b.option(bool, "debug_deps", "Build the C and Rust dependencies in debug instead of fast") orelse false;
    const deps: Deps = .{
        .target = requested_target,
        .optimize = if (debug_deps) .debug else .fast,
        .macos_sdk = b.option([]const u8, "macos_sdk", "Explicit macOS SDK selected by the source-bound builder") orelse b.graph.environ_map.get("SDKROOT"),
    };

    // Core's build entry supplies a content-verified archive explicitly.
    const prebuilt_v8_path: ?[]const u8 = prebuilt_v8_path_option orelse {
        std.debug.print("Use runtime/build.mjs with an explicit pinned V8 input.\n", .{});
        return error.PrebuiltV8Required;
    };
    const snapshot_path = b.option([]const u8, "snapshot_path", "Path to v8 snapshot");
    const wpt_extensions = b.option(bool, "wpt_extensions", "Extend WebAPI with WPT driver behavior") orelse false;
    const shared_v8 = b.option(bool, "shared_v8", "Link V8 as a shared library") orelse (dev_fast or (prebuilt_v8_path != null and std.mem.endsWith(u8, prebuilt_v8_path.?, ".so")));
    const v8_headers = b.option([]const u8, "v8-headers", "Candidate-only matching V8 include directory");
    if (v8_headers != null and shared_v8) return error.CandidateRequiresStaticV8;
    const use_llvm = b.option(bool, "use_llvm", "Use the LLVM backend") orelse !dev_fast;
    // Hot-code layout for the Linux release artifacts, see orderfile/README.md.
    // Opt-in (CI passes it): it needs LLD and costs link time on every build.
    const orderfile = b.option([]const u8, "orderfile", "Linker script packing hot sections, e.g. orderfile/lightpanda.ld (Linux/LLD release builds)");

    const version = resolveVersion(b);
    std.debug.print("Lightpanda {f}\n", .{version});

    const version_string = b.fmt("{f}", .{version});
    const version_encoded = std.mem.replaceOwned(u8, b.allocator, version_string, "+", "%2B") catch @panic("OOM");

    var opts = b.addOptions();
    opts.addOption([]const u8, "version", version_string);
    opts.addOption([]const u8, "version_encoded", version_encoded);
    opts.addOption(?[]const u8, "snapshot_path", snapshot_path);
    opts.addOption(bool, "wpt_extensions", wpt_extensions);
    opts.addOption(bool, "accounted_clone", v8_headers != null);
    const entities_provider = b.option(NativeProvider, "entities_provider", "Named character references implementation") orelse .zig;
    const png_provider = b.option(NativeProvider, "png_provider", "RGBA8 PNG export implementation") orelse .zig;
    opts.addOption(NativeProvider, "entities_provider", entities_provider);
    opts.addOption(NativeProvider, "png_provider", png_provider);

    const lightpanda_module = b.addModule("lightpanda", .{
        .root_source_file = b.path("src/lightpanda.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .link_libcpp = true,
        .sanitize_c = enable_csan,
        .sanitize_thread = enable_tsan,
    });
    const environment_api_module = b.createModule(.{
        .root_source_file = b.path("src/sys/environment.zig"),
        .target = target,
        .optimize = optimize,
    });
    lightpanda_module.addImport("environment_api", environment_api_module);
    const process_api_module = b.createModule(.{
        .root_source_file = b.path("../../mioa-os/process.zig"),
        .target = target,
        .optimize = optimize,
    });
    lightpanda_module.addImport("process_api", process_api_module);
    process_api_module.addImport("lightpanda", lightpanda_module);
    const thread_api_module = b.createModule(.{
        .root_source_file = b.path("../../mioa-os/thread.zig"),
        .target = target,
        .optimize = optimize,
    });
    lightpanda_module.addImport("thread_api", thread_api_module);
    lightpanda_module.addImport("lightpanda", lightpanda_module); // allow circular "lightpanda" import
    lightpanda_module.addImport("build_config", opts.createModule());
    // The Desktop-owned Zig kernel is the only renderer; `@import("render_kernel")`
    // resolves to `mioa-desktop/src/render_kernel/document.zig`.
    const render_kernel_module = b.createModule(.{
        .root_source_file = b.path("../../../../src/render_kernel/document.zig"),
        .target = target,
        .optimize = optimize,
    });
    lightpanda_module.addImport("render_kernel", render_kernel_module);
    lightpanda_module.addIncludePath(b.path("../../mioa-os/api/system"));
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/app_data.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/environment.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/executable_path.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/terminal.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/files.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/random.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("../../mioa-os/clock.c"),
        .flags = &.{"-std=c11"},
    });
    lightpanda_module.addIncludePath(b.path("src/c"));
    lightpanda_module.addCSourceFile(.{
        .file = b.path("src/c/ada.cpp"),
        .flags = &.{"-std=c++20"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("src/c/ada_bridge.cpp"),
        .flags = &.{"-std=c++20"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("src/c/html_bridge.cpp"),
        .flags = &.{"-std=c++20"},
    });
    lightpanda_module.addCSourceFile(.{
        .file = b.path("src/c/render_bridge.cpp"),
        .flags = &.{"-std=c++20"},
    });
    if (target.result.os.tag == .windows) {
        lightpanda_module.linkSystemLibrary("kernel32", .{});
    }
    if (v8_headers) |headers| {
        const cpp_flags = &[_][]const u8{
            "-std=c++23",                                                "-fno-rtti",                                   "-fno-exceptions",                    "-nostdinc++",
            "-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS",                  "-D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS", "-D_LIBCPP_INSTRUMENTED_WITH_ASAN=0", "-DCR_LIBCXX_REVISION=97b436da4c33663581d394f4ee0a5977fc38c2f4",
            "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE", "-DV8_COMPRESS_POINTERS",
        };
        // Isolate Chromium's C++ ABI from Zig's automatic system-libc++ flags.
        inline for (.{ "accounted_clone", "runtime_memory" }) |name| {
            const cpp = b.createModule(.{
                .target = target,
                .optimize = optimize,
                .link_libc = true,
                .link_libcpp = false,
            });
            cpp.addIncludePath(.{ .cwd_relative = headers });
            cpp.addIncludePath(.{ .cwd_relative = b.pathJoin(&.{ headers, "cpp-abi", "config" }) });
            cpp.addIncludePath(.{ .cwd_relative = b.pathJoin(&.{ headers, "cpp-abi", "libcxx", "include" }) });
            cpp.addIncludePath(.{ .cwd_relative = b.pathJoin(&.{ headers, "cpp-abi", "libcxxabi", "include" }) });
            cpp.addCSourceFile(.{ .file = b.path("src/browser/js/" ++ name ++ ".cpp"), .flags = cpp_flags });
            lightpanda_module.addObject(b.addObject(.{ .name = name, .root_module = cpp, .use_llvm = use_llvm }));
        }
    }

    const fmt_step = b.step("fmt", "Check code formatting");
    const fmt = b.addFmt(.{
        .paths = b.pathList(&.{ "src", "build.zig", "build.zig.zon" }),
        .check = true,
    });
    fmt_step.dependOn(&fmt.step);
    b.default_step.dependOn(fmt_step);

    // With an orderfile, the prebuilt V8 archive is rewritten so its hot
    // functions' sections can be addressed by the linker script.
    const v8_archive: ?Build.LazyPath = if (prebuilt_v8_path) |path| .{ .cwd_relative = path } else null;
    const v8_for_link = if (orderfile != null and v8_archive != null and !shared_v8) markHotSections(b, v8_archive.?) else v8_archive;
    linkV8(b, lightpanda_module, enable_asan, enable_tsan, v8_for_link, shared_v8, deps.macos_sdk);
    linkCurl(b, lightpanda_module, deps, enable_tsan, orderfile != null);
    linkNativePlatform(b, lightpanda_module, deps);
    linkZenai(b, lightpanda_module);
    linkIsocline(b, lightpanda_module);
    linkSqlite(b, lightpanda_module, deps, enable_csan, enable_tsan, orderfile != null);
    linkPcre2(b, lightpanda_module, deps, enable_csan, enable_tsan, orderfile != null);

    // Check compilation
    const check = b.step("check", "Check if lightpanda compiles");

    const check_lib = b.addLibrary(.{
        .name = "lightpanda_check",
        .root_module = lightpanda_module,
    });
    check.dependOn(&check_lib.step);

    // Extras (snapshot_creator) are off the default install to
    // avoid paying for three exe compiles on every edit. Build explicitly
    // with `zig build extras`.
    const extras_step = b.step("extras", "Build snapshot_creator");

    const exe_config: ExeConfig = .{
        .check = check,
        .lightpanda_module = lightpanda_module,
        .environment_api_module = environment_api_module,
        .process_api_module = process_api_module,
        .thread_api_module = thread_api_module,
        .target = target,
        .optimize = optimize,
        .use_llvm = use_llvm,
        .orderfile = orderfile,
        .sanitize_c = enable_csan,
        .sanitize_thread = enable_tsan,
    };

    {
        // browser
        const exe = addExe(b, exe_config, "lightpanda", "lightpanda_exe_check", "src/main.zig");
        b.installArtifact(exe);

        const run_cmd = b.addRunArtifact(exe);
        run_cmd.addPassthruArgs();
        const run_step = b.step("run", "Run the app");
        run_step.dependOn(&run_cmd.step);

        const version_info_step = b.step("version", "Print the resolved version information");
        const version_info_run = b.addRunArtifact(exe);
        version_info_run.addArg("version");
        version_info_step.dependOn(&version_info_run.step);
    }

    {
        // snapshot creator
        const exe = addExe(b, exe_config, "lightpanda-snapshot-creator", "snapshot_creator_check", "src/main_snapshot_creator.zig");
        extras_step.dependOn(&b.addInstallArtifact(exe, .{}).step);

        const run_cmd = b.addRunArtifact(exe);
        run_cmd.addPassthruArgs();
        const run_step = b.step("snapshot_creator", "Generate a v8 snapshot");
        run_step.dependOn(&run_cmd.step);
    }

    addGenerator(b, exe_config, "skills", "Generate LLM skill docs (zig-out/skills/<name>/SKILL.md)");
    addGenerator(b, exe_config, "docs", "Generate reference partials for the docs site (zig-out/docs/*.mdx)");

    {
        const native_options = b.addOptions();
        native_options.addOption(NativeProvider, "entities_provider", .zig);
        native_options.addOption(NativeProvider, "png_provider", .zig);
        const native_tests = b.addTest(.{
            .root_module = b.createModule(.{
                .root_source_file = b.path("src/native_tests.zig"),
                .target = target,
                .optimize = optimize,
                .link_libc = true,
                .imports = &.{.{ .name = "build_config", .module = native_options.createModule() }},
            }),
            .use_llvm = use_llvm,
        });
        const run_native_tests = b.addRunArtifact(native_tests);
        b.step("native-tests", "Run isolated Zig primitive behavior tests").dependOn(&run_native_tests.step);
    }

    {
        // test
        const tests = b.addTest(.{
            .root_module = lightpanda_module,
            .use_llvm = use_llvm,
            .test_runner = .{ .path = b.path("src/test_runner.zig"), .mode = .simple },
        });
        const run_tests = b.addRunArtifact(tests);
        const test_step = b.step("test", "Run unit tests");
        test_step.dependOn(&run_tests.step);

        // Render-path slice: the tests that exercise layout, paint, screenshot
        // and PDF. The custom runner filters on `TEST_FILTER` (one substring per
        // run, `name#subfilter`), so each slice is its own run of the same
        // already-compiled binary. Seconds instead of the whole suite.
        const render_step = b.step("test-render", "Run the renderer-facing browser tests");
        for ([_][]const u8{ "browser.screenshot:", "browser.pdf:" }) |filter| {
            const run = b.addRunArtifact(tests);
            run.setEnvironmentVariable("TEST_FILTER", filter);
            render_step.dependOn(&run.step);
        }
        // `native_layout` carries one recorded red (tests/render-known-red.txt),
        // so it is its own step: the Makefile compares its failure set with that
        // record instead of letting a `|| true` hide new breakage.
        const layout_run = b.addRunArtifact(tests);
        layout_run.setEnvironmentVariable("TEST_FILTER", "browser.native_layout:");
        b.step("test-render-layout", "Run native_layout tests (red set checked by the Makefile)")
            .dependOn(&layout_run.step);
    }

    {
        // kcov can't read line info from the self-hosted backend's DWARF.
        const cov_tests = b.addTest(.{
            .root_module = lightpanda_module,
            .use_llvm = true,
            .test_runner = .{ .path = b.path("src/test_runner.zig"), .mode = .simple },
        });
        const kcov = b.addSystemCommand(&.{ "kcov", "--clean" });
        // Otherwise stdio is checked and the runner's stderr summary fails the step.
        kcov.has_side_effects = true;
        kcov.addPrefixedDirectoryArg("--include-path=", b.path("src"));
        const report = kcov.addOutputDirectoryArg("coverage");
        kcov.addArtifactArg(cov_tests);
        // Shards are separate processes, each with its own report.
        kcov.setEnvironmentVariable("TEST_JOBS", "1");
        // Part of the cache key, so each filter gets a fresh report directory.
        kcov.setEnvironmentVariable("TEST_FILTER", b.graph.environ_map.get("TEST_FILTER") orelse "");

        const install = b.addInstallDirectory(.{ .source_dir = report, .install_dir = .prefix, .install_subdir = "coverage" });
        const coverage_step = b.step("coverage", "Run unit tests under kcov (zig-out/coverage/index.html)");
        coverage_step.dependOn(&install.step);
    }
}

const Deps = struct {
    target: Build.ResolvedTarget,
    optimize: std.lang.Optimize,
    macos_sdk: ?[]const u8,
};

const ExeConfig = struct {
    check: *Build.Step,
    lightpanda_module: *Build.Module,
    environment_api_module: *Build.Module,
    process_api_module: *Build.Module,
    thread_api_module: *Build.Module,
    target: Build.ResolvedTarget,
    optimize: std.lang.Optimize,
    use_llvm: bool,
    orderfile: ?[]const u8,
    sanitize_c: ?std.zig.SanitizeC,
    sanitize_thread: bool,
};

/// A `zig build <name>` step that runs `src/main_<name>.zig` with an output
/// directory and installs what it writes to `zig-out/<name>`.
fn addGenerator(b: *Build, config: ExeConfig, comptime name: []const u8, description: []const u8) void {
    const exe = addExe(b, config, "lightpanda-" ++ name, name ++ "_check", "src/main_" ++ name ++ ".zig");
    const run_cmd = b.addRunArtifact(exe);
    const out_dir = run_cmd.addOutputDirectoryArg2(name, .{});
    const install = b.addInstallDirectory(.{
        .source_dir = out_dir,
        .install_dir = .prefix,
        .install_subdir = name,
    });
    b.step(name, description).dependOn(&install.step);
}

fn addExe(b: *Build, config: ExeConfig, name: []const u8, check_name: []const u8, root_source_file: []const u8) *Build.Step.Compile {
    const exe = b.addExecutable(.{
        .name = name,
        .use_llvm = config.use_llvm,
        .root_module = b.createModule(.{
            .root_source_file = b.path(root_source_file),
            .target = config.target,
            .optimize = config.optimize,
            .sanitize_c = config.sanitize_c,
            .sanitize_thread = config.sanitize_thread,
            .imports = &.{
                .{ .name = "lightpanda", .module = config.lightpanda_module },
                .{ .name = "environment_api", .module = config.environment_api_module },
                .{ .name = "process_api", .module = config.process_api_module },
                .{ .name = "thread_api", .module = config.thread_api_module },
            },
        }),
    });

    if (config.orderfile) |path| {
        // Per-function/per-datum sections exist only so the orderfile script
        // can place individual hot functions; the self-hosted backend used by
        // debug builds does not support them on the C libraries, so they are
        // gated on the orderfile being set (release/LLVM only).
        exe.link_function_sections = true;
        exe.link_data_sections = true;
        exe.linker_script = .{ .cwd_relative = path };
    }

    const exe_check = b.addLibrary(.{
        .name = check_name,
        .root_module = exe.root_module,
    });
    config.check.dependOn(&exe_check.step);

    return exe;
}

fn devFastGlibcVersion(b: *Build) std.SemanticVersion {
    const host = b.graph.host.result.os.version_range.linux.glibc;
    const newest_known: std.SemanticVersion = .{ .major = 2, .minor = 43, .patch = 0 };
    return if (host.order(newest_known) == .gt) newest_known else host;
}

fn rootPath(b: *Build, sub_path: []const u8) []const u8 {
    return b.root.joinString(b.allocator, sub_path) catch @panic("OOM");
}

/// Renames the hot V8 functions' sections (`.text` -> `.text.hot.<sym>`, see
/// orderfile/mark_hot_sections.zig) so the orderfile script can gather them.
fn markHotSections(b: *Build, archive: Build.LazyPath) Build.LazyPath {
    const tool = b.addExecutable(.{
        .name = "mark_hot_sections",
        .root_module = b.createModule(.{
            .root_source_file = b.path("orderfile/mark_hot_sections.zig"),
            .target = b.graph.host,
            .optimize = .safe,
        }),
    });
    const run = b.addRunArtifact(tool);
    run.addFileArg(archive);
    run.addFileArg(b.path("orderfile/v8.txt"));
    return run.addOutputFileArg2("libc_v8.a", .{});
}

/// Per-function/per-datum sections let the -Dorderfile linker script place
/// individual hot functions. Only enabled for orderfile (release/LLVM) builds:
/// the self-hosted backend used by debug builds fails to link the C libraries
/// with them.
fn sectionize(lib: *Build.Step.Compile, enabled: bool) *Build.Step.Compile {
    if (enabled) {
        lib.link_function_sections = true;
        lib.link_data_sections = true;
    }
    return lib;
}

fn linkV8(
    b: *Build,
    mod: *Build.Module,
    is_asan: bool,
    is_tsan: bool,
    prebuilt_v8_path: ?Build.LazyPath,
    shared_v8: bool,
    macos_sdk: ?[]const u8,
) void {
    const target = mod.resolved_target.?;

    const dep = b.dependency("v8", .{
        .target = target,
        .optimize = mod.optimize.?,
        .is_asan = is_asan,
        .is_tsan = is_tsan,
        .inspector_subtype = false,
        .v8_enable_sandbox = is_tsan,
        .cache_root = rootPath(b, ".lp-cache"),
        .prebuilt_v8_path = prebuilt_v8_path,
        .shared_v8 = shared_v8,
    });
    const v8_module = dep.module("v8");
    if (target.result.os.tag == .macos) {
        const sdk = macos_sdk orelse @panic("macOS native build requires -Dmacos_sdk or SDKROOT");
        const frameworks: Build.LazyPath = .{ .cwd_relative = b.pathJoin(&.{ sdk, "System/Library/Frameworks" }) };
        // Configure this dependency module instance, not the pinned package.
        // A later headerless host-framework path can otherwise shadow the SDK.
        for (v8_module.include_dirs.items) |*include| switch (include.*) {
            .framework_path => include.* = .{ .framework_path = frameworks },
            .framework_path_system => include.* = .{ .framework_path_system = frameworks },
            else => {},
        };
    }
    mod.addImport("v8", v8_module);
}

fn linkNativePlatform(b: *Build, mod: *Build.Module, deps: Deps) void {
    if (deps.target.result.os.tag == .macos) {
        const sdk = deps.macos_sdk orelse @panic("macOS native build requires -Dmacos_sdk or SDKROOT");
        mod.addCSourceFile(.{ .file = b.path("../../mioa-os/broker.c"), .flags = &.{ "-isysroot", sdk } });
        mod.linkFramework("IOSurface", .{});
        mod.linkFramework("CoreFoundation", .{});
        mod.linkSystemLibrary("bsm", .{});
    }
}

fn linkSqlite(b: *Build, mod: *Build.Module, deps: Deps, enable_csan: ?std.zig.SanitizeC, is_tsan: bool, section: bool) void {
    const dep = b.dependency("sqlite3", .{});

    const lib_mod = cLibModule(b, deps.target, deps.optimize, is_tsan);
    lib_mod.sanitize_c = enable_csan;
    lib_mod.addCSourceFile(.{ .file = dep.path("sqlite3.c") });
    lib_mod.addIncludePath(dep.path(""));
    lib_mod.addCSourceFile(.{ .file = b.path("src/storage/sqlite/bind_copy.c") });
    const lib = sectionize(b.addLibrary(.{ .name = "sqlite3", .root_module = lib_mod }), section);

    const macros = [_]struct { []const u8, []const u8 }{
        .{ "SQLITE_DEFAULT_FILE_PERMISSIONS", "0600" },
        .{ "SQLITE_DEFAULT_MEMSTATUS", "0" },
        .{ "SQLITE_DEFAULT_WAL_SYNCHRONOUS", "1" },
        .{ "SQLITE_DQS", "0" },
        .{ "SQLITE_ENABLE_API_ARMOR", "1" },
        .{ "SQLITE_ENABLE_UNLOCK_NOTIFY", "1" },
        .{ "SQLITE_TEMP_STORE", "3" },
        .{ "SQLITE_THREADSAFE", "1" },
        .{ "SQLITE_UNTESTABLE", "1" },
        .{ "SQLITE_USE_ALLOCA", "1" },
        .{ "SQLITE_OMIT_AUTHORIZATION", "1" },
        .{ "SQLITE_OMIT_AUTOMATIC_INDEX", "1" },
        .{ "SQLITE_OMIT_AUTORESET", "1" },
        .{ "SQLITE_OMIT_AUTOVACUUM", "1" },
        .{ "SQLITE_OMIT_BETWEEN_OPTIMIZATION", "1" },
        .{ "SQLITE_OMIT_CASE_SENSITIVE_LIKE_PRAGMA", "1" },
        .{ "SQLITE_OMIT_COMPLETE", "1" },
        .{ "SQLITE_OMIT_DECLTYPE", "1" },
        .{ "SQLITE_OMIT_DEPRECATED", "1" },
        .{ "SQLITE_OMIT_DESERIALIZE", "1" },
        .{ "SQLITE_OMIT_GET_TABLE", "1" },
        .{ "SQLITE_OMIT_INCRBLOB", "1" },
        .{ "SQLITE_OMIT_JSON", "1" },
        .{ "SQLITE_OMIT_LIKE_OPTIMIZATION", "1" },
        .{ "SQLITE_OMIT_LOAD_EXTENSION", "1" },
        .{ "SQLITE_OMIT_PROGRESS_CALLBACK", "1" },
        .{ "SQLITE_OMIT_SHARED_CACHE", "1" },
        .{ "SQLITE_OMIT_TCL_VARIABLE", "1" },
        .{ "SQLITE_OMIT_TEMPDB", "1" },
        .{ "SQLITE_OMIT_TRACE", "1" },
        .{ "SQLITE_OMIT_UTF16", "1" },
        .{ "SQLITE_OMIT_XFER_OPT", "1" },
    };
    for (macros) |m| {
        lib.root_module.addCMacro(m[0], m[1]);
    }

    mod.linkLibrary(lib);

    mod.addImport("sqlite3", translateC(b, mod, dep.path("sqlite3.h")).mod);
}

fn linkPcre2(b: *Build, mod: *Build.Module, deps: Deps, enable_csan: ?std.zig.SanitizeC, is_tsan: bool, section: bool) void {
    const dep = b.dependency("pcre2", .{
        .target = deps.target,
        .optimize = deps.optimize,
        .linkage = .static,
    });

    const lib = sectionize(dep.artifact("pcre2-8"), section);
    lib.root_module.sanitize_c = enable_csan;
    lib.root_module.sanitize_thread = is_tsan;
    mod.linkLibrary(lib);

    const translator = translateC(b, mod, lib.getEmittedIncludeTree().path(b, "pcre2.h"));
    translator.defineCMacro("PCRE2_CODE_UNIT_WIDTH", "8");
    mod.addImport("pcre2", translator.mod);
}

fn linkCurl(b: *Build, mod: *Build.Module, deps: Deps, is_tsan: bool, section: bool) void {
    const curl = buildCurl(b, deps.target, deps.optimize, is_tsan, section);
    mod.linkLibrary(curl);

    const dep = b.dependency("curl", .{});
    const translator = translateC(b, mod, dep.path("include/curl/curl.h"));
    translator.addIncludePath(dep.path("include"));
    mod.addImport("curl", translator.mod);

    // zlib and brotli are linked to curl for their installed headers, and to
    // mod since Compression Streams call them directly.
    const zlib = buildZlib(b, deps.target, deps.optimize, is_tsan, section);
    curl.root_module.linkLibrary(zlib);
    mod.linkLibrary(zlib);

    const brotli = buildBrotli(b, deps.target, deps.optimize, is_tsan, section);
    inline for (brotli) |lib| {
        curl.root_module.linkLibrary(lib);
        mod.linkLibrary(lib);
    }

    const zstd = buildZstd(b, deps.target, deps.optimize, is_tsan, section);
    curl.root_module.linkLibrary(zstd);

    const nghttp2 = buildNghttp2(b, deps.target, deps.optimize, is_tsan, section);
    curl.root_module.linkLibrary(nghttp2);

    const boringssl = buildBoringSsl(b, deps.target, deps.optimize, section);
    for (boringssl) |lib| curl.root_module.linkLibrary(lib);

    if (deps.target.result.os.tag == .macos) {
        // needed for proxying on mac
        const sdk = deps.macos_sdk orelse @panic("macOS native build requires -Dmacos_sdk or SDKROOT");
        mod.addSystemFrameworkPath(.{ .cwd_relative = b.pathJoin(&.{ sdk, "System/Library/Frameworks" }) });
        mod.addSystemIncludePath(.{ .cwd_relative = b.pathJoin(&.{ sdk, "usr/include" }) });
        mod.linkFramework("CoreFoundation", .{});
        mod.linkFramework("SystemConfiguration", .{});
        mod.linkFramework("OpenGL", .{});
    }
}

fn cLibModule(b: *Build, target: Build.ResolvedTarget, optimize: std.lang.Optimize, is_tsan: bool) *Build.Module {
    return b.createModule(.{
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .sanitize_thread = is_tsan,
    });
}

fn buildZlib(b: *Build, target: Build.ResolvedTarget, optimize: std.lang.Optimize, is_tsan: bool, section: bool) *Build.Step.Compile {
    const dep = b.dependency("zlib", .{});

    const mod = cLibModule(b, target, optimize, is_tsan);
    const lib = sectionize(b.addLibrary(.{ .name = "z", .root_module = mod }), section);
    lib.installHeadersDirectory(dep.path(""), "", .{});
    mod.addCSourceFiles(.{
        .root = dep.path(""),
        .flags = &.{
            "-DHAVE_SYS_TYPES_H",
            "-DHAVE_STDINT_H",
            "-DHAVE_STDDEF_H",
            "-DHAVE_UNISTD_H",
        },
        .files = &.{
            "adler32.c", "compress.c", "crc32.c",
            "deflate.c", "gzclose.c",  "gzlib.c",
            "gzread.c",  "gzwrite.c",  "infback.c",
            "inffast.c", "inflate.c",  "inftrees.c",
            "trees.c",   "uncompr.c",  "zutil.c",
        },
    });
    // zcalloc backs both curl and CompressionStreams. The gz API also owns
    // raw buffers without an allocation callback; retain all upstream frees.
    admitNativeHeap(b, mod, &.{ "zutil.c", "gzlib.c", "gzread.c", "gzwrite.c" }, "src/sys/compression_memory.h");

    return lib;
}

fn buildBrotli(b: *Build, target: Build.ResolvedTarget, optimize: std.lang.Optimize, is_tsan: bool, section: bool) [3]*Build.Step.Compile {
    const dep = b.dependency("brotli", .{});

    const mod = cLibModule(b, target, optimize, is_tsan);
    mod.addIncludePath(dep.path("c/include"));
    // Quota denial must unwind allocations, not terminate the owning app.
    mod.addCMacro("BROTLI_ENCODER_CLEANUP_ON_OOM", "1");

    const brotlicmn = sectionize(b.addLibrary(.{ .name = "brotlicommon", .root_module = mod }), section);
    const brotlidec = sectionize(b.addLibrary(.{ .name = "brotlidec", .root_module = mod }), section);
    const brotlienc = sectionize(b.addLibrary(.{ .name = "brotlienc", .root_module = mod }), section);

    brotlicmn.installHeadersDirectory(dep.path("c/include/brotli"), "brotli", .{});
    mod.addCSourceFiles(.{
        .root = dep.path("c/common"),
        .files = &.{
            "transform.c",  "shared_dictionary.c", "platform.c",
            "dictionary.c", "context.c",           "constants.c",
        },
    });
    mod.addCSourceFiles(.{
        .root = dep.path("c/dec"),
        .files = &.{
            "bit_reader.c", "decode.c", "huffman.c",
            "prefix.c",     "state.c",  "static_init.c",
        },
    });
    mod.addCSourceFiles(.{
        .root = dep.path("c/enc"),
        .files = &.{
            "backward_references.c",        "backward_references_hq.c", "bit_cost.c",
            "block_splitter.c",             "brotli_bit_stream.c",      "cluster.c",
            "command.c",                    "compound_dictionary.c",    "compress_fragment.c",
            "compress_fragment_two_pass.c", "dictionary_hash.c",        "encoder_dict.c",
            "entropy_encode.c",             "fast_log.c",               "histogram.c",
            "literal_cost.c",               "memory.c",                 "metablock.c",
            "static_dict.c",                "static_dict_lut.c",        "static_init.c",
            "utf8_util.c",
        },
    });
    mod.addIncludePath(dep.path("c/enc"));
    mod.addCSourceFile(.{ .file = b.path("src/sys/brotli_encoder.c") });
    // Public allocator callbacks do not cover every default constructor or
    // static-dictionary initialization. Redirect only their libc fallbacks.
    admitNativeHeap(b, mod, &.{
        "platform.c", "shared_dictionary.c", "decode.c", "memory.c", "static_dict_lut.c",
    }, "src/sys/compression_memory.h");

    return .{ brotlicmn, brotlidec, brotlienc };
}

fn buildZstd(b: *Build, target: Build.ResolvedTarget, optimize: std.lang.Optimize, is_tsan: bool, section: bool) *Build.Step.Compile {
    const dep = b.dependency("zstd", .{});

    const mod = cLibModule(b, target, optimize, is_tsan);
    const lib = sectionize(b.addLibrary(.{ .name = "zstd", .root_module = mod }), section);
    lib.installHeader(dep.path("lib/zstd.h"), "zstd.h");
    lib.installHeader(dep.path("lib/zstd_errors.h"), "zstd_errors.h");
    // curl only decodes, so the compressor is left out.
    mod.addCSourceFiles(.{
        .root = dep.path("lib"),
        .flags = &.{
            // RFC 9659 caps the zstd content coding's window at 8MB, as Chrome does.
            "-DZSTD_MAXWINDOWSIZE_DEFAULT=(1<<23)",
        },
        .files = &.{
            "common/debug.c",                    "common/entropy_common.c",
            "common/error_private.c",            "common/fse_decompress.c",
            "common/xxhash.c",                   "common/zstd_common.c",
            "decompress/huf_decompress.c",       "decompress/zstd_ddict.c",
            "decompress/zstd_decompress.c",      "decompress/zstd_decompress_block.c",
            // Empty unless the x86_64 BMI2 fast path is enabled.
            "decompress/huf_decompress_amd64.S",
        },
    });
    // HAVE_ZSTD enables curl's live streaming decoder. Preserve customMem
    // and static/caller-owned workspaces; admit only default heap backing.
    admitNativeHeap(b, mod, &.{
        "common/xxhash.c", "decompress/zstd_ddict.c", "decompress/zstd_decompress.c",
    }, "src/sys/compression_memory.h");

    return lib;
}

fn buildBoringSsl(b: *Build, target: Build.ResolvedTarget, optimize: std.lang.Optimize, section: bool) [2]*Build.Step.Compile {
    const dep = b.dependency("boringssl-zig", .{
        .target = target,
        .optimize = optimize,
        .force_pic = true,
    });

    const ssl = sectionize(dep.artifact("ssl"), section);
    ssl.bundle_ubsan_rt = false;

    const crypto = sectionize(dep.artifact("crypto"), section);
    crypto.bundle_ubsan_rt = false;

    // The pinned BoringSSL weak allocator hooks are ELF-only. Redirect only
    // its heap-owning translation units, retaining upstream cleanse/realloc.
    admitNativeHeap(b, crypto.root_module, &.{
        "crypto/mem.cc",
        "crypto/err/err.cc",
        "crypto/thread_pthread.cc",
    }, "src/sys/boringssl_memory.h");
    admitNativeHeap(b, ssl.root_module, &.{"ssl/ssl_buffer.cc"}, "src/sys/boringssl_memory.h");

    return .{ ssl, crypto };
}

fn admitNativeHeap(b: *Build, mod: *Build.Module, paths: []const []const u8, header: []const u8) void {
    var isolated: std.ArrayList(Build.Module.CSourceFile) = .empty;
    for (mod.link_objects.items) |object| {
        switch (object) {
            .c_source_files => |sources| {
                var kept: std.ArrayList([]const u8) = .empty;
                for (sources.files) |file| {
                    const selected = for (paths) |path| {
                        if (std.mem.eql(u8, file, path)) break true;
                    } else false;
                    if (selected) {
                        const flags = b.allocator.alloc([]const u8, sources.flags.len + 6) catch @panic("OOM");
                        @memcpy(flags[0..sources.flags.len], sources.flags);
                        @memcpy(flags[sources.flags.len..], &[_][]const u8{
                            "-include",             rootPath(b, header),
                            "-fno-builtin-malloc",  "-fno-builtin-calloc",
                            "-fno-builtin-realloc", "-fno-builtin-free",
                        });
                        isolated.append(b.allocator, .{
                            .file = sources.root.path(b, file),
                            .flags = flags,
                        }) catch @panic("OOM");
                    } else {
                        kept.append(b.allocator, file) catch @panic("OOM");
                    }
                }
                sources.files = kept.toOwnedSlice(b.allocator) catch @panic("OOM");
            },
            else => {},
        }
    }
    if (isolated.items.len != paths.len) @panic("pinned native heap source inventory changed");
    for (isolated.items) |source| mod.addCSourceFile(source);
}

fn buildNghttp2(b: *Build, target: Build.ResolvedTarget, optimize: std.lang.Optimize, is_tsan: bool, section: bool) *Build.Step.Compile {
    const dep = b.dependency("nghttp2", .{});

    const mod = cLibModule(b, target, optimize, is_tsan);
    mod.addIncludePath(dep.path("lib/includes"));

    const config = b.addConfigHeader(.{
        .include_path = "nghttp2ver.h",
        .style = .{ .cmake = dep.path("lib/includes/nghttp2/nghttp2ver.h.in") },
    }, .{
        .PACKAGE_VERSION = "1.68.90",
        .PACKAGE_VERSION_NUM = 0x016890,
    });
    mod.addConfigHeader(config);

    const lib = sectionize(b.addLibrary(.{ .name = "nghttp2", .root_module = mod }), section);

    lib.installConfigHeader(config);
    lib.installHeadersDirectory(dep.path("lib/includes/nghttp2"), "nghttp2", .{});
    mod.addCSourceFiles(.{
        .root = dep.path("lib"),
        .flags = &.{
            "-DNGHTTP2_STATICLIB",
            "-DHAVE_TIME_H",
            "-DHAVE_ARPA_INET_H",
            "-DHAVE_NETINET_IN_H",
        },
        .files = &.{
            "sfparse.c",                 "nghttp2_alpn.c",   "nghttp2_buf.c",
            "nghttp2_callbacks.c",       "nghttp2_debug.c",  "nghttp2_extpri.c",
            "nghttp2_frame.c",           "nghttp2_hd.c",     "nghttp2_hd_huffman.c",
            "nghttp2_hd_huffman_data.c", "nghttp2_helper.c", "nghttp2_http.c",
            "nghttp2_map.c",             "nghttp2_mem.c",    "nghttp2_option.c",
            "nghttp2_outbound_item.c",   "nghttp2_pq.c",     "nghttp2_priority_spec.c",
            "nghttp2_queue.c",           "nghttp2_rcbuf.c",  "nghttp2_session.c",
            "nghttp2_stream.c",          "nghttp2_submit.c", "nghttp2_version.c",
            "nghttp2_ratelim.c",         "nghttp2_time.c",
        },
    });
    // new3's supplied nghttp2_mem remains authoritative. Callbacks/options
    // and the queue have separate raw allocations even with custom sessions.
    admitNativeHeap(b, mod, &.{
        "nghttp2_mem.c", "nghttp2_callbacks.c", "nghttp2_option.c", "nghttp2_queue.c",
    }, "src/sys/compression_memory.h");

    return lib;
}

fn buildCurl(
    b: *Build,
    target: Build.ResolvedTarget,
    optimize: std.lang.Optimize,
    is_tsan: bool,
    section: bool,
) *Build.Step.Compile {
    const dep = b.dependency("curl", .{});

    const mod = cLibModule(b, target, optimize, is_tsan);
    mod.addIncludePath(dep.path("lib"));
    mod.addIncludePath(dep.path("include"));

    const os = target.result.os.tag;
    const abi = target.result.abi;

    const is_gnu = abi.isGnu();
    const is_ios = os == .ios;
    const is_android = abi.isAndroid();
    const is_linux = os == .linux;
    const is_darwin = os.isDarwin();
    const is_windows = os == .windows;
    const is_netbsd = os == .netbsd;
    const is_openbsd = os == .openbsd;
    const is_freebsd = os == .freebsd;

    const byte_size = struct {
        fn it(b2: *std.Build, target2: Build.ResolvedTarget, name: []const u8, comptime ctype: std.Target.CType) []const u8 {
            return b2.fmt("#define SIZEOF_{s} {d}", .{ name, target2.result.cTypeByteSize(ctype).? });
        }
    }.it;

    const config = .{
        .HAVE_LIBZ = true,
        .HAVE_BROTLI = true,
        .HAVE_ZSTD = true,
        .USE_NGHTTP2 = true,

        .USE_OPENSSL = true,
        .CURL_BORINGSSL_VERSION = null,
        .CURL_CA_PATH = null,
        .CURL_CA_BUNDLE = null,
        .CURL_CA_FALLBACK = false,
        .CURL_CA_SEARCH_SAFE = false,
        .CURL_DEFAULT_SSL_BACKEND = "openssl",

        .CURL_DISABLE_AWS = true,
        .CURL_DISABLE_DICT = true,
        .CURL_DISABLE_DOH = true,
        .CURL_DISABLE_FILE = true,
        .CURL_DISABLE_FTP = true,
        .CURL_DISABLE_GOPHER = true,
        .CURL_DISABLE_KERBEROS_AUTH = true,
        .CURL_DISABLE_IMAP = true,
        .CURL_DISABLE_IPFS = true,
        .CURL_DISABLE_LDAP = true,
        .CURL_DISABLE_LDAPS = true,
        .CURL_DISABLE_MQTT = true,
        .CURL_DISABLE_PROGRESS_METER = true,
        .CURL_DISABLE_POP3 = true,
        .CURL_DISABLE_RTSP = true,
        .CURL_DISABLE_SMTP = true,
        .CURL_DISABLE_TELNET = true,
        .CURL_DISABLE_TFTP = true,
        .CURL_DISABLE_WEBSOCKETS = false, // Enable WebSocket support

        .ssize_t = null,
        ._FILE_OFFSET_BITS = 64,

        .USE_IPV6 = true,
        // IDN is handled before libcurl (HttpClient calls URL.ensureHostAscii,
        // backed by rust-url), so libcurl always receives an ASCII host and
        // does not link libidn2.
        .HAVE_LIBIDN2 = false,
        .HAVE_IDN2_H = false,
        .CURL_OS = switch (os) {
            .linux => if (is_android) "\"android\"" else "\"linux\"",
            else => b.fmt("\"{s}\"", .{@tagName(os)}),
        },

        .SIZEOF_INT_CODE = byte_size(b, target, "INT", .int),
        .SIZEOF_LONG_CODE = byte_size(b, target, "LONG", .long),

        .SIZEOF_OFF_T_CODE = byte_size(b, target, "OFF_T", .longlong),
        .SIZEOF_CURL_OFF_T_CODE = byte_size(b, target, "CURL_OFF_T", .longlong),
        .SIZEOF_CURL_SOCKET_T_CODE = byte_size(b, target, "CURL_SOCKET_T", .int),

        .SIZEOF_SIZE_T_CODE = byte_size(b, target, "SIZE_T", .longlong),
        .SIZEOF_TIME_T_CODE = byte_size(b, target, "TIME_T", .longlong),

        // headers availability
        .HAVE_ARPA_INET_H = !is_windows,
        .HAVE_DIRENT_H = true,
        .HAVE_FCNTL_H = true,
        .HAVE_IFADDRS_H = !is_windows,
        .HAVE_IO_H = is_windows,
        .HAVE_LIBGEN_H = true,
        .HAVE_LINUX_TCP_H = is_linux and is_gnu,
        .HAVE_LOCALE_H = true,
        .HAVE_NETDB_H = !is_windows,
        .HAVE_NETINET_IN6_H = is_android,
        .HAVE_NETINET_IN_H = !is_windows,
        .HAVE_NETINET_TCP_H = !is_windows,
        .HAVE_NETINET_UDP_H = !is_windows,
        .HAVE_NET_IF_H = !is_windows,
        .HAVE_POLL_H = !is_windows,
        .HAVE_PWD_H = !is_windows,
        .HAVE_STDATOMIC_H = true,
        .HAVE_STDBOOL_H = true,
        .HAVE_STRINGS_H = true,
        .HAVE_STROPTS_H = false,
        .HAVE_SYS_EVENTFD_H = is_linux or is_freebsd or is_netbsd,
        .HAVE_SYS_FILIO_H = !is_linux and !is_windows,
        .HAVE_SYS_IOCTL_H = !is_windows,
        .HAVE_SYS_PARAM_H = true,
        .HAVE_SYS_POLL_H = !is_windows,
        .HAVE_SYS_RESOURCE_H = !is_windows,
        .HAVE_SYS_SELECT_H = !is_windows,
        .HAVE_SYS_SOCKIO_H = !is_linux and !is_windows,
        .HAVE_SYS_TYPES_H = true,
        .HAVE_SYS_UN_H = !is_windows,
        .HAVE_SYS_UTIME_H = is_windows,
        .HAVE_TERMIOS_H = !is_windows,
        .HAVE_TERMIO_H = is_linux,
        .HAVE_UNISTD_H = true,
        .HAVE_UTIME_H = true,

        // general environment
        .CURL_KRB5_VERSION = null,
        .CURL_PATCHSTAMP = null,
        .HAVE_ALARM = !is_windows,
        .HAVE_ARC4RANDOM = is_android,
        .HAVE_ATOMIC = true,
        .HAVE_BOOL_T = true,
        .HAVE_BUILTIN_AVAILABLE = true,
        .HAVE_CLOCK_GETTIME_MONOTONIC = !is_darwin and !is_windows,
        .HAVE_CLOCK_GETTIME_MONOTONIC_RAW = is_linux,
        .HAVE_GETEUID = !is_windows,
        .HAVE_GETPPID = !is_windows,
        .HAVE_GETTIMEOFDAY = true,
        .HAVE_GLIBC_STRERROR_R = is_gnu,
        .HAVE_GMTIME_R = !is_windows,
        .HAVE_LOCALTIME_R = !is_windows,
        .HAVE_MACH_ABSOLUTE_TIME = is_darwin,
        .HAVE_MEMRCHR = !is_darwin and !is_windows,
        .HAVE_POSIX_STRERROR_R = !is_gnu and !is_windows,
        .HAVE_THREADS_POSIX = !is_windows,
        .HAVE_SETLOCALE = true,
        .HAVE_SETRLIMIT = !is_windows,
        .HAVE_SIGACTION = !is_windows,
        .HAVE_SIGINTERRUPT = !is_windows,
        .HAVE_SIGNAL = true,
        .HAVE_SIGSETJMP = !is_windows,
        .HAVE_STRCASECMP = !is_windows,
        .HAVE_STRCMPI = false,
        .HAVE_STRERROR_R = !is_windows,
        .HAVE_STRICMP = false,
        .HAVE_STRUCT_TIMEVAL = true,
        .HAVE_TIME_T_UNSIGNED = false,
        .HAVE_UTIME = true,
        .HAVE_UTIMES = !is_windows,
        .HAVE_WRITABLE_ARGV = !is_windows,
        .USE_RESOLV_THREADED = !is_windows,

        // filesystem, network
        .HAVE_ACCEPT4 = is_linux or is_freebsd or is_netbsd or is_openbsd,
        .HAVE_BASENAME = true,
        .HAVE_CLOSESOCKET = is_windows,
        .HAVE_DECL_FSEEKO = !is_windows,
        .HAVE_EVENTFD = is_linux or is_freebsd or is_netbsd,
        .HAVE_FCHMOD = !is_windows,
        .HAVE_FCNTL = !is_windows,
        .HAVE_FCNTL_O_NONBLOCK = !is_windows,
        .HAVE_FNMATCH = !is_windows,
        .HAVE_FREEADDRINFO = true,
        .HAVE_FSEEKO = !is_windows,
        .HAVE_FSETXATTR = is_darwin or is_linux or is_netbsd,
        .HAVE_FSETXATTR_5 = is_linux or is_netbsd,
        .HAVE_FSETXATTR_6 = is_darwin,
        .HAVE_GETADDRINFO = true,
        .HAVE_GETADDRINFO_THREADSAFE = is_linux or is_freebsd or is_netbsd,
        .HAVE_GETHOSTBYNAME_R = is_linux or is_freebsd,
        .HAVE_GETHOSTBYNAME_R_3 = false,
        .HAVE_GETHOSTBYNAME_R_5 = false,
        .HAVE_GETHOSTBYNAME_R_6 = is_linux,
        .HAVE_GETHOSTNAME = true,
        .HAVE_GETIFADDRS = if (is_windows) false else !is_android or target.result.os.versionRange().linux.android >= 24,
        .HAVE_GETPASS_R = is_netbsd,
        .HAVE_GETPEERNAME = true,
        .HAVE_GETPWUID = !is_windows,
        .HAVE_GETPWUID_R = !is_windows,
        .HAVE_GETRLIMIT = !is_windows,
        .HAVE_GETSOCKNAME = true,
        .HAVE_IF_NAMETOINDEX = !is_windows,
        .HAVE_IOCTLSOCKET = is_windows,
        .HAVE_IOCTLSOCKET_CAMEL = false,
        .HAVE_IOCTLSOCKET_CAMEL_FIONBIO = false,
        .HAVE_IOCTLSOCKET_FIONBIO = is_windows,
        .HAVE_IOCTL_FIONBIO = !is_windows,
        .HAVE_IOCTL_SIOCGIFADDR = !is_windows,
        .HAVE_OPENDIR = true,
        .HAVE_PIPE = !is_windows,
        .HAVE_PIPE2 = is_linux or is_freebsd or is_netbsd or is_openbsd,
        .HAVE_POLL = !is_windows,
        .HAVE_REALPATH = !is_windows,
        .HAVE_RECV = true,
        .HAVE_SA_FAMILY_T = !is_windows,
        .HAVE_SCHED_YIELD = !is_windows,
        .HAVE_SEND = true,
        .HAVE_SENDMMSG = !is_darwin and !is_windows,
        .HAVE_SENDMSG = !is_windows,
        .HAVE_SETSOCKOPT_SO_NONBLOCK = false,
        .HAVE_SOCKADDR_IN6_SIN6_SCOPE_ID = true,
        .HAVE_SOCKET = true,
        .HAVE_SOCKETPAIR = !is_windows,
        .HAVE_STRUCT_SOCKADDR_STORAGE = true,
        .HAVE_SUSECONDS_T = is_android or is_ios,
        .USE_UNIX_SOCKETS = !is_windows,
    };

    const curl_config = b.addConfigHeader(.{
        .include_path = "curl_config.h",
        .style = .{ .cmake = dep.path("lib/curl_config-cmake.h.in") },
    }, .{
        .CURL_EXTERN_SYMBOL = "__attribute__ ((__visibility__ (\"default\"))",
    });
    curl_config.addValues(config);

    const lib = sectionize(b.addLibrary(.{ .name = "curl", .root_module = mod }), section);
    mod.addConfigHeader(curl_config);
    lib.installHeadersDirectory(dep.path("include/curl"), "curl", .{});
    mod.addCSourceFiles(.{
        .root = dep.path("lib"),
        .flags = &.{
            "-D_GNU_SOURCE",
            "-DHAVE_CONFIG_H",
            "-DCURL_STATICLIB",
            "-DBUILDING_LIBCURL",
        },
        .files = &.{
            // You can include all files from lib, libcurl uses #ifdef-guards to exclude code for disabled functions
            "altsvc.c",                "amigaos.c",           "api.c",                 "bufq.c",
            "bufref.c",                "cf-h1-proxy.c",       "cf-h2-proxy.c",         "cf-haproxy.c",
            "cf-https-connect.c",      "cfilters.c",          "cf-ip-happy.c",         "cf-recvbuf.c",
            "cf-setup.c",              "cf-socket.c",         "conncache.c",           "connect.c",
            "content_encoding.c",      "cookie.c",            "creds.c",               "cshutdn.c",
            "curl_addrinfo.c",         "curl_ed25519.c",      "curl_endian.c",         "curl_fnmatch.c",
            "curl_fopen.c",            "curl_gethostname.c",  "curl_get_line.c",       "curl_gssapi.c",
            "curl_memrchr.c",          "curl_ntlm_core.c",    "curl_range.c",          "curl_sasl.c",
            "curl_sha512_256.c",       "curl_share.c",        "curl_sspi.c",           "curl_threads.c",
            "curl_trc.c",              "curlx/base64.c",      "curlx/basename.c",      "curlx/dynbuf.c",
            "curlx/inet_ntop.c",       "curlx/inet_pton.c",   "curlx/multibyte.c",     "curlx/nonblock.c",
            "curlx/snprintf.c",        "curlx/strcopy.c",     "curlx/strdup.c",        "curlx/strerr.c",
            "curlx/strparse.c",        "curlx/timediff.c",    "curlx/timeval.c",       "curlx/version_win32.c",
            "curlx/wait.c",            "curlx/warnless.c",    "curlx/win32-fopen.c",   "curlx/winapi.c",
            "cw-out.c",                "cw-pause.c",          "dict.c",                "dllmain.c",
            "dynhds.c",                "easy.c",              "easygetopt.c",          "easyoptions.c",
            "escape.c",                "fake_addrinfo.c",     "file.c",                "fileinfo.c",
            "formdata.c",              "ftp.c",               "ftplistparser.c",       "getenv.c",
            "getinfo.c",               "gopher.c",            "hash.c",                "headers.c",
            "hmac.c",                  "hsts.c",              "http1.c",               "http2.c",
            "http_aws_sigv4.c",        "http.c",              "http_chunks.c",         "http_digest.c",
            "http_httpsig.c",          "http_negotiate.c",    "http_ntlm.c",           "http_proxy.c",
            "idn.c",                   "if2ip.c",             "imap.c",                "ldap.c",
            "llist.c",                 "macos.c",             "md4.c",                 "md5.c",
            "memdebug.c",              "mime.c",              "mprintf.c",             "mqtt.c",
            "multi.c",                 "multi_ev.c",          "multi_ntfy.c",          "netrc.c",
            "openldap.c",              "parsedate.c",         "peer.c",                "pingpong.c",
            "pop3.c",                  "progress.c",          "protocol.c",            "proxy.c",
            "psl.c",                   "ptrarray.c",          "rand.c",                "ratelimit.c",
            "request.c",               "rtsp.c",              "select.c",              "sendf.c",
            "setopt.c",                "sha256.c",            "slist.c",               "smtp.c",
            "socketpair.c",            "socks.c",             "socks_gssapi.c",        "socks_sspi.c",
            "splay.c",                 "strcase.c",           "strequal.c",            "strerror.c",
            "telnet.c",                "tftp.c",              "thrdpool.c",            "thrdqueue.c",
            "transfer.c",              "u32_ptrset.c",        "u8_strset.c",           "uint-bset.c",
            "uint-spbset.c",           "uint-table.c",        "urlapi.c",              "url.c",
            "vauth/cleartext.c",       "vauth/cram.c",        "vauth/digest.c",        "vauth/digest_sspi.c",
            "vauth/gsasl.c",           "vauth/krb5_gssapi.c", "vauth/krb5_sspi.c",     "vauth/ntlm.c",
            "vauth/ntlm_sspi.c",       "vauth/oauth2.c",      "vauth/spnego_gssapi.c", "vauth/spnego_sspi.c",
            "vauth/vauth.c",           "vdns/asyn-ares.c",    "vdns/asyn-base.c",      "vdns/asyn-thrdd.c",
            "vdns/cf-dns.c",           "vdns/dnscache.c",     "vdns/doh.c",            "vdns/hostip4.c",
            "vdns/hostip6.c",          "vdns/hostip.c",       "vdns/httpsrr.c",        "version.c",
            "vquic/capsule.c",         "vquic/cf-capsule.c",  "vquic/cf-ngtcp2.c",     "vquic/cf-ngtcp2-cmn.c",
            "vquic/cf-ngtcp2-proxy.c", "vquic/cf-quiche.c",   "vquic/vquic.c",         "vquic/vquic-tls.c",
            "vssh/libssh2.c",          "vssh/libssh.c",       "vssh/vssh.c",           "vtls/apple.c",
            "vtls/cipher_suite.c",     "vtls/gtls.c",         "vtls/hostcheck.c",      "vtls/keylog.c",
            "vtls/mbedtls.c",          "vtls/openssl.c",      "vtls/rustls.c",         "vtls/schannel.c",
            "vtls/schannel_verify.c",  "vtls/vtls.c",         "vtls/vtls_config.c",    "vtls/vtls_scache.c",
            "vtls/vtls_spack.c",       "vtls/wolfssl.c",      "vtls/x509asn1.c",       "ws.c",
        },
    });

    return lib;
}

fn linkZenai(b: *Build, mod: *Build.Module) void {
    const dep = b.dependency("zenai", .{});
    mod.addImport("zenai", dep.module("zenai"));
}

fn linkIsocline(b: *Build, mod: *Build.Module) void {
    const dep = b.dependency("isocline", .{});
    mod.addIncludePath(dep.path("include"));
    mod.addCSourceFile(.{
        .file = dep.path("src/isocline.c"),
    });

    mod.addImport("isocline", translateC(b, mod, dep.path("include/isocline.h")).mod);
}

fn translateC(b: *Build, mod: *Build.Module, header: Build.LazyPath) Translator {
    return .init(b.dependency("translate_c", .{}), .{
        .c_source_file = header,
        .target = mod.resolved_target.?,
        .optimize = mod.optimize.?,
        // Zig 0.16's built-in translate-c defaulted to this.
        .default_init = true,
    });
}

/// Resolves the semantic version of the build.
///
/// The base version is read from `build.zig.zon`. This can be overridden
/// using the `-Dversion` command-line flag:
/// - If the flag contains a full semantic version (e.g., `1.2.3`), it replaces
///   the base version entirely.
/// - If the flag contains a simple string (e.g., `nightly`), it replaces only
///   the pre-release tag of the base version (e.g., `1.1.0-dev` -> `1.1.0-nightly`).
///
/// Core deliberately does not derive versions from a surrounding Git checkout.
fn resolveVersion(b: *std.Build) std.SemanticVersion {
    const opt_version = b.option([]const u8, "version", "Override the version of this build");

    return if (opt_version) |v|
        std.SemanticVersion.parse(v) catch blk: {
            var fallback = lightpanda_version;
            fallback.pre = v;
            break :blk fallback;
        }
    else
        lightpanda_version;
}
