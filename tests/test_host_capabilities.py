"""Real local streams and whole-file operations behind Host capabilities."""
import asyncio
import socket
import stat
import tempfile
from pathlib import Path
import unittest

from mioa_os import LocalHostFiles, LocalStreamConnections, LocalUnixChannels


class HostFilesTests(unittest.TestCase):
    def test_directory_and_whole_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = LocalHostFiles()
            root = Path(temporary) / "a" / "b"
            files.make_directory(root, mode=0o700, parents=True)
            self.assertTrue(root.is_dir())
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            target = root / "config.json"
            files.write_bytes(target, b"{}")
            self.assertTrue(files.exists(target))
            self.assertEqual(files.read_bytes(target), b"{}")
            files.remove(target)
            self.assertFalse(files.exists(target))
            files.remove(target, missing_ok=True)
            with self.assertRaises(OSError):
                files.remove(target)


class UnixChannelsTests(unittest.TestCase):
    def test_pair_readiness_peek_and_available_receive(self):
        channels = LocalUnixChannels()
        left, right = channels.pair()
        try:
            channels.register(left)
            right.sendall(b"abc")
            self.assertEqual(channels.select(), [(left, True, False)])
            self.assertEqual(channels.peek(left, 3), b"abc")
            self.assertEqual(channels.receive_available(left, 3), b"abc")
            self.assertIsNone(channels.receive_available(left, 3))
            channels.modify(left, writable=True)
            self.assertEqual(channels.select(), [(left, False, True)])
            channels.unregister(left)
            channels.shutdown(left)
        finally:
            left.close()
            right.close()
            channels.close()


class StreamConnectionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_connection_and_loopback_port_reservation(self):
        connections = LocalStreamConnections()

        async def echo(reader, writer):
            writer.write(await reader.read(16))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await connections.connect_tcp("127.0.0.1", port, limit=1024)
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await reader.readexactly(4), b"ping")
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
        port = connections.reserve_loopback_port()
        self.assertTrue(1 <= port <= 65535)

    async def test_adopted_unix_channel_exchanges_bytes(self):
        connections = LocalStreamConnections()
        left, right = socket.socketpair()
        try:
            reader, writer = await connections.adopt_unix(left, limit=1024)
            right.sendall(b"pong")
            self.assertEqual(await reader.readexactly(4), b"pong")
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(right.recv(4), b"ping")
            writer.close()
            await writer.wait_closed()
        finally:
            right.close()
