"""The benchmark runner's spend discipline, offline.

``invoke_once`` is the only function in the harness that can reach a provider, and every test here
stubs it. What is being pinned is the money: that a cache miss is not bought without explicit
confirmation, that a cached answer is never bought twice, and that the ceiling stops a runaway
part-way through rather than after the fact.
"""

from __future__ import annotations

import pathlib

import pytest

from bench.cache import ResponseCache
from bench.candidates import Candidate
from bench.cost import BudgetExceeded, RateTable, SpendLedger
from bench.runner import PARAMS, CallOutcome, key_for, run_candidate
from bench.tasks import Task, TaskSet


def planner_task(**overrides) -> Task:
    payload = {
        "task_id": "t-plan",
        "kind": "planner",
        "prompt": "Book it.",
        "expect": {"tool_name": "book_appointment", "args": {"patient_id": "PT-4417"}},
    }
    payload.update(overrides)
    return Task.model_validate(payload)


class TestRunnerSpendDiscipline:
    def _fixture(self, tmp_path: pathlib.Path):
        tasks = TaskSet(tasks=(planner_task(),))
        cache = ResponseCache(tmp_path)
        rates = RateTable(
            currency="USD",
            unit="per_1m_tokens",
            as_of="x",
            region="us-east-1",
            rates={"m": {"input": 1.0, "output": 1.0}},
        )
        candidate = Candidate(candidate_id="cand", spec="bedrock:m", label="Cand")
        return tasks, cache, rates, candidate

    def test_a_cache_miss_is_skipped_without_confirmation(self, tmp_path: pathlib.Path):
        tasks, cache, rates, candidate = self._fixture(tmp_path)
        called: list[str] = []

        def spy(*args, **kwargs):
            called.append("bought")
            return CallOutcome(ok=True, output={"tool_name": "book_appointment"})

        scores = run_candidate(
            candidate,
            tasks,
            cache,
            rates,
            SpendLedger(1.0),
            allow_spend=False,
            env={"AWS_DEFAULT_REGION": "us-east-1"},
            invoke=spy,
        )
        assert called == []
        assert scores == []

    def test_a_cache_miss_is_bought_with_confirmation(self, tmp_path: pathlib.Path):
        tasks, cache, rates, candidate = self._fixture(tmp_path)
        calls: list[str] = []

        def spy(*args, **kwargs):
            calls.append("bought")
            return CallOutcome(
                ok=True,
                output={"tool_name": "book_appointment", "patient_id": "PT-4417"},
                input_tokens=10,
                output_tokens=5,
            )

        scores = run_candidate(
            candidate,
            tasks,
            cache,
            rates,
            SpendLedger(1.0),
            allow_spend=True,
            env={"AWS_DEFAULT_REGION": "us-east-1"},
            invoke=spy,
        )
        assert calls == ["bought"]
        assert len(scores) == 1 and scores[0].tool_correct

    def test_a_cached_answer_is_never_re_bought(self, tmp_path: pathlib.Path):
        tasks, cache, rates, candidate = self._fixture(tmp_path)
        env = {"AWS_DEFAULT_REGION": "us-east-1"}
        bought: list[str] = []

        def spy(*args, **kwargs):
            bought.append("x")
            return CallOutcome(ok=True, output={"tool_name": "book_appointment"})

        for _ in range(3):
            run_candidate(
                candidate,
                tasks,
                cache,
                rates,
                SpendLedger(1.0),
                allow_spend=True,
                env=env,
                invoke=spy,
            )
        assert bought == ["x"], "a cached response was bought again"

    def test_the_ledger_stops_a_runaway_mid_run(self, tmp_path: pathlib.Path):
        many = TaskSet(tasks=tuple(planner_task(task_id=f"t-{i}") for i in range(10)))
        _, cache, rates, candidate = self._fixture(tmp_path)

        def spy(*args, **kwargs):
            return CallOutcome(
                ok=True,
                output={"tool_name": "book_appointment"},
                input_tokens=1_000_000,
                output_tokens=0,
            )

        with pytest.raises(BudgetExceeded):
            run_candidate(
                candidate,
                many,
                cache,
                rates,
                SpendLedger(2.5),
                allow_spend=True,
                env={"AWS_DEFAULT_REGION": "us-east-1"},
                invoke=spy,
            )

    def test_a_candidate_only_runs_its_declared_kinds(self, tmp_path: pathlib.Path):
        mixed = TaskSet(
            tasks=(
                planner_task(task_id="p1"),
                Task.model_validate(
                    {
                        "task_id": "t1",
                        "kind": "triage",
                        "prompt": "p",
                        "expect": {"fields": {"category": "OTHER"}},
                    }
                ),
            )
        )
        _, cache, rates, _ = self._fixture(tmp_path)
        triage_only = Candidate(
            candidate_id="micro", spec="bedrock:m", label="Micro", kinds=("triage",)
        )
        seen: list[str] = []

        def spy(candidate, task, system, prompt, env=None):
            seen.append(task.task_id)
            return CallOutcome(ok=True, output={"category": "OTHER"})

        run_candidate(
            triage_only,
            mixed,
            cache,
            rates,
            SpendLedger(1.0),
            allow_spend=True,
            env={"AWS_DEFAULT_REGION": "us-east-1"},
            invoke=spy,
        )
        assert seen == ["t1"]

    def test_the_key_covers_the_sampling_parameters(self, tmp_path: pathlib.Path):
        _, _, _, candidate = self._fixture(tmp_path)
        task = planner_task()
        system, prompt = "sys", "prompt"
        original = key_for(candidate, task, system, prompt, "m")
        PARAMS["temperature"] = 0.7
        try:
            assert key_for(candidate, task, system, prompt, "m") != original
        finally:
            PARAMS["temperature"] = 0.0
