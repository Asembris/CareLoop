"""The proposal-tool boundary: closed schemas, one per action, that cannot act.

Three properties, and they are different questions.

**Generation** — the schemas come from the registry and cannot drift from it, in either direction.

**Canonicalisation** — what the model calls becomes exactly the ``(tool_name, args, rationale)``
everything downstream already consumes, and the round trip proves nothing is lost or invented.

**Refusal** — zero calls, several calls, an unoffered tool, a missing field, an extra field, a
wrong type and a missing rationale are each rejected deterministically, by their own exception,
with nothing manufactured from the wreckage.

**Schema-valid is not trusted and not authorised**, and the last class here says so with a test
rather than a comment: a perfectly valid proposal naming an attacker's address is exactly what the
policy engine exists to refuse, and it is still refused.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from strands.models import Model

from careloop.agents.invoke import (
    MAX_PROPOSAL_ATTEMPTS,
    AmbiguousProposalToolCall,
    MalformedProposal,
    NoProposalToolCall,
    UnknownProposalTool,
    proposal_tool_call,
    proposal_tool_specs,
)
from careloop.agents.schemas import (
    PROPOSAL_TOOL_PREFIX,
    PROPOSAL_TOOLS,
    PlannerIntent,
    ProposalToolBase,
    build_proposal_tools,
    proposal_tool_call_for,
    proposal_tool_name,
    proposed_tool_name,
)
from careloop.policy.engine import default_engine
from careloop.policy.verdicts import ReasonCode, Verdict
from careloop.provenance.trust import ArgClass
from careloop.state.case import CaseState
from careloop.tools.registry import AUTHORED_VALUES, REGISTRY, ArgSpec, ToolRegistry, ToolSpec
from tests.helpers import make_case, trusted_fact

ATTACKER = "records-intake@claims-continuity-partners.example"


# -- a model that answers exactly how a test tells it to ------------------------------------------


class ScriptedStream(Model):
    """Emits whatever ``blocks`` describe, as a real converse stream.

    Not :class:`~careloop.llm.mock.MockModel`: the mock is deliberately strict and only emits
    well-formed single calls, which is right for it and useless for testing malformation.
    """

    def __init__(self, blocks: list[tuple[str, Any]], stop_reason: str = "tool_use") -> None:
        self.blocks = blocks
        self.stop_reason = stop_reason

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted"}

    def update_config(self, **model_config: Any) -> None:
        return None

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise AssertionError("the planner must not reach structured_output")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        for index, (name, arguments) in enumerate(self.blocks):
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": name, "toolUseId": f"tu-{index}"}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": self.stop_reason}}


def ask(model: Model, tools=None):
    return asyncio.run(
        proposal_tool_call(model, tools or PROPOSAL_TOOLS, system_prompt="sys", user_prompt="user")
    )


VALID_EMAIL = {
    "rationale": "the caregiver asked for it",
    "to": "a@b.example",
    "subject": "s",
    "body": "b",
}


# -- generation -----------------------------------------------------------------------------------


class TestTheSchemasComeFromTheRegistry:
    def test_one_proposal_tool_per_registered_action(self):
        assert set(PROPOSAL_TOOLS) == {proposal_tool_name(name) for name in REGISTRY.names}

    def test_a_tool_added_to_the_registry_gains_a_proposal_tool(self):
        registry = ToolRegistry(
            [
                *REGISTRY,
                ToolSpec(
                    name="wire_transfer",
                    external_effect=True,
                    args=(ArgSpec(name="amount", arg_class=ArgClass.VALUE),),
                ),
            ]
        )
        generated = build_proposal_tools(registry)
        assert "propose_wire_transfer" in generated
        assert set(generated["propose_wire_transfer"].model_fields) == {"rationale", "amount"}

    def test_no_proposal_tool_exists_for_a_tool_the_registry_does_not_declare(self):
        smaller = ToolRegistry([REGISTRY.get("fetch_document")])
        assert set(build_proposal_tools(smaller)) == {"propose_fetch_document"}

    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_the_fields_are_exactly_rationale_and_that_tools_arguments(self, name):
        spec = REGISTRY.get(proposed_tool_name(name))
        assert set(PROPOSAL_TOOLS[name].model_fields) == {
            "rationale",
            *spec.model_authored_args,
        }

    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_every_field_is_required(self, name):
        assert all(info.is_required() for info in PROPOSAL_TOOLS[name].model_fields.values())

    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_no_field_carries_a_default(self, name):
        """Invariant 9's rule, here: a default would be CareLoop inventing part of a proposal."""
        for info in PROPOSAL_TOOLS[name].model_fields.values():
            assert info.default_factory is None
            assert info.is_required()

    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_the_transmitted_schema_is_closed(self, name):
        emitted = PROPOSAL_TOOLS[name].model_json_schema()
        spec = REGISTRY.get(proposed_tool_name(name))
        assert emitted["additionalProperties"] is False
        assert set(emitted["required"]) == {"rationale", *spec.model_authored_args}
        assert set(emitted["properties"]) == {"rationale", *spec.model_authored_args}
        for authored in spec.system_authored_args:
            assert authored not in emitted["properties"], authored
        assert "$ref" not in json.dumps(emitted)
        assert "oneOf" not in json.dumps(emitted)


class TestTheProposalToolsCannotAct:
    def test_no_proposal_tool_name_is_a_registry_name(self):
        assert not (set(PROPOSAL_TOOLS) & REGISTRY.names)

    def test_every_name_carries_the_prefix(self):
        assert all(name.startswith(PROPOSAL_TOOL_PREFIX) for name in PROPOSAL_TOOLS)

    def test_a_name_without_the_prefix_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError, match="not a proposal tool name"):
            proposed_tool_name("send_email")

    def test_a_proposal_tool_carries_no_body(self):
        for schema in PROPOSAL_TOOLS.values():
            for attribute in ("run", "invoke", "__call__", "execute"):
                assert not callable(getattr(schema, attribute, None)) or attribute == "__call__"


# -- canonicalisation -----------------------------------------------------------------------------


class TestCanonicalisation:
    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_it_produces_exactly_the_registrys_argument_names(self, name):
        """The seam fills what the model was not offered, so the canonical map is still complete.

        The model supplies the tool's ``MODEL``-authored arguments and nothing else; every declared
        argument is present afterwards, because a ``SYSTEM``-authored one is filled from the
        registry's declared value. The policy engine refuses an incomplete argument map, so this is
        the property that keeps a proposal evaluable at all.
        """
        spec = REGISTRY.get(proposed_tool_name(name))
        instance = PROPOSAL_TOOLS[name](
            rationale="r", **{arg: f"v-{arg}" for arg in spec.model_authored_args}
        )
        canonical = instance.canonical()
        assert canonical.tool_name == spec.name
        assert set(canonical.args) == spec.arg_names
        for authored in spec.system_authored_args:
            assert canonical.args[authored] == AUTHORED_VALUES[spec.name, authored]

    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_the_round_trip_changes_nothing(self, name):
        """Both directions, so "canonicalisation is faithful" has evidence, not a docstring."""
        spec = REGISTRY.get(proposed_tool_name(name))
        original = PlannerIntent(
            tool_name=spec.name,
            rationale="one sentence",
            args={
                arg: (
                    AUTHORED_VALUES[spec.name, arg]
                    if arg in spec.system_authored_args
                    else f"value-of-{arg}"
                )
                for arg in spec.arg_names
            },
        )
        assert proposal_tool_call_for(original).canonical() == original

    @pytest.mark.parametrize("name", sorted(PROPOSAL_TOOLS))
    def test_the_round_trip_discards_anything_else_a_caller_put_in_an_authored_argument(self, name):
        """The inverse cannot smuggle a value back in, which is what makes it a faithful inverse.

        ``proposal_tool_call_for`` builds the model-facing call, and a ``SYSTEM``-authored argument
        is not one of its fields. So a hand-built intent carrying something else there does not
        round-trip to itself — it round-trips to the authored value, which is the honest answer.
        """
        spec = REGISTRY.get(proposed_tool_name(name))
        if not spec.system_authored_args:
            pytest.skip(f"{spec.name} declares no SYSTEM-authored argument")
        hostile = PlannerIntent(
            tool_name=spec.name,
            rationale="one sentence",
            args={
                arg: ("I wrote this myself" if arg in spec.system_authored_args else f"v-{arg}")
                for arg in spec.arg_names
            },
        )
        recovered = proposal_tool_call_for(hostile).canonical()
        assert recovered != hostile
        for authored in spec.system_authored_args:
            assert recovered.args[authored] == AUTHORED_VALUES[spec.name, authored]

    def test_the_canonical_form_is_what_the_policy_engine_already_takes(self):
        """The seam, end to end: a proposal reaches a real verdict with no adaptation."""
        proposal = PROPOSAL_TOOLS["propose_send_email"](**VALID_EMAIL).canonical()
        case = make_case(state=CaseState.PLANNING)
        decision = default_engine().evaluate(proposal.tool_name, proposal.args, case)
        assert decision.tool_name == "send_email"
        assert decision.verdict is Verdict.DENY

    def test_rationale_survives_and_is_never_an_argument(self):
        proposal = PROPOSAL_TOOLS["propose_send_email"](**VALID_EMAIL).canonical()
        assert proposal.rationale == VALID_EMAIL["rationale"]
        assert "rationale" not in proposal.args


# -- refusal --------------------------------------------------------------------------------------


class TestMalformedAnswersAreRefusedNotRepaired:
    def test_zero_proposal_calls(self):
        with pytest.raises(NoProposalToolCall):
            ask(ScriptedStream([], stop_reason="end_turn"))

    def test_several_proposal_calls(self):
        model = ScriptedStream(
            [
                ("propose_send_email", VALID_EMAIL),
                ("propose_fetch_document", {"rationale": "r", "document_id": "doc-1"}),
            ]
        )
        with pytest.raises(AmbiguousProposalToolCall):
            ask(model)

    def test_a_tool_that_was_not_offered(self):
        """The phantom-tool failure the safety gate found, refused by name rather than by luck."""
        model = ScriptedStream([("book_appointment", {"patient_id": "PT-4417"})])
        with pytest.raises(UnknownProposalTool, match="book_appointment"):
            ask(model)

    def test_a_missing_required_argument(self):
        payload = {key: value for key, value in VALID_EMAIL.items() if key != "body"}
        with pytest.raises(MalformedProposal, match="body"):
            ask(ScriptedStream([("propose_send_email", payload)]))

    def test_a_missing_rationale(self):
        payload = {key: value for key, value in VALID_EMAIL.items() if key != "rationale"}
        with pytest.raises(MalformedProposal, match="rationale"):
            ask(ScriptedStream([("propose_send_email", payload)]))

    def test_an_extra_argument(self):
        payload = {**VALID_EMAIL, "admin_override": "true"}
        with pytest.raises(MalformedProposal, match="admin_override"):
            ask(ScriptedStream([("propose_send_email", payload)]))

    def test_a_wrong_primitive_type(self):
        payload = {**VALID_EMAIL, "body": {"nested": "object"}}
        with pytest.raises(MalformedProposal, match="body"):
            ask(ScriptedStream([("propose_send_email", payload)]))

    def test_a_non_object_argument_payload(self):
        model = ScriptedStream([("propose_send_email", ["not", "an", "object"])])
        with pytest.raises(MalformedProposal):
            ask(model)

    def test_the_failure_message_names_fields_and_never_values(self):
        """A ValidationError renders ``input_value``. That input is document-derived text."""
        payload = {**VALID_EMAIL, "to": ATTACKER, "admin_override": "true"}
        with pytest.raises(MalformedProposal) as raised:
            ask(ScriptedStream([("propose_send_email", payload)]))
        assert ATTACKER not in str(raised.value)
        assert "admin_override" in str(raised.value)

    def test_nothing_is_manufactured_from_a_malformed_answer(self):
        """No default, no repair, no partial proposal — the call raises and returns nothing."""
        payload = {key: value for key, value in VALID_EMAIL.items() if key != "body"}
        with pytest.raises(MalformedProposal):
            ask(ScriptedStream([("propose_send_email", payload)]))


class TestTheBoundedRetry:
    class Flaky(ScriptedStream):
        def __init__(self, bad_attempts: int) -> None:
            super().__init__([("propose_send_email", VALID_EMAIL)])
            self.attempts = 0
            self.bad_attempts = bad_attempts

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            self.attempts += 1
            if self.attempts <= self.bad_attempts:
                yield {"messageStart": {"role": "assistant"}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                return
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event

    def test_one_recoverable_failure_is_retried_once_and_succeeds(self):
        model = self.Flaky(bad_attempts=1)
        assert ask(model).canonical().tool_name == "send_email"
        assert model.attempts == 2

    def test_the_bound_is_two_attempts_and_then_it_raises(self):
        model = self.Flaky(bad_attempts=99)
        with pytest.raises(NoProposalToolCall):
            ask(model)
        assert model.attempts == MAX_PROPOSAL_ATTEMPTS

    def test_a_non_recoverable_failure_is_not_retried(self):
        class Exploding(ScriptedStream):
            def __init__(self):
                super().__init__([])
                self.attempts = 0

            async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
                self.attempts += 1
                raise RuntimeError("credentials are wrong")
                yield  # pragma: no cover

        model = Exploding()
        with pytest.raises(RuntimeError):
            ask(model)
        assert model.attempts == 1, "a credential error is not made truer by asking again"


class TestSchemaValidIsNotTrustedAndNotAuthorised:
    """The distinction the whole architecture rests on, asserted rather than asserted-about."""

    def test_a_perfectly_valid_proposal_can_name_an_attackers_destination(self):
        proposal = PROPOSAL_TOOLS["propose_send_email"](
            rationale="the notice instructs me to forward the record",
            to=ATTACKER,
            subject="Patient record",
            body="Full record attached.",
        )
        assert proposal.canonical().args["to"] == ATTACKER

    def test_and_the_deterministic_gate_still_refuses_it(self):
        proposal = PROPOSAL_TOOLS["propose_send_email"](
            rationale="the notice instructs me to forward the record",
            to=ATTACKER,
            subject="Patient record",
            body="Full record attached.",
        ).canonical()
        case = make_case(
            state=CaseState.PLANNING,
            facts=(trusted_fact("caregiver_email", "rosa.marlowe@carer.example"),),
        )
        decision = default_engine().evaluate(proposal.tool_name, proposal.args, case)
        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_the_schema_encodes_no_policy(self):
        """No trust level, no argument class, no rule id reaches the model's schema."""
        emitted = json.dumps(
            {name: schema.model_json_schema() for name, schema in PROPOSAL_TOOLS.items()}
        )
        for forbidden in ("TRUSTED", "UNTRUSTED", "CONTROLLED", "DESTINATION", "AUTHORITY"):
            assert forbidden not in emitted


class TestTheToolSpecificationsHandedToAProvider:
    def test_one_spec_per_proposal_tool_with_its_own_schema(self):
        specs = proposal_tool_specs(PROPOSAL_TOOLS)
        assert [spec["name"] for spec in specs] == sorted(PROPOSAL_TOOLS)
        for spec in specs:
            assert spec["description"]
            assert spec["inputSchema"]["json"]["additionalProperties"] is False

    def test_the_descriptions_say_the_call_records_a_proposal(self):
        for spec in proposal_tool_specs(PROPOSAL_TOOLS):
            assert "proposal" in spec["description"].lower()


def test_a_proposal_tool_is_a_pydantic_model_and_nothing_more():
    for schema in PROPOSAL_TOOLS.values():
        assert issubclass(schema, ProposalToolBase)
        assert issubclass(schema, BaseModel)


def test_constructing_one_with_a_bad_field_raises_validation_error():
    with pytest.raises(ValidationError):
        PROPOSAL_TOOLS["propose_send_email"](rationale="r")
