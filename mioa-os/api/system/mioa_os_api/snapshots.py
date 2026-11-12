"""Host authority for private Profile migration snapshot locations."""
from pathlib import Path
from typing import Protocol


class UnsafeSnapshotStore(OSError):
    """The Host rejected a snapshot directory as unsafe."""


class SnapshotDirectories(Protocol):
    """Prepare a private snapshot directory for one stable Profile identity.

    The Host binds the root and decides whether the requester is authorized.
    Returns a location usable by the granted storage backend. This interface
    alone does not enforce isolation or protect against concurrent replacement.
    """

    def prepare(self, profile_identity: str) -> Path: ...
