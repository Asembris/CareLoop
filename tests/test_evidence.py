"""Recording and promoting evidence on a case.

The test that carries this module is :class:`TestPromotionIsVisibleToTheEngine`. Everything else
checks bookkeeping; that one checks the property, and it is the only one that can tell the fix
from the bug — see :class:`TestTheAssertionThatWouldNotHaveCaught` for the proof of that claim.
"""

from __future__ import annotations

import pytest
from helpers import FIXED_NOW, make_case, make_fact

from careloop.policy.engine import resolve_trust
from careloop.provenance import ArgClass, PromotionForbidden, TrustLevel, promote
from careloop.state import TimelineKind
from careloop.state.evidence import (
    FactNotOnCase,
    PromotionIneffective,
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

        assert resolve_trust(SLOT, promoted) is TrustLevel.CONTROLLED

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
        assert resolve_trust(SLOT, case) is TrustLevel.UNTRUSTED


class TestPromotionRefusesWhatItCannotMakeTrue:
    def test_a_fact_the_case_does_not_hold_is_refused(self) -> None:
        stray = make_fact("slot_start", SLOT)

        with pytest.raises(FactNotOnCase):
            promote_fact(make_case(), stray, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

    def test_an_invisible_promotion_raises_rather_than_succeeding_quietly(self) -> None:
        raw = make_fact("slot_start", SLOT)
        duplicate = make_fact("appointment_time", SLOT, source_doc="continuity-notice.pdf")
        case = record_fact(record_fact(make_case(), raw, now=FIXED_NOW), duplicate, now=FIXED_NOW)

        with pytest.raises(PromotionIneffective) as raised:
            promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert "appointment_time" in str(raised.value)

    def test_a_duplicate_already_at_the_target_level_does_not_block(self) -> None:
        raw = make_fact("slot_start", SLOT)
        trusted_twin = make_fact("configured_slot", SLOT, TrustLevel.TRUSTED, "trusted-config")
        case = record_fact(
            record_fact(make_case(), raw, now=FIXED_NOW), trusted_twin, now=FIXED_NOW
        )

        promoted = promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert resolve_trust(SLOT, promoted) is TrustLevel.CONTROLLED

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
