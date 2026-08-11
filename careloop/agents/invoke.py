"""The one place CareLoop calls a model.

Every LLM invocation in the system goes through :func:`structured_call`, and a source sweep in
``tests/test_agents.py`` asserts no other module under ``careloop/`` so much as names
``structured_output``. One call site means one place where the system prompt, the user message and
the output schema are assembled, and therefore one place to audit rather than a set of call sites
that have to be found first.

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

Driving :meth:`strands.models.Model.structured_output` directly avoids both, and is the path the
model benchmark measured — so what the benchmark scored is what CareLoop runs. It also leaves the
two agents with no tools and no event loop at all, which is what makes "agents propose, they do
not act" a property of the object graph rather than a promise about the code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from strands.models import Model


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


__all__ = ["NoStructuredOutput", "WrongStructuredOutputType", "structured_call"]
