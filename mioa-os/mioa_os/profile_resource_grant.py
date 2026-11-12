"""Per-principal grants from the host's resource-owner executor.

The socket capability is supplied by the trusted supervisor at enrollment; no
principal is sent on the wire. Disk charges are logical reservations, not a
filesystem quota or a measurement of SQLite page, journal, or cache overhead.
"""
from __future__ import annotations

import socket
import struct
import tempfile
import time
from typing import BinaryIO

_REQUEST = struct.Struct('!4sIQQQQQ')
_REPLY = struct.Struct('!4sIQQQ')
_BUFFER_BYTES = 1024 * 1024
_DISK_BLOCK = 1024 * 1024
_TIMEOUT = 2.0
_MAX_U64 = (1 << 64) - 1


class _Channel:
    def __init__(self, endpoint: socket.socket):
        self.endpoint = endpoint
        self.endpoint.settimeout(_TIMEOUT)
        self.sequence = 0
        self.failed = False

    def request(self, op: int, token: int = 0, host: int = 0,
                handles: int = 0, disk: int = 0) -> int:
        if self.failed or self.sequence == _MAX_U64:
            raise OSError('profile resource grant channel unavailable; effect unknown')
        self.sequence += 1
        seq = self.sequence
        try:
            deadline = time.monotonic() + _TIMEOUT
            self.endpoint.settimeout(_TIMEOUT)
            self.endpoint.sendall(_REQUEST.pack(b'PGR1', op, seq, token, host, handles, disk))
            data = bytearray()
            while len(data) < _REPLY.size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('profile grant deadline exceeded')
                self.endpoint.settimeout(remaining)
                part = self.endpoint.recv(_REPLY.size - len(data))
                if not part:
                    raise EOFError('profile grant EOF')
                data.extend(part)
            magic, status, received, result, reserved = _REPLY.unpack(data)
            if magic != b'PGR1' or received != seq or reserved or (status == 0 and
                    (not result or (op == 2 and result != token))) or (status and result):
                raise ValueError('invalid profile grant reply')
        except (OSError, EOFError, ValueError, struct.error) as exc:
            self.failed = True
            raise OSError('profile resource grant channel lost; effect unknown') from exc
        if status:
            if status in (4, 5, 6):
                raise ValueError('profile resource grant limit exceeded')
            self.failed = True
            raise OSError('profile resource grant rejected; effect unknown')
        return result

    def close(self) -> None:
        self.failed = True
        self.endpoint.close()


class _Spool:
    def __init__(self, channel: _Channel, file: BinaryIO, initial: int):
        self._channel = channel
        self._file = file
        self._available = 0
        self._tickets = [initial]
        self._closed = False

    def reserve(self, additional_bytes: int) -> None:
        if self._closed or type(additional_bytes) is not int or not 0 <= additional_bytes <= _MAX_U64:
            raise ValueError('invalid profile spool reservation')
        if additional_bytes > self._available:
            needed = additional_bytes - self._available
            amount = max(_DISK_BLOCK, needed)
            self._tickets.append(self._channel.request(1, disk=amount))
            self._available += amount
        self._available -= additional_bytes

    def write(self, data: bytes) -> int:
        return self._file.write(data)

    def read(self, count: int) -> bytes:
        return self._file.read(count)

    def seek(self, offset: int) -> int:
        return self._file.seek(offset)

    def discard_file(self) -> None:
        self._file.close()

    def close(self) -> None:
        if self._closed:
            return
        # A failed close does not prove descriptor or backing disposal.
        self.discard_file()
        self._closed = True
        # A lost reply is uncertain: never retry or infer release from EOF.
        while self._tickets:
            self._channel.request(2, token=self._tickets[-1])
            self._tickets.pop()


class ResourceGrants:
    def __init__(self):
        self._channels: dict[int, _Channel] = {}

    def bind(self, principal: int, endpoint: socket.socket) -> None:
        if principal in self._channels:
            raise ValueError('profile grant principal already enrolled')
        self._channels[principal] = _Channel(endpoint)

    def open_spool(self, principal: int) -> _Spool:
        channel = self._channels.get(principal)
        if channel is None:
            raise ValueError('authenticated storage spool grant unavailable')
        initial = channel.request(1, host=_BUFFER_BYTES, handles=1)
        try:
            file = tempfile.TemporaryFile(mode='w+b')
        except BaseException:
            channel.request(2, token=initial)
            raise
        return _Spool(channel, file, initial)

    def unbind(self, principal: int) -> None:
        channel = self._channels.pop(principal, None)
        if channel is not None:
            channel.close()

    def close(self) -> None:
        failure = None
        for principal in list(self._channels):
            try:
                self.unbind(principal)
            except BaseException as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note(f"additional grant channel close failure: {error!r}")
        if failure is not None:
            raise failure
