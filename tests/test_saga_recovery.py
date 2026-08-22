"""What CareLoop does with an authorised action a dead process left unrecorded.

The gap this closes was total and silent. Measured before the patch, on a case persisted in
``EXECUTING``::

    nodes: ('dispatch',)   status: Status.COMPLETED
    state: EXECUTING       version: unchanged        failure_reason: None

No edge left the state, so the run reported success having committed nothing, and the case sat
there for good — visible in the dashboard's "Working" column, unreachable by every API verb:
``reopen_case`` takes only ``DONE``, ``approve`` and ``reject`` need an approval the execute node
never raises, and a re-check needs ``AWAITING_HUMAN``. The outside world, meanwhile, may
already hold the appointment.

``ReconcileNode`` is what the state routes to now, and this file is what it does.

Three properties carry the weight.

:class:`TestABookingIsRecoveredUnderItsOwnFrozenIdentity` is the headline: a prepared booking whose
result was never recorded is re-issued under the *stored* key, against a real Demo World over a
loopback socket, and converges to one appointment, one durable result and ``DONE`` — with the
independent read-back doing the certifying, exactly as it does on a first pass.

:class:`TestANonReplayableActionStopsForAPerson` is the honest limit. Three of the four effect tools
have no measured provider idempotency, so an ambiguous outcome on one of them parks the case. Not
because parking is nice, but because an email that may or may not have been sent is not something an
agent may decide to send again.

:class:`TestReplayIsLicensedByTheWriteContractAndNotByAReading` is the one that stops the obvious
wrong fix. Nothing anywhere turns a negative observation into a retry, and the licence is decided
before any read is made.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    EMAIL_ARGS,
    SAGA_EVENT,
    SLOT,
    ExplodingPlanner,
    ExplodingTriage,
    booking_key,
    controlled_fact,
    count,
    executing_with_a_prepared_action,
    ready_to_execute,
)

from careloop.graph.context import unsettled_preparation
from careloop.graph.recovery import (
    RECOVERY_CLASS,
    RecoveryClass,
    ReplayRefused,
    ReplayUnavailable,
    replay,
)
from careloop.graph.runner import context_for, run_case
from careloop.graph.verify import ObservationOutcome
from careloop.policy import ReasonCode
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.clinic import ClinicClient, ClinicRefused, ClinicUnavailable
from demoworld.clinic import Clinic

#: An upload the shipped rules would allow: a portal from trusted configuration and a document id
#: the case has validated. Used to separate "the recovery class refused this" from "policy did".
UPLOAD_ARGS: dict[str, str] = {
    "portal_id": "PORTAL-NORTHGATE-RECORDS",
    "document_id": "doc-0001-referral",
}


def recover(store, *, clinic: ClinicClient | None = None, executor=None):
    """Run one graph pass over a persisted case, with the model seams booby-trapped.

    Returns ``(RunResult, context)``. Both agents raise if touched, so "recovery calls no model" is
    enforced by the harness rather than asserted after the fact — a regression that reached for the
    planner would fail here rather than produce a subtly wrong number somewhere else.
    """
    extra = {}
    if clinic is not None:
        extra["clinic"] = clinic
    if executor is not None:
        extra["executor"] = executor
    context = context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        now=lambda: FIXED_NOW,
        **extra,
    )
    return asyncio.run(run_case(context, max_node_executions=12)), context


def handoff_payload(case) -> dict:
    """The payload of the case's most recent manual handoff."""
    for event in reversed(case.timeline):
        if event.kind is TimelineKind.MANUAL_HANDOFF_REQUIRED:
            return dict(event.payload)
    raise AssertionError("the case carries no manual handoff")


class VanishingClinic(ClinicClient):
    """Books for real, then wipes the world before anything can read it back.

    The only way to produce an authoritative ``ABSENT`` from inside one run: the write genuinely
    happens and the clinic genuinely answers ``404`` to the ``GET`` that follows, because the world
    that held the appointment is gone. Nothing is stubbed — the ``POST`` is a real request to a real
    service — so what the test observes is the verifier's own behaviour rather than a mock's.

    ``writes`` counts the requests that left the process, which is what makes "no second write"
    an assertion rather than an inference from an appointment count that was reset anyway.
    """

    def __init__(self, base_url: str, *, world: Clinic) -> None:
        super().__init__(base_url)
        self._world = world
        self.writes = 0

    def book(self, args, idempotency_key):
        self.writes += 1
        booked = super().book(args, idempotency_key)
        self._world.reset()
        return booked


class TestABookingIsRecoveredUnderItsOwnFrozenIdentity:
    """The headline. A real clinic, a real socket, and one appointment at the end of it."""

    def test_a_prepared_booking_nobody_recorded_converges_to_done(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        result, _ = recover(store, clinic=clinic)

        case = store.get(CASE_ID)
        assert result.executed_nodes == ("dispatch", "reconcile", "verify")
        assert case.state is CaseState.DONE
        assert len(clinic_state.appointments()) == 1
        assert len(case.actions) == 1

    def test_the_recovered_action_carries_the_key_that_was_frozen(
        self, clinic: ClinicClient
    ) -> None:
        """The identity is reused, not re-minted. This is the whole property in one assertion."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        recover(store, clinic=clinic)

        case = store.get(CASE_ID)
        assert case.actions[0].idempotency_key == booking_key()
        assert unsettled_preparation(case) is None

    def test_the_clinic_minted_the_confirmation_from_that_same_key(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """The external system's own record agrees about which action this was."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        recover(store, clinic=clinic)

        held = clinic_state.appointments()[0]
        assert held.confirmation_id == f"apt-{booking_key()[:16]}"
        assert held.patient_id == BOOKING_ARGS["patient_id"]
        assert held.slot_start == SLOT
        assert held.reason == BOOKING_ARGS["reason"]

    def test_the_independent_read_back_is_what_certified_it(self, clinic: ClinicClient) -> None:
        """Recovery does not certify itself. The same separate ``GET`` does, on the same terms."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        recover(store, clinic=clinic)

        passed = [
            event
            for event in store.get(CASE_ID).timeline
            if event.kind is TimelineKind.VERIFICATION_PASSED
        ]
        assert len(passed) == 1
        assert passed[0].payload["read_back"] == "true"
        assert passed[0].payload["observation"] == str(ObservationOutcome.MATCHED)

    def test_recovery_consulted_no_model(self, clinic: ClinicClient) -> None:
        """Belt and braces over the exploding stubs: the run never entered plan or triage."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        result, _ = recover(store, clinic=clinic)

        assert "plan" not in result.executed_nodes
        assert "triage" not in result.executed_nodes
        assert "gate" not in result.executed_nodes
        assert count(store.get(CASE_ID), TimelineKind.PLAN_PROPOSED) == 1

    def test_recovery_proposes_nothing_and_approves_nothing(self, clinic: ClinicClient) -> None:
        """A recovery recovers knowledge. Nothing about it moves authority."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        recover(store, clinic=clinic)

        case = store.get(CASE_ID)
        assert case.approvals == ()
        assert count(case, TimelineKind.APPROVAL_REQUESTED) == 0
        assert count(case, TimelineKind.ACTION_PREPARED) == 1

    def test_a_second_recovery_run_suppresses_rather_than_repeating(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """Idempotent end to end. The recovered case is an ordinary finished case afterwards."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        recover(store, clinic=clinic)

        result, _ = recover(store, clinic=clinic)

        assert result.executed_nodes == ("dispatch",)
        assert store.get(CASE_ID).state is CaseState.DONE
        assert len(clinic_state.appointments()) == 1


class TestTheOriginalWriteIsNotRepeated:
    """The crash-D case: the clinic committed, the response was lost, nobody recorded it."""

    def test_a_booking_the_clinic_already_holds_is_not_booked_twice(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """Reproduces the world CareLoop wakes up to after losing a response.

        The appointment is placed through the real client under the frozen key — which is exactly
        what the dead process did — and then the case is left as that process left it: prepared, no
        record. Recovery must converge onto the existing appointment rather than make a second.
        """
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        clinic.book(BOOKING_ARGS, booking_key())
        assert len(clinic_state.appointments()) == 1
        before = clinic_state.appointments()[0]

        recover(store, clinic=clinic)

        assert len(clinic_state.appointments()) == 1
        assert clinic_state.appointments()[0] == before
        assert len(clinic_state.available()) == 2

    def test_the_recovered_result_is_the_clinics_original_record(
        self, clinic: ClinicClient
    ) -> None:
        """What CareLoop records is what the provider already held, not a fresh booking."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        original = clinic.book(BOOKING_ARGS, booking_key())

        recover(store, clinic=clinic)

        case = store.get(CASE_ID)
        assert original.confirmation_id in case.actions[0].result
        assert case.state is CaseState.DONE


class TestANonReplayableActionStopsForAPerson:
    """The honest limit. Only booking has a measured provider contract."""

    def _prepared_email(self, store) -> None:
        executing_with_a_prepared_action(store, tool_name="send_email", args=EMAIL_ARGS)

    def test_an_ambiguous_email_parks_rather_than_being_sent_again(self) -> None:
        store = InMemoryCaseStore()
        self._prepared_email(store)

        result, context = recover(store)

        case = store.get(CASE_ID)
        assert result.executed_nodes == ("dispatch", "reconcile")
        assert case.state is CaseState.AWAITING_HUMAN
        assert context.executor.invocation_count == 0

    def test_the_handoff_names_the_outcome_it_could_not_establish(self) -> None:
        store = InMemoryCaseStore()
        self._prepared_email(store)

        recover(store)

        payload = handoff_payload(store.get(CASE_ID))
        assert payload["reason_code"] == str(ReasonCode.EXTERNAL_OUTCOME_NOT_ESTABLISHED)
        assert payload["tool_name"] == "send_email"
        assert (
            payload["idempotency_key"]
            == unsettled_preparation(
                executing_with_a_prepared_action(
                    InMemoryCaseStore(), tool_name="send_email", args=EMAIL_ARGS
                )
            ).idempotency_key
        )

    def test_it_is_not_the_confirmed_state_code(self) -> None:
        """A caregiver handed the wrong code goes on the wrong errand.

        ``EXTERNAL_STATE_NOT_CONFIRMED`` says an action was carried out and its result cannot be
        confirmed — *check whether the appointment is right*. Here whether it was carried out is the
        open question — *check whether an appointment exists*.
        """
        store = InMemoryCaseStore()
        self._prepared_email(store)

        recover(store)

        payload = handoff_payload(store.get(CASE_ID))
        assert payload["reason_code"] != str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)

    def test_the_park_raises_no_approval(self) -> None:
        """There is nothing to authorise: the action was authorised, and then it was lost."""
        store = InMemoryCaseStore()
        self._prepared_email(store)

        recover(store)

        case = store.get(CASE_ID)
        assert case.approvals == ()
        assert case.pending_approval() is None

    def test_a_parked_case_never_reaches_waiting_external(self) -> None:
        """Waiting would claim CareLoop will resolve this itself, and it never will."""
        store = InMemoryCaseStore()
        self._prepared_email(store)

        recover(store)

        states = [
            event.payload["to"]
            for event in store.get(CASE_ID).timeline
            if event.kind is TimelineKind.STATE_TRANSITION
        ]
        assert str(CaseState.WAITING_EXTERNAL) not in states

    @pytest.mark.parametrize(
        "tool_name",
        sorted(
            name for name, klass in RECOVERY_CLASS.items() if klass is RecoveryClass.NOT_REPLAYABLE
        ),
    )
    def test_every_non_replayable_tool_behaves_the_same(self, tool_name: str) -> None:
        """Not a spelling of ``send_email``: the property is per declared class."""
        from careloop.tools.registry import REGISTRY

        spec = REGISTRY.get(tool_name)
        assert spec is not None
        args = {name: f"value-for-{name}" for name in sorted(spec.arg_names)}
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store, tool_name=tool_name, args=args)

        _, context = recover(store)

        assert store.get(CASE_ID).state is CaseState.AWAITING_HUMAN
        assert context.executor.invocation_count == 0

    def test_an_upload_policy_would_allow_is_still_not_replayed(self) -> None:
        """The discriminating case, and the reason the tests above are not enough on their own.

        ``send_email`` to an untrusted recipient is refused by the policy engine, so a park proves
        nothing about the recovery class — a mutant that made every action replayable would
        still see that case park, for another reason. This one is built so a replay *would* pass:
        a portal from trusted configuration, a validated document id, and a case in ``EXECUTING``
        — which is exactly what ``allow-upload-to-trusted-portal`` asks for.

        So the only thing standing between this prepared action and a second external effect is the
        declared recovery class. If it is ever weakened, the executor's invocation count moves.
        """
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(
            store,
            tool_name="upload_document",
            args=UPLOAD_ARGS,
            facts=(controlled_fact("document_id", UPLOAD_ARGS["document_id"]),),
        )

        _, context = recover(store)

        case = store.get(CASE_ID)
        assert context.executor.invocation_count == 0
        assert case.state is CaseState.AWAITING_HUMAN
        assert handoff_payload(case)["reason_code"] == str(
            ReasonCode.EXTERNAL_OUTCOME_NOT_ESTABLISHED
        )

    def test_and_the_same_upload_is_dispatched_when_it_is_a_first_attempt(self) -> None:
        """Vacuity for the test above. Nothing else about this case refuses the call.

        Without this, the park could be a policy refusal wearing a recovery class's clothes, which
        is precisely the confusion the previous test exists to remove.
        """
        store = InMemoryCaseStore()
        ready_to_execute(
            store,
            tool_name="upload_document",
            args=UPLOAD_ARGS,
            facts=(controlled_fact("document_id", UPLOAD_ARGS["document_id"]),),
        )

        _, context = recover(store)

        assert context.executor.invocation_count == 1
        assert store.get(CASE_ID).state is CaseState.DONE

    def test_the_park_says_which_kind_of_not_knowing_occurred(self) -> None:
        """The diagnostic distinguishes causes the reason code deliberately does not."""
        store = InMemoryCaseStore()
        self._prepared_email(store)

        recover(store)

        handoff = next(
            event
            for event in reversed(store.get(CASE_ID).timeline)
            if event.kind is TimelineKind.MANUAL_HANDOFF_REQUIRED
        )
        assert "no safe way to establish the outcome" in (handoff.diagnostic or "")


class TestAReplayWithNoObserverIsNotAllowedEither:
    """A replay nothing can read back would certify on CareLoop's own receipt."""

    def test_a_booking_recovery_with_no_clinic_bound_parks(self) -> None:
        """The action type is replayable; this run has nothing to observe it with.

        Recording a replay's result here and verifying it closed-loop would let the case reach
        ``DONE`` on strictly less evidence than a first pass with a clinic gets — the same hazard
        ``VerifyNode._nothing_was_observed`` closes for a caregiver's re-check.
        """
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        result, context = recover(store)

        assert store.get(CASE_ID).state is CaseState.AWAITING_HUMAN
        assert context.executor.invocation_count == 0
        assert result.executed_nodes == ("dispatch", "reconcile")

    def test_and_the_same_case_with_a_clinic_recovers(self, clinic: ClinicClient) -> None:
        """Vacuity for the test above: the case is otherwise perfectly recoverable."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        recover(store, clinic=clinic)

        assert store.get(CASE_ID).state is CaseState.DONE


class TestReplayIsLicensedByTheWriteContractAndNotByAReading:
    """The obvious wrong fix, refused structurally."""

    def test_nothing_in_the_recovery_path_reads_an_observation_outcome(self) -> None:
        from pathlib import Path

        import careloop

        recovery = (Path(careloop.__file__).parent / "graph" / "recovery.py").read_text(
            encoding="utf-8"
        )

        assert "ObservationOutcome" not in recovery.split('"""', 2)[2]

    def test_the_licence_is_decided_before_anything_is_read(self) -> None:
        """``may_replay`` takes an action type and the run's observers. Nothing else exists to pass.

        There is no parameter for a prior observation, a diagnostic, a status code or an outcome, so
        "``ABSENT`` means retry" is not a rule somebody forgot to write — it is a rule there is
        nowhere to express.
        """
        import inspect

        from careloop.graph.recovery import may_replay

        assert list(inspect.signature(may_replay).parameters) == ["action_type", "observers"]

    def test_an_absent_read_back_parks_and_issues_no_second_write(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The end-to-end statement of it, through the real verifier.

        A ``VanishingClinic`` books for real and then wipes the world behind CareLoop's back, so the
        replay succeeds, the result is recorded, and the independent read that follows comes back
        ``ABSENT`` — the clinic answering, definitely, that it holds no such confirmation. That is
        the strongest negative evidence this system can obtain, and it licenses nothing: the case
        parks, exactly one write ever left the process, and no appointment is created to replace the
        one that vanished.
        """
        base_url, _ = clinic_service
        vanishing = VanishingClinic(base_url, world=clinic_state)
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        try:
            _, context = recover(store, clinic=vanishing)
        finally:
            vanishing.close()

        case = store.get(CASE_ID)
        assert vanishing.writes == 1
        assert context.executor.invocation_count == 1
        assert case.state is CaseState.AWAITING_HUMAN
        assert handoff_payload(case)["reason_code"] == str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED)
        assert len(clinic_state.appointments()) == 0
        assert len(case.actions) == 1

    def test_a_re_check_of_that_case_still_issues_no_write(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """And asking again changes nothing. ``ABSENT`` twice is still not a licence.

        A caregiver's re-check is the one supported way to look again at a parked effect. It reads,
        it finds nothing, and it parks — it does not reach reconciliation, because the case holds an
        ``ActionRecord`` and there is no unsettled preparation for anything to re-issue.
        """
        base_url, _ = clinic_service
        vanishing = VanishingClinic(base_url, world=clinic_state)
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        try:
            recover(store, clinic=vanishing)
        finally:
            vanishing.close()

        from careloop.graph.runner import request_recheck

        request_recheck(store, CASE_ID, requested_by="rosa", now=FIXED_NOW)
        plain = ClinicClient(base_url)
        try:
            result, context = recover(store, clinic=plain)
        finally:
            plain.close()

        case = store.get(CASE_ID)
        assert result.executed_nodes == ("dispatch", "verify")
        assert context.executor.invocation_count == 0
        assert case.state is CaseState.AWAITING_HUMAN
        assert len(clinic_state.appointments()) == 0
        assert len(case.actions) == 1


class TestTheReplaySeamTranslatesTransportAndNothingElse:
    """Where HTTP stops on the recovery path."""

    def test_an_unreachable_provider_becomes_replay_unavailable(self) -> None:
        def dispatch():
            raise ClinicUnavailable("nobody answered")

        with pytest.raises(ReplayUnavailable):
            replay(dispatch)

    def test_a_refusal_becomes_replay_refused(self) -> None:
        def dispatch():
            raise ClinicRefused(operation="book_appointment", status_code=409)

        with pytest.raises(ReplayRefused):
            replay(dispatch)

    def test_a_policy_denial_passes_through_untouched(self) -> None:
        """The authority layer refusing is the caller's to handle, not transport to translate."""
        from careloop.policy.gate import PolicyDenied

        def dispatch():
            raise PolicyDenied("refused")

        with pytest.raises(PolicyDenied):
            replay(dispatch)

    def test_an_internal_fault_passes_through_untouched(self) -> None:
        """CareLoop being wrong about its identity fails loudly rather than reading as unknown."""
        from careloop.tools.executor import IdempotencyKeyCollision

        def dispatch():
            raise IdempotencyKeyCollision("two calls, one key")

        with pytest.raises(IdempotencyKeyCollision):
            replay(dispatch)

    def test_a_successful_dispatch_is_returned_verbatim(self) -> None:
        sentinel = object()

        assert replay(lambda: sentinel) is sentinel


class TestAnInconsistentCaseFailsClosed:
    """Recovery refuses rather than guessing which action a case is about."""

    def test_a_prepared_key_that_the_proposal_does_not_rederive_fails(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """Persist and recompute, then require agreement. A disagreement is the finding."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store, key="not-the-key-these-arguments-derive")

        recover(store, clinic=clinic)

        assert store.get(CASE_ID).state is CaseState.FAILED
        assert len(clinic_state.appointments()) == 0

    def test_an_executing_case_with_no_preparation_fails(self, clinic: ClinicClient) -> None:
        """Unreachable while the preparation and the transition share one commit. Guarded anyway."""
        from careloop.state.machine import transition

        store = InMemoryCaseStore()
        case = ready_to_execute(store)
        base = case.version
        case = transition(case, CaseState.EXECUTING, reason="setup", actor="test", now=FIXED_NOW)
        store.save(case, expected_version=base)

        recover(store, clinic=clinic)

        assert store.get(CASE_ID).state is CaseState.FAILED
