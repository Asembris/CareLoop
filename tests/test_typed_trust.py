"""Trust is bound to a semantic fact key, not to a bare literal.

The defect these tests exist for: :func:`careloop.policy.engine.resolve_trust` used to take
``(value, case)`` and match on ``fact.value`` alone, so the question it answered was *"has this
string ever been seen at this trust level, under any key at all?"*. A value that earned trust for
one purpose therefore satisfied a trust requirement for an entirely different argument.

All four collisions in :class:`TestCrossFieldCollisionsAreRefused` were **reproduced against the
shipped** ``rules.yaml``, ``REGISTRY`` and ``TRUSTED_CONFIG`` before the fix, and every one of them
reached a verdict the trust model says is impossible. The worst is
:meth:`~TestCrossFieldCollisionsAreRefused.test_the_configured_portal_id_does_not_satisfy_role`:
``deny-authority-grant-below-trusted`` is the system's only ``AUTHORITY`` guard, and its own
diagnostic argues that a role "can never resolve TRUSTED" because ``AUTHORITY`` is absent from
``PROMOTABLE_ARG_CLASSES``. That argument is about *promotion* and is correct. It said nothing about
*collision*, and naming the configured portal id as a role walked straight past it — no promotion
required.

It was reachable, not theoretical. :func:`careloop.agents.prompts.planner_user_prompt` renders
``fact {key}: {value}`` for **every** fact, so the planner is handed the three configured literals
in its own prompt and may place any string in any declared argument of the tool it chooses.

:class:`TestTheOldSemanticsAreDetected` is the non-vacuity proof: it restores the historical
value-only resolution and requires these tests to notice.
"""

from __future__ import annotations

import pytest
from helpers import make_case, make_fact, trusted_fact

from careloop.graph.trusted import TRUSTED_CONFIG, configured, trusted_facts
from careloop.policy import PolicyConfigError, ReasonCode, Verdict, load_rules_from_text
from careloop.policy.engine import PolicyEngine, default_engine
from careloop.provenance import TrustLevel
from careloop.state import CaseState
from careloop.tools import REGISTRY

CARER = configured("caregiver_email")
PROVIDER = configured("provider_id")
PORTAL = configured("portal_id")

PATIENT = "PT-4417"
SLOT = "2026-09-03T09:30:00"
DOCUMENT = "doc-0004-receipt"


@pytest.fixture
def engine() -> PolicyEngine:
    return default_engine()


def _case(*facts, state: CaseState = CaseState.PLANNING):
    """A case carrying the shipped trusted configuration, plus whatever a test adds."""
    return make_case(state=state, facts=(*trusted_facts(), *facts))


class TestTheConfiguredLiteralsAreDistinct:
    """A precondition. Without it the collision tests could pass for the wrong reason."""

    def test_the_three_configured_values_are_distinct(self) -> None:
        values = [value for _key, value in TRUSTED_CONFIG]
        assert len(set(values)) == len(values)

    def test_no_configured_value_collides_with_the_test_payloads(self) -> None:
        values = {value for _key, value in TRUSTED_CONFIG}
        assert values.isdisjoint({PATIENT, SLOT, DOCUMENT})


class TestCrossFieldCollisionsAreRefused:
    """Every one of these reached an unauthorised verdict under value-only resolution."""

    def test_a_controlled_patient_id_does_not_satisfy_document_id(
        self, engine: PolicyEngine
    ) -> None:
        """Reproduced as ALLOW ``allow-upload-to-trusted-portal``.

        ``patient_id`` is promoted to ``CONTROLLED`` by the referral's validator, which confirms it
        matches ``PT-0000``. Nothing about that check says anything whatsoever about a document, and
        no document validator ever saw this string — ``document_id_format`` would have refused it.
        """
        case = _case(make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED))

        decision = engine.evaluate(
            "upload_document", {"portal_id": PORTAL, "document_id": PATIENT}, case
        )

        assert decision.verdict is Verdict.DENY
        assert decision.arg_trust["document_id"] is TrustLevel.UNTRUSTED

    def test_a_controlled_document_id_does_not_satisfy_slot_start(
        self, engine: PolicyEngine
    ) -> None:
        """Reproduced as ALLOW ``allow-booking-on-validated-slot`` — and ``book_appointment`` is
        the one tool with a genuinely external effect, so this reached the clinic over HTTP.

        Only Demo World's own "this clinic offers no slot at ..." check refused it. That is a
        downstream validity check in a system CareLoop does not control, not an authority decision,
        and ``upload_document`` has no equivalent.
        """
        case = _case(make_fact("document_id", DOCUMENT, TrustLevel.CONTROLLED))

        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": DOCUMENT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST
        assert decision.matched_rule_id == "deny-booking-on-unvalidated-slot"

    def test_the_configured_portal_id_does_not_satisfy_role(self, engine: PolicyEngine) -> None:
        """The headline. Reproduced as REQUIRE_HUMAN, from trusted configuration alone.

        No poisoned document and no promotion were needed: the case carried nothing but the three
        configured facts. Naming the configured portal id as the *role* made ``role`` resolve
        ``TRUSTED``, ``deny-authority-grant-below-trusted`` did not match, and the system put a
        question to the caregiver that the trust model says can never legitimately be asked.
        """
        case = _case()

        decision = engine.evaluate(
            "grant_portal_access",
            {"grantee": CARER, "role": PORTAL, "portal_id": PORTAL},
            case,
        )

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-authority-grant-below-trusted"
        assert decision.arg_trust["role"] is TrustLevel.UNTRUSTED

    def test_the_configured_provider_id_does_not_satisfy_the_email_recipient(
        self, engine: PolicyEngine
    ) -> None:
        """Reproduced as REQUIRE_HUMAN: the destination guard was satisfied by a provider id.

        Not an exfiltration on its own — the literal is CareLoop's own — but it is the destination
        guard being satisfied by something that is not a destination, which is the same defect that
        makes the role case dangerous.
        """
        case = _case()

        decision = engine.evaluate(
            "send_email", {"to": PROVIDER, "subject": "s", "body": "b"}, case
        )

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-email-to-untrusted-recipient"

    def test_the_configured_caregiver_email_does_not_satisfy_the_provider(
        self, engine: PolicyEngine
    ) -> None:
        """The collision run in the other direction, so the fix cannot be a one-way special case."""
        case = _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))

        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": CARER,
                "patient_id": PATIENT,
                "slot_start": SLOT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.DENY
        assert decision.arg_trust["provider_id"] is TrustLevel.UNTRUSTED


class TestTheCorrectlyKeyedFactStillSatisfies:
    """The other half. Exactness must not be achieved by denying everything."""

    def test_booking_under_the_correct_keys_is_allowed(self, engine: PolicyEngine) -> None:
        case = _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))

        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": SLOT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"

    def test_upload_under_the_correct_keys_is_allowed(self, engine: PolicyEngine) -> None:
        case = _case(make_fact("document_id", DOCUMENT, TrustLevel.CONTROLLED))

        decision = engine.evaluate(
            "upload_document", {"portal_id": PORTAL, "document_id": DOCUMENT}, case
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-upload-to-trusted-portal"

    def test_email_to_the_configured_caregiver_reaches_the_human(
        self, engine: PolicyEngine
    ) -> None:
        """``send_email.to`` draws on ``caregiver_email``, so name and key genuinely differ here."""
        decision = engine.evaluate(
            "send_email", {"to": CARER, "subject": "s", "body": "b"}, _case()
        )

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.reason_code is ReasonCode.REQUIRES_HUMAN_APPROVAL

    def test_a_typed_datetime_still_matches_its_iso_fact(self, engine: PolicyEngine) -> None:
        """Canonicalisation is untouched; only the set of facts consulted narrows."""
        from datetime import datetime

        case = _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))

        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": datetime.fromisoformat(SLOT),
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.arg_trust["slot_start"] is TrustLevel.CONTROLLED

    @pytest.mark.parametrize(
        "variant",
        [PORTAL + " ", " " + PORTAL, PORTAL.lower(), PORTAL + ".", PORTAL.replace("O", "0")],
        ids=["trailing-space", "leading-space", "case", "trailing-dot", "homoglyph"],
    )
    def test_a_near_miss_of_a_correctly_keyed_destination_still_fails_closed(
        self, engine: PolicyEngine, variant: str
    ) -> None:
        decision = engine.evaluate(
            "upload_document", {"portal_id": variant, "document_id": DOCUMENT}, _case()
        )

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED


class TestLowestWinsIsScopedToOneKey:
    """Lowest-wins survives, scoped. Cross-key contamination does not."""

    def test_lowest_wins_within_one_key(self, engine: PolicyEngine) -> None:
        """The property the rule exists for: a poisoned duplicate is not laundered by its twin."""
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("provider_id", PROVIDER, TrustLevel.UNTRUSTED),
        )

        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": SLOT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.DENY
        assert decision.arg_trust["provider_id"] is TrustLevel.UNTRUSTED

    def test_an_unrelated_key_carrying_the_same_literal_does_not_drag_trust_down(
        self, engine: PolicyEngine
    ) -> None:
        """The correctness this buys, as distinct from the security it buys.

        ``careloop/graph/trusted.py`` used to warn that no configured value may also appear in a
        document, because a document-sourced twin under *any* key would drag the configured one to
        ``UNTRUSTED`` and deny a legitimate action for a reason no diagnostic explained. Under
        key-scoped resolution that is no longer possible, so correctness no longer depends on the
        configured literals being globally unique.
        """
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("patient_id", PROVIDER, TrustLevel.UNTRUSTED),
        )

        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": SLOT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.arg_trust["provider_id"] is TrustLevel.TRUSTED


class TestArgumentsWithNoTrustKey:
    """``subject``, ``body`` and ``reason`` describe nothing a fact can vouch for."""

    def test_a_free_text_argument_never_gains_trust_however_it_is_recorded(
        self, engine: PolicyEngine
    ) -> None:
        """Even a fact recorded under the argument's own name must not lend it trust."""
        case = _case(trusted_fact("subject", "Cardiology referral"))

        decision = engine.evaluate(
            "send_email", {"to": CARER, "subject": "Cardiology referral", "body": "b"}, case
        )

        assert decision.arg_trust["subject"] is TrustLevel.UNTRUSTED

    def test_the_registry_declares_no_trust_key_for_free_text(self) -> None:
        assert REGISTRY.trust_key("send_email", "subject") is None
        assert REGISTRY.trust_key("send_email", "body") is None
        assert REGISTRY.trust_key("book_appointment", "reason") is None

    def test_an_undeclared_argument_has_no_trust_key(self) -> None:
        """Fails closed for the same reason: nothing establishes trust for what is not declared."""
        assert REGISTRY.trust_key("send_email", "bcc") is None
        assert REGISTRY.trust_key("no_such_tool", "to") is None


class TestTheGrantPathHasNoProductionAuthority:
    """``grant_portal_access.role`` has no legitimate trusted producer, and now says so."""

    def test_no_trusted_configuration_supplies_a_role(self) -> None:
        assert configured("role") is None
        assert "role" not in {key for key, _value in TRUSTED_CONFIG}

    def test_no_promotable_key_supplies_a_role(self) -> None:
        """Nor could one: ``role`` is AUTHORITY-class and validation can never raise one."""
        from careloop.graph.extract import PROMOTABLE_KEYS

        assert "role" not in PROMOTABLE_KEYS

    def test_the_grant_tool_cannot_reach_allow_from_the_shipped_configuration(
        self, engine: PolicyEngine
    ) -> None:
        """Every configured literal tried as a role, in every state a grant could be proposed from.

        The correct safe state is a denied tool, not a manufactured trusted role.
        """
        for state in (CaseState.PLANNING, CaseState.READY_TO_EXECUTE, CaseState.EXECUTING):
            for _key, value in TRUSTED_CONFIG:
                decision = engine.evaluate(
                    "grant_portal_access",
                    {"grantee": CARER, "role": value, "portal_id": PORTAL},
                    _case(state=state),
                )
                assert decision.verdict is Verdict.DENY, (state, value)
                assert decision.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED, (state, value)


class TestARuleMayNotRequireTrustOfAnUntypedArgument:
    """Configuration mistakes about authority fail at load, not at evaluation."""

    def _rules(self, clause: str, arg: str) -> str:
        return (
            "version: 1\nrules:\n"
            "  - id: require-trusted-body\n"
            "    effect: DENY\n"
            "    tools: [send_email]\n"
            "    when:\n"
            f"      {clause}:\n"
            f"        - arg: {arg}\n"
            "          level: TRUSTED\n"
            "    reason_code: NO_RULE_MATCH\n"
            "    diagnostic: d\n"
        )

    @pytest.mark.parametrize("clause", ["arg_trust_at_least", "arg_trust_below"])
    def test_a_trust_predicate_on_an_untyped_argument_fails_to_load(self, clause: str) -> None:
        with pytest.raises(PolicyConfigError) as raised:
            load_rules_from_text(self._rules(clause, "body"), REGISTRY)

        assert "body" in str(raised.value)
        assert "trust_key" in str(raised.value)

    def test_a_trust_predicate_on_a_typed_argument_still_loads(self) -> None:
        """The other half: the check must not be refusing every rule."""
        ruleset = load_rules_from_text(self._rules("arg_trust_at_least", "to"), REGISTRY)

        assert ruleset.rule_ids == {"require-trusted-body"}

    def test_args_present_on_an_untyped_argument_is_still_permitted(self) -> None:
        """Presence is not a trust claim, so it needs no key."""
        ruleset = load_rules_from_text(
            "version: 1\nrules:\n"
            "  - id: allow-with-a-body\n"
            "    effect: ALLOW\n"
            "    tools: [send_email]\n"
            "    when:\n"
            "      args_present: [body]\n"
            "    diagnostic: d\n",
            REGISTRY,
        )

        assert ruleset.rule_ids == {"allow-with-a-body"}

    def test_every_shipped_trust_predicate_names_a_typed_argument(self) -> None:
        """The shipped rules, checked directly rather than only through the loader."""
        from careloop.policy import default_rules

        for rule in default_rules().rules:
            for predicate in (*rule.when.arg_trust_below, *rule.when.arg_trust_at_least):
                for tool in rule.tools:
                    assert REGISTRY.trust_key(tool, predicate.arg) is not None, (
                        f"{rule.id}: {tool}.{predicate.arg}"
                    )


def value_only_resolve_trust(fact_key: str | None, value: object, case) -> TrustLevel:
    """The historical resolver, restored verbatim except for the discarded ``fact_key``.

    Deliberately minimal. It differs from
    :func:`careloop.policy.engine.resolve_trust` in exactly one respect — it ignores the semantic
    key and matches on the canonical value alone — so anything it changes is attributable to that
    and to nothing else. It keeps the same canonicalisation, the same lowest-wins reduction, and
    the same ``UNTRUSTED`` answer for a value with no comparison form, because a mutant that also
    varied those would prove the tests notice *something* rather than that they notice this.

    This is the same discipline as ``evals/replay.py::permissive_engine``, one layer in: that
    mutant removes the rules and keeps the engine, and this one removes the semantic binding and
    keeps everything else. Notably it is **not** a broad permissive mutant — no rule, no argument
    class and no state clause is touched.
    """
    from careloop.provenance.canonical import canonical_value
    from careloop.provenance.trust import at_least

    canonical = canonical_value(value)
    if canonical is None:
        return TrustLevel.UNTRUSTED

    lowest: TrustLevel | None = None
    for fact in case.facts:
        if fact.value != canonical:
            continue
        if lowest is None or not at_least(fact.trust_level, lowest):
            lowest = fact.trust_level

    return TrustLevel.UNTRUSTED if lowest is None else lowest


@pytest.fixture
def value_only_engine(monkeypatch: pytest.MonkeyPatch) -> PolicyEngine:
    """The shipped engine with semantic binding removed and nothing else changed."""
    monkeypatch.setattr("careloop.policy.engine.resolve_trust", value_only_resolve_trust)
    return default_engine()


class TestTheOldSemanticsAreDetected:
    """Non-vacuity. Remove the semantic binding and these tests must fail.

    A regression suite that passes with the defence disabled was never testing the defence. Each
    method below asserts the *unauthorised* verdict the shipped system produced before the fix, so
    if :func:`careloop.policy.engine.resolve_trust` ever stops consulting ``fact_key`` again, the
    matching test in :class:`TestCrossFieldCollisionsAreRefused` fails and this one starts passing
    for the wrong reason — which is why both halves are here rather than only one.
    """

    def test_the_mutant_really_is_the_old_resolver(self) -> None:
        """It must ignore the key and must otherwise behave identically."""
        case = _case(make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED))

        assert value_only_resolve_trust("document_id", PATIENT, case) is TrustLevel.CONTROLLED
        assert value_only_resolve_trust(None, PATIENT, case) is TrustLevel.CONTROLLED
        assert value_only_resolve_trust("patient_id", 1.5, case) is TrustLevel.UNTRUSTED
        assert value_only_resolve_trust("patient_id", "unseen", case) is TrustLevel.UNTRUSTED

    def test_the_patient_id_collision_reappears(self, value_only_engine: PolicyEngine) -> None:
        case = _case(make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED))

        decision = value_only_engine.evaluate(
            "upload_document", {"portal_id": PORTAL, "document_id": PATIENT}, case
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-upload-to-trusted-portal"

    def test_the_document_id_collision_reappears(self, value_only_engine: PolicyEngine) -> None:
        case = _case(make_fact("document_id", DOCUMENT, TrustLevel.CONTROLLED))

        decision = value_only_engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": DOCUMENT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"

    def test_the_authority_bypass_reappears(self, value_only_engine: PolicyEngine) -> None:
        """The one that matters most: the AUTHORITY guard stops refusing."""
        decision = value_only_engine.evaluate(
            "grant_portal_access",
            {"grantee": CARER, "role": PORTAL, "portal_id": PORTAL},
            _case(),
        )

        assert decision.verdict is not Verdict.DENY
        assert decision.reason_code is not ReasonCode.AUTHORITY_NOT_TRUSTED
        assert decision.arg_trust["role"] is TrustLevel.TRUSTED

    def test_the_email_destination_collision_reappears(
        self, value_only_engine: PolicyEngine
    ) -> None:
        decision = value_only_engine.evaluate(
            "send_email", {"to": PROVIDER, "subject": "s", "body": "b"}, _case()
        )

        assert decision.verdict is not Verdict.DENY
        assert decision.arg_trust["to"] is TrustLevel.TRUSTED

    def test_cross_key_contamination_reappears(self, value_only_engine: PolicyEngine) -> None:
        """The false-denial direction returns too, which is the correctness half of the fix."""
        case = _case(
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            make_fact("patient_id", PROVIDER, TrustLevel.UNTRUSTED),
        )

        decision = value_only_engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": SLOT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.DENY
        assert decision.arg_trust["provider_id"] is TrustLevel.UNTRUSTED

    def test_the_legitimate_paths_are_unaffected_by_the_mutation(
        self, value_only_engine: PolicyEngine
    ) -> None:
        """Bounding the mutant: it must not be denying or allowing everything wholesale.

        The correctly-keyed booking is allowed under both resolvers. If the mutant changed that
        too, the collisions above would not be attributable to the semantic binding alone.
        """
        case = _case(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))

        decision = value_only_engine.evaluate(
            "book_appointment",
            {
                "provider_id": PROVIDER,
                "patient_id": PATIENT,
                "slot_start": SLOT,
                "reason": "cardiology review",
            },
            case,
        )

        assert decision.verdict is Verdict.ALLOW
