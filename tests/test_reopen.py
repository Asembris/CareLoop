"""Reopening a finished case, and the sweep that says who may.

``DONE`` gained an outgoing transition in commit 7 so that a claim rejected weeks after an
appointment was arranged reopens the case that arranged it, rather than starting a second one that
knows nothing about the first. That is a real weakening of a load-bearing property, and the thing
that makes it safe is not the transition table — it is **who is able to make the move**.

:class:`TestOnlyTheWorldCanReopenACase` is that guard, and it is the reason this module exists.
The transition table says the move is legal; the sweep says nothing the agent controls can make
it. Both halves are needed: today the graph has no edge out of ``DONE``, and the sweep is what
stops a later session adding one by adding a call.

Modelled on the ``ModelRole.EXTRACTION`` sweep in ``tests/test_agents.py``, for the same reason —
a rule enforced by convention is a rule the next session drifts past.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from helpers import FIXED_NOW

import careloop
from careloop.events.inbound import CLAIM_REJECTION_EVENT, REFERRAL_EVENT
from careloop.graph.runner import CaseNotReopenable, open_case, reopen_case
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.machine import REOPEN_FROM, REOPEN_TO, transition
from careloop.state.store import CaseNotFound

CASE_ID = "case-0001"

#: The layers the agent drives. None of them may name the reopen.
#:
#: ``nodes.py`` because a node calling it would be the graph leaving ``DONE``; ``builder.py`` and
#: ``conditions.py`` because an edge into a reopen is the same thing wearing a different hat;
#: ``agents/`` because a model must never reach it; ``tools/`` because a tool that could reopen a
#: case would be an effect with authority over case state; ``policy/`` because the authority layer
#: decides whether an action may happen, not whether a finished case starts again.
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


def package_root() -> Path:
    return Path(careloop.__file__).parent


def agent_facing_modules() -> list[Path]:
    root = package_root()
    modules: list[Path] = []
    for entry in AGENT_FACING:
        target = root / entry
        modules.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return modules


def names_in(path: Path) -> set[str]:
    """Every identifier and attribute name mentioned in ``path``.

    Read from the syntax tree rather than by substring, so a mention inside a docstring or comment
    does not count — the sweep is about what the code can *call*, and the runner's own docstrings
    legitimately discuss it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
    return found


class TestOnlyTheWorldCanReopenACase:
    """The enforced version of "the agent can never leave DONE; the world can"."""

    def test_the_sweep_runs_over_the_modules_it_claims_to(self) -> None:
        """Otherwise it sweeps nothing and passes forever."""
        swept = agent_facing_modules()

        assert len(swept) > 15
        assert all(path.is_file() for path in swept)
        assert any(path.name == "nodes.py" for path in swept)
        assert any(path.parent.name == "agents" for path in swept)
        assert any(path.parent.name == "tools" for path in swept)
        assert any(path.parent.name == "policy" for path in swept)

    def test_no_node_agent_tool_or_policy_module_names_the_reopen(self) -> None:
        offenders = sorted(
            str(path.relative_to(package_root()))
            for path in agent_facing_modules()
            if "reopen_case" in names_in(path)
        )

        assert offenders == [], (
            f"{offenders} name reopen_case. Only an inbound event may take a case out of DONE; "
            f"anything the agent drives that can call this is the agent leaving DONE."
        )

    def test_the_detector_finds_the_call_where_one_exists(self) -> None:
        """Vacuity, both ways: the runner does define and name it."""
        assert "reopen_case" in names_in(package_root() / "graph" / "runner.py")

    def test_the_detector_would_catch_a_node_that_called_it(self, tmp_path) -> None:
        """A node written the forbidden way must be reported, not missed."""
        offending = tmp_path / "nodes.py"
        offending.write_text(
            "from careloop.graph.runner import reopen_case\n\n\n"
            "class ReopeningNode:\n"
            "    def run(self, store, event):\n"
            "        return reopen_case(store, event, case_id='c')\n",
            encoding="utf-8",
        )

        assert "reopen_case" in names_in(offending)

    def test_a_mention_in_prose_does_not_count(self) -> None:
        """The sweep is about what the code can call. Docstrings discussing it are the point."""
        innocent = "'''A node. It must never call reopen_case.'''\n\n\nX = 1\n"
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
            fh.write(innocent)
            path = Path(fh.name)

        assert "reopen_case" not in names_in(path)
        path.unlink()

    def test_no_graph_edge_leaves_done(self) -> None:
        """The other half. The sweep stops a call; this stops a route to one.

        ``DispatchNode`` is the only node with unconditional entry, and its edges enumerate the
        states a case can resume from. ``DONE`` is deliberately absent, so a finished case reaching
        the graph again simply ends the run.
        """
        from careloop.graph import builder

        source = (Path(builder.__file__)).read_text(encoding="utf-8")
        edges = [line for line in source.splitlines() if "add_edge" in line]

        assert edges
        assert not [line for line in edges if "CaseState.DONE" in line]


class TestTheReopenItself:
    @staticmethod
    def _finished_case() -> InMemoryCaseStore:
        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        base = case.version
        for target in (
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.DONE,
        ):
            case = transition(case, target, reason="setup", actor="test", now=FIXED_NOW)
        store.save(case, expected_version=base)
        return store

    def test_it_moves_the_case_back_to_gathering_evidence(self) -> None:
        store = self._finished_case()

        reopened = reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert reopened.state is REOPEN_TO
        assert REOPEN_FROM is CaseState.DONE

    def test_it_is_the_same_case_rather_than_a_new_one(self) -> None:
        store = self._finished_case()
        before = store.get(CASE_ID)

        reopened = reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert reopened.case_id == before.case_id
        assert reopened.created_at == before.created_at
        assert reopened.facts == before.facts
        assert reopened.actions == before.actions
        assert reopened.approvals == before.approvals

    def test_the_opening_event_is_not_rewritten(self) -> None:
        """``event_id`` records what opened the case. A reopen is not an opening."""
        store = self._finished_case()

        reopened = reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert reopened.event_id == REFERRAL_EVENT.event_id

    def test_the_timeline_continues_rather_than_restarting(self) -> None:
        store = self._finished_case()
        before = store.get(CASE_ID)

        reopened = reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert reopened.timeline[: len(before.timeline)] == before.timeline
        assert [entry.seq for entry in reopened.timeline] == list(range(len(reopened.timeline)))

    def test_the_reopen_is_recorded_with_the_event_that_caused_it(self) -> None:
        store = self._finished_case()

        reopened = reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        entry = next(
            entry for entry in reopened.timeline if entry.kind is TimelineKind.CASE_REOPENED
        )
        assert entry.payload["event_id"] == CLAIM_REJECTION_EVENT.event_id
        assert entry.payload["opened_by"] == REFERRAL_EVENT.event_id
        assert "doc-0006-claim-rejection" in entry.payload["document_ids"]

    def test_it_is_persisted_rather_than_returned_only(self) -> None:
        store = self._finished_case()

        reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert store.get(CASE_ID).state is CaseState.GATHERING_EVIDENCE

    @pytest.mark.parametrize(
        "state",
        [
            CaseState.NEW,
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.AWAITING_HUMAN,
            CaseState.READY_TO_EXECUTE,
            CaseState.FAILED,
        ],
    )
    def test_a_case_that_is_not_done_is_refused(self, state: CaseState) -> None:
        """An event does not interrupt work in flight, and FAILED stays terminal."""
        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        base = case.version
        route = {
            CaseState.NEW: (),
            CaseState.GATHERING_EVIDENCE: (CaseState.GATHERING_EVIDENCE,),
            CaseState.PLANNING: (CaseState.GATHERING_EVIDENCE, CaseState.PLANNING),
            CaseState.AWAITING_HUMAN: (
                CaseState.GATHERING_EVIDENCE,
                CaseState.PLANNING,
                CaseState.AWAITING_HUMAN,
            ),
            CaseState.READY_TO_EXECUTE: (
                CaseState.GATHERING_EVIDENCE,
                CaseState.PLANNING,
                CaseState.READY_TO_EXECUTE,
            ),
            CaseState.FAILED: (CaseState.FAILED,),
        }[state]
        for target in route:
            case = transition(case, target, reason="setup", actor="test", now=FIXED_NOW)
        store.save(case, expected_version=base)

        with pytest.raises(CaseNotReopenable, match=str(state)):
            reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

    def test_a_case_that_does_not_exist_is_not_created(self) -> None:
        """A reopen names an existing case; inventing one would hide a caller's mistake."""
        store = InMemoryCaseStore()

        with pytest.raises(CaseNotFound):
            reopen_case(store, CLAIM_REJECTION_EVENT, case_id="case-9999", now=FIXED_NOW)
