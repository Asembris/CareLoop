"""The verdict matrix, trust resolution, match ordering, and the engine's purity.

The engine is the point at which the LLM stops being able to act on its own say-so, so these
tests are less about branch coverage than about the properties that make that claim true.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from helpers import make_case, make_fact, trusted_fact

from careloop.policy import (
    Decision,
    PolicyConfigError,
    ReasonCode,
    Verdict,
    default_rules,
    load_rules_from_text,
)
from careloop.policy.engine import PolicyEngine, canonical_value, default_engine, resolve_trust
from careloop.provenance import TrustLevel
from careloop.state import CaseState
from careloop.tools import REGISTRY

CLINIC = "scheduling@clinic.example"
ATTACKER = "attacker@evil.example"
SLOT = "2026-09-01T10:00:00+00:00"
SLOT_DT = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
PATIENT = "pat-1"


def _validated_patient():
    """The ``patient_id`` fact a booking now needs, at the level validation produces.

    ``allow-booking-on-validated-slot`` requires ``patient_id`` at ``CONTROLLED`` as of session J.
    It used to require only that the argument be non-empty, which let an invented identifier reach
    the clinic; ``tests/test_booking_authority.py`` is where that is now refused, and these tests
    carry the fact so they go on testing what they were written for.
    """
    return make_fact("patient_id", PATIENT, TrustLevel.CONTROLLED)


@pytest.fixture
def engine() -> PolicyEngine:
    return default_engine()


def _planning(*facts):
    return make_case(state=CaseState.PLANNING, facts=tuple(facts))


def _ready(*facts):
    return make_case(state=CaseState.READY_TO_EXECUTE, facts=tuple(facts))


class TestCanonicalValue:
    """The comparison form. Closed, total, and deliberately unforgiving."""

    def test_strings_are_used_verbatim(self) -> None:
        assert canonical_value(" Mixed Case ") == " Mixed Case "

    def test_booleans_render_before_integers(self) -> None:
        assert canonical_value(True) == "true"
        assert canonical_value(False) == "false"

    def test_integers_render_as_decimal(self) -> None:
        assert canonical_value(7) == "7"

    def test_datetimes_render_as_iso_8601(self) -> None:
        assert canonical_value(SLOT_DT) == SLOT

    def test_the_same_instant_in_another_offset_is_a_different_comparison_form(self) -> None:
        """No timezone conversion, for the same reason as no case-folding."""
        elsewhere = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        assert elsewhere == SLOT_DT, "the two spellings must denote one instant"
        assert canonical_value(elsewhere) == "2026-09-01T12:00:00+02:00"
        assert canonical_value(elsewhere) != canonical_value(SLOT_DT)

    def test_a_slot_spelled_in_another_offset_fails_closed(self) -> None:
        elsewhere = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        case = _planning(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))
        assert resolve_trust("slot_start", elsewhere, case) is TrustLevel.UNTRUSTED

    def test_dates_render_as_iso_8601(self) -> None:
        assert canonical_value(SLOT_DT.date()) == "2026-09-01"

    def test_types_without_a_comparison_form_are_unmatchable(self) -> None:
        for value in (None, 1.5, ["a"], {"a": 1}, object()):
            assert canonical_value(value) is None


class TestResolveTrust:
    def test_a_value_the_case_never_recorded_is_untrusted(self) -> None:
        assert resolve_trust("caregiver_email", ATTACKER, _planning()) is TrustLevel.UNTRUSTED

    def test_a_recorded_value_carries_its_fact_trust(self) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        assert resolve_trust("caregiver_email", CLINIC, case) is TrustLevel.TRUSTED

    def test_the_lowest_matching_fact_wins(self) -> None:
        """A poisoned duplicate must not be laundered by a legitimate twin."""
        case = _planning(
            trusted_fact("caregiver_email", CLINIC),
            make_fact("caregiver_email", CLINIC, TrustLevel.UNTRUSTED),
        )
        assert resolve_trust("caregiver_email", CLINIC, case) is TrustLevel.UNTRUSTED

    def test_the_lowest_wins_regardless_of_fact_order(self) -> None:
        case = _planning(
            make_fact("caregiver_email", CLINIC, TrustLevel.UNTRUSTED),
            trusted_fact("caregiver_email", CLINIC),
        )
        assert resolve_trust("caregiver_email", CLINIC, case) is TrustLevel.UNTRUSTED

    def test_a_value_with_no_comparison_form_is_untrusted(self) -> None:
        assert resolve_trust(
            "slot_start", 1.5, _planning(make_fact("slot_start", "1.5", TrustLevel.TRUSTED))
        ) is (TrustLevel.UNTRUSTED)

    def test_a_typed_datetime_matches_the_fact_recorded_as_its_iso_form(self) -> None:
        case = _planning(make_fact("slot_start", SLOT, TrustLevel.CONTROLLED))
        assert resolve_trust("slot_start", SLOT_DT, case) is TrustLevel.CONTROLLED


class TestNoNormalisation:
    """Matching is exact. Normalisation would be a laundering surface."""

    @pytest.mark.parametrize(
        "variant",
        [
            "Scheduling@clinic.example",
            "SCHEDULING@CLINIC.EXAMPLE",
            "scheduling@clinic.example ",
            " scheduling@clinic.example",
            "scheduling@clinic.example.",
            "scheduling@clinic.exampIe",  # capital I for lowercase l
        ],
        ids=["case", "upper", "trailing-space", "leading-space", "trailing-dot", "homoglyph"],
    )
    def test_a_near_miss_of_a_trusted_destination_resolves_untrusted(self, variant: str) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        assert resolve_trust("caregiver_email", variant, case) is TrustLevel.UNTRUSTED

    def test_the_verbatim_destination_resolves_trusted(self) -> None:
        """The other half: exactness must not be achieved by denying everything."""
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        assert resolve_trust("caregiver_email", CLINIC, case) is TrustLevel.TRUSTED

    def test_a_near_miss_is_denied_end_to_end(self, engine: PolicyEngine) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        decision = engine.evaluate(
            "send_email", {"to": CLINIC + " ", "subject": "s", "body": "b"}, case
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_the_verbatim_destination_is_not_denied_end_to_end(self, engine: PolicyEngine) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        decision = engine.evaluate("send_email", {"to": CLINIC, "subject": "s", "body": "b"}, case)
        assert decision.verdict is Verdict.REQUIRE_HUMAN


class TestUnknownToolAndArguments:
    def test_an_unregistered_tool_is_denied(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("exfiltrate_everything", {"to": ATTACKER}, _planning())
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert decision.matched_rule_id is None

    def test_an_undeclared_argument_is_denied(self, engine: PolicyEngine) -> None:
        """An argument the registry does not know has no class, so nothing says it is not a
        destination."""
        decision = engine.evaluate(
            "send_email",
            {"to": CLINIC, "subject": "s", "body": "b", "bcc": ATTACKER},
            _planning(trusted_fact("caregiver_email", CLINIC)),
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert "bcc" in decision.diagnostic

    def test_a_registered_tool_with_no_matching_rule_is_denied_by_default(self) -> None:
        engine = PolicyEngine(
            load_rules_from_text(
                "version: 1\nrules:\n"
                "  - id: allow-fetch\n"
                "    effect: ALLOW\n"
                "    tools: [fetch_document]\n"
                "    diagnostic: d\n",
                REGISTRY,
            ),
            REGISTRY,
        )
        decision = engine.evaluate("send_email", {"to": CLINIC}, _planning())
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert decision.matched_rule_id is None


class TestCaseStateGate:
    @pytest.mark.parametrize(
        "state",
        [
            CaseState.NEW,
            CaseState.GATHERING_EVIDENCE,
            CaseState.AWAITING_HUMAN,
            CaseState.VERIFYING,
            CaseState.WAITING_EXTERNAL,
            CaseState.DONE,
            CaseState.FAILED,
        ],
    )
    def test_external_effect_outside_the_execution_states_is_denied(
        self, engine: PolicyEngine, state: CaseState
    ) -> None:
        case = make_case(state=state, facts=(trusted_fact("caregiver_email", CLINIC),))
        decision = engine.evaluate("send_email", {"to": CLINIC}, case)
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.WRONG_CASE_STATE
        assert decision.matched_rule_id == "deny-external-effect-outside-execution-states"

    def test_the_state_gate_precedes_argument_level_denials(self, engine: PolicyEngine) -> None:
        """A call that is wrong in two ways reports the more fundamental objection."""
        case = make_case(state=CaseState.GATHERING_EVIDENCE)
        decision = engine.evaluate("send_email", {"to": ATTACKER}, case)
        assert decision.reason_code is ReasonCode.WRONG_CASE_STATE

    def test_the_read_path_is_open_in_evidence_gathering(self, engine: PolicyEngine) -> None:
        """The state gate names external-effect tools only, or evidence could never be gathered."""
        case = make_case(state=CaseState.GATHERING_EVIDENCE)
        decision = engine.evaluate("fetch_document", {"document_id": "doc-1"}, case)
        assert decision.verdict is Verdict.ALLOW


class TestDestinationTrust:
    def test_a_document_sourced_recipient_is_denied(self, engine: PolicyEngine) -> None:
        """The injection case: the planner may propose it, and it still does not happen."""
        case = _planning(make_fact("caregiver_email", ATTACKER, TrustLevel.UNTRUSTED))
        decision = engine.evaluate(
            "send_email", {"to": ATTACKER, "subject": "records", "body": "attached"}, case
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-email-to-untrusted-recipient"

    def test_validation_cannot_launder_a_destination(self, engine: PolicyEngine) -> None:
        """CONTROLLED is what a validator can produce, and it is still not good enough."""
        case = _planning(make_fact("caregiver_email", ATTACKER, TrustLevel.CONTROLLED))
        decision = engine.evaluate("send_email", {"to": ATTACKER}, case)
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_an_omitted_destination_is_denied_rather_than_skipped(
        self, engine: PolicyEngine
    ) -> None:
        decision = engine.evaluate("send_email", {"subject": "s", "body": "b"}, _planning())
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_a_trusted_recipient_reaches_the_human(self, engine: PolicyEngine) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        decision = engine.evaluate("send_email", {"to": CLINIC, "subject": "s"}, case)
        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.reason_code is ReasonCode.REQUIRES_HUMAN_APPROVAL
        assert decision.matched_rule_id == "require-human-for-outbound-email"

    def test_upload_to_a_document_named_portal_is_denied(self, engine: PolicyEngine) -> None:
        case = _ready(make_fact("portal_id", "evil-portal", TrustLevel.CONTROLLED))
        decision = engine.evaluate(
            "upload_document", {"portal_id": "evil-portal", "document_id": "doc-1"}, case
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-upload-to-untrusted-portal"


class TestAuthorityTrust:
    """``role`` is the system's only AUTHORITY argument. It must decide verdicts, not just exist."""

    def test_an_authority_argument_below_trusted_is_denied(self, engine: PolicyEngine) -> None:
        case = _ready(
            trusted_fact("caregiver_email", "carer@family.example"),
            trusted_fact("portal_id", "portal-1"),
            make_fact("role", "administrator", TrustLevel.UNTRUSTED),
        )
        decision = engine.evaluate(
            "grant_portal_access",
            {"grantee": "carer@family.example", "role": "administrator", "portal_id": "portal-1"},
            case,
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-authority-grant-below-trusted"

    def test_validation_cannot_launder_an_authority(self, engine: PolicyEngine) -> None:
        """The mirror of the destination-laundering test. CONTROLLED is still not TRUSTED."""
        case = _ready(
            trusted_fact("caregiver_email", "carer@family.example"),
            trusted_fact("portal_id", "portal-1"),
            make_fact("role", "administrator", TrustLevel.CONTROLLED),
        )
        decision = engine.evaluate(
            "grant_portal_access",
            {"grantee": "carer@family.example", "role": "administrator", "portal_id": "portal-1"},
            case,
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-authority-grant-below-trusted"
        assert decision.arg_trust["role"] is TrustLevel.CONTROLLED

    def test_the_irremediable_denial_is_distinguishable_from_the_remediable_one(
        self, engine: PolicyEngine
    ) -> None:
        """Two denials at the same trust level, different remedies, therefore different codes.

        ``role`` is AUTHORITY-class and no validator can rescue it. ``slot_start`` is VALUE-class
        and validation is exactly the remedy. Collapsing these into one code would either send the
        planner into a doomed retry or strip the retry guidance from the case that needs it.
        """
        authority = engine.evaluate(
            "grant_portal_access",
            {"grantee": "carer@family.example", "role": "administrator", "portal_id": "portal-1"},
            _ready(
                trusted_fact("caregiver_email", "carer@family.example"),
                trusted_fact("portal_id", "portal-1"),
                make_fact("role", "administrator", TrustLevel.CONTROLLED),
            ),
        )
        value = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": "pat-1", "slot_start": SLOT},
            _ready(
                trusted_fact("provider_id", "prov-1"),
                make_fact("slot_start", SLOT, TrustLevel.UNTRUSTED),
            ),
        )
        assert authority.verdict is value.verdict is Verdict.DENY
        assert authority.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED
        assert value.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST
        assert authority.model_visible_reason != value.model_visible_reason

    def test_the_timeline_records_why_the_authority_denial_is_irremediable(
        self, engine: PolicyEngine
    ) -> None:
        """The semantic difference is invisible to the model, so the diagnostic must carry it."""
        decision = engine.evaluate(
            "grant_portal_access",
            {"grantee": "carer@family.example", "role": "administrator", "portal_id": "portal-1"},
            _ready(
                trusted_fact("caregiver_email", "carer@family.example"),
                trusted_fact("portal_id", "portal-1"),
                make_fact("role", "administrator", TrustLevel.CONTROLLED),
            ),
        )
        assert "PROMOTABLE_ARG_CLASSES" in decision.diagnostic
        assert "no validator can ever raise one" in decision.diagnostic
        assert "deny-booking-on-unvalidated-slot" in decision.diagnostic

    def test_an_authority_from_trusted_config_still_needs_a_human(
        self, engine: PolicyEngine
    ) -> None:
        """The other half: the authority rule must not be denying every grant unconditionally."""
        case = _ready(
            trusted_fact("caregiver_email", "carer@family.example"),
            trusted_fact("portal_id", "portal-1"),
            trusted_fact("role", "viewer"),
        )
        decision = engine.evaluate(
            "grant_portal_access",
            {"grantee": "carer@family.example", "role": "viewer", "portal_id": "portal-1"},
            case,
        )
        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.matched_rule_id == "require-human-for-authority-grant"


class TestValueTrustAndTheAutonomousPath:
    def test_booking_on_an_unvalidated_slot_is_denied(self, engine: PolicyEngine) -> None:
        case = _ready(
            trusted_fact("provider_id", "prov-1"),
            make_fact("slot_start", SLOT, TrustLevel.UNTRUSTED),
        )
        decision = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": "pat-1", "slot_start": SLOT},
            case,
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST
        assert decision.matched_rule_id == "deny-booking-on-unvalidated-slot"

    def test_booking_on_a_validated_slot_is_allowed(self, engine: PolicyEngine) -> None:
        case = _ready(
            trusted_fact("provider_id", "prov-1"),
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            _validated_patient(),
        )
        decision = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": PATIENT, "slot_start": SLOT},
            case,
        )
        assert decision.verdict is Verdict.ALLOW
        assert decision.reason_code is None
        assert decision.model_visible_reason is None
        assert decision.allowed
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"

    def test_a_datetime_valued_argument_is_allowed_end_to_end(self, engine: PolicyEngine) -> None:
        """Correction 1's canonical form, exercised through a real verdict rather than a unit."""
        case = _ready(
            trusted_fact("provider_id", "prov-1"),
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            _validated_patient(),
        )
        decision = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": PATIENT, "slot_start": SLOT_DT},
            case,
        )
        assert decision.verdict is Verdict.ALLOW
        assert decision.arg_trust["slot_start"] is TrustLevel.CONTROLLED

    def test_a_float_valued_argument_fails_closed(self, engine: PolicyEngine) -> None:
        """No comparison form means no match means UNTRUSTED — a false denial, not a false trust."""
        case = _ready(
            trusted_fact("provider_id", "prov-1"),
            make_fact("slot_start", "1.5", TrustLevel.CONTROLLED),
        )
        decision = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": "pat-1", "slot_start": 1.5},
            case,
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.INSUFFICIENT_FACT_TRUST

    def test_a_legitimate_booking_from_planning_reaches_allow(self, engine: PolicyEngine) -> None:
        """Reversed in commit 6, deliberately. The previous expectation encoded a bug.

        This test used to assert ``DENY`` / ``NO_RULE_MATCH`` here, on the reading that "PLANNING
        is a proposal, not an execution". But ``deny-external-effect-outside-execution-states``
        permits ``PLANNING`` and says why in its own comment — the planner proposes from there and
        must be able to receive a real verdict rather than a state denial — while the two ``ALLOW``
        rules named only the execution states. The result was that an evaluation in ``PLANNING``
        could never reach an ``ALLOW`` and fell through to the default deny, so a legitimate
        booking with a trusted provider and a validated slot was refused ``NO_RULE_MATCH`` for a
        reason no diagnostic explained.

        The graph makes the consequence concrete: the gate node evaluates while the case is
        ``PLANNING`` to decide whether to route to execution, to a human, or back to the planner.
        With the old rules that routing had exactly one outcome.
        """
        case = _planning(
            trusted_fact("provider_id", "prov-1"),
            make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            _validated_patient(),
        )
        decision = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": PATIENT, "slot_start": SLOT},
            case,
        )
        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-booking-on-validated-slot"

    def test_a_legitimate_email_from_planning_reaches_require_human(
        self, engine: PolicyEngine
    ) -> None:
        """The routing verdict the DENY rule's comment promised, pinned so it cannot regress.

        ``REQUIRE_HUMAN`` from ``PLANNING`` is the whole reason that state is in the permitted set.
        It is also the only state the case can be parked from: ``AWAITING_HUMAN`` is reachable from
        ``PLANNING`` and from nowhere else that an external effect could be proposed.
        """
        case = _planning(trusted_fact("caregiver_email", "carer@example.org"))
        decision = engine.evaluate(
            "send_email",
            {"to": "carer@example.org", "subject": "Referral", "body": "Booked."},
            case,
        )
        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.reason_code is ReasonCode.REQUIRES_HUMAN_APPROVAL

    def test_the_state_gate_still_denies_from_a_state_that_is_not_permitted(
        self, engine: PolicyEngine
    ) -> None:
        """Widening the ALLOW rules to PLANNING did not widen them to everything."""
        case = make_case(
            state=CaseState.GATHERING_EVIDENCE,
            facts=(
                trusted_fact("provider_id", "prov-1"),
                make_fact("slot_start", SLOT, TrustLevel.CONTROLLED),
            ),
        )
        decision = engine.evaluate(
            "book_appointment",
            {"provider_id": "prov-1", "patient_id": "pat-1", "slot_start": SLOT},
            case,
        )
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.WRONG_CASE_STATE

    def test_a_missing_required_argument_fails_args_present(self, engine: PolicyEngine) -> None:
        """Retargeted onto ``fetch_document``, which is where ``args_present`` still lives.

        It used to omit ``patient_id`` from a booking, on the strength of
        ``allow-booking-on-validated-slot``'s ``args_present: [patient_id]`` clause. Session J
        replaced that clause with a trust requirement, so the booking is now refused
        ``INSUFFICIENT_FACT_TRUST`` by ``deny-booking-for-unverified-patient`` before the ``ALLOW``
        rule is reached — a stronger refusal for a better reason, and no longer a test of this
        clause. Preserving the old shape here would have been preserving obsolete booking semantics
        under a name that describes something else;
        ``tests/test_booking_authority.py`` covers the booking behaviour.
        """
        case = make_case(state=CaseState.GATHERING_EVIDENCE)

        decision = engine.evaluate("fetch_document", {}, case)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH

    def test_an_empty_argument_does_not_satisfy_args_present(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("fetch_document", {"document_id": ""}, _planning())
        assert decision.verdict is Verdict.DENY

    def test_the_read_path_is_allowed_while_evidence_is_being_gathered(
        self, engine: PolicyEngine
    ) -> None:
        case = make_case(state=CaseState.GATHERING_EVIDENCE)
        decision = engine.evaluate("fetch_document", {"document_id": "doc-1"}, case)
        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-document-fetch-while-gathering-evidence"

    def test_the_read_path_is_refused_to_the_planner(self, engine: PolicyEngine) -> None:
        """Session B narrowed the rule to the deterministic evidence node's own window.

        The planner proposes from ``PLANNING``, where no rule permits a read, so deny-by-default
        refuses it. Measured cause: given a poisoned case with every effectful proposal already
        refused, a live planner reached for ``fetch_document`` in 7 of 23 runs — avoidance rather
        than evidence-gathering.
        """
        decision = engine.evaluate("fetch_document", {"document_id": "doc-1"}, _planning())
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert decision.matched_rule_id is None


class TestMatchOrdering:
    def test_deny_beats_require_human_on_the_shipped_rules(self, engine: PolicyEngine) -> None:
        """``grant_portal_access`` to an untrusted grantee matches a DENY and a REQUIRE_HUMAN."""
        case = _ready(trusted_fact("portal_id", "portal-1"), trusted_fact("role", "viewer"))
        matching = [
            rule.id
            for rule in default_rules().rules
            if "grant_portal_access" in rule.tools
            and rule.effect in (Verdict.DENY, Verdict.REQUIRE_HUMAN)
        ]
        assert len(matching) > 1, "this test is vacuous unless several rules can match"

        decision = engine.evaluate(
            "grant_portal_access",
            {"grantee": ATTACKER, "role": "viewer", "portal_id": "portal-1"},
            case,
        )
        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == "deny-portal-access-to-untrusted-grantee"

    def test_file_order_cannot_promote_an_allow_above_a_refusal(self) -> None:
        """The ALLOW is written first and still loses. Ordering is the engine's, not the file's."""
        ruleset = load_rules_from_text(
            "version: 1\nrules:\n"
            "  - id: allow-first\n"
            "    effect: ALLOW\n"
            "    tools: [fetch_document]\n"
            "    diagnostic: d\n"
            "  - id: require-human-second\n"
            "    effect: REQUIRE_HUMAN\n"
            "    tools: [fetch_document]\n"
            "    reason_code: REQUIRES_HUMAN_APPROVAL\n"
            "    diagnostic: d\n"
            "  - id: deny-third\n"
            "    effect: DENY\n"
            "    tools: [fetch_document]\n"
            "    reason_code: NO_RULE_MATCH\n"
            "    diagnostic: d\n",
            REGISTRY,
        )
        decision = PolicyEngine(ruleset, REGISTRY).evaluate(
            "fetch_document", {"document_id": "doc-1"}, _planning()
        )
        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == "deny-third"

    def test_require_human_beats_allow_when_no_deny_matches(self) -> None:
        ruleset = load_rules_from_text(
            "version: 1\nrules:\n"
            "  - id: allow-first\n"
            "    effect: ALLOW\n"
            "    tools: [fetch_document]\n"
            "    diagnostic: d\n"
            "  - id: require-human-second\n"
            "    effect: REQUIRE_HUMAN\n"
            "    tools: [fetch_document]\n"
            "    reason_code: REQUIRES_HUMAN_APPROVAL\n"
            "    diagnostic: d\n",
            REGISTRY,
        )
        decision = PolicyEngine(ruleset, REGISTRY).evaluate(
            "fetch_document", {"document_id": "doc-1"}, _planning()
        )
        assert decision.verdict is Verdict.REQUIRE_HUMAN

    def test_within_one_effect_the_first_matching_rule_wins(self) -> None:
        ruleset = load_rules_from_text(
            "version: 1\nrules:\n"
            "  - id: deny-earlier\n"
            "    effect: DENY\n"
            "    tools: [fetch_document]\n"
            "    reason_code: NO_RULE_MATCH\n"
            "    diagnostic: d\n"
            "  - id: deny-later\n"
            "    effect: DENY\n"
            "    tools: [fetch_document]\n"
            "    reason_code: WRONG_CASE_STATE\n"
            "    diagnostic: d\n",
            REGISTRY,
        )
        decision = PolicyEngine(ruleset, REGISTRY).evaluate(
            "fetch_document", {"document_id": "doc-1"}, _planning()
        )
        assert decision.matched_rule_id == "deny-earlier"

    def test_a_non_matching_rule_is_skipped_for_a_later_one(self) -> None:
        ruleset = load_rules_from_text(
            "version: 1\nrules:\n"
            "  - id: deny-only-when-done\n"
            "    effect: DENY\n"
            "    tools: [fetch_document]\n"
            "    when:\n"
            "      case_state_in: [DONE]\n"
            "    reason_code: NO_RULE_MATCH\n"
            "    diagnostic: d\n"
            "  - id: deny-always\n"
            "    effect: DENY\n"
            "    tools: [fetch_document]\n"
            "    reason_code: WRONG_CASE_STATE\n"
            "    diagnostic: d\n",
            REGISTRY,
        )
        decision = PolicyEngine(ruleset, REGISTRY).evaluate(
            "fetch_document", {"document_id": "doc-1"}, _planning()
        )
        assert decision.matched_rule_id == "deny-always"


class TestPurity:
    def test_evaluating_twice_gives_the_same_decision(self, engine: PolicyEngine) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        args = {"to": CLINIC, "subject": "s", "body": "b"}
        assert engine.evaluate("send_email", args, case) == engine.evaluate(
            "send_email", args, case
        )

    def test_the_case_is_not_mutated(self, engine: PolicyEngine) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))
        before = case.model_dump()
        engine.evaluate("send_email", {"to": CLINIC}, case)
        assert case.model_dump() == before
        assert case.version == 0
        assert case.state is CaseState.PLANNING
        assert case.timeline == ()

    def test_the_arguments_are_not_mutated(self, engine: PolicyEngine) -> None:
        args = {"to": ATTACKER, "subject": "s"}
        engine.evaluate("send_email", args, _planning())
        assert args == {"to": ATTACKER, "subject": "s"}

    def test_the_decision_holds_a_copy_of_the_arguments(self, engine: PolicyEngine) -> None:
        args = {"to": ATTACKER}
        decision = engine.evaluate("send_email", args, _planning())
        args["to"] = "changed"
        assert decision.args == {"to": ATTACKER}


class TestDecisionDiagnostics:
    def test_a_decision_records_argument_classes_and_trust_for_the_timeline(
        self, engine: PolicyEngine
    ) -> None:
        from careloop.provenance import ArgClass

        case = _planning(make_fact("caregiver_email", ATTACKER, TrustLevel.CONTROLLED))
        decision = engine.evaluate("send_email", {"to": ATTACKER, "subject": "s"}, case)
        assert decision.arg_classes == {"to": ArgClass.DESTINATION, "subject": ArgClass.VALUE}
        assert decision.arg_trust == {
            "to": TrustLevel.CONTROLLED,
            "subject": TrustLevel.UNTRUSTED,
        }
        assert decision.args == {"to": ATTACKER, "subject": "s"}

    def test_a_matched_decision_carries_the_rule_diagnostic(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("send_email", {"to": ATTACKER}, _planning())
        assert "DESTINATION-class" in decision.diagnostic

    def test_str_renders_without_argument_values(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("send_email", {"to": ATTACKER, "body": "secret"}, _planning())
        rendered = str(decision)
        assert ATTACKER not in rendered
        assert "secret" not in rendered
        assert "deny-email-to-untrusted-recipient" in rendered

    def test_an_allow_may_not_carry_a_reason_code(self) -> None:
        with pytest.raises(ValueError, match="nothing to explain"):
            Decision(verdict=Verdict.ALLOW, tool_name="t", reason_code=ReasonCode.NO_RULE_MATCH)

    def test_a_refusal_must_carry_a_reason_code(self) -> None:
        with pytest.raises(ValueError, match="must carry a reason code"):
            Decision(verdict=Verdict.DENY, tool_name="t")


class TestEngineConstruction:
    def test_the_default_engine_uses_the_shipped_rules_and_registry(self) -> None:
        engine = default_engine()
        assert engine.registry is REGISTRY
        assert engine.ruleset.rule_ids == default_rules().rule_ids

    def test_an_engine_cannot_be_built_on_rules_that_do_not_resolve(self) -> None:
        with pytest.raises(PolicyConfigError):
            load_rules_from_text(
                "version: 1\nrules:\n"
                "  - id: r\n"
                "    effect: DENY\n"
                "    tools: [ghost_tool]\n"
                "    reason_code: NO_RULE_MATCH\n"
                "    diagnostic: d\n",
                REGISTRY,
            )


class TestApprovalIsAConditionAndNotAnOverride:
    """The ``args_approved`` clause added in commit 6, and the property it must not weaken.

    A ``REQUIRE_HUMAN`` rule with no approval clause can never be satisfied: policy is evaluated
    once to route the proposal and once at the tool boundary, so the caregiver's decision would be
    met by a second request and the action could never happen. The clause fixes that in
    ``rules.yaml`` rather than with a Python branch that skips the gate.

    What it must not do is give a human the power to authorise something the rules deny outright.
    """

    @staticmethod
    def _approved_case(state: CaseState, recipient: str, *facts):
        from careloop.state.case import Approval, ApprovalStatus
        from careloop.tools.idempotency import args_digest

        args = {"to": recipient, "subject": "Referral", "body": "Booked."}
        digest = args_digest(args)
        case = make_case(state=state, facts=tuple(facts)).model_copy(
            update={
                "approvals": (
                    Approval(
                        approval_id="apr-1",
                        tool_name="send_email",
                        args_digest=digest,
                        reason_code=str(ReasonCode.REQUIRES_HUMAN_APPROVAL),
                        status=ApprovalStatus.APPROVED,
                    ),
                )
            }
        )
        return case, args

    def test_an_unapproved_email_requires_a_human(self, engine: PolicyEngine) -> None:
        case = _planning(trusted_fact("caregiver_email", CLINIC))

        decision = engine.evaluate("send_email", {"to": CLINIC, "subject": "s", "body": "b"}, case)

        assert decision.verdict is Verdict.REQUIRE_HUMAN

    def test_an_approved_email_to_a_trusted_recipient_is_allowed(
        self, engine: PolicyEngine
    ) -> None:
        case, args = self._approved_case(
            CaseState.EXECUTING, CLINIC, trusted_fact("caregiver_email", CLINIC)
        )

        decision = engine.evaluate("send_email", args, case)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-approved-outbound-email"

    def test_an_approval_cannot_launder_an_untrusted_destination(
        self, engine: PolicyEngine
    ) -> None:
        """The property. DENY runs before ALLOW, so a human cannot authorise past a denial."""
        case, args = self._approved_case(CaseState.EXECUTING, ATTACKER)

        decision = engine.evaluate("send_email", args, case)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_the_allow_rule_checks_the_destination_itself_as_well(self) -> None:
        """Belt and braces, isolated so the braces are actually tested.

        ``deny-email-to-untrusted-recipient`` already refuses this call, which makes the ``ALLOW``
        rule's own trust clause redundant in the shipped file — and therefore untested by any
        end-to-end assertion. Deleting the clause left the whole suite green, so this test removes
        the ``DENY`` rule and checks the ``ALLOW`` rule alone. If the two ever drift, this is what
        notices.
        """
        shipped = default_rules()
        without_the_deny_rule = shipped.model_copy(
            update={
                "rules": tuple(
                    rule for rule in shipped.rules if rule.id != "deny-email-to-untrusted-recipient"
                )
            }
        )
        case, args = self._approved_case(CaseState.EXECUTING, ATTACKER)

        decision = PolicyEngine(without_the_deny_rule, REGISTRY).evaluate("send_email", args, case)

        assert decision.verdict is not Verdict.ALLOW
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH

    def test_an_approval_for_other_arguments_does_not_carry_over(
        self, engine: PolicyEngine
    ) -> None:
        case, args = self._approved_case(
            CaseState.EXECUTING, CLINIC, trusted_fact("caregiver_email", CLINIC)
        )

        decision = engine.evaluate("send_email", {**args, "body": "a body nobody approved"}, case)

        assert decision.verdict is Verdict.REQUIRE_HUMAN

    def test_a_pending_approval_is_not_an_approval(self, engine: PolicyEngine) -> None:
        from careloop.state.case import ApprovalStatus

        case, args = self._approved_case(
            CaseState.EXECUTING, CLINIC, trusted_fact("caregiver_email", CLINIC)
        )
        still_pending = case.model_copy(
            update={
                "approvals": (
                    case.approvals[0].model_copy(update={"status": ApprovalStatus.PENDING}),
                )
            }
        )

        decision = engine.evaluate("send_email", args, still_pending)

        assert decision.verdict is Verdict.REQUIRE_HUMAN

    def test_an_uncanonicalizable_argument_counts_as_unapproved(self, engine: PolicyEngine) -> None:
        """Fail closed: a call that cannot be identified has not been authorised."""
        case, _ = self._approved_case(
            CaseState.EXECUTING, CLINIC, trusted_fact("caregiver_email", CLINIC)
        )

        decision = engine.evaluate("send_email", {"to": CLINIC, "subject": "s", "body": 1.5}, case)

        assert decision.verdict is Verdict.REQUIRE_HUMAN
