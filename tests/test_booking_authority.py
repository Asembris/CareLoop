"""A booking's patient must be established by the case, not merely supplied by the planner.

The defect these tests exist for: ``allow-booking-on-validated-slot`` guarded ``patient_id`` with
``args_present``, which :func:`careloop.policy.engine._condition_holds` implements as "the canonical
value is neither absent nor empty". So the engine *computed* ``patient_id: UNTRUSTED`` and then
never asked. A booking with a provider from trusted configuration and a validated slot reached
``ALLOW`` carrying any non-empty string at all — and ``book_appointment`` is the one tool with a
genuinely external effect, so the string reached Demo World over HTTP and was recorded there as the
person being seen.

**This is independent of typed trust and is not a variation on it.** Session I bound trust to
``(fact key, canonical value)``, and that repair is working here: ``resolve_trust`` returns
``UNTRUSTED`` for every unsafe shape below, correctly, every time. The hole was that no rule read
the answer. One is about how trust is computed; this is about whether it is required.

Four shapes were reproduced against the shipped ``rules.yaml``, ``REGISTRY`` and ``TRUSTED_CONFIG``
before the rule was written, and all four reached ``ALLOW``:

* a patient identifier the planner invented, which no document mentions;
* a literal borrowed from an unrelated fact key — a validated ``claim_number`` that happens to be
  spelled like a patient id;
* a ``patient_id`` fact that exists but is still ``UNTRUSTED``, because its document failed its
  schema or a cross-document check withheld the key;
* the *real* identifier misquoted — ``"PT-4417 "``, ``"pt-4417"`` — which ``slot_start`` has always
  refused and which session B measured a live model doing 8 times out of 8 on that argument.

Demo World validates ``(provider_id, slot_start)`` against its seeded slots and the idempotency key.
It performs no patient validation whatsoever, so nothing downstream was refusing these either — and
a third-party service's own consistency rule would not be an authority decision if it did.

:class:`TestTheHistoricalPatientSemanticsAreDetected` is the non-vacuity proof. It rebuilds the
shipped rules with exactly two edits — the ``DENY`` rule removed, and the ``patient_id`` trust
predicate replaced by ``args_present`` — and requires the tests above to notice. It is deliberately
**not** the allow-all mutant in ``evals/replay.py``: that one proves the authority layer exists, and
this one has to prove that *this property* is what refuses these calls.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from helpers import make_case, make_fact, trusted_fact

from careloop.graph.trusted import configured, trusted_facts
from careloop.policy import (
    RULES_PATH,
    PolicyEngine,
    ReasonCode,
    Verdict,
    default_rules,
    load_rules_from_mapping,
)
from careloop.provenance import TrustLevel
from careloop.state import CaseState
from careloop.tools import REGISTRY

PROVIDER = configured("provider_id")

PATIENT = "PT-4417"
SLOT = "2026-09-03T09:30:00"

#: The rule that must refuse a booking whose patient the case has not established.
DENY_RULE = "deny-booking-for-unverified-patient"

#: The autonomous path. Reached only when provider, slot *and* patient all resolve.
ALLOW_RULE = "allow-booking-on-validated-slot"


@pytest.fixture
def engine() -> PolicyEngine:
    from careloop.policy import default_engine

    return default_engine()


def _case(*facts, state: CaseState = CaseState.PLANNING):
    """A case carrying the shipped trusted configuration, plus whatever a test adds."""
    return make_case(state=state, facts=(*trusted_facts(), *facts))


def _hero_evidence() -> tuple:
    """What the referral and the appointment instructions leave on a legitimate case.

    ``slot_start`` and ``patient_id`` both ``CONTROLLED``, which is what
    :func:`careloop.graph.extract.validate_and_promote` produces for them — not ``TRUSTED``, which
    nothing in CareLoop can produce for either.
    """
    return (
        make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
        make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED),
    )


def booking(**overrides: object) -> dict[str, object]:
    """The legitimate booking arguments, with whatever a test wants changed."""
    args: dict[str, object] = {
        "provider_id": PROVIDER,
        "patient_id": PATIENT,
        "slot_start": SLOT,
        "reason": "cardiology review",
    }
    args.update(overrides)
    return args


class TestThePatientMustBeEstablishedByTheCase:
    """Every one of these reached ``ALLOW`` under the shipped rules before the DENY rule existed."""

    def test_an_invented_patient_is_denied(self, engine: PolicyEngine) -> None:
        """The headline. Nothing on the case mentions this person at all.

        Not a collision and not a promotion failure: the planner is free to put any string in any
        argument of the tool it chose, and until this rule existed a string nobody had ever seen was
        as good as the one five documents agree on.
        """
        case = _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))

        decision = engine.evaluate("book_appointment", booking(patient_id="PT-INVENTED-999"), case)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST
        assert decision.matched_rule_id == DENY_RULE
        assert decision.arg_trust["patient_id"] is TrustLevel.UNTRUSTED

    def test_a_patient_borrowed_from_another_fact_key_is_denied(self, engine: PolicyEngine) -> None:
        """Typed trust is working and the booking rule was not reading it.

        ``claim_number`` is validated, ``CONTROLLED`` and spelled exactly like a patient identifier.
        ``resolve_trust`` correctly refuses to lend it to ``patient_id`` — session I's repair — and
        the verdict was ``ALLOW`` anyway, because ``args_present`` never consulted the trust the
        engine had already computed.
        """
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("claim_number", PATIENT, TrustLevel.CONTROLLED),
        )

        decision = engine.evaluate("book_appointment", booking(), case)

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == DENY_RULE
        assert decision.arg_trust["patient_id"] is TrustLevel.UNTRUSTED

    def test_a_patient_that_never_validated_is_denied(self, engine: PolicyEngine) -> None:
        """The remediable case: the fact is there, extraction recorded it, nothing promoted it.

        A document that failed its schema, or a ``patient-id-agrees-across-documents`` failure, both
        leave exactly this. ``INSUFFICIENT_FACT_TRUST`` is the honest code — correct or re-supply
        the document and the value becomes usable.
        """
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("patient_id", PATIENT, TrustLevel.UNTRUSTED),
        )

        decision = engine.evaluate("book_appointment", booking(), case)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST
        assert decision.matched_rule_id == DENY_RULE

    @pytest.mark.parametrize("misquoted", ["PT-4417 ", " PT-4417", "pt-4417", "PT-4417."])
    def test_a_misquoted_patient_is_denied(self, engine: PolicyEngine, misquoted: str) -> None:
        """Exact transcription, which is what ``slot_start`` has always demanded.

        :func:`careloop.provenance.canonical.canonical_value` is verbatim by design — no trimming,
        no case folding, no normalisation — so a value that differs by so much as a full stop is a
        different value and matches no fact. The planner's own system prompt says exactly that;
        until now the sentence was not true of this argument.
        """
        case = _case(*_hero_evidence())

        decision = engine.evaluate("book_appointment", booking(patient_id=misquoted), case)

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == DENY_RULE

    @pytest.mark.parametrize("supplied", [{}, {"patient_id": ""}])
    def test_an_absent_patient_is_denied_by_trust_rather_than_by_presence(
        self, engine: PolicyEngine, supplied: dict[str, str]
    ) -> None:
        """Removing ``args_present`` did not stop the empty and omitted cases being refused.

        ``_condition_holds`` resolves an argument the call omits to ``UNTRUSTED``, and no fact can
        carry an empty value under ``patient_id`` because ``patient_id_format`` demands
        ``PT-0000``. So the trust clause strictly dominates the presence clause, and it produces a
        better code: ``INSUFFICIENT_FACT_TRUST`` rather than the ``NO_RULE_MATCH`` a fallthrough
        gave.
        """
        args = booking()
        args.pop("patient_id")
        args.update(supplied)

        decision = engine.evaluate("book_appointment", args, _case(*_hero_evidence()))

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST
        assert decision.matched_rule_id == DENY_RULE

    def test_conflicting_patient_evidence_denies_the_booking(self, engine: PolicyEngine) -> None:
        """Lowest-wins, scoped to the key, reaching the booking rule.

        Two facts at one ``(key, value)`` and the lower one decides. In production
        :func:`careloop.state.evidence.record_fact` refuses the duplicate and ``promote_fact``
        raises rather than letting this exist — this asserts what the *policy engine* does if it
        ever does, which is the layer this rule lives in.
        """
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED),
            make_fact("patient_id", PATIENT, TrustLevel.UNTRUSTED, source_doc="forged.pdf"),
        )

        decision = engine.evaluate("book_appointment", booking(), case)

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == DENY_RULE


class TestTheLegitimateBookingIsUnchanged:
    """Bounding the rule. A guard that refuses the hero path too is not a guard, it is an outage."""

    def test_a_controlled_patient_books_autonomously(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("book_appointment", booking(), _case(*_hero_evidence()))

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == ALLOW_RULE
        assert decision.reason_code is None
        assert decision.arg_trust["patient_id"] is TrustLevel.CONTROLLED

    def test_the_allow_holds_from_every_state_the_rule_names(self, engine: PolicyEngine) -> None:
        """``PLANNING`` routes the proposal, ``EXECUTING`` is the tool boundary. Both reach it."""
        for state in (CaseState.PLANNING, CaseState.READY_TO_EXECUTE, CaseState.EXECUTING):
            case = _case(*_hero_evidence(), state=state)

            assert engine.evaluate("book_appointment", booking(), case).verdict is Verdict.ALLOW

    def test_a_higher_trust_patient_would_satisfy_it_too(self, engine: PolicyEngine) -> None:
        """The requirement is ``CONTROLLED`` **or higher**, not ``CONTROLLED`` exactly.

        Unreachable in production — ``TRUSTED_CONFIG`` declares no ``patient_id`` and ``promote``
        cannot manufacture ``TRUSTED`` — and asserted anyway, so that the day trusted configuration
        does name a patient the rule does not have to be rewritten.
        """
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            trusted_fact("patient_id", PATIENT),
        )

        assert engine.evaluate("book_appointment", booking(), case).verdict is Verdict.ALLOW

    def test_the_provider_requirement_is_untouched(self, engine: PolicyEngine) -> None:
        """A validated patient does not buy a destination. ``DESTINATION`` is not promotable."""
        case = _case(*_hero_evidence(), make_fact("provider_id", "PROV-ELSEWHERE"))

        decision = engine.evaluate("book_appointment", booking(provider_id="PROV-ELSEWHERE"), case)

        assert decision.verdict is Verdict.DENY

    def test_the_slot_requirement_is_untouched(self, engine: PolicyEngine) -> None:
        """And an established patient does not validate a time."""
        case = _case(
            make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED),
            make_fact("slot_start", SLOT, TrustLevel.UNTRUSTED),
        )

        decision = engine.evaluate("book_appointment", booking(), case)

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == "deny-booking-on-unvalidated-slot"

    def test_no_rule_ever_puts_a_booking_in_front_of_a_caregiver(self) -> None:
        """The product property, pinned so a future patch cannot buy safety with friction.

        Booking is the autonomous path: when the evidence establishes provider, slot and patient,
        CareLoop acts. Fixing a missing deterministic check by asking a human instead would be
        compensating for the check rather than making it.
        """
        parked = {
            rule.id
            for rule in default_rules().by_effect(Verdict.REQUIRE_HUMAN)
            if "book_appointment" in rule.tools
        }

        assert parked == set()


# -- the non-vacuity mutant ----------------------------------------------------------------------


def historical_ruleset():
    """The shipped rules with the patient provenance requirement removed, and nothing else.

    Built by editing the *parsed* shipped document rather than by copying it, so the mutant cannot
    drift away from what ships and cannot differ in some second respect nobody noticed. Two edits,
    each asserted to have actually happened:

    1. ``deny-booking-for-unverified-patient`` is dropped.
    2. ``allow-booking-on-validated-slot`` loses its ``patient_id`` trust predicate and regains
       ``args_present: [patient_id]``.

    Everything else — the argument classes, the state clauses, the match ordering, the loader's
    validation against the registry — is the shipped system. This is the same discipline as
    ``tests/test_typed_trust.py::value_only_resolve_trust`` and
    ``evals/replay.py::permissive_engine``: change one property, keep the rest, so whatever moves is
    attributable to that property alone.
    """
    raw: Any = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

    before = len(raw["rules"])
    raw["rules"] = [rule for rule in raw["rules"] if rule["id"] != DENY_RULE]
    assert len(raw["rules"]) == before - 1, f"{DENY_RULE} was not in the shipped rules"

    edited = 0
    for rule in raw["rules"]:
        if rule["id"] != ALLOW_RULE:
            continue
        predicates = rule["when"]["arg_trust_at_least"]
        rule["when"]["arg_trust_at_least"] = [
            predicate for predicate in predicates if predicate["arg"] != "patient_id"
        ]
        assert len(rule["when"]["arg_trust_at_least"]) == len(predicates) - 1, (
            f"{ALLOW_RULE} does not require trust of patient_id"
        )
        rule["when"]["args_present"] = ["patient_id"]
        edited += 1
    assert edited == 1

    return load_rules_from_mapping(raw, REGISTRY)


@pytest.fixture
def presence_only_engine() -> PolicyEngine:
    """The shipped engine with patient provenance replaced by patient presence."""
    return PolicyEngine(historical_ruleset(), REGISTRY)


class TestTheHistoricalPatientSemanticsAreDetected:
    """Remove the requirement and every test above must fail.

    A regression suite that passes with the defence disabled was never testing the defence. Each
    method here asserts the *unauthorised* verdict the shipped system produced before the rule was
    written, so if the requirement is ever weakened back to presence the matching test in
    :class:`TestThePatientMustBeEstablishedByTheCase` fails — which is why both halves are here.
    """

    def test_the_mutant_differs_from_the_shipped_rules_in_one_property(self) -> None:
        """Bounding the mutant itself. Two edits, and no third."""
        mutant = historical_ruleset()
        shipped = default_rules()

        assert shipped.rule_ids - mutant.rule_ids == {DENY_RULE}
        assert mutant.rule_ids - shipped.rule_ids == set()

        shipped_rules = {rule.id: rule for rule in shipped.rules}
        for rule in mutant.rules:
            if rule.id == ALLOW_RULE:
                continue
            assert rule == shipped_rules[rule.id], f"the mutant also changed {rule.id}"

        booking_rule = next(rule for rule in mutant.rules if rule.id == ALLOW_RULE)
        assert {predicate.arg for predicate in booking_rule.when.arg_trust_at_least} == {
            "provider_id",
            "slot_start",
        }
        assert booking_rule.when.args_present == ("patient_id",)

    def test_the_invented_patient_reappears(self, presence_only_engine: PolicyEngine) -> None:
        case = _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))

        decision = presence_only_engine.evaluate(
            "book_appointment", booking(patient_id="PT-INVENTED-999"), case
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == ALLOW_RULE

    def test_the_borrowed_key_patient_reappears(self, presence_only_engine: PolicyEngine) -> None:
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("claim_number", PATIENT, TrustLevel.CONTROLLED),
        )

        decision = presence_only_engine.evaluate("book_appointment", booking(), case)

        assert decision.verdict is Verdict.ALLOW

    def test_the_unvalidated_patient_reappears(self, presence_only_engine: PolicyEngine) -> None:
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("patient_id", PATIENT, TrustLevel.UNTRUSTED),
        )

        decision = presence_only_engine.evaluate("book_appointment", booking(), case)

        assert decision.verdict is Verdict.ALLOW

    def test_the_misquoted_patient_reappears(self, presence_only_engine: PolicyEngine) -> None:
        case = _case(*_hero_evidence())

        decision = presence_only_engine.evaluate(
            "book_appointment", booking(patient_id="pt-4417"), case
        )

        assert decision.verdict is Verdict.ALLOW

    def test_the_other_booking_requirements_are_unaffected_by_the_mutation(
        self, presence_only_engine: PolicyEngine
    ) -> None:
        """The mutant must not be permitting or refusing everything wholesale.

        Provider and slot are still enforced under it, and the legitimate booking is still allowed.
        If the mutation moved those too, the reappearances above would not be attributable to the
        patient property alone.
        """
        unvalidated_slot = presence_only_engine.evaluate(
            "book_appointment",
            booking(),
            _case(make_fact("slot_start", SLOT, TrustLevel.UNTRUSTED)),
        )
        assert unvalidated_slot.verdict is Verdict.DENY
        assert unvalidated_slot.matched_rule_id == "deny-booking-on-unvalidated-slot"

        untrusted_provider = presence_only_engine.evaluate(
            "book_appointment",
            booking(provider_id="PROV-ELSEWHERE"),
            _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED)),
        )
        assert untrusted_provider.verdict is Verdict.DENY

        legitimate = presence_only_engine.evaluate(
            "book_appointment", booking(), _case(*_hero_evidence())
        )
        assert legitimate.verdict is Verdict.ALLOW

    def test_the_mutation_leaves_the_other_tools_alone(
        self, presence_only_engine: PolicyEngine
    ) -> None:
        """Narrow, not global. The email, upload and grant paths decide exactly as they shipped."""
        from careloop.policy import default_engine

        shipped = default_engine()
        calls = (
            ("send_email", {"to": configured("caregiver_email"), "subject": "s", "body": "b"}),
            (
                "upload_document",
                {"portal_id": configured("portal_id"), "document_id": "doc-0005-receipt"},
            ),
            (
                "grant_portal_access",
                {
                    "grantee": configured("caregiver_email"),
                    "role": "records_admin",
                    "portal_id": configured("portal_id"),
                },
            ),
        )
        case = _case(
            *_hero_evidence(), make_fact("document_id", "doc-0005-receipt", TrustLevel.CONTROLLED)
        )

        for tool, args in calls:
            assert (
                presence_only_engine.evaluate(tool, args, case).verdict
                == shipped.evaluate(tool, args, case).verdict
            ), tool
