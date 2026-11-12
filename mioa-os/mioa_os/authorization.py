"""Host-assembled, event-loop-local authorization coordination.

This module supplies neither identity authentication nor a presenter, policy,
resource enforcement, platform permission, or execution isolation.
"""
import asyncio
from enum import Enum
from typing import Callable, Generic, Protocol, TypeVar

from mioa_os_api.authorization import (
    Allowed, AuthorizationStatus, Intent, NotAllowed, Rejection,
)

PrincipalT = TypeVar("PrincipalT")
ScopeT = TypeVar("ScopeT")
GrantT = TypeVar("GrantT")


class PresentationDecision(Enum):
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class AuthorizationPolicy(Protocol[PrincipalT, ScopeT, GrantT]):
    """Trusted capability-specific policy; methods must not block the event loop.

    snapshot returns an immutable Host-owned intent, including its scope, with
    no mutable aliases to caller input. validate raises for invalid intents.
    status never presents. authorize must recheck policy and return an
    endpoint-specific grant or rejection; approved is true only after a
    presenter reports a shown prompt and user approval.
    """
    def snapshot(self, principal: PrincipalT, intent: Intent[ScopeT]) -> Intent[ScopeT]: ...
    def validate(self, principal: PrincipalT, intent: Intent[ScopeT]) -> None: ...
    def status(self, principal: PrincipalT, intent: Intent[ScopeT]) -> AuthorizationStatus: ...
    def authorize(self, principal: PrincipalT, intent: Intent[ScopeT], *,
                  approved: bool) -> Allowed[GrantT] | NotAllowed: ...


class AuthorizationPresenter(Protocol[PrincipalT, ScopeT]):
    """A real Host presenter calls shown only after displaying this prompt.

    It returns a user decision or UNAVAILABLE, and should dismiss its UI when
    its coroutine is cancelled. Both callback and coroutine run on the same loop.
    """
    async def present(self, principal: PrincipalT, intent: Intent[ScopeT],
                      shown: Callable[[], None]) -> PresentationDecision: ...


class _Request(Generic[PrincipalT, ScopeT, GrantT]):
    def __init__(self, principal: PrincipalT, intent: Intent[ScopeT],
                 policy: AuthorizationPolicy[PrincipalT, ScopeT, GrantT],
                 presenter: AuthorizationPresenter[PrincipalT, ScopeT] | None,
                 timeout: float | None):
        self._loop = asyncio.get_running_loop()
        self._principal, self._intent, self._policy = principal, intent, policy
        self._presenter, self._timeout = presenter, timeout
        self._result: Allowed[GrantT] | NotAllowed | None = None
        self._completion = self._loop.create_future()
        self._shown = False
        self._timer: asyncio.TimerHandle | None = None
        self._task = self._loop.create_task(self._run())

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("authorization request belongs to another event loop")

    def outcome(self) -> Allowed[GrantT] | NotAllowed | None:
        self._check_loop()
        return self._result

    async def wait(self) -> Allowed[GrantT] | NotAllowed:
        self._check_loop()
        return await asyncio.shield(self._completion)

    async def cancel(self) -> Allowed[GrantT] | NotAllowed:
        self._check_loop()
        self._finish(NotAllowed(Rejection.CALLER_CANCELLED))
        return self._result

    def _finish(self, result: Allowed[GrantT] | NotAllowed) -> None:
        if self._result is not None:
            return
        self._result = result
        if self._timer is not None:
            self._timer.cancel()
        self._completion.set_result(result)
        if self._task is not asyncio.current_task():
            self._task.cancel()

    def _mark_shown(self) -> None:
        self._check_loop()
        if self._result is not None or self._shown:
            return
        self._shown = True
        if self._timeout is not None:
            self._timer = self._loop.call_later(
                self._timeout, self._finish, NotAllowed(Rejection.EXPIRED))

    def _authorize(self, approved: bool) -> None:
        try:
            result = self._policy.authorize(self._principal, self._intent, approved=approved)
            if not isinstance(result, (Allowed, NotAllowed)):
                raise TypeError("policy returned an invalid authorization result")
        except Exception:
            # A broken policy cannot grant authority. It is not a user denial.
            result = NotAllowed(Rejection.POLICY_DENIED)
        self._finish(result)

    async def _run(self) -> None:
        try:
            status = self._policy.status(self._principal, self._intent)
            if status is AuthorizationStatus.GRANTED:
                self._authorize(False)
            elif status is AuthorizationStatus.DENIED:
                self._finish(NotAllowed(Rejection.POLICY_DENIED))
            elif status is AuthorizationStatus.UNAVAILABLE:
                self._finish(NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
            elif status is not AuthorizationStatus.PROMPT:
                self._finish(NotAllowed(Rejection.POLICY_DENIED))
            elif self._presenter is None:
                self._finish(NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
            else:
                try:
                    decision = await self._presenter.present(
                        self._principal, self._intent, self._mark_shown)
                except Exception:
                    decision = PresentationDecision.UNAVAILABLE
                if self._result is not None:
                    return
                if not self._shown or decision is PresentationDecision.UNAVAILABLE:
                    self._finish(NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
                elif decision is PresentationDecision.APPROVED:
                    self._authorize(True)
                elif decision is PresentationDecision.DENIED:
                    self._finish(NotAllowed(Rejection.USER_DENIED))
                elif decision is PresentationDecision.CANCELLED:
                    self._finish(NotAllowed(Rejection.USER_CANCELLED))
                else:
                    self._finish(NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
        except asyncio.CancelledError:
            self._finish(NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
            raise
        except Exception:
            self._finish(NotAllowed(Rejection.POLICY_DENIED))


class HostAuthorizationEndpoint(Generic[PrincipalT, ScopeT, GrantT]):
    """Bind one Host-authenticated principal to a capability policy and presenter.

    The Host, not this object, authenticates the principal and enforces grants.
    All use is confined to the event loop on which the endpoint is constructed.
    """
    def __init__(self, principal: PrincipalT,
                 policy: AuthorizationPolicy[PrincipalT, ScopeT, GrantT],
                 presenter: AuthorizationPresenter[PrincipalT, ScopeT] | None = None,
                 *, prompt_timeout: float | None = None):
        if prompt_timeout is not None and not (0 < prompt_timeout < float("inf")):
            raise ValueError("prompt_timeout must be finite and positive")
        self._loop = asyncio.get_running_loop()
        self._principal, self._policy, self._presenter = principal, policy, presenter
        self._prompt_timeout = prompt_timeout

    def _snapshot(self, intent: Intent[ScopeT]) -> Intent[ScopeT]:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("authorization endpoint belongs to another event loop")
        snapshot = self._policy.snapshot(self._principal, intent)
        if not isinstance(snapshot, Intent):
            raise TypeError("policy returned an invalid authorization intent")
        self._policy.validate(self._principal, snapshot)
        return snapshot

    async def status(self, intent: Intent[ScopeT]) -> AuthorizationStatus:
        snapshot = self._snapshot(intent)
        result = self._policy.status(self._principal, snapshot)
        if not isinstance(result, AuthorizationStatus):
            raise TypeError("policy returned an invalid authorization status")
        return result

    def begin(self, intent: Intent[ScopeT]) -> _Request[PrincipalT, ScopeT, GrantT]:
        snapshot = self._snapshot(intent)
        return _Request(self._principal, snapshot, self._policy, self._presenter,
                        self._prompt_timeout)
