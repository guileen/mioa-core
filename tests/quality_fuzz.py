#!/usr/bin/env python3
"""Bounded PNG chunk-alphabet differential fuzzing; no conformance claim.

Fuzz: --decoder ABS --output DIR [--seed N --iterations N --oracle-source PATH].
Replay: replay --record FILE --decoder ABS [--oracle-source PATH].
Promote: promote --record FILE --decoder ABS --output NEW_DIR
         --confirm-spec SPEC_URL --confirm-oracle-sha256 SHA256.
Promotion never edits source fixtures or expectations. Review/copy the new fixture
explicitly. --oracle-source selects trusted local Python code, never record code.
`target(data, oracle, decoder)` is reusable by an optional Atheris/AFL adapter;
only stdlib and an actual libpng decoder are needed here. No fuzzer is claimed run.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import zlib

import toon

HERE = Path(__file__).resolve().parent
SPEC = "https://www.w3.org/TR/2025/REC-png-3-20250624/#5Chunk-layout"
REQUIREMENT = "PNG-CHUNK-TYPE-ALPHABET"
SIGNATURE = REQUIREMENT + ":oracle-accepts-exact-rgba/libpng-invalid-chunk-type"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PIXELS = bytes((17, 83, 149, 211))
MAX_BYTES = 65536
MAX_EVALUATIONS = 256


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def png(case):
    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + b"".join(chunk(bytes.fromhex(c["kind"]), bytes.fromhex(c["payload"])) for c in case)
            + chunk(b"IDAT", zlib.compress(b"\0" + PIXELS)) + chunk(b"IEND", b""))


def validate_case(case):
    if not isinstance(case, list) or len(case) > 16:
        raise ValueError("case must contain at most 16 chunks")
    for item in case:
        if not isinstance(item, dict) or set(item) != {"kind", "payload"}:
            raise ValueError("invalid chunk record")
        if not all(isinstance(item[k], str) for k in item):
            raise ValueError("chunk fields must be hex strings")
        kind, payload = bytes.fromhex(item["kind"]), bytes.fromhex(item["payload"])
        if len(kind) != 4 or len(payload) > 256:
            raise ValueError("chunk bounds exceeded")
    return case


def load_oracle(source):
    """Extract only the two trusted oracle functions, without UI imports/startup."""
    tree = ast.parse(source.read_bytes(), filename=str(source))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"require", "decode_png"}]
    if {node.name for node in functions} != {"require", "decode_png"}:
        raise ValueError("selected oracle lacks require/decode_png")
    namespace = {"struct": struct, "zlib": zlib}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["decode_png"]


def malformed_types(data):
    """Recognize only the bounded, CRC-valid chunk-type alphabet contract."""
    if len(data) > MAX_BYTES or not data.startswith(PNG_SIGNATURE):
        return False
    offset, bad = 8, False
    while offset < len(data):
        if offset + 12 > len(data):
            return False
        length = struct.unpack_from(">I", data, offset)[0]
        if offset + 12 + length > len(data):
            return False
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) != crc:
            return False
        bad |= not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind)
        offset += 12 + length
    return bad


class Decoder:
    def __init__(self, executable):
        self.executable = Path(executable).resolve()
        if not self.executable.is_file():
            raise FileNotFoundError(self.executable)

    def __call__(self, data):
        with tempfile.TemporaryDirectory(prefix="quality-fuzz-") as directory:
            source, output = Path(directory) / "input.png", Path(directory) / "output.rgba"
            source.write_bytes(data)
            try:
                process = subprocess.run([str(self.executable), str(source), str(output)],
                                         capture_output=True, timeout=5, check=False)
            except (subprocess.TimeoutExpired, OSError) as error:
                return {"status": "INVALID", "detail": str(error)}
            stderr = process.stderr.decode("utf-8", errors="replace")[:4096]
            result = {"returncode": process.returncode, "stderr": stderr}
            if process.returncode < 0:
                return dict(result, status="INVALID", detail="decoder terminated by signal")
            if process.returncode:
                return dict(result, status="REJECT" if process.returncode == 3 else "INVALID")
            try:
                metadata = json.loads(process.stdout)
                exact = metadata == {"width": 1, "height": 1, "format": "RGBA8"} and output.read_bytes() == PIXELS
            except (ValueError, OSError):
                exact = False
            return dict(result, status="ACCEPT" if exact else "INVALID")


def target(data, oracle, decoder):
    """One bounded input, returning only a stable semantic failure signature.

    Adapters may raise on result['signature']; decoder process errors must instead
    be reported as infrastructure failures, never as a discovered oracle bug.
    """
    if len(data) > MAX_BYTES:
        return {"signature": None, "status": "OUT_OF_SCOPE"}
    try:
        pixels = oracle(data, 1, 1)
        local = "ACCEPT" if pixels == PIXELS else "WRONG_PIXELS"
        detail = ""
    except (ValueError, zlib.error, struct.error) as error:
        local, detail = "REJECT", str(error)
    external = decoder(data)
    malformed = malformed_types(data)
    signature = (SIGNATURE if malformed and local == "ACCEPT"
                 and external["status"] == "REJECT" else None)
    return {"signature": signature, "oracle": local, "oracle_detail": detail,
            "libpng": external, "malformed_alphabet": malformed,
            "status": "FINDING" if signature else external["status"]}


def ddmin(items, predicate, budget):
    """Deterministic complement ddmin; callers must bind an exact signature."""
    current, granularity = list(items), 2
    while current and budget[0] > 0:
        size, reduced = math.ceil(len(current) / granularity), False
        for start in range(0, len(current), size):
            if budget[0] <= 0:
                return current
            candidate = current[:start] + current[start + size:]
            budget[0] -= 1
            if predicate(candidate):
                current, granularity, reduced = candidate, max(2, granularity - 1), True
                break
        if not reduced:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)
    return current


def neighbors(case):
    """Semantic reductions: remove chunks/bytes or restore one type byte.

    Minimality means 1-minimal in precisely this relation, not shortest PNG bytes.
    CRC/length and the required PNG skeleton are always rebuilt, never corrupted.
    """
    for index, item in enumerate(case):
        yield case[:index] + case[index + 1:]
        payload = bytes.fromhex(item["payload"])
        for position in range(len(payload)):
            replacement = dict(item, payload=(payload[:position] + payload[position + 1:]).hex())
            yield case[:index] + [replacement] + case[index + 1:]
        kind = bytes.fromhex(item["kind"])
        for position, baseline in enumerate(b"vpAg"):
            if kind[position] != baseline:
                replacement = dict(item, kind=(kind[:position] + bytes((baseline,)) + kind[position + 1:]).hex())
                yield case[:index] + [replacement] + case[index + 1:]


def minimize(case, evaluate, signature=SIGNATURE, max_evaluations=MAX_EVALUATIONS):
    budget = [max_evaluations]
    preserving = lambda candidate: evaluate(candidate).get("signature") == signature
    if evaluate(case).get("signature") != signature:
        raise ValueError("input does not reproduce the exact failure signature")
    current = ddmin(case, preserving, budget)
    # Byte-level reductions and a final exhaustive one-step certificate share
    # the same budget. Stop without claiming minimality if the budget expires.
    minimal = False
    while budget[0] > 0:
        reduced = False
        for candidate in neighbors(current):
            if budget[0] <= 0:
                break
            budget[0] -= 1
            if preserving(candidate):
                current, reduced = candidate, True
                break
        else:
            minimal = True
        if not reduced:
            break
    return current, {"evaluations": max_evaluations - budget[0], "budget": max_evaluations,
                     "one_minimal": minimal, "relation": "chunk deletion/payload-byte deletion/type-byte restoration"}


def generated_cases(seed, iterations):
    """Versioned seeds first, then deterministic local Random mutations."""
    corpus = json.loads((HERE / "quality_regressions/seeds.json").read_text())
    if corpus.get("schema") != 1:
        raise ValueError("invalid seed corpus")
    rng = random.Random(seed)
    for index in range(iterations):
        if index < len(corpus["cases"]):
            yield validate_case(corpus["cases"][index])
            continue
        items = [{"kind": b"vpAg".hex(), "payload": bytes(rng.randrange(256) for _ in range(rng.randrange(9))).hex()}
                 for _ in range(rng.randrange(1, 5))]
        chosen, position = rng.randrange(len(items)), rng.randrange(4)
        kind = bytearray.fromhex(items[chosen]["kind"])
        kind[position] = rng.choice((0, 48, 63, 127, 128, 255))
        items[chosen]["kind"] = kind.hex()
        yield items


def candidate_record(case, observation, identity, shrink, original=None, original_observation=None):
    data = png(case)
    original = case if original is None else original
    original_data = png(original)
    return {"schema": 1, "kind": "candidate", "requirement": REQUIREMENT,
            "signature": SIGNATURE, "spec": SPEC, "case": case, "png_hex": data.hex(),
            "png_sha256": digest(data), "observed": observation,
            "original": {"case": original, "png_hex": original_data.hex(),
                         "png_sha256": digest(original_data),
                         "observed": observation if original_observation is None else original_observation},
            "identity": identity, "shrink": shrink, "expected": "reject-invalid-chunk-type-alphabet"}


def read_record(path):
    if path.stat().st_size > 262144:
        raise ValueError("record size exceeds limit")
    record = json.loads(path.read_text())
    keys = {"schema", "kind", "requirement", "signature", "spec", "case", "png_hex",
            "png_sha256", "observed", "identity", "shrink", "expected", "original"}
    if not isinstance(record, dict) or set(record) != keys:
        raise ValueError("unexpected record fields; commands/paths are not record inputs")
    if (record["schema"] != 1 or record["kind"] not in {"candidate", "regression"}
            or record["requirement"] != REQUIREMENT or record["signature"] != SIGNATURE
            or record["spec"] != SPEC or record["expected"] != "reject-invalid-chunk-type-alphabet"):
        raise ValueError("unsupported contract or expectation; expectations are never inferred")
    if not all(isinstance(record[key], dict) for key in ("observed", "identity", "shrink")):
        raise ValueError("record evidence must be objects")
    if (not isinstance(record["observed"].get("libpng"), dict)
            or not isinstance(record["identity"].get("tools", {}), dict)):
        raise ValueError("nested decoder/tool evidence must be objects")
    original = record["original"]
    if not isinstance(original, dict) or set(original) != {"case", "png_hex", "png_sha256", "observed"}:
        raise ValueError("original discovery input/evidence is missing")
    original_data = png(validate_case(original["case"]))
    if (original["png_hex"] != original_data.hex() or original["png_sha256"] != digest(original_data)
            or not isinstance(original["observed"], dict)
            or original["observed"].get("signature") != SIGNATURE):
        raise ValueError("original discovery identity/signature mismatch")
    case = validate_case(record["case"])
    data = png(case)
    if record["png_hex"] != data.hex() or record["png_sha256"] != digest(data) or not malformed_types(data):
        raise ValueError("record bytes/hash/semantic case mismatch")
    return record


def replay_record(record, oracle, decoder):
    observed = target(bytes.fromhex(record["png_hex"]), oracle, decoder)
    passed = (observed["malformed_alphabet"] and observed["oracle"] == "REJECT"
              and observed["libpng"]["status"] == "REJECT")
    return {"status": "PASS" if passed else "FAIL", "requirement": REQUIREMENT,
            "expected": record["expected"], "observed": observed}


def promote(record, oracle, decoder, source_hash, confirm_spec, confirm_hash, output):
    if record["kind"] != "candidate" or confirm_spec != SPEC or confirm_hash != source_hash:
        raise ValueError("promotion requires explicit matching spec and current fixed-oracle hash")
    if (record["observed"].get("signature") != SIGNATURE
            or record["observed"].get("libpng", {}).get("status") != "REJECT"
            or record["shrink"].get("one_minimal") is not True):
        raise ValueError("candidate lacks stable minimized discovery evidence")
    recorded_decoder = record["identity"].get("tools", {}).get("libpng_decoder_sha256")
    if recorded_decoder != digest(decoder.executable.read_bytes()):
        raise ValueError("independent decoder identity changed since discovery")
    replay = replay_record(record, oracle, decoder)
    if replay["status"] != "PASS":
        raise ValueError("fixed oracle and actual libpng must reject before promotion")
    output.mkdir(parents=True, exist_ok=False)
    fixture = dict(record, kind="regression")
    # Preserve the discovered expectation and before evidence verbatim.
    (output / "regression.json").write_text(json.dumps(fixture, indent=2) + "\n")
    (output / "regression.png").write_bytes(bytes.fromhex(record["png_hex"]))
    (output / "original.png").write_bytes(bytes.fromhex(record["original"]["png_hex"]))
    confirmation = {"schema": 1, "spec": confirm_spec, "fixed_oracle_sha256": source_hash,
                    "fixture_sha256": digest(canonical(fixture)), "replay": replay}
    (output / "confirmation.json").write_text(json.dumps(confirmation, indent=2) + "\n")
    return {"status": "PASS", "fixture": str((output / "regression.json").resolve())}


def identity(source, decoder):
    files = [Path(__file__).resolve(), source, HERE / "quality_regressions/seeds.json"]
    return {"source_files": {str(p): digest(p.read_bytes()) for p in files},
            "tools": {"python": sys.version, "python_executable_sha256": digest(Path(sys.executable).read_bytes()),
                      "zlib_build": zlib.ZLIB_VERSION, "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
                      "libpng_decoder": str(decoder.executable),
                      "libpng_decoder_sha256": digest(decoder.executable.read_bytes())},
            "configuration": {"timeout_seconds": 5, "shrink_budget": MAX_EVALUATIONS,
                              "dimensions": [1, 1], "format": "RGBA8"},
            "corpora": [{"id": "mioa-png-alphabet-seeds", "revision": "1",
                         "sha256": digest(files[-1].read_bytes()), "license": "project-authored seed data"}]}


def fuzz(args):
    output, source = args.output.resolve(), args.oracle_source.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": 1, "status": "NOT_RUN", "scope": "PNG chunk-type alphabet oracle differential only",
              "identity": {}, "checks": [], "mutations": [], "artifacts": {},
              "fuzz": {"seed": args.seed, "iterations_requested": args.iterations,
                       "iterations_executed": 0, "findings": 0, "shrink_evaluations": 0}}
    try:
        decoder = Decoder(args.decoder)
        report["identity"] = identity(source, decoder)
        report["identity"]["configuration"].update(seed=args.seed, iterations=args.iterations)
        oracle = load_oracle(source)
        baseline = target(png([]), oracle, decoder)
        valid = baseline["oracle"] == "ACCEPT" and baseline["libpng"]["status"] == "ACCEPT"
        report["checks"].append({"id": "baseline", "requirement": REQUIREMENT,
                                 "oracle": "exact RGBA plus actual libpng", "status": "PASS" if valid else "FAIL",
                                 "detail": baseline})
        if not valid:
            report["status"] = "FAIL"
        else:
            report["status"] = "PASS"
            seen = set()
            for case in generated_cases(args.seed, args.iterations):
                observed = target(png(case), oracle, decoder)
                report["fuzz"]["iterations_executed"] += 1
                if observed["libpng"]["status"] == "INVALID":
                    report["status"] = "FAIL"
                    report["checks"].append({"id": "decoder-infrastructure", "requirement": REQUIREMENT,
                                             "oracle": "libpng process", "status": "FAIL", "detail": observed})
                    break
                if observed["signature"]:
                    report["status"] = "FAIL"
                    minimized, shrink = minimize(case, lambda item: target(png(item), oracle, decoder))
                    report["fuzz"]["shrink_evaluations"] += shrink["evaluations"]
                    final = target(png(minimized), oracle, decoder)
                    data = png(minimized)
                    if digest(data) in seen:
                        continue
                    seen.add(digest(data))
                    record = candidate_record(minimized, final, report["identity"], shrink, case, observed)
                    name = f"candidate-{digest(data)[:16]}"
                    path = output / (name + ".json")
                    path.write_text(json.dumps(record, indent=2) + "\n")
                    png_path = output / (name + ".png")
                    png_path.write_bytes(data)
                    original_path = output / (name + ".original.png")
                    original_path.write_bytes(png(case))
                    report["fuzz"]["findings"] += 1
                    report["artifacts"][name] = {"record": str(path), "png": str(png_path), "sha256": digest(data),
                                                 "original_png": str(original_path), "original_sha256": digest(png(case))}
            report["checks"].append({"id": "seeded-differential", "requirement": REQUIREMENT,
                                     "oracle": "CRC-valid grammar mutations / exact signature", "status": report["status"],
                                     "detail": "No finding is a valid bounded result; not evidence of conformance."})
    except (FileNotFoundError, PermissionError) as error:
        report["status"], report["error"] = "NOT_RUN", str(error)
    except (ValueError, OSError, SyntaxError) as error:
        report["status"], report["error"] = "FAIL", str(error)
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    (output / "report.toon").write_text(toon.encode(report) + "\n")
    print(toon.encode({"status": report["status"], "report": str(output / "report.toon")}))
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv.pop(0) if argv and argv[0] in {"replay", "promote"} else "fuzz"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--oracle-source", type=Path, default=HERE / "run_png_roundtrip.py")
    if action == "fuzz":
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument("--seed", default=0x504e47, type=int)
        parser.add_argument("--iterations", default=32, type=int)
    else:
        parser.add_argument("--record", required=True, type=Path)
        if action == "promote":
            parser.add_argument("--output", required=True, type=Path)
            parser.add_argument("--confirm-spec", required=True)
            parser.add_argument("--confirm-oracle-sha256", required=True)
    args = parser.parse_args(argv)
    if action == "fuzz":
        if not 1 <= args.iterations <= 10000:
            parser.error("iterations must be between 1 and 10000")
        return fuzz(args)
    try:
        record, decoder = read_record(args.record), Decoder(args.decoder)
        oracle = load_oracle(args.oracle_source)
        if action == "replay":
            result = replay_record(record, oracle, decoder)
            result["identity"] = identity(args.oracle_source, decoder)
            result["record_sha256"] = digest(args.record.read_bytes())
        else:
            result = promote(record, oracle, decoder, digest(args.oracle_source.read_bytes()),
                             args.confirm_spec, args.confirm_oracle_sha256, args.output)
    except (ValueError, OSError, SyntaxError) as error:
        result = {"status": "FAIL", "error": str(error)}
    # replay/promote is a program step consumed by quality/run.py; fuzz mode is agent-facing.
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
