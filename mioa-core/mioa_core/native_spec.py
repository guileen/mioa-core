"""Versioned native environment inputs; generation never happens during launch.

Overrides win over seeded defaults. Digests pin executable bytes, real host font
files and render inputs, not advertised GPU/audio identities. The manager owns
locking, atomic profile commits, revision archives and storage snapshots.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import struct
from pathlib import Path
from typing import Any, Iterable, Literal, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = 3
GENERATOR_VERSION = "native-3.3"
ADAPTER_VERSION = "native-cdp-1"
ROOT = Path(__file__).resolve().parents[2]
PINS = {
    "obscura": ("7c751c5cd8833ac951d48320218c4fe91aa97ac8", "browsers/engine-bench/.local/obscura-cost/target/release/obscura"),
    "lightpanda": ("c5f7bb29b127040c899d4e181b5709f76fc71a6f", "browsers/engine-bench/.local/lightpanda-cost/source/zig-out/bin/lightpanda"),
    "mioa": ("c5f7bb29b127040c899d4e181b5709f76fc71a6f", "browsers/engine-bench/.local/lightpanda-cost/source/zig-out/bin/lightpanda"),
}
_FILE_SHA_CACHE: dict[tuple[Any, ...], str] = {}
_FONT_FAMILY_CACHE: dict[str, tuple[str, ...]] = {}


class NativeError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details


class ResolvedConfig(TypedDict):
    user_agent: str
    platform: str
    locale: str
    languages: list[str]
    timezone: str
    hardware_concurrency: int
    device_memory: int | float
    screen_width: int
    screen_height: int
    avail_width: int
    avail_height: int
    color_depth: int
    pixel_depth: int
    device_scale_factor: int | float
    fonts: list[str]
    gl_backend: Literal["cgl-default", "cgl-accelerated", "cgl-generic-float"]


class RuntimeSpec(TypedDict):
    id: Literal["obscura", "lightpanda", "mioa"]
    version: str
    artifact_sha256: str
    adapter_version: str
    binary: str


def native_config_bytes(cfg: dict[str, Any]) -> bytes:
    return (json.dumps(cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def native_engine_config(cfg: dict[str, Any]) -> dict[str, Any]:
    if isinstance(cfg.get("runtime"), dict) and cfg["runtime"].get("id") == "mioa":
        out = copy.deepcopy(cfg)
        out["runtime"]["id"] = "lightpanda"
        return out
    return cfg


def environment_digest(cfg: dict[str, Any]) -> str:
    """Digest of the manager's canonical persisted configuration bytes."""
    return hashlib.sha256(native_config_bytes(native_engine_config(cfg))).hexdigest()


def file_sha256(path: str | Path) -> str:
    location = Path(path).resolve()
    def identity(stat: os.stat_result) -> tuple[Any, ...]:
        return (str(location), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    try:
        key = identity(location.stat())
        cached = _FILE_SHA_CACHE.get(key)
        if cached is not None:
            return cached
        with location.open("rb") as stream:
            before = identity(os.fstat(stream.fileno()))
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = identity(os.fstat(stream.fileno()))
        if before != after:
            raise NativeError("incompatible_profile", f"pinned artifact changed while hashing: {location}")
        if len(_FILE_SHA_CACHE) >= 8192:
            _FILE_SHA_CACHE.clear()
        _FILE_SHA_CACHE[before] = digest
        return digest
    except OSError as exc:
        raise NativeError("incompatible_profile", f"cannot read pinned artifact {location}: {exc}") from exc


def _require(condition: bool, message: str, code: str = "invalid_config") -> None:
    if not condition:
        raise NativeError(code, message)


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high



def resolve_binary(runtime: str, binary: str | Path | None = None) -> Path:
    _require(runtime in PINS, f"unknown native runtime {runtime!r}")
    configured = binary or os.environ.get(f"ANTI_TRACE_{runtime.upper()}_BINARY")
    path = Path(configured).expanduser() if configured else ROOT / PINS[runtime][1]
    path = path.resolve()
    _require(path.is_file() and os.access(path, os.X_OK), f"native executable missing or not executable: {path}; build pinned candidate or set --native-binary", "capability_unavailable")
    return path


def runtime_spec(runtime: str, binary: str | Path | None = None) -> RuntimeSpec:
    path = resolve_binary(runtime, binary)
    return {"id": runtime, "version": PINS[runtime][0], "artifact_sha256": file_sha256(path), "adapter_version": ADAPTER_VERSION, "binary": str(path)}


def _host_font_paths() -> list[str]:
    directories = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    paths = {str(p.resolve()) for directory in directories if directory.is_dir() for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}}
    _require(bool(paths), "no actual font files found; provide asset_paths.fonts", "capability_unavailable")
    return sorted(paths)


def _assets(resolved: dict[str, Any], paths: list[str]) -> dict[str, str]:
    fonts = sorted({file_sha256(path) for path in paths})
    render = {k: resolved[k] for k in ("device_scale_factor", "color_depth", "pixel_depth", "screen_width", "screen_height", "fonts", "gl_backend")}
    render["device_scale_factor"] = format(resolved["device_scale_factor"], ".6f").rstrip("0").rstrip(".")
    return {"fonts_sha256": _digest(fonts), "render_config_sha256": _digest(render)}


def _defaults(seed: str, runtime: str) -> ResolvedConfig:
    return {**_defaults_without_backend(seed), "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "gl_backend": "cgl-default"}

def _defaults_without_backend(seed: str) -> dict[str, Any]:
    state = bytes.fromhex(hashlib.sha256(bytes.fromhex(seed)).hexdigest())
    width, height = [(1280, 800), (1440, 900), (1512, 982), (1728, 1117), (1920, 1080)][state[0] % 5]
    return {"platform": "MacIntel", "locale": "en-US", "languages": ["en-US", "en"], "timezone": "UTC", "hardware_concurrency": [4, 8, 12, 16][state[1] % 4], "device_memory": [4, 8][state[2] % 2], "screen_width": width, "screen_height": height, "avail_width": width, "avail_height": height - 24, "color_depth": 24, "pixel_depth": 24, "device_scale_factor": [1, 2][state[3] % 2], "fonts": []}


def _resolve(seed: str, runtime: str, overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(_defaults(seed, runtime))
    _require(isinstance(overrides, dict), "overrides must be an object")
    _require(not (set(overrides) - set(result)), f"unknown overrides: {sorted(set(overrides) - set(result))}")
    result.update(copy.deepcopy(overrides))
    # Dependent defaults follow their explicit parent unless also overridden.
    if "locale" in overrides and "languages" not in overrides:
        _require(isinstance(result["locale"], str), "locale override must be a string")
        result["languages"] = list(dict.fromkeys([result["locale"], result["locale"].split("-")[0]]))
    for dimension in ("width", "height"):
        if f"screen_{dimension}" in overrides and f"avail_{dimension}" not in overrides:
            _require(_integer(result[f"screen_{dimension}"], 320, 16384), f"invalid screen_{dimension} override")
            result[f"avail_{dimension}"] = result[f"screen_{dimension}"] - (24 if dimension == "height" else 0)
    return result


def _validate_resolved(r: dict[str, Any]) -> None:
    _require(set(r) == set(ResolvedConfig.__annotations__), "resolved keys must match schema3 flat native fields")
    for key in ("user_agent", "platform", "locale", "timezone"):
        _require(isinstance(r[key], str) and bool(r[key].strip()) and len(r[key]) <= 2048 and not any(c in r[key] for c in "\r\n\0"), f"invalid resolved.{key}")
    _require(re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", r["locale"]) is not None, "locale must be a language tag")
    try:
        ZoneInfo(r["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise NativeError("invalid_config", f"unknown timezone {r['timezone']!r}") from exc
    _require(isinstance(r["languages"], list) and 0 < len(r["languages"]) <= 32 and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", v) for v in r["languages"]), "languages must contain valid language tags")
    _require(r["languages"][0] == r["locale"] and len(set(r["languages"])) == len(r["languages"]), "locale must be first in unique languages")
    _require(_integer(r["hardware_concurrency"], 1, 256), "hardware_concurrency must be integer 1..256")
    _require(type(r["device_memory"]) in (int, float) and r["device_memory"] in (0.25, 0.5, 1, 2, 4, 8), "device_memory must be a standard bucket")
    for dimension in ("width", "height"):
        _require(_integer(r[f"screen_{dimension}"], 320, 16384), f"invalid screen_{dimension}")
        _require(_integer(r[f"avail_{dimension}"], 1, r[f"screen_{dimension}"]), f"avail_{dimension} exceeds screen")
    _require(r["color_depth"] == 24 and r["pixel_depth"] == 24, "native rendering currently supports only 24-bit color/pixel depth")
    _require(type(r["device_scale_factor"]) in (int, float) and r["device_scale_factor"] in {0.5, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4}, "device_scale_factor must be a supported discrete scale")
    _require(isinstance(r["fonts"], list) and len(r["fonts"]) <= 1024 and all(isinstance(v, str) and v.strip() and len(v) <= 256 for v in r["fonts"]) and len(set(r["fonts"])) == len(r["fonts"]), "fonts must be unique real family names")
    _require(r["gl_backend"] in ("cgl-default", "cgl-accelerated", "cgl-generic-float"), "gl_backend must select a supported real CGL backend")


def identity_digest(cfg: dict[str, Any]) -> str:
    runtime = {key: cfg["runtime"].get(key) for key in ("id", "version", "artifact_sha256", "adapter_version")}
    return _digest({"runtime": runtime, "resolved": cfg["resolved"], "assets": cfg["assets"]})


def check_identity_collision(cfg: dict[str, Any], existing: Iterable[dict[str, Any]]) -> None:
    digest = identity_digest(cfg)
    for other in existing:
        if other.get("id") == cfg["id"] and other.get("name") == cfg["name"]:
            continue
        if other.get("id") == cfg["id"] or other.get("storage", {}).get("path") == cfg["storage"]["path"]:
            raise NativeError("identity_collision", "environment id or writable storage directory already belongs to another environment")
        if other.get("schema") != SCHEMA or not isinstance(other.get("runtime"), dict) or other["runtime"].get("id") != cfg["runtime"]["id"]:
            continue
        if identity_digest(other) == digest:
            raise NativeError("identity_collision", f"resolved runtime/assets identity duplicates environment {other.get('name', other.get('id'))!r}")


def create_native_spec(name: str, *, runtime: str, storage_path: str | Path, seed: str | None = None, overrides: dict[str, Any] | None = None, network: dict[str, Any] | None = None, permissions: dict[str, str] | None = None, existing: Iterable[dict[str, Any]] = (), binary: str | Path | None = None, asset_paths: dict[str, list[str]] | None = None) -> dict[str, Any]:
    _require(runtime in PINS, f"unknown native runtime {runtime!r}")
    _require(overrides is None or isinstance(overrides, dict), "overrides must be an object")
    _require(network is None or isinstance(network, dict), "network must be an object")
    _require(permissions is None or isinstance(permissions, dict), "permissions must be an object")
    seed = seed if seed is not None else secrets.token_hex(32)
    _require(isinstance(seed, str) and re.fullmatch(r"[0-9a-f]{32,128}", seed) is not None and len(seed) % 2 == 0, "seed must be 16..64 bytes lowercase hex")
    overrides = copy.deepcopy(overrides or {})
    resolved = _resolve(seed, runtime, overrides)
    _validate_resolved(resolved)
    paths = asset_paths or {"fonts": _host_font_paths()}
    _require(isinstance(paths, dict) and set(paths) == {"fonts"} and isinstance(paths["fonts"], list) and bool(paths["fonts"]) and all(isinstance(p, str) and Path(p).is_absolute() for p in paths["fonts"]), "asset_paths must contain actual absolute font files")
    cfg = {"schema": SCHEMA, "id": secrets.token_hex(16), "name": name, "mode": "managed", "revision": 1, "seed": seed, "generator_version": GENERATOR_VERSION, "runtime": runtime_spec(runtime, binary), "overrides": overrides, "resolved": resolved, "asset_paths": copy.deepcopy(paths), "assets": _assets(resolved, paths["fonts"]), "network": {"proxy_ref": None, "egress_policy": "strict", "allow_private_network": False, **(network or {})}, "storage": {"format_version": 1, "path": str(Path(storage_path).expanduser().resolve())}, "permissions": dict(permissions or {}), "capabilities": {}, "identity_scope": "environment"}
    cfg["resolved_sha256"] = _digest(resolved)
    validate_native_spec(cfg, name=name)
    check_identity_collision(cfg, existing)
    return cfg


def validate_native_spec(cfg: dict[str, Any], *, name: str | None = None, check_artifacts: bool = True) -> dict[str, Any]:
    _require(isinstance(cfg, dict) and cfg.get("schema") == SCHEMA, "native profiles require schema3; migrate explicitly", "incompatible_profile")
    _require(isinstance(cfg.get("id"), str) and bool(cfg["id"]) and _integer(cfg.get("revision"), 1, 2**53 - 1), "invalid environment id/revision")
    _require(isinstance(cfg.get("name"), str) and re.fullmatch(r"[\w-]+", cfg["name"]) is not None and cfg.get("mode") == "managed", "invalid managed environment name/mode")
    _require(name is None or cfg["name"] == name, "environment name differs from configuration")
    _require(cfg.get("identity_scope") == "environment", "identity_scope must be environment")
    _require(cfg.get("generator_version") == GENERATOR_VERSION, "generator version changed; explicit migration required", "incompatible_profile")
    _require(isinstance(cfg.get("seed"), str) and re.fullmatch(r"[0-9a-f]{32,128}", cfg["seed"]) is not None and len(cfg["seed"]) % 2 == 0, "invalid seed")
    runtime = cfg.get("runtime", {})
    _require(isinstance(runtime, dict) and runtime.get("id") in PINS, "invalid native runtime")
    _require(runtime.get("version") == PINS[runtime["id"]][0] and runtime.get("adapter_version") == ADAPTER_VERSION, "runtime/adapter version changed; migrate explicitly", "incompatible_profile")
    _require(_sha(runtime.get("artifact_sha256")), "missing pinned runtime digest")
    _require(isinstance(runtime.get("binary"), str) and Path(runtime["binary"]).is_absolute(), "runtime.binary must be absolute")
    _require(isinstance(cfg.get("resolved"), dict), "resolved must be an object")
    _validate_resolved(cfg["resolved"])
    _require(cfg["resolved"] == _resolve(cfg["seed"], runtime["id"], cfg.get("overrides", {})), "resolved does not match generator + explicit overrides", "incompatible_profile")
    _require(cfg.get("resolved_sha256") == _digest(cfg["resolved"]), "resolved digest mismatch", "incompatible_profile")
    storage = cfg.get("storage", {})
    _require(isinstance(storage, dict) and storage.get("format_version") == 1 and isinstance(storage.get("path"), str) and Path(storage["path"]).is_absolute(), "invalid storage path/format")
    net = cfg.get("network", {})
    _require(isinstance(net, dict) and not (set(net) - {"proxy_ref", "egress_policy", "allow_private_network"}) and net.get("egress_policy") in ("strict", "observe") and type(net.get("allow_private_network", False)) is bool, "invalid native network policy")
    ref = net.get("proxy_ref")
    _require(ref is None or (isinstance(ref, str) and re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", ref) is not None), "proxy_ref must be null or env:VARIABLE (never embed credentials)")
    permissions = cfg.get("permissions", {})
    _require(isinstance(permissions, dict) and all(isinstance(k, str) and v in ("deny", "prompt", "allow") for k, v in permissions.items()), "invalid permission policy")
    _require(not any(v != "deny" for v in permissions.values()), "native permission grants/prompt backends are not implemented", "capability_unavailable")
    assets, paths = cfg.get("assets", {}), cfg.get("asset_paths", {})
    _require(isinstance(assets, dict) and set(assets) == {"fonts_sha256", "render_config_sha256"} and all(_sha(v) for v in assets.values()), "invalid asset digests")
    _require(isinstance(paths, dict) and set(paths) == {"fonts"} and isinstance(paths["fonts"], list) and bool(paths["fonts"]) and all(isinstance(p, str) and Path(p).is_absolute() for p in paths["fonts"]), "asset_paths.fonts must pin actual absolute font files")
    _require(isinstance(cfg.get("capabilities"), dict), "capabilities must be an object")
    _require(all(isinstance(feature, str) and isinstance(value, dict) and value.get("state") in {"implemented", "disabled", "unsupported"} and isinstance(value.get("reason"), str) for feature, value in cfg["capabilities"].items()), "invalid capability entries")
    if "previous_environment_sha256" in cfg:
        _require(cfg["revision"] > 1 and _sha(cfg["previous_environment_sha256"]), "migration must bind prior revision digest")
    _require(cfg["revision"] == 1 or _sha(cfg.get("previous_environment_sha256")), "revision >1 requires the prior committed environment digest")
    if check_artifacts:
        binary = resolve_binary(runtime["id"], runtime["binary"])
        _require(file_sha256(binary) == runtime["artifact_sha256"], "native executable bytes changed; explicit migration required", "incompatible_profile")
        _require(_assets(cfg["resolved"], paths["fonts"]) == assets, "font/render assets changed; explicit migration required", "incompatible_profile")
    return cfg


def resolve_proxy(cfg: dict[str, Any]) -> str | None:
    ref = cfg["network"].get("proxy_ref")
    if ref is None:
        return None
    from . import profile
    value = os.environ.get(ref[4:])
    if value is None:
        value = profile.read_proxy_secret(cfg)
    _require(bool(value), "configured proxy credential reference is unavailable", "network_unavailable")
    try:
        profile._validate_proxy_url(value, cfg)
    except profile.ProfileError as exc:
        raise NativeError(exc.code, "invalid configured proxy URL") from exc
    return value


def compatibility_differences(saved: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    differences = []
    for key in ("schema", "generator_version", "assets", "resolved_sha256"):
        if saved.get(key) != current.get(key):
            differences.append({"field": key, "saved": saved.get(key), "current": current.get(key)})
    for section, fields in (("runtime", ("id", "version", "artifact_sha256", "adapter_version")), ("storage", ("format_version",))):
        for field in fields:
            before, after = saved.get(section, {}).get(field), current.get(section, {}).get(field)
            if before != after:
                differences.append({"field": f"{section}.{field}", "saved": before, "current": after})
    return differences


def migrate_native_spec(cfg: dict[str, Any], *, runtime: str | None = None, binary: str | Path | None = None, overrides: dict[str, Any] | None = None, assets: dict[str, list[str]] | None = None, network: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a new revision, never mutate/archive/commit the old one.

    Native persistent storage cannot be relabeled as another engine. The manager
    owns cold snapshots and the atomic commit of this same-engine revision.
    """
    target = runtime or cfg.get("runtime", {}).get("id")
    _require(target in PINS, "migration requires an existing native runtime")
    _require(cfg.get("schema") == SCHEMA and target == cfg.get("runtime", {}).get("id"), "storage migration requires the same native engine and schema3", "incompatible_profile")
    result = create_native_spec(cfg["name"], runtime=target, storage_path=cfg["storage"]["path"], seed=cfg["seed"], overrides=overrides if overrides is not None else cfg["overrides"], network=network if network is not None else cfg["network"], permissions=cfg["permissions"], binary=binary or cfg["runtime"]["binary"], asset_paths=assets or cfg["asset_paths"])
    result.update(id=cfg["id"], revision=cfg["revision"] + 1, previous_environment_sha256=environment_digest(cfg))
    return result


def native_asset_descriptors(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Return actual pinned source files for the manager's cold archive policy."""
    validate_native_spec(cfg)
    return [
        {"kind": "runtime", "path": cfg["runtime"]["binary"], "sha256": cfg["runtime"]["artifact_sha256"]},
        *({"kind": "font", "path": path, "sha256": file_sha256(path)} for path in sorted(cfg["asset_paths"]["fonts"])),
    ]


def rebind_asset_paths(cfg: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Return a byte-verified relocation without changing identity/revision.

    Reads new assets but never writes files or reads an absent original path.
    Changed bytes require explicit migration, not path rebinding.
    """
    validate_native_spec(cfg, check_artifacts=False)
    _require(isinstance(mapping, dict) and all(isinstance(old, str) and Path(old).is_absolute() and isinstance(new, str) and Path(new).is_absolute() for old, new in mapping.items()), "asset rebind mapping must contain absolute paths")
    allowed = {*cfg["asset_paths"]["fonts"], cfg["runtime"]["binary"]}
    _require(not (set(mapping) - allowed), "asset rebind contains paths not owned by this configuration")
    result = copy.deepcopy(cfg)
    result["asset_paths"]["fonts"] = sorted({mapping.get(path, path) for path in cfg["asset_paths"]["fonts"]})
    result["runtime"]["binary"] = mapping.get(cfg["runtime"]["binary"], cfg["runtime"]["binary"])
    validate_native_spec(result)
    return result


def native_font_assets() -> dict[str, list[str]]:
    """Discover actual host font files; no browser launch or family presets."""
    return {"fonts": _host_font_paths()}


def _font_file_families(path: str, digest: str) -> tuple[str, ...]:
    cached = _FONT_FAMILY_CACHE.get(digest)
    if cached is not None:
        return cached
    families: set[str] = set()
    try:
        with Path(path).open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            def section(offset: int, length: int) -> bytes:
                _require(0 <= offset <= size and 0 <= length <= min(size - offset, 16 * 1024 * 1024), f"invalid font table bounds: {path}")
                stream.seek(offset)
                value = stream.read(length)
                _require(len(value) == length, f"truncated font file: {path}")
                return value
            magic = section(0, 4)
            if magic == b"ttcf":
                count = struct.unpack(">I", section(8, 4))[0]
                _require(0 < count <= 4096, f"invalid font collection count: {path}")
                offsets = struct.unpack(f">{count}I", section(12, count * 4))
            else:
                offsets = (0,)
            for offset in offsets:
                header = section(offset, 12)
                _require(header[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"), f"unsupported SFNT font header: {path}", "capability_unavailable")
                table_count = struct.unpack_from(">H", header, 4)[0]
                _require(0 < table_count <= 4096, f"invalid font table count: {path}")
                tables = section(offset + 12, table_count * 16)
                names = None
                for index in range(table_count):
                    tag, _, start, length = struct.unpack_from(">4sIII", tables, index * 16)
                    if tag == b"name":
                        names = section(start, length)
                        break
                _require(names is not None and len(names) >= 6, f"font name table unavailable: {path}", "capability_unavailable")
                name_format, count, strings = struct.unpack_from(">HHH", names, 0)
                _require(name_format in (0, 1) and 6 + count * 12 <= len(names) and strings <= len(names), f"invalid font name table: {path}")
                candidates = []
                for index in range(count):
                    platform, encoding, language, name_id, length, start = struct.unpack_from(">HHHHHH", names, 6 + index * 12)
                    if name_id not in (1, 16):
                        continue
                    _require(strings + start + length <= len(names), f"invalid font family name bounds: {path}")
                    raw = names[strings + start:strings + start + length]
                    codec = "utf-16-be" if platform in (0, 3) else "mac_roman" if platform == 1 and encoding == 0 else None
                    if codec is None:
                        continue
                    try:
                        family = raw.decode(codec).strip("\0 \t\r\n")
                    except UnicodeDecodeError:
                        continue
                    if family and len(family) <= 256:
                        english = language in (0, 0x0409)
                        candidates.append((not english, name_id != 16, platform != 3, family))
                if candidates:
                    # One preferred real family per face; use typographic names
                    # where available, with actual legacy family names otherwise.
                    families.add(min(candidates)[3])
    except OSError as exc:
        raise NativeError("incompatible_profile", f"cannot read font metadata {path}: {exc}") from exc
    _require(bool(families), f"no supported family metadata in pinned font {path}", "capability_unavailable")
    _require(file_sha256(path) == digest, f"font changed while reading family metadata: {path}", "incompatible_profile")
    result = tuple(sorted(families, key=lambda name: (name.casefold(), name)))
    _FONT_FAMILY_CACHE[digest] = result
    return result


def native_font_families(asset_paths: dict[str, list[str]] | None = None) -> list[str]:
    """Read actual SFNT/TTC family names, cached by immutable content digest.

    The caller can seed-select a subset, then pass those names as resolved.fonts.
    Native font backends still validate their actual selectable family inventory.
    """
    paths = asset_paths if asset_paths is not None else native_font_assets()
    _require(isinstance(paths, dict) and set(paths) == {"fonts"} and isinstance(paths["fonts"], list) and bool(paths["fonts"]) and all(isinstance(path, str) and Path(path).is_absolute() for path in paths["fonts"]), "font metadata requires a nonempty absolute fonts asset list")
    families = {family for path in paths["fonts"] for family in _font_file_families(path, file_sha256(path))}
    return sorted(families, key=lambda name: (name.casefold(), name))
