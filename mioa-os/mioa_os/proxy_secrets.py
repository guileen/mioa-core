"""Trusted-local, explicitly rooted proxy credential authority."""
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from mioa_os_api import ProxySecretError


class LocalProxySecrets:
    """Environment-first lookup and owner-only atomic credential replacement."""

    def __init__(self, root: str | Path):
        self.root = Path(os.path.abspath(os.fspath(root)))

    def _path(self, identity: str, *, create: bool = False) -> Path:
        if not isinstance(identity, str) or not identity:
            raise ProxySecretError("invalid_config", "proxy credentials require an immutable environment id")
        directory = self.root / ".secrets" / "proxies"
        for path in (self.root, self.root / ".secrets", directory):
            if create:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.exists() and not path.is_symlink():
                break
            details = path.lstat()
            if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or (path != self.root and stat.S_IMODE(details.st_mode) != 0o700):
                raise ProxySecretError("unsafe_secret_store", "private credential directory is not owner-only")
            if create and path != self.root:
                self._sync(path.parent)
        key = hashlib.sha256(identity.encode()).hexdigest().upper()
        return directory / ("ANTI_TRACE_PROXY_" + key + ".json")

    @staticmethod
    def _sync(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def resolve(self, name: str, identity: str | None) -> str | None:
        value = os.environ.get(name)
        return self.read(identity) if value is None and identity is not None else value

    def read(self, identity: str) -> str | None:
        path = self._path(identity)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProxySecretError("unsafe_secret_store", "cannot open owned credential") from exc
        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1:
                raise ProxySecretError("unsafe_secret_store", "credential is not an owner-only regular file")
            try:
                value = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise ProxySecretError("invalid_config", "corrupt private credential") from exc
        if not isinstance(value, dict) or value.get("id") != identity or not isinstance(value.get("proxy"), str):
            raise ProxySecretError("invalid_config", "corrupt private credential")
        return value["proxy"]

    def write(self, identity: str, value: str) -> None:
        path = self._path(identity, create=True)
        data = (json.dumps({"id": identity, "proxy": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync(path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            Path(temporary).unlink(missing_ok=True)

    def delete(self, identity: str) -> None:
        path = self._path(identity)
        try:
            path.unlink()
        except FileNotFoundError:
            try:
                self._sync(path.parent)
            except FileNotFoundError:
                pass
        else:
            self._sync(path.parent)
