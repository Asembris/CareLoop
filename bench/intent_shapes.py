"""Candidate shapes for the planner's output schema, and the instrument that compares them.

Session A established that Nova 2 Lite produces a valid ``PlannerIntent`` essentially never on the
real graph's prompt: it emits a well-formed intent for the tool it chose and omits the seven
argument fields belonging to the tools it did not. The question this module exists to answer is
whether that is a property of the *model* or a property of the *schema*, and the only instrument
that can answer it holds the provider constant and varies nothing but the shape.

**Why the captured prompt.** Session A's refuted bisect is the reason: a bisect on a synthetic
input measures the synthetic input. Every shape here is measured against
``docs/live-runs/captured-planner-prompt.json`` — the prompt the real graph really sent, captured
from a live run rather than reconstructed.

**Why ``gpt-4o-mini`` cannot answer this question.** ``strands.models.openai`` reaches structured
output through ``client.beta.chat.completions.parse(response_format=...)``, which is OpenAI's
*strict* Structured Outputs: decoding is constrained so that every required field must be emitted.
A model on that path physically cannot reproduce Nova's failure, so its success measures the
provider's constraint rather than the schema's fit. It is measured anyway — "does swapping the
model unblock the demo" is a real question about the wired fallback — but it is reported with that
caveat attached to the number rather than beside it.

**The union is here to fail, and it fails offline.** :data:`UNION_WRAPPED` and
:data:`UNION_ROOTMODEL` are not candidates. They are the vacuity proof for
:func:`tool_spec_defects`, kept because durable offline evidence beats one measured failure — see
that function for what the SDK does to them, and note that the ``RootModel`` case produces an
*empty* schema with nothing raised, which is how a planner ships broken and never says why.

This module is a measuring instrument and lives in ``bench/`` for the reason that package's
docstring gives. ``careloop`` may never import it.
"""

from __future__ import annotations

import json
import pathlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, create_model
from strands.models import Model
from strands.tools.structured_output.structured_output_utils import convert_pydantic_to_tool_spec

from careloop.agents.invoke import NoStructuredOutput, WrongStructuredOutputType, structured_call
from careloop.tools.registry import REGISTRY, ToolRegistry

ROOT: Final[pathlib.Path] = pathlib.Path(__file__).parent.parent.resolve()

#: The prompt the real graph really sent, captured from a live run in session A.
CAPTURED_PROMPT_PATH: Final[pathlib.Path] = (
    ROOT / "docs" / "live-runs" / "captured-planner-prompt.json"
)

#: The sentence in the captured system prompt that is specific to the flat all-required shape.
#: Shapes that do not have twelve argument fields are told something else in ``adapted`` mode.
FLAT_SPECIFIC_SENTENCE: Final[str] = (
    "Fill in only the arguments belonging to the tool you chose and leave every other argument "
    "as the empty string."
)

#: What counts as "the provider produced nothing that satisfies the schema", as opposed to a
#: proposal that is merely wrong. Mirrors ``bench.live.STRUCTURED_OUTPUT_FAILURES``; a
#: ``ValidationError`` is a model failing to satisfy a schema and is exactly what is being measured.
SCHEMA_FAILURES: Final[tuple[type[BaseException], ...]] = (
    NoStructuredOutput,
    WrongStructuredOutputType,
    ValidationError,
)


def captured_prompt(path: pathlib.Path | None = None) -> tuple[str, str]:
    """``(system, user)`` as the graph really sent them. Read, never reconstructed."""
    payload = json.loads((path or CAPTURED_PROMPT_PATH).read_text(encoding="utf-8"))
    return payload["system"], payload["user"]


# ---------------------------------------------------------------------------------------------
# What the provider is actually handed
# ---------------------------------------------------------------------------------------------


def provider_tool_spec(schema: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema Bedrock receives for ``schema``.

    ``model_json_schema()`` is **not** this. ``BedrockModel.structured_output`` passes the model
    through ``convert_pydantic_to_tool_spec``, whose ``_flatten_schema`` keeps ``type``,
    ``properties``, ``title``, ``description`` and ``required`` and drops everything else —
    ``$defs`` included. What survives that is what ships.
    """
    return dict(convert_pydantic_to_tool_spec(schema)["inputSchema"]["json"])


def tool_spec_defects(schema: type[BaseModel]) -> list[str]:
    """Ways the schema a provider receives fails to describe ``schema``. Empty means sound.

    Every defect here was observed against ``strands-agents==1.51.0`` while choosing this
    session's shape, and each is silent — the conversion raises nothing in any of these cases:

    * **No properties at all.** ``RootModel[<discriminated union>]`` flattens to
      ``{"type": "object", "properties": {}}``. The provider is told the tool takes no arguments,
      the model dutifully sends none, and the failure surfaces as a validation error about the
      model rather than about the schema.
    * **Dangling ``$ref``.** A discriminated union inside a wrapper object survives as ``oneOf``
      with ``#/$defs/...`` pointers, while ``$defs`` itself is dropped. The provider receives
      references to definitions it was never given.
    * **A required field that is not a property**, which no reader of the schema can satisfy.
    * **No required fields**, on a schema whose model requires some — the constraint the planner
      depends on has been flattened away.
    """
    spec = provider_tool_spec(schema)
    properties = spec.get("properties") or {}
    required = spec.get("required") or []
    model_required = sorted(
        name for name, info in schema.model_fields.items() if info.is_required()
    )

    defects: list[str] = []
    if not properties:
        defects.append("the provider receives a schema with no properties at all")
    if "$ref" in json.dumps(spec):
        defects.append("the provider receives a $ref with no $defs to resolve it against")
    unsatisfiable = sorted(set(required) - set(properties))
    if unsatisfiable:
        defects.append(f"required names that are not properties: {unsatisfiable}")
    if model_required and not required:
        defects.append(
            f"the model requires {model_required} but the provider is told nothing is required"
        )
    return defects


# ---------------------------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------------------------


def _tool_names(registry: ToolRegistry) -> tuple[str, ...]:
    return tuple(sorted(registry.names))


def _class_name(tool_name: str) -> str:
    return "".join(part.title() for part in tool_name.split("_")) + "Intent"


def build_flat_args(registry: ToolRegistry = REGISTRY) -> type[BaseModel]:
    """Shape A — **FROZEN AS MEASURED.** One required ``str`` per argument of every tool.

    This is what ``careloop.agents.schemas.PlannerIntent`` was before session B reshaped it, and
    it is rebuilt here rather than imported. The first draft of this module imported the shipped
    schema as the control, which meant that the moment the shipped schema was fixed the control
    silently became a copy of the winner and the shootout could no longer reproduce its own
    result. A control that tracks the thing under test is not a control.

    Same precedent as ``bench/schemas.py``: what was measured is frozen where it was measured.
    ``docs/live-runs/step1-shipped-schema.json`` is the run this shape produced.
    """
    args = tuple(sorted({arg for tool in registry for arg in tool.arg_names}))
    fields: dict[str, tuple[type, object]] = {
        "tool_name": (
            Literal[_tool_names(registry)],  # type: ignore[valid-type]
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
    return create_model(
        "FlatIntent",
        __config__=ConfigDict(frozen=True, extra="forbid"),
        __doc__="One proposed next action, as a typed tool intent.",
        **fields,  # type: ignore[arg-type]
    )


def build_dict_args(registry: ToolRegistry = REGISTRY) -> type[BaseModel]:
    """Shape B — ``tool_name``, ``rationale`` and a string map of only the chosen tool's arguments.

    Three required fields, all of which the model intends to fill. Which arguments belong to which
    tool stops being expressed in the JSON schema and is enforced by a validator instead; the
    ``tool_name`` enumeration is unaffected.
    """

    class DictArgsIntent(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        tool_name: Literal[_tool_names(registry)] = Field(  # type: ignore[valid-type]
            description="Exactly one tool from the catalogue. Nothing else is callable."
        )
        rationale: str = Field(
            description="One sentence on why this action is the right next step for the case."
        )
        args: dict[str, str] = Field(
            description=(
                "The arguments of the tool you chose, by name. Include only that tool's "
                "arguments and no others."
            )
        )

    return DictArgsIntent


class NamedArgument(BaseModel):
    """One argument, as a name and a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="The argument's name, exactly as the catalogue spells it.")
    value: str = Field(description="The value to pass.")


def build_list_args(registry: ToolRegistry = REGISTRY) -> type[BaseModel]:
    """Shape D — the same three required fields, with the arguments as a list of name/value pairs.

    Included because an array of objects and a free-form string map are handled differently by
    different providers, and which one a model finds easier is a measurement rather than a guess.
    """

    class ListArgsIntent(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        tool_name: Literal[_tool_names(registry)] = Field(  # type: ignore[valid-type]
            description="Exactly one tool from the catalogue. Nothing else is callable."
        )
        rationale: str = Field(
            description="One sentence on why this action is the right next step for the case."
        )
        args: list[NamedArgument] = Field(
            description="The arguments of the tool you chose. Include no others."
        )

    return ListArgsIntent


def build_tool_choice(registry: ToolRegistry = REGISTRY) -> type[BaseModel]:
    """Shape E, first call — which tool, and why. No arguments at all.

    **This is not a proposal.** It names a tool and nothing downstream may treat it as an intent:
    it carries no arguments, so there is nothing for a policy engine to decide the authority of.
    """

    class ToolChoice(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        tool_name: Literal[_tool_names(registry)] = Field(  # type: ignore[valid-type]
            description="Exactly one tool from the catalogue. Nothing else is callable."
        )
        rationale: str = Field(
            description="One sentence on why this action is the right next step for the case."
        )

    return ToolChoice


def build_per_tool_arguments(tool_name: str, registry: ToolRegistry = REGISTRY) -> type[BaseModel]:
    """Shape E, second call — a flat schema carrying exactly one tool's arguments, all required.

    Every required field is one the model intends to fill, which is the property the flat
    all-tools shape lost. Per-argument descriptions survive, which the map and list shapes lose.
    """
    spec = registry.get(tool_name)
    if spec is None:
        raise KeyError(f"{tool_name!r} is not a registered tool")

    fields: dict[str, tuple[type, object]] = {
        arg: (str, Field(description=f"Argument {arg!r} of {tool_name}."))
        for arg in sorted(spec.arg_names)
    }
    return create_model(
        f"{_class_name(tool_name)}Arguments",
        __config__=ConfigDict(frozen=True, extra="forbid"),
        __doc__=f"The arguments of {tool_name}.",
        **fields,  # type: ignore[arg-type]
    )


def _build_union_variants(registry: ToolRegistry = REGISTRY) -> tuple[type[BaseModel], ...]:
    variants: list[type[BaseModel]] = []
    for tool in sorted(registry, key=lambda spec: spec.name):
        fields: dict[str, tuple[type, object]] = {
            "tool_name": (Literal[tool.name], Field(...)),  # type: ignore[valid-type]
            "rationale": (str, Field(description="Why this is the right next step.")),
        }
        for arg in sorted(tool.arg_names):
            fields[arg] = (str, Field(description=f"Argument {arg!r} of {tool.name}."))
        variants.append(
            create_model(
                _class_name(tool.name),
                __config__=ConfigDict(frozen=True, extra="forbid"),
                **fields,  # type: ignore[arg-type]
            )
        )
    return tuple(variants)


_VARIANTS = _build_union_variants()
_UNION = Annotated[Union[_VARIANTS], Field(discriminator="tool_name")]  # type: ignore[valid-type]  # noqa: UP007


class UnionWrapped(BaseModel):
    """The discriminated union in a wrapper object. **Not a candidate — a vacuity fixture.**

    Pydantic renders this as ``oneOf`` over five ``#/$defs/...`` references. The SDK's flattener
    keeps the ``oneOf`` verbatim and drops ``$defs``, so the provider receives five pointers to
    definitions it was never given. Nothing raises.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: _UNION  # type: ignore[valid-type]


#: The same union as a ``RootModel``, and the worse half of the evidence: this flattens to
#: ``{"type": "object", "properties": {}}``. An empty schema, no exception, and a planner that
#: would be told its one tool takes no arguments. **Not a candidate.**
UnionRootModel = RootModel[_UNION]  # type: ignore[valid-type]


# ---------------------------------------------------------------------------------------------
# Normalising every shape onto one answer
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """What a shape produced, reduced to the only two things the graph needs.

    Every candidate is compared on this, so a shape cannot look better by answering an easier
    question.
    """

    tool_name: str
    args: dict[str, str]


class ArgumentDefect(Exception):
    """Raised when output satisfied the schema but named arguments the chosen tool does not take.

    Deliberately distinct from a schema failure. A model that emits a syntactically valid intent
    naming ``to`` on ``book_appointment`` has not failed to satisfy the schema; it has proposed
    something the registry does not describe, and the two are different findings.
    """


def normalise(
    tool_name: str, supplied: Mapping[str, str], registry: ToolRegistry = REGISTRY
) -> Proposal:
    """Reduce raw supplied arguments to exactly what ``ToolSet.call`` binds.

    Missing declared arguments become the empty string, for session 5's delta D6 reason: the
    registry's exact argument set is what binds by name, and an empty ``DESTINATION`` is a value
    the policy engine should get the chance to deny rather than one that disappears.

    Raises:
        ArgumentDefect: if an argument the chosen tool does not declare carries a value.
    """
    spec = registry.get(tool_name)
    if spec is None:  # pragma: no cover - the Literal enumeration rejects this first
        raise ArgumentDefect(f"{tool_name!r} is not a registered tool")

    undeclared = sorted(
        name for name, value in supplied.items() if name not in spec.arg_names and value.strip()
    )
    if undeclared:
        raise ArgumentDefect(
            f"tool {tool_name!r} does not take argument(s) {', '.join(undeclared)}; "
            f"it takes {', '.join(sorted(spec.arg_names))}"
        )
    return Proposal(
        tool_name=tool_name,
        args={name: supplied.get(name, "") for name in sorted(spec.arg_names)},
    )


#: How the planner's user prompt renders one fact: ``| fact <key>: <value>``.
_FACT_LINE_PREFIX: Final[str] = "| fact "


def evidence_facts(user_prompt: str) -> dict[str, str]:
    """The facts the prompt showed the model, by key.

    Parsed out of the rendered prompt rather than reconstructed from a case, because the prompt is
    the only thing the model actually saw and the question is whether it copied what was in front
    of it. Multi-line excerpt facts key on ``excerpt:<doc>:<page>``, which no argument is named
    after, so their continuation lines are ignored without a special case.
    """
    facts: dict[str, str] = {}
    for raw in user_prompt.splitlines():
        line = raw.strip()
        if not line.startswith(_FACT_LINE_PREFIX):
            continue
        key, sep, value = line[len(_FACT_LINE_PREFIX) :].partition(": ")
        if sep:
            facts.setdefault(key.strip(), value.strip())
    return facts


def transcription_defects(proposal: Proposal, facts: Mapping[str, str]) -> list[str]:
    """Arguments whose value disagrees with the fact of the same name. Empty means faithful.

    **Why this is measured at all, and why it is not folded into "valid".** Trust resolves by
    matching an argument's value against ``case.facts`` **exactly** — no case-folding, no trimming,
    no normalisation, because normalisation is a laundering surface (session 2). So a proposal that
    satisfies the schema perfectly and copies ``2026-09-03T09:30:00`` as ``2026-09-03T09:30:00.``
    resolves ``UNTRUSTED`` and is denied ``INSUFFICIENT_FACT_TRUST``. Nothing about that denial
    says "the model added a full stop"; it reads as a policy bug and it is not one.

    It stays a separate column because schema fit is a property of the *shape* and transcription
    fidelity is a property of the *model*. Blending them would let a shape be blamed for a model's
    typing, which is the confusion this whole instrument exists to avoid.

    Only arguments the evidence actually stated a fact for are checked. ``body``, ``subject`` and
    ``reason`` are composed by the planner and are supposed to be new text.
    """
    defects: list[str] = []
    for name, value in sorted(proposal.args.items()):
        if not value.strip() or name not in facts:
            continue
        if value != facts[name]:
            defects.append(name)
    return defects


# ---------------------------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShapeCandidate:
    """One schema shape, and how to ask a model for it."""

    key: str
    title: str
    calls_per_proposal: int
    schemas: tuple[type[BaseModel], ...]
    """Every schema the provider is handed. Shape E hands over two."""

    adapted_instruction: str
    """What replaces the flat-specific sentence when the prompt is adapted for this shape."""

    async def propose(self, model: Model, system_prompt: str, user_prompt: str) -> Proposal:
        raise NotImplementedError  # pragma: no cover - every concrete shape overrides


@dataclass(frozen=True)
class SingleCallShape(ShapeCandidate):
    """A shape that asks once and reads the answer off one instance."""

    read: Any = None
    """``(instance) -> (tool_name, supplied)``."""

    async def propose(self, model: Model, system_prompt: str, user_prompt: str) -> Proposal:
        instance = await structured_call(
            model,
            self.schemas[0],
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        tool_name, supplied = self.read(instance)
        return normalise(tool_name, supplied)


@dataclass(frozen=True)
class TwoCallShape(ShapeCandidate):
    """Shape E — pick the tool, then fill that tool's arguments.

    **One agent making two calls, not a second agent.** Invariant 1 counts LLM agents, and this is
    the planner asking the same model twice; there is no new prompt-owning component, no new
    schema-owning component, and nothing between the two calls that decides anything.
    """

    async def propose(self, model: Model, system_prompt: str, user_prompt: str) -> Proposal:
        choice = await structured_call(
            model,
            self.schemas[0],
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        arguments_schema = build_per_tool_arguments(choice.tool_name)
        filled = await structured_call(
            model,
            arguments_schema,
            system_prompt=system_prompt,
            user_prompt=(
                f"{user_prompt}\n\nYou have chosen {choice.tool_name}. "
                f"Supply its arguments, copying identifiers exactly as written in the evidence."
            ),
        )
        return normalise(choice.tool_name, filled.model_dump())


def _read_flat(instance: Any) -> tuple[str, dict[str, str]]:
    """Every argument field on the instance, foreign ones included; :func:`normalise` sorts it."""
    return instance.tool_name, {
        name: getattr(instance, name)
        for name in instance.__class__.model_fields
        if name not in ("tool_name", "rationale")
    }


def _read_dict_args(instance: Any) -> tuple[str, dict[str, str]]:
    return instance.tool_name, dict(instance.args)


def _read_list_args(instance: Any) -> tuple[str, dict[str, str]]:
    return instance.tool_name, {pair.name: pair.value for pair in instance.args}


def build_candidates(registry: ToolRegistry = REGISTRY) -> tuple[ShapeCandidate, ...]:
    """The shapes to compare, control first."""
    return (
        SingleCallShape(
            key="A_flat",
            title="Frozen as measured: flat, every argument of every tool, all required",
            calls_per_proposal=1,
            schemas=(build_flat_args(registry),),
            adapted_instruction=FLAT_SPECIFIC_SENTENCE,
            read=_read_flat,
        ),
        SingleCallShape(
            key="B_map",
            title="tool_name + rationale + args as a string map",
            calls_per_proposal=1,
            schemas=(build_dict_args(registry),),
            adapted_instruction=(
                "Put the arguments of the tool you chose in the args object, by name, and put no "
                "other tool's arguments there."
            ),
            read=_read_dict_args,
        ),
        SingleCallShape(
            key="D_list",
            title="tool_name + rationale + args as name/value pairs",
            calls_per_proposal=1,
            schemas=(build_list_args(registry),),
            adapted_instruction=(
                "List the arguments of the tool you chose as name and value pairs, and list no "
                "other tool's arguments."
            ),
            read=_read_list_args,
        ),
        TwoCallShape(
            key="E_two_call",
            title="Two calls: choose the tool, then fill that tool's arguments",
            calls_per_proposal=2,
            schemas=(build_tool_choice(registry),),
            adapted_instruction=(
                "Name the tool you have chosen. You will be asked for its arguments separately."
            ),
        ),
    )


CANDIDATES: Final[tuple[ShapeCandidate, ...]] = build_candidates()


def candidate(key: str) -> ShapeCandidate:
    for shape in CANDIDATES:
        if shape.key == key:
            return shape
    raise KeyError(f"no shape candidate named {key!r}")


def system_prompt_for(shape: ShapeCandidate, captured: str, *, adapted: bool) -> str:
    """The system prompt this shape is measured with.

    ``verbatim`` is the true A/B and the default: every shape gets the prompt the graph really
    sent, including the sentence written for the flat shape. A shape that wins while carrying an
    instruction aimed at a different schema has won on the schema.

    ``adapted`` replaces that one sentence, and exists so a *losing* shape can be re-measured
    before it is concluded against — a shape told to do something it cannot has been measured on
    the instruction rather than on itself.
    """
    if not adapted or shape.adapted_instruction == FLAT_SPECIFIC_SENTENCE:
        return captured
    if FLAT_SPECIFIC_SENTENCE not in captured:  # pragma: no cover - the capture is on disk
        raise ValueError("the captured system prompt no longer contains the flat-specific sentence")
    return captured.replace(FLAT_SPECIFIC_SENTENCE, shape.adapted_instruction)


# ---------------------------------------------------------------------------------------------
# Running the comparison
# ---------------------------------------------------------------------------------------------


@dataclass
class ShapeTrial:
    """One call sequence for one shape against one model."""

    shape_key: str
    model_spec: str

    tool_name: str = ""
    args: dict[str, str] = field(default_factory=dict)
    schema_failure: str = ""
    argument_defect: str = ""
    error: str = ""
    latency_ms: float = 0.0

    misquoted: list[str] = field(default_factory=list)
    """Arguments whose value disagrees with the evidence's fact of the same name."""

    @property
    def valid(self) -> bool:
        """The schema was satisfied and the arguments are the chosen tool's own.

        A property of the **shape**. Deliberately blind to whether the values are any good — see
        :attr:`usable`, and :func:`transcription_defects` for why the two are kept apart.
        """
        return not (self.schema_failure or self.argument_defect or self.error)

    @property
    def usable(self) -> bool:
        """Valid, *and* every value it copied matches what the evidence said.

        A property of the **model**. This is the one that predicts whether the graph gets past the
        policy gate, because trust resolves on an exact match and a stray character does not.
        """
        return self.valid and not self.misquoted

    def as_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape_key,
            "model": self.model_spec,
            "valid": self.valid,
            "usable": self.usable,
            "tool_name": self.tool_name,
            "args": self.args,
            "schema_failure": self.schema_failure,
            "argument_defect": self.argument_defect,
            "misquoted": self.misquoted,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
        }


async def run_trial(
    shape: ShapeCandidate,
    model: Model,
    *,
    model_spec: str,
    system_prompt: str,
    user_prompt: str,
) -> ShapeTrial:
    """One attempt. Every failure is classified rather than counted.

    Session A's third instrument defect was a rate function that swallowed exceptions while
    counting them, and reported "0/8 valid" without noticing the failure had changed shape
    entirely. So the outcomes are kept apart: the provider failed to satisfy the schema, the
    output satisfied it but named arguments the tool does not take, the output was well-formed but
    misquoted a value the evidence stated, or something else broke.
    """
    trial = ShapeTrial(shape_key=shape.key, model_spec=model_spec)
    started = time.perf_counter()
    try:
        proposal = await shape.propose(model, system_prompt, user_prompt)
    except SCHEMA_FAILURES as failure:
        trial.schema_failure = f"{type(failure).__name__}: {_first_line(failure)}"
    except ArgumentDefect as failure:
        trial.argument_defect = str(failure)
    except Exception as failure:  # classified below, never swallowed
        trial.error = f"{type(failure).__name__}: {_first_line(failure)}"
    else:
        trial.tool_name = proposal.tool_name
        trial.args = proposal.args
        trial.misquoted = transcription_defects(proposal, evidence_facts(user_prompt))
    trial.latency_ms = (time.perf_counter() - started) * 1000
    return trial


def _first_line(failure: BaseException) -> str:
    """The first line of an exception's message.

    A ``ValidationError`` renders every offending field with its ``input_value``, and those values
    are text the planner copied out of a document. The first line names the count and the model
    and carries none of it.
    """
    return str(failure).splitlines()[0] if str(failure) else ""


def summarise(trials: Sequence[ShapeTrial]) -> dict[str, Any]:
    """Counts, never a single blended score."""
    return {
        "n": len(trials),
        "valid": sum(1 for trial in trials if trial.valid),
        "usable": sum(1 for trial in trials if trial.usable),
        "schema_failures": sum(1 for trial in trials if trial.schema_failure),
        "argument_defects": sum(1 for trial in trials if trial.argument_defect),
        "misquoted": sum(1 for trial in trials if trial.misquoted),
        "errors": sum(1 for trial in trials if trial.error),
        "tools_proposed": sorted({trial.tool_name for trial in trials if trial.tool_name}),
        "misquoted_arguments": sorted(
            {name for trial in trials for name in trial.misquoted},
        ),
    }


__all__ = [
    "CANDIDATES",
    "CAPTURED_PROMPT_PATH",
    "FLAT_SPECIFIC_SENTENCE",
    "SCHEMA_FAILURES",
    "ArgumentDefect",
    "NamedArgument",
    "Proposal",
    "ShapeCandidate",
    "ShapeTrial",
    "SingleCallShape",
    "TwoCallShape",
    "UnionRootModel",
    "UnionWrapped",
    "build_candidates",
    "build_dict_args",
    "build_flat_args",
    "build_list_args",
    "build_per_tool_arguments",
    "build_tool_choice",
    "candidate",
    "captured_prompt",
    "evidence_facts",
    "normalise",
    "provider_tool_spec",
    "run_trial",
    "summarise",
    "system_prompt_for",
    "tool_spec_defects",
    "transcription_defects",
]
