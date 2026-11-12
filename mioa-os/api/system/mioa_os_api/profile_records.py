"""Host-bound Profile record publication; no pathname authority is exposed."""
from typing import Literal, Protocol

ProfileRecord = Literal["config", "state"]


class ProfileCommitUncertain(OSError):
    """Publication may have happened; callers must reconcile before retrying."""


class ProfileRecordWriter(Protocol):
    """Write one record within a Profile selected and authorized by its Host.

    Success means an atomic replacement followed by the backend's file and
    containing-directory synchronization. It is not an isolation guarantee or
    proof against hardware failure. The caller must hold the Profile lease.
    ProfileCommitUncertain means publication may have happened; never infer
    failure or retry without reconciliation. Other errors before publication
    leave the previous record intact. Cancellation/crash outcomes are unknown.
    """

    def write(self, record: ProfileRecord, data: bytes) -> None: ...
