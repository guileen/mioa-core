"""Navigation commits, owner EOF, native death, and durable shutdown regressions."""
import asyncio
import base64
import hashlib
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mioa_core import profile
from mioa_core.engine import Engine as CoreEngine
from mioa_core.native import NativeContext, NativeError, NativeJSHandle, NativePage, NativeSession, _CDP, _WebSocket, _SupervisedProcess, _Supervisor, _version_endpoint, adopt_supervised_native
from mioa_core.native_spec import create_native_spec
from mioa_headless.native_service import NativeProtocol
from mioa_headless.service import Protocol
from mioa_os import LocalArtifactContent, LocalBlockingCalls, LocalHostFiles, LocalManagedPeerIdentity, LocalProcessIdentity, LocalProfileLeases, LocalSecureRandom, LocalStreamConnections, LocalUnixEndpoints, LocalUnixMessages, profile_owner_command


class WebSocketMessageBoundTests(unittest.IsolatedAsyncioTestCase):
    async def receive_fragments(self, first_size, pong_size, total_size):
        # Exercise the production byte parser without a connection/OS provider.
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"\x01\x7e" + struct.pack("!H", first_size) + b"x" * first_size
            + bytes((0x8a, pong_size)) + b"p" * pong_size
            + bytes((0x80, total_size - first_size)) + b"x" * (total_size - first_size))
        reader.feed_eof()
        with patch("mioa_core.native.MAX_MESSAGE", 128):
            return await _WebSocket.receive(SimpleNamespace(reader=reader))

    async def test_pong_does_not_consume_fragmented_data_budget(self):
        for first_size, pong_size in ((128, 1), (128, 125), (127, 125)):
            with self.subTest(first_size=first_size, pong_size=pong_size):
                self.assertEqual(await self.receive_fragments(first_size, pong_size, 128),
                                 b"x" * 128)

    async def test_pong_does_not_reset_fragmented_data_budget(self):
        with self.assertRaises(NativeError) as rejected:
            await self.receive_fragments(128, 1, 129)
        self.assertEqual(rejected.exception.code, "protocol_error")


class NavigationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def history_exchange(self, event, *, stale):
        class Wire:
            async def send(self, payload, *, abort=None):
                if abort is None or not abort():
                    writer.write(payload + b"\n")
                    await writer.drain()

            async def receive(self):
                payload = await reader.readline()
                if not payload:
                    raise EOFError("history peer closed")
                return payload

            async def close(self):
                writer.close()
                await writer.wait_closed()

        client, peer = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(Wire())
        owner = SimpleNamespace(monotonic_time=asyncio.get_running_loop().time)
        context = NativeContext(owner, cdp)
        page = NativePage(context, "page", "session", "https://example.com/current")
        page.frame_id = "main"
        context._pages["page"] = page
        operation = asyncio.create_task(page.go_back(wait_until="commit", timeout=1000))

        async def emit(message):
            peer_writer.write(json.dumps({"sessionId": "session", **message}).encode() + b"\n")
            await peer_writer.drain()

        async def request():
            return json.loads(await asyncio.wait_for(peer_reader.readline(), 1))

        try:
            query = await request()
            self.assertEqual(query["method"], "Page.getNavigationHistory")
            if event is not None:
                await emit(event)
            await emit({"id": query["id"], "result": {
                "entries": [{"id": 1}, {"id": 2}], "currentIndex": 1,
            }})
            if stale:
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(operation, 1)
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertEqual(page.url, event["params"].get("url",
                    event["params"].get("frame", {}).get("url")))
            else:
                navigation = await request()
                self.assertEqual(navigation["method"], "Page.navigateToHistoryEntry")
                self.assertEqual(navigation["params"], {"entryId": 1})
                await emit({"method": "Page.frameNavigated", "params": {"frame": {
                    "id": "main", "url": "https://example.com/previous",
                }}})
                await emit({"id": navigation["id"], "result": {}})
                # Serve follow-up document reads over the same CDP channel.
                while not operation.done():
                    reading = asyncio.create_task(request())
                    done, _ = await asyncio.wait((operation, reading), return_when=asyncio.FIRST_COMPLETED)
                    if reading not in done:
                        reading.cancel()
                        await asyncio.gather(reading, return_exceptions=True)
                        break
                    command = await reading
                    self.assertEqual(command["method"], "Runtime.evaluate")
                    await emit({"id": command["id"], "result": {
                        "result": {"type": "string", "value": page.url},
                    }})
                self.assertIsNone(await asyncio.wait_for(operation, 1))
                self.assertEqual(page.url, "https://example.com/previous")
            await cdp.close()
            self.assertEqual(await asyncio.wait_for(peer_reader.read(), 1), b"")
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            await cdp.close()
            peer_writer.close()
            await peer_writer.wait_closed()

    async def test_back_does_not_dispatch_history_from_a_replaced_main_document(self):
        for event in (
            {"method": "Page.frameNavigated", "params": {"frame": {
                "id": "main", "url": "https://example.com/new",
            }}},
            {"method": "Page.navigatedWithinDocument", "params": {
                "frameId": "main", "url": "https://example.com/current#new",
            }},
        ):
            with self.subTest(method=event["method"]):
                await self.history_exchange(event, stale=True)

    async def test_back_accepts_history_after_only_a_child_frame_changed(self):
        for event in (
            None,
            {"method": "Page.frameNavigated", "params": {"frame": {
                "id": "child", "parentId": "main", "url": "https://example.com/child",
            }}},
            {"method": "Page.navigatedWithinDocument", "params": {
                "frameId": "child", "url": "https://example.com/child#new",
            }},
        ):
            with self.subTest(event=event):
                await self.history_exchange(event, stale=False)


class NativeProcess:
    def __init__(self):
        self.returncode = None
        self.exited = asyncio.Event()
        self.waiting = asyncio.Event()

    async def wait(self):
        self.waiting.set()
        await self.exited.wait()
        return self.returncode

    def exit(self, code):
        self.returncode = code
        self.exited.set()


class Engine:
    def __init__(self):
        self.name = "lifecycle"
        self.stopping = asyncio.Event()
        self.control = SimpleNamespace(pause=Mock())
        self.native = self.context = None
        self.starting = asyncio.Event()
        self.launched = asyncio.Event()
        self.launch_gate = asyncio.Event()
        self.launch_gate.set()
        self.start_gate = asyncio.Event()
        self.start_gate.set()
        self.cleanup_gate = asyncio.Event()
        self.cleanup_gate.set()
        self.cleanup_started = asyncio.Event()
        self.shutdown_calls = self.close_count = 0
        self.start_error = self.shutdown_error = None
        self.cdp_dead = asyncio.Event()
        self.closed = False

    async def _launch(self, config):
        await self.launch_gate.wait()
        process = NativeProcess()
        cdp = SimpleNamespace(reader=asyncio.create_task(self.cdp_dead.wait()), failure=None, listeners=[])
        self.context = SimpleNamespace(cdp=cdp, pages=[], on=Mock(), remove_listener=Mock())
        self.native = SimpleNamespace(process=process, close=self.close_native)
        self.launched.set()

    async def start(self):
        self.starting.set()
        await self._launch({})
        await self.start_gate.wait()
        if self.start_error:
            raise self.start_error

    async def close_native(self):
        if self.closed:
            return
        self.cleanup_started.set()
        await self.cleanup_gate.wait()
        if self.native.process.returncode is None:
            self.native.process.exit(0)
        self.context.cdp.reader.cancel()
        await asyncio.gather(self.context.cdp.reader, return_exceptions=True)
        self.closed = True
        self.close_count += 1

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise self.shutdown_error
        if self.native:
            await self.native.close()


class Output(io.StringIO):
    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()

    def write(self, value):
        result = super().write(value)
        if json.loads(value).get("event") == "ready":
            self.ready.set()
        return result

    def messages(self):
        return [json.loads(line) for line in self.getvalue().splitlines()]


class NativeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_rejects_non_object_json_as_protocol_error(self):
        bodies = iter((b"null", b"[]", b'"unexpected"'))

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = next(bodies)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            for body in ("null", "array", "string"):
                with self.subTest(body=body):
                    with self.assertRaises(NativeError) as caught:
                        await _version_endpoint(port, connections=LocalStreamConnections())
                    self.assertEqual(caught.exception.code, "protocol_error")
        finally:
            server.close()
            await server.wait_closed()

    async def test_discovery_reads_close_delimited_body_across_writes(self):
        endpoint = "ws://127.0.0.1:1234/devtools/browser"
        body = json.dumps({"webSocketDebuggerUrl": endpoint}).encode()
        first_sent = asyncio.Event()
        release = asyncio.Event()

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body[:8])
                await writer.drain()
                first_sent.set()
                await release.wait()
                writer.write(body[8:])
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            discovery = asyncio.create_task(_version_endpoint(port, connections=LocalStreamConnections()))
            await first_sent.wait()
            await asyncio.sleep(0.05)
            release.set()
            self.assertEqual(await discovery, endpoint)
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    async def test_close_delimited_discovery_bounds_body_and_wait(self):
        release = asyncio.Event()
        bodies = iter((b"x" * 65537, b"{"))

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + next(bodies))
                await writer.drain()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            with self.assertRaises(NativeError) as caught:
                await asyncio.wait_for(_version_endpoint(port, connections=LocalStreamConnections()), 1.5)
            self.assertEqual(caught.exception.code, "protocol_error")
            with self.assertRaises(TimeoutError):
                await _version_endpoint(port, connections=LocalStreamConnections())
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    async def test_chunked_discovery_bounds_stalled_body_and_accepts_complete_body(self):
        release = asyncio.Event()
        endpoint = "ws://127.0.0.1:1234/devtools/browser"
        body = json.dumps({"webSocketDebuggerUrl": endpoint}).encode()
        responses = iter((b"4\r\nab", f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"))

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + next(responses))
                await writer.drain()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        stalled = asyncio.create_task(_version_endpoint(port, connections=LocalStreamConnections()))
        try:
            done, _ = await asyncio.wait({stalled}, timeout=1.5)
            self.assertIn(stalled, done, "stalled chunk did not time out")
            with self.assertRaises(TimeoutError):
                await stalled
            self.assertEqual(await _version_endpoint(port, connections=LocalStreamConnections()), endpoint)
        finally:
            stalled.cancel()
            await asyncio.gather(stalled, return_exceptions=True)
            release.set()
            server.close()
            await server.wait_closed()


class WebSocketHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_and_connection_tokens_are_exact(self):
        cases = (
            ("HTTP/1.1 200 101 misleading", "Upgrade", False),
            ("HTTP/1.1 101 Switching Protocols", "notupgrade", False),
            ("HTTP/1.1 101 Switching Protocols", "keep-alive, UpGrAdE", True),
        )
        for status, connection, accepted in cases:
            with self.subTest(status=status, connection=connection):
                finished = asyncio.Event()

                async def serve(reader, writer):
                    try:
                        request = await reader.readuntil(b"\r\n\r\n")
                        headers = dict(line.split(b":", 1) for line in request.split(b"\r\n")[1:] if b":" in line)
                        key = headers[b"Sec-WebSocket-Key"].strip()
                        accept = base64.b64encode(hashlib.sha1(
                            key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        writer.write(status.encode() + b"\r\nUpgrade: websocket\r\nConnection: "
                                     + connection.encode() + b"\r\nSec-WebSocket-Accept: "
                                     + accept + b"\r\n\r\n")
                        await writer.drain()
                        await reader.read()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        finished.set()

                server = await asyncio.start_server(serve, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                try:
                    if accepted:
                        channel = await _WebSocket.connect(f"ws://127.0.0.1:{port}/", LocalSecureRandom(), connections=LocalStreamConnections())
                        await channel.close()
                    else:
                        with self.assertRaises(NativeError) as error:
                            channel = await _WebSocket.connect(f"ws://127.0.0.1:{port}/", LocalSecureRandom(), connections=LocalStreamConnections())
                            await channel.close()
                        self.assertEqual(error.exception.code, "protocol_error")
                    await asyncio.wait_for(finished.wait(), 2)
                finally:
                    server.close()
                    await server.wait_closed()


class WebSocketFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_control_header_rejected_without_waiting_for_payload(self):
        for header in (b"\x09\x01", b"\x89\x7e", b"\x8a\x7f"):
            with self.subTest(header=header):
                client, peer = socket.socketpair()
                reader, writer = await asyncio.open_connection(sock=client)
                peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
                channel = _WebSocket(reader, writer, LocalSecureRandom())
                try:
                    peer_writer.write(header)
                    await peer_writer.drain()
                    with self.assertRaises(NativeError) as error:
                        await asyncio.wait_for(channel.receive(), 0.5)
                    self.assertEqual(error.exception.code, "protocol_error")
                finally:
                    writer.close()
                    peer_writer.close()
                    await writer.wait_closed()
                    await peer_writer.wait_closed()

    async def test_valid_ping_and_text_remain_readable(self):
        client, peer = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        channel = _WebSocket(reader, writer, LocalSecureRandom())
        try:
            payload = b"x" * 125
            peer_writer.write(b"\x89\x7d" + payload + b"\x81\x02ok")
            await peer_writer.drain()
            self.assertEqual(await asyncio.wait_for(channel.receive(), 0.5), b"ok")
            pong = await asyncio.wait_for(peer_reader.readexactly(131), 0.5)
            self.assertEqual(pong[:2], b"\x8a\xfd")
            self.assertEqual(bytes(byte ^ pong[2 + (index & 3)]
                                   for index, byte in enumerate(pong[6:])), payload)
        finally:
            writer.close()
            peer_writer.close()
            await writer.wait_closed()
            await peer_writer.wait_closed()


class WebSocketCloseOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_command_cannot_follow_close_frame(self):
        client, peer = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        channel = _WebSocket(reader, writer, LocalSecureRandom())
        tasks = []
        try:
            await channel.lock.acquire()
            closing = asyncio.create_task(channel.send(struct.pack("!H", 1000), 8))
            tasks.append(closing)
            # Both sends pass intake while open; Close reaches the lock first.
            await asyncio.sleep(0)
            sending = asyncio.create_task(channel.send(b"queued command"))
            tasks.append(sending)
            await asyncio.sleep(0)
            channel.lock.release()
            _, sent = await asyncio.wait_for(
                asyncio.gather(closing, sending, return_exceptions=True), 2)
            writer.close()
            await writer.wait_closed()
            wire = await asyncio.wait_for(peer_reader.read(), 1)
            self.assertEqual(wire[:2], b"\x88\x82")
            self.assertEqual(bytes(wire[6 + i] ^ wire[2 + i] for i in range(2)),
                             struct.pack("!H", 1000))
            self.assertEqual(wire[8:], b"", "command bytes followed the close frame")
            self.assertIsInstance(sent, NativeError)
            self.assertEqual(sent.code, "protocol_error")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            peer_writer.close()
            await writer.wait_closed()
            await peer_writer.wait_closed()


class NativeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(directory)
        read_fd, self.parent_fd = os.pipe()
        stdin = self.stack.enter_context(os.fdopen(read_fd, "rb", buffering=0))
        self.output = Output()
        self.stack.enter_context(patch("mioa_headless.native_service.sys.stdin", stdin))
        self.stack.enter_context(patch("mioa_headless.native_service.sys.stdout", self.output))
        self.ring = self.stack.enter_context(patch("mioa_headless.native_service.FrameRing")).return_value
        self.session = SimpleNamespace(state=AsyncMock(return_value={"pages": []}), close=Mock())
        self.stack.enter_context(patch("mioa_headless.native_service.Session", return_value=self.session))
        self.engine = Engine()
        self.protocol = NativeProtocol(self.engine, root, root / "control", root / "frame")
        self.run_task = None
        self.addAsyncCleanup(self.finish)

    def disconnect_parent(self):
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None

    async def finish(self):
        self.disconnect_parent()
        self.engine.launch_gate.set()
        self.engine.start_gate.set()
        self.engine.cleanup_gate.set()
        self.engine.stopping.set()
        if self.run_task is not None:
            await asyncio.wait_for(self.run_task, 2)

    def start_service(self):
        self.run_task = asyncio.create_task(self.protocol.run())

    async def ready(self):
        self.start_service()
        await asyncio.wait_for(self.output.ready.wait(), 1)
        self.assertNotIn("native_pid", self.output.messages()[0])

    async def result(self):
        return await asyncio.wait_for(self.run_task, 1)

    async def connect(self):
        reader, writer = await asyncio.open_unix_connection(str(self.protocol.socket))
        self.addAsyncCleanup(self.close_writer, writer)
        return reader, writer

    async def close_writer(self, writer):
        writer.close()
        await writer.wait_closed()

    async def send(self, writer, request):
        body = json.dumps(request).encode()
        writer.write(struct.pack("<I", len(body)) + body)
        await writer.drain()

    async def receive(self, reader):
        prefix = await asyncio.wait_for(reader.readexactly(4), 1)
        length, = struct.unpack("<I", prefix)
        return json.loads(await asyncio.wait_for(reader.readexactly(length), 1))

    def assert_cleaned(self):
        self.assertEqual(self.engine.shutdown_calls, 1)
        if self.engine.native:
            self.assertTrue(self.engine.closed)
            self.assertEqual(self.engine.close_count, 1)
            self.assertTrue(self.engine.context.cdp.reader.done())
        self.ring.close.assert_called_once()

    async def test_parent_eof_cancels_startup_before_native_exposure(self):
        self.engine.launch_gate.clear()
        self.start_service()
        await asyncio.wait_for(self.engine.starting.wait(), 1)
        self.disconnect_parent()
        self.assertEqual(await self.result(), 1)
        self.assertIsNone(self.engine.native)
        self.assertFalse(self.protocol.socket.exists())
        self.assertEqual(self.output.messages()[0]["error"]["code"], "runtime_error")
        self.assertNotIn("_launch", self.engine.__dict__)
        self.assert_cleaned()

    async def test_parent_eof_awaits_launch_owned_cleanup_before_shutdown(self):
        provisional = NativeProcess()
        created = asyncio.Event()
        release_cleanup = asyncio.Event()
        self.addCleanup(release_cleanup.set)
        cancellation_started = asyncio.Event()

        async def launch(config):
            created.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancellation_started.set()
                await release_cleanup.wait()
                provisional.exit(0)

        self.engine._launch = launch
        self.start_service()
        await asyncio.wait_for(created.wait(), 1)
        self.disconnect_parent()
        await asyncio.wait_for(cancellation_started.wait(), 1)
        self.assertEqual(self.engine.shutdown_calls, 0)
        self.assertFalse(self.run_task.done())
        release_cleanup.set()
        self.assertEqual(await self.result(), 1)
        self.assertEqual(provisional.returncode, 0)
        self.assertIs(self.engine._launch, launch)
        self.assert_cleaned()

    async def test_parent_eof_cleans_child_while_startup_is_pending(self):
        self.engine.start_gate.clear()
        self.start_service()
        await asyncio.wait_for(self.engine.launched.wait(), 1)
        self.disconnect_parent()
        self.assertEqual(await self.result(), 1)
        self.assertFalse(self.protocol.ready)
        self.assert_cleaned()

    async def test_parent_eof_without_socket_accept_cleans_idle_child(self):
        await self.ready()
        self.assertIsNone(self.protocol.writer)
        self.disconnect_parent()
        self.assertEqual(await self.result(), 1)
        self.assertEqual([message["event"] for message in self.output.messages()], ["ready", "error"])
        self.assertEqual(self.output.messages()[-1]["error"]["code"], "runtime_error")
        self.assert_cleaned()

    async def test_parent_eof_emits_terminal_error_on_connected_socket(self):
        await self.ready()
        reader, writer = await self.connect()
        await self.send(writer, {"id": 1, "method": "state"})
        await self.receive(reader)
        self.disconnect_parent()
        terminal = await self.receive(reader)
        self.assertEqual(await self.result(), 1)
        self.assertEqual(terminal, self.output.messages()[-1])
        self.assertEqual(terminal["event"], "error")
        self.assertEqual(terminal["error"]["code"], "runtime_error")
        self.assert_cleaned()

    async def test_process_death_during_pending_startup_is_terminal(self):
        self.engine.start_gate.clear()
        self.start_service()
        await asyncio.wait_for(self.engine.launched.wait(), 1)
        self.engine.native.process.exit(17)
        self.assertEqual(await self.result(), 1)
        self.assertFalse(self.protocol.ready)
        self.assertEqual(self.output.messages()[-1]["error"]["code"], "runtime_error")
        self.assert_cleaned()

    async def test_cdp_death_during_pending_startup_is_terminal(self):
        self.engine.start_gate.clear()
        self.start_service()
        await asyncio.wait_for(self.engine.launched.wait(), 1)
        self.engine.context.cdp.failure = RuntimeError("private startup detail")
        self.engine.cdp_dead.set()
        self.assertEqual(await self.result(), 1)
        self.assertFalse(self.protocol.ready)
        self.assertNotIn("private", self.output.getvalue())
        self.assert_cleaned()

    async def test_idle_process_death_without_client_is_terminal(self):
        await self.ready()
        await asyncio.wait_for(self.engine.native.process.waiting.wait(), 1)
        self.engine.native.process.exit(9)
        self.assertEqual(await self.result(), 1)
        self.assertEqual(self.output.messages()[-1]["event"], "error")
        self.assert_cleaned()

    async def test_idle_cdp_death_emits_sanitized_terminal_on_both_channels(self):
        await self.ready()
        reader, writer = await self.connect()
        await self.send(writer, {"id": 1, "method": "state"})
        self.assertEqual(await self.receive(reader), {"id": 1, "result": {"pages": []}})
        self.engine.context.cdp.failure = RuntimeError("secret URL and credentials")
        self.engine.cdp_dead.set()
        terminal = await self.receive(reader)
        self.assertEqual(await self.result(), 1)
        self.assertEqual(terminal, self.output.messages()[-1])
        self.assertEqual(terminal["event"], "error")
        self.assertNotIn("secret", json.dumps(terminal))
        self.assert_cleaned()

    async def test_shutdown_ack_occurs_once_after_durable_cleanup(self):
        await self.ready()
        reader, writer = await self.connect()
        self.engine.cleanup_gate.clear()
        await self.send(writer, {"id": 8, "method": "shutdown"})
        await asyncio.wait_for(self.engine.cleanup_started.wait(), 1)
        self.assertFalse(self.run_task.done())
        self.assertFalse(self.engine.closed)
        self.assertFalse(reader._buffer)
        self.engine.cleanup_gate.set()
        self.assertEqual(await self.receive(reader), {"id": 8, "result": {"status": "ok", "flushed": True}})
        self.assertEqual(await self.result(), 0)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertEqual([message["event"] for message in self.output.messages()], ["ready"])
        self.assert_cleaned()

    async def test_signal_stop_does_not_misclassify_cleanup_as_native_death(self):
        await self.ready()
        self.engine.stopping.set()  # same callback installed for SIGTERM/SIGINT
        self.assertEqual(await self.result(), 0)
        self.assertIsNone(self.protocol.fatal)
        self.assertEqual([message["event"] for message in self.output.messages()], ["ready"])
        self.assert_cleaned()

    async def test_startup_exception_still_closes_owned_native_child(self):
        self.engine.start_error = RuntimeError("private startup details")
        self.start_service()
        self.assertEqual(await self.result(), 1)
        self.assertNotIn("private", self.output.getvalue())
        self.assertFalse(self.protocol.ready)
        self.assert_cleaned()

    async def test_shutdown_exception_still_closes_child_and_returns_one_error_ack(self):
        await self.ready()
        reader, writer = await self.connect()
        self.engine.shutdown_error = RuntimeError("private shutdown details")
        await self.send(writer, {"id": 9, "method": "shutdown"})
        response = await self.receive(reader)
        self.assertEqual(response["id"], 9)
        self.assertIn("error", response)
        self.assertNotIn("private", json.dumps(response))
        self.assertEqual(await self.result(), 1)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assert_cleaned()

    async def test_ring_cleanup_failure_returns_error_ack_and_finishes_teardown(self):
        loop = asyncio.get_running_loop()
        connect_read_pipe = loop.connect_read_pipe
        transports = []

        async def connect(*args, **kwargs):
            result = await connect_read_pipe(*args, **kwargs)
            transports.append(result[0])
            return result

        with patch.object(loop, "connect_read_pipe", side_effect=connect), patch.object(
            loop, "remove_signal_handler", wraps=loop.remove_signal_handler
        ) as remove_signal_handler:
            await self.ready()
            reader, writer = await self.connect()
            self.ring.close.side_effect = RuntimeError("private artifact unlink failure")
            await self.send(writer, {"id": 10, "method": "shutdown"})
            response = await self.receive(reader)
            self.assertEqual(response["id"], 10)
            self.assertIn("error", response)
            self.assertNotIn("result", response)
            self.assertNotIn("private", json.dumps(response))
            self.assertEqual(await self.result(), 1)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            self.assertEqual(self.output.messages()[-1]["event"], "error")
            self.assertNotIn("private", self.output.getvalue())
            self.assertEqual(remove_signal_handler.call_count, 2)
            self.assertTrue(transports[0].is_closing())
            self.assert_cleaned()


class NativeAdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.contexts, self.waiters, self.sessions = [], [], []

    async def asyncTearDown(self):
        tasks = set(self.waiters)
        for context in self.contexts:
            tasks.update(context._adopting.values())
        for session in self.sessions:
            if session._close_task is not None:
                tasks.add(session._close_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def context(self, stage="Runtime.enable"):
        entered, resume = asyncio.Event(), asyncio.Event()
        calls, published = [], []

        async def send(method, params=None, session=None, **kwargs):
            calls.append(method)
            if method == stage:
                entered.set()
                await resume.wait()
            if method == "Target.attachToTarget":
                return {"sessionId": "session"}
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "main", "url": "about:blank"}}}
            if method in {"Page.enable", "Network.enable", "Runtime.enable"}:
                return {}
            raise AssertionError(method)

        cdp = SimpleNamespace(listeners=[], send=AsyncMock(side_effect=send),
                              close=AsyncMock(), _task=lambda value: None)
        context = NativeContext(SimpleNamespace(monotonic_time=LocalProcessIdentity().monotonic_time), cdp)
        context.on("page", published.append)
        self.contexts.append(context)
        return context, entered, resume, calls, published

    def adopt(self, context, *, joined=None):
        async def run():
            if joined is not None:
                joined.set()
            return await context._adopt({"targetId": "page", "url": "about:blank"})
        task = asyncio.create_task(run())
        self.waiters.append(task)
        return task

    def invalidate(self, context, reason):
        if reason == "destroyed":
            context._event({"method": "Target.targetDestroyed", "params": {"targetId": "page"}})
        elif reason == "detached":
            context._event({"method": "Inspector.detached", "sessionId": "session", "params": {}})
        elif reason == "revoked":
            context.cdp.revoked = True
        elif reason == "connection":
            context.cdp.failure = NativeError("protocol_error", "connection lost")
        elif reason == "closed":
            context.closed = True
        else:
            raise AssertionError(reason)

    async def test_cancelling_either_waiter_cannot_cancel_shared_adoption(self):
        for stage in ("Target.attachToTarget", "Runtime.enable"):
            for cancel_first in (False, True):
                with self.subTest(stage=stage, cancel_first=cancel_first):
                    context, entered, resume, calls, published = self.context(stage)
                    first = self.adopt(context)
                    await asyncio.wait_for(entered.wait(), 1)
                    joined = asyncio.Event()
                    second = self.adopt(context, joined=joined)
                    await asyncio.wait_for(joined.wait(), 1)
                    pending = context._adopting["page"]
                    cancelled, survivor = (first, second) if cancel_first else (second, first)
                    cancelled.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await cancelled
                    self.assertIs(context._adopting.get("page"), pending)
                    self.assertFalse(pending.done())
                    resume.set()
                    page = await asyncio.wait_for(survivor, 1)
                    self.assertEqual(context.pages, [page])
                    self.assertEqual(published, [page])
                    self.assertEqual(calls.count("Target.attachToTarget"), 1)
                    self.assertFalse(context._adopting)
                    self.assertIs(await context._adopt({"targetId": "page"}), page)

    async def test_lost_target_cannot_be_published_or_returned_after_initialization(self):
        for reason in ("destroyed", "detached", "revoked", "connection", "closed"):
            with self.subTest(reason=reason):
                context, entered, resume, _, published = self.context()
                waiter = self.adopt(context)
                await asyncio.wait_for(entered.wait(), 1)
                self.invalidate(context, reason)
                resume.set()
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(waiter, 1)
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertEqual(published, [])
                self.assertNotIn("page", context._pages)
                self.assertFalse(context._adopting)

    async def test_cached_closed_page_cannot_be_readopted(self):
        for reason in ("destroyed", "detached", "revoked", "connection", "closed"):
            with self.subTest(reason=reason):
                context, _, resume, calls, published = self.context()
                resume.set()
                page = await asyncio.wait_for(self.adopt(context), 1)
                self.invalidate(context, reason)
                with self.assertRaises(NativeError) as rejected:
                    await context._adopt({"targetId": "page"})
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertEqual(published, [page])
                self.assertEqual(calls.count("Target.attachToTarget"), 1)

    async def test_target_closed_by_page_listener_cannot_escape_to_waiter(self):
        context, _, resume, _, published = self.context()
        context.on("page", lambda page: self.invalidate(context, "destroyed"))
        resume.set()
        with self.assertRaises(NativeError) as rejected:
            await asyncio.wait_for(self.adopt(context), 1)
        self.assertEqual(rejected.exception.code, "stale_reference")
        self.assertEqual(published, [context._pages["page"]])
        self.assertTrue(published[0].is_closed())
        self.assertFalse(context._adopting)

    async def test_shutdown_reaps_adoption_after_its_last_waiter_cancels(self):
        context, entered, _, _, published = self.context()
        waiter = self.adopt(context)
        await asyncio.wait_for(entered.wait(), 1)
        pending = context._adopting["page"]
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        process = NativeProcess()
        process.stdout = process.stderr = None
        process.terminate = lambda: process.exit(0)
        session = NativeSession(process, {"network": {}}, Path("unused-config.json"), proxy=None)
        self.assertFalse(hasattr(session, "pid"))
        session.context, session.cdp = context, context.cdp
        self.sessions.append(session)
        await asyncio.wait_for(session.close(), 1)
        self.assertTrue(context.closed)
        self.assertTrue(pending.cancelled())
        self.assertFalse(context._adopting)
        self.assertFalse(context._pages)
        self.assertEqual(published, [])
        self.assertEqual(process.returncode, 0)
        context.cdp.close.assert_awaited_once()


class NativeLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_logs_redact_launch_credentials_after_parent_rotation(self):
        key = "MIOA_TEST_LAUNCH_PROXY"
        launched = "http://launch%2Duser:launch%2Dpassword@127.0.0.1:8123"
        cfg = {"network": {"proxy_ref": f"env:{key}"}, "seed": "launch-seed"}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import os; from urllib.parse import unquote; "
            f"print(os.environ[{key!r}]); print(unquote(os.environ[{key!r}])); print('launch-seed')",
            env={**os.environ, key: launched}, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            with patch.dict(os.environ, {key: "http://replacement:rotated@127.0.0.1:8124"}):
                session = NativeSession(process, cfg, Path("unused-config.json"), proxy=launched)
            await asyncio.wait_for(process.wait(), 5)
            await asyncio.gather(*session._log_tasks)
            text = session.log_text()
            self.assertIn("[private]", text)
            self.assertTrue(all(secret not in text for secret in (
                launched, "launch%2Duser", "launch%2Dpassword", "launch-user",
                "launch-password", cfg["seed"],
            )), "launched credentials escaped log redaction after parent rotation")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class StdioReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_omits_native_pid(self):
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        engine = Engine()
        protocol = Protocol(engine, Path("/unused"))
        reader = asyncio.StreamReader()
        loop = asyncio.get_running_loop()
        output = os.fdopen(output_read, "rb", buffering=0)
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), output)
        try:
            with os.fdopen(input_read, "rb", buffering=0) as stdin, \
                    os.fdopen(output_write, "wb", buffering=0) as stdout, \
                    patch("mioa_headless.service.sys.stdin", stdin), \
                    patch("mioa_headless.service.sys.stdout", stdout):
                run = asyncio.create_task(protocol.run())
                try:
                    ready = json.loads(await asyncio.wait_for(reader.readline(), 2))
                    self.assertEqual(ready["event"], "ready")
                    self.assertEqual(ready["protocol"], 1)
                    self.assertNotIn("pid", ready)
                    self.assertNotIn("native_pid", ready)
                finally:
                    engine.stopping.set()
                    await asyncio.wait_for(run, 2)
        finally:
            os.close(input_write)
            transport.close()


class EngineReadyStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(profile, "BASE", Path(directory)))

    async def test_ready_state_does_not_expose_native_pid(self):
        engine = CoreEngine("ready-regression", headless=True, initial_url=None, verify=False, profile_owners=Mock())
        engine._launch = AsyncMock()
        engine.page = Mock(return_value=SimpleNamespace(url="about:blank"))
        engine.native = SimpleNamespace()
        engine.receipt = {"verified": True}
        with patch.object(profile, "require", return_value={"runtime": {"id": "mioa"}}), \
                patch.object(profile, "save_state") as save:
            await engine.start()
        state = save.call_args.args[1]
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["receipt"], {"verified": True})
        self.assertNotIn("pid", state)
        self.assertNotIn("process_start", state)
        self.assertNotIn("native_pid", state)


class EngineShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_failure_or_cancellation_cannot_skip_runtime_and_state_cleanup(self):
        for failure in (RuntimeError("reference cleanup failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory, \
                    patch.object(profile, "BASE", Path(directory)):
                engine = CoreEngine("shutdown-regression", headless=True, initial_url=None, verify=False)
                profile.save_state(engine.name, {"status": "READY"})
                state = engine.workdir / "state.json"
                process = NativeProcess()
                process.stdout = process.stderr = None
                process.terminate = lambda: process.exit(0)
                engine.native = NativeSession(process, {"network": {}, "seed": "fixture"},
                                              engine.workdir / "config.json", proxy=None)
                engine.context, engine.active = SimpleNamespace(pages=[]), object()
                entered = asyncio.Event()

                async def clear(*, all_pages):
                    entered.set()
                    if isinstance(failure, asyncio.CancelledError):
                        await asyncio.Event().wait()
                    raise failure

                engine.refs = SimpleNamespace(clear=clear)
                shutdown = asyncio.create_task(engine.shutdown())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    if isinstance(failure, asyncio.CancelledError):
                        shutdown.cancel()
                    with self.assertRaises(type(failure)) as rejected:
                        await asyncio.wait_for(shutdown, 1)
                    if not isinstance(failure, asyncio.CancelledError):
                        self.assertIs(rejected.exception, failure)
                    self.assertEqual(process.returncode, 0)
                    self.assertIsNone(engine.context)
                    self.assertIsNone(engine.active)
                    self.assertFalse(state.exists())
                finally:
                    if not shutdown.done():
                        shutdown.cancel()
                    await asyncio.gather(shutdown, return_exceptions=True)
                    if engine.native._close_task is not None:
                        await asyncio.gather(engine.native._close_task, return_exceptions=True)


class NativeTransportCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_closes_log_tasks_when_cdp_close_fails(self):
        failure = RuntimeError("close write failed")
        cdp_client, cdp_peer = socket.socketpair()
        output_client, output_peer = socket.socketpair()
        cdp_peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=cdp_client)
        output, output_writer = await asyncio.open_connection(sock=output_client)
        process = SimpleNamespace(returncode=0, stdout=output, stderr=None)
        session = NativeSession(process, {"network": {}}, Path("unused"), proxy=None)
        session.cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        try:
            await asyncio.sleep(0)
            with patch.object(writer, "write", side_effect=failure), \
                    self.assertRaises(RuntimeError) as rejected:
                await asyncio.wait_for(session.close(), 3)
            self.assertIs(rejected.exception, failure)
            self.assertTrue(all(task.done() for task in session._log_tasks))
            self.assertEqual(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(cdp_peer, 1), 1), b"")
        finally:
            for task in session._log_tasks:
                task.cancel()
            await asyncio.gather(*session._log_tasks, return_exceptions=True)
            writer.close()
            output_writer.close()
            await asyncio.gather(writer.wait_closed(), output_writer.wait_closed(), return_exceptions=True)
            cdp_peer.close()
            output_peer.close()

    async def blocked_close(self, *, cancel):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def listener():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        cdp._task(listener())
        request = asyncio.create_task(cdp.send("Page.getFrameTree"))
        closing = None
        locked = False
        try:
            await asyncio.wait_for(entered.wait(), 1)
            header = await asyncio.wait_for(peer_reader.readexactly(2), 1)
            length = header[1] & 127
            if length == 126:
                length, = struct.unpack("!H", await peer_reader.readexactly(2))
            elif length == 127:
                length, = struct.unpack("!Q", await peer_reader.readexactly(8))
            await asyncio.wait_for(peer_reader.readexactly(4 + length), 1)
            await cdp.socket.lock.acquire()
            locked = True
            closing = asyncio.create_task(cdp.close())
            if cancel:
                await asyncio.sleep(0)
                closing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, 3)
            else:
                await asyncio.wait_for(closing, 3)
            self.assertEqual(await asyncio.wait_for(peer_reader.read(1), 1), b"")
            with self.assertRaises(NativeError) as rejected:
                await asyncio.wait_for(request, 1)
            self.assertEqual(rejected.exception.code, "protocol_error")
            await asyncio.wait_for(stopped.wait(), 1)
            with self.assertRaises(NativeError) as rejected:
                await cdp.send("Page.getFrameTree")
            self.assertEqual(rejected.exception.code, "protocol_error")
        finally:
            tasks = [request, cdp.reader, *cdp.event_tasks]
            if closing is not None:
                tasks.append(closing)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if locked:
                cdp.socket.lock.release()
            writer.close()
            peer_writer.close()
            await asyncio.gather(writer.wait_closed(), peer_writer.wait_closed(),
                                 return_exceptions=True)

    async def test_close_frame_timeout_still_releases_channel_and_waiters(self):
        await self.blocked_close(cancel=False)

    async def test_cancelled_close_still_releases_channel_and_waiters(self):
        await self.blocked_close(cancel=True)


class NativeResponseIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, response_id, *, valid):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        request = asyncio.create_task(cdp.send("Page.getFrameTree"))

        async def respond(identity, result):
            data = json.dumps({"id": identity, "result": result}).encode()
            peer_writer.write(bytes((0x81, len(data))) + data)
            await peer_writer.drain()

        try:
            header = await asyncio.wait_for(peer_reader.readexactly(2), 1)
            length = header[1] & 127
            await asyncio.wait_for(peer_reader.readexactly(4 + length), 1)
            if valid:
                # An unmatched response must not consume the current request.
                await respond(99, {"frameTree": {"frame": {"id": "stale"}}})
            await respond(response_id, {"frameTree": {"frame": {"id": "main"}}})
            if valid:
                self.assertEqual(await asyncio.wait_for(request, 1),
                                 {"frameTree": {"frame": {"id": "main"}}})
            else:
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(request, 1)
                self.assertEqual(rejected.exception.code, "protocol_error")
                with self.assertRaises(NativeError) as rejected:
                    await cdp.send("Page.getFrameTree")
                self.assertEqual(rejected.exception.code, "protocol_error")
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await cdp.close()
            peer_writer.close()
            await peer_writer.wait_closed()

    async def test_boolean_response_cannot_complete_integer_request(self):
        await self.exchange(True, valid=False)

    async def test_float_response_cannot_complete_integer_request(self):
        await self.exchange(1.0, valid=False)

    async def test_integer_response_survives_late_unmatched_response(self):
        await self.exchange(1, valid=True)

    async def test_reader_failure_cancels_command_waiting_for_write_lock(self):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        await cdp.socket.lock.acquire()
        request = asyncio.create_task(cdp.send("Page.navigate", {"url": "https://example.com/"}))
        try:
            await asyncio.sleep(0)
            self.assertTrue(cdp.pending)
            data = b'{"id":true,"result":{}}'
            peer_writer.write(bytes((0x81, len(data))) + data)
            await peer_writer.drain()
            await asyncio.wait_for(asyncio.shield(cdp.reader), 1)
            with self.assertRaises(NativeError) as rejected:
                await asyncio.wait_for(asyncio.shield(request), 1)
            self.assertEqual(rejected.exception.code, "protocol_error")
            cdp.socket.lock.release()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(peer_reader.read(1), 0.05)
        finally:
            if cdp.socket.lock.locked():
                cdp.socket.lock.release()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await cdp.close()
            peer_writer.close()
            await peer_writer.wait_closed()

    async def error_exchange(self, error, *, malformed):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        requests = [asyncio.create_task(cdp.send(method)) for method in
                    ("Page.unsupportedMethod", "Page.getFrameTree")]

        async def respond(value):
            data = json.dumps(value).encode()
            peer_writer.write(bytes((0x81, len(data))) + data)
            await peer_writer.drain()

        try:
            for _ in requests:
                header = await asyncio.wait_for(peer_reader.readexactly(2), 1)
                length = header[1] & 127
                self.assertLess(length, 126)
                await asyncio.wait_for(peer_reader.readexactly(4 + length), 1)
            await respond({"id": 1, "error": error})
            if malformed:
                await asyncio.wait_for(asyncio.shield(cdp.reader), 1)
                # Callers may resume after the reader task has completed.
                for request in requests:
                    with self.assertRaises(NativeError) as rejected:
                        await asyncio.wait_for(asyncio.shield(request), 1)
                    self.assertEqual(rejected.exception.code, "protocol_error")
                with self.assertRaises(NativeError) as rejected:
                    await cdp.send("Page.getFrameTree")
                self.assertEqual(rejected.exception.code, "protocol_error")
            else:
                await respond({"id": 2, "result": {"frameTree": {"frame": {"id": "main"}}}})
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(requests[0], 1)
                self.assertEqual(rejected.exception.code, "capability_unavailable")
                self.assertEqual(await asyncio.wait_for(requests[1], 1),
                                 {"frameTree": {"frame": {"id": "main"}}})
                self.assertIsNone(cdp.failure)
        finally:
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            await cdp.close()
            peer_writer.close()
            await peer_writer.wait_closed()

    async def test_malformed_error_settles_matching_and_other_commands(self):
        await self.error_exchange(None, malformed=True)

    async def test_valid_error_does_not_fail_another_pending_command(self):
        await self.error_exchange({"code": -32601, "message": "unknown method"}, malformed=False)


class NativeObservationReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        self.peer_reader, self.peer_writer = await asyncio.open_connection(sock=peer)
        self.cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        context = NativeContext(SimpleNamespace(platform="Darwin"), self.cdp)
        self.page = NativePage(context, "page", "session", "https://example.com/old")
        self.page.frame_id = "main"
        self.page._execution_context = 7
        context._pages["page"] = self.page

    async def asyncTearDown(self):
        await self.cdp.close()
        self.peer_writer.close()
        await self.peer_writer.wait_closed()

    async def request(self):
        header = await asyncio.wait_for(self.peer_reader.readexactly(2), 1)
        size = header[1] & 127
        if size == 126:
            size = int.from_bytes(await self.peer_reader.readexactly(2), "big")
        elif size == 127:
            size = int.from_bytes(await self.peer_reader.readexactly(8), "big")
        mask = await self.peer_reader.readexactly(4)
        data = await self.peer_reader.readexactly(size)
        return json.loads(bytes(value ^ mask[index % 4] for index, value in enumerate(data)))

    async def emit(self, **value):
        data = json.dumps({"sessionId": "session", **value}).encode()
        header = bytes((0x81, len(data))) if len(data) < 126 else b"\x81\x7e" + struct.pack("!H", len(data))
        self.peer_writer.write(header + data)
        await self.peer_writer.drain()

    async def test_request_callback_failure_does_not_break_cdp_or_other_callbacks(self):
        observed = []

        def failed_callback(request):
            raise RuntimeError("request callback failed")

        self.page.context.on("request", failed_callback)
        self.page.context.on("request", observed.append)
        command = asyncio.create_task(self.cdp.send("Page.getFrameTree", session="session"))
        request = await self.request()
        await self.emit(method="Network.requestWillBeSent", params={
            "requestId": "request-1",
            "request": {"url": "https://example.com/", "headers": {}, "method": "GET"},
        })
        await self.emit(id=request["id"], result={"ok": True})
        self.assertEqual(await asyncio.wait_for(command, 1), {"ok": True})
        self.assertEqual([item.url for item in observed], ["https://example.com/"])
        self.assertEqual(self.cdp.event_errors, ["request callback failed"])
        self.assertFalse(self.cdp.reader.done())

    async def test_request_callback_future_reports_its_failure(self):
        future = asyncio.get_running_loop().create_future()
        pending = asyncio.get_running_loop().create_future()
        self.page.context.on("request", lambda request: future)
        self.page.context.on("request", lambda request: pending)
        command = asyncio.create_task(self.cdp.send("Page.getFrameTree", session="session"))
        try:
            request = await self.request()
            await self.emit(method="Network.requestWillBeSent", params={
                "requestId": "request-1",
                "request": {"url": "https://example.com/", "headers": {}, "method": "GET"},
            })
            await self.emit(id=request["id"], result={"ok": True})
            self.assertEqual(await asyncio.wait_for(command, 1), {"ok": True})
            future.set_exception(RuntimeError("future listener failed"))
            await asyncio.sleep(0)
            self.assertEqual(self.cdp.event_errors, ["future listener failed"])
            self.assertFalse(self.cdp.reader.done())
            await self.cdp.close()
            self.assertTrue(pending.cancelled())
        finally:
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()
            if not pending.done():
                pending.cancel()

    async def replace_document(self):
        await self.emit(method="Runtime.executionContextsCleared", params={})
        await self.emit(method="Page.frameNavigated",
                        params={"frame": {"id": "main", "url": "https://example.com/new"}})
        await self.emit(method="Runtime.executionContextCreated",
                        params={"context": {"id": 8, "auxData": {"isDefault": True, "frameId": "main"}}})

    async def test_title_rejects_success_from_replaced_document(self):
        task = asyncio.create_task(self.page.title())
        request = await self.request()
        await self.replace_document()
        await self.emit(id=request["id"], result={"result": {"type": "string", "value": "Old title"}})
        with self.assertRaises(NativeError) as rejected:
            await asyncio.wait_for(task, 1)
        self.assertEqual(rejected.exception.code, "stale_reference")
        self.assertEqual(self.page.cached_title, "")

    async def test_replaced_context_failure_is_stale_but_new_document_is_readable(self):
        for kind in ("title", "handle", "child"):
            with self.subTest(kind=kind):
                target = self.page
                if kind == "child":
                    self.page._frame_ids.add("child")
                    self.page._frame_contexts["child"] = 9
                    target = self.page._frame_document("child")
                task = asyncio.create_task(target.title() if kind == "title"
                                           else target.evaluate_handle("() => ({})"))
                request = await self.request()
                await self.replace_document()
                await self.emit(id=request["id"], error={"code": -32000, "message": "Context unavailable"})
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(task, 1)
                self.assertEqual(rejected.exception.code, "stale_reference")
        # No frameStoppedLoading or load event: the committed document can
        # already supply metadata while resources are still arriving.
        task = asyncio.create_task(self.page.title())
        request = await self.request()
        await self.emit(id=request["id"], result={"result": {"type": "string", "value": "New title"}})
        self.assertEqual(await asyncio.wait_for(task, 1), "New title")
        self.assertEqual(self.page.cached_title, "New title")

    async def test_stable_context_failure_and_user_script_are_not_reclassified(self):
        for kind in ("title", "handle", "child", "script"):
            with self.subTest(kind=kind):
                target = self.page
                if kind == "child":
                    self.page._frame_ids.add("child")
                    self.page._frame_contexts["child"] = 9
                    target = self.page._frame_document("child")
                operation = (target.title() if kind == "title" else
                             target.evaluate("window.sideEffect = true") if kind == "script" else
                             target.evaluate_handle("() => ({})"))
                task = asyncio.create_task(operation)
                request = await self.request()
                if kind == "script":
                    await self.replace_document()
                await self.emit(id=request["id"], error={"code": -32000, "message": "Context unavailable"})
                with self.assertRaises(NativeError) as rejected:
                    await asyncio.wait_for(task, 1)
                self.assertEqual(rejected.exception.code, "protocol_error")
        self.assertEqual(self.cdp.sequence, 4)


class NativeNavigationTests(unittest.IsolatedAsyncioTestCase):
    def page(self):
        page = NativePage(SimpleNamespace(cdp=SimpleNamespace(),
            owner=SimpleNamespace(monotonic_time=LocalProcessIdentity().monotonic_time)),
            "page", "session", "https://example.com/current")
        page.frame_id = "main"
        page.evaluate = AsyncMock(side_effect=lambda expression: {
            "document.readyState": "complete", "location.href": page.url,
        }[expression])
        return page

    async def test_handle_creation_rejects_results_overtaken_by_navigation(self):
        for origin in ("page", "handle"):
            with self.subTest(origin=origin):
                page = self.page()
                released = []

                async def send(method, params=None, **kwargs):
                    if method == "Runtime.releaseObject":
                        released.append(params["objectId"])
                        return {}
                    self.assertIn(method, ("Runtime.evaluate", "Runtime.callFunctionOn"))
                    page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
                    return {"result": {"type": "object", "objectId": "old-result"}}

                page._send = send
                source = page if origin == "page" else NativeJSHandle(page, {"type": "object", "objectId": "source"})
                with self.assertRaises(NativeError) as rejected:
                    await source.evaluate_handle("() => ({})")
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertEqual(released, ["old-result"])

    async def test_locator_wait_rejects_target_lost_during_query(self):
        for state in ("attached", "detached", "visible", "hidden"):
            for reason in ("closed", "revoked", "failure"):
                with self.subTest(state=state, reason=reason):
                    page = self.page()

                    async def evaluate(expression):
                        counting = "return els.length;" in expression
                        if counting and state in ("visible", "hidden"):
                            return 1
                        if reason == "closed":
                            page._event("Inspector.detached", {})
                        elif reason == "revoked":
                            page.context.cdp.revoked = True
                        else:
                            page.context.cdp.failure = RuntimeError("connection lost")
                        if counting:
                            return 1 if state == "attached" else 0
                        return state == "visible"

                    page.evaluate = evaluate
                    with self.assertRaises(NativeError) as rejected:
                        await page.locator("#target").wait_for(state=state, timeout=100)
                    self.assertEqual(rejected.exception.code, "stale_reference")

    async def test_locator_wait_ignores_results_from_before_navigation(self):
        for state in ("attached", "detached", "visible", "hidden"):
            with self.subTest(state=state):
                page = self.page()
                navigated = False
                reads = 0

                async def evaluate(expression):
                    nonlocal navigated, reads
                    counting = "return els.length;" in expression
                    if counting and state in ("visible", "hidden"):
                        return 1
                    reads += 1
                    old_result = state in ("attached", "visible")
                    if not navigated:
                        navigated = True
                        page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
                        return int(old_result) if counting else old_result
                    return int(not old_result) if counting else not old_result

                page.evaluate = evaluate
                with self.assertRaises(NativeError) as rejected:
                    await page.locator("#target").wait_for(state=state, timeout=100)
                self.assertEqual(rejected.exception.code, "timeout")
                self.assertGreater(reads, 1)

    async def test_locator_value_discards_overtaken_read(self):
        for loss in ("navigation", "closed"):
            with self.subTest(loss=loss):
                page = self.page()
                reads = 0

                async def evaluate(expression):
                    nonlocal reads
                    if "return els.length;" in expression:
                        return 1
                    reads += 1
                    if reads == 1:
                        if loss == "navigation":
                            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
                        else:
                            page._event("Inspector.detached", {})
                        return "old"
                    return "new"

                page.evaluate = evaluate
                if loss == "navigation":
                    self.assertEqual(await page.locator("#target").inner_text(timeout=100), "new")
                    self.assertEqual(reads, 2)
                else:
                    with self.assertRaises(NativeError) as rejected:
                        await page.locator("#target").inner_text(timeout=100)
                    self.assertEqual(rejected.exception.code, "stale_reference")

    async def test_select_option_does_not_replay_after_navigation(self):
        page = self.page()
        writes = 0

        async def evaluate(expression):
            nonlocal writes
            if "return els.length;" in expression:
                return 1
            writes += 1
            if writes == 1:
                page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
            return ["chosen"]

        page.evaluate = evaluate
        with self.assertRaises(NativeError) as rejected:
            await page.locator("select").select_option("chosen", timeout=100)
        self.assertEqual(rejected.exception.code, "stale_reference")
        self.assertEqual(writes, 1)

    async def test_locator_click_discards_overtaken_hit_point(self):
        for loss in ("navigation", "closed"):
            with self.subTest(loss=loss):
                page = self.page()
                page.mouse = SimpleNamespace(click=AsyncMock())
                hits = 0

                async def evaluate(expression):
                    nonlocal hits
                    if "return els.length;" in expression:
                        return 1
                    if "scrollIntoView" not in expression:
                        return True
                    hits += 1
                    if hits == 1:
                        if loss == "navigation":
                            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
                        else:
                            page._event("Inspector.detached", {})
                        return {"x": 1, "y": 2}
                    return {"x": 3, "y": 4}

                page.evaluate = evaluate
                if loss == "navigation":
                    await page.locator("#target").click(timeout=100)
                    page.mouse.click.assert_awaited_once_with(3, 4, button="left", click_count=1)
                else:
                    with self.assertRaises(NativeError) as rejected:
                        await page.locator("#target").click(timeout=100)
                    self.assertEqual(rejected.exception.code, "stale_reference")
                    page.mouse.click.assert_not_awaited()

    async def frame_pointer_exchange(self, operation, *, stage=None, loss=None):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        owner = SimpleNamespace(monotonic_time=LocalProcessIdentity().monotonic_time)
        page = NativePage(SimpleNamespace(cdp=cdp, owner=owner), "page", "session",
                          "https://example.com/current")
        page.frame_id = "main"
        page._frame_ids.update(("main", "child"))
        frame = page._frame_document("child")
        frame.owner = NativeJSHandle(page, {"type": "object", "objectId": "retained-owner"})
        cdp.listeners.append(lambda message: page._event(message["method"], message.get("params", {})))
        inputs, released = [], []
        invalidated = False

        async def send(message):
            data = json.dumps({**message, "sessionId": "session"}).encode()
            header = bytes((0x81, len(data))) if len(data) < 126 else b"\x81\x7e" + struct.pack("!H", len(data))
            peer_writer.write(header + data)
            await peer_writer.drain()

        async def receive():
            first, second = await peer_reader.readexactly(2)
            self.assertEqual(first, 0x81)
            self.assertTrue(second & 0x80)
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", await peer_reader.readexactly(2))[0]
            mask = await peer_reader.readexactly(4)
            data = await peer_reader.readexactly(length)
            return json.loads(bytes(byte ^ mask[index & 3] for index, byte in enumerate(data)))

        async def serve():
            nonlocal invalidated
            while True:
                command = await receive()
                method, params = command["method"], command.get("params", {})
                current_stage = None
                if method == "Runtime.callFunctionOn":
                    if params["objectId"] == "retained-owner":
                        result = {"result": {"type": "object", "subtype": "node", "objectId": "temporary-owner"}}
                    else:
                        self.assertEqual(params["objectId"], "temporary-owner")
                        current_stage = "size"
                        result = {"result": {"type": "object", "value": [200, 100]}}
                elif method == "DOM.getBoxModel":
                    result = {"model": {"content": [100, 200, 500, 200, 500, 400, 100, 400]}}
                elif method == "Runtime.releaseObject":
                    released.append(params["objectId"])
                    if params["objectId"] == "temporary-owner":
                        current_stage = "release"
                    result = {}
                else:
                    self.assertEqual(method, "Input.dispatchMouseEvent")
                    inputs.append(params)
                    result = {}
                if stage is not None and current_stage == stage and not invalidated:
                    invalidated = True
                    if loss == "navigation":
                        await send({"method": "Page.frameNavigated", "params": {"frame": {
                            "id": "child", "parentId": "main", "url": "https://example.com/new"}}})
                    else:
                        await send({"method": "Page.frameDetached", "params": {"frameId": "child"}})
                await send({"id": command["id"], "result": result})

        server = asyncio.create_task(serve())
        try:
            error = None
            try:
                await asyncio.wait_for(getattr(frame.mouse, operation)(10, 20), 2)
            except NativeError as exc:
                error = exc.code
            if server.done():
                await server
            if stage is not None:
                self.assertTrue(invalidated)
                self.assertEqual((error, inputs), ("stale_reference", []))
            else:
                self.assertIsNone(error)
                self.assertEqual([event["type"] for event in inputs],
                                 ["mouseMoved"] if operation == "move" else
                                 ["mouseMoved", "mousePressed", "mouseReleased"])
                self.assertEqual([(event["x"], event["y"]) for event in inputs],
                                 [(120, 240)] * len(inputs))
            self.assertEqual(released.count("temporary-owner"), 1)
        finally:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
            await cdp.close()
            peer_writer.close()
            await peer_writer.wait_closed()

    async def test_frame_pointer_rejects_geometry_overtaken_by_frame_loss(self):
        for operation in ("move", "click"):
            for loss in ("navigation", "detached"):
                with self.subTest(operation=operation, loss=loss):
                    await self.frame_pointer_exchange(operation, stage="size", loss=loss)

    async def test_frame_pointer_rechecks_after_temporary_owner_release(self):
        for operation in ("move", "click"):
            for loss in ("navigation", "detached"):
                with self.subTest(operation=operation, loss=loss):
                    await self.frame_pointer_exchange(operation, stage="release", loss=loss)

    async def test_frame_pointer_scales_live_coordinates(self):
        for operation in ("move", "click"):
            with self.subTest(operation=operation):
                await self.frame_pointer_exchange(operation)

    async def test_frame_documents_rejects_navigation_during_enumeration(self):
        tree = {"frameTree": {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}]}}
        for stage in ("tree", "context"):
            with self.subTest(stage=stage):
                page = self.page()

                def navigate():
                    page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})

                async def send(*args, **kwargs):
                    if stage == "tree":
                        navigate()
                    return tree

                async def ensure_context():
                    if stage == "context":
                        navigate()

                page._send = send
                page._frame_document = Mock(return_value=SimpleNamespace(_ensure_context=ensure_context))
                with self.assertRaises(NativeError) as rejected:
                    await page.frame_documents()
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertNotIn("child", page._frame_ids)

    async def test_frame_documents_rejects_new_frame_during_enumeration(self):
        tree = {"frameTree": {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}]}}
        for stage in ("tree", "context"):
            with self.subTest(stage=stage):
                page = self.page()

                def attach():
                    page._event("Runtime.executionContextCreated", {
                        "context": {"id": 42, "auxData": {"isDefault": True, "frameId": "new"}},
                    })
                    page._event("Page.frameAttached", {"frameId": "new"})

                async def send(*args, **kwargs):
                    if stage == "tree":
                        attach()
                    return tree

                async def ensure_context():
                    if stage == "context":
                        attach()

                page._send = send
                page._frame_document = Mock(return_value=SimpleNamespace(_ensure_context=ensure_context))
                with self.assertRaises(NativeError) as rejected:
                    await page.frame_documents()
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertIn("new", page._frame_ids)

    async def test_child_context_creation_does_not_replace_new_document_context(self):
        page = self.page()
        page.context.cdp._task = asyncio.create_task
        page._frame_ids.add("child")
        old_frame = page._frame_document("child")

        async def send(method, params=None, **kwargs):
            self.assertEqual(method, "Page.createIsolatedWorld")
            page._event("Page.frameNavigated", {"frame": {"id": "child", "parentId": "main"}})
            page._event("Runtime.executionContextCreated", {
                "context": {"id": 202, "auxData": {"isDefault": True, "frameId": "child"}},
            })
            return {"executionContextId": 101}

        page._send = AsyncMock(side_effect=send)
        with self.assertRaises(NativeError) as rejected:
            await old_frame._ensure_context()
        self.assertEqual(rejected.exception.code, "stale_reference")
        self.assertEqual(page._frame_contexts["child"], 202)
        new_frame = page._frame_document("child")
        await new_frame._ensure_context()
        self.assertEqual(new_frame._execution_context, 202)
        self.assertEqual(page._send.await_count, 1)


    async def test_invalid_navigation_wait_state_rejected_before_dispatch(self):
        for operation in ("goto", "reload", "go_back", "wait_for_url"):
            with self.subTest(operation=operation):
                page = self.page()
                page.context.cdp.send = AsyncMock(return_value={
                    "frameId": "main", "entries": [{"id": 1}, {"id": 2}], "currentIndex": 1,
                })
                args = ("https://example.com/other",) if operation in ("goto", "wait_for_url") else ()
                with self.assertRaises(NativeError) as rejected:
                    await getattr(page, operation)(*args, wait_until="invalid", timeout=100)
                self.assertEqual(rejected.exception.code, "invalid_config")
                page.context.cdp.send.assert_not_awaited()
                page.evaluate.assert_not_awaited()

    async def test_go_back_rejects_target_lost_during_history_query(self):
        for loss in ("closed", "revoked", "failure"):
            with self.subTest(loss=loss):
                page = self.page()

                async def send(method, params, session, *, timeout):
                    if loss == "closed":
                        page._event("Inspector.detached", {})
                    elif loss == "revoked":
                        page.context.cdp.revoked = True
                    else:
                        page.context.cdp.failure = RuntimeError("connection lost")
                    return {"entries": [{"id": 1}], "currentIndex": 0}

                page.context.cdp.send = AsyncMock(side_effect=send)
                with self.assertRaises(NativeError) as rejected:
                    await page.go_back(timeout=100)
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertEqual(page.context.cdp.send.await_count, 1)

    async def test_go_back_returns_none_for_live_page_without_history(self):
        page = self.page()
        page.context.cdp.send = AsyncMock(return_value={"entries": [{"id": 1}], "currentIndex": 0})
        self.assertIsNone(await page.go_back(timeout=100))
        self.assertEqual(page.context.cdp.send.await_count, 1)

    async def test_go_back_rejects_malformed_target_without_navigating(self):
        for history in (
            {"entries": [{"id": 1}], "currentIndex": -1},
            {"entries": [], "currentIndex": 0},
            {"entries": [{"id": 1}], "currentIndex": 2},
            {"entries": [{}, {"id": 2}], "currentIndex": 1},
            {"entries": [None, {"id": 2}], "currentIndex": 1},
            {"entries": [{"id": True}, {"id": 2}], "currentIndex": 1},
        ):
            with self.subTest(history=history):
                page = self.page()
                page._send = AsyncMock(return_value=history)
                with self.assertRaises(NativeError) as rejected:
                    await page.go_back(timeout=100)
                self.assertEqual(rejected.exception.code, "protocol_error")
                self.assertEqual(page._send.await_count, 1)
                self.assertEqual(page._send.await_args.args[0], "Page.getNavigationHistory")

    async def test_page_initialization_preserves_frame_url_fragment(self):
        for frame, expected in (
            ({"id": "main", "url": "https://example.com/path", "urlFragment": "#section"},
             "https://example.com/path#section"),
            ({"id": "main", "url": "https://example.com/path#section"},
             "https://example.com/path#section"),
            ({"id": "main"}, "https://example.com/current"),
        ):
            with self.subTest(frame=frame):
                page = self.page()
                async def send(method, params=None, **kwargs):
                    return {"frameTree": {"frame": frame}} if method == "Page.getFrameTree" else {}
                page._send = send
                await page._initialize()
                self.assertEqual(page.url, expected)

    async def test_page_initialization_discards_frame_tree_overtaken_by_navigation(self):
        page = self.page()
        trees = 0

        async def send(method, params=None, **kwargs):
            nonlocal trees
            if method != "Page.getFrameTree":
                return {}
            trees += 1
            if trees == 1:
                page._event("Page.frameNavigated", {"frame": {
                    "id": "main", "url": "https://example.com/new",
                }})
                return {"frameTree": {"frame": {"id": "main", "url": "https://example.com/old"},
                                      "childFrames": [{"frame": {"id": "old-child"}}]}}
            return {"frameTree": {"frame": {"id": "main", "url": "https://example.com/new"},
                                  "childFrames": [{"frame": {"id": "new-child"}}]}}

        page._send = send
        await page._initialize()
        self.assertEqual(page.url, "https://example.com/new")
        self.assertEqual(page._frame_ids, {"main", "new-child"})
        self.assertEqual(trees, 2)

    async def test_page_initialization_discards_frame_tree_overtaken_by_target_url(self):
        for urls in (("https://example.com/new",),
                     ("https://example.com/new", "https://example.com/current")):
            with self.subTest(urls=urls):
                page = self.page()
                cdp = SimpleNamespace(listeners=[])
                context = NativeContext(page.context.owner, cdp)
                page.context = context
                context._pages[page.target_id] = page
                trees = 0

                async def send(method, params=None, **kwargs):
                    nonlocal trees
                    if method != "Page.getFrameTree":
                        return {}
                    trees += 1
                    if trees == 1:
                        for url in urls:
                            context._event({"method": "Target.targetInfoChanged", "params": {
                                "targetInfo": {"targetId": page.target_id, "url": url},
                            }})
                        return {"frameTree": {"frame": {"id": "main", "url": "https://example.com/old"}}}
                    return {"frameTree": {"frame": {"id": "main", "url": urls[-1]}}}

                page._send = send
                await page._initialize()
                self.assertEqual(page.url, urls[-1])
                self.assertEqual(trees, 2)

    async def test_commit_load_state_rejects_closed_or_revoked_page(self):
        for reason in ("closed", "revoked", "failure"):
            with self.subTest(reason=reason):
                page = self.page()
                if reason == "closed":
                    page._closed = True
                elif reason == "revoked":
                    page.context.cdp.revoked = True
                else:
                    page.context.cdp.failure = RuntimeError("connection lost")
                with self.assertRaises(NativeError) as rejected:
                    await page.wait_for_load_state("commit", timeout=250)
                self.assertEqual(rejected.exception.code, "stale_reference")
                page.evaluate.assert_not_awaited()

    async def test_load_state_rejects_page_closed_during_ready_state_query(self):
        for state, ready in (("domcontentloaded", "interactive"), ("load", "complete")):
            with self.subTest(state=state):
                page = self.page()

                async def evaluate(expression):
                    page._event("Inspector.detached", {})
                    return ready

                page.evaluate = evaluate
                with self.assertRaises(NativeError) as rejected:
                    await page.wait_for_load_state(state, timeout=250)
                self.assertEqual(rejected.exception.code, "stale_reference")

    async def test_load_state_ignores_ready_result_overtaken_by_main_frame_commit(self):
        for state in ("domcontentloaded", "load"):
            with self.subTest(state=state):
                page = self.page()
                second_query = asyncio.Event()
                release = asyncio.Event()
                calls = 0

                async def evaluate(expression):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
                        return "complete"
                    second_query.set()
                    await release.wait()
                    return "complete"

                page.evaluate = evaluate
                waiting = asyncio.create_task(page.wait_for_load_state(state, timeout=1000))
                try:
                    await asyncio.wait_for(second_query.wait(), 0.5)
                    self.assertFalse(waiting.done())
                    release.set()
                    await waiting
                finally:
                    if not waiting.done():
                        waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)

    async def test_wait_for_url_matches_exact_ipv6_and_existing_glob(self):
        page = self.page()
        page._url = "http://[::1]:8080/search?q=hello"
        await page.wait_for_url(page.url, timeout=250)
        await page.wait_for_url("http://*search*", timeout=250)

    async def test_wait_for_url_rejects_target_lost_during_query_without_publishing(self):
        for reason in ("closed", "revoked", "failure"):
            for matches in (False, True):
                with self.subTest(reason=reason, matches=matches):
                    page = self.page()
                    old_url = page.url
                    queried_url = "https://example.com/queried"

                    async def evaluate(expression):
                        if reason == "closed":
                            page._event("Inspector.detached", {})
                        elif reason == "revoked":
                            page.context.cdp.revoked = True
                        else:
                            page.context.cdp.failure = RuntimeError("connection lost")
                        return queried_url

                    page.evaluate = evaluate
                    with self.assertRaises(NativeError) as rejected:
                        await page.wait_for_url(queried_url if matches else "https://example.com/other",
                                                timeout=100, wait_until="commit")
                    self.assertEqual(rejected.exception.code, "stale_reference")
                    self.assertEqual(page.url, old_url)

    async def test_wait_for_url_discards_query_from_before_main_frame_commit(self):
        for same_document in (False, True):
            with self.subTest(same_document=same_document):
                page = self.page()
                old_url = page.url
                new_url = "https://example.com/new"
                calls = 0

                async def evaluate(expression):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if same_document:
                            page._event("Page.navigatedWithinDocument", {"frameId": "main", "url": new_url})
                        else:
                            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": new_url}})
                        return old_url
                    return new_url

                page.evaluate = evaluate
                with self.assertRaises(NativeError) as rejected:
                    await page.wait_for_url(old_url, timeout=100, wait_until="commit")
                self.assertEqual(rejected.exception.code, "timeout")
                self.assertEqual(page.url, new_url)
                self.assertGreater(calls, 1)

    async def test_navigation_url_reads_reject_overtaking_target_url_updates(self):
        for operation, stage in (("goto", "query"), ("wait", "query"), ("wait", "load")):
            with self.subTest(operation=operation, stage=stage):
                page = self.page()
                cdp = SimpleNamespace(listeners=[], send=AsyncMock())
                context = NativeContext(page.context.owner, cdp)
                page.context = context
                context._pages[page.target_id] = page
                old_url, new_url = page.url, "https://example.com/new"
                changed = False

                async def send(method, params, session, *, timeout):
                    page._event("Page.frameNavigated", {"frame": {"id": "main", "url": old_url}})
                    return {"frameId": "main"}

                async def evaluate(expression):
                    nonlocal changed
                    if not changed and ((stage == "query" and expression == "location.href")
                                        or (stage == "load" and expression == "document.readyState")):
                        changed = True
                        context._event({"method": "Target.targetInfoChanged", "params": {
                            "targetInfo": {"targetId": page.target_id, "url": new_url},
                        }})
                        return old_url if expression == "location.href" else "complete"
                    return "complete" if expression == "document.readyState" else (new_url if changed else old_url)

                cdp.send.side_effect = send
                page.evaluate = evaluate
                if operation == "goto":
                    await page.goto(old_url, timeout=500)
                else:
                    with self.assertRaises(NativeError) as rejected:
                        await page.wait_for_url(old_url, timeout=100, wait_until="load")
                    self.assertEqual(rejected.exception.code, "timeout")
                self.assertEqual(page.url, new_url)

    async def test_target_url_updates_overtake_reads_over_real_cdp(self):
        for operation, stage in (("goto", "query"), ("wait", "query"), ("wait", "load")):
            with self.subTest(operation=operation, stage=stage):
                client, peer = socket.socketpair()
                client.setblocking(False)
                peer.setblocking(False)
                reader, writer = await asyncio.open_connection(sock=client)
                peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
                cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
                owner = SimpleNamespace(monotonic_time=LocalProcessIdentity().monotonic_time)
                context = NativeContext(owner, cdp)
                old, new = "https://example.com/old", "https://example.com/new"
                page = NativePage(context, "page", "session", old)
                page.frame_id = "main"
                context._pages[page.target_id] = page
                queries = 0
                changed = False

                async def send(message, session=True):
                    data = json.dumps({**message, **({"sessionId": "session"} if session else {})}).encode()
                    header = bytes((0x81, len(data))) if len(data) < 126 else b"\x81\x7e" + struct.pack("!H", len(data))
                    peer_writer.write(header + data)
                    await peer_writer.drain()

                async def receive():
                    first, second = await peer_reader.readexactly(2)
                    self.assertEqual(first, 0x81)
                    self.assertTrue(second & 0x80)
                    length = second & 127
                    if length == 126:
                        length = struct.unpack("!H", await peer_reader.readexactly(2))[0]
                    mask = await peer_reader.readexactly(4)
                    data = await peer_reader.readexactly(length)
                    return json.loads(bytes(byte ^ mask[index & 3] for index, byte in enumerate(data)))

                async def serve():
                    nonlocal changed, queries
                    if operation == "goto":
                        command = await receive()
                        self.assertEqual(command["method"], "Page.navigate")
                        await send({"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": old}}})
                        await send({"id": command["id"], "result": {"frameId": "main"}})
                    while True:
                        command = await receive()
                        self.assertEqual(command["method"], "Runtime.evaluate")
                        expression = command["params"]["expression"]
                        is_url = "location.href" in expression
                        if is_url:
                            queries += 1
                        else:
                            self.assertIn("document.readyState", expression)
                        value = (new if changed else old) if is_url else "complete"
                        if not changed and ((stage == "query" and is_url) or (stage == "load" and not is_url)):
                            changed = True
                            await send({"method": "Target.targetInfoChanged", "params": {
                                "targetInfo": {"targetId": "page", "url": new}}}, session=False)
                        await send({"id": command["id"], "result": {"result": {"type": "string", "value": value}}})

                server = asyncio.create_task(serve())
                try:
                    if operation == "goto":
                        await page.goto(old, timeout=1000)
                    else:
                        with self.assertRaises(NativeError) as rejected:
                            await page.wait_for_url(old, timeout=150, wait_until="load")
                        self.assertEqual(rejected.exception.code, "timeout")
                    if server.done():
                        await server
                    self.assertTrue(changed)
                    self.assertGreater(queries, 1)
                    self.assertEqual(page.url, new)
                finally:
                    server.cancel()
                    await asyncio.gather(server, return_exceptions=True)
                    await cdp.close()
                    peer_writer.close()
                    await peer_writer.wait_closed()

    async def test_wait_for_url_rematches_after_navigation_during_load(self):
        for same_document in (False, True):
            with self.subTest(same_document=same_document):
                page = self.page()
                page.context.owner = SimpleNamespace(monotonic_time=asyncio.get_running_loop().time)
                old_url = page.url
                new_url = "https://example.com/new"
                committed = False

                async def evaluate(expression):
                    nonlocal committed
                    if expression == "location.href":
                        return new_url if committed else old_url
                    if not committed:
                        committed = True
                        if same_document:
                            page._event("Page.navigatedWithinDocument", {"frameId": "main", "url": new_url})
                        else:
                            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": new_url}})
                    return "complete"

                page.evaluate = evaluate
                with self.assertRaises(NativeError) as rejected:
                    await page.wait_for_url(old_url, timeout=100, wait_until="load")
                self.assertEqual(rejected.exception.code, "timeout")
                self.assertEqual(page.url, new_url)

    async def test_networkidle_restarts_after_main_frame_commit_between_polls(self):
        page = self.page()
        committed = asyncio.Event()
        calls = 0

        def commit():
            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.com/new"}})
            committed.set()

        async def ready_state(expression):
            nonlocal calls
            calls += 1
            if calls == 8:
                asyncio.get_running_loop().call_soon(commit)
            return "complete"

        page.evaluate = ready_state
        waiting = asyncio.create_task(page.wait_for_load_state("networkidle", timeout=2000))
        try:
            await asyncio.wait_for(committed.wait(), 1)
            await asyncio.sleep(0.25)
            self.assertFalse(waiting.done())
            await asyncio.wait_for(waiting, 1)
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_networkidle_restarts_after_request_between_polls(self):
        page = self.page()
        page.context._emit = Mock()
        started = asyncio.Event()
        calls = 0

        async def ready_state(expression):
            nonlocal calls
            calls += 1
            if calls == 8:
                page._event("Network.requestWillBeSent", {"requestId": "quick", "request": {}})
                page._event("Network.loadingFinished", {"requestId": "quick"})
                started.set()
            return "complete"

        page.evaluate = ready_state
        waiting = asyncio.create_task(page.wait_for_load_state("networkidle", timeout=2000))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0.25)
            self.assertFalse(waiting.done(), "request between polls must restart the idle window")
            await asyncio.wait_for(waiting, 1)
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


    async def test_commit_navigation_does_not_require_javascript_context(self):
        for same_document in (False, True):
            with self.subTest(same_document=same_document):
                page = self.page()
                destination = "https://example.com/current#section" if same_document else "https://example.com/new"
                page.evaluate = AsyncMock(side_effect=RuntimeError("execution context not yet available"))

                async def send(method, params, session, *, timeout):
                    if same_document:
                        page._event("Page.navigatedWithinDocument", {"frameId": "main", "url": destination})
                    else:
                        page._event("Page.frameNavigated", {"frame": {"id": "main", "url": destination}})
                    return {"frameId": "main"}

                page.context.cdp.send = AsyncMock(side_effect=send)
                self.assertIsNone(await page.goto(destination, wait_until="commit", timeout=1000))
                self.assertEqual(page.url, destination)
                page.evaluate.assert_not_awaited()

    async def test_commit_navigation_preserves_separate_url_fragment(self):
        page = self.page()
        page.evaluate = AsyncMock(side_effect=RuntimeError("execution context not yet available"))

        async def send(method, params, session, *, timeout):
            page._event("Page.frameNavigated", {"frame": {
                "id": "main", "url": "https://example.com/new?query=1", "urlFragment": "#section",
            }})
            return {"frameId": "main"}

        page.context.cdp.send = AsyncMock(side_effect=send)
        await page.goto("https://example.com/new?query=1#section", wait_until="commit", timeout=1000)
        self.assertEqual(page.url, "https://example.com/new?query=1#section")

    async def test_navigation_final_url_query_does_not_publish_stale_result(self):
        for change in ("closed", "revoked", "failure"):
            with self.subTest(change=change):
                page = self.page()
                destination = "https://example.com/first"

                async def send(method, params, session, *, timeout):
                    page._event("Page.frameNavigated", {"frame": {"id": "main", "url": destination}})
                    return {"frameId": "main"}

                async def evaluate(expression):
                    if expression == "document.readyState":
                        return "complete"
                    if change == "closed":
                        page._event("Inspector.detached", {})
                    elif change == "revoked":
                        page.context.cdp.revoked = True
                    elif change == "failure":
                        page.context.cdp.failure = RuntimeError("connection lost")
                    return destination

                page.context.cdp.send = AsyncMock(side_effect=send)
                page.evaluate = evaluate
                with self.assertRaises(NativeError) as rejected:
                    await page.goto(destination, timeout=1000)
                self.assertEqual(rejected.exception.code, "stale_reference")
                self.assertEqual(page.url, destination)

    async def navigation_with_final_commit(self, operation, *, completes):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
        owner = SimpleNamespace(monotonic_time=LocalProcessIdentity().monotonic_time)
        page = NativePage(SimpleNamespace(cdp=cdp, owner=owner), "page", "session",
                          "https://example.com/current")
        page.frame_id = "main"
        cdp.listeners.append(lambda message: page._event(message["method"], message.get("params", {})))
        first, final = "https://example.com/first", "https://example.com/final"
        rechecked, release = asyncio.Event(), asyncio.Event()

        async def send(message):
            data = json.dumps({**message, "sessionId": "session"}).encode()
            header = bytes((0x81, len(data))) if len(data) < 126 else b"\x81\x7e" + struct.pack("!H", len(data))
            peer_writer.write(header + data)
            await peer_writer.drain()

        async def receive():
            first_byte, second = await peer_reader.readexactly(2)
            self.assertEqual(first_byte, 0x81)
            self.assertTrue(second & 0x80)
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", await peer_reader.readexactly(2))[0]
            mask = await peer_reader.readexactly(4)
            data = await peer_reader.readexactly(length)
            return json.loads(bytes(byte ^ mask[index & 3] for index, byte in enumerate(data)))

        async def commit(url):
            await send({"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "url": url}}})
            await send({"method": "Network.responseReceived", "params": {
                "requestId": url, "frameId": "main", "type": "Document",
                "response": {"url": url, "status": 200, "headers": {}}}})

        async def serve():
            command = await receive()
            if operation == "go_back":
                self.assertEqual(command["method"], "Page.getNavigationHistory")
                await send({"id": command["id"], "result": {
                    "entries": [{"id": 1}, {"id": 2}], "currentIndex": 1}})
                command = await receive()
            self.assertEqual(command["method"], {
                "goto": "Page.navigate", "reload": "Page.reload",
                "go_back": "Page.navigateToHistoryEntry"}[operation])
            await commit(first)
            await send({"id": command["id"], "result": {"frameId": "main"}})
            redirected = False
            while True:
                command = await receive()
                self.assertEqual(command["method"], "Runtime.evaluate")
                if "document.readyState" in command["params"]["expression"]:
                    if redirected:
                        rechecked.set()
                        if completes:
                            await release.wait()
                    value = "complete" if not redirected or completes else "loading"
                else:
                    self.assertIn("location.href", command["params"]["expression"])
                    value = final if redirected else first
                    if not redirected:
                        redirected = True
                        await commit(final)
                await send({"id": command["id"], "result": {
                    "result": {"type": "string", "value": value}}})

        server = asyncio.create_task(serve())
        args = (first,) if operation == "goto" else ()
        navigation = asyncio.create_task(getattr(page, operation)(*args, timeout=1000 if completes else 150))
        observed = asyncio.create_task(rechecked.wait())
        try:
            if completes:
                done, _ = await asyncio.wait((navigation, observed, server), timeout=0.75,
                                             return_when=asyncio.FIRST_COMPLETED)
                if server in done:
                    await server
                self.assertIn(observed, done, "navigation must recheck the newly committed document")
                self.assertFalse(navigation.done(), "navigation returned before the final document loaded")
                release.set()
                response = await navigation
                self.assertEqual((page.url, response.url, response.status), (final, final, 200))
            else:
                with self.assertRaises(NativeError) as rejected:
                    await navigation
                self.assertEqual(rejected.exception.code, "timeout")
                self.assertTrue(rechecked.is_set())
                self.assertEqual(page.url, final)
        finally:
            for task in (navigation, observed, server):
                task.cancel()
            await asyncio.gather(navigation, observed, server, return_exceptions=True)
            await cdp.close()
            peer_writer.close()
            await peer_writer.wait_closed()

    async def test_site_icon_rejects_nonstring_fields_over_real_cdp(self):
        for result in (
            {"documentId": "doc", "revision": 1, "state": []},
            {"documentId": "doc", "revision": 1, "state": {}},
            {"documentId": "doc", "revision": 1, "state": "ready",
             "sha256": "0" * 64, "mime": [], "data": "AA=="},
            {"documentId": "doc", "revision": 1, "state": "ready",
             "sha256": "0" * 64, "mime": {}, "data": "AA=="},
        ):
            with self.subTest(result=result):
                client, peer = socket.socketpair()
                client.setblocking(False)
                peer.setblocking(False)
                reader, writer = await asyncio.open_connection(sock=client)
                peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
                cdp = _CDP(_WebSocket(reader, writer, LocalSecureRandom()))
                page = NativePage(SimpleNamespace(cdp=cdp), "page", "session", "https://example.com")
                icon_data = b"\x89PNG\r\n\x1a\n"
                initial = {"documentId": "doc", "revision": 0, "state": "ready",
                           "sha256": hashlib.sha256(icon_data).hexdigest(), "mime": "image/png",
                           "data": base64.b64encode(icon_data).decode()}

                async def serve():
                    for expected, reply in (
                        ("Page.subscribeSiteIcons", {}),
                        ("Page.getSiteIcon", initial),
                        ("Page.getSiteIcon", result),
                        ("Page.getSiteIcon", {"documentId": "doc", "revision": 2, "state": "missing"}),
                    ):
                        first, second = await peer_reader.readexactly(2)
                        self.assertEqual(first, 0x81)
                        self.assertTrue(second & 0x80)
                        length = second & 127
                        if length == 126:
                            length = struct.unpack("!H", await peer_reader.readexactly(2))[0]
                        mask = await peer_reader.readexactly(4)
                        raw = await peer_reader.readexactly(length)
                        command = json.loads(bytes(byte ^ mask[index & 3] for index, byte in enumerate(raw)))
                        self.assertEqual(command["method"], expected)
                        data = json.dumps({"id": command["id"], "sessionId": "session", "result": reply}).encode()
                        header = bytes((0x81, len(data))) if len(data) < 126 else b"\x81\x7e" + struct.pack("!H", len(data))
                        peer_writer.write(header + data)
                        await peer_writer.drain()

                server = asyncio.create_task(serve())
                try:
                    icon = await asyncio.wait_for(page.site_icon(), 2)
                    self.assertEqual(icon.data, icon_data)
                    with self.assertRaises(NativeError) as rejected:
                        await asyncio.wait_for(page.site_icon(), 2)
                    self.assertEqual(rejected.exception.code, "protocol_error")
                    self.assertIs(page.cached_site_icon, icon)
                    self.assertIsNone(await asyncio.wait_for(page.site_icon(), 2))
                    await asyncio.wait_for(server, 2)
                finally:
                    server.cancel()
                    await asyncio.gather(server, return_exceptions=True)
                    await cdp.close()
                    peer_writer.close()
                    await peer_writer.wait_closed()

    async def test_navigation_rechecks_load_after_final_url_commit_over_real_cdp(self):
        for operation in ("goto", "reload", "go_back"):
            with self.subTest(operation=operation):
                await self.navigation_with_final_commit(operation, completes=True)

    async def test_navigation_final_url_commit_keeps_original_deadline_over_real_cdp(self):
        await self.navigation_with_final_commit("goto", completes=False)

    async def test_child_and_context_events_cannot_complete_main_navigation(self):
        for operation in ("goto", "reload", "go_back"):
            with self.subTest(operation=operation):
                page = self.page()
                destination = page.url if operation == "reload" else "https://example.com/destination"

                def commit():
                    page._event("Page.frameNavigated", {"frame": {"id": "main", "url": destination}})
                    page._event("Network.responseReceived", {
                        "requestId": "loader", "frameId": "main", "type": "Document",
                        "response": {"url": destination, "status": 201, "headers": {}},
                    })

                scheduled = None

                async def send(method, params, session, *, timeout):
                    nonlocal scheduled
                    if method == "Page.getNavigationHistory":
                        return {"entries": [{"id": 1}, {"id": 2}], "currentIndex": 1}
                    self.assertIn(method, {"Page.navigate", "Page.reload", "Page.navigateToHistoryEntry"})
                    for event, payload in (
                        ("Runtime.executionContextsCleared", {}),
                        ("Runtime.executionContextDestroyed", {"executionContextId": 7}),
                        ("Page.frameNavigated", {"frame": {"id": "child", "parentId": "main"}}),
                        ("Page.navigatedWithinDocument", {"frameId": "child", "url": "about:blank#child"}),
                        ("Page.frameDetached", {"frameId": "child"}),
                    ):
                        page._event(event, payload)
                    scheduled = asyncio.get_running_loop().call_soon(commit)
                    return {"frameId": "main", "loaderId": "loader"}

                page.context.cdp.send = AsyncMock(side_effect=send)
                try:
                    args = (destination,) if operation == "goto" else ()
                    response = await getattr(page, operation)(*args, timeout=1000)
                    self.assertIsNotNone(response)
                    self.assertEqual((response.url, response.status), (destination, 201))
                    self.assertEqual(page.url, destination)
                finally:
                    if scheduled is not None:
                        scheduled.cancel()

    async def test_navigation_recovers_document_response_by_loader_id(self):
        page = self.page()
        page.frame_id = None
        destination = "https://example.com/destination"

        async def send(method, params, session, *, timeout):
            self.assertEqual(method, "Page.navigate")
            page._event("Network.responseReceived", {
                "requestId": "request-1", "loaderId": "loader-1", "frameId": "main",
                "type": "Document", "response": {"url": destination, "status": 201, "headers": {}},
            })
            page._event("Network.responseReceived", {
                "requestId": "loader-1", "loaderId": "loader-1", "frameId": "main",
                "type": "Script", "response": {"url": destination + "/script.js", "status": 404, "headers": {}},
            })
            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": destination}})
            return {"frameId": "main", "loaderId": "loader-1"}

        page.context.cdp.send = AsyncMock(side_effect=send)
        response = await page.goto(destination, wait_until="commit", timeout=1000)
        self.assertIsNotNone(response)
        self.assertEqual((response.url, response.status), (destination, 201))

    async def test_navigation_response_matches_final_committed_loader(self):
        for present in (True, False):
            with self.subTest(response_present=present):
                page = self.page()
                destination = "https://example.com/final"

                async def send(method, params, session, *, timeout):
                    self.assertEqual(method, "Page.navigate")
                    if present:
                        page._event("Network.responseReceived", {
                            "requestId": "request-a", "loaderId": "loader-a", "frameId": "main",
                            "type": "Document", "response": {"url": destination, "status": 201, "headers": {}},
                        })
                    page._event("Network.responseReceived", {
                        "requestId": "request-b", "loaderId": "loader-b", "frameId": "main",
                        "type": "Document", "response": {"url": "https://example.com/other", "status": 404, "headers": {}},
                    })
                    page._event("Page.frameNavigated", {"frame": {
                        "id": "main", "loaderId": "loader-a", "url": destination}})
                    return {"frameId": "main", "loaderId": "loader-a"}

                page.context.cdp.send = AsyncMock(side_effect=send)
                response = await page.goto(destination, wait_until="commit", timeout=1000)
                if present:
                    self.assertIsNotNone(response)
                    self.assertEqual((response.url, response.status), (destination, 201))
                else:
                    self.assertIsNone(response)

    async def test_same_document_commit_before_or_after_command_reply(self):
        for before_reply in (True, False):
            with self.subTest(before_reply=before_reply):
                page = self.page()
                destination = page.url + "#section"
                scheduled = None

                def commit():
                    page._event("Page.navigatedWithinDocument", {"frameId": "main", "url": destination})

                async def send(method, params, session, *, timeout):
                    nonlocal scheduled
                    if before_reply:
                        commit()
                    else:
                        scheduled = asyncio.get_running_loop().call_soon(commit)
                    return {"frameId": "main"}

                page.context.cdp.send = AsyncMock(side_effect=send)
                try:
                    self.assertIsNone(await page.goto(destination, timeout=1000))
                    self.assertEqual(page.url, destination)
                finally:
                    if scheduled is not None:
                        scheduled.cancel()

    async def test_detached_target_cannot_publish_an_observed_commit(self):
        page = self.page()

        async def send(method, params, session, *, timeout):
            page._event("Page.frameNavigated", {"frame": {"id": "main", "url": page.url}})
            page._event("Inspector.detached", {})
            return {"frameId": "main"}

        page.context.cdp.send = AsyncMock(side_effect=send)
        with self.assertRaises(NativeError) as rejected:
            await page.reload(timeout=1000)
        self.assertEqual(rejected.exception.code, "stale_reference")
        page.evaluate.assert_not_awaited()


@unittest.skipUnless(sys.platform == "darwin", "supervisor authentication uses Darwin peer audit tokens")
class SupervisedCDPEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir="/tmp")))
        self.home = root / ("long-home-" + "x" * 120)
        self.profile = self.home / "profiles" / "endpoint-lifecycle"
        self.data = self.profile / "data"
        self.data.mkdir(parents=True)
        self.config = self.profile / "config.json"
        self.config.write_bytes(b"committed profile bytes\n")
        (self.data / "user.sqlite").write_bytes(b"persistent user data\n")
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home), "TMPDIR": str(self.home)}))
        self.children = []
        self.endpoints = []
        self.endpoint_service = LocalUnixEndpoints()
        self.blocking_calls = LocalBlockingCalls()
        self.messages = LocalUnixMessages()
        self.identity = LocalProcessIdentity()
        self.processes = []
        self.servers = []
        self.channels = []
        self.addAsyncCleanup(self.finish)

    async def finish(self):
        for process in self.processes:
            if process._wait_task:
                process._wait_task.cancel()
        for task in self.servers:
            task.cancel()
        await asyncio.gather(*(p._wait_task for p in self.processes if p._wait_task),
                             *self.servers, return_exceptions=True)
        for channel in self.channels:
            channel.close()
        for child in self.children:
            if child.returncode is None:
                child.kill()
            await child.wait()
        # Fixture cleanup also waits for real exit; never guesses from EOF.
        for endpoint in self.endpoints:
            for entry in endpoint.directory.glob("keep-*"):
                entry.unlink()
            endpoint.dispose()

    async def start_child(self, *, domain=0, failure=None, launch_id=None):
        endpoint = self.endpoint_service.create()
        self.endpoints.append(endpoint)
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); "
            "s.listen(); print('bound',flush=True); sys.stdin.buffer.read(1)",
            str(endpoint.path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self.children.append(child)
        self.assertEqual(await asyncio.wait_for(child.stdout.readline(), 2), b"bound\n")
        client, host = socket.socketpair()
        host.setblocking(False)
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.application_domains = (0, 4, 5, 6, 7)
        supervisor._reserved_domains.add(domain)
        self.channels.extend([supervisor.socket, host])
        control, peer = socket.socketpair()
        self.channels.extend([control, peer])
        supervisor._cdp_endpoints.add(endpoint)
        process = _SupervisedProcess(supervisor, 71 + domain if launch_id is None else launch_id,
                                     51, control, "endpoint-lifecycle", domain, endpoint)
        self.processes.append(process)
        queried = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def read_exact(size):
            value = b""
            while len(value) < size:
                block = await loop.sock_recv(host, size - len(value))
                if not block:
                    raise EOFError("test supervisor closed")
                value += block
            return value

        async def serve():
            while True:
                length, = struct.unpack("!I", await read_exact(4))
                request = json.loads(await read_exact(length))
                queried.set()
                if failure == "eof":
                    host.close()
                    return
                if failure == "timeout":
                    await asyncio.Event().wait()
                if request["op"] == "stop" and failure not in {"wrong-child", "boolean-child", "float-child"}:
                    child.stdin.close()
                    await child.wait()
                reply = {"op": request["op"], "launch_id": process.launch_id,
                         "status": "running" if child.returncode is None else "exited"}
                if child.returncode is not None:
                    reply["exit_status"] = child.returncode
                if failure == "wrong-child":
                    reply.update(launch_id=process.launch_id + 1, status="exited", exit_status=0)
                if failure in {"boolean-child", "float-child"}:
                    reply.update(launch_id=True if failure == "boolean-child" else float(process.launch_id),
                                 status="exited", exit_status=0)
                raw = json.dumps(reply).encode()
                await loop.sock_sendall(host, struct.pack("!I", len(raw)) + raw)

        self.servers.append(asyncio.create_task(serve()))
        return process, child, endpoint, queried

    async def test_long_home_two_same_profile_domains_bind_unique_private_short_endpoints(self):
        first, child0, endpoint0, _ = await self.start_child()
        second, child4, endpoint4, _ = await self.start_child(domain=4)
        self.assertGreaterEqual(len(os.fsencode(self.data / ".native" / "cdp-0123456789abcdef.sock")), 104)
        self.assertNotEqual(endpoint0.directory, endpoint4.directory)
        self.assertEqual(first.profile_name, second.profile_name)
        for endpoint in (endpoint0, endpoint4):
            self.assertEqual(endpoint.directory.parent, Path("/tmp"))
            self.assertEqual(stat.S_IMODE(endpoint.directory.stat().st_mode), 0o700)
            self.assertLess(len(os.fsencode(endpoint.path)), 104)
            self.assertTrue(stat.S_ISSOCK(endpoint.path.stat().st_mode))
        await first.stop()
        self.assertEqual(await child0.wait(), 0)
        self.assertFalse(endpoint0.directory.exists())
        self.assertTrue(endpoint4.path.exists())
        self.assertIsNone(child4.returncode)
        await second.stop()
        self.assertEqual(self.config.read_bytes(), b"committed profile bytes\n")
        self.assertEqual((self.data / "user.sqlite").read_bytes(), b"persistent user data\n")
        self.assertFalse((self.data / ".native").exists())

    async def test_normal_session_close_disposes_only_exited_domain(self):
        process, child, endpoint, _ = await self.start_child()
        # Another domain keeps the shared Profile owner alive.
        process.supervisor.profile_sessions += 1
        session = NativeSession(process, {"network": {}}, self.config, proxy=None)
        await session.close()
        self.assertEqual(await child.wait(), 0)
        self.assertFalse(endpoint.directory.exists())
        self.assertNotIn(endpoint, process.supervisor._cdp_endpoints)
        self.assertEqual(process.supervisor.profile_sessions, 1)

    async def test_natural_exit_query_disposes_endpoint(self):
        process, child, endpoint, _ = await self.start_child()
        child.stdin.close()
        await child.wait()
        # OS exit alone has not yet delivered authenticated supervisor evidence.
        self.assertTrue(endpoint.path.exists())
        self.assertEqual(await process.wait(), 0)
        self.assertFalse(endpoint.directory.exists())
        self.assertNotIn(endpoint, process.supervisor._cdp_endpoints)

    async def test_waiter_timeout_retains_live_child_endpoint(self):
        process, child, endpoint, queried = await self.start_child(failure="timeout")
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(process.wait(), 0.05)
        await queried.wait()
        self.assertIsNone(child.returncode)
        self.assertIsNone(process.returncode)
        self.assertTrue(endpoint.path.exists())
        self.assertIn(endpoint, process.supervisor._cdp_endpoints)

    async def test_supervisor_eof_retains_endpoint_even_after_actual_unreported_exit(self):
        process, child, endpoint, _ = await self.start_child(failure="eof")
        child.stdin.close()
        await child.wait()
        with self.assertRaises(EOFError):
            await process.wait()
        self.assertIsNone(process.returncode)
        self.assertTrue(endpoint.path.exists())
        self.assertIn(endpoint, process.supervisor._cdp_endpoints)

    async def test_other_child_exit_reply_does_not_dispose_live_endpoint(self):
        process, child, endpoint, _ = await self.start_child(failure="wrong-child")
        with self.assertRaises(NativeError):
            await process.stop()
        self.assertIsNone(child.returncode)
        self.assertIsNone(process.returncode)
        self.assertTrue(endpoint.path.exists())
        self.assertIn(endpoint, process.supervisor._cdp_endpoints)

    async def test_coerced_exit_identity_does_not_release_live_domain(self):
        for launch_id, failure in ((1, "boolean-child"), (71, "float-child")):
            with self.subTest(reply=failure):
                process, child, endpoint, _ = await self.start_child(
                    launch_id=launch_id, failure=failure)
                try:
                    await process.stop()
                except NativeError as exc:
                    self.assertEqual(exc.code, "protocol_error")
                else:
                    self.fail(f"accepted {failure}: child_live={child.returncode is None}, "
                              f"endpoint_exists={endpoint.path.exists()}, returncode={process.returncode}")
                self.assertIsNone(child.returncode)
                self.assertIsNone(process.returncode)
                self.assertTrue(stat.S_ISSOCK(endpoint.path.stat().st_mode))
                self.assertIn(endpoint, process.supervisor._cdp_endpoints)
                self.assertIn(process.domain, process.supervisor._reserved_domains)

    async def test_exact_exit_never_recursively_removes_unexpected_contents(self):
        process, child, endpoint, _ = await self.start_child()
        unexpected = endpoint.directory / "keep-user-data"
        unexpected.write_bytes(b"not an owned socket")
        with self.assertRaises(OSError):
            await process.stop()
        self.assertEqual(await child.wait(), 0)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(unexpected.read_bytes(), b"not an owned socket")
        self.assertTrue(endpoint.directory.exists())
        self.assertIn(endpoint, process.supervisor._cdp_endpoints)

    async def test_old_exit_observation_cannot_release_reused_domain_reservation(self):
        process, child, endpoint, _ = await self.start_child(domain=5)
        self.assertIn(5, process.supervisor._reserved_domains)
        await process.stop()
        self.assertEqual(await child.wait(), 0)
        self.assertNotIn(5, process.supervisor._reserved_domains)
        # A replacement now owns the slot. Re-reading the old exact exit may
        # settle only its own endpoint, never the replacement's reservation.
        process.supervisor._reserved_domains.add(5)
        process._record_exit({"status": "exited", "exit_status": 0})
        self.assertIn(5, process.supervisor._reserved_domains)

    async def test_supervisor_descriptor_receipt_precedes_sender_close(self):
        client, host = socket.socketpair()
        host.setblocking(False)
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        source, peer = socket.socketpair()
        self.channels.extend([supervisor.socket, host, source, peer])
        payload = {"op": "ready", "launch_id": 71, "pid": os.getpid()}
        data = json.dumps({"fd_transfer_id": 1, "payload": payload}).encode()
        packet = struct.pack("!I", len(data)) + data
        sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", source.fileno()))])
        loop = asyncio.get_running_loop()
        if sent < len(packet):
            await loop.sock_sendall(host, packet[sent:])
        receiver = asyncio.create_task(asyncio.to_thread(supervisor._receive, 1))
        self.servers.append(receiver)

        async def receive():
            async def exact(size):
                result = b""
                while len(result) < size:
                    block = await loop.sock_recv(host, size - len(result))
                    if not block:
                        raise EOFError("descriptor receipt channel closed")
                    result += block
                return result
            size, = struct.unpack("!I", await exact(4))
            return json.loads(await exact(size))

        self.assertEqual(await asyncio.wait_for(receive(), 2), {"op": "fd_received", "fd_transfer_id": 1})
        source.close()
        value, fds = await asyncio.wait_for(receiver, 2)
        self.assertEqual(value, payload)
        adopted = fds[0]
        self.channels.append(adopted)
        adopted.settimeout(2)
        peer.settimeout(2)
        self.assertFalse(os.get_inheritable(adopted.fileno()))
        peer.sendall(b"PING")
        self.assertEqual(adopted.recv(4), b"PING")
        adopted.sendall(b"PONG")
        self.assertEqual(peer.recv(4), b"PONG")

    async def test_invalid_profile_control_descriptor_does_not_leak_pipe_reader(self):
        client, host = socket.socketpair()
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        self.channels.extend([supervisor.socket, host])
        reader, writer = os.pipe()
        try:
            value = {"fd_transfer_id": 1, "payload": {
                "op": "profile_started", "launch_id": 71, "pid": os.getpid(),
                "application_domains": [0],
            }}
            data = json.dumps(value).encode()
            packet = struct.pack("!I", len(data)) + data
            sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", reader))])
            if sent < len(packet):
                host.sendall(packet[sent:])
            os.close(reader)
            reader = -1
            with self.assertRaises((NativeError, OSError)):
                await supervisor.start_profile("invalid-descriptor", Path("/tmp"))
            with self.assertRaises(BrokenPipeError, msg="rejected control descriptor retained a pipe reader"):
                os.write(writer, b"x")
        finally:
            if reader >= 0:
                os.close(reader)
            os.close(writer)

    async def test_replayed_descriptor_receipt_closes_unadopted_endpoint(self):
        client, host = socket.socketpair()
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.fd_transfer_id = 2
        source, peer = socket.socketpair()
        self.channels.extend([supervisor.socket, host, source, peer])
        data = json.dumps({"fd_transfer_id": 1, "payload": {"op": "ready", "launch_id": 71}}).encode()
        packet = struct.pack("!I", len(data)) + data
        sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", source.fileno()))])
        if sent < len(packet):
            host.sendall(packet[sent:])
        with self.assertRaises(NativeError) as rejected:
            await asyncio.to_thread(supervisor._receive, 1)
        self.assertEqual(rejected.exception.code, "protocol_error")
        source.close()
        peer.settimeout(2)
        self.assertEqual(peer.recv(1), b"")

    async def test_invalid_receipt_or_failed_ack_closes_received_socket(self):
        for failure in ("json", "count", "ack"):
            with self.subTest(failure=failure):
                client, host = socket.socketpair()
                supervisor = _Supervisor(client, endpoints=self.endpoint_service,
                                         blocking_calls=self.blocking_calls, messages=self.messages,
                                         profile_command=profile_owner_command(),
                                         identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
                source, peer = socket.socketpair()
                self.channels.extend([client, host, source, peer])
                data = (b"{" if failure == "json" else
                        json.dumps({"fd_transfer_id": 1, "payload": {"op": "ready"}}).encode())
                packet = struct.pack("!I", len(data)) + data
                sent = host.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               struct.pack("=i", source.fileno()))])
                host.sendall(packet[sent:])
                source.close()
                if failure == "ack":
                    host.close()
                with self.assertRaises((ValueError, NativeError, OSError)):
                    await asyncio.to_thread(supervisor._receive, 0 if failure == "count" else 1)
                self.assertEqual(supervisor.fd_transfer_id, 0)
                peer.settimeout(2)
                self.assertEqual(peer.recv(1), b"")
                if failure == "ack":
                    self.assertEqual(client.fileno(), -1)

    async def test_cancellation_holds_channel_and_lock_until_real_worker_finishes(self):
        client, host = socket.socketpair()
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        self.channels.extend([supervisor.socket, host])
        reply = json.dumps({"op": "query", "launch_id": 71, "status": "running"}).encode()
        host.sendall(struct.pack("!I", len(reply)) + reply)
        loop = asyncio.get_running_loop()
        reached, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        request = supervisor._request

        def exchange_then_hold(op, launch_id):
            try:
                result = request(op, launch_id)
                loop.call_soon_threadsafe(reached.set)
                if not release.wait(5):
                    raise TimeoutError("test did not release real supervisor worker")
                return result
            finally:
                loop.call_soon_threadsafe(finished.set)

        with patch.object(supervisor, "_request", side_effect=exchange_then_hold):
            pending = asyncio.create_task(supervisor.request("query", 71))
            try:
                await asyncio.wait_for(reached.wait(), 2)
                pending.cancel()
                await asyncio.sleep(0)
                self.assertFalse(pending.done(), "cancellation abandoned a running supervisor worker")
                self.assertTrue(supervisor.lock.locked())
                self.assertGreaterEqual(supervisor.socket.fileno(), 0)
                pending.cancel()
                await asyncio.sleep(0)
                self.assertFalse(pending.done(), "repeated cancellation abandoned the worker")
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
                await asyncio.wait_for(finished.wait(), 2)
        self.assertTrue(pending.cancelled())
        self.assertFalse(supervisor.lock.locked())
        self.assertEqual(supervisor.socket.fileno(), -1)

    async def test_explicit_spawn_rejection_preserves_shared_supervisor_channel(self):
        client, host = socket.socketpair()
        host.setblocking(False)
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.owners_enrolled = True
        supervisor.application_domains = (0, 4, 5, 6, 7)
        control, peer = socket.socketpair()
        self.channels.extend([supervisor.socket, host, control, peer])
        loop = asyncio.get_running_loop()

        async def receive():
            packet = b""
            while len(packet) < 4:
                packet += await loop.sock_recv(host, 4 - len(packet))
            size, = struct.unpack("!I", packet)
            packet = b""
            while len(packet) < size:
                packet += await loop.sock_recv(host, size - len(packet))
            return json.loads(packet)

        async def send(value):
            packet = json.dumps(value).encode()
            await loop.sock_sendall(host, struct.pack("!I", len(packet)) + packet)

        async def host_owner():
            self.assertEqual(await receive(), {"op": "spawn", "domain": 4})
            await send({"op": "spawn_rejected", "code": "busy", "domain": 4})
            self.assertEqual(await receive(), {"op": "query", "launch_id": 91})
            await send({"op": "query", "launch_id": 91, "status": "running"})

        server = asyncio.create_task(host_owner())
        self.servers.append(server)
        with self.assertRaises(NativeError) as rejected:
            await supervisor.spawn({"op": "spawn", "domain": 4}, control)
        self.assertEqual(rejected.exception.code, "busy")
        self.assertGreaterEqual(supervisor.socket.fileno(), 0)
        self.assertEqual((await supervisor.request("query", 91))["status"], "running")
        await server

    async def test_uncertain_spawn_retains_short_endpoint_and_committed_long_profile(self):
        from mioa_core import profile
        self.stack.enter_context(patch.object(profile, "BASE", self.home))
        # Real pinned file identities suffice: this transport-failure test must
        # stop before execution, not pretend Python is a Mioa engine.
        asset = self.home / "font-fixture"
        asset.write_bytes(b"asset hash fixture")
        cfg = create_native_spec("endpoint-lifecycle", runtime="mioa", storage_path=self.data,
                                 binary=sys.executable, asset_paths={"fonts": [str(asset)]},
                                 artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
        committed = (json.dumps(cfg) + "\n").encode()
        self.config.write_bytes(committed)
        client, host = socket.socketpair()
        host.setblocking(False)
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.application_domains = (0, 4, 5, 6, 7)
        control, peer = socket.socketpair()
        self.channels.extend([supervisor.socket, host, control, peer])
        loop = asyncio.get_running_loop()
        received = asyncio.Event()

        async def abandon_spawn_after_receiving():
            # Receive an entire real launch request, then lose the host channel.
            # No fabricated ready/exit reply can establish child nonexistence.
            header = b""
            while len(header) < 4:
                header += await loop.sock_recv(host, 4 - len(header))
            size, = struct.unpack("!I", header)
            body = b""
            while len(body) < size:
                body += await loop.sock_recv(host, size - len(body))
            received.set()
            host.close()

        self.servers.append(asyncio.create_task(abandon_spawn_after_receiving()))
        try:
            with self.assertRaises(EOFError) as failed:
                await adopt_supervised_native("endpoint-lifecycle", cfg, supervisor, 51, control,
                                              random=LocalSecureRandom(), artifact_content=LocalArtifactContent(),
                                              connections=LocalStreamConnections(), files=LocalHostFiles())
        finally:
            self.endpoints.extend(supervisor._cdp_endpoints)
        self.assertTrue(received.is_set())
        self.assertEqual(len(supervisor._cdp_endpoints), 1)
        endpoint = next(iter(supervisor._cdp_endpoints))
        self.assertTrue(endpoint.directory.exists())
        self.assertFalse(endpoint.path.exists())
        self.assertLess(len(os.fsencode(endpoint.path)), 104)
        self.assertTrue(any(str(endpoint.directory) in note for note in failed.exception.__notes__))
        self.assertEqual(self.config.read_bytes(), committed)
        self.assertEqual((self.data / "user.sqlite").read_bytes(), b"persistent user data\n")

    async def test_pre_send_validation_failure_removes_only_never_issued_endpoint(self):
        from mioa_core import profile
        self.stack.enter_context(patch.object(profile, "BASE", self.home))
        asset = self.home / "font-fixture"
        asset.write_bytes(b"asset hash fixture")
        cfg = create_native_spec("endpoint-lifecycle", runtime="mioa", storage_path=self.data,
                                 binary=sys.executable, asset_paths={"fonts": [str(asset)]},
                                 artifact_content=LocalArtifactContent(), random=LocalSecureRandom())
        committed = (json.dumps(cfg) + "\n").encode()
        self.config.write_bytes(committed)
        client, host = socket.socketpair()
        supervisor = _Supervisor(client, endpoints=self.endpoint_service, blocking_calls=self.blocking_calls,
                                 messages=self.messages, profile_command=profile_owner_command(),
                                 identity=self.identity, managed_identity=LocalManagedPeerIdentity(), leases=LocalProfileLeases())
        supervisor.application_domains = (0, 4, 5, 6, 7)
        control, peer = socket.socketpair()
        self.channels.extend([supervisor.socket, host, peer])
        control.close()
        allocated = []
        create_endpoint = self.endpoint_service.create

        def allocate_real_endpoint():
            endpoint = create_endpoint()
            allocated.append(endpoint)
            self.endpoints.append(endpoint)
            return endpoint

        with patch.object(self.endpoint_service, "create", side_effect=allocate_real_endpoint):
            with self.assertRaises(NativeError) as failed:
                await adopt_supervised_native("endpoint-lifecycle", cfg, supervisor, 51, control,
                                              random=LocalSecureRandom(), artifact_content=LocalArtifactContent(),
                                              connections=LocalStreamConnections(), files=LocalHostFiles())
        self.assertEqual(failed.exception.code, "verification_failed")
        self.assertEqual(len(allocated), 1)
        self.assertFalse(allocated[0].directory.exists())
        self.assertFalse(supervisor._cdp_endpoints)
        host.settimeout(0.01)
        with self.assertRaises(socket.timeout):
            host.recv(1)
        self.assertEqual(self.config.read_bytes(), committed)
        self.assertEqual((self.data / "user.sqlite").read_bytes(), b"persistent user data\n")
