"""Executing the benchmark: cache lookup, model call, scoring.

Separated from ``run.py`` so that everything except the actual network call is testable offline.
The one function that touches a provider is :func:`invoke_once`, and it is the only thing the
offline tests stub.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from bench.cache import CachedCall, ResponseCache, call_key
from bench.candidates import Candidate
from bench.cost import RateTable, SpendLedger, estimate_tokens
from bench.prompts import system_prompt_for, user_prompt_for
from bench.scoring import TaskScore, score_task
from bench.tasks import Task, TaskSet
from careloop.llm.providers import build_model, parse_spec

#: Sampling parameters, identical for every candidate. Temperature zero because the benchmark is
#: about capability, not about sampling luck, and because a cached answer should be the answer the
#: model would give again.
PARAMS: dict[str, float | int] = {"temperature": 0.0, "max_tokens": 512}

#: Failures that are genuinely the model's answer, and are therefore worth caching as a result.
#:
#: Everything else — no credentials, connection reset, TLS failure, throttling — says nothing about
#: the model and must not be written to the cache, or a transient outage would be frozen into the
#: results table and every later run would replay it without retrying. That is not hypothetical:
#: the first live run cached 68 ``NoCredentialsError`` entries as though four candidates had failed
#: the benchmark.
MODEL_LEVEL_FAILURES: frozenset[str] = frozenset(
    {
        "ValidationError",
        "WrongOutputType",
        "ValueError",
        "ContextWindowOverflowException",
    }
)


def is_model_failure(error_type: str) -> bool:
    """Whether a failure is the model's answer rather than a failure to reach it."""
    return error_type in MODEL_LEVEL_FAILURES


class CallOutcome(BaseModel):
    """What one live call produced."""

    ok: bool
    output: dict | None = None
    error_type: str = ""
    error_message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    latency_ms: float = 0.0
    provider_latency_ms: float = 0.0


class _Driven(BaseModel):
    """Everything one structured-output call yielded, not just the instance."""

    model_config = {"arbitrary_types_allowed": True}

    instance: BaseModel
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    provider_latency_ms: float = 0.0


async def _drive(model, output_model: type[BaseModel], system: str, prompt: str) -> _Driven:
    """Consume the whole event stream, keeping the usage the ``stop`` event carries.

    ``structured_output`` yields every stream event before the final ``{"output": ...}``, and the
    ``stop`` event's third element is the provider's own token count. Taking the count from the
    provider rather than estimating it is what makes the cost column a measurement.
    """
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    result: BaseModel | None = None
    usage: dict = {}
    metrics: dict = {}
    async for event in model.structured_output(output_model, messages, system_prompt=system):
        if "stop" in event and isinstance(event["stop"], tuple) and len(event["stop"]) >= 4:
            usage = event["stop"][2] or {}
            metrics = event["stop"][3] or {}
        elif "metadata" in event:
            # Belt and braces: providers other than Bedrock may report usage this way instead.
            usage = event["metadata"].get("usage", usage) or usage
            metrics = event["metadata"].get("metrics", metrics) or metrics
        if "output" in event:
            result = event["output"]
    if result is None:
        raise RuntimeError("provider yielded no output envelope")

    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)
    if input_tokens or output_tokens:
        return _Driven(
            instance=result,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_latency_ms=float(metrics.get("latencyMs", 0) or 0),
        )

    # The provider reported nothing. Estimate, and say so — a cost column silently reading $0.0000
    # would make the most expensive candidate look free.
    return _Driven(
        instance=result,
        input_tokens=estimate_tokens(system) + estimate_tokens(prompt),
        output_tokens=estimate_tokens(result.model_dump_json()),
        tokens_estimated=True,
        provider_latency_ms=float(metrics.get("latencyMs", 0) or 0),
    )


def invoke_once(
    candidate: Candidate, task: Task, system: str, prompt: str, env=None
) -> CallOutcome:
    """Make one real structured-output call. The only function here that touches a network."""
    spec = parse_spec(candidate.spec)
    model = build_model(spec, env)
    model.update_config(**PARAMS)

    started = time.perf_counter()
    try:
        driven = asyncio.run(_drive(model, task.output_model, system, prompt))
    except Exception as exc:
        return CallOutcome(
            ok=False,
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    latency_ms = (time.perf_counter() - started) * 1000

    if not isinstance(driven.instance, task.output_model):
        return CallOutcome(
            ok=False,
            error_type="WrongOutputType",
            error_message=f"provider returned {type(driven.instance).__name__}",
            latency_ms=latency_ms,
        )
    try:
        payload = driven.instance.model_dump()
    except ValidationError as exc:  # pragma: no cover - defensive
        return CallOutcome(
            ok=False,
            error_type="ValidationError",
            error_message=str(exc)[:500],
            latency_ms=latency_ms,
        )

    return CallOutcome(
        ok=True,
        output=payload,
        latency_ms=latency_ms,
        provider_latency_ms=driven.provider_latency_ms,
        input_tokens=driven.input_tokens,
        output_tokens=driven.output_tokens,
        tokens_estimated=driven.tokens_estimated,
    )


def resolve_model_id(candidate: Candidate, env=None) -> str:
    """The concrete model id a candidate resolves to, for rate lookup and the results table."""
    spec = parse_spec(candidate.spec)
    if spec.provider == "bedrock":
        from careloop.llm.providers import resolve_bedrock_model_id

        return resolve_bedrock_model_id(spec, env)
    return spec.model_id


def key_for(candidate: Candidate, task: Task, system: str, prompt: str, model_id: str) -> str:
    schema = task.output_model
    return call_key(
        candidate_id=candidate.candidate_id,
        model_id=model_id,
        base_url=parse_spec(candidate.spec).base_url,
        task_id=task.task_id,
        system_prompt=system,
        prompt=prompt,
        schema_name=schema.__name__,
        schema_fields=tuple(sorted(schema.model_fields)),
        params=dict(PARAMS),
    )


def run_candidate(
    candidate: Candidate,
    tasks: TaskSet,
    cache: ResponseCache,
    rates: RateTable,
    ledger: SpendLedger,
    *,
    allow_spend: bool,
    env=None,
    invoke: Callable[..., CallOutcome] = invoke_once,
    on_event: Callable[[str], None] = lambda _msg: None,
) -> list[TaskScore]:
    """Score one candidate over every task it is measured on.

    Cache hits cost nothing and are never re-bought. A miss with ``allow_spend`` false is recorded
    as a skip rather than silently executed.
    """
    model_id = resolve_model_id(candidate, env)
    scores: list[TaskScore] = []

    for task in tasks:
        if task.kind not in candidate.kinds:
            continue

        system = system_prompt_for(task)
        prompt = user_prompt_for(task)
        key = key_for(candidate, task, system, prompt, model_id)

        cached = cache.get(candidate.candidate_id, key)
        if cached is None:
            if not allow_spend:
                on_event(f"    SKIP  {task.task_id} (cache miss, spending not confirmed)")
                continue
            outcome = invoke(candidate, task, system, prompt, env)
            cached = CachedCall(
                key=key,
                candidate_id=candidate.candidate_id,
                task_id=task.task_id,
                model_id=model_id,
                ok=outcome.ok,
                output=outcome.output,
                error_type=outcome.error_type,
                error_message=outcome.error_message,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                tokens_estimated=outcome.tokens_estimated,
                latency_ms=outcome.latency_ms,
                provider_latency_ms=outcome.provider_latency_ms,
            )
            if cached.ok or is_model_failure(cached.error_type):
                cache.put(cached)
            else:
                on_event(
                    f"    ....  {task.task_id} not cached: {cached.error_type} is a failure to "
                    f"reach the model, not a result"
                )
            cost = rates.cost_usd(model_id, cached.input_tokens, cached.output_tokens)
            ledger.charge(cost)
            status = "ok  " if cached.ok else "FAIL"
            on_event(
                f"    {status}  {task.task_id}  {cached.latency_ms:7.0f} ms  "
                f"${cost:.6f}" + ("" if cached.ok else f"  {cached.error_type}")
            )
        else:
            cost = rates.cost_usd(model_id, cached.input_tokens, cached.output_tokens)
            on_event(f"    hit   {task.task_id}  (cached, $0)")

        scores.append(score_task(task, cached, cost))

    return scores


__all__ = [
    "PARAMS",
    "CallOutcome",
    "invoke_once",
    "key_for",
    "resolve_model_id",
    "run_candidate",
]
