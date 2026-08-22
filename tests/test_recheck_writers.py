"""Who may send a parked case back to verification, and who may not.

``AWAITING_HUMAN → VERIFYING`` is now a legal transition, and legality is the smaller half of what
makes it safe. The larger half is **who can make the move**: a re-check is a human operation, and an
agent that could ask for one has been handed a way to re-enter verification on its own terms.

This is the arrangement ``tests/test_reopen.py`` already uses for the one edge out of ``DONE``, and
it is here for the same reason. The transition table says the move is permitted; the sweep says
nothing the agent drives can perform it. Both are needed — today no node calls
:func:`careloop.graph.runner.request_recheck`, and the sweep is what stops a later session adding a
call and quietly turning a caregiver's operation into an autonomous recovery loop.

**Two properties, deliberately separate**, because they fail differently:

:class:`TestOnlyAPersonCanAskForAReCheck` is the call-site sweep — no agent-facing module may name
the function.

:class:`TestOnlyTheRunnerWritesTheReCheckTransition` is the writer sweep — no module outside the
runner may transition a case to ``VERIFYING`` at all, so a node cannot route around the function by
writing the state itself. Deleting the sweep above alone would leave that door open, which is the
same redundancy ``TRANSITIONS`` and the missing ``verify → plan`` edge have.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import careloop
from careloop.graph.runner import request_recheck
from careloop.state.case import CaseState
from careloop.state.machine import TRANSITIONS, is_legal

#: The layers the agent drives. None of them may ask for a re-check.
#:
#: ``graph/nodes.py`` because a node asking for one would be the graph re-entering verification on
#: its own authority — the autonomous observation retry Patch 2B deliberately defers, arriving by
#: the back door. ``builder.py`` and ``conditions.py`` because an edge into a re-check is the same
#: thing wearing a different hat. ``graph/verify.py`` because the component that decides a read
#: failed must not also be able to order another. ``agents/`` because a model must never reach it;
#: ``tools/`` because a tool that could re-check would be an effect with authority over case
#: lifecycle; ``policy/`` because the authority layer decides whether an action may happen, not what
#: a stopped case does next.
AGENT_FACING = (
    "graph/nodes.py",
    "graph/builder.py",
    "graph/conditions.py",
    "graph/context.py",
    "graph/extract.py",
    "graph/verify.py",
    "agents",
    "tools",
    "policy",
)

#: The one module allowed to write ``transition(..., VERIFYING)``.
#:
#: ``graph/nodes.py`` is the other legitimate writer and is listed separately below, because its two
#: calls are the *execution* entries and are a different property from this one.
RECHECK_WRITER = "graph/runner.py"


def package_root() -> Path:
    return Path(careloop.__file__).parent


def agent_facing_modules() -> list[Path]:
    root = package_root()
    modules: list[Path] = []
    for entry in AGENT_FACING:
        target = root / entry
        modules.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return modules


def names_used(source: str) -> set[str]:
    """Every bare name and attribute this module mentions.

    Both forms, so ``request_recheck(...)`` and ``runner.request_recheck(...)`` are caught, and an
    import without a call is caught too — a module that imports it has already decided it might.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
    return found


def transitions_to_verifying(source: str) -> list[str]:
    """Every ``transition(..., CaseState.VERIFYING, ...)`` call, by enclosing function name."""
    tree = ast.parse(source)
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if callee != "transition":
            continue
        for argument in node.args[1:]:
            if isinstance(argument, ast.Attribute) and argument.attr == "VERIFYING":
                found.append(enclosing.get(node, "module"))
    return found


class TestOnlyAPersonCanAskForAReCheck:
    """The call-site sweep. Nothing the agent drives may name the operation."""

    @pytest.mark.parametrize("module", agent_facing_modules(), ids=lambda path: str(path.name))
    def test_no_agent_facing_module_names_the_recheck(self, module: Path) -> None:
        assert "request_recheck" not in names_used(module.read_text(encoding="utf-8")), (
            f"{module} names request_recheck; a re-check is a caregiver's request and nothing the "
            f"agent drives may make one"
        )

    def test_the_sweep_runs_over_the_modules_it_claims_to(self) -> None:
        """Vacuity: an empty sweep passes every assertion over it."""
        modules = {path.name for path in agent_facing_modules()}

        assert {"nodes.py", "builder.py", "conditions.py", "verify.py"} <= modules
        assert len(agent_facing_modules()) > 15

    def test_the_matcher_would_catch_a_call_however_it_is_spelled(self) -> None:
        """Both spellings and the bare import, so the guard is not defeated by an alias."""
        assert "request_recheck" in names_used("request_recheck(store, case_id)")
        assert "request_recheck" in names_used("runner.request_recheck(store, case_id)")
        assert "request_recheck" in names_used("from careloop.graph.runner import request_recheck")

    def test_the_api_layer_does_name_it(self) -> None:
        """The counterpart: the one caller that should exist, does.

        Without this the sweep above would pass on a system where nobody can re-check at all.
        """
        app = (package_root() / "api" / "app.py").read_text(encoding="utf-8")

        assert "request_recheck" in names_used(app)


class TestOnlyTheRunnerWritesTheReCheckTransition:
    """The writer sweep. A node cannot route around the operation by writing the state itself."""

    def test_the_runner_is_the_only_non_node_writer_of_verifying(self) -> None:
        writers = {
            str(path.relative_to(package_root())).replace("\\", "/")
            for path in package_root().rglob("*.py")
            if transitions_to_verifying(path.read_text(encoding="utf-8"))
        }

        assert writers == {RECHECK_WRITER, "graph/nodes.py"}

    def test_the_runners_only_such_write_is_the_recheck(self) -> None:
        runner = (package_root() / "graph" / "runner.py").read_text(encoding="utf-8")

        assert transitions_to_verifying(runner) == ["request_recheck"]

    def test_the_nodes_writes_are_all_post_effect(self) -> None:
        """The other legitimate entries, counted so a further one would be visible.

        Four, all inside a node's ``run``. Two are :class:`careloop.graph.nodes.ExecuteNode` moving
        on after an effect was recorded or a duplicate of one was suppressed. Two are
        :class:`careloop.graph.nodes.ReconcileNode` doing the same for an effect a crash left
        unrecorded — one after a replay produced a result, one for a result that was recorded
        without its transition landing. All four hold an ``ActionRecord`` by the time they run, so
        "every entry into ``VERIFYING`` is post-effect" is unchanged by there being more of them.
        """
        nodes = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")

        assert transitions_to_verifying(nodes) == ["run", "run", "run", "run"]

    def test_the_matcher_finds_what_it_claims_to(self) -> None:
        """Vacuity: a matcher that found nothing would make every assertion above pass."""
        source = (
            "def f():\n    case = transition(case, CaseState.VERIFYING, reason='r', actor='a')\n"
        )

        assert transitions_to_verifying(source) == ["f"]
        assert transitions_to_verifying("transition(case, CaseState.DONE)") == []


class TestTheTransitionIsLegalAndNarrow:
    """The table half, pinned as an exact set so a fourth target is a deliberate edit."""

    def test_awaiting_human_can_reach_verification(self) -> None:
        assert is_legal(CaseState.AWAITING_HUMAN, CaseState.VERIFYING)

    def test_and_nothing_else_was_opened_up(self) -> None:
        assert TRANSITIONS[CaseState.AWAITING_HUMAN] == frozenset(
            {
                CaseState.READY_TO_EXECUTE,
                CaseState.PLANNING,
                CaseState.VERIFYING,
                CaseState.FAILED,
            }
        )

    def test_verification_still_cannot_return_a_case_to_planning(self) -> None:
        """The closed post-effect property, unmoved by this patch and re-asserted here.

        A re-check adds a way *into* verification. It must add no way out of one, or the second
        external effect this repository already measured becomes reachable again by a longer route.
        """
        assert not is_legal(CaseState.VERIFYING, CaseState.PLANNING)
        assert TRANSITIONS[CaseState.VERIFYING] == frozenset(
            {CaseState.DONE, CaseState.AWAITING_HUMAN, CaseState.FAILED}
        )

    def test_done_is_still_reachable_only_from_verifying(self) -> None:
        """Invariant 4, untouched: a re-check completes a case through the same one door."""
        sources = [state for state, targets in TRANSITIONS.items() if CaseState.DONE in targets]

        assert sources == [CaseState.VERIFYING]

    def test_the_operation_is_importable_and_takes_no_target(self) -> None:
        """The client trust boundary, as a fact about the signature rather than about a handler.

        There is no parameter through which any caller — a browser, a route, or a future node —
        could name the action to observe. That is what makes "the client cannot aim it" structural.
        """
        import inspect

        parameters = set(inspect.signature(request_recheck).parameters)

        assert parameters == {"store", "case_id", "requested_by", "now"}
        assert not parameters & {
            "tool_name",
            "args",
            "args_digest",
            "idempotency_key",
            "confirmation_id",
            "action",
            "action_index",
            "target",
        }
