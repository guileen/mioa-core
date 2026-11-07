"""Bounded native wire and metadata-only capture regressions."""
import asyncio
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mioa_core.control import ControlState
from mioa_headless.native_service import MAX_MESSAGE, OBSERVER_WORLD, NativeProtocol
from mioa_headless.service import Session, ServiceError, decode_request


class Writer:
    def __init__(self):
        self.data = bytearray()
    def write(self, value):
        self.data.extend(value)
    async def drain(self):
        pass


class NativeWireTests(unittest.IsolatedAsyncioTestCase):
    def protocol(self):
        engine = SimpleNamespace(stopping=asyncio.Event())
        return NativeProtocol(engine, Path('/unused'), Path('/unused/control'), Path('/unused/frame'))

    async def test_native_wire_is_length_prefixed_and_shutdown_is_out_of_band(self):
        protocol = self.protocol()
        protocol.session = SimpleNamespace(state=AsyncMock(return_value={"pages": [], "page_id": "p"}))
        reader = asyncio.StreamReader()
        for request in ({"id": 1, "method": "state"}, {"id": 2, "method": "shutdown"}):
            body = json.dumps(request).encode()
            reader.feed_data(struct.pack('<I', len(body)) + body)
        writer = Writer()
        await protocol.accept(reader, writer)
        length, = struct.unpack_from('<I', writer.data)
        reply = json.loads(writer.data[4:4 + length])
        self.assertEqual(reply, {"id": 1, "result": {"pages": [], "page_id": "p"}})
        self.assertEqual(protocol.shutdown_id, 2)
        self.assertTrue(protocol.engine.stopping.is_set())

    async def test_declared_oversize_is_rejected_without_reading_body(self):
        protocol = self.protocol()
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack('<I', MAX_MESSAGE + 1))
        writer = Writer()
        await asyncio.wait_for(protocol.accept(reader, writer), 1)
        self.assertTrue(protocol.engine.stopping.is_set())
        self.assertFalse(writer.data)

    async def test_native_envelope_and_resize_are_bounded(self):
        request = b'{"id":1,"method":"resize","params":{"width":800,"height":600,"device_scale_factor":1}}'
        self.assertEqual(decode_request(request, native=True)['method'], 'resize')
        for body in (b'', b' ' * (MAX_MESSAGE + 1), b'{"id":1,"method":"state","params":{"x":NaN}}'):
            with self.assertRaises(ServiceError):
                decode_request(body, native=True)
        with self.assertRaises(ServiceError):
            decode_request(request + b'\n')

    async def test_priority_stop_is_correlated_while_normal_navigation_waits(self):
        protocol = self.protocol()
        entered, release, stop_entered, stop_release = (asyncio.Event() for _ in range(4))
        replies = asyncio.Queue()
        async def navigate(params):
            entered.set()
            await release.wait()
            return {"status": "stopped", "operation": "navigate"}
        async def stop(params):
            stop_entered.set()
            await stop_release.wait()
            return {"status": "stopped", "operation": "stop"}
        protocol.session = SimpleNamespace(action=navigate, stop=stop,
                                          state=AsyncMock(return_value={"page_id": "p"}))
        protocol.emit = replies.put
        reader = asyncio.StreamReader()
        def feed(identifier, method, params=None):
            body = json.dumps({"id": identifier, "method": method, "params": params or {}}).encode()
            reader.feed_data(struct.pack("<I", len(body)) + body)
        client = asyncio.create_task(protocol.accept(reader, Writer()))
        try:
            feed(10, "action", {"operation": "navigate"})
            await asyncio.wait_for(entered.wait(), 1)
            feed(11, "stop", {"page_id": "p", "owner_revision": 3})
            await asyncio.wait_for(stop_entered.wait(), 1)
            # Bound the interrupt lane; a second stop must not reach Session.
            feed(12, "stop", {"page_id": "p", "owner_revision": 3})
            rejected = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual(rejected["id"], 12)
            self.assertEqual(rejected["error"]["code"], "busy")
            feed(13, "state")
            protocol.session.state.assert_not_awaited()
            stop_release.set()
            reply = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual(reply, {"id": 11, "result": {"status": "stopped", "operation": "stop"}})
            self.assertFalse(release.is_set())
            release.set()
            navigation = await asyncio.wait_for(replies.get(), 1)
            state = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual((navigation["id"], state["id"]), (10, 13))
            feed(14, "shutdown")
            await asyncio.wait_for(client, 1)
            self.assertEqual(protocol.shutdown_id, 14)
        finally:
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)

    async def test_eof_cancels_both_request_lanes(self):
        protocol = self.protocol()
        entered, stop_entered = asyncio.Event(), asyncio.Event()
        async def navigate(_):
            entered.set()
            await asyncio.Event().wait()
        async def stop(_):
            stop_entered.set()
            await asyncio.Event().wait()
        protocol.session = SimpleNamespace(action=navigate, stop=stop)
        reader = asyncio.StreamReader()
        client = asyncio.create_task(protocol.accept(reader, Writer()))
        try:
            for identifier, method, event in ((1, "action", entered), (2, "stop", stop_entered)):
                body = json.dumps({"id": identifier, "method": method}).encode()
                reader.feed_data(struct.pack("<I", len(body)) + body)
                await asyncio.wait_for(event.wait(), 1)
            reader.feed_eof()
            await asyncio.wait_for(client, 1)
            self.assertTrue(protocol.engine.stopping.is_set())
            self.assertTrue(protocol.normal.cancelled())
            self.assertTrue(protocol.interrupt.cancelled())
        finally:
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)

    async def test_navigation_metadata_uses_native_history_and_event_cached_loading(self):
        protocol = self.protocol()
        page = SimpleNamespace(session_id="s", frame_id="f", observation_id=1,
                               evaluate=AsyncMock(return_value="complete"),
                               _send=AsyncMock(return_value={"currentIndex": 1, "entries": [{"id": 1}, {"id": 2}, {"id": 3}]}))
        protocol.engine.context = SimpleNamespace(pages=[page])
        protocol.invalidate = lambda: None
        session = Session(protocol.engine)
        expected = {"state": "known", "can_go_back": True, "can_go_forward": True, "loading": False}
        self.assertEqual(await session.navigation(page), expected)
        self.assertEqual(await session.navigation(page), expected)
        page._send.assert_awaited_once_with("Page.getNavigationHistory")
        page.evaluate.assert_awaited_once_with("document.readyState")
        protocol.event({"method": "Page.frameStartedLoading", "sessionId": "s", "params": {"frameId": "f"}})
        self.assertEqual(await session.navigation(page), {**expected, "loading": True})
        protocol.event({"method": "Page.frameStoppedLoading", "sessionId": "s", "params": {"frameId": "f"}})
        self.assertEqual(await session.navigation(page), expected)
        self.assertEqual(page.evaluate.await_count, 1)
        unavailable = await session.navigation(SimpleNamespace())
        self.assertEqual(unavailable, {"state": "unavailable", "can_go_back": None, "can_go_forward": None, "loading": None})
    async def test_event_burst_coalesces_and_advances_semantic_epoch(self):
        protocol = self.protocol()
        page = SimpleNamespace(session_id='s', native_visual_epoch=0)
        protocol.engine.context = SimpleNamespace(pages=[page])
        protocol.emit = AsyncMock()
        for _ in range(10):
            protocol.event({'method': 'Runtime.bindingCalled', 'sessionId': 's',
                            'params': {'name': '__pandaNativeInvalidated', 'payload': ''}})
        await protocol.notification
        protocol.emit.assert_awaited_once_with({'event': 'invalidated'})
        self.assertEqual(page.native_visual_epoch, 10)
        protocol.event({'method': 'Runtime.consoleAPICalled', 'sessionId': 's', 'params': {}})
        self.assertTrue(protocol.notification.done())

    async def test_invalidation_during_emit_is_not_lost(self):
        protocol = self.protocol()
        emitted = []
        async def emit(value):
            emitted.append(value)
            if len(emitted) == 1:
                protocol.invalidate()
        protocol.emit = emit
        protocol.invalidate()
        await protocol.notification
        self.assertEqual(emitted, [{"event": "invalidated"}, {"event": "invalidated"}])

    async def test_observer_binding_and_script_use_real_isolated_world(self):
        protocol = self.protocol()
        page = SimpleNamespace(session_id="s", frame_id="f")
        page.frame_documents = AsyncMock(return_value=[page])
        async def send(method, params=None):
            return {"executionContextId": 7} if method == "Page.createIsolatedWorld" else {}
        page._send = AsyncMock(side_effect=send)
        protocol.invalidate = lambda: None
        await protocol.observe_page(page)
        page._send.assert_any_await("Runtime.addBinding",
            {"name": "__pandaNativeInvalidated", "executionContextName": OBSERVER_WORLD})
        page._send.assert_any_await("Page.createIsolatedWorld", {"frameId": "f", "worldName": OBSERVER_WORLD})
        scripts = [args.args[1] for args in page._send.await_args_list if args.args[0] == "Page.addScriptToEvaluateOnNewDocument"]
        self.assertEqual(scripts[0]["worldName"], OBSERVER_WORLD)
        self.assertEqual(protocol.observed_pages, {"s"})

    async def test_native_capture_has_no_png_or_base64_payload_and_evicts_old_generations(self):
        page = SimpleNamespace(observation_id=1, native_visual_epoch=0, title=AsyncMock(return_value="Example"),
                               url="https://example.com", site_icon=AsyncMock(return_value=None))
        result = {'page_id': 'p', 'frame_id': 'f', 'observation_id': 'o',
                  'viewport': {'width': 20, 'height': 10}, 'width': 20, 'height': 10, 'stride': 80,
                  'dpr_milli': 1000, 'theme_edge_rgba': 0xff112233, 'caret_rect': [1., 2., 0., 4.],
                  'caret_kind': 'native-shaped-insertion',
                  'render_fingerprint': 'a' * 64, 'focused_frame_id': 'f', 'focused_element_id': 'node-123'}
        control = ControlState(page_id='p', frame_id='f')
        refs = SimpleNamespace(viewport=AsyncMock(return_value=result), frame_id=lambda _: 'f')
        engine = SimpleNamespace(control=control, refs=refs, page=lambda: page, name="test",
                                 refresh_pages=AsyncMock(), context=SimpleNamespace(pages=[page]),
                                 operations=SimpleNamespace(page_id=lambda _: 'p'),
                                 native=SimpleNamespace(config={"resolved": {"device_scale_factor": 1}}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        class Ring:
            path = Path(directory.name) / "frame"
            back = 2
            seq = 0
            def publish(self, metadata):
                self.seq += 1
                return self.seq
        ring = Ring()
        session = Session(engine, ring)
        self.addCleanup(session.close)
        await session.control({'operation': 'request_human'})
        frames = [await session.capture() for _ in range(33)]
        self.assertNotIn('png', frames[-1])
        self.assertNotIn('data', frames[-1])
        self.assertEqual(len(session.recent_frame_ids), 32)
        action = {'operation': 'mouse.click', 'args': {'x': 1, 'y': 1}, 'page_id': 'p', 'frame_id': 'f',
                  'observation_id': 'o', 'owner_revision': control.owner_revision}
        with self.assertRaises(ServiceError):
            session.validate_action({**action, 'image_id': frames[0]['image_id'], 'frame_seq': 1})
        session.validate_action({**action, 'image_id': frames[1]['image_id'], 'frame_seq': 2})
        current = {**action, 'image_id': frames[-1]['image_id'], 'frame_seq': 33}
        for extra in ({"document_epoch": 2}, {"viewport_epoch": 1}, {"focused_element_id": "node-other"},
                      {"focused_frame_id": "other-frame"}):
            with self.subTest(extra=extra), self.assertRaises(ServiceError):
                session.validate_action({**current, **extra})
