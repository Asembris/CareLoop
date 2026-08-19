"""What the agents are allowed to say, and what they structurally cannot.

Nothing here measures model quality. That was the benchmark's job — see
``docs/model-benchmark.md`` — and repeating it against a scripted mock would prove nothing.

**Session B moved a load-bearing check.** Until session B the planner's schema carried one field
per argument of every tool, so a wrong-tool argument was refused twice: once by the field set the
provider was handed, and once by the validator. The map shape expresses no argument binding to the
provider at all, so :class:`TestTheValidatorIsTheOnlyThingLeftGuardingThis` is now the only thing
between the model and an argument nothing has classified. It is written to fail loudly if that
validator is weakened, and the mutation pass covers it specifically.
"""

from __future__ import annotations

from inspect import signature
from itertools import permutations

import pytest
from pydantic import BaseModel, ValidationError

from careloop.agents.schemas import (
    ARG_NAMES,
    INTENT_FIELDS,
    NOT_SUPPLIED,
    TOOL_NAMES,
    EmptyRegistry,
    FrozenArguments,
    PlannerIntent,
    PlannerIntentBase,
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
    return PlannerIntent(tool_name=tool_name, rationale="because", args=args)


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


class TestTheSummaryStatesTheOutstandingWork:
    """The planner's only statement of what the case is *for*, so it has to be about the work.

    ``planner_user_prompt`` is a narrow projection — the summary and the facts, never the inbound
    event body — so a summary that names the document that arrived leaves the planner with no
    statement of what was asked for. Measured against the deployed model on the
    appointment-instructions fixture: with a summary naming the paperwork, ``send_email`` was
    proposed 0 times in 8 production runs and the caregiver checkpoint never appeared; with a
    summary naming the outstanding work, 10 of 10 and a pending approval every time.

    Offline this can only pin the contract, not the model's obedience to it. That is the honest
    division: the wording is asserted here, and the behaviour it buys is measured against a real
    model and recorded in the session log.
    """

    def test_the_summary_asks_for_the_work_rather_than_the_document(self) -> None:
        description = TriageResult.model_fields["summary"].description or ""

        assert "outstanding" in description, description
        assert "still has to be done" in description, description

    def test_it_warns_against_naming_the_paperwork(self) -> None:
        """The specific failure mode, named in the contract so it cannot quietly come back."""
        description = TriageResult.model_fields["summary"].description or ""

        assert "document that arrived" in description, description

    def test_the_summary_is_still_required_and_carries_no_default(self) -> None:
        """A default would let CareLoop invent the one sentence the planner reasons from."""
        field = TriageResult.model_fields["summary"]

        assert field.is_required()
        assert field.default_factory is None


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
    def test_the_field_set_is_exactly_the_intent_fields(self):
        """Closed, and no longer growing with the registry — that is the session B shape."""
        assert set(PlannerIntent.model_fields) == set(INTENT_FIELDS)

    def test_the_field_set_does_not_grow_when_the_registry_does(self):
        """The property the flat shape did not have, and the reason it failed against a model."""
        generated = build_planner_intent(synthetic_registry())
        assert set(generated.model_fields) == set(PlannerIntent.model_fields)

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

    def test_the_arguments_are_frozen_too_and_not_merely_the_field_holding_them(self):
        """Pydantic's ``frozen`` freezes fields, not the objects they hold.

        The same shallow-freeze hole session 2 closed on ``Case`` by making its collections
        tuples. A tuple would change the schema the provider receives, so the mapping refuses
        mutation instead.
        """
        proposal = intent("send_email", to="rosa.marlowe@carer.example")
        assert isinstance(proposal.args, FrozenArguments)
        for mutate in (
            lambda: proposal.args.__setitem__("to", ATTACKER),
            lambda: proposal.args.update({"to": ATTACKER}),
            lambda: proposal.args.setdefault("cc", ATTACKER),
            lambda: proposal.args.pop("to"),
            lambda: proposal.args.clear(),
        ):
            with pytest.raises(TypeError):
                mutate()
        assert proposal.args["to"] == "rosa.marlowe@carer.example"

    def test_an_invented_field_is_rejected_rather_than_dropped(self):
        with pytest.raises(ValidationError):
            PlannerIntent(
                tool_name="fetch_document",
                rationale="because",
                args={"document_id": "doc-0001-referral"},
                execute_now="true",
            )


class TestPlannerIntentRejectsWhatTheRegistryDoesNotDeclare:
    def test_an_unregistered_tool_is_rejected(self):
        with pytest.raises(ValidationError):
            intent("wire_transfer")

    def test_the_schema_the_provider_is_given_enumerates_the_registry_and_nothing_else(self):
        """Rejection at the *schema* boundary, which is a different thing from rejection at all.

        The JSON schema is what the provider is handed, so an enumerated ``tool_name`` constrains
        what the model can emit rather than catching it afterwards. Constructing an intent with an
        unregistered name is refused either way — the validator would catch it — so the assertion
        above cannot tell the two apart, and a widened annotation costs a wasted call and a replan
        without failing a single test. This is the one that notices.

        ``tests/test_provider_tool_spec.py`` carries the same assertion one layer further out,
        against the tool spec the SDK actually transmits, because those two are not the same
        document.
        """
        field = PlannerIntent.model_json_schema()["properties"]["tool_name"]
        assert set(field["enum"]) == set(TOOL_NAMES)

    def test_the_arguments_reach_the_provider_as_a_string_map(self):
        field = PlannerIntent.model_json_schema()["properties"]["args"]
        assert field["type"] == "object"
        assert field["additionalProperties"] == {"type": "string"}

    def test_a_tool_added_to_the_registry_widens_the_enumeration(self):
        generated = build_planner_intent(synthetic_registry())
        field = generated.model_json_schema()["properties"]["tool_name"]
        assert set(field["enum"]) == set(TOOL_NAMES) | {"wire_transfer"}


class TestTheValidatorIsTheOnlyThingLeftGuardingThis:
    """The check that used to be enforced twice and is now enforced once.

    The flat shape enumerated every argument as a field, so the provider's own schema refused an
    argument no tool declared. The map shape says nothing to the provider about which arguments
    belong to which tool, so everything below rests on
    ``PlannerIntentBase._bind_arguments_to_the_chosen_tool``. Delete that validator and this class
    is what goes red.
    """

    def test_an_argument_belonging_to_another_tool_is_rejected(self):
        with pytest.raises(ValidationError):
            intent("fetch_document", document_id="doc-0001-referral", to=ATTACKER)

    @pytest.mark.parametrize("chosen,other", list(permutations(TOOL_NAMES, 2)))
    def test_no_tool_accepts_any_other_tools_argument(self, chosen, other):
        """Every ordered pair, so the guard cannot hold for the one example it was written with."""
        foreign = sorted(REGISTRY.get(other).arg_names - REGISTRY.get(chosen).arg_names)
        if not foreign:
            pytest.skip(f"{other} declares no argument {chosen} does not")
        with pytest.raises(ValidationError):
            intent(chosen, **{foreign[0]: "a value the chosen tool has no field for"})

    def test_an_argument_no_tool_declares_at_all_is_rejected(self):
        with pytest.raises(ValidationError):
            intent("fetch_document", document_id="doc-0001-referral", beneficiary="acct-1")

    def test_the_rejection_message_names_arguments_and_never_their_values(self):
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

    def test_the_guard_is_not_passing_because_nothing_is_ever_accepted(self):
        """Vacuity: the arguments a tool *does* declare must go through untouched."""
        proposal = intent("send_email", to="rosa.marlowe@carer.example", subject="Referral")
        assert proposal.args["to"] == "rosa.marlowe@carer.example"
        assert proposal.args["subject"] == "Referral"


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

    def test_a_tool_given_no_arguments_at_all_still_gets_the_full_set(self):
        """The model may omit ``args`` entries; it may not thereby omit them from the call."""
        proposal = intent("book_appointment")
        assert set(proposal.args) == REGISTRY.get("book_appointment").arg_names
        assert set(proposal.args.values()) == {""}

    @pytest.mark.parametrize("tool_name", TOOL_NAMES)
    def test_args_bind_against_the_real_tool_body_for_every_tool(self, tool_name):
        """The reason empties are kept: ``ToolSet.call`` binds by name against the exact set.

        Binding rather than calling — this asserts the mapping is callable, without producing an
        effect or touching an executor.
        """
        proposal = intent(tool_name)
        method = getattr(ToolSet, tool_name)
        signature(method).bind(None, **proposal.args)

    def test_the_arguments_survive_json_encoding(self):
        """``idempotency_key`` JSON-encodes the mapping, so the frozen type must remain a dict."""
        import json

        proposal = intent("send_email", to="rosa.marlowe@carer.example")
        assert json.loads(json.dumps(proposal.args)) == dict(proposal.args)


class TestGeneratedFromTheRegistryInBothDirections:
    def test_a_tool_added_to_the_registry_can_be_proposed(self):
        registry = synthetic_registry()
        generated = build_planner_intent(registry)
        assert "wire_transfer" in tool_names(registry)
        proposal = generated(
            tool_name="wire_transfer",
            rationale="because",
            args={"beneficiary": "acct-1", "amount": "100"},
        )
        assert proposal.args == {"beneficiary": "acct-1", "amount": "100"}

    def test_an_argument_added_to_the_registry_becomes_acceptable_for_its_own_tool(self):
        generated = build_planner_intent(synthetic_registry())
        proposal = generated(
            tool_name="wire_transfer", rationale="because", args={"beneficiary": "acct-1"}
        )
        assert proposal.args == {"amount": "", "beneficiary": "acct-1"}

    def test_and_stays_unacceptable_for_every_other_tool(self):
        generated = build_planner_intent(synthetic_registry())
        with pytest.raises(ValidationError):
            generated(
                tool_name="fetch_document", rationale="because", args={"beneficiary": "acct-1"}
            )

    def test_a_tool_absent_from_the_registry_cannot_be_proposed(self):
        assert "wire_transfer" not in TOOL_NAMES
        with pytest.raises(ValidationError):
            intent("wire_transfer", beneficiary="acct-1")

    def test_the_shipped_schema_and_the_shipped_registry_agree_on_tool_names(self):
        assert set(TOOL_NAMES) == REGISTRY.names

    def test_the_argument_names_helper_still_describes_the_registry(self):
        """No longer the field set; still a true statement, and the prompt renders from it."""
        assert set(ARG_NAMES) == {arg for tool in REGISTRY for arg in tool.arg_names}
        assert set(argument_names(synthetic_registry())) == set(ARG_NAMES) | {
            "beneficiary",
            "amount",
        }

    def test_generation_is_deterministic(self):
        first = build_planner_intent(REGISTRY)
        second = build_planner_intent(REGISTRY)
        assert list(first.model_fields) == list(second.model_fields)


class TestGenerationFailsClosed:
    def test_a_registry_with_no_tools_raises_rather_than_generating_an_unusable_schema(self):
        with pytest.raises(EmptyRegistry):
            build_planner_intent(ToolRegistry([]))

    @pytest.mark.parametrize("field", INTENT_FIELDS)
    def test_an_argument_named_like_an_intent_field_is_no_longer_a_collision(self, field):
        """``ReservedArgumentName`` guarded a hazard the map shape removes.

        Under the flat shape an argument called ``rationale`` became a *field* and silently
        shadowed the intent's own, so generation refused it. Arguments are now map keys and cannot
        shadow anything, so the guard was deleted rather than kept pointing at an impossible
        condition — a guard for something that cannot happen implies a hazard that does not exist.
        This is what replaces it: the collision is constructed, and it is harmless.
        """
        colliding = ToolRegistry(
            [
                ToolSpec(
                    name="shadow",
                    external_effect=False,
                    args=(ArgSpec(name=field, arg_class=ArgClass.VALUE),),
                )
            ]
        )
        generated = build_planner_intent(colliding)
        proposal = generated(tool_name="shadow", rationale="because", args={field: "a value"})
        assert proposal.rationale == "because"
        assert proposal.tool_name == "shadow"
        assert proposal.args == {field: "a value"}
