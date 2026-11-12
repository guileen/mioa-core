"""One owned native process, one raw CDP connection, explicit target sessions.

This is a deliberately bounded Playwright-style subset, not a second browser or
an identity injection layer. Unsupported native commands remain typed errors.
Only DOM observation/selection uses page JavaScript; input uses native CDP Input.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import hashlib
import inspect
import json
import math
import re
import struct
from dataclasses import dataclass
from functools import wraps
from pathlib import PurePath
from typing import Any, AsyncIterator, Callable
from urllib.parse import unquote, urlsplit
from mioa_os_api import ArtifactContent, BlockingCalls, ChildProcess, ChildProcesses, FileExports, HostFiles, ManagedPeerIdentity, ProcessEnvironment, ProcessIdentity, ProcessOutput, ProfileLease, ProfileLeaseCopy, ProfileLeases, ProxySecrets, SecureRandom, StreamConnections, SupervisorChannel, UnixEndpoint, UnixEndpoints, UnixMessages

from .native_spec import NativeError, _secure_bytes, resolve_proxy, validate_native_spec

MAX_MESSAGE = 32 * 1024 * 1024
MAX_LOG = 1024 * 1024
DEFAULT_TIMEOUT = 30000
# POSIX SIGTERM as encoded in a negative wait status; native runtimes are macOS-only.
_EXIT_SIGTERM = -15
# A render-only native rejection is a certain non-dispatch, so a rebind retry is
# never a replay of input that may already have been delivered.
RENDER_REBINDS = 2
RENDER_ONLY_REJECTION = "stale native render fingerprint"
_MISSING = object()


class ProtocolError(NativeError):
    def __init__(self, method: str, error: dict[str, Any]):
        text = str(error.get("message", "native protocol error"))
        unavailable = error.get("code") == -32601 or any(word in text.lower() for word in ("not implemented", "unsupported", "method not found", "unknown method", "targetalreadyloaded"))
        code = "stale_reference" if error.get("code") == -32042 else "capability_unavailable" if unavailable else "protocol_error"
        super().__init__(code, f"{method}: {text}", details=error)
        self.method = method


def _render_only_rejection(exc: ProtocolError) -> bool:
    """True only for the runtime's pre-dispatch render comparison."""
    details = exc.details if isinstance(exc.details, dict) else {}
    return details.get("code") == -32042 and details.get("message") == RENDER_ONLY_REJECTION


def _deadline(page: NativePage | NativeFrame, timeout: float | None) -> float:
    return page.context.owner.monotonic_time() + (DEFAULT_TIMEOUT if timeout is None else timeout) / 1000


def _remaining(page: NativePage | NativeFrame, deadline: float) -> float:
    value = deadline - page.context.owner.monotonic_time()
    if value <= 0:
        raise NativeError("timeout", "native operation deadline exceeded")
    return value


def _validate_load_state(state: str) -> None:
    if state not in {"load", "domcontentloaded", "networkidle", "commit"}:
        raise NativeError("invalid_config", f"unknown load state {state!r}")


def _bounded(function: Callable) -> Callable:
    @wraps(function)
    async def bounded(*args: Any, **kwargs: Any) -> Any:
        timeout = kwargs.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise NativeError("invalid_config", "timeout must be a finite positive millisecond value")
        try:
            async with asyncio.timeout(timeout / 1000):
                return await function(*args, **kwargs)
        except TimeoutError as exc:
            error = NativeError("timeout", f"{function.__qualname__} exceeded {timeout:g}ms; action side effects may be unknown")
            error.startup_stage = getattr(exc, "startup_stage", "engine_start")
            raise error from exc
    return bounded


class _WebSocket:
    """RFC6455 client: no extensions, masked client frames, bounded messages."""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, random: SecureRandom):
        self.reader, self.writer, self.random = reader, writer, random
        self.lock = asyncio.Lock()
        self.closed = False

    @classmethod
    async def connect(cls, url: str, random: SecureRandom, *,
                      connections: StreamConnections) -> _WebSocket:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or parsed.username:
            raise NativeError("verification_failed", "native endpoint must be a loopback ws URL")
        reader, writer = await asyncio.wait_for(
            connections.connect_tcp(parsed.hostname, parsed.port or 80, limit=MAX_MESSAGE + 65536), 5)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return await cls._upgrade(reader, writer, path, f"{host}:{parsed.port or 80}", random)

    @classmethod
    async def connect_owned_unix(cls, sock: Any, peer: object,
                                 managed_identity: ManagedPeerIdentity, random: SecureRandom, *,
                                 connections: StreamConnections) -> _WebSocket:
        """Adopt a connected Unix socket only after OS peer verification."""
        try:
            verified = managed_identity.verify_child(sock, peer)
            if not verified:
                raise NativeError("verification_failed", "native peer identity does not match supervised child")
            reader, writer = await connections.adopt_unix(sock, limit=MAX_MESSAGE + 65536)
        except BaseException:
            sock.close()
            raise
        return await cls._upgrade(reader, writer, "/", "localhost:0", random)

    @classmethod
    async def _upgrade(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       path: str, host: str, random: SecureRandom) -> _WebSocket:
        try:
            key = base64.b64encode(_secure_bytes(random, 16)).decode()
            writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = raw.decode("latin1").split("\r\n")
            headers = {name.lower():value.strip() for name,value in (line.split(":",1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            status = lines[0].split(" ", 2)
            connection = {token.strip().lower() for token in headers.get("connection", "").split(",")}
            if (len(raw) > 65536 or status[:2] != ["HTTP/1.1", "101"]
                    or headers.get("sec-websocket-accept", "") != expected
                    or headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in connection
                    or "sec-websocket-extensions" in headers):
                raise NativeError("protocol_error", "invalid native websocket handshake")
            return cls(reader, writer, random)
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def send(self, payload: bytes, opcode: int = 1, *, abort: Callable[[], bool] | None = None) -> None:
        if self.closed:
            raise NativeError("protocol_error", "native websocket closed")
        if len(payload) > MAX_MESSAGE:
            raise NativeError("invalid_config", "CDP message exceeds transport bound")
        mask = _secure_bytes(self.random, 4)
        length = len(payload)
        header = bytes((0x80 | opcode, 0x80 | length)) if length < 126 else bytes((0x80 | opcode, 0x80 | (126 if length <= 65535 else 127))) + struct.pack("!H" if length <= 65535 else "!Q", length)
        masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        async with self.lock:
            if self.closed:
                raise NativeError("protocol_error", "native websocket closed")
            if abort is not None and abort():
                raise NativeError("protocol_error", "native command no longer pending")
            if opcode == 8:
                self.closed = True
            self.writer.write(header + mask + masked)
            await self.writer.drain()

    async def receive(self) -> bytes:
        chunks: list[bytes] = []
        size = 0
        fragmented = False
        while True:
            first, second = await self.reader.readexactly(2)
            fin, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80:
                raise NativeError("protocol_error", "unexpected websocket extension or masked server frame")
            length = second & 127
            if opcode >= 8 and (not fin or length > 125):
                raise NativeError("protocol_error", "invalid websocket control frame")
            if length == 126:
                length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if length > MAX_MESSAGE or (opcode < 8 and size + length > MAX_MESSAGE):
                raise NativeError("protocol_error", "native websocket message exceeds bound")
            payload = await self.reader.readexactly(length)
            if opcode >= 8:
                if opcode == 8:
                    raise NativeError("protocol_error", "native websocket peer closed")
                if opcode == 9:
                    await self.send(payload, 10)
                elif opcode != 10:
                    raise NativeError("protocol_error", "unknown websocket control frame")
                continue
            if opcode == 1 and not fragmented:
                fragmented = True
            elif opcode != 0 or not fragmented:
                raise NativeError("protocol_error", "invalid websocket data sequence")
            chunks.append(payload)
            size += length
            if fin:
                return b"".join(chunks)

    async def close(self) -> None:
        if self.closed:
            return
        try:
            with contextlib.suppress(OSError, NativeError):
                await asyncio.wait_for(self.send(struct.pack("!H", 1000), 8), 1)
        finally:
            self.closed = True
            self.writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(self.writer.wait_closed(), 2)


class _CDP:
    def __init__(self, socket_: _WebSocket):
        self.socket = socket_
        self.sequence = 0
        self.pending: dict[int, tuple[asyncio.Future, str, str | None]] = {}
        self.listeners: list[Callable] = []
        self.execution_domain = 0
        self.execution_epoch = 0
        self.event_tasks: set[asyncio.Future] = set()
        self.event_errors: list[str] = []
        self.failure: BaseException | None = None
        self.reader = asyncio.create_task(self._read())

    def _task(self, value: Any) -> None:
        if inspect.isawaitable(value):
            task = asyncio.ensure_future(value)
            self.event_tasks.add(task)
            task.add_done_callback(self._task_done)

    def _record_event_error(self, error: BaseException) -> None:
        self.event_errors.append(str(error))
        del self.event_errors[:-50]

    def _task_done(self, task: asyncio.Future) -> None:
        self.event_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._record_event_error(error)

    async def _read(self) -> None:
        try:
            while True:
                message = json.loads(await self.socket.receive())
                if "id" in message:
                    if type(message["id"]) is not int:
                        raise NativeError("protocol_error", "native response id must be an integer")
                    # Retain cleanup ownership until decoding and delivery finish.
                    pending = self.pending.get(message["id"])
                    if pending is None:
                        continue
                    future, method, session = pending
                    if future.done():
                        self.pending.pop(message["id"], None)
                        continue
                    if message.get("sessionId") != session:
                        future.set_exception(NativeError("protocol_error", f"{method}: response session mismatch"))
                    elif "error" in message:
                        future.set_exception(ProtocolError(method, message["error"]))
                    else:
                        result = message.get("result")
                        if result is not None and not isinstance(result, dict):
                            future.set_exception(NativeError("protocol_error", f"{method}: non-object result"))
                        else:
                            future.set_result(result or {})
                    self.pending.pop(message["id"], None)
                else:
                    message["_execution_domain"] = self.execution_domain
                    message["_execution_epoch"] = self.execution_epoch
                    for listener in tuple(self.listeners):
                        self._task(listener(message))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.failure = exc
        finally:
            for future, _, _ in self.pending.values():
                if not future.done():
                    future.set_exception(NativeError("protocol_error", f"native connection lost: {self.failure}"))
            self.pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None, session: str | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if getattr(self, "revoked", False):
            raise NativeError("stale_reference", "execution incarnation is revoked")
        if self.failure or self.reader.done():
            raise NativeError("protocol_error", f"native connection unavailable: {self.failure}")
        self.sequence += 1
        sequence = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = (future, method, session)
        frame = {"id": sequence, "method": method, "params": params or {}}
        if session:
            frame["sessionId"] = session
        write_task = None
        try:
            async with asyncio.timeout(timeout):
                payload = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode()
                write_task = asyncio.create_task(self.socket.send(payload, abort=future.done))
                done, _ = await asyncio.wait((write_task, future), return_when=asyncio.FIRST_COMPLETED)
                if future in done and not future.cancelled() and future.exception() is not None:
                    return await future
                await write_task
                return await future
        except TimeoutError as exc:
            raise NativeError("timeout", f"{method} exceeded {timeout:g}s; action side effects may be unknown") from exc
        finally:
            self.pending.pop(sequence, None)
            if write_task is not None:
                if not write_task.done():
                    write_task.cancel()
                await asyncio.gather(write_task, return_exceptions=True)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def close(self) -> None:
        try:
            await self.socket.close()
        finally:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            for task in tuple(self.event_tasks):
                task.cancel()
            if self.event_tasks:
                await asyncio.gather(*self.event_tasks, return_exceptions=True)


@dataclass
class NativeRequest:
    url: str
    headers: dict[str, str]
    resource_type: str
    method: str

    async def all_headers(self) -> dict[str, str]:
        return dict(self.headers)


@dataclass
class NativeResponse:
    url: str
    status: int
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status <= 299



@dataclass(frozen=True, slots=True)
class NativeSiteIcon:
    """Verified original browser resource bytes, not a viewport-derived image."""
    document_id: str
    revision: int
    sha256: str
    mime: str
    data: bytes

class NativeContext:
    def __init__(self, session: NativeSession, cdp: _CDP):
        self.owner, self.cdp = session, cdp
        self._pages: dict[str, NativePage] = {}
        self._adopting: dict[str, asyncio.Task] = {}
        self._callbacks: dict[str, list[Callable]] = {}
        self._peer_sessions: list[NativeSession] = []
        self._application_root = self
        self._application_lock = asyncio.Lock()
        self.closed = False
        cdp.listeners.append(self._event)

    @property
    def pages(self) -> list[NativePage]:
        return ([page for page in self._pages.values() if not page.is_closed() and getattr(self.cdp, "failure", None) is None]
                + [page for session in self._peer_sessions for page in session.context.pages])

    def on(self, event: str, callback: Callable) -> None:
        if event not in {"request", "page", "close"}:
            raise NativeError("capability_unavailable", f"Context event {event!r} is not implemented")
        self._callbacks.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Callable) -> None:
        callbacks = self._callbacks.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def _emit(self, event: str, value: Any) -> None:
        for callback in tuple(self._callbacks.get(event, ())):
            try:
                self.cdp._task(callback(value))
            except Exception as error:
                self.cdp._record_event_error(error)

    async def initialize(self) -> None:
        # Native LP's pre-context STARTUP target is not a page. Create the real
        # persistent target explicitly before enabling target discovery.
        await self._new_local_page()
        await self.cdp.send("Target.setDiscoverTargets", {"discover": True})
        targets = await self.cdp.send("Target.getTargets")
        if not isinstance(targets.get("targetInfos"), list):
            raise NativeError("protocol_error", "Target.getTargets omitted targetInfos")
        for target in targets["targetInfos"]:
            if target.get("type") == "page":
                await self._adopt(target)

    def _event(self, event: dict[str, Any]) -> None:
        if self.closed or getattr(self.cdp, "revoked", False):
            return
        method, params = event.get("method"), event.get("params", {})
        if method == "Target.targetCreated" and params.get("targetInfo", {}).get("type") == "page":
            self.cdp._task(self._adopt(params["targetInfo"]))
        elif method == "Target.targetDestroyed":
            page = self._pages.get(params.get("targetId"))
            if page:
                page._discard_frames()
                page._closed = True
                page._site_icon = None
                page._site_icon_version = None
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo", {})
            page = self._pages.get(info.get("targetId"))
            if page:
                url = info.get("url", page._url)
                if url != page._url:
                    page._target_url_revision += 1
                    page._url = url
                if isinstance(info.get("title"), str):
                    page._title = info["title"]
        else:
            for page in tuple(self._pages.values()):
                if event.get("sessionId") == page.session_id:
                    page._event(method, params)
                    break

    async def _adopt(self, target: dict[str, Any]) -> NativePage:
        if self.closed or getattr(self.cdp, "revoked", False):
            raise NativeError("stale_reference", "execution incarnation is revoked")
        target_id = target.get("targetId")
        if not isinstance(target_id, str) or not target_id or target_id == "TID-STARTUP":
            raise NativeError("protocol_error", "native runtime returned invalid/uninitialized page target")
        task = self._adopting.get(target_id)
        if task is None:
            existing = self._pages.get(target_id)
            if existing is not None:
                if existing.is_closed():
                    raise NativeError("stale_reference", "page target is no longer live")
                return existing

            async def attach() -> NativePage:
                reply = await self.cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                session_id = reply.get("sessionId")
                if not isinstance(session_id, str) or not session_id or session_id == "STARTUP":
                    raise NativeError("protocol_error", "native target attach omitted real session ID")
                page = NativePage(self, target_id, session_id, target.get("url", "about:blank"))
                if isinstance(target.get("title"), str):
                    page._title = target["title"]
                self._pages[target_id] = page
                try:
                    await page._initialize()
                    if self.closed or page.is_closed():
                        raise NativeError("stale_reference", "page target was revoked while attaching")
                except BaseException:
                    self._pages.pop(target_id, None)
                    raise
                self._emit("page", page)
                return page

            def finished(done: asyncio.Task) -> None:
                if self._adopting.get(target_id) is done:
                    self._adopting.pop(target_id)
                if not done.cancelled():
                    done.exception()  # The context owns completion even if every waiter cancels.

            task = asyncio.create_task(attach())
            self._adopting[target_id] = task
            task.add_done_callback(finished)
        page = await asyncio.shield(task)
        if self.closed or page.is_closed():
            raise NativeError("stale_reference", "page target is no longer live")
        return page

    async def _new_local_page(self) -> NativePage:
        if self.closed or getattr(self.cdp, "revoked", False):
            raise NativeError("stale_reference", "execution domain is closed")
        reply = await self.cdp.send("Target.createTarget", {"url": "about:blank"})
        return await self._adopt({"targetId": reply.get("targetId"), "url": "about:blank"})

    async def new_page(self, *, synchronize_with: NativePage | None = None) -> NativePage:
        root = self._application_root
        if root.closed:
            raise NativeError("stale_reference", "context is closed")
        if synchronize_with is not None:
            if synchronize_with not in root.pages or synchronize_with.is_closed():
                raise NativeError("stale_reference", "synchronous page group is no longer live")
            return await synchronize_with.context._new_local_page()
        process = root.owner.process
        if not isinstance(process, _SupervisedProcess):
            return await self._new_local_page()
        return await root._new_application()

    def application_domain(self) -> int:
        """Choose local admission identity; Host still decides actual admission."""
        root = self._application_root
        if root.closed:
            raise NativeError("stale_reference", "context is closed")
        process = root.owner.process
        if not isinstance(process, _SupervisedProcess):
            raise NativeError("capability_unavailable", "managed application creation requires supervision")
        domain = next((item for item in process.supervisor.application_domains
                       if item not in process.supervisor._reserved_domains), None)
        if domain is None:
            raise NativeError("busy", "managed application domain capacity is exhausted")
        return domain

    async def _new_application(self, domain: int | None = None) -> NativePage:
        root = self._application_root
        process = root.owner.process
        async with root._application_lock:
            if domain is None:
                domain = root.application_domain()
            elif (root.closed or domain not in process.supervisor.application_domains
                  or domain in process.supervisor._reserved_domains):
                raise NativeError("busy", "application admission changed before dispatch")
            supervisor = process.supervisor
            peer = await adopt_supervised_native(
                process.profile_name, root.owner.config, supervisor,
                process.profile_launch_id, process.profile_control, domain=domain,
                random=root.owner.random, artifact_content=root.owner.artifact_content,
                connections=root.owner.connections, files=root.owner.files)
            root._peer_sessions.append(peer)
            peer.context._application_root = root
            peer.context._callbacks = root._callbacks
            for listener in root.cdp.listeners:
                if listener != root._event:
                    peer.cdp.listeners.append(listener)
            page = peer.context.pages[0]
            root._emit("page", page)
            return page

    async def cookies(self, urls: str | list[str] | None = None) -> list[dict[str, Any]]:
        page = self.pages[0] if self.pages else await self.new_page()
        reply = await page._send("Network.getAllCookies") if urls is None else await page._send("Network.getCookies", {"urls": [urls] if isinstance(urls, str) else urls})
        if not isinstance(reply.get("cookies"), list):
            raise NativeError("protocol_error", "native cookie reply omitted cookies")
        return reply["cookies"]

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        page = self.pages[0] if self.pages else await self.new_page()
        await page._send("Network.setCookies", {"cookies": cookies})

    async def clear_cookies(self) -> None:
        page = self.pages[0] if self.pages else await self.new_page()
        await page._send("Network.clearBrowserCookies")

    async def close(self) -> None:
        await self.owner.close()


class NativePage:
    def __init__(self, context: NativeContext, target: str, session: str, url: str):
        self.context, self.page_id, self.target_id, self.session_id = context, target, target, session
        self.frame_id: str | None = None
        self.observation_id = 0
        self._main_frame_commit = 0
        self._committed_loader: str | None = None
        self._url, self._closed = url, False
        self._target_url_revision = 0
        self._title = ""
        self._responses: dict[str, NativeResponse] = {}
        self._requests: dict[str, str | None] = {}
        self._request_generation = 0
        self._document_response: NativeResponse | None = None
        self._execution_context: int | None = None
        self._frame_contexts: dict[str, int] = {}
        self._frame_ids: set[str] = set()
        self._frames: dict[str, NativeFrame] = {}
        session = getattr(context, "owner", None)
        platform = getattr(session, "platform", "Darwin")
        self.mouse = NativeMouse(self)
        self.keyboard = NativeKeyboard(self, platform=platform)
        self._native_input_batch: tuple[asyncio.Task, list[dict[str, Any]]] | None = None
        self._site_icon: NativeSiteIcon | None = None
        self._site_icon_version: tuple[str, int] | None = None
        self._site_icons_subscribed = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def cached_title(self) -> str:
        """Last observed title; reading inventory must not execute page script."""
        return self._title

    @property
    def cached_site_icon(self) -> NativeSiteIcon | None:
        return self._site_icon

    def is_closed(self) -> bool:
        return (self._closed or getattr(self.context.cdp, "revoked", False)
                or getattr(self.context.cdp, "failure", None) is not None)

    def _take_frames(self) -> list[NativeFrame]:
        frames = list(self._frames.values())
        self._frames.clear()
        for frame in frames:
            frame._closed = True
        return frames

    def _discard_frames(self) -> None:
        for frame in self._take_frames():
            self.context.cdp._task(frame._release_owner())

    async def _release_frames(self) -> None:
        frames = self._take_frames()
        if frames:
            await asyncio.gather(*(frame._release_owner() for frame in frames))

    def _frame_document(self, identifier: str) -> NativeFrame:
        frame = self._frames.get(identifier)
        if frame is not None and frame.is_closed():
            self._frames.pop(identifier)
            frame._closed = True
            self.context.cdp._task(frame._release_owner())
            frame = None
        if frame is None:
            frame = NativeFrame(self, identifier)
            self._frames[identifier] = frame
        return frame

    async def frame_documents(self) -> list[NativePage | NativeFrame]:
        """Return actual target documents; every child requires its own context."""
        revision = self.observation_id
        def check_revision() -> None:
            if self.is_closed() or self.observation_id != revision:
                raise NativeError("stale_reference", "native document changed during frame enumeration")
        tree = await self._send("Page.getFrameTree")
        check_revision()
        identifiers: list[str] = []
        def collect(branch: dict[str, Any]) -> None:
            identifier = branch.get("frame", {}).get("id")
            if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                raise NativeError("protocol_error", "native frame tree contains an invalid/duplicate frame")
            identifiers.append(identifier)
            for child in branch.get("childFrames", []):
                collect(child)
        collect(tree.get("frameTree", {}))
        if identifiers[0] != self.frame_id:
            raise NativeError("stale_reference", "native main document changed during frame enumeration")
        self._frame_ids = set(identifiers)
        self._frame_contexts = {identifier: context for identifier, context in self._frame_contexts.items() if identifier in self._frame_ids}
        for identifier, frame in tuple(self._frames.items()):
            if identifier not in self._frame_ids:
                self._frames.pop(identifier)
                frame._closed = True
                self.context.cdp._task(frame._release_owner())
        documents: list[NativePage | NativeFrame] = [self]
        for identifier in identifiers[1:]:
            frame = self._frame_document(identifier)
            await frame._ensure_context()
            check_revision()
            documents.append(frame)
        return documents

    async def _send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if self._closed:
            raise NativeError("stale_reference", f"target {self.target_id} is closed")
        batch = self._native_input_batch
        if (batch is not None and batch[0] is asyncio.current_task()
                and method in {"Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText"}):
            if len(batch[1]) >= 32:
                raise NativeError("invalid_config", "native input sequence exceeds 32 events")
            batch[1].append({"method": method.split(".", 1)[1], "params": params or {}})
            return {}
        return await self.context.cdp.send(method, params, self.session_id, timeout=timeout)

    async def _initialize(self) -> None:
        await self._send("Page.enable")
        await self._send("Network.enable")
        deadline = _deadline(self, DEFAULT_TIMEOUT)
        while True:
            revision = self.observation_id, self._target_url_revision
            tree = await self._send("Page.getFrameTree", timeout=_remaining(self, deadline))
            if self.is_closed():
                raise NativeError("stale_reference", "page target was revoked while reading its frame tree")
            if revision == (self.observation_id, self._target_url_revision):
                break
        frame = tree.get("frameTree", {}).get("frame", {})
        if not isinstance(frame.get("id"), str) or not frame["id"]:
            raise NativeError("protocol_error", "native page omitted main frame identity")
        self.frame_id = frame["id"]
        if "url" in frame:
            self._url = frame["url"] + frame.get("urlFragment", "")
        def register(branch: dict[str, Any]) -> None:
            identifier = branch.get("frame", {}).get("id")
            if isinstance(identifier, str):
                self._frame_ids.add(identifier)
            for child in branch.get("childFrames", []):
                register(child)
        register(tree.get("frameTree", {}))
        await self._send("Runtime.enable")

    def _event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Page.frameNavigated":
            frame = params.get("frame", {})
            identifier = frame.get("id")
            if isinstance(identifier, str):
                self._frame_ids.add(identifier)
                self._frame_contexts.pop(identifier, None)
            self.observation_id += 1
            self._discard_frames()
            if not frame.get("parentId"):
                self._main_frame_commit += 1
                loader = frame.get("loaderId")
                self._committed_loader = loader if isinstance(loader, str) and loader else None
                self.frame_id = frame.get("id", self.frame_id)
                if "url" in frame:
                    self._url = frame["url"] + frame.get("urlFragment", "")
                self._title = ""
                self._execution_context = None
                self._frame_ids = {self.frame_id}
                self._frame_contexts.clear()
                self._site_icon = None
                self._site_icon_version = None
        elif method == "Page.frameAttached":
            self._frame_ids.add(params["frameId"])
            self.observation_id += 1
            self._discard_frames()
        elif method == "Page.frameDetached":
            self._frame_ids.discard(params.get("frameId"))
            self._frame_contexts.pop(params.get("frameId"), None)
            self.observation_id += 1
            self._discard_frames()
        elif method == "Page.navigatedWithinDocument":
            if params.get("frameId") == self.frame_id:
                self._main_frame_commit += 1
                self._committed_loader = None
                self._url = params.get("url", self._url)
            self.observation_id += 1
            self._discard_frames()
        elif method == "Runtime.executionContextCreated":
            context = params.get("context", {})
            aux = context.get("auxData", {})
            if aux.get("isDefault") and isinstance(aux.get("frameId"), str) and type(context.get("id")) is int:
                self._frame_ids.add(aux["frameId"])
                self._frame_contexts[aux["frameId"]] = context["id"]
                if aux["frameId"] == self.frame_id:
                    self._execution_context = context["id"]
        elif method in {"Runtime.executionContextsCleared", "Runtime.executionContextDestroyed"}:
            self.observation_id += 1
            self._discard_frames()
            if method.endswith("Cleared"):
                self._frame_contexts.clear()
                self._execution_context = None
            else:
                identifier = params.get("executionContextId")
                self._frame_contexts = {frame: context for frame, context in self._frame_contexts.items() if context != identifier}
                if identifier == self._execution_context:
                    self._execution_context = None
        elif method == "Network.requestWillBeSent":
            request = params.get("request", {})
            request_id = params.get("requestId")
            if request_id:
                self._request_generation += 1
                self._requests[request_id] = params.get("frameId")
            headers = {str(k).lower(): str(v) for k, v in request.get("headers", {}).items()}
            self.context._emit("request", NativeRequest(request.get("url", ""), headers, str(params.get("type", "other")).lower(), request.get("method", "GET")))
        elif method == "Network.responseReceived":
            response = params.get("response", {})
            item = NativeResponse(response.get("url", ""), int(response.get("status", 0)), {str(k).lower(): str(v) for k, v in response.get("headers", {}).items()})
            loader_id = params.get("loaderId")
            if params.get("type") == "Document" and isinstance(loader_id, str) and loader_id:
                self._responses[loader_id] = item
                if len(self._responses) > 256:
                    self._responses.pop(next(iter(self._responses)))
            if params.get("type") == "Document" and params.get("frameId") == self.frame_id:
                self._document_response = item
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            self._requests.pop(params.get("requestId"), None)
        elif method in {"Inspector.detached", "Target.detachedFromTarget"}:
            self._closed = True
            self._discard_frames()
            self._site_icon = None
            self._site_icon_version = None

    async def _evaluate_remote(self, expression: str, arg: Any, by_value: bool) -> dict[str, Any]:
        if not isinstance(expression, str):
            raise NativeError("invalid_config", "evaluate requires JavaScript source string")
        source = expression[3:] if expression.startswith("mw:") else expression
        encoded_source = json.dumps(source,ensure_ascii=False)
        if isinstance(arg, NativeJSHandle):
            if arg.page is not self:
                raise NativeError("invalid_config", "cannot evaluate a handle in another target")
            return await arg._call(
                f"(argument) => {{ const value = (0, globalThis.eval)({encoded_source}); return typeof value === 'function' ? value(argument) : value; }}",
                _MISSING,by_value)
        invocation = "" if arg is _MISSING else json.dumps(arg,ensure_ascii=False,allow_nan=False)
        source = f"(() => {{ const value = (0, globalThis.eval)({encoded_source}); return typeof value === 'function' ? value({invocation}) : value; }})()"
        params: dict[str, Any] = {"expression": source, "awaitPromise": True, "returnByValue": by_value, "userGesture": True}
        if self._execution_context is not None:
            params["contextId"] = self._execution_context
        reply = await self._send("Runtime.evaluate", params)
        if reply.get("exceptionDetails"):
            _remote_value(reply)
        return reply

    async def evaluate(self, expression: str, arg: Any = _MISSING) -> Any:
        return _remote_value(await self._evaluate_remote(expression, arg, True))

    async def evaluate_handle(self, expression: str, arg: Any = _MISSING) -> NativeJSHandle:
        revision = self.observation_id
        try:
            reply = await self._evaluate_remote(expression, arg, False)
        except ProtocolError as exc:
            if self.is_closed() or revision != self.observation_id:
                raise NativeError("stale_reference", "target changed while creating remote handle") from exc
            raise
        if not isinstance(reply.get("result"), dict):
            raise NativeError("protocol_error", "native handle evaluation omitted result")
        return await _make_handle(self, reply["result"], revision)

    async def query_selector(self, selector: str) -> NativeElementHandle | None:
        handle = await self.evaluate_handle("selector => document.querySelector(selector)", selector)
        element = handle.as_element()
        if element is None:
            await handle.dispose()
        return element

    async def title(self) -> str:
        epoch = self.observation_id
        try:
            title = await self.evaluate("document.title")
        except ProtocolError as exc:
            if self.is_closed() or epoch != self.observation_id:
                raise NativeError("stale_reference", "target changed while reading title") from exc
            raise
        if self.is_closed() or epoch != self.observation_id:
            raise NativeError("stale_reference", "target changed while reading title")
        if isinstance(title, str):
            self._title = title
        return title

    async def site_icon(self) -> NativeSiteIcon | None:
        """Read document icon metadata; unchanged revisions omit image bytes."""
        # Optional metadata must not prevent basic page startup on other backends.
        if not self._site_icons_subscribed:
            await self._send("Page.subscribeSiteIcons")
            self._site_icons_subscribed = True
        epoch = self.observation_id
        known = self._site_icon_version
        params = {} if known is None else {"knownDocument": known[0], "knownRevision": known[1]}
        reply = await self._send("Page.getSiteIcon", params)
        if self._closed or epoch != self.observation_id:
            # Navigation won the observation race. Never publish the old asset.
            return None
        document, revision = reply.get("documentId"), reply.get("revision")
        if (not isinstance(document, str) or not 0 < len(document) <= 256
                or type(revision) is not int or not 0 <= revision < 2**64):
            raise NativeError("protocol_error", "native icon omitted document revision")
        version = (document, revision)
        if reply.get("unchanged") is True:
            if known != version:
                raise NativeError("protocol_error", "native icon returned an unknown revision")
            return self._site_icon
        state = reply.get("state")
        if not isinstance(state, str) or state not in {"missing", "loading", "ready", "failed", "unsupported"}:
            raise NativeError("protocol_error", "native icon returned an invalid resource state")
        icon = None
        if state == "ready":
            digest, mime, encoded = (reply.get(key) for key in ("sha256", "mime", "data"))
            if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or not isinstance(mime, str) or mime not in {"image/png", "image/x-icon"}
                    or not isinstance(encoded, str)
                    or not 0 < len(encoded) <= ((512 * 1024 + 2) // 3) * 4):
                raise NativeError("protocol_error", "native icon exceeded its resource bounds")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise NativeError("protocol_error", "native icon contained invalid resource data") from exc
            if (not 0 < len(data) <= 512 * 1024 or hashlib.sha256(data).hexdigest() != digest
                    or (mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"))
                    or (mime == "image/x-icon" and not data.startswith(b"\x00\x00\x01\x00"))):
                raise NativeError("verification_failed", "native icon resource digest or format disagrees")
            icon = NativeSiteIcon(document, revision, digest, mime, data)
        self._site_icon_version, self._site_icon = version, icon
        return icon

    async def _wait_for_commit(self, previous_commit: int, deadline: float) -> None:
        # Context invalidation and child-frame events are not main-frame commits.
        while True:
            if self.is_closed():
                raise NativeError("stale_reference", "target closed during navigation")
            if self._main_frame_commit != previous_commit:
                return
            await asyncio.sleep(min(0.01, _remaining(self, deadline)))

    async def _finish_navigation(self, previous_commit: int, wait_until: str, deadline: float,
                                 loader_id: str | None = None) -> NativeResponse | None:
        await self._wait_for_commit(previous_commit, deadline)
        while True:
            await self.wait_for_load_state(wait_until, timeout=_remaining(self, deadline) * 1000)
            if wait_until == "commit":
                break
            revision = self._main_frame_commit, self._target_url_revision
            current_url = await self.evaluate("location.href")
            if self.is_closed():
                raise NativeError("stale_reference", "target closed during navigation")
            if revision != (self._main_frame_commit, self._target_url_revision):
                continue
            self._url = current_url
            break
        self.observation_id += 1
        if self._committed_loader is not None:
            return self._responses.get(self._committed_loader)
        if self._document_response is None and loader_id:
            self._document_response = self._responses.get(loader_id)
        return self._document_response

    @_bounded
    async def goto(self, url: str, *, wait_until: str = "load", timeout: float = DEFAULT_TIMEOUT) -> NativeResponse | None:
        _validate_load_state(wait_until)
        deadline = _deadline(self, timeout)
        self._document_response = None
        previous_commit = self._main_frame_commit
        reply = await self._send("Page.navigate", {"url": url}, timeout=_remaining(self, deadline))
        if reply.get("errorText"):
            raise NativeError("network_unavailable", str(reply["errorText"]), details=reply)
        if reply.get("isDownload"):
            raise NativeError("capability_unavailable", "navigation produced a download, not a page")
        if reply.get("frameId"):
            self.frame_id = reply["frameId"]
        return await self._finish_navigation(previous_commit, wait_until, deadline, reply.get("loaderId"))

    @_bounded
    async def wait_for_load_state(self, state: str = "load", *, timeout: float = DEFAULT_TIMEOUT) -> None:
        _validate_load_state(state)
        deadline = _deadline(self, timeout)
        if state == "commit":
            if self.is_closed():
                raise NativeError("stale_reference", "target closed while waiting for commit")
            _remaining(self, deadline)
            return
        quiet_since: float | None = None
        quiet_generation = self._request_generation
        quiet_commit = self._main_frame_commit
        while True:
            _remaining(self, deadline)
            commit = self._main_frame_commit
            ready = await self.evaluate("document.readyState")
            if self.is_closed():
                raise NativeError("stale_reference", "target closed while waiting for load state")
            if commit != self._main_frame_commit:
                quiet_since = None
                continue
            if state == "domcontentloaded" and ready in {"interactive", "complete"} or state == "load" and ready == "complete":
                return
            if state == "networkidle" and ready == "complete" and not self._requests:
                if quiet_since is None or quiet_generation != self._request_generation or quiet_commit != commit:
                    quiet_since = self.context.owner.monotonic_time()
                    quiet_generation = self._request_generation
                    quiet_commit = commit
                if self.context.owner.monotonic_time() - quiet_since >= 0.5:
                    return
            else:
                quiet_since = None
            await asyncio.sleep(min(0.05, _remaining(self, deadline)))

    async def wait_for_timeout(self, timeout: float) -> None:
        if timeout < 0:
            raise NativeError("invalid_config", "negative wait duration")
        await asyncio.sleep(timeout / 1000)

    @_bounded
    async def wait_for_url(self, url: str, *, timeout: float = DEFAULT_TIMEOUT, wait_until: str = "load") -> None:
        _validate_load_state(wait_until)
        deadline = _deadline(self, timeout)
        while True:
            _remaining(self, deadline)
            revision = self._main_frame_commit, self._target_url_revision
            current_url = await self.evaluate("location.href")
            if self.is_closed():
                raise NativeError("stale_reference", "target closed while waiting for URL")
            if revision != (self._main_frame_commit, self._target_url_revision):
                continue
            self._url = current_url
            if self._url == url or fnmatch.fnmatchcase(self._url, url):
                await self.wait_for_load_state(wait_until, timeout=_remaining(self, deadline) * 1000)
                if revision == (self._main_frame_commit, self._target_url_revision):
                    return
                continue
            await asyncio.sleep(min(0.05, _remaining(self, deadline)))

    @_bounded
    async def reload(self, *, wait_until: str = "load", timeout: float = DEFAULT_TIMEOUT) -> NativeResponse | None:
        _validate_load_state(wait_until)
        deadline = _deadline(self, timeout)
        self._document_response = None
        previous_commit = self._main_frame_commit
        await self._send("Page.reload", timeout=_remaining(self, deadline))
        return await self._finish_navigation(previous_commit, wait_until, deadline)

    @_bounded
    async def go_back(self, *, wait_until: str = "load", timeout: float = DEFAULT_TIMEOUT) -> NativeResponse | None:
        _validate_load_state(wait_until)
        deadline = _deadline(self, timeout)
        history_commit = self._main_frame_commit
        history = await self._send("Page.getNavigationHistory", timeout=_remaining(self, deadline))
        if self.is_closed() or history_commit != self._main_frame_commit:
            raise NativeError("stale_reference", "target changed while reading navigation history")
        entries, index = history.get("entries"), history.get("currentIndex")
        if not isinstance(entries, list) or type(index) is not int or not 0 <= index < len(entries):
            raise NativeError("protocol_error", "native navigation history is malformed")
        if index == 0:
            return None
        if not isinstance(entries[index - 1], dict) or type(entries[index - 1].get("id")) is not int:
            raise NativeError("protocol_error", "native navigation history is malformed")
        self._document_response = None
        previous_commit = self._main_frame_commit
        await self._send("Page.navigateToHistoryEntry", {"entryId": entries[index - 1]["id"]}, timeout=_remaining(self, deadline))
        return await self._finish_navigation(previous_commit, wait_until, deadline)

    async def bring_to_front(self) -> None:
        await self._send("Page.bringToFront")

    async def close(self) -> None:
        if self._closed:
            return
        await self._release_frames()
        reply = await self.context.cdp.send("Target.closeTarget", {"targetId": self.target_id})
        if reply.get("success") is False:
            raise NativeError("protocol_error", "native runtime refused target close")
        self._closed = True
        self._site_icon = None
        self._site_icon_version = None

    async def guarded_input(self, operation: str, args: dict[str, Any], fingerprint: str | None = None, *,
                            rebind: Callable[[], Any] | None = None,
                            displayed: tuple[int, int] | None = None,
                            channel_id: int | None = None) -> dict[str, Any] | None:
        """Dispatch once; physical channels retain independent terminal authority."""
        begin = operation in {"mouse.down", "keyboard.down"}
        terminal = operation in {"mouse.up", "keyboard.up", "input.cancel"}
        if (begin or terminal) != (channel_id is not None) or (channel_id is not None and
                (type(channel_id) is not int or not 0 < channel_id <= 2**53 - 1)):
            raise NativeError("invalid_config", "physical input requires a bounded channel identity")
        if not terminal and displayed is None and (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)):
            raise NativeError("stale_reference", "native input requires the displayed render fingerprint")
        if displayed is not None and (rebind is not None or len(displayed) != 2
                or any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in displayed)):
            raise NativeError("invalid_config", "invalid displayed native input binding")
        if channel_id is not None and rebind is not None:
            raise NativeError("invalid_config", "physical input cannot be replayed")
        if self._native_input_batch is not None:
            raise NativeError("busy", "native input sequence is already being prepared")
        if operation == "input.cancel":
            try:
                return await self._send("Input.dispatchNativeAction", {"channelId": channel_id,
                                        "channelPhase": "cancel", "events": []}, timeout=20)
            finally:
                self.mouse.buttons = 0
                self.keyboard.modifiers = 0
        family, method = operation.split(".", 1)
        if family not in {"mouse", "keyboard"} or method not in {"move", "click", "wheel", "press", "insert_text", "down", "up"}:
            raise NativeError("invalid_config", "unsupported guarded input operation")
        state = self.mouse.x, self.mouse.y, self.mouse.buttons, self.keyboard.modifiers
        events: list[dict[str, Any]] = []
        try:
            self._native_input_batch = (asyncio.current_task(), events)
            try:
                prepared = dict(args)
                if operation == "mouse.down":
                    await self.mouse.move(prepared.pop("x"), prepared.pop("y"))
                if family == "keyboard" and method in {"down", "up"}:
                    parts = prepared["key"].split("+") if prepared["key"] != "+" else ["+"]
                    if not 0 < len(parts) <= 8 or len(set(parts)) != len(parts):
                        raise NativeError("invalid_config", "invalid physical key chord")
                    for part in parts if method == "down" else reversed(parts):
                        await getattr(self.keyboard, method)(part)
                else:
                    await getattr(getattr(self, family), method)(**prepared)
            finally:
                self._native_input_batch = None
            if not events:
                raise NativeError("invalid_config", "native input sequence is empty")
            rebinds = 0
            while True:
                try:
                    guard = ({"expectedSceneRevision": displayed[0], "expectedVisualGeneration": displayed[1]}
                             if displayed is not None else {"expectedRenderFingerprint": fingerprint})
                    channel = ({"channelId": channel_id, "channelPhase": "begin" if begin else "end"}
                               if channel_id is not None else {})
                    result = await self._send("Input.dispatchNativeAction", {**guard, **channel, "events": events}, timeout=20)
                    if terminal and result.get("status") == "reset":
                        self.mouse.buttons = 0
                        self.keyboard.modifiers = 0
                    return result if terminal else None
                except ProtocolError as exc:
                    if rebind is None or rebinds >= RENDER_REBINDS or not _render_only_rejection(exc):
                        raise
                    rebinds += 1
                fingerprint = await rebind()
                if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                        or any(char not in "0123456789abcdef" for char in fingerprint)):
                    raise NativeError("stale_reference", "native input requires the displayed render fingerprint")
        except BaseException:
            self.mouse.x, self.mouse.y, self.mouse.buttons, self.keyboard.modifiers = state
            raise

    async def capture_raw_frame(self, path: str, slot: int) -> dict[str, Any]:
        """Raster directly into an LPFR producer-owned slot, with metadata-only CDP."""
        reply = await self._send("Page.captureRawFrame", {"path": path, "slot": slot}, timeout=20)
        width, height, stride = (reply.get(key) for key in ("width", "height", "stride"))
        if (reply.get("format") != "premultiplied-rgba8"
                or any(type(value) is not int for value in (width, height, stride))
                or width <= 0 or height <= 0 or stride != width * 4 or stride * height > 3840 * 2160 * 4
                or type(reply.get("dpr_milli")) is not int or not 250 <= reply["dpr_milli"] <= 8000
                or type(reply.get("theme_edge_rgba")) is not int or not 0 <= reply["theme_edge_rgba"] <= 0xffffffff
                or not isinstance(reply.get("render_fingerprint"), str) or len(reply["render_fingerprint"]) != 64
                or any(char not in "0123456789abcdef" for char in reply["render_fingerprint"])):
            raise NativeError("verification_failed", "raw native capture omitted bounded RGBA metadata")
        if (not isinstance(reply.get("focused_frame_id"), str) or not 0 < len(reply["focused_frame_id"]) <= 256
                or (reply.get("focused_element_id") is not None
                    and (not isinstance(reply["focused_element_id"], str) or not 0 < len(reply["focused_element_id"]) <= 256))):
            raise NativeError("verification_failed", "raw native capture omitted actual focused document identity")
        caret = reply.get("caret_rect")
        if (reply.get("caret_kind") not in {"native-shaped-insertion", "focused-element-bounds", "none"}
                or not isinstance(caret, list) or len(caret) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e7 for value in caret)
                or caret[2] < 0 or caret[3] < 0):
            raise NativeError("verification_failed", "raw native capture omitted explicit native caret geometry")
        return reply

    @_bounded
    async def screenshot(self, *, path: str | PurePath | None = None, full_page: bool = False, timeout: float = DEFAULT_TIMEOUT, type: str = "png", quality: int | None = None, caret: str = "initial") -> bytes:
        if caret != "initial":
            raise NativeError("capability_unavailable","native capture does not alter page caret styling")
        if type not in {"png", "jpeg"}:
            raise NativeError("capability_unavailable", f"screenshot format {type!r}")
        exports = self.context._application_root.owner.file_exports if path is not None else None
        if path is not None and exports is None:
            raise NativeError("capability_unavailable", "Host has not installed file export authority")
        params: dict[str, Any] = {"format": type, "captureBeyondViewport": full_page}
        if quality is not None:
            params["quality"] = quality
        if full_page:
            metrics = await self._send("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics.get("contentSize")
            if not isinstance(size, dict) or not all(key in size for key in ("width", "height")):
                raise NativeError("capability_unavailable", "native layout metrics missing full-page geometry")
            params["clip"] = {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}
        reply = await self._send("Page.captureScreenshot", params, timeout=timeout / 1000)
        try:
            result = base64.b64decode(reply["data"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise NativeError("protocol_error", "native screenshot omitted valid base64 data") from exc
        signature = b"\x89PNG\r\n\x1a\n" if type == "png" else b"\xff\xd8"
        if not result.startswith(signature):
            raise NativeError("protocol_error", "native screenshot has wrong image signature")
        if path is not None:
            exports.write_bytes(path, result)
        return result

    def locator(self, selector: str) -> NativeLocator:
        return NativeLocator(self, [("text", selector[5:]) if selector.startswith("text=") else ("css", selector)])

    def get_by_role(self, role: str, *, name: str | None = None, exact: bool = False) -> NativeLocator:
        return NativeLocator(self, [("role", {"role": role, "name": name, "exact": exact})])

    def get_by_text(self, text: str, *, exact: bool = False) -> NativeLocator:
        return NativeLocator(self, [("text", {"text": text, "exact": exact})])

    @_bounded
    async def inner_text(self, selector: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        return await self.locator(selector).inner_text(timeout=timeout)

    async def accessibility_snapshot(self) -> dict[str, Any]:
        reply = await self._send("Accessibility.getFullAXTree")
        if not isinstance(reply.get("nodes"), list):
            raise NativeError("capability_unavailable", "native accessibility tree not available")
        return {"page_id": self.page_id, "frame_id": self.frame_id, "source": "accessibility", "coverage": {"main_frame": True, "subframes": "native-reported", "shadow_dom": "native-reported"}, "nodes": reply["nodes"]}


class _FrameMouse:
    def __init__(self, frame: NativeFrame):
        self.frame = frame

    async def _point(self, x: float, y: float) -> tuple[float, float]:
        if self.frame.is_closed():
            raise NativeError("stale_reference", "child frame is detached/navigated")
        retained = self.frame.owner
        if retained is None:
            raise NativeError("capability_unavailable", "frame pointer actions require a retained frame-owner element")
        owner = await retained.get_property("owner")
        try:
            if owner.as_element() is None:
                raise NativeError("protocol_error", "retained frame owner is not a DOM element")
            reply = await owner.page._send("DOM.getBoxModel", {"objectId": owner._check()})
            quad = reply.get("model", {}).get("content")
            size = await owner.evaluate("el => [el.clientWidth,el.clientHeight]")
            if not isinstance(quad, list) or len(quad) != 8 or not all(isinstance(v, (int, float)) for v in quad) or not all(size):
                raise NativeError("capability_unavailable", "native frame owner geometry is unavailable")
            if abs(quad[1] - quad[3]) > 0.01 or abs(quad[0] - quad[6]) > 0.01 or abs(quad[2] - quad[4]) > 0.01 or abs(quad[5] - quad[7]) > 0.01:
                raise NativeError("capability_unavailable", "transformed iframe pointer coordinates are unsupported")
            point = quad[0] + x * (quad[2] - quad[0]) / size[0], quad[1] + y * (quad[7] - quad[1]) / size[1]
        finally:
            owner.disposed = True
            if not self.frame.root.is_closed() and owner.remote.get("objectId"):
                await self.frame.root._send("Runtime.releaseObject", {"objectId": owner.remote["objectId"]})
        if self.frame.is_closed():
            raise NativeError("stale_reference", "child frame changed while resolving pointer coordinates")
        return point

    async def move(self, x: float, y: float, **kwargs: Any) -> None:
        await self.frame.root.mouse.move(*await self._point(x, y), **kwargs)

    async def click(self, x: float, y: float, **kwargs: Any) -> None:
        await self.frame.root.mouse.click(*await self._point(x, y), **kwargs)


class NativeFrame:
    """A real child document and its session-bound execution context."""
    def __init__(self, root: NativePage, frame_id: str):
        self.root, self.frame_id = root, frame_id
        self.owner: NativeJSHandle | None = None
        self._closed, self._epoch = False, root.observation_id
        self.context, self.page_id, self.target_id, self.session_id = root.context, root.page_id, root.target_id, root.session_id
        self.mouse, self.keyboard = _FrameMouse(self), root.keyboard

    async def _release_owner(self) -> None:
        retained, self.owner = self.owner, None
        if retained is not None and not retained.disposed:
            retained.disposed = True
            if not self.root.is_closed() and retained.remote.get("objectId"):
                await self.root._send("Runtime.releaseObject", {"objectId": retained.remote["objectId"]})

    @property
    def observation_id(self) -> int:
        return self.root.observation_id

    @property
    def _execution_context(self) -> int | None:
        return self.root._frame_contexts.get(self.frame_id)

    def is_closed(self) -> bool:
        return self._closed or self.root.is_closed() or self._epoch != self.root.observation_id or self.frame_id not in self.root._frame_ids

    async def _send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if self.is_closed():
            raise NativeError("stale_reference", "child frame is detached")
        return await self.root._send(method, params, timeout=timeout)

    async def _ensure_context(self) -> None:
        if self.is_closed():
            raise NativeError("stale_reference", "child frame is detached/navigated")
        if self._execution_context is None:
            try:
                reply = await self._send("Page.createIsolatedWorld", {"frameId": self.frame_id, "worldName": "anti-trace-observation"})
            except ProtocolError as exc:
                if self.is_closed():
                    raise NativeError("stale_reference", "child document changed while resolving its execution context") from exc
                raise NativeError("capability_unavailable", "child execution context is unavailable in this CDP target session", details=exc.details) from exc
            if self.is_closed():
                raise NativeError("stale_reference", "child document changed while resolving its execution context")
            identifier = reply.get("executionContextId")
            if type(identifier) is not int:
                raise NativeError("capability_unavailable", "native runtime omitted child frame execution context")
            self.root._frame_contexts[self.frame_id] = identifier
        if self.is_closed():
            raise NativeError("stale_reference", "child document changed while resolving its execution context")

    async def _evaluate_remote(self, expression: str, arg: Any, by_value: bool) -> dict[str, Any]:
        await self._ensure_context()
        return await NativePage._evaluate_remote(self, expression, arg, by_value)

    async def evaluate(self, expression: str, arg: Any = _MISSING) -> Any:
        return _remote_value(await self._evaluate_remote(expression, arg, True))

    async def evaluate_handle(self, expression: str, arg: Any = _MISSING) -> NativeJSHandle:
        return await NativePage.evaluate_handle(self, expression, arg)

    async def query_selector(self, selector: str) -> NativeElementHandle | None:
        handle = await self.evaluate_handle("selector => document.querySelector(selector)", selector)
        element = handle.as_element()
        if element is None:
            await handle.dispose()
        return element


# Selection is observational JavaScript over the authoritative native DOM.
# No Window/Navigator/Canvas/Audio/WebGL objects are replaced by the adapter.
_QUERY = r"""(steps) => {
  const roots = scope => {
    const out = [scope];
    for (let i=0; i<out.length; i++)
      for (const el of out[i].querySelectorAll('*'))
        if (el.shadowRoot) out.push(el.shadowRoot);
    return out;
  };
  const all = (scope, css) => [...new Set(roots(scope).flatMap(r => Array.from(r.querySelectorAll(css))))];
  const norm = x => String(x || '').replace(/\s+/g, ' ').trim();
  const text = el => norm(el.innerText || el.textContent);
  const name = el => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) return norm(labelled.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' '));
    return norm(el.getAttribute('aria-label') || Array.from(el.labels || []).map(x => x.textContent).join(' ') || el.getAttribute('alt') || (el.tagName === 'INPUT' && ['button','submit','reset'].includes(el.type) ? el.value : '') || text(el) || el.getAttribute('title'));
  };
  const role = el => el.getAttribute('role') || (
    el.tagName === 'A' && el.hasAttribute('href') ? 'link' :
    ({BUTTON:'button',TEXTAREA:'textbox',SELECT:el.multiple?'listbox':'combobox',OPTION:'option',IMG:'img',H1:'heading',H2:'heading',H3:'heading',H4:'heading',H5:'heading',H6:'heading',SUMMARY:'button'}[el.tagName]) ||
    (el.tagName === 'INPUT' ? ({checkbox:'checkbox',radio:'radio',button:'button',submit:'button',reset:'button',range:'slider',number:'spinbutton',search:'searchbox',hidden:'none'}[el.type] || 'textbox') : null));
  let current = [document];
  for (const [kind, value] of steps) {
    if (kind === 'nth') { current = current[value < 0 ? current.length + value : value] ? [current[value < 0 ? current.length + value : value]] : []; continue; }
    let next = [];
    for (const scope of current) {
      if (kind === 'css') next.push(...all(scope,value));
      else if (kind === 'text') {
        const wanted = typeof value === 'string' ? {text:value,exact:false} : value;
        const matches = all(scope,'*').filter(el => wanted.exact ? text(el) === norm(wanted.text) : text(el).includes(norm(wanted.text)));
        next.push(...matches.filter(el => !matches.some(child => child !== el && el.contains(child))));
      } else if (kind === 'role') next.push(...all(scope,'*').filter(el => role(el) === value.role && (value.name == null || (value.exact ? name(el) === norm(value.name) : name(el).includes(norm(value.name))))));
      else throw new Error('Unsupported locator selection kind: ' + kind);
    }
    current = [...new Set(next)];
  }
  return current;
}"""


class NativeLocator:
    """DOM locators pierce open shadow roots, not cross-origin frames.

    Role matching covers explicit roles, common native HTML roles, aria labels
    and associated labels. It is not advertised as full accessibility-name
    computation. Native AX is separately exposed by accessibility_snapshot.
    """
    def __init__(self, page: NativePage, steps: list[tuple[str, Any]]):
        self.page, self.steps = page, steps

    @property
    def first(self) -> NativeLocator:
        return self.nth(0)

    @property
    def last(self) -> NativeLocator:
        return self.nth(-1)

    def nth(self, index: int) -> NativeLocator:
        return NativeLocator(self.page, [*self.steps, ("nth", index)])

    def locator(self, selector: str) -> NativeLocator:
        return NativeLocator(self.page, [*self.steps, ("text", selector[5:]) if selector.startswith("text=") else ("css", selector)])

    def get_by_role(self, role: str, *, name: str | None = None, exact: bool = False) -> NativeLocator:
        return NativeLocator(self.page, [*self.steps, ("role", {"role": role, "name": name, "exact": exact})])

    def get_by_text(self, text: str, *, exact: bool = False) -> NativeLocator:
        return NativeLocator(self.page, [*self.steps, ("text", {"text": text, "exact": exact})])

    async def _read(self, body: str, *, strict: bool = True) -> Any:
        steps = json.dumps(self.steps, ensure_ascii=False, allow_nan=False)
        return await self.page.evaluate(f"""(() => {{
          const els = ({_QUERY})({steps});
          if ({str(strict).lower()} && els.length > 1) throw new Error('strict locator matched ' + els.length + ' elements');
          const el = els[0]; {body}
        }})()""")

    async def count(self) -> int:
        return await self._read("return els.length;", strict=False)

    async def is_visible(self) -> bool:
        return await self._read("""if (!el) return false;
          const r=el.getBoundingClientRect(),s=getComputedStyle(el);
          return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden' && s.visibility!=='collapse';""")

    async def is_enabled(self) -> bool:
        return await self._read("return !!el && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !el.closest('fieldset[disabled]');")

    @_bounded
    async def wait_for(self, *, state: str = "visible", timeout: float = DEFAULT_TIMEOUT) -> None:
        if state not in {"attached", "detached", "visible", "hidden"}:
            raise NativeError("invalid_config", f"unknown locator wait state {state!r}")
        deadline = _deadline(self.page, timeout)
        while True:
            _remaining(self.page, deadline)
            revision = self.page.observation_id
            count = await self.count()
            if self.page.is_closed():
                raise NativeError("stale_reference", "target closed while waiting for locator")
            if revision != self.page.observation_id:
                continue
            if state == "attached" and count or state == "detached" and not count:
                return
            if state in {"visible", "hidden"}:
                visible = await self.is_visible()
                if self.page.is_closed():
                    raise NativeError("stale_reference", "target closed while waiting for locator")
                if revision != self.page.observation_id:
                    continue
                if visible == (state == "visible"):
                    return
            await asyncio.sleep(min(0.05, _remaining(self.page, deadline)))

    async def _value(self, body: str, timeout: float, *, side_effect: bool = False) -> Any:
        deadline = _deadline(self.page, timeout)
        while True:
            await self.wait_for(state="attached", timeout=_remaining(self.page, deadline) * 1000)
            revision = self.page.observation_id
            value = await self._read("if (!el) throw new Error('locator detached'); " + body)
            if self.page.is_closed():
                raise NativeError("stale_reference", "target closed while reading locator")
            if revision == self.page.observation_id:
                return value
            if side_effect:
                raise NativeError("stale_reference", "target changed during locator action; outcome may have applied")

    @_bounded
    async def inner_text(self, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        return await self._value("return el.innerText;", timeout)

    @_bounded
    async def text_content(self, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
        return await self._value("return el.textContent;", timeout)

    @_bounded
    async def input_value(self, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        return await self._value("if (!('value' in el)) throw new Error('element is not an input'); return el.value;", timeout)

    @_bounded
    async def get_attribute(self, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
        return await self._value(f"return el.getAttribute({json.dumps(name)});", timeout)

    async def all_text_contents(self) -> list[str]:
        return await self._read("return els.map(el => el.textContent || '');", strict=False)

    async def _point(self, timeout: float) -> tuple[float, float]:
        deadline = _deadline(self.page, timeout)
        await self.wait_for(state="visible", timeout=_remaining(self.page, deadline) * 1000)
        while True:
            revision = self.page.observation_id
            point = await self._read("""if (!el) return null;
              if (el.disabled || el.getAttribute('aria-disabled')==='true') return null;
              el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
              const r=el.getBoundingClientRect();
              const x=Math.max(0,r.left)+(Math.min(innerWidth,r.right)-Math.max(0,r.left))/2;
              const y=Math.max(0,r.top)+(Math.min(innerHeight,r.bottom)-Math.max(0,r.top))/2;
              if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (typeof document.elementFromPoint !== 'function') throw new Error('native DOM lacks hit-testing');
              const hit=document.elementFromPoint(x,y);
              if (hit!==el && !el.contains(hit) && hit!==el.getRootNode()?.host) return null;
              return {x,y};""")
            if self.page.is_closed():
                raise NativeError("stale_reference", "target closed while locating input point")
            if revision != self.page.observation_id:
                await self.wait_for(state="visible", timeout=_remaining(self.page, deadline) * 1000)
                continue
            if point is not None:
                return point["x"], point["y"]
            await asyncio.sleep(min(0.05, _remaining(self.page, deadline)))

    @_bounded
    async def click(self, *, timeout: float = DEFAULT_TIMEOUT, button: str = "left", click_count: int = 1, position: dict[str, float] | None = None) -> None:
        x, y = await self._point(timeout)
        if position is not None:
            box = await self._value("const r=el.getBoundingClientRect(); return {x:r.x,y:r.y};", timeout)
            x, y = box["x"] + position["x"], box["y"] + position["y"]
        await self.page.mouse.click(x, y, button=button, click_count=click_count)

    @_bounded
    async def hover(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        await self.page.mouse.move(*await self._point(timeout))

    async def _input_handle(self, timeout: float) -> NativeElementHandle:
        if isinstance(self, NativeElementHandle):
            self._check()
            return self
        await self.wait_for(state="attached", timeout=timeout)
        steps = json.dumps(self.steps, ensure_ascii=False, allow_nan=False)
        handle = await self.page.evaluate_handle(f"""(() => {{
          const els=({_QUERY})({steps});
          if (els.length!==1) throw new Error('input requires exactly one attached element');
          return els[0];
        }})()""")
        if not isinstance(handle, NativeElementHandle):
            await handle.dispose()
            raise NativeError("stale_reference", "input element detached")
        return handle

    @contextlib.asynccontextmanager
    async def _input_target(self, timeout: float) -> AsyncIterator[tuple[NativeElementHandle, NativeJSHandle]]:
        handle = await self._input_handle(timeout)
        try:
            document = await handle.evaluate_handle("el => el.ownerDocument")
            try:
                yield handle, document
            finally:
                await document.dispose()
        finally:
            if handle is not self:
                await handle.dispose()

    @_bounded
    async def fill(self, value: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        await self.wait_for(state="visible", timeout=timeout)
        async with self._input_target(timeout) as (handle, document):
            await handle._value("""if (el.disabled || el.readOnly) throw new Error('element is not editable');
              el.focus();
              if (el.tagName==='INPUT' || el.tagName==='TEXTAREA') {
                if (typeof el.select !== 'function') throw new Error('input cannot select text');
                el.select();
              } else if (el.isContentEditable) {
                const r=document.createRange();r.selectNodeContents(el);
                const s=getSelection();s.removeAllRanges();s.addRange(r);
              } else throw new Error('fill requires an input, textarea or contenteditable');
              return true;""", timeout)
            await handle._keyboard_input("insert_text" if value else "press", value or "Delete", document)
            actual = await handle._value("return el.isContentEditable ? el.textContent : el.value;", timeout)
            if actual != value:
                raise NativeError("verification_failed", "native text insertion did not produce requested editable value", details={"expected_length": len(value), "actual_length": len(actual or "")})

    @_bounded
    async def press(self, key: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        async with self._input_target(timeout) as (handle, document):
            await handle._value("el.focus(); return true;", timeout)
            await handle._keyboard_input("press", key, document)

    @_bounded
    async def press_sequentially(self, text: str, *, delay: float = 0, timeout: float = DEFAULT_TIMEOUT) -> None:
        async with self._input_target(timeout) as (handle, document):
            await handle._value("el.focus(); return true;", timeout)
            deadline = _deadline(self.page, timeout)
            for character in text:
                _remaining(self.page, deadline)
                await handle._keyboard_input("insert_text", character, document)
                if delay:
                    await asyncio.sleep(min(delay / 1000, _remaining(self.page, deadline)))

    @_bounded
    async def select_option(self, values: str | dict[str, Any] | list[Any], *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
        values = values if isinstance(values, list) else [values]
        selected = await self._value(f"""if (el.tagName!=='SELECT') throw new Error('select_option requires select');
          const wanted={json.dumps(values, ensure_ascii=False)};
          const matches=Array.from(el.options).filter((o,i)=>wanted.some(v=>typeof v==='string'?o.value===v:(v.value!=null?o.value===v.value:v.label!=null?o.label===v.label:i===v.index)));
          if (matches.length<wanted.length) return null;
          if (!el.multiple && matches.length>1) throw new Error('single select cannot choose multiple values');
          for (const option of el.options) option.selected=matches.includes(option);
          el.dispatchEvent(new Event('input',{{bubbles:true}}));el.dispatchEvent(new Event('change',{{bubbles:true}}));
          return matches.map(o=>o.value);""", timeout, side_effect=True)
        if selected is None:
            raise NativeError("invalid_config", "requested select option does not exist")
        return selected

    async def _checked(self, desired: bool, timeout: float) -> None:
        checked = await self._value("if (!('checked' in el)) throw new Error('check requires a checkbox/radio');return el.checked;", timeout)
        if checked == desired:
            return
        await self.click(timeout=timeout)
        if await self._value("return el.checked;", timeout) != desired:
            raise NativeError("verification_failed", "native click did not change checked state")

    @_bounded
    async def check(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        await self._checked(True, timeout)

    @_bounded
    async def uncheck(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        await self._checked(False, timeout)


class NativeMouse:
    def __init__(self, page: NativePage):
        self.page, self.x, self.y = page, 0.0, 0.0
        self.buttons = 0

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        if steps < 1:
            raise NativeError("invalid_config", "mouse move steps must be positive")
        start_x, start_y = self.x, self.y
        for index in range(1, steps + 1):
            self.x = start_x + (x - start_x) * index / steps
            self.y = start_y + (y - start_y) * index / steps
            await self.page._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": self.x, "y": self.y, "buttons": self.buttons, "modifiers": self.page.keyboard.modifiers})

    async def down(self, *, button: str = "left", click_count: int = 1) -> None:
        bit = {"left": 1, "right": 2, "middle": 4}.get(button)
        if bit is None:
            raise NativeError("invalid_config", f"invalid mouse button {button!r}")
        self.buttons |= bit
        await self.page._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": self.x, "y": self.y, "button": button, "buttons": self.buttons, "clickCount": click_count, "modifiers": self.page.keyboard.modifiers})

    async def up(self, *, button: str = "left", click_count: int = 1) -> None:
        bit = {"left": 1, "right": 2, "middle": 4}.get(button)
        if bit is None:
            raise NativeError("invalid_config", f"invalid mouse button {button!r}")
        self.buttons &= ~bit
        await self.page._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": self.x, "y": self.y, "button": button, "buttons": self.buttons, "clickCount": click_count, "modifiers": self.page.keyboard.modifiers})

    async def click(self, x: float, y: float, *, button: str = "left", click_count: int = 1, delay: float = 0) -> None:
        await self.move(x, y)
        for count in range(1, click_count + 1):
            await self.down(button=button, click_count=count)
            if delay:
                await asyncio.sleep(delay / 1000)
            await self.up(button=button, click_count=count)

    async def wheel(self, delta_x: float, delta_y: float, *,
                    x: float | None = None, y: float | None = None) -> None:
        if (x is None) != (y is None):
            raise NativeError("invalid_config", "wheel requires both current coordinates")
        if x is not None:
            self.x, self.y = x, y
        await self.page._send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": self.x, "y": self.y, "deltaX": delta_x, "deltaY": delta_y, "modifiers": self.page.keyboard.modifiers})


_KEYS = {
    "Enter": ("Enter", 13), "Tab": ("Tab", 9), "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46), "Escape": ("Escape", 27), "Space": ("Space", 32),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40),
    "Home": ("Home", 36), "End": ("End", 35), "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34), "Shift": ("ShiftLeft", 16),
    "Control": ("ControlLeft", 17), "Alt": ("AltLeft", 18), "Meta": ("MetaLeft", 91),
}
_MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


class NativeKeyboard:
    def __init__(self, page: NativePage, *, platform: str = "Darwin"):
        self.page, self.modifiers, self.platform = page, 0, platform

    def _key(self, key: str) -> tuple[str, str, int]:
        if key == "ControlOrMeta":
            key = "Meta" if self.platform == "Darwin" else "Control"
        if key in _KEYS:
            code, virtual = _KEYS[key]
            return (" " if key == "Space" else key), code, virtual
        if len(key) == 1:
            return key, "Key" + key.upper() if key.isascii() and key.isalpha() else "Digit" + key if key.isdigit() else "", ord(key.upper()) if key.isascii() else 0
        if re.fullmatch(r"F(?:[1-9]|1[0-2])", key):
            return key, key, 111 + int(key[1:])
        if re.fullmatch(r"Key[A-Z]", key):
            character = key[3] if self.modifiers & 8 else key[3].lower()
            return character, key, ord(key[3])
        raise NativeError("capability_unavailable", f"keyboard key {key!r} is outside the native subset")

    async def down(self, key: str) -> None:
        actual, code, virtual = self._key(key)
        self.modifiers |= _MODIFIERS.get(actual, 0)
        text = actual if len(actual) == 1 and not self.modifiers & (1 | 2 | 4) else "\r" if actual == "Enter" else ""
        params = {"type": "keyDown" if text else "rawKeyDown", "key": actual, "code": code, "windowsVirtualKeyCode": virtual, "nativeVirtualKeyCode": virtual, "modifiers": self.modifiers, "text": text, "unmodifiedText": text}
        await self.page._send("Input.dispatchKeyEvent", params)

    async def up(self, key: str) -> None:
        actual, code, virtual = self._key(key)
        self.modifiers &= ~_MODIFIERS.get(actual, 0)
        await self.page._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": actual, "code": code, "windowsVirtualKeyCode": virtual, "nativeVirtualKeyCode": virtual, "modifiers": self.modifiers})

    async def press(self, key: str, *, delay: float = 0) -> None:
        parts = key.split("+") if key != "+" else ["+"]
        held: list[str] = []
        try:
            for part in parts:
                await self.down(part)
                held.append(part)
            if delay:
                await asyncio.sleep(delay / 1000)
        finally:
            for part in reversed(held):
                await self.up(part)

    async def insert_text(self, text: str) -> None:
        await self.page._send("Input.insertText", {"text": text})

    async def type(self, text: str, *, delay: float = 0) -> None:
        for character in text:
            if character.isascii() and (character.isalnum() or character in " \n\t"):
                await self.press({"\n": "Enter", "\t": "Tab", " ": "Space"}.get(character, character))
            else:
                await self.insert_text(character)
            if delay:
                await asyncio.sleep(delay / 1000)


async def _version_endpoint(port: int, *, connections: StreamConnections) -> str:
    reader, writer = await asyncio.wait_for(
        connections.connect_tcp("127.0.0.1", port, limit=2**16), 1)
    try:
        writer.write(f"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
        if not raw.startswith(b"HTTP/1.1 200 ") and not raw.startswith(b"HTTP/1.0 200 "):
            raise OSError("native HTTP discovery not ready")
        headers = dict(line.lower().split(b":", 1) for line in raw.split(b"\r\n")[1:] if b":" in line)
        if b"content-length" in headers:
            length = int(headers[b"content-length"].strip())
            if not 0 < length <= 65536:
                raise NativeError("protocol_error", "native discovery body outside bound")
            body = await asyncio.wait_for(reader.readexactly(length), 1)
        elif headers.get(b"transfer-encoding", b"").strip() == b"chunked":
            chunks, length = [], 0
            async with asyncio.timeout(1):
                while True:
                    size = int((await reader.readline()).split(b";", 1)[0], 16)
                    if size == 0:
                        break
                    length += size
                    if length > 65536:
                        raise NativeError("protocol_error", "native discovery body exceeds bound")
                    chunks.append(await reader.readexactly(size))
                    if await reader.readexactly(2) != b"\r\n":
                        raise NativeError("protocol_error", "malformed discovery chunk")
            body = b"".join(chunks)
        else:
            try:
                body = await asyncio.wait_for(reader.readexactly(65537), 1)
            except asyncio.IncompleteReadError as exc:
                body = exc.partial
            if len(body) > 65536:
                raise NativeError("protocol_error", "native discovery body exceeds bound")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise NativeError("protocol_error", "native /json/version must return an object")
        endpoint = value.get("webSocketDebuggerUrl")
        if not isinstance(endpoint, str):
            raise NativeError("protocol_error", "native /json/version omitted websocket endpoint")
        return endpoint
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


class _SpawnRejected(NativeError):
    """Authenticated pre-admission rejection: no child or uncertain effects."""



class _Supervisor:
    """One inherited, authenticated host channel; failures make effects unknown."""
    def __init__(self, channel: Any, *, endpoints: UnixEndpoints,
                 blocking_calls: BlockingCalls, messages: UnixMessages,
                 profile_command: tuple[str, ...], leases: ProfileLeases,
                 identity: ProcessIdentity | None = None, managed_identity: ManagedPeerIdentity | None = None):
        self.socket = channel
        try:
            if identity is None or managed_identity is None:
                raise NativeError("capability_unavailable", "supervisor requires authorized identity capabilities")
            self.identity = identity
            self.managed_identity = managed_identity
            verified = managed_identity.verify_supervisor(channel)
            if not verified:
                raise NativeError("verification_failed", "supervisor peer identity is unavailable")
            channel.settimeout(25)
        except BaseException:
            channel.close()
            raise
        self.lock = asyncio.Lock()
        self.owners_enrolled = False
        self.profile_request_id = 3
        self.profile_sessions = 0
        self.fd_transfer_id = 0
        self.application_domains: tuple[int, ...] = ()
        self._reserved_domains: set[int] = set()
        # Includes issued endpoints whose spawn/exit outcome is still unknown.
        self._cdp_endpoints: set[UnixEndpoint] = set()
        self.endpoints = endpoints
        self.blocking_calls = blocking_calls
        self.messages = messages
        self.leases = leases
        self.profile_command = profile_command


    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if not 0 < len(data) <= 16384:
            raise NativeError("invalid_config", "supervisor request exceeds bound")
        self.socket.sendall(struct.pack("!I", len(data)) + data)

    def _send_file(self, message: dict[str, Any], file: ProfileLeaseCopy) -> None:
        data = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if not 0 < len(data) <= 16384:
            raise NativeError("invalid_config", "supervisor request exceeds bound")
        self.leases.send_copy(self.socket, data, file, max_bytes=16384)

    def _receive(self, count: int = 0, *, spawn_domain: int | None = None,
                 decoder: Callable | None = None, verification: bool = False) -> tuple[Any, list[Any]]:
        try:
            if decoder is not None:
                transfer_id, value, endpoints = self.managed_identity.receive_frame(self.socket)
            else:
                body, endpoints = self.messages.receive(self.socket, max_bytes=16384, max_sockets=4)
        except ValueError as exc:
            raise NativeError("protocol_error", str(exc)) from exc
        try:
            if decoder is not None:
                if endpoints and (type(transfer_id) is not int
                                  or not self.fd_transfer_id < transfer_id <= 2**53 - 1):
                    raise NativeError("protocol_error", "invalid supervisor descriptor receipt identity")
            else:
                transfer_id = None
                value = json.loads(body)
                if endpoints:
                    if (not isinstance(value, dict) or set(value) != {"fd_transfer_id", "payload"}
                            or type(value["fd_transfer_id"]) is not int
                            or not self.fd_transfer_id < value["fd_transfer_id"] <= 2**53 - 1
                            or not isinstance(value["payload"], dict)):
                        raise NativeError("protocol_error", "invalid supervisor descriptor receipt identity")
                    transfer_id, value = value["fd_transfer_id"], value["payload"]
            if (spawn_domain is not None and isinstance(value, dict)
                    and set(value) == {"op", "code", "domain"}
                    and value.get("op") == "spawn_rejected" and not endpoints
                    and type(value.get("domain")) is int and value["domain"] == spawn_domain
                    and value.get("code") in {"busy", "invalid_argument"}):
                raise _SpawnRejected("busy" if value["code"] == "busy" else "invalid_config",
                                     "supervisor rejected application before admission")
            if ((decoder is None or not endpoints) and not isinstance(value, dict)) or len(endpoints) != count:
                raise NativeError("protocol_error", "invalid supervisor response or descriptor count")
            if transfer_id is not None:
                try:
                    self._send({"op": "fd_received", "fd_transfer_id": transfer_id})
                except BaseException:
                    self.socket.close()
                    raise
                self.fd_transfer_id = transfer_id
            if decoder is not None:
                try:
                    value = decoder(value)
                except ValueError as exc:
                    raise NativeError("verification_failed" if verification else "protocol_error", str(exc)) from exc
            return value, endpoints
        except BaseException:
            for endpoint in endpoints:
                endpoint.close()
            raise

    def _start_profile(self, name: str, home: PurePath) -> tuple[int, object, Any]:
        from .profile_service import confirm_lease_escrow, receive_leases
        argv = [*self.profile_command, "4", name, str(home)]
        if any(len(arg.encode("utf-8")) >= 256 for arg in argv):
            raise NativeError("invalid_config", "profile owner launch arguments exceed sidecar bounds")
        self._send({"op": "profile_start", "argv": argv, "domain": 1})
        (reply, profile_peer), rights = self._receive(1, decoder=self.managed_identity.profile_started)
        control = rights[0]
        domains = reply.get("application_domains")
        if (not isinstance(domains, list) or not 1 <= len(domains) <= 256
                or any(type(domain) is not int or not 0 <= domain < 256
                       or domain in (1, 2, 3) for domain in domains)
                or len(set(domains)) != len(domains) or domains[0] != 0):
            control.close()
            raise NativeError("protocol_error", "invalid managed application domain inventory")
        self.application_domains = tuple(domains)
        control.settimeout(20)
        try:
            try:
                self.managed_identity.profile_ready(control, profile_peer)
            except ValueError as exc:
                raise NativeError("verification_failed", str(exc)) from exc
            leases = receive_leases(control, 1, leases=self.leases)
            try:
                for slot, lease in enumerate(leases):
                    self._send_file({"op": "escrow", "slot": slot, "domain": 1}, lease)
                    accepted, _ = self._receive()
                    if accepted != {"op": "escrowed", "slot": slot, "domain": 1}:
                        raise NativeError("protocol_error", "Profile lease escrow was not confirmed")
                confirm_lease_escrow(control, 2)
            finally:
                for lease in leases:
                    lease.close()
            return reply["launch_id"], profile_peer, control
        except BaseException:
            control.close()
            raise

    async def start_profile(self, name: str, home: PurePath) -> tuple[int, object, Any]:
        async with self.lock:
            try:
                return await self.blocking_calls.run(lambda: self._start_profile(name, home),
                                                     discard=lambda result: result[2].close())
            except BaseException as exc:
                exc.startup_stage = "profile_handshake"
                self.socket.close()
                raise

    def _spawn(self, request: dict[str, Any], profile_control: Any) -> tuple[int, object, Any]:
        from .profile_service import await_owner_ready, enroll_domain, enroll_owner
        profile_control.settimeout(20)
        self._send(request)
        enrolled = set()
        enrollment_ids = () if self.owners_enrolled else (self.profile_request_id, self.profile_request_id + 2)
        for request_id in enrollment_ids:
            owner, owner_fds = self._receive(1, decoder=self.managed_identity.owner_enrollment)
            try:
                kind, owner_launch = owner.get("kind"), owner.get("launch_id")
                if (owner.get("op") != "enroll_owner" or kind not in {"cookie", "idb"} or kind in enrolled
                        or type(owner_launch) is not int or owner_launch <= 0):
                    raise NativeError("protocol_error", "invalid profile owner identity")
                endpoint = owner_fds.pop()
                try:
                    enroll_owner(profile_control, endpoint, request_id, kind=kind, messages=self.messages)
                finally:
                    endpoint.close()
                enrolled.add(kind)
                self._send({"op": "enrolled_owner", "kind": kind, "launch_id": owner_launch})
                started, _ = self._receive()
                if (started.get("op") != "owner_started" or started.get("kind") != kind
                        or type(started.get("launch_id")) is not int
                        or started["launch_id"] != owner_launch):
                    raise NativeError("protocol_error", "invalid profile owner startup identity")
                await_owner_ready(profile_control, request_id + 1, kind=kind)
                self._send({"op": "owner_ready", "kind": kind, "launch_id": owner_launch})
            finally:
                for endpoint in owner_fds:
                    endpoint.close()
        if enrollment_ids:
            self.profile_request_id += 4
            self.owners_enrolled = True
        (reply, peer), fds = self._receive(4, spawn_domain=request["domain"],
                                           decoder=self.managed_identity.child_enrollment)
        try:
            launch_id = reply["launch_id"]
            rpc, events, idb, grant = fds
            fds = []
            try:
                enroll_domain(profile_control, rpc, self.profile_request_id, events_endpoint=events, idb_endpoint=idb,
                              grant_endpoint=grant, messages=self.messages)
                self.profile_request_id += 1
            finally:
                rpc.close()
                events.close()
                idb.close()
                grant.close()
            self._send({"op": "enrolled", "launch_id": launch_id})
            _, fds = self._receive(1, decoder=lambda message: self.managed_identity.child_ready(message, peer),
                                   verification=True)
            return launch_id, peer, fds.pop()
        finally:
            for endpoint in fds:
                endpoint.close()

    async def spawn(self, request: dict[str, Any], profile_control: Any) -> tuple[int, object, Any]:
        async with self.lock:
            try:
                return await self.blocking_calls.run(lambda: self._spawn(request, profile_control),
                                                     discard=lambda result: result[2].close())
            except _SpawnRejected:
                raise
            except BaseException:
                self.socket.close()
                raise

    def _request(self, op: str, launch_id: int) -> dict[str, Any]:
        self._send({"op": op, "launch_id": launch_id})
        reply, _ = self._receive()
        if (reply.get("op") != op or type(reply.get("launch_id")) is not int
                or reply["launch_id"] != launch_id
                or reply.get("status") not in ("running", "exited")
                or (reply["status"] == "exited" and type(reply.get("exit_status")) is not int)):
            raise NativeError("protocol_error", "invalid supervisor lifecycle response")
        return reply

    async def request(self, op: str, launch_id: int) -> dict[str, Any]:
        async with self.lock:
            try:
                return await self.blocking_calls.run(lambda: self._request(op, launch_id))
            except BaseException:
                self.socket.close()
                raise



def bind_supervisor(*, supervisor_channel: SupervisorChannel,
                    endpoints: UnixEndpoints, blocking_calls: BlockingCalls,
                    messages: UnixMessages, profile_command: tuple[str, ...],
                    leases: ProfileLeases, identity: ProcessIdentity | None = None,
                    managed_identity: ManagedPeerIdentity | None = None) -> _Supervisor:
    """Consume and verify the inherited channel without starting a Profile owner."""
    try:
        channel = supervisor_channel.take()
    except (KeyError, ValueError, OSError) as exc:
        raise NativeError("verification_failed", "supervisor channel unavailable") from exc
    return _Supervisor(channel, endpoints=endpoints, blocking_calls=blocking_calls,
                       messages=messages, profile_command=profile_command, leases=leases,
                       identity=identity, managed_identity=managed_identity)


async def start_supervised_profile(name: str, home: PurePath, *,
                                   supervisor_channel: SupervisorChannel,
                                   endpoints: UnixEndpoints,
                                   blocking_calls: BlockingCalls,
                                   messages: UnixMessages,
                                   profile_command: tuple[str, ...], leases: ProfileLeases,
                                   identity: ProcessIdentity | None = None,
                                   managed_identity: ManagedPeerIdentity | None = None) -> tuple[_Supervisor, int, Any]:
    supervisor = bind_supervisor(supervisor_channel=supervisor_channel, endpoints=endpoints,
                                 blocking_calls=blocking_calls, messages=messages,
                                 profile_command=profile_command, leases=leases,
                                 identity=identity, managed_identity=managed_identity)
    try:
        launch_id, _, control = await supervisor.start_profile(name, home)
        return supervisor, launch_id, control
    except BaseException:
        supervisor.socket.close()
        raise




class _SupervisedProcess:
    def __init__(self, supervisor: _Supervisor, launch_id: int,
                 profile_launch_id: int, profile_control: Any, profile_name: str, domain: int,
                 cdp_endpoint: UnixEndpoint):
        self.supervisor, self.launch_id = supervisor, launch_id
        self.profile_launch_id, self.profile_control = profile_launch_id, profile_control
        self.profile_name, self.domain = profile_name, domain
        self.cdp_endpoint = cdp_endpoint
        supervisor.profile_sessions += 1
        self.returncode = None
        self.stdout = self.stderr = None
        self._wait_task: asyncio.Task | None = None

    def _record_exit(self, reply: dict[str, Any]) -> None:
        # Call only with a response authenticated and correlated by _Supervisor.
        if reply["status"] == "exited" and self.returncode is None:
            self.returncode = reply["exit_status"]
            self.cdp_endpoint.dispose()
            self.supervisor._cdp_endpoints.discard(self.cdp_endpoint)
            self.supervisor._reserved_domains.discard(self.domain)

    async def stop(self) -> None:
        reply = await self.supervisor.request("stop", self.launch_id)
        self._record_exit(reply)

    async def wait(self) -> int:
        if self._wait_task is None:
            self._wait_task = asyncio.create_task(self._poll_exit())
        return await asyncio.shield(self._wait_task)

    async def _poll_exit(self) -> int:
        while self.returncode is None:
            reply = await self.supervisor.request("query", self.launch_id)
            self._record_exit(reply)
            if self.returncode is not None:
                break
            await asyncio.sleep(0.1)
        return self.returncode

    async def finish_profile(self) -> None:
        self.supervisor.profile_sessions -= 1
        if self.supervisor.profile_sessions:
            return
        self.profile_control.close()
        async with asyncio.timeout(13):
            while True:
                reply = await self.supervisor.request("query_profile", self.profile_launch_id)
                if reply["status"] == "exited":
                    if reply["exit_status"] != 0:
                        raise NativeError("protocol_error", "Profile owner exited without durable shutdown")
                    return
                await asyncio.sleep(0.1)


class NativeSession:
    def __init__(self, process: ChildProcess | _SupervisedProcess, cfg: dict[str, Any], config_path: PurePath,
                 *, proxy: str | None, identity: ProcessIdentity | None = None,
                 artifact_content: ArtifactContent | None = None,
                 connections: StreamConnections | None = None,
                 files: HostFiles | None = None,
                 random: SecureRandom | None = None):
        self.process, self.config, self.config_path = process, cfg, config_path
        self.identity, self.random = identity, random
        self.artifact_content = artifact_content
        self.connections, self.files = connections, files
        self.platform = identity.platform if identity is not None else "Darwin"
        self.file_exports: FileExports | None = None
        self.context: NativeContext | None = None
        self.receipt: dict[str, Any] = {}
        self.cdp: _CDP | None = None
        self.logs = bytearray()
        self._log_tasks: list[asyncio.Task] = []
        self._close_task: asyncio.Task | None = None
        self._closed = False
        self._secret = proxy
        for stream in (process.stdout, process.stderr):
            if stream:
                self._log_tasks.append(asyncio.create_task(self._drain(stream)))

    def monotonic_time(self) -> float:
        if self.identity is not None:
            return self.identity.monotonic_time()
        raise NativeError("capability_unavailable", "session monotonic clock requires an authorized ProcessIdentity")

    async def _drain(self, stream: ProcessOutput) -> None:
        while block := await stream.read(8192):
            self.logs.extend(block)
            if len(self.logs) > MAX_LOG:
                del self.logs[:-MAX_LOG]

    def log_text(self) -> str:
        text = self.logs.decode("utf-8", errors="replace")
        proxy = urlsplit(self._secret) if self._secret else None
        sensitive = [self._secret, self.config["seed"]]
        if proxy:
            sensitive.extend([proxy.username, proxy.password, unquote(proxy.username or ""), unquote(proxy.password or "")])
        for secret in sorted({value for value in sensitive if value}, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[private]")
        return text

    async def _close(self) -> None:
        self._closed = True
        error: BaseException | None = None
        if self.context:
            for peer in self.context._peer_sessions:
                try:
                    await peer.close()
                except Exception as exc:
                    error = error or exc
        if self.context:
            self.context.closed = True
            for page in self.context.pages:
                page._discard_frames()
                page._closed = True
                page._site_icon = None
                page._site_icon_version = None
            adoptions = tuple(self.context._adopting.values())
            for task in adoptions:
                task.cancel()
            if adoptions:
                await asyncio.gather(*adoptions, return_exceptions=True)
        try:
            if self.process.returncode is None:
                if isinstance(self.process, _SupervisedProcess):
                    await self.process.stop()
                    try:
                        await asyncio.wait_for(self.process.wait(), 13)
                    except TimeoutError as exc:
                        raise NativeError("timeout", "supervised child exit is unknown; durable flush is not verified") from exc
                else:
                    # SIGTERM uses each runtime's normal storage flush path.
                    with contextlib.suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 10)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            self.process.kill()
                        await asyncio.wait_for(self.process.wait(), 3)
                        error = NativeError("timeout", "native shutdown required SIGKILL; durable flush is not verified")
            if isinstance(self.process, _SupervisedProcess):
                try:
                    await self.process.finish_profile()
                except TimeoutError as exc:
                    raise NativeError("timeout", "Profile owner exit is unknown; durable flush is not verified") from exc
        finally:
            try:
                if self.cdp:
                    await self.cdp.close()
            finally:
                for task in self._log_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*self._log_tasks, return_exceptions=True)
                if isinstance(self.process, _SupervisedProcess):
                    if self.process.supervisor.profile_sessions == 0:
                        self.process.profile_control.close()
                        self.process.supervisor.socket.close()
        if error:
            raise error
        if self.process.returncode not in (0, _EXIT_SIGTERM):
            raise NativeError("protocol_error", f"native runtime exited {self.process.returncode}; durable flush is not verified")

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)


def _verify_receipt(cfg: dict[str, Any], receipt: dict[str, Any], digest: str) -> None:
    runtime_id = "lightpanda" if cfg["runtime"]["id"] == "mioa" else cfg["runtime"]["id"]
    expected = {"schema": 3, "id": cfg["id"], "revision": cfg["revision"], "environment_sha256": digest, "runtime": {"id": runtime_id, "version": cfg["runtime"]["version"]}, "assets": cfg["assets"]}
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise NativeError("verification_failed", f"Environment.getInfo mismatched {key}", details={"expected": value, "actual": receipt.get(key)})
    capabilities = receipt.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        raise NativeError("verification_failed", "native receipt omitted actual capability manifest")
    for feature, value in capabilities.items():
        if not isinstance(value, dict) or value.get("state") not in {"implemented", "disabled", "unsupported"} or not isinstance(value.get("reason"), str):
            raise NativeError("verification_failed", f"malformed native capability {feature!r}")
    for feature, required in cfg.get("capabilities", {}).items():
        if required.get("state") == "implemented" and capabilities.get(feature, {}).get("state") != "implemented":
            raise NativeError("incompatible_profile", f"previously implemented native capability {feature!r} is unavailable")


@_bounded
async def adopt_supervised_native(name: str, cfg: dict[str, Any], supervisor: _Supervisor,
                                  profile_launch_id: int, profile_control: Any,
                                  *, domain: int = 0, timeout: float = DEFAULT_TIMEOUT,
                                  random: SecureRandom | None = None,
                                  artifact_content: ArtifactContent | None = None,
                                  connections: StreamConnections | None = None,
                                  files: HostFiles | None = None) -> NativeSession:
    """Start and adopt the supervisor's authenticated, private Unix CDP child."""
    if random is None:
        raise NativeError("capability_unavailable", "native launch requires an authorized SecureRandom")
    if artifact_content is None or connections is None or files is None:
        raise NativeError("capability_unavailable", "native launch requires authorized artifact, stream and file capabilities")
    from . import native_spec as ns, profile
    if type(domain) is not int or domain not in supervisor.application_domains:
        raise NativeError("invalid_config", "execution domain was not admitted by supervisor")
    if domain in supervisor._reserved_domains:
        raise NativeError("busy", "execution domain is already reserved")
    validate_native_spec(cfg, name=name, artifact_content=artifact_content)
    if cfg["runtime"]["id"] not in {"mioa", "lightpanda"}:
        raise NativeError("capability_unavailable", "supervised Unix CDP requires the Mioa runtime")
    if cfg["network"].get("proxy_ref") is not None:
        raise NativeError("capability_unavailable", "supervisor has no authenticated proxy-secret delivery")
    try:
        resolved = artifact_content.resolve(cfg["storage"]["path"])
        expected = artifact_content.resolve(profile.profile_dir(name) / "data")
    except OSError as exc:
        raise NativeError("invalid_config", f"native storage path unavailable: {exc}") from exc
    if resolved != expected:
        raise NativeError("invalid_config", "native storage must be this environment's data directory")
    directory = PurePath(resolved)
    files.make_directory(directory, mode=0o700, parents=True)
    private = directory / ".native"
    files.make_directory(private, mode=0o700)
    config_path = profile.profile_dir(name) / "config.json"
    try:
        raw = files.read_bytes(config_path)
        if json.loads(raw) != cfg:
            raise NativeError("verification_failed", "launch input differs from committed profile")
    except (OSError, ValueError) as exc:
        raise NativeError("invalid_config", "committed native configuration unavailable") from exc
    engine_cfg = ns.native_engine_config(cfg)
    if engine_cfg is cfg:
        engine_path, engine_raw = config_path, raw
    else:
        engine_path = private / "engine-config.json"
        engine_raw = ns.native_config_bytes(engine_cfg)
        files.write_bytes(engine_path, engine_raw)
    digest = hashlib.sha256(engine_raw).hexdigest()
    endpoint = supervisor.endpoints.create()
    try:
        argv = [cfg["runtime"]["binary"], "serve", "--unix-socket", str(endpoint.path),
                "--environment-config", str(engine_path)]
        for resource in ("iframe", "worker", "image", "stylesheet"):
            argv += ["--load-resources", resource]
        if any(len(arg.encode("utf-8")) >= 256 for arg in argv) or len(argv) > 16:
            raise NativeError("invalid_config", "supervised launch arguments exceed sidecar bounds")
        if profile_control.fileno() < 0:
            raise NativeError("verification_failed", "Profile owner control is closed")
    except BaseException:
        endpoint.dispose()  # No spawn request has been issued.
        raise
    supervisor._cdp_endpoints.add(endpoint)
    supervisor._reserved_domains.add(domain)
    try:
        launch_id, peer, cdp_socket = await supervisor.spawn(
            {"op": "spawn", "argv": argv, "profile": name, "config_sha256": digest,
             "domain": domain, "storage_dir": str(directory)}, profile_control)
    except _SpawnRejected as exc:
        supervisor._reserved_domains.discard(domain)
        supervisor._cdp_endpoints.discard(endpoint)
        endpoint.dispose()
        exc.startup_stage = "spawn"
        raise
    except BaseException as exc:
        exc.startup_stage = "spawn"
        # Draining a cancelled worker or losing its channel is not child-exit evidence.
        exc.add_note(f"native spawn outcome unknown; CDP directory retained: {endpoint.directory}")
        raise
    process = _SupervisedProcess(supervisor, launch_id, profile_launch_id, profile_control, name, domain,
                                 endpoint)
    session = None
    startup_stage = "cdp_connect"
    try:
        session = NativeSession(process, cfg, config_path, proxy=None, identity=supervisor.identity,
                                artifact_content=artifact_content, connections=connections,
                                files=files, random=random)
        session.cdp = _CDP(await _WebSocket.connect_owned_unix(
            cdp_socket, peer, supervisor.managed_identity, random, connections=connections))
        session.cdp.execution_domain = domain
        session.cdp.execution_epoch = launch_id
        startup_stage = "receipt"
        session.receipt = await session.cdp.send("Environment.getInfo")
        _verify_receipt(cfg, session.receipt, digest)
        reply = await supervisor.request("query", launch_id)
        process._record_exit(reply)
        if reply["status"] != "running":
            raise NativeError("verification_failed", "supervised child exited before readiness")
        session.context = NativeContext(session, session.cdp)
        startup_stage = "target_initialize"
        await session.context.initialize()
        return session
    except BaseException as exc:
        exc.startup_stage = startup_stage
        with contextlib.suppress(Exception):
            if session is not None:
                await session.close()
            else:
                cdp_socket.close()
                await process.stop()
                await asyncio.wait_for(process.wait(), 13)
                await process.finish_profile()
        if endpoint in supervisor._cdp_endpoints:
            exc.add_note(f"native exit/endpoint disposal unconfirmed; CDP directory retained: {endpoint.directory}")
        raise


@_bounded
async def launch_native(name: str, cfg: dict[str, Any], headless: bool,
                        *, process_environment: ProcessEnvironment,
                        child_processes: ChildProcesses, profile_lease: ProfileLease | None = None,
                        identity: ProcessIdentity | None = None, proxy_secrets: ProxySecrets | None = None,
                        random: SecureRandom | None = None,
                        artifact_content: ArtifactContent | None = None,
                        connections: StreamConnections | None = None,
                        files: HostFiles | None = None) -> NativeSession:
    """Launch only the pinned source binary; verify raw-file receipt before use.

    Native GUI is the shared authenticated viewer over this very session. A
    headful request never launches an alternate browser or replaces screenshots.
    The manager must own its profile lease before invoking this function.
    """
    if random is None:
        raise NativeError("capability_unavailable", "native launch requires an authorized SecureRandom")
    if identity is None:
        raise NativeError("capability_unavailable", "native launch requires an authorized ProcessIdentity")
    from . import profile
    if profile_lease is None or not profile_lease.authorizes(profile.BASE, profile.sanitize(name), "engine"):
        raise NativeError("busy", "native launch requires the manager's active engine lease")
    if artifact_content is None or connections is None or files is None:
        raise NativeError("capability_unavailable", "native launch requires authorized artifact, stream and file capabilities")
    validate_native_spec(cfg, name=name, artifact_content=artifact_content)
    runtime = cfg["runtime"]["id"]
    proxy = resolve_proxy(cfg, proxy_secrets=proxy_secrets)
    try:
        resolved = artifact_content.resolve(cfg["storage"]["path"])
        expected = artifact_content.resolve(profile.profile_dir(name) / "data")
    except OSError as exc:
        raise NativeError("invalid_config", f"native storage path unavailable: {exc}") from exc
    if resolved != expected:
        raise NativeError("invalid_config", "native storage must be this environment's isolated data directory")
    directory = PurePath(resolved)
    files.make_directory(directory, mode=0o700, parents=True)
    private = directory / ".native"
    files.make_directory(private, mode=0o700)
    config_path = profile.profile_dir(name) / "config.json"
    try:
        raw = files.read_bytes(config_path)
        committed = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise NativeError("invalid_config", f"committed native configuration unavailable: {config_path}") from exc
    if committed != cfg:
        raise NativeError("verification_failed", "launch input differs from committed profile configuration")
    from . import native_spec as _ns
    engine_cfg = _ns.native_engine_config(cfg)
    if engine_cfg is not cfg:
        engine_config_path = private / "engine-config.json"
        raw_engine = _ns.native_config_bytes(engine_cfg)
        files.write_bytes(engine_config_path, raw_engine)
    else:
        engine_config_path = config_path
        raw_engine = raw
    digest = hashlib.sha256(raw_engine).hexdigest()
    # Core owns reserved-key policy; Host supplies the authorized base.
    # No inherited runtime config, private service entrypoints or proxy bypass.
    overlay = {"ANTI_TRACE_PARENT_EOF": "1"}
    if proxy and cfg["network"].get("proxy_ref"):
        overlay[cfg["network"]["proxy_ref"].removeprefix("env:")] = proxy
    environment = process_environment.derive(remove=(
        "OBSCURA_ENVIRONMENT", "OBSCURA_ENVIRONMENT_GL_BACKEND", "OBSCURA_PROXY",
        "LIGHTPANDA_ENVIRONMENT", "LIGHTPANDA_HTTP_PROXY", "MIOA_ENVIRONMENT", "MIOA_HTTP_PROXY",
        "MIOA_COOKIE_OWNER_FD", "MIOA_COOKIE_OWNER_DB", "MIOA_PROFILE_SERVICE_FD",
        "MIOA_PROFILE_EVENTS_FD", "MIOA_IDB_OWNER_FD", "MIOA_IDB_OWNER_DIR",
        "MIOA_TRUSTED_RASTER_HOST_PID", "MIOA_TRUSTED_RASTER_HOST_UID",
        "MIOA_TRUSTED_RASTER_HOST_VERSION", "MIOA_TRUSTED_RASTER_SOCKET", "MIOA_SUPERVISOR_FD",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ), overlay=overlay)
    ready_path = private / f"ready-{_secure_bytes(random, 8).hex()}.json"
    args = [cfg["runtime"]["binary"]]
    if runtime == "obscura":
        if cfg["network"].get("allow_private_network"):
            args.append("--allow-private-network")
        args += ["--environment-config", str(config_path), "serve", "--workers", "1", "--host", "127.0.0.1", "--port", "0", "--storage-dir", str(directory), "--ready-file", str(ready_path)]
        port = None
        if proxy:
            environment["OBSCURA_PROXY"] = proxy
    else:
        # Lightpanda's current server has no ready-file/port-zero contract.
        # Reserve a kernel-selected loopback port, then require the child and
        # digest-bound receipt on it; another process cannot satisfy readiness.
        port = connections.reserve_loopback_port()
        args += ["serve", "--host", "127.0.0.1", "--port", str(port), "--environment-config", str(engine_config_path)]
        for resource in ("iframe", "worker", "image", "stylesheet"):
            args += ["--load-resources", resource]
        environment.update(LIGHTPANDA_DISABLE_TELEMETRY="true", LIGHTPANDA_DISABLE_CORE_DUMP="1", MIOA_DISABLE_TELEMETRY="true", MIOA_DISABLE_CORE_DUMP="1")
    try:
        if profile_lease is None or not profile_lease.authorizes(profile.BASE, profile.sanitize(name), "engine"):
            raise NativeError("busy", "native launch requires the manager's active engine lease")
        process = await child_processes.start(args, environment=environment, profile_lease=profile_lease)
    except OSError as exc:
        raise NativeError("capability_unavailable", f"native executable could not launch: {exc}") from exc
    session = NativeSession(process, cfg, config_path, proxy=proxy, identity=identity,
                            artifact_content=artifact_content, connections=connections,
                            files=files, random=random)
    try:
        deadline = session.monotonic_time() + 30
        endpoint = None
        last_discovery_error: BaseException | None = None
        while session.monotonic_time() < deadline:
            if process.returncode is not None:
                raise NativeError("invalid_config", f"native startup exited {process.returncode}: {session.log_text()[-8192:]}")
            try:
                if runtime == "obscura":
                    if not files.exists(ready_path):
                        await asyncio.sleep(0.05)
                        continue
                    ready = json.loads(files.read_bytes(ready_path))
                    port = ready.get("port")
                    endpoint = ready.get("ws_url") or ready.get("websocket_url")
                    if not endpoint:
                        if type(port) is not int or not 1 <= port <= 65535:
                            raise NativeError("protocol_error", "native ready file omitted actual port")
                        endpoint = f"ws://127.0.0.1:{port}{ready.get('websocket_path', '/devtools/browser')}"
                else:
                    endpoint = await _version_endpoint(port, connections=connections)
                break
            except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
                last_discovery_error = exc
                await asyncio.sleep(0.05)
        if endpoint is None:
            raise NativeError("timeout", f"native startup discovery failed: {last_discovery_error}; {session.log_text()[-4096:]}")
        session.cdp = _CDP(await _WebSocket.connect(endpoint, random, connections=connections))
        session.receipt = await session.cdp.send("Environment.getInfo")
        _verify_receipt(cfg, session.receipt, digest)
        if process.returncode is not None:
            raise NativeError("verification_failed", "native process exited before readiness")
        session.context = NativeContext(session, session.cdp)
        await session.context.initialize()
        return session
    except BaseException:
        with contextlib.suppress(Exception):
            await session.close()
        raise
    finally:
        files.remove(ready_path, missing_ok=True)


def _remote_value(reply: dict[str, Any]) -> Any:
    if reply.get("exceptionDetails"):
        details = reply["exceptionDetails"]
        text = details.get("exception", {}).get("description") or details.get("text") or "JavaScript evaluation failed"
        raise NativeError("stale_reference" if "__anti_trace_stale_node__" in text else "evaluation_error", text, details=details)
    remote = reply.get("result")
    if not isinstance(remote, dict):
        raise NativeError("protocol_error", "native evaluation omitted remote result")
    if "value" in remote:
        return remote["value"]
    if remote.get("type") == "undefined" or remote.get("subtype") == "null":
        return None
    special = remote.get("unserializableValue")
    if isinstance(special, str) and special.endswith("n"):
        return int(special[:-1])
    if special in {"NaN", "Infinity", "-Infinity", "-0"}:
        return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf, "-0": -0.0}[special]
    raise NativeError("capability_unavailable", "native result is not serializable", details=remote)


class NativeJSHandle:
    """Session-bound remote object, never a page-global selector registry."""
    def __init__(self, page: NativePage, remote: dict[str, Any]):
        self.page, self.remote = page, remote
        self.frame_id, self.epoch = page.frame_id, page.observation_id
        self.disposed = False

    def _check(self) -> str:
        if self.disposed or self.page.is_closed() or self.frame_id != self.page.frame_id or self.epoch != self.page.observation_id:
            raise NativeError("stale_reference", "remote handle belongs to a disposed/navigated frame")
        object_id = self.remote.get("objectId")
        if not isinstance(object_id, str):
            raise NativeError("invalid_config", "primitive handle cannot be used as a remote object")
        return object_id

    async def _call(self, expression: str, arg: Any, by_value: bool) -> dict[str, Any]:
        object_id = self._check()
        arguments = []
        if arg is not _MISSING:
            arguments = [{"objectId": arg._check()}] if isinstance(arg, NativeJSHandle) else [{"value": arg}]
            if isinstance(arg, NativeJSHandle) and arg.page is not self.page:
                raise NativeError("invalid_config", "cannot pass a handle across target sessions")
        invocation = "this" + (",arg" if arg is not _MISSING else "")
        declaration = f"function(arg) {{ return ({expression})({invocation}); }}"
        reply = await self.page._send("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": declaration, "arguments": arguments, "returnByValue": by_value, "awaitPromise": True, "userGesture": True})
        if reply.get("exceptionDetails"):
            _remote_value(reply)
        return reply

    async def evaluate(self, expression: str, arg: Any = _MISSING) -> Any:
        return _remote_value(await self._call(expression, arg, True))

    async def evaluate_handle(self, expression: str, arg: Any = _MISSING) -> NativeJSHandle:
        revision = self.page.observation_id
        reply = await self._call(expression, arg, False)
        if not isinstance(reply.get("result"), dict):
            raise NativeError("protocol_error", "native handle evaluation omitted result")
        return await _make_handle(self.page, reply["result"], revision)

    async def get_property(self, property_name: str) -> NativeJSHandle:
        return await self.evaluate_handle("(object,key) => object[key]", property_name)

    async def json_value(self) -> Any:
        if "objectId" not in self.remote:
            return _remote_value({"result": self.remote})
        return await self.evaluate("object => object")

    def as_element(self) -> NativeElementHandle | None:
        return self if isinstance(self, NativeElementHandle) else None

    async def dispose(self) -> None:
        if self.disposed:
            return
        self.disposed = True
        if self.remote.get("objectId") and not self.page.is_closed():
            await self.page._send("Runtime.releaseObject", {"objectId": self.remote["objectId"]})


class NativeElementHandle(NativeJSHandle, NativeLocator):
    """Input methods operate on the original remote DOM node, never re-query."""
    async def _keyboard_input(self, method: str, value: str, document: NativeJSHandle) -> None:
        root = self.page.root if isinstance(self.page, NativeFrame) else self.page
        if root._native_input_batch is not None:
            raise NativeError("busy", "native input sequence is already being prepared")
        object_id = self._check()
        modifiers = root.keyboard.modifiers
        events: list[dict[str, Any]] = []
        try:
            root._native_input_batch = (asyncio.current_task(), events)
            try:
                await getattr(root.keyboard, method)(value)
            finally:
                root._native_input_batch = None
            self._check()
            await self.page._send("Input.dispatchNativeAction", {
                "expectedObjectId": object_id, "expectedDocumentId": document._check(), "events": events})
        finally:
            root.keyboard.modifiers = modifiers

    async def content_frame(self) -> NativeFrame | None:
        reply = await self.page._send("DOM.describeNode", {"objectId": self._check(), "depth": 1})
        node = reply.get("node", {})
        if str(node.get("localName") or node.get("nodeName", "")).lower() not in {"iframe", "frame"}:
            return None
        identifier = node.get("frameId")
        if identifier is None:
            return None
        if not isinstance(identifier, str) or not identifier:
            raise NativeError("protocol_error", "frame owner returned an invalid frame id")
        root = self.page.root if isinstance(self.page, NativeFrame) else self.page
        tree = await root._send("Page.getFrameTree")
        def contains(branch: dict[str, Any]) -> bool:
            return branch.get("frame", {}).get("id") == identifier or any(contains(child) for child in branch.get("childFrames", []))
        if not contains(tree.get("frameTree", {})) or identifier == root.frame_id:
            raise NativeError("capability_unavailable", "frame is not exposed in this native target session")
        root._frame_ids.add(identifier)
        frame = root._frame_document(identifier)
        await frame._ensure_context()
        if frame.owner is None:
            retained = await self.evaluate_handle("el => ({owner:el})")
            if frame.is_closed():
                await retained.dispose()
                raise NativeError("stale_reference", "frame navigated while retaining its owner")
            if frame.owner is None:
                frame.owner = retained
            else:
                await retained.dispose()
        return frame

    async def _read(self, body: str, *, strict: bool = True) -> Any:
        return await NativeJSHandle.evaluate(self, f"""el => {{
          if (!el || !el.isConnected || el.ownerDocument !== document) throw new Error('__anti_trace_stale_node__');
          const els=[el]; {body}
        }}""")

    @_bounded
    async def wait_for_element_state(self, state: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if state not in {"visible", "hidden", "enabled", "disabled", "editable", "stable"}:
            raise NativeError("capability_unavailable", f"element state {state!r}")
        if state == "stable":
            deadline = _deadline(self.page, timeout)
            previous = None
            while True:
                box = await self._read("const r=el.getBoundingClientRect();return [r.x,r.y,r.width,r.height];")
                if previous == box:
                    return
                previous = box
                await asyncio.sleep(min(0.05, _remaining(self.page, deadline)))
        elif state in {"visible", "hidden"}:
            await NativeLocator.wait_for(self, state=state, timeout=timeout)
        else:
            deadline = _deadline(self.page, timeout)
            while True:
                enabled = await self.is_enabled()
                editable = await self._read("return !el.readOnly && (el.isContentEditable || el.tagName==='INPUT' || el.tagName==='TEXTAREA');")
                if enabled == (state != "disabled") and (state != "editable" or editable):
                    return
                await asyncio.sleep(min(0.05, _remaining(self.page, deadline)))


async def _make_handle(page: NativePage | NativeFrame, remote: dict[str, Any], revision: int) -> NativeJSHandle:
    handle = NativeElementHandle(page, remote) if remote.get("subtype") == "node" else NativeJSHandle(page, remote)
    if page.is_closed() or page.observation_id != revision:
        with contextlib.suppress(Exception):
            await handle.dispose()
        raise NativeError("stale_reference", "target changed while creating remote handle")
    return handle
