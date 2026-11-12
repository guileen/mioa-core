#!/usr/bin/env python3
"""Small Core-only web assertions, exact reftests and native-frame contracts."""
from __future__ import annotations

import argparse
import base64
import asyncio
import contextlib
import hashlib
from http.cookies import SimpleCookie
import json
import math
import os
import secrets
from pathlib import Path
import struct
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit
import zlib

import evidence_view
import toon

HERE = Path(__file__).resolve().parent
CORE = HERE.parent
# Developer source mode: never discover a sibling product or private adapter.
sys.path[:0] = [str(CORE / package) for package in ("mioa-core", "mioa-headless", "mioa-os/api/system", "mioa-os")]
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
        require(case["kind"] in {"assertion", "reftest", "native", "wpt"}, "unknown case kind")
        require(type(case.get("timeout", 15)) in (int, float) and 0 < case.get("timeout", 15) <= 120, "invalid case deadline")
        for field in ("test", "reference", "script", "destination", "back_script", "forward_script"):
            if field in case:
                relative = Path(case[field])
                require(not relative.is_absolute() and ".." not in relative.parts, "fixture path escaped web directory")
                require((HERE / "web" / relative).is_file(), f"missing fixture {relative}")
        if case["kind"] == "wpt":
            pinned = json.loads((HERE / "wpt-manifest.json").read_text())
            for entry in pinned["files"]:
                require(sha((CORE / entry["path"]).read_bytes()) == entry["sha256"],
                        f"pinned WPT input changed: {entry['path']}")
    return cases


@contextlib.asynccontextmanager
async def fixture_server(*, authentication=False, streams=None):
    # Snapshot only fixture bytes, never expose profiles, output or repository roots.
    files = {"/" + path.relative_to(HERE / "web").as_posix(): path.read_bytes()
             for path in (HERE / "web").rglob("*") if path.is_file() and path.suffix in {".html", ".js", ".css"}}
    sessions = set()
    handlers, writers = set(), set()
    def session_page(state, form):
        return ('<!doctype html><meta charset="utf-8"><title>Local session task</title>'
                '<style>input,button{position:absolute;left:20px;width:240px;height:32px;box-sizing:border-box}'
                '#username{top:20px}#password{top:60px}button{top:100px}</style>'
                '<h2 style="margin-top:150px">Local session task</h2>' + form
                + ('<p id="private-draft">' + state["draft"] + '</p>' if state.get("authenticated") else "")
                + '<script>window.sessionView=' + json.dumps(state, ensure_ascii=False)
                + ';window.caseReady=true;</script>').encode()
    login_form = ('<form method="post" action="/session/login">'
                  '<input id="username" name="username" aria-label="Username">'
                  '<input id="password" name="password" type="password" aria-label="Password">'
                  '<button>Sign in</button></form>')
    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        writers.add(writer)
        stream = None
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            method, target, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            path = unquote(urlsplit(target).path)
            if streams is not None and method == "GET" and path in streams:
                stream = streams[path]
                prefix, tail = stream["prefix"], stream["tail"]
                if "requested" in stream:
                    stream["requested"].set()
                if "headers_release" in stream:
                    await stream["headers_release"].wait()
                content_type = (f"Content-Type: {stream['content_type']}\r\n"
                                if stream["content_type"] is not None else "")
                writer.write((f"HTTP/1.1 200 OK\r\n{content_type}"
                              f"Content-Length: {len(prefix) + len(tail)}\r\n"
                              "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode())
                await asyncio.wait_for(writer.drain(), 2)
                stream["headers_flushed"].set()
                await stream["body_release"].wait()
                writer.write(prefix)
                await asyncio.wait_for(writer.drain(), 2)
                stream["prefix_flushed"].set()
                await stream["release"].wait()
                writer.write(tail)
                await asyncio.wait_for(writer.drain(), 2)
                stream["tail_sent"] = True
                return
            headers = {}
            for line in request.split(b"\r\n")[1:-2]:
                key, value = line.decode("latin-1").split(":", 1)
                headers[key.lower()] = value.strip()
            extra = ""
            body = files.get(path) if method == "GET" else None
            status = "200 OK" if body is not None else "404 Not Found"
            if authentication and path.startswith("/session/"):
                size = int(headers.get("content-length", "0"))
                if not 0 <= size <= 4096:
                    raise ValueError("fixture form exceeds its limit")
                data = await asyncio.wait_for(reader.readexactly(size), 2) if size else b""
                cookies = SimpleCookie()
                cookies.load(headers.get("cookie", ""))
                token = cookies["mioa_session"].value if "mioa_session" in cookies else None
                anonymous = {"authenticated": False}
                if method == "GET" and path == "/session/login":
                    status, body = "200 OK", session_page(anonymous, login_form)
                elif method == "POST" and path == "/session/login":
                    values = parse_qs(data.decode("utf-8"))
                    if values == {"username": ["local-user"], "password": ["fixture-password"]}:
                        token = secrets.token_urlsafe(24)
                        sessions.add(token)
                        status, body = "303 See Other", b""
                        extra = (f"Location: /session/private\r\nSet-Cookie: mioa_session={token}; "
                                 "Path=/session/; HttpOnly; SameSite=Lax\r\n")
                    else:
                        status, body = "401 Unauthorized", session_page(anonymous, login_form)
                elif method == "GET" and path == "/session/private":
                    authorized = token in sessions
                    status = "200 OK" if authorized else "401 Unauthorized"
                    body = session_page(
                        {"authenticated": True, "draft": "server-only draft 中文"} if authorized else anonymous,
                        '<form method="post" action="/session/logout"><button>Sign out</button></form>'
                        if authorized else login_form)
                elif method == "POST" and path == "/session/logout":
                    sessions.discard(token)
                    status, body = "303 See Other", b""
                    extra = ("Location: /session/login\r\nSet-Cookie: mioa_session=; "
                             "Path=/session/; HttpOnly; SameSite=Lax; Max-Age=0\r\n")
            body = body if body is not None else b"not found"
            mime = "text/javascript" if path.endswith(".js") else "text/css" if path.endswith(".css") else "text/html"
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: {mime}; charset=utf-8\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n{extra}\r\n").encode() + body)
            await asyncio.wait_for(writer.drain(), 2)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
            writers.discard(writer)
            handlers.discard(task)
            if stream is not None:
                stream["closed"].set()
    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=16384)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        # start_server does not own client callbacks. Reap them before
        # wait_closed, which also waits for active clients on Python 3.12+.
        for writer in tuple(writers):
            writer.close()
        pending = tuple(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


async def ready(page):
    # No arbitrary settling sleeps: fixture completion, fonts, then two animation frames.
    while not await page.evaluate("document.readyState === 'complete' && window.caseReady === true"):
        if page.web_test_errors:
            raise CaseFailure("ERROR", "uncaught fixture JavaScript exception before readiness", page.web_test_errors)
        await asyncio.sleep(0.01)
    await page.evaluate("async () => { if (document.fonts) await document.fonts.ready; await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); return true; }")
async def poll_path(page, path: str):
    target = "/" + path
    while True:
        try:
            if await page.evaluate("location.pathname === " + json.dumps(target)):
                return
        except Exception:
            pass
        await asyncio.sleep(0.01)




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

    async def reject_stale(fingerprint, reason):
        clicks = await page.evaluate("window.clicks")
        try:
            await page.guarded_input("mouse.click", {"x": 25, "y": 25}, fingerprint)
        except ProtocolError as exc:
            require(exc.code == "stale_reference", reason + " returned the wrong error class")
        else:
            raise CaseFailure("FAIL", reason + " accepted input from an obsolete capture")
        require(await page.evaluate("window.clicks") == clicks, reason + " dispatched before rejection")

    await page.evaluate("document.getElementById('box').style.backgroundColor = '#00ff00'")
    await reject_stale(stable["render_fingerprint"], "paint change without recapture")
    try:
        await page.capture_raw_frame(str(ring.path.with_name("missing-frame")), ring.back)
    except ProtocolError:
        pass
    else:
        raise CaseFailure("FAIL", "capture unexpectedly accepted an absent ring")
    await reject_stale(stable["render_fingerprint"], "failed capture after paint change")
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
    prefocus, _ = await capture(page, ring)
    await page.evaluate("document.getElementById('field').focus()")
    await reject_stale(prefocus["render_fingerprint"], "focus change without recapture")
    focused, _ = await capture(page, ring)
    require(focused.get("focused_element_id") is not None and focused["focused_element_id"] != after.get("focused_element_id"),
            "raw frame did not identify the newly focused input")
    require(focused["caret_kind"] in {"native-shaped-insertion", "focused-element-bounds"}, "focused input omitted native caret geometry")
    x, y, width, height = focused["caret_rect"]
    require(100 <= x <= 220 and 20 <= y <= 50 and width >= 0 and 0 < height <= 30, "native caret is outside the authored input")
    await page.evaluate("""() => {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 40;
      canvas.style.cssText = 'position:absolute;left:250px;top:20px';
      document.body.appendChild(canvas);
      window.guardCanvas = canvas.getContext('2d');
      guardCanvas.fillStyle = '#ff0000'; guardCanvas.fillRect(0,0,40,40);
    }""")
    canvas_frame, canvas_pixels = await capture(page, ring)
    probes(canvas_pixels, [[260, 30, "ff0000ff"]])
    await page.evaluate("guardCanvas.fillStyle = '#0000ff'; guardCanvas.fillRect(0,0,40,40)")
    await reject_stale(canvas_frame["render_fingerprint"], "Canvas write without recapture")
    await page.evaluate("document.body.style.height = '2500px'")
    prescroll, _ = await capture(page, ring)
    await page.evaluate("window.scrollTo(0,30)")
    await reject_stale(prescroll["render_fingerprint"], "scroll change without recapture")
    epoch = page.observation_id
    await navigate(page, origin, case["test"])
    new_document, new_pixels = await capture(page, ring)
    require(page.observation_id != epoch, "navigation preserved an obsolete document observation")
    require(new_document["render_fingerprint"] != focused["render_fingerprint"], "navigation retained focused render identity")
    probes(new_pixels, [[25, 25, "ff0000ff"]])
    return {"before": before, "after": after, "focused": focused, "navigated": new_document,
            "rejected_without_recapture": ["paint", "failed-capture", "focus", "canvas", "scroll"]}


async def scaled_overflow(page, ring, directory, scale):
    from mioa_headless.frame_ring import HEADER, SLOT_SIZE
    observations = []
    for offset in (0, 80):
        await page.evaluate(f"for (const id of ['port','translated']) document.getElementById(id).scrollTop = {offset}")
        metadata = await page.capture_raw_frame(str(ring.path), ring.back)
        width, height = int(WIDTH * scale), int(HEIGHT * scale)
        require((metadata["width"], metadata["height"], metadata["stride"], metadata["dpr_milli"])
                == (width, height, width * 4, int(scale * 1000)), "scaled frame geometry")
        start = HEADER + ring.back * SLOT_SIZE + HEADER
        pixels = bytes(ring.mapping[start:start + width * height * 4])
        entries = [(int(20 * scale), int(30 * scale), "ff0000ff" if offset == 0 else "0000ffff"),
                   (int(90 * scale) - 1, int(50 * scale), "ff0000ff" if offset == 0 else "0000ffff"),
                   (int(90 * scale), int(50 * scale), "ffffffff"),
                   (int(20 * scale), int(100 * scale), "ffffffff"),
                   (int(122 * scale), int(12 * scale), "ffffffff"),
                   (int(165 * scale), int(55 * scale), "00ff00ff"),
                   (int(212 * scale), int(55 * scale), "ffffffff"),
                   (int(199 * scale), int(145 * scale), "ff00ffff"),
                   (int(200 * scale), int(145 * scale), "ffffffff"),
                   (int(140 * scale), int(160 * scale), "ffffffff"),
                   (int(255 * scale), int(35 * scale), "ffffffff"),
                   (int(275 * scale), int(35 * scale), "000000ff"),
                   (int(279 * scale), int(35 * scale), "ffffffff"),
                   (int(225 * scale), int(95 * scale), "ffffffff"),
                   (int(230 * scale), int(80 * scale), "ffffffff"),
                   (int(240 * scale), int(95 * scale), "000000ff"),
                   (int(300 * scale), int(95 * scale), "ffffffff"),
                   (int(260 * scale), int(150 * scale), "ffffffff"),
                   (int(295 * scale), int(150 * scale), "ffffffff"),
                   (int(70 * scale), int(205 * scale), "ff0000ff" if offset == 0 else "0000ffff"),
                   (int(140 * scale) - 1, int(205 * scale), "ff0000ff" if offset == 0 else "0000ffff"),
                   (int(140 * scale), int(205 * scale), "ffffffff"),
                   (int(60 * scale), int(185 * scale), "ffffffff"),
                   (int(100 * scale), math.ceil(225 * scale), "ffffffff"),
                   (int(181 * scale), int(186 * scale), "ffffffff"),
                   (int(205 * scale), int(205 * scale), "00ffffff"),
                   (int(240 * scale) - 1, int(205 * scale), "00ffffff"),
                   (int(240 * scale), int(205 * scale), "ffffffff"),
                   (int(260 * scale), int(205 * scale), "ffffffff"),
                   (int(275 * scale), int(205 * scale), "ffffffff"),
                   (int(295 * scale), int(205 * scale), "ffffffff")]
        for x, y, color in entries:
            actual = pixels[(y * width + x) * 4:(y * width + x) * 4 + 4].hex()
            require(actual == color,
                    f"scale={scale} scroll={offset} pixel=({x},{y}) {actual} != {color}")
        soft_x, soft_y = int(283 * scale), int(155 * scale)
        soft = pixels[(soft_y * width + soft_x) * 4:(soft_y * width + soft_x) * 4 + 4]
        require(0 < soft[0] < 250 and soft[0] == soft[1] == soft[2] and soft[3] == 255,
                f"scaled rounded shadow lost its translucent ink: {soft.hex()}")
        def region(x0, y0, x1, y1):
            return (pixels[(y * width + x) * 4:(y * width + x) * 4 + 4]
                    for y in range(int(y0 * scale), int(y1 * scale))
                    for x in range(int(x0 * scale), int(x1 * scale)))
        require(any(pixel == b"\0\0\0\xff" for pixel in region(10, 130, 90, 160)),
                "clipped shaped text has no visible ink")
        require(all(pixel == b"\xff\xff\xff\xff" for pixel in region(90, 130, 110, 165)),
                "shaped text escaped horizontal overflow clip")
        require(all(pixel == b"\xff\xff\xff\xff" for pixel in region(10, 160, 90, 165)),
                "shaped text escaped vertical overflow clip")
        (directory / f"overflow-{scale}-{offset}.png").write_bytes(png_bytes(width, height, pixels))
        observations.append({"scale": scale, "scroll": offset, "frame": metadata, "soft_shadow": soft.hex()})
    opacity_states = []
    for opacity in (0.5, 1.0, 0.0):
        await page.evaluate(f"document.getElementById('zero-group').style.opacity = '{opacity}'")
        metadata = await page.capture_raw_frame(str(ring.path), ring.back)
        require((metadata["width"], metadata["height"], metadata["stride"], metadata["dpr_milli"])
                == (width, height, width * 4, int(scale * 1000)), "opacity transition frame geometry")
        pixels = bytes(ring.mapping[start:start + width * height * 4])
        expected, tolerance = (128, 1) if opacity == 0.5 else (0 if opacity else 255, 0)
        samples = []
        for x in (260, 275, 295):
            index = (int(205 * scale) * width + int(x * scale)) * 4
            pixel = pixels[index:index + 4]
            require(pixel[0] == pixel[1] == pixel[2] and pixel[3] == 255
                    and abs(pixel[0] - expected) <= tolerance,
                    f"opacity={opacity} x={x} group applied more than once or retained stale ink: {pixel.hex()}")
            samples.append(pixel.hex())
        (directory / f"opacity-{opacity}.png").write_bytes(png_bytes(width, height, pixels))
        opacity_states.append({"opacity": opacity, "frame": metadata, "pixels": samples})
    return {"observations": observations, "opacity_states": opacity_states}


async def scaled_canvas(page, ring, directory):
    from mioa_headless.frame_ring import HEADER, SLOT_SIZE
    from run_png_roundtrip import decode_png
    observations = []
    for scale in (1.5, 2, 0.75):
        await page._send("Emulation.setDeviceMetricsOverride", {
            "width": WIDTH, "height": HEIGHT, "deviceScaleFactor": scale, "mobile": False})
        await page.evaluate("ctx.fillStyle='#d02040';ctx.fillRect(0,0,20,20)")
        for offset, updated in ((0, False), (10, False), (10, True)):
            await page.evaluate(f"document.getElementById('port').scrollTop={offset}")
            if updated:
                await page.evaluate("ctx.fillStyle='#00ff00';ctx.fillRect(0,0,20,20)")
            frame = await page.capture_raw_frame(str(ring.path), ring.back)
            width, height = int(WIDTH * scale), int(HEIGHT * scale)
            require((frame["width"], frame["height"], frame["dpr_milli"])
                    == (width, height, int(scale * 1000)), "Canvas device geometry")
            start = HEADER + ring.back * SLOT_SIZE + HEADER
            pixels = bytes(ring.mapping[start:start + width * height * 4])
            entries = [(int(100 * scale) - 1, int(75 * scale), "ffffffff"),
                       (int(100 * scale), int(75 * scale), "000000ff"),
                       (int(101 * scale), int(41 * scale), "ffffffff"),
                       (int(120 * scale), int((60 - offset) * scale), "ffffffff"),
                       (int(120 * scale), int((80 - offset) * scale), "00ff00ff" if updated else "d02040ff"),
                       (int(160 * scale), int((80 - offset) * scale), "2563ebff"),
                       (int(188 * scale), int(75 * scale), "ffffffff")]
            for x, y, expected in entries:
                actual = pixels[(y * width + x) * 4:(y * width + x) * 4 + 4].hex()
                require(actual == expected,
                        f"Canvas scale={scale} scroll={offset} updated={updated} ({x},{y}) {actual} != {expected}")
            stem = f"canvas-{scale}-{offset}-{updated}"
            (directory / f"{stem}.png").write_bytes(png_bytes(width, height, pixels))
            reply = await page._send("Page.captureScreenshot", {"clip": {
                "x": 90, "y": 30, "width": 120, "height": 100, "scale": 1}})
            encoded = base64.b64decode(reply["data"])
            cropped_width, cropped_height = int(120 * scale), int(100 * scale)
            cropped = decode_png(encoded, cropped_width, cropped_height)
            for x, y, expected in entries:
                local_x, local_y = x - int(90 * scale), y - int(30 * scale)
                actual = cropped[(local_y * cropped_width + local_x) * 4:(local_y * cropped_width + local_x) * 4 + 4].hex()
                require(actual == expected,
                        f"Canvas region scale={scale} ({local_x},{local_y}) {actual} != {expected}")
            (directory / f"{stem}-region.png").write_bytes(encoded)
            observations.append({"scale": scale, "scroll": offset, "updated": updated, "frame": frame})
    return {"observations": observations,
            "scope": "native full and cropped captures; Canvas backing updates, object-fit/position, border/padding, rounded ancestor clip and scroll at fractional DPR"}


def validate_wpt(value):
    def envelope(condition, message):
        if not condition:
            raise CaseFailure("ERROR", message, value)
    envelope(isinstance(value, dict) and value.get("complete") is True, "WPT completion missing")
    harness = value.get("harness")
    envelope(isinstance(harness, dict) and type(harness.get("status")) is int
             and harness["status"] in {0, 1, 2, 3}, "unknown WPT harness status")
    rows = value.get("subtests")
    envelope(isinstance(rows, list), "WPT subtests missing")
    for row in rows:
        envelope(isinstance(row, dict) and isinstance(row.get("name"), str)
                 and type(row.get("status")) is int and row["status"] in {0, 1, 2, 3, 4},
                 "malformed WPT subtest")
    if harness["status"] != 0:
        raise CaseFailure("TIMEOUT" if harness["status"] == 2 else "ERROR", "WPT harness failed", value)
    envelope(rows, "WPT registered no tests")
    envelope(len({row["name"] for row in rows}) == len(rows), "duplicate WPT test names")
    if any(row["status"] == 2 for row in rows):
        raise CaseFailure("TIMEOUT", "WPT subtest timed out", value)
    if any(row["status"] in {3, 4} for row in rows):
        raise CaseFailure("UNSUPPORTED", "WPT not run or precondition failed", value)
    if any(row["status"] == 1 for row in rows):
        raise CaseFailure("FAIL", "WPT semantic assertion failed", value)
    return value


async def history_form(page, origin, case):
    # Text entry uses the same native pointer/keyboard path as search-return.
    await page.mouse.click(40, 35)
    await page.keyboard.insert_text("Café & tea")
    await page.mouse.click(40, 85)
    await page.keyboard.insert_text("First line\nSecond line")
    prepared = await page.evaluate("""() => {
      document.getElementById('remember').click();
      document.getElementById('daily').click();
      document.getElementById('category').selectedIndex = 2;
      document.getElementById('secret').value = 'private-test-value';
      const transfer = new DataTransfer();
      document.getElementById('removed').remove();
      document.getElementById('survivor').value = 'surviving edit';
      transfer.items.add(new File(['fixture'], 'fixture.txt', {type: 'text/plain'}));
      document.getElementById('upload').files = transfer.files;
      return document.getElementById('query').value === 'Café & tea' &&
        document.getElementById('notes').value === 'First line\\nSecond line' &&
        document.getElementById('survivor').value === 'surviving edit' &&
        document.getElementById('remember').checked &&
        document.getElementById('daily').checked &&
        document.getElementById('category').value === 'books' &&
        document.getElementById('secret').value === 'private-test-value' &&
        document.getElementById('upload').files.length === 1;
    }""")
    require(prepared is True, "history form controls were not populated before navigation")
    await navigate(page, origin, case["destination"])
    await page.mouse.click(40, 35)
    await page.keyboard.insert_text("Forward entry")
    require(await page.evaluate("document.getElementById('forward').value === 'Forward entry'"),
            "forward form text was not populated before back navigation")
    if case.get("javascript_back"):
        await page.evaluate("() => { setTimeout(() => history.back(), 0); return true; }")
        await poll_path(page, case["test"])
    else:
        await page.go_back()
    await ready(page)
    helper = (HERE / "web" / "assert.js").read_text()
    back = (HERE / "web" / case["back_script"]).read_text()
    first = await page.evaluate("async () => {" + helper + "\nawait (" + back + ")(); return {complete:true,subtests:rows}; }")
    # Use the protocol to exercise forward traversal; the native facade has no go_forward.
    history = await page._send("Page.getNavigationHistory")
    entries, index = history.get("entries"), history.get("currentIndex")
    require(isinstance(entries, list) and type(index) is int and index + 1 < len(entries),
            "forward history entry is missing")
    await page._send("Page.navigateToHistoryEntry", {"entryId": entries[index + 1]["id"]})
    await poll_path(page, case["destination"])
    await ready(page)
    forward = (HERE / "web" / case["forward_script"]).read_text()
    second = await page.evaluate("async () => {" + helper + "\nawait (" + forward + ")(); return {complete:true,subtests:rows}; }")
    require(isinstance(first, dict) and first.get("complete") is True and isinstance(first.get("subtests"), list)
            and isinstance(second, dict) and second.get("complete") is True and isinstance(second.get("subtests"), list),
            "history assertion script omitted its completion/result envelope")
    return validate_assertions({"complete": True, "subtests": first["subtests"] + second["subtests"]}, case["assertions"])


async def storage_draft(page, origin, case):
    draft = {"query": "C++ café & tea", "notes": "第一行\nSecond line", "filters": ["books", "中文"]}
    expected = {"draft": draft, "tab": "original-tab"}
    read = """() => {
        const draft = localStorage.getItem('mioa-draft');
        return {draft: draft === null ? null : JSON.parse(draft),
                tab: sessionStorage.getItem('mioa-tab')};
    }"""
    write = """value => {
        localStorage.setItem('mioa-draft', JSON.stringify(value.draft));
        sessionStorage.setItem('mioa-tab', value.tab);
    }"""
    await page.evaluate(write, expected)
    await navigate(page, origin, "fixtures/empty.html")
    await page.reload(timeout=10000)
    observed = {"after_reload": await page.evaluate(read)}
    require(observed["after_reload"] == expected,
            f"draft or tab state was lost across same-origin navigation/reload: {observed!r}")
    async with fixture_server() as foreign:
        await navigate(page, foreign, case["test"])
        observed["foreign_initial"] = await page.evaluate(read)
        require(observed["foreign_initial"] == {"draft": None, "tab": None},
                f"another origin could read the original draft or tab state: {observed!r}")
        await page.evaluate(write, {"draft": {"query": "foreign draft"}, "tab": "foreign-tab"})
        await navigate(page, origin, case["test"])
        observed["restored"] = await page.evaluate(read)
        require(observed["restored"] == expected,
                f"another origin overwrote the original draft or tab state: {observed!r}")
        await navigate(page, foreign, case["test"])
        observed["foreign_restored"] = await page.evaluate(read)
        require(observed["foreign_restored"] == {
                    "draft": {"query": "foreign draft"}, "tab": "foreign-tab"},
                f"origin partition did not retain its own state after returning: {observed!r}")
    return {"goal": case["goal"], "observations": observed,
            "scope": "one tab, navigation/reload and two loopback origins; no restart or login"}


async def storage_tabs(page, origin, case):
    original = {"query": "C++ café & tea", "revision": 1}
    edited = {"query": "中文 books", "revision": 2}
    continued = {"query": "remaining tab", "revision": 3}
    read = """() => {
        const draft = localStorage.getItem('mioa-shared-draft');
        return {draft: draft === null ? null : JSON.parse(draft),
                tab: sessionStorage.getItem('mioa-tab-draft')};
    }"""
    write = """value => {
        localStorage.setItem('mioa-shared-draft', JSON.stringify(value.draft));
        sessionStorage.setItem('mioa-tab-draft', value.tab);
    }"""
    await page.evaluate(write, {"draft": original, "tab": "tab-A"})
    other = await page.context.new_page()
    observed = {}
    try:
        await navigate(other, origin, case["test"])
        require(await other.evaluate("window.opener === null") is True,
                "independent tab unexpectedly has an opener")
        observed["new_tab"] = await other.evaluate(read)
        require(observed["new_tab"] == {"draft": original, "tab": None},
                f"independent tab did not share only the local draft: {observed!r}")
        await other.evaluate(write, {"draft": edited, "tab": "tab-B"})
        await page.reload(timeout=10000)
        observed["original_reloaded"] = await page.evaluate(read)
        require(observed["original_reloaded"] == {"draft": edited, "tab": "tab-A"},
                f"second tab edit was lost or replaced the original session: {observed!r}")
        await other.reload(timeout=10000)
        observed["second_reloaded"] = await other.evaluate(read)
        require(observed["second_reloaded"] == {"draft": edited, "tab": "tab-B"},
                f"second tab session was lost or overwritten: {observed!r}")
    finally:
        await other.close()
    await page.evaluate("draft => localStorage.setItem('mioa-shared-draft', JSON.stringify(draft))", continued)
    observed["after_peer_close"] = await page.evaluate(read)
    require(observed["after_peer_close"] == {"draft": continued, "tab": "tab-A"},
            f"remaining tab could not continue after peer close: {observed!r}")
    return {"goal": case["goal"], "observations": observed,
            "scope": "two independent same-origin tabs in one standalone Profile; no popup, restart or managed-domain qualification"}


async def storage_profiles(page, origin, case, directory):
    from mioa_core import profile
    from mioa_core.native import launch_native
    from mioa_core.native_spec import create_native_spec
    from mioa_os import LocalArtifactContent, LocalChildProcesses, LocalHostFiles, LocalProcessEnvironment, LocalProcessIdentity, LocalProfileLeases, LocalSecureRandom, LocalStreamConnections

    original = {"draft": {"query": "C++ café & tea", "revision": 1}, "tab": "profile-A", "cookie": "profile-A"}
    second = {"draft": {"query": "中文 books", "revision": 2}, "tab": "profile-B", "cookie": "profile-B"}
    empty = {"draft": None, "tab": None, "cookie": None}
    read = r"""() => {
        const draft = localStorage.getItem('mioa-profile-draft');
        const cookie = document.cookie.match(/(?:^|;\s*)mioa-profile=([^;]*)/);
        return {draft: draft === null ? null : JSON.parse(draft),
                tab: sessionStorage.getItem('mioa-profile-tab'),
                cookie: cookie ? cookie[1] : null};
    }"""
    write = """value => {
        localStorage.setItem('mioa-profile-draft', JSON.stringify(value.draft));
        sessionStorage.setItem('mioa-profile-tab', value.tab);
        document.cookie = 'mioa-profile=' + value.cookie + '; Path=/; SameSite=Lax';
    }"""
    await page.evaluate(write, original)
    primary = page.context.owner.config
    name = "web-peer"
    cfg = create_native_spec(name, runtime=primary["runtime"]["id"],
        binary=primary["runtime"]["binary"], storage_path=profile.profile_dir(name) / "data",
        seed=primary["seed"], overrides=primary["overrides"], network=primary["network"],
        permissions=primary["permissions"], asset_paths=primary["asset_paths"],
        artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
    profile.save_config(name, cfg)
    observed, errors = {}, []
    peer = None
    with profile.exclusive(name, leases=LocalProfileLeases()) as owned:
        require(owned, "independent Profile lease was unavailable")
        try:
            peer = await bounded(launch_native(name, cfg, True,
                child_processes=LocalChildProcesses(), process_environment=LocalProcessEnvironment(),
                identity=LocalProcessIdentity(), profile_lease=owned, random=LocalSecureRandom(),
                artifact_content=LocalArtifactContent(),
                connections=LocalStreamConnections(), files=LocalHostFiles()), 40)
            other = peer.context.pages[0] if peer.context.pages else await peer.context.new_page()
            def script_event(event):
                if event.get("method") == "Runtime.exceptionThrown" and event.get("sessionId") == other.session_id:
                    errors.append(event.get("params", {}).get("exceptionDetails", {}))
                    del errors[:-50]
            peer.cdp.listeners.append(script_event)
            other.web_test_errors = errors
            await navigate(other, origin, case["test"])
            observed["peer_initial"] = await other.evaluate(read)
            require(observed["peer_initial"] == empty,
                    f"independent Profile could read another Profile's data: {observed!r}")
            await other.evaluate(write, second)
            await other.reload(timeout=10000)
            observed["peer_reloaded"] = await other.evaluate(read)
            require(observed["peer_reloaded"] == second,
                    f"peer Profile did not retain its own data: {observed!r}")
            await page.reload(timeout=10000)
            observed["original_reloaded"] = await page.evaluate(read)
            require(observed["original_reloaded"] == original,
                    f"peer Profile overwrote the original data: {observed!r}")
            await other.evaluate("""() => {
                localStorage.clear(); sessionStorage.clear();
                document.cookie = 'mioa-profile=; Path=/; Max-Age=0';
            }""")
            await other.reload(timeout=10000)
            observed["peer_cleared"] = await other.evaluate(read)
            require(observed["peer_cleared"] == empty,
                    f"peer Profile did not clear its own data: {observed!r}")
            if errors:
                raise CaseFailure("ERROR", "uncaught peer fixture JavaScript exception", errors)
            require(peer.process.returncode is None, "peer native process exited before task completion")
        finally:
            if peer:
                try:
                    await peer.close()
                finally:
                    (directory / "peer-native.log").write_text(peer.log_text())
    await page.reload(timeout=10000)
    observed["after_peer_close"] = await page.evaluate(read)
    require(observed["after_peer_close"] == original,
            f"peer Profile cleanup changed the original data: {observed!r}")
    continued = {**original, "draft": {"query": "continue in profile A", "revision": 3}}
    await page.evaluate(write, continued)
    observed["continued"] = await page.evaluate(read)
    require(observed["continued"] == continued,
            f"original Profile could not continue after peer exit: {observed!r}")
    return {"goal": case["goal"], "observations": observed, "peer_receipt": peer.receipt,
            "scope": "two simultaneous standalone Profiles, same seed and origin; local/session storage and script-visible cookies only; no restart, managed domain or login"}


async def http_session(page, ring, case, directory):
    observed = {}
    async with fixture_server(authentication=True) as origin:
        await navigate(page, origin, "session/private")
        observed["anonymous"] = await page.evaluate("window.sessionView")
        require(observed["anonymous"] == {"authenticated": False}, "anonymous request received private content")
        await navigate(page, origin, "session/login")
        await page.mouse.click(40, 35)
        await page.keyboard.insert_text("local-user")
        await page.mouse.click(40, 75)
        await page.keyboard.insert_text("fixture-password")
        await page.mouse.click(40, 115)
        await poll_path(page, "session/private")
        await ready(page)
        expected = {"authenticated": True, "draft": "server-only draft 中文"}
        observed["signed_in"] = await page.evaluate("window.sessionView")
        observed["script_cookie"] = await page.evaluate("document.cookie")
        require(observed["signed_in"] == expected, f"login did not retrieve server-protected content: {observed!r}")
        require("mioa_session=" not in observed["script_cookie"], "HttpOnly session leaked to page script")
        await page.reload(timeout=10000)
        await ready(page)
        observed["after_reload"] = await page.evaluate("window.sessionView")
        require(observed["after_reload"] == expected, f"authenticated reload lost the session: {observed!r}")
        observed["visible_draft"] = await page.evaluate("document.getElementById('private-draft').innerText")
        require(observed["visible_draft"] == expected["draft"], "authenticated document did not display the protected draft")
        _, pixels = await capture(page, ring)
        (directory / "authenticated.png").write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
        await page.mouse.click(40, 115)
        await poll_path(page, "session/login")
        await ready(page)
        observed["signed_out"] = await page.evaluate("window.sessionView")
        require(observed["signed_out"] == {"authenticated": False}, "logout did not reach the anonymous document")
        await navigate(page, origin, "session/private")
        observed["after_logout"] = await page.evaluate("window.sessionView")
        require(observed["after_logout"] == {"authenticated": False}, "logout retained access to private content")
        _, pixels = await capture(page, ring)
        (directory / "signed-out.png").write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
    return {"goal": case["goal"], "observations": observed,
            "scope": "one standalone Profile; loopback HTTP form, Set-Cookie, HttpOnly, redirect, reload and logout; no TLS, live identity provider or restart"}


async def independent_noopener(page, ring, case, directory):
    context = page.context
    baseline = {item.target_id for item in context.pages}
    streams = {}
    for name in ("link", "noopener", "noreferrer"):
        streams["/independent/" + name] = {
            "prefix": (
                '<!doctype html><meta charset="utf-8"><title>Independent child</title>'
                '<style>body{margin:0;background:#fff}'
                '#child{position:absolute;left:20px;top:20px;width:80px;height:60px;'
                'background:#00ff00}</style><div id="child">CHILD</div>'
                '<script>window.childClicks=0;document.getElementById("child").onclick='
                '()=>{window.childClicks++};</script>'
            ).encode(),
            "tail": b'<p id="finished">independent destination</p><script>window.caseReady=true;</script>',
            "content_type": "text/html; charset=utf-8",
            **{key: asyncio.Event() for key in (
                "requested", "headers_release", "headers_flushed", "body_release",
                "prefix_flushed", "release", "closed")},
            "tail_sent": False,
        }
    observed = {}
    async with fixture_server(streams=streams) as origin:
        try:
            for name in ("link", "noopener", "noreferrer"):
                async def phase(label, awaitable):
                    try:
                        return await bounded(awaitable, 5)
                    except CaseFailure as exc:
                        raise CaseFailure(exc.status, f"{name}/{label}: {exc}") from exc
                source = await phase("launcher creation", context.new_page())
                source.web_test_errors = []
                await source._send("Emulation.setDeviceMetricsOverride", {
                    "width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False})
                await phase("launcher navigation", navigate(source, origin, "fixtures/native.html"))
                launcher_frame_id = source.frame_id
                pages_before = {item.target_id for item in context.pages}
                created = asyncio.Queue()
                destroyed = asyncio.Queue()
                async def on_page(item):
                    created.put_nowait(item)
                def on_target(message):
                    if message.get("method") == "Target.targetDestroyed":
                        destroyed.put_nowait(message["params"]["targetId"])
                context.on("page", on_page)
                context.cdp.listeners.append(on_target)
                stream = streams["/independent/" + name]
                try:
                    url = origin + "/independent/" + name
                    if name == "link":
                        await source.evaluate("""url => {
                            const link = document.createElement('a');
                            link.id = 'independent-link'; link.href = url;
                            link.target = '_blank'; link.textContent = 'Open child';
                            link.style.cssText = 'position:absolute;left:120px;top:100px;'
                                + 'width:160px;height:40px;display:block;background:#00f';
                            document.body.appendChild(link);
                            window.launcherRuns = 0;
                        }""", url)
                        source_frame, _ = await capture(source, ring)
                        await source.guarded_input("mouse.click", {"x": 150, "y": 120},
                                                   source_frame["render_fingerprint"])
                    else:
                        named = await phase("named popup setup", source.evaluate("""() => {
                            window.namedPopup = window.open('about:blank', 'native-task-name');
                            namedPopup.taskMarker = 'opener-bearing';
                            const reused = window.open('', 'native-task-name');
                            return {same:reused === namedPopup, opener:namedPopup.opener === window,
                                    marker:reused.taskMarker, closed:namedPopup.closed};
                        }"""))
                        require(named == {"same": True, "opener": True,
                                          "marker": "opener-bearing", "closed": False},
                                f"{name}: opener-bearing named reuse regressed: {named!r}")
                        require(source.frame_id == launcher_frame_id,
                                f"{name}: opener-bearing popup replaced launcher frame identity: "
                                f"{launcher_frame_id!r} -> {source.frame_id!r}")
                        # Only the script-created noopener window is under test.
                        # The opener-bearing popup need not be a CDP page target.
                        while not created.empty():
                            created.get_nowait()
                        opened = await phase("script activation", source.evaluate("""value => {
                            window.launcherRuns = 0;
                            return window.open(value.url, 'native-task-name', value.features) === null;
                        }""", {"url": url, "features": name}))
                        require(opened is True, f"{name}: window.open exposed a noopener WindowProxy")
                    await bounded(stream["requested"].wait(), 3)
                    child = await bounded(created.get(), 3)
                    child.web_test_errors = []
                    require(child.target_id not in pages_before and child in context.pages,
                            f"{name}: activation did not publish a separate usable page")
                    require({item.target_id for item in context.pages} == pages_before | {child.target_id},
                            f"{name}: activation created unexpected independent targets")
                    pending = await child.evaluate("""() => ({
                        opener:window.opener === null, ready:document.readyState,
                        marker:window.preResponseMarker = 'usable-before-headers'
                    })""")
                    require(pending["opener"] is True and pending["marker"] == "usable-before-headers"
                            and not stream["headers_release"].is_set()
                            and not stream["headers_flushed"].is_set(),
                            f"{name}: new target was not executable before response headers: {pending!r}")
                    source_state = await phase("launcher still usable", source.evaluate("""() => ({
                        runs:++window.launcherRuns,
                        named:window.namedPopup ? {
                            marker:namedPopup.taskMarker, opener:namedPopup.opener === window,
                            same:window.open('', 'native-task-name') === namedPopup,
                            closed:namedPopup.closed
                        } : null
                    })"""))
                    require(source_state["runs"] == 1, f"{name}: launcher stopped executing during child request")
                    if name != "link":
                        require(source_state["named"] == {
                            "marker": "opener-bearing", "opener": True, "same": True, "closed": False},
                            f"{name}: noopener reused or replaced the opener-bearing named popup: {source_state!r}")
                        await phase("named popup close", source.evaluate("namedPopup.close()"))
                    await phase("launcher close", source.close())
                    require(source not in context.pages and child in context.pages
                            and await child.evaluate("window.preResponseMarker") == "usable-before-headers",
                            f"{name}: closing launcher destroyed or replaced its independent child")
                    stream["headers_release"].set()
                    await bounded(stream["headers_flushed"].wait(), 3)
                    require(not stream["body_release"].is_set() and not stream["prefix_flushed"].is_set(),
                            f"{name}: fixture body escaped its response gate")
                    stream["body_release"].set()
                    await bounded(stream["prefix_flushed"].wait(), 3)
                    stream["release"].set()
                    await bounded(stream["closed"].wait(), 3)
                    await phase("destination load", child.wait_for_load_state("load", timeout=5000))
                    await phase("destination ready", ready(child))
                    complete = await child.evaluate("""() => ({
                        opener:window.opener === null,
                        text:document.getElementById('finished').textContent,
                        clicks:window.childClicks, path:location.pathname
                    })""")
                    require(complete == {"opener": True, "text": "independent destination",
                                         "clicks": 0, "path": "/independent/" + name}
                            and stream["tail_sent"], f"{name}: destination content differs: {complete!r}")
                    await child._send("Emulation.setDeviceMetricsOverride", {
                        "width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False})
                    metadata, pixels = await capture(child, ring)
                    probes(pixels, [[25, 60, "00ff00ff"], [125, 60, "ffffffff"]])
                    (directory / (name + "-child.png")).write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
                    await phase("native click", child.guarded_input(
                        "mouse.click", {"x": 25, "y": 60}, metadata["render_fingerprint"]))
                    require(await phase("native click receipt", child.evaluate("window.childClicks")) == 1,
                            f"{name}: independent target did not receive native input")
                    await phase("replacement navigation", navigate(child, origin, "fixtures/native.html"))
                    await phase("replacement reload", child.reload(timeout=5000))
                    await phase("replacement ready", ready(child))
                    replacement, pixels = await capture(child, ring)
                    probes(pixels, [[25, 25, "ff0000ff"]])
                    require(await child.evaluate("window.opener === null") is True,
                            f"{name}: navigation/reload restored an opener")
                    # Document script, not driver Target.closeTarget, proves
                    # script closability after a root-document replacement.
                    close_script = ("window.close(); location.href='about:blank'"
                                    if name == "noreferrer" else "window.close()")
                    await phase("document close", child.evaluate(close_script))
                    async def child_destroyed():
                        while await destroyed.get() != child.target_id:
                            pass
                    await phase("target destruction", child_destroyed())
                    require(child.is_closed() and child not in context.pages,
                            f"{name}: window.close did not retire its CDP page")
                    require({item.target_id for item in context.pages} == baseline
                            and await page.evaluate("1 + 1") == 2,
                            f"{name}: closing child affected another independent target")
                    targets = await context.cdp.send("Target.getTargets")
                    require({item["targetId"] for item in targets["targetInfos"]
                             if item["type"] == "page"} == baseline,
                            f"{name}: script close left an orphan registered page target")
                    observed[name] = {
                        "target_id": child.target_id, "before_headers": pending,
                        "launcher": source_state, "launcher_closed_before_headers": True,
                        "destination": complete, "frame": metadata, "native_clicks": 1,
                        "replacement_frame": replacement, "script_closed_after_reload": True,
                        "remaining_targets": sorted(baseline),
                    }
                finally:
                    context.remove_listener("page", on_page)
                    context.cdp.listeners.remove(on_target)
        finally:
            # On failure the enclosing native session owns process-wide cleanup;
            # do not obscure the original assertion with more page commands.
            if sys.exception() is None:
                extras = tuple(extra for extra in context.pages if extra.target_id not in baseline)
                await asyncio.gather(*(bounded(extra.close(), 2) for extra in extras))
    return {"goal": case["goal"], "observations": observed,
            "scope": "real native link activation and document window.open; event-gated HTTP headers/body; CDP page adoption/evaluation/capture/input/destruction; independent launcher and child lifecycles",
            "limitations": case["limitations"]}


async def progressive_html(page, ring, case, directory):
    streams = {}
    for name in ("utf8-split", "meta-legacy", "bom-precedence", "utf16-bom", "mime-sniff", "stopped", "replaced", "intercepted", "override"):
        legacy = name in ("meta-legacy", "override")
        text = "café €" if legacy else "café 漢😀"
        encoding = "windows-1252" if legacy else "utf-16le" if name == "utf16-bom" else "utf-8"
        prefix = (
            '<!doctype html><html><head><meta charset="' + encoding + '">'
            '<title>Progressive HTML</title><style>'
            'body{margin:0;background:#fff}'
            '#prefix{position:absolute;left:20px;top:20px;width:80px;height:60px;background:#00ff00}'
            '#tail{position:absolute;left:120px;top:20px;width:80px;height:60px;background:#0000ff}'
            '#prefix-text{position:absolute;top:110px}</style></head><body>'
            '<div id="prefix">PREFIX</div><p id="prefix-text">' + text + '</p>'
            '<script>window.streamPrefixRuns=(window.streamPrefixRuns||0)+1;'
            'window.streamCounts=window.streamCounts||{tail:0,dcl:0,load:0};'
            'document.addEventListener("DOMContentLoaded",()=>{streamCounts.dcl++});'
            'window.addEventListener("load",()=>{streamCounts.load++});'
            "document.write('<span id=\"written\">WRITE</span>');</script>"
        ).encode(encoding)
        if name == "utf16-bom":
            # BOM overrides the UTF-8 HTTP charset. Even the ASCII markup is
            # UTF-16LE; split the final e-acute code unit across the EOF gate.
            prefix = b"\xff\xfe" + prefix + '<p id="encoded">caf'.encode(encoding) + b"\xe9"
            tail = b"\x00" + ' 漢😀</p>'.encode(encoding)
        elif legacy:
            # Split a start tag in addition to exercising meta-selected legacy
            # decoding; no completed #encoded element exists before release.
            prefix += b'<p id="enco'
            tail = ('ded">' + text + '</p>').encode(encoding)
        elif name == "bom-precedence":
            # BOM wins over the conflicting HTTP charset, while an entity
            # remains pending in the same tokenizer across the EOF gate.
            prefix = b"\xef\xbb\xbf" + prefix + b'<p id="encoded">caf&#xE'
            tail = '9; 漢😀</p>'.encode(encoding)
        else:
            # The event gate forces this UTF-8 sequence across separate socket
            # deliveries, not merely two writes the kernel could coalesce.
            prefix += b'<p id="encoded">caf\xc3'
            tail = b'\xa9' + ' 漢😀</p>'.encode(encoding)
        tail += ('<div id="tail">TAIL</div><script>streamCounts.tail++;'
                 'window.caseReady=true;</script></body></html>').encode(encoding)
        streams["/stream/" + name] = {
            "prefix": prefix, "tail": tail, "text": text,
            "content_type": None if name == "mime-sniff"
                            else "text/html; charset=windows-1252" if name in ("bom-precedence", "override")
                            else "text/html" if legacy else "text/html; charset=utf-8",
            **{key: asyncio.Event() for key in
               ("headers_flushed", "body_release", "prefix_flushed", "release", "closed")},
            "tail_sent": False,
        }
    streams["/stream/xml"] = {
        "prefix": b'<?xml version="1.0" encoding="windows-',
        "tail": '1252"?><root>café €</root>'.encode("windows-1252"),
        "content_type": "application/xml",
        **{key: asyncio.Event() for key in
           ("headers_flushed", "body_release", "prefix_flushed", "release", "closed")},
        "tail_sent": False,
    }
    snapshot = """() => {
        const prefix = document.getElementById('prefix-text');
        const encoded = document.getElementById('encoded');
        return {ready:document.readyState, prefix:prefix ? prefix.textContent : null,
                encoded:encoded ? encoded.textContent : null,
                tail:!!document.getElementById('tail'), runs:window.streamPrefixRuns || 0,
                written:document.querySelectorAll('#written').length,
                counts:window.streamCounts || null};
    }"""
    observed = {}
    async with fixture_server(streams=streams) as origin:
        async def open_prefix(name):
            stream = streams["/stream/" + name]
            await page.goto(origin + "/stream/" + name, wait_until="commit", timeout=5000)
            await bounded(stream["headers_flushed"].wait(), 2)
            require(not stream["prefix_flushed"].is_set() and not stream["closed"].is_set(),
                    "commit waited for response body rather than headers")
            stream["body_release"].set()
            await bounded(stream["prefix_flushed"].wait(), 2)
            async def prefix_visible():
                while True:
                    value = await page.evaluate(snapshot)
                    if value["prefix"] == stream["text"] and value["runs"]:
                        return value
            value = await bounded(prefix_visible(), 5)
            require(value["ready"] == "loading" and value["runs"] == 1 and value["written"] == 1
                    and value["counts"] == {"tail": 0, "dcl": 0, "load": 0}
                    and value["tail"] is False,
                    f"{name}: prefix was replayed or document completed before EOF: {value!r}")
            metadata, pixels = await capture(page, ring)
            (directory / (name + "-loading.png")).write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
            probes(pixels, [[25, 60, "00ff00ff"], [125, 60, "ffffffff"]])
            after_capture = await page.evaluate(snapshot)
            require(after_capture["ready"] == "loading" and after_capture["runs"] == 1 and after_capture["written"] == 1
                    and after_capture["counts"] == {"tail": 0, "dcl": 0, "load": 0}
                    and not stream["release"].is_set() and not stream["closed"].is_set(),
                    f"{name}: capture did not precede EOF: {after_capture!r}")
            observed[name] = {"loading": after_capture, "loading_frame": metadata}
            return stream

        for name in ("utf8-split", "meta-legacy", "bom-precedence", "utf16-bom", "mime-sniff"):
            stream = await open_prefix(name)
            # EOF can only become available after both DOM and native pixels
            # have proved consumption of this response's complete HTML prefix.
            stream["release"].set()
            await bounded(stream["closed"].wait(), 2)
            await page.wait_for_load_state("load", timeout=5000)
            value = await page.evaluate(snapshot)
            require(value == {"ready": "complete", "prefix": stream["text"], "encoded": stream["text"],
                              "tail": True, "runs": 1, "written": 1, "counts": {"tail": 1, "dcl": 1, "load": 1}},
                    f"{name}: EOF lost decoded text or replayed scripts/lifecycle events: {value!r}")
            require(stream["tail_sent"], f"{name}: fixture did not finish its real socket response")
            metadata, pixels = await capture(page, ring)
            (directory / (name + "-complete.png")).write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
            probes(pixels, [[25, 60, "00ff00ff"], [125, 60, "0000ffff"]])
            observed[name].update(complete=value, complete_frame=metadata)

        # Non-HTML responses still buffer through EOF. In particular, an XML
        # declaration split across deliveries must select the full-body decoder.
        stream = streams["/stream/xml"]
        await page.goto(origin + "/stream/xml", wait_until="commit", timeout=5000)
        await bounded(stream["headers_flushed"].wait(), 2)
        require(not stream["prefix_flushed"].is_set() and not stream["closed"].is_set(),
                "XML commit waited for its buffered response body")
        stream["body_release"].set()
        await bounded(stream["prefix_flushed"].wait(), 2)
        require(not stream["closed"].is_set() and not stream["tail_sent"],
                "XML fixture reached EOF before its declaration boundary was gated")
        stream["release"].set()
        await bounded(stream["closed"].wait(), 2)
        await page.wait_for_load_state("load", timeout=5000)
        xml = await page.evaluate("""() => ({
            type:document.contentType, ready:document.readyState,
            root:document.documentElement.tagName, text:document.documentElement.textContent
        })""")
        require(xml == {"type": "application/xml", "ready": "complete", "root": "root", "text": "café €"},
                f"buffered XML lost its split declaration or legacy text: {xml!r}")
        require(stream["tail_sent"], "XML fixture did not finish its socket response")
        observed["xml"] = xml

        stream = await open_prefix("stopped")
        await page.evaluate("window.stop()")
        stopped = await page.evaluate(snapshot)
        stream["release"].set()
        await bounded(stream["closed"].wait(), 2)
        await page.evaluate("async () => { await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); return true; }")
        value = await page.evaluate(snapshot)
        require(value["prefix"] == stream["text"] and value["runs"] == 1 and value["written"] == 1
                and value["tail"] is False and value["counts"]["tail"] == 0,
                f"window.stop lost the prefix or allowed late response content/script: {value!r}")
        metadata, pixels = await capture(page, ring)
        (directory / "stopped-after-old-eof.png").write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
        probes(pixels, [[25, 60, "00ff00ff"], [125, 60, "ffffffff"]])
        observed["stopped"].update(stopped=stopped, after_old_eof=value, stopped_frame=metadata,
                                  old_tail_sent=stream["tail_sent"])

        stream = await open_prefix("replaced")
        await navigate(page, origin, "fixtures/native.html")
        metadata, pixels = await capture(page, ring)
        (directory / "replacement-before-old-eof.png").write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
        probes(pixels, [[25, 25, "ff0000ff"]])
        require(await page.evaluate("document.getElementById('prefix') === null && window.streamCounts === undefined"),
                "replacement retained the old streaming document")
        stream["release"].set()
        await bounded(stream["closed"].wait(), 2)
        await page.evaluate("async () => { await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); return true; }")
        metadata, pixels = await capture(page, ring)
        (directory / "replacement-after-old-eof.png").write_bytes(png_bytes(WIDTH, HEIGHT, pixels))
        probes(pixels, [[25, 25, "ff0000ff"], [125, 60, "ffffffff"]])
        replacement = await page.evaluate("""() => ({
            ready:document.readyState, path:location.pathname,
            oldContent:!!document.getElementById('prefix') || !!document.getElementById('tail'),
            oldScript:window.streamCounts !== undefined, clicks:window.clicks
        })""")
        require(replacement == {"ready": "complete", "path": "/fixtures/native.html",
                                "oldContent": False, "oldScript": False, "clicks": 0},
                f"late old response changed the replacement document: {replacement!r}")
        observed["replaced"].update(replacement=replacement, replacement_frame=metadata,
                                    old_tail_sent=stream["tail_sent"])

        # A response-stage interceptor must see the complete original body
        # before navigation exposes any of its DOM or executes its scripts.
        for name in ("intercepted", "override"):
            stream = streams["/stream/" + name]
            url = origin + "/stream/" + name
            before = await page.evaluate(snapshot)
            paused = asyncio.Queue()
            def on_pause(message):
                if message.get("method") == "Fetch.requestPaused" and message.get("sessionId") == page.session_id:
                    paused.put_nowait(message["params"])
            page.context.cdp.listeners.append(on_pause)
            navigation = None
            try:
                stage = "Request" if name == "override" else "Response"
                await page._send("Fetch.enable", {"patterns": [{"urlPattern": url, "requestStage": stage}]})
                navigation = asyncio.create_task(page.goto(url, wait_until="commit", timeout=5000))
                if stage == "Request":
                    request = await bounded(paused.get(), 3)
                    require("responseStatusCode" not in request, "request-stage pause arrived after response")
                    await page._send("Fetch.continueRequest", {"requestId": request["requestId"], "interceptResponse": True})
                await bounded(stream["headers_flushed"].wait(), 3)
                stream["body_release"].set()
                await bounded(stream["prefix_flushed"].wait(), 3)
                require(not navigation.done() and await page.evaluate(snapshot) == before,
                        f"{name}: unapproved streaming prefix reached the document")
                stream["release"].set()
                await bounded(stream["closed"].wait(), 3)
                response = await bounded(paused.get(), 3)
                require(response.get("responseStatusCode") == 200 and not navigation.done(),
                        f"{name}: response was not held before commit: {response!r}")
                body = await page._send("Fetch.getResponseBody", {"requestId": response["requestId"]})
                data = base64.b64decode(body["body"]) if body.get("base64Encoded") else body["body"].encode()
                require(data == stream["prefix"] + stream["tail"], f"{name}: interceptor lost the original body")
                require(await page.evaluate(snapshot) == before, f"{name}: paused response executed scripts")
                await page._send("Fetch.continueResponse", {"requestId": response["requestId"]})
                await bounded(navigation, 5)
                await page.wait_for_load_state("load", timeout=5000)
                value = await page.evaluate(snapshot)
                require(value == {"ready": "complete", "prefix": stream["text"], "encoded": stream["text"],
                                  "tail": True, "runs": 1, "written": 1, "counts": {"tail": 1, "dcl": 1, "load": 1}},
                        f"{name}: releasing response replayed or lost document work: {value!r}")
                observed[name] = {"complete": value, "intercepted_bytes": len(data)}
            finally:
                if navigation is not None and not navigation.done():
                    navigation.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await navigation
                page.context.cdp.listeners.remove(on_pause)
                await page._send("Fetch.disable")
    return {"goal": case["goal"], "observations": observed,
            "scope": "real loopback sockets and source-engine DOM/native frames; event-gated headers/prefix/EOF; UTF-8/UTF-16/tag/entity splits, legacy meta, BOM precedence, headerless MIME sniff, document.write, stop/replacement, buffered legacy XML and intercepted legacy HTML"}


async def exercise(page, ring, origin, case, directory):
    await page._send("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": case.get("dpr", 1), "mobile": False})
    if case.get("progressive_html"):
        return await progressive_html(page, ring, case, directory)
    if case.get("independent_noopener"):
        return await independent_noopener(page, ring, case, directory)
    if case.get("http_session"):
        return await http_session(page, ring, case, directory)
    if case["kind"] == "wpt":
        await page.goto(origin + "/" + case["test"], timeout=10000)
        while True:
            value = await page.evaluate("window.__mioa_wpt || null")
            if value is not None:
                return validate_wpt(value)
            await asyncio.sleep(0.01)
    await navigate(page, origin, case["test"])
    if case["kind"] == "assertion":
        if case.get("history_form"):
            return await history_form(page, origin, case)
        if case.get("input"):
            await page.mouse.click(40, 35)
            await page.keyboard.insert_text(case.get("input_text", "Core"))
        if "destination" in case:
            if case.get("submit_key"):
                await page.keyboard.press(case["submit_key"])
            else:
                await page.mouse.click(40, 80)
            await poll_path(page, case["destination"])
            await ready(page)
            if case.get("back"):
                await page.go_back()
                await ready(page)
        helper = (HERE / "web" / "assert.js").read_text()
        source = (HERE / "web" / case["script"]).read_text()
        value = await page.evaluate("async () => {" + helper + "\nawait (" + source + ")(); return {complete:true,subtests:rows}; }")
        return validate_assertions(value, case["assertions"])
    if case["kind"] == "native":
        if case.get("storage_draft"):
            return await storage_draft(page, origin, case)
        if case.get("storage_tabs"):
            return await storage_tabs(page, origin, case)
        if case.get("storage_profiles"):
            return await storage_profiles(page, origin, case, directory)
        if case.get("scaled_overflow"):
            return await scaled_overflow(page, ring, directory, case["dpr"])
        if case.get("scaled_canvas"):
            return await scaled_canvas(page, ring, directory)
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
    from mioa_os import LocalArtifactContent, LocalArtifactDiscovery, LocalChildProcesses, LocalHostFiles, LocalProcessEnvironment, LocalProcessIdentity, LocalProfileLeases, LocalSecureRandom, LocalStreamConnections
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
                           "avail_height": 320, "device_scale_factor": case.get("dpr", 1)},
                network={"allow_private_network": True}, artifact_discovery=LocalArtifactDiscovery(),
                artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
            profile.save_config(name, cfg)
            result["runtime"] = cfg["runtime"]
            result["inputs_sha256"] = {case[key]: sha((HERE / "web" / case[key]).read_bytes())
                                       for key in ("test", "reference", "script", "destination") if key in case}
            with profile.exclusive(name, leases=LocalProfileLeases()) as owned:
                require(owned, "isolated native engine lease was unavailable")
                try:
                    session = await bounded(launch_native(name, cfg, True,
                        child_processes=LocalChildProcesses(),
                        process_environment=LocalProcessEnvironment(), identity=LocalProcessIdentity(),
                        profile_lease=owned, random=LocalSecureRandom(),
                        artifact_content=LocalArtifactContent(),
                        connections=LocalStreamConnections(), files=LocalHostFiles()), 40)
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
    return result


def source_info(binary, cases):
    from mioa_core.native_spec import file_sha256
    from mioa_os import LocalArtifactContent
    sources = {}
    for root in (HERE, CORE / "mioa-core" / "mioa_core", CORE / "mioa-headless" / "mioa_headless"):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".js", ".json", ".html", ".css", ".c"} and "__pycache__" not in path.parts:
                sources[path.relative_to(CORE).as_posix()] = sha(path.read_bytes())
    scales = {case.get("dpr", 1) for case in cases}
    return {"binary": str(binary), "binary_sha256": file_sha256(binary, artifact_content=LocalArtifactContent()), "sources_sha256": sources,
            "viewport": {"width": WIDTH, "height": HEIGHT,
                         "dpr": next(iter(scales)) if len(scales) == 1 else None,
                         "case_dpr": {case["id"]: case.get("dpr", 1) for case in cases}},
            "python": sys.version}


async def run(args, cases, output):
    report = {"schema": 1, "suite": args.suite, "results": [], "status": "ERROR"}
    try:
        binary = Path(args.binary).expanduser().resolve()
        require(binary.is_file() and os.access(binary, os.X_OK), f"native binary is absent or not executable: {binary}")
        report["source"] = source_info(binary, cases)
        async with fixture_server() as origin:
            for case in cases:
                result = await run_case(case, binary, origin, output)
                report["results"].append(result)
                if args.format == "text" and result["status"] != "PASS":
                    print(f'{result["status"]} {result["id"]} {result["duration_ms"]}ms', flush=True)
        report["status"] = "PASS" if all(row["status"] == "PASS" for row in report["results"]) else "FAIL"
    except Exception as exc:
        report["error"] = failure_record(exc)
    readable = output / "results.toon"
    readable.write_text(evidence_view.render(report))
    report["readable_report"] = {"path": readable.name, "sha256": sha(readable.read_bytes())}
    json_write(output / "results.json", report)
    report_path = output / "results.json"
    summary = {"status": report["status"], "cases": len(cases), "report": str(readable)}
    if args.format == "json":
        summary["data"] = str(report_path)
        print(json.dumps(summary))
    else:
        print(toon.encode(summary))
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", help="explicit real native engine executable (required except --list)")
    parser.add_argument("--output", type=Path, help="new, empty artifact directory; defaults outside source under the system temp directory")
    parser.add_argument("--filter", action="append", default=[], help="case-id substring (repeatable, OR)")
    parser.add_argument("--kind", choices=("assertion", "reftest", "native", "wpt"))
    parser.add_argument("--suite", choices=("web", "controls"), default="web")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--format", choices=("toon", "json"), default="toon")
    args = parser.parse_args(argv)
    cases = [case for case in load_suite(args.suite) if (not args.filter or any(term in case["id"] for term in args.filter))
             and (not args.kind or case["kind"] == args.kind)]
    if not cases:
        parser.error("selection matched no cases (not a successful empty run)")
    if args.list:
        print(json.dumps(cases, indent=2) if args.format == "json" else toon.encode(cases))
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
    return asyncio.run(run(args, cases, output))


if __name__ == "__main__":
    raise SystemExit(main())
