"""Each proof tool writes down the verdict it already reached, and writes down the same one.

Three operator tools produce live evidence — the object store's write contract, the deployed
behavioural lock, and one AgentCore evaluation. Every one of them already *computed* whether it
held: ``Report.satisfied``, the sum of the matrix's own ``ok`` flags, the expectation comparison.
Every one of them also wrote its ``--out`` file **and then** returned a non-zero exit code, so a
failed run left behind a file that looked exactly like a successful one. Nothing downstream could
tell the two apart, which is the whole reason ``careloopbench.record`` needs something to gate on.

So each tool now serialises that verdict as ``passed``, and this file holds the two properties
worth holding: **the field is the tool's own verdict** rather than a second opinion computed
beside it, and **a failing run says so**. The second is the anti-vacuity half — a ``passed`` that
is ``true`` however the run went is a field a recorder could not use for anything.

``passed`` and not ``satisfied``, ``locked`` or ``expectation``: a recorder that must work for
three tools cannot know which one it is holding, and one name it can require is the point. Each
tool keeps its own vocabulary beside it, unchanged — these patches added a field and moved no
observation, which is what the "written through unchanged" assertions are for.

Nothing here reaches a network, an AWS service or a deployed anything. The seams that would are
replaced; what is exercised is each tool's own ``main``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

import pytest

import scripts.evaluations_prove as prove
import scripts.objectstore_contract as contract
import scripts.verify_lock as lock

# ------------------------------------------------------------------ the object store's contract


def a_report(*satisfied: bool) -> contract.Report:
    """A report whose checks hold exactly as told. The names are positional and mean nothing."""
    return contract.Report(
        checks=[
            contract.Check(name=f"check-{index}", satisfied=held, observed=f"HTTP {200 + index}")
            for index, held in enumerate(satisfied)
        ]
    )


@pytest.fixture
def no_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` imports boto3 and builds a client before anything else. Neither is under test."""

    class Session:
        def client(self, *_: Any, **__: Any) -> object:
            return object()

    module = type("boto3", (), {"session": type("session", (), {"Session": Session})})
    monkeypatch.setitem(sys.modules, "boto3", module)


class TestTheObjectStoreContractRecordsItsOwnVerdict:
    """``passed`` is ``report.satisfied``, and a contract that did not hold says so."""

    @pytest.fixture(autouse=True)
    def _held(self, monkeypatch: pytest.MonkeyPatch, no_boto3: None) -> None:
        self.monkeypatch = monkeypatch
        self.observed(True, True, True)

    def observed(self, *satisfied: bool) -> None:
        self.report = a_report(*satisfied)
        self.monkeypatch.setattr(contract, "run_contract", lambda *_, **__: self.report)

    def run(self, tmp_path: Path) -> tuple[int, dict[str, Any]]:
        out = tmp_path / "objectstore.json"
        code = contract.main(["--bucket", "irrelevant", "--out", str(out)])
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_a_satisfied_contract_passes_and_exits_zero(self, tmp_path: Path) -> None:
        code, written = self.run(tmp_path)

        assert written["passed"] is True
        assert code == 0

    def test_a_broken_contract_does_not_pass_and_exits_non_zero(self, tmp_path: Path) -> None:
        """Anti-vacuity. A field that is true however the run went is not a verdict."""
        self.observed(True, False, True)

        code, written = self.run(tmp_path)

        assert written["passed"] is False
        assert code != 0

    def test_the_last_check_is_as_load_bearing_as_the_first(self, tmp_path: Path) -> None:
        """The destructive overwrite check is what makes the create-once ones mean anything."""
        self.observed(True, True, False)

        _, written = self.run(tmp_path)

        assert written["passed"] is False

    def test_it_is_not_a_second_opinion_about_the_same_run(self, tmp_path: Path) -> None:
        """``satisfied`` is this contract's own word for it. The two may never disagree."""
        _, written = self.run(tmp_path)

        assert written["passed"] == written["satisfied"]

    def test_the_observations_are_exactly_what_the_report_holds(self, tmp_path: Path) -> None:
        """A verdict was added. Nothing about what the provider answered moved."""
        _, written = self.run(tmp_path)

        assert written["checks"] == [
            {"name": check.name, "satisfied": check.satisfied, "observed": check.observed}
            for check in self.report.checks
        ]


# ------------------------------------------------------------------ the deployed behavioural lock


def a_run(ok: bool) -> dict[str, Any]:
    return {"scenario": "referral", "run": 1, "ok": ok}


def a_coupling(ok: bool) -> dict[str, Any]:
    return {"both_booked": ok, "ended": ok, "second_reset": False, "ok": ok}


def run_lock_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    matrix: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    coupled: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """``main`` with every request-making seam replaced. No socket is opened."""
    out = tmp_path / "lock.json"
    monkeypatch.setattr(lock, "scenario_matrix", lambda *_, **__: matrix)
    monkeypatch.setattr(lock, "approval_cycle", lambda _base, index, _runs: approvals[index])
    monkeypatch.setattr(lock, "coupling", lambda *_, **__: coupled)
    monkeypatch.setattr(
        sys, "argv", ["verify_lock.py", "--runs", str(len(approvals)), "--out", str(out)]
    )
    code = lock.main()
    return code, json.loads(out.read_text(encoding="utf-8"))


class TestTheDeployedLockRecordsItsOwnVerdict:
    """``passed`` is the lock's own sum, and each of its three parts can refuse it."""

    def test_a_clean_pass_passes_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, written = run_lock_main(
            tmp_path,
            monkeypatch,
            matrix=[a_run(True), a_run(True)],
            approvals=[a_run(True)],
            coupled=a_coupling(True),
        )

        assert written["passed"] is True
        assert code == 0

    def test_a_missed_scenario_row_does_not_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, written = run_lock_main(
            tmp_path,
            monkeypatch,
            matrix=[a_run(True), a_run(False)],
            approvals=[a_run(True)],
            coupled=a_coupling(True),
        )

        assert written["passed"] is False
        assert code != 0

    def test_a_missed_approval_cycle_does_not_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, written = run_lock_main(
            tmp_path,
            monkeypatch,
            matrix=[a_run(True), a_run(True)],
            approvals=[a_run(False)],
            coupled=a_coupling(True),
        )

        assert written["passed"] is False
        assert code != 0

    def test_a_broken_coupling_does_not_pass_even_when_every_run_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third part. A verdict read off the run counts alone would call this a pass."""
        code, written = run_lock_main(
            tmp_path,
            monkeypatch,
            matrix=[a_run(True), a_run(True)],
            approvals=[a_run(True)],
            coupled=a_coupling(False),
        )

        assert written["passed"] is False
        assert code != 0

    def test_the_rows_are_written_through_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        matrix = [a_run(True), a_run(False)]
        approvals = [a_run(True)]
        coupled = a_coupling(True)

        _, written = run_lock_main(
            tmp_path, monkeypatch, matrix=matrix, approvals=approvals, coupled=coupled
        )

        assert written["matrix"] == matrix
        assert written["approvals"] == approvals
        assert written["coupling"] == coupled


# --------------------------------------------------------------------------- the AgentCore proof


TRACE: Final[str] = "1-00000000-000000000000000000000000"

#: One graph span carrying a settlement claim, and the clinic write beside it. Enough for
#: ``transport_facts`` to describe. No AWS call reads it and nothing here asserts about its shape
#: beyond it surviving into the record unchanged.
SPANS: Final[list[dict[str, Any]]] = [
    {
        "traceId": TRACE,
        "spanId": "0000000000000001",
        "attributes": {"careloop.effect.type": "book_appointment"},
    },
    {
        "traceId": TRACE,
        "spanId": "0000000000000002",
        "attributes": {
            "http.method": "POST",
            "http.url": "http://127.0.0.1:8100/clinic/appointments",
        },
    },
]


def run_prove_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str,
    extra: list[str],
) -> tuple[int, dict[str, Any]]:
    """``main`` with discovery and ``Evaluate`` replaced. Nothing reaches AWS."""
    out = tmp_path / "agentcore.json"
    monkeypatch.setattr(prove, "_logs_client", lambda: object())
    monkeypatch.setattr(prove, "collect_trace", lambda *_, **__: list(SPANS))
    monkeypatch.setattr(
        prove,
        "evaluate",
        lambda *_, **__: {
            "label": label,
            "value": 1.0 if label == "PASS" else 0.0,
            "value_present": True,
            "explanation": "SETTLED",
            "evaluator_id": "CareLoopSettlementIntegrity-test",
        },
    )
    monkeypatch.setattr(
        sys, "argv", ["evaluations_prove.py", "--trace-id", TRACE, "--out", str(out), *extra]
    )
    code = prove.main()
    return code, json.loads(out.read_text(encoding="utf-8"))


class TestTheAgentCoreProofRecordsItsOwnVerdict:
    """``passed`` is *the stated expectation was met*, and only that."""

    def test_a_met_expectation_passes_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, written = run_prove_main(
            tmp_path, monkeypatch, label="PASS", extra=["--evaluator-id", "e", "--expect", "PASS"]
        )

        assert written["expectation"] == prove.MET
        assert written["passed"] is True
        assert code == 0

    def test_an_unmet_expectation_does_not_pass_and_exits_non_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect this patch closes: it recorded the contradiction and exited zero."""
        code, written = run_prove_main(
            tmp_path, monkeypatch, label="FAIL", extra=["--evaluator-id", "e", "--expect", "PASS"]
        )

        assert written["expectation"] == prove.NOT_MET
        assert written["passed"] is False
        assert code != 0

    def test_an_evaluation_that_asserted_nothing_is_not_a_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``--expect``: nothing was claimed, so nothing was proven — and nothing failed."""
        code, written = run_prove_main(
            tmp_path, monkeypatch, label="PASS", extra=["--evaluator-id", "e"]
        )

        assert written["expectation"] == prove.NOT_ASSERTED
        assert written["passed"] is False
        assert code == 0

    def test_a_discovery_run_is_not_a_proof_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-evaluate`` reads spans and calls no evaluator, so it establishes nothing."""
        code, written = run_prove_main(tmp_path, monkeypatch, label="PASS", extra=["--no-evaluate"])

        assert "result" not in written
        assert written["passed"] is False
        assert code == 0

    def test_the_transport_observations_are_still_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A verdict was added beside what the trace said. What it said did not move."""
        _, written = run_prove_main(
            tmp_path, monkeypatch, label="PASS", extra=["--evaluator-id", "e", "--expect", "PASS"]
        )

        assert written["trace_id"] == TRACE
        assert written["transport"]["span_count"] == len(SPANS)
        assert written["transport"]["clinic_write_count"] == 1
