"""Owned headless engine: one native runtime process, one authorized page lease.

Derived from `cli/anti_trace/engine.py` (Engine native path). Extracted for core
ownership:

- native launch/runtime selection, navigation guarding, page selection, frame
  acquisition and shutdown are preserved;
- the ATB manager UDS, CLI action dispatcher, script runner, HTTP viewer and
  Camoufox/Playwright adapter stay in ATB as external product adapters;
- `_launch` remains the exact method name/async boundary that
  `mioa_headless.native_service` wraps to observe startup;
- initial navigation uses the same `page.goto(wait_until="domcontentloaded",
  timeout=20000)` call that the prior `actions.run("goto")` path issued.

`Engine.start` no longer serves the ATB manager socket or HTTP viewer. A product
adapter that owns those resources opens them first, then passes
``state_extra={"socket": ...}`` so the single atomic ``READY`` write carries the
live socket path. ``Engine.activate`` is the public context-attachment seam for
a product-owned launch path (for example a Playwright/Firefox context).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Mapping

from . import net, profile
from .control import ControlError, ControlState
from .operations import OPERATION_SPECS, ScriptOperations
from .refs import ActionError, RefStore

logger = logging.getLogger("mioa_core.engine")


class Engine:
    def __init__(self, name: str, headless: bool, initial_url: str | None, verify: bool = True):
        self.name,self.initial_url,self.verify = name,initial_url,verify
        self.headless,self.gui = True,not headless
        self.native: Any = None
        self.context: Any = None
        self.active: Any = None
        self.stopping = asyncio.Event()
        self.workdir = profile.profile_dir(name)
        self.control = ControlState()
        self.operations: ScriptOperations | None = None
        self.refs = RefStore(lambda page:self.operations.page_id(page),
                             lambda:(self.control.run_revision,self.control.owner_revision))
        self.receipt: dict[str, Any] = {}
        self.failed_documents: dict[Any,int] = {}
        self.failure_sequence = 0
        self.human_selection_epoch = 0
        self.run_selection_epoch = 0

    def page(self) -> Any:
        pages = self.context.pages if self.context else []
        if self.active is not None and not self.active.is_closed():
            return self.active
        self.active = pages[-1] if pages else None
        if self.active is None:
            raise ActionError("no page available",code="stale_reference")
        self.control.set_page(self.operations.page_id(self.active),
                              self.refs.frame_id(getattr(self.active,"main_frame",self.active)))
        return self.active

    async def refresh_pages(self) -> None:
        refresh = getattr(self.context,"refresh_pages",None)
        if refresh:
            await refresh()

    async def _navigation(self, page: Any, execute: Any) -> Any:
        previous = self.failed_documents.get(page)
        try:
            return await execute()
        except Exception as exc:
            if self.failed_documents.get(page) != previous:
                error = ActionError("document transport failed; no navigation retry was attempted",code="network_unavailable")
                error.side_effects = "unknown"
                raise error from exc
            raise

    async def activate(self) -> None:
        """Bind a launched context to this engine's page lease and operations."""
        self.active = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.operations = ScriptOperations(self.context,active_page=self.page,axtree=self._axtree)
        self.control.set_page(self.operations.page_id(self.active),
                              self.refs.frame_id(getattr(self.active,"main_frame",self.active)))

    async def _launch(self, cfg: dict[str, Any]) -> None:
        runtime = (cfg.get("runtime") or {}).get("id")
        if runtime in profile.NATIVE_RUNTIMES:
            from .native import launch_native
            self.native = await launch_native(self.name,cfg,self.headless)
            self.context, self.receipt = self.native.context,self.native.receipt
        else:
            raise profile.ProfileError("incompatible_profile: explicit migration to a pinned schema3 runtime is required")
        await self.activate()

    async def _observe_egress(self) -> dict[str, Any] | None:
        probe = await self.context.new_page()
        try:
            return await net.browser_geography(probe)
        finally:
            await probe.close()

    async def start(self, *, state_extra: Mapping[str, Any] | None = None) -> None:
        cfg = profile.require(self.name)
        await self._launch(cfg)
        observed = None
        network = cfg.get("network") or {}
        if self.verify:
            try:
                observed = await self._observe_egress()
            except Exception as exc:
                if (network.get("proxy_ref") or network.get("proxy")) and network.get("egress_policy","strict") == "strict":
                    raise ActionError("declared proxy egress could not be verified",code="network_unavailable") from exc
                logger.warning("egress observation unavailable: %s",type(exc).__name__)
        if self.initial_url:
            await self._navigation(self.page(),lambda:self.page().goto(
                self.initial_url,wait_until="domcontentloaded",timeout=20000))
        resolved = cfg.get("resolved") or {}
        coherent = None if not observed else observed.get("timezone") == resolved.get("timezone")
        state = {
            "pid":os.getpid(),"process_start":profile.process_start(os.getpid()),"status":"READY",
            "started_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            "headless":self.headless,"gui":self.gui,"observed_egress":(observed or {}).get("ip"),
            "egress_consistent":coherent,"timezone":resolved.get("timezone"),"locale":resolved.get("locale"),
            "url":self.page().url,"runtime":(cfg.get("runtime") or {}).get("id"),
            "native_pid":self.native.pid if self.native else None,"receipt":self.receipt,
        }
        if state_extra:
            overlap = set(state_extra) & set(state)
            if overlap:
                raise ValueError(f"state_extra must not override engine-owned state keys: {sorted(overlap)}")
            state.update(state_extra)
        profile.save_state(self.name,state)
        logger.info("ready pid=%s runtime=%s",os.getpid(),(cfg.get("runtime") or {}).get("id"))

    async def _axtree(self, page: Any, **kwargs: Any) -> dict[str, Any]:
        """Capture the bounded observation owned by this engine's RefStore.

        `RefStore.capture` holds the snapshot handle, masks password text/value
        and emits opaque references; publishing the returned identity here is
        what lets ControlState reject writes from a superseded observation.
        """
        result = await self.refs.capture(page,**kwargs)
        self.control.set_observation(result["page_id"],result["frame_id"],result["observation_id"])
        return result

    async def _select(self, page: Any) -> None:
        if page is not self.active:
            await self.refs.clear()
            self.active = page
            self.control.set_page(self.operations.page_id(page),
                                  self.refs.frame_id(getattr(page,"main_frame",page)))
            await page.bring_to_front()

    async def _viewer_execute(self, operation: str, args: dict[str, Any]) -> Any:
        await self.refresh_pages()
        if operation == "viewer.pages":
            return {"pages":[{"page_id":self.operations.page_id(page),"url":page.url,"title":await page.title()}
                             for page in self.context.pages],"page_id":self.operations.page_id(self.page())}
        if operation == "viewer.frame":
            return await self.refs.viewport(self.page())
        page = self.page()
        try:
            await self.refs.validate(self.refs.visual,page)
        except ActionError as exc:
            raise ControlError(exc.code,str(exc)) from exc
        if operation == "context.select_page":
            target = self.operations.page(args["target_page_id"])
            if target is not page:
                self.human_selection_epoch += 1
            await self._select(target)
        elif operation == "page.focus":
            await page.bring_to_front()
        else:
            family,method = operation.split(".",1)
            values = [args[key] for key in OPERATION_SPECS[operation]]
            options = {key:value for key,value in args.items() if key not in {
                "page_id","frame_id","observation_id","owner_revision","viewer_id","image_id",*OPERATION_SPECS[operation]}}
            await getattr(getattr(page,family),method)(*values,**options)
        self.control.invalidate_observation()
        return {"status":"ok","operation":operation}

    async def shutdown(self) -> None:
        await self.refs.clear()
        try:
            if self.native:
                await self.native.close()
            elif self.context:
                await self.context.close()
        finally:
            self.context = self.active = None
            profile.clear_state(self.name)
