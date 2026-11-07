"""Guarded input keeps the original native mouse/key semantics in one command."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mioa_core.native import NativePage, NativeError, ProtocolError


class GuardedInputTests(unittest.IsolatedAsyncioTestCase):
    def page(self):
        cdp = SimpleNamespace(send=AsyncMock(return_value={}))
        return NativePage(SimpleNamespace(cdp=cdp), "p", "s", "about:blank")

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

    async def test_bad_fingerprint_has_no_native_side_effect(self):
        page = self.page()
        with self.assertRaises(NativeError):
            await page.guarded_input("keyboard.insert_text", {"text": "x"}, "old")
        page.context.cdp.send.assert_not_awaited()

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
