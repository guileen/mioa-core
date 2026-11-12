"""Trusted-local snapshot directory preparation; not an isolation boundary."""
import hashlib
import os
from pathlib import Path
import stat

from mioa_os_api.snapshots import UnsafeSnapshotStore


class LocalSnapshotDirectories:
    def __init__(self, profile_root: Path):
        self.root = Path(profile_root)

    def prepare(self, profile_identity: str) -> Path:
        snapshots = self.root / "snapshots"
        directory = snapshots / hashlib.sha256(profile_identity.encode()).hexdigest()
        for path in (snapshots, directory):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            details = path.lstat()
            if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
                raise UnsafeSnapshotStore("snapshots must be manager-owned directories")
            os.chmod(path, 0o700)
        return directory
