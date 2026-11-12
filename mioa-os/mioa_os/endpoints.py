"""Private short Unix-socket pathname leases; no process-exit inference."""
import contextlib
import errno
import os
from pathlib import Path
import stat
import tempfile


class _UnixEndpointLease:
    __slots__ = ("_directory", "_path", "_disposed")

    def __init__(self):
        # HOME/TMPDIR may exceed sun_path; persistent Profile data stays elsewhere.
        self._directory = Path(tempfile.mkdtemp(prefix="mioa-cdp-", dir="/tmp"))
        self._path = self._directory / "cdp.sock"
        self._disposed = False
        if len(os.fsencode(self._path)) >= 104:
            self.dispose()
            raise OSError(errno.ENAMETOOLONG, "Unix endpoint exceeds supported address limit")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def directory(self) -> Path:
        return self._directory

    def dispose(self) -> None:
        if self._disposed:
            return
        try:
            mode = self._path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(mode):
                raise OSError(errno.EINVAL, "endpoint pathname is not an owned socket")
            self._path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self._directory.rmdir()
        self._disposed = True


class LocalUnixEndpoints:
    """Host factory, serialized by the caller; no ambient path or tree-delete API."""

    def create(self) -> _UnixEndpointLease:
        return _UnixEndpointLease()
