"""Guarded input keeps the original native mouse/key semantics in one command."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from mioa_os import LocalProcessIdentity

from mioa_core.native import NativeContext, NativeElementHandle, NativePage, NativeError, ProtocolError, _SupervisedProcess
from mioa_core.engine import Engine

from mioa_core.control import ControlState
from mioa_headless.service import Session

class GuardedInputTests(unittest.IsolatedAsyncioTestCase):
    def page(self):
        cdp = SimpleNamespace(send=AsyncMock(return_value={}))
        return NativePage(SimpleNamespace(cdp=cdp, owner=SimpleNamespace(monotonic_time=LocalProcessIdentity().monotonic_time)), "p", "s", "about:blank")

    def element_input(self, *, borrowed, failure=None, error=None):
        page = self.page()
        node = NativeElementHandle(page, {"type": "object", "subtype": "node", "objectId": "element"})
        released = []

        async def send(method, params=None, session=None, **kwargs):
            if method == "Runtime.callFunctionOn":
                source = params["functionDeclaration"]
                if "el => el.ownerDocument" in source:
                    if failure == "document":
                        raise error
                    return {"result": {"type": "object", "objectId": "document"}}
                if "return el.isContentEditable ? el.textContent : el.value;" in source:
                    return {"result": {"type": "string", "value": "typed"}}
                return {"result": {"type": "boolean", "value": True}}
            if method == "Input.dispatchNativeAction":
                if failure == "input":
                    raise error
                return {}
            if method == "Runtime.releaseObject":
                released.append(params["objectId"])
                if params["objectId"] == "document" and failure == "release":
                    raise error
                return {}
            raise AssertionError(method)

        page.context.cdp.send.side_effect = send
        target = node if borrowed else page.locator("input")
        node.wait_for = target.wait_for = AsyncMock()
        if not borrowed:
            target._input_handle = AsyncMock(return_value=node)
        return target, node, released

    async def test_document_release_failure_still_releases_owned_input_node(self):
        for operation, value in (("fill", "typed"), ("press", "Enter"), ("press_sequentially", "ab")):
            for borrowed in (False, True):
                with self.subTest(operation=operation, borrowed=borrowed):
                    error = NativeError("protocol_error", "document release failed")
                    target, node, released = self.element_input(borrowed=borrowed, failure="release", error=error)
                    with self.assertRaises(NativeError) as caught:
                        await getattr(target, operation)(value)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(released, ["document"] if borrowed else ["document", "element"])
                    self.assertEqual(node.disposed, not borrowed)
                    if borrowed:
                        self.assertEqual(node._check(), "element")

    async def test_failed_or_cancelled_input_releases_only_owned_handles(self):
        for operation, value in (("fill", "typed"), ("press", "Enter"), ("press_sequentially", "ab")):
            for borrowed in (False, True):
                for failure in ("document", "input"):
                    for error in (NativeError("stale_reference", "document changed"), asyncio.CancelledError()):
                        with self.subTest(operation=operation, borrowed=borrowed, failure=failure, error=type(error).__name__):
                            target, node, released = self.element_input(borrowed=borrowed, failure=failure, error=error)
                            with self.assertRaises(type(error)) as caught:
                                await getattr(target, operation)(value)
                            self.assertIs(caught.exception, error)
                            expected = [] if failure == "document" else ["document"]
                            self.assertEqual(released, expected if borrowed else expected + ["element"])
                            self.assertEqual(node.disposed, not borrowed)

    async def test_successful_input_respects_handle_ownership(self):
        for operation, value in (("fill", "typed"), ("press", "Enter"), ("press_sequentially", "ab")):
            for borrowed in (False, True):
                with self.subTest(operation=operation, borrowed=borrowed):
                    target, node, released = self.element_input(borrowed=borrowed)
                    await getattr(target, operation)(value)
                    self.assertEqual(node.disposed, not borrowed)
                    self.assertEqual(released, ["document"] if borrowed else ["document", "element"])
                    if borrowed:
                        self.assertEqual(node._check(), "element")
                        await getattr(target, operation)(value)
                        self.assertEqual(node._check(), "element")
                        self.assertEqual(released, ["document", "document"])

    async def test_rejected_physical_preparation_restores_mouse_and_modifiers(self):
        page = self.page()
        page.mouse.x, page.mouse.y = 4, 6
        page.context.cdp.send.side_effect = ProtocolError(
            "Input.dispatchNativeAction", {"code": -32042, "message": "stale displayed native frame"})
        for operation, args in (("mouse.down", {"x": 17, "y": 19}),
                                ("keyboard.down", {"key": "Control+Shift+KeyA"})):
            with self.subTest(operation=operation), self.assertRaises(ProtocolError):
                await page.guarded_input(operation, args, displayed=(12, 5), channel_id=1)
            self.assertEqual((page.mouse.x, page.mouse.y, page.mouse.buttons, page.keyboard.modifiers), (4, 6, 0, 0))
            self.assertIsNone(page._native_input_batch)
        self.assertEqual(page.context.cdp.send.await_count, 2)

    async def test_physical_chord_up_and_cancel_release_local_state(self):
        page = self.page()
        await page.guarded_input("keyboard.down", {"key": "Control+Shift+KeyA"}, displayed=(12, 5), channel_id=1)
        self.assertEqual(page.keyboard.modifiers, 10)
        await page.guarded_input("keyboard.up", {"key": "Control+Shift+KeyA"}, channel_id=1)
        self.assertEqual(page.keyboard.modifiers, 0)
        await page.guarded_input("mouse.down", {"x": 17, "y": 19}, displayed=(13, 6), channel_id=2)
        self.assertEqual(page.mouse.buttons, 1)
        await page.guarded_input("input.cancel", {}, channel_id=2)
        self.assertEqual(page.mouse.buttons, 0)
        self.assertEqual(page.context.cdp.send.await_count, 4)

    async def test_click_has_one_fingerprint_guarded_native_turn(self):
        page = self.page()
        await page.guarded_input("mouse.click", {"x": 17, "y": 19, "button": "left", "click_count": 1}, "a" * 64)
        page.context.cdp.send.assert_awaited_once()
        args = page.context.cdp.send.await_args.args
        self.assertEqual(args[0], "Input.dispatchNativeAction")
        self.assertEqual(args[1]["expectedRenderFingerprint"], "a" * 64)
        self.assertEqual([event["params"]["type"] for event in args[1]["events"]],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertTrue(all(event["method"] == "dispatchMouseEvent" for event in args[1]["events"]))
        self.assertIsNone(page._native_input_batch)

    async def test_keyboard_events_cannot_dispatch_before_native_guard(self):
        page = self.page()
        await page.guarded_input("keyboard.press", {"key": "Control+a"}, "b" * 64)
        page.context.cdp.send.assert_awaited_once()
        events = page.context.cdp.send.await_args.args[1]["events"]
        self.assertEqual([event["method"] for event in events], ["dispatchKeyEvent"] * 4)
        self.assertEqual(page.keyboard.modifiers, 0)

    async def test_displayed_scene_uses_existing_events_without_fingerprint_rebind(self):
        page = self.page()
        await page.guarded_input("mouse.click", {"x": 3, "y": 7}, displayed=(12, 5))
        request = page.context.cdp.send.await_args.args[1]
        self.assertEqual(request["expectedSceneRevision"], 12)
        self.assertEqual(request["expectedVisualGeneration"], 5)
        self.assertNotIn("expectedRenderFingerprint", request)
        self.assertEqual([event["params"]["type"] for event in request["events"]],
                         ["mouseMoved", "mousePressed", "mouseReleased"])

    async def test_displayed_guard_never_rebinds_or_replays(self):
        page = self.page()
        page.context.cdp.send.side_effect = ProtocolError(
            "Input.dispatchNativeAction", {"code": -32042, "message": "stale displayed native frame"})
        with self.assertRaises(ProtocolError):
            await page.guarded_input("keyboard.insert_text", {"text": "x"}, displayed=(12, 5))
        page.context.cdp.send.assert_awaited_once()
        with self.assertRaises(NativeError):
            await page.guarded_input("keyboard.insert_text", {"text": "x"},
                                     displayed=(12, 5), rebind=AsyncMock())

    async def test_synchronous_page_group_stays_in_its_explicit_domain(self):
        class CDP:
            execution_domain, execution_epoch = 5, 81
            def __init__(self):
                self.listeners = []
                self.targets = 0
            def _task(self, value):
                if asyncio.iscoroutine(value):
                    asyncio.create_task(value)
            async def send(self, method, params=None, session=None, **kwargs):
                if method == "Target.createTarget":
                    self.targets += 1
                    return {"targetId": f"target-{self.targets}"}
                if method == "Target.attachToTarget":
                    return {"sessionId": f"session-{params['targetId']}"}
                if method == "Page.getFrameTree":
                    return {"frameTree": {"frame": {"id": f"frame-{session}", "url": "about:blank"}}}
                if method in {"Page.enable", "Network.enable", "Runtime.enable"}:
                    return {}
                raise AssertionError(method)
        cdp = CDP()
        root = NativeContext(SimpleNamespace(process=object(), monotonic_time=LocalProcessIdentity().monotonic_time), cdp)
        first = await root._new_local_page()
        second = await root.new_page(synchronize_with=first)
        self.assertEqual(root.pages, [first, second])
        self.assertIs(first.context, second.context)
        self.assertEqual(cdp.targets, 2)
        cdp.revoked = True
        with self.assertRaises(NativeError) as stale:
            await root.new_page(synchronize_with=first)
        self.assertEqual(stale.exception.code, "stale_reference")
        self.assertEqual(cdp.targets, 2)

    async def test_full_negotiated_domain_inventory_rejects_before_spawn(self):
        process = object.__new__(_SupervisedProcess)
        process.supervisor = SimpleNamespace(application_domains=(0, 4, 5, 6, 7),
                                             _reserved_domains={0, 4, 5, 6, 7})
        root = NativeContext(SimpleNamespace(process=process), SimpleNamespace(listeners=[]))
        with self.assertRaises(NativeError) as full:
            await root.new_page()
        self.assertEqual(full.exception.code, "busy")
        self.assertEqual(root.pages, [])

    async def test_stopping_peer_keeps_primary_page_and_retires_dead_session(self):
        primary = object.__new__(_SupervisedProcess)
        primary.domain, primary.profile_name = 0, "test"
        primary.supervisor, primary.profile_launch_id, primary.profile_control = object(), 7, object()
        root = NativeContext(SimpleNamespace(process=primary, config={}),
                             SimpleNamespace(listeners=[], execution_domain=0))
        first = NativePage(root, "p0", "s0", "about:blank")
        root._pages[first.target_id] = first
        peer_process = SimpleNamespace(domain=4, stop=AsyncMock(), wait=AsyncMock(return_value=0))
        peer = SimpleNamespace(process=peer_process, close=AsyncMock())
        peer.context = NativeContext(peer, SimpleNamespace(listeners=[], execution_domain=4))
        second = NativePage(peer.context, "p4", "s4", "about:blank")
        peer.context._pages[second.target_id] = second
        root._peer_sessions.append(peer)
        engine = object.__new__(Engine)
        engine.context, engine.active = root, first
        engine.operations = SimpleNamespace(page_id=lambda page: page.page_id)
        engine.refs = SimpleNamespace(frame_id=lambda _: "f0", forget_page=AsyncMock(), page_visuals={})
        engine.control = ControlState(page_id="p0", frame_id="f0")
        session = Session(engine)
        result = await session.page_stop({"page_id": "p4"})
        self.assertEqual(result["stopped_page_ids"], ["p4"])
        self.assertEqual(root.pages, [first])
        self.assertFalse(first.is_closed())
        self.assertEqual(root._peer_sessions, [])
        self.assertTrue(second.context.cdp.revoked)
        self.assertFalse(root.closed)
        with self.assertRaises(NativeError) as stale:
            await second._send("Input.dispatchMouseEvent", {"type": "mousePressed"})
        self.assertEqual(stale.exception.code, "stale_reference")

    async def test_bad_fingerprint_has_no_native_side_effect(self):
        page = self.page()
        with self.assertRaises(NativeError):
            await page.guarded_input("keyboard.insert_text", {"text": "x"}, "old")
        page.context.cdp.send.assert_not_awaited()

    async def test_changed_broker_source_keeps_clean_borrow_release_but_cannot_bind(self):
        for closed in (False, True):
            page = self.page()
            page.context.cdp.execution_domain = 4
            proof = {"epoch": 17, "broker": "exact-operation", "phase": "borrow-released",
                     "trust_profile": "trusted-core-cpu-v1"}
            async def completed(*args, **kwargs):
                page.observation_id += 1
                if closed:
                    page._closed = True
                return {"capture_stable": True, "input_guard": "native-exact-v1",
                        "writer_completion": proof, "visual_generation": 3}
            page._send = completed
            engine = object.__new__(Engine)
            engine.page_by_id = AsyncMock(return_value=page)
            engine.refs = SimpleNamespace(frame_id=lambda frame: "original-frame")
            before = page.observation_id
            result = await engine.export_broker_page("original-page", {})
            self.assertIs(result["writer_completion"], proof)
            self.assertFalse(result["capture_stable"])
            self.assertEqual(result["input_guard"], "unavailable")
            self.assertEqual(result["document_epoch"], before)
            self.assertEqual(result["frame_id"], "original-frame")
            self.assertEqual(result["execution_domain"], 4)

    async def test_batch_collection_is_task_scoped(self):
        page = self.page()
        page._native_input_batch = (asyncio.current_task(), [])
        await asyncio.create_task(page._send("Input.insertText", {"text": "agent"}))
        page.context.cdp.send.assert_awaited_once_with("Input.insertText", {"text": "agent"}, "s", timeout=30)
        self.assertEqual(page._native_input_batch[1], [])

    async def test_raw_capture_preserves_explicit_native_caret_geometry(self):
        page = self.page()
        reply = {"width": 100, "height": 50, "stride": 400, "dpr_milli": 1000,
                 "theme_edge_rgba": 0xffffffff, "format": "premultiplied-rgba8",
                 "render_fingerprint": "a" * 64, "focused_frame_id": "f", "focused_element_id": "node-1",
                 "caret_kind": "native-shaped-insertion", "caret_rect": [17.5, 3.0, 0.0, 16.0]}
        page.context.cdp.send.return_value = reply
        self.assertIs(await page.capture_raw_frame("/private/frame", 2), reply)
        for changed in ({"caret_kind": "estimated"}, {"caret_rect": [0, 0, -1, 12]},
                        {"caret_rect": [float("nan"), 0, 0, 12]}, {"focused_frame_id": None}):
            page.context.cdp.send.return_value = {**reply, **changed}
            with self.subTest(changed=changed), self.assertRaises(NativeError):
                await page.capture_raw_frame("/private/frame", 2)

    def test_native_render_mismatch_is_stale_not_a_runtime_error(self):
        error = ProtocolError("Input.dispatchNativeAction", {"code": -32042, "message": "stale native render fingerprint"})
        self.assertEqual(error.code, "stale_reference")
