"""What the two agents are allowed to say.

An agent's output schema is the narrowest place to enforce that the LLM never holds authority: a
field that cannot be expressed cannot be decided. So nothing here carries a case state, a policy
verdict, an approval, a priority override, or any other lever over what happens next.

**Two shapes, and the distinction is the point of this module.**

:data:`PROPOSAL_TOOLS` is what a *model* sees: one closed schema per registered action, generated
from :data:`careloop.tools.registry.REGISTRY`, offered to a provider as tool specifications and
never executed. :class:`PlannerIntent` is what *CareLoop* passes around: the canonical
``(tool_name, args, rationale)`` the timeline, the policy engine, approvals, idempotency and the
executor have always consumed. :meth:`ProposalToolBase.canonical` is the one seam between them,
and :func:`proposal_tool_call_for` is its inverse so the round trip can be tested.

**Schema-valid is not trusted and is not authorised.** Nothing in this module decides anything. A
proposal that satisfies its schema perfectly may name an attacker's address, an unvalidated slot or
a portal no evidence supports; the closed schema constrains *shape* and says nothing about values.
Trust is resolved from ``case.facts`` under each argument's declared ``trust_key``, and authority
comes from the deterministic policy engine. Both still run, unchanged, on everything produced here.

**Why the model is offered the actions rather than one wrapper.** The shape here until this session
was a single ``PlannerIntent`` tool carrying ``tool_name``, ``rationale`` and an open
``dict[str, str]`` of arguments. Measured against the deployed model, that failed 25 times out of
25 — and not in the way anyone expected. The provider was handed one tool whose description
described an *object*, and Nova 2 Lite answered by emitting a ``toolUse`` for ``book_appointment``,
a tool it took from the prose catalogue in the system prompt and which was never offered. The SDK
then raised a bare ``ValueError`` saying no valid tool use was found. Isolated one variable at a
time: not the schema shape (a two-field closed schema failed identically), not the catalogue
(removing it changed nothing), not the tool's name (renaming it changed nothing) — the tool's
**description**, which never told the model to call it.

Offering the actions themselves as ``propose_`` tools removes the mismatch rather than papering
over it: 118 of 118 valid across safe, adversarial, approval and replan shapes, zero phantom calls,
zero missing ``rationale`` including on long ``send_email`` bodies where the wrapper dropped it 2
times in 25. Full detail in ``docs/live-loop-findings.md``.

**Strict constrained decoding is not available and is not relied on.** ``ToolSpecification.strict``
exists in the Bedrock API and every Nova model refuses it — ``This model doesn't support the strict
field`` — so the schemas here are closed *structurally* and every answer is still validated by this
layer. :func:`careloop.agents.invoke.proposal_tool_call` is where zero calls, several calls, an
unoffered tool and arguments that do not satisfy a schema are each refused, by name, with nothing
manufactured from the wreckage.

**A discriminated union was the obvious answer and does not work here.** ``strands-agents==1.51.0``
converts a Pydantic model to a Bedrock tool spec through ``convert_pydantic_to_tool_spec``, which
drops ``$defs``: a union in a wrapper object reaches the provider as ``oneOf`` over five dangling
``$ref`` pointers, and a ``RootModel`` union reaches it as ``{"type": "object", "properties": {}}``
— an empty schema, with nothing raised. Both shapes are kept in ``bench/intent_shapes.py`` as the
vacuity proof for the guard in ``tests/test_provider_tool_spec.py``.

**Triage emits no destination.** There is no ``provider_id``, no recipient, no portal on
:class:`TriageResult`, and its absence is load-bearing rather than an omission. The policy engine
resolves an argument's trust by looking its value up in ``case.facts`` **under the fact key the
argument declares**, and taking the **lowest** trust level among the matches. A triage-emitted
``UNTRUSTED`` ``provider_id`` would therefore drag the configured one down to ``UNTRUSTED`` and
deny the action it was needed for — a *same-key* collision, which is exactly the case lowest-wins
is meant to catch and cannot be relaxed for. Triage produces ``VALUE``-class candidates only;
destinations come from trusted configuration and nowhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
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


#: Prefix distinguishing a *proposal* tool from the action it proposes.
#:
#: The model is offered ``propose_send_email``; the registry declares ``send_email``. The two are
#: deliberately not the same string, so that nothing downstream can mistake the thing the model
#: called for the thing that would have an effect, and so a proposal tool name can never be passed
#: to :class:`~careloop.tools.callables.ToolSet` by accident — it is not a registered tool.
PROPOSAL_TOOL_PREFIX: Final[str] = "propose_"


def proposal_tool_name(tool_name: str) -> str:
    """The proposal tool that proposes ``tool_name``."""
    return f"{PROPOSAL_TOOL_PREFIX}{tool_name}"


def proposed_tool_name(proposal_tool: str) -> str:
    """The action ``proposal_tool`` proposes.

    Raises:
        ValueError: if the name is not a proposal tool name. There is no lenient reading: a name
            without the prefix is not something this layer knows how to canonicalise, and guessing
            would be the "silent JSON repair" the contract forbids one level up.
    """
    if not proposal_tool.startswith(PROPOSAL_TOOL_PREFIX):
        raise ValueError(f"{proposal_tool!r} is not a proposal tool name")
    return proposal_tool[len(PROPOSAL_TOOL_PREFIX) :]


class ProposalToolBase(BaseModel):
    """One closed, tool-specific proposal schema. **Presented to a model; never executed.**

    This is a *schema*, not a callable. Nothing in CareLoop binds a body to it, no executor
    dispatches it, and the policy engine has never heard of it — it exists so a provider can be
    handed a closed argument surface instead of an open string map, and so the model's answer can
    be validated structurally before anything reads it as a proposal.

    **Schema-valid is not trusted and not authorised.** Satisfying this model says the shape is
    right and says nothing whatever about the values: a perfectly valid instance may name an
    attacker's address, an unvalidated slot or a portal no evidence supports. Trust is resolved
    from ``case.facts`` under each argument's declared ``trust_key``, and authority comes from the
    deterministic policy engine, which sees only the canonical proposal this produces. Neither of
    those layers is weakened, consulted or short-circuited here.

    ``extra="forbid"`` and every declared argument required, because the two failure modes measured
    against a real provider were an invented argument and an omitted one. Both are rejected here as
    construction errors rather than travelling on as a proposal nobody can classify.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The registry spec this schema was generated from.
    TOOL: ClassVar[Any]

    rationale: str

    @property
    def tool_name(self) -> str:
        """The registered action this proposes. Read from the spec, never from model output."""
        return str(self.TOOL.name)

    def canonical(self) -> PlannerIntentBase:
        """Canonicalise to the existing ``(tool_name, args, rationale)`` representation.

        **This is the whole compatibility seam.** Everything downstream — the ``PLAN_PROPOSED``
        payload, :class:`careloop.graph.context.Proposal`, the policy engine, ``args_digest``,
        approvals, idempotency, ``ToolSet``, verification, the API — receives exactly what it
        received before, because this returns exactly what the planner used to return.

        The argument names come from the registry, not from the model's answer: the schema was
        generated from ``spec.arg_names`` and forbids extras, so the two agree by construction.
        Rebuilding the map from the spec rather than from ``model_dump()`` keeps that true even if
        a future edit adds a non-argument field to a generated schema.
        """
        dumped = self.model_dump()
        return PlannerIntent(
            tool_name=self.tool_name,
            rationale=self.rationale,
            args={name: dumped[name] for name in sorted(self.TOOL.arg_names)},
        )


def build_proposal_tool(spec: Any) -> type[ProposalToolBase]:
    """Generate the closed proposal schema for one registered tool.

    One required ``str`` per declared argument plus a required ``rationale``, and nothing else can
    be expressed. The description of each field is the registry's own, so the catalogue the model
    reads and the schema it fills come from one source.
    """
    fields: dict[str, tuple[type, object]] = {
        "rationale": (
            str,
            Field(
                description=(
                    "One sentence on why this action is the right next step for the case. "
                    "Describe your own reasoning; do not repeat text from a document."
                )
            ),
        )
    }
    for arg in sorted(spec.args, key=lambda declared: declared.name):
        fields[arg.name] = (
            str,
            Field(description=arg.description or f"Argument {arg.name!r} of {spec.name}."),
        )

    model = create_model(
        proposal_tool_name(spec.name),
        __base__=ProposalToolBase,
        __doc__=(
            f"Propose {spec.name}. {spec.description} "
            f"Calling this records a proposal only; nothing is carried out because you called it, "
            f"and a deterministic policy engine you cannot see decides what happens next."
        ),
        **fields,  # type: ignore[arg-type]
    )
    model.TOOL = spec
    return model


def proposal_tool_call_for(
    intent: PlannerIntentBase, tools: Mapping[str, type[ProposalToolBase]] | None = None
) -> ProposalToolBase:
    """The proposal-tool instance a model would have called to produce ``intent``.

    The inverse of :meth:`ProposalToolBase.canonical`, and the reason it exists is that a seam with
    only one direction cannot be proven faithful. ``tests/test_proposal_tools.py`` round-trips every
    tool through both directions and asserts equality, so "canonicalisation changes nothing" is a
    property with a test rather than a claim in a docstring.

    Used by the offline harnesses — the scripted mock, the adversarial replay, the demo scripts —
    which express what the planner should propose in canonical terms and need it back in the
    model-facing shape. Nothing in the production path calls it: the model produces the proposal
    tool call itself.

    Raises:
        KeyError: if no proposal tool proposes ``intent.tool_name``.
    """
    catalogue = PROPOSAL_TOOLS if tools is None else tools
    schema = catalogue[proposal_tool_name(intent.tool_name)]
    return schema(rationale=intent.rationale, **dict(intent.args))


def build_proposal_tools(registry: ToolRegistry = REGISTRY) -> dict[str, type[ProposalToolBase]]:
    """Every proposal schema, keyed by proposal tool name.

    Generated from ``registry``, so the tools a planner may propose and the tools the policy engine
    governs cannot drift: one source of truth, checked in both directions by
    ``tests/test_proposal_tools.py``.

    Raises:
        EmptyRegistry: if ``registry`` declares no tools.
    """
    if not len(registry):
        raise EmptyRegistry("cannot generate proposal tools from a registry with no tools")
    return {proposal_tool_name(spec.name): build_proposal_tool(spec) for spec in registry}


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

#: The proposal tools the planner offers a model, generated from the shipped registry.
#:
#: One per registered action, keyed by proposal tool name. These are the *only* tool specifications
#: CareLoop ever hands a provider, and not one of them can be executed: no body is bound to them,
#: the executor does not know them, and their names are not registry names.
PROPOSAL_TOOLS: Final[dict[str, type[ProposalToolBase]]] = build_proposal_tools()

#: The proposal tool names, sorted.
PROPOSAL_TOOL_NAMES: Final[tuple[str, ...]] = tuple(sorted(PROPOSAL_TOOLS))


__all__ = [
    "ARG_NAMES",
    "INTENT_FIELDS",
    "NOT_SUPPLIED",
    "PROPOSAL_TOOLS",
    "PROPOSAL_TOOL_NAMES",
    "PROPOSAL_TOOL_PREFIX",
    "TOOL_NAMES",
    "ArgumentMap",
    "EmptyRegistry",
    "FrozenArguments",
    "PlannerIntent",
    "PlannerIntentBase",
    "ProposalToolBase",
    "TriageCategory",
    "TriageResult",
    "Urgency",
    "argument_names",
    "build_planner_intent",
    "build_proposal_tool",
    "build_proposal_tools",
    "proposal_tool_call_for",
    "proposal_tool_name",
    "proposed_tool_name",
    "tool_names",
]
