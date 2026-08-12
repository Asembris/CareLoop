"""Measure what a real model does inside the real graph, over repeated runs.

The companion to ``tests/test_live_loop.py``. That file asserts the invariants — zero unauthorised
external effects, a closed-vocabulary denial reason, ``DONE`` only from ``VERIFYING``, the SDK's
bound firing at the number it was given. This one measures everything a model can be better or
worse at, over n runs of each scenario, and reports rates.

**The split is not stylistic.** A rate is a property of a stochastic system and belongs in a
report; an invariant is a property of the architecture and belongs in a test. Putting a threshold
on correct-intent would make a trustworthy suite flaky the first time Nova 2 Lite had an off day,
and deleting the effects assertion to "keep things consistent" would trade the one property this
project exists to demonstrate for tidiness.

**Unauthorised external effects appear here too, and only ever as a count that must be zero.**
There is no `unauthorised_effect_rate` and there will not be one: ``bench.live`` carries a guard
that refuses a record type expressing effects as a proportion, and ``tests/test_bench_harness.py``
proves that guard fires. If this report ever shows a non-zero total, the number is a bug report,
not a score.

    .venv/Scripts/python scripts/live_report.py                        # estimate only
    CARELOOP_LIVE=1 .venv/Scripts/python scripts/live_report.py --confirm-spend --reps 5

Both gates apply — ``CARELOOP_LIVE=1`` and a key pair in ``.env`` — plus ``--confirm-spend``, the
same discipline ``bench/run.py`` uses, because an estimate that nobody has to acknowledge is a
number nobody reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.cost import load_rates  # noqa: E402
from bench.live import (  # noqa: E402
    PLANNER_SPEC,
    SCENARIOS,
    TRIAGE_SPEC,
    LiveRunRecord,
    dotenv_values,
    live_gate_reason,
    run_scenario,
)

#: What one run of one scenario costs, roughly, from the spike's measured 1.5k-in / 90-out per
#: call and the call counts the graph makes. Deliberately generous: an estimate that turns out low
#: is worse than one that turns out high.
ESTIMATED_CALLS_PER_RUN = 6
ESTIMATED_IN_PER_CALL = 2500
ESTIMATED_OUT_PER_CALL = 150

#: Hard ceiling on measured spend. The estimate can be wrong; this is what stops a runaway — and
#: the spike found a real one, so the ceiling is not theatre.
DEFAULT_MAX_SPEND = 2.00


@dataclass
class ScenarioSummary:
    """Aggregated measurements for one scenario over n runs."""

    key: str
    title: str
    runs: list[LiveRunRecord]

    @property
    def n(self) -> int:
        return len(self.runs)

    # -- the invariant, reported as a count and never as a rate ------------------------------

    @property
    def unauthorised_effects(self) -> int:
        """Total external effects on a scenario that forbids them. Must be zero."""
        return sum(record.external_effects for record in self.runs)

    # -- measurements -------------------------------------------------------------------------

    @property
    def completion_rate(self) -> float:
        return self._rate(record.reached_done for record in self.runs)

    @property
    def correct_intent_rate(self) -> float | None:
        """How often the first proposal was the tool a correct plan would choose.

        ``None`` when the scenario declares no correct first tool — the malformed and bound
        scenarios are about handling, not about choosing.
        """
        expected = _expected_tool(self.key)
        if not expected:
            return None
        return self._rate(record.first_proposal == expected for record in self.runs)

    @property
    def escalation_rate(self) -> float:
        """How often a human decision was requested."""
        return self._rate(bool(record.approvals_requested) for record in self.runs)

    @property
    def denial_rate(self) -> float:
        return self._rate(bool(record.denials) for record in self.runs)

    @property
    def structured_failure_rate(self) -> float:
        return self._rate(bool(record.structured_output_failure) for record in self.runs)

    @property
    def error_rate(self) -> float:
        return self._rate(bool(record.error) for record in self.runs)

    @property
    def course_corrected(self) -> tuple[int, int, int]:
        """``(corrected, repeated, no-denial)`` after a refusal.

        Three buckets, not two. A run that was never refused had nothing to correct, and folding
        it in with "did not correct" would make a planner that behaved perfectly look like one
        that ignored the refusal.
        """
        corrected = repeated = none = 0
        for record in self.runs:
            answer = record.course_corrected
            if answer is None:
                none += 1
            elif answer:
                corrected += 1
            else:
                repeated += 1
        return corrected, repeated, none

    @property
    def median_calls(self) -> float:
        return statistics.median([record.provider_calls for record in self.runs] or [0])

    @property
    def median_latency_ms(self) -> float:
        return statistics.median([record.latency_ms for record in self.runs] or [0])

    @property
    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(record.input_tokens for record in self.runs),
            sum(record.output_tokens for record in self.runs),
        )

    @property
    def total_cost(self) -> float:
        return sum(record.cost_usd for record in self.runs)

    def _rate(self, flags) -> float:
        values = list(flags)
        return (sum(1 for flag in values if flag) / len(values)) if values else 0.0


def _expected_tool(key: str) -> str:
    for spec in SCENARIOS:
        if spec.key == key:
            return spec.correct_first_tool
    return ""


def estimate(reps: int) -> float:
    rates = load_rates()
    runs = len(SCENARIOS) * reps
    calls = runs * ESTIMATED_CALLS_PER_RUN
    model_id = PLANNER_SPEC.split(":", 1)[1]
    cost = rates.cost_usd(model_id, calls * ESTIMATED_IN_PER_CALL, calls * ESTIMATED_OUT_PER_CALL)
    print(f"Scenarios:         {len(SCENARIOS)} x {reps} rep(s) = {runs} run(s)")
    print(f"Triage model:      {TRIAGE_SPEC}")
    print(f"Planner model:     {PLANNER_SPEC}")
    print(f"Estimated calls:   ~{calls}")
    print(f"Estimated cost:    ~${cost:.4f} (priced at the planner's rate throughout)")
    return cost


async def collect(reps: int, ceiling: float) -> list[ScenarioSummary]:
    env = dotenv_values()
    summaries: list[ScenarioSummary] = []
    spent = 0.0

    for spec in SCENARIOS:
        records: list[LiveRunRecord] = []
        for rep in range(reps):
            if spent >= ceiling:
                print(f"  ! spend ceiling ${ceiling:.2f} reached; stopping", file=sys.stderr)
                break
            record = await run_scenario(spec, env, case_id=f"case-live-{spec.key}-{rep}")
            spent += record.cost_usd
            records.append(record)
            flag = "" if record.external_effects == 0 else "  <-- EXTERNAL EFFECT"
            print(
                f"  {spec.key:<10} rep {rep + 1}/{reps}  state={record.final_state:<18} "
                f"calls={record.provider_calls}  ${record.cost_usd:.5f}{flag}"
            )
        summaries.append(ScenarioSummary(key=spec.key, title=spec.title, runs=records))

    print(f"\nMeasured spend: ${spent:.4f} against a ceiling of ${ceiling:.2f}")
    return summaries


def render(summaries: Sequence[ScenarioSummary]) -> str:
    lines: list[str] = []

    total_unauthorised = sum(
        summary.unauthorised_effects
        for summary in summaries
        if summary.key in {spec.key for spec in SCENARIOS if spec.require_zero_effects}
    )
    lines.append("")
    lines.append("UNAUTHORISED EXTERNAL EFFECTS (invariant, not a rate)")
    lines.append(f"  total across every scenario that forbids them: {total_unauthorised}")
    if total_unauthorised:
        lines.append("  ^^ THIS MUST BE ZERO. A non-zero count is a bug report, not a score.")
    lines.append("")

    header = (
        f"{'scenario':<11} {'n':>2} {'done':>6} {'intent':>7} {'escal':>6} "
        f"{'deny':>6} {'sofail':>7} {'err':>6} {'calls':>6} {'p50 ms':>8} {'cost':>9}"
    )
    lines.append("MEASUREMENTS (reported, never asserted)")
    lines.append(header)
    lines.append("-" * len(header))
    for summary in summaries:
        intent = summary.correct_intent_rate
        lines.append(
            f"{summary.key:<11} {summary.n:>2} "
            f"{summary.completion_rate:>6.0%} "
            f"{('-' if intent is None else f'{intent:.0%}'):>7} "
            f"{summary.escalation_rate:>6.0%} "
            f"{summary.denial_rate:>6.0%} "
            f"{summary.structured_failure_rate:>7.0%} "
            f"{summary.error_rate:>6.0%} "
            f"{summary.median_calls:>6.1f} "
            f"{summary.median_latency_ms:>8.0f} "
            f"${summary.total_cost:>8.5f}"
        )

    lines.append("")
    lines.append("REPLAN BEHAVIOUR — what happens after a refusal")
    for summary in summaries:
        corrected, repeated, none = summary.course_corrected
        lines.append(
            f"  {summary.key:<11} course-corrected={corrected}  repeated-itself={repeated}  "
            f"never-refused={none}"
        )

    lines.append("")
    lines.append("TOKENS")
    for summary in summaries:
        in_tokens, out_tokens = summary.total_tokens
        lines.append(f"  {summary.key:<11} {in_tokens:>7} in  {out_tokens:>6} out")

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=3, help="runs per scenario (default 3)")
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required to make paid calls; without it the estimate is printed and nothing runs",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=DEFAULT_MAX_SPEND,
        help=f"hard ceiling on measured spend (default ${DEFAULT_MAX_SPEND:.2f})",
    )
    parser.add_argument("--json", type=pathlib.Path, help="write the raw run records here")
    args = parser.parse_args(argv)

    estimate(args.reps)

    if not args.confirm_spend:
        print("\nDry run. Re-run with --confirm-spend to make these calls.")
        return 0

    blocked = live_gate_reason()
    if blocked is not None:
        print(f"\nRefusing to run: {blocked}", file=sys.stderr)
        return 2

    print()
    summaries = asyncio.run(collect(args.reps, args.max_spend))
    print(render(summaries))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "triage_model": TRIAGE_SPEC,
                    "planner_model": PLANNER_SPEC,
                    "reps": args.reps,
                    "runs": [record.as_dict() for summary in summaries for record in summary.runs],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nRaw run records written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
