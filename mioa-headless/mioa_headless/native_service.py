"""One bounded local control socket plus event-driven invalidation notifications."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import struct
import sys
from typing import Any

from .frame_ring import FrameRing
from .service import OPERATION_TIMEOUT, ServiceError, Session, decode_request, safe_error

MAX_MESSAGE = 256 * 1024
OBSERVER_WORLD = "__panda_native_observation"
# One observer per document. It does not sample screenshots or run an idle timer.
OBSERVER = """(() => {
 const key='__pandaNativeObserver';
 if (globalThis[key]) return;
 const notify=()=>{try{globalThis.__pandaNativeInvalidated('');}catch(_){}};
 const observer=new MutationObserver(notify);
 observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
 for(const name of ['scroll','resize','input','change','focusin','focusout','selectionchange','load',
                    'animationstart','animationend','transitionrun','transitionend'])
   addEventListener(name,notify,true);
 globalThis[key]=observer;
})()"""


class NativeProtocol:
    def __init__(self, engine: Any, home: Path, socket: Path, shm: Path):
        self.engine, self.home, self.socket, self.shm = engine, home, socket, shm
        self.writer: asyncio.StreamWriter | None = None
        self.output_lock = asyncio.Lock()
        self.client: asyncio.Task | None = None
        self.notification: asyncio.Task | None = None
        self.invalidation_revision = 0
        self.invalidated_pages: set[str] = set()
        self.global_invalidation = False
        self.server = None
        self.ring = None
        self.session = None
        self.shutdown_id = None
        self.ready = False
        self.observed_pages = set()
        self.lifecycle: asyncio.Task | None = None
        self.fatal: ServiceError | None = None
        # One bounded FIFO per resolved execution domain, plus the legacy lane.
        # Control/stop have reserved slots and never queue behind native work.
        self.lanes: dict[int | None, tuple[asyncio.Queue, asyncio.Task]] = {}
        self.interrupts: dict[int | None, asyncio.Task] = {}
        self.demands: dict[int, asyncio.Task] = {}
        self.control_transition: asyncio.Task | None = None
        self.queued_terminals: set[int] = set()
        self.admitted_input_channels: dict[int, dict] = {}
        self.input_channel_high_water = 0

    async def emit(self, value: dict):
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_MESSAGE:
            raise ServiceError("protocol_error", "Native control response exceeds 256KiB.")
        async with self.output_lock:
            if self.writer is None:
                return
            self.writer.write(struct.pack("<I", len(encoded)) + encoded)
            async with asyncio.timeout(5):
                await self.writer.drain()

    async def observe_page(self, page):
        identity = (page.context.cdp.execution_domain, page.session_id)
        if identity in self.observed_pages:
            return
        await page._send("Page.subscribeNativeFrames")
        await page._send("Runtime.addBinding", {"name": "__pandaNativeInvalidated", "executionContextName": OBSERVER_WORLD})
        # The fork's worldName has real isolated V8 contexts; do not expose
        # observer globals or the notification binding to the page's main world.
        for document in await page.frame_documents():
            world = await page._send("Page.createIsolatedWorld", {"frameId": document.frame_id, "worldName": OBSERVER_WORLD})
            context_id = world.get("executionContextId")
            if type(context_id) is not int:
                raise ServiceError("verification_failed", "Native observation world omitted its context identity.")
            reply = await page._send("Runtime.evaluate", {"expression": OBSERVER, "contextId": context_id})
            if reply.get("exceptionDetails"):
                raise ServiceError("capability_unavailable", "Native isolated observation could not be installed.")
        await page._send("Page.addScriptToEvaluateOnNewDocument", {"source": OBSERVER, "worldName": OBSERVER_WORLD})
        self.observed_pages.add(identity)
        self.invalidate(self.engine.operations.page_id(page))

    def event(self, value: dict):
        method, params = value.get("method"), value.get("params", {})
        meaningful = method in {"Page.frameNavigated", "Page.navigatedWithinDocument", "Page.frameDetached",
                               "Page.frameStartedLoading", "Page.frameStoppedLoading",
                               "Page.loadEventFired", "Page.domContentEventFired", "Network.loadingFinished",
                               "Network.loadingFailed", "Target.targetCreated", "Target.targetDestroyed",
                               "Page.nativeFrameInvalidated", "Page.siteIconChanged"}
        if method == "Runtime.bindingCalled":
            meaningful = params.get("name") == "__pandaNativeInvalidated"
        if meaningful:
            if not any(value.get("_execution_domain", 0) == page.context.cdp.execution_domain
                       and value.get("_execution_epoch", 0) == getattr(page.context.cdp, "execution_epoch", 0)
                       for page in self.engine.context.pages):
                return
            page_id = None
            for page in self.engine.context.pages:
                if (value.get("sessionId") == page.session_id
                        and value.get("_execution_domain", 0) == page.context.cdp.execution_domain
                        and value.get("_execution_epoch", 0) == getattr(page.context.cdp, "execution_epoch", 0)):
                    if method in {"Page.frameNavigated", "Page.navigatedWithinDocument",
                                  "Page.frameStartedLoading", "Page.frameStoppedLoading"}:
                        page.native_navigation_revision = getattr(page, "native_navigation_revision", 0) + 1
                    if method == "Page.frameNavigated" and not params.get("frame", {}).get("parentId"):
                        # A replacement root can have a different frame id from
                        # the earlier started event; do not reuse old idle state.
                        page.native_loading = None
                    if method in {"Page.frameStartedLoading", "Page.frameStoppedLoading"} and params.get("frameId") == page.frame_id:
                        page.native_loading = method == "Page.frameStartedLoading"
                    page.native_visual_epoch = getattr(page, "native_visual_epoch", 0) + 1
                    page_id = self.engine.operations.page_id(page)
            self.invalidate(page_id)

    def invalidate(self, page_id: str | None = None):
        if page_id is None:
            self.global_invalidation = True
        elif len(self.invalidated_pages) < 100 or page_id in self.invalidated_pages:
            self.invalidated_pages.add(page_id)
        else:
            self.global_invalidation = True
        self.invalidation_revision += 1
        if self.notification is None or self.notification.done():
            self.notification = asyncio.create_task(self.notify())

    async def notify(self):
        try:
            while not self.engine.stopping.is_set():
                revision = self.invalidation_revision
                pages, self.invalidated_pages = self.invalidated_pages, set()
                global_invalidation, self.global_invalidation = self.global_invalidation, False
                if global_invalidation:
                    await self.emit({"event": "invalidated", "revision": revision})
                for page_id in pages:
                    await self.emit({"event": "invalidated", "page_id": page_id, "revision": revision})
                # Leading-edge feedback is immediate; only subsequent bursts wait.
                # Keep the task alive through the cooldown so new events coalesce.
                await asyncio.sleep(0.008)
                if revision == self.invalidation_revision:
                    return
        except (ConnectionError, OSError, TimeoutError):
            self.engine.stopping.set()

    async def respond(self, request: dict):
        response = {"id": request["id"]}
        method, params = request["method"], request["params"]
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT):
                if getattr(request.get("_source_page"), "_headless_stop_revoked", False):
                    channel = (self.session.input_channels.get(params.get("channel_id"))
                               if method == "page_broker_action" and
                               params.get("operation") in {"mouse.up", "keyboard.up", "input.cancel"} else None)
                    if channel is None or channel["page"] is not request.get("_source_page"):
                        raise ServiceError("stale_reference", "Request execution incarnation was revoked.")
                if ("_execution_domain" in request
                        and self.request_domain(request) != request["_execution_domain"]):
                    raise ServiceError("stale_reference", "Request source execution domain changed.")
                if ("_active_page_id" in request and self.engine.operations.page_id(self.engine.page())
                        != request["_active_page_id"]):
                    raise ServiceError("stale_reference", "Request source active page changed.")
                if method == "state":
                    if params:
                        raise ServiceError("invalid_config", "State takes no parameters.")
                    result = await self.session.state()
                elif method == "frame":
                    if set(params) - {"page_id"} or ("page_id" in params and
                            (not isinstance(params["page_id"], str) or not 0 < len(params["page_id"]) <= 256)):
                        raise ServiceError("invalid_config", "Frame accepts an optional page_id.")
                    revision = self.invalidation_revision
                    result = await self.session.capture(params.get("page_id"))
                    result["invalidation_revision"] = revision
                elif method == "page_resize":
                    result = await self.session.resize_page(params)
                elif method == "page_viewport":
                    result = await self.session.page_viewport(params)
                elif method == "page_demand":
                    result = await self.session.page_demand(params)
                elif method == "page_broker_frame":
                    result = await self.session.page_broker_frame(params)
                elif method == "page_bind_displayed":
                    result = await self.session.page_bind_displayed(params)
                elif method == "page_broker_action":
                    result = await self.session.page_broker_action(params)
                elif method == "page_action":
                    result = await self.session.page_action(params)
                elif method == "page_stop":
                    result = await self.session.page_stop(params)
                elif method == "resize":
                    result = await self.session.resize(params)
                elif method == "control":
                    result = await self.session.control(params)
                elif method == "new_application":
                    result = await self.session.new_application(params)
                elif method == "axtree":
                    result = await self.session.axtree(params)
                elif method == "stop":
                    result = await self.session.stop(params)
                else:
                    result = await self.session.action(params)
            if (method in {"frame", "axtree", "page_viewport"}
                    and getattr(request.get("_source_page"), "_headless_stop_revoked", False)):
                raise ServiceError("stale_reference", "Observation source was revoked by execution domain Stop.")
            response["result"] = result
        except Exception as exc:
            # A read already dispatched when explicit Stop revoked its source
            # still settles its own ID, but cannot become a healthy observation
            # or a transport-fatal failure for a surviving domain. Never rewrite
            # action errors: dispatched input can have unknown side effects.
            if (method in {"frame", "axtree", "page_viewport"}
                    and getattr(request.get("_source_page"), "_headless_stop_revoked", False)):
                exc = ServiceError("stale_reference", "Observation source was revoked by execution domain Stop.")
            response["error"] = safe_error(exc)
        try:
            await self.emit(response)
        except (ConnectionError, OSError, TimeoutError, ServiceError):
            self.engine.stopping.set()

    def request_domain(self, request: dict) -> int | None:
        method, params = request["method"], request["params"]
        if method == "page_broker_action" and params.get("operation") in {"mouse.up", "keyboard.up", "input.cancel"}:
            channel = (self.session.input_channels.get(params.get("channel_id")) or
                       self.admitted_input_channels.get(params.get("channel_id")))
            if channel is None:
                raise ServiceError("stale_reference", "Input terminal has no reservation.")
            return channel["domain"]
        if method.startswith("page_") or (method == "frame" and "page_id" in params):
            return self.session.execution_domain(params.get("page_id"))
        if method == "frame" and not self.engine.context.pages:
            return None
        if method in {"action", "resize", "stop", "axtree", "frame"}:
            return self.session.active_execution_domain()
        return None

    async def serial_requests(self, requests: asyncio.Queue, domain: int | None):
        while not self.engine.stopping.is_set():
            request = await requests.get()
            try:
                # Stop only fences its source domain. Global ownership changes
                # revoke admission in ControlState before waiting for all actions.
                fences = [task for key, task in self.interrupts.items()
                          if key is None or key == domain]
                if fences:
                    await asyncio.gather(*fences)
                await self.respond(request)
            finally:
                if "_terminal_channel" in request:
                    self.queued_terminals.discard(request["_terminal_channel"])
                    if request["_terminal_channel"] not in self.session.input_channels:
                        self.admitted_input_channels.pop(request["_terminal_channel"], None)
                requests.task_done()

    def enqueue(self, request: dict, domain: int | None) -> None:
        if domain not in self.lanes:
            if len(self.lanes) >= 8:
                raise ServiceError("busy", "Native execution lane capacity is exhausted.")
            queue = asyncio.Queue(maxsize=32)  # 16 ordinary + 16 reserved terminals
            self.lanes[domain] = (queue, asyncio.create_task(self.serial_requests(queue, domain)))
        queue = self.lanes[domain][0]
        terminal = (request["method"] == "page_broker_action" and
                    request["params"].get("operation") in {"mouse.up", "keyboard.up", "input.cancel"})
        channel_id = request["params"].get("channel_id")
        begin = (request["method"] == "page_broker_action" and
                 request["params"].get("operation") in {"mouse.down", "keyboard.down"})
        if begin:
            if type(channel_id) is not int or not 0 < channel_id <= 2**53 - 1 or channel_id <= self.input_channel_high_water:
                raise ServiceError("invalid_config", "Input channel identity was already used or invalid.")
            if len(self.admitted_input_channels) >= 16:
                raise ServiceError("busy", "Input terminal reservation capacity is exhausted.")
        if terminal:
            if type(channel_id) is not int or not 0 < channel_id <= 2**53 - 1:
                raise ServiceError("invalid_config", "Invalid input terminal identity.")
            if channel_id not in self.admitted_input_channels:
                raise ServiceError("stale_reference", "Input terminal has no reservation.")
            if channel_id in self.queued_terminals:
                raise ServiceError("invalid_config", "Input terminal is already queued.")
            if len(self.queued_terminals) >= 16:
                raise ServiceError("busy", "Input terminal queue capacity is exhausted.")
        elif queue.qsize() >= 16:
            raise ServiceError("busy", "Native execution domain queue is full.")
        try:
            queue.put_nowait(request)
        except asyncio.QueueFull:
            raise ServiceError("busy", "Native execution domain queue is full.") from None
        if terminal:
            self.queued_terminals.add(channel_id)
            request["_terminal_channel"] = channel_id
        if begin:
            self.admitted_input_channels[channel_id] = {"domain": domain, "page": request.get("_source_page")}
            self.input_channel_high_water = channel_id

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self.writer is not None or self.engine.stopping.is_set():
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            return
        self.writer = writer
        self.client = asyncio.current_task()
        # Workers are admitted lazily only for live, locally resolved domains.
        try:
            while not self.engine.stopping.is_set():
                first = await reader.readexactly(1)  # idle connections need no heartbeat
                async with asyncio.timeout(5):
                    prefix = first + await reader.readexactly(3)
                    length, = struct.unpack("<I", prefix)
                    if not 0 < length <= MAX_MESSAGE:
                        raise ServiceError("invalid_config", "Native request exceeds 256KiB.")
                    body = await reader.readexactly(length)
                response = {"id": None}
                try:
                    request = decode_request(body, native=True)
                    response["id"] = request["id"]
                    method, params = request["method"], request["params"]
                    if method == "shutdown":
                        if params:
                            raise ServiceError("invalid_config", "Shutdown takes no parameters.")
                        self.shutdown_id = request["id"]
                        self.engine.stopping.set()
                        return  # response follows durable engine shutdown
                    if method == "control":
                        if self.control_transition is not None and not self.control_transition.done():
                            raise ServiceError("busy", "Only one ownership transition may be pending.")
                        self.control_transition = asyncio.create_task(self.respond(request))
                    else:
                        domain = self.request_domain(request)
                        request["_execution_domain"] = domain
                        if domain is not None:
                            if method == "page_broker_action" and params.get("operation") in {"mouse.up", "keyboard.up", "input.cancel"}:
                                channel = (self.session.input_channels.get(params.get("channel_id")) or
                                           self.admitted_input_channels[params["channel_id"]])
                                request["_source_page"] = channel["page"]
                            elif method.startswith("page_") or (method == "frame" and "page_id" in params):
                                request["_source_page"] = self.engine.local_page_by_id(params.get("page_id"))
                            else:
                                # Active work already has an authoritative local
                                # page; never resolve it again through inventory.
                                page = self.engine.page()
                                request["_source_page"] = page
                                request["_active_page_id"] = self.engine.operations.page_id(page)
                        if method in {"stop", "page_stop"}:
                            if domain in self.interrupts and not self.interrupts[domain].done():
                                raise ServiceError("busy", "Only one stop per domain may be pending.")
                            self.interrupts[domain] = asyncio.create_task(self.respond(request))
                        elif method == "page_demand":
                            if domain in self.demands and not self.demands[domain].done():
                                raise ServiceError("busy", "Only one demand transition per domain may be pending.")
                            self.demands[domain] = asyncio.create_task(self.respond(request))
                        else:
                            self.enqueue(request, domain)
                    # Let dispatch enter its boundary before reading another
                    # complete packet already buffered on the same connection.
                    await asyncio.sleep(0)
                    continue
                except Exception as exc:
                    response["error"] = safe_error(exc)
                await self.emit(response)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError, ServiceError):
            self.engine.stopping.set()
        except asyncio.CancelledError:
            # run() owns stream teardown; a cancelled asyncio server callback
            # otherwise closes the transport before its terminal frame is sent.
            return
        finally:
            lanes = (tuple(task for _, task in self.lanes.values()) + tuple(self.interrupts.values())
                     + tuple(self.demands.values()))
            if self.control_transition is not None:
                lanes += (self.control_transition,)
            for task in lanes:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*lanes, return_exceptions=True)
            except asyncio.CancelledError:
                # run() can cancel accept while this drain is already active.
                # Finish normally so asyncio keeps the stream for the durable ack.
                await asyncio.gather(*lanes, return_exceptions=True)

    async def read_parent(self, reader: asyncio.StreamReader):
        try:
            while await reader.read(8192):
                pass  # stdin is the parent's lifetime lease, not the native wire
        finally:
            if not self.engine.stopping.is_set():
                self.fatal = ServiceError("runtime_error", "Native owner disconnected unexpectedly.")
            self.engine.stopping.set()

    async def watch_engine(self):
        process = asyncio.create_task(self.engine.native.process.wait())
        reader = self.engine.context.cdp.reader
        try:
            done, _ = await asyncio.wait({process, reader}, return_when=asyncio.FIRST_COMPLETED)
            if not self.engine.stopping.is_set():
                if hasattr(self.engine.native.process, "domain") and self.ready:
                    self.invalidate()
                    return
                message = ("Native runtime exited unexpectedly." if process in done
                           else "Native control connection closed unexpectedly.")
                self.fatal = ServiceError("runtime_error", message)
                self.engine.stopping.set()
        finally:
            # The CDP reader belongs to NativeSession.close, not this monitor.
            if not process.done():
                process.cancel()
            await asyncio.gather(process, return_exceptions=True)

    def watch_launched_engine(self):
        if self.engine.native is None:
            return
        cdp = self.engine.context.cdp
        if not self.engine.stopping.is_set() and (
            self.engine.native.process.returncode is not None or cdp.reader.done() or cdp.failure is not None
        ):
            self.fatal = ServiceError("runtime_error", "Native runtime connection ended during startup.")
            self.engine.stopping.set()
        if self.lifecycle is None:
            self.lifecycle = asyncio.create_task(self.watch_engine())

    async def start(self):
        if self.engine.stopping.is_set():
            return
        # launch_native owns cancellation/child cleanup until _launch returns.
        # Attach at that phase boundary: start() may then await navigation or
        # egress indefinitely, before the control socket is available.
        launch = self.engine._launch
        previous = self.engine.__dict__.get("_launch")
        had_override = "_launch" in self.engine.__dict__

        async def launch_and_watch(*args, **kwargs):
            try:
                return await launch(*args, **kwargs)
            finally:
                self.watch_launched_engine()

        self.engine._launch = launch_and_watch
        try:
            await self.engine.start()
        finally:
            if had_override:
                self.engine._launch = previous
            else:
                del self.engine._launch
        self.watch_launched_engine()
        if self.engine.stopping.is_set():
            return
        self.engine.context.cdp.listeners.append(self.event)
        self.engine.context.on("page", self.observe_page)
        for page in self.engine.context.pages:
            await self.observe_page(page)
        if self.engine.stopping.is_set():
            return
        self.server = await asyncio.start_unix_server(self.accept, path=str(self.socket), limit=MAX_MESSAGE + 4, backlog=1)
        os.chmod(self.socket, 0o600)
        self.watch_launched_engine()
        if self.engine.stopping.is_set():
            return
        sys.stdout.write(json.dumps({"event": "ready", "protocol": 1, "profile": self.engine.name,
            "runtime": "mioa", "home": str(self.home),
            "native_socket": str(self.socket),
            "frame_shm": str(self.shm), "frame_protocol": 2}, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        self.ready = True

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        signals = []
        failure = None
        startup = stopping = parent = None
        input_transport = None
        try:
            reader = asyncio.StreamReader()
            input_pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
            try:
                input_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), input_pipe)
            except BaseException:
                input_pipe.close()
                raise
            parent = asyncio.create_task(self.read_parent(reader))
            self.ring = FrameRing(self.shm, self.socket)
            self.session = Session(self.engine, self.ring)
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.engine.stopping.set)
                signals.append(sig)
            startup = asyncio.create_task(self.start())
            stopping = asyncio.create_task(self.engine.stopping.wait())
            await asyncio.wait({startup, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if startup.done():
                await startup
            if self.engine.stopping.is_set():
                if self.fatal is not None:
                    raise self.fatal
                if not self.ready:
                    raise ServiceError("startup_cancelled", "Native owner stopped during startup.")
            else:
                await startup
                await stopping
            failure = self.fatal
        except Exception as exc:
            failure = exc
        finally:
            # Disarm death reporting before normal shutdown closes CDP/process.
            self.engine.stopping.set()
            # Cancelling a launch can expose its final native owner in finally.
            # Drain startup before taking the monitor snapshot for cleanup.
            if startup is not None:
                if not startup.done():
                    startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
            if self.server is not None:
                try:
                    self.server.close()
                    # wait_closed also waits for accepted streams on recent
                    # Python; drain our client and send its ack first.
                except Exception as exc:
                    failure = failure or exc
            tasks = (stopping, parent, self.lifecycle, self.client, self.notification)
            for task in tasks:
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
            try:
                context = self.engine.context
                if context is not None:
                    context.remove_listener("page", self.observe_page)
                    with contextlib.suppress(ValueError):
                        context.cdp.listeners.remove(self.event)
                self.engine.control.pause("desktop_disconnected")
            except Exception as exc:
                failure = failure or exc
            try:
                await self.engine.shutdown()
            except Exception as exc:
                failure = failure or exc
            finally:
                if self.engine.native is not None:
                    try:
                        await self.engine.native.close()
                    except Exception as exc:
                        failure = failure or exc
            if self.session is not None:
                try:
                    self.session.close()
                except Exception as exc:
                    failure = failure or exc
            if self.ring is not None:
                try:
                    self.ring.close()
                except Exception as exc:
                    failure = failure or exc
            for sig in signals:
                try:
                    loop.remove_signal_handler(sig)
                except Exception as exc:
                    failure = failure or exc
            if input_transport is not None:
                try:
                    input_transport.close()
                except Exception as exc:
                    failure = failure or exc
            terminal = {"event": "error", "error": safe_error(failure, startup=not self.ready)} if failure else None
            if self.shutdown_id is not None:
                with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                    await self.emit({"id": self.shutdown_id, **({"error": safe_error(failure)} if failure else {"result": {"status": "ok", "flushed": True}})})
            elif terminal is not None:
                with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                    await self.emit(terminal)
            if self.writer is not None:
                self.writer.close()
                with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                    async with asyncio.timeout(2):
                        await self.writer.wait_closed()
            if self.server is not None:
                try:
                    async with asyncio.timeout(2):
                        await self.server.wait_closed()
                except Exception as exc:
                    failure = failure or exc
        if failure is not None:
            terminal = {"event": "error", "error": safe_error(failure, startup=not self.ready)}
            with contextlib.suppress(BrokenPipeError, OSError):
                sys.stdout.write(json.dumps(terminal, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        return int(failure is not None)
