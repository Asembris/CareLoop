"""Asking CareLoop to look again at the exact thing it already did.

``tests/test_post_effect_lifecycle.py`` closed the safety half: once an external effect has been
attempted and cannot be certified, the case parks for a person and never returns to autonomous
planning or execution. That left a **liveness** gap, and it was total — a caregiver who went to the
clinic, found the record wrong and corrected it had no supported way to tell CareLoop, and the case
stayed parked forever. Measured before this patch: ``approve`` and ``reject`` raise
``NoSuchApproval`` because the parking raises no approval, ``reopen_case`` refuses anything that is
not ``DONE``, ``POST /api/events`` is that refusal over HTTP, and a bare ``run_case`` executes
``("dispatch",)`` and commits nothing because ``AWAITING_HUMAN`` satisfies no outgoing edge.

This file owns the operation that closes it, and the whole of what makes it safe is that it is
**observation and not authority**:

    a re-check re-reads the exact previously recorded effect named by the current post-effect
    handoff, and does nothing else.

Three classes carry the weight and the rest are the matrix around them.

:class:`TestAReCheckWithNoObserverCannotCertify` is the load-bearing one. Adding the transition
without it reproduces a real false-``DONE``: the closed-loop checks all pass, because the booking
*response* was always correct and only the world diverged, so a re-check run that omits the clinic
verifies clean and certifies a case whose external record is still wrong. That is certifying by
*removing* evidence — going from "we looked and could not confirm" to "we did not look, so it is
fine" — and it is one deployment-wiring change away at any time.

:class:`TestTheReCheckObservesTheRecordTheHandoffNames` is the identity proof, on a case carrying
two bookings with different confirmation ids where the handoff names the first.

:class:`TestAReCheckIsNotAuthority` is the isolation proof: no proposal, no policy verdict, no
approval, no executor invocation, no new ``ActionRecord``, and — the sharper assertion —
**zero ``DUPLICATE_SUPPRESSED``**, because the point is that the effect path is never entered
rather than that idempotency caught it on the way out.

The misbehaving clinics are imported from ``test_verify_readback`` rather than rebuilt, for the
reason ``test_post_effect_lifecycle`` imports them: they are the same divergences, and a second copy
would be a second thing to keep in step.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from clinic_helpers import clinic_server
from helpers import FIXED_NOW
from pydantic import ValidationError
from test_post_effect_lifecycle import _planner, _triage, count, entries, run_against, seq_of
from test_verify_readback import (
    BOOKING,
    CASE_ID,
    CannotBeReadBack,
    ChangesTheReason,
    LosesTheRecord,
    SwapsThePatient,
)

from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.context import (
    RecheckTarget,
    entered_verifying_from,
    manual_handoff,
    recheck_target,
)
from careloop.graph.runner import CaseNotRecheckable, context_for, request_recheck, run_case
from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE, ObservationOutcome
from careloop.policy.verdicts import ReasonCode
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.case import ActionRecord
from careloop.tools.clinic import ClinicClient, ClinicUnavailable
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.results import AppointmentBooked, EmailSent
from demoworld.clinic import Clinic

#: The two nodes a re-check run may execute. No plan, no gate, no execute.
RECHECK_PATH = ("dispatch", "verify")

CARER = "rosa.marlowe@carer.example"


# -- observers that record what they were asked ---------------------------------------------------


class RecordsWhatItWasAsked(ClinicClient):
    """A real clinic client that writes down every confirmation id it is asked to read.

    The instrument for :class:`TestTheReCheckObservesTheRecordTheHandoffNames`. Asserting that two
    key strings compare equal proves the *bookkeeping*; this proves which external object was
    actually fetched, which is the thing a caregiver is relying on.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.observed: list[str] = []

    def appointment(self, confirmation_id: str) -> AppointmentBooked:  # type: ignore[override]
        self.observed.append(confirmation_id)
        return super().appointment(confirmation_id)


class StillCannotBeRead(ClinicClient):
    """An observer that counts its attempts and never succeeds.

    Counting is the point: Patch 2B is deferred, so a re-check must perform **one** observation
    episode and stop. A test that only asserted the outcome would pass against a retry loop.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.attempts = 0

    def appointment(self, confirmation_id: str) -> AppointmentBooked:  # type: ignore[override]
        self.attempts += 1
        raise ClinicUnavailable(
            f"the clinic at {self.base_url} could not be reached: ConnectTimeout"
        )


# -- building a parked case, and re-checking it ---------------------------------------------------


def park(clinic_factory, world, base_url: str) -> tuple[Case, InMemoryCaseStore]:
    """Run a booking to rest against a misbehaving clinic and return the parked case."""
    result, context = run_against(clinic_factory(base_url, world))
    assert result.case.state is CaseState.AWAITING_HUMAN, result.case.state
    return result.case, context.store


def recheck_run(
    store: InMemoryCaseStore,
    clinic: ClinicClient | None,
    *,
    proposals: Sequence[dict[str, str]] = (BOOKING,) * 4,
):
    """Run the graph once over a case the caregiver has already asked to have re-checked.

    The planner script is supplied and deliberately non-empty: the mock raises rather than
    inventing an answer, so a regression that reached the planner reports ``MockScriptExhausted``
    standing in front of the real failure. Nothing reaches it while the re-check is correct.
    """
    context = context_for(
        store,
        REFERRAL_EVENT,
        case_id=CASE_ID,
        triage=_triage(),
        planner=_planner(proposals),
        now=lambda: FIXED_NOW,
        clinic=clinic,
    )
    return asyncio.run(run_case(context, max_node_executions=8)), context


def counts(case: Case) -> dict[str, int]:
    """Everything a re-check must not move, in one comparable mapping."""
    return {
        "actions": len(case.actions),
        "approvals": len(case.approvals),
        "PLAN_PROPOSED": count(case, TimelineKind.PLAN_PROPOSED),
        "POLICY_ALLOW": count(case, TimelineKind.POLICY_ALLOW),
        "POLICY_DENY": count(case, TimelineKind.POLICY_DENY),
        "ACTION_EXECUTED": count(case, TimelineKind.ACTION_EXECUTED),
        "DUPLICATE_SUPPRESSED": count(case, TimelineKind.DUPLICATE_SUPPRESSED),
        "APPROVAL_REQUESTED": count(case, TimelineKind.APPROVAL_REQUESTED),
    }


def handoffs(case: Case) -> list:
    return entries(case, TimelineKind.MANUAL_HANDOFF_REQUIRED)


def requests(case: Case) -> list:
    return entries(case, TimelineKind.EXTERNAL_RECHECK_REQUESTED)


def assert_no_authority_was_restored(before: Case, after: Case, context) -> None:
    """The isolation property, asserted once and reused by every scenario below.

    Each clause fails against a different way of getting this wrong, which is why they are not one
    another restated. ``DUPLICATE_SUPPRESSED == 0`` is the sharpest: a re-check that routed through
    ``ExecuteNode`` and was saved by the idempotency ledger would leave the other counts intact and
    this one at one.
    """
    assert counts(after) == counts(before)
    assert counts(after)["DUPLICATE_SUPPRESSED"] == 0
    assert context.executor.invocation_count == 0
    assert after.actions == before.actions
    assert after.approvals == ()
    assert after.pending_approval() is None
    assert [record.result for record in after.actions] == [
        record.result for record in before.actions
    ]


# -- 1: the corrected world -----------------------------------------------------------------------


class TestACorrectedExternalRecordCanBeReChecked:
    """**The primary Patch 2C proof.** The clinic was wrong, a person fixed it, the case completes.

    The divergence is the narrowest the suite can construct: confirmation, provider, patient and
    slot are all exactly what CareLoop executed, and only the free-text ``reason`` moved. So the
    only thing that can have failed the first verification is that comparison, and the only thing
    that can pass the second is the correction.
    """

    @staticmethod
    def _corrected(base_url: str, world: Clinic):
        parked, store = park(ChangesTheReason, world, base_url)
        held = world.appointments()[0]
        world.amend(held.confirmation_id, reason=BOOKING["reason"])
        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
        result, context = recheck_run(store, ClinicClient(base_url))
        return parked, result, context

    def test_the_case_completes_through_the_ordinary_verifier(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, result, _context = self._corrected(base_url, world)

        assert result.case.state is CaseState.DONE
        assert result.executed_nodes == RECHECK_PATH
        passed = entries(result.case, TimelineKind.VERIFICATION_PASSED)
        assert len(passed) == 1
        assert passed[0].payload["observation"] == str(ObservationOutcome.MATCHED)
        assert passed[0].payload["read_back"] == "true"

    def test_nothing_was_planned_authorised_or_executed(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, result, context = self._corrected(base_url, world)

        assert_no_authority_was_restored(parked, result.case, context)

    def test_the_same_action_is_still_the_only_one_and_its_result_is_byte_identical(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The strongest available statement that nothing was re-performed.

        Not "one ``ActionRecord``" — that would also hold if the effect had been repeated and
        suppressed. The recorded key, the recorded digest and the recorded result bytes are all
        unchanged, and ``careloop/tools/`` carries no clock, so byte-identical is a real assertion
        rather than an approximation.
        """
        base_url, world = clinic_service
        parked, result, _context = self._corrected(base_url, world)

        assert len(result.case.actions) == len(parked.actions) == 1
        assert result.case.actions[0] == parked.actions[0]
        assert result.case.actions[0].result == parked.actions[0].result

    def test_the_audit_trail_names_the_request_the_transition_and_the_action(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, result, _context = self._corrected(base_url, world)
        case = result.case

        asked = requests(case)
        assert len(asked) == 1
        assert asked[0].actor == f"human:{CARER}"
        assert asked[0].payload["tool_name"] == "book_appointment"
        assert (
            asked[0].payload["idempotency_key"]
            == handoffs(parked)[0].payload["idempotency_key"]
            == case.actions[0].idempotency_key
        )

    def test_no_effect_event_stands_between_the_request_and_completion(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The judge-visible story, asserted as an ordered slice rather than as counts."""
        base_url, world = clinic_service
        _parked, result, _context = self._corrected(base_url, world)
        asked_at = seq_of(result.case, TimelineKind.EXTERNAL_RECHECK_REQUESTED)

        after = [event.kind for event in result.case.timeline if event.seq > asked_at]

        assert after == [
            TimelineKind.STATE_TRANSITION,
            TimelineKind.VERIFICATION_PASSED,
            TimelineKind.STATE_TRANSITION,
        ]

    def test_the_transition_is_from_awaiting_human_and_names_the_caregiver(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, result, _context = self._corrected(base_url, world)
        moves = [
            event
            for event in result.case.timeline
            if event.kind is TimelineKind.STATE_TRANSITION
            and event.payload["from"] == str(CaseState.AWAITING_HUMAN)
        ]

        assert len(moves) == 1
        assert moves[0].payload["to"] == str(CaseState.VERIFYING)
        assert moves[0].actor == f"human:{CARER}"

    def test_the_clinic_holds_exactly_one_appointment_throughout(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity: the case did not complete because a second booking papered over the first."""
        base_url, world = clinic_service
        self._corrected(base_url, world)

        assert len(world.appointments()) == 1


# -- 2: the world that was not corrected ----------------------------------------------------------


class TestAReCheckOfAStillWrongRecordParksAgain:
    """Three evidence states, one lifecycle, and the first handoff untouched in all of them.

    The re-check is not a second chance for the *effect*; it is a second *look*. When the look
    still fails, the case goes back exactly where it was, and the trail keeps both episodes.
    """

    @staticmethod
    def _rechecked(clinic_factory, base_url: str, world: Clinic, observer=None):
        parked, store = park(clinic_factory, world, base_url)
        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
        result, context = recheck_run(
            store, observer if observer is not None else clinic_factory(base_url, world)
        )
        return parked, result, context

    @pytest.mark.parametrize(
        ("clinic_factory", "outcome"),
        [
            (ChangesTheReason, ObservationOutcome.DIVERGED),
            (SwapsThePatient, ObservationOutcome.DIVERGED),
            (LosesTheRecord, ObservationOutcome.ABSENT),
        ],
        ids=["diverged-reason", "diverged-patient", "absent"],
    )
    def test_it_parks_again_with_the_evidence_state_recorded(
        self,
        clinic_service: tuple[str, Clinic],
        clinic_state: Clinic,
        clinic_factory,
        outcome,
    ) -> None:
        base_url, world = clinic_service
        parked, result, context = self._rechecked(clinic_factory, base_url, world)
        case = result.case

        assert case.state is CaseState.AWAITING_HUMAN
        assert result.executed_nodes == RECHECK_PATH
        assert_no_authority_was_restored(parked, case, context)

        failures = entries(case, TimelineKind.VERIFICATION_FAILED)
        assert len(failures) == 2
        assert failures[1].payload["observation"] == str(outcome)

    def test_the_first_handoff_is_not_overwritten_and_the_second_names_the_same_action(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, result, _context = self._rechecked(ChangesTheReason, base_url, world)

        both = handoffs(result.case)
        assert len(both) == 2
        assert both[0] == handoffs(parked)[0]
        assert both[1].payload["idempotency_key"] == both[0].payload["idempotency_key"]
        assert both[1].payload["reason_code"] == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)

    def test_the_case_can_be_re_checked_again(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Liveness is not spent by one attempt. A caregiver who fixes it later still can."""
        base_url, world = clinic_service
        _parked, result, _context = self._rechecked(ChangesTheReason, base_url, world)

        assert recheck_target(result.case) is not None

    def test_an_unavailable_read_is_attempted_exactly_once(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Patch 2B is deferred, and this is what says so rather than a comment.

        Counting the observer's own attempts rather than counting ``verify`` executions: a retry
        loop *inside* the node would leave the node count at one and this at more.
        """
        base_url, world = clinic_service
        parked, store = park(CannotBeReadBack, world, base_url)
        observer = StillCannotBeRead(base_url)
        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
        result, context = recheck_run(store, observer)

        assert observer.attempts == 1
        assert result.executed_nodes.count("verify") == 1
        assert result.case.state is CaseState.AWAITING_HUMAN
        assert entries(result.case, TimelineKind.VERIFICATION_FAILED)[1].payload["observation"] == (
            str(ObservationOutcome.UNAVAILABLE)
        )
        assert_no_authority_was_restored(parked, result.case, context)


# -- 3: the observer that was not there -----------------------------------------------------------


class TestAReCheckWithNoObserverCannotCertify:
    """**The load-bearing class.** A re-check that reads nothing may not certify anything.

    Reproduced against the tree before this patch: force the transition, run with ``clinic=None``,
    and the case reaches ``DONE`` reporting ``observation=NOT_OBSERVED`` and ``read_back=false``
    while Demo World still holds ``ATTACKER SUPPLIED REASON``. Every closed-loop check passes
    because they always did — the booking *response* was correct, and only the world diverged — so
    dropping the observer does not make verification fail, it makes it *stop looking*.

    The rule that makes the guard principled rather than ad hoc: a verification may not certify a
    case whose own trail already records that an independent read of this action was attempted and
    did not confirm it, unless an independent read is performed now. Certifying without one would
    be certifying by *removing* evidence.

    Scoped to the re-check episode and to nothing else — see
    :class:`TestTheGuardIsScopedToReChecks`, which is the mandatory control.
    """

    @staticmethod
    def _unobserved(base_url: str, world: Clinic):
        parked, store = park(ChangesTheReason, world, base_url)
        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
        result, context = recheck_run(store, None)
        return parked, result, context

    def test_the_case_does_not_reach_done(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, result, _context = self._unobserved(base_url, world)

        assert result.case.state is not CaseState.DONE
        assert result.case.state is CaseState.AWAITING_HUMAN
        assert count(result.case, TimelineKind.VERIFICATION_PASSED) == 0

    def test_it_fails_closed_saying_nothing_was_read(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, result, _context = self._unobserved(base_url, world)

        failure = entries(result.case, TimelineKind.VERIFICATION_FAILED)[1]
        assert failure.payload["observation"] == str(ObservationOutcome.NOT_OBSERVED)
        assert failure.payload["read_back"] == "false"

    def test_it_parks_through_the_ordinary_handoff(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """No new state, no new reason code — the existing failure lifecycle, reused."""
        base_url, world = clinic_service
        parked, result, context = self._unobserved(base_url, world)

        both = handoffs(result.case)
        assert len(both) == 2
        assert both[1].payload["reason_code"] == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)
        assert both[1].payload["idempotency_key"] == parked.actions[0].idempotency_key
        assert_no_authority_was_restored(parked, result.case, context)

    def test_the_world_really_was_still_wrong(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity: the guard is what refused, not a clinic that happened to agree."""
        base_url, world = clinic_service
        self._unobserved(base_url, world)

        assert world.appointments()[0].reason == ChangesTheReason.OTHER_REASON


class TestTheGuardIsScopedToReChecks:
    """**The mandatory control.** ``NOT_OBSERVED`` is not globally redefined as failure.

    A first-pass run with no external system to read still verifies closed-loop and still reaches
    ``DONE``, saying plainly that it read nothing. Three of the four effect tools have no external
    system at all, so a guard that fired on every ``NOT_OBSERVED`` would break the ordinary
    simulated path — which is most of the demo.
    """

    def test_a_fully_simulated_first_pass_booking_still_reaches_done(self) -> None:
        result, _context = run_against(None)

        assert result.case.state is CaseState.DONE
        passed = entries(result.case, TimelineKind.VERIFICATION_PASSED)[0]
        assert passed.payload["read_back"] == "false"
        assert passed.payload["observation"] == str(ObservationOutcome.NOT_OBSERVED)

    def test_the_discriminator_tells_the_two_episodes_apart(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The primitive the guard reads, asserted directly.

        Durable, and written by :func:`careloop.state.machine.transition` rather than held in a
        run-scoped flag — so it survives a process boundary and describes *this* episode rather
        than "a re-check happened once on this case".
        """
        base_url, world = clinic_service
        result, _context = run_against(None)
        assert entered_verifying_from(result.case) is CaseState.EXECUTING

        parked, store = park(ChangesTheReason, world, base_url)
        assert entered_verifying_from(parked) is CaseState.EXECUTING

        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

        assert entered_verifying_from(store.get(CASE_ID)) is CaseState.AWAITING_HUMAN

    def test_a_case_that_never_verified_has_no_episode(self) -> None:
        from careloop.graph.runner import open_case

        store = InMemoryCaseStore()
        opened = open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert entered_verifying_from(opened) is None


# -- 4: which external object is read -------------------------------------------------------------


class TestTheReCheckObservesTheRecordTheHandoffNames:
    """A case carrying two bookings, where the handoff names the one that is **not** last.

    "Which action could not be certified" and "which action was recorded most recently" are
    different questions that agree on every case the shipped flow produces today — the exact shape
    of accident this repository has been bitten by three times. So the decoy sits *after* the real
    booking and carries its own distinct confirmation id.

    Three separate assertions, carried by three different mechanisms, and it is worth being precise
    about which does what:

    *The audit identity* comes from :func:`careloop.graph.context.recheck_target`, which resolves
    through ``handoff.idempotency_key``. A resolver reading ``case.actions[-1]`` names the decoy
    here and this is what catches it.

    *The external object read* comes from :class:`careloop.graph.nodes.VerifyNode`, which derives
    its own target from ``latest_proposal``. That derivation is deliberately not overridden — the
    re-check's job is to **prove the two agree before scheduling**, not to hand the node a target.

    *The consistency guard* is what makes the second sentence true rather than hopeful, and
    :class:`TestAnIdentityMismatchIsRefused` is where it is exercised.
    """

    @staticmethod
    def _two_bookings(base_url: str, world: Clinic):
        """A parked case whose second, later ``ActionRecord`` is a different booking."""
        parked, store = park(ChangesTheReason, world, base_url)
        held = world.appointments()[0]
        world.amend(held.confirmation_id, reason=BOOKING["reason"])

        other = {**BOOKING, "slot_start": "2026-09-04T14:00:00"}
        decoy = ActionRecord(
            idempotency_key=idempotency_key(CASE_ID, "book_appointment", other),
            action_type="book_appointment",
            args_digest=args_digest(other),
            result=AppointmentBooked(
                confirmation_id="apt-decoy-never-minted",
                provider_id=other["provider_id"],
                patient_id=other["patient_id"],
                slot_start=other["slot_start"],
                reason=other["reason"],
            ).model_dump_json(),
            recorded_at=FIXED_NOW,
        )
        case = store.get(CASE_ID)
        store.save(
            case.model_copy(
                update={"actions": (*case.actions, decoy), "version": case.version + 1}
            ),
            expected_version=case.version,
        )
        return parked, store, decoy

    def test_the_setup_really_does_put_the_decoy_last(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity: without this the assertions below pass against a latest-record resolver."""
        base_url, world = clinic_service
        parked, store, decoy = self._two_bookings(base_url, world)
        case = store.get(CASE_ID)

        assert [record.action_type for record in case.actions] == [
            "book_appointment",
            "book_appointment",
        ]
        assert case.actions[-1].idempotency_key == decoy.idempotency_key
        assert decoy.idempotency_key != parked.actions[0].idempotency_key

    def test_the_resolver_names_the_handoffs_record_and_not_the_later_one(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, store, decoy = self._two_bookings(base_url, world)
        # One load, because the store re-parses JSON on every read: identity is only meaningful
        # within one object graph, and that is exactly the claim being made — the record the
        # resolver returns is the one sitting in this case's own action ledger.
        case = store.get(CASE_ID)

        target = recheck_target(case)

        assert target is not None
        assert target.idempotency_key == parked.actions[0].idempotency_key
        assert target.idempotency_key != decoy.idempotency_key
        assert target.action is case.actions[0]
        assert target.action is not case.actions[-1]

    def test_the_confirmation_id_actually_fetched_is_the_handoff_bound_one(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The external object, not two strings that compare equal."""
        base_url, world = clinic_service
        parked, store, _decoy = self._two_bookings(base_url, world)
        observer = RecordsWhatItWasAsked(base_url)
        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
        result, _context = recheck_run(store, observer)

        booked = AppointmentBooked.model_validate_json(parked.actions[0].result)
        assert observer.observed == [booked.confirmation_id]
        assert "apt-decoy-never-minted" not in observer.observed
        assert result.case.state is CaseState.DONE

    def test_the_audit_event_names_that_record_too(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, store, decoy = self._two_bookings(base_url, world)
        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

        asked = requests(store.get(CASE_ID))[0]

        assert asked.payload["idempotency_key"] == parked.actions[0].idempotency_key
        assert asked.payload["idempotency_key"] != decoy.idempotency_key


# -- 5: the identity guard ------------------------------------------------------------------------


class TestAnIdentityMismatchIsRefused:
    """The handoff names one action and the current proposal keys to another. Refuse.

    :class:`careloop.graph.nodes.VerifyNode` resolves its own target from ``latest_proposal``, so a
    re-check that did not check this would have the run observe a *different* object than the
    handoff names — silently, with a timeline that reads correctly throughout. Unreachable under the
    shipped flow, because a parked case never plans again and a reopened one's newest handoff always
    matches its newest proposal; it is a fail-closed guard against a persisted or tampered case, and
    against the next change that makes the two drift.

    Never a silent choice between the two identities. The two disagreeing is the finding.
    """

    @staticmethod
    def _mismatched(base_url: str, world: Clinic):
        """A parked case whose latest proposal is for a booking the handoff does not name."""
        from careloop.state.machine import append_event

        parked, store = park(ChangesTheReason, world, base_url)
        case = store.get(CASE_ID)
        # A different *slot* rather than a different reason. ``reason`` is declared non-identifying,
        # so rewording it no longer moves the key — which is the whole of the identity patch and
        # would make this guard's instrument a no-op. A time is an identifying coordinate, and the
        # clinic's own slot table agrees that it names a different appointment.
        drifted = {**BOOKING, "slot_start": "2026-09-04T14:00:00"}
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
        return parked, store

    def test_the_resolver_refuses(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = self._mismatched(base_url, world)

        assert recheck_target(store.get(CASE_ID)) is None

    def test_the_request_is_refused_and_nothing_moves(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = self._mismatched(base_url, world)
        before = store.get(CASE_ID)

        with pytest.raises(CaseNotRecheckable):
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

        after = store.get(CASE_ID)
        assert (after.version, after.state) == (before.version, before.state)
        assert after.timeline == before.timeline
        assert after.actions == before.actions
        assert after.approvals == before.approvals

    def test_no_external_read_happens(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Refused *before* observing, not after observing the wrong thing."""
        base_url, world = clinic_service
        _parked, store = self._mismatched(base_url, world)
        observer = RecordsWhatItWasAsked(base_url)

        with pytest.raises(CaseNotRecheckable):
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

        assert observer.observed == []

    def test_the_two_identities_really_do_disagree(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Vacuity: the refusal is the mismatch and not something else about the case."""
        base_url, world = clinic_service
        _parked, store = self._mismatched(base_url, world)
        case = store.get(CASE_ID)
        from careloop.graph.context import latest_proposal

        proposal = latest_proposal(case)
        derived = idempotency_key(CASE_ID, proposal.tool_name, proposal.args)

        assert manual_handoff(case).idempotency_key != derived
        assert case.action_for(manual_handoff(case).idempotency_key) is not None


# -- 6: what the resolver will not accept ---------------------------------------------------------


class TestWhatIsNotReCheckable:
    """Everything the static resolver refuses, and why each refusal is its own property."""

    def test_a_handoff_with_no_recorded_identity_is_refused(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """A pre-2A handoff. **No fallback, no migration, no latest-action guess.**

        A record written before the identity was ever observed cannot truthfully satisfy an
        operation whose defining property is exact action identity.
        """
        from careloop.state.machine import append_event, transition

        base_url, world = clinic_service
        parked, _store = park(ChangesTheReason, world, base_url)
        historical = parked.model_copy(
            update={
                "state": CaseState.VERIFYING,
                "timeline": tuple(
                    event
                    for event in parked.timeline
                    if event.kind is not TimelineKind.MANUAL_HANDOFF_REQUIRED
                    and not (
                        event.kind is TimelineKind.STATE_TRANSITION
                        and event.payload["to"] == str(CaseState.AWAITING_HUMAN)
                    )
                ),
            }
        )
        historical = append_event(
            historical,
            kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
            actor="system",
            summary="book_appointment was carried out but could not be confirmed",
            payload={
                "tool_name": "book_appointment",
                "reason_code": str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED),
            },
            now=FIXED_NOW,
        )
        historical = transition(
            historical, CaseState.AWAITING_HUMAN, reason="legacy", actor="system", now=FIXED_NOW
        )

        handoff = manual_handoff(historical)
        assert handoff is not None
        assert handoff.reason_code == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)
        assert handoff.tool_name == "book_appointment"
        assert handoff.idempotency_key is None
        assert recheck_target(historical) is None

    def test_a_nothing_safe_to_do_handoff_is_refused(self) -> None:
        """Nothing was carried out, so there is no external result to observe."""
        from test_no_safe_action import EVERY_TOOL_REFUSED, drive

        parked, _context = drive(*EVERY_TOOL_REFUSED)
        handoff = manual_handoff(parked)

        assert handoff is not None
        assert handoff.reason_code == str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)
        assert handoff.tool_name is None
        assert handoff.idempotency_key is None
        assert recheck_target(parked) is None

    def test_a_case_with_a_pending_approval_is_refused(self) -> None:
        """An ordinary decision request. A re-check endpoint is not a second decision surface."""
        from api_helpers import parked_store

        case = parked_store().get(CASE_ID)

        assert case.state is CaseState.AWAITING_HUMAN
        assert case.pending_approval() is not None
        assert manual_handoff(case) is None
        assert recheck_target(case) is None

    def test_a_handoff_whose_key_names_no_action_is_refused(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, _store = park(ChangesTheReason, world, base_url)
        orphaned = parked.model_copy(update={"actions": ()})

        assert manual_handoff(orphaned).idempotency_key is not None
        assert orphaned.action_for(manual_handoff(orphaned).idempotency_key) is None
        assert recheck_target(orphaned) is None

    def test_an_action_type_with_no_independent_observer_is_refused(self) -> None:
        """A closed-loop re-read over unchanged local data is not external recovery.

        ``send_email`` has no external system to consult, so "re-checking" one would compare
        CareLoop's record against CareLoop's record and certify on the strength of it. That is the
        no-observer hazard with the paperwork in order, and the capability declaration is what
        refuses it.
        """
        from careloop.graph.runner import open_case
        from careloop.state.machine import append_event, transition

        case = open_case(InMemoryCaseStore(), REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        email = {"to": CARER, "subject": "an update", "body": "the referral has arrived"}
        key = idempotency_key(CASE_ID, "send_email", email)
        case = case.model_copy(
            update={
                "actions": (
                    ActionRecord(
                        idempotency_key=key,
                        action_type="send_email",
                        args_digest=args_digest(email),
                        result=EmailSent(
                            message_id=f"msg-{key[:16]}", to=email["to"], subject=email["subject"]
                        ).model_dump_json(),
                        recorded_at=FIXED_NOW,
                    ),
                )
            }
        )
        case = append_event(
            case,
            kind=TimelineKind.PLAN_PROPOSED,
            actor="planner",
            summary="proposed send_email",
            payload={"tool_name": "send_email", "args": dict(email), "rationale": "r"},
            now=FIXED_NOW,
        )
        case = append_event(
            case,
            kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
            actor="system",
            summary="send_email was carried out but could not be confirmed",
            payload={
                "tool_name": "send_email",
                "reason_code": str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED),
                "idempotency_key": key,
            },
            now=FIXED_NOW,
        )
        for target in (
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.AWAITING_HUMAN,
        ):
            case = transition(case, target, reason="setup", actor="test", now=FIXED_NOW)

        assert manual_handoff(case).idempotency_key == key
        assert case.action_for(key) is not None
        assert "send_email" not in INDEPENDENTLY_OBSERVABLE
        assert recheck_target(case) is None

    @pytest.mark.parametrize(
        "state",
        [
            CaseState.NEW,
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.WAITING_EXTERNAL,
            CaseState.DONE,
            CaseState.FAILED,
        ],
    )
    def test_no_state_but_awaiting_human_is_re_checkable(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, state: CaseState
    ) -> None:
        """The whole matrix, driven off ``CaseState`` so a new member joins it automatically."""
        base_url, world = clinic_service
        parked, _store = park(ChangesTheReason, world, base_url)
        moved = parked.model_copy(update={"state": state})

        assert recheck_target(moved) is None


# -- 7: the request is made once ------------------------------------------------------------------


class TestARequestIsMadeOnce:
    """Double-click semantics, deterministically and without a race.

    After the first request commits, the case is ``VERIFYING`` and the state precondition refuses
    the second. No timing dependency: the guard is the durable state, not a window.
    """

    def test_a_second_request_is_refused_and_appends_nothing(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = park(ChangesTheReason, world, base_url)

        request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
        after_first = store.get(CASE_ID)

        with pytest.raises(CaseNotRecheckable):
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)

        after_second = store.get(CASE_ID)
        assert after_second.version == after_first.version
        assert after_second.timeline == after_first.timeline
        assert len(requests(after_second)) == 1


# -- 8: the capability declaration ----------------------------------------------------------------


class TestObserverCapability:
    """What "independently observable" is declared to mean, and that the wiring agrees.

    Capability is *static*: this action type has a real external system behind it. Availability is
    *runtime*: this particular run has the observer bound. Both are needed and they are different
    questions — a case may be structurally re-checkable while a misconfigured run has no clinic.
    """

    def test_the_two_real_providers_are_declared_observable(self) -> None:
        assert frozenset({"book_appointment", "upload_document"}) == INDEPENDENTLY_OBSERVABLE

    def test_every_declared_type_is_an_effect_tool(self) -> None:
        from careloop.tools.registry import REGISTRY

        assert REGISTRY.external_effect_tools >= INDEPENDENTLY_OBSERVABLE

    def test_the_two_simulated_effects_are_deliberately_absent(self) -> None:
        """Not an oversight. Equality with the registry would be the wrong assertion.

        ``send_email`` and ``grant_portal_access`` have no real provider and no independent
        observer, so declaring them would force two fake observers into existence.
        """
        from careloop.tools.registry import REGISTRY

        assert REGISTRY.external_effect_tools - INDEPENDENTLY_OBSERVABLE == frozenset(
            {"send_email", "grant_portal_access"}
        )

    def test_a_clinic_bound_run_realises_the_clinics_half_of_the_declaration(self) -> None:
        """The drift guard, scoped to one provider now that there are two.

        It asserted equality with :data:`INDEPENDENTLY_OBSERVABLE` while the clinic was the only
        real provider, which conflated two facts: *this action type can be observed at all*, and
        *this run has the observer for it bound*. With a second provider the two come apart —
        a clinic-bound run realises the clinic's half and nothing else — and that is exactly the
        capability-versus-availability distinction this class exists to keep separate.

        A **subset**, so the drift it was written for is still caught: a read-back wired up for an
        action type nothing declares observable would fail here, which is the direction that would
        let a re-check be accepted for an action nothing can observe.
        """
        with clinic_server() as (base_url, _world):
            store = InMemoryCaseStore()
            from careloop.graph.runner import open_case

            open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
            bound = context_for(
                store, REFERRAL_EVENT, case_id=CASE_ID, clinic=ClinicClient(base_url)
            )
            unbound = context_for(store, REFERRAL_EVENT, case_id=CASE_ID)

        assert frozenset(bound.readbacks) == frozenset({"book_appointment"})
        assert frozenset(bound.readbacks) <= INDEPENDENTLY_OBSERVABLE
        assert frozenset(unbound.readbacks) == frozenset()


# -- 9: the read model ----------------------------------------------------------------------------


class TestTheResolverIsOneAnswer:
    """``recheck_target`` is the single source of truth for static eligibility.

    The writer and the projector both read it, so the button a caregiver sees and the endpoint that
    validates their click cannot disagree about whether a handoff is bound to a valid target.
    """

    def test_it_returns_the_tool_the_key_and_the_record(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, _store = park(ChangesTheReason, world, base_url)

        target = recheck_target(parked)

        assert isinstance(target, RecheckTarget)
        assert target.tool_name == "book_appointment"
        assert target.idempotency_key == parked.actions[0].idempotency_key
        assert target.action is parked.actions[0]

    def test_it_is_frozen(self, clinic_service: tuple[str, Clinic], clinic_state: Clinic) -> None:
        base_url, world = clinic_service
        parked, _store = park(ChangesTheReason, world, base_url)

        with pytest.raises(ValidationError):
            recheck_target(parked).tool_name = "send_email"

    def test_it_writes_nothing(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """A pure reader. Calling it repeatedly moves no version and appends no entry."""
        base_url, world = clinic_service
        parked, store = park(ChangesTheReason, world, base_url)

        for _ in range(3):
            recheck_target(store.get(CASE_ID))

        after = store.get(CASE_ID)
        assert (after.version, after.timeline) == (parked.version, parked.timeline)
