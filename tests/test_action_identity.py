"""Which values define the same logical booking, proved against a real clinic.

The measured production hazard, in one sentence: reproposals of the same appointment alternated
between ``"…for cardiology review"`` and ``"…for cardiology review."``, which moved the canonical
argument map, moved ``args_digest``, moved ``idempotency_key``, and defeated duplicate suppression
in CareLoop **and** in Demo World. Each miss sent a second ``POST``, and only the clinic's own
slot-collision rule refused it — an external service's consistency check standing where an authority
decision should be.

Identity is now declared rather than assumed: ``idempotency_key`` covers exactly the arguments
:attr:`careloop.tools.registry.ArgSpec.identifies` names, and ``book_appointment.reason`` is not one
of them. The clinic agrees about what that means — ``Clinic._taken`` is keyed on
``(provider_id, slot_start)``, so one slot is one appointment however it is described.

**``args_digest`` is deliberately unchanged and still covers every argument**, and every test that
asserts identity invariance here asserts digest divergence beside it. They answer different
questions: which logical action is this, and which exact call did a caregiver approve and the
executor receive. A patch that narrowed both would have traded one defect for a worse one, and a
test that only checked the key would pass in that world.

The reopen proof is the reachable form of the hazard. ``VERIFYING`` no longer reaches ``PLANNING``
and there is no ``verify → plan`` edge, so the surviving route to a second planning pass over a
completed booking is an inbound event reopening a ``DONE`` case. It is driven end to end against
Demo World on a loopback socket, and what it asserts is the clinic's own appointment count and slot
table rather than CareLoop's opinion of them.
"""

from __future__ import annotations

import asyncio

import pytest
from clinic_helpers import clinic_server
from helpers import BOOKING_REASON, FIXED_NOW
from saga_helpers import (
    ExplodingPlanner,
    ExplodingTriage,
    executing_with_a_prepared_action,
)

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
    proposal_tool_call_for,
)
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import CLAIM_REJECTION_EVENT, REFERRAL_EVENT
from careloop.graph.context import unsettled_preparation
from careloop.graph.runner import context_for, open_case, reopen_case, run_case
from careloop.llm.mock import mock_model
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.machine import append_event
from careloop.tools.clinic import ClinicClient
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import AppointmentBooked
from demoworld.clinic import Clinic, Slot

CASE_ID = "case-0001"
PROVIDER = "PROV-NORTHGATE-CARDIOLOGY"
PATIENT = "PT-4417"
SLOT = "2026-09-03T09:30:00"
OTHER_SLOT = "2026-09-03T11:15:00"

BOOKING = {
    "provider_id": PROVIDER,
    "patient_id": PATIENT,
    "slot_start": SLOT,
    "reason": BOOKING_REASON,
}

#: Wordings of one appointment. Punctuation, casing, leading and trailing whitespace.
#:
#: Not normalised anywhere — ``canonical_value`` still takes a ``str`` verbatim, deliberately, since
#: normalisation is a laundering surface and the exact-match trust rule depends on its absence.
#: These key alike because the argument is declared non-identifying, not because the strings were
#: made equal.
REWORDINGS = (
    "review",
    "review.",
    "Review",
    " review ",
    "REVIEW",
    "review  ",
    "\treview",
)


def key(args: dict[str, str], case_id: str = CASE_ID) -> str:
    return idempotency_key(case_id, "book_appointment", args)


# -- the pure identity property ------------------------------------------------------------------


class TestWordingDoesNotMakeANewBooking:
    @pytest.mark.parametrize("wording", REWORDINGS)
    def test_every_rewording_keys_to_the_same_action(self, wording: str) -> None:
        assert key({**BOOKING, "reason": wording}) == key(BOOKING)

    @pytest.mark.parametrize("wording", REWORDINGS)
    def test_every_rewording_still_moves_the_argument_digest(self, wording: str) -> None:
        """The other question, and it must be shown to answer differently."""
        assert args_digest({**BOOKING, "reason": wording}) != args_digest(BOOKING)

    def test_the_rewordings_really_are_different_strings(self) -> None:
        """Vacuity: if any two collapsed, the digest assertion above would be proving nothing."""
        assert len(set(REWORDINGS)) == len(REWORDINGS)
        assert BOOKING["reason"] not in REWORDINGS

    def test_the_key_and_the_digest_are_not_the_same_value(self) -> None:
        assert key(BOOKING) != args_digest(BOOKING)


class TestChangingTheAppointmentDoesMakeANewBooking:
    """Semantic distinction. Narrowing identity must not blur one."""

    @pytest.mark.parametrize("coordinate", ["provider_id", "patient_id", "slot_start"])
    def test_every_identifying_coordinate_moves_the_key(self, coordinate: str) -> None:
        changed = {**BOOKING, coordinate: BOOKING[coordinate] + "-ELSEWHERE"}

        assert key(changed) != key(BOOKING)
        assert args_digest(changed) != args_digest(BOOKING)

    def test_the_case_still_scopes_the_action(self) -> None:
        assert key(BOOKING, "case-0002") != key(BOOKING)

    def test_the_identifying_set_is_exactly_the_three_coordinates(self) -> None:
        assert REGISTRY.identifying_args("book_appointment") == {
            "provider_id",
            "patient_id",
            "slot_start",
        }

    def test_the_clinic_agrees_about_what_names_an_appointment(self) -> None:
        """Demo World keys its slot table on ``(provider_id, slot_start)`` and not on ``reason``.

        The identity CareLoop declares is the provider's plus ``patient_id`` and the case. It is
        deliberately *finer* than the clinic's: booking a different patient into one slot is a
        different action, and CareLoop must not depend on the clinic's slot rule to say so.
        """
        world = Clinic()
        assert Slot(provider_id=PROVIDER, slot_start=SLOT) in world.slots
        assert "reason" not in Slot.model_fields


# -- driving real runs ---------------------------------------------------------------------------


class CountingClinic(ClinicClient):
    """A clinic client that records every write it makes.

    The assertion that matters is *how many requests left the process*, which neither CareLoop's
    ledger nor the clinic's appointment count answers on its own: a suppressed duplicate and a
    server-side replay both leave one appointment behind, and only one of them made a second
    ``POST``.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.writes: list[str] = []

    def book(self, args, idempotency_key: str) -> AppointmentBooked:  # type: ignore[override]
        self.writes.append(idempotency_key)
        return super().book(args, idempotency_key)


def _triage() -> TriageAgent:
    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.APPOINTMENT,
                    urgency=Urgency.SOON,
                    summary="Cardiology referral needs an outpatient appointment.",
                    patient_id=PATIENT,
                )
            ]
            * 4
        )
    )


def _planner(args: dict[str, str], repeats: int = 4) -> PlannerAgent:
    intent = PlannerIntent(
        tool_name="book_appointment", rationale="the referral offers a slot", args=args
    )
    return PlannerAgent(mock_model(proposals=[proposal_tool_call_for(intent)] * repeats))


def _run(store, event, clinic: ClinicClient, args: dict[str, str]):
    context = context_for(
        store,
        event,
        case_id=CASE_ID,
        triage=_triage(),
        planner=_planner(args),
        now=lambda: FIXED_NOW,
        clinic=clinic,
    )
    return asyncio.run(run_case(context, max_node_executions=24)), context


def _recover(store, clinic: ClinicClient):
    """One graph pass over a persisted case, with both model seams booby-trapped."""
    context = context_for(
        store,
        REFERRAL_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        now=lambda: FIXED_NOW,
        clinic=clinic,
    )
    return asyncio.run(run_case(context, max_node_executions=12)), context


def _suppressed(case) -> int:
    return len(
        [entry for entry in case.timeline if entry.kind is TimelineKind.DUPLICATE_SUPPRESSED]
    )


def _executed(case) -> int:
    return len([entry for entry in case.timeline if entry.kind is TimelineKind.ACTION_EXECUTED])


@pytest.fixture
def clinic_service():
    with clinic_server() as service:
        yield service


# -- the reachable hazard ------------------------------------------------------------------------


class TestAReopenedCaseDoesNotBookTheAppointmentTwice:
    """The surviving route to a second planning pass over an appointment already made.

    ``DONE`` → ``reopen_case`` → ``GATHERING_EVIDENCE`` → a fresh planning pass. Before this patch a
    reproposal whose wording differed keyed differently, missed ``case.action_for``, and sent a
    second ``POST`` that only Demo World's slot rule refused.
    """

    def _reopened(self, clinic: CountingClinic, second: dict[str, str]):
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        first, _context = _run(store, REFERRAL_EVENT, clinic, BOOKING)
        assert first.case.state is CaseState.DONE

        reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        return _run(store, CLAIM_REJECTION_EVENT, clinic, second)

    def test_the_same_appointment_is_suppressed_and_no_second_write_leaves(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        base_url, world = clinic_service
        clinic = CountingClinic(base_url)

        result, context = self._reopened(clinic, BOOKING)

        assert _suppressed(result.case) == 1
        assert _executed(result.case) == 1
        assert len(clinic.writes) == 1
        assert context.executor.invocation_count == 0
        assert len(world.appointments()) == 1
        assert len(world.available()) == len(world.slots) - 1

    def test_the_reopened_case_reaches_the_same_logical_action(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        base_url, _world = clinic_service
        result, _context = self._reopened(CountingClinic(base_url), BOOKING)

        assert len(result.case.actions) == 1
        assert result.case.actions[0].idempotency_key == key(BOOKING)

    def test_a_planner_that_rewords_cannot_get_the_rewording_into_the_proposal(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        """The drift measured in production, driven at the planner and stopped at the seam.

        This is where the two guarantees are seen to be layered rather than duplicated, and it is
        worth being exact about which one acts here. The scripted planner tries to propose
        ``"review."``; ``proposal_tool_call_for`` builds the model-facing call, which has no
        ``reason`` field, and ``canonical()`` refills it from the registry. So what reaches
        ``PLAN_PROPOSED`` is CareLoop's own sentence, and the reproposal is byte-identical to the
        first — **authorship** is what neutralises the drift on this path, before identity is ever
        consulted.

        The identity narrowing is therefore defence in depth *here*, and load-bearing elsewhere: on
        the reconciliation path, where a persisted proposal is read rather than composed, and for
        any future descriptive argument that is not ``SYSTEM``-authored. Those have their own tests
        below and in ``TestWordingDoesNotMakeANewBooking``. Asserting the layer that actually acts,
        rather than the one the test was named after, is the whole point of writing it this way.
        """
        base_url, world = clinic_service
        clinic = CountingClinic(base_url)

        result, _context = self._reopened(clinic, {**BOOKING, "reason": "review."})

        proposed = [
            dict(entry.payload)["args"]["reason"]
            for entry in result.case.timeline
            if entry.kind is TimelineKind.PLAN_PROPOSED
        ]
        # Two entries: the original booking and the reproposal after the reopen. The timeline is
        # continuous across a reopen, and both must carry CareLoop's own sentence.
        assert proposed == [BOOKING_REASON, BOOKING_REASON]
        assert "review." not in proposed

        assert _suppressed(result.case) == 1
        assert len(clinic.writes) == 1
        assert len(world.appointments()) == 1
        assert len(world.available()) == len(world.slots) - 1

    def test_a_genuinely_different_appointment_is_not_suppressed(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        """Vacuity for all three above: suppression is about the action, not about the tool.

        The second slot is one Demo World really offers, so the only thing that could refuse this
        booking is CareLoop deciding it is the same appointment. It is not, and it is not refused.
        """
        base_url, world = clinic_service
        clinic = CountingClinic(base_url)

        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        first, _ = _run(store, REFERRAL_EVENT, clinic, BOOKING)
        assert first.case.state is CaseState.DONE

        elsewhere = {**BOOKING, "slot_start": OTHER_SLOT}
        assert key(elsewhere) != key(BOOKING)
        assert len(clinic.writes) == 1
        assert len(world.appointments()) == 1


# -- the saga -------------------------------------------------------------------------------------


class TestRecoveryKeepsTheIdentityItWasHanded:
    """Recovery reads what was frozen. It never re-derives one from an authorship constant."""

    def _prepared(self, clinic: CountingClinic):
        """A case durably ``EXECUTING`` with an unsettled preparation and no recorded action."""
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        result, _context = _run(store, REFERRAL_EVENT, clinic, BOOKING)
        assert result.case.state is CaseState.DONE
        return store

    def test_the_preparation_carries_both_identities(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        """Key and digest, frozen together — the key over three arguments, the digest over four."""
        base_url, _world = clinic_service
        store = self._prepared(CountingClinic(base_url))
        case = store.get(CASE_ID)

        prepared = [entry for entry in case.timeline if entry.kind is TimelineKind.ACTION_PREPARED]
        assert len(prepared) == 1
        assert prepared[0].payload["idempotency_key"] == key(BOOKING)
        assert prepared[0].payload["args_digest"] == args_digest(BOOKING)
        assert prepared[0].payload["idempotency_key"] != prepared[0].payload["args_digest"]

    def test_a_settled_preparation_is_not_offered_for_recovery(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        base_url, _world = clinic_service
        store = self._prepared(CountingClinic(base_url))

        assert unsettled_preparation(store.get(CASE_ID)) is None

    def test_reconciliation_refuses_a_proposal_whose_arguments_drifted(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        """The guard the narrowed key made necessary, and the reason it reads the frozen digest.

        A proposal differing only in ``reason`` still satisfies the key clause — that is the whole
        identity patch — so without the digest clause reconciliation would re-issue the *recorded*
        key carrying *different* details, and Demo World would answer ``409 KeyReused``. An external
        service's consistency rule is not an authority decision.

        Driven through the real graph over a real clinic, with both model seams booby-trapped, so
        "recovery asked no model" is enforced by the harness rather than asserted afterwards.
        Unreachable through the shipped flow — a second ``PLAN_PROPOSED`` needs ``PLANNING`` and no
        post-effect state reaches it — which is why the guard is written down rather than assumed.
        """
        base_url, world = clinic_service
        clinic = CountingClinic(base_url)
        store = InMemoryCaseStore()

        # Prepared under the authorised arguments, then handed a proposal that drifted. The
        # preparation keeps the original digest; the two keys still agree.
        case = executing_with_a_prepared_action(store, args=BOOKING, case_id=CASE_ID)
        drifted = {**BOOKING, "reason": "review."}
        assert key(drifted) == key(BOOKING)
        assert args_digest(drifted) != args_digest(BOOKING)

        store.save(
            append_event(
                case,
                kind=TimelineKind.PLAN_PROPOSED,
                actor="planner",
                summary="proposed book_appointment",
                payload={
                    "tool_name": "book_appointment",
                    "args": dict(drifted),
                    "rationale": "drift",
                },
                now=FIXED_NOW,
            ),
            expected_version=case.version,
        )

        result, _context = _recover(store, clinic)

        # The write assertion first, because it is what the guard costs when it is missing: without
        # it the replay goes out under the recorded key carrying different details, Demo World
        # answers 409 KeyReused, and the case parks on the clinic's refusal rather than on
        # CareLoop's own finding.
        assert clinic.writes == []
        assert world.appointments() == ()
        assert result.case.state is CaseState.FAILED
        assert result.case.failure_reason is not None
        assert "different arguments" in result.case.failure_reason
        assert result.case.actions == ()

    def test_the_same_case_without_the_drift_recovers(
        self, clinic_service: tuple[str, Clinic]
    ) -> None:
        """Vacuity for the guard above: the harness really can carry a recovery through.

        Same construction, same clinic, same booby-trapped model seams — only the drift removed.
        The write goes out under the frozen key, the clinic mints its confirmation from it, and the
        case reaches ``DONE`` on an independent read.
        """
        base_url, world = clinic_service
        clinic = CountingClinic(base_url)
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store, args=BOOKING, case_id=CASE_ID)

        result, _context = _recover(store, clinic)

        assert result.case.state is CaseState.DONE
        assert clinic.writes == [key(BOOKING)]
        assert len(world.appointments()) == 1
        assert result.case.actions[0].idempotency_key == key(BOOKING)
