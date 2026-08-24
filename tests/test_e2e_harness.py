"""The e2e harness's own guards.

``scripts/e2e_serve.py`` is test-only code, but two things about it decide whether a red e2e run
can be diagnosed at all, and neither is checkable from a browser:

**A consumed script and a broken CareLoop look identical.** Both end as a ``FAILED`` case. They are
opposite conclusions — the first says the harness is under-scripted and nothing about the
application, the second says the application did something wrong — so the harness raises a type
named for itself and this asserts that the name survives all the way out to what a reader sees.

**The poisoned scenario stops working silently when a tool is added.** ``PlanNode`` parks a case as
``NO_SAFE_ACTION_AVAILABLE`` only when every tool in the registry has been proposed and refused, so
a script naming four of five tools produces a case that fails on the node budget instead — blamed
on a runaway detector, several layers from the cause. That was measured, not imagined: it is what
the harness did before the guard existed.

Imported by path rather than by name: ``scripts/`` is not a package and is deliberately not in the
wheel, which ``tests/test_resume.py`` handles the same way for the same reason.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import FIXED_NOW

from careloop.agents.schemas import TriageResult
from careloop.api.runs import RunRegistry, RunState
from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.runner import open_case, run_case
from careloop.graph.trusted import DEMO_CAREGIVER
from careloop.llm.mock import MockScriptExhausted, MockScriptTypeMismatch
from careloop.state import CaseState, InMemoryCaseStore
from careloop.tools.registry import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "scripts" / "e2e_serve.py"


def load_harness() -> Any:
    """Import ``scripts/e2e_serve.py`` as a module."""
    spec = importlib.util.spec_from_file_location("e2e_serve", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_serve"] = module
    spec.loader.exec_module(module)
    return module


harness = load_harness()


class TestAnExhaustedScriptNamesItself:
    """The diagnosis a red e2e run needs is *whose* script ran out."""

    def test_the_raised_type_is_the_harness_own(self) -> None:
        model = harness.ScriptedModel(structured_outputs=[])

        async def ask() -> None:
            async for _ in model.structured_output(TriageResult, []):
                pass

        with pytest.raises(harness.E2EScriptExhausted):
            asyncio.run(ask())

    def test_it_is_not_the_offline_suite_s_own_exhaustion(self) -> None:
        # `MockScriptExhausted` is what every other mock in this repository raises. Inheriting it
        # here would make the two indistinguishable at exactly the moment the difference matters,
        # so `E2EScriptExhausted` deliberately does not subclass it.
        assert not issubclass(harness.E2EScriptExhausted, MockScriptExhausted)

    def test_the_strictness_of_the_underlying_mock_is_inherited(self) -> None:
        # Subclassed rather than reimplemented, so a scripted answer of the wrong type still
        # raises. A lenient harness model would let a spec pass against a shape the real provider
        # could never produce.
        model = harness.ScriptedModel(structured_outputs=[harness.intent("send_email", {})])

        async def ask() -> None:
            async for _ in model.structured_output(TriageResult, []):
                pass

        # A type mismatch, not an exhaustion: the script has an answer, it is the wrong shape.
        with pytest.raises(MockScriptTypeMismatch):
            asyncio.run(ask())

    def test_the_name_reaches_the_case_and_the_run_over_http(self) -> None:
        """The two places a reader actually looks.

        ``run_case`` writes ``f"a graph node raised: {type(failure).__name__}"`` as the failure
        reason and the run registry records the same type name as ``RunView.error``. Both are
        projected, so the word "E2EScriptExhausted" is what a red suite puts in front of somebody
        — rather than a bare ``FAILED`` that could mean either thing.
        """
        store = InMemoryCaseStore()
        open_case(
            store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-dry", now=FIXED_NOW
        )

        # Deliberately under-scripted: no answers at all, so the first triage call runs it dry.
        context = harness.context_for(
            store,
            REFERRAL_EVENT,
            case_id="case-dry",
            triage=harness.TriageAgent(harness.ScriptedModel(structured_outputs=[])),
            planner=harness.PlannerAgent(harness.ScriptedModel(structured_outputs=[])),
        )

        async def drive():
            # Started inside the loop: `RunRegistry.start` creates a task, which needs a running
            # one. This is the same shape the API uses, where a request is already on the loop.
            registry = RunRegistry()
            record = registry.start("case-dry", lambda: run_case(context))
            return await registry.wait(record.run_id, 30.0)

        settled = asyncio.run(drive())

        assert settled.state is RunState.ERRORED
        assert settled.error == "E2EScriptExhausted"

        failed = store.get("case-dry")
        assert failed.state is CaseState.FAILED
        assert "E2EScriptExhausted" in (failed.failure_reason or "")


class TestThePoisonedScriptCoversTheRegistry:
    """A scenario that cannot park is a scenario that fails for the wrong reason."""

    def test_every_registry_tool_has_a_refusable_proposal(self) -> None:
        proposed = {tool for tool, _ in harness.EVERY_TOOL_REFUSED}
        assert REGISTRY.names <= proposed

    def test_the_guard_refuses_a_script_that_misses_one(self) -> None:
        # The vacuity proof for the check above: with a tool dropped, the harness refuses to load
        # rather than serving a scenario that walks the node budget and reports FAILED.
        complete = harness.EVERY_TOOL_REFUSED
        try:
            harness.EVERY_TOOL_REFUSED = complete[:-1]
            with pytest.raises(harness.MissingRefusalProposal) as refused:
                harness.check_the_refusal_script_covers_the_registry()
        finally:
            harness.EVERY_TOOL_REFUSED = complete

        # Names the missing tool, because "the script is wrong" is not an actionable sentence.
        assert complete[-1][0] in str(refused.value)

    def test_the_shipped_script_loads(self) -> None:
        harness.check_the_refusal_script_covers_the_registry()
