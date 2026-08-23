"""The clinic booked it, CareLoop was not told, and CareLoop worked it out on its own.

One press of ``POST /api/demo/scenarios/recovery``, against a real Demo World on a loopback socket,
through the real routes, the real graph, the real gate, the real saga, the real wake and the real
read-back. Nothing else is done: no restart, no click, no approval, no re-check, no second request,
and no state written by anything but the nodes that own it.

**What it earns, in the order the numbers matter.**

======================================  =========================================================
provider appointments                   1
provider ``POST``s that really happened  2
logical action identity                 1, frozen before the first request left the process
durable ``ActionRecord``s                1
``PLAN_PROPOSED`` entries                1
planner / triage calls during recovery   0, and the seams raise if asked
independent read                         one ``GET`` of the clinic's own record, ``MATCHED``
case                                     ``DONE``
======================================  =========================================================

**Two writes and one appointment is the truthful claim, and the pair has to be stated together.**
CareLoop did not decline to send the booking again; it sent it again under the identity it had
already frozen, which is a lookup that also repairs. What it never did was create a second
appointment. Any product copy claiming CareLoop "did not send twice" is false, and
``tests/test_recovery_copy.py`` is what turns red when somebody writes it.

**The wake is the shipped one.** :data:`careloop.api.wake.WAKE_DELAYS` decides when the next
opportunity arrives, and :class:`Clock` *reads* the delay that was asked for rather than choosing a
shorter one. A demo running on a wake nobody else would ever experience would be demonstrating
something that does not ship.

**The zero-model claim is structural and is asserted twice, separately.** The first run legitimately
plans — a model is what proposes the booking. Every context built for that case afterwards carries
agents that raise, so a recovery *cannot* consult one; and the seams are counted as well, so the
claim is measured rather than inferred. One ``PLAN_PROPOSED`` is deliberately **not** treated as
the same fact: a model can be asked and fail without ever producing a plan, which is exactly why the
product's evidence block says "one plan" and never "no model was asked".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from api_helpers import intent, planner_proposing, triage_agent
from helpers import BOOKING_REASON
from saga_helpers import ExplodingPlanner, ExplodingTriage

from careloop.api.app import create_app
from careloop.api.demo import DEMO_SCENARIOS
from careloop.api.faults import BOOKING_CREATE_PATH, booking_response_loss
from careloop.api.wake import WAKE_DELAYS, ReconcileWaker
from careloop.graph.runner import context_for
from careloop.state import CaseState
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import ClinicClient
from careloop.tools.idempotency import idempotency_key
from demoworld.clinic import Appointment, Clinic

RECOVERY = DEMO_SCENARIOS["recovery"]

#: What the scripted planner proposes. Every value is one the shipped referral supports and one
#: Demo World seeds, so the booking is authorised by the real rules and accepted by the real clinic.
BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": BOOKING_REASON,
}

#: The identity the booking derives, computed the way CareLoop computes it.
FROZEN_KEY = idempotency_key(RECOVERY.case_id, "book_appointment", BOOKING)

#: How long the press waits for its own run to settle. Generous: a real graph over real HTTP.
PRESS_WAIT = 20.0

#: Loop turns a released wake gets before this test calls it a deadlock. A detector, not a wait.
TURNS = 500


class Clock:
    """The waker's sleeper, under this test's control.

    Records the delay each wake asked for and holds it until :meth:`fire` releases it — the
    arrangement ``tests/test_reconcile_liveness.py`` uses, and for the same reason: the schedule is
    asserted from what was *requested*, which is the thing the policy actually decides.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.registrations = 0
        self._waiting: list[asyncio.Event] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        gate = asyncio.Event()
        self._waiting.append(gate)
        await gate.wait()

    async def until_asleep(self) -> None:
        """Wait until an armed wake has genuinely reached the sleeper.

        ``_consider_wake`` creates a task and the task does not reach the sleeper until the loop
        runs it; firing before that would be manipulating a wake that has not started.
        """
        for _ in range(TURNS):
            if self._waiting:
                return
            await asyncio.sleep(0)
        raise AssertionError("no wake ever reached its sleeper")

    async def fire(self) -> None:
        """Release every waiting wake and wait for the registration it performs."""
        assert self._waiting, "no wake was waiting to be fired"
        before = self.registrations
        gates, self._waiting = self._waiting, []
        for gate in gates:
            gate.set()
        for _ in range(TURNS):
            if self.registrations > before:
                return
            await asyncio.sleep(0)
        raise AssertionError("the released wake never reached a registration")


class Counting:
    """An agent that records that it was asked, and then asks the real one."""

    def __init__(self, tally: PhasedAgents, inner: object) -> None:
        self._tally = tally
        self._inner = inner

    async def classify(self, *args: object, **kwargs: object):
        self._tally.triage_calls += 1
        return await self._inner.classify(*args, **kwargs)

    async def propose(self, *args: object, **kwargs: object):
        self._tally.planner_calls += 1
        return await self._inner.propose(*args, **kwargs)


class PhasedAgents:
    """Scripted agents for a case's first run, and booby-trapped ones for every run after it.

    The first run plans, because a model is what proposes the booking. Every run after it on that
    case is a recovery, and a recovery that asked a model anything would be inventing an action to
    explain one it had lost — so the second context and beyond carry
    :class:`~saga_helpers.ExplodingPlanner` and :class:`~saga_helpers.ExplodingTriage`, which raise.
    """

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.contexts_built = 0
        self.planner_calls = 0
        self.triage_calls = 0
        self.exploding_planner = ExplodingPlanner()
        self.exploding_triage = ExplodingTriage()

    def _scripted(self) -> tuple[object, object]:
        return triage_agent(), planner_proposing(*[intent("book_appointment", **BOOKING)] * 4)

    def for_case(self, case_id: str) -> tuple[object, object]:
        if case_id != self.case_id:
            return self._scripted()

        self.contexts_built += 1
        if self.contexts_built > 1:
            return self.exploding_triage, self.exploding_planner

        triage, planner = self._scripted()
        return Counting(self, triage), Counting(self, planner)


class RecoveryWorld:
    """One visitor world, wired as a deployed one is, with a clock this test can hold.

    ``SqliteCaseStore(":memory:")`` because that is what
    :func:`careloop.api.sessions.demo_session_worlds` builds; a proof against a different store
    class would be proving a combination the deployment never runs.
    """

    def __init__(self, base_url: str) -> None:
        self.clock = Clock()
        self.clinic = ClinicClient(base_url)
        self.faults = booking_response_loss(base_url)
        self.agents = PhasedAgents(RECOVERY.case_id)
        self.stranded_detail: dict = {}
        self.settled_detail: dict = {}
        self.stranded_appointments: tuple[Appointment, ...] = ()
        self.settled_appointments: tuple[Appointment, ...] = ()
        self.stranded_state: CaseState | None = None
        self.stranded_actions = 0

        def build(store, inbound, case_id: str):
            triage, planner = self.agents.for_case(case_id)
            reaching = self.faults.clinic if self.faults.claims(case_id) else self.clinic
            return context_for(
                store,
                inbound,
                case_id=case_id,
                triage=triage,
                planner=planner,
                clinic=reaching,
            )

        self.app = create_app(
            SqliteCaseStore(":memory:"),
            context_factory=build,
            clinic=self.clinic,
            demo=True,
            faults=self.faults,
        )
        deps = self.app.state.careloop
        # The production waker with a sleeper this test holds. Nothing else about it changes: the
        # delays it asks for are still `WAKE_DELAYS`, and `Clock` reads them.
        deps.wakes = ReconcileWaker(sleeper=self.clock.sleep)

        real = deps.register_recovery

        def watched(case_id: str):
            outcome = real(case_id)
            self.clock.registrations += 1
            return outcome

        deps.register_recovery = watched  # type: ignore[method-assign]
        self.deps = deps

    def client(self) -> httpx.AsyncClient:
        """An HTTP client on *this* loop, so routes, wake and assertions share one.

        ``TestClient`` runs the app on a portal thread with its own event loop, which a controlled
        sleeper cannot be released from without threading the release back through that portal.
        Same routes, same middleware, same dependency resolution — one fewer loop.
        """
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://careloop.test"
        )

    @property
    def posts(self) -> list[str]:
        """Every write that really left the process, read off the transport that carried them."""
        return [seen for seen in self.faults.transport.requests if seen.startswith("POST")]

    @property
    def reads(self) -> list[str]:
        return [seen for seen in self.faults.transport.requests if seen.startswith("GET")]

    async def aclose(self) -> None:
        await self.deps.drain()
        self.faults.close()
        self.clinic.close()
        self.deps.store.close()


async def drive_the_recovery(base_url: str, world_state: Clinic) -> RecoveryWorld:
    """Press 04, observe the ambiguity, let the wake fire, and hand back what happened.

    The only thing a judge does is the press. The only other thing that happens is a wake CareLoop
    scheduled for itself reaching the delay it had already asked for.
    """
    world = RecoveryWorld(base_url)
    async with world.client() as client:
        pressed = await client.post(f"/api/demo/scenarios/recovery?wait={PRESS_WAIT}")
        assert pressed.status_code == 202, pressed.text

        # Both sides of the ambiguity, snapshotted at the same instant. The provider's state is
        # read from Demo World's own object rather than from anything CareLoop returned, because
        # what CareLoop returned about this booking is nothing at all.
        world.stranded_appointments = world_state.appointments()
        world.stranded_state = world.deps.store.get(RECOVERY.case_id).state
        world.stranded_actions = len(world.deps.store.get(RECOVERY.case_id).actions)
        world.stranded_detail = (await client.get(f"/api/cases/{RECOVERY.case_id}")).json()

        await world.clock.until_asleep()
        await world.clock.fire()
        await world.deps.runs.drain()

        world.settled_appointments = world_state.appointments()
        world.settled_detail = (await client.get(f"/api/cases/{RECOVERY.case_id}")).json()
    return world


def kinds_of(detail: dict, kind: str) -> list[dict]:
    return [entry for entry in detail["timeline"] if entry["kind"] == kind]


def transitions_of(detail: dict) -> list[tuple[str, str]]:
    return [
        (entry["payload"]["from"], entry["payload"]["to"])
        for entry in kinds_of(detail, "STATE_TRANSITION")
    ]


@pytest.fixture(scope="module")
def recovered(clinic_service: tuple[str, Clinic]) -> Iterator[RecoveryWorld]:
    """The whole scenario, driven once and asserted on many times."""
    base_url, world_state = clinic_service
    world_state.reset()
    world = asyncio.run(drive_the_recovery(base_url, world_state))
    yield world
    asyncio.run(world.aclose())


class TestOnlyThePressHappened:
    """Automatic liveness. Nothing was clicked, refreshed, restarted or asked for a second time."""

    def test_one_wake_was_scheduled_and_it_registered_one_recovery(
        self, recovered: RecoveryWorld
    ) -> None:
        assert recovered.clock.registrations == 1

    def test_the_wake_asked_for_the_shipped_delay(self, recovered: RecoveryWorld) -> None:
        """Read from :data:`careloop.api.wake.WAKE_DELAYS`, never chosen here.

        The scenario has to demonstrate the wake that ships. Shortening it for a demo would be
        showing a judge something nobody else would ever experience.
        """
        assert recovered.clock.delays == [WAKE_DELAYS[0]]
        assert WAKE_DELAYS[0] == 5.0

    def test_no_approval_was_ever_raised(self, recovered: RecoveryWorld) -> None:
        """A recovery recovers knowledge, never authority. Nobody was asked for anything."""
        assert recovered.settled_detail["approvals"] == []
        assert recovered.settled_detail["decision"] is None


class TestTheProviderCommittedBeforeAnythingWasAmbiguous:
    """P1. The write really landed, and the provider is what says so."""

    def test_the_clinic_held_the_appointment_while_careloop_did_not(
        self, recovered: RecoveryWorld
    ) -> None:
        """The gap, measured on both sides at one instant. This is the whole situation.

        Demo World held the appointment; the case held no result. Until this scenario existed
        nothing a browser could press produced that state, which is why
        ``e2e/tests/unreachable-states.spec.ts`` has to mock the post-effect states.
        """
        assert len(recovered.stranded_appointments) == 1
        assert recovered.stranded_appointments[0].confirmation_id == f"apt-{FROZEN_KEY[:16]}"
        assert recovered.stranded_actions == 0

    def test_the_discarded_answer_was_a_creation(self, recovered: RecoveryWorld) -> None:
        """The seam fires on ``201`` only, so a loss *is* the provider stating it committed."""
        assert recovered.faults.transport.losses == 1
        assert recovered.faults.transport.lost_keys == [FROZEN_KEY]

    def test_the_appointment_holds_what_was_authorised(self, recovered: RecoveryWorld) -> None:
        held = recovered.stranded_appointments[0]

        assert held.provider_id == BOOKING["provider_id"]
        assert held.patient_id == BOOKING["patient_id"]
        assert held.slot_start == BOOKING["slot_start"]
        assert held.reason == BOOKING["reason"]


class TestCareLoopWaitedRatherThanGuessing:
    """P2. No result, no failure, no replan — and no second opinion invented to cover the gap."""

    def test_the_case_reached_waiting_external(self, recovered: RecoveryWorld) -> None:
        assert recovered.stranded_state is CaseState.WAITING_EXTERNAL

    def test_it_recorded_no_effect_it_could_not_confirm(self, recovered: RecoveryWorld) -> None:
        assert recovered.stranded_detail["actions"] == []
        assert kinds_of(recovered.stranded_detail, "ACTION_EXECUTED") == []
        assert kinds_of(recovered.stranded_detail, "VERIFICATION_PASSED") == []

    def test_the_identity_was_frozen_before_anything_was_sent(
        self, recovered: RecoveryWorld
    ) -> None:
        prepared = kinds_of(recovered.stranded_detail, "ACTION_PREPARED")

        assert len(prepared) == 1
        assert prepared[0]["payload"]["tool_name"] == "book_appointment"
        assert prepared[0]["payload"]["idempotency_key"] == FROZEN_KEY

    def test_it_did_not_fail(self, recovered: RecoveryWorld) -> None:
        assert recovered.stranded_detail["failure_reason"] is None
        assert recovered.stranded_detail["state"] == "WAITING_EXTERNAL"


class TestTheSameActionWasReplayedAndOneAppointmentExists:
    """P4 and P6, and the distinction between them is the claim this product may make."""

    def test_more_than_one_write_left_the_process(self, recovered: RecoveryWorld) -> None:
        """**Two POSTs.** CareLoop did not avoid sending the booking again.

        It sent it again under the identity it had already frozen, which is a lookup that also
        repairs. Copy anywhere in this product saying CareLoop "did not send twice" or "never
        resent" is false; ``tests/test_recovery_copy.py`` is what fails when somebody writes it.
        """
        assert len(recovered.posts) == 2
        assert recovered.posts == [f"POST {BOOKING_CREATE_PATH}"] * 2

    def test_exactly_one_appointment_exists(self, recovered: RecoveryWorld) -> None:
        """**One appointment.** Read off the provider's own state, not off CareLoop's account."""
        assert len(recovered.settled_appointments) == 1
        assert recovered.settled_appointments[0].confirmation_id == f"apt-{FROZEN_KEY[:16]}"

    def test_it_is_the_appointment_the_first_attempt_created(
        self, recovered: RecoveryWorld
    ) -> None:
        """Not a second one that happens to look the same. Same object, same confirmation id."""
        assert recovered.settled_appointments == recovered.stranded_appointments

    def test_the_replay_reused_the_frozen_identity(self, recovered: RecoveryWorld) -> None:
        executed = kinds_of(recovered.settled_detail, "ACTION_EXECUTED")

        assert len(executed) == 1
        assert executed[0]["payload"]["idempotency_key"] == FROZEN_KEY

    def test_one_durable_action_was_recorded(self, recovered: RecoveryWorld) -> None:
        actions = recovered.settled_detail["actions"]

        assert len(actions) == 1
        assert actions[0]["action_type"] == "book_appointment"
        assert actions[0]["idempotency_key"] == FROZEN_KEY

    def test_the_prepared_and_executed_identities_are_the_same(
        self, recovered: RecoveryWorld
    ) -> None:
        """What the product's evidence block compares, computed here the same way it is there."""
        prepared = kinds_of(recovered.settled_detail, "ACTION_PREPARED")[0]
        executed = kinds_of(recovered.settled_detail, "ACTION_EXECUTED")[0]

        assert prepared["payload"]["idempotency_key"] == executed["payload"]["idempotency_key"]

    def test_careloop_suppressed_nothing_as_a_duplicate(self, recovered: RecoveryWorld) -> None:
        """The provider deduplicated, not CareLoop, and the trail says so by staying silent.

        Worth pinning because it is the fact a plausible description of this scenario gets wrong.
        CareLoop's own ledger never held this action — the handler raised before it was written —
        so the second write really was issued and Demo World answered it out of its own key map.
        A ``DUPLICATE_SUPPRESSED`` entry here would mean something else happened.
        """
        assert kinds_of(recovered.settled_detail, "DUPLICATE_SUPPRESSED") == []


class TestTheRecoveryAskedNoModelAnything:
    """P5, asserted two ways because they are two different facts."""

    def test_the_planner_seam_raised_if_touched_and_was_not(self, recovered: RecoveryWorld) -> None:
        assert recovered.agents.exploding_planner.calls == 0

    def test_the_triage_seam_raised_if_touched_and_was_not(self, recovered: RecoveryWorld) -> None:
        assert recovered.agents.exploding_triage.calls == 0

    def test_the_recovery_ran_against_the_booby_trapped_seams(
        self, recovered: RecoveryWorld
    ) -> None:
        """The vacuity guard for the two above: a second context really was built for this case.

        Without this, "the exploding planner was never called" would also pass on a run that never
        happened.
        """
        assert recovered.agents.contexts_built == 2

    def test_the_model_was_consulted_once_by_the_first_run_only(
        self, recovered: RecoveryWorld
    ) -> None:
        assert recovered.agents.planner_calls == 1
        assert recovered.agents.triage_calls == 1

    def test_one_plan_was_ever_proposed(self, recovered: RecoveryWorld) -> None:
        """A separate fact from the counters above, and a weaker one.

        One persisted plan does not prove no model invocation occurred — a model can be asked and
        fail without producing a plan. The counters are the zero-model claim; this is what the
        durable trail can support on its own, and it is what the product's evidence block says.
        """
        assert len(kinds_of(recovered.settled_detail, "PLAN_PROPOSED")) == 1

    def test_one_action_was_ever_prepared(self, recovered: RecoveryWorld) -> None:
        assert len(kinds_of(recovered.settled_detail, "ACTION_PREPARED")) == 1


class TestItWasSettledByAnIndependentRead:
    """P7. The clinic's own record, fetched separately, compared against what was authorised."""

    def test_the_read_back_matched(self, recovered: RecoveryWorld) -> None:
        passed = kinds_of(recovered.settled_detail, "VERIFICATION_PASSED")

        assert len(passed) == 1
        assert passed[0]["payload"]["read_back"] == "true"
        assert passed[0]["payload"]["observation"] == "MATCHED"

    def test_the_read_was_its_own_request_to_the_clinic(self, recovered: RecoveryWorld) -> None:
        """One ``GET``, of the confirmation id, over the same socket the writes went down."""
        assert len(recovered.reads) == 1
        assert recovered.reads[0] == f"GET {BOOKING_CREATE_PATH}/apt-{FROZEN_KEY[:16]}"

    def test_the_case_finished(self, recovered: RecoveryWorld) -> None:
        assert recovered.settled_detail["state"] == "DONE"
        assert recovered.settled_detail["section"] == "completed"


class TestTheTrailCarriesTheWholeStory:
    """P8. Everything the product renders is a durable entry the API already projects."""

    def test_the_two_recovery_transitions_are_recorded(self, recovered: RecoveryWorld) -> None:
        """These *are* the record of the ambiguity and of the re-claim.

        ``_stop_with_the_outcome_unestablished`` writes no timeline entry of its own on the waiting
        branch, so ``EXECUTING → WAITING_EXTERNAL`` is what says the answer never came, and
        ``WAITING_EXTERNAL → EXECUTING`` is what says the frozen attempt was re-claimed. The
        frontend renders exactly these two and no other transition.
        """
        moves = transitions_of(recovered.settled_detail)

        assert ("EXECUTING", "WAITING_EXTERNAL") in moves
        assert ("WAITING_EXTERNAL", "EXECUTING") in moves

    def test_the_case_never_went_back_to_planning_after_the_effect(
        self, recovered: RecoveryWorld
    ) -> None:
        moves = transitions_of(recovered.settled_detail)

        assert [move for move in moves if move[1] == "PLANNING"] == [
            ("GATHERING_EVIDENCE", "PLANNING")
        ]

    def test_the_payload_keys_the_product_reads_are_all_projected(
        self, recovered: RecoveryWorld
    ) -> None:
        """No API change was needed for any of this, and this is what says so.

        Every field the recovery presentation uses is already on the allowlist:
        ``ACTION_PREPARED.idempotency_key``, ``ACTION_EXECUTED.idempotency_key``, the transitions'
        ``from`` and ``to``, and the verification's ``read_back`` and ``observation``.
        """
        prepared = kinds_of(recovered.settled_detail, "ACTION_PREPARED")[0]
        executed = kinds_of(recovered.settled_detail, "ACTION_EXECUTED")[0]
        verified = kinds_of(recovered.settled_detail, "VERIFICATION_PASSED")[0]
        moved = kinds_of(recovered.settled_detail, "STATE_TRANSITION")[0]

        assert "idempotency_key" in prepared["payload"]
        assert "idempotency_key" in executed["payload"]
        assert {"from", "to"} <= set(moved["payload"])
        assert {"read_back", "observation"} <= set(verified["payload"])

    def test_no_diagnostic_reached_the_response(self, recovered: RecoveryWorld) -> None:
        """Withheld wholesale, exactly as before. A new scenario does not widen the surface."""
        assert all("diagnostic" not in entry for entry in recovered.settled_detail["timeline"])


@pytest.fixture(scope="module")
def two_cases(clinic_service: tuple[str, Clinic]) -> Iterator[dict]:
    base_url, world_state = clinic_service
    world_state.reset()

    async def drive() -> dict:
        world = RecoveryWorld(base_url)
        try:
            world.faults.arm(RECOVERY.case_id)
            async with world.client() as client:
                unrelated = await client.post(
                    "/api/events",
                    json={"event_id": RECOVERY.event_id, "case_id": "unrelated-case"},
                    params={"wait": PRESS_WAIT},
                )
                assert unrelated.status_code == 202, unrelated.text
                await world.deps.runs.drain()

                return {
                    "unrelated_state": world.deps.store.get("unrelated-case").state,
                    "unrelated_actions": len(world.deps.store.get("unrelated-case").actions),
                    "losses": world.faults.transport.losses,
                    "still_armed": world.faults.transport.armed,
                    "claims_recovery": world.faults.claims(RECOVERY.case_id),
                    "appointments": world_state.appointments(),
                }
        finally:
            await world.aclose()

    yield asyncio.run(drive())


class TestAnUnrelatedBookingCannotConsumeTheFault:
    """The containment property, end to end, with two real cases in one visitor world.

    Correction 3's requirement. The scenario lock and the per-case run registry do **not** make an
    interleaved booking impossible — ``RunRegistry.start`` refuses only a second run of the *same*
    case — so this is proven by the binding rather than argued from the architecture.

    Driven in the order that discriminates: the unrelated case books **first**, while the fault is
    armed for the recovery case. If the binding were absent it would take the loss, the recovery
    case's own booking would then succeed normally, and the recovery would never happen at all.
    """

    def test_the_unrelated_case_booked_normally(self, two_cases: dict) -> None:
        assert two_cases["unrelated_state"] is CaseState.DONE
        assert two_cases["unrelated_actions"] == 1

    def test_it_really_reached_the_clinic(self, two_cases: dict) -> None:
        """The vacuity guard. A case that never booked could not have consumed a fault either."""
        assert len(two_cases["appointments"]) == 1

    def test_it_did_not_consume_the_recovery_fault(self, two_cases: dict) -> None:
        assert two_cases["losses"] == 0
        assert two_cases["still_armed"] is True
        assert two_cases["claims_recovery"] is True
