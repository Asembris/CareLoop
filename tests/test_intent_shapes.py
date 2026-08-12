"""The shape-comparison instrument, checked without spending anything.

Everything here runs offline, on CI, with no credentials. What it guards is that the instrument
measures what it claims to: that each candidate reaches the provider as a schema describing the
shape it says it does, that every shape is reduced to the same answer before being compared, and
that a shape cannot look better by answering an easier question.

The live half — what a real model does with each shape — is deliberately not here. It is a
measurement of a stochastic system and belongs in a report, for the reason ``bench/live.py``
records at length.
"""

from __future__ import annotations

from typing import get_origin

import pytest
from pydantic import BaseModel, ValidationError

from bench.intent_shapes import (
    CANDIDATES,
    FLAT_SPECIFIC_SENTENCE,
    ArgumentDefect,
    ShapeTrial,
    TwoCallShape,
    build_dict_args,
    build_list_args,
    build_per_tool_arguments,
    build_tool_choice,
    candidate,
    captured_prompt,
    normalise,
    provider_tool_spec,
    summarise,
    system_prompt_for,
    tool_spec_defects,
)
from careloop.agents.schemas import PlannerIntent
from careloop.provenance import ArgClass
from careloop.tools.registry import REGISTRY, ArgSpec, ToolRegistry, ToolSpec


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


def _valid_payload(schema: type[BaseModel]) -> dict[str, object]:
    """The smallest payload ``schema`` accepts, whatever its shape.

    Every candidate's fields are a ``tool_name`` enumeration, strings, a string map or a list of
    pairs, so this stays a lookup rather than becoming a second schema definition.
    """
    payload: dict[str, object] = {}
    for name, info in schema.model_fields.items():
        origin = get_origin(info.annotation)
        if name == "tool_name":
            payload[name] = "fetch_document"
        elif origin is dict:
            payload[name] = {}
        elif origin is list:
            payload[name] = []
        else:
            payload[name] = ""
    return payload


class TestTheCapturedPromptIsTheRealOne:
    """Session A's durable lesson: a bisect on a synthetic input measures the synthetic input."""

    def test_the_capture_is_on_disk_and_carries_both_halves(self):
        system, user = captured_prompt()
        assert system.strip()
        assert user.strip()

    def test_the_capture_carries_the_poisonable_evidence_block(self):
        _, user = captured_prompt()
        assert "--- BEGIN EVIDENCE ---" in user
        assert "--- END EVIDENCE ---" in user

    def test_the_capture_still_contains_the_sentence_the_adapted_mode_replaces(self):
        """If the capture is regenerated, ``--adapted`` must fail loudly rather than silently."""
        system, _ = captured_prompt()
        assert FLAT_SPECIFIC_SENTENCE in system


class TestEveryShapeReachesTheProviderIntact:
    """A shape whose emitted schema is defective would be measured on the defect."""

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_the_emitted_schema_has_no_defects(self, shape):
        for schema in shape.schemas:
            assert tool_spec_defects(schema) == []

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_tool_name_survives_as_a_closed_enumeration(self, shape):
        """Whatever the shape, an unregistered tool is refused at the schema boundary."""
        emitted = provider_tool_spec(shape.schemas[0])
        assert set(emitted["properties"]["tool_name"]["enum"]) == set(REGISTRY.names)

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_every_required_name_is_a_property(self, shape):
        for schema in shape.schemas:
            emitted = provider_tool_spec(schema)
            assert set(emitted.get("required", [])) <= set(emitted["properties"])

    def test_the_per_tool_argument_schemas_are_sound_too(self):
        """Shape E hands over a second schema, generated per tool, and it ships as well."""
        for name in REGISTRY.names:
            schema = build_per_tool_arguments(name)
            assert tool_spec_defects(schema) == []
            emitted = provider_tool_spec(schema)
            assert set(emitted["properties"]) == set(REGISTRY.get(name).arg_names)
            assert set(emitted["required"]) == set(REGISTRY.get(name).arg_names)


class TestOnlyTheShippedShapeAsksForEveryArgument:
    """The property under investigation, stated as an assertion rather than as a belief."""

    def test_the_shipped_shape_requires_every_argument_of_every_tool(self):
        emitted = provider_tool_spec(PlannerIntent)
        every_argument = {arg for tool in REGISTRY for arg in tool.arg_names}
        assert every_argument <= set(emitted["required"])

    @pytest.mark.parametrize(
        "shape", [shape for shape in CANDIDATES if shape.key != "A_flat"], ids=lambda s: s.key
    )
    def test_every_other_shape_requires_no_argument_field_at_all(self, shape):
        """The whole point: a model choosing one tool is never asked about another's arguments."""
        emitted = provider_tool_spec(shape.schemas[0])
        every_argument = {arg for tool in REGISTRY for arg in tool.arg_names}
        assert not (set(emitted["required"]) & every_argument)


class TestTheShapesAreGeneratedFromTheRegistry:
    """A shape that hard-coded a tool name could drift from what the policy engine governs."""

    def test_a_registry_with_an_extra_tool_widens_every_enumeration(self):
        registry = synthetic_registry()
        for build in (build_dict_args, build_list_args, build_tool_choice):
            schema = build(registry)
            emitted = provider_tool_spec(schema)
            assert "wire_transfer" in emitted["properties"]["tool_name"]["enum"]

    def test_an_unregistered_tool_has_no_argument_schema(self):
        with pytest.raises(KeyError):
            build_per_tool_arguments("wire_transfer")


class TestEveryShapeIsReducedToOneAnswer:
    """Comparison is only meaningful if the shapes are compared on the same thing."""

    def test_missing_declared_arguments_become_the_empty_string(self):
        proposal = normalise("send_email", {"to": "a@b.example"})
        assert proposal.args == {"body": "", "subject": "", "to": "a@b.example"}

    def test_the_answer_is_exactly_what_the_tool_declares(self):
        proposal = normalise("book_appointment", {})
        assert set(proposal.args) == set(REGISTRY.get("book_appointment").arg_names)

    def test_an_argument_the_tool_does_not_take_is_a_defect_not_a_silent_drop(self):
        with pytest.raises(ArgumentDefect):
            normalise("book_appointment", {"to": "attacker@evil.example"})

    def test_an_empty_foreign_argument_is_not_a_defect(self):
        """The flat shape carries every argument, so empties are noise rather than a proposal."""
        proposal = normalise("book_appointment", {"to": ""})
        assert "to" not in proposal.args

    def test_a_defect_is_counted_apart_from_a_schema_failure(self):
        trials = [
            ShapeTrial(shape_key="x", model_spec="m", argument_defect="took to"),
            ShapeTrial(shape_key="x", model_spec="m", schema_failure="ValidationError: 7 errors"),
            ShapeTrial(shape_key="x", model_spec="m", tool_name="send_email"),
        ]
        stats = summarise(trials)
        assert (stats["valid"], stats["schema_failures"], stats["argument_defects"]) == (1, 1, 1)


class TestTheUnionIsKeptAsEvidence:
    """The vacuity proof for :func:`tool_spec_defects`, and the reason the union was not chosen.

    Both shapes below are what the approved plan originally proposed. Neither is a candidate;
    they are here because offline evidence that survives outlives one measured failure, and
    because the ``RootModel`` case is silent — the conversion raises nothing at all.
    """

    def test_the_wrapped_union_reaches_the_provider_with_dangling_references(self):
        from bench.intent_shapes import UnionWrapped

        defects = tool_spec_defects(UnionWrapped)
        assert any("$ref" in defect for defect in defects)

    def test_the_rootmodel_union_reaches_the_provider_as_an_empty_schema(self):
        from bench.intent_shapes import UnionRootModel

        emitted = provider_tool_spec(UnionRootModel)
        assert emitted.get("properties") == {}
        assert any("no properties" in defect for defect in tool_spec_defects(UnionRootModel))

    def test_neither_conversion_raises(self):
        """The failure mode is silence, which is why a test rather than a try/except is needed."""
        from bench.intent_shapes import UnionRootModel, UnionWrapped

        for schema in (UnionWrapped, UnionRootModel):
            assert isinstance(provider_tool_spec(schema), dict)

    def test_the_shipped_schema_passes_the_check_the_unions_fail(self):
        """Both directions. A check nothing passes is as useless as one nothing fails."""
        assert tool_spec_defects(PlannerIntent) == []


class TestThePromptIsHeldConstantByDefault:
    """Varying the schema *and* the prompt would measure neither."""

    def test_verbatim_is_the_captured_prompt_unchanged(self):
        system, _ = captured_prompt()
        for shape in CANDIDATES:
            assert system_prompt_for(shape, system, adapted=False) == system

    def test_adapted_replaces_only_the_flat_specific_sentence(self):
        system, _ = captured_prompt()
        shape = candidate("B_map")
        adapted = system_prompt_for(shape, system, adapted=True)
        assert FLAT_SPECIFIC_SENTENCE not in adapted
        assert shape.adapted_instruction in adapted
        assert len(adapted.splitlines()) == len(system.splitlines())

    def test_adapting_the_flat_shape_changes_nothing(self):
        system, _ = captured_prompt()
        assert system_prompt_for(candidate("A_flat"), system, adapted=True) == system


class TestTheTwoCallShapeIsOneAgentMakingTwoCalls:
    """Invariant 1 counts LLM agents, and shape E adds none."""

    def test_the_tool_choice_output_carries_no_arguments(self):
        """It is not a proposal: there is nothing for a policy engine to decide about."""
        choice = build_tool_choice()
        every_argument = {arg for tool in REGISTRY for arg in tool.arg_names}
        assert not (set(choice.model_fields) & every_argument)

    def test_the_tool_choice_output_cannot_express_execution_or_state(self):
        assert set(build_tool_choice().model_fields) == {"tool_name", "rationale"}

    def test_it_declares_that_it_costs_two_calls(self):
        shape = candidate("E_two_call")
        assert isinstance(shape, TwoCallShape)
        assert shape.calls_per_proposal == 2

    def test_every_single_call_shape_declares_one(self):
        for shape in CANDIDATES:
            if not isinstance(shape, TwoCallShape):
                assert shape.calls_per_proposal == 1


class TestNoShapeCanExpressAuthority:
    """Whatever the shape, the schema is a proposal and nothing more."""

    FORBIDDEN = ("execute", "approve", "approved", "state", "verdict", "urgency", "priority")

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_no_field_name_suggests_a_decision(self, shape):
        for schema in shape.schemas:
            for name in schema.model_fields:
                assert name.lower() not in self.FORBIDDEN

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_every_shape_forbids_extra_fields(self, shape):
        """Refused *for being extra*, not for a payload that was invalid anyway.

        The obvious version of this test hands every shape one payload and watches it raise. Two
        of the four shapes would then raise about missing fields and the guard would pass with
        ``extra="forbid"`` deleted, so the valid payload is built per shape first.
        """
        for schema in shape.schemas:
            assert schema.model_config.get("extra") == "forbid"
            payload = _valid_payload(schema)
            schema.model_validate(payload)  # the baseline really is valid

            with pytest.raises(ValidationError) as raised:
                schema.model_validate({**payload, "execute": True})
            assert [error["type"] for error in raised.value.errors()] == ["extra_forbidden"]

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_every_shape_is_frozen(self, shape):
        for schema in shape.schemas:
            assert schema.model_config.get("frozen") is True

    def test_no_shape_carries_a_default(self):
        """Session A refuted defaults live; this stops one reappearing as a convenience."""
        for shape in CANDIDATES:
            for schema in shape.schemas:
                for name, info in schema.model_fields.items():
                    assert info.is_required(), f"{schema.__name__}.{name} carries a default"


class TestTheInstrumentDoesNotEchoDocumentText:
    """A ``ValidationError`` renders every offending field with its ``input_value``."""

    def test_only_the_first_line_of_a_failure_is_kept(self):
        from bench.intent_shapes import _first_line

        try:
            PlannerIntent(tool_name="send_email", rationale="x")
        except ValidationError as failure:
            kept = _first_line(failure)
            assert "input_value" not in kept
            assert "validation error" in kept.lower()
        else:  # pragma: no cover - the shipped schema requires more than two fields
            pytest.fail("the shipped schema accepted an intent with no arguments")


class TestTheShapesAreRealPydanticModels:
    """Cheap, and it catches a candidate list that has drifted into holding something else."""

    @pytest.mark.parametrize("shape", CANDIDATES, ids=lambda shape: shape.key)
    def test_every_schema_is_a_model(self, shape):
        for schema in shape.schemas:
            assert issubclass(schema, BaseModel)
