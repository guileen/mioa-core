"""Isolated regression for the restored bounded ``tools.axtree`` observation.

No native runtime, profile, network or product test suite is involved. The fake
frame/handle exercises the real Engine -> ScriptOperations -> RefStore ->
ControlState path, including sensitivity masking and stale-ownership rejection.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for package in ("mioa-core", "mioa-headless"):
    path = str(ROOT / package)
    if path not in sys.path:
        sys.path.insert(0, path)

from mioa_core import native as native_module  # noqa: E402
from mioa_core import profile  # noqa: E402
from mioa_core.engine import Engine  # noqa: E402
from mioa_core.refs import ActionError  # noqa: E402
from mioa_headless.native_service import NativeProtocol  # noqa: E402
from mioa_headless.service import Protocol, ServiceError, Session, decode_request  # noqa: E402

ROWS = [
    {"tag": "a", "role": "link", "type": None, "name": "nav", "text": "Hello",
     "placeholder": "", "value": None, "disabled": False, "sensitive": False},
    {"tag": "input", "role": "textbox", "type": "password", "name": "n" * 200,
     "text": "hunter2", "placeholder": "placeholder", "value": "secret",
     "disabled": False, "sensitive": True},
]


class _Node:
    def __init__(self) -> None:
        self.disposed = False

    def as_element(self) -> "_Node":
        return self

    async def evaluate(self, script: str) -> bool:
        return True

    async def dispose(self) -> None:
        self.disposed = True


class _Handle:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.disposed = False

    async def evaluate(self, script: str, *args):
        if "s.view.every" in script:
            return True
        return {"title": "Regression", "url": "https://example.test/", "elements": self.rows,
                "total": len(self.rows), "truncated": False, "shadow_roots": 0, "frames": 0}

    async def evaluate_handle(self, script: str, index: int) -> _Node:
        return _Node()

    async def dispose(self) -> None:
        self.disposed = True


class _Frame:
    frame_id = "frame-regression"

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.captures = 0

    async def evaluate_handle(self, script: str, options: dict) -> _Handle:
        assert "MutationObserver" in script
        assert 1 <= options["limit"] <= 300 and 1 <= options["text"] <= 120
        self.captures += 1
        return _Handle(copy.deepcopy(self.rows[:options["limit"]]))


class _Page:
    def __init__(self, rows: list[dict]) -> None:
        self.main_frame = _Frame(rows)
        self.url = "https://example.test/"
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


class AxtreeObservationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self._base = profile.BASE
        profile.BASE = Path(self.tempdir.name)
        self._launch = native_module.launch_native
        self.page = _Page(ROWS)

        async def fake_launch(name, cfg, headless):
            return SimpleNamespace(context=SimpleNamespace(pages=[self.page]),
                                   receipt={"runtime": "lightpanda"}, pid=4242)

        native_module.launch_native = fake_launch

    def tearDown(self) -> None:
        native_module.launch_native = self._launch
        profile.BASE = self._base
        self.tempdir.cleanup()

    async def _engine(self) -> Engine:
        engine = Engine("axtree-regression", headless=True, initial_url=None, verify=False)
        await engine._launch({"runtime": {"id": "lightpanda"}})
        return engine

    async def test_observation_publication_and_masking(self) -> None:
        engine = await self._engine()
        self.assertEqual(engine.operations.axtree, engine._axtree)

        result = await Session(engine).axtree({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "axtree")
        self.assertEqual(result["source"], "dom")
        self.assertTrue(result["coverage"]["selected_frame"])
        self.assertEqual(result["page_id"], engine.operations.page_id(self.page))
        self.assertEqual(result["frame_id"], "frame-regression")
        self.assertEqual(engine.control.snapshot()["observation_id"], result["observation_id"])
        self.assertEqual(engine.control.snapshot()["page_id"], result["page_id"])

        link, password = result["elements"]
        self.assertEqual(link["ref"], "@1")
        self.assertEqual(link["frame_id"], "frame-regression")
        self.assertEqual(link["text"], "Hello")
        self.assertEqual(password["ref"], "@2")
        self.assertEqual(password["text"], "<redacted>")
        self.assertEqual(password["value"], "<redacted>")

    async def test_bounds_and_unknown_fields_are_rejected(self) -> None:
        engine = await self._engine()
        session = Session(engine)
        frame = self.page.main_frame
        for params, code in (({"max_elements": 0}, "invalid_config"),
                             ({"max_elements": 301}, "invalid_config"),
                             ({"max_text": 0}, "invalid_config"),
                             ({"max_text": True}, "invalid_config"),
                             ({"unknown": 1}, "invalid_config")):
            with self.subTest(params=params):
                with self.assertRaises(ServiceError) as context:
                    await session.axtree(params)
                self.assertEqual(context.exception.code, code)
        self.assertEqual(frame.captures, 0)
        self.assertIsNone(engine.control.snapshot()["observation_id"])

    async def test_text_bound_truncates_rows(self) -> None:
        engine = await self._engine()
        result = await Session(engine).axtree({"max_text": 5})
        link, password = result["elements"]
        self.assertEqual(link["text"], "Hello")
        self.assertEqual(password["name"], "nnnnn")
        self.assertEqual(password["placeholder"], "place")
        self.assertEqual(password["text"], "<redacted>")

    async def test_stale_ownership_invalidates_references(self) -> None:
        engine = await self._engine()
        result = await Session(engine).axtree({})
        reference = result["elements"][0]["ref"]
        self.assertIsNotNone(await engine.refs.resolve(self.page, reference))

        engine.control.pause("ownership_changed")
        self.assertIsNone(engine.control.snapshot()["observation_id"])
        with self.assertRaises(ActionError) as context:
            await engine.refs.resolve(self.page, reference)
        self.assertEqual(context.exception.code, "stale_reference")
        with self.assertRaises(ActionError) as context:
            await engine.refs.validate(engine.refs.snapshot, self.page)
        self.assertEqual(context.exception.code, "stale_reference")

    async def test_wire_ledgers_dispatch_axtree(self) -> None:
        engine = await self._engine()
        for native in (False, True):
            request = decode_request(b'{"id":7,"method":"axtree","params":{}}\n', native=native)
            self.assertEqual(request["method"], "axtree")
            self.assertEqual(request["params"], {})

        stdio = Protocol(engine, Path("/tmp"))
        written: list[dict] = []
        seen = asyncio.Event()

        async def stdio_emit(value: dict) -> None:
            written.append(value)
            seen.set()

        stdio.emit = stdio_emit
        stdio.queue.put_nowait({"id": 3, "method": "axtree", "params": {"max_elements": 1}})
        task = asyncio.create_task(stdio.process_requests())
        try:
            await asyncio.wait_for(seen.wait(), 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(written[0]["id"], 3)
        self.assertEqual(len(written[0]["result"]["elements"]), 1)

        native = NativeProtocol(engine, Path("/tmp"), Path("/tmp/s.sock"), Path("/tmp/s.shm"))
        native.session = Session(engine)
        native_written: list[dict] = []

        async def native_emit(value: dict) -> None:
            native_written.append(value)

        native.emit = native_emit
        await native.respond({"id": 4, "method": "axtree", "params": {}})
        self.assertNotIn("error", native_written[0])
        self.assertEqual(native_written[0]["result"]["source"], "dom")

    async def test_active_run_cannot_be_interleaved(self) -> None:
        engine = await self._engine()
        session = Session(engine)
        engine.control.begin_run("run-regression")
        with self.assertRaises(ServiceError) as context:
            await session.axtree({})
        self.assertEqual(context.exception.code, "busy")
        self.assertEqual(self.page.main_frame.captures, 0)
        engine.control.finish_run("run-regression")

        result = await session.axtree({})
        self.assertEqual(result["source"], "dom")
        self.assertEqual(engine.control.snapshot()["observation_id"], result["observation_id"])


if __name__ == "__main__":
    unittest.main()
