"""Benchmark CLI.

Cost discipline is enforced here, in three layers, because the OpenAI balance is small and not
replaceable:

1. **Nothing is bought without ``--confirm-spend``.** The default run is a dry run: it reports
   what it would cost and scores whatever is already cached.
2. **An estimate is printed before any paid call**, computed from the actual prompts about to be
   sent rather than from a guess about their size.
3. **A hard ceiling is enforced against measured spend during the run** (``--max-spend``, default
   $1.00). The estimate can be wrong; the ceiling is what actually stops a runaway.

Cache hits never re-spend, so re-running an unchanged task set costs nothing and needs no flag.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections.abc import Mapping

from bench import candidates as candidates_module
from bench.cache import ResponseCache
from bench.candidates import Candidate
from bench.cost import (
    ESTIMATED_OUTPUT_TOKENS,
    BudgetExceeded,
    RateTable,
    SpendEstimate,
    SpendLedger,
    estimate_tokens,
    load_rates,
)
from bench.prompts import system_prompt_for, user_prompt_for
from bench.runner import key_for, resolve_model_id, run_candidate
from bench.scoring import CandidateReport, aggregate
from bench.tasks import TaskSet, load_tasks

RESULTS_DIR = pathlib.Path(__file__).parent / "results"
DEFAULT_MAX_SPEND = 1.00


def load_dotenv(path: pathlib.Path) -> dict[str, str]:
    """Read ``.env`` into a mapping. Values are never printed."""
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


def estimate_candidate(
    candidate: Candidate,
    tasks: TaskSet,
    cache: ResponseCache,
    rates: RateTable,
    env: Mapping[str, str],
) -> SpendEstimate:
    """What this candidate would cost, counting only the calls that are not already cached."""
    model_id = resolve_model_id(candidate, env)
    calls = 0
    cached = 0
    in_tokens = 0
    out_tokens = 0

    for task in tasks:
        if task.kind not in candidate.kinds:
            continue
        calls += 1
        system = system_prompt_for(task)
        prompt = user_prompt_for(task)
        if cache.get(candidate.candidate_id, key_for(candidate, task, system, prompt, model_id)):
            cached += 1
            continue
        in_tokens += estimate_tokens(system) + estimate_tokens(prompt)
        out_tokens += ESTIMATED_OUTPUT_TOKENS

    return SpendEstimate(
        candidate_id=candidate.candidate_id,
        model_id=model_id,
        calls=calls,
        cached_calls=cached,
        estimated_input_tokens=in_tokens,
        estimated_output_tokens=out_tokens,
        estimated_usd=rates.cost_usd(model_id, in_tokens, out_tokens),
    )


def format_report(report: CandidateReport) -> str:
    def pct(value: float | None) -> str:
        return "  n/a " if value is None else f"{value * 100:5.1f}%"

    def ms(value: float | None) -> str:
        return "   n/a" if value is None else f"{value:6.0f}"

    return (
        f"  {report.label:<38} "
        f"struct {pct(report.structured_rate)}  "
        f"tool {pct(report.tool_rate)}  "
        f"args {pct(report.args_rate)}  "
        f"triage {pct(report.triage_rate)}  "
        f"extract {pct(report.extraction_rate)}  "
        f"inject {pct(report.injection_rate)}  "
        f"p50 {ms(report.latency_median_ms)}ms  "
        f"max {ms(report.latency_worst_ms)}ms  "
        f"${report.total_cost_usd:.4f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CareLoop model-provider benchmark")
    parser.add_argument("--candidates", nargs="*", default=None, help="candidate ids; default all")
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="permit paid calls. Without it, only cached responses are scored.",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=DEFAULT_MAX_SPEND,
        help=f"hard ceiling in USD, enforced against measured spend (default {DEFAULT_MAX_SPEND})",
    )
    parser.add_argument("--env-file", type=pathlib.Path, default=pathlib.Path(".env"))
    parser.add_argument("--results", type=pathlib.Path, default=RESULTS_DIR / "summary.json")
    args = parser.parse_args(argv)

    env: dict[str, str] = {**os.environ, **load_dotenv(args.env_file)}
    tasks = load_tasks()
    rates = load_rates()
    cache = ResponseCache()

    try:
        selected = candidates_module.resolve(args.candidates)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Task set: {len(tasks)} tasks {sorted(tasks.kinds)}")
    print(f"Rates:    {rates.as_of}, {rates.region}, {rates.currency} {rates.unit}")
    print()

    print("Estimated cost of uncached calls:")
    estimates = [estimate_candidate(c, tasks, cache, rates, env) for c in selected]
    total_estimate = sum(e.estimated_usd for e in estimates)
    for est in estimates:
        print(
            f"  {est.candidate_id:<24} {est.billable_calls:>3} to buy, "
            f"{est.cached_calls:>3} cached   ~${est.estimated_usd:.4f}"
        )
    print(f"  {'TOTAL':<24} {'':>3}            ~${total_estimate:.4f}")
    print()

    if not args.confirm_spend:
        billable = sum(e.billable_calls for e in estimates)
        if billable:
            print(
                f"DRY RUN. {billable} call(s) would be bought for about ${total_estimate:.4f}.\n"
                f"Re-run with --confirm-spend to proceed. Scoring cached responses only.\n"
            )
    if total_estimate > args.max_spend:
        print(
            f"error: estimate ${total_estimate:.4f} exceeds the ceiling ${args.max_spend:.4f}. "
            f"Raise --max-spend deliberately.",
            file=sys.stderr,
        )
        return 3

    ledger = SpendLedger(args.max_spend)
    reports: list[CandidateReport] = []
    exit_code = 0

    for candidate in selected:
        print(f"{candidate.label}  [{candidate.spec}]")
        try:
            scores = run_candidate(
                candidate,
                tasks,
                cache,
                rates,
                ledger,
                allow_spend=args.confirm_spend,
                env=env,
                on_event=print,
            )
        except BudgetExceeded as exc:
            print(f"  ABORTED: {exc}", file=sys.stderr)
            exit_code = 4
            break
        if scores:
            report = aggregate(
                candidate.candidate_id,
                candidate.label,
                resolve_model_id(candidate, env),
                scores,
            )
            reports.append(report)
        print()

    written = cache.flush()
    print(f"Cache written: {len(written)} file(s), spend this run ${ledger.spent_usd:.4f}")
    print()

    if reports:
        print("RESULTS")
        for report in reports:
            print(format_report(report))
        print()

        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(
            json.dumps(
                {
                    "rates_as_of": rates.as_of,
                    "region": rates.region,
                    "task_count": len(tasks),
                    "reports": [r.model_dump() for r in reports],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.results}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
