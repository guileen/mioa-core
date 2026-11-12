"""Trusted-local framed descriptor transfer; ownership is all-or-nothing."""
from __future__ import annotations
import array
import contextlib
import os
import socket
import stat
import struct
from collections.abc import Sequence


class _FrameReader:
    def __init__(self, channel: socket.socket, max_bytes: int, max_descriptors: int):
        self.channel = channel
        self.max_bytes = max_bytes
        self.max_descriptors = max_descriptors
        self.buffer = bytearray()
        self.length: int | None = None
        self.raw: list[int] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True
        raw, self.raw = self.raw, []
        _close_raw(raw)
        self.buffer.clear()

    def read(self, flags: int = 0) -> tuple[bytes, list[int]] | None:
        if self.closed:
            raise ValueError("Unix message reader is closed")
        itemsize = array.array("i").itemsize
        # XNU installs undisclosed rights on ancillary truncation. Receive its
        # kernel batch ceiling (512; Linux SCM_MAX_FD is 253) before bounds checks.
        ancillary_size = socket.CMSG_SPACE(512 * itemsize)
        try:
            while True:
                remaining = (4 if self.length is None else self.length) - len(self.buffer)
                block, ancillary, received_flags, _ = self.channel.recvmsg(
                    remaining, ancillary_size, flags)
                invalid = False
                previous_count = len(self.raw)
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        values = array.array("i")
                        values.frombytes(data[:len(data) - len(data) % itemsize])
                        self.raw.extend(values)
                        invalid |= len(data) % itemsize != 0
                    else:
                        invalid = True
                if invalid or received_flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                    raise ValueError("invalid or truncated Unix ancillary data")
                if len(self.raw) > self.max_descriptors:
                    raise ValueError("too many Unix descriptors")
                for index in range(previous_count, len(self.raw)):
                    os.set_inheritable(self.raw[index], False)
                if not block:
                    raise EOFError("Unix message disconnected")
                self.buffer.extend(block)
                if len(self.buffer) < (4 if self.length is None else self.length):
                    continue
                if self.length is None:
                    self.length, = struct.unpack("!I", self.buffer)
                    self.buffer.clear()
                    if not 0 < self.length <= self.max_bytes:
                        raise ValueError("Unix message size outside bound")
                    continue
                body = bytes(self.buffer)
                raw, self.raw = self.raw, []
                self.buffer.clear()
                self.length = None
                return body, raw
        except BlockingIOError:
            if flags & socket.MSG_DONTWAIT:
                return None
            self.close()
            raise
        except BaseException:
            self.close()
            raise


def _receive_frame(channel: socket.socket, max_bytes: int, max_descriptors: int) -> tuple[bytes, list[int]]:
    reader = _FrameReader(channel, max_bytes, max_descriptors)
    try:
        frame = reader.read()
        assert frame is not None
        return frame
    finally:
        reader.close()


def _close_raw(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        with contextlib.suppress(OSError):
            os.close(descriptor)


class _ReceivedFile:
    def __init__(self, descriptor: int):
        self._descriptor = descriptor

    def fileno(self) -> int:
        if self._descriptor < 0:
            raise ValueError("received file is closed")
        return self._descriptor

    def close(self) -> None:
        if self._descriptor >= 0:
            descriptor, self._descriptor = self._descriptor, -1
            os.close(descriptor)


def _adopt_sockets(body: bytes, raw: list[int]) -> tuple[bytes, list[socket.socket]]:
    owned: list[socket.socket] = []
    try:
        while raw:
            # Explicit family/type avoids implicit probing before adoption.
            endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=raw[-1])
            raw.pop()
            owned.append(endpoint)
            if (endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                    or not isinstance(endpoint.getsockname(), (str, bytes))
                    or not isinstance(endpoint.getpeername(), (str, bytes))):
                raise ValueError("descriptor is not a connected Unix stream")
            os.set_inheritable(endpoint.fileno(), False)
        owned.reverse()
        return body, owned
    except BaseException:
        for endpoint in owned:
            with contextlib.suppress(OSError):
                endpoint.close()
        _close_raw(raw)
        raise


class _SocketReader:
    def __init__(self, channel: socket.socket, max_bytes: int, max_sockets: int):
        if channel.gettimeout() not in (None, 0):
            raise ValueError("incremental Unix receive requires timeout None or 0")
        self._frame = _FrameReader(channel, max_bytes, max_sockets)

    def read_available(self) -> tuple[bytes, list[socket.socket]] | None:
        try:
            if self._frame.channel.gettimeout() not in (None, 0):
                raise ValueError("incremental Unix receive requires timeout None or 0")
            frame = self._frame.read(socket.MSG_DONTWAIT)
            return None if frame is None else _adopt_sockets(*frame)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._frame.close()


def _socket_bounds(max_bytes: int, max_sockets: int) -> None:
    if (type(max_bytes) is not int or not 1 <= max_bytes <= 1048576
            or type(max_sockets) is not int or not 0 <= max_sockets <= 4):
        raise ValueError("Unix message bounds outside supported range")

def _send_frame(channel: socket.socket, body: bytes, descriptors: list[int],
                max_bytes: int) -> None:
    if not isinstance(body, bytes) or not 1 <= len(body) <= max_bytes:
        raise ValueError("Unix message size outside bound")
    packet = struct.pack("!I", len(body)) + body
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))] if descriptors else []
    sent = channel.sendmsg([packet], ancillary)
    if sent <= 0:
        raise EOFError("Unix descriptor transfer made no progress")
    if sent < len(packet):
        channel.sendall(packet[sent:])


def _file_bounds(max_bytes: int, max_files: int) -> None:
    if (type(max_bytes) is not int or not 1 <= max_bytes <= 1048576
            or type(max_files) is not int or not 0 <= max_files <= 2):
        raise ValueError("Unix file message bounds outside supported range")


class LocalUnixMessages:
    def send_sockets(self, channel: socket.socket, body: bytes,
                     endpoints: Sequence[socket.socket], *, max_bytes: int) -> None:
        _socket_bounds(max_bytes, len(endpoints))
        _send_frame(channel, body, [endpoint.fileno() for endpoint in endpoints], max_bytes)

    def send_files(self, channel: socket.socket, body: bytes,
                   files: Sequence[int | _ReceivedFile], *, max_bytes: int) -> None:
        _file_bounds(max_bytes, len(files))
        _send_frame(channel, body, [file if isinstance(file, int) else file.fileno()
                                    for file in files], max_bytes)

    def reader(self, channel: socket.socket, *, max_bytes: int,
               max_sockets: int) -> _SocketReader:
        _socket_bounds(max_bytes, max_sockets)
        return _SocketReader(channel, max_bytes, max_sockets)

    def receive(self, channel: socket.socket, *, max_bytes: int,
                max_sockets: int) -> tuple[bytes, list[socket.socket]]:
        _socket_bounds(max_bytes, max_sockets)
        return _adopt_sockets(*_receive_frame(channel, max_bytes, max_sockets))

    def receive_files(self, channel: socket.socket, *, max_bytes: int,
                      max_files: int) -> tuple[bytes, list[_ReceivedFile]]:
        _file_bounds(max_bytes, max_files)
        body, raw = _receive_frame(channel, max_bytes, max_files)
        owned: list[_ReceivedFile] = []
        try:
            while raw:
                descriptor = raw[-1]
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("received descriptor is not a regular file")
                os.set_inheritable(descriptor, False)
                received = _ReceivedFile(descriptor)
                raw.pop()
                owned.append(received)
            owned.reverse()
            return body, owned
        except BaseException:
            for received in owned:
                with contextlib.suppress(OSError):
                    received.close()
            _close_raw(raw)
            raise
