#!/usr/bin/env python3
"""Run real native Canvas PNG exports and decode them independently with Python zlib."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import zlib

from run_web import CORE, HERE, bounded, fixture_server, navigate, validate_assertions
import toon

ASSERTIONS = [
    "Canvas PNG exports preserve exact straight RGBA rows",
    "Canvas PNG resize and empty exports follow bitmap state",
    "OffscreenCanvas PNG exports preserve pixels, resize and empty behavior",
    "WebGL Canvas PNG exports preserve actual GPU colors",
    "Canvas PNG compression is effective across long images",
]
RGBA = bytes((255, 0, 17, 255, 32, 200, 64, 128, 123, 45, 67, 0,
              1, 2, 3, 1, 255, 254, 253, 254, 9, 8, 7, 64))


def entropy_pixels():
    state = 0x504e47
    result = bytearray()
    for _ in range(97 * 41 * 4):
        state ^= (state << 13) & 0xffffffff
        state ^= state >> 17
        state ^= (state << 5) & 0xffffffff
        result.append(state >> 24)
    return bytes(result)
EXPECTED = {
    "no-context blank": (3, 2, bytes(24)),
    "no-context blank Blob": (3, 2, bytes(24)),
    "toDataURL RGBA": (3, 2, RGBA),
    "toBlob RGBA": (3, 2, RGBA),
    "toBlob snapshot before resize": (3, 2, RGBA),
    "resized data URL cleared": (2, 3, bytes(24)),
    "resized Blob cleared": (2, 3, bytes(24)),
    "OffscreenCanvas RGBA": (3, 2, RGBA),
    "OffscreenCanvas resized cleared": (2, 3, bytes(24)),
    "WebGL Canvas RGBA": (2, 2, bytes((255, 0, 0, 255)) * 4),
    "OffscreenCanvas WebGL RGBA": (2, 2, bytes((255, 0, 0, 255)) * 4),
    "multi-window compressed RGBA": (257, 67, bytes(i % 251 for i in range(257 * 67 * 4))),
    "high-entropy RGBA": (97, 41, entropy_pixels()),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decode_png(data: bytes, width: int, height: int) -> bytes:
    require(data[:8] == b"\x89PNG\r\n\x1a\n", "invalid PNG signature")
    offset, headers, ends = 8, 0, 0
    compressed = bytearray()
    idat_ended = False
    while offset < len(data):
        require(offset + 12 <= len(data), "truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        require(length <= 0x7fffffff and offset + length + 12 <= len(data), "invalid PNG chunk length")
        kind = data[offset + 4:offset + 8]
        require(all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind),
                "invalid PNG chunk type alphabet")
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        require(zlib.crc32(kind + payload) == crc, f"{kind!r} CRC mismatch")
        if kind == b"IHDR":
            require(offset == 8 and headers == 0, "IHDR order/duplication")
            require(payload == struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0), "RGBA8 geometry/format mismatch")
            headers += 1
        elif kind == b"IDAT":
            require(headers == 1 and not idat_ended, "nonconsecutive or early IDAT")
            compressed.extend(payload)
        elif kind == b"IEND":
            require(length == 0 and offset + 12 == len(data), "IEND/trailing bytes mismatch")
            ends += 1
        else:
            require(kind[0] & 32, f"unknown critical chunk {kind!r}")
            if compressed:
                idat_ended = True
        offset += length + 12
    require(headers == 1 and ends == 1 and compressed, "missing required PNG chunks")
    stride = width * 4
    expected_length = (stride + 1) * height
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected_length + 1)
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
            "invalid/trailing zlib stream or oversized output")
    require(len(raw) == expected_length, "incorrect decoded scanline length")
    pixels = bytearray(width * height * 4)
    for y in range(height):
        filter_type = raw[y * (stride + 1)]
        require(filter_type <= 4, "unknown PNG scanline filter")
        for x in range(stride):
            index = y * stride + x
            left = pixels[index - 4] if x >= 4 else 0
            above = pixels[index - stride] if y else 0
            upper_left = pixels[index - stride - 4] if y and x >= 4 else 0
            p = left + above - upper_left
            distances = (abs(p - left), abs(p - above), abs(p - upper_left))
            paeth = (left, above, upper_left)[distances.index(min(distances))]
            prediction = (0, left, above, (left + above) // 2, paeth)[filter_type]
            pixels[index] = (raw[y * (stride + 1) + 1 + x] + prediction) & 255
    return bytes(pixels)


async def run(binary: Path, output: Path):
    from mioa_core import profile
    from mioa_core.native import launch_native
    from mioa_core.native_spec import create_native_spec
    from mioa_os import LocalArtifactContent, LocalArtifactDiscovery, LocalChildProcesses, LocalHostFiles, LocalProcessEnvironment, LocalProcessIdentity, LocalProfileLeases, LocalSecureRandom, LocalStreamConnections

    helper = (HERE / "web/assert.js").read_text()
    script = (HERE / "web/scripts/png-roundtrip.js").read_text()
    old_base, session = profile.BASE, None
    sources = [Path(__file__).resolve(), HERE / "web/assert.js",
               HERE / "web/scripts/png-roundtrip.js", HERE / "web/fixtures/png-roundtrip.html"]
    report = {"status": "ERROR", "binary": str(binary),
              "inputs_sha256": {str(path.relative_to(CORE)): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in sources}}
    try:
        with tempfile.TemporaryDirectory(prefix="mioa-png-profile-") as temporary:
            profile.BASE = Path(temporary)
            name = "png-roundtrip"
            cfg = create_native_spec(name, runtime="mioa", binary=binary,
                storage_path=profile.profile_dir(name) / "data", seed="01" * 32,
                network={"allow_private_network": True}, artifact_discovery=LocalArtifactDiscovery(),
                artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
            profile.save_config(name, cfg)
            report["runtime"] = cfg["runtime"]
            with profile.exclusive(name, leases=LocalProfileLeases()) as owned:
                require(owned, "isolated native engine lease unavailable")
                try:
                    session = await bounded(launch_native(name, cfg, True,
                        child_processes=LocalChildProcesses(),
                        process_environment=LocalProcessEnvironment(), identity=LocalProcessIdentity(),
                        profile_lease=owned, random=LocalSecureRandom(),
                        artifact_content=LocalArtifactContent(),
                        connections=LocalStreamConnections(), files=LocalHostFiles()), 40)
                    report["receipt"] = session.receipt
                    page = session.context.pages[0] if session.context.pages else await bounded(session.context.new_page(), 5)
                    errors = []
                    def script_event(event):
                        if event.get("method") == "Runtime.exceptionThrown" and event.get("sessionId") == page.session_id:
                            errors.append(event.get("params", {}).get("exceptionDetails", {}))
                    session.cdp.listeners.append(script_event)
                    page.web_test_errors = errors
                    async with fixture_server() as origin:
                        await bounded(navigate(page, origin, "fixtures/png-roundtrip.html"), 15)
                        result = await bounded(page.evaluate("async () => {" + helper + "\nawait (" + script +
                            ")(); return {complete:true,subtests:rows,exports:window.pngRoundtripExports}; }"), 30)
                    report["assertions"] = validate_assertions(result, ASSERTIONS)
                    require(not errors, f"uncaught native JavaScript errors: {errors!r}")
                    records = result["exports"]
                    require([record["name"] for record in records] == list(EXPECTED), "PNG export names/order mismatch")
                    decoded = []
                    for index, record in enumerate(records):
                        width, height, expected = EXPECTED[record["name"]]
                        require((record["width"], record["height"]) == (width, height), "fixture geometry mismatch")
                        data = base64.b64decode(record["base64"], validate=True)
                        pixels = decode_png(data, width, height)
                        require(pixels == expected, f"independent exact RGBA check failed: {record['name']}")
                        if record["name"] == "multi-window compressed RGBA":
                            require(len(data) < len(expected) // 4, "PNG did not effectively compress")
                        (output / f"{index:02d}.png").write_bytes(data)
                        decoded.append({"name": record["name"], "width": width, "height": height,
                                        "png_bytes": len(data), "rgba_bytes": len(pixels)})
                    require(session.process.returncode is None, "native process exited before completion")
                    report["decoded"] = decoded
                    report["status"] = "PASS"
                finally:
                    if session is not None:
                        try:
                            await session.close()
                        finally:
                            (output / "native.log").write_text(session.log_text())
    except Exception as error:
        report["status"] = "FAIL"
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        profile.BASE = old_base
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / "report.toon").write_text(toon.encode(report) + "\n")
    print(toon.encode({"status": report["status"], "output": str(output), "error": report.get("error")}))
    return 0 if report["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    binary = args.binary.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error("--binary must be an executable real native engine")
    output = args.output.expanduser().resolve() if args.output else Path(tempfile.mkdtemp(prefix="mioa-png-roundtrip-"))
    if output.is_relative_to(CORE) and not output.is_relative_to(CORE / ".local"):
        parser.error("--output must be outside the source tree or under .local")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(binary, output))


if __name__ == "__main__":
    raise SystemExit(main())
