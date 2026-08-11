"""What the agents are allowed to say, and what they structurally cannot.

Nothing here measures model quality. That was the benchmark's job — see
``docs/model-benchmark.md`` — and repeating it against a scripted mock would prove nothing.
"""

from __future__ import annotations

from inspect import signature

import pytest
from pydantic import BaseModel, ValidationError

from careloop.agents.schemas import (
    ARG_NAMES,
    INTENT_FIELDS,
    NOT_SUPPLIED,
    TOOL_NAMES,
    EmptyRegistry,
    PlannerIntent,
    PlannerIntentBase,
    ReservedArgumentName,
    TriageCategory,
    TriageResult,
    Urgency,
    argument_names,
    build_planner_intent,
    tool_names,
)
from careloop.provenance.trust import ArgClass
from careloop.tools.callables import ToolSet
from careloop.tools.registry import REGISTRY, ArgSpec, ToolRegistry, ToolSpec

ATTACKER = "records-intake@claims-continuity-partners.example"


def intent(tool_name: str, **args: str) -> PlannerIntentBase:
    """Build an intent, leaving every argument the caller did not name empty."""
    fields = dict.fromkeys(ARG_NAMES, NOT_SUPPLIED)
    fields.update(args)
    return PlannerIntent(tool_name=tool_name, rationale="because", **fields)


def synthetic_registry() -> ToolRegistry:
    """The shipped tools plus one the shipped registry does not declare."""
    return ToolRegistry(
        [
            *REGISTRY,
            ToolSpec(
                name="wire_transfer",
                description="Move money.",
                external_effect=True,
                args=(
                    ArgSpec(name="beneficiary", arg_class=ArgClass.DESTINATION),
                    ArgSpec(name="amount", arg_class=ArgClass.VALUE),
                ),
            ),
        ]
    )


class TestTriageResult:
    def test_accepts_a_well_formed_classification(self):
        result = TriageResult(
            category=TriageCategory.APPOINTMENT,
            urgency=Urgency.SOON,
            summary="Book the cardiology consultation.",
            patient_id="PT-4417",
        )
        assert result.category is TriageCategory.APPOINTMENT
        assert result.patient_id == "PT-4417"

    def test_the_empty_string_means_the_source_did_not_state_it(self):
        result = TriageResult(
            category=TriageCategory.OTHER,
            urgency=Urgency.ROUTINE,
            summary="Unclear.",
            patient_id=NOT_SUPPLIED,
        )
        assert result.patient_id == ""

    def test_a_category_outside_the_enum_is_rejected(self):
        with pytest.raises(ValidationError):
            TriageResult(
                category="WIRE_TRANSFER", urgency=Urgency.URGENT, summary="s", patient_id="p"
            )

    def test_an_urgency_outside_the_enum_is_rejected(self):
        with pytest.raises(ValidationError):
            TriageResult(
                category=TriageCategory.BILLING, urgency="IMMEDIATELY", summary="s", patient_id="p"
            )

    def test_an_invented_field_is_rejected_rather_than_dropped(self):
        with pytest.raises(ValidationError):
            TriageResult(
                category=TriageCategory.RECORDS,
                urgency=Urgency.ROUTINE,
                summary="s",
                patient_id="p",
                escalate_to="attacker@evil.example",
            )

    def test_is_frozen(self):
        result = TriageResult(
            category=TriageCategory.RECORDS, urgency=Urgency.ROUTINE, summary="s", patient_id="p"
        )
        with pytest.raises(ValidationError):
            result.patient_id = "PT-0000"


class TestTriageCannotNameADestination:
    """Invariant 3, reached through the engine's lowest-wins trust resolution.

    Trust resolves by looking an argument's *value* up in ``case.facts`` and taking the lowest
    level among the matches. A triage-emitted ``UNTRUSTED`` fact holding the same string as a
    ``TRUSTED`` configuration fact would therefore pull the legitimate value down and deny the
    action it was needed for. Triage emits ``VALUE``-class candidates only.
    """

    def test_no_field_of_triage_result_is_a_destination_or_authority_argument(self):
        classified = {
            arg.name: arg.arg_class
            for tool in REGISTRY
            for arg in tool.args
            if arg.arg_class is not ArgClass.VALUE
        }
        offending = sorted(set(TriageResult.model_fields) & set(classified))
        assert offending == [], (
            f"TriageResult names {offending}, which the registry classifies as "
            f"{[classified[name] for name in offending]}"
        )

    def test_the_check_would_notice_a_destination_field(self):
        """Vacuity: the assertion above must fail against a schema that does name one."""

        class LeakyTriageResult(BaseModel):
            summary: str
            provider_id: str

        classified = {
            arg.name: arg.arg_class
            for tool in REGISTRY
            for arg in tool.args
            if arg.arg_class is not ArgClass.VALUE
        }
        assert sorted(set(LeakyTriageResult.model_fields) & set(classified)) == ["provider_id"]

    def test_the_registry_really_does_classify_some_arguments_as_destinations(self):
        """And that the check above is not passing over an empty set."""
        destinations = {
            arg.name
            for tool in REGISTRY
            for arg in tool.args
            if arg.arg_class is ArgClass.DESTINATION
        }
        assert destinations


class TestPlannerIntentIsAProposalAndNothingMore:
    def test_the_field_set_is_exactly_the_intent_fields_plus_the_registry_arguments(self):
        assert set(PlannerIntent.model_fields) == set(INTENT_FIELDS) | set(ARG_NAMES)

    def test_no_field_can_express_execution_approval_state_or_priority(self):
        forbidden = (
            "execute",
            "approve",
            "approved",
            "confirm",
            "confirmed",
            "authorise",
            "authorize",
            "authority",
            "override",
            "force",
            "bypass",
            "urgent",
            "priority",
            "state",
            "verdict",
            "allow",
            "deny",
            "trust",
        )
        offending = sorted(
            name
            for name in PlannerIntent.model_fields
            if any(word in name.lower() for word in forbidden)
        )
        assert offending == []

    def test_the_forbidden_word_check_is_not_vacuous(self):
        """Vacuity: it must fire against a schema carrying the field it exists to forbid."""

        class EagerIntent(BaseModel):
            tool_name: str
            approved: bool

        forbidden = ("approve", "execute")
        offending = sorted(
            name
            for name in EagerIntent.model_fields
            if any(word in name.lower() for word in forbidden)
        )
        assert offending == ["approved"]

    def test_is_frozen(self):
        proposal = intent("fetch_document", document_id="doc-0001-referral")
        with pytest.raises(ValidationError):
            proposal.tool_name = "send_email"

    def test_an_invented_field_is_rejected_rather_than_dropped(self):
        with pytest.raises(ValidationError):
            intent("fetch_document", document_id="doc-0001-referral", execute_now="true")


class TestPlannerIntentRejectsWhatTheRegistryDoesNotDeclare:
    def test_an_unregistered_tool_is_rejected_at_the_schema_boundary(self):
        with pytest.raises(ValidationError):
            intent("wire_transfer")

    def test_an_undeclared_argument_is_rejected_at_the_construction_boundary(self):
        with pytest.raises(ValidationError):
            intent("fetch_document", document_id="doc-0001-referral", to=ATTACKER)

    def test_the_rejection_message_names_fields_and_never_their_values(self):
        """The validator's own message is model-safe; see the note in ``schemas.py``."""
        with pytest.raises(ValidationError) as caught:
            intent("fetch_document", document_id="doc-0001-referral", to=ATTACKER)
        messages = [error["msg"] for error in caught.value.errors()]
        assert messages
        for message in messages:
            assert ATTACKER not in message
            assert "to" in message

    def test_an_empty_undeclared_argument_is_fine_because_it_supplies_nothing(self):
        proposal = intent("fetch_document", document_id="doc-0001-referral", to=NOT_SUPPLIED)
        assert proposal.args == {"document_id": "doc-0001-referral"}

    def test_whitespace_is_not_a_way_to_supply_an_undeclared_argument_unnoticed(self):
        """``"   "`` counts as empty, so it cannot smuggle a value past the check either way."""
        proposal = intent("fetch_document", document_id="doc-0001-referral", to="   ")
        assert proposal.args == {"document_id": "doc-0001-referral"}


class TestPlannerIntentArgs:
    def test_args_are_exactly_what_the_chosen_tool_declares(self):
        proposal = intent(
            "book_appointment",
            provider_id="PROV-1",
            patient_id="PT-4417",
            slot_start="2026-09-01T09:00:00+00:00",
            reason="cardiology review",
        )
        assert set(proposal.args) == REGISTRY.get("book_appointment").arg_names

    def test_declared_but_empty_arguments_are_kept_not_dropped(self):
        proposal = intent("book_appointment", provider_id="PROV-1", patient_id="PT-4417")
        assert proposal.args == {
            "provider_id": "PROV-1",
            "patient_id": "PT-4417",
            "slot_start": "",
            "reason": "",
        }

    @pytest.mark.parametrize("tool_name", TOOL_NAMES)
    def test_args_bind_against_the_real_tool_body_for_every_tool(self, tool_name):
        """The reason empties are kept: ``ToolSet.call`` binds by name against the exact set.

        Binding rather than calling — this asserts the mapping is callable, without producing an
        effect or touching an executor.
        """
        proposal = intent(tool_name)
        method = getattr(ToolSet, tool_name)
        signature(method).bind(None, **proposal.args)


class TestGeneratedFromTheRegistryInBothDirections:
    def test_a_tool_added_to_the_registry_appears_in_the_schema(self):
        registry = synthetic_registry()
        generated = build_planner_intent(registry)
        assert "wire_transfer" in tool_names(registry)
        proposal = generated(
            tool_name="wire_transfer",
            rationale="because",
            **dict.fromkeys(argument_names(registry), NOT_SUPPLIED)
            | {"beneficiary": "acct-1", "amount": "100"},
        )
        assert proposal.args == {"beneficiary": "acct-1", "amount": "100"}

    def test_an_argument_added_to_the_registry_appears_as_a_field(self):
        generated = build_planner_intent(synthetic_registry())
        assert {"beneficiary", "amount"} <= set(generated.model_fields)

    def test_nothing_appears_that_the_registry_does_not_declare(self):
        assert set(PlannerIntent.model_fields) - set(INTENT_FIELDS) == {
            arg for tool in REGISTRY for arg in tool.arg_names
        }

    def test_a_tool_absent_from_the_registry_is_absent_from_the_schema(self):
        assert "wire_transfer" not in TOOL_NAMES
        assert "beneficiary" not in PlannerIntent.model_fields

    def test_the_shipped_schema_and_the_shipped_registry_agree_on_tool_names(self):
        assert set(TOOL_NAMES) == REGISTRY.names

    def test_generation_is_deterministic(self):
        first = build_planner_intent(REGISTRY)
        second = build_planner_intent(REGISTRY)
        assert list(first.model_fields) == list(second.model_fields)


class TestGenerationFailsClosed:
    def test_a_registry_with_no_tools_raises_rather_than_generating_an_unusable_schema(self):
        with pytest.raises(EmptyRegistry):
            build_planner_intent(ToolRegistry([]))

    def test_an_argument_named_like_an_intent_field_raises(self):
        colliding = ToolRegistry(
            [
                ToolSpec(
                    name="shadow",
                    external_effect=False,
                    args=(ArgSpec(name="rationale", arg_class=ArgClass.VALUE),),
                )
            ]
        )
        with pytest.raises(ReservedArgumentName):
            build_planner_intent(colliding)

    @pytest.mark.parametrize("field", INTENT_FIELDS)
    def test_every_intent_field_is_protected_from_collision(self, field):
        colliding = ToolRegistry(
            [
                ToolSpec(
                    name="shadow",
                    external_effect=False,
                    args=(ArgSpec(name=field, arg_class=ArgClass.VALUE),),
                )
            ]
        )
        with pytest.raises(ReservedArgumentName):
            build_planner_intent(colliding)
