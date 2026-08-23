"""A verifier may expect more than an argument supplies; an effect claim may not grow with it.

Verification and settlement telemetry ask two different questions, and until this session one
mechanism answered both. Every field a verifier compared came from the arguments policy authorised,
so ``COMPARED_ARGUMENTS`` and :attr:`~careloop.graph.verify.ObservableEffect.state_fields` could be
the same set without anybody deciding that they were — and the first provider whose verification
needs a value no argument carries would have made that accident load-bearing in the wrong direction.

There are two ways to reach such a value and only one of them is safe:

* declare it as an argument, so it enters the proposal, the authority path, ``args_digest``,
  possibly the idempotency key and the outbound payload — in order to answer a question about
  *scrutiny*. That is the fold this repository has closed several times going the other way.
* declare it as a :class:`~careloop.graph.verify.DerivedExpectation`: computed at verification time
  from the arguments already authorised, compared against what an external system holds, and
  **absent from the authorised effect state** that ``careloop.effect.fields`` and
  ``careloop.effect.state_digest`` are taken over.

This file proves the second exists, works, and cannot leak into the first. Nothing CareLoop ships
declares one yet, so every derived expectation here is synthetic on purpose: the seam is proven
rather than described, and it is proven before a provider forces it.

The frozen nine telemetry attributes, ``CareLoopSettlementIntegrity`` and every evaluator
declaration are untouched by all of this, which ``TestTheEffectClaimIsUnmoved`` measures rather
than asserts.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest
from helpers import BOOKING_REASON

from careloop.graph.verify import (
    COMPARED_ARGUMENTS,
    DERIVED_EXPECTATIONS,
    INDEPENDENTLY_OBSERVABLE,
    OBSERVABLE_STATE,
    DerivedExpectation,
    ObservableEffect,
    ObservationOutcome,
    UnverifiableAction,
    check_derived_expectations_do_not_shadow_arguments,
    check_observable_state_matches_verification,
    compared_arguments,
    derived_names,
    expectations,
    verify_action,
)
from careloop.graph.witness import EffectDispatch, _effect_claim
from careloop.state.case import ActionRecord
from careloop.tools.idempotency import args_digest, canonical_args, idempotency_key
from careloop.tools.results import AppointmentBooked

#: The verification **module**. ``careloop.graph`` re-exports no name that shadows it, but the
#: tests below monkeypatch module constants, so they take the module rather than the symbols.
verification = sys.modules["careloop.graph.verify"]

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

CASE_ID = "case-0001"
BOOKING = "book_appointment"
PROVIDER = "PROV-NORTHGATE-CARDIOLOGY"
SLOT = "2026-09-03T09:30:00"

ARGS = {
    "provider_id": PROVIDER,
    "patient_id": "PT-4417",
    "slot_start": SLOT,
    "reason": BOOKING_REASON,
}

#: A derived expectation that restates the slot. Faithful, so a verification using it passes.
#:
#: Deliberately not a *new* fact about the world: this file is about the mechanism, and a derivation
#: that computed something interesting would make its own correctness the subject.
FAITHFUL = DerivedExpectation(name="expected_slot", derive=lambda args: args.get("slot_start"))

#: The same expectation, wrong. What a derivation disagreeing with the world looks like.
UNFAITHFUL = DerivedExpectation(name="expected_slot", derive=lambda _args: "2026-01-01T00:00:00")

#: A derivation that cannot establish anything — the held document is missing, the source is
#: unreadable. It contributes no entry rather than a guess.
UNESTABLISHED = DerivedExpectation(name="expected_slot", derive=lambda _args: None)


def booked(slot: str = SLOT) -> AppointmentBooked:
    return AppointmentBooked(
        confirmation_id="apt-0001",
        provider_id=PROVIDER,
        patient_id="PT-4417",
        slot_start=slot,
        reason=BOOKING_REASON,
    )


def record() -> ActionRecord:
    return ActionRecord(
        action_type=BOOKING,
        idempotency_key=idempotency_key(CASE_ID, BOOKING, ARGS),
        args_digest=args_digest(ARGS),
        result=booked().model_dump_json(),
    )


def verify(**observed: object):
    return verify_action(
        case_id=CASE_ID,
        action_type=BOOKING,
        args=ARGS,
        record=record(),
        result=booked(),
        **observed,
    )


@pytest.fixture
def slot_is_derived(monkeypatch: pytest.MonkeyPatch):
    """Point booking's ``slot_start`` comparison at a derived expectation instead of the argument.

    The whole path runs: :func:`verify_action` builds the expectations, ``_verify_booking`` reads
    the comparison map, and ``_mismatches`` is told which names are derived. Nothing is stubbed
    except the two declarations a real provider integration would be adding anyway.
    """

    def apply(expectation: DerivedExpectation) -> None:
        monkeypatch.setattr(verification, "DERIVED_EXPECTATIONS", {BOOKING: (expectation,)})
        monkeypatch.setattr(
            verification,
            "_BOOKING_COMPARISON",
            {
                "provider_id": "provider_id",
                "patient_id": "patient_id",
                "slot_start": expectation.name,
                "reason": "reason",
            },
        )

    return apply


class TestTheExpectationsAreTheArgumentsUntilSomethingSaysOtherwise:
    """A. Booking is unchanged, and the way it is unchanged is worth pinning."""

    def test_nothing_shipped_declares_a_derived_expectation(self) -> None:
        """An empty table is the honest state. An entry would be a guarantee nobody is making."""
        assert DERIVED_EXPECTATIONS == {}

    @pytest.mark.parametrize("action_type", sorted({BOOKING, "send_email", "upload_document"}))
    def test_the_expectations_are_exactly_the_canonical_arguments(self, action_type: str) -> None:
        canonical = canonical_args(ARGS)

        assert expectations(action_type, canonical) == canonical
        assert derived_names(action_type) == frozenset()

    def test_the_compared_arguments_are_what_they_always_were(self) -> None:
        """The filter is a no-op while nothing is derived, so no consumer of this moved."""
        assert COMPARED_ARGUMENTS[BOOKING] == frozenset(
            {"provider_id", "patient_id", "slot_start", "reason"}
        )

    def test_a_booking_still_verifies_against_an_agreeing_external_record(self) -> None:
        result = verify(observed=booked())

        assert result.passed
        assert result.outcome is ObservationOutcome.MATCHED


class TestAVerifierCanExpectWhatNoArgumentSupplies:
    """B. The seam, driven end to end through :func:`verify_action`."""

    def test_a_faithful_derivation_verifies(self, slot_is_derived) -> None:
        """The compared name is in no proposal, in no ``args_digest``, and in no rule."""
        slot_is_derived(FAITHFUL)

        assert FAITHFUL.name not in ARGS
        result = verify(observed=booked())

        assert result.passed
        assert result.outcome is ObservationOutcome.MATCHED

    def test_a_derivation_that_disagrees_with_the_world_fails_verification(
        self, slot_is_derived
    ) -> None:
        slot_is_derived(UNFAITHFUL)

        result = verify(observed=booked())

        assert not result.passed
        assert result.outcome is ObservationOutcome.DIVERGED

    def test_a_derivation_that_establishes_nothing_fails_closed(self, slot_is_derived) -> None:
        """``None`` contributes no expectation, so everything compared against it mismatches.

        The alternative — treating an unestablished expectation as satisfied — would make a
        verification silently weaker exactly when the thing it needed was unavailable.
        """
        slot_is_derived(UNESTABLISHED)

        result = verify(observed=booked())

        assert not result.passed

    def test_a_derived_mismatch_does_not_claim_the_value_was_authorised(
        self, slot_is_derived
    ) -> None:
        """The wording is a contract, not prose.

        ``tests/test_evaluator_drift.py`` and ``tests/test_witness_seam.py`` derive the settlement
        field universe by running a verifier and reading ``" was authorised as "`` back out of the
        diagnostics. A derived expectation reusing that phrase would enlarge a frozen telemetry
        contract from inside a string, so it says ``derived as`` — and it is also simply true, since
        no proposal carried this value and no rule authorised it.
        """
        slot_is_derived(UNFAITHFUL)

        diagnostic = verify(observed=booked()).diagnostic

        assert "slot_start was derived as" in diagnostic
        assert "slot_start was authorised as" not in diagnostic

    def test_the_arguments_still_report_as_authorised(self, slot_is_derived) -> None:
        """Bounding the wording change: only the derived name reports differently.

        On the same verification, with a derived expectation in play, an argument-sourced field
        that disagrees still says ``authorised as`` — so the two phrases distinguish the two
        populations rather than one having replaced the other.
        """
        slot_is_derived(UNFAITHFUL)

        diagnostic = verify_action(
            case_id=CASE_ID,
            action_type=BOOKING,
            args=ARGS,
            record=record(),
            result=booked(),
            observed=booked().model_copy(update={"provider_id": "PROV-ELSEWHERE"}),
        ).diagnostic

        assert "provider_id was authorised as" in diagnostic
        assert "slot_start was derived as" in diagnostic


class TestTheEffectClaimIsUnmoved:
    """C. A richer verifier does not require a richer effect-state digest."""

    def test_the_declared_external_state_names_no_derived_expectation(self) -> None:
        for action_type, observable in OBSERVABLE_STATE.items():
            assert frozenset(observable.state_fields) & derived_names(action_type) == frozenset()

    def test_the_effect_claim_is_byte_identical_with_a_derived_expectation_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measurement, rather than an argument that it must be so.

        ``careloop.effect.fields`` and ``careloop.effect.state_digest`` are projected from
        :data:`OBSERVABLE_STATE`, which an import-time check keeps argument-sourced. Declaring a
        derived expectation therefore cannot move either — and this is what a mutant putting one
        into the effect state would break.
        """
        before = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=canonical_args(ARGS)
        )
        monkeypatch.setattr(verification, "DERIVED_EXPECTATIONS", {BOOKING: (FAITHFUL,)})
        after = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=canonical_args(ARGS)
        )

        assert before == after
        assert before is not None
        assert before.fields == "patient_id,provider_id,reason,slot_start"
        assert FAITHFUL.name not in before.fields

    def test_a_derived_name_is_filtered_out_of_the_compared_arguments(self) -> None:
        """So ``check_non_identifying_arguments_are_verified`` cannot be satisfied by one.

        An argument may leave the idempotency key only where something still compares it. A derived
        expectation is not that something — it is a value CareLoop computed — and letting one count
        would make narrowing identity a way of narrowing scrutiny after all.
        """
        comparison = {"provider_id": "provider_id", "slot_start": "expected_slot"}
        declared = {BOOKING: (FAITHFUL,)}

        assert compared_arguments(BOOKING, comparison, declared) == frozenset({"provider_id"})
        assert compared_arguments(BOOKING, comparison, {}) == frozenset(
            {"provider_id", "expected_slot"}
        )


class TestPuttingADerivedValueIntoTheEffectStateIsRefused:
    """E. The mutant, provoked. The refusal is at import, so it is an outage rather than a bug."""

    def test_a_state_field_that_is_a_derived_name_fails_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verification, "DERIVED_EXPECTATIONS", {BOOKING: (FAITHFUL,)})
        monkeypatch.setattr(
            verification,
            "OBSERVABLE_STATE",
            {
                BOOKING: ObservableEffect(
                    state_fields=("expected_slot", "patient_id", "provider_id", "reason"),
                    resource_id_field="confirmation_id",
                )
            },
        )
        monkeypatch.setattr(
            verification,
            "_BOOKING_COMPARISON",
            {
                "provider_id": "provider_id",
                "patient_id": "patient_id",
                "slot_start": "expected_slot",
                "reason": "reason",
            },
        )
        monkeypatch.setattr(
            verification,
            "_EXTERNAL_COMPARISONS",
            {BOOKING: (verification._BOOKING_COMPARISON, "confirmation_id")},
        )

        with pytest.raises(UnverifiableAction, match="derived expectation"):
            check_observable_state_matches_verification()

    def test_the_shipped_declarations_satisfy_the_check(self) -> None:
        """Non-vacuity for the test above: the refusal is about the mutant, not about the check."""
        check_observable_state_matches_verification()

    def test_a_derived_name_that_shadows_an_argument_is_refused(self) -> None:
        """A derived value displacing an authorised one makes the comparison self-referential."""
        shadowing = DerivedExpectation(name="patient_id", derive=lambda _args: "PT-0000")

        with pytest.raises(UnverifiableAction, match="share a declared argument name"):
            check_derived_expectations_do_not_shadow_arguments({BOOKING: (shadowing,)})

    def test_an_expectation_for_an_unregistered_action_is_refused(self) -> None:
        with pytest.raises(UnverifiableAction, match="registry does not"):
            check_derived_expectations_do_not_shadow_arguments({"no_such_tool": (FAITHFUL,)})

    def test_two_expectations_of_one_name_are_refused(self) -> None:
        with pytest.raises(UnverifiableAction, match="not distinct"):
            check_derived_expectations_do_not_shadow_arguments({BOOKING: (FAITHFUL, UNFAITHFUL)})

    def test_the_shipped_table_satisfies_it(self) -> None:
        check_derived_expectations_do_not_shadow_arguments()


class TestWithoutTheSeamTheDerivedComparisonStopsWorking:
    """D. The non-vacuity mutant: revert derived expectation support.

    Restores the pre-correction behaviour — the verifier is handed the canonical arguments and
    nothing else — and requires the seam's own test to notice.
    """

    @pytest.fixture
    def without_derived_support(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            verification, "expectations", lambda _action_type, canonical: dict(canonical)
        )

    def test_a_faithful_derivation_no_longer_verifies(
        self, without_derived_support: None, slot_is_derived
    ) -> None:
        slot_is_derived(FAITHFUL)

        result = verify(observed=booked())

        assert not result.passed

    def test_the_mutant_leaves_an_ordinary_booking_alone(
        self, without_derived_support: None
    ) -> None:
        """Bounding it. Reverting the seam breaks derived comparisons and nothing else, which is
        what makes the test above evidence about the seam rather than about breakage."""
        assert verify(observed=booked()).passed


class TestTheDeclarationsStayWhereTheyAre:
    """The observable types did not move, so no evaluator or telemetry contract is in question."""

    def test_only_booking_is_independently_observable(self) -> None:
        assert frozenset({BOOKING}) == INDEPENDENTLY_OBSERVABLE

    def test_the_booking_field_set_is_unchanged(self) -> None:
        assert OBSERVABLE_STATE[BOOKING].state_fields == (
            "patient_id",
            "provider_id",
            "reason",
            "slot_start",
        )
