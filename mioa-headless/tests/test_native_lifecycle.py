"""Owner EOF, native death, and durable native shutdown regressions."""
import asyncio
from contextlib import ExitStack
import io
import json
import os
import socket
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mioa_core.native import NativeContext, NativeError, NativePage, _CDP, _WebSocket
from mioa_headless.native_service import NativeProtocol


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
        self.native = SimpleNamespace(process=process, pid=123, close=self.close_native)
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


class WebSocketCloseOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_command_cannot_follow_close_frame(self):
        client, peer = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        channel = _WebSocket(reader, writer)
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


class NativeTransportCloseTests(unittest.IsolatedAsyncioTestCase):
    async def blocked_close(self, *, cancel):
        client, peer = socket.socketpair()
        client.setblocking(False)
        peer.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client)
        peer_reader, peer_writer = await asyncio.open_connection(sock=peer)
        cdp = _CDP(_WebSocket(reader, writer))
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
