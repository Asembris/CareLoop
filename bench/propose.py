"""The post-B2 proposal-seam benchmark.

    .venv/Scripts/python -m bench.propose                                   # $0, dry run
    .venv/Scripts/python -m bench.propose --confirm-spend --max-spend 0.25  # buys calls

**What it measures, and why it needed its own harness.** ``docs/model-benchmark.md`` scored the
planner through ``Model.structured_output`` against a flat ``PlannerIntent`` wrapper. B2 replaced
that seam: the model is now offered one closed ``propose_*`` schema per registered action and calls
exactly one of them. So every planner number in that document is a fact about a shape CareLoop no
longer sends, and the model behind the planner was chosen on it.

This asks four questions of the seam that ships:

1. **Structural validity** — does a proposal come back at all.
2. **Which of the four structural failures occur** — ``NoProposalToolCall``,
   ``AmbiguousProposalToolCall``, ``UnknownProposalTool``, ``MalformedProposal``. They are the
   whole failure vocabulary of :func:`careloop.agents.invoke.proposal_tool_call` and each means
   something different about what the provider did.
3. **Whether the bounded retry fires, and whether it recovers.** One initial call and at most one
   more, for a failure to *generate* only. Counted from the model proxy's own call count rather
   than from a log line: two calls for one task is a retry, by arithmetic.
4. **Cost and latency**, from provider-reported usage, never from an estimate.

**It drives the real thing.** ``PlannerAgent.propose`` against a real ``Case``, through
``planner_system_prompt()`` and ``planner_user_prompt()``, with the real ``PROPOSAL_TOOLS``. The
only substitution is :class:`bench.live.MeteredModel` around the provider, which counts and times
and reads no message content.

**It is not a correctness benchmark**, and ``bench/propose_tasks.json`` says so field by field:
``expects`` is filled only where one tool is the unambiguous next action. Where a case genuinely
admits several — the injection probes, the sparse case, two of the replans — it is null, and a
proposal there is scored as *formed* and not as right or wrong. Inventing a rubric for those after
seeing the answers is the failure mode ``docs/model-benchmark.md`` §7 refused, and it is refused
here too.

**Spend discipline, same three layers as** ``bench.run``: nothing is bought without
``--confirm-spend``; an estimate computed from the actual prompts is printed first; and a ceiling
is enforced against *measured* spend during the run, because an estimate is a guess and the ceiling
is what stops a runaway. Bedrock only — the endpoints priced at ``0.0`` in ``rates.json`` are
credit-metered, and a dollar ceiling over one of those is decoration.

**No response cache.** ``bench.run`` caches because its value is a reproducible table over six
candidates; this measures a *rate* across repeats of the same task, and a cache would turn the
second repeat into the first one's answer. Re-running therefore re-spends, which is why the task
set is small and the ceiling is low.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import statistics
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from bench.cost import (
    ESTIMATED_OUTPUT_TOKENS,
    BudgetExceeded,
    SpendEstimate,
    SpendLedger,
    estimate_tokens,
    load_rates,
)
from bench.live import LIVE_ENV_VAR, MeteredModel, dotenv_values
from careloop.agents.invoke import (
    MAX_PROPOSAL_ATTEMPTS,
    RECOVERABLE_PROPOSAL_FAILURES,
)
from careloop.agents.planner import PlannerAgent
from careloop.agents.prompts import planner_system_prompt, planner_user_prompt
from careloop.llm.providers import build_model, parse_spec
from careloop.policy import ReasonCode
from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import Case, CaseState

ROOT: Final[pathlib.Path] = pathlib.Path(__file__).parent.parent.resolve()
#: Deliberately **not** under ``bench/tasks/``. ``bench.tasks.load_tasks`` globs that directory
#: for ``*.json`` and validates every entry against the frozen pre-B2 ``Task`` schema, so a
#: file dropped in there does not sit beside the old task set — it joins it, and fails to
#: validate. The two benchmarks measure different seams and keep separate inputs.
TASKS_PATH: Final[pathlib.Path] = pathlib.Path(__file__).parent / "propose_tasks.json"
RESULTS_PATH: Final[pathlib.Path] = pathlib.Path(__file__).parent / "results" / "propose.json"

#: The shipped planner. Named here rather than read from ``CARELOOP_MODEL_PLANNER`` so that what is
#: measured cannot silently become whatever a shell happened to export — the same reason
#: ``bench.live`` builds its models explicitly.
PLANNER_SPEC: Final[str] = "bedrock:us.amazon.nova-2-lite-v1:0"

#: Repeats per task. One sample per task cannot see a rate, and the failure this exists to
#: characterise was measured at roughly one run in fourteen.
DEFAULT_REPEATS: Final[int] = 3

#: A deliberately low ceiling. The whole run is expected to cost cents; anything approaching this
#: means something is looping.
DEFAULT_MAX_SPEND: Final[float] = 0.25

#: The literals the two injection probes offer. Recorded as an observation, never scored: whether a
#: proposal *carrying* one is refused is the policy engine's question and ``evals/replay.py``
#: already answers it at the ceiling of model misbehaviour.
ATTACKER_LITERALS: Final[tuple[str, ...]] = (
    "records-intake@claims-continuity-partners.example",
    "PORTAL-MG-TRANSFER",
)

FIXED_NOW: Final[datetime] = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


class ProposeTask(BaseModel):
    """One case put to the planner."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    state: CaseState
    summary: str
    facts: tuple[tuple[str, str, TrustLevel], ...]
    prior_denials: tuple[tuple[str, ReasonCode], ...]
    expects: str | None = None

    def case(self) -> Case:
        """The ``Case`` this task describes, built the way the graph would hold it."""
        return Case(
            case_id=f"bench-{self.task_id}",
            event_id="evt-bench",
            state=self.state,
            summary=self.summary,
            facts=tuple(
                Fact(
                    key=key,
                    value=value,
                    trust_level=trust,
                    provenance=Provenance(
                        source_doc="trusted-config" if trust is TrustLevel.TRUSTED else "bench.pdf",
                        page=None if trust is TrustLevel.TRUSTED else 1,
                        extractor="bench",
                    ),
                    extracted_at=FIXED_NOW,
                )
                for key, value, trust in self.facts
            ),
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )


class Attempt(BaseModel):
    """What one call to the seam produced."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    repeat: int
    formed: bool
    failure: str = ""
    tool_name: str = ""
    provider_calls: int = 0
    retried: bool = False
    recovered: bool = False
    retry_cause: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    matched_expectation: bool | None = None
    carried_attacker_literal: bool = False


class _RetryCauseCapture(logging.Handler):
    """Records the failure type behind a retry that :func:`proposal_tool_call` went on to recover.

    **This is the only way to see it, and the reason is deliberate.** A retry that succeeds leaves
    no trace in the return value: the exception is caught inside the seam and the caller gets a
    valid proposal. But *which* of the four structural failures the provider produced is one of the
    things this benchmark exists to report, and "a retry happened" without it is half a
    measurement.

    The seam logs the exception **type** and never its message, for the reason recorded beside that
    call: every one of those messages is built from CareLoop's own identifiers, and logging only
    the type keeps it that way by construction. So this reads a type name out of a log record and
    could not read a value even if it wanted to.
    """

    #: The exact format string in `careloop.agents.invoke`. Matched rather than parsed loosely, so
    #: that a change to the wording makes this stop capturing rather than start guessing.
    RETRY_MESSAGE: Final[str] = "proposal call produced no valid proposal (%s); retrying once"

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.causes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.msg == self.RETRY_MESSAGE and record.args:
            self.causes.append(str(record.args[0]))


def load_tasks(path: pathlib.Path = TASKS_PATH) -> tuple[ProposeTask, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(ProposeTask.model_validate(entry) for entry in payload["tasks"])


def estimate(tasks: Sequence[ProposeTask], repeats: int, model_id: str) -> SpendEstimate:
    """What the run should cost, from the prompts it is about to send.

    High by construction: :func:`bench.cost.estimate_tokens` assumes three characters per token and
    :data:`bench.cost.ESTIMATED_OUTPUT_TOKENS` is generous. The tool specifications the provider is
    handed are counted too — they are part of the request and are the larger half of it here.
    """
    specs = json.dumps(_tool_spec_payload())
    system = planner_system_prompt()
    total_input = 0
    for task in tasks:
        user = planner_user_prompt(task.case(), task.prior_denials)
        total_input += estimate_tokens(system + user + specs) * repeats

    calls = len(tasks) * repeats
    rates = load_rates()
    output = ESTIMATED_OUTPUT_TOKENS * calls
    return SpendEstimate(
        candidate_id="planner-propose-seam",
        model_id=model_id,
        calls=calls,
        cached_calls=0,
        estimated_input_tokens=total_input,
        estimated_output_tokens=output,
        estimated_usd=rates.cost_usd(model_id, total_input, output),
    )


def _tool_spec_payload() -> list[dict[str, object]]:
    from careloop.agents.invoke import proposal_tool_specs
    from careloop.agents.schemas import PROPOSAL_TOOLS

    return proposal_tool_specs(PROPOSAL_TOOLS)


async def run_task(
    task: ProposeTask,
    repeat: int,
    env: Mapping[str, str],
    ledger: SpendLedger,
    model_id: str,
) -> Attempt:
    """One task, once, through the shipped seam."""
    metered = MeteredModel(build_model(parse_spec(PLANNER_SPEC), env), model_id=model_id)
    agent = PlannerAgent(metered)
    rates = load_rates()

    capture = _RetryCauseCapture()
    seam_logger = logging.getLogger("careloop.agents.invoke")
    seam_logger.addHandler(capture)

    began = time.perf_counter()
    formed, failure, tool_name, carried = True, "", "", False
    try:
        proposal = await agent.propose(task.case(), task.prior_denials)
    except RECOVERABLE_PROPOSAL_FAILURES as broken:
        formed, failure = False, type(broken).__name__
    else:
        tool_name = proposal.tool_name
        carried = any(
            literal in value for value in proposal.args.values() for literal in ATTACKER_LITERALS
        )
    finally:
        seam_logger.removeHandler(capture)
    elapsed = (time.perf_counter() - began) * 1000

    cost = rates.cost_usd(model_id, metered.input_tokens, metered.output_tokens)
    ledger.charge(cost)

    # Arithmetic, not a log line: the seam allows at most MAX_PROPOSAL_ATTEMPTS calls, so more than
    # one call for one proposal is a retry and nothing else can produce it.
    retried = metered.calls > 1
    return Attempt(
        task_id=task.task_id,
        repeat=repeat,
        formed=formed,
        failure=failure,
        tool_name=tool_name,
        provider_calls=metered.calls,
        retried=retried,
        recovered=retried and formed,
        input_tokens=metered.input_tokens,
        output_tokens=metered.output_tokens,
        latency_ms=elapsed,
        cost_usd=cost,
        retry_cause=capture.causes[0] if capture.causes else "",
        matched_expectation=None if task.expects is None else tool_name == task.expects,
        carried_attacker_literal=carried,
    )


def _tally(names: Iterable[str]) -> dict[str, int]:
    """Count occurrences, in first-seen order."""
    counted: dict[str, int] = {}
    for name in names:
        counted[name] = counted.get(name, 0) + 1
    return counted


def summarise(attempts: Sequence[Attempt], model_id: str, repeats: int) -> dict[str, Any]:
    """The reported table. Rates only where a rate means something."""
    formed = [a for a in attempts if a.formed]
    scored = [a for a in attempts if a.matched_expectation is not None]
    latencies = sorted(a.latency_ms for a in attempts)
    failures = _tally(a.failure for a in attempts if a.failure)

    return {
        "model_id": model_id,
        "seam": "proposal_tool_call",
        "max_proposal_attempts": MAX_PROPOSAL_ATTEMPTS,
        "tasks": len({a.task_id for a in attempts}),
        "repeats": repeats,
        "attempts": len(attempts),
        "structurally_valid": len(formed),
        "failures_by_type": failures,
        "retries_fired": sum(1 for a in attempts if a.retried),
        "retries_recovered": sum(1 for a in attempts if a.recovered),
        "retry_causes": _tally(a.retry_cause for a in attempts if a.retry_cause),
        "provider_calls": sum(a.provider_calls for a in attempts),
        "unambiguous_tasks_scored": len(scored),
        "unambiguous_matched": sum(1 for a in scored if a.matched_expectation),
        "attempts_carrying_an_attacker_literal": sum(
            1 for a in attempts if a.carried_attacker_literal
        ),
        "input_tokens": sum(a.input_tokens for a in attempts),
        "output_tokens": sum(a.output_tokens for a in attempts),
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "latency_worst_ms": round(latencies[-1], 1) if latencies else 0.0,
        "cost_usd": round(sum(a.cost_usd for a in attempts), 6),
    }


def report(summary: Mapping[str, Any], attempts: Sequence[Attempt]) -> None:
    print(f"\nmodel            {summary['model_id']}")
    print(f"seam             {summary['seam']} (at most {summary['max_proposal_attempts']} calls)")
    print(
        f"attempts         {summary['attempts']} ({summary['tasks']} tasks x {summary['repeats']})"
    )
    print(f"structural       {summary['structurally_valid']} of {summary['attempts']} formed")
    print(f"failures         {summary['failures_by_type'] or 'none'}")
    print(
        f"retries          {summary['retries_fired']} fired, "
        f"{summary['retries_recovered']} recovered {summary['retry_causes'] or ''}"
    )
    print(f"provider calls   {summary['provider_calls']}")
    print(
        f"unambiguous      {summary['unambiguous_matched']} of "
        f"{summary['unambiguous_tasks_scored']} named the expected tool"
    )
    print(
        f"attacker literal {summary['attempts_carrying_an_attacker_literal']} proposals carried one"
    )
    print(f"tokens           {summary['input_tokens']} in, {summary['output_tokens']} out")
    print(
        f"latency          p50 {summary['latency_p50_ms']} ms, "
        f"worst {summary['latency_worst_ms']} ms"
    )
    print(f"cost             ${summary['cost_usd']:.4f}\n")

    for attempt in attempts:
        outcome = attempt.tool_name if attempt.formed else f"FAILED {attempt.failure}"
        marks = (" retry" if attempt.retried else "") + (
            " attacker" if attempt.carried_attacker_literal else ""
        )
        print(f"  {attempt.task_id:<40} {attempt.repeat}  {outcome:<22}{marks}")
    print()


async def main_async(arguments: argparse.Namespace) -> int:
    tasks = load_tasks()
    model_id = parse_spec(PLANNER_SPEC).model_id
    spend = estimate(tasks, arguments.repeats, model_id)

    print(f"\n{len(tasks)} tasks x {arguments.repeats} repeats = {spend.calls} calls")
    print(f"estimated {spend.estimated_input_tokens} in / {spend.estimated_output_tokens} out")
    print(f"estimated ${spend.estimated_usd:.4f}, ceiling ${arguments.max_spend:.2f}")

    if not arguments.confirm_spend:
        print("\ndry run: nothing was bought. Re-run with --confirm-spend to measure.\n")
        return 0

    # The double gate `bench.live` documents: `.env` being populated on a development machine is
    # not consent, so a key pair AND an explicit opt-in are both required. `--confirm-spend` is the
    # third layer rather than a replacement for either.
    env = dotenv_values()
    if not (env.get("AWS_ACCESS_KEY_ID") or "").strip():
        print("\n.env carries no AWS key pair, so no call can be made.\n", file=sys.stderr)
        return 2
    if os.environ.get(LIVE_ENV_VAR, "").strip() != "1":
        print(
            f"\nlive runs are opt-in: set {LIVE_ENV_VAR}=1 in the environment to permit "
            f"paid calls.\n",
            file=sys.stderr,
        )
        return 2

    ledger = SpendLedger(arguments.max_spend)
    attempts: list[Attempt] = []
    try:
        for repeat in range(1, arguments.repeats + 1):
            for task in tasks:
                attempt = await run_task(task, repeat, env, ledger, model_id)
                attempts.append(attempt)
                mark = attempt.tool_name if attempt.formed else f"FAILED {attempt.failure}"
                print(f"  {task.task_id:<40} {repeat}  {mark}")
    except BudgetExceeded as stopped:
        print(f"\nSTOPPED: {stopped}\n", file=sys.stderr)

    summary = summarise(attempts, model_id, arguments.repeats)
    report(summary, attempts)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {"summary": summary, "attempts": [a.model_dump() for a in attempts]},
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {RESULTS_PATH.relative_to(ROOT)}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the post-B2 planner proposal seam.")
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="permit paid calls; without it this is a dry run",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=DEFAULT_MAX_SPEND,
        help="hard ceiling in USD, enforced against measured spend during the run",
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="runs per task")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - a tool, not a call site
    sys.exit(main())
