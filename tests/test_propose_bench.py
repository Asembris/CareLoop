"""The post-B2 proposal-seam benchmark, checked offline at $0.

``bench/propose.py`` spends money when it runs, so nothing here runs it. What is checked is
everything that can be wrong *before* a call is bought: the task set validates, the expectations
name tools that exist, the arithmetic in the summary is right, and the two couplings that are
invisible until they break — the task file's location, and the log message the retry-cause capture
reads.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from bench import propose
from bench.tasks import TASKS_DIR, load_tasks
from careloop.agents import invoke
from careloop.provenance import TrustLevel
from careloop.state import Case
from careloop.tools import REGISTRY


class TestTheTaskSet:
    def test_it_loads_and_validates(self) -> None:
        tasks = propose.load_tasks()
        assert len(tasks) == 12

    def test_task_ids_are_unique(self) -> None:
        ids = [task.task_id for task in propose.load_tasks()]
        assert len(ids) == len(set(ids))

    def test_every_expectation_names_a_registered_tool(self) -> None:
        for task in propose.load_tasks():
            if task.expects is not None:
                assert task.expects in REGISTRY.names, task.task_id

    def test_every_registered_tool_is_expected_by_some_task(self) -> None:
        """A seam benchmark that never asks for a tool has not measured that tool's schema."""
        expected = {task.expects for task in propose.load_tasks() if task.expects}
        assert expected == REGISTRY.names

    def test_the_replan_path_is_covered(self) -> None:
        """The behaviour ``docs/model-benchmark.md`` §5 records as entirely unmeasured.

        Pinned at exactly three because ``docs/model-benchmark-post-b2.md`` states the number and
        divides by it — "2 of 9 replan attempts against 0 of 27 clean ones". Adding a fourth task
        without updating that sentence would leave a published arithmetic claim wrong, which is
        how this assertion earned its exactness rather than a `>=`.
        """
        with_denials = [task for task in propose.load_tasks() if task.prior_denials]
        assert len(with_denials) == 3

    def test_every_task_builds_a_real_case(self) -> None:
        for task in propose.load_tasks():
            case = task.case()
            assert isinstance(case, Case)
            assert case.summary == task.summary
            assert len(case.facts) == len(task.facts)

    def test_trusted_facts_carry_no_document_provenance(self) -> None:
        """A fixture that gave a TRUSTED fact a page number would describe a case the graph
        cannot produce: ``careloop/graph/trusted.py`` seeds these from configuration, and a fact
        whose provenance names a document is a fact that came from one."""
        for task in propose.load_tasks():
            for fact in task.case().facts:
                if fact.trust_level is TrustLevel.TRUSTED:
                    assert fact.provenance.page is None
                    assert fact.provenance.source_doc == "trusted-config"

    def test_only_the_injection_probes_carry_an_attacker_literal(self) -> None:
        """Fixture hygiene: a stray attacker address in a clean task would make the observation
        count meaningless in the direction that flatters the model."""
        for task in propose.load_tasks():
            carries = any(
                literal in value
                for _, value, _ in task.facts
                for literal in propose.ATTACKER_LITERALS
            )
            assert carries == ("injection" in task.task_id), task.task_id


class TestItDoesNotDisturbTheFrozenBenchmark:
    """The pre-B2 task set is a frozen artifact and its loader globs a whole directory.

    Session W put ``propose.json`` in ``bench/tasks/`` and turned eight of
    ``tests/test_bench_harness.py``'s assertions red: ``bench.tasks.load_tasks`` validates every
    ``*.json`` in there against the old ``Task`` schema, so a new file does not sit beside the old
    set — it joins it. These are the two halves of that, so it cannot happen twice.
    """

    def test_the_propose_tasks_live_outside_the_frozen_directory(self) -> None:
        assert propose.TASKS_PATH.parent != TASKS_DIR
        assert propose.TASKS_PATH.name not in {p.name for p in TASKS_DIR.glob("*.json")}

    def test_the_frozen_task_set_still_loads(self) -> None:
        assert len(load_tasks().tasks) > 0


class TestTheRetryCauseCapture:
    """The one coupling that fails silently rather than loudly."""

    def test_it_matches_the_message_the_seam_actually_logs(self) -> None:
        """Non-vacuity for the capture: reword the log line in ``careloop/agents/invoke.py`` and
        this fails, rather than the benchmark quietly reporting zero retry causes forever."""
        source = inspect.getsource(invoke.proposal_tool_call)
        assert propose._RetryCauseCapture.RETRY_MESSAGE in source

    def test_it_records_the_type_name_from_a_matching_record(self) -> None:
        import logging

        capture = propose._RetryCauseCapture()
        record = logging.LogRecord(
            name="careloop.agents.invoke",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=propose._RetryCauseCapture.RETRY_MESSAGE,
            args=("MalformedProposal",),
            exc_info=None,
        )
        capture.emit(record)
        assert capture.causes == ["MalformedProposal"]

    def test_it_ignores_any_other_warning(self) -> None:
        import logging

        capture = propose._RetryCauseCapture()
        record = logging.LogRecord(
            name="careloop.agents.invoke",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="proposal call succeeded on retry",
            args=None,
            exc_info=None,
        )
        capture.emit(record)
        assert capture.causes == []


class TestTheSummaryArithmetic:
    def _attempt(self, **overrides: object) -> propose.Attempt:
        base: dict[str, object] = {"task_id": "t", "repeat": 1, "formed": True}
        return propose.Attempt.model_validate(base | overrides)

    def test_a_recovered_retry_counts_once_in_each_column(self) -> None:
        attempts = [
            self._attempt(retried=True, recovered=True, retry_cause="MalformedProposal"),
            self._attempt(),
        ]
        summary = propose.summarise(attempts, "m", 1)
        assert summary["retries_fired"] == 1
        assert summary["retries_recovered"] == 1
        assert summary["retry_causes"] == {"MalformedProposal": 1}
        assert summary["structurally_valid"] == 2

    def test_a_failure_is_counted_by_type_and_is_not_structurally_valid(self) -> None:
        attempts = [
            self._attempt(formed=False, failure="MalformedProposal", retried=True),
            self._attempt(formed=False, failure="NoProposalToolCall"),
        ]
        summary = propose.summarise(attempts, "m", 1)
        assert summary["structurally_valid"] == 0
        assert summary["failures_by_type"] == {
            "MalformedProposal": 1,
            "NoProposalToolCall": 1,
        }
        assert summary["retries_recovered"] == 0

    def test_null_expectations_are_left_out_of_the_scored_column(self) -> None:
        """The whole point of `expects: null`: an unscored row must not become a wrong one."""
        attempts = [
            self._attempt(matched_expectation=True),
            self._attempt(matched_expectation=None),
        ]
        summary = propose.summarise(attempts, "m", 1)
        assert summary["unambiguous_tasks_scored"] == 1
        assert summary["unambiguous_matched"] == 1


class TestTheSpendGates:
    def test_the_estimate_is_computed_without_touching_a_provider(self) -> None:
        spend = propose.estimate(propose.load_tasks(), 3, "us.amazon.nova-2-lite-v1:0")
        assert spend.calls == 36
        assert spend.estimated_usd > 0
        assert spend.cached_calls == 0

    def test_a_dry_run_buys_nothing_and_succeeds(self) -> None:
        assert propose.main([]) == 0

    def test_the_planner_spec_is_a_bedrock_one(self) -> None:
        """A dollar ceiling over a credit-metered endpoint is decoration — see
        ``bench/rates.json``."""
        assert propose.PLANNER_SPEC.startswith("bedrock:")

    def test_the_results_path_is_not_committed_over_the_frozen_summary(self) -> None:
        frozen = pathlib.Path("bench/results/summary.json").resolve()
        assert propose.RESULTS_PATH.resolve() != frozen


@pytest.mark.parametrize(
    "failure", sorted(t.__name__ for t in invoke.RECOVERABLE_PROPOSAL_FAILURES)
)
def test_every_recoverable_failure_type_is_reportable(failure: str) -> None:
    """The four names the addendum reports on are exactly the seam's own vocabulary."""
    assert failure in {
        "NoProposalToolCall",
        "AmbiguousProposalToolCall",
        "UnknownProposalTool",
        "MalformedProposal",
    }
