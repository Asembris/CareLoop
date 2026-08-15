"""Recording and promoting evidence on a case.

The test that carries this module is :class:`TestPromotionIsVisibleToTheEngine`. Everything else
checks bookkeeping; that one checks the property, and it is the only one that can tell the fix
from the bug — see :class:`TestTheAssertionThatWouldNotHaveCaught` for the proof of that claim.
"""

from __future__ import annotations

import pytest
from helpers import FIXED_NOW, make_case, make_fact, trusted_fact

from careloop.policy.engine import resolve_trust
from careloop.provenance import (
    ArgClass,
    Fact,
    PromotionForbidden,
    Provenance,
    TrustLevel,
    promote,
)
from careloop.state import TimelineKind
from careloop.state.evidence import (
    FactNotOnCase,
    PromotionIneffective,
    already_recorded,
    promote_fact,
    record_fact,
)

SLOT = "2026-09-03T09:30:00"


def case_with_raw_slot() -> tuple[object, object]:
    """A case carrying one UNTRUSTED slot fact, and the fact itself."""
    raw = make_fact("slot_start", SLOT)
    case = record_fact(make_case(), raw, now=FIXED_NOW)
    return case, raw


class TestPromotionIsVisibleToTheEngine:
    """The assertion the whole module exists for."""

    def test_a_promoted_value_resolves_controlled_through_the_engine(self) -> None:
        case, raw = case_with_raw_slot()

        promoted = promote_fact(
            case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        assert resolve_trust("slot_start", SLOT, promoted) is TrustLevel.CONTROLLED

    def test_the_untrusted_original_is_gone_rather_than_shadowed(self) -> None:
        case, raw = case_with_raw_slot()

        promoted = promote_fact(
            case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        matching = [fact for fact in promoted.facts if fact.value == SLOT]
        assert len(matching) == 1
        assert matching[0].trust_level is TrustLevel.CONTROLLED

    def test_promotion_replaces_in_place_and_does_not_grow_the_facts(self) -> None:
        case, raw = case_with_raw_slot()
        other = make_fact("patient_id", "PT-4417")
        case = record_fact(case, other, now=FIXED_NOW)

        promoted = promote_fact(
            case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        assert len(promoted.facts) == len(case.facts)
        assert [fact.key for fact in promoted.facts] == ["slot_start", "patient_id"]

    def test_the_promoted_fact_records_its_validator(self) -> None:
        case, raw = case_with_raw_slot()

        promoted = promote_fact(
            case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        assert promoted.fact("slot_start").validated_by == "iso_datetime"


class TestTheAssertionThatWouldNotHaveCaught:
    """Why the engine-level assertion is the one that matters.

    These two tests are written against a case built the **wrong** way — the promoted fact
    appended beside its untrusted original, which is what
    :func:`careloop.provenance.facts.promote` produces if a caller appends the result. The
    plausible assertion passes; the engine-level one fails. That difference is the bug that
    open decision 9 recorded, pinned here so it cannot come back through another route.
    """

    @staticmethod
    def appended_rather_than_replaced():
        """The bug: promoted fact appended, untrusted original left in place."""
        case, raw = case_with_raw_slot()
        promoted = promote(
            raw, to=TrustLevel.CONTROLLED, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW
        )
        return case.model_copy(update={"facts": (*case.facts, promoted)})

    def test_a_controlled_fact_is_present_even_when_the_promotion_did_nothing(self) -> None:
        case = self.appended_rather_than_replaced()

        # This is the tempting assertion, and it is satisfied by the broken case.
        assert any(fact.trust_level is TrustLevel.CONTROLLED for fact in case.facts)

    def test_but_the_engine_still_reads_the_value_as_untrusted(self) -> None:
        case = self.appended_rather_than_replaced()

        # Lowest-wins: the untrusted original is still matching, so the promotion is invisible.
        assert resolve_trust("slot_start", SLOT, case) is TrustLevel.UNTRUSTED


class TestPromotionRefusesWhatItCannotMakeTrue:
    def test_a_fact_the_case_does_not_hold_is_refused(self) -> None:
        stray = make_fact("slot_start", SLOT)

        with pytest.raises(FactNotOnCase):
            promote_fact(make_case(), stray, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

    def test_an_invisible_promotion_raises_rather_than_succeeding_quietly(self) -> None:
        """A second ``UNTRUSTED`` fact under the *same* key and value really does hide it.

        Built by rebuilding ``case.facts`` rather than through :func:`record_fact`, which refuses a
        duplicate ``(key, value)`` outright. This guard exists for a fact that arrived by some
        other route, and constructing one directly is the only way to produce that.

        This test used to plant the twin under ``appointment_time`` — a *different* key — because
        under value-only resolution any key would do. It no longer would, and a test that went on
        passing for that reason would have been asserting the shadowing check was broader than the
        resolver rather than equal to it. The inverted case is the next test.
        """
        raw = make_fact("slot_start", SLOT)
        case = record_fact(make_case(), raw, now=FIXED_NOW)
        shadowed = case.model_copy(
            update={
                "facts": (
                    *case.facts,
                    make_fact("slot_start", SLOT, source_doc="continuity-notice.pdf"),
                )
            }
        )

        with pytest.raises(PromotionIneffective) as raised:
            promote_fact(shadowed, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert "slot_start" in str(raised.value)

    def test_a_twin_under_another_key_does_not_block_the_promotion(self) -> None:
        """The inverted expectation, and the reason it inverted.

        ``appointment_time`` carrying the same literal is not a duplicate of ``slot_start``; it is
        a different statement that happens to be spelled the same. Under value-only resolution it
        genuinely did shadow the promotion, so refusing here was correct then and would be a false
        failure now. The twin is left exactly where it was — untouched and still ``UNTRUSTED``,
        which is the second assertion.
        """
        raw = make_fact("slot_start", SLOT)
        twin = make_fact("appointment_time", SLOT, source_doc="continuity-notice.pdf")
        case = record_fact(record_fact(make_case(), raw, now=FIXED_NOW), twin, now=FIXED_NOW)

        promoted = promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert resolve_trust("slot_start", SLOT, promoted) is TrustLevel.CONTROLLED
        assert resolve_trust("appointment_time", SLOT, promoted) is TrustLevel.UNTRUSTED

    def test_a_duplicate_already_at_the_target_level_does_not_block(self) -> None:
        raw = make_fact("slot_start", SLOT)
        trusted_twin = make_fact("configured_slot", SLOT, TrustLevel.TRUSTED, "trusted-config")
        case = record_fact(
            record_fact(make_case(), raw, now=FIXED_NOW), trusted_twin, now=FIXED_NOW
        )

        promoted = promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert resolve_trust("slot_start", SLOT, promoted) is TrustLevel.CONTROLLED

    @pytest.mark.parametrize("arg_class", [ArgClass.DESTINATION, ArgClass.AUTHORITY])
    def test_unpromotable_argument_classes_are_refused(self, arg_class: ArgClass) -> None:
        case, raw = case_with_raw_slot()

        with pytest.raises(PromotionForbidden):
            promote_fact(case, raw, arg_class=arg_class, validator="v", now=FIXED_NOW)

    def test_promotion_to_trusted_is_refused(self) -> None:
        case, raw = case_with_raw_slot()

        with pytest.raises(PromotionForbidden):
            promote_fact(
                case,
                raw,
                arg_class=ArgClass.VALUE,
                validator="v",
                to=TrustLevel.TRUSTED,
                now=FIXED_NOW,
            )

    def test_promotion_is_not_idempotent_through_this_api_either(self) -> None:
        case, raw = case_with_raw_slot()
        once = promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        with pytest.raises(PromotionForbidden):
            promote_fact(
                once,
                once.fact("slot_start"),
                arg_class=ArgClass.VALUE,
                validator="v",
                now=FIXED_NOW,
            )


class TestARepeatedValueIsNotRecordedTwice:
    """Open decision 9's third face.

    The first was append-versus-replace on promotion; the second was triage emitting a duplicate
    of a value the evidence node would later promote. This is the third: a *second document*
    legitimately restating a value the case already carries. All three are the same hazard —
    under lowest-wins, any path that can add a second fact at an existing ``(key, value)`` can
    silently downgrade trust — and all three are now closed at the writer rather than at each
    caller.

    The resume makes it concrete rather than hypothetical: the claim rejection arriving weeks
    later restates ``Patient ID: PT-4417``, which the referral supplied and validation promoted.
    """

    @staticmethod
    def _promoted_case() -> tuple[object, object]:
        case, raw = case_with_raw_slot()
        return (
            promote_fact(
                case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
            ),
            raw,
        )

    def test_the_duplicate_is_not_added_to_the_facts(self) -> None:
        case, _raw = self._promoted_case()

        again = record_fact(case, make_fact("slot_start", SLOT), now=FIXED_NOW)

        assert len(again.facts) == len(case.facts)

    def test_and_the_promoted_value_still_resolves_controlled(self) -> None:
        """The assertion that matters, at the engine. Without the guard this reads UNTRUSTED."""
        case, _raw = self._promoted_case()

        again = record_fact(case, make_fact("slot_start", SLOT), now=FIXED_NOW)

        assert resolve_trust("slot_start", SLOT, again) is TrustLevel.CONTROLLED

    def test_recording_the_duplicate_without_the_guard_would_downgrade_it(self) -> None:
        """Vacuity: the same case built by appending the duplicate directly does read UNTRUSTED.

        This is what the guard prevents, demonstrated rather than asserted — the plausible test
        ("a CONTROLLED fact is on the case") passes in both worlds and can tell them apart in
        neither.
        """
        case, _raw = self._promoted_case()

        appended = case.model_copy(update={"facts": (*case.facts, make_fact("slot_start", SLOT))})

        assert any(fact.trust_level is TrustLevel.CONTROLLED for fact in appended.facts)
        assert resolve_trust("slot_start", SLOT, appended) is TrustLevel.UNTRUSTED

    def test_the_skip_is_on_the_timeline_rather_than_silent(self) -> None:
        """An invisible trust-safety mechanism is the same hazard somewhere else."""
        case, _raw = self._promoted_case()

        again = record_fact(case, make_fact("slot_start", SLOT), now=FIXED_NOW)

        entry = again.timeline[-1]
        assert entry.kind is TimelineKind.EVIDENCE_DUPLICATE_SKIPPED
        assert entry.payload["held_trust_level"] == "CONTROLLED"
        assert "lowest level" in (entry.diagnostic or "")

    def test_the_same_key_with_a_different_value_is_still_recorded(self) -> None:
        """Deduplication is on ``(key, value)``. Two documents disagreeing is not a duplicate —
        it is a conflict, and both facts have to be on the case for anything to notice it."""
        case, _raw = self._promoted_case()

        other = record_fact(case, make_fact("slot_start", "2026-10-01T09:00:00"), now=FIXED_NOW)

        assert len(other.facts) == len(case.facts) + 1

    def test_a_different_key_with_the_same_value_is_still_recorded(self) -> None:
        case, _raw = self._promoted_case()

        other = record_fact(case, make_fact("rescheduled_slot", SLOT), now=FIXED_NOW)

        assert len(other.facts) == len(case.facts) + 1

    def test_already_recorded_reports_the_fact_that_is_in_the_way(self) -> None:
        case, _raw = self._promoted_case()

        held = already_recorded(case, make_fact("slot_start", SLOT))

        assert held is not None
        assert held.trust_level is TrustLevel.CONTROLLED
        assert already_recorded(case, make_fact("slot_start", "something else")) is None


class TestTheCitation:
    """``source: referral.pdf p.2`` — invariant 3's provenance made visible in the timeline."""

    def test_a_paginated_fact_cites_its_page(self) -> None:
        fact = Fact(
            key="slot_start",
            value=SLOT,
            provenance=Provenance(source_doc="referral.pdf", page=2, extractor="test"),
            extracted_at=FIXED_NOW,
        )

        assert fact.citation == "referral.pdf p.2"

    def test_an_unpaginated_fact_cites_the_source_alone(self) -> None:
        """Trusted configuration has no page, and inventing one would be a lie in the audit."""
        assert trusted_fact("caregiver_email", "rosa@example.test").citation == "trusted-config"

    def test_the_timeline_summary_carries_the_citation(self) -> None:
        fact = Fact(
            key="slot_start",
            value=SLOT,
            provenance=Provenance(source_doc="referral.pdf", page=2, extractor="test"),
            extracted_at=FIXED_NOW,
        )

        recorded = record_fact(make_case(), fact, now=FIXED_NOW)

        assert "source: referral.pdf p.2" in recorded.timeline[-1].summary

    def test_the_citation_leaks_neither_the_value_nor_the_trust_level(self) -> None:
        """It goes in a summary, so it has to be safe there."""
        fact = Fact(
            key="slot_start",
            value=SLOT,
            provenance=Provenance(source_doc="referral.pdf", page=2, extractor="test"),
            extracted_at=FIXED_NOW,
        )

        assert SLOT not in fact.citation
        assert str(fact.trust_level) not in fact.citation


class TestTheTimeline:
    def test_recording_appends_an_evidence_entry_and_bumps_the_version(self) -> None:
        case = make_case()

        recorded = record_fact(case, make_fact("patient_id", "PT-4417"), now=FIXED_NOW)

        assert recorded.timeline[-1].kind is TimelineKind.EVIDENCE_RECORDED
        assert recorded.version == case.version + 1

    def test_promotion_appends_a_promotion_entry_and_bumps_the_version(self) -> None:
        case, raw = case_with_raw_slot()

        promoted = promote_fact(
            case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        entry = promoted.timeline[-1]
        assert entry.kind is TimelineKind.FACT_PROMOTED
        assert entry.payload["from_trust_level"] == "UNTRUSTED"
        assert entry.payload["to_trust_level"] == "CONTROLLED"
        assert promoted.version == case.version + 1

    def test_summaries_omit_the_value_and_diagnostics_carry_it(self) -> None:
        case, raw = case_with_raw_slot()

        promoted = promote_fact(
            case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        for entry in promoted.timeline:
            assert SLOT not in entry.summary
        assert any(SLOT in (entry.diagnostic or "") for entry in promoted.timeline)

    def test_nothing_else_about_the_case_moves(self) -> None:
        case, raw = case_with_raw_slot()

        promoted = promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert promoted.state is case.state
        assert promoted.case_id == case.case_id
        assert promoted.approvals == case.approvals
        assert promoted.actions == case.actions
