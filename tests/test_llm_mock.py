"""The mock model provider.

Two things are being pinned here. The SDK contract the spike established by execution — an async
generator yielding ``{"output": instance}`` — and the mock's strictness, which is what stops a
test passing because the mock was obliging rather than because the code was right.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from strands.models import Model

from careloop.agents.invoke import proposal_tool_specs
from careloop.agents.schemas import PROPOSAL_TOOLS
from careloop.llm.mock import (
    MockModel,
    MockScriptExhausted,
    MockScriptTypeMismatch,
    mock_model,
)


class Classification(BaseModel):
    urgency: str
    patient_id: str


class OtherShape(BaseModel):
    total: int


def user(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


async def collect_stream(model: MockModel, messages: list[dict], **kwargs) -> list[dict]:
    return [event async for event in model.stream(messages, **kwargs)]


async def collect_structured(model: MockModel, output_model: type, prompt: list[dict]) -> list:
    return [event async for event in model.structured_output(output_model, prompt)]


class TestModelInterface:
    def test_mock_is_a_strands_model(self):
        assert isinstance(mock_model(), Model)

    def test_every_abstract_method_is_implemented(self):
        # Instantiating an ABC with an unimplemented abstract method raises TypeError, so this
        # succeeding is the assertion.
        assert MockModel.__abstractmethods__ == frozenset()

    def test_config_round_trips(self):
        model = mock_model()
        model.update_config(temperature=0.0, model_id="scripted")
        config = model.get_config()
        assert config["temperature"] == 0.0
        assert config["model_id"] == "scripted"

    def test_get_config_returns_a_copy(self):
        model = mock_model()
        model.get_config()["model_id"] = "mutated"
        assert model.get_config()["model_id"] == "mock"


class TestStructuredOutput:
    """The contract from docs/spike-findings.md §4."""

    def test_yields_the_output_envelope(self):
        answer = Classification(urgency="HIGH", patient_id="PT-4417")
        model = mock_model(structured=[answer])
        events = asyncio.run(collect_structured(model, Classification, [user("classify")]))
        assert events == [{"output": answer}]

    def test_yields_the_scripted_instance_itself(self):
        answer = Classification(urgency="LOW", patient_id="PT-0001")
        model = mock_model(structured=[answer])
        events = asyncio.run(collect_structured(model, Classification, [user("classify")]))
        assert events[0]["output"] is answer

    def test_is_an_async_generator_not_a_coroutine(self):
        # The distinction cost a spike iteration: awaiting a generator fails deep in the SDK
        # rather than at the call site, so it is pinned here.
        import inspect

        assert inspect.isasyncgenfunction(MockModel.structured_output)

    def test_answers_are_consumed_in_order(self):
        first = Classification(urgency="HIGH", patient_id="PT-1")
        second = Classification(urgency="LOW", patient_id="PT-2")
        model = mock_model(structured=[first, second])
        prompt = [user("classify")]
        assert asyncio.run(collect_structured(model, Classification, prompt))[0]["output"] is first
        assert asyncio.run(collect_structured(model, Classification, prompt))[0]["output"] is second

    def test_wrong_type_raises_rather_than_coercing(self):
        model = mock_model(structured=[OtherShape(total=3)])
        with pytest.raises(MockScriptTypeMismatch) as excinfo:
            asyncio.run(collect_structured(model, Classification, [user("classify")]))
        assert "OtherShape" in str(excinfo.value)
        assert "Classification" in str(excinfo.value)

    def test_exhaustion_raises_rather_than_inventing(self):
        model = mock_model(structured=[])
        with pytest.raises(MockScriptExhausted):
            asyncio.run(collect_structured(model, Classification, [user("classify")]))

    def test_a_scripted_exception_is_raised(self):
        model = mock_model(structured=[RuntimeError("provider exploded")])
        with pytest.raises(RuntimeError, match="provider exploded"):
            asyncio.run(collect_structured(model, Classification, [user("classify")]))


class TestStream:
    def test_emits_the_converse_envelope_the_spike_recorded(self):
        model = mock_model(text=["ok"])
        events = asyncio.run(collect_stream(model, [user("hello")]))
        keys = [next(iter(event)) for event in events]
        assert keys == [
            "messageStart",
            "contentBlockDelta",
            "contentBlockStop",
            "messageStop",
            "metadata",
        ]
        assert events[0]["messageStart"]["role"] == "assistant"
        assert events[1]["contentBlockDelta"]["delta"]["text"] == "ok"
        assert events[3]["messageStop"]["stopReason"] == "end_turn"

    def test_metadata_carries_usage(self):
        model = mock_model(text=["ok"])
        events = asyncio.run(collect_stream(model, [user("hello")]))
        assert "usage" in events[-1]["metadata"]

    def test_two_runs_of_one_script_are_identical(self):
        # No clock anywhere in the mock: a latency stamp here would make two runs of the same
        # script differ, and the whole point of scripting is that they do not.
        first = asyncio.run(collect_stream(mock_model(text=["ok"]), [user("hello")]))
        second = asyncio.run(collect_stream(mock_model(text=["ok"]), [user("hello")]))
        assert first == second

    def test_exhaustion_raises(self):
        model = mock_model(text=[])
        with pytest.raises(MockScriptExhausted):
            asyncio.run(collect_stream(model, [user("hello")]))

    def test_a_scripted_exception_is_raised(self):
        model = mock_model(text=[TimeoutError("throttled")])
        with pytest.raises(TimeoutError, match="throttled"):
            asyncio.run(collect_stream(model, [user("hello")]))


class TestScriptsAreIndependent:
    def test_streaming_does_not_consume_structured_answers(self):
        model = mock_model(text=["ok"], structured=[Classification(urgency="LOW", patient_id="P")])
        asyncio.run(collect_stream(model, [user("hello")]))
        assert model.remaining == (0, 1, 0)

    def test_structured_output_does_not_consume_text_answers(self):
        model = mock_model(text=["ok"], structured=[Classification(urgency="LOW", patient_id="P")])
        asyncio.run(collect_structured(model, Classification, [user("classify")]))
        assert model.remaining == (1, 0, 0)


class TestCallRecording:
    def test_records_what_the_caller_asked_for(self):
        model = mock_model(structured=[Classification(urgency="HIGH", patient_id="PT-4417")])
        asyncio.run(collect_structured(model, Classification, [user("classify this")]))
        (call,) = model.calls
        assert call.kind == "structured_output"
        assert call.output_model_name == "Classification"
        assert call.messages[0]["content"][0]["text"] == "classify this"

    def test_records_tool_names_offered_to_the_model(self):
        """A call carrying tool specs is a proposal call, answered from the proposal queue."""
        proposal = PROPOSAL_TOOLS["propose_fetch_document"](
            rationale="read what we already hold", document_id="doc-0001-referral"
        )
        model = mock_model(proposals=[proposal])
        specs = proposal_tool_specs(PROPOSAL_TOOLS)
        asyncio.run(collect_stream(model, [user("plan")], tool_specs=specs))
        assert model.calls[0].tool_names == tuple(sorted(PROPOSAL_TOOLS))

    def test_a_proposal_call_is_answered_with_a_real_tool_use_block(self):
        """The wire shape, not a shorthand — the same parsing a provider's answer gets."""
        proposal = PROPOSAL_TOOLS["propose_fetch_document"](
            rationale="read what we already hold", document_id="doc-0001-referral"
        )
        model = mock_model(proposals=[proposal])
        events = asyncio.run(
            collect_stream(model, [user("plan")], tool_specs=proposal_tool_specs(PROPOSAL_TOOLS))
        )
        starts = [e for e in events if "contentBlockStart" in e]
        assert (
            starts[0]["contentBlockStart"]["start"]["toolUse"]["name"] == "propose_fetch_document"
        )
        assert events[-2]["messageStop"]["stopReason"] == "tool_use"

    def test_a_proposal_the_caller_did_not_offer_is_refused(self):
        proposal = PROPOSAL_TOOLS["propose_send_email"](
            rationale="r", to="a@b.example", subject="s", body="b"
        )
        model = mock_model(proposals=[proposal])
        only_fetch = proposal_tool_specs(
            {"propose_fetch_document": PROPOSAL_TOOLS["propose_fetch_document"]}
        )
        with pytest.raises(MockScriptTypeMismatch):
            asyncio.run(collect_stream(model, [user("plan")], tool_specs=only_fetch))

    def test_an_exhausted_call_is_still_recorded(self):
        # The call happened. A mock that only records successful calls would hide the call that
        # broke the expectation.
        model = mock_model()
        with pytest.raises(MockScriptExhausted):
            asyncio.run(collect_stream(model, [user("hello")]))
        assert len(model.calls) == 1
