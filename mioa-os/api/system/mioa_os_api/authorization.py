"""Provider-facing asynchronous authorization contracts; no implementation."""
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar

ScopeT = TypeVar("ScopeT")
GrantT = TypeVar("GrantT", covariant=True)


class RequestKind(Enum):
    CAPABILITY = "capability"
    ACTION = "action"


class Lifetime(Enum):
    ONCE = "once"
    SESSION = "session"
    PERSISTENT = "persistent"


@dataclass(frozen=True, slots=True)
class Intent(Generic[ScopeT]):
    """A request, not authority; the Host authenticates the caller separately.

    The capability-specific endpoint snapshots and validates operation and scope.
    Frozen Intent does not make a mutable scope immutable. Purpose is untrusted
    presentation text. Lifetime is an upper bound, not a promise of duration.
    """

    kind: RequestKind
    operation: str
    scope: ScopeT
    purpose: str
    lifetime: Lifetime


class AuthorizationStatus(Enum):
    GRANTED = "granted"
    DENIED = "denied"
    PROMPT = "prompt"
    UNAVAILABLE = "unavailable"


class Rejection(Enum):
    USER_DENIED = "user_denied"
    USER_CANCELLED = "user_cancelled"
    CALLER_CANCELLED = "caller_cancelled"
    EXPIRED = "expired"
    PRESENTATION_UNAVAILABLE = "presentation_unavailable"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True, slots=True)
class Allowed(Generic[GrantT]):
    """A capability or action-specific approval, as defined by the endpoint."""

    grant: GrantT


@dataclass(frozen=True, slots=True)
class NotAllowed:
    reason: Rejection


class AuthorizationRequest(Protocol[GrantT]):
    """An opaque Host-owned request bound to its authenticated caller.

    outcome returns None while pending, otherwise the immutable terminal result.
    wait suspends without occupying a thread; cancelling a waiter does not cancel
    the request. cancel requests caller cancellation and returns the actual final
    result: a concurrent user or policy decision may already have won. All waiters
    and repeated calls observe the same terminal result. Only an actually shown,
    unanswered prompt can expire. A late decision cannot revive a terminal request.
    A successful grant does not undo or guarantee any already-dispatched action.
    """

    def outcome(self) -> Allowed[GrantT] | NotAllowed | None: ...

    async def wait(self) -> Allowed[GrantT] | NotAllowed: ...

    async def cancel(self) -> Allowed[GrantT] | NotAllowed: ...


class AuthorizationEndpoint(Protocol[ScopeT, GrantT]):
    """One Host-bound principal and capability-specific authorization endpoint.

    status queries current authority without prompting; PROMPT means a decision
    may be needed, not that a prompt is displayed or will be available. begin
    validates the intent and returns a request without waiting for the user.
    The Host owns presentation and serializes decisions, cancellation and expiry.
    Caller labels do not establish identity. Implementations enforce their scope
    and check current authority on use; an OS-level grant alone never authorizes
    another MIOA principal. This protocol supplies no service or sandbox.
    """

    async def status(self, intent: Intent[ScopeT]) -> AuthorizationStatus: ...

    def begin(self, intent: Intent[ScopeT]) -> AuthorizationRequest[GrantT]: ...
