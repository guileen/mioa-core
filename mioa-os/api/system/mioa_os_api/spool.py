"""Profile spool authority for the existing trusted-local enrollment binding.

The connected endpoint is a local transport detail, not a portable grant or
proof of isolation. The supervisor binds each endpoint to its principal.
"""
from socket import socket
from typing import Protocol


class Spool(Protocol):
    """Owned seekable temporary storage and its Host resource reservations.

    Reserve before writing. discard_file closes backing storage without
    releasing tickets when associated database cleanup is uncertain. close
    closes backing storage first and only then releases tickets; a failed
    release has an unknown effect and must not be retried automatically.
    """
    def reserve(self, additional_bytes: int) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, count: int) -> bytes: ...
    def seek(self, offset: int) -> int: ...
    def discard_file(self) -> None: ...
    def close(self) -> None: ...


class SpoolGrants(Protocol):
    """One local grant-channel registry, owned by the Profile service.

    bind transfers an already-authenticated endpoint for one principal; EOF
    is not evidence that outstanding allocations were released. close and
    unbind close channels and do not infer release of uncertain reservations.
    close attempts every channel before reporting the first failure; a failed
    channel close has unknown effect and is not retried.
    """
    def bind(self, principal: int, endpoint: socket) -> None: ...
    def open_spool(self, principal: int) -> Spool: ...
    def unbind(self, principal: int) -> None: ...
    def close(self) -> None: ...
