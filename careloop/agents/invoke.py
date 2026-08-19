"""The one place CareLoop calls a model.

Every LLM invocation in the system goes through this module, and a source sweep in
``tests/test_agents.py`` asserts no other module under ``careloop/`` so much as names
``structured_output``. One module means one place where the system prompt, the user message and
the output schema are assembled, and therefore one place to audit rather than a set of call sites
that have to be found first.

**Two seams, not one, since B2.** Triage goes through :func:`structured_call`; the planner goes
through :func:`proposal_tool_call`, for the measured reason recorded above that function. This
docstring said "every LLM invocation goes through ``structured_call``" until session W, which had
been false since the planner moved.

**Why not** ``strands.Agent``. Two reasons, in order of importance.

An ``Agent`` reaching structured output through the non-deprecated path does it by forcing an
internal ``StructuredOutputTool`` call through the event loop. That tool would then arrive at
commit 6's ``BeforeToolCall`` hook — a deny-by-default policy engine that has never heard of it,
holds no rule naming it, and would refuse it. An SDK-internal tool tripping CareLoop's own gate is
a failure that would surface deep inside graph execution looking like nothing in particular.

And the path that does *not* do that, ``Agent.structured_output_async``, is deprecated in
``strands-agents==1.51.0``; it warns with ``stacklevel=2``, so the warning is attributed to its
caller, and ``pyproject.toml`` turns ``DeprecationWarning`` raised from ``careloop.*`` into an
error. ``docs/spike-findings.md`` §3 recorded the method as confirmed without noticing this;
it is corrected there.

Driving :meth:`strands.models.Model.structured_output` directly avoids both. It also leaves the
two agents with no tools and no event loop at all, which is what makes "agents propose, they do
not act" a property of the object graph rather than a promise about the code — and that stays true
of :func:`proposal_tool_call`, which hands a provider tool *specifications* and dispatches nothing.

**The 2026-08-11 benchmark measured the ``structured_output`` path, and for the planner that is no
longer the path CareLoop runs.** ``docs/model-benchmark.md`` is the pre-B2 record and says so;
``docs/model-benchmark-post-b2.md`` measures this seam.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from strands.models import Model

logger = logging.getLogger(__name__)


class NoStructuredOutput(Exception):
    """Raised when a provider's stream ended without an ``{"output": ...}`` envelope.

    The spike established that ``Model.structured_output`` is an async *generator* which yields
    the instance in an envelope rather than returning it. A provider that yields no envelope has
    produced nothing; treating that as an empty result would hand the graph a default-valued
    intent that no model proposed.
    """


class WrongStructuredOutputType(Exception):
    """Raised when a provider yielded something that is not the requested schema.

    The mock refuses this at the script boundary. Real providers are validated here instead, so
    the check holds for the models CareLoop actually runs on and not only for the one the tests
    see.
    """


async def structured_call[T: BaseModel](
    model: Model,
    output_model: type[T],
    *,
    system_prompt: str,
    user_prompt: str,
) -> T:
    """Ask ``model`` for one instance of ``output_model``.

    The user message is the only place untrusted content appears; the system prompt is a literal
    from :mod:`careloop.agents.prompts`. Neither is assembled here — this function transports what
    it is handed and unwraps what comes back.

    Raises:
        NoStructuredOutput: if the provider's stream carried no output envelope.
        WrongStructuredOutputType: if what it carried was not an ``output_model``.
    """
    messages = [{"role": "user", "content": [{"text": user_prompt}]}]

    result: object | None = None
    async for event in model.structured_output(output_model, messages, system_prompt=system_prompt):
        if "output" in event:
            result = event["output"]

    if result is None:
        raise NoStructuredOutput(f"provider yielded no output envelope for {output_model.__name__}")
    if not isinstance(result, output_model):
        # Names types, never values. What came back is model output derived from document
        # content, and an exception message is a place it should not be copied to.
        raise WrongStructuredOutputType(
            f"provider yielded {type(result).__name__} where {output_model.__name__} was asked for"
        )
    return result


# -------------------------------------------------------------------------------------------------
# Proposal tool calls
# -------------------------------------------------------------------------------------------------
#
# The planner does not use ``structured_output``. Measured against the deployed model, that path
# hands the provider a single wrapper tool whose description describes an *object* rather than an
# instruction to call it, and Nova 2 Lite answers by inventing a tool named after an entry in the
# prompt catalogue — 25 of 25 responses carried a ``toolUse`` for ``book_appointment`` when the
# only tool offered was ``PlannerIntent``. The SDK then raises a bare ``ValueError`` saying no
# valid tool use was found, which is true and says nothing about why.
#
# So the planner offers the actions themselves, as closed proposal schemas, and reads back the one
# the model called. Everything provider-shaped about that — the converse stream, ``toolUse``
# blocks, ``toolChoice`` — lives here, so ``planner.py`` reasons about proposals and never about
# response dictionaries.
#
# **This is not an agent loop.** No ``strands.Agent`` is constructed, no event loop runs, no tool
# is dispatched and no real CareLoop tool is registered with anything. The proposal schemas are
# specifications handed to a provider and nothing else; the ``BeforeToolCall`` hook is never
# reached because nothing here calls a tool.


class NoProposalToolCall(Exception):
    """The provider's answer carried no call to any offered proposal tool.

    Recoverable in the sense that asking again can produce a different answer: it is a failure to
    generate, not a refusal and not a policy outcome.
    """


class AmbiguousProposalToolCall(Exception):
    """The provider called more than one proposal tool.

    The planner proposes exactly one next action. Two calls is not a proposal to disambiguate by
    picking one — choosing between them here would be this layer deciding what was proposed.
    """


class UnknownProposalTool(Exception):
    """The provider called a tool that was not offered.

    Names the tool it called and the tools it was offered — both are CareLoop's own identifiers,
    never model-authored argument values.
    """


class MalformedProposal(Exception):
    """The provider called an offered tool with arguments that do not satisfy its schema.

    **Names fields, never values.** A ``ValidationError`` renders ``input_value``, and that input
    is model output derived from document content; echoing it into an exception message would put
    untrusted text into logs and, worse, one refactor away from a model's own context.
    """


#: Failures a second attempt could plausibly fix: the model did not produce a well-formed proposal.
#:
#: Deliberately narrow. A throttle, a credential error, a missing model or a bug in this package is
#: not made truer by asking again, and a retry that swallowed one would hide a real fault.
RECOVERABLE_PROPOSAL_FAILURES: Final[tuple[type[Exception], ...]] = (
    NoProposalToolCall,
    AmbiguousProposalToolCall,
    UnknownProposalTool,
    MalformedProposal,
)

#: One initial call and at most one retry. A bound, not a policy.
MAX_PROPOSAL_ATTEMPTS: Final[int] = 2


def proposal_tool_specs(tools: Mapping[str, type[BaseModel]]) -> list[dict[str, object]]:
    """The tool specifications for ``tools``, in the shape a provider is handed.

    Built from each schema's own JSON schema and docstring, so the description a model reads and
    the fields it must fill come from the generated model rather than from a second table.
    """
    return [
        {
            "name": name,
            "description": (schema.__doc__ or "").strip(),
            "inputSchema": {"json": schema.model_json_schema()},
        }
        for name, schema in sorted(tools.items())
    ]


async def _one_proposal_attempt[T: BaseModel](
    model: Model,
    tools: Mapping[str, type[T]],
    *,
    system_prompt: str,
    user_prompt: str,
) -> T:
    """Ask ``model`` to call exactly one of ``tools``, and validate what came back."""
    from strands.event_loop import streaming

    specs = proposal_tool_specs(tools)

    last: dict[str, object] | None = None
    async for event in streaming.process_stream(
        model.stream(
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            tool_specs=specs,
            system_prompt=system_prompt,
            tool_choice={"any": {}},
        )
    ):
        last = event

    if last is None or "stop" not in last:
        raise NoProposalToolCall("the provider's stream ended without a stop event")

    _, message, _, _ = last["stop"]  # type: ignore[index]
    calls = [block["toolUse"] for block in message["content"] if "toolUse" in block]
    if not calls:
        raise NoProposalToolCall("the provider's answer carried no tool use at all")
    if len(calls) > 1:
        raise AmbiguousProposalToolCall(
            f"the provider called {len(calls)} tools; a proposal is exactly one action"
        )

    call = calls[0]
    name = call.get("name")
    if name not in tools:
        raise UnknownProposalTool(
            f"the provider called {name!r}, which was not offered; "
            f"offered: {', '.join(sorted(tools))}"
        )

    arguments = call.get("input")
    if not isinstance(arguments, dict):
        raise MalformedProposal(f"{name} was called with no argument object")

    try:
        return tools[name](**arguments)
    except ValidationError as invalid:
        # Fields, never values. `error["loc"]` is the field path; `error["input"]` is not touched.
        fields = sorted(
            {".".join(str(part) for part in error["loc"]) or "<root>" for error in invalid.errors()}
        )
        raise MalformedProposal(
            f"{name} was called with arguments that do not satisfy its schema; "
            f"offending field(s): {', '.join(fields)}"
        ) from invalid


async def proposal_tool_call[T: BaseModel](
    model: Model,
    tools: Mapping[str, type[T]],
    *,
    system_prompt: str,
    user_prompt: str,
) -> T:
    """Ask ``model`` to call exactly one of ``tools`` and return the validated instance.

    Deterministic about every way the answer can be wrong: zero calls, several calls, a tool that
    was not offered, and arguments that do not satisfy the schema each raise their own exception.
    **Nothing is manufactured.** No JSON is repaired, no free text is parsed, no field is defaulted
    and no proposal is invented — what comes back is an instance the provider produced and Pydantic
    validated, or nothing comes back at all.

    **One bounded retry, and only for a failure to generate.** Measured on the deployed model the
    proposal call is reliable, so this is defence in depth rather than a workaround: a provider
    that answers badly once is asked once more, with the identical schemas and the identical
    prompts. Anything outside :data:`RECOVERABLE_PROPOSAL_FAILURES` — a throttle, a credential
    error, a bug here — propagates from the first attempt untouched. Nothing about a *policy*
    outcome reaches this function, so no denial is ever retried.

    Raises:
        NoProposalToolCall: the answer carried no call to an offered tool.
        AmbiguousProposalToolCall: the answer called more than one.
        UnknownProposalTool: the answer called a tool that was not offered.
        MalformedProposal: the arguments do not satisfy the called tool's schema.
    """
    for attempt in range(1, MAX_PROPOSAL_ATTEMPTS + 1):
        try:
            proposal = await _one_proposal_attempt(
                model, tools, system_prompt=system_prompt, user_prompt=user_prompt
            )
        except RECOVERABLE_PROPOSAL_FAILURES as failure:
            if attempt == MAX_PROPOSAL_ATTEMPTS:
                logger.warning(
                    "proposal call failed again (%s); no attempts left", type(failure).__name__
                )
                raise
            # The exception *type* only. Every one of these messages is built from CareLoop's own
            # identifiers, but logging the type keeps that true by construction rather than by
            # everyone remembering.
            logger.warning(
                "proposal call produced no valid proposal (%s); retrying once",
                type(failure).__name__,
            )
        else:
            if attempt > 1:
                logger.warning("proposal call succeeded on retry")
            return proposal
    raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover


__all__ = [
    "MAX_PROPOSAL_ATTEMPTS",
    "RECOVERABLE_PROPOSAL_FAILURES",
    "AmbiguousProposalToolCall",
    "MalformedProposal",
    "NoProposalToolCall",
    "NoStructuredOutput",
    "UnknownProposalTool",
    "WrongStructuredOutputType",
    "proposal_tool_call",
    "proposal_tool_specs",
    "structured_call",
]
