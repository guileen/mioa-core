"""Single-loop ownership lease for the owned native runtime.

Derived verbatim from `cli/anti_trace/viewer.py` ControlError + ControlState
(lines 61-322). The ATB HTTP ViewerServer, its routes, authentication and
desktop-facing UI are deliberately excluded; this module owns only the
authorization/ownership state that the headless transport and native dispatch
share.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class ControlError(RuntimeError):
    """A typed manager error; ``code`` is safe for a JSON error response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.side_effects = "none"


class ControlState:
    """Single-loop lease state; a check alone does NOT reserve an action boundary.

    ``begin_run`` and ``finish_run`` are synchronous. Ownership transitions are
    asynchronous and wait for the previously accepted action to finish. Call
    ``pause`` after uncertain/cancelled side effects. No timeout or disconnect
    silently transfers ownership back to the agent.
    """

    def __init__(self, *, page_id: str | None = None, frame_id: str | None = None):
        self.owner = "agent"
        self.owner_id: str | None = None
        self.run_id: str | None = None
        self.page_id = page_id
        self.frame_id = frame_id
        self.observation_id: str | None = None
        self.run_revision = 0
        self.owner_revision = 0
        self.page_revision = 0
        self.observation_revision = 0
        self.reason: str | None = None
        self._pending_human: str | None = None
        self._condition = asyncio.Condition()
        self._active_task: asyncio.Task[Any] | None = None
        self._active_lease: tuple[str, str] | None = None
        self._revision_event = asyncio.Event()

    def snapshot(self) -> dict[str, Any]:
        return {"owner": self.owner, "owner_id": self.owner_id,
                "run_id": self.run_id, "run_revision": self.run_revision,
                "owner_revision": self.owner_revision,
                "page_id": self.page_id, "frame_id": self.frame_id,
                "page_revision": self.page_revision,
                "observation_id": self.observation_id,
                "observation_revision": self.observation_revision,
                "pending_human": self._pending_human is not None,
                "in_flight": self._active_task is not None, "reason": self.reason}

    def begin_run(self, run_id: str, *, page_id: str | None = None,
                  frame_id: str | None = None) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            raise ControlError("invalid_config", "run_id is required")
        if self.owner != "agent" or self._pending_human:
            raise ControlError("paused", "人工接管或暂停中；请显式恢复 Agent")
        if self.run_id is not None or self._active_task is not None:
            raise ControlError("busy", "已有运行或动作尚未结束")
        self.run_id = run_id
        self.owner_id = run_id
        self.run_revision += 1
        self.owner_revision += 1
        self.reason = None
        self.invalidate_observation()
        if page_id is not None:
            self.set_page(page_id, frame_id)
        self._signal_revision()
        return self.snapshot()

    def finish_run(self, run_id: str) -> dict[str, Any]:
        if run_id != self.run_id:
            raise ControlError("stale_reference", "运行版本已改变")
        if self._active_task is not None:
            raise ControlError("busy", "动作仍在执行，不能释放运行")
        self.run_id = None
        self.run_revision += 1
        if self.owner == "agent":
            self.owner_id = None
            self.owner_revision += 1
        self.invalidate_observation()
        self._signal_revision()
        return self.snapshot()

    def set_page(self, page_id: str, frame_id: str | None) -> None:
        if (page_id, frame_id) != (self.page_id, self.frame_id):
            self.page_id, self.frame_id = page_id, frame_id
            self.page_revision += 1
            self.invalidate_observation()

    def set_observation(self, page_id: str, frame_id: str,
                        observation_id: str) -> dict[str, Any]:
        if not all(isinstance(value, str) and value for value in
                   (page_id, frame_id, observation_id)):
            raise ControlError("invalid_config", "页面、frame 和观察 ID 必须来自实际运行时")
        self.set_page(page_id, frame_id)
        if observation_id != self.observation_id:
            self.observation_id = observation_id
            self.observation_revision += 1
        return self.snapshot()

    def invalidate_observation(self) -> None:
        self.observation_id = None
        self.observation_revision += 1

    def _signal_revision(self) -> None:
        self._revision_event.set()
        self._revision_event = asyncio.Event()

    async def wait_agent(self, run_id: str | None) -> dict[str, Any]:
        """Cancellable lease wait; the runner's deadline bounds this await.

        A real run supplies its ID. ``None`` is reserved for manager operations
        outside a script. A handover does not fail a running script, but a
        changed/finished run cannot acquire the next run's writing lease.
        """
        while True:
            event = self._revision_event
            if run_id is not None and run_id != self.run_id:
                raise ControlError("stale_reference", "等待中的运行已经结束或改变")
            if self.owner == "agent" and self._pending_human is None:
                return self.snapshot()
            await event.wait()

    def pause(self, reason: str = "explicit_pause") -> dict[str, Any]:
        self.owner, self.owner_id = "paused", None
        self._pending_human = None
        self.owner_revision += 1
        self.reason = reason
        self.invalidate_observation()
        self._signal_revision()
        return self.snapshot()

    async def request_human(self, viewer_id: str) -> dict[str, Any]:
        if not isinstance(viewer_id, str) or not viewer_id:
            raise ControlError("invalid_config", "viewer_id is required")
        async with self._condition:
            if self.owner == "human":
                if self.owner_id != viewer_id:
                    raise ControlError("busy", "另一查看器持有人工控制权")
                return self.snapshot()
            if self._pending_human is not None:
                raise ControlError("busy", "人工接管请求正在等待动作边界")
            self.pause("waiting_for_action_boundary")
            self._pending_human = viewer_id
            try:
                await self._condition.wait_for(lambda: self._active_task is None)
                if self._pending_human != viewer_id:
                    raise ControlError("paused", "接管请求已断开或取消")
                self.owner, self.owner_id = "human", viewer_id
                self._pending_human = None
                self.owner_revision += 1
                self.reason = None
                self.invalidate_observation()
                self._signal_revision()
                return self.snapshot()
            except BaseException:
                if self._pending_human == viewer_id:
                    self.pause("handover_interrupted")
                raise

    async def _resume(self, run_id: str | None, reason: str) -> dict[str, Any]:
        async with self._condition:
            if run_id is not None and run_id != self.run_id:
                raise ControlError("stale_reference", "不能恢复已结束的运行")
            self.pause(reason)
            transition = self.owner_revision
            await self._condition.wait_for(lambda: self._active_task is None)
            if transition != self.owner_revision:
                raise ControlError("busy", "恢复期间控制权再次改变")
            self.owner, self.owner_id = "agent", self.run_id
            self.owner_revision += 1
            self.reason = None
            self.invalidate_observation()
            self._signal_revision()
            self._condition.notify_all()
            return self.snapshot()

    async def resume_agent(self, run_id: str | None = None) -> dict[str, Any]:
        return await self._resume(run_id, "explicit_resume")

    async def reclaim(self, run_id: str | None = None) -> dict[str, Any]:
        return await self._resume(run_id, "explicit_reclaim")

    async def disconnect(self, viewer_id: str) -> dict[str, Any]:
        async with self._condition:
            if ((self.owner == "human" and self.owner_id == viewer_id)
                    or self._pending_human == viewer_id):
                self.pause("viewer_disconnected")
                self._condition.notify_all()
            return self.snapshot()

    def check_write(self, actor: str, owner_id: str, *,
                    page_id: str | None = None, frame_id: str | None = None,
                    observation_id: str | None = None,
                    owner_revision: int | None = None) -> dict[str, Any]:
        if actor not in {"agent", "human"} or not isinstance(owner_id, str) or not owner_id:
            raise ControlError("invalid_config", "写操作必须声明 Agent 或人工所有者")
        if self.owner != actor or self._pending_human is not None:
            raise ControlError("paused", "当前控制权不允许此写操作")
        if actor == "human" and self.owner_id != owner_id:
            raise ControlError("busy", "此查看器没有人工控制权")
        if actor == "agent" and self.run_id is not None and owner_id != self.run_id:
            raise ControlError("busy", "此动作不属于当前运行")
        self._check_versions(page_id, frame_id, observation_id, owner_revision)
        return self.snapshot()

    def _check_versions(self, page_id: str | None, frame_id: str | None,
                        observation_id: str | None, owner_revision: int | None) -> None:
        for expected, actual in ((page_id, self.page_id), (frame_id, self.frame_id),
                                 (observation_id, self.observation_id),
                                 (owner_revision, self.owner_revision)):
            if expected is not None and expected != actual:
                raise ControlError("stale_reference", "页面、画面或控制版本已改变，请重新观察")

    @asynccontextmanager
    async def action(self, actor: str, owner_id: str, **versions: Any) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is self._active_task:
            if self._active_lease != (actor, owner_id):
                raise ControlError("busy", "嵌套动作不能改变控制租约")
            self._check_versions(versions.get("page_id"), versions.get("frame_id"),
                                 versions.get("observation_id"), versions.get("owner_revision"))
            yield
            return
        expected_run = self.run_id if actor == "agent" else None
        if expected_run is not None and owner_id != expected_run:
            raise ControlError("busy", "此动作不属于当前运行")
        while True:
            if actor == "agent":
                await self.wait_agent(expected_run)
            async with self._condition:
                if actor == "agent" and (self.owner != "agent" or self._pending_human):
                    continue
                self.check_write(actor, owner_id, **versions)
                if self._active_task is not None:
                    await self._condition.wait()
                    continue
                self._active_task, self._active_lease = task, (actor, owner_id)
                break
        try:
            yield
        except asyncio.CancelledError:
            self.pause("action_cancelled_side_effects_unknown")
            raise
        finally:
            async with self._condition:
                self._active_task = self._active_lease = None
                self._condition.notify_all()

    @asynccontextmanager
    async def observation(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is self._active_task:
            yield
            return
        async with self._condition:
            await self._condition.wait_for(lambda: self._active_task is None)
            self._active_task, self._active_lease = task, ("observation", "")
        try:
            yield
        finally:
            async with self._condition:
                self._active_task = self._active_lease = None
                self._condition.notify_all()
