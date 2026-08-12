"""Compare planner output schemas against one another, with the provider held constant.

The instrument session A needed and did not have. It asks one question:

    given the prompt the real graph really sent, how often does a model produce a usable
    proposal — and does that depend on the model, or on the shape of the schema?

Holding the provider constant and varying only the schema is what makes the answer a diagnosis
rather than a preference. Running ``gpt-4o-mini`` alongside answers a *different* question, and
the report says so on the row: ``strands.models.openai`` reaches structured output through
OpenAI's strict Structured Outputs, where decoding is constrained so that every required field
must be emitted, so that model cannot reproduce the failure being investigated.

    .venv/Scripts/python scripts/schema_shootout.py                       # estimate only
    CARELOOP_LIVE=1 .venv/Scripts/python scripts/schema_shootout.py --confirm-spend --reps 8

Both gates apply — ``CARELOOP_LIVE=1`` and a key pair in ``.env`` — plus ``--confirm-spend``.

**Nothing here is cached, deliberately.** ``bench/cache.py`` keys on the prompt and the schema, so
eight repetitions of one call would collapse into one cached answer and a rate would be measured
from a single sample. Repeat sampling is exactly what a cache must not touch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.cost import UnknownRate, load_rates  # noqa: E402
from bench.intent_shapes import (  # noqa: E402
    CANDIDATES,
    ShapeCandidate,
    ShapeTrial,
    candidate,
    captured_prompt,
    provider_tool_spec,
    run_trial,
    summarise,
    system_prompt_for,
    tool_spec_defects,
)
from bench.live import MeteredModel, dotenv_values, live_gate_reason  # noqa: E402
from careloop.llm.providers import build_model, parse_spec  # noqa: E402

#: The provider whose behaviour is under investigation. Every shape is measured against this one.
DEFAULT_MODELS: tuple[str, ...] = ("bedrock:us.amazon.nova-2-lite-v1:0",)

#: Models whose structured-output path constrains decoding, so a success measures the provider's
#: constraint rather than the schema's fit. Flagged on every row they appear in.
CONSTRAINED_DECODING: frozenset[str] = frozenset({"openai"})

DEFAULT_MAX_SPEND = 2.00

#: From the captured prompt: ~2.2k in, and generously 200 out per call.
ESTIMATED_IN_PER_CALL = 2500
ESTIMATED_OUT_PER_CALL = 200


@dataclass
class Cell:
    """Every trial of one shape against one model."""

    shape_key: str
    shape_title: str
    model_spec: str
    trials: list[ShapeTrial] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def constrained(self) -> bool:
        return parse_spec(self.model_spec).provider in CONSTRAINED_DECODING


def _cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Priced, or loudly not priced.

    ``bench.live._cost`` swallows an unknown rate so a live run does not lose its measurement.
    Here the number feeds the spend ceiling, and a ceiling that silently prices a model at zero
    is not a ceiling.
    """
    return load_rates().cost_usd(model_id, input_tokens, output_tokens)


def estimate(shapes: Sequence[ShapeCandidate], models: Sequence[str], reps: int) -> float:
    calls = sum(shape.calls_per_proposal for shape in shapes) * reps * len(models)
    rates = load_rates()
    worst = 0.0
    for spec_text in models:
        model_id = parse_spec(spec_text).model_id
        try:
            worst = max(
                worst,
                rates.cost_usd(model_id, ESTIMATED_IN_PER_CALL, ESTIMATED_OUT_PER_CALL),
            )
        except UnknownRate as failure:
            print(f"Refusing to estimate: {failure}", file=sys.stderr)
            raise
    total = worst * calls
    print(f"Shapes:            {', '.join(shape.key for shape in shapes)}")
    print(f"Models:            {', '.join(models)}")
    print(f"Repetitions:       {reps}")
    print(f"Estimated calls:   ~{calls}")
    print(f"Estimated cost:    ~${total:.4f} (priced at the dearest candidate's rate throughout)")
    return total


def report_tool_specs(shapes: Sequence[ShapeCandidate]) -> None:
    """What each shape looks like by the time a provider sees it. Offline, free, and first.

    A shape whose emitted schema is already defective must not be measured live: the measurement
    would be of the defect, and it would read as a model result.
    """
    print("\nWHAT THE PROVIDER IS HANDED (offline)")
    for shape in shapes:
        for schema in shape.schemas:
            emitted = provider_tool_spec(schema)
            defects = tool_spec_defects(schema)
            required = emitted.get("required") or []
            status = "sound" if not defects else "; ".join(defects)
            print(
                f"  {shape.key:<12} {schema.__name__:<28} "
                f"required={len(required):<3} properties={len(emitted.get('properties') or {}):<3} "
                f"{status}"
            )


async def collect(
    shapes: Sequence[ShapeCandidate],
    models: Sequence[str],
    reps: int,
    ceiling: float,
    *,
    adapted: bool,
) -> list[Cell]:
    env = dotenv_values()
    captured_system, captured_user = captured_prompt()
    cells: list[Cell] = []
    spent = 0.0

    for spec_text in models:
        spec = parse_spec(spec_text)
        inner = build_model(spec, env)
        for shape in shapes:
            cell = Cell(shape_key=shape.key, shape_title=shape.title, model_spec=spec_text)
            system_prompt = system_prompt_for(shape, captured_system, adapted=adapted)
            for rep in range(reps):
                if spent >= ceiling:
                    print(f"  ! spend ceiling ${ceiling:.2f} reached; stopping", file=sys.stderr)
                    break
                metered = MeteredModel(inner, model_id=spec.model_id)
                trial = await run_trial(
                    shape,
                    metered,
                    model_spec=spec_text,
                    system_prompt=system_prompt,
                    user_prompt=captured_user,
                )
                cost = _cost(spec.model_id, metered.input_tokens, metered.output_tokens)
                spent += cost
                cell.trials.append(trial)
                cell.input_tokens += metered.input_tokens
                cell.output_tokens += metered.output_tokens
                cell.cost_usd += cost
                mark = "ok " if trial.valid else "FAIL"
                detail = (
                    trial.tool_name
                    if trial.valid
                    else (trial.schema_failure or trial.argument_defect or trial.error)
                )
                print(
                    f"  {spec_text:<38} {shape.key:<12} rep {rep + 1}/{reps} {mark} {detail[:88]}"
                )
            cells.append(cell)

    print(f"\nMeasured spend: ${spent:.4f} against a ceiling of ${ceiling:.2f}")
    return cells


def render(cells: Sequence[Cell]) -> str:
    lines: list[str] = ["", "VALID PROPOSALS PER SHAPE (counts, not a blended score)"]
    header = (
        f"{'model':<38} {'shape':<12} {'n':>3} {'valid':>6} {'schemafail':>11} "
        f"{'argdefect':>10} {'err':>4} {'p50 ms':>8} {'cost':>9}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for cell in cells:
        stats = summarise(cell.trials)
        latencies = sorted(trial.latency_ms for trial in cell.trials)
        p50 = latencies[len(latencies) // 2] if latencies else 0.0
        lines.append(
            f"{cell.model_spec:<38} {cell.shape_key:<12} {stats['n']:>3} "
            f"{stats['valid']:>6} {stats['schema_failures']:>11} "
            f"{stats['argument_defects']:>10} {stats['errors']:>4} "
            f"{p50:>8.0f} ${cell.cost_usd:>8.5f}"
        )

    if any(cell.constrained for cell in cells):
        lines.append("")
        lines.append(
            "  ! rows on an openai: model use strict Structured Outputs, where decoding is"
        )
        lines.append(
            "    constrained so every required field must be emitted. A high valid count there"
        )
        lines.append(
            "    measures the provider's constraint, not the schema's fit. Do not read it as"
        )
        lines.append("    evidence about the shape.")

    lines.append("")
    lines.append("TOOLS PROPOSED")
    for cell in cells:
        stats = summarise(cell.trials)
        lines.append(f"  {cell.model_spec:<38} {cell.shape_key:<12} {stats['tools_proposed']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--shapes",
        default=",".join(shape.key for shape in CANDIDATES),
        help="comma-separated shape keys (default: all)",
    )
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="comma-separated model specs (default: the planner under investigation)",
    )
    parser.add_argument("--reps", type=int, default=8, help="trials per shape per model")
    parser.add_argument(
        "--adapted",
        action="store_true",
        help=(
            "replace the captured system prompt's flat-specific sentence with one written for "
            "each shape; the default is verbatim, which is the true A/B"
        ),
    )
    parser.add_argument("--confirm-spend", action="store_true", help="required to make paid calls")
    parser.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND)
    parser.add_argument("--json", type=pathlib.Path, help="write the raw trials here")
    args = parser.parse_args(argv)

    shapes = tuple(candidate(key.strip()) for key in args.shapes.split(",") if key.strip())
    models = tuple(spec.strip() for spec in args.models.split(",") if spec.strip())

    estimate(shapes, models, args.reps)
    report_tool_specs(shapes)

    if not args.confirm_spend:
        print("\nDry run. Re-run with --confirm-spend to make these calls.")
        return 0

    blocked = live_gate_reason()
    if blocked is not None:
        print(f"\nRefusing to run: {blocked}", file=sys.stderr)
        return 2

    print()
    cells = asyncio.run(collect(shapes, models, args.reps, args.max_spend, adapted=args.adapted))
    print(render(cells))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "reps": args.reps,
                    "adapted_prompt": args.adapted,
                    "cells": [
                        {
                            "shape": cell.shape_key,
                            "title": cell.shape_title,
                            "model": cell.model_spec,
                            "constrained_decoding": cell.constrained,
                            "summary": summarise(cell.trials),
                            "input_tokens": cell.input_tokens,
                            "output_tokens": cell.output_tokens,
                            "cost_usd": round(cell.cost_usd, 6),
                            "trials": [trial.as_dict() for trial in cell.trials],
                        }
                        for cell in cells
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nRaw trials written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
