"""Original browser image resources in one session-owned private directory."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any


class IconAssets:
    def __init__(self, parent: Path):
        # FrameRing already validated and locked the mode-0700 session parent.
        # A fresh child prevents cleanup from touching any pre-existing files.
        self.path = Path(tempfile.mkdtemp(prefix="site-icons-", dir=parent))
        self._files: dict[str, Path] = {}

    def publish(self, icon: Any) -> dict[str, str] | None:
        if icon is None:
            return None
        data, digest, mime = icon.data, icon.sha256, icon.mime
        if (not isinstance(data, bytes) or not 0 < len(data) <= 512 * 1024
                or mime not in {"image/png", "image/x-icon"}
                or not isinstance(digest, str)):
            raise ValueError("icon must contain bounded original bytes and their exact SHA-256")
        path = self._files.get(digest)
        if path is None:
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("icon resource digest disagrees with its original bytes")
            path = self.path / digest
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            self._files[digest] = path
        return {"path": str(path), "sha256": digest, "mime": mime}

    def retain(self, digests: set[str]) -> None:
        for digest in tuple(self._files):
            if digest not in digests:
                self._files[digest].unlink(missing_ok=True)
                del self._files[digest]

    def close(self) -> None:
        self.retain(set())
        self.path.rmdir()
