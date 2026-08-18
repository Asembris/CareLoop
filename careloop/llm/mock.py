"""The mock model provider.

The only model the test suite ever sees. Everything CareLoop asserts about the spine — state
transitions, policy verdicts, idempotency, loop bounds — is asserted against scripted model output,
so the suite runs offline, at $0, and deterministically.

The mock is deliberately **strict**. It never invents a response:

- an exhausted script raises :class:`MockScriptExhausted` rather than returning something plausible
- a scripted instance of the wrong type raises :class:`MockScriptTypeMismatch` rather than coercing
- a scripted :class:`Exception` is raised, so provider failure is testable rather than hypothetical

Each of those is a test that would otherwise pass for the wrong reason. A mock that answers
whatever it is asked proves the caller ran, not that the caller was right.

The one subtle contract is :meth:`MockModel.structured_output`, which the spike established is an
async **generator** and must yield ``{"output": instance}`` rather than returning the instance. See
``docs/spike-findings.md`` §4.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable, Iterable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from careloop.agents.schemas import ProposalToolBase

T = TypeVar("T", bound=BaseModel)


class MockScriptExhausted(Exception):
    """Raised when the mock is called more times than its script has answers.

    Deliberately not a soft default. A mock that keeps answering after its script runs out turns
    "the graph made three model calls" into an untested assumption.
    """


class MockScriptTypeMismatch(Exception):
    """Raised when a scripted structured output is not an instance of the requested model.

    The real provider validates against ``output_model``; a mock that returns whatever it was
    handed would let a test assert on a shape the real provider could never produce.
    """


class RecordedCall(BaseModel):
    """One call the mock received, for assertions about what the caller actually asked."""

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    kind: str
    """``"stream"`` or ``"structured_output"``."""

    messages: tuple[dict[str, Any], ...]
    system_prompt: str | None = None
    output_model_name: str | None = None
    tool_names: tuple[str, ...] = ()


class MockModel(Model):
    """A ``strands`` model whose answers are scripted in advance.

    Two independent scripts, because the two entry points are independent: ``stream`` consumes
    ``text_outputs`` and ``structured_output`` consumes ``structured_outputs``. Sharing one queue
    would couple a test's expectations about text to its expectations about typed output.

    An entry in either script may be an :class:`Exception`, which is raised instead of returned.
    """

    def __init__(
        self,
        *,
        text_outputs: Iterable[str | Exception] = (),
        structured_outputs: Iterable[BaseModel | Exception] = (),
        proposal_outputs: Iterable[ProposalToolBase | Exception] = (),
        model_id: str = "mock",
    ) -> None:
        self._text: deque[str | Exception] = deque(text_outputs)
        self._structured: deque[BaseModel | Exception] = deque(structured_outputs)
        self._proposals: deque[ProposalToolBase | Exception] = deque(proposal_outputs)
        self._config: dict[str, Any] = {"model_id": model_id}
        self._calls: list[RecordedCall] = []

    # -- introspection for tests ------------------------------------------------------------

    @property
    def calls(self) -> tuple[RecordedCall, ...]:
        """Every call the mock has received, in order."""
        return tuple(self._calls)

    @property
    def remaining(self) -> tuple[int, int, int]:
        """``(text answers left, structured answers left, proposals left)``."""
        return len(self._text), len(self._structured), len(self._proposals)

    # -- Model interface --------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Emit a scripted answer as a well-formed converse event sequence.

        Two shapes, chosen by what the caller offered rather than by a flag. A call carrying tool
        specifications is a **proposal call** and is answered from ``proposal_outputs`` with a real
        ``toolUse`` block; a call carrying none is answered from ``text_outputs``. The queues stay
        separate for the reason the class docstring gives — a test's expectations about proposals
        should not be coupled to its expectations about text.

        The ``toolUse`` sequence is the shape a real Bedrock stream produces, block start carrying
        the name and a delta carrying the JSON arguments, so a mock answer exercises the same
        parsing a provider's answer does.
        """
        self._calls.append(
            RecordedCall(
                kind="stream",
                messages=tuple(dict(m) for m in messages),
                system_prompt=system_prompt,
                tool_names=tuple(spec["name"] for spec in (tool_specs or [])),
            )
        )

        if tool_specs:
            if not self._proposals:
                raise MockScriptExhausted(
                    f"a proposal call offered {len(tool_specs)} tool(s) with no proposals left"
                )
            proposal = self._proposals.popleft()
            if isinstance(proposal, Exception):
                raise proposal
            offered = {spec["name"] for spec in tool_specs}
            name = type(proposal).__name__
            if name not in offered:
                raise MockScriptTypeMismatch(
                    f"script holds a {name} but the caller offered {sorted(offered)}"
                )
            yield {"messageStart": {"role": "assistant"}}
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": name, "toolUseId": "tu-mock-1"}}
                }
            }
            yield {
                "contentBlockDelta": {"delta": {"toolUse": {"input": proposal.model_dump_json()}}}
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            yield {
                "metadata": {
                    "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                    "metrics": {"latencyMs": 0},
                }
            }
            return

        if not self._text:
            raise MockScriptExhausted(
                f"stream called {len(self._calls)} time(s) with no text answers left to give"
            )
        answer = self._text.popleft()
        if isinstance(answer, Exception):
            raise answer

        # The shape the spike recorded from a real Bedrock stream. Emitting the real envelope
        # rather than a convenient shorthand is what keeps the mock substitutable.
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": answer}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                # No clock: a fixed latency keeps two runs of the same script byte-identical.
                "metrics": {"latencyMs": 0},
            }
        }

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """Yield ``{"output": instance}``.

        An async generator, not a coroutine, and the envelope is required — both established by
        execution in the spike, and both fail confusingly rather than loudly if got wrong.
        """
        self._calls.append(
            RecordedCall(
                kind="structured_output",
                messages=tuple(dict(m) for m in prompt),
                system_prompt=system_prompt,
                output_model_name=output_model.__name__,
            )
        )
        if not self._structured:
            raise MockScriptExhausted(
                f"structured_output called for {output_model.__name__} with no answers left"
            )
        answer = self._structured.popleft()
        if isinstance(answer, Exception):
            raise answer
        if not isinstance(answer, output_model):
            raise MockScriptTypeMismatch(
                f"script holds {type(answer).__name__} but the caller asked for "
                f"{output_model.__name__}"
            )
        yield {"output": answer}


def mock_model(
    *,
    text: Sequence[str | Exception] = (),
    structured: Sequence[BaseModel | Exception] = (),
    proposals: Sequence[ProposalToolBase | Exception] = (),
) -> MockModel:
    """Convenience constructor for tests."""
    return MockModel(text_outputs=text, structured_outputs=structured, proposal_outputs=proposals)


__all__ = [
    "MockModel",
    "MockScriptExhausted",
    "MockScriptTypeMismatch",
    "RecordedCall",
    "mock_model",
]
