"""Spike: should Triage and Planner become real ``strands.Agent`` instances?

**This file is a measuring instrument, not production code.** It imports ``careloop`` and touches
nothing in it. It lives under ``scripts/`` rather than ``tests/`` on purpose: ``testpaths =
["tests"]`` means pytest never collects it, so nothing in a suite of 1408 tests can come to depend
on a probe whose whole job is to find out what happens.

Commit 5 dropped ``strands.Agent`` for direct ``Model.structured_output`` on two grounds — that
``Agent.structured_output`` is deprecated, and that the replacement forces an internal
``StructuredOutputTool`` through the event loop, where CareLoop's deny-by-default gate would refuse
it. Both objections were real. What was never evaluated is the modern path on an agent owning
**zero tools**, with the policy hook **never attached to it**: if there is no gate on that agent,
there may be nothing for the SDK's internal tool to collide with.

Six probes, numbered to the questions the session brief asks. Each reports what it *observed*, and
each prints its evidence rather than only its verdict, because a verdict with no evidence behind it
is the thing this project keeps failing tests for.

    .venv/Scripts/python scripts/spike_agent.py                     # offline, $0
    .venv/Scripts/python scripts/spike_agent.py --live --confirm-spend

**Why the offline half needs a subclass of ``MockModel``, and why that is a finding rather than a
workaround.** The forced structured-output path requires the model to emit ``toolUse`` stream
events. ``MockModel.stream`` emits text only, so the shipped mock cannot drive this path at all —
probe 1 runs it anyway and reports exactly how it fails. :class:`ToolEmittingMockModel` below is
what it would take to make it work. It is defined here, in the spike, so that its size is
*measured* rather than estimated: adopting the agent path means that delta lands in
``careloop/llm/mock.py``, the one object every test in the suite depends on.

Live mode reads ``.env`` into a mapping and builds the model explicitly. It never sets a
``CARELOOP_MODEL_*`` variable, because ``tests/test_llm_providers.py::
TestTheSuiteItselfUsesNoRealProvider`` is a tripwire that fails the suite if the ambient
environment selects a real provider, and a spike that tripped it would have broken the property it
was written to protect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
import warnings
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel  # noqa: E402
from strands import Agent  # noqa: E402
from strands.hooks import BeforeToolCallEvent  # noqa: E402
from strands.models import Model  # noqa: E402
from strands.types.content import Message  # noqa: E402
from strands.types.streaming import StreamEvent  # noqa: E402
from strands.types.tools import ToolSpec  # noqa: E402

from bench.cost import estimate_tokens, load_rates  # noqa: E402
from careloop.agents.invoke import structured_call  # noqa: E402
from careloop.agents.prompts import (  # noqa: E402
    planner_system_prompt,
    planner_user_prompt,
    triage_system_prompt,
    triage_user_prompt,
)
from careloop.agents.schemas import (  # noqa: E402
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
)
from careloop.llm.mock import MockModel, RecordedCall  # noqa: E402
from careloop.llm.providers import build_model, parse_spec  # noqa: E402
from careloop.provenance import Fact, Provenance, TrustLevel  # noqa: E402
from careloop.state import Case, CaseState  # noqa: E402

#: The model the live half runs against — the planner's measured default.
LIVE_MODEL_SPEC = "bedrock:us.amazon.nova-2-lite-v1:0"

#: A fixed instant. The spike stamps nothing with a real clock, for the same reason
#: ``careloop/tools/`` may not read one: two runs should be comparable.
FIXED_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)

#: Probe 2's canaries. Two unrelated cases, each carrying a token that could only have come from
#: its own prompt, so "did case B see case A" is a substring question with an unambiguous answer.
CANARY_A = "CANARY-ALPHA-7F3D91"
CANARY_B = "CANARY-BRAVO-2E8C44"

#: Turn cap on probe 3's deny-everything sub-probe. A cancelled structured-output tool never
#: produces the output the event loop is waiting for, so the loop forces again — unbounded, that
#: is a paid runaway rather than a clean failure. Measured once at 245 calls; bounded ever since.
DENY_PROBE_TURNS = 3


# ---------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """One probe's finding.

    ``verdict`` is deliberately free text rather than a pass/fail flag. Most of these questions do
    not have a pass and a fail — they have an answer, and the decision gate is what turns answers
    into a recommendation.
    """

    number: int
    title: str
    verdict: str
    evidence: list[str] = field(default_factory=list)
    error: str = ""

    def render(self) -> str:
        # ASCII only. The Windows console this is developed on is cp1252, and a box-drawing
        # character turns a finished probe run into a UnicodeEncodeError at the print.
        lines = [f"== Probe {self.number}: {self.title}", f"   VERDICT: {self.verdict}"]
        lines.extend(f"     - {line}" for line in self.evidence)
        if self.error:
            lines.append(f"     ! {self.error}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe": self.number,
            "title": self.title,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "error": self.error,
        }


# ---------------------------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    """One request as the *provider* received it.

    Probe 2 turns on this distinction. Leakage is a property of what crossed the wire, not of the
    object we happen to be able to inspect: ``agent.messages`` is the agent's own bookkeeping and
    an SDK that rebuilt the request from somewhere else would leave it looking innocent.
    """

    kind: str
    system_prompt: str
    body: str
    tool_names: tuple[str, ...]
    tool_choice: str

    def mentions(self, token: str) -> bool:
        return token in self.body or token in self.system_prompt


class RecordingModel(Model):
    """A transparent proxy that records every request and every usage report.

    Delegates all four abstract methods plus :attr:`stateful`, which ``Agent.__init__`` reads, so
    an agent built over this behaves exactly as one built over the wrapped model.
    """

    def __init__(self, inner: Model) -> None:
        self.inner = inner
        self.requests: list[RecordedRequest] = []
        self.usage: list[dict[str, int]] = []
        self.latencies_ms: list[float] = []

    # -- introspection ----------------------------------------------------------------------

    @property
    def stream_calls(self) -> int:
        return sum(1 for request in self.requests if request.kind == "stream")

    @property
    def structured_calls(self) -> int:
        return sum(1 for request in self.requests if request.kind == "structured_output")

    @property
    def total_calls(self) -> int:
        return len(self.requests)

    @property
    def input_tokens(self) -> int:
        return sum(entry.get("inputTokens", 0) for entry in self.usage)

    @property
    def output_tokens(self) -> int:
        return sum(entry.get("outputTokens", 0) for entry in self.usage)

    def saw(self, token: str) -> list[int]:
        """Indices of the requests carrying ``token``."""
        return [index for index, request in enumerate(self.requests) if request.mentions(token)]

    # -- Model interface --------------------------------------------------------------------

    @property
    def stateful(self) -> bool:
        return self.inner.stateful

    def get_config(self) -> Any:
        return self.inner.get_config()

    def update_config(self, **model_config: Any) -> None:
        self.inner.update_config(**model_config)

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        self.requests.append(
            RecordedRequest(
                kind="stream",
                system_prompt=str(system_prompt or ""),
                body=json.dumps(messages, default=str),
                tool_names=tuple(spec["name"] for spec in (tool_specs or [])),
                tool_choice=json.dumps(kwargs.get("tool_choice"), default=str),
            )
        )
        started = time.perf_counter()
        async for event in self.inner.stream(messages, tool_specs, system_prompt, **kwargs):
            self._capture_usage(event)
            yield event
        self.latencies_ms.append((time.perf_counter() - started) * 1000)

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.requests.append(
            RecordedRequest(
                kind="structured_output",
                system_prompt=str(system_prompt or ""),
                body=json.dumps(prompt, default=str),
                tool_names=(output_model.__name__,),
                tool_choice="",
            )
        )
        started = time.perf_counter()
        async for event in self.inner.structured_output(
            output_model, prompt, system_prompt, **kwargs
        ):
            self._capture_usage(event)
            yield event
        self.latencies_ms.append((time.perf_counter() - started) * 1000)

    def _capture_usage(self, event: Any, depth: int = 0) -> None:
        """Pull token usage out of whatever shape the provider reported it in.

        **Searches nested, and the first version did not.** The agent path yields a top-level
        ``{"metadata": {"usage": ...}}``, but ``Model.structured_output`` wraps its stream in an
        ``{"event": ...}`` envelope, so a top-level lookup reported zero tokens for the direct
        path and made it look as though Bedrock surfaced no usage there. That would have been a
        false negative on the one question open decision 16 turns on, produced by the instrument
        rather than by the SDK.

        Session 4 established that ``strands-agents==1.51.0`` surfaces usage through Bedrock's
        ``structured_output`` and not through the OpenAI one, so this still reads defensively —
        a missing envelope is an answer, not an error.
        """
        if not isinstance(event, dict) or depth > 4:
            return
        metadata = event.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("usage"), dict):
            self.usage.append(dict(metadata["usage"]))
            return
        for value in event.values():
            if isinstance(value, dict):
                self._capture_usage(value, depth + 1)


class ToolEmittingMockModel(MockModel):
    """``MockModel`` plus ``toolUse`` emission — the price of migration, made concrete.

    The shipped mock emits text and nothing else, so an ``Agent`` asked for structured output over
    it never gets the tool call the event loop is waiting for. This subclass answers a request
    with a ``toolUse`` block whenever the request offers a tool spec matching the class of the next
    scripted answer, and falls back to the parent's text behaviour otherwise.

    Two properties are deliberate. It emits the tool call **only once per scripted answer**, so a
    loop that keeps asking cannot be hidden by a mock that keeps answering — the same reason
    ``MockScriptExhausted`` exists. And the fall-through means the shape of the failure when a
    scripted answer runs out is the parent's, not a new one invented here.

    **Everything in this class would have to land in ``careloop/llm/mock.py``.** That is the point
    of writing it here: the delta is measurable rather than asserted.
    """

    def __init__(
        self,
        *,
        tool_answers: Iterable[BaseModel] = (),
        text_outputs: Iterable[str | Exception] = (),
        structured_outputs: Iterable[BaseModel | Exception] = (),
        model_id: str = "mock-tool-emitting",
    ) -> None:
        super().__init__(
            text_outputs=text_outputs,
            structured_outputs=structured_outputs,
            model_id=model_id,
        )
        self._tool_answers: deque[BaseModel] = deque(tool_answers)
        self._tool_use_counter = 0

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        offered = {spec["name"] for spec in (tool_specs or [])}
        pending = self._tool_answers[0] if self._tool_answers else None

        if pending is None or type(pending).__name__ not in offered:
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event
            return

        answer = self._tool_answers.popleft()
        self._tool_use_counter += 1
        self._calls.append(
            RecordedCall(
                kind="stream",
                messages=tuple(dict(message) for message in messages),
                system_prompt=system_prompt,
                tool_names=tuple(sorted(offered)),
            )
        )

        name = type(answer).__name__
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": f"tu_{self._tool_use_counter}", "name": name}}
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": answer.model_dump_json()}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }


class ToolNameRecorder:
    """A hook provider that records every tool the SDK is about to call.

    Structurally identical to :class:`careloop.policy.hook.PolicyHook` — it subscribes to
    ``BeforeToolCallEvent`` — so what it observes is exactly what CareLoop's gate would observe if
    it were attached to the same agent.
    """

    def __init__(self, *, cancel_with: str | None = None) -> None:
        self.seen: list[str] = []
        self.cancel_with = cancel_with

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        self.seen.append(event.tool_use["name"])
        if self.cancel_with is not None:
            event.cancel_tool = self.cancel_with


# ---------------------------------------------------------------------------------------------
# Fixtures the probes drive
# ---------------------------------------------------------------------------------------------


def spike_case(marker: str) -> Case:
    """A case in ``PLANNING`` carrying ``marker`` where document evidence would sit.

    Built here rather than imported from ``tests/helpers.py`` so the spike depends on nothing the
    suite might reshape, and so the marker's position — inside the evidence block, which is where
    untrusted content goes — is visible in this file.
    """
    facts = (
        Fact(
            key="patient_id",
            value="PT-4417",
            trust_level=TrustLevel.CONTROLLED,
            provenance=Provenance(source_doc="referral.pdf", extractor="spike"),
            extracted_at=FIXED_NOW,
        ),
        Fact(
            key="excerpt:referral.pdf:p1",
            value=f"Referral for cardiology. Reference {marker}.",
            trust_level=TrustLevel.UNTRUSTED,
            provenance=Provenance(source_doc="referral.pdf", page=1, extractor="spike"),
            extracted_at=FIXED_NOW,
        ),
    )
    return Case(
        case_id="case-spike",
        event_id="evt-spike",
        state=CaseState.PLANNING,
        summary=f"Referral received; reference {marker}.",
        facts=facts,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def triage_prompts(marker: str) -> tuple[str, str]:
    event = (
        f"From: Cardiology Clinic\nSubject: Referral confirmation {marker}\n\n"
        f"Patient PT-4417 has been referred. Please arrange an appointment within 14 days."
    )
    return triage_system_prompt(), triage_user_prompt(event)


def planner_prompts(marker: str) -> tuple[str, str]:
    return planner_system_prompt(), planner_user_prompt(spike_case(marker))


def scripted_triage() -> TriageResult:
    return TriageResult(
        category=TriageCategory.APPOINTMENT,
        urgency=Urgency.SOON,
        summary="Arrange a cardiology appointment.",
        patient_id="PT-4417",
    )


def scripted_intent() -> BaseModel:
    return PlannerIntent(
        tool_name="fetch_document",
        rationale="Read the referral before proposing anything with an effect.",
        args={},
    )


# ---------------------------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------------------------

#: A factory takes the answers the offline mock should give and returns a recording model. In live
#: mode the answers are ignored — the point is that the same probe body drives both.
ModelFactory = Callable[[Sequence[BaseModel]], RecordingModel]


def mock_factory(answers: Sequence[BaseModel]) -> RecordingModel:
    return RecordingModel(
        ToolEmittingMockModel(
            tool_answers=list(answers),
            text_outputs=["no structured output offered"] * 4,
            structured_outputs=list(answers),
        )
    )


def plain_mock_factory(answers: Sequence[BaseModel]) -> RecordingModel:
    """The shipped mock, unmodified. Probe 1 runs this to record how it fails."""
    return RecordingModel(
        MockModel(
            text_outputs=["I have read the referral."] * 4,
            structured_outputs=list(answers),
        )
    )


def live_factory(env: dict[str, str]) -> ModelFactory:
    def factory(_answers: Sequence[BaseModel]) -> RecordingModel:
        return RecordingModel(build_model(parse_spec(LIVE_MODEL_SPEC), env))

    return factory


# ---------------------------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------------------------


def build_agent(
    model: Model,
    system_prompt: str,
    *,
    hooks: list[Any] | None = None,
    conversation_manager: Any = None,
) -> Agent:
    """A zero-tool agent, with the callback handler silenced so probe output stays readable."""
    kwargs: dict[str, Any] = {
        "model": model,
        "tools": [],
        "system_prompt": system_prompt,
        "callback_handler": None,
    }
    if hooks is not None:
        kwargs["hooks"] = hooks
    if conversation_manager is not None:
        kwargs["conversation_manager"] = conversation_manager
    return Agent(**kwargs)


async def probe_1_structured_output(factory: ModelFactory, *, live: bool) -> ProbeResult:
    """Does a zero-tool ``Agent`` return a valid ``TriageResult`` and ``PlannerIntent``?"""
    result = ProbeResult(
        number=1,
        title="Zero-tool Agent via structured_output_model=",
        verdict="not run",
    )

    if not live:
        # The shipped mock first, unmodified, because how it fails is the finding.
        plain = plain_mock_factory([scripted_triage()])
        system, user = triage_prompts(CANARY_A)
        agent = build_agent(plain, system)
        try:
            await agent.invoke_async(user, structured_output_model=TriageResult)
            result.evidence.append(
                "shipped MockModel: SUCCEEDED unexpectedly — the text-only stream was enough"
            )
        except Exception as failure:
            result.evidence.append(
                f"shipped MockModel: {type(failure).__name__} after "
                f"{plain.stream_calls} stream call(s) — {str(failure)[:120]}"
            )

    outcomes: list[str] = []
    for label, output_model, answer, prompts in (
        ("TriageResult", TriageResult, scripted_triage(), triage_prompts(CANARY_A)),
        ("PlannerIntent", PlannerIntent, scripted_intent(), planner_prompts(CANARY_A)),
    ):
        model = factory([answer])
        system, user = prompts
        agent = build_agent(model, system)
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                agent_result = await agent.invoke_async(user, structured_output_model=output_model)
            structured = agent_result.structured_output
            valid = isinstance(structured, output_model)
            outcomes.append(f"{label}={'valid' if valid else 'INVALID'}")
            result.evidence.append(
                f"{label}: structured_output is {type(structured).__name__}, "
                f"stop_reason={agent_result.stop_reason}, "
                f"provider calls={model.total_calls}"
            )
            deprecations = [
                str(entry.message)
                for entry in caught
                if issubclass(entry.category, DeprecationWarning)
            ]
            if deprecations:
                result.evidence.append(f"{label}: DeprecationWarning — {deprecations[0][:100]}")
            else:
                result.evidence.append(f"{label}: no DeprecationWarning raised")
        except Exception as failure:
            outcomes.append(f"{label}=FAILED")
            result.evidence.append(f"{label}: {type(failure).__name__} — {str(failure)[:160]}")

    result.verdict = ", ".join(outcomes)
    return result


async def probe_2_cross_case_leakage(factory: ModelFactory, *, live: bool) -> ProbeResult:
    """Can case B observe case A? Measured on what the provider received."""
    result = ProbeResult(number=2, title="Cross-case history leakage", verdict="not run")
    findings: list[str] = []

    # (a) One Agent, reused across two unrelated cases.
    model = factory([scripted_triage(), scripted_triage()])
    system, user_a = triage_prompts(CANARY_A)
    _, user_b = triage_prompts(CANARY_B)
    agent = build_agent(model, system)
    await agent.invoke_async(user_a, structured_output_model=TriageResult)
    first_calls = model.total_calls
    await agent.invoke_async(user_b, structured_output_model=TriageResult)

    leaked_into = [index for index in model.saw(CANARY_A) if index >= first_calls]
    findings.append("reused-agent=LEAKS" if leaked_into else "reused-agent=clean")
    result.evidence.append(
        f"(a) reused Agent: case A's canary appears in request(s) {leaked_into} of "
        f"{model.total_calls}, where requests {first_calls}+ belong to case B"
    )
    result.evidence.append(
        f"(a) corroboration only — agent.messages holds {len(agent.messages)} message(s) "
        f"after two invocations"
    )

    # (b) A fresh Agent per invocation, over one shared model object.
    shared = factory([scripted_triage(), scripted_triage()])
    agent_a = build_agent(shared, system)
    await agent_a.invoke_async(user_a, structured_output_model=TriageResult)
    boundary = shared.total_calls
    agent_b = build_agent(shared, system)
    await agent_b.invoke_async(user_b, structured_output_model=TriageResult)

    fresh_leaks = [index for index in shared.saw(CANARY_A) if index >= boundary]
    findings.append("fresh-agent=LEAKS" if fresh_leaks else "fresh-agent=clean")
    result.evidence.append(
        f"(b) fresh Agent per invocation, shared model: case A's canary appears in "
        f"request(s) {fresh_leaks} of case B's {shared.total_calls - boundary}"
    )
    result.evidence.append(
        f"(b) corroboration only — agent_b.messages holds {len(agent_b.messages)} message(s)"
    )

    # (c) NullConversationManager, for the record.
    from strands.agent.conversation_manager import NullConversationManager

    null_model = factory([scripted_triage(), scripted_triage()])
    null_agent = build_agent(null_model, system, conversation_manager=NullConversationManager())
    await null_agent.invoke_async(user_a, structured_output_model=TriageResult)
    null_boundary = null_model.total_calls
    await null_agent.invoke_async(user_b, structured_output_model=TriageResult)

    null_leaks = [index for index in null_model.saw(CANARY_A) if index >= null_boundary]
    findings.append("null-manager=LEAKS" if null_leaks else "null-manager=clean")
    result.evidence.append(
        f"(c) NullConversationManager: case A's canary appears in request(s) {null_leaks} "
        f"of case B's"
    )
    result.evidence.append(
        "(c) from source: apply_management is a no-op that PRESERVES full history. It is the "
        "'manage nothing' manager, not the 'remember nothing' manager — the name is the "
        "opposite of what it sounds like for this purpose."
    )

    if not live:
        result.evidence.append(
            "offline note: the mock echoes nothing back, so a leak here is the SDK resending "
            "prior turns rather than a model repeating them"
        )

    result.verdict = ", ".join(findings)
    return result


async def probe_3_tool_exposure(factory: ModelFactory, *, live: bool) -> ProbeResult:
    """Does structured output expose a tool that would reach a policy hook?"""
    result = ProbeResult(number=3, title="Tool exposure to a BeforeToolCall hook", verdict="")

    recorder = ToolNameRecorder()
    model = factory([scripted_triage()])
    system, user = triage_prompts(CANARY_A)
    agent = build_agent(model, system, hooks=[recorder])

    before = sorted(agent.tool_registry.registry) + sorted(agent.tool_registry.dynamic_tools)
    try:
        await agent.invoke_async(user, structured_output_model=TriageResult)
    except Exception as failure:
        result.error = f"{type(failure).__name__}: {str(failure)[:160]}"
    after = sorted(agent.tool_registry.registry) + sorted(agent.tool_registry.dynamic_tools)

    result.evidence.append(f"agent.tools declared: {agent.tool_names}")
    result.evidence.append(f"tool registry before invocation: {before or 'empty'}")
    result.evidence.append(f"tool registry after invocation: {after or 'empty'} (cleanup runs)")
    result.evidence.append(f"tool names that reached BeforeToolCallEvent: {recorder.seen}")
    result.evidence.append(
        "tool spec names offered to the provider: "
        f"{sorted({name for request in model.requests for name in request.tool_names})}"
    )

    # And the collision itself: a hook that refuses what it does not recognise. This is CareLoop's
    # gate, in miniature — a deny-by-default engine meeting an SDK-internal tool it has no rule for.
    #
    # The call count is reported alongside the failure on purpose. A first run showed
    # "RecursionError while encoding a JSON object", which reads like an artifact of this file's
    # own json.dumps recording and is in fact a runaway re-prompt: a cancelled structured-output
    # tool never produces the output the loop is waiting for, so the loop forces again. Counting
    # the provider calls is what tells those two apart, and a finding that cannot distinguish the
    # instrument from the subject is not a finding.
    #
    # Bounded at DENY_PROBE_TURNS. Unbounded, the live run measured **245 provider calls and 244
    # hook firings** before Python's recursion limit ended it — a paid runaway, and the reason the
    # bound is here rather than in a note. The number is the finding; re-running the runaway to
    # re-observe it would be buying the same answer twice.
    denier = ToolNameRecorder(cancel_with="REQUIRES_HUMAN_APPROVAL")
    denied_model = factory([scripted_triage()])
    denied_agent = build_agent(denied_model, system, hooks=[denier])
    try:
        denied_result = await denied_agent.invoke_async(
            user,
            structured_output_model=TriageResult,
            limits={"turns": DENY_PROBE_TURNS},
        )
        got = denied_result.structured_output
        result.evidence.append(
            f"with a deny-everything hook attached: structured_output={type(got).__name__}, "
            f"stop_reason={denied_result.stop_reason}, hook saw {denier.seen}"
        )
    except (Exception, RecursionError) as failure:
        result.evidence.append(
            f"with a deny-everything hook attached: {type(failure).__name__} — {str(failure)[:140]}"
        )
    result.evidence.append(
        f"deny-everything run made {denied_model.total_calls} provider call(s) and the hook fired "
        f"{len(denier.seen)} time(s), bounded at turns={DENY_PROBE_TURNS}. UNBOUNDED, the live "
        f"run measured 245 calls / 244 firings before RecursionError — see ADR 0001"
    )

    if recorder.seen:
        result.verdict = (
            f"a tool DOES reach the hook on a zero-tool agent: {sorted(set(recorder.seen))}"
        )
    else:
        result.verdict = "no tool reached the hook"
    if not live:
        result.evidence.append("offline: driven by ToolEmittingMockModel, not by a real provider")
    return result


async def probe_4_call_counts(factory: ModelFactory, *, live: bool) -> ProbeResult:
    """Does the agent path make more provider calls than the direct one?"""
    result = ProbeResult(number=4, title="Provider calls per structured output", verdict="")

    direct = factory([scripted_triage()])
    system, user = triage_prompts(CANARY_A)
    await structured_call(direct, TriageResult, system_prompt=system, user_prompt=user)
    result.evidence.append(
        f"direct path: {direct.structured_calls} structured_output call(s), "
        f"{direct.stream_calls} stream call(s)"
    )

    agent_model = factory([scripted_triage()])
    agent = build_agent(agent_model, system)
    try:
        await agent.invoke_async(user, structured_output_model=TriageResult)
    except Exception as failure:
        result.error = f"{type(failure).__name__}: {str(failure)[:140]}"
    result.evidence.append(
        f"agent path: {agent_model.structured_calls} structured_output call(s), "
        f"{agent_model.stream_calls} stream call(s)"
    )

    forced = [
        index
        for index, request in enumerate(agent_model.requests)
        if request.tool_choice not in ("null", "")
    ]
    result.evidence.append(
        f"requests carrying a tool_choice (the forced-mode re-prompt): {forced or 'none'}"
    )
    if live:
        result.evidence.append(
            f"direct tokens in/out: {direct.input_tokens}/{direct.output_tokens}; "
            f"agent tokens in/out: {agent_model.input_tokens}/{agent_model.output_tokens}"
        )
        result.evidence.append(
            f"direct latency: {sum(direct.latencies_ms):.0f} ms; "
            f"agent latency: {sum(agent_model.latencies_ms):.0f} ms"
        )

    ratio = agent_model.total_calls / max(direct.total_calls, 1)
    result.verdict = (
        f"agent {agent_model.total_calls} call(s) vs direct {direct.total_calls} ({ratio:.1f}x)"
    )
    return result


async def probe_5_limits(factory: ModelFactory, *, live: bool) -> ProbeResult:
    """Do invocation-level limits exist on this path, and what does a normal run use?"""
    result = ProbeResult(number=5, title="Invocation-level limits", verdict="")

    from strands.types.agent import Limits

    result.evidence.append(f"strands.types.agent.Limits keys: {sorted(Limits.__annotations__)}")

    measured = factory([scripted_triage()])
    system, user = triage_prompts(CANARY_A)
    agent = build_agent(measured, system)
    try:
        normal = await agent.invoke_async(user, structured_output_model=TriageResult)
        cycles = normal.metrics.cycle_count
        usage = normal.metrics.accumulated_usage
        result.evidence.append(
            f"measured normal invocation: {cycles} cycle(s), "
            f"{usage.get('inputTokens', 0)} in / {usage.get('outputTokens', 0)} out, "
            f"{measured.total_calls} provider call(s)"
        )
    except Exception as failure:
        result.error = f"{type(failure).__name__}: {str(failure)[:140]}"

    capped = factory([scripted_triage()])
    capped_agent = build_agent(capped, system)
    try:
        limited = await capped_agent.invoke_async(
            user, structured_output_model=TriageResult, limits={"turns": 1}
        )
        result.evidence.append(
            f"with limits={{'turns': 1}}: stop_reason={limited.stop_reason}, "
            f"structured_output={type(limited.structured_output).__name__}, "
            f"{capped.total_calls} provider call(s)"
        )
        result.verdict = f"limits accepted; turns=1 gives stop_reason={limited.stop_reason}"
    except Exception as failure:
        result.evidence.append(
            f"with limits={{'turns': 1}}: {type(failure).__name__} — {str(failure)[:140]}"
        )
        result.verdict = f"limits accepted; turns=1 raises {type(failure).__name__}"
    return result


async def probe_6_metrics(factory: ModelFactory, *, live: bool) -> ProbeResult:
    """Is token usage available? Open decision 16 hinges on the answer."""
    result = ProbeResult(number=6, title="Token usage on each path", verdict="")

    agent_model = factory([scripted_triage()])
    system, user = triage_prompts(CANARY_A)
    agent = build_agent(agent_model, system)
    agent_usage: dict[str, int] = {}
    try:
        agent_result = await agent.invoke_async(user, structured_output_model=TriageResult)
        agent_usage = dict(agent_result.metrics.accumulated_usage)
        result.evidence.append(f"AgentResult.metrics.accumulated_usage: {agent_usage}")
        result.evidence.append(
            f"AgentResult.metrics.cycle_count: {agent_result.metrics.cycle_count}; "
            f"context_size: {agent_result.context_size}"
        )
    except Exception as failure:
        result.error = f"{type(failure).__name__}: {str(failure)[:140]}"

    # Through the proxy, not through ``direct.inner``. The first version reached past the wrapper
    # to the wrapped model, so nothing was recorded and this probe reported "no usage on the
    # direct path" while probe 4 — which goes through ``structured_call`` and therefore through
    # the proxy — reported 1526 in / 88 out for the same call. Two probes disagreeing is what
    # found it; the one that bypassed its own instrument was the one that was wrong.
    direct = factory([scripted_triage()])
    envelopes: list[str] = []
    async for event in direct.structured_output(
        TriageResult,
        [{"role": "user", "content": [{"text": user}]}],
        system_prompt=system,
    ):
        keys = ",".join(sorted(event)) if isinstance(event, dict) else type(event).__name__
        envelopes.append(keys)
    result.evidence.append(f"direct path yielded envelope key sets: {envelopes}")
    result.evidence.append(
        f"direct path usage captured by the recording proxy: {direct.usage or 'none'}"
    )

    reported = bool(agent_usage.get("totalTokens")) or bool(direct.usage)
    result.verdict = (
        "usage available on at least one path" if reported else "no usage reported on either path"
    )
    if not live:
        result.evidence.append(
            "offline: the mock reports zeros by design (invariant 9 — no clock, no drift), so "
            "the live run is the only one whose numbers mean anything"
        )
    return result


PROBES = (
    probe_1_structured_output,
    probe_2_cross_case_leakage,
    probe_3_tool_exposure,
    probe_4_call_counts,
    probe_5_limits,
    probe_6_metrics,
)


# ---------------------------------------------------------------------------------------------
# Spend discipline and entry point
# ---------------------------------------------------------------------------------------------


def load_dotenv(path: pathlib.Path) -> dict[str, str]:
    """Read ``.env`` into a mapping. Values are never printed.

    A local copy of ``bench/run.py``'s reader rather than an import of it: that one belongs to the
    benchmark CLI's argument handling, and the spike needs the twenty lines, not the coupling.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            values[key.strip()] = value
    return values


#: Live calls the six probes make, counted by reading them. Deliberately an over-estimate: a
#: printed figure that turns out to be low is worse than one that turns out to be high.
ESTIMATED_LIVE_CALLS = 24


def print_estimate() -> float:
    """Print what the live half would cost, and return the figure."""
    rates = load_rates()
    system, user = triage_prompts(CANARY_A)
    per_call_in = estimate_tokens(system) + estimate_tokens(user)
    per_call_out = 200
    total_in = per_call_in * ESTIMATED_LIVE_CALLS
    total_out = per_call_out * ESTIMATED_LIVE_CALLS
    cost = rates.cost_usd(LIVE_MODEL_SPEC.split(":", 1)[1], total_in, total_out)
    print(f"Live model:        {LIVE_MODEL_SPEC}")
    print(f"Estimated calls:   {ESTIMATED_LIVE_CALLS}")
    print(f"Estimated tokens:  {total_in} in / {total_out} out")
    print(f"Estimated cost:    ${cost:.4f}")
    return cost


async def run(*, live: bool, factory: ModelFactory) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for probe in PROBES:
        try:
            results.append(await probe(factory, live=live))
        except Exception as failure:  # a probe must not stop the ones after it
            number = int(probe.__name__.split("_")[1])
            results.append(
                ProbeResult(
                    number=number,
                    title=probe.__name__,
                    verdict="probe raised",
                    error=f"{type(failure).__name__}: {failure}",
                )
            )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    # The console this runs on is cp1252. A probe finding is evidence that gets pasted into a
    # findings document, so it has to survive being printed rather than arrive mojibaked.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the probes against Nova 2 Lite instead of the mock",
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required alongside --live; without it, the estimate is printed and nothing is bought",
    )
    parser.add_argument("--json", type=pathlib.Path, help="write the raw results here")
    args = parser.parse_args(argv)

    if args.live:
        print_estimate()
        if not args.confirm_spend:
            print("\nDry run. Re-run with --confirm-spend to make these calls.")
            return 0
        env = load_dotenv(ROOT / ".env")
        if not (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY")):
            print("\n.env carries no AWS key pair; live mode needs one.", file=sys.stderr)
            return 2
        factory = live_factory(env)
        print(f"\nRunning LIVE against {LIVE_MODEL_SPEC}\n")
    else:
        factory = mock_factory
        print("Running OFFLINE against the mock provider ($0)\n")

    results = asyncio.run(run(live=args.live, factory=factory))

    for result in results:
        print(result.render())
        print()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "mode": "live" if args.live else "mock",
                    "model": LIVE_MODEL_SPEC if args.live else "mock",
                    "probes": [result.as_dict() for result in results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Raw results written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
