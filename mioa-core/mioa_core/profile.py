"""Cold, lease-protected managed environment persistence.

Proxy URLs live under the owner-only manager secret store, keyed by immutable
environment id; configs contain references only. Portable exports include
content-addressed font bytes, never credentials unless explicitly requested.
Same-engine migration snapshots preserve config/data before atomic config
commit; rollback atomically exchanges complete profile trees. Private rollback
snapshots retain legacy config bytes and configured credentials (0700/0600);
they are not portable exports.

Core owns native runtimes only. A consumer that introduces another managed
runtime supplies a ``RuntimeAdapter`` mapping to ``export``, ``import_archive``
and ``migrate``. Core never imports the runtime implementation, and a missing
adapter fails with ``capability_unavailable`` instead of falling back silently.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import io
import json
import os
import shutil
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol
from mioa_os_api import ArtifactContent, AtomicDirectories, AtomicExchangeUnavailable, ProfileDataReset, ProfileLeases, ProfileOwners, ProxySecrets, WallClock
from mioa_os_api.snapshots import SnapshotDirectories, UnsafeSnapshotStore
from urllib.parse import urlsplit

SCHEMA = 3
NATIVE_RUNTIMES = frozenset({"obscura", "lightpanda", "mioa"})
BASE: Path | None = None


def _base() -> Path:
    if BASE is None:
        raise ProfileError("capability_unavailable: Host profile root is required")
    return BASE


def store() -> Path:
    """Profile store under the explicitly Host-selected root."""
    return _base() / "profiles"


def _profiles() -> Path:
    return store()
TRANSIENT = {"engine.log", "engine.sock", ".lock", ".spawn", "state.json"}
LOCK_FILES = {"engine": ".lock", "spawn": ".spawn", "catalog": ".catalog"}
ARCHIVE_MANIFEST = ".archive-manifest.json"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ProfileError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        prefix, separator, _ = message.partition(":")
        self.code = code or (prefix if separator and re.fullmatch(r"[a-z][a-z_]*", prefix) else "profile_error")
        super().__init__(message)


def sanitize(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in "-_").strip("-_")
    if not cleaned:
        raise ProfileError(f"invalid environment name: {name!r}")
    return cleaned


def profile_dir(name: str) -> Path:
    return store() / sanitize(name)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, data: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical_bytes(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def load_config(name: str) -> dict[str, Any] | None:
    return _read(profile_dir(name) / "config.json")


def save_config(name: str, cfg: dict[str, Any]) -> None:
    _write(profile_dir(name) / "config.json", cfg)
    os.chmod(profile_dir(name), 0o700)


def load_state(name: str) -> dict[str, Any] | None:
    return _read(profile_dir(name) / "state.json")


def save_state(name: str, state: dict[str, Any]) -> None:
    _write(profile_dir(name) / "state.json", state, mode=0o600)


def clear_state(name: str) -> None:
    (profile_dir(name) / "state.json").unlink(missing_ok=True)



def log_path(name: str) -> Path:
    return profile_dir(name) / "engine.log"


def exists(name: str) -> bool:
    return (profile_dir(name) / "config.json").is_file()

def exclusive(name: str, timeout: float = 0.0, kind: str = "engine", *,
              leases: ProfileLeases):
    """Acquire an explicitly Host-provided store and legacy profile lease."""
    if kind not in LOCK_FILES:
        raise ProfileError(f"unknown lease kind: {kind}")
    return leases.acquire(_base(), sanitize(name), kind, timeout)


def list_profiles(*, owners: ProfileOwners) -> list[dict[str, Any]]:
    if owners is None:
        raise ProfileError("capability_unavailable: Host profile owners service is required")
    _profiles().mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for entry in sorted(_profiles().iterdir()):
        if not entry.is_dir() or not (entry / "config.json").is_file():
            continue
        try:
            if sanitize(entry.name) != entry.name:
                continue
        except ProfileError:
            continue
        cfg = load_config(entry.name) or {}
        state = load_state(entry.name) or {}
        attached = cfg.get("mode") == "attached"
        running = owners.alive(BASE, entry.name)
        if not running:
            state = {}
        rows.append({
            "name": entry.name,
            "status": "ATTACHED" if attached else ("RUNNING" if running else "IDLE"),
            "mode": "attached" if attached else "managed",
            "cdp": cfg.get("cdp"),
            "socket": state.get("socket"),
            "proxy": "CONFIGURED" if (cfg.get("network") or {}).get("proxy_ref") or (cfg.get("network") or {}).get("proxy") else "DIRECT",
            "egress_ip": (cfg.get("network") or {}).get("egress", {}).get("ip"),
            "timezone": (cfg.get("network") or {}).get("egress", {}).get("timezone"),
            "locale": (cfg.get("network") or {}).get("egress", {}).get("locale"),
            "fingerprint_id": (cfg.get("identity") or {}).get("id"),
            "created_at": cfg.get("created_at"),
            "started_at": state.get("started_at"),
            "url": state.get("url"),
        })
    return rows


def require(name: str) -> dict[str, Any]:
    cfg = load_config(name)
    if cfg is None:
        raise ProfileError(f"environment {sanitize(name)!r} does not exist; create it first")
    return cfg


def reset_data(name: str, *, data_reset: ProfileDataReset) -> None:
    """Reset runtime data through an explicit Host authority."""
    if data_reset is None:
        raise ProfileError("capability_unavailable: Host profile data reset service is required")
    data_reset.reset(profile_dir(name))


def remove(name: str, *, proxy_secrets: ProxySecrets | None = None) -> None:
    cfg = require(name)
    owned_secret = bool(cfg.get("id") and (cfg.get("network") or {}).get("proxy_ref") == _proxy_ref(cfg))
    if owned_secret:
        _secret_authority(proxy_secrets)
    shutil.rmtree(profile_dir(name))
    if owned_secret:
        clear_proxy_secret(cfg, proxy_secrets=proxy_secrets)


def _proxy_ref(cfg: dict[str, Any]) -> str:
    identity = cfg.get("id")
    if not isinstance(identity, str) or not identity:
        raise ProfileError("invalid_config: proxy credentials require an immutable environment id")
    return "env:ANTI_TRACE_PROXY_" + hashlib.sha256(identity.encode()).hexdigest().upper()


def _secret_authority(proxy_secrets: ProxySecrets | None) -> ProxySecrets:
    if proxy_secrets is None:
        raise ProfileError("capability_unavailable: Host proxy-secret service is required")
    return proxy_secrets


def read_proxy_secret(cfg: dict[str, Any], *, proxy_secrets: ProxySecrets | None) -> str | None:
    if (cfg.get("network") or {}).get("proxy_ref") != _proxy_ref(cfg):
        return None
    return _secret_authority(proxy_secrets).read(cfg["id"])


def clear_proxy_secret(cfg: dict[str, Any], *, proxy_secrets: ProxySecrets | None) -> None:
    if cfg.get("id"):
        _secret_authority(proxy_secrets).delete(cfg["id"])


def configure_proxy(cfg: dict[str, Any], value: str | None, *, proxy_secrets: ProxySecrets | None = None) -> None:
    network = cfg.setdefault("network", {})
    if value is None or value.strip().upper() in {"DIRECT", "NONE", ""}:
        if cfg.get("id") and network.get("proxy_ref") == _proxy_ref(cfg):
            clear_proxy_secret(cfg, proxy_secrets=proxy_secrets)
        network["proxy_ref"] = None
    elif re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", value):
        from .native_spec import resolve_proxy
        candidate = copy.deepcopy(cfg)
        candidate["network"]["proxy_ref"] = value
        resolve_proxy(candidate, proxy_secrets=proxy_secrets)
        if cfg.get("id") and network.get("proxy_ref") == _proxy_ref(cfg) and value != network["proxy_ref"]:
            clear_proxy_secret(cfg, proxy_secrets=proxy_secrets)
        network["proxy_ref"] = value
    else:
        # Validate without installing or changing process-global environment.
        _validate_proxy_url(value, cfg)
        _secret_authority(proxy_secrets).write(cfg["id"], value)
        network["proxy_ref"] = _proxy_ref(cfg)
    network.pop("proxy", None)
    network.pop("proxy_credentials_required", None)


def _validate_proxy_url(value: str, cfg: dict[str, Any]) -> None:
    try:
        parsed = urlsplit(value)
        camoufox = (cfg.get("runtime") or {}).get("id") == "camoufox"
        schemes = {"http", "https", "socks5h"} | ({"socks5"} if camoufox else set())
        valid = parsed.scheme in schemes and parsed.hostname is not None and (parsed.port is not None or camoufox) and not parsed.path.strip("/") and not parsed.query and not parsed.fragment and not any(c in value for c in "\r\n\0")
        if camoufox and parsed.scheme.startswith("socks") and parsed.username is not None:
            valid = False
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProfileError("invalid_config: invalid or unsupported proxy URL (native runtimes require an explicit port)")


class _HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.digest.update(data)
        return data


class RuntimeAdapter(Protocol):
    """Consumer-owned lifecycle for a managed runtime core does not implement.

    ``validate`` must raise ``ProfileError`` for an invalid configuration.
    ``migrate`` must return the complete replacement config for ``name``; core
    still owns leases, snapshot creation, collision checks and the atomic
    config commit.
    """

    def validate(self, cfg: dict[str, Any], *, check_artifacts: bool = True) -> None: ...

    def migrate(self, cfg: dict[str, Any], *, name: str, binary: str | Path | None,
                overrides: dict[str, Any] | None, inline_proxy: str | None) -> dict[str, Any]: ...


RuntimeAdapters = Mapping[str, RuntimeAdapter]


def _runtime_adapter(runtime: str | None, adapters: RuntimeAdapters | None) -> RuntimeAdapter:
    adapter = (adapters or {}).get(runtime) if runtime is not None else None
    if adapter is None:
        raise ProfileError(f"capability_unavailable: no profile runtime adapter is registered for {runtime!r}")
    return adapter


def _validate_runtime(cfg: dict[str, Any], *, runtime_adapters: RuntimeAdapters | None = None,
                      check_artifacts: bool = True,
                      artifact_content: ArtifactContent | None = None) -> None:
    runtime = (cfg.get("runtime") or {}).get("id")
    if runtime in NATIVE_RUNTIMES:
        from .native_spec import validate_native_spec
        validate_native_spec(cfg, check_artifacts=check_artifacts, artifact_content=artifact_content)
    else:
        _runtime_adapter(runtime, runtime_adapters).validate(cfg, check_artifacts=check_artifacts)


def _validate_managed(cfg: dict[str, Any], *, storage: Path | None = None,
                      runtime_adapters: RuntimeAdapters | None = None,
                      artifact_content: ArtifactContent | None = None) -> None:
    if cfg.get("mode") != "managed" or cfg.get("schema") != SCHEMA:
        raise ProfileError("incompatible_profile: explicit managed schema3 migration required")
    if cfg.get("network", {}).get("proxy"):
        raise ProfileError("incompatible_profile: managed schema3 credentials must use a private reference")
    if storage is not None and cfg.get("storage", {}).get("path") != str((storage / "data").resolve()):
        raise ProfileError("incompatible_profile: storage is not owned by this profile")
    _validate_runtime(cfg, runtime_adapters=runtime_adapters, artifact_content=artifact_content)


def _archive(directory: Path, cfg: dict[str, Any], out: Path, *, include_credentials: bool = False, bundle_fonts: bool = True, snapshot_identity: str | None = None, proxy_secrets: ProxySecrets | None = None, artifact_content: ArtifactContent | None = None) -> Path:
    name = cfg["name"]
    out = out.resolve()
    if out.is_relative_to(directory.resolve()):
        raise ProfileError("invalid_config: archive destination must be outside the profile")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    os.close(descriptor)
    entries: dict[str, Any] = {}
    font_bindings: dict[str, str] = {}
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as tar:
            def add_bytes(relative: str, data: bytes) -> None:
                info = tarfile.TarInfo(f"{name}/{relative}")
                info.size, info.mode = len(data), 0o600
                tar.addfile(info, io.BytesIO(data))
                entries[relative] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

            def add_file(relative: str, path: Path) -> None:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ProfileError("unsafe_archive: profile assets must be regular files")
                    reader = _HashReader(stream)
                    info = tarfile.TarInfo(f"{name}/{relative}")
                    info.size, info.mode, info.mtime = before.st_size, 0o600, before.st_mtime
                    tar.addfile(info, reader)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise ProfileError(f"busy: profile changed during archive: {relative}")
                entries[relative] = {"size": before.st_size, "sha256": reader.digest.hexdigest()}

            if snapshot_identity is not None:
                original_bytes = (directory / "config.json").read_bytes()
                if json.loads(original_bytes) != cfg:
                    raise ProfileError("busy: configuration changed before snapshot")
                add_bytes("config.json", original_bytes)
            else:
                add_bytes("config.json", canonical_bytes(cfg))
            if bundle_fonts:
                from .native_spec import file_sha256
                for filename in cfg.get("asset_paths", {}).get("fonts", []):
                    digest = file_sha256(filename, artifact_content=artifact_content)
                    relative = f".assets/fonts/{digest}"
                    if relative not in entries:
                        add_file(relative, Path(filename))
                    if entries[relative]["sha256"] != digest:
                        raise ProfileError("busy: font changed during archive")
                    font_bindings[filename] = relative
            for path in sorted(directory.rglob("*")):
                relative = path.relative_to(directory).as_posix()
                if relative == "config.json" or relative.split("/")[0] in TRANSIENT | {ARCHIVE_MANIFEST, ".proxy-credentials.json"} or (bundle_fonts and relative.split("/")[0] == ".assets"):
                    continue
                details = path.lstat()
                if stat.S_ISDIR(details.st_mode):
                    info = tarfile.TarInfo(f"{name}/{relative}")
                    info.type, info.mode = tarfile.DIRTYPE, 0o700
                    tar.addfile(info)
                elif stat.S_ISREG(details.st_mode):
                    add_file(relative, path)
                else:
                    raise ProfileError(f"unsafe_archive: unsafe profile entry: {relative}")
            credentials = False
            if include_credentials and cfg.get("network", {}).get("proxy_ref"):
                if snapshot_identity is None:
                    from .native_spec import resolve_proxy
                    secret = resolve_proxy(cfg, proxy_secrets=proxy_secrets)
                elif cfg.get("id") and cfg["network"]["proxy_ref"] == _proxy_ref(cfg):
                    secret = read_proxy_secret(cfg, proxy_secrets=proxy_secrets)
                else:
                    secret = None
                if secret is not None:
                    add_bytes(".proxy-credentials.json", canonical_bytes({"id": cfg["id"], "proxy": secret}))
                    credentials = True
            manifest = canonical_bytes({"schema": 1, "profile_schema": cfg["schema"], "runtime": cfg.get("runtime"), "assets": cfg.get("assets"), "storage_format_version": cfg.get("storage", {}).get("format_version"), "credentials_included": credentials, "font_bindings": font_bindings, "snapshot_identity": snapshot_identity, "files": entries})
            info = tarfile.TarInfo(f"{name}/{ARCHIVE_MANIFEST}")
            info.size, info.mode = len(manifest), 0o600
            tar.addfile(info, io.BytesIO(manifest))
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, out)
        _sync_directory(out.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return out


def export(name: str, dest: Path, *, leases: ProfileLeases, owners: ProfileOwners,
           include_credentials: bool = False, runtime_adapters: RuntimeAdapters | None = None, proxy_secrets: ProxySecrets | None = None,
           artifact_content: ArtifactContent | None = None) -> Path:
    if dest is None:
        raise ProfileError("capability_unavailable: explicit export destination is required")
    name = sanitize(name)
    with exclusive(name, kind="spawn", leases=leases) as serialized, exclusive(name, leases=leases) as owned:
        if not serialized or not owned or owners.alive(BASE, name):
            raise ProfileError(f"busy: stop {name} before export")
        cfg = require(name)
        _validate_managed(cfg, storage=profile_dir(name), runtime_adapters=runtime_adapters,
                          artifact_content=artifact_content)
        return _archive(profile_dir(name), cfg, dest, include_credentials=include_credentials,
                        proxy_secrets=proxy_secrets, artifact_content=artifact_content)


@contextmanager
def _archive_reader(path: Path):
    try:
        with tarfile.open(path, "r:gz") as archive:
            yield archive
    except (tarfile.TarError, ValueError, UnicodeError, EOFError) as exc:
        raise ProfileError("archive_integrity: malformed archive or manifest") from exc


def _extract_archive(archive: Path, staging: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if not archive.is_file():
        raise ProfileError("not_found: archive not found")
    with _archive_reader(archive) as tar:
        members = tar.getmembers()
        roots, names = set(), set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name or not path.parts:
                raise ProfileError("unsafe_archive: unsafe archive path")
            canonical = path.as_posix()
            if canonical != member.name.rstrip("/") or canonical in names or not (member.isfile() or member.isdir()) or (len(path.parts) == 1 and member.isfile()):
                raise ProfileError("unsafe_archive: unsafe or duplicate archive entry")
            names.add(canonical)
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ProfileError("unsafe_archive: archive must contain exactly one profile root")
        root = next(iter(roots))
        if root != sanitize(root):
            raise ProfileError("unsafe_archive: invalid archive root")
        manifest_file = tar.extractfile(f"{root}/{ARCHIVE_MANIFEST}") if f"{root}/{ARCHIVE_MANIFEST}" in names else None
        if manifest_file is None:
            raise ProfileError("incompatible_profile: integrity manifest missing")
        with manifest_file:
            manifest = json.load(manifest_file)
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("profile_schema") not in (2, SCHEMA) or not isinstance(manifest.get("files"), dict):
            raise ProfileError("incompatible_profile: unsupported archive schema")
        expected = manifest["files"]
        actual = {PurePosixPath(m.name).relative_to(root).as_posix() for m in members if m.isfile() and m.name != f"{root}/{ARCHIVE_MANIFEST}"}
        if actual != set(expected) or "config.json" not in expected:
            raise ProfileError("archive_integrity: archive content differs from integrity manifest")
        if bool(manifest.get("credentials_included")) != (".proxy-credentials.json" in actual):
            raise ProfileError("archive_integrity: credential manifest mismatch")
        tar.extractall(staging, members=members, filter="data")
        extracted = staging / root
        for relative, recorded in expected.items():
            path = extracted / relative
            if not isinstance(recorded, dict) or not path.is_file() or path.is_symlink() or path.stat().st_size != recorded.get("size"):
                raise ProfileError("archive_integrity: archive size mismatch")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != recorded.get("sha256"):
                raise ProfileError("archive_integrity: archive digest mismatch")
            os.chmod(path, 0o600)
        cfg = _read(extracted / "config.json")
        if not cfg or cfg.get("schema") != manifest["profile_schema"] or cfg.get("mode") != "managed":
            raise ProfileError("incompatible_profile: invalid managed configuration")
        if any(key in cfg and not isinstance(cfg[key], dict) for key in ("runtime", "assets", "storage", "network", "resolved", "asset_paths")):
            raise ProfileError("invalid_config: archive configuration sections must be objects")
        if cfg.get("runtime") != manifest.get("runtime") or cfg.get("assets") != manifest.get("assets") or cfg.get("storage", {}).get("format_version") != manifest.get("storage_format_version"):
            raise ProfileError("archive_integrity: archive configuration contradicts manifest")
        if any((extracted / entry).exists() for entry in TRANSIENT):
            raise ProfileError("unsafe_archive: transient runtime state is not importable")
        return extracted, cfg, manifest


def _sync_tree(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in directory.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        os.chmod(path, 0o700)
        _sync_directory(path)
    os.chmod(directory, 0o700)
    _sync_directory(directory)


def import_archive(archive: Path, rename: str | None = None, *, leases: ProfileLeases, owners: ProfileOwners,
                   proxy: str | None = None, native_binary: str | Path | None = None,
                   runtime_adapters: RuntimeAdapters | None = None, proxy_secrets: ProxySecrets | None = None,
                   artifact_content: ArtifactContent | None = None) -> str:
    store().mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=store())).resolve()
    cfg: dict[str, Any] = {}
    try:
        extracted, cfg, manifest = _extract_archive(archive, staging)
        if cfg["schema"] != SCHEMA:
            raise ProfileError("incompatible_profile: migrate the source profile before import")
        _validate_runtime(cfg, runtime_adapters=runtime_adapters, check_artifacts=False,
                          artifact_content=artifact_content)
        dest_name = sanitize(rename or cfg["name"])
        with exclusive(dest_name, kind="spawn", leases=leases) as serialized, exclusive(dest_name, leases=leases) as owned, exclusive("catalog", kind="catalog", leases=leases) as catalog:
            if not serialized or not owned or not catalog:
                raise ProfileError(f"busy: {dest_name}")
            dest = profile_dir(dest_name)
            if dest.exists() or dest.is_symlink():
                raise ProfileError("already_exists: destination environment exists")
            cfg["name"] = dest_name
            cfg.setdefault("storage", {})["path"] = str((dest / "data").resolve())
            from .native_spec import check_identity_collision, file_sha256
            check_identity_collision(cfg, [load_config(row["name"]) or {} for row in list_profiles(owners=owners)])
            bindings = manifest.get("font_bindings")
            fonts = cfg.get("asset_paths", {}).get("fonts", [])
            if not isinstance(bindings, dict) or set(bindings) != set(fonts):
                raise ProfileError("archive_integrity: complete bundled font bindings are required")
            rebound = []
            for original in fonts:
                relative = bindings[original]
                if not isinstance(relative, str) or not re.fullmatch(r"\.assets/fonts/[0-9a-f]{64}", relative) or relative not in manifest["files"] or file_sha256(extracted / relative, artifact_content=artifact_content) != relative.rsplit("/", 1)[1]:
                    raise ProfileError("archive_integrity: invalid bundled font binding")
                rebound.append(str((extracted / relative).resolve()))
            cfg["asset_paths"]["fonts"] = sorted(set(rebound))
            if native_binary is not None:
                binary = Path(native_binary).expanduser().resolve()
                if file_sha256(binary, artifact_content=artifact_content) != cfg["runtime"]["artifact_sha256"]:
                    raise ProfileError("incompatible_profile: replacement runtime must have identical pinned bytes")
                cfg["runtime"]["binary"] = str(binary)
            _validate_managed(cfg, runtime_adapters=runtime_adapters, artifact_content=artifact_content)
            cfg["asset_paths"]["fonts"] = sorted({str(dest / Path(path).relative_to(extracted)) for path in rebound})
            credential = _read(extracted / ".proxy-credentials.json")
            if manifest.get("credentials_included") and credential is None:
                raise ProfileError("archive_integrity: invalid credential payload")
            if credential is not None and (credential.get("id") != cfg["id"] or not isinstance(credential.get("proxy"), str)):
                raise ProfileError("archive_integrity: credential identity mismatch")
            secret_changed = False
            secret_backup = None
            try:
                if proxy is not None or credential is not None:
                    selected = proxy if proxy is not None else credential["proxy"]
                    installs_secret = bool(selected and selected.strip().upper() not in {"DIRECT", "NONE"} and not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", selected))
                    owns_secret = cfg.get("network", {}).get("proxy_ref") == _proxy_ref(cfg)
                    if installs_secret:
                        _validate_proxy_url(selected, cfg)
                    if installs_secret or owns_secret:
                        secret_backup = _secret_authority(proxy_secrets).read(cfg["id"])
                        secret_changed = True
                    configure_proxy(cfg, selected, proxy_secrets=proxy_secrets)
                _write(extracted / "config.json", cfg)
                (extracted / ARCHIVE_MANIFEST).unlink()
                (extracted / ".proxy-credentials.json").unlink(missing_ok=True)
                _sync_tree(extracted)
                os.replace(extracted, dest)
            except BaseException:
                if secret_changed:
                    authority = _secret_authority(proxy_secrets)
                    if secret_backup is None:
                        authority.delete(cfg["id"])
                    else:
                        authority.write(cfg["id"], secret_backup)
                raise
            _sync_directory(store())
        return dest_name
    finally:
        shutil.rmtree(staging, ignore_errors=True)




def migrate(name: str, *, leases: ProfileLeases, owners: ProfileOwners, clock: WallClock, directories: AtomicDirectories,
            snapshots: SnapshotDirectories, binary: str | Path | None = None, overrides: dict[str, Any] | None = None,
            assets: dict[str, list[str]] | None = None, rollback: str | Path | None = None,
            runtime_adapters: RuntimeAdapters | None = None, proxy_secrets: ProxySecrets | None = None,
            artifact_content: ArtifactContent | None = None) -> dict[str, Any]:
    name = sanitize(name)
    with exclusive(name, kind="spawn", leases=leases) as serialized, exclusive(name, leases=leases) as owned, exclusive("catalog", kind="catalog", leases=leases) as catalog:
        if not serialized or not owned or not catalog or owners.alive(BASE, name):
            raise ProfileError(f"busy: stop {name} before migration")
        cfg = require(name)
        directory = profile_dir(name)
        if cfg.get("mode") != "managed" or cfg.get("schema") not in (2, SCHEMA):
            raise ProfileError("incompatible_profile: migration requires managed schema2/3")
        if rollback is not None:
            if binary is not None or overrides is not None or assets is not None:
                raise ProfileError("invalid_config: rollback cannot change runtime, overrides or assets")
            staging = Path(tempfile.mkdtemp(prefix=".rollback-", dir=store())).resolve()
            try:
                extracted, previous, manifest = _extract_archive(Path(rollback).expanduser(), staging)
                if previous.get("name") != name or manifest.get("snapshot_identity") != cfg.get("id") or previous.get("runtime", {}).get("id", "camoufox") != cfg.get("runtime", {}).get("id", "camoufox"):
                    raise ProfileError("incompatible_profile: snapshot belongs to a different environment/runtime")
                if previous.get("storage", {}).get("format_version", 1) != cfg.get("storage", {}).get("format_version", 1):
                    raise ProfileError("incompatible_profile: snapshot storage format differs")
                if previous.get("schema") == SCHEMA:
                    _validate_managed(previous, storage=directory, runtime_adapters=runtime_adapters,
                                      artifact_content=artifact_content)
                    if previous["id"] != cfg.get("id"):
                        raise ProfileError("incompatible_profile: snapshot environment id changed")
                    from .native_spec import check_identity_collision
                    check_identity_collision(previous, [load_config(row["name"]) or {} for row in list_profiles(owners=owners) if row["name"] != name])
                configuration_digest = manifest["files"]["config.json"]["sha256"]
                credential = _read(extracted / ".proxy-credentials.json")
                if manifest.get("credentials_included") and credential is None:
                    raise ProfileError("archive_integrity: invalid snapshot credential payload")
                if credential is not None and (credential.get("id") != previous.get("id") or not isinstance(credential.get("proxy"), str)):
                    raise ProfileError("archive_integrity: snapshot credential identity mismatch")
                (extracted / ".proxy-credentials.json").unlink(missing_ok=True)
                (extracted / ARCHIVE_MANIFEST).unlink()
                _sync_tree(extracted)
                backup = None
                owned_credential = bool(previous.get("id") and previous.get("network", {}).get("proxy_ref") == _proxy_ref(previous))
                remove_current = bool(not previous.get("network", {}).get("proxy_ref") and cfg.get("id") and cfg.get("network", {}).get("proxy_ref") == _proxy_ref(cfg))
                secret_cfg = previous if owned_credential else cfg
                if owned_credential or remove_current:
                    if owned_credential and credential is not None:
                        _validate_proxy_url(credential["proxy"], previous)
                    backup = read_proxy_secret(secret_cfg, proxy_secrets=proxy_secrets)
                try:
                    if owned_credential or remove_current:
                        if not owned_credential or credential is None:
                            clear_proxy_secret(secret_cfg, proxy_secrets=proxy_secrets)
                        else:
                            _secret_authority(proxy_secrets).write(previous["id"], credential["proxy"])
                    try:
                        directories.exchange(directory, extracted)
                    except AtomicExchangeUnavailable as exc:
                        raise ProfileError("capability_unavailable: atomic directory exchange is unavailable") from exc
                except BaseException:
                    if owned_credential or remove_current:
                        if backup is None:
                            clear_proxy_secret(secret_cfg, proxy_secrets=proxy_secrets)
                        else:
                            _secret_authority(proxy_secrets).write(secret_cfg["id"], backup)
                    raise
                _sync_directory(store())
                # Secret changes were committed before the tree exchange.
                return {"status": "ok", "environment": name, "revision": previous.get("revision", 1), "rolled_back": True, "configuration_sha256": configuration_digest}
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        if cfg.get("storage", {}).get("format_version", 1) != 1 or cfg.get("storage", {}).get("path", str((directory / "data").resolve())) != str((directory / "data").resolve()):
            raise ProfileError("incompatible_profile: storage format/path is not owned and compatible")
        runtime = cfg.get("runtime", {}).get("id", "camoufox")
        inline_proxy = cfg.get("network", {}).get("proxy") if cfg.get("schema") == 2 else None
        from .native_spec import check_identity_collision
        if runtime in NATIVE_RUNTIMES:
            from .native_spec import migrate_native_spec
            result = migrate_native_spec(cfg, binary=binary, overrides=overrides, assets=assets,
                                         artifact_content=artifact_content)
        else:
            if assets is not None:
                raise ProfileError("invalid_config: asset migration requires a native runtime")
            result = _runtime_adapter(runtime, runtime_adapters).migrate(
                cfg, name=name, binary=binary, overrides=overrides, inline_proxy=inline_proxy)
        with (directory / "config.json").open("rb") as stream:
            result["previous_environment_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        _validate_managed(result, storage=directory, runtime_adapters=runtime_adapters,
                          artifact_content=artifact_content)
        if inline_proxy and inline_proxy.strip().upper() not in {"DIRECT", "NONE", ""}:
            _validate_proxy_url(inline_proxy, result)
        check_identity_collision(result, [load_config(row["name"]) or {} for row in list_profiles(owners=owners) if row["name"] != name])
        timestamp = clock.unix_time_ns()
        if type(timestamp) is not int:
            raise ProfileError("capability_unavailable: invalid Host wall clock value")
        try:
            snapshot_dir = snapshots.prepare(str(cfg.get("id", name)))
        except UnsafeSnapshotStore as exc:
            raise ProfileError(f"unsafe_snapshot_store: {exc}") from exc
        snapshot = snapshot_dir / f"revision-{cfg.get('revision', 1)}-{timestamp}.atb.tar.gz"
        _archive(directory, cfg, snapshot, bundle_fonts=False, snapshot_identity=result["id"], include_credentials=True, proxy_secrets=proxy_secrets,
                 artifact_content=artifact_content)
        backup = None
        installed_inline = bool(inline_proxy and inline_proxy.strip().upper() not in {"DIRECT", "NONE", ""})
        if installed_inline:
            probe = copy.deepcopy(result)
            probe["network"]["proxy_ref"] = _proxy_ref(result)
            backup = read_proxy_secret(probe, proxy_secrets=proxy_secrets)
        try:
            if inline_proxy:
                configure_proxy(result, inline_proxy, proxy_secrets=proxy_secrets)
            save_config(name, result)
        except BaseException:
            if installed_inline and load_config(name) != result:
                if backup is None:
                    clear_proxy_secret(result, proxy_secrets=proxy_secrets)
                else:
                    _secret_authority(proxy_secrets).write(result["id"], backup)
            raise
        with snapshot.open("rb") as stream:
            snapshot_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"status": "ok", "environment": name, "revision": result["revision"], "previous_revision": cfg.get("revision", 1), "snapshot": str(snapshot), "snapshot_sha256": snapshot_digest, "configuration_sha256": hashlib.sha256(canonical_bytes(result)).hexdigest()}
