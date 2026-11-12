"""Page deadlines use the explicitly installed session clock."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mioa_core.native import (NativeContext, NativeElementHandle, NativeError,
                              NativeFrame, NativeLocator, NativePage, NativeSession,
                              adopt_supervised_native, launch_native)
from mioa_os import LocalChildProcesses, LocalSecureRandom


class Clock:
    platform = "Linux"

    def __init__(self):
        self.now = 100.0
        self.reads = 0

    def monotonic_time(self):
        self.reads += 1
        return self.now


class NativeClockTests(unittest.IsolatedAsyncioTestCase):
    def page(self, identity):
        process = SimpleNamespace(stdout=None, stderr=None)
        session = NativeSession(process, {}, Path("unused"), proxy=None, identity=identity)
        cdp = SimpleNamespace(listeners=[], send=AsyncMock())
        return NativePage(NativeContext(session, cdp), "p", "s", "about:blank")

    async def test_navigation_budget_uses_session_clock(self):
        clock = Clock()
        page = self.page(clock)

        async def send(method, params=None, *, timeout=30):
            self.assertEqual(method, "Page.navigate")
            self.assertAlmostEqual(timeout, 1.0)
            clock.now += 0.4
            page._main_frame_commit += 1
            return {}

        page._send = send
        await page.goto("https://example.test", wait_until="commit", timeout=1000)
        self.assertGreater(clock.reads, 3)
        self.assertAlmostEqual(clock.now, 100.4)

    async def test_networkidle_restarts_for_request_and_commit(self):
        for event in ("request", "commit"):
            with self.subTest(event=event):
                clock = Clock()
                page = self.page(clock)
                page.evaluate = AsyncMock(return_value="complete")
                sleeps = []

                async def sleep(delay):
                    sleeps.append(delay)
                    clock.now += delay
                    if len(sleeps) == 4:
                        if event == "request":
                            page._event("Network.requestWillBeSent", {"requestId": "r"})
                            page._event("Network.loadingFinished", {"requestId": "r"})
                        else:
                            page._main_frame_commit += 1

                with patch("mioa_core.native.asyncio.sleep", sleep):
                    await page.wait_for_load_state("networkidle", timeout=2000)
                self.assertGreaterEqual(clock.now, 100.7)
                self.assertGreater(clock.reads, len(sleeps))

    async def test_locator_and_element_handle_share_frame_session_deadline(self):
        clock = Clock()
        page = self.page(clock)
        frame = NativeFrame(page, "child")
        page._frame_ids.add("child")
        locator = NativeLocator(frame, [("css", "input")])
        locator.count = AsyncMock(return_value=0)
        element = NativeElementHandle(frame, {"objectId": "node"})
        element._read = AsyncMock(side_effect=[[0, 0, 1, 1], [1, 0, 1, 1]])

        async def sleep(delay):
            clock.now += delay + 0.000001

        with patch("mioa_core.native.asyncio.sleep", sleep):
            for operation in (lambda: locator.wait_for(state="attached", timeout=50),
                              lambda: element.wait_for_element_state("stable", timeout=50)):
                with self.assertRaises(NativeError) as failed:
                    await operation()
                self.assertEqual(failed.exception.code, "timeout")
        self.assertEqual(element._read.await_count, 2)
        self.assertGreater(clock.reads, 0)

    async def test_missing_identity_fails_before_navigation_or_launch(self):
        page = self.page(None)
        with self.assertRaises(NativeError) as failed:
            await page.goto("https://example.test")
        self.assertEqual(failed.exception.code, "capability_unavailable")
        page.context.cdp.send.assert_not_awaited()
        with self.assertRaises(NativeError) as failed:
            await launch_native("unused", {}, True, child_processes=LocalChildProcesses(),
                                process_environment=object(), random=LocalSecureRandom())
        self.assertEqual(failed.exception.code, "capability_unavailable")

    async def test_missing_random_rejects_both_launch_paths_before_dispatch(self):
        for launch in (
            launch_native("unused", {}, True, child_processes=object(),
                          process_environment=object(), identity=object()),
            adopt_supervised_native("unused", {}, object(), 1, None),
        ):
            with self.assertRaises(NativeError) as failed:
                await launch
            self.assertEqual(failed.exception.code, "capability_unavailable")
            self.assertIn("SecureRandom", str(failed.exception))
