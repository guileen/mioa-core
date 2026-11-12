"""Ambient artifact reads for an explicitly trusted local Host."""
import hashlib
import os
from pathlib import Path, PurePath
import stat


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class LocalArtifactContent:
    """Read selected files; this does not authorize or pin their bytes."""

    def resolve(self, path: str | PurePath) -> str:
        return str(Path(path).expanduser().resolve())

    def executable(self, path: str | PurePath) -> str:
        location = Path(path).expanduser().resolve()
        mode = location.stat().st_mode
        if not stat.S_ISREG(mode) or not os.access(location, os.X_OK):
            raise OSError(f"native executable missing or not executable: {location}")
        return str(location)

    def fingerprint(self, path: str | PurePath) -> tuple[int, int, int, int, int]:
        return _identity(Path(path).stat())

    def size(self, path: str | PurePath) -> int:
        location = Path(path)
        value = location.stat()
        if not stat.S_ISREG(value.st_mode):
            raise OSError(f"not a regular file: {location}")
        return value.st_size

    def digest(self, path: str | PurePath) -> str:
        location = Path(path)
        with open(location, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"not a regular file: {location}")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        if _identity(before) != _identity(after):
            raise OSError(f"pinned artifact changed while hashing: {location}")
        return digest

    def read(self, path: str | PurePath, offset: int, length: int) -> bytes:
        location = Path(path)
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0:
            raise OSError(f"invalid artifact read bounds: {location}")
        with open(location, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or offset + length > before.st_size:
                raise OSError(f"invalid artifact read bounds: {location}")
            stream.seek(offset)
            value = stream.read(length)
            after = os.fstat(stream.fileno())
        if len(value) != length or _identity(before) != _identity(after):
            raise OSError(f"pinned artifact changed while reading: {location}")
        return value
