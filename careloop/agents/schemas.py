"""What the two agents are allowed to say.

An agent's output schema is the narrowest place to enforce that the LLM never holds authority: a
field that cannot be expressed cannot be decided. So neither schema here carries a case state, a
policy verdict, an approval, a priority override, or any other lever over what happens next.
:class:`PlannerIntent` in particular is a *proposal* — a tool name, its arguments, and a sentence
of reasoning — and there is deliberately no field through which it could say "and execute this".
That is checked by enumeration rather than by inspection: the intent's field set is exactly
``INTENT_FIELDS`` plus the argument names the tool registry declares, and a test pins it.

**``PlannerIntent`` is generated from the registry.** Tool names and argument fields come from
:data:`careloop.tools.registry.REGISTRY`, so they cannot drift from the tools the policy engine
governs. A planner offered a tool the engine has never classified is a planner proposing effects
nothing decides the authority of.

**Why the flat, all-required, all-``str`` argument shape.** Optional fields and ``X | None`` unions
render as nullable JSON-schema types that some strict structured-output implementations reject,
and a rejection arrives looking like a model failure when the cause was the schema. Every argument
field is therefore a required ``str`` and :data:`NOT_SUPPLIED` — the empty string — means "not
supplied". The benchmark measured this shape; ``docs/model-benchmark.md`` has the numbers.

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
from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from careloop.tools.registry import REGISTRY, ToolRegistry

#: The empty string, meaning "the source did not state this" or "this tool does not take it".
NOT_SUPPLIED: Final[str] = ""

#: Fields every :class:`PlannerIntent` carries regardless of which tools exist. An argument name
#: colliding with one of these would silently shadow it, so :func:`build_planner_intent` refuses.
INTENT_FIELDS: Final[tuple[str, ...]] = ("tool_name", "rationale")


class ReservedArgumentName(Exception):
    """Raised when a registry declares an argument whose name is already an intent field."""


class EmptyRegistry(Exception):
    """Raised when an intent is generated from a registry declaring no tools.

    A planner with no callable tools has nothing to propose. Failing here is louder than emitting
    a schema whose ``tool_name`` can never be satisfied.
    """


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

    Split from the generated fields so the validator and the :attr:`args` accessor are ordinary,
    readable code rather than something assembled by ``create_model``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The registry the concrete subclass was generated from.
    TOOL_REGISTRY: ClassVar[ToolRegistry]

    #: Names of the generated argument fields — every field that is not in ``INTENT_FIELDS``.
    ARGUMENT_FIELDS: ClassVar[frozenset[str]]

    tool_name: str
    rationale: str

    @model_validator(mode="after")
    def _reject_arguments_the_chosen_tool_does_not_declare(self) -> PlannerIntentBase:
        """Refuse an intent that supplies an argument its tool has never declared.

        The flat schema carries every argument of every tool, so ``fetch_document`` still has a
        ``to`` field. Filling it is not harmless: the policy engine denies undeclared arguments
        because an argument the registry does not classify has no argument class, and therefore
        nothing establishes that it is not a destination. Rejecting here fails closed one step
        earlier, and turns "an undeclared argument" into a construction error rather than a call
        the engine has to reason about.

        This validator's own message names fields and never their values, so it is safe to show a
        model. Note what that does *not* cover: pydantic wraps it in a ``ValidationError`` whose
        rendering includes ``input_value``, which does carry the supplied arguments. Nothing in
        CareLoop returns a ``ValidationError`` to a model, and nothing should start to — the
        argument values are text the planner copied out of a document, and echoing them back is
        the loop this schema exists to keep closed.
        """
        spec = self.TOOL_REGISTRY.get(self.tool_name)
        if spec is None:  # pragma: no cover - Literal already rejects unregistered names
            raise ValueError(f"{self.tool_name!r} is not a registered tool")

        supplied = sorted(
            name
            for name in self.ARGUMENT_FIELDS
            if name not in spec.arg_names and getattr(self, name).strip()
        )
        if supplied:
            raise ValueError(
                f"tool {self.tool_name!r} does not take argument(s) {', '.join(supplied)}; "
                f"it takes {', '.join(sorted(spec.arg_names))}"
            )
        return self

    @property
    def args(self) -> dict[str, str]:
        """Every argument the chosen tool declares, including the ones left empty.

        Empty strings are kept rather than dropped. ``ToolSet.call`` binds by name against the
        registry's exact argument set, so a mapping missing a declared-but-unsupplied argument
        raises ``TypeError`` at the call site; and an empty ``DESTINATION`` is a value the policy
        engine should get the chance to deny, not one that quietly disappears before it is asked.
        """
        spec = self.TOOL_REGISTRY.get(self.tool_name)
        if spec is None:  # pragma: no cover - construction already guaranteed otherwise
            raise ValueError(f"{self.tool_name!r} is not a registered tool")
        return {name: getattr(self, name) for name in sorted(spec.arg_names)}


def tool_names(registry: ToolRegistry = REGISTRY) -> tuple[str, ...]:
    """Every tool name in ``registry``, sorted, for a stable schema."""
    return tuple(sorted(registry.names))


def argument_names(registry: ToolRegistry = REGISTRY) -> tuple[str, ...]:
    """The union of every argument name across every tool in ``registry``, sorted."""
    return tuple(sorted({arg for tool in registry for arg in tool.arg_names}))


def build_planner_intent(registry: ToolRegistry = REGISTRY) -> type[PlannerIntentBase]:
    """Generate the planner's output schema from ``registry``.

    Taking the registry as a parameter is what makes the both-directions drift test possible: a
    synthetic registry carrying an extra tool must produce a schema carrying that tool, and a
    schema must never carry one the registry does not declare.

    Raises:
        EmptyRegistry: if ``registry`` declares no tools.
        ReservedArgumentName: if an argument name collides with an intent field.
    """
    names = tool_names(registry)
    if not names:
        raise EmptyRegistry("cannot generate a planner intent from a registry with no tools")

    args = argument_names(registry)
    reserved = sorted(set(args) & set(INTENT_FIELDS))
    if reserved:
        raise ReservedArgumentName(
            f"argument name(s) {', '.join(reserved)} collide with intent field(s) "
            f"{', '.join(INTENT_FIELDS)}"
        )

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
    }
    for arg in args:
        owners = ", ".join(sorted(tool.name for tool in registry if arg in tool.arg_names))
        fields[arg] = (
            str,
            Field(
                description=(
                    f"Argument '{arg}' of {owners}. Leave it as the empty string unless the tool "
                    f"you chose takes it."
                )
            ),
        )

    intent = create_model(
        "PlannerIntent",
        __base__=PlannerIntentBase,
        __doc__=(
            "One proposed next action, as a typed tool intent.\n\n"
            "A proposal and nothing more. It carries no case state, no verdict and no approval, "
            "and there is no field through which it could ask to be executed: a deterministic "
            "policy engine decides that, and this schema is not how it is spoken to.\n\n"
            "Generated from careloop.tools.registry.REGISTRY, so the tool names and argument "
            "fields are exactly the ones the policy engine governs."
        ),
        **fields,  # type: ignore[arg-type]
    )
    intent.TOOL_REGISTRY = registry
    intent.ARGUMENT_FIELDS = frozenset(args)
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
    "EmptyRegistry",
    "PlannerIntent",
    "PlannerIntentBase",
    "ReservedArgumentName",
    "TriageCategory",
    "TriageResult",
    "Urgency",
    "argument_names",
    "build_planner_intent",
    "tool_names",
]
