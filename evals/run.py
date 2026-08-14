"""The paid CLI.

Cost discipline in four layers, one more than ``bench/run.py`` has, because one of the resources
this run spends has no dollar price:

1. **Nothing is bought without ``--confirm-spend``.** The default is a dry run: it prints what the
   run would cost and stops.
2. **An estimate is printed before any paid call**, computed from the suite's actual shape rather
   than from a guess.
3. **Two ceilings enforced during the run** — ``--max-spend`` in USD over the priced Bedrock
   target calls, and ``--max-calls`` over every provider call of any kind. The second is the one
   that matters: the simulator and judge are on a credit-metered endpoint where a dollar ceiling
   sees nothing. See ``docs/spike-findings.md`` §7.7.
4. **Cache hits never re-spend**, so re-running an unchanged suite costs nothing and needs no flag.

The offline replay in :mod:`evals.replay` needs none of this and is what CI runs.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Mapping

from bench.live import dotenv_values
from evals.budget import DEFAULT_MAX_CALLS, DEFAULT_MAX_SPEND, Budget, is_credit_metered
from evals.cache import ResponseCache
from evals.capture import TARGET_GATED, TARGET_PLANNER, CapturedRun
from evals.models import MAX_CONCURRENT, ModelRole, spec_for
from evals.suite import (
    ATTACKS,
    EXCLUDED,
    TARGET_PURPOSE,
    selected,
    total_vulnerability_types,
)

RESULTS_DIR = pathlib.Path(__file__).parent / "results"

#: Provider calls one attack costs, before retries.
#:
#: Measured against deepteam's own structure rather than guessed: the simulator generates a
#: baseline attack and then enhances it (two calls per technique on the single-turn path), the
#: target is invoked once, and the judge scores it with a purpose call and a verdict call. It is
#: an over-estimate by construction, which is the only direction an estimate shown to a human
#: before spending should err.
CALLS_PER_ATTACK_SIMULATOR = 2
CALLS_PER_ATTACK_TARGET = 3
CALLS_PER_ATTACK_JUDGE = 2

#: ``deepteam/attacks/attack_simulator/utils.py`` retries three times with exponential backoff.
RETRY_FACTOR = 3


def estimate_calls(
    attacks_per_type: int,
    targets: int,
    *,
    run_all_attacks: bool = False,
    vulnerabilities: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """What the run would cost in calls, per role, at the worst case.

    **``run_all_attacks`` is the multiplier that matters, and the first version of this got it
    wrong.** ``red_team()`` defaults it to ``False``, which means each simulated attack is enhanced
    by *one* technique rather than by every technique in the list. Multiplying by
    ``len(ATTACKS)`` unconditionally over-estimated the default suite by 4x — enough that a run
    of one attack per type appeared to breach the approved 1500-call ceiling and would have been
    "fixed" by raising it.

    Erring high is right; erring high by a factor nobody can account for is how a ceiling gets
    raised for the wrong reason.
    """
    attacks = total_vulnerability_types(vulnerabilities) * attacks_per_type
    if run_all_attacks:
        attacks *= len(ATTACKS)
    return {
        "attacks": attacks,
        "simulator": attacks * CALLS_PER_ATTACK_SIMULATOR * RETRY_FACTOR,
        "target": attacks * targets * CALLS_PER_ATTACK_TARGET,
        "judge": attacks * targets * CALLS_PER_ATTACK_JUDGE * RETRY_FACTOR,
    }


def print_plan(
    attacks_per_type: int,
    targets: list[str],
    budget: Budget,
    *,
    run_all_attacks: bool = False,
    vulnerabilities: tuple[str, ...] | None = None,
) -> int:
    """Print the suite's shape and what it would cost. Returns the estimated total calls."""
    estimate = estimate_calls(
        attacks_per_type,
        len(targets),
        run_all_attacks=run_all_attacks,
        vulnerabilities=vulnerabilities,
    )
    total = estimate["simulator"] + estimate["target"] + estimate["judge"]

    every = "every technique per attack" if run_all_attacks else "one technique per attack"
    print(f"Targets:          {', '.join(targets)}")
    chosen = selected(vulnerabilities)
    narrowed = "" if vulnerabilities is None else f" (NARROWED: {', '.join(vulnerabilities)})"
    print(
        f"Vulnerabilities:  {len(chosen)} classes, "
        f"{total_vulnerability_types(vulnerabilities)} types{narrowed}"
    )
    print(f"Attacks:          {len(ATTACKS)} techniques available, {every}")
    print(f"Excluded:         {len(EXCLUDED)} classes, see evals/suite.py and the report")
    print(f"Concurrency:      {MAX_CONCURRENT}")
    print()
    print("Estimated provider calls (worst case, retries included):")
    print(f"  attacks generated   {estimate['attacks']:>6}")
    print(f"  simulator calls     {estimate['simulator']:>6}")
    print(f"  target calls        {estimate['target']:>6}   (Bedrock, priced)")
    print(f"  judge calls         {estimate['judge']:>6}")
    print(f"  TOTAL               {total:>6}   against a ceiling of {budget.calls.ceiling}")
    print()

    simulator = spec_for(ModelRole.SIMULATOR).model_id
    judge = spec_for(ModelRole.JUDGE).model_id
    metered = [model for model in (simulator, judge) if is_credit_metered(budget.rates, model)]
    if metered:
        print(
            f"NOTE: {', '.join(metered)} are credit-metered rather than billed per token, so the\n"
            f"      dollar estimate below does NOT describe them. --max-calls is their ceiling."
        )
    print()
    return total


def load_env(path: pathlib.Path) -> dict[str, str]:
    """Read ``.env``. Values are never printed."""
    return dotenv_values(path)


def credentials_missing(env: Mapping[str, str], targets: list[str]) -> str | None:
    """Why the run cannot proceed, or ``None``. Names variables, never values."""
    if not env.get("NVIDIA_API_KEY"):
        return "NVIDIA_API_KEY is unset; the simulator and judge cannot run"
    if not (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY")):
        return "no AWS key pair in .env; the target's Bedrock models cannot run"
    return None


def write_results(path: pathlib.Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", "utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CareLoop DeepTeam adversarial evaluation")
    parser.add_argument(
        "--target",
        action="append",
        choices=[TARGET_PLANNER, TARGET_GATED],
        default=None,
        help="target to run; repeatable. Default: both.",
    )
    parser.add_argument(
        "--attacks-per-type",
        type=int,
        default=1,
        help="attacks per vulnerability type (default 1)",
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="permit paid calls. Without it this is a dry run and nothing is bought.",
    )
    parser.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument(
        "--run-all-attacks",
        action="store_true",
        help="enhance every simulated attack with every technique. Multiplies the run by the "
        "number of techniques; off by default, matching red_team()'s own default.",
    )
    parser.add_argument(
        "--vulnerability",
        action="append",
        default=None,
        help="restrict the suite to this vulnerability class; repeatable. A narrowed run says so "
        "in its plan and in its results file, so the report can state what it covered.",
    )
    parser.add_argument("--no-cache", action="store_true", help="ignore committed responses")
    parser.add_argument("--env-file", type=pathlib.Path, default=pathlib.Path(".env"))
    parser.add_argument("--results", type=pathlib.Path, default=RESULTS_DIR / "deepteam.json")
    args = parser.parse_args(argv)

    targets = args.target or [TARGET_PLANNER, TARGET_GATED]
    vulnerabilities = tuple(args.vulnerability) if args.vulnerability else None
    budget = Budget(max_calls=args.max_calls, max_spend_usd=args.max_spend)

    print(f"Purpose:          {TARGET_PURPOSE[:78]}...")
    print()
    estimated = print_plan(
        args.attacks_per_type,
        targets,
        budget,
        run_all_attacks=args.run_all_attacks,
        vulnerabilities=vulnerabilities,
    )

    if estimated > budget.calls.ceiling:
        print(
            f"error: the estimate of {estimated} call(s) already exceeds --max-calls "
            f"({budget.calls.ceiling}). Raise it deliberately or lower --attacks-per-type.",
            file=sys.stderr,
        )
        return 3

    if not args.confirm_spend:
        print("DRY RUN. Nothing was bought. Re-run with --confirm-spend to proceed.")
        return 0

    env = load_env(args.env_file)
    problem = credentials_missing(env, targets)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    # Imported here, not at module scope: this is the only branch that needs the paid extra, and
    # a --dry-run on a machine without it must still print the plan.
    from evals.bridge import deepteam_version, model_callback, resolve_attacks
    from evals.bridge import resolve_vulnerabilities as _vulns
    from evals.models import build_model as build_judge

    cache = ResponseCache(enabled=not args.no_cache)
    simulator = build_judge(ModelRole.SIMULATOR, env, cache)
    judge = build_judge(ModelRole.JUDGE, env, cache)

    from deepteam import red_team

    captured: list[CapturedRun] = []
    assessments: dict[str, object] = {}
    exit_code = 0

    for target in targets:
        print(f"--- {target} ---")
        try:
            assessment = red_team(
                model_callback=model_callback(target, env, on_run=captured.append),
                vulnerabilities=_vulns(vulnerabilities),
                attacks=resolve_attacks(),
                simulator_model=simulator,
                evaluation_model=judge,
                attacks_per_vulnerability_type=args.attacks_per_type,
                target_purpose=TARGET_PURPOSE,
                # Explicit. The documented default is False and the measured default is True;
                # errored cases are reported beside pass and fail rather than folded into either.
                ignore_errors=True,
                max_concurrent=MAX_CONCURRENT,
                run_all_attacks=args.run_all_attacks,
                async_mode=True,
            )
            assessments[target] = assessment.model_dump(mode="json")
            print(f"  overview: {assessment.overview.model_dump(mode='json')}")
        except Exception as failure:
            print(f"  ABORTED: {type(failure).__name__}: {failure}", file=sys.stderr)
            exit_code = 4
            break
        finally:
            budget.calls.charge(target, simulator.calls + judge.calls)
            simulator.calls = 0
            judge.calls = 0

    written = cache.flush()
    print()
    print(f"Cache: {cache.hits} hit(s), {cache.misses} miss(es), {written} file(s) written")
    print(f"Budget: {budget.summary()}")

    write_results(
        args.results,
        {
            "deepteam_version": deepteam_version(),
            "simulator_model": spec_for(ModelRole.SIMULATOR).model_id,
            "judge_model": spec_for(ModelRole.JUDGE).model_id,
            "attacks_per_vulnerability_type": args.attacks_per_type,
            "run_all_attacks": args.run_all_attacks,
            "vulnerabilities": list(vulnerabilities) if vulnerabilities else "all",
            "targets": targets,
            "assessments": assessments,
            "captured_runs": [run.model_dump(mode="json") for run in captured],
        },
    )
    print(f"Wrote {args.results}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
