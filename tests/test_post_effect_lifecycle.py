"""What happens after an effect CareLoop attempted and cannot certify.

``tests/test_verify_readback.py`` proves that verification *notices* — the clinic moved the
appointment, swapped the patient, rewrote the reason, or cannot show the booking at all, and the
case never reaches ``DONE``. This file is about the question that starts where that one stops:
**once CareLoop knows an external effect happened and cannot certify the result, what is it allowed
to do next?**

Until this session the answer was "go back to the planner", and the cost was measured rather than
argued. Two shapes, and only the first was written down (open decision 18):

*The same proposal, repeated.* The execute node finds the effect already recorded, suppresses it,
verification reads the same divergent state and fails again — four full cycles, then the SDK's node
bound stops the run and ``run_case`` marks the case ``FAILED``. No second effect, because
idempotency held; but the terminal outcome was **framework exhaustion, not a decision**.

*A changed proposal.* ``book_appointment.reason`` is planner-authored free text that carries no
``trust_key`` and is named by no rule, so changing it alone changes the argument digest, changes the
idempotency key, and defeats duplicate suppression at both layers. Measured against a clinic that
loses the record: **two ``ACTION_EXECUTED`` entries, two ``ActionRecord``s, two executor
invocations, two confirmation ids** — a second autonomous external booking issued *after* CareLoop
already knew it could not confirm the first. Against a clinic that diverges, the second ``POST``
reached the clinic and only Demo World's own slot-collision rule refused it, which is an external
service's consistency check standing in for an authority decision CareLoop never made.

So the property this file owns is not "the loop is wasteful". It is:

    once an external effect has been attempted, a failed verification cannot return the case to
    autonomous planning or autonomous execution.

:class:`TestNoSecondEffectFollowsAFailedCertification` is the load-bearing class. Everything else
here could pass while a changed second proposal still executed; that class is what fails if one
ever can again.

**Every failure condition is covered, deliberately.** The read succeeded and the values disagree;
the clinic answered that it holds no such booking; nothing answered at all. Since Patch 2A those
are three distinguishable evidence states rather than one ``passed=False`` — see
``tests/test_observation_outcome.py``, which owns what they are *called* — and their immediate
safety lifecycle is identical, which is what is asserted here. Their *remediation* may differ
later; that this file's assertions are indifferent to which one occurred is the point of it.

The misbehaving clinics are imported from ``test_verify_readback`` rather than rebuilt. They are
the same divergences; this file asserts what the case does about them, and a second copy would be a
second thing to keep in step.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from helpers import FIXED_NOW
from strands.multiagent.base import Status
from test_verify_readback import (
    BOOKING,
    CASE_ID,
    CannotBeReadBack,
    ChangesTheReason,
    LosesTheRecord,
    SwapsThePatient,
)

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import PlannerIntent, TriageCategory, TriageResult, Urgency
from careloop.agents.triage import TriageAgent
from careloop.api.projections import NEEDS_YOU, decision_view, section_for
from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.runner import (
    NoSuchApproval,
    RunResult,
    approve,
    context_for,
    open_case,
    reject,
    run_case,
)
from careloop.llm.mock import mock_model
from careloop.policy.verdicts import ReasonCode
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.clinic import ClinicClient
from demoworld.clinic import Clinic

#: The seven-node path a case takes when nothing sends it round again.
STRAIGHT_THROUGH = ("dispatch", "triage", "evidence", "plan", "gate", "execute", "verify")

#: A second booking differing from the first in nothing but its reason.
#:
#: The whole severity argument in one constant. ``reason`` declares no ``trust_key``, so no rule may
#: require its trust and none does; it is free text the planner writes. Changing it changes the
#: canonical argument map, so ``args_digest`` and therefore ``idempotency_key`` both move, and
#: neither CareLoop's ``ActionRecord`` suppression nor Demo World's server-side key layer applies.
REBOOKING = {**BOOKING, "reason": "rebooking after the clinic record could not be confirmed"}


# -- driving one run -----------------------------------------------------------------------------


def _triage() -> TriageAgent:
    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.APPOINTMENT,
                    urgency=Urgency.SOON,
                    summary="Cardiology referral needs an outpatient appointment.",
                    patient_id="PT-4417",
                )
            ]
        )
    )


def _planner(proposals: Sequence[dict[str, str]]) -> PlannerAgent:
    """A planner that proposes each booking in turn.

    Scripts are deliberately longer than the repaired system needs. The mock raises rather than
    inventing an answer when it runs out, so a script sized for the fixed behaviour would make a
    regression report ``MockScriptExhausted`` — a confusing failure standing in front of the real
    one. Extra entries cost nothing and are never reached once the case parks.
    """
    return PlannerAgent(
        mock_model(
            structured=[
                PlannerIntent(
                    tool_name="book_appointment",
                    rationale="the referral offers a slot",
                    args=dict(args),
                )
                for args in proposals
            ]
        )
    )


def run_against(
    clinic: ClinicClient | None,
    *,
    proposals: Sequence[dict[str, str]] = (BOOKING,) * 8,
    max_node_executions: int = 24,
) -> tuple[RunResult, object]:
    """Open a referral case and run it to rest against ``clinic``."""
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
    )
    return asyncio.run(run_case(context, max_node_executions=max_node_executions)), context


# -- reading a case back -------------------------------------------------------------------------


def entries(case: Case, kind: TimelineKind) -> list:
    """Every timeline entry of one kind, in order."""
    return [event for event in case.timeline if event.kind is kind]


def count(case: Case, kind: TimelineKind) -> int:
    return len(entries(case, kind))


def seq_of(case: Case, kind: TimelineKind) -> int:
    """The position of the one entry of ``kind``. Asserts there is exactly one."""
    found = entries(case, kind)
    assert len(found) == 1, f"expected exactly one {kind}, found {len(found)}"
    return found[0].seq


def transitions_to(case: Case, state: CaseState) -> list[int]:
    """The sequence numbers of every transition into ``state``."""
    return [
        event.seq
        for event in case.timeline
        if event.kind is TimelineKind.STATE_TRANSITION and event.payload["to"] == str(state)
    ]


# -- the shared assertion ------------------------------------------------------------------------


def assert_parked_after_one_attempt(result: RunResult, context) -> None:
    """The whole Patch 1 property, asserted once and reused by every scenario below.

    Each clause is here because it fails against the pre-repair behaviour, and they fail for
    different reasons rather than restating one another:

    *One of each.* One proposal, one verification, one effect. The pre-repair run produced five
    verifications and six proposals off a single effect.

    *No authority after the failure.* No ``POLICY_ALLOW`` and no transition into
    ``READY_TO_EXECUTE`` once verification has failed. This is the clause the second-effect hazard
    actually turns on — the loop was survivable while the proposal was identical, and it stopped
    being survivable the moment one argument changed.

    *Parked, not exhausted.* ``AWAITING_HUMAN`` reached in seven nodes with the graph reporting
    ``COMPLETED``. The pre-repair run reached ``FAILED`` in twenty-four with ``Status.FAILED``,
    which is the SDK's bound firing rather than CareLoop deciding anything.

    *Parked without authority.* No ``Approval`` exists, so there is nothing for any client to post a
    decision to — the closed manual-handoff invariant, applied to a second reason for stopping.
    """
    case = result.case

    assert count(case, TimelineKind.PLAN_PROPOSED) == 1
    assert count(case, TimelineKind.VERIFICATION_FAILED) == 1
    assert count(case, TimelineKind.VERIFICATION_PASSED) == 0
    assert count(case, TimelineKind.ACTION_EXECUTED) == 1
    assert count(case, TimelineKind.DUPLICATE_SUPPRESSED) == 0
    assert len(case.actions) == 1
    assert context.executor.invocation_count == 1

    failed_at = seq_of(case, TimelineKind.VERIFICATION_FAILED)
    assert [
        event.seq
        for event in case.timeline
        if event.kind is TimelineKind.POLICY_ALLOW and event.seq > failed_at
    ] == []
    dispatched = transitions_to(case, CaseState.READY_TO_EXECUTE)
    assert [seq for seq in dispatched if seq > failed_at] == []
    assert [seq for seq in transitions_to(case, CaseState.PLANNING) if seq > failed_at] == []

    handoff_at = seq_of(case, TimelineKind.MANUAL_HANDOFF_REQUIRED)
    assert handoff_at > failed_at
    assert entries(case, TimelineKind.MANUAL_HANDOFF_REQUIRED)[0].payload["reason_code"] == str(
        ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED
    )

    assert case.state is CaseState.AWAITING_HUMAN
    assert result.executed_nodes == STRAIGHT_THROUGH
    assert not result.hit_the_bound
    assert result.status is Status.COMPLETED

    assert case.pending_approval() is None
    assert case.approvals == ()


# -- A: the external record disagrees ------------------------------------------------------------


class TestAVerifiedDivergenceParksTheCase:
    """The clinic kept the appointment and rewrote why it was made.

    The narrowest divergence the suite can construct — confirmation, provider, patient and slot are
    all exactly what CareLoop executed — so the only thing that can fail verification is the
    comparison of ``reason``, and the only thing that can fail these assertions is the lifecycle.
    """

    def test_the_case_parks_for_a_person_after_exactly_one_attempt(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        result, context = run_against(ChangesTheReason(base_url, world))

        assert_parked_after_one_attempt(result, context)

    def test_the_verification_evidence_is_kept(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The handoff does not swallow the failure. Three entries, three different jobs.

        ``ACTION_EXECUTED`` says what happened, ``VERIFICATION_FAILED`` says why it could not be
        certified, and ``MANUAL_HANDOFF_REQUIRED`` says what CareLoop decided to do about it.
        """
        base_url, world = clinic_service
        result, _context = run_against(ChangesTheReason(base_url, world))
        failure = entries(result.case, TimelineKind.VERIFICATION_FAILED)[0]

        assert failure.payload["read_back"] == "true"
        assert "read back from the external system" in failure.diagnostic
        assert "reason" in failure.diagnostic
        assert [
            event.kind
            for event in result.case.timeline
            if event.kind
            in (
                TimelineKind.ACTION_EXECUTED,
                TimelineKind.VERIFICATION_FAILED,
                TimelineKind.MANUAL_HANDOFF_REQUIRED,
            )
        ] == [
            TimelineKind.ACTION_EXECUTED,
            TimelineKind.VERIFICATION_FAILED,
            TimelineKind.MANUAL_HANDOFF_REQUIRED,
        ]

    def test_the_effect_really_did_happen_exactly_once(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity: the case is not parked because nothing was ever booked."""
        base_url, world = clinic_service
        run_against(ChangesTheReason(base_url, world))

        held = clinic_state.appointments()
        assert len(held) == 1
        assert held[0].reason == ChangesTheReason.OTHER_REASON


# -- B: a second field, so the lifecycle is generic ------------------------------------------------


class TestTheLifecycleIsGenericRatherThanReasonSpecific:
    """The same parking for a patient swap, which is an authority-shaped divergence rather than a
    fidelity-shaped one.

    One second field, deliberately. A four-field matrix would restate ``_verify_booking``'s own
    tests; what needs proving here is that the lifecycle is a property of *verification failing*
    rather than of which field failed.
    """

    def test_a_swapped_patient_parks_the_case_the_same_way(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        result, context = run_against(SwapsThePatient(base_url, world))

        assert_parked_after_one_attempt(result, context)
        assert "patient_id" in entries(result.case, TimelineKind.VERIFICATION_FAILED)[0].diagnostic

    def test_the_clinic_really_is_holding_someone_else(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        run_against(SwapsThePatient(base_url, world))

        assert clinic_state.appointments()[0].patient_id == SwapsThePatient.OTHER_PATIENT


# -- C: the read could not establish anything ------------------------------------------------------


class TestAnUnavailableReadBackParksTheCase:
    """The clinic accepted the booking and then could not show it.

    A different evidence state from A and B — CareLoop does not know the world disagrees, only that
    it cannot be confirmed — and Patch 1 deliberately gives it the same immediate lifecycle. The
    conservative direction: if the effect may have landed, re-planning toward another one is the
    move that turns uncertainty into a second appointment.
    """

    def test_a_clinic_that_cannot_confirm_parks_the_case(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        result, context = run_against(LosesTheRecord(base_url, world))

        assert_parked_after_one_attempt(result, context)

    def test_the_diagnostic_says_the_booking_could_not_be_confirmed(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """And it says that rather than naming a field, which is the honest difference from A."""
        base_url, world = clinic_service
        result, _context = run_against(LosesTheRecord(base_url, world))
        failure = entries(result.case, TimelineKind.VERIFICATION_FAILED)[0]

        assert "could not confirm" in failure.diagnostic
        assert failure.payload["read_back"] == "true"

    def test_no_read_back_is_retried(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Patch 1 stops; it does not retry the observation. Patch 2A still does not.

        One ``verify`` execution in the whole run. The discriminator ``Verification`` was missing
        now exists — this scenario records ``observation=ABSENT``, and the two that follow record
        ``UNAVAILABLE`` — but classifying the evidence is not the same decision as retrying on it,
        and Patch 2A deliberately made only the first. ``tests/test_observation_outcome.py`` counts
        the reads themselves, which is the stronger form of this assertion.
        """
        base_url, world = clinic_service
        result, _context = run_against(LosesTheRecord(base_url, world))

        assert result.executed_nodes.count("verify") == 1


class TestAnUnreachableClinicParksTheCaseTheSameWay:
    """The third external evidence state: the booking landed and the read never completed.

    Distinct from :class:`TestAnUnavailableReadBackParksTheCase` above, where the clinic *answered*
    that it holds no such appointment. Here nothing answered at all. Patch 2A tells the two apart
    in the audit trail — ``ABSENT`` against ``UNAVAILABLE`` — and deliberately gives them the same
    lifecycle, which is what this class pins.
    """

    def test_the_case_parks_for_a_person_after_exactly_one_attempt(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        result, context = run_against(CannotBeReadBack(base_url, world))

        assert_parked_after_one_attempt(result, context)

    def test_the_booking_really_did_land_at_the_clinic(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity: the case is not parked because the effect never happened.

        This is exactly the shape that makes replanning dangerous — the appointment is real and
        CareLoop cannot see it — so the effect has to be shown to exist for the lifecycle assertion
        above to mean anything.
        """
        base_url, world = clinic_service
        run_against(CannotBeReadBack(base_url, world))

        assert len(clinic_state.appointments()) == 1

    def test_a_changed_second_booking_is_never_issued(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The load-bearing class's property, for the third evidence state."""
        base_url, world = clinic_service
        result, context = run_against(
            CannotBeReadBack(base_url, world), proposals=(BOOKING, *(REBOOKING,) * 7)
        )

        assert_parked_after_one_attempt(result, context)
        assert len(clinic_state.appointments()) == 1
        assert context.executor.invocation_count == 1


# -- the load-bearing one --------------------------------------------------------------------------


class TestNoSecondEffectFollowsAFailedCertification:
    """**The property judges would care about, and the reason this patch exists.**

    Everything above could pass while a changed second proposal still executed: the same-proposal
    loop was already safe from duplicate effects, so "one ``ActionRecord``" proves nothing on its
    own unless the planner tried something *different*. Here it does.

    The planner proposes the booking, verification cannot certify it, and the planner would then
    propose a booking differing in one free-text argument. Before this patch that reached
    ``POLICY_ALLOW`` and dispatched: against a clinic that cannot confirm, two real bookings;
    against a clinic that diverges, a second ``POST`` that only Demo World's slot rule refused.
    Neither refusal was CareLoop's.

    The assertion is not "the run ended tidily". It is that the second proposal is never reached,
    because the case never returns to the planner at all.
    """

    def test_a_changed_second_booking_is_never_issued_after_a_divergence(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        result, context = run_against(
            ChangesTheReason(base_url, world), proposals=(BOOKING, *(REBOOKING,) * 7)
        )

        assert_parked_after_one_attempt(result, context)
        assert len(clinic_state.appointments()) == 1

    def test_a_changed_second_booking_is_never_issued_after_an_unconfirmable_read(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The one that produced two real bookings before the repair.

        ``LosesTheRecord`` resets the world after each booking, so the slot is free again and Demo
        World has nothing to object to — which is exactly why this is the shape that landed a second
        effect. The clinic cannot save CareLoop here, so nothing but the lifecycle can.
        """
        base_url, world = clinic_service
        result, context = run_against(
            LosesTheRecord(base_url, world), proposals=(BOOKING, *(REBOOKING,) * 7)
        )

        assert_parked_after_one_attempt(result, context)
        assert len(result.case.actions) == 1
        assert context.executor.invocation_count == 1
        assert [entry.action_type for entry in context.executor.ledger] == ["book_appointment"]

    def test_the_changed_proposal_would_have_been_a_second_effect(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity for the two above: the second proposal is not suppressible.

        If ``REBOOKING`` keyed the same as ``BOOKING``, idempotency would refuse it and both tests
        would pass against the defect. It does not, and this is where that is checked rather than
        assumed.
        """
        from careloop.tools.idempotency import args_digest, idempotency_key

        assert args_digest(REBOOKING) != args_digest(BOOKING)
        assert idempotency_key(CASE_ID, "book_appointment", REBOOKING) != idempotency_key(
            CASE_ID, "book_appointment", BOOKING
        )


# -- the parked case carries no authority ----------------------------------------------------------


class TestTheParkingCarriesNoAuthority:
    """A handoff is a status, not a capability — the closed invariant, applied to a second reason.

    ``NO_SAFE_ACTION_AVAILABLE`` used to raise an ``Approval`` with both identifying fields empty,
    and approving it released a case whose last proposal had been *denied*. The lesson generalises
    exactly: a case parked because CareLoop cannot certify what it did has nothing to authorise
    either — the effect already happened, and no decision a caregiver could post would change what
    the clinic holds.
    """

    @staticmethod
    def _parked(clinic_service: tuple[str, Clinic]) -> tuple[Case, InMemoryCaseStore]:
        base_url, world = clinic_service
        result, context = run_against(ChangesTheReason(base_url, world))
        return result.case, context.store

    def test_no_approval_is_created(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        case, _store = self._parked(clinic_service)

        assert case.approvals == ()
        assert case.pending_approval() is None

    def test_the_caregiver_sees_a_manual_follow_up_card(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The existing manual-handoff projection, reached by a second route.

        Empty identifier and empty tool name are the honest rendering of "there is nothing here to
        decide", not placeholders — there is no approval id for any client to post to.
        """
        case, _store = self._parked(clinic_service)
        decision = decision_view(case)

        assert decision is not None
        assert decision.approval_id == ""
        assert decision.tool_name == ""
        assert decision.args == {}
        assert decision.reason_code == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)
        assert section_for(case.state) == NEEDS_YOU

    @pytest.mark.parametrize("decide", [approve, reject], ids=["approve", "reject"])
    def test_no_decision_can_be_posted_against_it(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, decide
    ) -> None:
        """A fabricated identifier is a plain ``404``, and nothing moves before it is refused."""
        case, store = self._parked(clinic_service)

        with pytest.raises(NoSuchApproval):
            decide(store, CASE_ID, "apr-0", decided_by="rosa")

        after = store.get(CASE_ID)
        assert (after.version, after.state) == (case.version, case.state)
        assert after.timeline == case.timeline


# -- the parking names the action it could not certify ---------------------------------------------


class TestTheHandoffNamesTheActionItCouldNotCertify:
    """**The durable recovery identity.**

    The handoff used to record the tool name and the reason code, which between them say *"a
    booking on this case could not be confirmed"*. That is enough for a caregiver's card and not
    enough for anything to act on later: a case can carry more than one ``ActionRecord``, and "the
    booking" is not an identity.

    So the entry now carries the ``idempotency_key`` of the exact record that failed certification
    — the same key :class:`careloop.graph.nodes.VerifyNode` had already derived to *find* that
    record, passed through rather than recomputed. The digest is deliberately not used: it covers
    argument names and canonical values and not the case or the tool, and this project has closed
    three digest-only identity defects already.

    This is representation, not capability. Nothing consumes the key yet; what it buys is that a
    parked case is bound to one action rather than to a tool name, which is the precondition any
    later re-check has to be able to trust.
    """

    @staticmethod
    def _handoff(case: Case):
        found = entries(case, TimelineKind.MANUAL_HANDOFF_REQUIRED)
        assert len(found) == 1
        return found[0]

    @pytest.mark.parametrize(
        "clinic_factory",
        [ChangesTheReason, SwapsThePatient, LosesTheRecord],
        ids=["diverged-reason", "diverged-patient", "absent"],
    )
    def test_the_handoff_carries_the_key_of_the_record_that_failed(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, clinic_factory
    ) -> None:
        base_url, world = clinic_service
        result, _context = run_against(clinic_factory(base_url, world))
        case = result.case

        assert len(case.actions) == 1
        handoff = self._handoff(case)
        assert handoff.payload["reason_code"] == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)
        assert handoff.payload["idempotency_key"] == case.actions[0].idempotency_key

    def test_the_key_resolves_to_the_very_record_that_was_verified(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Object identity, not equal-looking strings.

        ``Case.action_for`` returns the record out of ``case.actions``, so ``is`` is the strongest
        available statement that the key names *that* record rather than one that happens to
        stringify the same way.
        """
        base_url, world = clinic_service
        result, _context = run_against(ChangesTheReason(base_url, world))
        case = result.case

        resolved = case.action_for(self._handoff(case).payload["idempotency_key"])

        assert resolved is not None
        assert resolved is case.actions[0]
        assert resolved.action_type == "book_appointment"

    def test_a_handoff_with_nothing_carried_out_names_no_action(self) -> None:
        """The other handoff is unchanged, and must be: ``NO_SAFE_ACTION_AVAILABLE`` means every
        tool was refused and nothing happened, so there is no action for a key to name."""
        from test_no_safe_action import EVERY_TOOL_REFUSED, drive

        parked, _context = drive(*EVERY_TOOL_REFUSED)
        handoff = next(
            event for event in parked.timeline if event.kind is TimelineKind.MANUAL_HANDOFF_REQUIRED
        )

        assert handoff.payload["reason_code"] == str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)
        assert "idempotency_key" not in handoff.payload


class TestTheHandoffIdentityIsNotTheLatestRecord:
    """A case with more than one recorded action, where the one being verified is *not* the last.

    "Which action could not be certified" and "which action was recorded most recently" are
    different questions that agree on every case the shipped flow produces today, which is exactly
    the shape of accident this repository has been bitten by three times. So the case here is built
    with a second, unrelated ``ActionRecord`` sitting *after* the booking, and the handoff must
    still name the booking.

    Verification fails closed-loop — the recorded result names a provider nobody authorised — so no
    clinic is needed and the divergence is unambiguous. The evidence class is therefore
    ``NOT_OBSERVED``, which is correct and is not what this class is about.
    """

    @staticmethod
    def _case_carrying_two_actions() -> tuple[InMemoryCaseStore, str, str]:
        """A case in ``VERIFYING`` whose proposal names the *first* of two recorded actions."""
        from careloop.state.case import ActionRecord
        from careloop.state.machine import append_event, transition
        from careloop.tools.idempotency import args_digest, idempotency_key
        from careloop.tools.results import AppointmentBooked, EmailSent

        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        base = case.version

        booking_key = idempotency_key(CASE_ID, "book_appointment", BOOKING)
        booked = ActionRecord(
            idempotency_key=booking_key,
            action_type="book_appointment",
            args_digest=args_digest(BOOKING),
            result=AppointmentBooked(
                confirmation_id=f"apt-{booking_key[:16]}",
                provider_id="PROV-SOMEWHERE-ELSE",  # not what was authorised
                patient_id=BOOKING["patient_id"],
                slot_start=BOOKING["slot_start"],
                reason=BOOKING["reason"],
            ).model_dump_json(),
            recorded_at=FIXED_NOW,
        )

        email_args = {"to": "rosa@example.test", "subject": "update", "body": "an update"}
        email_key = idempotency_key(CASE_ID, "send_email", email_args)
        emailed = ActionRecord(
            idempotency_key=email_key,
            action_type="send_email",
            args_digest=args_digest(email_args),
            result=EmailSent(
                message_id=f"msg-{email_key[:16]}",
                to=email_args["to"],
                subject=email_args["subject"],
            ).model_dump_json(),
            recorded_at=FIXED_NOW,
        )

        # The booking first, the unrelated effect last. "Latest" is now the wrong answer.
        case = case.model_copy(update={"actions": (booked, emailed)})
        case = append_event(
            case,
            kind=TimelineKind.PLAN_PROPOSED,
            actor="planner",
            summary="proposed book_appointment",
            payload={"tool_name": "book_appointment", "args": dict(BOOKING), "rationale": "r"},
            now=FIXED_NOW,
        )
        for target in (
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
        ):
            case = transition(case, target, reason="setup", actor="test", now=FIXED_NOW)
        store.save(case, expected_version=base)
        return store, booking_key, email_key

    def _run(self):
        store, booking_key, email_key = self._case_carrying_two_actions()
        context = context_for(
            store,
            REFERRAL_EVENT,
            case_id=CASE_ID,
            triage=_triage(),
            planner=_planner((BOOKING,) * 4),
            now=lambda: FIXED_NOW,
        )
        result = asyncio.run(run_case(context, max_node_executions=6))
        return result, booking_key, email_key

    def test_the_setup_really_does_carry_two_records_with_the_decoy_last(self) -> None:
        """Vacuity: without this the test below passes against a "latest record" implementation."""
        result, booking_key, email_key = self._run()

        assert [record.action_type for record in result.case.actions] == [
            "book_appointment",
            "send_email",
        ]
        assert result.case.actions[-1].idempotency_key == email_key
        assert booking_key != email_key

    def test_the_handoff_names_the_verified_booking_and_not_the_later_record(self) -> None:
        result, booking_key, email_key = self._run()
        handoff = entries(result.case, TimelineKind.MANUAL_HANDOFF_REQUIRED)[0]

        assert result.case.state is CaseState.AWAITING_HUMAN
        assert handoff.payload["reason_code"] == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)
        assert handoff.payload["idempotency_key"] == booking_key
        assert handoff.payload["idempotency_key"] != email_key

    def test_and_it_resolves_to_the_booking_record_itself(self) -> None:
        result, _booking_key, _email_key = self._run()
        handoff = entries(result.case, TimelineKind.MANUAL_HANDOFF_REQUIRED)[0]

        resolved = result.case.action_for(handoff.payload["idempotency_key"])

        assert resolved is result.case.actions[0]


# -- and none of this touched the path that works --------------------------------------------------


class TestTheVerifiedPathIsUnchanged:
    """The positive control. A booking the clinic agrees with still finishes."""

    def test_a_good_booking_still_reaches_done_through_verification(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        result, context = run_against(clinic)

        assert result.case.state is CaseState.DONE
        assert count(result.case, TimelineKind.VERIFICATION_PASSED) == 1
        assert count(result.case, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 0
        assert result.executed_nodes == STRAIGHT_THROUGH
        assert context.executor.invocation_count == 1

    def test_a_fully_simulated_booking_still_reaches_done(self) -> None:
        """No clinic, no read-back, and the lifecycle change is not quietly gating on one."""
        result, _context = run_against(None)

        assert result.case.state is CaseState.DONE
        assert entries(result.case, TimelineKind.VERIFICATION_PASSED)[0].payload["read_back"] == (
            "false"
        )
