"""``model_json_schema()`` is not what the provider receives.

Session 5 closed a mutation survivor by asserting on the JSON schema rather than on construction,
because the schema is what constrains a model rather than what catches it afterwards. Session B
found that the same reasoning runs one layer further out and stops one layer too early.

``BedrockModel.structured_output`` does not transmit ``model_json_schema()``. It passes the model
through ``strands.tools.structured_output.convert_pydantic_to_tool_spec``, whose ``_flatten_schema``
keeps ``type``, ``properties``, ``title``, ``description`` and ``required`` and **drops everything
else, ``$defs`` included**. Two shapes this project seriously considered do not survive that:

* a discriminated union in a wrapper object arrives as ``oneOf`` over five ``$ref`` pointers with
  no ``$defs`` to resolve them against;
* the same union as a ``RootModel`` arrives as ``{"type": "object", "properties": {}}`` — an empty
  schema describing a tool that takes nothing.

**Neither conversion raises.** A planner built on either would have shipped, been handed an
unusable schema by its own SDK, and reported a model that could not follow instructions. So the
assertion belongs on the artifact that actually ships, and it is written here rather than beside
the schema because the two failing shapes are measurement artifacts and live in ``bench/``.

The union shapes are the vacuity proof. Every assertion below is made against the shipped schema,
which must pass, and against those two, which must fail.
"""

from __future__ import annotations

import json

import pytest

from bench.intent_shapes import UnionRootModel, UnionWrapped, provider_tool_spec, tool_spec_defects
from careloop.agents.schemas import INTENT_FIELDS, PlannerIntent, TriageResult, build_planner_intent
from careloop.provenance.trust import ArgClass
from careloop.tools.registry import REGISTRY, ArgSpec, ToolRegistry, ToolSpec

#: The shapes CareLoop actually hands a provider.
SHIPPED = (PlannerIntent, TriageResult)

#: The shapes that reach a provider damaged, kept so every check below has something to fail on.
DAMAGED = (UnionWrapped, UnionRootModel)


class TestWhatTheProviderReceivesDescribesWhatWeAsked:
    @pytest.mark.parametrize("schema", SHIPPED, ids=lambda schema: schema.__name__)
    def test_the_shipped_schemas_convert_soundly(self, schema):
        assert tool_spec_defects(schema) == []

    @pytest.mark.parametrize("schema", SHIPPED, ids=lambda schema: schema.__name__)
    def test_every_field_the_model_requires_is_required_in_the_transmitted_schema(self, schema):
        required_by_model = {
            name for name, info in schema.model_fields.items() if info.is_required()
        }
        assert required_by_model <= set(provider_tool_spec(schema).get("required", []))

    @pytest.mark.parametrize("schema", SHIPPED, ids=lambda schema: schema.__name__)
    def test_every_field_the_model_declares_is_a_property_in_the_transmitted_schema(self, schema):
        assert set(schema.model_fields) == set(provider_tool_spec(schema)["properties"])

    @pytest.mark.parametrize("schema", SHIPPED, ids=lambda schema: schema.__name__)
    def test_nothing_transmitted_points_at_a_definition_that_was_dropped(self, schema):
        assert "$ref" not in json.dumps(provider_tool_spec(schema))


class TestTheGuardFailsOnTheShapesItExistsToCatch:
    """Vacuity, both directions. A check nothing fails is not a check."""

    @pytest.mark.parametrize("schema", DAMAGED, ids=lambda schema: schema.__name__)
    def test_a_union_shape_is_reported_as_defective(self, schema):
        assert tool_spec_defects(schema) != []

    def test_the_wrapped_union_loses_its_definitions(self):
        emitted = provider_tool_spec(UnionWrapped)
        assert "$ref" in json.dumps(emitted)
        assert "$defs" not in emitted

    def test_the_rootmodel_union_transmits_an_empty_schema(self):
        emitted = provider_tool_spec(UnionRootModel)
        assert emitted.get("properties") == {}
        assert not emitted.get("required")

    @pytest.mark.parametrize("schema", DAMAGED, ids=lambda schema: schema.__name__)
    def test_the_conversion_is_silent_about_all_of_it(self, schema):
        """The reason this is a test and not a ``try``/``except`` somewhere in the code."""
        assert isinstance(provider_tool_spec(schema), dict)


class TestTheToolEnumerationSurvivesTransmission:
    """Session 5's assertion, re-made against the document that actually ships.

    ``tool_name`` being a ``Literal`` is what stops a model naming a tool the policy engine has
    never classified. That constraint is worth nothing if it does not survive the conversion, and
    the map shape leans on it harder than the flat shape did: the enumeration is now the only
    thing the transmitted schema says about what may be proposed.
    """

    def test_the_transmitted_enumeration_is_exactly_the_registry(self):
        emitted = provider_tool_spec(PlannerIntent)
        assert set(emitted["properties"]["tool_name"]["enum"]) == REGISTRY.names

    def test_a_tool_added_to_the_registry_widens_the_transmitted_enumeration(self):
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
        emitted = provider_tool_spec(build_planner_intent(registry))
        assert set(emitted["properties"]["tool_name"]["enum"]) == REGISTRY.names | {"wire_transfer"}

    def test_the_transmitted_schema_asks_for_the_three_intent_fields_and_no_more(self):
        emitted = provider_tool_spec(PlannerIntent)
        assert set(emitted["required"]) == set(INTENT_FIELDS)
        assert set(emitted["properties"]) == set(INTENT_FIELDS)

    def test_the_transmitted_schema_asks_for_no_argument_by_name(self):
        """The change that fixed the planner, asserted where the model actually reads it."""
        emitted = provider_tool_spec(PlannerIntent)
        every_argument = {arg for tool in REGISTRY for arg in tool.arg_names}
        assert not (set(emitted["properties"]) & every_argument)
