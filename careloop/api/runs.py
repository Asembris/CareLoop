"""Graph runs in flight.

**Why the verbs return 202 rather than the finished case.**

A graph run is not instant, and against a real provider it is seconds. The dashboard has a "Working"
section, and a synchronous endpoint would make that section unreachable: the caller blocks for the
whole run and everything appears at once, so the column never lights up while there is work in it.
That is the demo beat — an email arrives, the case appears in Working, then the decision card
appears — and it is the reason this exists.

Nothing had to be built to make in-flight work *observable*, only to stop it being hidden. Every
node commits its transition to the store before it finishes, so a dashboard polling ``GET
/api/cases`` sees a case move through ``GATHERING_EVIDENCE``, ``PLANNING`` and ``EXECUTING`` as it
goes. A synchronous endpoint would throw that away.

**This registry is execution context, not business facts, and invariant 8 is untouched.** Nothing
here is consulted for anything durable: whether an action happened is ``case.actions``, whether a
human approved is ``case.approvals``, and whether a run failed leaves the case ``FAILED`` with a
reason on its timeline. Losing this registry on restart costs a caller the answer to "how did that
one run go", and nothing else. It is in memory for exactly that reason, and putting anything the
store should own in here would be the invariant 8 violation it currently is not.

No task queue, no worker process, no websocket: one ``asyncio`` task per run, in the process already
serving the request.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from careloop.graph.runner import RunResult


class RunState(StrEnum):
    """Where a scheduled run has got to."""

    RUNNING = "RUNNING"
    SETTLED = "SETTLED"
    """The graph stopped. The *case* may be parked, done or failed — read the case for that."""

    ERRORED = "ERRORED"
    """A node raised. The case is ``FAILED`` and its timeline says why."""


class RunRecord(BaseModel):
    """One run, as the registry holds it."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    case_id: str
    state: RunState
    executed_nodes: tuple[str, ...]
    error: str | None
    """The *type name* of what was raised, never its message.

    A message is free text assembled from wherever the failure happened and can carry argument
    values; the type name says what kind of thing went wrong without becoming a channel. What
    actually went wrong is on the case's timeline, where the audit trail keeps detail.
    """


class UnknownRun(KeyError):
    """Raised when a run id was never scheduled here."""


class RunAlreadyInFlight(Exception):
    """Raised when a case already has a run that has not settled.

    Two concurrent runs of one case would race on the store's compare-and-set and one would lose
    with a ``VersionConflict`` from somewhere deep in a node. Refusing the second is a scheduling
    decision, which is the only kind of decision this layer makes.
    """


class RunRegistry:
    """Runs scheduled by this process, keyed by run id."""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._counter: dict[str, int] = {}

    def next_run_id(self, case_id: str) -> str:
        """A readable, per-process-deterministic run id.

        Derived from the case and a counter rather than a clock or a random source, so a test can
        name the run it expects and a log line reads as ``run-case-0001-2`` rather than a uuid.
        """
        self._counter[case_id] = self._counter.get(case_id, 0) + 1
        return f"run-{case_id}-{self._counter[case_id]}"

    def in_flight_for(self, case_id: str) -> RunRecord | None:
        """The unsettled run for ``case_id``, if one is going."""
        for record in self._records.values():
            if record.case_id == case_id and record.state is RunState.RUNNING:
                return record
        return None

    def get(self, run_id: str) -> RunRecord:
        """One run.

        Raises:
            UnknownRun: if this process never scheduled it.
        """
        if run_id not in self._records:
            raise UnknownRun(f"no run {run_id!r} in this process")
        return self._records[run_id]

    def start(self, case_id: str, run: Callable[[], Awaitable[RunResult]]) -> RunRecord:
        """Schedule ``run`` and return immediately.

        Raises:
            RunAlreadyInFlight: if this case already has an unsettled run.
        """
        if (existing := self.in_flight_for(case_id)) is not None:
            raise RunAlreadyInFlight(
                f"case {case_id!r} already has run {existing.run_id} in flight; two concurrent "
                f"runs of one case would race on the store's compare-and-set"
            )

        run_id = self.next_run_id(case_id)
        self._records[run_id] = RunRecord(
            run_id=run_id,
            case_id=case_id,
            state=RunState.RUNNING,
            executed_nodes=(),
            error=None,
        )
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id, run))
        return self._records[run_id]

    async def _drive(self, run_id: str, run: Callable[[], Awaitable[RunResult]]) -> None:
        """Run to completion and record what happened. Never re-raises.

        A raising node has already left the case ``FAILED`` — ``run_case`` marks it before letting
        the exception out — so there is nothing left to do here but write down that it happened.
        Letting it escape would only produce an unretrieved-task warning on an event loop nobody is
        watching.
        """
        record = self._records[run_id]
        try:
            result = await run()
        except Exception as failure:
            self._records[run_id] = record.model_copy(
                update={"state": RunState.ERRORED, "error": type(failure).__name__}
            )
        else:
            self._records[run_id] = record.model_copy(
                update={"state": RunState.SETTLED, "executed_nodes": result.executed_nodes}
            )

    async def wait(self, run_id: str, timeout: float) -> RunRecord:
        """Wait up to ``timeout`` seconds for a run to settle, then report where it got to.

        A timeout is not an error and does not cancel anything: the run keeps going and the caller
        is told it is still ``RUNNING``, which is the same answer polling would have given. Shielded
        for that reason — ``wait_for`` cancels what it is waiting on, and a caller giving up must
        not kill work the store is going to be asked about.
        """
        task = self._tasks.get(run_id)
        if task is None or task.done() or timeout <= 0:
            return self.get(run_id)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.get(run_id)

    async def drain(self) -> None:
        """Wait for every scheduled run to finish. For orderly shutdown and for tests."""
        if pending := [task for task in self._tasks.values() if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "RunAlreadyInFlight",
    "RunRecord",
    "RunRegistry",
    "RunState",
    "UnknownRun",
]
