"""Host coordinator behavior; these tests do not assert OS isolation."""
import asyncio
import unittest

from mioa_os.authorization import HostAuthorizationEndpoint, PresentationDecision
from mioa_os_api.authorization import (
    Allowed, AuthorizationStatus, Intent, Lifetime, NotAllowed, Rejection, RequestKind,
)


INTENT = Intent(RequestKind.CAPABILITY, "read", "document:42", "Read document", Lifetime.ONCE)


class Policy:
    def __init__(self, status=AuthorizationStatus.PROMPT):
        self.current = status
        self.authorizations = []

    def snapshot(self, principal, intent):
        return intent

    def validate(self, principal, intent):
        if intent.operation != "read" or intent.scope != "document:42":
            raise ValueError("unsupported intent")

    def status(self, principal, intent):
        return self.current

    def authorize(self, principal, intent, *, approved):
        self.authorizations.append((principal, intent, approved))
        if self.current is AuthorizationStatus.DENIED:
            return NotAllowed(Rejection.POLICY_DENIED)
        return Allowed((principal, intent.scope, intent.lifetime))


class Presenter:
    def __init__(self, *, show=True):
        self.show = show
        self.started = asyncio.Event()
        self.decision = asyncio.get_running_loop().create_future()
        self.dismissed = asyncio.Event()

    async def present(self, principal, intent, shown):
        if self.show:
            shown()
        self.started.set()
        try:
            return await asyncio.shield(self.decision)
        finally:
            self.dismissed.set()


class HostAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_smoke_multiple_waiters_decision_and_binding(self):
        policy, presenter = Policy(), Presenter()
        endpoint = HostAuthorizationEndpoint("authenticated-A", policy, presenter)
        self.assertIs(await endpoint.status(INTENT), AuthorizationStatus.PROMPT)
        request = endpoint.begin(INTENT)
        self.assertIsNone(request.outcome())
        first = asyncio.create_task(request.wait())
        second = asyncio.create_task(request.wait())
        await presenter.started.wait()
        self.assertIsNone(request.outcome())
        presenter.decision.set_result(PresentationDecision.APPROVED)
        result = await first
        self.assertIs(result, await second)
        self.assertIs(result, await request.cancel())
        self.assertEqual(result, Allowed(("authenticated-A", "document:42", Lifetime.ONCE)))
        self.assertEqual(policy.authorizations, [("authenticated-A", INTENT, True)])

    async def test_waiter_cancellation_does_not_cancel_request(self):
        presenter = Presenter()
        request = HostAuthorizationEndpoint("A", Policy(), presenter).begin(INTENT)
        waiter = asyncio.create_task(request.wait())
        await presenter.started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertIsNone(request.outcome())
        presenter.decision.set_result(PresentationDecision.DENIED)
        self.assertEqual(await request.wait(), NotAllowed(Rejection.USER_DENIED))

    async def test_presenter_cancellation_completes_without_authorization(self):
        policy, presenter = Policy(), Presenter()
        request = HostAuthorizationEndpoint("A", policy, presenter).begin(INTENT)
        await presenter.started.wait()
        presenter.decision.cancel()
        try:
            result = await asyncio.wait_for(request.wait(), 0.1)
            self.assertEqual(result, NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
            self.assertEqual(policy.authorizations, [])
        finally:
            await request.cancel()

    async def test_cancel_wins_and_late_decision_does_not_authorize(self):
        policy, presenter = Policy(), Presenter()
        request = HostAuthorizationEndpoint("A", policy, presenter).begin(INTENT)
        await presenter.started.wait()
        result = await request.cancel()
        presenter.decision.set_result(PresentationDecision.APPROVED)
        await presenter.dismissed.wait()
        self.assertIs(await request.wait(), result)
        self.assertEqual(result, NotAllowed(Rejection.CALLER_CANCELLED))
        self.assertEqual(policy.authorizations, [])

    async def test_timeout_only_after_display_and_late_decision_is_ignored(self):
        policy, presenter = Policy(), Presenter()
        request = HostAuthorizationEndpoint("A", policy, presenter, prompt_timeout=0.01).begin(INTENT)
        await presenter.started.wait()
        self.assertEqual(await request.wait(), NotAllowed(Rejection.EXPIRED))
        presenter.decision.set_result(PresentationDecision.APPROVED)
        await presenter.dismissed.wait()
        self.assertEqual(policy.authorizations, [])
        hidden = Presenter(show=False)
        pending = HostAuthorizationEndpoint("A", Policy(), hidden, prompt_timeout=0.01).begin(INTENT)
        await hidden.started.wait()
        await asyncio.sleep(0.03)
        self.assertIsNone(pending.outcome())
        hidden.decision.set_result(PresentationDecision.DENIED)
        self.assertEqual(await pending.wait(), NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))

    async def test_no_presenter_is_unavailable_not_denied(self):
        policy = Policy()
        request = HostAuthorizationEndpoint("A", policy).begin(INTENT)
        self.assertEqual(await request.wait(), NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))
        self.assertEqual(policy.authorizations, [])

    async def test_existing_authority_and_policy_denial_are_separate(self):
        policy = Policy(AuthorizationStatus.GRANTED)
        endpoint = HostAuthorizationEndpoint("A", policy)
        self.assertEqual(await endpoint.begin(INTENT).wait(), Allowed(("A", "document:42", Lifetime.ONCE)))
        self.assertFalse(policy.authorizations[0][2])
        policy.current = AuthorizationStatus.DENIED
        self.assertEqual(await endpoint.begin(INTENT).wait(), NotAllowed(Rejection.POLICY_DENIED))

    async def test_policy_failure_fails_closed_and_presentation_failure_is_unavailable(self):
        class BrokenPolicy(Policy):
            def authorize(self, principal, intent, *, approved):
                raise RuntimeError("policy backend failed")

        policy = BrokenPolicy(AuthorizationStatus.GRANTED)
        self.assertEqual(
            await HostAuthorizationEndpoint("A", policy).begin(INTENT).wait(),
            NotAllowed(Rejection.POLICY_DENIED),
        )

        class BrokenPresenter:
            async def present(self, principal, intent, shown):
                raise RuntimeError("UI unavailable")

        request = HostAuthorizationEndpoint("A", Policy(), BrokenPresenter()).begin(INTENT)
        self.assertEqual(await request.wait(), NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))

    async def test_cancel_before_driver_runs_never_presents(self):
        presenter = Presenter()
        request = HostAuthorizationEndpoint("A", Policy(), presenter).begin(INTENT)
        self.assertEqual(await request.cancel(), NotAllowed(Rejection.CALLER_CANCELLED))
        await asyncio.sleep(0)
        self.assertFalse(presenter.started.is_set())

    async def test_policy_rechecks_after_user_approval(self):
        policy, presenter = Policy(), Presenter()
        request = HostAuthorizationEndpoint("A", policy, presenter).begin(INTENT)
        await presenter.started.wait()
        policy.current = AuthorizationStatus.DENIED
        presenter.decision.set_result(PresentationDecision.APPROVED)
        self.assertEqual(await request.wait(), NotAllowed(Rejection.POLICY_DENIED))

    async def test_validation_and_presenter_unavailability(self):
        presenter = Presenter()
        endpoint = HostAuthorizationEndpoint("A", Policy(), presenter)
        invalid = Intent(RequestKind.ACTION, "write", "document:42", "", Lifetime.ONCE)
        with self.assertRaises(ValueError):
            endpoint.begin(invalid)
        with self.assertRaises(ValueError):
            await endpoint.status(invalid)
        request = endpoint.begin(INTENT)
        await presenter.started.wait()
        presenter.decision.set_result(PresentationDecision.UNAVAILABLE)
        self.assertEqual(await request.wait(), NotAllowed(Rejection.PRESENTATION_UNAVAILABLE))

    async def test_user_cancel_is_distinct(self):
        presenter = Presenter()
        request = HostAuthorizationEndpoint("A", Policy(), presenter).begin(INTENT)
        await presenter.started.wait()
        presenter.decision.set_result(PresentationDecision.CANCELLED)
        self.assertEqual(await request.wait(), NotAllowed(Rejection.USER_CANCELLED))

    async def test_mutating_caller_scope_after_prompt_cannot_expand_grant(self):
        class ScopedPolicy(Policy):
            def snapshot(self, principal, intent):
                return Intent(intent.kind, intent.operation, tuple(intent.scope),
                              intent.purpose, intent.lifetime)

            def validate(self, principal, intent):
                if intent.scope != ("document:42",):
                    raise ValueError("unsupported scope")

        presenter = Presenter()
        scope = ["document:42"]
        intent = Intent(RequestKind.CAPABILITY, "read", scope, "", Lifetime.ONCE)
        request = HostAuthorizationEndpoint("A", ScopedPolicy(), presenter).begin(intent)
        await presenter.started.wait()
        scope.append("document:secret")
        presenter.decision.set_result(PresentationDecision.APPROVED)
        self.assertEqual(await request.wait(), Allowed(("A", ("document:42",), Lifetime.ONCE)))

    async def test_invalid_host_snapshot_is_rejected_before_presentation(self):
        class InvalidSnapshot(Policy):
            def snapshot(self, principal, intent):
                return None

        presenter = Presenter()
        endpoint = HostAuthorizationEndpoint("A", InvalidSnapshot(), presenter)
        with self.assertRaises(TypeError):
            endpoint.begin(INTENT)
        with self.assertRaises(TypeError):
            await endpoint.status(INTENT)
        self.assertFalse(presenter.started.is_set())
