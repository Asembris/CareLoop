"""One pytest process, and the rule that nothing but a pass is a pass.

**Exactly one subprocess.** Every claim a tier selects contributes its node ids to one union, and
that union is handed to pytest once. Running per claim would multiply the interpreter start-up by
sixty and would let two claims sharing a test disagree about whether it passed.

**Class granularity, so the mapping is total.** Every declared node is
``tests/<module>.py::<Class>`` (the registry refuses anything else), and pytest's JUnit
``classname`` for a test inside it is
``tests.<module>.<Class>``. That is a pure string transform in one direction and therefore
unambiguous in the other — no path guessing, no heuristics, and no case where two declared nodes
could claim the same testcase.

**A node that collected nothing is a failure, not an absence.** So is a skip. A benchmark that let
either count as evidence would report "proven" for a property whose test was renamed, deleted,
gated behind an unavailable extra, or quietly xfailed — which is precisely the drift this layer
exists to catch, arriving through the layer itself.

**Collection is checked before anything runs**, because pytest aborts the whole session on an
unknown node id and would otherwise turn one typo into "nothing was measured" rather than into a
sentence naming the node. The check costs one interpreter start; the readable failure is worth it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: Outcomes a JUnit ``testcase`` can carry, in the order they are looked for. ``skipped`` is here
#: deliberately: it is not a pass, and it is the one a reader is most likely to assume away.
FAILING_TAGS: Final[tuple[str, ...]] = ("error", "failure", "skipped")

#: What a node whose class collected no tests at all is called. Distinct from ``failed`` because
#: the repair is different — one is a broken test, the other is a broken pointer.
MISSING: Final[str] = "missing"
PASSED: Final[str] = "passed"


class EvidenceDoesNotCollect(Exception):
    """A declared node id does not name anything pytest can run.

    Raised before the evidence run rather than discovered inside it, so a renamed test class reads
    as "this claim points at nothing" instead of as an empty result set.
    """


@dataclass(frozen=True)
class NodeOutcome:
    """What happened to every test under one declared node id."""

    node: str
    tests: int
    status: str

    @property
    def is_pass(self) -> bool:
        return self.status == PASSED and self.tests > 0


@dataclass(frozen=True)
class EvidenceRun:
    """One invocation's answer for every node it was asked about."""

    outcomes: Mapping[str, NodeOutcome]
    tests: int
    seconds: float

    def passed(self, node: str) -> bool:
        outcome = self.outcomes.get(node)
        return outcome is not None and outcome.is_pass

    def failing(self, nodes: Sequence[str]) -> tuple[str, ...]:
        """``"<node> (<status>)"`` for every node that did not pass, in declaration order."""
        return tuple(
            f"{node} ({self.outcomes[node].status if node in self.outcomes else MISSING})"
            for node in nodes
            if not self.passed(node)
        )


def junit_classname(node: str) -> str:
    """``tests/test_x.py::TestY`` -> ``tests.test_x.TestY``. The whole of the mapping."""
    path, _, klass = node.partition("::")
    return f"{path.removesuffix('.py').replace('/', '.')}.{klass}"


def _pytest(arguments: Sequence[str], *, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def check_collects(nodes: Sequence[str], *, root: Path = REPO_ROOT) -> None:
    """Refuse before running if any declared node names nothing.

    Both halves matter. pytest's own exit status catches an id it cannot resolve at all; the
    per-node check below catches the subtler case of a class that resolves and holds no tests,
    which pytest reports as success and which would otherwise render as evidence.
    """
    if not nodes:
        raise EvidenceDoesNotCollect("no evidence was selected; there is nothing to run")

    finished = _pytest(["--collect-only", "-q", *nodes], root=root)
    collected = {line.strip() for line in finished.stdout.splitlines() if "::" in line}

    missing = tuple(
        node for node in nodes if not any(item.startswith(f"{node}::") for item in collected)
    )
    if missing:
        errors = "\n".join(
            line for line in finished.stdout.splitlines() if line.startswith("ERROR")
        )
        raise EvidenceDoesNotCollect(
            "declared evidence does not collect:\n  "
            + "\n  ".join(missing)
            + (f"\n{errors}" if errors else "")
        )


def _outcome_for(node: str, cases: Sequence[ElementTree.Element]) -> NodeOutcome:
    if not cases:
        return NodeOutcome(node=node, tests=0, status=MISSING)
    for tag in FAILING_TAGS:
        if any(case.find(tag) is not None for case in cases):
            return NodeOutcome(node=node, tests=len(cases), status=tag)
    return NodeOutcome(node=node, tests=len(cases), status=PASSED)


def run_evidence(nodes: Sequence[str], *, root: Path = REPO_ROOT) -> EvidenceRun:
    """Run every declared node once and report what happened to each.

    The return value says nothing about claims. Deciding what an outcome means for a claim is the
    report's job, and keeping the two apart is what lets a claim require its counter-evidence to
    pass as well without this module knowing what counter-evidence is.
    """
    check_collects(nodes, root=root)

    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "evidence.xml"
        finished = _pytest(["-q", f"--junitxml={report}", *nodes], root=root)
        if not report.exists():
            raise EvidenceDoesNotCollect(
                f"pytest wrote no report (exit {finished.returncode}):\n{finished.stdout[-2000:]}"
            )
        tree = ElementTree.parse(report)

    by_class: dict[str, list[ElementTree.Element]] = {}
    for case in tree.iter("testcase"):
        by_class.setdefault(case.get("classname", ""), []).append(case)

    outcomes = {node: _outcome_for(node, by_class.get(junit_classname(node), [])) for node in nodes}
    suite = tree.find("testsuite")
    seconds = float(suite.get("time", "0")) if suite is not None else 0.0

    return EvidenceRun(
        outcomes=outcomes,
        tests=sum(outcome.tests for outcome in outcomes.values()),
        seconds=seconds,
    )


__all__ = [
    "EvidenceDoesNotCollect",
    "EvidenceRun",
    "NodeOutcome",
    "check_collects",
    "junit_classname",
    "run_evidence",
]
