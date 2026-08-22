"""What the graph actually claims, driven through real runs against a real clinic.

``tests/test_witness_seam.py`` proves the projections in isolation. This proves the two things a
unit test cannot: that the **domain** hands them the right facts, and that handing them anything at
all changes nothing about what CareLoop does.

**The authority invariance class is the load-bearing one.** Every scenario below is run three
times -- with a witness that records, one that does nothing, and one that raises from both methods
-- and the case, its version, its whole timeline, its actions, the executor's invocation count, the
clinic's own appointment list, the verification verdict and the API projection are required to be
equal across all three. If a witness could influence anything, that is where it shows.

**Every claim is checked against an independently recomputed value**, never against the other
claim. The re-check test is the sharpest instance: it asserts that the ``NOT_IN_THIS_TRACE`` effect
digest equals a digest computed here from the original booking arguments, so a regression that
sourced it from the recorded result or from the clinic's answer fails even while the two emitted
digests still compare equal to each other.

The misbehaving clinics are imported from ``test_verify_readback`` rather than rebuilt, for the
reason ``test_recheck`` imports them: they are the same divergences, and a second copy would be a
second thing to keep in step.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from clinic_helpers import clinic_server
from helpers import FIXED_NOW
from test_post_effect_lifecycle import _planner, _triage, count, entries
from test_verify_readback import (
    BOOKING,
    CASE_ID,
    CannotBeReadBack,
    ChangesTheReason,
    LosesTheRecord,
    SwapsThePatient,
)

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import PlannerIntent, proposal_tool_call_for
from careloop.api.projections import case_detail
from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.runner import context_for, open_case, request_recheck, run_case
from careloop.graph.verify import ObservationOutcome
from careloop.graph.witness import (
    EffectClaim,
    EffectDispatch,
    NullWitness,
    ObservationClaim,
    ObservationMode,
    Settlement,
)
from careloop.llm.mock import mock_model
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.clinic import ClinicClient, ClinicUnavailable
from careloop.tools.idempotency import digest_mapping
from careloop.tools.results import AppointmentBooked
from demoworld.clinic import Clinic

CARER = "rosa.marlowe@carer.example"

#: The joint state the booking authorises, recomputed here from the booking arguments alone.
#:
#: **Independent of everything the run emits.** Nothing below reads a digest off a claim and
#: compares it to another claim -- each is compared to this, which is what makes an agreement
#: evidence rather than a tautology.
AUTHORISED_DIGEST = digest_mapping(
    {
        "patient_id": BOOKING["patient_id"],
        "provider_id": BOOKING["provider_id"],
        "reason": BOOKING["reason"],
        "slot_start": BOOKING["slot_start"],
    }
)

CANONICAL_FIELDS = "patient_id,provider_id,reason,slot_start"


# -- witnesses ------------------------------------------------------------------------------------


class RecordingWitness:
    """Keeps every claim, in order."""

    def __init__(self) -> None:
        self.effects: list[EffectClaim] = []
        self.observations: list[ObservationClaim] = []

    def record_effect(self, claim: EffectClaim) -> None:
        self.effects.append(claim)

    def record_observation(self, claim: ObservationClaim) -> None:
        self.observations.append(claim)

    @property
    def effect(self) -> EffectClaim:
        assert len(self.effects) == 1, f"expected one effect claim, got {len(self.effects)}"
        return self.effects[0]

    @property
    def observation(self) -> ObservationClaim:
        assert len(self.observations) == 1, (
            f"expected one observation claim, got {len(self.observations)}"
        )
        return self.observations[0]


class ThrowingWitness:
    """Raises from both protocol methods, every time."""

    def __init__(self) -> None:
        self.attempts = 0

    def record_effect(self, claim: EffectClaim) -> None:
        self.attempts += 1
        raise RuntimeError("this witness is broken")

    def record_observation(self, claim: ObservationClaim) -> None:
        self.attempts += 1
        raise RuntimeError("this witness is broken")


WITNESSES = {
    "null": NullWitness,
    "recording": RecordingWitness,
    "throwing": ThrowingWitness,
}


# -- driving runs ---------------------------------------------------------------------------------


def run_booking(
    clinic: ClinicClient | None,
    witness: object,
    *,
    proposals: Sequence[dict[str, str]] = (BOOKING,) * 8,
    max_node_executions: int = 24,
    store: InMemoryCaseStore | None = None,
):
    """Open a referral case and run it to rest, with ``witness`` bound to the run."""
    if store is None:
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
    context = context_for(
        store,
        REFERRAL_EVENT,
        case_id=CASE_ID,
        triage=_triage(),
        planner=_planner(proposals),
        now=lambda: FIXED_NOW,
        clinic=clinic,
        witness=witness,
    )
    return asyncio.run(run_case(context, max_node_executions=max_node_executions)), context


def _emailing_planner() -> PlannerAgent:
    """A planner that proposes an e-mail, which is an effect nothing external can read back."""
    return PlannerAgent(
        mock_model(
            proposals=[
                proposal_tool_call_for(
                    PlannerIntent(
                        tool_name="send_email",
                        rationale="the referral asks for confirmation",
                        args={
                            "to": "clinic@example.test",
                            "subject": "Referral",
                            "body": "Please confirm the appointment.",
                        },
                    )
                )
            ]
            * 8
        )
    )


def replay_store(case: Case) -> InMemoryCaseStore:
    """The finished case, rehydrated across a process boundary and released to execute again.

    The technique ``tests/test_case_run.py`` already uses to reach duplicate suppression: nothing
    of the graph's execution state survives, the executor's ledger is empty, and the only thing
    that can suppress the second dispatch is ``Case.actions``.
    """
    revived = Case.model_validate_json(case.model_dump_json())
    store = InMemoryCaseStore()
    store.create(revived.model_copy(update={"state": CaseState.READY_TO_EXECUTE}))
    return store


def business_state(case: Case, context) -> dict[str, object]:
    """Everything a witness must not be able to move, in one comparable mapping."""
    return {
        "state": case.state,
        "version": case.version,
        "timeline": [
            (event.seq, event.kind, event.actor, event.summary, event.payload)
            for event in case.timeline
        ],
        "actions": [record.model_dump() for record in case.actions],
        "approvals": [approval.model_dump() for approval in case.approvals],
        "facts": [fact.model_dump() for fact in case.facts],
        "invocations": context.executor.invocation_count,
        "projection": case_detail(case).model_dump(),
    }


# =================================================================================================
# Authority invariance -- the invariant the whole patch has to earn
# =================================================================================================


class TestAWitnessChangesNothingAboutWhatCareLoopDoes:
    """Three witnesses, one scenario at a time, and every business fact required to be equal.

    ``ThrowingWitness`` is the sharp one: it fails at both emission points, on the line after a
    real external booking and on the line after a durable settlement, and the run has to be
    indistinguishable from one that was never watched.
    """

    @staticmethod
    def _three(build_clinic) -> dict[str, dict[str, object]]:
        seen: dict[str, dict[str, object]] = {}
        for label, factory in WITNESSES.items():
            with clinic_server() as (base_url, world):
                result, context = run_booking(build_clinic(base_url, world), factory())
                seen[label] = business_state(result.case, context) | {
                    "nodes": result.executed_nodes,
                    "status": result.status,
                    "clinic": [held.model_dump() for held in world.appointments()],
                }
        return seen

    def test_a_booking_that_verifies_is_identical_under_all_three(self) -> None:
        seen = self._three(lambda base_url, _world: ClinicClient(base_url))
        assert seen["null"]["state"] is CaseState.DONE
        assert seen["recording"] == seen["null"]
        assert seen["throwing"] == seen["null"]

    def test_a_booking_that_diverges_is_identical_under_all_three(self) -> None:
        seen = self._three(ChangesTheReason)
        assert seen["null"]["state"] is CaseState.AWAITING_HUMAN
        assert seen["recording"] == seen["null"]
        assert seen["throwing"] == seen["null"]

    def test_a_read_that_cannot_be_completed_is_identical_under_all_three(self) -> None:
        seen = self._three(CannotBeReadBack)
        assert seen["null"]["state"] is CaseState.AWAITING_HUMAN
        assert seen["recording"] == seen["null"]
        assert seen["throwing"] == seen["null"]

    def test_a_fully_simulated_run_is_identical_under_all_three(self) -> None:
        """No clinic at all, so the comparison covers the path most runs actually take."""
        seen: dict[str, dict[str, object]] = {}
        for label, factory in WITNESSES.items():
            result, context = run_booking(None, factory())
            seen[label] = business_state(result.case, context) | {"nodes": result.executed_nodes}
        assert seen["null"]["state"] is CaseState.DONE
        assert seen["recording"] == seen["null"]
        assert seen["throwing"] == seen["null"]

    def test_a_duplicate_suppression_is_identical_under_all_three(self) -> None:
        seen: dict[str, dict[str, object]] = {}
        for label, factory in WITNESSES.items():
            with clinic_server() as (base_url, world):
                first, _ = run_booking(ClinicClient(base_url), NullWitness())
                assert first.case.state is CaseState.DONE
                result, context = run_booking(
                    ClinicClient(base_url), factory(), store=replay_store(first.case)
                )
                seen[label] = business_state(result.case, context) | {
                    "nodes": result.executed_nodes,
                    "clinic": [held.model_dump() for held in world.appointments()],
                }
        assert seen["null"]["state"] is CaseState.DONE
        assert seen["recording"] == seen["null"]
        assert seen["throwing"] == seen["null"]

    def test_a_re_check_is_identical_under_all_three(self) -> None:
        seen: dict[str, dict[str, object]] = {}
        for label, factory in WITNESSES.items():
            with clinic_server() as (base_url, world):
                parked, context = run_booking(ChangesTheReason(base_url, world), NullWitness())
                assert parked.case.state is CaseState.AWAITING_HUMAN
                store = context.store
                world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])
                request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
                result, rechecked = run_booking(
                    ClinicClient(base_url), factory(), store=store, max_node_executions=8
                )
                seen[label] = business_state(result.case, rechecked) | {
                    "nodes": result.executed_nodes
                }
        assert seen["null"]["state"] is CaseState.DONE
        assert seen["recording"] == seen["null"]
        assert seen["throwing"] == seen["null"]

    def test_the_throwing_witness_really_was_reached(self) -> None:
        """Vacuity proof for the class above: a witness nobody called proves nothing."""
        with clinic_server() as (base_url, _world):
            witness = ThrowingWitness()
            result, _ = run_booking(ClinicClient(base_url), witness)
        assert result.case.state is CaseState.DONE
        assert witness.attempts == 2


# =================================================================================================
# PERFORMED
# =================================================================================================


class TestANormalBookingClaimsThatItWasPerformed:
    @pytest.fixture
    def performed(self):
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            result, context = run_booking(ClinicClient(base_url), witness)
            assert result.case.state is CaseState.DONE
            yield witness, result, world, context

    def test_the_effect_is_claimed_once_as_performed(self, performed) -> None:
        witness, _result, _world, _context = performed
        assert witness.effect.effect_type == "book_appointment"
        assert witness.effect.dispatch is EffectDispatch.PERFORMED

    def test_the_field_list_is_the_canonical_one(self, performed) -> None:
        witness, _result, _world, _context = performed
        assert witness.effect.fields == CANONICAL_FIELDS

    def test_the_effect_digest_is_the_authorised_joint_state(self, performed) -> None:
        witness, _result, _world, _context = performed
        assert witness.effect.state_digest == AUTHORISED_DIGEST

    def test_a_real_external_appointment_exists_behind_the_claim(self, performed) -> None:
        """The claim is only worth anything if something outside CareLoop actually happened."""
        _witness, _result, world, _context = performed
        held = world.appointments()
        assert len(held) == 1
        assert held[0].patient_id == BOOKING["patient_id"]
        assert held[0].slot_start == BOOKING["slot_start"]

    def test_the_executor_really_dispatched(self, performed) -> None:
        _witness, _result, _world, context = performed
        assert context.executor.invocation_count == 1

    def test_the_action_was_recorded_as_executed(self, performed) -> None:
        _witness, result, _world, _context = performed
        assert count(result.case, TimelineKind.ACTION_EXECUTED) == 1
        assert count(result.case, TimelineKind.DUPLICATE_SUPPRESSED) == 0


class TestPerformedIsNotClaimedUntilTheCallHasReturned:
    """M5. A dispatch that raised is not a dispatch that happened.

    **The claim half is unchanged and the lifecycle half is not**, and the difference is the saga.
    A call that never returned still claims nothing at all: CareLoop does not know what became of
    the effect, and ``PERFORMED``, ``SUPPRESSED`` and ``NOT_IN_THIS_TRACE`` are three positive
    statements it is in no position to make. That was this class's point and it still is.

    What the case *does* afterwards changed. It used to end ``FAILED`` — terminal, unrecoverable —
    on the reasoning that an integration that is down is a fault. Measured against a real clinic,
    that filed a real external effect under "did not happen": the write can already have landed and
    only the answer be lost, which is precisely the outcome nobody can tell apart from a request
    that never arrived. So the case now waits in ``WAITING_EXTERNAL`` under the identity it froze
    before dispatching, and a later run reconciles it. See ``tests/test_saga_chaos.py``, where the
    write really does land and the answer really is lost.
    """

    class RefusesToBook(ClinicClient):
        def book(self, args, idempotency_key: str) -> AppointmentBooked:  # type: ignore[override]
            raise ClinicUnavailable("the clinic could not be reached: ConnectTimeout")

    def test_a_booking_that_never_returned_claims_nothing_at_all(self) -> None:
        with clinic_server() as (base_url, _world):
            witness = RecordingWitness()
            run_booking(self.RefusesToBook(base_url), witness)
        assert witness.effects == []
        assert witness.observations == []

    def test_the_case_waits_rather_than_failing(self) -> None:
        """The lifecycle half, and the one the saga moved. ``FAILED`` closed a door too early."""
        with clinic_server() as (base_url, _world):
            store = InMemoryCaseStore()
            open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
            run_booking(self.RefusesToBook(base_url), RecordingWitness(), store=store)
            case = store.get(CASE_ID)
            assert case.state is CaseState.WAITING_EXTERNAL
            assert case.failure_reason is None
            assert case.actions == ()


# =================================================================================================
# SUPPRESSED
# =================================================================================================


class TestADuplicateClaimsThatItWasSuppressed:
    @pytest.fixture
    def suppressed(self):
        with clinic_server() as (base_url, world):
            first, _ = run_booking(ClinicClient(base_url), NullWitness())
            assert first.case.state is CaseState.DONE
            held_before = len(world.appointments())

            witness = RecordingWitness()
            result, context = run_booking(
                ClinicClient(base_url), witness, store=replay_store(first.case)
            )
            yield witness, result, world, context, held_before

    def test_the_effect_is_claimed_once_as_suppressed(self, suppressed) -> None:
        witness, _result, _world, _context, _before = suppressed
        assert witness.effect.dispatch is EffectDispatch.SUPPRESSED
        assert witness.effect.effect_type == "book_appointment"

    def test_no_second_appointment_was_written(self, suppressed) -> None:
        _witness, _result, world, _context, before = suppressed
        assert len(world.appointments()) == before == 1

    def test_the_executor_was_never_invoked(self, suppressed) -> None:
        """No clinic POST left the process, which is what ``SUPPRESSED`` asserts."""
        _witness, _result, _world, context, _before = suppressed
        assert context.executor.invocation_count == 0

    def test_the_domain_recorded_the_suppression_too(self, suppressed) -> None:
        """One suppression on this pass, and still the single execution from the first."""
        _witness, result, _world, _context, _before = suppressed
        assert count(result.case, TimelineKind.DUPLICATE_SUPPRESSED) == 1
        assert count(result.case, TimelineKind.ACTION_EXECUTED) == 1
        assert len(result.case.actions) == 1

    def test_the_effect_digest_is_still_the_authorised_joint_state(self, suppressed) -> None:
        witness, _result, _world, _context, _before = suppressed
        assert witness.effect.state_digest == AUTHORISED_DIGEST

    def test_the_independent_read_still_happened_and_matched(self, suppressed) -> None:
        witness, result, _world, _context, _before = suppressed
        assert result.case.state is CaseState.DONE
        assert witness.observation.mode is ObservationMode.INDEPENDENT_READ
        assert witness.observation.outcome is ObservationOutcome.MATCHED
        assert witness.observation.settlement is Settlement.VERIFIED


# =================================================================================================
# NOT_IN_THIS_TRACE -- and the digest-source property the plan correction demanded
# =================================================================================================


class TestAReCheckClaimsTheEffectBelongsToAnEarlierTrace:
    @pytest.fixture
    def rechecked(self):
        with clinic_server() as (base_url, world):
            parked, context = run_booking(ChangesTheReason(base_url, world), NullWitness())
            assert parked.case.state is CaseState.AWAITING_HUMAN
            store = context.store
            world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

            witness = RecordingWitness()
            result, rerun = run_booking(
                ClinicClient(base_url), witness, store=store, max_node_executions=8
            )
            yield witness, result, world, rerun

    def test_the_run_is_dispatch_then_verify_and_nothing_else(self, rechecked) -> None:
        _witness, result, _world, _rerun = rechecked
        assert result.executed_nodes == ("dispatch", "verify")

    def test_the_effect_is_claimed_once_as_belonging_to_an_earlier_trace(self, rechecked) -> None:
        witness, _result, _world, _rerun = rechecked
        assert witness.effect.effect_type == "book_appointment"
        assert witness.effect.dispatch is EffectDispatch.NOT_IN_THIS_TRACE

    def test_no_clinic_write_and_no_executor_invocation_happened(self, rechecked) -> None:
        _witness, result, world, rerun = rechecked
        assert rerun.executor.invocation_count == 0
        assert len(world.appointments()) == 1
        assert count(result.case, TimelineKind.ACTION_EXECUTED) == 1  # the original, not a second
        assert count(result.case, TimelineKind.DUPLICATE_SUPPRESSED) == 0

    def test_one_independent_read_did_happen(self, rechecked) -> None:
        witness, _result, _world, _rerun = rechecked
        assert witness.observation.mode is ObservationMode.INDEPENDENT_READ
        assert witness.observation.outcome is ObservationOutcome.MATCHED

    def test_the_effect_digest_comes_from_the_original_authorised_arguments(
        self, rechecked
    ) -> None:
        """**The plan correction, asserted.**

        Compared against a digest computed in this module from ``BOOKING`` alone -- not against the
        observation digest beside it. A regression that derived the effect digest from the recorded
        result, from the clinic's answer, or by copying the observation digest would still make the
        two emitted values equal, and would fail here.
        """
        witness, _result, _world, _rerun = rechecked
        assert witness.effect.state_digest == AUTHORISED_DIGEST

    def test_it_is_the_same_digest_a_first_pass_would_have_claimed(self, rechecked) -> None:
        """Dispatch mode says what became of the effect; it never changes what was authorised."""
        witness, _result, _world, _rerun = rechecked
        with clinic_server() as (base_url, _fresh):
            first_pass = RecordingWitness()
            run_booking(ClinicClient(base_url), first_pass)
        assert witness.effect.state_digest == first_pass.effect.state_digest
        assert witness.effect.fields == first_pass.effect.fields
        assert witness.effect.dispatch is not first_pass.effect.dispatch

    def test_the_case_settles(self, rechecked) -> None:
        witness, result, _world, _rerun = rechecked
        assert result.case.state is CaseState.DONE
        assert witness.observation.settlement is Settlement.VERIFIED


class TestANormalPassNeverClaimsThatTheEffectBelongsElsewhere:
    """M3. ``NOT_IN_THIS_TRACE`` is reachable only from the caregiver's re-check."""

    def test_a_first_pass_claims_performed(self) -> None:
        with clinic_server() as (base_url, _world):
            witness = RecordingWitness()
            run_booking(ClinicClient(base_url), witness)
        assert witness.effect.dispatch is EffectDispatch.PERFORMED

    def test_a_duplicate_claims_suppressed(self) -> None:
        with clinic_server() as (base_url, _world):
            first, _ = run_booking(ClinicClient(base_url), NullWitness())
            witness = RecordingWitness()
            run_booking(ClinicClient(base_url), witness, store=replay_store(first.case))
        assert witness.effect.dispatch is EffectDispatch.SUPPRESSED

    def test_a_simulated_run_claims_performed(self) -> None:
        witness = RecordingWitness()
        run_booking(None, witness)
        assert witness.effect.dispatch is EffectDispatch.PERFORMED

    def test_no_run_that_reached_the_execute_node_ever_claims_otherwise(self) -> None:
        with clinic_server() as (base_url, world):
            for build in (
                lambda url, _w: ClinicClient(url),
                ChangesTheReason,
                SwapsThePatient,
                LosesTheRecord,
                CannotBeReadBack,
            ):
                witness = RecordingWitness()
                result, _ = run_booking(build(base_url, world), witness)
                assert "execute" in result.executed_nodes
                assert witness.effect.dispatch is not EffectDispatch.NOT_IN_THIS_TRACE
                world.reset()


# =================================================================================================
# The observation half
# =================================================================================================


class TestWhatAnIndependentReadIsReportedAsHavingEstablished:
    @staticmethod
    def _observe(build_clinic) -> tuple[RecordingWitness, Case, Clinic]:
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            result, _ = run_booking(build_clinic(base_url, world), witness)
            return witness, result.case, world

    def test_a_matching_read_settles(self) -> None:
        witness, case, _world = self._observe(lambda url, _w: ClinicClient(url))
        assert case.state is CaseState.DONE
        assert witness.observation.mode is ObservationMode.INDEPENDENT_READ
        assert witness.observation.outcome is ObservationOutcome.MATCHED
        assert witness.observation.settlement is Settlement.VERIFIED
        assert witness.observation.state_digest == AUTHORISED_DIGEST

    def test_a_diverging_read_does_not(self) -> None:
        witness, case, _world = self._observe(ChangesTheReason)
        assert case.state is CaseState.AWAITING_HUMAN
        assert witness.observation.outcome is ObservationOutcome.DIVERGED
        assert witness.observation.settlement is Settlement.NOT_CERTIFIED
        assert witness.observation.state_digest != AUTHORISED_DIGEST

    def test_a_clinic_that_holds_nothing_reports_an_absence(self) -> None:
        witness, case, _world = self._observe(LosesTheRecord)
        assert case.state is CaseState.AWAITING_HUMAN
        assert witness.observation.mode is ObservationMode.INDEPENDENT_READ
        assert witness.observation.outcome is ObservationOutcome.ABSENT
        assert witness.observation.settlement is Settlement.NOT_CERTIFIED
        assert witness.observation.state_digest is None

    def test_a_read_that_could_not_be_completed_reports_unavailable(self) -> None:
        witness, case, _world = self._observe(CannotBeReadBack)
        assert case.state is CaseState.AWAITING_HUMAN
        assert witness.observation.mode is ObservationMode.INDEPENDENT_READ
        assert witness.observation.outcome is ObservationOutcome.UNAVAILABLE
        assert witness.observation.settlement is Settlement.NOT_CERTIFIED
        assert witness.observation.state_digest is None

    def test_a_run_with_no_observer_reports_that_it_read_nothing(self) -> None:
        witness = RecordingWitness()
        result, _ = run_booking(None, witness)
        assert result.case.state is CaseState.DONE
        assert witness.observation.mode is ObservationMode.NONE
        assert witness.observation.outcome is ObservationOutcome.NOT_OBSERVED
        assert witness.observation.state_digest is None

    def test_the_resource_is_named_even_when_the_read_failed(self) -> None:
        """A 404 on a confirmation id is evidence, and the id is what makes it legible."""
        for build in (LosesTheRecord, CannotBeReadBack):
            witness, case, _world = self._observe(build)
            recorded = case.actions[-1]
            assert witness.observation.resource_id is not None
            assert witness.observation.resource_id in recorded.result

    def test_the_resource_named_is_the_one_the_clinic_actually_holds(self) -> None:
        witness, _case, world = self._observe(lambda url, _w: ClinicClient(url))
        assert witness.observation.resource_id == world.appointments()[0].confirmation_id


class TestTheModeFollowsTheVerificationAndNotTheWiring:
    """M7. *A read was attempted* and *a read-back was configured* are different facts."""

    def test_an_attempted_read_that_failed_still_reports_an_independent_read(self) -> None:
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            run_booking(CannotBeReadBack(base_url, world), witness)
        assert witness.observation.mode is ObservationMode.INDEPENDENT_READ
        assert witness.observation.outcome is ObservationOutcome.UNAVAILABLE

    def test_a_re_check_with_no_observer_reports_that_nothing_was_read(self) -> None:
        """The one place ``NOT_OBSERVED`` is a refusal rather than an ordinary simulated run."""
        with clinic_server() as (base_url, world):
            parked, context = run_booking(ChangesTheReason(base_url, world), NullWitness())
            assert parked.case.state is CaseState.AWAITING_HUMAN
            store = context.store
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

        witness = RecordingWitness()
        result, _ = run_booking(None, witness, store=store, max_node_executions=8)

        assert result.case.state is CaseState.AWAITING_HUMAN
        assert witness.observation.mode is ObservationMode.NONE
        assert witness.observation.outcome is ObservationOutcome.NOT_OBSERVED
        assert witness.observation.settlement is Settlement.NOT_CERTIFIED

    def test_a_simulated_first_pass_still_settles_while_saying_it_read_nothing(self) -> None:
        """The control: ``NOT_OBSERVED`` is not redefined as failure globally."""
        witness = RecordingWitness()
        result, _ = run_booking(None, witness)
        assert result.case.state is CaseState.DONE
        assert witness.observation.settlement is Settlement.VERIFIED
        assert witness.observation.mode is ObservationMode.NONE


# =================================================================================================
# Digest independence, at run level
# =================================================================================================


class TestTheTwoDigestsAreDerivedFromDifferentThings:
    def test_one_field_of_real_external_divergence_moves_only_the_observed_digest(self) -> None:
        """The clinic rewrites ``reason`` behind CareLoop's back; nothing else differs."""
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            run_booking(ChangesTheReason(base_url, world), witness)
            held = world.appointments()[0]

        assert witness.effect.state_digest == AUTHORISED_DIGEST
        assert witness.observation.state_digest != AUTHORISED_DIGEST
        assert witness.observation.state_digest == digest_mapping(
            {
                "patient_id": held.patient_id,
                "provider_id": held.provider_id,
                "reason": held.reason,
                "slot_start": held.slot_start,
            }
        )

    def test_a_swapped_patient_moves_only_the_observed_digest(self) -> None:
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            run_booking(SwapsThePatient(base_url, world), witness)
            held = world.appointments()[0]

        assert witness.effect.state_digest == AUTHORISED_DIGEST
        assert held.patient_id != BOOKING["patient_id"]
        assert witness.observation.state_digest != witness.effect.state_digest

    def test_they_agree_only_when_the_world_actually_agrees(self) -> None:
        with clinic_server() as (base_url, _world):
            agreeing = RecordingWitness()
            run_booking(ClinicClient(base_url), agreeing)
        assert agreeing.effect.state_digest == agreeing.observation.state_digest

    def test_the_effect_digest_is_the_same_whatever_the_clinic_did(self) -> None:
        """Four clinics, four outcomes, one authorised joint state."""
        digests = set()
        for build in (
            lambda url, _w: ClinicClient(url),
            ChangesTheReason,
            SwapsThePatient,
            LosesTheRecord,
        ):
            with clinic_server() as (base_url, world):
                witness = RecordingWitness()
                run_booking(build(base_url, world), witness)
                digests.add(witness.effect.state_digest)
        assert digests == {AUTHORISED_DIGEST}


# =================================================================================================
# Cardinality
# =================================================================================================


class TestOneRunProducesAtMostOneOfEachClaim:
    @pytest.mark.parametrize(
        ("label", "build"),
        [
            ("performed", lambda url, _w: ClinicClient(url)),
            ("diverged", ChangesTheReason),
            ("absent", LosesTheRecord),
            ("unavailable", CannotBeReadBack),
        ],
    )
    def test_exactly_one_effect_and_one_observation(self, label: str, build) -> None:
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            result, _ = run_booking(build(base_url, world), witness)
        assert len(witness.effects) == 1, label
        assert len(witness.observations) == 1, label
        assert result.executed_nodes.count("execute") == 1
        assert result.executed_nodes.count("verify") == 1

    def test_a_duplicate_run_produces_one_of_each(self) -> None:
        with clinic_server() as (base_url, _world):
            first, _ = run_booking(ClinicClient(base_url), NullWitness())
            witness = RecordingWitness()
            result, _ = run_booking(ClinicClient(base_url), witness, store=replay_store(first.case))
        assert len(witness.effects) == 1
        assert len(witness.observations) == 1
        assert result.executed_nodes.count("execute") == 1

    def test_a_re_check_produces_one_of_each(self) -> None:
        with clinic_server() as (base_url, world):
            parked, context = run_booking(ChangesTheReason(base_url, world), NullWitness())
            assert parked.case.state is CaseState.AWAITING_HUMAN
            store = context.store
            world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
            witness = RecordingWitness()
            result, _ = run_booking(
                ClinicClient(base_url), witness, store=store, max_node_executions=8
            )
        assert len(witness.effects) == 1
        assert len(witness.observations) == 1
        assert result.executed_nodes.count("verify") == 1
        assert "execute" not in result.executed_nodes

    def test_a_case_that_parks_before_acting_claims_nothing(self) -> None:
        """A run that never dispatches has no effect to account for and must not invent one."""
        witness = RecordingWitness()
        result, _ = run_booking(
            None,
            witness,
            proposals=({**BOOKING, "patient_id": "PT-NEVER-VALIDATED"},) * 8,
            max_node_executions=14,
        )
        assert result.case.state is not CaseState.DONE
        assert count(result.case, TimelineKind.ACTION_EXECUTED) == 0
        assert witness.effects == []
        assert witness.observations == []

    def test_a_non_observable_effect_claims_nothing(self) -> None:
        """An e-mail is a real external effect with no external system to read it back.

        Three of the four effect tools are simulated by design, so a claim about one could never be
        checked by anything outside CareLoop -- and digesting the arguments would put a subject and
        a body through a hash for no reader. The run is driven to whatever rest it reaches; what
        matters is that a proposal for an unobservable tool was gated and nothing was claimed.
        """
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        witness = RecordingWitness()
        context = context_for(
            store,
            REFERRAL_EVENT,
            case_id=CASE_ID,
            triage=_triage(),
            planner=_emailing_planner(),
            now=lambda: FIXED_NOW,
            witness=witness,
        )
        result = asyncio.run(run_case(context, max_node_executions=14))

        assert count(result.case, TimelineKind.PLAN_PROPOSED) >= 1
        assert entries(result.case, TimelineKind.PLAN_PROPOSED)[0].payload["tool_name"] == (
            "send_email"
        )
        assert witness.effects == []
        assert witness.observations == []


# =================================================================================================
# Privacy, over real runs
# =================================================================================================

HOSTILE_BOOKING = {
    "provider_id": BOOKING["provider_id"],
    "patient_id": BOOKING["patient_id"],
    "slot_start": BOOKING["slot_start"],
    "reason": "Margaret Okonjo, margaret.okonjo@example.test, +44 7700 900123, NHS 943 476 5919",
}


class TestNothingSensitiveSurvivesARealRun:
    def _rendered(self, witness: RecordingWitness) -> str:
        parts: list[str] = []
        for claim in [*witness.effects, *witness.observations]:
            for name in claim.__dataclass_fields__:
                value = getattr(claim, name)
                if value is not None:
                    parts.append(str(value))
        return " ".join(parts)

    def test_a_hostile_reason_reaches_the_clinic_and_never_the_witness(self) -> None:
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            result, _ = run_booking(
                ClinicClient(base_url), witness, proposals=(HOSTILE_BOOKING,) * 8
            )
            held = world.appointments()

        assert result.case.state is CaseState.DONE
        assert held[0].reason == HOSTILE_BOOKING["reason"], "the marker must really have travelled"

        rendered = self._rendered(witness)
        for marker in ("Margaret", "Okonjo", "900123", "943 476", "example.test"):
            assert marker not in rendered, marker

    def test_no_authorised_value_of_any_kind_appears(self) -> None:
        with clinic_server() as (base_url, _world):
            witness = RecordingWitness()
            run_booking(ClinicClient(base_url), witness, proposals=(HOSTILE_BOOKING,) * 8)
        rendered = self._rendered(witness)
        for name, value in HOSTILE_BOOKING.items():
            assert value not in rendered, name

    def test_no_diagnostic_text_or_clinic_words_appear(self) -> None:
        with clinic_server() as (base_url, world):
            witness = RecordingWitness()
            result, _ = run_booking(ChangesTheReason(base_url, world), witness)
        rendered = self._rendered(witness)
        assert ChangesTheReason.OTHER_REASON not in rendered
        failed = entries(result.case, TimelineKind.VERIFICATION_FAILED)[-1]
        assert failed.diagnostic
        assert failed.diagnostic not in rendered

    def test_no_url_or_case_identifier_appears(self) -> None:
        with clinic_server() as (base_url, _world):
            witness = RecordingWitness()
            run_booking(ClinicClient(base_url), witness)
        rendered = self._rendered(witness)
        assert base_url not in rendered
        assert "http" not in rendered
        assert CASE_ID not in rendered

    def test_every_emitted_string_stays_short(self) -> None:
        with clinic_server() as (base_url, _world):
            witness = RecordingWitness()
            run_booking(ClinicClient(base_url), witness, proposals=(HOSTILE_BOOKING,) * 8)
        for claim in [*witness.effects, *witness.observations]:
            for name in claim.__dataclass_fields__:
                value = getattr(claim, name)
                if value is not None:
                    assert len(str(value)) <= 120, name
