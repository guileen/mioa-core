"""Ambient loopback/unix stream connections for an explicitly trusted Host."""
import asyncio
import socket
from typing import Any


class LocalStreamConnections:
    """Connect or adopt streams; this does not authorize a destination or peer."""

    async def connect_tcp(self, host: str, port: int, *, limit: int):
        return await asyncio.open_connection(host, port, limit=limit)

    async def adopt_unix(self, channel: Any, *, limit: int):
        channel.setblocking(False)
        return await asyncio.open_connection(sock=channel, limit=limit)

    def reserve_loopback_port(self) -> int:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            return reservation.getsockname()[1]
