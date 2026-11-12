"""Ambient directory/whole-file operations for an explicitly trusted Host."""
from pathlib import Path, PurePath
import stat


class LocalHostFiles:
    """Operate on Core-owned paths; this does not authorize or isolate them."""

    def make_directory(self, path: str | PurePath, *, mode: int, parents: bool = False) -> None:
        Path(path).mkdir(mode=mode, parents=parents, exist_ok=True)

    def read_bytes(self, path: str | PurePath) -> bytes:
        return Path(path).read_bytes()

    def write_bytes(self, path: str | PurePath, data: bytes) -> None:
        Path(path).write_bytes(data)

    def exists(self, path: str | PurePath) -> bool:
        return Path(path).exists()

    def is_symlink(self, path: str | PurePath) -> bool:
        return Path(path).is_symlink()

    def is_file(self, path: str | PurePath) -> bool:
        return Path(path).is_file()

    def size(self, path: str | PurePath) -> int:
        location = Path(path)
        value = location.stat()
        if not stat.S_ISREG(value.st_mode):
            raise OSError(f"not a regular file: {location}")
        return value.st_size

    def remove(self, path: str | PurePath, *, missing_ok: bool = False) -> None:
        Path(path).unlink(missing_ok=missing_ok)
