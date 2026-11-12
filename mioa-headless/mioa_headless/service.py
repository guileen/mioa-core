"""Bounded parent/child NDJSON protocol over one owned mioa_core Engine.

This service owns the headless transport only. The shared Engine, ControlState,
RefStore and native process come from mioa_core; no ATB CLI, desktop UI or HTTP
viewer source is imported. ANTI_TRACE_HOME is retained as the profile-root
compatibility name that existing profiles already use.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import struct
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .icon_assets import IconAssets

MAX_REQUEST_BYTES = 32 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_QUEUED_REQUESTS = 16
MAX_PAGES = 100
OPERATION_TIMEOUT = 20
# Pointer input tolerates animation-only render changes; keyboard and non-native
# input keep the stricter capture-scoped fingerprint rule.
POINTER_OPERATIONS = frozenset({"mouse.click", "mouse.move", "mouse.wheel"})
# Browser-chrome actions change the control state, not captured page pixels.
# A real user can press Ctrl+Tab immediately after a click, so these must not
# require the capture basis that accepted input deliberately consumes.
SHELL_OPERATIONS = frozenset({"navigate", "new_tab", "select_tab", "close_tab", "back", "forward", "reload", "page.focus"})
POINTER_REBINDS = 2
_RENDER_FINGERPRINT = re.compile(r"[0-9a-fA-F]{64}")


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Mioa Headless"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Mioa Headless"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mioa-headless"


def validate_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("invalid_config", "Navigation requires a URL string.")
    value = value.strip()
    if not value or len(value) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        raise ServiceError("invalid_config", "Navigation URL is empty, too long or contains invalid characters.")
    if value == "about:blank":
        return value
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme.lower() in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None)
        parsed.port
    except ValueError as exc:
        raise ServiceError("invalid_config", "Navigation URL is malformed.") from exc
    if not valid:
        raise ServiceError("invalid_config", "Only HTTP(S) and about:blank navigation without embedded credentials is allowed.")
    return value


def safe_error(exc: BaseException, *, startup: bool = False) -> dict[str, str]:
    if isinstance(exc, ServiceError):
        return {"code": exc.code, "message": str(exc)}
    code = "timeout" if isinstance(exc, TimeoutError) else getattr(exc, "code", "startup_failed" if startup else "runtime_error")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z_]{0,63}", code):
        code = "startup_failed" if startup else "runtime_error"
    messages = {
        "busy": "The profile or control lease is owned by another operation.",
        "paused": "Human ownership or an explicit agent handback is required.",
        "stale_reference": "The page, image or control state changed; capture a fresh frame.",
        "incompatible_profile": "The saved runtime, binary or font assets are incompatible; explicit migration is required.",
        "capability_unavailable": "The pinned native runtime or a requested runtime capability is unavailable.",
        "invalid_config": "The saved profile or requested operation is invalid.",
        "network_unavailable": "The native runtime could not complete the network operation.",
        "verification_failed": "The native runtime did not provide verifiable session or screenshot data.",
        "timeout": "The native operation timed out; uncertain input remains paused.",
        "protocol_error": "The native runtime did not complete the operation or durable shutdown correctly.",
    }
    return {"code": code, "message": messages.get(code, "The headless service could not start." if startup else "The native runtime operation failed.")}


def decode_request(line: bytes, *, native: bool = False) -> dict[str, Any]:
    if not native and (len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n")):
        raise ServiceError("invalid_config", "Requests must be newline-terminated and at most 32768 bytes.")
    if native and not 0 < len(line) <= 256 * 1024:
        raise ServiceError("invalid_config", "Native requests must be at most 256KiB.")
    try:
        request = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ServiceError("invalid_config", "Request must be valid UTF-8 JSON.") from exc
    if not isinstance(request, dict) or type(request.get("id")) is not int or not 0 <= request["id"] <= 2**53 - 1:
        raise ServiceError("invalid_config", "Request requires a nonnegative integer id.")
    if (set(request) - {"id", "method", "params"} or not isinstance(request.get("method"), str)
            or request["method"] not in {"state", "frame", "control", "action", "axtree", "shutdown", *({"resize", "stop"} if native else set())}):
        raise ServiceError("invalid_config", "Unknown request method or request fields.")
    if not isinstance(request.get("params", {}), dict):
        raise ServiceError("invalid_config", "Request params must be an object.")
    request.setdefault("params", {})
    return request

def _public_url(value: Any) -> str:
    # Page metadata is untrusted; never return URL userinfo to the shell.
    value = str(value or "")[:2048]
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return ""
    return value


def _number(value: Any, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def make_frame(result: Any) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(result, dict):
        raise ServiceError("verification_failed", "The runtime did not return a screenshot.")
    png = result.get("png")
    if (not isinstance(png, bytes) or not 24 <= len(png) <= MAX_FRAME_BYTES
            or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR"):
        raise ServiceError("verification_failed", "A bounded real viewport PNG is required.")
    width, height = struct.unpack(">II", png[16:24])
    if not 0 < width <= 16384 or not 0 < height <= 16384 or width * height > 32_000_000:
        raise ServiceError("verification_failed", "The viewport PNG dimensions exceed service limits.")
    if result.get("width", width) != width or result.get("height", height) != height:
        raise ServiceError("verification_failed", "Screenshot dimensions disagree with the PNG.")
    viewport = result.get("viewport")
    if not isinstance(viewport, dict):
        raise ServiceError("verification_failed", "The runtime omitted CSS viewport dimensions.")
    vw, vh = viewport.get("width"), viewport.get("height")
    if not _number(vw, 1, 16384) or not _number(vh, 1, 16384) or abs(width / height - vw / vh) > max(0.01, 2 / height):
        raise ServiceError("verification_failed", "Screenshot and CSS viewport geometry are inconsistent.")
    if any(not isinstance(result.get(key), str) or not 0 < len(result[key]) <= 256
           for key in ("page_id", "frame_id", "observation_id")):
        raise ServiceError("verification_failed", "The runtime omitted actual page/frame/observation identifiers.")
    frame = {key: result[key] for key in ("page_id", "frame_id", "observation_id")}
    frame.update(image_id=secrets.token_urlsafe(18), width=width, height=height,
                 viewport={"width": vw, "height": vh})
    return png, frame


class Session:
    def __init__(self, engine: Any, ring: Any = None):
        self.engine = engine
        self.human_id = secrets.token_urlsafe(32)
        self.frame: dict[str, Any] | None = None
        self.recent_frame_ids: list[str] = []
        self.ring = ring
        self.frame_scopes: dict[str, tuple] = {}
        self.viewport_epoch = 0
        self.icon_assets = IconAssets(ring.path.parent) if ring is not None else None
        self.pending_navigation: dict[str, Any] | None = None
        self.stopping_navigation = False

    def close(self) -> None:
        if self.icon_assets is not None:
            self.icon_assets.close()
            self.icon_assets = None

    def _authorization_scope(self, frame: dict[str, Any]) -> tuple:
        """Live page/document/viewport/focus/owner identity input must still match."""
        state = self.engine.control.snapshot()
        page = self.engine.page()
        # Native input verifies actual pixels atomically; notifications only schedule capture.
        visual_epoch = getattr(page, "native_visual_epoch", 0) if self.ring is None else None
        return (frame["page_id"], frame["frame_id"], frame["observation_id"],
                frame["viewport"]["width"], frame["viewport"]["height"],
                state["owner_revision"], getattr(page, "observation_id", None), self.viewport_epoch,
                visual_epoch)

    def _scope(self, frame: dict[str, Any]) -> tuple:
        # The render fingerprint identifies the captured pixels; it is never an
        # authorization scope, and native pointer dispatch rebinds it to the live render.
        return (*self._authorization_scope(frame), frame.get("render_fingerprint"))

    def _raw_destination(self) -> tuple[str, int] | None:
        path, slot = getattr(self.ring, "path", None), getattr(self.ring, "back", None)
        return (str(path), slot) if path is not None and type(slot) is int else None

    async def _rebind_pointer(self, frame: dict[str, Any], scope: tuple | None,
                              expected: dict[str, Any]) -> str:
        """Bind pointer dispatch to the current live render after revalidating scope.

        An animation tick changes pixels only. Every await before dispatch is
        followed by authorization revalidation, and dispatch itself re-checks the
        render it will actually hit, so this refresh is not claimed to be race-free.
        """
        destination = self._raw_destination()
        if destination is None:
            raise ServiceError("stale_reference", "Native pointer rebinding requires the frame ring.")
        result = await self.engine.page().capture_raw_frame(*destination)
        fingerprint = result.get("render_fingerprint")
        if not isinstance(fingerprint, str) or _RENDER_FINGERPRINT.fullmatch(fingerprint) is None:
            raise ServiceError("verification_failed", "Native frame omitted its render fingerprint.")
        if scope is not None and scope[:-1] != self._authorization_scope(frame):
            raise ServiceError("stale_reference", "Document, viewport or ownership epoch changed.")
        if (result.get("focused_frame_id") != frame.get("focused_frame_id")
                or result.get("focused_element_id") != frame.get("focused_element_id")):
            raise ServiceError("stale_reference", "Keyboard focus differs from the displayed native document.")
        await self.engine.refs.validate(self.engine.refs.visual, self.engine.page(), check_mutations=False)
        self.engine.control.check_write("human", self.human_id, **expected)
        return fingerprint

    def snapshot(self) -> dict[str, Any]:
        result = self.engine.control.snapshot()
        result["human_control"] = result["owner"] == "human" and result["owner_id"] == self.human_id
        if result["owner"] == "human":
            result["owner_id"] = None
        return result

    async def _pages_metadata(self) -> dict[str, Any]:
        # The caller holds the observation/action boundary through metadata and capture.
        await self.engine.refresh_pages()
        pages = []
        icons = set()
        for page in self.engine.context.pages[:MAX_PAGES]:
            icon = (self.icon_assets.publish(await page.site_icon())
                    if self.icon_assets is not None else None)
            if icon is not None:
                icons.add(icon["sha256"])
            pages.append({"page_id": self.engine.operations.page_id(page),
                          "title": str(await page.title() or "")[:256], "url": _public_url(page.url),
                          "icon": icon})
        if self.icon_assets is not None:
            self.icon_assets.retain(icons)
        active = self.engine.page()
        self.engine.control.set_page(self.engine.operations.page_id(active),
                                     self.engine.refs.frame_id(getattr(active, "main_frame", active)))
        cfg = getattr(getattr(self.engine, "native", None), "config", None)
        network = cfg.get("network") if isinstance(cfg, dict) else None
        proxy_mode = ("PROXY" if network.get("proxy_ref") else "DIRECT") if isinstance(network, dict) else None
        return {"profile": self.engine.name, "runtime": "mioa", "pages": pages,
                "page_id": self.engine.operations.page_id(active), "trust": {"proxy_mode": proxy_mode, "region": None}}

    async def state(self) -> dict[str, Any]:
        async with self.engine.control.observation():
            return {**await self._pages_metadata(), "state": self.snapshot()}

    async def axtree(self, params: dict[str, Any]) -> dict[str, Any]:
        """Bounded semantic observation of the active page.

        This is the existing ``tools.axtree`` operation dispatched to the core
        ``ScriptOperations`` table; the engine supplies the capture/RefStore
        callback. It runs under the same observation lease as ``state``/``frame``,
        refuses to interleave with an agent run, and republishes the actual
        page/frame/observation identity so a superseded observation cannot be
        reused.
        """
        if set(params) - {"max_elements", "max_text"}:
            raise ServiceError("invalid_config", "Unknown axtree fields.")
        from mioa_core.operations import MAX_ELEMENTS, MAX_TEXT, ScriptError
        from mioa_core.refs import ActionError
        async with self.engine.control.observation():
            # begin_run is synchronous and refuses while this observation lease is
            # held, so a run cannot start between this check and the capture.
            if self.engine.control.run_id is not None:
                raise ServiceError("busy", "An agent run owns this session; observe after it finishes.")
            page = self.engine.page()
            try:
                return await self.engine.operations.execute("tools.axtree", {
                    "page_id": self.engine.operations.page_id(page),
                    "max_elements": params.get("max_elements", MAX_ELEMENTS),
                    "max_text": params.get("max_text", MAX_TEXT)})
            except (ScriptError, ActionError) as exc:
                raise ServiceError(exc.code, str(exc)) from exc

    async def navigation(self, page: Any) -> dict[str, Any]:
        unavailable = {"state": "unavailable", "can_go_back": None, "can_go_forward": None,
                       "loading": None}
        if not hasattr(page, "_send"):
            return unavailable
        epoch = (getattr(page, "observation_id", None), getattr(page, "native_navigation_revision", 0))
        cached = getattr(page, "_shell_navigation_cache", None)
        if cached is not None and cached[0] == epoch:
            return dict(cached[1])
        try:
            history = await page._send("Page.getNavigationHistory")
            loading = getattr(page, "native_loading", None)
            if loading is None:
                ready = await page.evaluate("document.readyState")
                loading = ready != "complete" if isinstance(ready, str) and ready in {"loading", "interactive", "complete"} else None
            entries, index = history.get("entries"), history.get("currentIndex")
            if not isinstance(entries, list) or type(index) is not int or not 0 <= index < len(entries):
                raise ServiceError("protocol_error", "The runtime returned malformed navigation history.")
            value = {"state": "known" if type(loading) is bool else "unavailable",
                     "can_go_back": index > 0, "can_go_forward": index + 1 < len(entries), "loading": loading}
        except Exception as exc:
            if getattr(exc, "code", None) != "capability_unavailable":
                raise
            value = unavailable
        page._shell_navigation_cache = (epoch, value)
        return dict(value)

    async def stop(self, params: dict[str, Any]) -> dict[str, Any]:
        if (set(params) != {"page_id", "owner_revision"}
                or not isinstance(params["page_id"], str) or not 0 < len(params["page_id"]) <= 256
                or type(params["owner_revision"]) is not int or params["owner_revision"] < 0):
            raise ServiceError("invalid_config", "Stop requires page_id and owner_revision.")
        self.engine.control.check_write("human", self.human_id, **params)
        page = self.engine.page()
        if self.engine.operations.page_id(page) != params["page_id"]:
            raise ServiceError("stale_reference", "The active page changed before stop.")
        pending = self.pending_navigation
        if self.stopping_navigation:
            raise ServiceError("busy", "A stop is already pending.")
        if pending is not None and (pending["page"] is not page or pending["revision"] != params["owner_revision"]):
            raise ServiceError("stale_reference", "Stop does not belong to the pending navigation.")
        active = getattr(self.engine.control, "_active_task", None)
        if active is not None and pending is None:
            raise ServiceError("busy", "Only navigation can be interrupted.")
        self.stopping_navigation = True
        if pending is not None:
            pending["stop_requested"] = True
        try:
            # Deliberately outside control.action: that boundary is held by the
            # navigation being stopped. No await separates ownership/page checks
            # from dispatch on this event loop.
            await page._send("Page.stopLoading")
        except BaseException:
            if pending is not None and not pending["stop"].done():
                pending["stop"].set_result(False)
            raise
        else:
            # An uncommitted abort can leave the previous document in place
            # without another root loading event. Re-read its real readyState,
            # rather than claiming the command acknowledgment means idle.
            page.native_loading = None
            page.native_navigation_revision = getattr(page, "native_navigation_revision", 0) + 1
            if pending is not None and not pending["stop"].done():
                pending["stop"].set_result(True)
            return {"status": "stopped", "operation": "stop", "state": self.snapshot()}
        finally:
            self.stopping_navigation = False

    async def _interruptible_navigation(self, operation: str, args: dict[str, Any]) -> bool:
        pending = self.pending_navigation
        stop = pending["stop"]
        if pending["stop_requested"] and await stop:
            return True  # stopped while preparing, before Page.navigate dispatch
        task = asyncio.create_task(self._execute(operation, args))
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                try:
                    await task
                except Exception as exc:
                    # Only the runtime's explicit abort is intentional; unrelated
                    # network/protocol failures are never hidden by a racing stop.
                    cause = exc
                    while cause.__cause__ is not None:
                        cause = cause.__cause__
                    aborted = (getattr(cause, "code", None) == "network_unavailable"
                               and getattr(cause, "message", str(cause)) == "net::ERR_ABORTED")
                    if not pending["stop_requested"] or not aborted or not await stop:
                        raise
                    return True
                return stop.done() and stop.result()
            if not stop.result():
                await task
                return False
            if task.done():
                await task  # preserve a real error that raced the successful stop
            else:
                task.cancel()  # cancel only the load waiter, never the control lease
                await asyncio.gather(task, return_exceptions=True)
            return True
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # action() owns the pending identity through final observation cleanup.

    async def capture(self) -> dict[str, Any]:
        async with self.engine.control.observation():
            metadata = await self._pages_metadata() if self.ring is not None else {}
            if self.ring is None:
                result = await self.engine._viewer_execute("viewer.frame", {})
                png, frame = make_frame(result)
                payload = {"png": base64.b64encode(png).decode("ascii")}
            else:
                result = await self.engine.refs.viewport(self.engine.page(),
                    raw_destination=(str(self.ring.path), self.ring.back))
                frame = {key: result[key] for key in ("page_id", "frame_id", "observation_id", "viewport", "width", "height")}
                fingerprint = result.get("render_fingerprint")
                if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
                    raise ServiceError("verification_failed", "Native frame omitted its render fingerprint.")
                frame["render_fingerprint"] = fingerprint
                frame["focused_frame_id"] = result.get("focused_frame_id")
                frame["focused_element_id"] = result.get("focused_element_id")
                frame["image_id"] = secrets.token_urlsafe(18)
                caret = result.get("caret_rect")
                if (not isinstance(caret, list) or len(caret) != 4
                        or any(not _number(value, -1e7, 1e7) for value in caret)):
                    raise ServiceError("verification_failed", "Native focus geometry is invalid.")
                try:
                    seq = self.ring.publish(result)
                except ValueError as exc:
                    raise ServiceError("verification_failed", "Native raw frame exceeds ring bounds.") from exc
                frame.update(source_device_scale_factor=self.engine.native.config["resolved"]["device_scale_factor"],
                             frame_seq=seq, stride=result["stride"], dpr_milli=result["dpr_milli"],
                             theme_edge_rgba=result["theme_edge_rgba"], caret_rect=caret,
                             caret_kind=result["caret_kind"],
                             document_epoch=self.engine.page().observation_id, viewport_epoch=self.viewport_epoch)
                payload = {"shm": {"path": str(self.ring.path), "version": 2,
                                   "capacity_bytes": 3840 * 2160 * 4}}
            frame["navigation"] = await self.navigation(self.engine.page())
            self.engine.control.set_observation(frame["page_id"], frame["frame_id"], frame["observation_id"])
            scope = self._scope(frame)
            if self.frame is not None and self.frame_scopes.get(self.frame["image_id"]) != scope:
                self.recent_frame_ids.clear()
                self.frame_scopes.clear()
            self.frame = frame
            self.recent_frame_ids.append(frame["image_id"])
            self.frame_scopes[frame["image_id"]] = scope
            for expired in self.recent_frame_ids[:-32]:
                self.frame_scopes.pop(expired, None)
            del self.recent_frame_ids[:-32]
            return {**metadata, **frame, **payload, "owner_revision": scope[5], "state": self.snapshot()}

    async def resize(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) != {"width", "height", "device_scale_factor", "page_id", "owner_revision"}:
            raise ServiceError("invalid_config", "Resize requires dimensions, page_id and owner_revision.")
        if (not isinstance(params["page_id"], str) or not 0 < len(params["page_id"]) <= 256
                or type(params["owner_revision"]) is not int or params["owner_revision"] < 0):
            raise ServiceError("invalid_config", "Resize requires a valid page and ownership revision.")
        width, height, dpr = (params[key] for key in ("width", "height", "device_scale_factor"))
        if (type(width) is not int or type(height) is not int or not 1 <= width <= 16384 or not 1 <= height <= 16384
                or not _number(dpr, 0.25, 8) or math.ceil(width * dpr) * math.ceil(height * dpr) * 4 > 3840 * 2160 * 4):
            raise ServiceError("invalid_config", "Resize exceeds the native frame capacity.")
        expected = {key: params[key] for key in ("page_id", "owner_revision")}
        async with self.engine.control.action("human", self.human_id, **expected):
            await self.engine.refresh_pages()
            page = self.engine.page()
            self.engine.control.check_write("human", self.human_id, **expected)
            if self.engine.operations.page_id(page) != params["page_id"]:
                raise ServiceError("stale_reference", "The active page changed before resize.")
            self.frame = None
            self.recent_frame_ids.clear()
            self.frame_scopes.clear()
            self.viewport_epoch += 1
            self.engine.control.invalidate_observation()
            await self.engine.refs.clear()
            await page._send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": dpr, "mobile": False})
        return {"state": self.snapshot(), "viewport": {"width": width, "height": height}, "device_scale_factor": dpr}
    async def control(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) - {"operation", "owner_revision", "run_id"}:
            raise ServiceError("invalid_config", "Unknown control fields.")
        operation = params.get("operation")
        if not isinstance(operation, str) or operation not in {"request_human", "resume_agent", "reclaim"}:
            raise ServiceError("invalid_config", "Unknown ownership transition.")
        state = self.engine.control.snapshot()
        revision = params.get("owner_revision")
        if operation != "request_human" or revision is not None:
            if type(revision) is not int or revision != state["owner_revision"]:
                raise ServiceError("stale_reference", "Ownership changed; refresh before handing control back.")
        run_id = params.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not 0 < len(run_id) <= 256):
            raise ServiceError("invalid_config", "run_id must identify the active agent run.")
        if state["owner"] == "human" and state["owner_id"] != self.human_id:
            raise ServiceError("busy", "A different viewer holds human ownership.")
        self.frame = None
        self.recent_frame_ids.clear()
        self.frame_scopes.clear()
        if operation == "request_human":
            await self.engine.control.request_human(self.human_id)
        else:
            transition = self.engine.control.resume_agent if operation == "resume_agent" else self.engine.control.reclaim
            await transition(run_id)
        return {"state": self.snapshot()}

    def validate_action(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if set(params) - {"operation", "args", "image_id", "frame_seq", "page_id", "frame_id", "observation_id", "owner_revision",
                          "document_epoch", "viewport_epoch", "focused_frame_id", "focused_element_id"}:
            raise ServiceError("invalid_config", "Unknown action fields.")
        operation, supplied = params.get("operation"), params.get("args")
        if not isinstance(operation, str) or not isinstance(supplied, dict):
            raise ServiceError("invalid_config", "Action operation must be a string and args must be an object.")
        shell = operation in SHELL_OPERATIONS
        frame = self.frame
        if shell:
            revision = params.get("owner_revision")
            if type(revision) is not int:
                raise ServiceError("invalid_config", "Invalid ownership revision.")
            expected = {"owner_revision": revision}
            self.engine.control.check_write("human", self.human_id, **expected)
        else:
            if frame is None or (params.get("image_id") != frame["image_id"] and params.get("image_id") not in self.recent_frame_ids):
                raise ServiceError("stale_reference", "Capture a new frame before sending input.")
            pointer = self.ring is not None and operation in POINTER_OPERATIONS
            scope = self.frame_scopes.get(params.get("image_id"))
            if scope is not None:
                # Animation changes only captured render identity; pointer input is
                # authorized by the live scope and rebound at dispatch, not by pixels.
                recorded = scope[:-1] if pointer else scope
                live = self._authorization_scope(frame) if pointer else self._scope(frame)
                if recorded != live or (pointer and scope[-1] != frame.get("render_fingerprint")):
                    raise ServiceError("stale_reference", "Document, viewport or ownership epoch changed.")
            if "frame_seq" in params:
                seq = params["frame_seq"]
                if type(seq) is not int or "frame_seq" not in frame or not 0 <= frame["frame_seq"] - seq <= 31:
                    raise ServiceError("stale_reference", "Frame generation is outside the active 32-frame window.")
            for epoch in ("document_epoch", "viewport_epoch"):
                if epoch in params and (type(params[epoch]) is not int or params[epoch] != frame.get(epoch)):
                    raise ServiceError("stale_reference", "Input belongs to a different document or viewport epoch.")
            for identity in ("focused_frame_id", "focused_element_id"):
                if identity in params and params[identity] != frame.get(identity):
                    raise ServiceError("stale_reference", "Keyboard focus differs from the displayed native document.")
            expected = {key: params.get(key) for key in ("page_id", "frame_id", "observation_id", "owner_revision")}
            if any(expected[key] != frame[key] for key in ("page_id", "frame_id", "observation_id")) or type(expected["owner_revision"]) is not int:
                raise ServiceError("stale_reference", "Action does not match the observed page and ownership revision.")
            self.engine.control.check_write("human", self.human_id, **expected)
        args: dict[str, Any] = {}
        if operation in {"navigate", "new_tab"}:
            args["url"] = validate_url(supplied.get("url", "about:blank") if operation == "new_tab" else supplied.get("url"))
        elif operation in {"select_tab", "close_tab"}:
            target = supplied.get("target_page_id")
            if not isinstance(target, str) or not 0 < len(target) <= 256:
                raise ServiceError("invalid_config", "A real target_page_id is required.")
            args["target_page_id"] = target
        elif operation in {"mouse.click", "mouse.move", "mouse.wheel"}:
            for key, maximum in (("x", frame["viewport"]["width"]), ("y", frame["viewport"]["height"])):
                value = supplied.get(key)
                if not _number(value, 0, maximum) or value >= maximum:
                    raise ServiceError("invalid_config", "Mouse coordinates are outside the observed CSS viewport.")
                args[key] = value
            if operation == "mouse.click":
                button, count = supplied.get("button", "left"), supplied.get("click_count", 1)
                if not isinstance(button, str) or button not in {"left", "middle", "right"} or type(count) is not int or count not in {1, 2, 3}:
                    raise ServiceError("invalid_config", "Invalid mouse button or click count.")
                args.update(button=button, click_count=count)
            elif operation == "mouse.wheel":
                for key in ("delta_x", "delta_y"):
                    if not _number(supplied.get(key), -10000, 10000):
                        raise ServiceError("invalid_config", "Wheel deltas exceed service limits.")
                    args[key] = supplied[key]
        elif operation == "keyboard.press":
            key = supplied.get("key")
            if not isinstance(key, str) or not 0 < len(key) <= 100 or any(ord(char) < 32 for char in key):
                raise ServiceError("invalid_config", "Invalid key name.")
            args["key"] = key
        elif operation == "keyboard.insert_text":
            text = supplied.get("text")
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 8192 or "\0" in text:
                raise ServiceError("invalid_config", "Input text is empty, too large or contains NUL.")
            args["text"] = text
        elif operation not in {"back", "forward", "reload", "page.focus"}:
            raise ServiceError("invalid_config", "Unsupported human action.")
        if set(supplied) - set(args):
            raise ServiceError("invalid_config", "Unknown action arguments.")
        return expected, args

    async def _select(self, page: Any) -> None:
        if page is not self.engine.active:
            self.engine.human_selection_epoch += 1
        await self.engine._select(page)

    async def _history(self, page: Any, delta: int) -> None:
        # NativePage currently exposes go_back but no go_forward. Use its own
        # session transport, commit wait and document epoch, not injected JS.
        history = await page._send("Page.getNavigationHistory")
        entries, index = history.get("entries"), history.get("currentIndex")
        if not isinstance(entries, list) or type(index) is not int or not 0 <= index < len(entries):
            raise ServiceError("protocol_error", "The runtime returned malformed navigation history.")
        target = index + delta
        if not 0 <= target < len(entries):
            return
        entry = entries[target]
        if not isinstance(entry, dict) or type(entry.get("id")) is not int:
            raise ServiceError("protocol_error", "The runtime returned an invalid history entry.")
        validate_url(entry.get("url"))
        if delta < 0:
            await page.go_back(wait_until="domcontentloaded", timeout=OPERATION_TIMEOUT * 1000)
            return
        deadline = time.monotonic() + OPERATION_TIMEOUT
        previous_epoch = page.observation_id
        page._document_response = None
        await page._send("Page.navigateToHistoryEntry", {"entryId": entry["id"]}, timeout=max(0.001, deadline - time.monotonic()))
        await page._wait_for_commit(previous_epoch, deadline)
        await page.wait_for_load_state("domcontentloaded", timeout=max(1, (deadline - time.monotonic()) * 1000))
        page._url = await page.evaluate("location.href")
        page.observation_id += 1

    async def _execute(self, operation: str, args: dict[str, Any]) -> None:
        page = self.engine.page()
        if operation == "navigate":
            await self.engine._navigation(page, lambda: page.goto(args["url"], wait_until="domcontentloaded", timeout=OPERATION_TIMEOUT * 1000))
        elif operation in {"back", "forward"}:
            await self.engine._navigation(page, lambda: self._history(page, -1 if operation == "back" else 1))
        elif operation == "reload":
            await self.engine._navigation(page, lambda: page.reload(wait_until="domcontentloaded", timeout=OPERATION_TIMEOUT * 1000))
        elif operation == "new_tab":
            if len(self.engine.context.pages) >= MAX_PAGES:
                raise ServiceError("busy", "The session has reached the 100-tab limit.")
            created = await self.engine.context.new_page()
            await self._select(created)
            if args["url"] != "about:blank":
                await self.engine._navigation(created, lambda: created.goto(args["url"], wait_until="domcontentloaded", timeout=OPERATION_TIMEOUT * 1000))
        elif operation == "select_tab":
            await self._select(self.engine.operations.page(args["target_page_id"]))
        elif operation == "close_tab":
            target = self.engine.operations.page(args["target_page_id"])
            pages = self.engine.context.pages
            if target is page:
                others = [item for item in pages if item is not target]
                replacement = others[min(pages.index(target), len(others) - 1)] if others else await self.engine.context.new_page()
                await self._select(replacement)
            await target.close()
        elif operation == "page.focus":
            await page.bring_to_front()
        else:
            family, method = operation.split(".", 1)
            await getattr(getattr(page, family), method)(**args)

    async def action(self, params: dict[str, Any]) -> dict[str, Any]:
        expected, args = self.validate_action(params)
        async with self.engine.control.action("human", self.human_id, **expected):
            self.validate_action(params)
            navigation = params["operation"] in {"navigate", "reload", "back", "forward"}
            if navigation:
                self.pending_navigation = {"page": self.engine.page(), "revision": expected["owner_revision"],
                                           "stop": asyncio.get_running_loop().create_future(), "stop_requested": False}
            try:
                await self.engine.refresh_pages()
                self.validate_action(params)  # Refresh may replace the active document/page.
                # Native SHM input has the stronger atomic pixel+semantic guard below.
                # Keep document/view/control checks, not asynchronous mutation delivery.
                if params["operation"] not in SHELL_OPERATIONS:
                    await self.engine.refs.validate(self.engine.refs.visual, self.engine.page(),
                                                    check_mutations=self.ring is None)
                guarded = self.ring is not None and params["operation"].startswith(("mouse.", "keyboard."))
                fingerprint = self.frame.get("render_fingerprint") if guarded else None
                if guarded and (not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None):
                    raise ServiceError("stale_reference", "Capture a native frame before sending input.")
                frame = self.frame
                scope = self.frame_scopes.get(frame["image_id"]) if frame is not None else None
                self.frame = None  # Accepted input is never replayable, even if rebind or dispatch fails.
                self.recent_frame_ids.clear()
                self.frame_scopes.clear()
                rebind = None
                if guarded and params["operation"] in POINTER_OPERATIONS and self._raw_destination() is not None and frame is not None:
                    async def rebind():
                        return await self._rebind_pointer(frame, scope, expected)
                    fingerprint = await rebind()
                stopped = False
                try:
                    if guarded:
                        await self.engine.page().guarded_input(params["operation"], args, fingerprint, rebind=rebind)
                    elif navigation:
                        stopped = await self._interruptible_navigation(params["operation"], args)
                    else:
                        await self._execute(params["operation"], args)
                finally:
                    # Do not cancel the action task for an intentional stop:
                    # ControlState correctly pauses unexpected cancellation.
                    self.engine.control.invalidate_observation()
                    await self.engine.refs.clear()
            finally:
                if navigation:
                    self.pending_navigation = None
        from mioa_core import profile
        state = profile.load_state(self.engine.name) or {}
        state["url"] = self.engine.page().url
        profile.save_state(self.engine.name, state)
        return {"status": "stopped" if stopped else "ok", "operation": params["operation"], "state": self.snapshot()}


class _OutputProtocol(asyncio.streams.FlowControlMixin, asyncio.Protocol):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__(loop=loop)
        self.closed = loop.create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        if not self.closed.done():
            self.closed.set_result(None)

    def _get_close_waiter(self, stream: Any) -> asyncio.Future[None]:
        return self.closed


class Protocol:
    def __init__(self, engine: Any, home: Path):
        self.engine, self.home = engine, home
        self.session = Session(engine)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(MAX_QUEUED_REQUESTS)
        self.output_lock = asyncio.Lock()
        self.writer: asyncio.StreamWriter | None = None
        self.input_transport: asyncio.ReadTransport | None = None
        self.shutdown_id: int | None = None
        self.fatal: BaseException | None = None
        self.ready = False

    async def emit(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self.output_lock:
            self.writer.write(encoded)
            async with asyncio.timeout(5):
                await self.writer.drain()

    async def read_requests(self, reader: asyncio.StreamReader) -> None:
        try:
            while not self.engine.stopping.is_set():
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        self.fatal = ServiceError("invalid_config", "The final request was not newline-terminated.")
                    break
                except asyncio.LimitOverrunError:
                    self.fatal = ServiceError("invalid_config", "Request line exceeds the 32768-byte service limit.")
                    break
                try:
                    request = decode_request(line)
                except ServiceError as exc:
                    request = {"id": None, "error": safe_error(exc)}
                if request.get("method") == "shutdown":
                    if request["params"]:
                        request = {"id": request["id"], "error": safe_error(ServiceError("invalid_config", "Shutdown takes no parameters."))}
                    else:
                        self.shutdown_id = request["id"]
                        break
                if self.queue.full():
                    if not self.ready:
                        self.fatal = ServiceError("busy", "Too many requests were queued during startup.")
                        break
                    await self.emit({"id": request["id"], "error": {"code": "busy", "message": "At most 16 requests may be queued; wait for a response."}})
                else:
                    self.queue.put_nowait(request)
        except (ConnectionError, OSError, TimeoutError):
            pass
        finally:
            self.engine.stopping.set()

    async def process_requests(self) -> None:
        while True:
            request = await self.queue.get()
            response = {"id": request["id"]}
            if "error" in request:
                response["error"] = request["error"]
            else:
                method, params = request["method"], request["params"]
                try:
                    async with asyncio.timeout(OPERATION_TIMEOUT):
                        if method in {"state", "frame"}:
                            if params:
                                raise ServiceError("invalid_config", "State and frame take no parameters.")
                            result = await (self.session.state() if method == "state" else self.session.capture())
                        elif method == "control":
                            result = await self.session.control(params)
                        elif method == "axtree":
                            result = await self.session.axtree(params)
                        else:
                            result = await self.session.action(params)
                    response["result"] = result
                except Exception as exc:
                    response["error"] = safe_error(exc)
            try:
                await self.emit(response)
            except (ConnectionError, OSError, TimeoutError):
                self.engine.stopping.set()
                return

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_REQUEST_BYTES)
        input_pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        output_pipe = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        self.input_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), input_pipe)
        output_transport, output_protocol = await loop.connect_write_pipe(lambda: _OutputProtocol(loop), output_pipe)
        self.writer = asyncio.StreamWriter(output_transport, output_protocol, None, loop)
        signals = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.engine.stopping.set)
            signals.append(sig)
        producer = asyncio.create_task(self.read_requests(reader))
        startup = asyncio.create_task(self.engine.start())
        stopping = asyncio.create_task(self.engine.stopping.wait())
        consumer: asyncio.Task[Any] | None = None
        failure: BaseException | None = None
        try:
            await asyncio.wait({startup, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if stopping.done() and not startup.done():
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
                failure = self.fatal or ServiceError("startup_cancelled", "Startup ended because the owner disconnected or requested shutdown.")
            else:
                await startup
                await self.emit({"event": "ready", "protocol": 1, "profile": self.engine.name,
                                 "runtime": "mioa", "home": str(self.home), "pid": os.getpid(),
                                 "native_pid": self.engine.native.pid})
                self.ready = True
                consumer = asyncio.create_task(self.process_requests())
                await stopping
                failure = self.fatal
        except Exception as exc:
            failure = exc
        finally:
            for task in (producer, startup, stopping, consumer):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (producer, startup, stopping, consumer) if task is not None), return_exceptions=True)
            self.engine.control.pause("desktop_disconnected")
            try:
                await self.engine.shutdown()
            except Exception as exc:
                failure = failure or exc
            finally:
                # Even a reference/socket cleanup failure must not orphan the
                # one owned native process. NativeSession.close is idempotent.
                if self.engine.native is not None:
                    try:
                        await self.engine.native.close()
                    except Exception as exc:
                        failure = failure or exc
                for sig in signals:
                    loop.remove_signal_handler(sig)
                self.input_transport.close()
        try:
            if failure is not None and not self.ready:
                await self.emit({"event": "error", "error": safe_error(failure, startup=True)})
            if self.shutdown_id is not None:
                await self.emit({"id": self.shutdown_id, **({"error": safe_error(failure)} if failure else {"result": {"status": "ok", "flushed": True}})})
            elif failure is not None and self.ready:
                await self.emit({"event": "error", "error": safe_error(failure)})
        except (ConnectionError, OSError, TimeoutError):
            pass
        self.writer.close()
        try:
            async with asyncio.timeout(2):
                await self.writer.wait_closed()
        except TimeoutError:
            output_transport.abort()
        except (ConnectionError, OSError):
            pass
        return 1 if failure else 0


def _prepare_profile(args: Any, profile: Any, native_spec: Any) -> None:
    name = args.profile
    location = profile.profile_dir(name)
    config_path = location / "config.json"
    if config_path.exists() or config_path.is_symlink():
        cfg = profile.load_config(name)
        if cfg is None or not isinstance(cfg.get("runtime"), dict) or cfg["runtime"].get("id") not in {"mioa", "lightpanda"}:
            raise ServiceError("incompatible_profile", "An existing profile must contain a valid managed Mioa configuration.")
        native_spec.validate_native_spec(cfg, name=name)
        if Path(cfg["storage"]["path"]).resolve() != location.resolve() / "data":
            raise ServiceError("incompatible_profile", "The saved profile does not own this isolated storage directory.")
        if args.binary is not None:
            requested = native_spec.runtime_spec(cfg["runtime"].get("id") or "mioa", args.binary)
            if requested != cfg["runtime"]:
                raise ServiceError("incompatible_profile", "Requested binary differs from the pinned profile; explicit migration is required.")
        if args.font_file is not None:
            requested_fonts = sorted({str(Path(path).expanduser().resolve()) for path in args.font_file})
            if requested_fonts != sorted(cfg["asset_paths"]["fonts"]):
                raise ServiceError("incompatible_profile", "Requested fonts differ from the pinned profile; explicit migration is required.")
        if args.allow_private_network is not None and args.allow_private_network != cfg["network"].get("allow_private_network", False) or (
            getattr(args, "native_socket", None) is not None and (Path(cfg["storage"]["path"]) / "environment.sqlite").is_file()
        ):
            if getattr(args, "native_socket", None) is not None:
                desired_private = bool(args.allow_private_network) if args.allow_private_network is not None else cfg["network"].get("allow_private_network", False)
                env_db = Path(cfg["storage"]["path"]) / "environment.sqlite"
                db_row = None
                if env_db.is_file():
                    import sqlite3
                    with sqlite3.connect(env_db) as conn:
                        db_row = conn.execute("select revision, semantic_sha256, raw_sha256 from environment where singleton=1").fetchone()
                need_migrate = desired_private != cfg["network"].get("allow_private_network", False)
                if db_row is not None and (cfg.get("revision") != db_row[0] or native_spec.environment_digest(cfg) != db_row[2]):
                    need_migrate = True
                if need_migrate:
                    cfg = native_spec.migrate_native_spec(cfg, network={**cfg["network"], "allow_private_network": desired_private})
                    if db_row is not None:
                        cfg["revision"] = int(db_row[0]) + 1
                        cfg["previous_environment_sha256"] = str(db_row[2])
                    profile.save_config(name, cfg)
            else:
                raise ServiceError("incompatible_profile", "Requested network policy differs from the saved profile; explicit migration is required.")
        return
    if location.exists() and any(path.name not in {".lock", ".spawn"} for path in location.iterdir()):
        raise ServiceError("incompatible_profile", "A nonempty profile without a valid configuration will not be overwritten.")
    with profile.exclusive("catalog", kind="catalog") as catalog:
        if not catalog:
            raise ServiceError("busy", "The profile catalog is in use.")
        assets = ({"fonts": sorted({str(Path(path).expanduser().resolve()) for path in args.font_file})}
                  if args.font_file else native_spec.native_font_assets())
        families = native_spec.native_font_families(assets)
        if not families:
            raise ServiceError("capability_unavailable", "No real font families are available in the pinned font files.")
        existing = [profile.require(row["name"]) for row in profile.list_profiles() if row["name"] != name]
        cfg = native_spec.create_native_spec(name, runtime="mioa", binary=args.binary,
                                             storage_path=location / "data", asset_paths=assets,
                                             overrides={"fonts": sorted(families)}, existing=existing,
                                             network={"allow_private_network": bool(args.allow_private_network)})
        cfg["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        profile.save_config(name, cfg)


async def run_service(args: Any) -> int:
    if sys.platform != "darwin":
        raise ServiceError("unsupported_platform", "The current Mioa native artifact requires macOS CGL; Linux/Windows execution is unsupported.")
    home = Path(args.home).expanduser().resolve() if args.home else default_home().resolve()
    os.environ["ANTI_TRACE_HOME"] = str(home)
    try:
        from mioa_core import native_spec, profile
        from mioa_core.engine import Engine
    except ImportError as exc:
        raise ServiceError("capability_unavailable", "Install the project's Python 3.12 dependencies before starting the service.") from exc
    profile.BASE = home
    if not isinstance(args.profile, str) or not re.fullmatch(r"[\w-]{1,80}", args.profile) or profile.sanitize(args.profile) != args.profile:
        raise ServiceError("invalid_config", "Profile names must be 1–80 letters, digits, underscores or hyphens without edge hyphens/underscores.")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep both lifecycle and engine leases through native durable shutdown.
    # The native child inherits the engine lease, so owner death never permits
    # a second engine to write while its original process is still alive.
    with profile.exclusive(args.profile, kind="spawn") as spawned, profile.exclusive(args.profile) as owned:
        if not spawned or not owned or profile.owner_alive(profile.load_state(args.profile)):
            raise ServiceError("busy", "Another process owns this persistent profile.")
        _prepare_profile(args, profile, native_spec)
        profile.save_state(args.profile, {"pid": os.getpid(), "process_start": profile.process_start(os.getpid()),
                                          "status": "STARTING", "runtime": "mioa"})
        # Skip redundant navigation: the fresh native context owns about:blank.
        initial_url = None if args.url == "about:blank" else args.url
        engine = Engine(args.profile, headless=True, initial_url=initial_url, verify=False)
        try:
            if getattr(args, "native_socket", None) is not None:
                from .native_service import NativeProtocol
                return await NativeProtocol(engine, home, Path(args.native_socket), Path(args.frame_shm)).run()
            return await Protocol(engine, home).run()
        finally:
            # Also covers failure while opening stdio transports, before run's
            # engine cleanup region has been entered.
            if engine.native is None and engine.context is None:
                profile.clear_state(args.profile)
