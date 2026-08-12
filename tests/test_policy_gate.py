"""The shared decision and its cache.

The class that carries this module is :class:`TestTheCacheCannotAnswerForAChangedCase`. The rest
checks that caching happens at all; that one checks that it stops happening when it must, which is
the property session 2 carried forward as blocking.
"""

from __future__ import annotations

import pytest
from helpers import FIXED_NOW, make_case, make_fact, trusted_fact

from careloop.policy.gate import (
    PolicyDenied,
    PolicyGate,
    decision_cache_key,
    facts_fingerprint,
)
from careloop.policy.verdicts import ReasonCode, Verdict
from careloop.provenance import ArgClass, TrustLevel
from careloop.state import CaseState, record_fact
from careloop.state.evidence import promote_fact

SLOT = "2026-09-03T09:30:00"
PROVIDER = "PROV-NORTHGATE-CARD"


def booking_args(slot: str = SLOT) -> dict[str, str]:
    return {
        "provider_id": PROVIDER,
        "patient_id": "PT-4417",
        "slot_start": slot,
        "reason": "cardiology review",
    }


class Holder:
    """A mutable case reference, standing in for whatever the execute node holds."""

    def __init__(self, case) -> None:
        self.case = case

    def __call__(self):
        return self.case


class TestOneEvaluationServesBothEnforcementPoints:
    def test_a_repeated_question_about_an_unchanged_case_is_answered_from_cache(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        first = gate.decide("send_email", {"to": "x@evil.example", "subject": "s", "body": "b"})
        second = gate.decide("send_email", {"to": "x@evil.example", "subject": "s", "body": "b"})

        assert gate.evaluations == 1
        assert first is second

    def test_a_different_tool_is_evaluated_separately(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        gate.decide("send_email", {"to": "a@b.example", "subject": "s", "body": "b"})
        gate.decide("fetch_document", {"document_id": "doc-0001-referral"})

        assert gate.evaluations == 2

    def test_different_arguments_are_evaluated_separately(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        gate.decide("send_email", {"to": "a@b.example", "subject": "s", "body": "b"})
        gate.decide("send_email", {"to": "c@d.example", "subject": "s", "body": "b"})

        assert gate.evaluations == 2


class TestTheCacheCannotAnswerForAChangedCase:
    """The TOCTOU window session 2 recorded as blocking for this commit.

    Each test changes the case between two identical questions and asserts the second question
    reached the engine. With ``case.version`` and the facts fingerprint dropped from the key,
    every one of these returns the first answer and the counts stay at 1.
    """

    def test_a_promoted_fact_between_the_two_checks_forces_a_fresh_evaluation(self) -> None:
        raw = make_fact("slot_start", SLOT)
        case = record_fact(make_case(state=CaseState.EXECUTING), raw, now=FIXED_NOW)
        case = record_fact(case, trusted_fact("provider_id", PROVIDER), now=FIXED_NOW)
        holder = Holder(case)
        gate = PolicyGate(holder)

        before = gate.decide("book_appointment", booking_args())
        assert before.verdict is Verdict.DENY
        assert before.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST

        holder.case = promote_fact(
            holder.case, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )
        after = gate.decide("book_appointment", booking_args())

        assert gate.evaluations == 2
        assert after.verdict is Verdict.ALLOW

    def test_a_new_fact_between_the_two_checks_forces_a_fresh_evaluation(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        gate.decide("send_email", {"to": PROVIDER, "subject": "s", "body": "b"})
        holder.case = record_fact(
            holder.case, trusted_fact("caregiver_email", PROVIDER), now=FIXED_NOW
        )
        gate.decide("send_email", {"to": PROVIDER, "subject": "s", "body": "b"})

        assert gate.evaluations == 2

    def test_a_state_change_between_the_two_checks_forces_a_fresh_evaluation(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        gate.decide("send_email", {"to": "a@b.example", "subject": "s", "body": "b"})
        holder.case = holder.case.model_copy(update={"state": CaseState.NEW})
        gate.decide("send_email", {"to": "a@b.example", "subject": "s", "body": "b"})

        assert gate.evaluations == 2

    def test_the_key_itself_moves_when_any_of_the_three_moves(self) -> None:
        case = make_case(state=CaseState.EXECUTING)
        args = {"to": "a@b.example", "subject": "s", "body": "b"}
        base = decision_cache_key(case, "send_email", args)

        bumped = decision_cache_key(case.model_copy(update={"version": 9}), "send_email", args)
        restated = decision_cache_key(
            case.model_copy(update={"state": CaseState.PLANNING}), "send_email", args
        )
        refacted = decision_cache_key(
            record_fact(case, make_fact("k", "v"), now=FIXED_NOW), "send_email", args
        )

        assert len({base, bumped, restated, refacted}) == 4

    def test_a_stale_answer_is_what_the_incomplete_key_would_have_produced(self) -> None:
        """Proof the guard is not vacuous: the pre-fix key really does collide.

        This is the key session 2 planned — ``(case_id, tool, args)`` and nothing else. It is
        identical across a case change that flips the verdict, which is exactly the window.
        """
        raw = make_fact("slot_start", SLOT)
        before = record_fact(make_case(state=CaseState.EXECUTING), raw, now=FIXED_NOW)
        after = promote_fact(
            before, raw, arg_class=ArgClass.VALUE, validator="iso_datetime", now=FIXED_NOW
        )

        def old_key(case) -> tuple[str, str, str]:
            return (case.case_id, "book_appointment", repr(sorted(booking_args().items())))

        assert old_key(before) == old_key(after)
        assert decision_cache_key(before, "book_appointment", booking_args()) != decision_cache_key(
            after, "book_appointment", booking_args()
        )


class TestUncanonicalizableArgumentsAreNeverCached:
    def test_a_float_argument_is_evaluated_fresh_every_time(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        gate.decide("send_email", {"to": "a@b.example", "subject": "s", "body": 1.5})
        gate.decide("send_email", {"to": "a@b.example", "subject": "s", "body": 1.5})

        assert gate.evaluations == 2

    def test_such_a_call_has_no_cache_key_at_all(self) -> None:
        case = make_case(state=CaseState.EXECUTING)

        assert decision_cache_key(case, "send_email", {"to": None}) is None

    def test_and_it_still_fails_closed(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        decision = gate.decide("send_email", {"to": 1.5, "subject": "s", "body": "b"})

        assert decision.verdict is Verdict.DENY


class TestTheFactsFingerprint:
    def test_it_moves_when_a_trust_level_moves(self) -> None:
        raw = make_fact("slot_start", SLOT)
        case = record_fact(make_case(), raw, now=FIXED_NOW)
        promoted = promote_fact(case, raw, arg_class=ArgClass.VALUE, validator="v", now=FIXED_NOW)

        assert facts_fingerprint(case) != facts_fingerprint(promoted)

    def test_it_moves_when_a_value_moves(self) -> None:
        one = record_fact(make_case(), make_fact("k", "a"), now=FIXED_NOW)
        other = record_fact(make_case(), make_fact("k", "b"), now=FIXED_NOW)

        assert facts_fingerprint(one) != facts_fingerprint(other)

    def test_it_does_not_move_for_a_timeline_only_change(self) -> None:
        """The fingerprint identifies the trust picture; the version covers everything else."""
        case = record_fact(make_case(), make_fact("k", "a"), now=FIXED_NOW)
        later = record_fact(case, make_fact("k", "a"), now=FIXED_NOW).model_copy(
            update={"facts": case.facts}
        )

        assert facts_fingerprint(case) == facts_fingerprint(later)
        assert decision_cache_key(case, "t", {}) != decision_cache_key(later, "t", {})

    def test_an_empty_case_still_fingerprints(self) -> None:
        assert facts_fingerprint(make_case())


class TestCheckRaisesWithoutLeaking:
    def test_a_denied_call_raises_policy_denied(self) -> None:
        holder = Holder(make_case(state=CaseState.EXECUTING))
        gate = PolicyGate(holder)

        with pytest.raises(PolicyDenied) as raised:
            gate.check("send_email", {"to": "x@evil.example", "subject": "s", "body": "b"})

        assert raised.value.decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_require_human_raises_too_because_a_tool_boundary_cannot_park(self) -> None:
        case = record_fact(
            make_case(state=CaseState.EXECUTING),
            trusted_fact("caregiver_email", "carer@example.org"),
            now=FIXED_NOW,
        )
        gate = PolicyGate(Holder(case))

        with pytest.raises(PolicyDenied) as raised:
            gate.check("send_email", {"to": "carer@example.org", "subject": "s", "body": "b"})

        assert raised.value.decision.verdict is Verdict.REQUIRE_HUMAN

    def test_the_exception_message_carries_no_argument_value(self) -> None:
        gate = PolicyGate(Holder(make_case(state=CaseState.EXECUTING)))

        with pytest.raises(PolicyDenied) as raised:
            gate.check("send_email", {"to": "x@evil.example", "subject": "s", "body": "secret"})

        assert "evil.example" not in str(raised.value)
        assert "secret" not in str(raised.value)

    def test_but_the_decision_it_carries_does_for_the_timeline(self) -> None:
        gate = PolicyGate(Holder(make_case(state=CaseState.EXECUTING)))

        with pytest.raises(PolicyDenied) as raised:
            gate.check("send_email", {"to": "x@evil.example", "subject": "s", "body": "b"})

        assert raised.value.decision.args["to"] == "x@evil.example"
        assert raised.value.decision.arg_trust["to"] is TrustLevel.UNTRUSTED

    def test_an_allowed_call_returns_its_decision(self) -> None:
        gate = PolicyGate(Holder(make_case(state=CaseState.GATHERING_EVIDENCE)))

        decision = gate.check("fetch_document", {"document_id": "doc-0001-referral"})

        assert decision.verdict is Verdict.ALLOW
