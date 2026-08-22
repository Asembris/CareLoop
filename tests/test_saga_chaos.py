"""Two ways of losing an answer, and one appointment at the end of both.

Everything else in the saga suites reasons about a case that *was* abandoned. These two make the
abandoning happen, in the two shapes it actually takes:

**The hard kill.** A real child process, a real socket, a real SQLite file. The clinic commits the
booking and answers; the child is killed with ``os._exit`` on the line after the response arrives,
so no ``finally`` runs, no lifespan drains, and nothing is committed. The parent then reads Demo
World directly and finds the appointment — the world has changed and CareLoop's store does not know
it. A second process opens the same file and converges.

**The lost response.** No kill at all: the process survives and the *answer* does not. The write
genuinely lands and the client raises as though nobody answered, which is the one thing a caller can
never tell apart from a request that never arrived. Measured before this was handled: the exception
left the node, ``run_case`` marked the case ``FAILED`` — terminal — and the clinic held a booking
nobody would ever look for again.

Both converge on the same six numbers, and the numbers are the point::

    provider appointments        == 1
    logical actions              == 1
    durable results              == 1
    provider idempotency key     == the key frozen before anything was sent
    model calls during recovery  == 0
    verification                 == MATCHED, and the case is DONE

**The kill point is what makes the first proof mean anything.** It is chosen so the provider commit
demonstrably precedes the death, and that is established by *reading the clinic before restarting
anything* rather than by trusting an exit code: if the appointment were not there, the test would be
proving recovery from a crash that happened too early to matter.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    SAGA_EVENT,
    SLOT,
    ExplodingPlanner,
    ExplodingTriage,
    booking_key,
    count,
    ready_to_execute,
)

from careloop.graph.context import unsettled_preparation
from careloop.graph.runner import context_for, run_case
from careloop.graph.verify import ObservationOutcome
from careloop.state import CaseState, TimelineKind
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import ClinicClient, ClinicUnavailable
from demoworld.clinic import Clinic

#: What the killed child exits with. Distinctive, so "it died where it was told to" is checkable.
KILL_CODE = 137

#: How long the child gets. Generous: it books once over loopback and then stops existing.
CHILD_TIMEOUT = 60.0


REPO_ROOT = Path(__file__).resolve().parent.parent


CHILD_SOURCE = textwrap.dedent(
    '''
    """A CareLoop process that dies the instant the clinic confirms its booking.

    Written to a file and run with ``subprocess`` rather than spawned through ``multiprocessing``:
    the death has to be a real ``os._exit`` in a real interpreter, and a generated script says
    exactly what that interpreter will do without any pickling, start-method or import-shape
    questions in the way.
    """

    import asyncio
    import json
    import os
    import sys

    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {tests!r})

    from helpers import FIXED_NOW
    from saga_helpers import CASE_ID, SAGA_EVENT, ExplodingPlanner, ExplodingTriage

    from careloop.graph.runner import context_for, run_case
    from careloop.state.sqlite_store import SqliteCaseStore
    from careloop.tools.clinic import ClinicClient


    class KillingClinic(ClinicClient):
        """Books for real, records what the clinic said, and then stops existing.

        ``os._exit`` rather than ``sys.exit``: no ``finally``, no ``atexit``, no flush, no chance
        for anything to be committed on the way out. The confirmation is written to a file *before*
        the exit so the parent can prove the clinic answered — and the parent then checks Demo World
        itself rather than believing this file, because a file this process wrote is this process's
        own receipt.
        """

        def book(self, args, idempotency_key):
            booked = super().book(args, idempotency_key)
            with open({receipt!r}, "w", encoding="utf-8") as handle:
                json.dump({{"confirmation_id": booked.confirmation_id}}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os._exit({kill_code})


    def main() -> None:
        store = SqliteCaseStore({db!r})
        clinic = KillingClinic({url!r})
        context = context_for(
            store,
            SAGA_EVENT,
            case_id=CASE_ID,
            triage=ExplodingTriage(),
            planner=ExplodingPlanner(),
            clinic=clinic,
            now=lambda: FIXED_NOW,
        )
        asyncio.run(run_case(context, max_node_executions=12))
        raise SystemExit("the child was supposed to die during the booking and did not")


    main()
    '''
)


def recover_in_this_process(db: Path, clinic: ClinicClient):
    """A fresh store handle, a fresh executor and both model seams trapped. The restart."""
    store = SqliteCaseStore(db)
    context = context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        clinic=clinic,
        now=lambda: FIXED_NOW,
    )
    result = asyncio.run(run_case(context, max_node_executions=12))
    return result, store


class TestTheProcessDiesAfterTheClinicCommits:
    """The hero proof. A real kill, between the provider's commit and CareLoop's record."""

    @pytest.fixture
    def killed(self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, tmp_path):
        """Run a child that books and dies, then hand back what the world and the store hold.

        The setup is done here rather than in the child so the case reaching ``READY_TO_EXECUTE``
        is not part of what is being proved, and so a child that failed to start is distinguishable
        from a child that died where it was told to.
        """
        base_url, _ = clinic_service
        db = tmp_path / "cases.sqlite3"
        receipt = tmp_path / "confirmation.json"

        setup = SqliteCaseStore(db)
        ready_to_execute(setup)
        setup.close()

        script = tmp_path / "dying_careloop.py"
        script.write_text(
            CHILD_SOURCE.format(
                repo=str(REPO_ROOT),
                tests=str(REPO_ROOT / "tests"),
                db=str(db),
                url=base_url,
                receipt=str(receipt),
                kill_code=KILL_CODE,
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT,
            check=False,
        )
        return completed, db, receipt, clinic_state

    def test_the_child_died_where_it_was_told_to(self, killed) -> None:
        completed, _, receipt, _ = killed

        assert completed.returncode == KILL_CODE, completed.stderr
        assert receipt.exists()

    def test_the_clinic_committed_before_the_death(self, killed) -> None:
        """**The load-bearing setup assertion**, and it is read from the world rather than the file.

        Without it this proves recovery from a crash that happened too early to matter. Demo World
        is asked directly: it holds the appointment, and the slot it consumed is gone.
        """
        _, _, receipt, world = killed
        confirmation = json.loads(receipt.read_text(encoding="utf-8"))["confirmation_id"]

        held = world.appointments()
        assert len(held) == 1
        assert held[0].confirmation_id == confirmation
        assert len(world.available()) == 2

    def test_careloop_did_not_know(self, killed) -> None:
        """The other half of the setup: the world changed and the store cannot say so.

        A durably prepared action, no result, and a state that means "an authorised action was
        dispatched and nobody knows what became of it".
        """
        _, db, _, _ = killed
        store = SqliteCaseStore(db)
        try:
            case = store.get(CASE_ID)
            assert case.state is CaseState.EXECUTING
            assert case.actions == ()
            assert count(case, TimelineKind.ACTION_PREPARED) == 1
            assert unsettled_preparation(case).idempotency_key == booking_key()
        finally:
            store.close()

    def test_a_new_process_converges_on_the_one_appointment(
        self, killed, clinic_service: tuple[str, Clinic]
    ) -> None:
        """The proof. Six numbers, and the case finishes."""
        _, db, receipt, world = killed
        confirmation = json.loads(receipt.read_text(encoding="utf-8"))["confirmation_id"]
        base_url, _ = clinic_service
        clinic = ClinicClient(base_url)

        try:
            result, store = recover_in_this_process(db, clinic)
        finally:
            clinic.close()

        case = store.get(CASE_ID)
        try:
            assert len(world.appointments()) == 1
            assert world.appointments()[0].confirmation_id == confirmation
            assert len(world.available()) == 2

            assert count(case, TimelineKind.ACTION_PREPARED) == 1
            assert len(case.actions) == 1
            assert case.actions[0].idempotency_key == booking_key()
            assert confirmation in case.actions[0].result

            assert result.executed_nodes == ("dispatch", "reconcile", "verify")
            assert count(case, TimelineKind.PLAN_PROPOSED) == 1
            assert case.approvals == ()

            passed = [
                event for event in case.timeline if event.kind is TimelineKind.VERIFICATION_PASSED
            ]
            assert len(passed) == 1
            assert passed[0].payload["read_back"] == "true"
            assert passed[0].payload["observation"] == str(ObservationOutcome.MATCHED)
            assert case.state is CaseState.DONE
        finally:
            store.close()

    def test_the_recovery_asked_no_model_anything(
        self, killed, clinic_service: tuple[str, Clinic]
    ) -> None:
        """Enforced by the harness rather than counted afterwards.

        Both agents raise on their first call, so a recovery that reached for either would fail
        the node and end the case ``FAILED``, which is louder than a counter nobody reads.
        """
        _, db, _, _ = killed
        base_url, _ = clinic_service
        clinic = ClinicClient(base_url)

        try:
            result, store = recover_in_this_process(db, clinic)
        finally:
            clinic.close()

        try:
            assert "plan" not in result.executed_nodes
            assert "triage" not in result.executed_nodes
            assert "gate" not in result.executed_nodes
            assert store.get(CASE_ID).state is CaseState.DONE
        finally:
            store.close()


class LosingClinic(ClinicClient):
    """The write lands and the answer does not.

    ``super().book`` really posts, so Demo World really commits; the exception raised afterwards is
    what a dropped connection looks like from inside :meth:`careloop.tools.clinic.ClinicClient._
    request`. That is the whole of crash point D as a caller experiences it, and it is
    indistinguishable — necessarily, not incidentally — from a request that never arrived.

    ``writes`` counts what left the process, which is what makes "exactly one write, ever" an
    assertion rather than an inference.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.writes = 0

    def book(self, args, idempotency_key):
        self.writes += 1
        super().book(args, idempotency_key)
        raise ClinicUnavailable("the response was lost on the way back")


class TestTheProviderCommitsAndTheResponseIsLost:
    """No crash. The process survives and the answer does not."""

    @pytest.fixture
    def stranded(self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, tmp_path):
        """Drive a real first attempt whose response never comes back."""
        base_url, _ = clinic_service
        db = tmp_path / "cases.sqlite3"
        setup = SqliteCaseStore(db)
        ready_to_execute(setup)
        setup.close()

        store = SqliteCaseStore(db)
        losing = LosingClinic(base_url)
        context = context_for(
            store,
            SAGA_EVENT,
            case_id=CASE_ID,
            triage=ExplodingTriage(),
            planner=ExplodingPlanner(),
            clinic=losing,
            now=lambda: FIXED_NOW,
        )
        result = asyncio.run(run_case(context, max_node_executions=12))
        losing_writes = losing.writes
        losing.close()
        store.close()
        return result, db, losing_writes, clinic_state

    def test_the_write_landed_even_though_the_answer_did_not(self, stranded) -> None:
        _, _, writes, world = stranded

        assert writes == 1
        assert len(world.appointments()) == 1
        assert world.appointments()[0].confirmation_id == f"apt-{booking_key()[:16]}"

    def test_the_case_waits_rather_than_failing(self, stranded) -> None:
        """The repair. It ended ``FAILED`` — terminal, unrecoverable — before this branch existed.

        ``WAITING_EXTERNAL`` is the honest state: the outcome is unknown, and CareLoop can settle
        it itself as soon as something answers. ``FAILED`` said the case was broken and shut.
        """
        _, db, _, _ = stranded
        store = SqliteCaseStore(db)
        try:
            case = store.get(CASE_ID)
            assert case.state is CaseState.WAITING_EXTERNAL
            assert case.failure_reason is None
            assert case.actions == ()
            assert unsettled_preparation(case).idempotency_key == booking_key()
        finally:
            store.close()

    def test_the_run_never_reached_verification(self, stranded) -> None:
        """Nothing was certified, because nothing was known. The run stops at the effect."""
        result, _, _, _ = stranded

        assert result.executed_nodes == ("dispatch", "execute")

    def test_a_later_run_converges_on_the_one_appointment(
        self, stranded, clinic_service: tuple[str, Clinic]
    ) -> None:
        """The same six numbers, reached without any process ever having died."""
        _, db, first_writes, world = stranded
        base_url, _ = clinic_service
        clinic = ClinicClient(base_url)

        try:
            result, store = recover_in_this_process(db, clinic)
        finally:
            clinic.close()

        case = store.get(CASE_ID)
        try:
            assert first_writes == 1
            assert len(world.appointments()) == 1
            assert world.appointments()[0].confirmation_id == f"apt-{booking_key()[:16]}"
            assert len(world.available()) == 2

            assert count(case, TimelineKind.ACTION_PREPARED) == 1
            assert len(case.actions) == 1
            assert case.actions[0].idempotency_key == booking_key()

            assert result.executed_nodes == ("dispatch", "reconcile", "verify")
            assert count(case, TimelineKind.PLAN_PROPOSED) == 1
            assert case.approvals == ()

            passed = [
                event for event in case.timeline if event.kind is TimelineKind.VERIFICATION_PASSED
            ]
            assert passed[0].payload["observation"] == str(ObservationOutcome.MATCHED)
            assert case.state is CaseState.DONE
        finally:
            store.close()

    def test_the_recovered_booking_holds_what_was_authorised(self, stranded, clinic_service):
        """Not merely *an* appointment: the one the case was authorised to make."""
        _, db, _, world = stranded
        base_url, _ = clinic_service
        clinic = ClinicClient(base_url)

        try:
            _, store = recover_in_this_process(db, clinic)
        finally:
            clinic.close()
        store.close()

        held = world.appointments()[0]
        assert held.patient_id == BOOKING_ARGS["patient_id"]
        assert held.provider_id == BOOKING_ARGS["provider_id"]
        assert held.slot_start == SLOT
        assert held.reason == BOOKING_ARGS["reason"]


class TestBothRoutesReachTheSameIdentity:
    """The two proofs are about one action, and the key is where that is visible."""

    def test_the_key_is_a_function_of_the_case_and_nothing_else(self) -> None:
        """No clock, no random source, no run id, no process id, no fresh model output.

        This is why a second process can pick up the first's work at all, and why the two chaos
        proofs can assert the same literal.
        """
        from careloop.tools.idempotency import idempotency_key

        assert booking_key() == idempotency_key(CASE_ID, "book_appointment", BOOKING_ARGS)
        assert booking_key() == idempotency_key(CASE_ID, "book_appointment", dict(BOOKING_ARGS))

    def test_the_clinic_derives_its_confirmation_from_that_key(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """Which is what lets both proofs name the same appointment without being told it."""
        booked = clinic.book(BOOKING_ARGS, booking_key())

        assert booked.confirmation_id == f"apt-{booking_key()[:16]}"
        assert clinic_state.appointments()[0].confirmation_id == booked.confirmation_id
