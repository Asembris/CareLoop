"""Who picks up a case the last process abandoned.

Reconciliation gave ``EXECUTING`` and ``WAITING_EXTERNAL`` somewhere to go. Nothing sent them there.
Measured before this patch, on a case persisted mid-effect: ``POST /api/events`` answers ``409``
because ``reopen_case`` takes only ``DONE``; the decision routes answer ``404`` because the execute
node raises no approval; ``POST /api/cases/{id}/recheck`` answers ``409`` because a re-check needs
``AWAITING_HUMAN``. The case was correct, recoverable, and unreachable by every verb the API has.

The trigger is process startup, and it is the smallest one that works: no scheduler, no queue, no
EventBridge, no worker. **Anything in one of those two states when a process starts is orphaned by
definition**, because that process has scheduled nothing yet — which is the whole safety argument,
and the reason no lease, claim table or heartbeat is needed. Where a second process starts beside a
live one, the case version's compare-and-set rejects the loser, which
``tests/test_saga_concurrency.py`` measures rather than assumes.

:class:`TestTheSweepFindsWhatWasAbandoned` is the property. :class:`TestTheSweepIsNarrow` is the
half that matters more in practice: a sweep that picked up healthy cases would restart work nobody
asked it to redo.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    SAGA_EVENT,
    booking_key,
    executing_with_a_prepared_action,
    ready_to_execute,
)

from careloop.api.deps import ApiDeps
from careloop.api.sessions import SessionWorlds
from careloop.events.simulator import EventSimulator
from careloop.graph.runner import RunResult
from careloop.state import Case, CaseState, InMemoryCaseStore
from careloop.state.machine import transition


def events_holding_the_saga_event() -> EventSimulator:
    """An event simulator that can answer for the delivery every saga case records."""
    return EventSimulator({SAGA_EVENT.event_id: SAGA_EVENT})


class RecordingRunner:
    """A graph runner that records what it was asked to run and runs nothing.

    The sweep's contract is *scheduling*, and scheduling is all this observes. Driving a real graph
    here would test reconciliation a second time and make it impossible to tell a sweep that picked
    the wrong case from a node that refused it.
    """

    def __init__(self, store) -> None:
        self.store = store
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, store, inbound, case_id) -> RunResult:
        self.asked.append((case_id, inbound.event_id))
        from strands.multiagent.base import Status

        return RunResult(
            case=store.get(case_id), status=Status.COMPLETED, executed_nodes=("dispatch",)
        )


async def sweep(store, *, runner: RecordingRunner) -> tuple[str, ...]:
    """Run the startup sweep the way the lifespan does, and let the scheduled tasks finish."""
    deps = ApiDeps(store=store, events=events_holding_the_saga_event(), graph_runner=runner)
    resumed = deps.resume_unsettled()
    await deps.drain()
    return resumed


def in_state(store, state: CaseState, *, case_id: str) -> Case:
    """Move a hand-built case into ``state`` and persist it."""
    case = store.get(case_id)
    base = case.version
    moved = transition(case, state, reason="setup", actor="test", now=FIXED_NOW)
    return store.save(moved, expected_version=base)


class TestTheSweepFindsWhatWasAbandoned:
    """Both mid-effect states, and the event each is carried forward under."""

    def test_an_executing_case_is_scheduled(self) -> None:
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        runner = RecordingRunner(store)

        resumed = asyncio.run(sweep(store, runner=runner))

        assert resumed == (CASE_ID,)
        assert runner.asked == [(CASE_ID, SAGA_EVENT.event_id)]

    def test_a_waiting_external_case_is_scheduled(self) -> None:
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        in_state(store, CaseState.WAITING_EXTERNAL, case_id=CASE_ID)
        runner = RecordingRunner(store)

        resumed = asyncio.run(sweep(store, runner=runner))

        assert resumed == (CASE_ID,)

    def test_each_case_is_resumed_under_its_own_event(self) -> None:
        """Late binding would resume every case under the last one's delivery."""
        store = InMemoryCaseStore()
        for suffix in ("a", "b", "c"):
            executing_with_a_prepared_action(store, case_id=f"case-{suffix}")
        runner = RecordingRunner(store)

        asyncio.run(sweep(store, runner=runner))

        assert sorted(runner.asked) == [
            ("case-a", SAGA_EVENT.event_id),
            ("case-b", SAGA_EVENT.event_id),
            ("case-c", SAGA_EVENT.event_id),
        ]
        assert len(runner.asked) == 3

    def test_the_sweep_schedules_and_decides_nothing(self) -> None:
        """It starts runs. It reads no tool, no argument, no key, and writes no case."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        before = store.get(CASE_ID)
        runner = RecordingRunner(store)

        asyncio.run(sweep(store, runner=runner))

        after = store.get(CASE_ID)
        assert after.version == before.version
        assert after.state is CaseState.EXECUTING
        assert after.timeline == before.timeline


class TestTheSweepIsNarrow:
    """Everything it must not pick up."""

    @pytest.mark.parametrize(
        "state",
        [
            CaseState.NEW,
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.AWAITING_HUMAN,
        ],
        ids=str,
    )
    def test_a_pre_effect_case_is_left_alone(self, state: CaseState) -> None:
        """Nothing was dispatched, so there is nothing to reconcile and nobody asked for a run."""
        store = InMemoryCaseStore()
        case = ready_to_execute(store)
        if state is not CaseState.READY_TO_EXECUTE:
            store.save(
                case.model_copy(update={"state": state, "version": case.version + 1}),
                expected_version=case.version,
            )
        runner = RecordingRunner(store)

        resumed = asyncio.run(sweep(store, runner=runner))

        assert resumed == ()
        assert runner.asked == []

    @pytest.mark.parametrize("state", [CaseState.VERIFYING, CaseState.DONE], ids=str)
    def test_a_post_result_case_is_left_alone(self, state: CaseState) -> None:
        """``VERIFYING`` already resumes through ``dispatch -> verify`` and needs no sweep.

        Picking it up here would schedule a second run for a case the existing route already
        handles, which is not unsafe but is work nobody asked for — and ``DONE`` is finished.
        """
        store = InMemoryCaseStore()
        case = ready_to_execute(store)
        store.save(
            case.model_copy(update={"state": state, "version": case.version + 1}),
            expected_version=case.version,
        )
        runner = RecordingRunner(store)

        assert asyncio.run(sweep(store, runner=runner)) == ()

    def test_a_case_with_no_recorded_delivery_is_skipped_rather_than_raising(self) -> None:
        """A startup that died on one odd row would take the process down for every other case."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        case = store.get(CASE_ID)
        stripped = tuple(event for event in case.timeline if event.payload.get("event_id") is None)
        store.save(
            case.model_copy(update={"timeline": stripped, "version": case.version + 1}),
            expected_version=case.version,
        )
        runner = RecordingRunner(store)

        assert asyncio.run(sweep(store, runner=runner)) == ()

    def test_a_case_naming_an_event_this_process_lacks_is_skipped(self) -> None:
        """A store restored beside a different event catalogue is odd, not fatal."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        runner = RecordingRunner(store)

        async def go() -> tuple[str, ...]:
            deps = ApiDeps(store=store, events=EventSimulator(), graph_runner=runner)
            return deps.resume_unsettled()

        assert asyncio.run(go()) == ()


class TestTheSweepIsTheSameQuestionInBothAppShapes:
    """One method, two shapes, exactly as ``drain`` already is."""

    def test_a_session_registry_answers_it(self) -> None:
        """Empty at startup, and correct rather than a stub: no world exists to have been abandoned.

        Every session store is ``SqliteCaseStore(":memory:")``, so a restart inherits nothing
        durable and there is no case that could have survived the process that abandoned it.
        """
        worlds = SessionWorlds(build=lambda session: ApiDeps(store=InMemoryCaseStore()))

        assert worlds.resume_unsettled() == ()

    def test_both_shapes_expose_it(self) -> None:
        """The lifespan asks one question and does not have to know which shape it got."""
        worlds = SessionWorlds(build=lambda session: ApiDeps(store=InMemoryCaseStore()))

        assert callable(ApiDeps(store=InMemoryCaseStore()).resume_unsettled)
        assert callable(worlds.resume_unsettled)


class TestTheSweepRecoversForReal:
    """End to end through the real graph, not the recording runner."""

    def test_a_swept_case_reaches_done_against_a_real_clinic(self, clinic, clinic_state) -> None:
        """The whole chain: sweep, dispatch, reconcile, replay, independent read, ``DONE``."""
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        async def go() -> tuple[str, ...]:
            deps = ApiDeps(
                store=store,
                events=events_holding_the_saga_event(),
                context_factory=_clinic_bound_factory(clinic),
            )
            resumed = deps.resume_unsettled()
            await deps.drain()
            return resumed

        resumed = asyncio.run(go())

        case = store.get(CASE_ID)
        assert resumed == (CASE_ID,)
        assert case.state is CaseState.DONE
        assert len(clinic_state.appointments()) == 1
        assert case.actions[0].idempotency_key == booking_key()
        assert clinic_state.appointments()[0].patient_id == BOOKING_ARGS["patient_id"]


def _clinic_bound_factory(clinic):
    """A context factory binding every run to the test's clinic, with both model seams trapped."""
    from saga_helpers import ExplodingPlanner, ExplodingTriage

    from careloop.graph.runner import context_for

    def build(store, inbound, case_id):
        return context_for(
            store,
            inbound,
            case_id=case_id,
            triage=ExplodingTriage(),
            planner=ExplodingPlanner(),
            clinic=clinic,
            now=lambda: FIXED_NOW,
        )

    return build


class TestTheLifespanIsWhatCallsIt:
    """The wiring, asserted through a real app rather than through the method.

    Everything above exercises :meth:`careloop.api.deps.ApiDeps.resume_unsettled` directly, which
    proves the sweep is correct and proves nothing about whether anything calls it. Measured: a
    mutant deleting the call from the lifespan left all sixteen of those tests green. This is the
    one that turns red.
    """

    @staticmethod
    def _app_over(store, clinic):
        from careloop.api.app import create_app

        return create_app(
            store,
            events=events_holding_the_saga_event(),
            context_factory=_clinic_bound_factory(clinic),
            clinic=clinic,
        )

    def test_starting_the_app_recovers_an_abandoned_case(self, clinic, clinic_state) -> None:
        from fastapi.testclient import TestClient

        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)

        with TestClient(self._app_over(store, clinic)):
            settled = _wait_for(store, CaseState.DONE)

        assert settled is CaseState.DONE
        assert len(clinic_state.appointments()) == 1
        assert store.get(CASE_ID).actions[0].idempotency_key == booking_key()

    def test_starting_the_app_over_healthy_cases_schedules_nothing(self, clinic) -> None:
        """Vacuity: an app that resumed everything would pass the test above and be wrong."""
        from fastapi.testclient import TestClient

        store = InMemoryCaseStore()
        ready_to_execute(store)
        before = store.get(CASE_ID)

        with TestClient(self._app_over(store, clinic)) as client:
            assert client.get("/api/health").status_code == 200

        assert store.get(CASE_ID).version == before.version
        assert store.get(CASE_ID).state is CaseState.READY_TO_EXECUTE


def _wait_for(store, state: CaseState, *, timeout: float = 20.0) -> CaseState:
    """Poll the store until the case reaches ``state`` or the deadline passes.

    Polled rather than slept through, and with a deadline, for the reason
    ``tests/clinic_helpers.py`` polls for its socket: a fixed sleep is either slow or flaky. The
    run is started by the lifespan on the app's own loop, so the only handle this thread has on it
    is the durable state it commits.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = store.get(CASE_ID).state
        if current is state or current is CaseState.FAILED:
            return current
        time.sleep(0.02)
    return store.get(CASE_ID).state
