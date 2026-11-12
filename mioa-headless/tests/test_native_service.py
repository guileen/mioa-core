"""Bounded native wire and metadata-only capture regressions."""
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mioa_core.control import ControlError, ControlState
from mioa_core.engine import Engine
from mioa_core.native import NativePage
from mioa_core.refs import ActionError, RefStore, _CHECK_JS
from mioa_headless.frame_ring import FrameRing, HEADER, SLOT_SIZE
from mioa_headless.native_service import MAX_MESSAGE, OBSERVER_WORLD, NativeProtocol
from mioa_headless.service import Session, ServiceError, decode_request
from mioa_os import LocalSecureRandom


class Writer:
    def __init__(self):
        self.data = bytearray()
    def write(self, value):
        self.data.extend(value)
    async def drain(self):
        pass


class NativeWireTests(unittest.IsolatedAsyncioTestCase):
    def protocol(self):
        engine = object.__new__(Engine)
        page = NativePage(SimpleNamespace(cdp=SimpleNamespace(execution_domain=0)),
                          "p", "s", "about:blank")
        engine.stopping = asyncio.Event()
        engine.context = SimpleNamespace(pages=[page])
        engine.active = page
        engine.control = ControlState(page_id="p", frame_id="f")
        engine.operations = SimpleNamespace(page_id=lambda page: page.page_id)
        return NativeProtocol(engine, Path('/unused'), Path('/unused/control'), Path('/unused/frame'))

    async def test_reused_domain_and_session_reject_old_incarnation_events(self):
        protocol = self.protocol()
        page = protocol.engine.active
        page.context.cdp.execution_epoch = 82
        event = {"method": "Page.nativeFrameInvalidated", "sessionId": "s",
                 "_execution_domain": 0, "_execution_epoch": 81, "params": {}}
        previous = protocol.invalidation_revision
        protocol.event(event)
        self.assertEqual(protocol.invalidation_revision, previous)
        self.assertFalse(hasattr(page, "native_visual_epoch"))
        protocol.event({**event, "_execution_epoch": 82})
        self.assertEqual(page.native_visual_epoch, 1)
        self.assertEqual(protocol.invalidation_revision, previous + 1)
        protocol.notification.cancel()
        await asyncio.gather(protocol.notification, return_exceptions=True)

    async def test_queued_old_source_is_rejected_before_current_domain_dispatch(self):
        protocol = self.protocol()
        old_page = NativePage(SimpleNamespace(cdp=SimpleNamespace(execution_domain=0)),
                              "old", "s", "about:blank")
        old_page._headless_stop_revoked = True
        responses = []
        async def emit(value):
            responses.append(value)
        protocol.emit = emit
        await protocol.respond({"id": 9, "method": "page_action", "params": {},
                                "_source_page": old_page, "_execution_domain": 0})
        self.assertEqual(responses[0]["error"]["code"], "stale_reference")

    async def test_active_domain_uses_resolved_page_and_fences_its_stop_without_inventory(self):
        page = NativePage(SimpleNamespace(cdp=SimpleNamespace(execution_domain=4)),
                          "p4", "s4", "about:blank")
        engine = SimpleNamespace(page=lambda: page,
                                 operations=SimpleNamespace(page_id=lambda item: item.page_id))
        session = Session(engine)
        self.assertEqual(session.active_execution_domain(), 4)
        session.stopping_domains.add(0)
        self.assertEqual(session.active_execution_domain(), 4)
        session.stopping_domains.add(4)
        with self.assertRaises(ServiceError) as caught:
            session.active_execution_domain()
        self.assertEqual(caught.exception.code, "stale_reference")

    async def test_legacy_active_page_does_not_require_native_context(self):
        page = SimpleNamespace(page_id="legacy-page")
        engine = SimpleNamespace(page=lambda: page,
                                 operations=SimpleNamespace(page_id=lambda item: item.page_id))
        self.assertEqual(Session(engine).active_execution_domain(), 0)

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
        protocol.session = SimpleNamespace(action=navigate, stop=stop, active_execution_domain=lambda: 0,
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
            # Local metadata/control state must not wait on A's native Stop.
            state = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual(state["id"], 13)
            protocol.session.state.assert_awaited_once()
            self.assertFalse(stop_release.is_set())
            self.assertFalse(release.is_set())
            stop_release.set()
            reply = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual(reply, {"id": 11, "result": {"status": "stopped", "operation": "stop"}})
            self.assertFalse(release.is_set())
            release.set()
            navigation = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual(navigation["id"], 10)
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
        protocol.session = SimpleNamespace(action=navigate, stop=stop, active_execution_domain=lambda: 0)
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
            self.assertTrue(all(task.cancelled() for _, task in protocol.lanes.values()))
            self.assertTrue(all(task.cancelled() for task in protocol.interrupts.values()))
        finally:
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)

    async def test_navigation_metadata_uses_native_history_and_event_cached_loading(self):
        protocol = self.protocol()
        page = SimpleNamespace(session_id="s", frame_id="f", observation_id=1,
                               evaluate=AsyncMock(return_value="complete"),
                               context=SimpleNamespace(cdp=SimpleNamespace(execution_domain=0)),
                               _send=AsyncMock(return_value={"currentIndex": 1, "entries": [{"id": 1}, {"id": 2}, {"id": 3}]}))
        protocol.engine.context = SimpleNamespace(pages=[page])
        protocol.engine.operations = SimpleNamespace(page_id=lambda page: "p")
        protocol.invalidate = lambda *_: None
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
        page = SimpleNamespace(session_id='s', native_visual_epoch=0,
                               context=SimpleNamespace(cdp=SimpleNamespace(execution_domain=0)))
        protocol.engine.context = SimpleNamespace(pages=[page])
        protocol.engine.operations = SimpleNamespace(page_id=lambda page: "p")
        protocol.emit = AsyncMock()
        for _ in range(10):
            protocol.event({'method': 'Runtime.bindingCalled', 'sessionId': 's',
                            'params': {'name': '__pandaNativeInvalidated', 'payload': ''}})
        await protocol.notification
        protocol.emit.assert_awaited_once_with({'event': 'invalidated', 'page_id': 'p', 'revision': 10})
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
        self.assertEqual(emitted, [{"event": "invalidated", "revision": 1}, {"event": "invalidated", "revision": 2}])

    async def test_first_invalidation_reaches_wire_before_burst_cooldown(self):
        protocol = self.protocol()
        local, remote = socket.socketpair()
        reader, peer = await asyncio.open_connection(sock=local)
        _, writer = await asyncio.open_connection(sock=remote)
        protocol.writer = writer
        cooldowns = asyncio.Queue()

        async def cooldown(_):
            release = asyncio.Event()
            await cooldowns.put(release)
            await release.wait()

        async def receive():
            length, = struct.unpack("<I", await reader.readexactly(4))
            return json.loads(await reader.readexactly(length))

        try:
            with patch("mioa_headless.native_service.asyncio.sleep", side_effect=cooldown):
                protocol.invalidate("p")
                first_window = await asyncio.wait_for(cooldowns.get(), 1)
                # The timer is deliberately held: no wall-clock performance threshold.
                self.assertEqual(await asyncio.wait_for(receive(), 1),
                                 {"event": "invalidated", "page_id": "p", "revision": 1})
                for _ in range(100):
                    protocol.invalidate("p")
                    protocol.invalidate("q")
                first_window.set()
                next_window = await asyncio.wait_for(cooldowns.get(), 1)
                trailing = [await asyncio.wait_for(receive(), 1) for _ in range(2)]
                self.assertEqual({value["page_id"] for value in trailing}, {"p", "q"})
                self.assertTrue(all(value["event"] == "invalidated" for value in trailing))
                self.assertTrue(all(value["revision"] == 201 for value in trailing))
                next_window.set()
                await asyncio.wait_for(protocol.notification, 1)
            writer.close()
            await writer.wait_closed()
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        finally:
            if protocol.notification is not None:
                protocol.notification.cancel()
                await asyncio.gather(protocol.notification, return_exceptions=True)
            writer.close()
            peer.close()
            await asyncio.gather(writer.wait_closed(), peer.wait_closed(), return_exceptions=True)

    async def test_frame_watermark_excludes_invalidation_while_capture_is_running(self):
        protocol = self.protocol()
        local, remote = socket.socketpair()
        reader, peer = await asyncio.open_connection(sock=local)
        _, writer = await asyncio.open_connection(sock=remote)
        protocol.writer = writer
        entered, release = asyncio.Event(), asyncio.Event()

        async def capture(_):
            entered.set()
            await release.wait()
            return {"page_id": "p"}

        protocol.session = SimpleNamespace(capture=capture)
        protocol.invalidation_revision = 5
        task = asyncio.create_task(protocol.respond({"id": 1, "method": "frame", "params": {}}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            protocol.invalidate("p")
            release.set()
            received = []
            for _ in range(2):
                length, = struct.unpack("<I", await asyncio.wait_for(reader.readexactly(4), 1))
                received.append(json.loads(await asyncio.wait_for(reader.readexactly(length), 1)))
            frame, = [packet["result"] for packet in received if "result" in packet]
            notification, = [packet for packet in received if packet.get("event") == "invalidated"]
            # Capturing revision 5 cannot retire the newer update, regardless
            # of the order in which the two packets reach the viewer.
            self.assertLess(frame["invalidation_revision"], notification["revision"])
            self.assertEqual(frame["invalidation_revision"], 5)
            await task
        finally:
            release.set()
            task.cancel()
            if protocol.notification is not None:
                protocol.notification.cancel()
            await asyncio.gather(task, *([protocol.notification] if protocol.notification else []),
                                 return_exceptions=True)
            writer.close()
            peer.close()
            await asyncio.gather(writer.wait_closed(), peer.wait_closed(), return_exceptions=True)

    async def test_observer_binding_and_script_use_real_isolated_world(self):
        protocol = self.protocol()
        page = SimpleNamespace(session_id="s", frame_id="f",
                               context=SimpleNamespace(cdp=SimpleNamespace(execution_domain=0)))
        page.frame_documents = AsyncMock(return_value=[page])
        async def send(method, params=None):
            return {"executionContextId": 7} if method == "Page.createIsolatedWorld" else {}
        page._send = AsyncMock(side_effect=send)
        protocol.invalidate = lambda *_: None
        protocol.engine.operations = SimpleNamespace(page_id=lambda page: "p")
        await protocol.observe_page(page)
        page._send.assert_any_await("Runtime.addBinding",
            {"name": "__pandaNativeInvalidated", "executionContextName": OBSERVER_WORLD})
        page._send.assert_any_await("Page.createIsolatedWorld", {"frameId": "f", "worldName": OBSERVER_WORLD})
        scripts = [args.args[1] for args in page._send.await_args_list if args.args[0] == "Page.addScriptToEvaluateOnNewDocument"]
        self.assertEqual(scripts[0]["worldName"], OBSERVER_WORLD)
        self.assertEqual(protocol.observed_pages, {(0, "s")})

    async def test_native_capture_has_no_png_or_base64_payload_and_evicts_old_generations(self):
        page = SimpleNamespace(observation_id=1, native_visual_epoch=0, title=AsyncMock(return_value="Example"),
                               url="https://example.com", site_icon=AsyncMock(return_value=None))
        page.is_closed = lambda: False
        page.context = SimpleNamespace(cdp=SimpleNamespace(execution_domain=0))
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


class DomainExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = []
        self.generation = {0: 0, 4: 0}
        pages = []
        for domain in (0, 4):
            async def send(method, params=None, session=None, *, timeout=30, domain=domain):
                self.calls.append((domain, method, params))
                if domain == 0 and method == "Page.captureBrokerFrame":
                    self.entered.set()
                    await self.release.wait()
                if method == "Page.captureBrokerFrame":
                    self.generation[domain] += 1
                    return {"capture_stable": True, "input_guard": "native-exact-v1",
                            "visual_generation": self.generation[domain],
                            "viewport": {"width": 100, "height": 80}}
                return {}
            context = SimpleNamespace(cdp=SimpleNamespace(execution_domain=domain, send=send))
            page = NativePage(context, f"p{domain}", f"s{domain}", "about:blank")
            page.frame_id, page.observation_id = f"f{domain}", 1
            pages.append(page)
        self.engine = object.__new__(Engine)
        self.engine.context = SimpleNamespace(pages=pages)
        self.engine.active = pages[0]
        self.engine.stopping = asyncio.Event()
        self.engine.operations = SimpleNamespace(page_id=lambda page: page.page_id)
        self.engine.refs = SimpleNamespace(frame_id=lambda page: page.frame_id, forget_page=AsyncMock(), page_visuals={})
        self.engine.control = ControlState(page_id="p0", frame_id="f0")
        self.session = Session(self.engine)
        await self.session.control({"operation": "request_human"})

    def raster(self, page_id):
        return {"page_id": page_id, "broker": "trusted-test-endpoint", "epoch": 1,
                "hostPid": 1, "hostUid": 1, "timeoutMs": 10000}

    def binding(self, frame, scene=1):
        return {key: frame[key] for key in
                ("page_id", "frame_id", "document_epoch", "visual_generation")} | {"scene_revision": scene}

    async def physical_begin(self, channel_id, operation="keyboard.down", args=None):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        basis = self.binding(frame, scene=channel_id)
        await self.session.page_bind_displayed(basis)
        request = {**basis, "owner_revision": self.engine.control.owner_revision,
                   "operation": operation, "args": args or {"key": "Control"}, "channel_id": channel_id}
        await self.session.page_broker_action(request)
        return request

    async def test_physical_terminal_retains_original_page_after_displayed_consumption(self):
        request = await self.physical_begin(1, "mouse.down", {"x": 17, "y": 19})
        self.assertNotIn("p4", self.session.displayed_broker_frames)
        result = await self.session.page_broker_action({**request, "operation": "mouse.up", "args": {}})
        self.assertEqual(result, {"status": "ok", "page_id": "p4", "channel_id": 1})
        native = [(domain, params) for domain, method, params in self.calls if method == "Input.dispatchNativeAction"]
        self.assertEqual([domain for domain, _ in native], [4, 4])
        self.assertEqual(native[-1][1]["channelPhase"], "end")
        self.assertEqual(self.engine.context.pages[1].mouse.buttons, 0)
        self.assertFalse(self.session.input_channels)
        with self.assertRaises(ServiceError):
            await self.session.page_broker_action({**request, "operation": "mouse.up", "args": {}})
        self.assertEqual(len([call for call in self.calls if call[1] == "Input.dispatchNativeAction"]), 2)

    async def test_changed_terminal_basis_resets_original_not_request_page(self):
        request = await self.physical_begin(1)
        result = await self.session.page_broker_action({**request, "page_id": "p0",
                                                       "operation": "keyboard.up", "args": {}})
        self.assertEqual(result, {"status": "reset", "page_id": "p4", "channel_id": 1})
        domain, method, params = self.calls[-1]
        self.assertEqual((domain, method, params["channelPhase"], params["events"]),
                         (4, "Input.dispatchNativeAction", "cancel", []))
        self.assertFalse(self.session.input_channels)
        self.assertEqual(self.engine.context.pages[1].keyboard.modifiers, 0)

    async def test_channel_capacity_and_reuse_reject_before_native_effect(self):
        requests = [await self.physical_begin(channel_id) for channel_id in range(1, 17)]
        with self.assertRaises(ServiceError) as full:
            await self.physical_begin(17)
        self.assertEqual(full.exception.code, "busy")
        self.assertEqual(len(self.session.input_channels), 16)
        self.assertEqual(len([call for call in self.calls if call[1] == "Input.dispatchNativeAction"]), 16)
        await self.session.page_broker_action({**requests[0], "operation": "input.cancel", "args": {}})
        with self.assertRaises(ServiceError) as reused:
            await self.physical_begin(1)
        self.assertEqual(reused.exception.code, "invalid_config")
        await self.physical_begin(17)
        self.assertEqual(len(self.session.input_channels), 16)

    async def test_reserved_channels_accept_out_of_order_without_reuse(self):
        later = await self.physical_begin(2)
        earlier = await self.physical_begin(1)
        self.assertEqual(set(self.session.input_channels), {1, 2})
        with self.assertRaises(ServiceError) as occupied:
            await self.physical_begin(17)
        self.assertEqual(occupied.exception.code, "busy")
        await self.session.page_broker_action({**earlier, "operation": "input.cancel", "args": {}})
        with self.assertRaises(ServiceError) as replay:
            await self.physical_begin(1)
        self.assertEqual(replay.exception.code, "invalid_config")
        await self.physical_begin(17)
        self.assertEqual(set(self.session.input_channels), {2, 17})
        await self.session.page_broker_action({**later, "operation": "input.cancel", "args": {}})
        self.assertEqual(set(self.session.input_channels), {17})

    async def test_unknown_begin_only_resets_on_terminal(self):
        page = self.engine.context.pages[1]
        original = page.context.cdp.send
        async def unknown(method, *args, **kwargs):
            if method == "Input.dispatchNativeAction":
                raise TimeoutError("unknown native dispatch")
            return await original(method, *args, **kwargs)
        page.context.cdp.send = unknown
        with self.assertRaises(TimeoutError):
            await self.physical_begin(1)
        self.assertIn(1, self.session.input_channels)
        self.assertFalse(self.session.input_channels[1]["known"])
        self.assertEqual(page.keyboard.modifiers, 0)
        page.context.cdp.send = original
        request = self.session.input_channels[1]["basis"]
        await self.session.page_broker_action({**request, "operation": "keyboard.up", "args": {}, "channel_id": 1})
        self.assertEqual(self.calls[-1][2]["channelPhase"], "cancel")
        self.assertFalse(self.session.input_channels)

    async def test_unknown_terminal_cannot_replay_up(self):
        request = await self.physical_begin(1)
        page = self.engine.context.pages[1]
        original = page.context.cdp.send
        attempted = []
        async def unknown(method, params=None, *args, **kwargs):
            if method == "Input.dispatchNativeAction":
                attempted.append(params["channelPhase"])
                raise TimeoutError("unknown terminal acknowledgement")
            return await original(method, params, *args, **kwargs)
        page.context.cdp.send = unknown
        terminal = {**request, "operation": "keyboard.up", "args": {}}
        with self.assertRaises(TimeoutError):
            await self.session.page_broker_action(terminal)
        self.assertFalse(self.session.input_channels[1]["known"])
        page.context.cdp.send = original
        result = await self.session.page_broker_action(terminal)
        self.assertEqual(attempted, ["end"])
        self.assertEqual(result["status"], "reset")
        self.assertEqual(self.calls[-1][2]["channelPhase"], "cancel")
        self.assertEqual(page.keyboard.modifiers, 0)

    async def test_scope_rollovers_reset_retained_channels_without_up(self):
        for channel_id, changed in enumerate(("document", "viewport", "demand", "owner"), 1):
            with self.subTest(changed=changed):
                request = await self.physical_begin(channel_id)
                if changed == "document":
                    self.engine.context.pages[1].observation_id += 1
                elif changed == "viewport":
                    self.session.page_viewport_epochs["p4"] = self.session.page_viewport_epochs.get("p4", 0) + 1
                elif changed == "demand":
                    await self.session.page_demand({"page_id": "p4", "enabled": False})
                else:
                    await self.session.control({"operation": "resume_agent", "owner_revision": self.engine.control.owner_revision})
                    await self.session.control({"operation": "request_human"})
                result = await self.session.page_broker_action({**request, "operation": "keyboard.up", "args": {}})
                self.assertEqual(result["status"], "reset")
                self.assertEqual(self.calls[-1][2]["channelPhase"], "cancel")
                self.assertFalse(self.session.input_channels)
                if changed == "demand":
                    await self.session.page_demand({"page_id": "p4", "enabled": True})

    async def test_revoked_closed_source_can_only_clean_its_retained_terminal(self):
        request = await self.physical_begin(1)
        page = self.engine.context.pages[1]
        page._closed = True
        page._headless_stop_revoked = True
        calls = len(self.calls)
        protocol = NativeProtocol(self.engine, Path("/unused"), Path("/unused/control"), Path("/unused/frame"))
        protocol.session = self.session
        replies = asyncio.Queue()
        protocol.emit = replies.put
        await protocol.respond({"id": 1, "method": "page_broker_action",
                                "params": {**request, "operation": "keyboard.up", "args": {}},
                                "_source_page": page, "_execution_domain": 4})
        self.assertEqual(await replies.get(), {"id": 1, "result": {
            "status": "reset", "page_id": "p4", "channel_id": 1}})
        self.assertFalse(self.session.input_channels)
        self.assertEqual(len(self.calls), calls)

    async def test_native_queue_reserves_terminal_behind_full_ordinary_lane(self):
        protocol = NativeProtocol(self.engine, Path("/unused"), Path("/unused/control"), Path("/unused/frame"))
        protocol.session = self.session
        queue = asyncio.Queue(maxsize=32)
        protocol.lanes[4] = (queue, None)
        protocol.enqueue({"id": 1, "method": "page_broker_action",
                          "params": {"operation": "keyboard.down", "channel_id": 1},
                          "_source_page": self.engine.context.pages[1]}, 4)
        for identifier in range(2, 17):
            protocol.enqueue({"id": identifier, "method": "state", "params": {}}, 4)
        with self.assertRaises(ServiceError):
            protocol.enqueue({"id": 17, "method": "state", "params": {}}, 4)
        terminal = {"id": 18, "method": "page_broker_action",
                    "params": {"operation": "keyboard.up", "channel_id": 1, "page_id": "p0"}}
        self.assertEqual(protocol.request_domain(terminal), 4)
        protocol.enqueue(terminal, 4)
        self.assertEqual(queue.qsize(), 17)
        with self.assertRaises(ServiceError):
            protocol.enqueue({**terminal, "id": 19}, 4)

    async def test_wire_keeps_blocked_domain_order_while_peer_captures_binds_and_inputs(self):
        protocol = NativeProtocol(self.engine, Path("/unused"), Path("/unused/control"), Path("/unused/frame"))
        protocol.session = self.session
        replies = asyncio.Queue()
        protocol.emit = replies.put
        reader = asyncio.StreamReader()

        def feed(identifier, method, params):
            body = json.dumps({"id": identifier, "method": method, "params": params}).encode()
            reader.feed_data(struct.pack("<I", len(body)) + body)

        client = asyncio.create_task(protocol.accept(reader, Writer()))
        try:
            feed(1, "page_broker_frame", self.raster("p0"))
            await asyncio.wait_for(self.entered.wait(), 1)
            feed(2, "page_broker_frame", self.raster("p0"))
            feed(3, "page_broker_frame", self.raster("p4"))
            reply = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual(reply["id"], 3)
            basis = self.binding(reply["result"])
            feed(4, "page_bind_displayed", basis)
            self.assertEqual((await asyncio.wait_for(replies.get(), 1))["result"],
                             {"status": "bound", **basis})
            feed(5, "page_broker_action", {**basis, "owner_revision": self.engine.control.owner_revision,
                 "operation": "mouse.wheel", "args": {"x": 37, "y": 29, "delta_x": 0, "delta_y": 20}})
            self.assertEqual((await asyncio.wait_for(replies.get(), 1))["id"], 5)
            self.assertFalse(self.release.is_set())
            self.assertEqual(sum(domain == 0 and method == "Page.captureBrokerFrame"
                                 for domain, method, _ in self.calls), 1)
            events = [params["events"] for domain, method, params in self.calls
                      if domain == 4 and method == "Input.dispatchNativeAction"]
            self.assertEqual((events[0][0]["params"]["x"], events[0][0]["params"]["y"]), (37, 29))
            self.release.set()
            first = await asyncio.wait_for(replies.get(), 1)
            second = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual((first["id"], second["id"]), (1, 2))
        finally:
            self.release.set()
            reader.feed_eof()
            await asyncio.wait_for(client, 1)

    async def test_full_domain_queue_does_not_consume_peer_or_control_admission(self):
        protocol = NativeProtocol(self.engine, Path("/unused"), Path("/unused/control"), Path("/unused/frame"))
        protocol.session = self.session
        replies = asyncio.Queue()
        protocol.emit = replies.put
        reader = asyncio.StreamReader()

        def feed(identifier, method, params):
            body = json.dumps({"id": identifier, "method": method, "params": params}).encode()
            reader.feed_data(struct.pack("<I", len(body)) + body)

        client = asyncio.create_task(protocol.accept(reader, Writer()))
        try:
            feed(1, "page_broker_frame", self.raster("p0"))
            await asyncio.wait_for(self.entered.wait(), 1)
            for identifier in range(2, 19):
                feed(identifier, "page_broker_frame", self.raster("p0"))
            feed(19, "page_broker_frame", self.raster("p4"))
            rejected = await asyncio.wait_for(replies.get(), 1)
            peer = await asyncio.wait_for(replies.get(), 1)
            self.assertEqual((rejected["id"], rejected["error"]["code"]), (18, "busy"))
            self.assertEqual(peer["id"], 19)
            feed(20, "page_demand", {"page_id": "p0", "enabled": False})
            self.assertEqual((await asyncio.wait_for(replies.get(), 1))["id"], 20)
            self.assertFalse(self.release.is_set())
        finally:
            self.release.set()
            reader.feed_eof()
            await asyncio.wait_for(client, 1)

    async def test_disable_enable_rejects_old_bind_until_a_fresh_capture_is_presented(self):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        basis = self.binding(frame)
        await self.session.page_bind_displayed(basis)
        await self.session.page_demand({"page_id": "p4", "enabled": False})
        await self.session.page_demand({"page_id": "p4", "enabled": True})
        for operation in (
                self.session.page_bind_displayed(basis),
                self.session.page_broker_action({**basis, "owner_revision": self.engine.control.owner_revision,
                    "operation": "mouse.click", "args": {"x": 1, "y": 1}})):
            with self.assertRaises(ServiceError) as rejected:
                await operation
            self.assertEqual(rejected.exception.code, "stale_reference")
        fresh = await self.session.page_broker_frame(self.raster("p4"))
        self.assertGreater(fresh["visual_generation"], frame["visual_generation"])
        basis = self.binding(fresh, scene=2)
        self.assertEqual(await self.session.page_bind_displayed(basis), {"status": "bound", **basis})

    async def test_demand_revokes_a_blocked_capture_before_native_completion(self):
        capture = asyncio.create_task(self.session.page_broker_frame(self.raster("p0")))
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            await asyncio.wait_for(self.session.page_demand({"page_id": "p0", "enabled": False}), 1)
            await self.session.page_demand({"page_id": "p0", "enabled": True})
            self.assertFalse(capture.done())
            self.release.set()
            frame = await asyncio.wait_for(capture, 1)
            self.assertFalse(frame["capture_stable"])
            self.assertEqual(frame["input_guard"], "unavailable")
            with self.assertRaises(ServiceError):
                await self.session.page_bind_displayed(self.binding(frame))
        finally:
            self.release.set()
            await asyncio.gather(capture, return_exceptions=True)

    async def test_delayed_bind_ack_cannot_restore_a_disabled_generation(self):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        page = self.engine.context.pages[1]
        entered, release = asyncio.Event(), asyncio.Event()
        original = page.context.cdp.send

        async def delayed(method, *args, **kwargs):
            if method == "Page.bindDisplayedFrame":
                entered.set()
                await release.wait()
            return await original(method, *args, **kwargs)

        page.context.cdp.send = delayed
        binding = asyncio.create_task(self.session.page_bind_displayed(self.binding(frame)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.session.page_demand({"page_id": "p4", "enabled": False}), 1)
            await self.session.page_demand({"page_id": "p4", "enabled": True})
            release.set()
            with self.assertRaises(ServiceError) as stale:
                await asyncio.wait_for(binding, 1)
            self.assertEqual(stale.exception.code, "stale_reference")
            self.assertNotIn("p4", self.session.displayed_broker_frames)
        finally:
            release.set()
            await asyncio.gather(binding, return_exceptions=True)

    async def test_ownership_rollover_revokes_captured_and_displayed_broker_bases(self):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        basis = self.binding(frame)
        await self.session.page_bind_displayed(basis)
        old_revision = self.engine.control.owner_revision
        await self.session.control({"operation": "resume_agent", "owner_revision": old_revision})
        await self.session.control({"operation": "request_human"})
        self.assertFalse(self.session.broker_frames)
        self.assertFalse(self.session.displayed_broker_frames)
        for revision in (old_revision, self.engine.control.owner_revision):
            with self.assertRaises((ServiceError, ControlError)):
                await self.session.page_broker_action({**basis, "owner_revision": revision,
                    "operation": "mouse.click", "args": {"x": 1, "y": 1}})
        with self.assertRaises(ServiceError):
            await self.session.page_bind_displayed(basis)
        fresh = await self.session.page_broker_frame(self.raster("p4"))
        await self.session.page_bind_displayed(self.binding(fresh, scene=2))

    async def test_ownership_transfer_waits_for_capture_but_never_recaches_its_old_epoch(self):
        capture = asyncio.create_task(self.session.page_broker_frame(self.raster("p0")))
        transition = None
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            transition = asyncio.create_task(self.session.control({
                "operation": "resume_agent", "owner_revision": self.engine.control.owner_revision}))
            await asyncio.sleep(0)
            self.assertEqual(self.engine.control.owner, "paused")
            self.assertFalse(transition.done())
            self.release.set()
            frame = await asyncio.wait_for(capture, 1)
            self.assertFalse(frame["capture_stable"])
            self.assertEqual(frame["input_guard"], "unavailable")
            self.assertEqual(frame["capture_error"], "OwnershipChanged")
            await asyncio.wait_for(transition, 1)
            await self.session.control({"operation": "request_human"})
            self.assertFalse(self.session.broker_frames)
            with self.assertRaises(ServiceError):
                await self.session.page_bind_displayed(self.binding(frame))
        finally:
            self.release.set()
            await asyncio.gather(capture, *([transition] if transition else []), return_exceptions=True)

    async def test_ownership_transfer_fences_delayed_bind_ack_and_new_owner_needs_fresh_capture(self):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        page = self.engine.context.pages[1]
        entered, release = asyncio.Event(), asyncio.Event()
        original = page.context.cdp.send

        async def delayed(method, *args, **kwargs):
            if method == "Page.bindDisplayedFrame":
                entered.set()
                await release.wait()
            return await original(method, *args, **kwargs)

        page.context.cdp.send = delayed
        binding = asyncio.create_task(self.session.page_bind_displayed(self.binding(frame)))
        transition = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            transition = asyncio.create_task(self.session.control({
                "operation": "resume_agent", "owner_revision": self.engine.control.owner_revision}))
            await asyncio.sleep(0)
            self.assertEqual(self.engine.control.owner, "paused")
            self.assertFalse(transition.done())
            release.set()
            with self.assertRaises(ServiceError) as stale:
                await asyncio.wait_for(binding, 1)
            self.assertEqual(stale.exception.code, "stale_reference")
            await asyncio.wait_for(transition, 1)
            await self.session.control({"operation": "request_human"})
            self.assertFalse(self.session.displayed_broker_frames)
            with self.assertRaises(ServiceError):
                await self.session.page_bind_displayed(self.binding(frame, scene=2))
        finally:
            release.set()
            await asyncio.gather(binding, *([transition] if transition else []), return_exceptions=True)

    async def test_control_revision_changes_outside_session_also_expire_broker_basis(self):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        self.engine.control.pause()
        await self.engine.control.request_human(self.session.human_id)
        with self.assertRaises(ServiceError) as stale:
            await self.session.page_bind_displayed(self.binding(frame))
        self.assertEqual(stale.exception.code, "stale_reference")

    async def test_failed_native_bind_cannot_authorize_a_displayed_scene(self):
        frame = await self.session.page_broker_frame(self.raster("p4"))
        basis = self.binding(frame)
        await self.session.page_bind_displayed(basis)
        page = self.engine.context.pages[1]
        original = page.context.cdp.send

        async def fail(method, *args, **kwargs):
            if method == "Page.bindDisplayedFrame":
                raise ServiceError("stale_reference", "native binding was rejected")
            return await original(method, *args, **kwargs)

        page.context.cdp.send = fail
        with self.assertRaises(ServiceError):
            await self.session.page_bind_displayed({**basis, "scene_revision": 2})
        with self.assertRaises(ServiceError):
            await self.session.page_broker_action({**basis, "owner_revision": self.engine.control.owner_revision,
                "operation": "mouse.click", "args": {"x": 1, "y": 1}})

    async def test_wheel_requires_current_coordinates(self):
        with self.assertRaises(ServiceError):
            self.session._page_input_args("mouse.wheel", {"delta_x": 0, "delta_y": 20},
                                          {"viewport": {"width": 100, "height": 80}})

class CaptureSnapshot:
    """Model native observer evidence; production RefStore validates every use."""
    def __init__(self, page):
        self.page = page
        self.document_epoch = page.observation_id

    async def evaluate(self, script, options=None):
        if script == _CHECK_JS:
            valid = not self.page.is_closed() and self.page.observation_id == self.document_epoch
            return {"width": 1, "height": 1} if valid and options["viewport"] else valid
        if script == "s => s.observer.disconnect()":
            return None
        raise AssertionError(f"Unexpected observer evaluation: {script}")

    async def dispose(self):
        pass


class OrdinaryCaptureDomainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.raw_requests = {}
        self.native_inputs = []
        self.generations = {0: 0, 4: 0}
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Z8sAAAAASUVORK5CYII=")
        pages = []
        for domain in (0, 4):
            async def send(method, params=None, session=None, *, timeout=30, domain=domain):
                if method in {"Page.captureScreenshot", "Page.captureRawFrame"}:
                    self.generations[domain] += 1
                    marker = domain * 10 + self.generations[domain]
                    if method == "Page.captureRawFrame":
                        self.raw_requests[domain] = dict(params)
                        descriptor = os.open(params["path"], os.O_RDWR)
                        try:
                            os.pwrite(descriptor, bytes([marker]) * 4,
                                      HEADER + params["slot"] * SLOT_SIZE + HEADER)
                        finally:
                            os.close(descriptor)
                    if domain == 0:
                        self.entered.set()
                        await self.release.wait()
                    if method == "Page.captureScreenshot":
                        return {"data": base64.b64encode(png).decode("ascii")}
                    return {"format": "premultiplied-rgba8", "width": 1, "height": 1, "stride": 4,
                            "dpr_milli": 1000, "theme_edge_rgba": 0xff112233,
                            "caret_rect": [0., 0., 0., 0.], "caret_kind": "none",
                            "render_fingerprint": "a" * 64, "focused_frame_id": f"f{domain}",
                            "focused_element_id": None}
                if method == "Runtime.evaluate":
                    if "innerWidth" in params["expression"]:
                        return {"result": {"value": {"width": 1, "height": 1, "device_scale_factor": 1}}}
                    return {"result": {"value": "complete"}}
                if method == "Page.getNavigationHistory":
                    return {"currentIndex": 0, "entries": [{"id": 1}]}
                if method == "Emulation.setDeviceMetricsOverride":
                    self.assertEqual(params, {"width": 1, "height": 1, "deviceScaleFactor": 1, "mobile": False})
                    return {}
                if method == "Page.captureBrokerFrame":
                    self.generations[domain] += 1
                    return {"capture_stable": True, "input_guard": "native-exact-v1",
                            "visual_generation": self.generations[domain],
                            "viewport": {"width": 1, "height": 1}}
                if method == "Input.dispatchNativeAction":
                    self.assertEqual(params["expectedRenderFingerprint"], "a" * 64)
                    self.assertTrue(params["events"])
                    self.native_inputs.append((domain, params))
                    return {}
                raise AssertionError(f"Unexpected native method: {method}")

            page = NativePage(SimpleNamespace(cdp=SimpleNamespace(execution_domain=domain, send=send)),
                              f"p{domain}", f"s{domain}", "about:blank")
            page.frame_id, page.observation_id = f"f{domain}", 1
            pages.append(page)
        self.engine = object.__new__(Engine)
        self.engine.context = SimpleNamespace(pages=pages)
        self.engine.active = pages[0]
        self.engine.operations = SimpleNamespace(page_id=lambda page: page.page_id)
        self.engine.control = ControlState(page_id="p0", frame_id="f0")
        self.engine.refs = RefStore(random=LocalSecureRandom())
        for page in pages:
            self.engine.refs.page_visuals[page] = {
                "page": page, "frame": page, "versions": None, "independent": True,
                "handle": CaptureSnapshot(page), "page_id": page.page_id, "frame_id": page.frame_id,
                "observation_id": f"observation-{page.page_id}", "children": []}
        self.session = Session(self.engine)
        await self.session.control({"operation": "request_human"})

    async def test_maximum_legal_primary_socket_path_does_not_limit_page_ring_capture(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            root = base / ("x" * (100 - len(os.fsencode(base))))
            root.mkdir(mode=0o700)
            socket = root / "s"
            self.assertEqual(len(os.fsencode(socket)), 103)
            ring = FrameRing(root / "frame", socket)
            await self.engine.control.resume_agent()
            self.session = Session(self.engine, ring)
            await self.session.control({"operation": "request_human"})
            try:
                frame = await asyncio.wait_for(self.session.capture("p4"), 1)
                page_ring = self.session.domain_rings[4]
                self.assertGreater(len(os.fsencode(page_ring.path)), 103)
                self.assertIsNone(page_ring.socket)
                self.assertEqual(frame["frame_seq"], 1)
                front = page_ring.lib.lpfr_acquire(page_ring.base, 0)
                offset = HEADER + front * SLOT_SIZE + HEADER
                self.assertEqual(page_ring.mapping[offset:offset + 4], bytes([41]) * 4)
            finally:
                self.session.close()
                ring.close()

    async def _closed_resize_epoch_is_collected(self, *, rollover):
        resized = await self.session.resize_page({
            "page_id": "p0", "width": 1, "height": 1, "device_scale_factor": 1,
            "owner_revision": self.engine.control.owner_revision})
        self.assertEqual(resized["viewport_epoch"], 1)
        if rollover:
            await self.session.control({
                "operation": "resume_agent", "owner_revision": self.engine.control.owner_revision})
            await self.session.control({"operation": "request_human"})
        self.assertEqual(self.session.page_viewport_epochs["p0"], 1)
        self.assertNotIn("p0", self.session.page_frames)
        self.engine.context.pages[0]._closed = True
        peer = await asyncio.wait_for(self.session.capture("p4"), 1)
        self.assertEqual(peer["page_id"], "p4")
        self.assertNotIn("p0", self.session.page_viewport_epochs)

    async def test_closed_resized_page_epoch_is_collected_without_any_capture_basis(self):
        await self._closed_resize_epoch_is_collected(rollover=False)

    async def test_closed_resized_page_epoch_is_collected_after_ownership_clears_frames(self):
        await self._closed_resize_epoch_is_collected(rollover=True)

    async def test_broker_only_capture_and_resize_collect_closed_page_epochs_without_ordinary_frames(self):
        async def resize(page_id):
            return await self.session.resize_page({
                "page_id": page_id, "width": 1, "height": 1, "device_scale_factor": 1,
                "owner_revision": self.engine.control.owner_revision})

        async def broker(page_id):
            return await self.session.page_broker_frame({
                "page_id": page_id, "broker": "trusted-test-endpoint", "epoch": 1,
                "hostPid": 1, "hostUid": 1, "timeoutMs": 10000})

        await resize("p0")
        first = await broker("p0")
        self.assertEqual(first["viewport_epoch"], 1)
        self.assertIn("p0", self.session.broker_frames)
        self.assertNotIn("p0", self.session.page_frames)
        self.engine.context.pages[0]._closed = True
        peer = await broker("p4")
        self.assertEqual(peer["viewport_epoch"], 0)
        self.assertNotIn("p0", self.session.page_viewport_epochs)
        self.assertNotIn("p0", self.session.broker_frames)

        await resize("p4")
        self.assertEqual(self.session.page_viewport_epochs["p4"], 1)
        self.engine.context.pages[1]._closed = True
        send = self.engine.context.pages[1].context.cdp.send
        replacement = NativePage(SimpleNamespace(cdp=SimpleNamespace(execution_domain=8, send=send)),
                                 "p8", "s8", "about:blank")
        replacement.frame_id, replacement.observation_id = "f8", 1
        self.engine.context.pages.append(replacement)
        await resize("p8")
        self.assertEqual(self.session.page_viewport_epochs, {"p8": 1})
        self.assertNotIn("p4", self.session.broker_frames)
        self.assertFalse(self.session.page_frames)
        self.assertFalse(self.entered.is_set())

    async def test_stopping_active_domain_does_not_gate_peer_page_capture(self):
        self.session.stopping_domains.add(0)
        peer = await asyncio.wait_for(self.session.capture("p4"), 1)
        self.assertEqual(peer["page_id"], "p4")
        with self.assertRaises(ServiceError):
            await self.session.capture("p0")

    async def test_ringless_capture_in_peer_domain_finishes_while_first_native_capture_waits(self):
        capture = asyncio.create_task(self.session.capture("p0"))
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            peer = await asyncio.wait_for(self.session.capture("p4"), 1)
            self.assertEqual(peer["page_id"], "p4")
            self.assertTrue(base64.b64decode(peer["png"]).startswith(b"\x89PNG"))
            self.assertFalse(capture.done())
            self.release.set()
            self.assertEqual((await asyncio.wait_for(capture, 1))["page_id"], "p0")
        finally:
            self.release.set()
            await asyncio.gather(capture, return_exceptions=True)

    async def test_lpfr_peer_capture_has_independent_back_slot_and_preserves_retained_front(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ring = FrameRing(root / "frame", root / "control.sock")
            await self.engine.control.resume_agent()
            self.session = Session(self.engine, ring)
            await self.session.control({"operation": "request_human"})
            capture = asyncio.create_task(self.session.capture("p0"))
            try:
                await asyncio.wait_for(self.entered.wait(), 1)
                peer = await asyncio.wait_for(self.session.capture("p4"), 1)
                peer_ring = self.session.domain_rings[4]
                self.assertNotEqual(peer["shm"]["path"], self.raw_requests[0]["path"])
                front = peer_ring.lib.lpfr_acquire(peer_ring.base, 0)
                offset = HEADER + front * SLOT_SIZE + HEADER
                retained = peer_ring.mapping[offset:offset + 4]
                self.assertEqual(retained, bytes([41]) * 4)
                for _ in range(5):
                    peer = await asyncio.wait_for(self.session.capture("p4"), 1)
                    self.assertEqual(peer_ring.mapping[offset:offset + 4], retained)
                action = {key: peer[key] for key in (
                    "image_id", "frame_seq", "page_id", "frame_id", "observation_id", "owner_revision",
                    "document_epoch", "viewport_epoch", "focused_frame_id", "focused_element_id")}
                # Legacy active-viewer serialization is not the page-specific
                # writer lock, including during native pointer rebinding.
                async with self.session.active_capture_lock:
                    await asyncio.wait_for(self.session.page_action({
                        **action, "operation": "mouse.move", "args": {"x": 0, "y": 0}}), 1)
                self.assertEqual(self.native_inputs[0][0], 4)
                self.assertEqual(self.raw_requests[4]["path"], str(peer_ring.path))
                self.assertEqual(peer_ring.mapping[offset:offset + 4], retained)
                blocked_offset = HEADER + self.raw_requests[0]["slot"] * SLOT_SIZE + HEADER
                domain_ring = self.session.domain_rings[0]
                self.assertNotEqual(str(domain_ring.path), str(ring.path))
                self.assertEqual(domain_ring.mapping[blocked_offset:blocked_offset + 4], b"\x01" * 4)
                self.assertEqual(ring.seq, 0)
                self.assertFalse(capture.done())
                self.release.set()
                original = await asyncio.wait_for(capture, 1)
                self.assertEqual(original["shm"]["path"], str(domain_ring.path))
                original_front = domain_ring.lib.lpfr_acquire(domain_ring.base, 0)
                original_offset = HEADER + original_front * SLOT_SIZE + HEADER
                self.assertEqual(domain_ring.mapping[original_offset:original_offset + 4], b"\x01" * 4)
                self.assertEqual(peer_ring.mapping[offset:offset + 4], retained)
            finally:
                self.release.set()
                await asyncio.gather(capture, return_exceptions=True)
                self.session.close()
                ring.close()

    async def test_ordinary_capture_cannot_restore_page_basis_during_owner_transfer(self):
        capture = asyncio.create_task(self.session.capture("p0"))
        transition = None
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            transition = asyncio.create_task(self.session.control({
                "operation": "resume_agent", "owner_revision": self.engine.control.owner_revision}))
            await asyncio.sleep(0)
            self.assertEqual(self.engine.control.owner, "paused")
            self.assertFalse(transition.done())
            self.release.set()
            with self.assertRaises(ServiceError) as stale:
                await asyncio.wait_for(capture, 1)
            self.assertEqual(stale.exception.code, "stale_reference")
            await asyncio.wait_for(transition, 1)
            self.assertNotIn("p0", self.session.page_frames)
        finally:
            self.release.set()
            await asyncio.gather(capture, *([transition] if transition else []), return_exceptions=True)

    async def test_native_document_replacement_still_fails_real_refstore_capture_validation(self):
        capture = asyncio.create_task(self.session.capture("p0"))
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            self.engine.context.pages[0].observation_id += 1
            self.release.set()
            with self.assertRaises(ActionError) as stale:
                await asyncio.wait_for(capture, 1)
            self.assertEqual(stale.exception.code, "stale_reference")
            self.assertNotIn("p0", self.session.page_frames)
        finally:
            self.release.set()
            await asyncio.gather(capture, return_exceptions=True)


class DomainLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_waits_for_every_accepted_domain_and_rejects_queued_old_owner(self):
        control = ControlState()
        await control.request_human("viewer")
        revision = control.owner_revision
        entered = {domain: asyncio.Event() for domain in (0, 4)}
        release = {domain: asyncio.Event() for domain in (0, 4)}

        async def action(domain):
            async with control.action("human", "viewer", owner_revision=revision, execution_domain=domain):
                entered[domain].set()
                await release[domain].wait()

        active = [asyncio.create_task(action(domain)) for domain in (0, 4)]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
        queued = asyncio.create_task(action(0))
        transition = asyncio.create_task(control.resume_agent())
        try:
            await asyncio.sleep(0)
            release[0].set()
            await asyncio.wait_for(active[0], 1)
            self.assertFalse(transition.done())
            self.assertEqual(control.owner, "paused")
            release[4].set()
            await asyncio.wait_for(active[1], 1)
            await asyncio.wait_for(transition, 1)
            with self.assertRaises(ControlError) as stale:
                await queued
            self.assertIn(stale.exception.code, {"paused", "stale_reference"})
            self.assertEqual(control.owner, "agent")
        finally:
            for event in release.values():
                event.set()
            await asyncio.gather(*active, queued, transition, return_exceptions=True)

    async def test_cancelled_domain_action_pauses_global_lease_without_erasing_peer_record(self):
        control = ControlState()
        entered = asyncio.Event()

        async def pending():
            async with control.action("agent", "run", execution_domain=0):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(pending())
        await asyncio.wait_for(entered.wait(), 1)
        async with control.observation(execution_domain=4):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(control.owner, "paused")
            self.assertEqual(control.reason, "action_cancelled_side_effects_unknown")
            self.assertTrue(control.snapshot()["in_flight"])
        self.assertFalse(control.snapshot()["in_flight"])

    async def test_cancelling_unaccepted_same_domain_work_does_not_mark_side_effects_unknown(self):
        control = ControlState()
        entered, release = asyncio.Event(), asyncio.Event()

        async def accepted():
            async with control.action("agent", "run", execution_domain=0):
                entered.set()
                await release.wait()

        async def queued():
            async with control.action("agent", "run", execution_domain=0):
                self.fail("Cancelled queued action must not execute.")

        active = asyncio.create_task(accepted())
        await asyncio.wait_for(entered.wait(), 1)
        waiting = asyncio.create_task(queued())
        try:
            await asyncio.sleep(0)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            self.assertEqual(control.owner, "agent")
            self.assertIsNone(control.reason)
            self.assertTrue(control.snapshot()["in_flight"])
        finally:
            release.set()
            await asyncio.gather(active, waiting, return_exceptions=True)


class EmptyWorkspaceApplicationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise authorization and ownership fencing with a stateful admission endpoint."""

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("mioa_core.profile.BASE", Path(directory)))

    async def make_session(self, *, hold_admission=False):
        from mioa_core.operations import ScriptOperations

        entered, release = asyncio.Event(), asyncio.Event()
        if not hold_admission:
            release.set()

        class Admission:
            def __init__(self):
                self.pages = []
                self.spawned = 0
                self.fronted = []

            def application_domain(self):
                return 5

            async def _new_application(self, domain):
                self.spawned += 1
                entered.set()
                await release.wait()
                endpoint = self

                async def send(method, params=None, session=None, **kwargs):
                    if method == "Page.bringToFront":
                        endpoint.fronted.append(session)
                        return {}
                    raise AssertionError(method)

                page = NativePage(SimpleNamespace(cdp=SimpleNamespace(
                    execution_domain=domain, execution_epoch=93, send=send)),
                    "new-target", "new-session", "about:blank")
                page.frame_id = "new-frame"
                self.pages.append(page)
                return page

        engine = Engine("empty-workspace-fixture", True, None, verify=False)
        engine.context = Admission()
        engine.operations = ScriptOperations(engine.context, active_page=engine.page, axtree=engine._axtree,
                                             random=LocalSecureRandom())
        session = Session(engine)
        await engine.control.request_human(session.human_id)
        return session, entered, release

    async def test_create_from_empty_workspace_has_no_frame_or_old_page_dependency(self):
        session, _, _ = await self.make_session()
        revision = session.engine.control.owner_revision
        result = await session.new_application({"url": "about:blank", "owner_revision": revision})
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["execution_domain"], result["execution_epoch"]), (5, 93))
        self.assertEqual(session.engine.context.spawned, 1)
        self.assertEqual(session.engine.context.fronted, ["new-session"])
        self.assertIs(session.engine.active, session.engine.context.pages[0])
        self.assertEqual(session.engine.control.page_id, result["page_id"])

    async def test_stale_owner_or_forged_page_fields_never_spawn(self):
        session, _, _ = await self.make_session()
        revision = session.engine.control.owner_revision
        for params in ({"url": "about:blank", "owner_revision": revision - 1},
                       {"url": "about:blank", "owner_revision": revision, "page_id": "old"},
                       {"url": "about:blank", "owner_revision": revision, "human_id": session.human_id}):
            with self.assertRaises((ControlError, ServiceError)):
                await session.new_application(params)
        self.assertEqual(session.engine.context.spawned, 0)

    async def test_ownership_transition_waits_for_creation_and_fences_later_web_effects(self):
        session, entered, release = await self.make_session(hold_admission=True)
        revision = session.engine.control.owner_revision
        create = asyncio.create_task(session.new_application({"url": "about:blank", "owner_revision": revision}))
        await entered.wait()
        transfer = asyncio.create_task(session.engine.control.reclaim())
        await asyncio.sleep(0)
        self.assertFalse(transfer.done())
        release.set()
        with self.assertRaises(ControlError):
            await create
        await transfer
        self.assertEqual(session.engine.control.owner, "agent")
        self.assertEqual(session.engine.context.spawned, 1)
        self.assertEqual(len(session.engine.context.pages), 1)
        self.assertEqual(session.engine.context.fronted, [])

    async def test_reclaim_during_reference_release_cannot_select_or_focus_created_page(self):
        session, _, _ = await self.make_session()
        engine = session.engine
        old_page = object()
        engine.active = old_page
        entered, release = asyncio.Event(), asyncio.Event()

        class Handle:
            async def evaluate(self, expression):
                entered.set()
                await release.wait()

            async def dispose(self):
                pass

        engine.refs.visual = {"handle": Handle()}
        revision, selection = engine.control.owner_revision, engine.human_selection_epoch
        create = asyncio.create_task(session.new_application({"url": "about:blank", "owner_revision": revision}))
        transfer = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            transfer = asyncio.create_task(engine.control.reclaim())
            await asyncio.sleep(0)
            self.assertFalse(transfer.done())
            release.set()
            with self.assertRaises(ControlError):
                await create
            await transfer
            self.assertIs(engine.active, old_page)
            self.assertEqual(engine.human_selection_epoch, selection)
            self.assertEqual(engine.context.fronted, [])
            self.assertEqual(engine.control.owner, "agent")
        finally:
            release.set()
            await asyncio.gather(create, *([transfer] if transfer else []), return_exceptions=True)

    async def test_native_creation_routes_without_resolving_an_active_page(self):
        session, _, _ = await self.make_session()
        protocol = NativeProtocol(session.engine, Path("/unused"), Path("/unused/control"), Path("/unused/frame"))
        request = decode_request(b'{"id":1,"method":"new_application","params":{"url":"about:blank","owner_revision":1}}',
                                 native=True)
        self.assertIsNone(protocol.request_domain(request))
        state = await session.state()
        self.assertEqual(state["pages"], [])
        self.assertIsNone(state["page_id"])
        with self.assertRaises(ServiceError):
            decode_request(b'{"id":1,"method":"new_application","params":{}}\n')

    async def test_empty_frame_is_metadata_without_acquiring_raster_or_old_basis(self):
        session, _, _ = await self.make_session()
        # Any access to a raster endpoint would fail; the empty-workspace
        # branch must never derive a target or manufacture a publication.
        session.ring = object()
        session.frame = {"image_id": "retired-image"}
        session.recent_frame_ids.append("retired-image")
        session.frame_scopes["retired-image"] = ("retired",)
        protocol = NativeProtocol(session.engine, Path("/unused"), Path("/unused/control"), Path("/unused/frame"))
        self.assertIsNone(protocol.request_domain({"method": "frame", "params": {}}))
        result = await session.capture()
        self.assertIs(result["empty_workspace"], True)
        self.assertEqual(result["pages"], [])
        self.assertIsNone(result["page_id"])
        self.assertEqual(result["owner_revision"], session.engine.control.owner_revision)
        self.assertEqual(result["state"], session.snapshot())
        self.assertIsNone(session.frame)
        self.assertFalse(session.recent_frame_ids)
        self.assertFalse(session.frame_scopes)
        for pixel_key in ("image_id", "frame_seq", "width", "height", "stride", "png", "data"):
            self.assertNotIn(pixel_key, result)
        with self.assertRaises(ServiceError):
            await session.capture("retired-page")
