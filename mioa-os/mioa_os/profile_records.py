"""Atomic Profile record publication for an explicitly trusted local Host."""
import os
from pathlib import Path
import tempfile

from mioa_os_api.profile_records import ProfileCommitUncertain, ProfileRecord


class LocalProfileRecordWriter:
    """Bind an existing Profile directory; not an authorization or sandbox boundary.

    The Host is responsible for the directory, access control, and Profile lease.
    Concurrent directory replacement is not prevented by this local adapter.
    """

    def __init__(self, profile_directory: Path):
        self._directory = Path(profile_directory)

    def write(self, record: ProfileRecord, data: bytes) -> None:
        if record not in ("config", "state"):
            raise ValueError("unknown Profile record")
        if not isinstance(data, bytes):
            raise TypeError("Profile record must be bytes")
        target = self._directory / (record + ".json")
        descriptor, temporary = tempfile.mkstemp(prefix="." + target.name + ".", dir=self._directory)
        attempted = False
        try:
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                attempted = True
                os.replace(temporary, target)
                directory = os.open(self._directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                Path(temporary).unlink(missing_ok=True)
        except OSError as exc:
            if attempted:
                raise ProfileCommitUncertain("Profile record publication may have occurred") from exc
            raise
