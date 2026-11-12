"""Bounded parent/child NDJSON protocol over one owned mioa_core Engine.

This service owns the headless transport only. The shared Engine, ControlState,
RefStore and native process come from mioa_core; no ATB CLI, desktop UI or HTTP
viewer source is imported. The Host explicitly selects the profile root.
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
import tempfile
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .frame_ring import FrameRing
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
    result = {"code": code, "message": (str(exc) if isinstance(exc, ServiceError) else
              messages.get(code, "The headless service could not start." if startup else "The native runtime operation failed."))}
    if startup:
        stage = getattr(exc, "startup_stage", "engine_start")
        result["stage"] = stage if stage in {
            "profile_handshake", "spawn", "cdp_connect", "receipt", "target_initialize", "engine_start"
        } else "engine_start"
    return result


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
            or request["method"] not in {"state", "frame", "control", "action", "axtree", "shutdown", *({"resize", "page_resize", "page_viewport", "page_demand", "page_broker_frame", "page_bind_displayed", "page_broker_action", "page_stop", "page_action", "new_application", "stop"} if native else set())}):
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
        self.page_viewport_epochs: dict[str, int] = {}
        self.page_frames: dict[str, dict[str, Any]] = {}
        self.broker_frames: dict[str, dict[int, dict[str, Any]]] = {}
        self.displayed_broker_frames: dict[str, dict[str, Any]] = {}
        self.input_channels: dict[int, dict[str, Any]] = {}
        self.input_channel_high_water = [0] * 16
        self.page_resize_unknown: set[str] = set()
        self.page_demand_disabled: set[str] = set()
        self.page_demand_epochs: dict[str, int] = {}
        self.stopping_domains: set[int] = set()
        # The legacy active viewer retains its original ring. Per-page captures
        # own a separate producer/back slot in each execution domain.
        self.active_capture_lock = asyncio.Lock()
        self.domain_rings: dict[int, Any] = {}
        self.domain_ring_directories: dict[int, tempfile.TemporaryDirectory] = {}
        self.owner_epoch = 0
        self.icon_assets = IconAssets(ring.path.parent) if ring is not None else None
        self.pending_navigation: dict[str, Any] | None = None
        self.stopping_navigation = False

    def close(self) -> None:
        if self.icon_assets is not None:
            self.icon_assets.close()
            self.icon_assets = None
        for domain, directory in self.domain_ring_directories.items():
            self.domain_rings[domain].close()
            directory.cleanup()
        self.domain_ring_directories.clear()
        self.domain_rings.clear()

    def _prune_page_state(self) -> None:
        """Collect closed-page state from local inventory, without remote work."""
        live_ids = {self.engine.operations.page_id(page) for page in self.engine.context.pages
                    if not page.is_closed()}
        self.page_demand_disabled.intersection_update(live_ids)
        self.page_resize_unknown.intersection_update(live_ids)
        for cache in (self.page_frames, self.page_viewport_epochs, self.page_demand_epochs,
                      self.broker_frames, self.displayed_broker_frames):
            for stale in set(cache) - live_ids:
                cache.pop(stale, None)

    def execution_domain(self, page_id: str) -> int:
        """Resolve routing from live local inventory, never a remote refresh."""
        if not isinstance(page_id, str) or not 0 < len(page_id) <= 256:
            raise ServiceError("invalid_config", "Invalid page identity.")
        for page in self.engine.context.pages:
            if not page.is_closed() and self.engine.operations.page_id(page) == page_id:
                domain = getattr(getattr(getattr(page, "context", None), "cdp", None), "execution_domain", 0)
                if domain in self.stopping_domains:
                    raise ServiceError("stale_reference", "Execution domain is stopping.")
                return domain
        raise ServiceError("stale_reference", "Page is no longer available.")

    def active_execution_domain(self) -> int:
        page = self.engine.page()
        page_id = self.engine.operations.page_id(page)
        if self.frame is not None and self.frame["page_id"] != page_id:
            self.frame = None
            self.recent_frame_ids.clear()
            self.frame_scopes.clear()
        # Engine.page() is the authoritative active-page resolver for native and
        # legacy providers. Do not require a second inventory/provider lookup.
        domain = getattr(getattr(getattr(page, "context", None), "cdp", None), "execution_domain", 0)
        if domain in self.stopping_domains:
            raise ServiceError("stale_reference", "Execution domain is stopping.")
        return domain

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

    def _capture_ring(self, domain: int) -> Any:
        if self.ring is None:
            return None
        if domain not in self.domain_rings:
            if len(self.domain_rings) >= MAX_PAGES:
                raise ServiceError("busy", "Frame ring domain capacity is exhausted.")
            directory = tempfile.TemporaryDirectory(prefix="lpfr-", dir=self.ring.path.parent)
            root = Path(directory.name)
            try:
                ring = FrameRing(root / "frame")
            except BaseException:
                directory.cleanup()
                raise
            self.domain_rings[domain] = ring
            self.domain_ring_directories[domain] = directory
        return self.domain_rings[domain]

    def _raw_destination(self, domain: int | None = None) -> tuple[str, int] | None:
        ring = self.ring if domain is None else self._capture_ring(domain)
        path, slot = getattr(ring, "path", None), getattr(ring, "back", None)
        return (str(path), slot) if path is not None and type(slot) is int else None

    def _owner_basis(self) -> tuple[int, int]:
        return self.owner_epoch, self.engine.control.snapshot()["owner_revision"]

    def _check_owner_basis(self, basis: tuple[int, int]) -> None:
        if basis != self._owner_basis():
            raise ServiceError("stale_reference", "Ownership changed during observation.")

    async def _rebind_pointer(self, frame: dict[str, Any], scope: tuple | None,
                              expected: dict[str, Any]) -> str:
        """Bind pointer dispatch to the current live render after revalidating scope.

        An animation tick changes pixels only. Every await before dispatch is
        followed by authorization revalidation, and dispatch itself re-checks the
        render it will actually hit, so this refresh is not claimed to be race-free.
        """
        async with self.active_capture_lock:
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

    async def _pages_metadata(self, refresh_page: Any = None) -> dict[str, Any]:
        # Native inventory is event-owned. A frame for B must never execute
        # script or resource RPCs in A just to populate the tab strip.
        await self.engine.refresh_pages()
        pages = []
        icons = set()
        for page in self.engine.context.pages[:MAX_PAGES]:
            if hasattr(page, "cached_title"):
                if page is refresh_page:
                    await page.title()
                    if self.icon_assets is not None:
                        await page.site_icon()
                title, site_icon = page.cached_title, page.cached_site_icon
            else:
                title = await page.title()
                site_icon = await page.site_icon() if self.icon_assets is not None else None
            icon = self.icon_assets.publish(site_icon) if self.icon_assets is not None else None
            if icon is not None:
                icons.add(icon["sha256"])
            pages.append({"page_id": self.engine.operations.page_id(page),
                          "title": str(title or "")[:256], "url": _public_url(page.url),
                          "execution_domain": getattr(getattr(getattr(page, "context", None), "cdp", None), "execution_domain", 0),
                          "execution_epoch": getattr(getattr(getattr(page, "context", None), "cdp", None), "execution_epoch", 0),
                          "icon": icon})
        if self.icon_assets is not None:
            self.icon_assets.retain(icons)
        active = self.engine.page() if pages else None
        if active is not None:
            self.engine.control.set_page(self.engine.operations.page_id(active),
                                         self.engine.refs.frame_id(getattr(active, "main_frame", active)))
        cfg = getattr(getattr(self.engine, "native", None), "config", None)
        network = cfg.get("network") if isinstance(cfg, dict) else None
        proxy_mode = ("PROXY" if network.get("proxy_ref") else "DIRECT") if isinstance(network, dict) else None
        return {"profile": self.engine.name, "runtime": "mioa", "pages": pages,
                "page_id": self.engine.operations.page_id(active) if active is not None else None,
                "trust": {"proxy_mode": proxy_mode, "region": None}}

    async def new_application(self, params: dict[str, Any]) -> dict[str, Any]:
        if (set(params) != {"url", "owner_revision"}
                or type(params["owner_revision"]) is not int or params["owner_revision"] < 0):
            raise ServiceError("invalid_config", "Application creation requires URL and ownership revision.")
        validate_url(params["url"])
        revision = params["owner_revision"]
        self.engine.control.check_write("human", self.human_id, owner_revision=revision)
        context = self.engine.context
        domain = context.application_domain()
        async with self.engine.control.action("human", self.human_id,
                                             owner_revision=revision, execution_domain=domain):
            if len(context.pages) >= MAX_PAGES:
                raise ServiceError("busy", "The session has reached the page capacity.")
            created = await context._new_application(domain)
            # Ownership may have entered its fence while admission was pending.
            # The accepted launch remains tracked, but no later Web effect can
            # dispatch under the superseded ownership generation.
            self.engine.control.check_write("human", self.human_id, owner_revision=revision)
            await self._select(created, authorize=lambda: self.engine.control.check_write(
                "human", self.human_id, owner_revision=revision))
            self.engine.control.check_write("human", self.human_id, owner_revision=revision)
            if params["url"] != "about:blank":
                await self.engine._navigation(created, lambda: created.goto(
                    params["url"], wait_until="domcontentloaded", timeout=OPERATION_TIMEOUT * 1000))
            self.engine.control.invalidate_observation()
            return {"status": "ok", "page_id": self.engine.operations.page_id(created),
                    "execution_domain": created.context.cdp.execution_domain,
                    "execution_epoch": created.context.cdp.execution_epoch, "state": self.snapshot()}

    async def state(self) -> dict[str, Any]:
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
        async with self.engine.control.observation(execution_domain=self.active_execution_domain()):
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
        active = self.engine.control.in_flight()
        if active and pending is None:
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

    async def capture(self, page_id: str | None = None) -> dict[str, Any]:
        if page_id is None and not self.engine.context.pages:
            # Empty workspace is real metadata, not a synthetic page/frame.
            # No pixel slot or publication lease exists at this endpoint.
            self.frame = None
            self.recent_frame_ids.clear()
            self.frame_scopes.clear()
            metadata = await self.state()
            if metadata["pages"]:
                raise ServiceError("stale_reference", "Workspace acquired a page during observation.")
            return {**metadata, "empty_workspace": True,
                    "owner_revision": self.engine.control.owner_revision}
        metadata = {}
        domain = self.execution_domain(page_id) if page_id is not None else self.active_execution_domain()
        owner_basis = self._owner_basis()
        async with self.engine.control.observation(execution_domain=domain), (
                self.active_capture_lock if page_id is None and self.ring is not None
                else contextlib.nullcontext()):
            self._check_owner_basis(owner_basis)
            ring = self.ring if page_id is None else self._capture_ring(domain)
            page = await self.engine.page_by_id(page_id) if page_id is not None else self.engine.page()
            if page_id is None and self.active_execution_domain() != domain:
                raise ServiceError("stale_reference", "Active execution domain changed before capture.")
            if self.ring is not None and page_id is None:
                metadata = await self._pages_metadata(refresh_page=page)
            if self.engine.operations.page_id(page) in self.page_resize_unknown:
                raise ServiceError("stale_reference", "Page viewport outcome is unknown.")
            demand_epoch = self.page_demand_epochs.get(page_id, 0)
            if page_id is not None:
                self._prune_page_state()
                if page_id not in self.page_frames and len(self.page_frames) >= MAX_PAGES:
                    raise ServiceError("busy", "Page observation capacity is exhausted.")
                if page_id in self.page_demand_disabled:
                    raise ServiceError("busy", "Visual demand is disabled for this page.")
            if self.ring is None:
                result = (await self.engine.export_page(page_id) if page_id is not None
                          else await self.engine._viewer_execute("viewer.frame", {}))
                png, frame = make_frame(result)
                payload = {"png": base64.b64encode(png).decode("ascii")}
            else:
                result = await self.engine.refs.viewport(page, independent=page_id is not None,
                    raw_destination=(str(ring.path), ring.back))
                self._check_owner_basis(owner_basis)
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
                    seq = ring.publish(result)
                except ValueError as exc:
                    raise ServiceError("verification_failed", "Native raw frame exceeds ring bounds.") from exc
                frame.update(source_device_scale_factor=(result["dpr_milli"] / 1000 if page_id is not None
                                                         else self.engine.native.config["resolved"]["device_scale_factor"]),
                             frame_seq=seq, stride=result["stride"], dpr_milli=result["dpr_milli"],
                             theme_edge_rgba=result["theme_edge_rgba"], caret_rect=caret,
                             caret_kind=result["caret_kind"],
                             document_epoch=page.observation_id,
                             viewport_epoch=(self.page_viewport_epochs.get(page_id, 0)
                                             if page_id is not None else self.viewport_epoch))
                payload = {"shm": {"path": str(ring.path), "version": 2,
                                   "capacity_bytes": 3840 * 2160 * 4}}
            frame["navigation"] = await self.navigation(page)
            self._check_owner_basis(owner_basis)
            if page_id is not None:
                self.execution_domain(page_id)
            else:
                self.active_execution_domain()
            if page_id is None and self.engine.page() is not page:
                raise ServiceError("stale_reference", "Active page changed during capture.")
            if page_id is not None:
                await self.engine.refs.validate(self.engine.refs.page_visuals.get(page), page,
                                                check_mutations=self.ring is None)
                self._check_owner_basis(owner_basis)
                if self.ring is not None and page.observation_id != frame["document_epoch"]:
                    raise ServiceError("stale_reference", "Page document changed during capture.")
                if (page_id in self.page_demand_disabled
                        or demand_epoch != self.page_demand_epochs.get(page_id, 0)):
                    raise ServiceError("stale_reference", "Page demand changed during capture.")
                # A basis is per-page and never changes the active viewer's lease.
                frame["owner_revision"] = owner_basis[1]
                self.page_frames[page_id] = frame
                return {**frame, **payload}
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


    async def resize_page(self, params: dict[str, Any]) -> dict[str, Any]:
        """Configure one page without selecting it; caller must hold the human lease."""
        if set(params) != {"width", "height", "device_scale_factor", "page_id", "owner_revision"}:
            raise ServiceError("invalid_config", "Page resize requires dimensions, page_id and owner_revision.")
        page_id, revision = params["page_id"], params["owner_revision"]
        width, height, dpr = (params[key] for key in ("width", "height", "device_scale_factor"))
        if (not isinstance(page_id, str) or not 0 < len(page_id) <= 256
                or type(revision) is not int or revision < 0
                or type(width) is not int or type(height) is not int
                or not 1 <= width <= 16384 or not 1 <= height <= 16384
                or not _number(dpr, 0.25, 8)
                or math.ceil(width * dpr) * math.ceil(height * dpr) * 4 > 3840 * 2160 * 4):
            raise ServiceError("invalid_config", "Page resize exceeds supported bounds.")
        async with self.engine.control.action("human", self.human_id, owner_revision=revision,
                                             execution_domain=self.execution_domain(page_id)):
            self._prune_page_state()
            page = await self.engine.page_by_id(page_id)
            self.engine.control.check_write("human", self.human_id, owner_revision=revision)
            if page_id in self.page_resize_unknown:
                raise ServiceError("stale_reference", "An earlier resize has an unknown outcome.")
            self.page_frames.pop(page_id, None)
            self.page_resize_unknown.add(page_id)
            if page is self.engine.active:
                self.frame = None
                self.recent_frame_ids.clear()
                self.frame_scopes.clear()
                self.engine.control.invalidate_observation()
                await self.engine.refs.clear()
            await page._send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": dpr, "mobile": False})
            actual = await page.evaluate("() => ({width:innerWidth,height:innerHeight,device_scale_factor:devicePixelRatio})")
            if (not isinstance(actual, dict) or actual.get("width") != width or actual.get("height") != height
                    or actual.get("device_scale_factor") != dpr):
                raise ServiceError("verification_failed", "Resize outcome could not be confirmed.")
            await self.engine.refs.forget_page(page)
            epoch = self.page_viewport_epochs.get(page_id, 0) + 1
            self.page_viewport_epochs[page_id] = epoch
            if page is self.engine.active:
                self.frame = None
                self.recent_frame_ids.clear()
                self.frame_scopes.clear()
                self.viewport_epoch = epoch
                self.engine.control.invalidate_observation()
                await self.engine.refs.clear()
            self.page_resize_unknown.remove(page_id)
        return {"page_id": page_id, "viewport": {"width": width, "height": height},
                "device_scale_factor": dpr, "viewport_epoch": epoch}

    async def page_viewport(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) != {"page_id"}:
            raise ServiceError("invalid_config", "Page viewport requires page_id.")
        async with self.engine.control.observation(execution_domain=self.execution_domain(params["page_id"])):
            page_id = params["page_id"]
            page = await self.engine.page_by_id(page_id)
            actual = await page.evaluate("() => ({width:innerWidth,height:innerHeight,device_scale_factor:devicePixelRatio})")
            return {"page_id": page_id, "viewport": actual,
                    "viewport_epoch": self.page_viewport_epochs.get(page_id, 0),
                    "resize_state": "unknown" if page_id in self.page_resize_unknown else "confirmed"}

    async def page_demand(self, params: dict[str, Any]) -> dict[str, Any]:
        """Record display demand, not a production grant or scheduler policy."""
        if set(params) != {"page_id", "enabled"} or type(params["enabled"]) is not bool:
            raise ServiceError("invalid_config", "Page demand requires page_id and enabled.")
        # Demand revocation is a short control-plane transition, not native work
        # queued behind the capture/bind it must revoke.
        self.execution_domain(params["page_id"])
        page_id = params["page_id"]
        page = self.engine.local_page_by_id(page_id)
        self._prune_page_state()
        if params["enabled"]:
            self.page_demand_disabled.discard(page_id)
        else:
            self.page_demand_disabled.add(page_id)
            self.page_demand_epochs[page_id] = self.page_demand_epochs.get(page_id, 0) + 1
            self.page_frames.pop(page_id, None)
            self.broker_frames.pop(page_id, None)
            self.displayed_broker_frames.pop(page_id, None)
            await self.engine.refs.forget_page(page)
        return {"page_id": page_id, "enabled": params["enabled"]}

    async def page_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) != {"page_id"}:
            raise ServiceError("invalid_config", "Page stop requires page_id.")
        page = await self.engine.page_by_id(params["page_id"])
        process = page.context.owner.process
        if not hasattr(process, "domain"):
            raise ServiceError("capability_unavailable", "Independent runtime Stop requires managed execution.")
        stopped = [item for item in self.engine.context.pages if item.context is page.context]
        self.stopping_domains.add(process.domain)
        # Revoke locally before waiting for CPU exit. Queued work and late
        # callbacks cannot acquire authority even if this domain slot is reused.
        page.context.cdp.revoked = True
        for item in stopped:
            page_id = self.engine.operations.page_id(item)
            item._headless_stop_revoked = True
            self.page_frames.pop(page_id, None)
            self.broker_frames.pop(page_id, None)
            self.displayed_broker_frames.pop(page_id, None)
        stopped_ids = {self.engine.operations.page_id(item) for item in stopped}
        if self.frame is not None and self.frame["page_id"] in stopped_ids:
            self.frame = None
            self.recent_frame_ids.clear()
            self.frame_scopes.clear()
        if self.engine.active in stopped:
            self.engine.control.invalidate_observation()
        await process.stop()
        async with asyncio.timeout(13):
            await process.wait()
        # Mark the exited domain closed before releasing remote handles: a
        # disposed realm needs no RPC and must not delay the surviving domain.
        for item in stopped:
            item._closed = True
            item._discard_frames()
            await self.engine.refs.forget_page(item)
        if self.engine.active in stopped:
            await self.engine.refs.clear()
        # Exact exit was observed. Close only this session's Profile reference.
        if page.context is not self.engine.context:
            await page.context.owner.close()
            self.engine.context._peer_sessions.remove(page.context.owner)
        else:
            # The root is the Profile/application-group container, not the
            # lifetime of its initial native executor. Retain its Profile lease.
            await page.context.cdp.close()
        self.stopping_domains.discard(process.domain)
        if self.engine.context.pages:
            # Engine.page selects only a live survivor and updates ControlState's
            # page/frame identity; it never brings the stopped page back to front.
            self.engine.page()
        elif self.engine.active in stopped:
            self.engine.active = None
            self.engine.control.set_page(None, None)
            self.engine.control.invalidate_observation()
        return {"status": "stopped", "execution_domain": process.domain,
                "execution_epoch": getattr(page.context.cdp, "execution_epoch", 0),
                "stopped_page_ids": [self.engine.operations.page_id(item) for item in stopped],
                "state": self.snapshot()}

    async def page_broker_frame(self, params: dict[str, Any]) -> dict[str, Any]:
        """Request trusted native raster; this is writer completion, not publication."""
        if set(params) != {"page_id", "broker", "epoch", "hostPid", "hostUid", "timeoutMs"}:
            raise ServiceError("invalid_config", "Broker raster requires page and broker identities.")
        page_id, broker = params["page_id"], params["broker"]
        if (not isinstance(page_id, str) or not 0 < len(page_id) <= 256
                or not isinstance(broker, str) or not 0 < len(broker) < 128
                or not broker.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in broker)
                or type(params["epoch"]) is not int or not 0 < params["epoch"] <= 2**53 - 1
                or type(params["hostPid"]) is not int or not 0 < params["hostPid"] <= 2**31 - 1
                or type(params["hostUid"]) is not int or not 0 <= params["hostUid"] <= 2**32 - 1
                or type(params["timeoutMs"]) is not int or not 1 <= params["timeoutMs"] <= 10000):
            raise ServiceError("invalid_config", "Invalid broker raster identities or timeout.")
        owner_basis = self._owner_basis()
        async with self.engine.control.observation(execution_domain=self.execution_domain(page_id)):
            self._check_owner_basis(owner_basis)
            self._prune_page_state()
            if page_id not in self.broker_frames and len(self.broker_frames) >= MAX_PAGES:
                raise ServiceError("busy", "Broker observation capacity is exhausted.")
            if page_id in self.page_resize_unknown or page_id in self.page_demand_disabled:
                raise ServiceError("stale_reference", "Page geometry or demand is unavailable.")
            # A previous LPFR input basis does not describe this publication.
            self.page_frames.pop(page_id, None)
            demand_epoch = self.page_demand_epochs.get(page_id, 0)
            result = await self.engine.export_broker_page(page_id, {key: params[key] for key in
                ("broker", "epoch", "hostPid", "hostUid", "timeoutMs")})
            frame = {**result, "viewport_epoch": self.page_viewport_epochs.get(page_id, 0),
                     "demand_epoch": demand_epoch, "owner_epoch": owner_basis[0],
                     "owner_revision": owner_basis[1]}
            if owner_basis != self._owner_basis():
                # Writer-release evidence survives revocation for Host cleanup.
                return {**frame, "capture_stable": False, "input_guard": "unavailable",
                        "capture_error": "OwnershipChanged"}
            if (page_id in self.page_demand_disabled or demand_epoch != self.page_demand_epochs.get(page_id, 0)
                    or frame.get("execution_domain") in self.stopping_domains):
                # Preserve exact native writer-release evidence for Host cleanup,
                # but never publish or bind a capture from a revoked generation.
                return {**frame, "capture_stable": False, "input_guard": "unavailable",
                        "capture_error": "DemandChanged"}
            if frame.get("capture_stable") is True and frame.get("input_guard") == "native-exact-v1":
                captures = self.broker_frames.setdefault(page_id, {})
                captures[frame["visual_generation"]] = frame
                while len(captures) > 8:
                    captures.pop(next(iter(captures)))
            return frame

    def _broker_basis(self, params: dict[str, Any], frame: dict[str, Any]) -> None:
        self._check_owner_basis((frame["owner_epoch"], frame["owner_revision"]))
        for key in ("page_id", "frame_id", "document_epoch", "visual_generation"):
            if params[key] != frame[key]:
                raise ServiceError("stale_reference", "Displayed broker basis changed.")
        if (params["page_id"] in self.page_resize_unknown or params["page_id"] in self.page_demand_disabled
                or frame["viewport_epoch"] != self.page_viewport_epochs.get(params["page_id"], 0)
                or frame["demand_epoch"] != self.page_demand_epochs.get(params["page_id"], 0)):
            raise ServiceError("stale_reference", "Displayed broker geometry or demand changed.")

    async def page_bind_displayed(self, params: dict[str, Any]) -> dict[str, Any]:
        required = {"page_id", "frame_id", "document_epoch", "visual_generation", "scene_revision"}
        if (set(params) != required or not isinstance(params["page_id"], str)
                or any(type(params[key]) is not int or not 0 < params[key] <= 2**53 - 1
                       for key in ("visual_generation", "scene_revision"))):
            raise ServiceError("invalid_config", "Displayed binding requires exact page and scene identities.")
        async with self.engine.control.observation(execution_domain=self.execution_domain(params["page_id"])):
            frame = self.broker_frames.get(params["page_id"], {}).get(params["visual_generation"])
            if frame is None:
                raise ServiceError("stale_reference", "Native capture expired.")
            self._broker_basis(params, frame)
            page = await self.engine.page_by_id(params["page_id"])
            self._broker_basis(params, frame)
            if page.observation_id != frame["document_epoch"]:
                raise ServiceError("stale_reference", "Displayed document changed.")
            # A failed native rebind must not leave the previous scene's input
            # authorization available in the service cache.
            self.displayed_broker_frames.pop(params["page_id"], None)
            await page._send("Page.bindDisplayedFrame", {
                "sceneRevision": params["scene_revision"], "visualGeneration": params["visual_generation"]})
            self._broker_basis(params, frame)
            if (page.is_closed() or page.observation_id != frame["document_epoch"]
                    or self.broker_frames.get(params["page_id"], {}).get(params["visual_generation"]) is not frame):
                raise ServiceError("stale_reference", "Displayed binding was invalidated before acknowledgment.")
            self.displayed_broker_frames[params["page_id"]] = {**frame, "scene_revision": params["scene_revision"]}
            return {"status": "bound", **params}

    async def page_broker_action(self, params: dict[str, Any]) -> dict[str, Any]:
        required = {"page_id", "frame_id", "document_epoch", "visual_generation", "scene_revision",
                    "owner_revision", "operation", "args"}
        operation, channel_id = params.get("operation"), params.get("channel_id")
        begin = operation in {"mouse.down", "keyboard.down"}
        terminal = operation in {"mouse.up", "keyboard.up", "input.cancel"}
        if (set(params) != required | ({"channel_id"} if begin or terminal else set())
                or not isinstance(params.get("page_id"), str)
                or any(type(params.get(key)) is not int or not 0 < params[key] <= 2**53 - 1
                       for key in ("document_epoch", "visual_generation", "scene_revision", "owner_revision"))
                or not isinstance(params.get("frame_id"), str)
                or not isinstance(operation, str)
                or operation not in {"mouse.move", "mouse.click", "mouse.wheel", "keyboard.press",
                                     "keyboard.insert_text", "mouse.down", "mouse.up", "keyboard.down", "keyboard.up", "input.cancel"}
                or not isinstance(params.get("args"), dict)
                or ((begin or terminal) and (type(channel_id) is not int or not 0 < channel_id <= 2**53 - 1))):
            raise ServiceError("invalid_config", "Broker input requires a complete displayed basis.")
        if terminal:
            return await self._terminal_broker_input(params)
        async with self.engine.control.action("human", self.human_id, owner_revision=params["owner_revision"],
                                             execution_domain=self.execution_domain(params["page_id"])):
            frame = self.displayed_broker_frames.get(params["page_id"])
            if frame is None or frame["scene_revision"] != params["scene_revision"]:
                raise ServiceError("stale_reference", "Displayed scene changed.")
            self._broker_basis(params, frame)
            page = await self.engine.page_by_id(params["page_id"])
            self.engine.control.check_write("human", self.human_id, owner_revision=params["owner_revision"])
            self._broker_basis(params, frame)
            if page.observation_id != frame["document_epoch"]:
                raise ServiceError("stale_reference", "Displayed document changed.")
            args = self._page_input_args(operation, params["args"], frame)
            if begin:
                lane = (channel_id - 1) % 16
                if channel_id <= self.input_channel_high_water[lane]:
                    raise ServiceError("invalid_config", "Input channel identity was already used.")
                if (len(self.input_channels) >= 16
                        or any((active - 1) % 16 == lane for active in self.input_channels)):
                    raise ServiceError("busy", "Input terminal capacity is exhausted.")
                self.input_channel_high_water[lane] = channel_id
                self.input_channels[channel_id] = {
                    "page": page, "frame": frame, "basis": {key: params[key] for key in required - {"operation", "args"}},
                    "operation": operation, "args": args,
                    "domain": self.execution_domain(params["page_id"]), "known": False}
            self.displayed_broker_frames.pop(params["page_id"], None)
            await page.guarded_input(operation, args,
                                     displayed=(params["scene_revision"], params["visual_generation"]),
                                     **({"channel_id": channel_id} if begin else {}))
            if begin:
                self.input_channels[channel_id]["known"] = True
            return {"status": "ok", "page_id": params["page_id"], **({"channel_id": channel_id} if begin else {})}

    async def _terminal_broker_input(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params["channel_id"]
        channel = self.input_channels.get(channel_id)
        if channel is None:
            raise ServiceError("stale_reference", "Input channel is no longer active.")
        # The retained page object/domain, never request routing or current focus,
        # owns termination. Observation serialization deliberately needs no lease.
        async with self.engine.control.observation(execution_domain=channel["domain"]):
            if self.input_channels.get(channel_id) is not channel:
                raise ServiceError("stale_reference", "Input channel was already terminated.")
            page, frame = channel["page"], channel["frame"]
            reset = (params["operation"] == "input.cancel" or not channel["known"]
                     or getattr(page, "_headless_stop_revoked", False))
            try:
                if any(params[key] != value for key, value in channel["basis"].items()):
                    raise ServiceError("stale_reference", "Input terminal basis changed.")
                self._broker_basis(params, frame)
                self.engine.control.check_write("human", self.human_id, owner_revision=params["owner_revision"])
                if page.is_closed() or page.observation_id != frame["document_epoch"]:
                    raise ServiceError("stale_reference", "Input document changed.")
                expected = channel["operation"].replace(".down", ".up")
                if params["operation"] not in {expected, "input.cancel"}:
                    raise ServiceError("invalid_config", "Input terminal family changed.")
                if params["operation"] != "input.cancel":
                    original = channel["args"]
                    allowed = {"x", "y", "button", "click_count"} if expected == "mouse.up" else {"key"}
                    if set(params["args"]) - allowed or any(
                            value != original[key] for key, value in params["args"].items()):
                        raise ServiceError("invalid_config", "Input terminal identity changed.")
                elif params["args"]:
                    raise ServiceError("invalid_config", "Input cancel takes no arguments.")
            except Exception:
                reset = True
            if page.is_closed():
                self.input_channels.pop(channel_id)
                return {"status": "reset", "page_id": channel["basis"]["page_id"], "channel_id": channel_id}
            args = {} if reset else {key: value for key, value in channel["args"].items() if key not in {"x", "y"}}
            channel["known"] = False  # A lost terminal ACK must never replay events.
            result = await page.guarded_input("input.cancel" if reset else expected, args, channel_id=channel_id)
            self.input_channels.pop(channel_id)
            return {"status": "reset" if reset or (result or {}).get("status") == "reset" else "ok",
                    "page_id": channel["basis"]["page_id"], "channel_id": channel_id}

    def _page_input_args(self, operation: str, supplied: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any]:
        args = {}
        if operation in {"mouse.click", "mouse.move", "mouse.wheel", "mouse.down"}:
            for key, maximum in (("x", frame["viewport"]["width"]), ("y", frame["viewport"]["height"])):
                value = supplied.get(key)
                if not _number(value, 0, maximum) or value >= maximum:
                    raise ServiceError("invalid_config", "Mouse coordinates are outside the page viewport.")
                args[key] = value
            if operation in {"mouse.click", "mouse.down"}:
                button, count = supplied.get("button", "left"), supplied.get("click_count", 1)
                if not isinstance(button, str) or button not in {"left", "middle", "right"} or type(count) is not int or count not in {1, 2, 3}:
                    raise ServiceError("invalid_config", "Invalid mouse button or click count.")
                args.update(button=button, click_count=count)
        if operation == "mouse.wheel":
            for key in ("delta_x", "delta_y"):
                value = supplied.get(key)
                if not _number(value, -10000, 10000):
                    raise ServiceError("invalid_config", "Invalid wheel delta.")
                args[key] = value
        elif operation in {"keyboard.press", "keyboard.down"}:
            key = supplied.get("key")
            if not isinstance(key, str) or not 0 < len(key) <= 100 or any(ord(c) < 32 for c in key):
                raise ServiceError("invalid_config", "Invalid key name.")
            args["key"] = key
        elif operation == "keyboard.insert_text":
            text = supplied.get("text")
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 8192 or "\0" in text:
                raise ServiceError("invalid_config", "Invalid input text.")
            args["text"] = text
        elif operation not in {"mouse.move", "mouse.click", "mouse.down"}:
            raise ServiceError("invalid_config", "Unsupported page input.")
        if set(supplied) - set(args):
            raise ServiceError("invalid_config", "Unknown page input arguments.")
        return args

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
        async with self.engine.control.action("human", self.human_id, **expected,
                                             execution_domain=self.active_execution_domain()):
            await self.engine.refresh_pages()
            page = self.engine.page()
            self._prune_page_state()
            if params["page_id"] in self.page_resize_unknown:
                raise ServiceError("stale_reference", "Page viewport outcome is unknown.")
            self.engine.control.check_write("human", self.human_id, **expected)
            if self.engine.operations.page_id(page) != params["page_id"]:
                raise ServiceError("stale_reference", "The active page changed before resize.")
            self.frame = None
            self.recent_frame_ids.clear()
            self.frame_scopes.clear()
            self.viewport_epoch += 1
            self.page_viewport_epochs[params["page_id"]] = self.viewport_epoch
            self.page_frames.pop(params["page_id"], None)
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
        self.owner_epoch += 1
        self.frame = None
        self.recent_frame_ids.clear()
        self.frame_scopes.clear()
        self.page_frames.clear()
        self.broker_frames.clear()
        self.displayed_broker_frames.clear()
        if operation == "request_human":
            await self.engine.control.request_human(self.human_id)
        else:
            transition = self.engine.control.resume_agent if operation == "resume_agent" else self.engine.control.reclaim
            await transition(run_id)
        return {"state": self.snapshot()}

    async def page_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch native input against an explicitly captured page basis."""
        required = {"operation", "args", "image_id", "frame_seq", "page_id", "frame_id",
                    "observation_id", "owner_revision", "document_epoch", "viewport_epoch",
                    "focused_frame_id", "focused_element_id"}
        if set(params) != required or self.ring is None:
            raise ServiceError("invalid_config", "Page input requires a complete native frame basis.")
        page_id = params["page_id"]
        if not isinstance(page_id, str):
            raise ServiceError("invalid_config", "Invalid page identity.")
        frame = self.page_frames.get(page_id)
        if frame is None or any(params[key] != frame.get(key) for key in required - {"operation", "args"}):
            raise ServiceError("stale_reference", "Page frame basis is stale.")
        revision = params["owner_revision"]
        if any(type(params[key]) is not int for key in ("frame_seq", "owner_revision", "document_epoch", "viewport_epoch")):
            raise ServiceError("invalid_config", "Invalid page frame epochs.")
        if type(revision) is not int:
            raise ServiceError("invalid_config", "Invalid ownership revision.")
        operation, supplied = params["operation"], params["args"]
        if not isinstance(operation, str) or not isinstance(supplied, dict):
            raise ServiceError("invalid_config", "Invalid page input.")
        args = self._page_input_args(operation, supplied, frame)
        async with self.engine.control.action("human", self.human_id, owner_revision=revision,
                                             execution_domain=self.execution_domain(page_id)):
            page = await self.engine.page_by_id(page_id)

            async def revalidate():
                self.engine.control.check_write("human", self.human_id, owner_revision=revision)
                if (self.page_frames.get(page_id) is not frame or page_id in self.page_resize_unknown or page.is_closed()
                        or page.observation_id != frame["document_epoch"]
                        or self.page_viewport_epochs.get(page_id, 0) != frame["viewport_epoch"]):
                    raise ServiceError("stale_reference", "Page document or viewport changed.")
                await self.engine.refs.validate(self.engine.refs.page_visuals.get(page), page,
                                                check_mutations=False)
                self.engine.control.check_write("human", self.human_id, owner_revision=revision)
                if (self.page_frames.get(page_id) is not frame or page_id in self.page_resize_unknown or page.is_closed()
                        or page.observation_id != frame["document_epoch"]
                        or self.page_viewport_epochs.get(page_id, 0) != frame["viewport_epoch"]):
                    raise ServiceError("stale_reference", "Page document or viewport changed.")

            async def rebind():
                await revalidate()
                destination = self._raw_destination(self.execution_domain(page_id))
                if destination is None:
                    raise ServiceError("stale_reference", "Page input requires the frame ring.")
                result = await page.capture_raw_frame(*destination)
                fingerprint = result.get("render_fingerprint")
                if not isinstance(fingerprint, str) or _RENDER_FINGERPRINT.fullmatch(fingerprint) is None:
                    raise ServiceError("verification_failed", "Native frame omitted its render fingerprint.")
                if (result.get("focused_frame_id") != frame["focused_frame_id"]
                        or result.get("focused_element_id") != frame["focused_element_id"]):
                    raise ServiceError("stale_reference", "Page focus changed.")
                await revalidate()
                return fingerprint

            try:
                await revalidate()
                fingerprint = await rebind() if operation in POINTER_OPERATIONS else frame["render_fingerprint"]
                await page.guarded_input(operation, args, fingerprint,
                                         rebind=rebind if operation in POINTER_OPERATIONS else None)
            finally:
                if self.page_frames.get(page_id) is frame:
                    self.page_frames.pop(page_id, None)
        return {"status": "ok", "operation": operation, "page_id": page_id}

    def validate_action(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if set(params) - {"operation", "args", "image_id", "frame_seq", "page_id", "frame_id", "observation_id", "owner_revision",
                          "document_epoch", "viewport_epoch", "focused_frame_id", "focused_element_id"}:
            raise ServiceError("invalid_config", "Unknown action fields.")
        operation, supplied = params.get("operation"), params.get("args")
        if not isinstance(operation, str) or not isinstance(supplied, dict):
            raise ServiceError("invalid_config", "Action operation must be a string and args must be an object.")
        frame = self.frame
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

    async def _select(self, page: Any, *, authorize: Callable[[], None] | None = None) -> None:
        def before_select() -> None:
            if authorize is not None:
                authorize()
            self.engine.human_selection_epoch += 1

        await self.engine._select(page, before_select=before_select)

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
        async with self.engine.control.action("human", self.human_id, **expected,
                                             execution_domain=self.active_execution_domain()):
            self.validate_action(params)
            navigation = params["operation"] in {"navigate", "reload", "back", "forward"}
            if navigation:
                self.pending_navigation = {"page": self.engine.page(), "revision": expected["owner_revision"],
                                           "stop": asyncio.get_running_loop().create_future(), "stop_requested": False}
            try:
                await self.engine.refresh_pages()
                self.validate_action(params)  # Refresh may replace the active document/page.
                shell = params["operation"] in SHELL_OPERATIONS
                # Native SHM input has the stronger atomic pixel+semantic guard below.
                # Keep document/view/control checks, not asynchronous mutation delivery.
                if not shell:
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
                # Native dispatch checks the displayed fingerprint atomically.
                # Repaint only after its exact, certain non-dispatch rejection.
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
        url = self.engine.page().url
        if state.get("url") != url:
            state["url"] = url
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
                                 "runtime": "mioa", "home": str(self.home)})
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


def _prepare_profile(args: Any, profile: Any, native_spec: Any, *, leases: Any, owners: Any, artifact_discovery: Any = None, artifact_content: Any = None, random: Any = None) -> None:
    name = args.profile
    location = profile.profile_dir(name)
    config_path = location / "config.json"
    if config_path.exists() or config_path.is_symlink():
        cfg = profile.load_config(name)
        if cfg is None or not isinstance(cfg.get("runtime"), dict) or cfg["runtime"].get("id") not in {"mioa", "lightpanda"}:
            raise ServiceError("incompatible_profile", "An existing profile must contain a valid managed Mioa configuration.")
        native_spec.validate_native_spec(cfg, name=name, artifact_content=artifact_content)
        if Path(cfg["storage"]["path"]).resolve() != location.resolve() / "data":
            raise ServiceError("incompatible_profile", "The saved profile does not own this isolated storage directory.")
        if args.binary is not None:
            requested = native_spec.runtime_spec(cfg["runtime"].get("id") or "mioa", args.binary,
                                                 artifact_content=artifact_content)
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
                    cfg = native_spec.migrate_native_spec(cfg, network={**cfg["network"], "allow_private_network": desired_private},
                                                          artifact_content=artifact_content)
                    if db_row is not None:
                        cfg["revision"] = int(db_row[0]) + 1
                        cfg["previous_environment_sha256"] = str(db_row[2])
                    profile.save_config(name, cfg)
            else:
                raise ServiceError("incompatible_profile", "Requested network policy differs from the saved profile; explicit migration is required.")
        return
    if location.exists() and any(path.name not in {".lock", ".spawn"} for path in location.iterdir()):
        raise ServiceError("incompatible_profile", "A nonempty profile without a valid configuration will not be overwritten.")
    with profile.exclusive("catalog", kind="catalog", leases=leases) as catalog:
        if not catalog:
            raise ServiceError("busy", "The profile catalog is in use.")
        assets = ({"fonts": sorted({str(Path(path).expanduser().resolve()) for path in args.font_file})}
                  if args.font_file is not None else native_spec.native_font_assets(artifact_discovery=artifact_discovery))
        families = native_spec.native_font_families(assets, artifact_content=artifact_content)
        if not families:
            raise ServiceError("capability_unavailable", "No real font families are available in the pinned font files.")
        existing = [profile.require(row["name"]) for row in profile.list_profiles(owners=owners) if row["name"] != name]
        cfg = native_spec.create_native_spec(name, runtime="mioa", binary=args.binary,
                                             storage_path=location / "data", asset_paths=assets,
                                             overrides={"fonts": sorted(families)}, existing=existing,
                                             network={"allow_private_network": bool(args.allow_private_network)},
                                             artifact_discovery=artifact_discovery,
                                             artifact_content=artifact_content, random=random)
        cfg["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        profile.save_config(name, cfg)


async def run_service(args: Any) -> int:
    if sys.platform != "darwin":
        raise ServiceError("unsupported_platform", "The current Mioa native artifact requires macOS CGL; Linux/Windows execution is unsupported.")
    supervised = "MIOA_SUPERVISOR_FD" in os.environ
    if not supervised:
        return await _run_service(args, None, None)
    try:
        from mioa_core.native import bind_supervisor
        from mioa_os import LocalBlockingCalls, LocalManagedPeerIdentity, LocalProcessIdentity, LocalProfileLeases, LocalSupervisorChannel, LocalUnixEndpoints, LocalUnixMessages, profile_owner_command
    except ImportError as exc:
        raise ServiceError("capability_unavailable", "Install the project's Python 3.12 dependencies before starting the service.") from exc
    leases = LocalProfileLeases()
    supervisor = bind_supervisor(
        supervisor_channel=LocalSupervisorChannel(), endpoints=LocalUnixEndpoints(),
        blocking_calls=LocalBlockingCalls(), messages=LocalUnixMessages(),
        profile_command=profile_owner_command(), identity=LocalProcessIdentity(),
        managed_identity=LocalManagedPeerIdentity(), leases=leases)
    try:
        return await _run_service(args, supervisor, leases)
    finally:
        supervisor.socket.close()


async def _run_service(args: Any, supervisor: Any, leases: Any) -> int:
    supervised = supervisor is not None
    home = Path(args.home).expanduser().resolve() if args.home else default_home().resolve()
    try:
        from mioa_core import native_spec, profile
        from mioa_core.engine import Engine
        from mioa_os import LocalArtifactContent, LocalArtifactDiscovery, LocalChildProcesses, LocalFileExports, LocalHostFiles, LocalProcessEnvironment, LocalProcessIdentity, LocalProfileLeases, LocalProfileOwners, LocalProxySecrets, LocalSecureRandom, LocalStreamConnections
    except ImportError as exc:
        raise ServiceError("capability_unavailable", "Install the project's Python 3.12 dependencies before starting the service.") from exc
    profile.BASE = home
    if not isinstance(args.profile, str) or not re.fullmatch(r"[\w-]{1,80}", args.profile) or profile.sanitize(args.profile) != args.profile:
        raise ServiceError("invalid_config", "Profile names must be 1–80 letters, digits, underscores or hyphens without edge hyphens/underscores.")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    leases = leases if supervised else LocalProfileLeases()
    owners = LocalProfileOwners()
    artifact_content = LocalArtifactContent()
    # Ordinary children inherit the engine lease; supervised startup instead
    # uses the separate Profile owner and its domain endpoints.
    with profile.exclusive(args.profile, kind="spawn", leases=leases) as spawned, (
            contextlib.nullcontext(True) if supervised else profile.exclusive(args.profile, leases=leases)) as owned:
        if not spawned or not owned or owners.alive(home, args.profile):
            raise ServiceError("busy", "Another process owns this persistent profile.")
        with (profile.exclusive(args.profile, leases=leases) if supervised else contextlib.nullcontext(True)) as prepare_owned:
            if not prepare_owned:
                raise ServiceError("busy", "Another process owns this persistent profile.")
            _prepare_profile(args, profile, native_spec, leases=leases, owners=owners,
                             artifact_discovery=None if supervised else LocalArtifactDiscovery(),
                             artifact_content=artifact_content, random=LocalSecureRandom())
        owner_launch = owner_control = None
        if supervised:
            owner_launch, _, owner_control = await supervisor.start_profile(args.profile, home)
        try:
            owners.publish(home, args.profile)
            profile.save_state(args.profile, {"status": "STARTING", "runtime": "mioa"})
        except BaseException:
            if owner_control is not None:
                owner_control.close()
            if supervisor is not None:
                supervisor.socket.close()
            raise
        # Skip redundant navigation: the fresh native context owns about:blank.
        initial_url = None if args.url == "about:blank" else args.url
        engine = Engine(args.profile, headless=True, initial_url=initial_url, verify=False,
                        file_exports=None if supervised else LocalFileExports(),
                        child_processes=None if supervised else LocalChildProcesses(),
                        process_environment=None if supervised else LocalProcessEnvironment(),
                        proxy_secrets=None if supervised else LocalProxySecrets(home),
                        process_identity=LocalProcessIdentity(), profile_lease=None if supervised else owned,
                        profile_owners=owners, secure_random=LocalSecureRandom(),
                        artifact_content=artifact_content,
                        stream_connections=LocalStreamConnections(),
                        host_files=LocalHostFiles())
        engine.profile_owner_control = owner_control
        engine.supervisor = supervisor
        engine.profile_owner_launch_id = owner_launch
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
            if owner_control is not None:
                owner_control.close()
            if supervisor is not None and engine.native is None:
                supervisor.socket.close()
