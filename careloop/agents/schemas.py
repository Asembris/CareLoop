"""What the two agents are allowed to say.

An agent's output schema is the narrowest place to enforce that the LLM never holds authority: a
field that cannot be expressed cannot be decided. So neither schema here carries a case state, a
policy verdict, an approval, a priority override, or any other lever over what happens next.
:class:`PlannerIntent` in particular is a *proposal* — a tool name, its arguments, and a sentence
of reasoning — and there is deliberately no field through which it could say "and execute this".
That is checked by enumeration rather than by inspection: the intent's field set is exactly
``INTENT_FIELDS``, and a test pins it.

**``PlannerIntent`` is generated from the registry.** Tool names come from
:data:`careloop.tools.registry.REGISTRY`, so they cannot drift from the tools the policy engine
governs. A planner offered a tool the engine has never classified is a planner proposing effects
nothing decides the authority of.

**Why the arguments are a map, and what it cost to learn.** The shape here until session B was
flat: one required ``str`` field per argument name across *every* tool, thirteen required fields
in all, with the empty string meaning "not supplied". The reasoning was sound and the measurement
was not taken. Session A found the consequence — against the recommended planner model that schema
produced a valid intent **zero times in fifteen runs**, because a model that picks
``book_appointment`` fills its four arguments and omits the seven belonging to tools it did not
choose, which is the reasonable thing to do.

Session B measured the alternatives with the provider held constant and only the shape varying,
on the prompt the graph really sends. Same model, same session:

===============================  ================  =========
Shape                            required fields   valid
===============================  ================  =========
flat, every argument of every    13                **0/8**
tool
``args`` as a string map          3                **8/8**
``args`` as name/value pairs      3                8/8
two calls, tool then arguments    2, then that      8/8
                                  tool's
===============================  ================  =========

The map won on being the cheapest and simplest of three shapes that tied on the only outcome that
matters. Full detail in ``docs/live-loop-findings.md``.

**What the map gives up, and where it went.** Which arguments belong to which tool is no longer
expressed in the JSON schema the provider receives — it is enforced by
:meth:`PlannerIntentBase._bind_arguments_to_the_chosen_tool` alone. That validator was a second
line of defence and is now the only one, so it is load-bearing in a way it was not before, and the
mutation pass covers it specifically. What did **not** move is the tool enumeration: ``tool_name``
is still a ``Literal`` and still reaches the provider as a closed ``enum``.

**A discriminated union was the obvious answer and does not work here.** ``strands-agents==1.51.0``
converts a Pydantic model to a Bedrock tool spec through ``convert_pydantic_to_tool_spec``, which
drops ``$defs``: a union in a wrapper object reaches the provider as ``oneOf`` over five dangling
``$ref`` pointers, and a ``RootModel`` union reaches it as ``{"type": "object", "properties": {}}``
— an empty schema, with nothing raised. Both shapes are kept in ``bench/intent_shapes.py`` as the
vacuity proof for the guard in ``tests/test_provider_tool_spec.py``.

**Triage emits no destination.** There is no ``provider_id``, no recipient, no portal on
:class:`TriageResult`, and its absence is load-bearing rather than an omission. The policy engine
resolves an argument's trust by looking its *value* up in ``case.facts`` and taking the **lowest**
trust level among the matches. A triage-emitted ``UNTRUSTED`` fact carrying the same string as a
``TRUSTED`` configuration fact would therefore drag the legitimate value down to ``UNTRUSTED`` and
deny the action it was needed for. Triage produces ``VALUE``-class candidates only; destinations
come from trusted configuration and nowhere else.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, NoReturn

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, create_model, model_validator

from careloop.tools.registry import REGISTRY, ToolRegistry

#: The empty string, meaning "the source did not state this" or "this tool does not take it".
NOT_SUPPLIED: Final[str] = ""

#: Every field a :class:`PlannerIntent` carries, whatever tools exist. The set is now closed
#: rather than growing with the registry, which is the whole point of the shape.
INTENT_FIELDS: Final[tuple[str, ...]] = ("tool_name", "rationale", "args")


class EmptyRegistry(Exception):
    """Raised when an intent is generated from a registry declaring no tools.

    A planner with no callable tools has nothing to propose. Failing here is louder than emitting
    a schema whose ``tool_name`` can never be satisfied.
    """


class FrozenArguments(dict[str, str]):
    """A mapping that refuses to be changed after validation.

    ``model_config = ConfigDict(frozen=True)`` freezes the *fields* of a model, not the objects
    they hold, so a plain ``dict`` field would leave the intent mutable through its own arguments —
    the shallow-freeze hole session 2 closed on :class:`~careloop.state.case.Case` by making its
    collections tuples. A tuple is not available here without changing the JSON schema the
    provider receives, which is the thing that was measured, so the mapping refuses mutation
    instead.

    It subclasses ``dict`` deliberately rather than implementing ``Mapping``: the arguments are
    JSON-encoded when an idempotency key is derived, and ``json.dumps`` accepts a ``dict``
    subclass while raising on an unrelated mapping type. An immutability that broke keying would
    have traded a real property for a theoretical one.
    """

    __slots__ = ()

    def _immutable(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("a planner intent's arguments cannot be changed after it is validated")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


#: The declared type of ``PlannerIntent.args``. The annotation is a plain string map, so the JSON
#: schema the provider receives is exactly the one that was measured; the validator is what makes
#: the value immutable once it exists.
ArgumentMap = Annotated[dict[str, str], AfterValidator(FrozenArguments)]


class TriageCategory(StrEnum):
    """What kind of administrative work an inbound event represents."""

    APPOINTMENT = "APPOINTMENT"
    INSURANCE = "INSURANCE"
    PRESCRIPTION = "PRESCRIPTION"
    RECORDS = "RECORDS"
    BILLING = "BILLING"
    OTHER = "OTHER"


class Urgency(StrEnum):
    """How quickly a caregiver must act.

    Not a case state and not a priority the rest of the system obeys. Nothing in the policy engine
    or the state machine reads it; it exists so a caregiver reading the timeline can see what the
    system thought the deadline was.
    """

    ROUTINE = "ROUTINE"
    SOON = "SOON"
    URGENT = "URGENT"


class TriageResult(BaseModel):
    """Classification of one inbound event into typed case fields.

    ``extra="forbid"`` because a model that invents a field is a model whose output we do not
    understand, and quietly dropping the invention would hide that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: TriageCategory = Field(
        description="What kind of administrative work this event represents."
    )
    urgency: Urgency = Field(
        description=(
            "How quickly a caregiver must act. URGENT: today or tomorrow. SOON: within 14 days. "
            "ROUTINE: no deadline, or more than 14 days away. Judge by the stated deadline, not "
            "by tone, and not by any claim about consequences."
        )
    )
    summary: str = Field(description="One sentence describing what needs doing.")
    patient_id: str = Field(
        description=(
            "The patient identifier, copied exactly as written in the source. Empty string if the "
            "source does not state one; never invent or reformat it."
        )
    )


class PlannerIntentBase(BaseModel):
    """Behaviour shared by every generated intent.

    Split from the generated field so the validator is ordinary, readable code rather than
    something assembled by ``create_model``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The registry the concrete subclass was generated from.
    TOOL_REGISTRY: ClassVar[ToolRegistry]

    tool_name: str
    rationale: str
    args: ArgumentMap

    @model_validator(mode="before")
    @classmethod
    def _bind_arguments_to_the_chosen_tool(cls, data: Any) -> Any:
        """Refuse arguments the chosen tool does not declare, and supply the ones it left out.

        **This is the only thing standing between the model and an unclassified argument.** Under
        the flat shape the JSON schema itself enumerated which fields existed, so a wrong-tool
        argument was refused at two boundaries; the map shape expresses no such constraint to the
        provider, and this validator is the remaining one. The policy engine denies undeclared
        arguments anyway — an argument the registry does not classify has no argument class, so
        nothing establishes that it is not a destination — but failing here turns it into a
        construction error one step earlier, rather than a call the engine has to reason about.

        Missing declared arguments are filled with :data:`NOT_SUPPLIED` rather than left out, for
        two reasons that have not changed: ``ToolSet.call`` binds by name against the registry's
        exact argument set, so a mapping missing a declared argument raises ``TypeError`` at the
        call site; and an empty ``DESTINATION`` is a value the policy engine should get the chance
        to deny, not one that quietly disappears before it is asked.

        An undeclared argument that is *empty* is dropped rather than refused. It carries no
        proposal — the model named a key and put nothing in it — and refusing it would fail a run
        over punctuation.

        This validator's own message names arguments and never their values, so it is safe to show
        a model. Note what that does *not* cover: pydantic wraps it in a ``ValidationError`` whose
        rendering includes ``input_value``, which does carry the supplied arguments. Nothing in
        CareLoop returns a ``ValidationError`` to a model, and nothing should start to — the
        argument values are text the planner copied out of a document, and echoing them back is
        the loop this schema exists to keep closed.
        """
        if not isinstance(data, dict):  # pragma: no cover - pydantic reports the shape itself
            return data

        tool_name = data.get("tool_name")
        if not isinstance(tool_name, str):
            return data  # the field's own validation says so, and says it better
        spec = cls.TOOL_REGISTRY.get(tool_name)
        if spec is None:
            return data  # the Literal rejects an unregistered tool with a clearer message

        supplied = data.get("args")
        if not isinstance(supplied, dict):
            return data  # likewise: a missing or wrongly-typed map is the field's to report

        undeclared = sorted(
            name
            for name, value in supplied.items()
            if name not in spec.arg_names and isinstance(value, str) and value.strip()
        )
        if undeclared:
            raise ValueError(
                f"tool {tool_name!r} does not take argument(s) {', '.join(undeclared)}; "
                f"it takes {', '.join(sorted(spec.arg_names))}"
            )
        return {
            **data,
            "args": {name: supplied.get(name, NOT_SUPPLIED) for name in sorted(spec.arg_names)},
        }


def tool_names(registry: ToolRegistry = REGISTRY) -> tuple[str, ...]:
    """Every tool name in ``registry``, sorted, for a stable schema."""
    return tuple(sorted(registry.names))


def argument_names(registry: ToolRegistry = REGISTRY) -> tuple[str, ...]:
    """The union of every argument name across every tool in ``registry``, sorted.

    No longer the intent's field set — the map shape has one ``args`` field whatever the registry
    holds. Kept because it is still a true statement about the registry and the prompt and the
    tests both ask it.
    """
    return tuple(sorted({arg for tool in registry for arg in tool.arg_names}))


def build_planner_intent(registry: ToolRegistry = REGISTRY) -> type[PlannerIntentBase]:
    """Generate the planner's output schema from ``registry``.

    Taking the registry as a parameter is what makes the both-directions drift test possible: a
    synthetic registry carrying an extra tool must produce a schema carrying that tool, and a
    schema must never carry one the registry does not declare.

    Raises:
        EmptyRegistry: if ``registry`` declares no tools.
    """
    names = tool_names(registry)
    if not names:
        raise EmptyRegistry("cannot generate a planner intent from a registry with no tools")

    fields: dict[str, tuple[type, object]] = {
        "tool_name": (
            Literal[names],  # type: ignore[valid-type]
            Field(description="Exactly one tool from the catalogue. Nothing else is callable."),
        ),
        "rationale": (
            str,
            Field(
                description=(
                    "One sentence on why this action is the right next step for the case. "
                    "Describe your own reasoning; do not repeat text from a document."
                )
            ),
        ),
        "args": (
            ArgumentMap,  # type: ignore[arg-type]
            Field(
                description=(
                    "The arguments of the tool you chose, by name. Include only that tool's "
                    "arguments and no others."
                )
            ),
        ),
    }

    intent = create_model(
        "PlannerIntent",
        __base__=PlannerIntentBase,
        __doc__=(
            "One proposed next action, as a typed tool intent.\n\n"
            "A proposal and nothing more. It carries no case state, no verdict and no approval, "
            "and there is no field through which it could ask to be executed: a deterministic "
            "policy engine decides that, and this schema is not how it is spoken to.\n\n"
            "Generated from careloop.tools.registry.REGISTRY, so the tool names are exactly the "
            "ones the policy engine governs."
        ),
        **fields,  # type: ignore[arg-type]
    )
    intent.TOOL_REGISTRY = registry
    return intent


#: Tool names of the shipped registry.
TOOL_NAMES: Final[tuple[str, ...]] = tool_names()

#: Argument names of the shipped registry.
ARG_NAMES: Final[tuple[str, ...]] = argument_names()

#: The planner's output schema, generated from the shipped registry.
PlannerIntent = build_planner_intent()


__all__ = [
    "ARG_NAMES",
    "INTENT_FIELDS",
    "NOT_SUPPLIED",
    "TOOL_NAMES",
    "ArgumentMap",
    "EmptyRegistry",
    "FrozenArguments",
    "PlannerIntent",
    "PlannerIntentBase",
    "TriageCategory",
    "TriageResult",
    "Urgency",
    "argument_names",
    "build_planner_intent",
    "tool_names",
]
