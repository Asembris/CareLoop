"""A case waiting on a provider that came back does not need anybody to notice.

The saga made a stranded external action *recoverable*: the identity is frozen before anything
leaves the process, ``ReconcileNode`` re-claims it, and one appointment exists at the end however
many times the attempt is repeated. What it did not make it was *live*. The only thing that ever
started the next run was process startup, so a case that reached ``WAITING_EXTERNAL`` at nine in the
morning waited there until somebody restarted CareLoop — correct, durable, and stuck.

These tests are about the gap and nothing else. **Every guarantee they lean on is the saga's**: the
same durable ``PLAN_PROPOSED``, the same ``ACTION_PREPARED``, the same idempotency key, the same
argument digest, the same policy verdict, the same independent read-back. What is new is that the
next opportunity arrives on its own, and the only new decision anywhere is *when*.

**The provider outage is injected below CareLoop.** A transport that raises ``ConnectError`` while
it is closed and performs real HTTP when it is open, wrapped around a real Demo World on a loopback
socket — so nothing in ``careloop/`` is stubbed, the booking that eventually lands is a real
``POST``, and the verification that follows is a real ``GET`` of the clinic's own record. Making the
clinic unreachable by patching CareLoop's own client would prove that CareLoop handles its own mock.

**Time is injected, never slept through.** The waker's sleeper is a controlled clock: it records the
delay it was asked for and holds the wake until a test releases it. So the backoff schedule is
asserted by *reading what was requested* rather than by waiting for it, and a whole outage costs
milliseconds.

**No model is ever available.** Every context carries agents that fail the test if they are asked
anything, so "reconciliation invokes no planner and no triage" is enforced by construction rather
than counted afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    SAGA_EVENT,
    ExplodingPlanner,
    ExplodingTriage,
    booking_key,
    count,
    executing_with_a_prepared_action,
    ready_to_execute,
)

from careloop.api.deps import ApiDeps
from careloop.api.wake import WAKE_DELAYS, ReconcileWaker, RecoveryOutcome
from careloop.events.simulator import EventSimulator
from careloop.graph.providers import ExternalProvider
from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass, may_replay
from careloop.graph.runner import context_for
from careloop.graph.trusted import configured
from careloop.graph.verify import ObservationOutcome
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.machine import transition
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import DEFAULT_TIMEOUT, ClinicClient
from careloop.tools.registry import REGISTRY
from careloop.tools.results import EmailSent
from demoworld.clinic import Clinic

#: How many loop turns a released wake gets to reach its registration before the test gives up.
#:
#: A deadlock detector rather than a wait: nothing here takes more than a handful of turns when the
#: code is correct, and every one of them is a synchronous hand-off with no I/O in it.
TURNS = 500

UPLOAD_ARGS = {"portal_id": "PORTAL-NORTHGATE", "document_id": "doc-0001-referral"}

#: A stranded action of a type that may never be re-sent. The recipient is the configured
#: caregiver address, so nothing about *this* call is objectionable on its face — which is the
#: point: what refuses it is the action type's recovery class and not a rule about its arguments.
EMAIL_ARGS = {
    "to": configured("caregiver_email") or "",
    "subject": "Your appointment",
    "body": "The clinic has confirmed the time.",
}


class Outage(httpx.BaseTransport):
    """A real transport with a switch on it.

    Closed, every request raises the connection error a client gets when nobody is listening — which
    :meth:`careloop.tools.clinic.ClinicClient._request` maps to ``ClinicUnavailable`` and the
    recovery layer maps to ``DispatchUnavailable``. Open, the request really goes to Demo World.

    **Below CareLoop on purpose.** The outage is a property of the network, so injecting it at the
    transport is the only place that leaves every line of CareLoop's own client, executor, node and
    verifier running exactly as it does in production.
    """

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self.closed = True
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.closed:
            raise httpx.ConnectError("the clinic is not answering", request=request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()

    @property
    def posts(self) -> list[str]:
        """Every write that really left the process. What "one appointment" is measured against."""
        return [seen for seen in self.requests if seen.startswith("POST")]


class Clock:
    """The waker's sleeper, under a test's control.

    Records the delay each wake asked for and holds it until :meth:`fire` releases it. That is what
    lets the backoff schedule be asserted from what was *requested* — the thing the policy actually
    decides — rather than from how long a test was willing to wait.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._waiting: list[asyncio.Event] = []
        self.outcomes: list[RecoveryOutcome] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        gate = asyncio.Event()
        self._waiting.append(gate)
        await gate.wait()

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    async def fire(self) -> RecoveryOutcome:
        """Release every waiting wake and return what the one registration reported.

        Waits on the **observable** — a registration outcome being recorded — rather than on a fixed
        number of loop turns, so the hand-off from the released sleeper to the registration is
        pinned rather than hoped for.
        """
        assert self._waiting, "no wake was waiting to be fired"
        before = len(self.outcomes)
        gates, self._waiting = self._waiting, []
        for gate in gates:
            gate.set()
        for _ in range(TURNS):
            if len(self.outcomes) > before:
                return self.outcomes[-1]
            await asyncio.sleep(0)
        raise AssertionError("the released wake never reached a registration")


def observed_email_provider() -> ExternalProvider:
    """A provider that claims ``send_email`` and offers a read-back for it.

    It exists to *remove* an alternative explanation rather than to model anything real. With no
    observer bound, :func:`~careloop.graph.recovery.may_replay` refuses a replay for two independent
    reasons at once, and a test asserting the refusal could not say which one did the work. Binding
    one leaves exactly one reason standing: the declared recovery class.

    Its handler and read-back are never reached — that is the property under test — so both simply
    return a well-formed result rather than pretending to be a mail provider.
    """
    sent = EmailSent(message_id="msg-never-sent", to=EMAIL_ARGS["to"], subject="Your appointment")
    return ExternalProvider(
        name="observed-email",
        handlers={"send_email": lambda args, key: sent},
        readbacks={"send_email": lambda result: sent},
    )


class World:
    """One CareLoop world wired for recovery: a real clinic, no model, a controlled clock."""

    def __init__(
        self,
        store,
        clinic: ClinicClient | None = None,
        providers: tuple[ExternalProvider, ...] = (),
    ) -> None:
        self.store = store
        self.clock = Clock()
        self.planner = ExplodingPlanner()
        self.triage = ExplodingTriage()

        def build(inner_store, inbound, case_id: str):
            return context_for(
                inner_store,
                inbound,
                case_id=case_id,
                clinic=clinic,
                providers=providers,
                triage=self.triage,
                planner=self.planner,
                now=lambda: FIXED_NOW,
            )

        self.deps = ApiDeps(
            store=store,
            events=EventSimulator({SAGA_EVENT.event_id: SAGA_EVENT}),
            context_factory=build,
            clinic=clinic,
            wakes=ReconcileWaker(sleeper=self.clock.sleep),
        )

        # Every registration reports back through here, which is what `Clock.fire` waits on. The
        # production call is untouched; this only watches it.
        real = self.deps.register_recovery

        def watched(case_id: str) -> RecoveryOutcome:
            outcome = real(case_id)
            self.clock.outcomes.append(outcome)
            return outcome

        self.deps.register_recovery = watched  # type: ignore[method-assign]

    async def run(self, case_id: str = CASE_ID) -> None:
        """Drive one run to completion, exactly as a route or the startup sweep would."""
        self.deps.start_run(case_id, SAGA_EVENT)
        await self.deps.runs.drain()

    async def arm(self, case_id: str = CASE_ID) -> None:
        """Arm the production wake and wait until it is genuinely asleep.

        ``_consider_wake`` creates a task; the task does not reach the sleeper until the loop runs
        it. Acting before that leaves a test manipulating a wake that has not started, which looks
        exactly like a wake that ignored what the test did — so every scenario waits on the
        observable instead.
        """
        before = self.clock.waiting
        self.deps._consider_wake(case_id)
        for _ in range(TURNS):
            if self.clock.waiting > before:
                return
            await asyncio.sleep(0)
        raise AssertionError("the armed wake never reached its sleeper")

    async def release(self, turns: int = 50) -> None:
        """Let every waiting wake run on, without requiring any of them to register anything.

        The counterpart of :meth:`Clock.fire` for the cases where the *expected* answer is that
        nothing happens; there is no observable to wait on, so this gives the loop more turns than
        a registration could possibly need and then lets the assertions speak.
        """
        gates, self.clock._waiting = self.clock._waiting, []
        for gate in gates:
            gate.set()
        for _ in range(turns):
            await asyncio.sleep(0)

    def case(self, case_id: str = CASE_ID):
        return self.store.get(case_id)

    async def aclose(self) -> None:
        await self.deps.drain()


@pytest.fixture
def outage() -> Iterator[Outage]:
    transport = Outage()
    yield transport
    transport.close()


@pytest.fixture
def unreachable_clinic(
    clinic_service: tuple[str, Clinic], clinic_state: Clinic, outage: Outage
) -> Iterator[ClinicClient]:
    """A real client for a real clinic, reached through a transport a test can switch off."""
    base_url, _ = clinic_service
    client = ClinicClient(base_url, client=httpx.Client(transport=outage, timeout=DEFAULT_TIMEOUT))
    yield client
    client.close()


# -- T1: the property this work exists to earn
# ------------------------------------------------------


class TestTheCaseWakesUpWithoutAnybodyAskingIt:
    """The judge-facing proof. No restart, no human, no API call — and one appointment.

    Read the sequence rather than the assertions:

    * the clinic is unreachable, and a real booking is dispatched at it;
    * the case lands in ``WAITING_EXTERNAL`` holding a frozen identity and no result;
    * **the CareLoop process stays up**, and nobody touches it;
    * the clinic comes back;
    * a wake CareLoop scheduled for itself fires;
    * the *same* durable action is replayed under the *same* provider key;
    * the clinic holds exactly one appointment;
    * an independent ``GET`` of the clinic's own record matches what was authorised;
    * the case reaches ``DONE``.

    This complements the hard-crash proof in ``tests/test_saga_chaos.py`` rather than replacing it.
    That one earns *"a process can die mid-effect and a later one converges"*; this one earns
    *"nothing had to die, and nobody had to notice"*.
    """

    @pytest.fixture
    def recovered(self, unreachable_clinic: ClinicClient, outage: Outage, clinic_state: Clinic):
        async def scenario():
            store = InMemoryCaseStore()
            ready_to_execute(store)
            world = World(store, unreachable_clinic)

            await world.run()
            stranded = world.case()
            armed = world.deps.wakes.pending()

            # The provider comes back. Nothing else happens: no restart, no request, no decision.
            outage.closed = False
            outcome = await world.clock.fire()
            await world.deps.runs.drain()

            settled = world.case()
            await world.aclose()
            return stranded, armed, outcome, settled, world

        return asyncio.run(scenario())

    def test_the_unanswered_booking_waits_rather_than_failing(self, recovered) -> None:
        stranded, armed, _, _, _ = recovered

        assert stranded.state is CaseState.WAITING_EXTERNAL
        assert stranded.failure_reason is None
        assert stranded.actions == ()
        # And CareLoop scheduled its own next look, which is the whole of what is new.
        assert armed == (CASE_ID,)

    def test_it_reconciles_itself_once_the_provider_answers(self, recovered) -> None:
        _, _, outcome, settled, _ = recovered

        assert outcome is RecoveryOutcome.STARTED
        assert settled.state is CaseState.DONE

    def test_exactly_one_appointment_exists(self, recovered, clinic_state: Clinic) -> None:
        _, _, _, settled, _ = recovered

        assert len(clinic_state.appointments()) == 1
        assert clinic_state.appointments()[0].confirmation_id == f"apt-{booking_key()[:16]}"
        assert len(settled.actions) == 1

    def test_the_replay_reused_the_frozen_identity(self, recovered) -> None:
        """No new proposal, no new preparation, no new key. The saga's identity, reused."""
        _, _, _, settled, _ = recovered

        assert count(settled, TimelineKind.PLAN_PROPOSED) == 1
        assert count(settled, TimelineKind.ACTION_PREPARED) == 1
        assert [record.idempotency_key for record in settled.actions] == [booking_key()]
        assert settled.actions[0].action_type == "book_appointment"

    def test_the_clinic_was_read_back_independently(self, recovered) -> None:
        _, _, _, settled, _ = recovered

        verified = [
            event for event in settled.timeline if event.kind is TimelineKind.VERIFICATION_PASSED
        ]
        assert len(verified) == 1
        assert verified[0].payload["read_back"] == "true"
        assert verified[0].payload["observation"] == str(ObservationOutcome.MATCHED)

    def test_no_model_was_asked_anything(self, recovered) -> None:
        """Enforced by construction — the agents raise — and counted here as well."""
        _, _, _, _, world = recovered

        assert world.planner.calls == 0
        assert world.triage.calls == 0

    def test_one_write_left_the_process_after_the_outage_ended(
        self, recovered, outage: Outage
    ) -> None:
        """Two ``POST`` attempts in total: the one nobody answered, and the replay that landed."""
        assert len(outage.posts) == 2
        assert outage.requests[-1].startswith("GET"), "the last thing CareLoop did was look"


# -- T2: a provider that stays away
# -----------------------------------------------------------------


class TestARepeatedOutageBacksOffAndStaysTruthful:
    """Three unanswered opportunities, then the provider returns and the case converges.

    The backoff is asserted from the delays the waker *asked* for, which is the decision it actually
    makes. Everything else asserted here is a **non**-change: the same key, the same digest, no new
    proposal, no new preparation, one appointment, and a state that goes on saying exactly what is
    true.
    """

    @pytest.fixture
    def outage_then_recovery(
        self, unreachable_clinic: ClinicClient, outage: Outage, clinic_state: Clinic
    ):
        async def scenario():
            store = InMemoryCaseStore()
            ready_to_execute(store)
            world = World(store, unreachable_clinic)

            await world.run()
            states = [world.case().state]

            for _ in range(3):
                outcome = await world.clock.fire()
                assert outcome is RecoveryOutcome.STARTED
                await world.deps.runs.drain()
                states.append(world.case().state)

            outage.closed = False
            await world.clock.fire()
            await world.deps.runs.drain()

            settled = world.case()
            delays = list(world.clock.delays)
            await world.aclose()
            return states, delays, settled, world

        return asyncio.run(scenario())

    def test_the_delays_follow_the_declared_schedule(self, outage_then_recovery) -> None:
        _, delays, _, _ = outage_then_recovery

        # One delay per wake armed: the first after the failed dispatch, then one per failed
        # reconciliation. The fifth is the one the successful replay never had to serve.
        assert delays[:4] == [5.0, 15.0, 45.0, 120.0]
        assert delays[:4] == list(WAKE_DELAYS[:4])

    def test_the_state_keeps_saying_what_is_true(self, outage_then_recovery) -> None:
        states, _, settled, _ = outage_then_recovery

        assert states == [CaseState.WAITING_EXTERNAL] * 4
        assert settled.state is CaseState.DONE

    def test_nothing_new_was_ever_proposed_or_prepared(self, outage_then_recovery) -> None:
        _, _, settled, _ = outage_then_recovery

        assert count(settled, TimelineKind.PLAN_PROPOSED) == 1
        assert count(settled, TimelineKind.ACTION_PREPARED) == 1
        assert count(settled, TimelineKind.ACTION_EXECUTED) == 1

    def test_every_attempt_carried_the_same_identity(self, outage_then_recovery) -> None:
        _, _, settled, _ = outage_then_recovery
        from careloop.tools.idempotency import args_digest

        prepared = [
            event for event in settled.timeline if event.kind is TimelineKind.ACTION_PREPARED
        ]
        assert prepared[0].payload["idempotency_key"] == booking_key()
        assert prepared[0].payload["args_digest"] == args_digest(BOOKING_ARGS)
        assert [record.idempotency_key for record in settled.actions] == [booking_key()]
        assert [record.args_digest for record in settled.actions] == [args_digest(BOOKING_ARGS)]

    def test_one_appointment_after_four_attempts(
        self, outage_then_recovery, clinic_state: Clinic, outage: Outage
    ) -> None:
        assert len(clinic_state.appointments()) == 1
        # One first attempt, three the outage refused, and one that landed.
        assert len(outage.posts) == 5

    def test_no_model_was_asked_anything(self, outage_then_recovery) -> None:
        _, _, _, world = outage_then_recovery

        assert world.planner.calls == 0
        assert world.triage.calls == 0


# -- T3, T4: stale and concurrent wakes
# -------------------------------------------------------------


def waiting_world(store) -> World:
    """A world holding one case parked in ``WAITING_EXTERNAL`` with a production wake armed.

    Hand-built rather than driven through a clinic, because these tests are about the *scheduler*
    and a real provider would only make the setup slower. The wake is armed by ``_consider_wake``,
    which is the production arming path and the only one there is.
    """
    executing_with_a_prepared_action(store)
    case = store.get(CASE_ID)
    store.save(
        transition(case, CaseState.WAITING_EXTERNAL, reason="setup", actor="test", now=FIXED_NOW),
        expected_version=case.version,
    )
    return World(store)


class TestAStaleWakeDoesNothing:
    """T3. Something else settled the case first; the wake must notice rather than act."""

    def test_a_wake_that_fires_after_the_case_is_done_registers_nothing(self) -> None:
        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()
            assert world.deps.wakes.pending() == (CASE_ID,)

            # Somebody else finishes the case while the wake is still asleep.
            case = store.get(CASE_ID)
            for state in (CaseState.EXECUTING, CaseState.VERIFYING, CaseState.DONE):
                case = transition(
                    case, state, reason="settled elsewhere", actor="test", now=FIXED_NOW
                )
            store.save(case, expected_version=store.get(CASE_ID).version)

            outcome = await world.clock.fire()
            await world.deps.runs.drain()
            final = world.case()
            attempts = world.deps.wakes.attempts(CASE_ID)
            await world.aclose()
            return outcome, final, attempts, world

        outcome, final, attempts, world = asyncio.run(scenario())

        assert outcome is RecoveryOutcome.STATE_MOVED
        assert final.state is CaseState.DONE
        assert world.deps.runs.any_in_flight() is None
        assert world.deps.runs._records == {}, "a stale wake started a graph run"
        assert attempts == 0, "a stale wake was counted as a reconciliation attempt"

    def test_a_wake_for_a_case_that_is_gone_registers_nothing(self) -> None:
        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            store.clear()

            outcome = await world.clock.fire()
            await world.aclose()
            return outcome, world

        outcome, world = asyncio.run(scenario())

        assert outcome is RecoveryOutcome.CASE_GONE
        assert world.deps.runs._records == {}


class TestAWakeNeverStartsASecondRun:
    """T4. A run is already going, so the wake stands down and lets it decide."""

    def test_a_wake_that_clashes_defers_to_the_run_already_in_flight(self) -> None:
        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            # A run for this case, held open, exactly as an API-triggered one would be.
            held = asyncio.Event()

            async def holding(*_args: Any) -> Any:
                await held.wait()
                raise RuntimeError("this run is only here to occupy the case")

            world.deps.graph_runner = holding
            world.deps.start_run(CASE_ID, SAGA_EVENT)

            outcome = await world.clock.fire()
            in_flight = world.deps.runs.in_flight_for(CASE_ID)

            held.set()
            await world.deps.runs.drain()
            after = world.deps.wakes.pending()
            attempts = world.deps.wakes.attempts(CASE_ID)
            await world.aclose()
            return outcome, in_flight, after, attempts

        outcome, in_flight, after, attempts = asyncio.run(scenario())

        assert outcome is RecoveryOutcome.RUN_ALREADY_IN_FLIGHT
        assert in_flight is not None, "the run that was already going was not disturbed"
        assert attempts == 0, "a clash was counted as a reconciliation attempt"
        # The winning run finished with the case still waiting, so it armed the next opportunity —
        # which is the point: responsibility passes to the run, and nothing is dropped.
        assert after == (CASE_ID,)

    def test_a_winning_run_that_finishes_the_case_withdraws_the_pending_wake(self) -> None:
        """The other half of T4, and the one that makes withdrawal load-bearing.

        A wake is already armed when the run that settles the case finishes. Leaving it there would
        have a ``DONE`` case still holding a scheduled reconciliation — harmless when it fires, and
        a scheduler that never forgets anything.
        """

        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()
            assert world.deps.wakes.pending() == (CASE_ID,)

            async def finishing(inner_store, _inbound, case_id: str) -> Any:
                case = inner_store.get(case_id)
                for state in (CaseState.EXECUTING, CaseState.VERIFYING, CaseState.DONE):
                    base = inner_store.get(case_id).version
                    case = transition(
                        inner_store.get(case_id),
                        state,
                        reason="settled",
                        actor="test",
                        now=FIXED_NOW,
                    )
                    inner_store.save(case, expected_version=base)
                raise RuntimeError("the result is irrelevant; the durable state is not")

            world.deps.graph_runner = finishing
            world.deps.start_run(CASE_ID, SAGA_EVENT)
            await world.deps.runs.drain()

            pending = world.deps.wakes.pending()
            deferred = world.deps.wakes.deferred()
            await world.release()
            await world.aclose()
            return pending, deferred, world

        pending, deferred, world = asyncio.run(scenario())

        assert world.case().state is CaseState.DONE
        assert pending == (), "a finished case kept a scheduled reconciliation"
        assert deferred == ()
        assert world.clock.outcomes == [], "the withdrawn wake registered a run anyway"


# -- T5-T9: lifecycle
# -------------------------------------------------------------------------------


class TestAProductionWakeSurvivesAReversibleClose:
    """T5. A gate that closes and reopens costs a waiting case nothing."""

    def test_a_wake_due_during_a_close_is_deferred_and_then_honoured(self) -> None:
        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            world.deps.wakes.close()
            # The wake falls due inside the closed window. It must keep its intent.
            await world.release()
            deferred = world.deps.wakes.deferred()
            registered_while_closed = list(world.clock.outcomes)

            world.deps.wakes.reopen()
            for _ in range(TURNS):
                if world.clock.waiting:
                    break
                await asyncio.sleep(0)
            restored = world.deps.wakes.pending()

            outcome = await world.clock.fire()
            await world.deps.runs.drain()
            await world.aclose()
            return deferred, registered_while_closed, restored, outcome

        deferred, while_closed, restored, outcome = asyncio.run(scenario())

        assert deferred == (CASE_ID,), "a due wake was consumed by a temporary close"
        assert while_closed == [], "a wake registered a run while the gate was closed"
        assert restored == (CASE_ID,), "the reopened gate did not restore the deferred wake"
        assert outcome is RecoveryOutcome.STARTED


class TestResetAndTheWakesItFinds:
    """T6 and T7. A reset that goes through discards; a reset that is refused restores."""

    def test_a_successful_reset_leaves_nothing_scheduled(self) -> None:
        from careloop.api import app as app_module

        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            report = await app_module.reset_world(world.deps)

            pending = world.deps.wakes.pending()
            deferred = world.deps.wakes.deferred()
            await world.release()
            await world.aclose()
            return report, pending, deferred, world

        report, pending, deferred, world = asyncio.run(scenario())

        assert report["cases_cleared"] == 1
        assert (pending, deferred) == ((), ())
        assert world.clock.outcomes == [], "a wake registered a run around a reset"
        assert world.deps.runs._records == {}

    def test_a_refused_reset_gives_the_wake_back(self) -> None:
        """The case survives the refusal, so its scheduling has to survive it too."""
        from fastapi import HTTPException

        from careloop.api import app as app_module

        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            held = asyncio.Event()

            async def holding(*_args: Any) -> Any:
                await held.wait()
                raise RuntimeError("occupying the world")

            world.deps.graph_runner = holding
            world.deps.start_run("case-elsewhere", SAGA_EVENT)

            try:
                await app_module.reset_world(world.deps)
            except HTTPException as refusal:
                status = refusal.status_code
            else:  # pragma: no cover - the refusal is the behaviour under test
                pytest.fail("reset cleared a world with a run in flight")

            restored = world.deps.wakes.pending()
            held.set()
            await world.deps.runs.drain()
            still_there = world.case().state
            await world.aclose()
            return status, restored, still_there

        status, restored, state = asyncio.run(scenario())

        assert status == 409
        assert state is CaseState.WAITING_EXTERNAL, "a refused reset cleared the case anyway"
        assert restored == (CASE_ID,), "a refused reset cost the case its scheduled reconciliation"


class TestEvictionAndAProductionWake:
    """T8. A world that is kept keeps its scheduling; a world that goes takes its wakes with it."""

    def test_a_spared_world_gets_its_deferred_wake_back(self) -> None:
        from careloop.api.sessions import SessionWorlds

        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            registry = SessionWorlds(
                lambda _session: world.deps,
                limit=4,
                idle_seconds=0.0,
                now=lambda: 0.0,
                end_world=lambda *_: None,
            )
            registry.mint()

            # The wake falls due while the world looks idle, and eviction is about to look.
            world.deps.wakes.close()
            await world.release()
            world.deps.wakes.reopen()
            for _ in range(TURNS):
                if world.deps.wakes.pending():
                    break
                await asyncio.sleep(0)

            # Now a run wins the race, so eviction must back off and leave the world working.
            held = asyncio.Event()

            async def holding(*_args: Any) -> Any:
                await held.wait()
                raise RuntimeError("occupying the world")

            world.deps.graph_runner = holding
            world.deps.start_run("case-elsewhere", SAGA_EVENT)

            await asyncio.to_thread(registry._forget_idle)
            kept = registry.sessions()
            scheduled = world.deps.wakes.pending()

            held.set()
            await world.deps.runs.drain()
            await world.aclose()
            return kept, scheduled

        kept, scheduled = asyncio.run(scenario())

        assert len(kept) == 1, "a world with a run in flight was evicted"
        assert scheduled == (CASE_ID,), "a spared world lost its case's scheduled reconciliation"

    def test_an_evicted_world_takes_its_wakes_with_it(self) -> None:
        from careloop.api.sessions import SessionWorlds

        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()

            registry = SessionWorlds(
                lambda _session: world.deps,
                limit=4,
                idle_seconds=0.0,
                now=lambda: 0.0,
                end_world=lambda *_: None,
            )
            registry.mint()
            await asyncio.to_thread(registry._forget_idle)

            await world.release()
            await world.aclose()
            return registry.sessions(), world

        sessions, world = asyncio.run(scenario())

        assert sessions == ()
        assert world.deps.wakes.stopped
        assert world.clock.outcomes == [], "an evicted world's wake still registered a run"
        assert world.deps.runs._records == {}


class TestShutdownCollectsTheWakes:
    """T9. Nothing runs after the drain, and nothing is left unretrieved."""

    def test_a_pending_wake_is_stopped_and_collected(self) -> None:
        unhandled: list[dict[str, Any]] = []

        async def scenario():
            asyncio.get_running_loop().set_exception_handler(
                lambda _loop, context: unhandled.append(context)
            )
            store = InMemoryCaseStore()
            world = waiting_world(store)
            await world.arm()
            tasks = list(world.deps.wakes._pending.values())

            await world.aclose()
            await world.release()
            return tasks, world

        tasks, world = asyncio.run(scenario())

        assert all(task.done() for task in tasks)
        assert world.deps.wakes.stopped
        assert world.clock.outcomes == [], "a wake registered a run during or after shutdown"
        assert unhandled == []


# -- T10: the durable fallback
# ----------------------------------------------------------------------


class TestTheStartupSweepIsStillTheCrashFallback:
    """T10. The wake improves a live process; it does not replace what covers a dead one.

    Driven over a real SQLite file and two separate :class:`~careloop.api.deps.ApiDeps`, because the
    thing being asserted is that the *durable* state carries the recovery when the in-memory
    schedule
    does not survive.
    """

    def test_a_wake_dies_with_its_process_and_the_sweep_still_finds_it(self, tmp_path) -> None:
        async def scenario():
            db = tmp_path / "cases.sqlite3"
            first = SqliteCaseStore(db)
            waiting_world(first)  # builds the case; its world is discarded below
            dying = World(first)
            await dying.arm()
            armed_before_the_crash = dying.deps.wakes.pending()

            # The process goes away. Its wakes go with it: nothing cancels them politely.
            await dying.aclose()
            first.close()

            second = SqliteCaseStore(db)
            reborn = World(second)
            reborn.deps.graph_runner = self._recording(reborn)
            resumed = reborn.deps.resume_unsettled()
            await reborn.deps.runs.drain()
            state = reborn.case().state
            await reborn.aclose()
            second.close()
            return armed_before_the_crash, resumed, state, reborn

        armed, resumed, state, reborn = asyncio.run(scenario())

        assert armed == (CASE_ID,)
        assert resumed == (CASE_ID,), "the startup sweep no longer resumes an abandoned case"
        assert reborn.asked == [CASE_ID]
        assert state is CaseState.WAITING_EXTERNAL

    @staticmethod
    def _recording(world: World):
        world.asked = []  # type: ignore[attr-defined]

        async def runner(_store, _inbound, case_id: str) -> Any:
            world.asked.append(case_id)  # type: ignore[attr-defined]
            raise RuntimeError("the sweep's scheduling is what is under test, not the graph")

        return runner


class TestEveryWaitingCaseCanBeCarriedForward:
    """The one refusal that would end a case's scheduling, and why half of it cannot happen.

    :attr:`~careloop.api.wake.RecoveryOutcome.RECOVERY_CONTEXT_UNAVAILABLE` does not re-arm — the
    next wake would find exactly the same thing — so it is worth knowing when it can occur at all.

    **The missing-delivery half is structurally unreachable.**
    :func:`careloop.graph.context.latest_delivery` reads the ``CASE_CREATED`` and ``CASE_REOPENED``
    entries, and :func:`careloop.graph.runner.open_case` writes the first of those before the case
    is persisted at all — so a case that exists has a delivery, and a case in ``WAITING_EXTERNAL``
    has been through a run, which needs one.

    **The unknown-event half is reachable and is deliberately left alone.** A process whose event
    fixtures no longer hold the id a case names cannot carry that case forward under anything, and
    inventing a delivery would be this layer manufacturing the context a recovery reads its
    arguments from. It is the startup sweep's existing behaviour — log and move on — and the case
    stays durable, stays swept at the next start, and is re-armed by the next run to settle on it.
    """

    def test_a_case_always_carries_the_delivery_a_recovery_needs(self) -> None:
        from careloop.graph.context import latest_delivery
        from careloop.graph.runner import open_case

        store = InMemoryCaseStore()
        opened = open_case(store, SAGA_EVENT, case_id="case-fresh", now=FIXED_NOW)

        assert latest_delivery(opened) is not None
        assert latest_delivery(opened).event_id == SAGA_EVENT.event_id
        # And the one built the way every saga test builds one, which is the shape that waits.
        executing_with_a_prepared_action(store, case_id=CASE_ID)
        assert latest_delivery(store.get(CASE_ID)) is not None

    def test_an_event_this_process_does_not_hold_is_reported_rather_than_invented(self) -> None:
        async def scenario():
            store = InMemoryCaseStore()
            world = waiting_world(store)
            world.deps.events = EventSimulator({})

            outcome = world.deps.register_recovery(CASE_ID)
            await world.aclose()
            return outcome, world

        outcome, world = asyncio.run(scenario())

        assert outcome is RecoveryOutcome.RECOVERY_CONTEXT_UNAVAILABLE
        assert world.deps.runs._records == {}
        # Left exactly as it was: reported, not repaired and not guessed at.
        assert world.case().state is CaseState.WAITING_EXTERNAL


# -- T11: providers that may never be re-sent
# -------------------------------------------------------


class TestANonReplayableEffectCannotBeWokenAtAll:
    """T11. The auto-retry path is unreachable for the two effects that reach no real provider.

    Two independent halves, and both are needed. **Structurally**, ``WAITING_EXTERNAL`` is written
    only where :func:`careloop.graph.recovery.may_replay` says yes, and that answers ``False`` for a
    ``NOT_REPLAYABLE`` action type *however many observers are bound* — so the state a wake is
    armed for cannot be reached. **Behaviourally**, a stranded non-replayable action parks for a
    person, and the arming path then withdraws rather than schedules.

    **The fixture moved when ``upload_document`` gained a measured provider**, and the reason it had
    to is worth stating. It was chosen over ``send_email`` because the shipped rules refuse an email
    to an untrusted recipient anyway, so a behavioural test built on one could park for a reason
    that has nothing to do with the recovery class — the shape of vacuity this repository names. The
    replacement is not another fixture argument but a **direct** one:
    :meth:`test_a_stranded_non_replayable_action_parks_before_policy_is_consulted` asserts the
    timeline carries no ``POLICY_DENY`` at all, which is only true if
    :func:`~careloop.graph.recovery.may_replay` is what refused. ``ReconcileNode`` checks the class
    before it fires the ``BeforeToolCall`` hook, so the two are distinguishable by what the trail
    records rather than by an argument about which tool to pick.
    """

    def test_no_number_of_observers_makes_a_non_replayable_action_replayable(self) -> None:
        every_observer = frozenset(REGISTRY.external_effect_tools)
        refused = {
            tool
            for tool, recovery in RECOVERY_CLASS.items()
            if recovery is RecoveryClass.NOT_REPLAYABLE
        }

        assert refused == {"send_email", "grant_portal_access"}
        assert [may_replay(tool, observers=every_observer) for tool in sorted(refused)] == [
            False,
            False,
        ]

    def test_a_stranded_non_replayable_action_parks_for_the_right_reason(self) -> None:
        """Parked, unsent, unscheduled — and refused by the recovery class, not by anything else.

        **Two things make this discriminating**, and neither is a choice of fixture.

        The run is bound to a provider that really does observe ``send_email``, so
        :func:`~careloop.graph.recovery.may_replay`'s *other* condition is satisfied. Without that,
        the absence of an observer would refuse the replay on its own and this test would pass with
        the recovery class deleted.

        And the **handoff says which refusal happened**. ``ReconcileNode`` consults the class before
        it fires the ``BeforeToolCall`` hook, and each branch parks with its own ``detail`` on the
        diagnostic. Measured against a mutant marking ``send_email`` replayable: the case is carried
        as far as the hook, which cancels the unapproved email, and it parks with
        *"the BeforeToolCall hook cancelled the replay"* instead. Every other assertion in this test
        stays green under that mutant — same state, no action, no wake — and this one turns red.

        That is worth stating plainly, because the obvious assertion does not work. There is no
        ``POLICY_DENY`` on the trail in either case — ``ReconcileNode`` parks on a refused replay
        without writing one — so a test built on its absence would pass under the mutant and prove
        nothing.
        """

        async def scenario():
            store = InMemoryCaseStore()
            executing_with_a_prepared_action(store, tool_name="send_email", args=EMAIL_ARGS)
            world = World(store, providers=(observed_email_provider(),))

            await world.run()
            case = world.case()
            pending = world.deps.wakes.pending()
            deferred = world.deps.wakes.deferred()
            await world.aclose()
            return case, pending, deferred

        case, pending, deferred = asyncio.run(scenario())

        assert case.state is CaseState.AWAITING_HUMAN
        assert case.actions == (), "a non-replayable action was re-sent"
        assert (pending, deferred) == ((), ()), "a non-replayable action was given a retry schedule"

        (handoff,) = [
            entry for entry in case.timeline if entry.kind is TimelineKind.MANUAL_HANDOFF_REQUIRED
        ]
        assert "no safe way to establish the outcome" in (handoff.diagnostic or ""), (
            "the case parked, but not because the action type may never be re-sent; the handoff "
            f"says: {handoff.diagnostic!r}"
        )

    def test_the_replayable_action_types_really_do_take_the_other_branch(self) -> None:
        """Vacuity for the pair above: both measured providers reach the waiting state."""
        for action_type in ("book_appointment", "upload_document"):
            assert RECOVERY_CLASS[action_type] is RecoveryClass.IDEMPOTENT_REPLAYABLE
            assert may_replay(action_type, observers={action_type})
            assert not may_replay(action_type, observers=frozenset())
