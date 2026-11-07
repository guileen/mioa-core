"""Protocol/lease regressions; actual native lifecycle is an integration check."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mioa_core.control import ControlError, ControlState
from mioa_core.native import NativePage, ProtocolError
from mioa_core.native_spec import NativeError
from mioa_core.refs import ActionError, RefStore, _CHECK_JS
from mioa_headless.service import (
    MAX_QUEUED_REQUESTS,
    MAX_REQUEST_BYTES,
    Protocol,
    ServiceError,
    Session,
    _prepare_profile,
    decode_request,
    make_frame,
    safe_error,
    validate_url,
)


class ProtocolValidationTests(unittest.TestCase):
    def test_request_envelope_and_bounds(self):
        self.assertEqual(decode_request(b'{"id":1,"method":"state"}\n'),
                         {"id": 1, "method": "state", "params": {}})
        invalid = [b'{"id":true,"method":"state"}\n',
                   b'{"id":1,"method":[]}\n',
                   b'{"id":1,"method":"frame","params":[]}\n',
                   b'{"id":1,"method":"action","params":{"x":NaN}}\n',
                   b'{"id":1,"method":"state"}',
                   b" " * MAX_REQUEST_BYTES + b"\n",
                   '{"id":1,"method":"state"}'.encode("utf-16") + b"\n"]
        for line in invalid:
            with self.subTest(line=line[:100]), self.assertRaises(ServiceError):
                decode_request(line)

    def test_navigation_never_accepts_executable_schemes_or_credentials(self):
        self.assertEqual(validate_url(" https://example.com/path "), "https://example.com/path")
        self.assertEqual(validate_url("about:blank"), "about:blank")
        for value in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "example.com",
                      "https://user:password@example.com", "https://example.com/\npath", "https://[broken", "https://example.com:invalid"]:
            with self.subTest(value=value), self.assertRaises(ServiceError):
                validate_url(value)

    def test_frame_metadata_comes_from_png_and_real_observation(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Z8sAAAAASUVORK5CYII=")
        result = {"png": png, "viewport": {"width": 1, "height": 1},
                  "page_id": "page-1", "frame_id": "frame-1", "observation_id": "observation-1"}
        actual, frame = make_frame(result)
        self.assertEqual(actual, png)
        self.assertEqual((frame["width"], frame["height"]), (1, 1))
        self.assertNotEqual(frame["image_id"], make_frame(result)[1]["image_id"])
        for changed in [{"width": 2}, {"viewport": {"width": 100, "height": 1}},
                        {"observation_id": None}, {"png": b"fake"}]:
            with self.subTest(changed=changed), self.assertRaises(ServiceError):
                make_frame({**result, **changed})

    def test_errors_do_not_return_native_logs_or_config(self):
        error = RuntimeError("seed=private; proxy=password; raw native log")
        error.code = "invalid_config"
        self.assertNotIn("private", json.dumps(safe_error(error)))
        self.assertNotIn("password", json.dumps(safe_error(error)))
        self.assertEqual(safe_error(TimeoutError())["code"], "timeout")

    def test_corrupted_existing_profile_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            original = b"not a valid configuration\n"
            config.write_bytes(original)
            profile = SimpleNamespace(profile_dir=lambda _: root, load_config=lambda _: None)
            with self.assertRaises(ServiceError) as raised:
                _prepare_profile(SimpleNamespace(profile="test"), profile, None)
            self.assertEqual(raised.exception.code, "incompatible_profile")
            self.assertEqual(config.read_bytes(), original)


class ObserverSnapshot:
    """Model observer delivery separately from current document/view identity."""
    def __init__(self):
        self.mutations = 0
        self.document_current = self.viewport_current = True

    async def evaluate(self, script, check_mutations=True):
        if script == _CHECK_JS:
            return (self.document_current and self.viewport_current
                    and (not check_mutations or self.mutations == 0))
        if "width:s.view[2]" in script:
            return {"width": 100, "height": 80}
        if "observer.disconnect" in script:
            return None
        raise AssertionError("Unexpected snapshot evaluation")

    async def dispose(self):
        pass


class NativeViewportAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_capture_survives_observer_delivery_but_png_and_refs_do_not(self):
        for raw in (True, False):
            with self.subTest(raw=raw):
                handle = ObserverSnapshot()
                async def pixels(*args, **kwargs):
                    handle.mutations += 1
                    return {"render_fingerprint": "a" * 64} if raw else b"png"
                page = SimpleNamespace(is_closed=lambda: False, capture_raw_frame=pixels, screenshot=pixels)
                refs = RefStore()
                record = {"page": page, "versions": None, "handle": handle,
                          "page_id": "page-1", "frame_id": "frame-1", "observation_id": "observation-1"}
                refs.visual = record
                if raw:
                    result = await refs.viewport(page, raw_destination=("/unused/frame", 1))
                    self.assertEqual(result["viewport"], {"width": 100, "height": 80})
                    self.assertIs(refs.visual, record)
                else:
                    with self.assertRaises(ActionError):
                        await refs.viewport(page)
                refs.references["@1"] = (record, 0)
                with self.assertRaises(ActionError):
                    await refs.resolve(page, "@1")

    async def test_raw_capture_still_rejects_replaced_document_or_view(self):
        for identity in ("document_current", "viewport_current"):
            with self.subTest(identity=identity):
                handle = ObserverSnapshot()
                async def pixels(*_):
                    setattr(handle, identity, False)
                    return {"render_fingerprint": "a" * 64}
                page = SimpleNamespace(is_closed=lambda: False, capture_raw_frame=pixels)
                refs = RefStore()
                refs.visual = {"page": page, "versions": None, "handle": handle}
                with self.assertRaises(ActionError):
                    await refs.viewport(page, raw_destination=("/unused/frame", 1))


class SessionLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = SimpleNamespace(control=ControlState(page_id="page-1", frame_id="frame-1"))
        self.session = Session(self.engine)

    async def observed_human_frame(self):
        await self.session.control({"operation": "request_human"})
        self.engine.control.set_observation("page-1", "frame-1", "observation-1")
        self.session.frame = {"page_id": "page-1", "frame_id": "frame-1", "observation_id": "observation-1",
                              "image_id": "image-1", "viewport": {"width": 100, "height": 80}}
        return {"operation": "mouse.click", "args": {"x": 10, "y": 20}, "image_id": "image-1",
                "page_id": "page-1", "frame_id": "frame-1", "observation_id": "observation-1",
                "owner_revision": self.engine.control.owner_revision}

    async def human_resize_request(self):
        observed = await self.observed_human_frame()
        page = SimpleNamespace(page_id="page-1", _send=AsyncMock())
        self.engine.page = lambda: page
        self.engine.operations = SimpleNamespace(page_id=lambda target: target.page_id)
        self.engine.refresh_pages = AsyncMock()
        self.engine.refs = SimpleNamespace(clear=AsyncMock())
        return {"width": 800, "height": 600, "device_scale_factor": 2,
                "page_id": observed["page_id"], "owner_revision": observed["owner_revision"]}

    async def test_human_token_stays_private_and_handback_is_explicit(self):
        state = (await self.session.control({"operation": "request_human"}))["state"]
        self.assertTrue(state["human_control"])
        self.assertIsNone(state["owner_id"])
        self.assertNotIn(self.session.human_id, json.dumps(state))
        with self.assertRaises(ServiceError):
            await self.session.control({"operation": "resume_agent", "owner_revision": state["owner_revision"] - 1})
        resumed = await self.session.control({"operation": "resume_agent", "owner_revision": state["owner_revision"]})
        self.assertEqual(resumed["state"]["owner"], "agent")
        self.assertFalse(resumed["state"]["human_control"])

    async def test_another_viewer_cannot_be_mistaken_for_this_service(self):
        await self.engine.control.request_human("different-viewer")
        self.assertFalse(self.session.snapshot()["human_control"])
        with self.assertRaises(ServiceError):
            await self.session.control({"operation": "reclaim", "owner_revision": self.engine.control.owner_revision})

    async def test_every_action_requires_current_lease_image_and_observation(self):
        request = await self.observed_human_frame()
        _, args = self.session.validate_action(request)
        self.assertEqual(args, {"x": 10, "y": 20, "button": "left", "click_count": 1})
        for changed in [{"image_id": "old"}, {"page_id": "old"}, {"frame_id": "old"},
                        {"observation_id": "old"}, {"owner_revision": True}]:
            with self.subTest(changed=changed), self.assertRaises((ServiceError, ControlError)):
                self.session.validate_action({**request, **changed})
        self.engine.control.invalidate_observation()
        with self.assertRaises(ControlError):
            self.session.validate_action(request)

    async def test_navigation_and_tab_controls_have_identical_stale_checks(self):
        request = await self.observed_human_frame()
        for operation, args in [("navigate", {"url": "https://example.com"}), ("new_tab", {}),
                                ("back", {}), ("forward", {}), ("reload", {}),
                                ("select_tab", {"target_page_id": "page-2"}),
                                ("close_tab", {"target_page_id": "page-2"})]:
            action = {**request, "operation": operation, "args": args}
            self.session.validate_action(action)
            with self.subTest(operation=operation), self.assertRaises(ServiceError):
                self.session.validate_action({**action, "image_id": "old"})

    async def test_input_bounds_and_unknown_fields_are_not_forwarded(self):
        request = await self.observed_human_frame()
        for args in [{"x": 100, "y": 10}, {"x": True, "y": 10}, {"x": 1, "y": 1, "button": []},
                     {"x": 1, "y": 1, "force": True}]:
            with self.subTest(args=args), self.assertRaises(ServiceError):
                self.session.validate_action({**request, "args": args})

    async def test_recent_images_only_survive_identical_semantic_scope(self):
        request = await self.observed_human_frame()
        page = SimpleNamespace(observation_id=4, native_visual_epoch=0)
        self.engine.page = lambda: page
        self.session.frame["frame_seq"] = 32
        self.session.recent_frame_ids = [f"image-{n}" for n in range(1, 33)]
        self.session.frame_scopes["image-1"] = self.session._scope(self.session.frame)
        self.session.validate_action({**request, "frame_seq": 1})
        with self.assertRaises(ServiceError):
            self.session.validate_action({**request, "frame_seq": 0})
        for field in ("observation_id", "native_visual_epoch"):
            original = getattr(page, field)
            setattr(page, field, original + 1)
            with self.subTest(field=field), self.assertRaises(ServiceError):
                self.session.validate_action(request)
            setattr(page, field, original)
        self.session.viewport_epoch += 1
        with self.assertRaises(ServiceError):
            self.session.validate_action(request)

    async def test_old_image_cannot_cross_owner_epoch_even_with_new_revision(self):
        request = await self.observed_human_frame()
        self.engine.page = lambda: SimpleNamespace(observation_id=1)
        self.session.frame_scopes["image-1"] = self.session._scope(self.session.frame)
        self.engine.control.owner_revision += 1
        with self.assertRaises(ServiceError):
            self.session.validate_action({**request, "owner_revision": self.engine.control.owner_revision})

    async def test_human_navigation_failure_keeps_lease_and_consumes_image(self):
        request = await self.observed_human_frame()
        self.engine.refresh_pages = AsyncMock()
        self.engine.page = lambda: object()
        self.engine.refs = SimpleNamespace(visual=object(), validate=AsyncMock(), clear=AsyncMock())
        self.session._execute = AsyncMock(side_effect=ServiceError("network_unavailable", "Navigation failed."))
        with self.assertRaises(ServiceError):
            await self.session.action({**request, "operation": "navigate", "args": {"url": "https://example.com"}})
        self.assertEqual(self.engine.control.owner, "human")
        self.assertEqual(self.engine.control.owner_id, self.session.human_id)
        self.assertIsNone(self.session.frame)
        self.assertIsNone(self.engine.control.observation_id)

    async def test_stop_interrupts_pending_navigation_without_cancelling_human_lease(self):
        resize = await self.human_resize_request()
        request = await self.observed_human_frame()
        page = self.engine.page()
        page.url = "https://example.com"
        self.engine.name = "test"
        self.engine.refs.visual = object()
        self.engine.refs.validate = AsyncMock()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def navigate(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.session._execute = navigate
        with patch("mioa_core.profile.load_state", return_value={}), patch("mioa_core.profile.save_state"):
            action = asyncio.create_task(self.session.action(
                {**request, "operation": "navigate", "args": {"url": page.url}}))
            await asyncio.wait_for(entered.wait(), 1)
            stopped = await asyncio.wait_for(self.session.stop(
                {key: resize[key] for key in ("page_id", "owner_revision")}), 1)
            result = await asyncio.wait_for(action, 1)
        page._send.assert_awaited_once_with("Page.stopLoading")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.engine.control.owner, "human")
        self.assertEqual(self.engine.control.owner_id, self.session.human_id)
        self.assertIsNone(self.engine.control.observation_id)

    async def test_stop_rejects_stale_cross_page_and_agent_ownership_before_native_dispatch(self):
        resize = await self.human_resize_request()
        params = {key: resize[key] for key in ("page_id", "owner_revision")}
        page = self.engine.page()
        for changed in ({"page_id": "page-2"}, {"owner_revision": params["owner_revision"] - 1},
                        {"owner_revision": True}):
            with self.subTest(changed=changed), self.assertRaises((ServiceError, ControlError)):
                await self.session.stop({**params, **changed})
        page.page_id = "different-active-page"
        with self.assertRaises(ServiceError):
            await self.session.stop(params)
        page.page_id = "page-1"
        await self.session.control({"operation": "resume_agent", "owner_revision": params["owner_revision"]})
        with self.assertRaises(ControlError):
            await self.session.stop({**params, "owner_revision": self.engine.control.owner_revision})
        page._send.assert_not_awaited()

    async def test_stop_does_not_hide_unrelated_pending_navigation_failure(self):
        resize = await self.human_resize_request()
        request = await self.observed_human_frame()
        self.engine.refs.visual = object()
        self.engine.refs.validate = AsyncMock()
        entered, fail = asyncio.Event(), asyncio.Event()
        async def navigate(*_):
            entered.set()
            await fail.wait()
            raise ServiceError("network_unavailable", "A real unrelated failure.")
        async def stop(*_):
            fail.set()
            await asyncio.sleep(0)
            return {}
        self.session._execute = navigate
        self.engine.page()._send.side_effect = stop
        action = asyncio.create_task(self.session.action(
            {**request, "operation": "reload", "args": {}}))
        await asyncio.wait_for(entered.wait(), 1)
        await self.session.stop({key: resize[key] for key in ("page_id", "owner_revision")})
        with self.assertRaises(ServiceError) as raised:
            await asyncio.wait_for(action, 1)
        self.assertEqual(str(raised.exception), "A real unrelated failure.")
        self.assertEqual(self.engine.control.owner, "human")

    async def test_native_err_aborted_from_successful_stop_is_a_stopped_outcome(self):
        resize = await self.human_resize_request()
        request = await self.observed_human_frame()
        page = self.engine.page()
        page.url = "about:blank"
        self.engine.name = "test"
        self.engine.refs.visual = object()
        self.engine.refs.validate = AsyncMock()
        entered, abort, aborted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def navigate(*_):
            entered.set()
            await abort.wait()
            aborted.set()
            raise NativeError("network_unavailable", "net::ERR_ABORTED", details={"errorText": "net::ERR_ABORTED"})
        async def stop(*_):
            abort.set()
            await aborted.wait()
            await asyncio.sleep(0)
            return {}
        self.session._execute = navigate
        page._send.side_effect = stop
        with patch("mioa_core.profile.load_state", return_value={}), patch("mioa_core.profile.save_state"):
            action = asyncio.create_task(self.session.action(
                {**request, "operation": "navigate", "args": {"url": "https://example.com"}}))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.session.stop(
                {key: resize[key] for key in ("page_id", "owner_revision")}), 1)
            result = await asyncio.wait_for(action, 1)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.engine.control.owner, "human")

    async def test_native_authority_accepts_late_observer_but_consumes_changed_frame(self):
        for operation, args in (("mouse.click", {"x": 10, "y": 20}),
                                ("keyboard.press", {"key": "Enter"})):
            for changed_native_frame in (False, True):
                with self.subTest(operation=operation, changed_native_frame=changed_native_frame):
                    request = await self.observed_human_frame()
                    self.session.frame["render_fingerprint"] = "a" * 64
                    self.session.ring = object()
                    self.engine.refresh_pages = AsyncMock()
                    self.engine.name = "test"
                    self.session._execute = AsyncMock()
                    dispatched, accepted_events = [], []
                    async def native_turn(method, params, *_args, **_kwargs):
                        self.assertIsNone(self.session.frame)
                        dispatched.append(method)
                        current = ("b" if changed_native_frame else "a") * 64
                        if params["expectedRenderFingerprint"] != current:
                            raise ProtocolError(method, {"code": -32042, "message": "stale native render fingerprint"})
                        accepted_events.extend(params["events"])
                        return {}
                    page = NativePage(SimpleNamespace(cdp=SimpleNamespace(send=native_turn)),
                                      "page-1", "session-1", "about:blank")
                    page.native_visual_epoch = 0
                    self.engine.page = lambda: page
                    handle = ObserverSnapshot()
                    refs = RefStore()
                    refs.visual = {"page": page, "versions": None, "handle": handle}
                    self.engine.refs = refs
                    self.session.frame_scopes[request["image_id"]] = self.session._scope(self.session.frame)
                    # A delayed notification and style MutationObserver delivery are
                    # older than the native pixels; neither may veto native dispatch.
                    page.native_visual_epoch += 1
                    handle.mutations += 1
                    with self.assertRaises(ActionError):
                        await refs.validate(refs.visual, page)
                    action = {**request, "operation": operation, "args": args}
                    with patch("mioa_core.profile.load_state", return_value={}), patch("mioa_core.profile.save_state"):
                        if changed_native_frame:
                            with self.assertRaises(ProtocolError) as raised:
                                await self.session.action(action)
                            self.assertEqual(raised.exception.code, "stale_reference")
                            self.assertFalse(accepted_events)
                        else:
                            result = await self.session.action(action)
                            self.assertEqual(result["status"], "ok")
                            self.assertTrue(accepted_events)
                    self.assertEqual(dispatched, ["Input.dispatchNativeAction"])
                    self.assertIsNone(self.session.frame)
                    self.assertFalse(self.session.frame_scopes)
                    self.assertFalse(self.session.recent_frame_ids)
                    self.assertIsNone(self.engine.control.observation_id)
                    self.assertEqual(self.engine.control.owner, "human")
                    with self.assertRaises(ServiceError):
                        await self.session.action(action)
                    self.assertEqual(len(dispatched), 1)
                    self.session._execute.assert_not_awaited()

    async def test_native_pointer_rebinds_on_animation_tick_but_rejects_scope_change(self):
        request = await self.observed_human_frame()
        self.session.frame["render_fingerprint"] = "a" * 64
        self.session.ring = SimpleNamespace(path=Path("/unused/frame"), back=1)
        self.engine.refresh_pages = AsyncMock()
        self.engine.name = "test"
        seen_fingerprints = []
        async def guarded_input(operation, args, fingerprint, *, rebind=None):
            self.assertIsNone(self.session.frame)
            seen_fingerprints.append(fingerprint)
            if len(seen_fingerprints) == 1 and rebind is not None:
                seen_fingerprints.append(await rebind())
        page = SimpleNamespace(
            observation_id=4,
            url="about:blank",
            guarded_input=guarded_input,
            capture_raw_frame=AsyncMock(return_value={
                "render_fingerprint": "b" * 64,
                "focused_frame_id": self.session.frame.get("focused_frame_id"),
                "focused_element_id": self.session.frame.get("focused_element_id"),
            }),
        )
        self.engine.page = lambda: page
        self.engine.refs = SimpleNamespace(visual=object(), validate=AsyncMock(), clear=AsyncMock())
        self.session.frame_scopes[request["image_id"]] = self.session._scope(self.session.frame)
        with patch("mioa_core.profile.load_state", return_value={}), patch("mioa_core.profile.save_state"):
            result = await self.session.action({**request, "operation": "mouse.click", "args": {"x": 10, "y": 10}})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen_fingerprints, ["b" * 64, "b" * 64])
        self.assertIsNone(self.session.frame)

    async def test_native_action_rechecks_document_after_page_refresh(self):
        request = await self.observed_human_frame()
        self.session.ring = object()
        self.session.frame["render_fingerprint"] = "a" * 64
        page = SimpleNamespace(observation_id=4, guarded_input=AsyncMock())
        self.engine.page = lambda: page
        self.engine.refs = SimpleNamespace(visual=object(), validate=AsyncMock(), clear=AsyncMock())
        self.session.frame_scopes[request["image_id"]] = self.session._scope(self.session.frame)
        async def refresh():
            page.observation_id += 1
        self.engine.refresh_pages = refresh
        with self.assertRaises(ServiceError) as raised:
            await self.session.action(request)
        self.assertEqual(raised.exception.code, "stale_reference")
        page.guarded_input.assert_not_awaited()

    async def test_native_input_requires_render_fingerprint(self):
        request = await self.observed_human_frame()
        self.session.ring = object()
        page = SimpleNamespace(guarded_input=AsyncMock())
        self.engine.page = lambda: page
        self.engine.refresh_pages = AsyncMock()
        self.engine.refs = SimpleNamespace(visual=object(), validate=AsyncMock(), clear=AsyncMock())
        for fingerprint in (None, "", "z" * 64):
            with self.subTest(fingerprint=fingerprint):
                self.session.frame["render_fingerprint"] = fingerprint
                with self.assertRaises(ServiceError) as raised:
                    await self.session.action(request)
                self.assertEqual(raised.exception.code, "stale_reference")
        page.guarded_input.assert_not_awaited()

    async def test_changed_render_fingerprint_invalidates_recent_images(self):
        request = await self.observed_human_frame()
        self.engine.page = lambda: SimpleNamespace(observation_id=1)
        self.session.frame["render_fingerprint"] = "a" * 64
        self.session.frame_scopes[request["image_id"]] = self.session._scope(self.session.frame)
        self.session.frame["render_fingerprint"] = "b" * 64
        with self.assertRaises(ServiceError):
            self.session.validate_action(request)

    async def test_resize_invalidates_observation_before_native_dispatch(self):
        request = await self.human_resize_request()

        async def send(method, params):
            self.assertIsNone(self.session.frame)
            self.assertFalse(self.session.recent_frame_ids)
            self.assertFalse(self.session.frame_scopes)
            self.assertIsNone(self.engine.control.observation_id)
            self.assertEqual(self.engine.control._active_lease, ("human", self.session.human_id))
            self.engine.refs.clear.assert_awaited_once()

        self.engine.page()._send.side_effect = send
        await self.session.resize(request)
        self.engine.page()._send.assert_awaited_once_with("Emulation.setDeviceMetricsOverride",
            {"width": 800, "height": 600, "deviceScaleFactor": 2, "mobile": False})
        self.assertIsNone(self.session.frame)
        self.assertIsNone(self.engine.control.observation_id)
        self.assertEqual(self.engine.control.owner, "human")
        with self.assertRaises(ServiceError):
            await self.session.resize({**request, "width": 3840, "height": 2160})

    async def test_resize_rejects_agent_ownership_without_dispatch(self):
        request = await self.human_resize_request()
        await self.session.control({"operation": "resume_agent", "owner_revision": request["owner_revision"]})
        self.engine.control.begin_run("agent-run")
        request["owner_revision"] = self.engine.control.owner_revision
        with self.assertRaises(ControlError) as raised:
            await self.session.resize(request)
        self.assertEqual(raised.exception.code, "paused")
        self.engine.page()._send.assert_not_awaited()
        self.engine.refresh_pages.assert_not_awaited()
        self.engine.refs.clear.assert_not_awaited()

    async def test_resize_requires_target_and_exact_ownership_revision(self):
        request = await self.human_resize_request()
        for missing in ("page_id", "owner_revision"):
            with self.subTest(missing=missing), self.assertRaises(ServiceError):
                await self.session.resize({key: value for key, value in request.items() if key != missing})
        for changed in ({"page_id": "old-page"}, {"owner_revision": request["owner_revision"] - 1}):
            with self.subTest(changed=changed), self.assertRaises(ControlError) as raised:
                await self.session.resize({**request, **changed})
            self.assertEqual(raised.exception.code, "stale_reference")
        for changed in ({"page_id": None}, {"owner_revision": True}, {"owner_revision": None}):
            with self.subTest(changed=changed), self.assertRaises(ServiceError):
                await self.session.resize({**request, **changed})
        self.engine.page()._send.assert_not_awaited()
        self.engine.refs.clear.assert_not_awaited()
        self.assertIsNotNone(self.session.frame)

    async def test_resize_rechecks_active_page_after_refresh(self):
        request = await self.human_resize_request()
        target = self.engine.page()
        replacement = SimpleNamespace(page_id="page-2", _send=AsyncMock())
        self.engine.refresh_pages.side_effect = lambda: setattr(self.engine, "page", lambda: replacement)
        with self.assertRaises(ServiceError) as raised:
            await self.session.resize(request)
        self.assertEqual(raised.exception.code, "stale_reference")
        target._send.assert_not_awaited()
        replacement._send.assert_not_awaited()
        self.engine.refs.clear.assert_not_awaited()
        self.assertIsNotNone(self.session.frame)

    async def test_native_frame_metadata_and_capture_share_observation_boundary(self):
        first = SimpleNamespace(page_id="page-1", url="https://example.com/one", observation_id=1)
        second = SimpleNamespace(page_id="page-2", url="https://example.com/two", observation_id=2)
        active = first
        contender = None
        action_entered = asyncio.Event()

        async def select_second():
            nonlocal active
            async with self.engine.control.action("agent", "agent"):
                action_entered.set()
                active = second
                self.engine.control.set_page("page-2", "frame-2")

        def assert_boundary():
            self.assertIs(self.engine.control._active_task, asyncio.current_task())
            self.assertEqual(self.engine.control._active_lease, ("observation", ""))
            self.assertFalse(action_entered.is_set())

        async def title():
            nonlocal contender
            assert_boundary()
            if contender is None:
                contender = asyncio.create_task(select_second())
                await asyncio.sleep(0)
                self.assertFalse(action_entered.is_set())
            return "Example"

        first.title = second.title = title
        self.engine.name = "profile-one"
        self.engine.page = lambda: active
        self.engine.context = SimpleNamespace(pages=[first, second])
        self.engine.native = SimpleNamespace(config={"resolved": {"device_scale_factor": 1}})
        self.engine.operations = SimpleNamespace(page_id=lambda page: page.page_id)

        async def refresh():
            assert_boundary()

        async def viewport(page, *, raw_destination):
            assert_boundary()
            self.assertIs(page, first)
            self.assertEqual(raw_destination, ("/unused/frame.shm", 1))
            return {"page_id": "page-1", "frame_id": "frame-1", "observation_id": "observation-1",
                    "viewport": {"width": 100, "height": 80}, "width": 100, "height": 80,
                    "stride": 400, "dpr_milli": 1000, "theme_edge_rgba": 0, "caret_rect": [0, 0, 0, 0],
                    "caret_kind": "none",
                    "render_fingerprint": "a" * 64}

        def publish(result):
            assert_boundary()
            return 7

        self.engine.refresh_pages = AsyncMock(side_effect=refresh)
        self.engine.refs = SimpleNamespace(frame_id=lambda page: "frame-1" if page is first else "frame-2",
                                           viewport=AsyncMock(side_effect=viewport))
        self.session.ring = SimpleNamespace(path=Path("/unused/frame.shm"), back=1, publish=publish)
        result = await self.session.capture()
        await contender
        self.assertTrue(action_entered.is_set())
        self.assertIs(active, second)
        selected_metadata = next(page for page in result["pages"] if page["page_id"] == result["page_id"])
        self.assertEqual(selected_metadata["url"], first.url)
        self.assertEqual(result["page_id"], "page-1")
        self.assertEqual(result["render_fingerprint"], "a" * 64)
        self.assertEqual(result["state"]["page_id"], result["page_id"])
        self.assertEqual(result["state"]["frame_id"], result["frame_id"])
        self.assertEqual(result["state"]["observation_id"], result["observation_id"])
        self.assertEqual(result["state"]["owner_revision"], result["owner_revision"])
        self.assertNotIn("png", result)
        json.dumps(result)

    async def test_state_derives_active_page_after_collecting_metadata(self):
        first = SimpleNamespace(page_id="page-1", url="about:blank")
        second = SimpleNamespace(page_id="page-2", url="https://example.com")
        active = first

        async def title():
            nonlocal active
            active = second
            return "Selected"

        first.title = second.title = title
        self.engine.name = "profile-one"
        self.engine.page = lambda: active
        self.engine.context = SimpleNamespace(pages=[first, second])
        self.engine.operations = SimpleNamespace(page_id=lambda page: page.page_id)
        self.engine.refs = SimpleNamespace(frame_id=lambda page: "frame-2")
        self.engine.refresh_pages = AsyncMock()
        result = await self.session.state()
        self.assertEqual(result["page_id"], "page-2")
        self.assertEqual(result["state"]["page_id"], "page-2")
        self.assertEqual(result["trust"], {"proxy_mode": None, "region": None})
        for proxy_ref, mode in ((None, "DIRECT"), ("env:PRIVATE_PROXY", "PROXY")):
            self.engine.native = SimpleNamespace(config={"network": {"proxy_ref": proxy_ref},
                                                         "seed": "private-seed"})
            trust = (await self.session.state())["trust"]
            self.assertEqual(trust, {"proxy_mode": mode, "region": None})
            self.assertNotIn("PRIVATE_PROXY", json.dumps(trust))
            self.assertNotIn("private-seed", json.dumps(trust))

    async def test_stdio_frame_keeps_png_transport(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Z8sAAAAASUVORK5CYII=")
        self.engine._viewer_execute = AsyncMock(return_value={
            "png": png, "viewport": {"width": 1, "height": 1},
            "page_id": "page-1", "frame_id": "frame-1", "observation_id": "observation-1"})
        self.engine.page = lambda: SimpleNamespace(observation_id=1)
        result = await self.session.capture()
        self.assertEqual(base64.b64decode(result["png"]), png)
        self.assertNotIn("shm", result)
        self.engine._viewer_execute.assert_awaited_once_with("viewer.frame", {})

    async def test_history_rejects_privileged_urls_before_navigation(self):
        history = {"entries": [{"id": 31, "url": "https://example.com/one"},
                               {"id": 32, "url": "file:///private"}], "currentIndex": 0}

        async def send(method, params=None, **options):
            if method != "Page.getNavigationHistory":
                self.fail("A privileged history entry reached native navigation.")
            return history

        page = SimpleNamespace(_send=send)
        with self.assertRaises(ServiceError) as raised:
            await self.session._history(page, 1)
        self.assertEqual(raised.exception.code, "invalid_config")

    async def test_shutdown_bypasses_full_bounded_queue(self):
        engine = SimpleNamespace(stopping=asyncio.Event(), control=self.engine.control)
        protocol = Protocol(engine, Path("/unused"))
        reader = asyncio.StreamReader(limit=MAX_REQUEST_BYTES)
        reader.feed_data(b"".join(json.dumps({"id": index, "method": "state"}).encode() + b"\n"
                                   for index in range(MAX_QUEUED_REQUESTS)))
        reader.feed_data(b'{"id":100,"method":"shutdown"}\n')
        await asyncio.wait_for(protocol.read_requests(reader), 1)
        self.assertEqual(protocol.queue.qsize(), MAX_QUEUED_REQUESTS)
        self.assertEqual(protocol.shutdown_id, 100)
        self.assertTrue(engine.stopping.is_set())

    async def test_queue_overflow_is_rejected_without_growing_or_blocking_shutdown(self):
        engine = SimpleNamespace(stopping=asyncio.Event(), control=self.engine.control)
        protocol = Protocol(engine, Path("/unused"))
        protocol.ready = True
        rejected = []

        async def emit(value):
            rejected.append(value)

        protocol.emit = emit
        reader = asyncio.StreamReader(limit=MAX_REQUEST_BYTES)
        reader.feed_data(b"".join(json.dumps({"id": index, "method": "control", "params": {"operation": "request_human"}}).encode() + b"\n"
                                   for index in range(MAX_QUEUED_REQUESTS + 1)))
        reader.feed_data(b'{"id":100,"method":"shutdown"}\n')
        await asyncio.wait_for(protocol.read_requests(reader), 1)
        self.assertEqual(protocol.queue.qsize(), MAX_QUEUED_REQUESTS)
        self.assertEqual(rejected[0]["error"]["code"], "busy")
        self.assertEqual(protocol.shutdown_id, 100)



if __name__ == "__main__":
    unittest.main()
