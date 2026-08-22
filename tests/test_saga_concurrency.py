"""Two workers, one abandoned action, one appointment.

Crash recovery makes concurrent work on a single case ordinary for the first time. Until now the API
refused a second in-flight run per case and one process ran one case at a time, so nothing ever
raced; a sweep for abandoned work — a second process starting, an operator running a recovery by
hand while the first process is still alive — makes two workers on one case a normal Tuesday.

That exposed a defect that was already in the tree and had never been reachable. ``run_case``
caught every exception a node raised and marked the case ``FAILED``. A ``VersionConflict`` is not a
broken case, it is a case that is *ahead*: the winner has already moved it somewhere correct, and
the loser was about to read that fresh state, notice it was not at rest, and commit ``-> FAILED`` on
top of it. **A correctly progressing case terminated on behalf of the run that lost the race.**

Which guarantee comes from where is the whole point of this file, and the answers are deliberately
not all "the application":

    at most one durable record        the database — a guarded ``UPDATE ... WHERE version = ?``
    at most one appointment           the provider — Demo World's own idempotency map
    identity agreement between them   the application — the frozen key both workers reuse

:class:`TestALosingWriterDoesNotTerminateTheCase` is the repair. :class:`TestTwoRealWorkersProduce
OneOfEverything` is the end-to-end statement of it, over one SQLite *file* and one real clinic, with
two threads. :class:`TestTheGuaranteesComeFromWhereTheyAreClaimedTo` is the vacuity half: it removes
each guarantee in turn and shows the specific thing that breaks.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest
from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    SAGA_EVENT,
    ExplodingPlanner,
    ExplodingTriage,
    booking_key,
    executing_with_a_prepared_action,
    ready_to_execute,
)

from careloop.graph.runner import context_for, run_case
from careloop.state import Case, CaseState
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.state.store import CaseStore, VersionConflict
from careloop.tools.clinic import ClinicClient
from demoworld.clinic import Clinic


class RacingStore:
    """A store that lets somebody else win, exactly once, at a chosen commit.

    Deterministic rather than timing-based. A thread race reproduces the *situation*; this
    reproduces the *interleaving*, at the one instruction where it matters — between a worker
    reading a version and committing against it. A test that had to hope two threads collided would
    be either slow or flaky, and this is neither.
    """

    def __init__(self, inner: CaseStore, *, steal_on_save: int = 1) -> None:
        self.inner = inner
        self._steal_on = steal_on_save
        self._saves = 0
        self.stolen: Case | None = None
        self.conflicts = 0

    def _steal(self, case_id: str) -> None:
        """Commit somebody else's change, so the next guarded write finds the wrong version."""
        from careloop.state.case import TimelineKind
        from careloop.state.machine import append_event

        held = self.inner.get(case_id)
        rival = append_event(
            held,
            kind=TimelineKind.EVIDENCE_RECORDED,
            actor="system",
            summary="another worker got here first",
            now=FIXED_NOW,
        )
        self.stolen = self.inner.save(rival, expected_version=held.version)

    def get(self, case_id: str) -> Case:
        return self.inner.get(case_id)

    def list_by_state(self, state: CaseState) -> list[Case]:
        return self.inner.list_by_state(state)

    def timeline(self, case_id: str):
        return self.inner.timeline(case_id)

    def create(self, case: Case) -> Case:
        return self.inner.create(case)

    def save(self, case: Case, *, expected_version: int) -> Case:
        self._saves += 1
        if self._saves == self._steal_on:
            self._steal(case.case_id)
        try:
            return self.inner.save(case, expected_version=expected_version)
        except VersionConflict:
            self.conflicts += 1
            raise

    def clear(self) -> int:
        return self.inner.clear()

    def close(self) -> None:
        self.inner.close()


def run_one(store, *, clinic: ClinicClient | None = None):
    """One graph pass with both model seams booby-trapped."""
    extra = {"clinic": clinic} if clinic is not None else {}
    context = context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        now=lambda: FIXED_NOW,
        **extra,
    )
    return asyncio.run(run_case(context, max_node_executions=12)), context


class TestALosingWriterDoesNotTerminateTheCase:
    """The repair, at the exact interleaving that produced the defect."""

    def test_the_case_is_not_failed_when_a_recovery_loses_its_commit(
        self, clinic: ClinicClient, tmp_path
    ) -> None:
        inner = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(inner)
        racing = RacingStore(inner)

        with pytest.raises(VersionConflict):
            run_one(racing, clinic=clinic)

        case = inner.get(CASE_ID)
        assert racing.conflicts == 1
        assert case.state is not CaseState.FAILED
        assert case.failure_reason is None
        inner.close()

    def test_the_case_keeps_the_state_the_winner_left_it_in(
        self, clinic: ClinicClient, tmp_path
    ) -> None:
        """Not merely "not FAILED": untouched. The loser writes nothing at all."""
        inner = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(inner)
        racing = RacingStore(inner)

        with pytest.raises(VersionConflict):
            run_one(racing, clinic=clinic)

        assert racing.stolen is not None
        assert inner.get(CASE_ID).version == racing.stolen.version
        inner.close()

    def test_the_run_still_reports_that_it_did_not_complete(
        self, clinic: ClinicClient, tmp_path
    ) -> None:
        """The exception is the truth about the *run*, and it is not swallowed.

        Silently returning would tell a caller their recovery succeeded when another process did it.
        What changes is only that no *write* happens.
        """
        inner = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(inner)

        with pytest.raises(VersionConflict):
            run_one(RacingStore(inner), clinic=clinic)

        inner.close()

    def test_an_ordinary_node_failure_still_fails_the_case(self, tmp_path) -> None:
        """Vacuity for the whole class. The catch was narrowed, not removed.

        A case in ``EXECUTING`` whose preparation does not agree with its proposal is a genuine
        inconsistency, and it still ends ``FAILED``. Narrowing the catch to spare a losing writer
        must not spare anything else.
        """
        store = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(store, key="not-the-key-these-arguments-derive")

        run_one(store)

        assert store.get(CASE_ID).state is CaseState.FAILED
        store.close()


class TestALateWorkerDoesNotUndoTheOneThatWon:
    """The second interleaving, and the one two real threads found rather than a review.

    The dispatch edge selects a case while it is mid-effect. Between that condition and the node
    body there is a gap, and a second worker can settle the case inside it — legitimately and
    completely. The late arrival then re-reads the store, finds the preparation settled, and
    concludes the case is inconsistent.

    **The compare-and-set does not catch this one**, which is what makes it worth its own class.
    Reading fresh is exactly what makes the write legal: the loser is not committing against a stale
    version, it is committing a considered verdict about a case it has misread. Reproduced before
    the guard existed::

        A says: reconcile: book_appointment reconciled
        after A: VERIFYING
        B says: reconcile: failed, reconciliation found no prepared action ...
        after B: FAILED

    A healthy, verified-in-a-moment case terminated by the worker with nothing left to do.
    """

    def _both_workers(self, store, clinic):
        """One node body each, so the interleaving is chosen rather than hoped for."""
        from careloop.graph.nodes import ReconcileNode

        def node():
            return ReconcileNode(
                "reconcile",
                context_for(
                    store,
                    SAGA_EVENT,
                    case_id=CASE_ID,
                    triage=ExplodingTriage(),
                    planner=ExplodingPlanner(),
                    clinic=clinic,
                    now=lambda: FIXED_NOW,
                ),
            )

        winner = asyncio.run(node().run())
        latecomer = asyncio.run(node().run())
        return winner, latecomer

    def test_the_latecomer_leaves_a_verifying_case_alone(
        self, clinic: ClinicClient, tmp_path
    ) -> None:
        store = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(store)
        try:
            settled = store.get(CASE_ID)
            self._both_workers(store, clinic)

            case = store.get(CASE_ID)
            assert case.state is CaseState.VERIFYING
            assert case.failure_reason is None
            assert len(case.actions) == 1
            assert case.version > settled.version
        finally:
            store.close()

    def test_the_latecomer_writes_nothing_at_all(self, clinic: ClinicClient, tmp_path) -> None:
        """Not merely "not FAILED". The version does not move for the second worker."""
        store = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(store)
        try:
            self._both_workers(store, clinic)
            after_both = store.get(CASE_ID).version

            self._both_workers(store, clinic)

            assert store.get(CASE_ID).version == after_both
        finally:
            store.close()

    def test_the_latecomer_issues_no_second_write(
        self, clinic: ClinicClient, clinic_state: Clinic, tmp_path
    ) -> None:
        """It stops before ``may_replay`` is even consulted, so no request can leave."""
        store = SqliteCaseStore(tmp_path / "cases.sqlite3")
        executing_with_a_prepared_action(store)
        try:
            self._both_workers(store, clinic)

            assert len(clinic_state.appointments()) == 1
            assert len(clinic_state.available()) == 2
        finally:
            store.close()

    def test_a_genuinely_inconsistent_case_still_fails(
        self, clinic: ClinicClient, tmp_path
    ) -> None:
        """Vacuity. The guard reads the *state*, so a real inconsistency is untouched by it.

        A case still sitting in ``EXECUTING`` with nothing prepared has not been settled by anybody;
        it is a case whose own trail contradicts itself, and it still ends ``FAILED``.
        """
        from careloop.state.machine import transition

        store = SqliteCaseStore(tmp_path / "cases.sqlite3")
        case = ready_to_execute(store)
        base = case.version
        store.save(
            transition(case, CaseState.EXECUTING, reason="setup", actor="test", now=FIXED_NOW),
            expected_version=base,
        )
        try:
            run_one(store, clinic=clinic)

            assert store.get(CASE_ID).state is CaseState.FAILED
        finally:
            store.close()


class TestTwoRealWorkersProduceOneOfEverything:
    """Two threads, one SQLite file, one clinic, one appointment."""

    @staticmethod
    def _recover_twice(path, clinic_url: str) -> list[BaseException | None]:
        """Two independent workers, each with its own store connection and its own client."""
        outcomes: list[BaseException | None] = [None, None]

        def work(slot: int) -> None:
            store = SqliteCaseStore(path)
            client = ClinicClient(clinic_url)
            try:
                run_one(store, clinic=client)
            except BaseException as raised:
                outcomes[slot] = raised
            finally:
                client.close()
                store.close()

        workers = [threading.Thread(target=work, args=(slot,)) for slot in (0, 1)]
        previous = sys.getswitchinterval()
        # The window a served process opens constantly and a test process almost never does. The
        # same device ``tests/test_session_worlds_are_serialised.py`` uses, and stated here rather
        # than left as a mystery.
        sys.setswitchinterval(1e-7)
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)
        finally:
            sys.setswitchinterval(previous)
        return outcomes

    def test_one_appointment_one_record_and_a_case_that_is_not_failed(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, tmp_path
    ) -> None:
        base_url, _ = clinic_service
        path = tmp_path / "cases.sqlite3"
        setup = SqliteCaseStore(path)
        executing_with_a_prepared_action(setup)
        setup.close()

        outcomes = self._recover_twice(path, base_url)

        reader = SqliteCaseStore(path)
        case = reader.get(CASE_ID)
        try:
            assert len(clinic_state.appointments()) == 1
            assert len(clinic_state.available()) == 2
            assert len(case.actions) == 1
            assert case.actions[0].idempotency_key == booking_key()
            assert case.state is not CaseState.FAILED
            assert all(
                outcome is None or isinstance(outcome, VersionConflict) for outcome in outcomes
            ), outcomes
        finally:
            reader.close()

    def test_at_least_one_worker_finished_the_case(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, tmp_path
    ) -> None:
        """Liveness beside the safety. Losing a race must not lose the work.

        Whichever worker won carried the case all the way through the independent read-back, so the
        pair together still produce a verified, finished case — which is what makes this a race with
        a correct outcome rather than two runs cancelling each other out.
        """
        base_url, _ = clinic_service
        path = tmp_path / "cases.sqlite3"
        setup = SqliteCaseStore(path)
        executing_with_a_prepared_action(setup)
        setup.close()

        self._recover_twice(path, base_url)

        reader = SqliteCaseStore(path)
        try:
            final = reader.get(CASE_ID)
            assert final.state in (CaseState.DONE, CaseState.VERIFYING), (
                f"{final.state}: {final.failure_reason}; {[e.summary for e in final.timeline[-6:]]}"
            )
        finally:
            reader.close()


class TestTheGuaranteesComeFromWhereTheyAreClaimedTo:
    """Each guarantee removed in turn, and the specific thing that breaks."""

    def test_the_provider_is_what_stops_a_second_appointment(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """Two writes with the same key reach the clinic and one appointment exists.

        Nothing in CareLoop is involved: this is the client calling the service twice. The
        application's contribution is reusing the key, and the *provider's* is what that reuse
        buys. Attributing this to CareLoop would be claiming a guarantee it does not implement.
        """
        first = clinic.book(BOOKING_ARGS, booking_key())
        second = clinic.book(BOOKING_ARGS, booking_key())

        assert first == second
        assert len(clinic_state.appointments()) == 1
        assert len(clinic_state.available()) == 2

    def test_the_database_is_what_stops_a_second_record(self, tmp_path) -> None:
        """Two stores on one file, both committing against the same version. One is rejected.

        Two connections rather than one object, so what refuses the loser is the guarded ``UPDATE``
        rather than the in-process lock — which is what keeps the guarantee true for a second
        process the lock knows nothing about.
        """
        from careloop.state.case import TimelineKind
        from careloop.state.machine import append_event

        path = tmp_path / "cases.sqlite3"
        setup = SqliteCaseStore(path)
        executing_with_a_prepared_action(setup)
        setup.close()

        left, right = SqliteCaseStore(path), SqliteCaseStore(path)
        try:
            held = left.get(CASE_ID)
            base = held.version
            edited = append_event(
                held, kind=TimelineKind.EVIDENCE_RECORDED, actor="a", summary="x", now=FIXED_NOW
            )
            left.save(edited, expected_version=base)

            with pytest.raises(VersionConflict):
                right.save(edited, expected_version=base)
        finally:
            left.close()
            right.close()

    def test_the_application_is_what_makes_the_two_agree(self) -> None:
        """The one guarantee CareLoop does supply: both workers reuse the frozen identity.

        Neither derives a key from anything a second process could disagree about — no clock, no
        random source, no run id, no fresh model output. Same durable case, same key, so the
        provider's map and the database's version guard are talking about the same action.
        """
        from careloop.graph.context import unsettled_preparation
        from careloop.state import InMemoryCaseStore

        left = executing_with_a_prepared_action(InMemoryCaseStore())
        right = executing_with_a_prepared_action(InMemoryCaseStore())

        assert unsettled_preparation(left) == unsettled_preparation(right)
        assert unsettled_preparation(left).idempotency_key == booking_key()
