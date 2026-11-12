"""Ambient Unix channel readiness for an explicitly trusted local Host."""
import selectors
import socket

from pathlib import PurePath


class LocalUnixChannels:
    """Observe caller-owned Unix stream handles; this grants no authorization."""

    def __init__(self) -> None:
        self._selector = selectors.DefaultSelector()

    def pair(self) -> tuple[socket.socket, socket.socket]:
        return socket.socketpair()

    def register(self, channel: socket.socket, *, writable: bool = False) -> None:
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if writable else 0)
        self._selector.register(channel, events)

    def modify(self, channel: socket.socket, *, writable: bool) -> None:
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if writable else 0)
        self._selector.modify(channel, events)

    def unregister(self, channel: socket.socket) -> None:
        self._selector.unregister(channel)

    def select(self) -> list[tuple[socket.socket, bool, bool]]:
        ready = []
        for key, mask in self._selector.select():
            ready.append((key.fileobj, bool(mask & selectors.EVENT_READ),
                          bool(mask & selectors.EVENT_WRITE)))
        return ready

    def peek(self, channel: socket.socket, size: int) -> bytes:
        return channel.recv(size, socket.MSG_PEEK)

    def receive_available(self, channel: socket.socket, size: int) -> bytes | None:
        channel.setblocking(True)
        try:
            return channel.recv(size, socket.MSG_DONTWAIT)
        except BlockingIOError:
            return None
        finally:
            channel.settimeout(1.0)

    def shutdown(self, channel: socket.socket) -> None:
        channel.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self._selector.close()
