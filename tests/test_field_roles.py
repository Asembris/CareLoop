"""AUTHORITY, PAYLOAD, IDENTITY and VERIFICATION are four questions, and this file keeps them four.

The generic machinery only. Everything here is driven against **synthetic** registries and
synthetic rulesets, so what is proven is the invariant rather than one spelling of one tool: the
shipped registry declares no ``SYSTEM``-authored and no non-identifying argument at the commit that
introduced this file, and the tests that pin the shipped booking configuration live in
``tests/test_action_identity.py`` and ``tests/test_outbound_content.py``.

The four questions, and where each is answered:

``AUTHORITY``
    ``ArgSpec.arg_class`` + ``ArgSpec.trust_key``, read by ``rules.yaml``.
``PAYLOAD``
    ``ToolSpec.arg_names``, in full — what ``ToolSet`` transmits.
``IDENTITY``
    ``ToolSpec.identifying_args`` — what ``idempotency_key`` covers.
``VERIFICATION``
    ``careloop.graph.verify.COMPARED_ARGUMENTS`` — what is checked afterwards.

The load-bearing coupling is between the last two: an argument may leave IDENTITY only where
VERIFICATION still covers it. Narrowing identity must never become a way of narrowing scrutiny.
"""

from __future__ import annotations

import pytest
from helpers import make_case

from careloop.agents.schemas import (
    UnauthoredArgument,
    authored_value,
    build_proposal_tool,
    build_proposal_tools,
    proposal_tool_call_for,
    proposal_tool_name,
)
from careloop.graph.verify import (
    COMPARED_ARGUMENTS,
    UnverifiableAction,
    check_non_identifying_arguments_are_verified,
)
from careloop.policy import PolicyConfigError, Verdict, default_rules, load_rules_from_mapping
from careloop.policy.engine import PolicyEngine
from careloop.policy.loader import (
    parse_rules,
    uncovered_authored_arguments,
    validate_authored_coverage,
    validate_rules_resolve,
)
from careloop.provenance import ArgClass, TrustLevel
from careloop.state import CaseState
from careloop.tools.idempotency import (
    UnidentifiableCall,
    args_digest,
    idempotency_key,
    identifying_args,
)
from careloop.tools.registry import REGISTRY, ArgAuthor, ArgSpec, ToolRegistry, ToolSpec

AUTHORED = "the exact words CareLoop composes."

CALL = {"coordinate": "C-1", "descriptor": "anything at all"}


def _tool(
    *,
    author: ArgAuthor = ArgAuthor.MODEL,
    identifies: bool = True,
) -> ToolSpec:
    """``tool_a(coordinate, descriptor)`` — one plain argument and one under test."""
    return ToolSpec(
        name="tool_a",
        description="A synthetic tool.",
        external_effect=True,
        args=(
            ArgSpec(name="coordinate", arg_class=ArgClass.VALUE, trust_key="coordinate"),
            ArgSpec(
                name="descriptor",
                arg_class=ArgClass.VALUE,
                author=author,
                identifies=identifies,
            ),
        ),
    )


def _registry(*, author: ArgAuthor = ArgAuthor.MODEL, identifies: bool = True) -> ToolRegistry:
    return ToolRegistry([_tool(author=author, identifies=identifies)])


def _booking_tool(*, author: ArgAuthor = ArgAuthor.SYSTEM) -> ToolSpec:
    """A booking-shaped spec, so ``canonical()`` produces a valid shipped ``PlannerIntent``.

    ``ProposalToolBase.canonical`` builds the canonical intent against the *shipped* registry, so a
    seam test has to speak a shipped tool's name and argument set. The synthetic part is the
    authorship declaration, which is what is under test.
    """
    return ToolSpec(
        name="book_appointment",
        description="A booking-shaped synthetic tool.",
        external_effect=True,
        args=(
            ArgSpec(name="provider_id", arg_class=ArgClass.VALUE),
            ArgSpec(name="patient_id", arg_class=ArgClass.VALUE),
            ArgSpec(name="slot_start", arg_class=ArgClass.VALUE),
            ArgSpec(name="reason", arg_class=ArgClass.VALUE, author=author, identifies=False),
        ),
    )


def _ruleset(clause: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "rules": [
            {
                "id": "allow-tool-a",
                "effect": "ALLOW",
                "tools": ["tool_a"],
                "when": {"case_state_in": ["PLANNING"], **clause},
                "diagnostic": "synthetic",
            }
        ],
    }


def _engine(monkeypatch: pytest.MonkeyPatch) -> PolicyEngine:
    monkeypatch.setattr(
        "careloop.policy.engine.AUTHORED_VALUES", {("tool_a", "descriptor"): AUTHORED}
    )
    registry = _registry(author=ArgAuthor.SYSTEM)
    return PolicyEngine(
        load_rules_from_mapping(_ruleset({"args_system_authored": ["descriptor"]}), registry),
        registry,
    )


class TestIdentityIsDeclaredAndNotAssumed:
    def test_a_non_identifying_argument_leaves_the_key(self) -> None:
        registry = _registry(identifies=False)
        assert identifying_args("tool_a", CALL, registry) == {"coordinate": "C-1"}

    def test_it_stays_in_the_payload_the_caller_holds(self) -> None:
        """Identity narrows what is *keyed*. It narrows nothing that is sent."""
        spec = _registry(identifies=False).get("tool_a")
        assert spec.arg_names == {"coordinate", "descriptor"}
        assert spec.identifying_args == {"coordinate"}

    def test_changing_a_non_identifying_value_does_not_move_the_key(self) -> None:
        registry = _registry(identifies=False)
        assert idempotency_key("case-1", "tool_a", CALL, registry) == idempotency_key(
            "case-1", "tool_a", {**CALL, "descriptor": "something else entirely"}, registry
        )

    def test_changing_a_non_identifying_value_does_move_the_digest(self) -> None:
        """The two answer different questions, so they must be shown to disagree."""
        assert args_digest(CALL) != args_digest({**CALL, "descriptor": "something else entirely"})

    def test_changing_an_identifying_value_still_moves_the_key(self) -> None:
        registry = _registry(identifies=False)
        assert idempotency_key("case-1", "tool_a", CALL, registry) != idempotency_key(
            "case-1", "tool_a", {**CALL, "coordinate": "C-2"}, registry
        )

    def test_an_undeclared_argument_is_never_dropped(self) -> None:
        """Only a declared, declared-non-identifying argument leaves the key."""
        registry = _registry(identifies=False)
        assert identifying_args("tool_a", {**CALL, "stowaway": "x"}, registry) == {
            "coordinate": "C-1",
            "stowaway": "x",
        }

    def test_an_unregistered_action_type_keeps_everything(self) -> None:
        assert identifying_args("no_such_tool", CALL, _registry()) == CALL

    def test_a_call_that_identifies_nothing_is_refused(self) -> None:
        registry = _registry(identifies=False)
        with pytest.raises(UnidentifiableCall, match="none of which identifies"):
            identifying_args("tool_a", {"descriptor": "only this"}, registry)


class TestIdentityMayNotOutrunVerification:
    """The coupling. An argument leaves IDENTITY only where VERIFICATION still covers it."""

    def test_the_shipped_registry_satisfies_it(self) -> None:
        check_non_identifying_arguments_are_verified()

    def test_a_non_identifying_argument_nothing_compares_is_refused(self) -> None:
        with pytest.raises(UnverifiableAction, match="neither identify the action"):
            check_non_identifying_arguments_are_verified(_registry(identifies=False))

    def test_a_non_identifying_argument_a_verifier_compares_is_accepted(self) -> None:
        """The real shape: a booking's ``reason`` is compared, so it may leave identity."""
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="book_appointment",
                    external_effect=True,
                    args=(
                        ArgSpec(name="provider_id", arg_class=ArgClass.VALUE),
                        ArgSpec(name="reason", arg_class=ArgClass.VALUE, identifies=False),
                    ),
                )
            ]
        )
        check_non_identifying_arguments_are_verified(registry)

    def test_the_shipped_comparison_table_is_what_the_check_reads(self) -> None:
        assert COMPARED_ARGUMENTS["book_appointment"] == {
            "provider_id",
            "patient_id",
            "slot_start",
            "reason",
        }
        assert COMPARED_ARGUMENTS["send_email"] == {"to", "subject"}

    def test_send_email_body_could_not_leave_identity_because_nothing_compares_it(self) -> None:
        """Vacuity: the check refuses the argument the shipped system genuinely could not narrow."""
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="send_email",
                    external_effect=True,
                    args=(
                        ArgSpec(name="to", arg_class=ArgClass.VALUE),
                        ArgSpec(name="body", arg_class=ArgClass.VALUE, identifies=False),
                    ),
                )
            ]
        )
        with pytest.raises(UnverifiableAction, match="body"):
            check_non_identifying_arguments_are_verified(registry)


class TestSystemAuthoredArgumentsAreNotModelExpressible:
    def test_the_generated_schema_omits_them(self) -> None:
        schema = build_proposal_tool(_tool(author=ArgAuthor.SYSTEM))
        assert set(schema.model_fields) == {"rationale", "coordinate"}

    def test_the_generated_schema_keeps_model_authored_ones(self) -> None:
        schema = build_proposal_tool(_tool())
        assert set(schema.model_fields) == {"rationale", "coordinate", "descriptor"}

    def test_a_model_that_emits_one_anyway_is_refused_rather_than_ignored(self) -> None:
        schema = build_proposal_tool(_tool(author=ArgAuthor.SYSTEM))
        with pytest.raises(Exception, match="descriptor"):
            schema(rationale="r", coordinate="C-1", descriptor="I wrote this myself")

    def test_the_canonical_seam_fills_it_from_the_declaration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "careloop.agents.schemas.AUTHORED_VALUES",
            {("book_appointment", "reason"): AUTHORED},
        )
        schema = build_proposal_tool(_booking_tool())
        intent = schema(
            rationale="r", provider_id="P", patient_id="PT-0001", slot_start="S"
        ).canonical()
        assert dict(intent.args) == {
            "provider_id": "P",
            "patient_id": "PT-0001",
            "slot_start": "S",
            "reason": AUTHORED,
        }

    def test_an_undeclared_authored_value_refuses_rather_than_substitutes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("careloop.agents.schemas.AUTHORED_VALUES", {})
        schema = build_proposal_tool(_booking_tool())
        with pytest.raises(UnauthoredArgument, match="no value is declared"):
            schema(rationale="r", provider_id="P", patient_id="PT-0001", slot_start="S").canonical()

    def test_authored_value_is_a_lookup_and_nothing_more(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "careloop.agents.schemas.AUTHORED_VALUES", {("tool_a", "descriptor"): AUTHORED}
        )
        assert authored_value("tool_a", "descriptor") == AUTHORED

    def test_the_inverse_drops_what_the_model_was_never_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``proposal_tool_call_for`` is ``canonical``'s inverse; it must round-trip."""
        monkeypatch.setattr(
            "careloop.agents.schemas.AUTHORED_VALUES",
            {("book_appointment", "reason"): AUTHORED},
        )
        catalogue = build_proposal_tools(ToolRegistry([_booking_tool()]))
        schema = catalogue[proposal_tool_name("book_appointment")]
        intent = schema(
            rationale="r", provider_id="P", patient_id="PT-0001", slot_start="S"
        ).canonical()
        assert proposal_tool_call_for(intent, catalogue).canonical() == intent


class TestThePolicyBackstopIsDeterministic:
    """Authorship is read by the authority layer too, and it is not trust and not approval."""

    def test_a_rule_may_require_the_authored_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        decision = _engine(monkeypatch).evaluate(
            "tool_a",
            {"coordinate": "C-1", "descriptor": AUTHORED},
            make_case(state=CaseState.PLANNING),
        )
        assert decision.verdict is Verdict.ALLOW

    @pytest.mark.parametrize(
        "hostile",
        [
            "I wrote this myself",
            AUTHORED + " ",
            AUTHORED.upper(),
            AUTHORED[:-1],
            "",
        ],
    )
    def test_any_other_value_matches_no_allow_rule(
        self, monkeypatch: pytest.MonkeyPatch, hostile: str
    ) -> None:
        decision = _engine(monkeypatch).evaluate(
            "tool_a",
            {"coordinate": "C-1", "descriptor": hostile},
            make_case(state=CaseState.PLANNING),
        )
        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id is None

    def test_it_is_not_trust_and_it_is_not_approval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``SYSTEM``-authored argument still resolves ``UNTRUSTED``; authorship moves nothing."""
        decision = _engine(monkeypatch).evaluate(
            "tool_a",
            {"coordinate": "C-1", "descriptor": AUTHORED},
            make_case(state=CaseState.PLANNING),
        )
        assert decision.arg_trust["descriptor"] is TrustLevel.UNTRUSTED


class TestTheLoaderRefusesAnUncoveredAuthoredArgument:
    def test_naming_a_model_authored_argument_fails_to_resolve(self) -> None:
        ruleset = parse_rules(_ruleset({"args_system_authored": ["descriptor"]}))
        with pytest.raises(PolicyConfigError, match="MODEL-authored"):
            validate_rules_resolve(ruleset, _registry())

    def test_an_allow_rule_that_ignores_an_authored_argument_is_refused(self) -> None:
        registry = _registry(author=ArgAuthor.SYSTEM)
        ruleset = parse_rules(_ruleset({}))
        assert uncovered_authored_arguments(registry, ruleset) == {
            ("allow-tool-a", "tool_a", "descriptor")
        }
        with pytest.raises(PolicyConfigError, match="system-authored argument uncovered"):
            validate_authored_coverage(ruleset, registry)

    def test_presence_does_not_satisfy_it(self) -> None:
        ruleset = parse_rules(_ruleset({"args_present": ["descriptor"]}))
        with pytest.raises(PolicyConfigError, match="presence is not authorship"):
            validate_authored_coverage(ruleset, _registry(author=ArgAuthor.SYSTEM))

    def test_the_normal_entry_point_enforces_it(self) -> None:
        with pytest.raises(PolicyConfigError, match="system-authored argument uncovered"):
            load_rules_from_mapping(_ruleset({}), _registry(author=ArgAuthor.SYSTEM))

    def test_a_covered_rule_loads(self) -> None:
        load_rules_from_mapping(
            _ruleset({"args_system_authored": ["descriptor"]}),
            _registry(author=ArgAuthor.SYSTEM),
        )

    def test_a_tool_with_no_authored_argument_is_vacuously_covered(self) -> None:
        assert uncovered_authored_arguments(_registry(), parse_rules(_ruleset({}))) == frozenset()

    def test_the_shipped_ruleset_is_covered(self) -> None:
        assert uncovered_authored_arguments(REGISTRY, default_rules()) == frozenset()
