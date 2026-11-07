#!/usr/bin/env python3
"""Small Core-only web assertions, exact reftests and native-frame contracts."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit
import zlib

HERE = Path(__file__).resolve().parent
CORE = HERE.parent
# Developer source mode: never discover a sibling product or private adapter.
sys.path[:0] = [str(CORE / "mioa-core"), str(CORE / "mioa-headless")]
WIDTH, HEIGHT = 320, 240


class CaseFailure(Exception):
    def __init__(self, status: str, message: str, details=None):
        super().__init__(message)
        self.status, self.details = status, details


def require(condition, message):
    if not condition:
        raise CaseFailure("FAIL", message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_write(path: Path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise ValueError("invalid RGBA geometry")
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    scanlines = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


def compare_pixels(actual: bytes, reference: bytes, width: int, height: int):
    require(len(actual) == len(reference) == width * height * 4, "pixel buffer geometry differs")
    changed, maximum = 0, 0
    diff = bytearray(len(actual))
    for offset in range(0, len(actual), 4):
        delta = max(abs(actual[offset + c] - reference[offset + c]) for c in range(4))
        changed += delta != 0
        maximum = max(maximum, delta)
        diff[offset:offset + 4] = bytes((255 if delta else 0, 0, 255 if delta else 0, 255))
    return {"different_pixels": changed, "max_channel_difference": maximum}, bytes(diff)


def validate_assertions(value, expected):
    if not isinstance(value, dict) or value.get("complete") is not True or not isinstance(value.get("subtests"), list):
        raise CaseFailure("ERROR", "assertion script omitted its completion/result envelope", value)
    rows = value["subtests"]
    if [row.get("name") for row in rows if isinstance(row, dict)] != expected:
        raise CaseFailure("ERROR", "assertion names/order differ from the manifest", value)
    for row in rows:
        if row.get("status") not in {"PASS", "FAIL"} or type(row.get("assertions")) is not int or row["assertions"] < 1:
            raise CaseFailure("ERROR", "subtest did not execute a real assertion", value)
    if any(row["status"] != "PASS" for row in rows):
        raise CaseFailure("FAIL", "JavaScript assertion failed", value)
    return value


async def bounded(awaitable, seconds):
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except TimeoutError as exc:
        raise CaseFailure("TIMEOUT", f"case exceeded its {seconds:g}s deadline") from exc


def failure_record(exc, returncode=None):
    if returncode is not None:
        return {"status": "CRASH", "message": f"native process exited unexpectedly ({returncode})", "cause": str(exc)}
    if isinstance(exc, CaseFailure):
        return {"status": exc.status, "message": str(exc), "details": exc.details}
    code = getattr(exc, "code", None)
    return {"status": "TIMEOUT" if code == "timeout" or isinstance(exc, TimeoutError) else "ERROR",
            "message": str(exc), "error_type": type(exc).__name__, "code": code}


def load_suite(suite):
    manifest = json.loads((HERE / "web" / "cases.json").read_text())
    require(manifest.get("schema") == 1, "unknown web manifest schema")
    cases = manifest["cases" if suite == "web" else "controls"]
    ids = set()
    for case in cases:
        require(case["id"] not in ids and all(c.isalnum() or c in "-_" for c in case["id"]), "invalid/duplicate case id")
        ids.add(case["id"])
        require(case["kind"] in {"assertion", "reftest", "native"}, "unknown case kind")
        require(type(case.get("timeout", 15)) in (int, float) and 0 < case.get("timeout", 15) <= 120, "invalid case deadline")
        for field in ("test", "reference", "script"):
            if field in case:
                relative = Path(case[field])
                require(not relative.is_absolute() and ".." not in relative.parts, "fixture path escaped web directory")
                require((HERE / "web" / relative).is_file(), f"missing fixture {relative}")
    return cases


@contextlib.asynccontextmanager
async def fixture_server():
    # Snapshot only fixture bytes, never expose profiles, output or repository roots.
    files = {"/" + path.relative_to(HERE / "web").as_posix(): path.read_bytes()
             for path in (HERE / "web").rglob("*") if path.is_file() and path.suffix in {".html", ".js", ".css"}}
    async def handle(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            method, target, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            path = unquote(urlsplit(target).path)
            body = files.get(path) if method == "GET" else None
            status = "200 OK" if body is not None else "404 Not Found"
            body = body if body is not None else b"not found"
            mime = "text/javascript" if path.endswith(".js") else "text/css" if path.endswith(".css") else "text/html"
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: {mime}; charset=utf-8\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").encode() + body)
            await asyncio.wait_for(writer.drain(), 2)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=16384)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


async def ready(page):
    # No arbitrary settling sleeps: fixture completion, fonts, then two animation frames.
    while not await page.evaluate("document.readyState === 'complete' && window.caseReady === true"):
        if page.web_test_errors:
            raise CaseFailure("ERROR", "uncaught fixture JavaScript exception before readiness", page.web_test_errors)
        await asyncio.sleep(0.01)
    await page.evaluate("async () => { if (document.fonts) await document.fonts.ready; await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); return true; }")


async def navigate(page, origin, fixture):
    await page.goto(origin + "/" + fixture, timeout=10000)
    await ready(page)


async def capture(page, ring):
    from mioa_headless.frame_ring import HEADER, SLOT_SIZE
    metadata = await page.capture_raw_frame(str(ring.path), ring.back)
    require((metadata["width"], metadata["height"], metadata["stride"], metadata["dpr_milli"]) == (WIDTH, HEIGHT, WIDTH * 4, 1000),
            f"native frame geometry differs: {metadata}")
    require(metadata["focused_frame_id"] == page.frame_id, "raw focus identity is not the live main document")
    offset = HEADER + ring.back * SLOT_SIZE + HEADER
    pixels = bytes(ring.mapping[offset:offset + WIDTH * HEIGHT * 4])
    return metadata, pixels


def probes(pixels, entries):
    for x, y, color in entries:
        expected = bytes.fromhex(color)
        require(len(expected) == 4 and 0 <= x < WIDTH and 0 <= y < HEIGHT, "invalid pixel probe")
        actual = pixels[(y * WIDTH + x) * 4:(y * WIDTH + x) * 4 + 4]
        require(actual == expected, f"pixel ({x},{y}) = {actual.hex()}, expected {color}")


async def native_contract(page, ring, origin, case, directory):
    from mioa_core.native import ProtocolError
    before, pixels = await capture(page, ring)
    (directory / "before.png").write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
    probes(pixels, [[25, 25, "ff0000ff"], [80, 80, "ffffffff"]])
    stable, stable_pixels = await capture(page, ring)
    require(before["render_fingerprint"] == stable["render_fingerprint"] and pixels == stable_pixels,
            "unchanged document did not produce a stable frame identity")
    await page.evaluate("document.getElementById('box').style.backgroundColor = '#00ff00'")
    after, changed = await capture(page, ring)
    (directory / "mutated.png").write_bytes(png_bytes(WIDTH, HEIGHT, changed))
    probes(changed, [[25, 25, "00ff00ff"], [80, 80, "ffffffff"]])
    require(after["render_fingerprint"] != before["render_fingerprint"], "paint mutation did not invalidate render identity")
    # A stale native action must reject before dispatch, not quietly act on a new paint.
    try:
        await page.guarded_input("mouse.click", {"x": 25, "y": 25}, before["render_fingerprint"])
    except ProtocolError as exc:
        require(exc.code == "stale_reference", "stale render action returned the wrong error class")
    else:
        raise CaseFailure("FAIL", "stale render fingerprint accepted native input")
    require(await page.evaluate("window.clicks") == 0, "stale input dispatched an event before rejection")
    await page.guarded_input("mouse.click", {"x": 25, "y": 25}, after["render_fingerprint"])
    require(await page.evaluate("window.clicks") == 1, "current render native click missed the painted target")
    await page.evaluate("document.getElementById('field').focus()")
    focused, _ = await capture(page, ring)
    require(focused.get("focused_element_id") is not None and focused["focused_element_id"] != after.get("focused_element_id"),
            "raw frame did not identify the newly focused input")
    require(focused["caret_kind"] in {"native-shaped-insertion", "focused-element-bounds"}, "focused input omitted native caret geometry")
    x, y, width, height = focused["caret_rect"]
    require(100 <= x <= 220 and 20 <= y <= 50 and width >= 0 and 0 < height <= 30, "native caret is outside the authored input")
    epoch = page.observation_id
    await navigate(page, origin, case["test"])
    new_document, new_pixels = await capture(page, ring)
    require(page.observation_id != epoch, "navigation preserved an obsolete document observation")
    require(new_document["render_fingerprint"] != focused["render_fingerprint"], "navigation retained focused render identity")
    probes(new_pixels, [[25, 25, "ff0000ff"]])
    return {"before": before, "after": after, "focused": focused, "navigated": new_document}


async def exercise(page, ring, origin, case, directory):
    await page._send("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False})
    await navigate(page, origin, case["test"])
    if case["kind"] == "assertion":
        if case.get("input"):
            await page.mouse.click(40, 35)
            await page.keyboard.insert_text("Core")
        helper = (HERE / "web" / "assert.js").read_text()
        source = (HERE / "web" / case["script"]).read_text()
        value = await page.evaluate("async () => {" + helper + "\nawait (" + source + ")(); return {complete:true,subtests:rows}; }")
        return validate_assertions(value, case["assertions"])
    if case["kind"] == "native":
        return await native_contract(page, ring, origin, case, directory)
    actual_meta, actual = await capture(page, ring)
    (directory / "actual.png").write_bytes(png_bytes(WIDTH, HEIGHT, actual))
    await navigate(page, origin, case["reference"])
    reference_meta, reference = await capture(page, ring)
    (directory / "reference.png").write_bytes(png_bytes(WIDTH, HEIGHT, reference))
    metrics, diff = compare_pixels(actual, reference, WIDTH, HEIGHT)
    if metrics["different_pixels"]:
        (directory / "diff.png").write_bytes(png_bytes(WIDTH, HEIGHT, diff))
        raise CaseFailure("FAIL", "exact reftest pixel mismatch", metrics)
    probes(actual, case["probes"])
    return {**metrics, "actual": actual_meta, "reference": reference_meta}


async def run_case(case, binary, origin, output):
    from mioa_core import profile
    from mioa_core.native import launch_native
    from mioa_core.native_spec import create_native_spec
    from mioa_headless.frame_ring import FrameRing
    directory = output / case["id"]
    directory.mkdir()
    result = {"id": case["id"], "kind": case["kind"], "status": "ERROR"}
    started = time.monotonic()
    session, ring = None, None
    script_errors = []
    old_base = profile.BASE
    with tempfile.TemporaryDirectory(prefix="mioa-web-") as temporary:
        profile.BASE = Path(temporary)
        name = "web-test"
        try:
            cfg = create_native_spec(name, runtime="mioa", binary=binary,
                storage_path=profile.profile_dir(name) / "data", seed="01" * 32,
                overrides={"screen_width": WIDTH, "screen_height": 320, "avail_width": WIDTH,
                           "avail_height": 320, "device_scale_factor": 1},
                network={"allow_private_network": True})
            profile.save_config(name, cfg)
            result["runtime"] = cfg["runtime"]
            result["inputs_sha256"] = {case[key]: sha((HERE / "web" / case[key]).read_bytes())
                                       for key in ("test", "reference", "script") if key in case}
            with profile.exclusive(name) as owned:
                require(owned, "isolated native engine lease was unavailable")
                try:
                    session = await bounded(launch_native(name, cfg, True), 40)
                    result["receipt"] = session.receipt
                    page = session.context.pages[0] if session.context.pages else await bounded(session.context.new_page(), 5)
                    def script_event(event):
                        if event.get("method") == "Runtime.exceptionThrown" and event.get("sessionId") == page.session_id:
                            script_errors.append(event.get("params", {}).get("exceptionDetails", {}))
                            del script_errors[:-50]
                    session.cdp.listeners.append(script_event)
                    page.web_test_errors = script_errors
                    ring = FrameRing(Path(temporary) / "frame.shm", Path(temporary) / "frame.sock")
                    result["details"] = await bounded(exercise(page, ring, origin, case, directory), case.get("timeout", 15))
                    if script_errors:
                        raise CaseFailure("ERROR", "uncaught fixture JavaScript exception", script_errors)
                    require(session.process.returncode is None, "native process exited before case completion")
                    result["status"] = "PASS"
                except Exception as exc:
                    result.update(failure_record(exc, session.process.returncode if session else None))
                    if script_errors:
                        result["script_errors"] = script_errors
                finally:
                    if session:
                        try:
                            await session.close()
                        except Exception as exc:
                            result["shutdown_error"] = str(exc)
                            if result["status"] == "PASS":
                                result.update(failure_record(exc))
                        (directory / "native.log").write_text(session.log_text())
                    if ring:
                        ring.close()
        except Exception as exc:
            result.update(failure_record(exc))
        finally:
            profile.BASE = old_base
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    json_write(directory / "result.json", result)
    return result


def source_info(binary):
    from mioa_core.native_spec import file_sha256
    sources = {}
    for root in (HERE, CORE / "mioa-core" / "mioa_core", CORE / "mioa-headless" / "mioa_headless"):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".js", ".json", ".html", ".css", ".c"} and "__pycache__" not in path.parts:
                sources[path.relative_to(CORE).as_posix()] = sha(path.read_bytes())
    return {"binary": str(binary), "binary_sha256": file_sha256(binary), "sources_sha256": sources,
            "viewport": {"width": WIDTH, "height": HEIGHT, "dpr": 1}, "python": sys.version}


async def run(args, cases, output):
    report = {"schema": 1, "suite": args.suite, "results": [], "status": "ERROR"}
    try:
        binary = Path(args.binary).expanduser().resolve()
        require(binary.is_file() and os.access(binary, os.X_OK), f"native binary is absent or not executable: {binary}")
        report["source"] = source_info(binary)
        async with fixture_server() as origin:
            for case in cases:
                result = await run_case(case, binary, origin, output)
                report["results"].append(result)
                print(json.dumps({"id": result["id"], "status": result["status"], "duration_ms": result["duration_ms"]}), flush=True)
        report["status"] = "PASS" if all(row["status"] == "PASS" for row in report["results"]) else "FAIL"
    except Exception as exc:
        report["error"] = failure_record(exc)
    json_write(output / "results.json", report)
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", help="explicit real native engine executable (required except --list)")
    parser.add_argument("--output", type=Path, help="new, empty artifact directory; defaults outside source under the system temp directory")
    parser.add_argument("--filter", action="append", default=[], help="case-id substring (repeatable, OR)")
    parser.add_argument("--kind", choices=("assertion", "reftest", "native"))
    parser.add_argument("--suite", choices=("web", "controls"), default="web")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    cases = [case for case in load_suite(args.suite) if (not args.filter or any(term in case["id"] for term in args.filter))
             and (not args.kind or case["kind"] == args.kind)]
    if not cases:
        parser.error("selection matched no cases (not a successful empty run)")
    if args.list:
        print(json.dumps(cases, indent=2))
        return 0
    if not args.binary:
        parser.error("--binary is required; missing capabilities are never skipped")
    output = args.output.expanduser().resolve() if args.output else Path(tempfile.mkdtemp(prefix="mioa-web-results-"))
    # Evidence must never contaminate tracked source, even when explicitly requested.
    if output.is_relative_to(CORE) and not output.is_relative_to(CORE / ".local"):
        parser.error("--output must be outside the Core source tree, or beneath ignored core/.local")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    print(f"Artifacts: {output}", file=sys.stderr)
    return asyncio.run(run(args, cases, output))


if __name__ == "__main__":
    raise SystemExit(main())
