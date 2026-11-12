#!/usr/bin/env python3
"""Isolated Entities/PNG evidence; no browser build and no conformance claim."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import toon

CORE = Path(__file__).resolve().parents[1]
ADAPTERS = CORE / "tests/quality"
ASSETS = ADAPTERS / "assets"
CONTRACTS = ADAPTERS / "native_contracts.json"
BROWSER = CORE / "runtime/engine/src/browser"
SENTINEL = [0xDEADBEEF, 0xCAFEBABE]
ENTITY_PREFIX = "entities: prefixes, exact matches, and dead ends preserve output"
ENTITY_LEGACY = "entities: case and legacy semicolon aliases"
ENTITY_SCALARS = "entities: two scalars, astral characters, and longest name"
PNG_PIXELS = "PNG preserves straight alpha, row boundaries, dimensions and checksums"
PNG_CALLBACK = "PNG callback ABI propagates rejection and encoding errors"
PNG_ALLOC = "PNG propagates every allocation failure without leaking"

# Each source mutation is linked to a single, filtered, maintained semantic test.
# The tests and their helpers are never edited; replacement is restricted to the
# per-module production prefix pinned in native_contracts.json.
MUTATIONS = [
    dict(id="ent-second-scalar", module="entities.zig", test=ENTITY_SCALARS,
         old="out.* = entry.codepoints;", new="out.* = .{ entry.codepoints[0], 0 };", errors=["TestExpectedEqual"]),
    dict(id="ent-exact-as-prefix", module="entities.zig", test=ENTITY_PREFIX,
         old="out.* = entry.codepoints;\n        return 2;", new="out.* = entry.codepoints;\n        return 1;", errors=["TestExpectedEqual"]),
    dict(id="ent-prefix-as-exact", module="entities.zig", test=ENTITY_PREFIX,
         old="return if (std.mem.startsWith(u8, entry.name, name)) 1 else 0;", new="return if (std.mem.startsWith(u8, entry.name, name)) 2 else 0;", errors=["TestExpectedEqual"]),
    dict(id="ent-case-fold", module="entities.zig", test=ENTITY_LEGACY,
         old="if (name.len > data.max_name_length) return 0;", new='if (std.mem.eql(u8, name, "Amp;")) return lookup("AMP;", out);\n    if (name.len > data.max_name_length) return 0;', errors=["TestExpectedEqual"]),
    dict(id="ent-drop-legacy", module="entities.zig", test=ENTITY_LEGACY,
         old="if (name.len > data.max_name_length) return 0;", new="if (name.len > 0 and name[name.len - 1] != ';') return 1;\n    if (name.len > data.max_name_length) return 0;", errors=["TestExpectedEqual"]),
    dict(id="ent-nonexact-clobber", module="entities.zig", test=ENTITY_PREFIX,
         old="if (name.len > data.max_name_length) return 0;", new="out.* = .{ 0, 0 };\n    if (name.len > data.max_name_length) return 0;", errors=["TestExpectedEqual"]),
    dict(id="png-channel-order", module="png.zig", test=PNG_PIXELS,
         old="try compressor.writer.writeAll(rgba[offset..][0..row_length]);", new="for (0..width) |x| {\n            const p = rgba[offset + x * 4 ..][0..4];\n            try compressor.writer.writeAll(&.{ p[2], p[1], p[0], p[3] });\n        }", errors=["TestExpectedEqual"]),
    dict(id="png-drop-alpha", module="png.zig", test=PNG_PIXELS,
         old="try compressor.writer.writeAll(rgba[offset..][0..row_length]);", new="for (0..width) |x| {\n            const p = rgba[offset + x * 4 ..][0..4];\n            try compressor.writer.writeAll(&.{ p[0], p[1], p[2], 255 });\n        }", errors=["TestExpectedEqual"]),
    dict(id="png-width", module="png.zig", test=PNG_PIXELS,
         old="std.mem.writeInt(u32, header[0..4], width, .big);", new="std.mem.writeInt(u32, header[0..4], width + 1, .big);", errors=["TestExpectedEqual"]),
    dict(id="png-height", module="png.zig", test=PNG_PIXELS,
         old="std.mem.writeInt(u32, header[4..8], height, .big);", new="std.mem.writeInt(u32, header[4..8], height + 1, .big);", errors=["TestExpectedEqual"]),
    dict(id="png-callback-rejection", module="png.zig", test=PNG_CALLBACK,
         old="return callback(context, bytes.ptr, bytes.len);", new="_ = callback(context, bytes.ptr, bytes.len);\n    return true;", errors=["TestUnexpectedResult"]),
    dict(id="png-failure-publication", module="png.zig", test=PNG_CALLBACK,
         old="const bytes = encode(allocator, pointer[0..length], width, height) catch return false;", new="const bytes = encode(allocator, pointer[0..length], width, height) catch {\n        _ = callback(context, signature, signature.len);\n        return false;\n    };", errors=["TestUnexpectedResult"]),
    dict(id="png-allocation-cleanup", module="png.zig", test=PNG_ALLOC,
         old="defer allocator.destroy(compressor);", new="// Mutation: omit compressor ownership release.", errors=["MemoryLeakDetected"], leak=True),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entity_cases(table: dict) -> tuple[list[str], list[dict], dict]:
    """Independent oracle from the complete fixed WHATWG JSON, not native data."""
    exact = {name[1:]: item["codepoints"] for name, item in table.items()}
    prefixes = {name[:n] for name in exact for n in range(len(name))} - exact.keys()
    names = sorted(exact) + sorted(prefixes)
    dead = {name + "\x00" for name in exact} | {"&amp;", "Amp;", "é", "𝔄", "zzzz;", "a" * 64}
    if any(name in exact or name in prefixes for name in dead):
        raise ValueError("dead-end oracle overlaps a valid name or prefix")
    names += sorted(dead)
    expected = [{"status": 2, "codepoints": points + [0] * (2 - len(points))} for _, points in sorted(exact.items())]
    expected += [{"status": 1, "codepoints": SENTINEL} for _ in sorted(prefixes)]
    expected += [{"status": 0, "codepoints": SENTINEL} for _ in sorted(dead)]
    return names, expected, {"exact": len(exact), "prefix_only": len(prefixes), "no_match": len(dead)}


def baseline_inventory(text: str, contracts: dict) -> dict:
    """Require fixed harness IDs, independent of the current test source text."""
    required = contracts["required_native_tests"]
    names = [entry["name"] for entry in required]
    ids = [entry["id"] for entry in required]
    if (len(required) != contracts["required_native_test_count"] or
            len(set(names)) != len(names) or len(set(ids)) != len(ids)):
        raise ValueError("invalid fixed native-test inventory")
    records = re.findall(r"^(\d+)/(\d+) ([^\n]+?)\.\.\.OK$", text, re.MULTILINE)
    summary = re.search(r"^All (\d+) tests passed\.$", text, re.MULTILINE)
    executed = [name for _, _, name in records]
    missing = [entry["id"] for entry in required if entry["name"] not in executed]
    total = int(summary.group(1)) if summary else 0
    if (missing or total < len(required) or len(records) != total or
            len(set(executed)) != total or
            any(int(index) != i or int(count) != total for i, (index, count, _) in enumerate(records, 1))):
        raise ValueError(f"baseline did not execute every fixed required native test: missing={missing}")
    return {"executed_tests": executed, "required_tests": required, "total": total}


def entity_inventory(native: dict, community_count: int, contracts: dict) -> dict:
    expected = contracts["entity_table_entries"]
    if type(native.get("entries")) is not int or native["entries"] != expected or community_count != expected:
        raise ValueError(f"native/community entity table inventory must both equal pinned {expected}: native={native}, community={community_count}")
    return {"native_entries": native["entries"], "community_entries": community_count}


def apply_mutation(original: str, mutation: dict, contracts: dict) -> str:
    """Patch only before the explicit production/test-helper boundary."""
    boundary = contracts["mutation_modules"][mutation["module"]]["test_helper_boundary"]
    if original.count(boundary) != 1:
        raise ValueError("production/test-helper boundary must occur exactly once")
    production, suffix = original.split(boundary, 1)
    if '\ntest "' in production or production.count(mutation["old"]) != 1:
        raise ValueError("source contract patch must match production exactly once, never test helpers")
    return production.replace(mutation["old"], mutation["new"], 1) + boundary + suffix


def mutation_verdict(compiled: dict, tested: dict | None, mutation: dict, text: str) -> str:
    """Only intended assertion/leak evidence at normal exit 1 kills a mutant."""
    if compiled.get("returncode") != 0 or compiled.get("timed_out"):
        return "INVALID"
    if tested is None or tested.get("timed_out"):
        return "INVALID"
    rc = tested.get("returncode")
    if rc == 0:
        return "SURVIVED"
    if rc != 1:  # signal exits, panics, loader errors and crashes never kill
        return "INVALID"
    # Non-TTY Zig prints the test name before invoking its function. Its failure
    # follows on that record (possibly after expectation diagnostics).
    marker = re.search(r"\d+/\d+ [^\n]*" + re.escape(mutation["test"]) + r"\.\.\.", text)
    if marker is None:
        return "INVALID"
    region = text[marker.end():]
    next_test = re.search(r"\n\d+/\d+ ", region)
    if next_test:
        region = region[:next_test.start()]
    if any(f"FAIL ({error})" in region for error in mutation["errors"]):
        return "KILLED"
    if mutation.get("leak") and re.search(r"\b[1-9]\d* tests leaked memory\.", text) and "leaked" in region.lower():
        return "KILLED"
    return "INVALID"


def command(argv: list, directory: Path, label: str, timeout: int = 120) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    stdout, stderr = directory / f"{label}.stdout", directory / f"{label}.stderr"
    record = {"argv": [str(arg) for arg in argv], "stdout": str(stdout), "stderr": str(stderr), "returncode": None, "timed_out": False}
    try:
        proc = subprocess.run(record["argv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        out, err = proc.stdout, proc.stderr
        record["returncode"] = proc.returncode
    except subprocess.TimeoutExpired as exc:
        record["timed_out"] = True
        out, err = exc.stdout or b"", exc.stderr or b""
    except OSError as exc:
        out, err = b"", str(exc).encode()
        record["error"] = str(exc)
    stdout.write_bytes(out)
    stderr.write_bytes(err)
    return record


def command_text(record: dict) -> str:
    return Path(record["stdout"]).read_text(errors="replace") + Path(record["stderr"]).read_text(errors="replace")


def require_command(record: dict) -> None:
    if record["returncode"] != 0 or record["timed_out"]:
        raise RuntimeError(f"command failed (returncode={record['returncode']}, timeout={record['timed_out']}): {record['argv']}; logs {record['stderr']}")


def stage_source(destination: Path) -> None:
    (destination / "browser").mkdir(parents=True)
    for name in ("entities.zig", "entities_data.zig", "png.zig"):
        shutil.copyfile(BROWSER / name, destination / "browser" / name)
    shutil.copyfile(CORE / "runtime/engine/src/native_tests.zig", destination / "native_tests.zig")
    for name in ("entity_probe.zig", "png_probe.zig", "probe_io.zig", "native_quality_tests.zig"):
        shutil.copyfile(ADAPTERS / name, destination / name)
    (destination / "build_config.zig").write_text('pub const NativeProvider = enum { rust, zig };\npub const entities_provider: NativeProvider = .zig;\npub const png_provider: NativeProvider = .zig;\n')


def zig_command(zig: str, source: Path, root: str, binary: Path, test: bool = False, test_filter: str | None = None) -> list:
    argv = [zig, "test" if test else "build-exe", "-ODebug", "-lc", f"-femit-bin={binary}", "--cache-dir", str(source.parent / "zig-cache"), "--global-cache-dir", str(source.parent / "zig-global-cache")]
    if test:
        argv += ["--test-no-exec"]
    if test_filter:
        argv += ["--test-filter", test_filter]
    return argv + ["--dep", "build_config", f"-Mroot={source / root}", f"-Mbuild_config={source / 'build_config.zig'}"]


def run(output: Path, zig_request: str | None = None, cc_request: str | None = None) -> dict:
    logs = output / "logs"
    logs.mkdir()
    report = {"schema": 1, "status": "FAIL", "scope": "Isolated native Entities/PNG contracts, fixed WHATWG entity table and one PNGSuite RGBA8 image; not HTML/PNG/browser conformance.", "identity": {"source_files": {}, "tools": {}, "configuration": {"optimize": "Debug", "entities_provider": "zig", "png_provider": "zig", "compile_timeout_seconds": 120, "test_timeout_seconds": 60, "platform": sys.platform, "machine": os.uname().machine, "environment": {key: os.environ.get(key) for key in ("SDKROOT", "MACOSX_DEPLOYMENT_TARGET", "CPATH", "LIBRARY_PATH", "PKG_CONFIG_PATH", "ZIG_LIB_DIR")}}, "corpora": []}, "checks": [], "mutations": [{"id": m["id"], "requirement": "QV-MUTATION", "status": "NOT_RUN", "intended_test": m["test"], "expected_errors": m["errors"], "compile_returncode": None, "test_returncode": None} for m in MUTATIONS], "artifacts": {}}
    current = None
    def check(identifier, requirement, oracle):
        row = {"id": identifier, "requirement": requirement, "oracle": oracle, "status": "NOT_RUN", "detail": "not executed"}
        report["checks"].append(row)
        return row
    try:
        source_paths = [Path(__file__), CONTRACTS, CORE / "runtime/engine/src/native_tests.zig"] + [BROWSER / n for n in ("entities.zig", "entities_data.zig", "png.zig")] + sorted(ADAPTERS.glob("*.zig")) + [ADAPTERS / "libpng_decoder.c", ASSETS / "manifest.json"]
        report["identity"]["source_files"] = {str(p.relative_to(CORE)): sha256(p) for p in source_paths}
        contracts = json.loads(CONTRACTS.read_text())
        report["identity"]["configuration"]["native_contracts_sha256"] = sha256(CONTRACTS)
        current = check("community-assets", "QV-IDENTITY", "pinned manifest SHA-256 and full vendored license/source files")
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        for name, item in manifest["files"].items():
            if sha256(ASSETS / name) != item["sha256"]:
                raise ValueError(f"asset hash mismatch: {name}")
            report["identity"]["source_files"][str((ASSETS / name).relative_to(CORE))] = item["sha256"]
        report["identity"]["corpora"] = manifest["corpora"]
        current.update(status="PASS", detail={"files": manifest["files"]})
        current = check("native-prerequisites", "QV-IDENTITY", "installed Zig, C compiler and pkg-config libpng; no downloaded toolchains")
        tools = {"zig": shutil.which(zig_request or "zig"), "cc": shutil.which(cc_request or "cc"), "pkg_config": shutil.which("pkg-config")}
        missing = [name for name, path in tools.items() if path is None]
        if missing:
            current.update(detail={"missing": missing})
            report["status"] = "NOT_RUN"
            return report
        for name, path in tools.items():
            version = command([path, "version" if name == "zig" else "--version"], logs, f"{name}-version", 30)
            require_command(version)
            report["identity"]["tools"][name] = {"path": str(Path(path).resolve()), "sha256": sha256(Path(path)), "version": command_text(version).strip(), "version_command": version}
        pkg = tools["pkg_config"]
        flags = command([pkg, "--cflags", "--libs", "libpng"], logs, "libpng-flags", 30)
        version = command([pkg, "--modversion", "libpng"], logs, "libpng-version", 30)
        libdir = command([pkg, "--variable=libdir", "libpng"], logs, "libpng-libdir", 30)
        if any(row["returncode"] != 0 or row["timed_out"] for row in (flags, version, libdir)):
            current.update(detail={"missing": ["installed pkg-config libpng"], "commands": [flags, version, libdir]})
            report["status"] = "NOT_RUN"
            return report
        library_directory = Path(Path(libdir["stdout"]).read_text().strip())
        libraries = {str(p.resolve()): sha256(p) for pattern in ("libpng*.dylib", "libpng*.so*") for p in library_directory.glob(pattern) if p.is_file()}
        if not libraries:
            raise RuntimeError(f"no hashable installed libpng shared library in {library_directory}")
        cflags = shlex.split(Path(flags["stdout"]).read_text())
        report["identity"]["tools"]["libpng"] = {"version": Path(version["stdout"]).read_text().strip(), "libraries_sha256": libraries, "flags": cflags}
        current.update(status="PASS", detail={"tools": tools})
        current = check("native-build", "QV-IDENTITY", "compile standalone probes and maintained test root with explicit Zig providers")
        source, binaries = output / "source", output / "bin"
        stage_source(source)
        for original in source_paths:
            if original.suffix == ".zig":
                staged = source / ("browser" if original.parent == BROWSER else "") / original.name
                if sha256(staged) != report["identity"]["source_files"][str(original.relative_to(CORE))]:
                    raise ValueError(f"source changed while staging: {original}")
        binaries.mkdir()
        report["identity"]["configuration"]["build_config_sha256"] = sha256(source / "build_config.zig")
        builds = []
        for root, name, is_test in (("entity_probe.zig", "entity_probe", False), ("png_probe.zig", "png_probe", False), ("native_quality_tests.zig", "native_tests", True)):
            binary = binaries / name
            record = command(zig_command(tools["zig"], source, root, binary, is_test), logs, f"build-{name}")
            builds.append(record)
            current["commands"] = builds
            require_command(record)
            report["artifacts"][name] = str(binary)
        decoder = binaries / "libpng_decoder"
        record = command([tools["cc"], "-std=c11", "-O2", "-Wall", "-Wextra", str(ADAPTERS / "libpng_decoder.c"), "-o", str(decoder)] + cflags, logs, "build-libpng-decoder")
        builds.append(record)
        require_command(record)
        report["artifacts"]["libpng_decoder"] = str(decoder)
        report["identity"]["binaries_sha256"] = {name: sha256(Path(path)) for name, path in report["artifacts"].items()}
        current.update(status="PASS", detail="All standalone executables compiled successfully")
        current = check("maintained-native-tests", "QV-PNG-ORACLE", "actual entities.zig/png.zig tests, allocation failure enumeration and additional public callback publication test")
        baseline = command([binaries / "native_tests"], logs, "native-baseline", 60)
        current["command"] = baseline
        require_command(baseline)
        baseline_text = command_text(baseline)
        current.update(status="PASS", detail=baseline_inventory(baseline_text, contracts))
        current = check("whatwg-all-entities", "QV-ENT-DATA", "complete pinned WHATWG JSON exact names, all prefix-only names and separate dead ends; unchanged sentinels; equal native/community table inventories reject native-only extras")
        table = json.loads((ASSETS / "entities.json").read_text())
        count_result = command([binaries / "entity_probe", "--table-count"], logs, "native-entity-inventory", 60)
        current["inventory_command"] = count_result
        require_command(count_result)
        inventory = entity_inventory(json.loads(Path(count_result["stdout"]).read_text()), len(table), contracts)
        names, expected, counts = entity_cases(table)
        corpus = output / "entity-input.json"
        corpus.write_text(json.dumps(names, ensure_ascii=True) + "\n")
        result = command([binaries / "entity_probe", corpus], logs, "community-entities", 60)
        current["command"] = result
        require_command(result)
        actual = json.loads(Path(result["stdout"]).read_text())
        if actual != expected:
            if len(actual) != len(expected):
                raise ValueError(f"entity response count {len(actual)} != {len(expected)}")
            index = next(i for i, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1])
            raise ValueError(f"entity {names[index]!r}: expected {expected[index]}, got {actual[index]}")
        current.update(status="PASS", detail={**counts, **inventory}, input_sha256=sha256(corpus))
        current = check("pngsuite-primary-reference", "QV-PNG-ORACLE", "official PNGSuite README and pngtest.c: basn6a08 is 8-bit RGBA, 160 zero RGB samples (alpha excluded), 32 rows; no gamma or premultiplication")
        reference = ASSETS / "basn6a08.png"
        info = manifest["corpora"][1]["expected"]
        png_data = reference.read_bytes()
        import struct
        width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", png_data[16:29])
        if (width, height, depth, color, compression, filtering, interlace) != (info["width"], info["height"], info["bit_depth"], info["color_type"], 0, 0, 0):
            raise ValueError("fixed PNG IHDR differs from recorded primary reference")
        reference_rgba = output / "pngsuite.rgba"
        decoded = command([decoder, reference, reference_rgba], logs, "pngsuite-decode", 60)
        current["command"] = decoded
        require_command(decoded)
        metadata = json.loads(Path(decoded["stdout"]).read_text())
        pixels = reference_rgba.read_bytes()
        rgb_zero_samples = sum(pixels[channel::4].count(0) for channel in range(3))
        if metadata != {"width": width, "height": height, "format": "RGBA8"} or len(pixels) != width * height * 4 or rgb_zero_samples != info["rgb_zero_samples"]:
            raise ValueError("libpng output violates official PNGSuite geometry/160-zero-RGB-sample oracle")
        current.update(status="PASS", detail={"metadata": metadata, "rgb_zero_samples": rgb_zero_samples, "decoded_sha256": sha256(reference_rgba), "pixel_oracle": "RGB zero sample count is primary (pngtest.c excludes alpha); decoded pixel SHA is observed, not an independent expected hash"})
        current = check("png-independent-rgba", "QV-PNG-ORACLE", "actual Zig encoder independently decoded by installed libpng, exact straight RGBA bytes and dimensions")
        fixtures = [("straight-alpha", 3, 2, bytes((255, 0, 17, 255, 32, 200, 64, 128, 123, 45, 67, 0, 1, 2, 3, 1, 255, 254, 253, 254, 9, 8, 7, 64))), ("history-windows", 257, 131, bytes(i % 251 for i in range(257 * 131 * 4))), ("pngsuite-reencode", width, height, pixels)]
        results = []
        for name, width, height, pixels in fixtures:
            raw, encoded, decoded_path = (output / f"{name}.{ext}" for ext in ("rgba", "png", "decoded.rgba"))
            raw.write_bytes(pixels)
            encoding = command([binaries / "png_probe", width, height, raw, encoded], logs, f"encode-{name}", 60)
            require_command(encoding)
            decoding = command([decoder, encoded, decoded_path], logs, f"decode-{name}", 60)
            require_command(decoding)
            metadata = json.loads(Path(decoding["stdout"]).read_text())
            if metadata != {"width": width, "height": height, "format": "RGBA8"} or decoded_path.read_bytes() != pixels:
                raise ValueError(f"exact independent RGBA mismatch: {name}")
            results.append({"id": name, "width": width, "height": height, "input_sha256": sha256(raw), "png_sha256": sha256(encoded), "decoded_sha256": sha256(decoded_path), "commands": [encoding, decoding]})
        current.update(status="PASS", detail=results)
        current = check("source-contract-mutations", "QV-MUTATION", "successfully compile each source mutant, then require its intended maintained semantic assertion/leak evidence, never build error/timeout/crash")
        for mutation, row in zip(MUTATIONS, report["mutations"]):
            directory = output / "mutations" / mutation["id"]
            mutant_source = directory / "source"
            shutil.copytree(source, mutant_source)
            path = mutant_source / "browser" / mutation["module"]
            original = path.read_text()
            try:
                patched = apply_mutation(original, mutation, contracts)
            except ValueError as exc:
                row.update(status="INVALID", detail=str(exc))
                continue
            path.write_text(patched)
            row.update(source=str(path), source_sha256=sha256(path), source_original_sha256=hashlib.sha256(original.encode()).hexdigest(), patch={"old": mutation["old"], "new": mutation["new"]})
            executable = directory / "native_tests"
            compiled = command(zig_command(tools["zig"], mutant_source, "native_quality_tests.zig", executable, True, mutation["test"]), directory, "compile")
            row.update(compile=compiled, compile_returncode=compiled["returncode"])
            tested = None
            text = ""
            if compiled["returncode"] == 0 and not compiled["timed_out"]:
                row["binary_sha256"] = sha256(executable)
                row["binary"] = str(executable)
                tested = command([executable], directory, "test", 60)
                row.update(test=tested, test_returncode=tested["returncode"])
                text = command_text(tested)
            row["status"] = mutation_verdict(compiled, tested, mutation, text)
            row["evidence"] = text[-16000:]
        killed = sum(row["status"] == "KILLED" for row in report["mutations"])
        if killed < 10 or any(row["status"] != "KILLED" for row in report["mutations"]):
            raise ValueError(f"mutation gate: {killed}/{len(MUTATIONS)} killed; every declared mutation must compile and fail its intended semantic test")
        current.update(status="PASS", detail={"killed": killed, "total": len(MUTATIONS)})
        report["status"] = "PASS"
    except Exception as exc:
        if current is not None:
            current.update(status="FAIL", detail=str(exc))
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        changed = [name for name, expected in report["identity"]["source_files"].items()
                   if not (CORE / name).is_file() or sha256(CORE / name) != expected]
        if changed:
            report["status"] = "FAIL"
            report["source_drift"] = changed
            report["checks"].append({"id": "source-unchanged", "requirement": "QV-IDENTITY",
                                     "oracle": "same immutable source/corpus identity throughout execution",
                                     "status": "FAIL", "detail": changed})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zig")
    parser.add_argument("--cc")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.is_relative_to(CORE) and not output.is_relative_to(CORE / ".local"):
        parser.error("--output must be outside Core or under Core/.local (private mutation copies only)")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be absent or an empty directory; artifacts are never overwritten")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    report = run(output, args.zig, args.cc)
    path = output / "report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n")
    (output / "report.toon").write_text(toon.encode(report) + "\n")
    print(toon.encode({"status": report["status"], "report": str(output / "report.toon")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
