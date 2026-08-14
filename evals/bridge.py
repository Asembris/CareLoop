"""The one module that imports DeepTeam.

**Read the first ten lines before editing.** ``deepteam/telemetry.py`` executes at import and,
unless already opted out, resolves the operator's public IP address through
``https://api.ipify.org`` and initialises Sentry against a hard-coded DSN. Setting the variables
in a shell is not sufficient and neither is a ``conftest.py``: an ``import deepteam`` anywhere
earlier in the process wins. They are set here, above the import, and
``tests/test_evals_suite.py`` asserts the ordering in the source rather than trusting it.

Assignment, not ``setdefault``: an environment that has already set the variable to something
other than ``YES`` must still be overridden. The alternative is a machine where the opt-out looks
present and does nothing.

Everything else here is conversion. :class:`~evals.capture.CapturedRun` becomes an ``RTTurn`` with
``tools_called`` populated from real invocations, and that is the whole reason this file exists:
DeepTeam cannot assess ``ToolOrchestrationAbuse`` or ``ExcessiveAgency`` from a name, so a bridge
that passed text only would be asking the judge to score a transcript with the evidence removed.
"""

from __future__ import annotations

import os

# Must precede the deepteam import. See the module docstring and docs/spike-findings.md §7.2.
os.environ["DEEPTEAM_TELEMETRY_OPT_OUT"] = "YES"
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

import importlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from careloop.agents.planner import PlannerAgent
from careloop.agents.triage import TriageAgent
from careloop.llm.providers import build_model, parse_spec
from evals.capture import TARGET_GATED, TARGET_PLANNER, CapturedRun
from evals.suite import ATTACKS, selected
from evals.targets import Delivery, run_gated_pipeline, run_planner_only

#: The deployed planner and triage models. Testing anything else would not be testing production.
#: Named here rather than read from ``CARELOOP_MODEL_*``, which the suite's own tripwire requires
#: stay unset — the same reason ``bench/live.py`` builds its models explicitly.
TARGET_TRIAGE_SPEC = "bedrock:us.amazon.nova-micro-v1:0"
TARGET_PLANNER_SPEC = "bedrock:us.amazon.nova-2-lite-v1:0"


def rt_turn(run: CapturedRun) -> Any:
    """A :class:`CapturedRun` as the ``RTTurn`` DeepTeam judges.

    ``tools_called`` carries every invocation with its parameters and its output, including the
    ones policy refused — a refusal is the most informative thing in a gated run and hiding it
    would leave the judge unable to distinguish "did not act" from "was stopped".
    """
    test_case = importlib.import_module("deepteam.test_case")

    return test_case.RTTurn(
        role="assistant",
        content=run.narrative,
        tools_called=[
            test_case.ToolCall(
                name=call.name,
                input_parameters=dict(call.input_parameters),
                output=call.output,
                reasoning=(
                    f"refused by policy: {call.refusal}"
                    if call.refusal
                    else ("could not be dispatched" if call.error else "performed")
                ),
            )
            for call in run.tools_called
        ],
    )


def target_agents(env: Mapping[str, str]) -> tuple[TriageAgent, PlannerAgent]:
    """Triage and planner on the deployed configuration, built from ``env`` alone."""
    return (
        TriageAgent(build_model(parse_spec(TARGET_TRIAGE_SPEC), env)),
        PlannerAgent(build_model(parse_spec(TARGET_PLANNER_SPEC), env)),
    )


def model_callback(
    target: str,
    env: Mapping[str, str],
    *,
    delivery: Delivery = Delivery.DOCUMENT,
    alongside_real_work: bool = False,
    on_run: Callable[[CapturedRun], None] | None = None,
) -> Callable[..., Any]:
    """The callback DeepTeam drives, bound to one target.

    Async, because deepteam awaits the callback on every multi-turn path and calls it plainly on
    the single-turn ones; an async callable satisfies both. ``turns`` is accepted and ignored —
    CareLoop's unit of work is a case, not a conversation, and a second turn opens a second case
    rather than continuing one.
    """

    async def callback(attack: str, turns: Sequence[Any] | None = None) -> Any:
        attack_id = f"deepteam-{abs(hash(attack)) % (10**12):012d}"
        if target == TARGET_PLANNER:
            _, planner = target_agents(env)
            run = await run_planner_only(
                attack,
                attack_id=attack_id,
                planner=planner,
                delivery=delivery,
                alongside_real_work=alongside_real_work,
                case_id=f"case-{target}-{attack_id}",
            )
        elif target == TARGET_GATED:
            triage, planner = target_agents(env)
            run = await run_gated_pipeline(
                attack,
                attack_id=attack_id,
                triage=triage,
                planner=planner,
                delivery=delivery,
                alongside_real_work=alongside_real_work,
                case_id=f"case-{target}-{attack_id}",
            )
        else:
            raise ValueError(f"unknown target {target!r}")

        if on_run is not None:
            on_run(run)
        return rt_turn(run)

    return callback


def resolve_vulnerabilities(names: tuple[str, ...] | None = None) -> list[Any]:
    """Instantiate every vulnerability named in :mod:`evals.suite`.

    Raises:
        AttributeError: naming the class, if a DeepTeam release drops or renames one. Loud is
            correct — a suite that silently ran five of six vulnerabilities would report a
            headline over a smaller N than the report claims.
    """
    module = importlib.import_module("deepteam.vulnerabilities")
    return [getattr(module, spec.name)(types=list(spec.types)) for spec in selected(names)]


def resolve_attacks() -> list[Any]:
    """Instantiate every attack technique named in :mod:`evals.suite`."""
    module = importlib.import_module("deepteam.attacks.single_turn")
    return [getattr(module, spec.name)() for spec in ATTACKS]


def deepteam_version() -> str:
    """The installed DeepTeam version, recorded on every generated attack's provenance."""
    from importlib.metadata import version

    return version("deepteam")


__all__ = [
    "TARGET_PLANNER_SPEC",
    "TARGET_TRIAGE_SPEC",
    "deepteam_version",
    "model_callback",
    "resolve_attacks",
    "resolve_vulnerabilities",
    "rt_turn",
    "target_agents",
]
