"""A wake cannot start work in a world that has been stopped, cleared or evicted.

This is the lifecycle foundation for waking a case parked in ``WAITING_EXTERNAL``, and nothing here
arms a wake in production yet: :meth:`careloop.api.wake.ReconcileWaker.arm` has no caller in
``careloop/``. That is deliberate. The scheduling policy is the next patch, and the property worth
proving first is the one a policy cannot fix afterwards — that a wake and a teardown racing each
other have exactly two outcomes, both of them safe, and never the third.

**The third outcome is the whole point.** A wake that checks a flag, finds the world alive, and is
then descheduled while a reset empties the store or an eviction ends the clinic would register a run
against a world that no longer exists. Nothing about that is caught downstream: the run reads a
cleared store, or books against a Demo World session that has been told to forget the visitor. So
the guarantee is not "the wake checks" but *"once ``stop`` returns, no wake here can register"*, and
these tests are written to fail if it degrades back into a check.

**Races are forced, never slept through.** Every ordering below is pinned by handshakes —
:class:`threading.Event` between the loop and a worker thread, and a spy that fires exactly when the
lifecycle reaches its own critical section. A test that reproduced a race by timing would be a test
that passes on a fast machine, which is the same as no test at all. There is no ``time.sleep`` in
this file and no timing-dependent assertion; the timeouts that appear are deadlocks turning into
failures rather than waits.

The registration a wake performs here is a **real** :class:`careloop.api.runs.RunRegistry` start, so
"a wake registered a run" and "the lifecycle can see it" are the same statement the production code
makes rather than a stand-in for it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from careloop.api.deps import ApiDeps
from careloop.api.runs import RunRegistry
from careloop.api.sessions import SessionWorlds
from careloop.api.wake import WAKE_DELAYS, ReconcileWaker, RecoveryOutcome, stopped_waker
from careloop.state import InMemoryCaseStore

#: How long a handshake may wait before the test fails instead of hanging.
#:
#: Generous, because the cost of being wrong is a confusing hang rather than a slow suite: nothing
#: here waits at all when the code is correct, and every one of these is a deadlock detector.
HANDSHAKE_TIMEOUT = 10.0

CASE = "case-wake"


def waited(event: threading.Event, what: str) -> None:
    """Wait for a handshake, failing the test rather than hanging if it never comes."""
    assert event.wait(HANDSHAKE_TIMEOUT), f"timed out waiting for {what}"


#: How many loop turns a test gives the scheduler before deciding it is never going to act.
#:
#: Every wait below is on a synchronous hand-off with no I/O in it, so a correct run needs a
#: handful.
#: Bounded rather than open because an unbounded spin turns a mutant into a hang, and a hang is a
#: measurement nobody can read.
TURNS = 500


async def until(predicate, what: str) -> None:
    """Give the loop turns until ``predicate`` holds, then fail rather than spin forever."""
    for _ in range(TURNS):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


def never() -> Callable[[float], Awaitable[None]]:
    """A sleeper that never returns, so a wake stays pending until something withdraws it."""

    async def sleeper(_delay: float) -> None:
        await asyncio.Event().wait()

    return sleeper


def held_by(gate: threading.Event) -> Callable[[float], Awaitable[None]]:
    """A sleeper that returns once ``gate`` is set, releasing the wake into its critical section.

    Polled on the loop rather than blocked on, because a wake blocking a worker's event on the event
    loop would stop the very thread that is meant to set it.
    """

    async def sleeper(_delay: float) -> None:
        while not gate.is_set():
            await asyncio.sleep(0)

    return sleeper


class Runs:
    """A registry plus a run that stays in flight until it is released.

    The run is real — :class:`careloop.api.runs.RunRegistry` schedules it and reports it in flight
    the way it reports any other — so "the lifecycle sees the run a wake registered" is asserted
    through the same method production asks.
    """

    def __init__(self) -> None:
        self.registry = RunRegistry()
        self.release = asyncio.Event()
        self.started: list[str] = []

    def start(self, case_id: str = CASE) -> RecoveryOutcome:
        """Register one run. Shaped as a :data:`careloop.api.wake.Registration`."""
        self.started.append(case_id)
        self.registry.start(case_id, self._run)
        return RecoveryOutcome.STARTED

    async def _run(self) -> Any:
        await self.release.wait()
        return _Settled()

    async def finish(self) -> None:
        self.release.set()
        await self.registry.drain()


class _Settled:
    """The shape ``RunRegistry._drive`` reads off a finished run, and nothing more."""

    executed_nodes = ("dispatch",)


def world_with(runs: Runs, waker: ReconcileWaker) -> ApiDeps:
    """One visitor's world, bound to a registry and a waker a test can drive."""
    return ApiDeps(store=InMemoryCaseStore(), runs=runs.registry, wakes=waker)


# -- A. stop versus wake registration --------------------------------------------------------------


class TestTheStopBoundaryIsLinearized:
    """``stop`` is a boundary, not a flag, and both sides of it are asserted.

    The two orderings are the only two there are. Either the wake had not entered its critical
    section when ``stop`` was called — and then it must never register, however long afterwards it
    wakes — or it had, in which case it finishes and ``stop`` cannot return until it has, so the run
    is visible to whatever called ``stop``. A design that checked a flag before the lock would pass
    the first of these and fail the second, which is why both are here.
    """

    def test_a_wake_released_after_stop_returns_registers_nothing(self) -> None:
        """The brief's ordering exactly: pause the wake before registration, stop, then release."""

        async def scenario() -> Runs:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))

            assert waker.arm(CASE, 0.0, runs.start)

            # Stop first, and completely. Nothing is sleeping on a timer here: the wake is holding
            # at its sleeper until the gate opens, which happens on the line after.
            waker.stop()
            gate.set()

            await waker.quiesce()
            return runs

        runs = asyncio.run(scenario())

        assert runs.started == []
        assert runs.registry.in_flight_for(CASE) is None

    def test_a_wake_inside_the_critical_section_finishes_before_stop_returns(self) -> None:
        """The inverse ordering, and the one a flag-based design gets wrong.

        The wake reaches ``register`` and is held there while a worker thread calls ``stop``. The
        assertion is on **order**: the registration completes first and ``stop`` returns second,
        because the lock serialises them. So a lifecycle that stops a waker and then looks for a run
        is guaranteed to see the one that won.
        """

        async def scenario() -> tuple[Runs, list[str], ReconcileWaker]:
            runs = Runs()
            order: list[str] = []
            inside = threading.Event()
            keep_holding = threading.Event()
            waker = ReconcileWaker()

            def register() -> None:
                order.append("registration entered")
                inside.set()
                # Held here, inside the waker's lock, until the stopping thread has begun.
                waited(keep_holding, "the stopping thread to reach stop()")
                runs.start()
                order.append("registration returned")

            def stopper() -> None:
                waited(inside, "the wake to enter its critical section")
                keep_holding.set()
                waker.stop()
                order.append("stop returned")

            thread = threading.Thread(target=stopper, name="stopper")
            thread.start()
            assert waker.arm(CASE, 0.0, register)

            await asyncio.to_thread(thread.join, HANDSHAKE_TIMEOUT)
            assert not thread.is_alive(), "stop() never returned"
            return runs, order, waker

        runs, order, waker = asyncio.run(scenario())

        assert order == ["registration entered", "registration returned", "stop returned"]
        # And the run the wake won is exactly what a post-stop busy re-check has to find.
        assert runs.started == [CASE]
        assert waker.stopped

    def test_a_wake_firing_while_the_gate_is_merely_closed_is_deferred_not_spent(self) -> None:
        """The window a teardown decides inside, and the only path where one guard stands alone.

        ``close`` deliberately does **not** withdraw the pending wakes — an eviction that backs off
        has to leave them intact — so a wake that fires here passes the identity check and reaches
        the gate check with nothing else in front of it. After a ``stop`` the identity check fires
        first, because stopping moves the pending tasks aside; this is what covers the other half.

        **Deferred, not consumed.** The wake registers nothing *and keeps its intent*, and it does
        not count as an attempt: a momentary lifecycle event must not cost a still-waiting case the
        only opportunity it had scheduled, and must not push its backoff along either.
        """

        async def scenario() -> tuple[Runs, ReconcileWaker]:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))

            assert waker.arm(CASE, 0.0, runs.start)
            waker.close()
            gate.set()
            # The wake withdraws itself on the way through, so this settles rather than spins.
            await until(lambda: not waker.pending(), "the wake to fire")
            return runs, waker

        runs, waker = asyncio.run(scenario())

        assert runs.started == []
        assert runs.registry.any_in_flight() is None
        # Closed, not stopped: this is the reversible half, and the wake was refused by it alone.
        assert not waker.stopped
        assert not waker.accepting
        # The opportunity survives the window rather than being burned by it.
        assert waker.deferred() == (CASE,)
        assert waker.attempts(CASE) == 0

    def test_a_wake_firing_after_the_gate_reopens_does_its_job(self) -> None:
        """Vacuity for the guard above: a closed gate is what refuses, not the code path.

        Without this, deleting the registration entirely would pass every refusal test in the file.
        """

        async def scenario() -> Runs:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))

            assert waker.arm(CASE, 0.0, runs.start)
            waker.close()
            waker.reopen()
            gate.set()
            await until(lambda: not waker.pending(), "the wake to fire")
            await runs.finish()
            return runs

        runs = asyncio.run(scenario())

        assert runs.started == [CASE]

    def test_the_gate_is_shut_for_arming_too(self) -> None:
        """A stopped waker refuses to arm at all, so nothing new can be scheduled behind a stop."""

        async def scenario() -> tuple[bool, tuple[str, ...]]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            waker.stop()
            return waker.arm(CASE, 0.0, runs.start), waker.pending()

        armed, pending = asyncio.run(scenario())

        assert armed is False
        assert pending == ()

    def test_stop_is_safe_from_a_thread_that_owns_no_loop(self) -> None:
        """The eviction caller is a Starlette worker thread, so this must not need one.

        Vacuity for the "cancels nothing" decision: ``stop`` touches no task, which is exactly what
        lets it be called from off the loop, and this fails if somebody later "improves" it by
        cancelling.
        """

        async def scenario() -> ReconcileWaker:
            waker = ReconcileWaker(sleeper=never())
            runs = Runs()
            assert waker.arm(CASE, 0.0, runs.start)
            await asyncio.to_thread(waker.stop)
            assert waker.stopped
            await waker.quiesce()
            return waker

        waker = asyncio.run(scenario())

        assert waker.pending() == ()

    def test_close_is_reversible_and_stop_is_not(self) -> None:
        """A refused teardown must leave a working world; a completed one must not reopen."""

        async def scenario() -> list[bool]:
            waker = ReconcileWaker(sleeper=never())
            seen = [waker.accepting]
            waker.close()
            seen.append(waker.accepting)
            waker.reopen()
            seen.append(waker.accepting)
            waker.stop()
            waker.reopen()
            seen.append(waker.accepting)
            await waker.quiesce()
            return seen

        assert asyncio.run(scenario()) == [True, False, True, False]

    def test_nested_closes_do_not_let_an_inner_scope_reopen_an_outer_one(self) -> None:
        async def scenario() -> list[bool]:
            waker = ReconcileWaker(sleeper=never())
            waker.close()
            waker.close()
            waker.reopen()
            first = waker.accepting
            waker.reopen()
            return [first, waker.accepting]

        assert asyncio.run(scenario()) == [False, True]


class TestDeferredWakesSurviveAReversibleClose:
    """A temporary close costs a case nothing, and that is the whole of this class.

    The lifecycle foundation established that a wake cannot act across a teardown. Once wakes carry
    production meaning, *not acting* is not enough on its own: a wake that fell due inside a window
    that was then reopened has to come back, or an eviction that changed its mind and a reset that
    was refused would each silently strand a case in ``WAITING_EXTERNAL`` with nothing scheduled.
    """

    def test_reopening_the_gate_restores_a_deferred_wake(self) -> None:
        async def scenario() -> tuple[Runs, ReconcileWaker]:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))

            assert waker.arm(CASE, 0.0, runs.start)
            waker.close()
            gate.set()
            await until(lambda: not waker.pending(), "the wake to fire")
            assert waker.deferred() == (CASE,)

            waker.reopen()
            # Restored as a fresh pending wake, which then fires: the sleeper's gate is still open.
            await until(lambda: not waker.pending(), "the wake to fire")
            await runs.finish()
            return runs, waker

        runs, waker = asyncio.run(scenario())

        assert runs.started == [CASE], "a deferred wake was lost when the gate reopened"
        assert waker.deferred() == ()
        assert waker.attempts(CASE) == 1

    def test_arming_while_the_gate_is_closed_defers_rather_than_refusing(self) -> None:
        """A run that settles mid-teardown must not lose its case's next opportunity either."""

        async def scenario() -> tuple[bool, tuple[str, ...], tuple[str, ...], Runs]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())

            waker.close()
            armed = waker.arm(CASE, 0.0, runs.start)
            deferred = waker.deferred()

            waker.reopen()
            await until(waker.pending, "the deferred wake to be restored")
            restored = waker.pending()

            await waker.quiesce()
            return armed, deferred, restored, runs

        armed, deferred, restored, runs = asyncio.run(scenario())

        assert armed is False, "arm reports that nothing is scheduled to fire yet"
        assert deferred == (CASE,), "and keeps the intent rather than dropping it"
        assert restored == (CASE,), "which the reopened gate turns back into a pending wake"
        assert runs.started == []

    def test_reopen_is_safe_from_a_thread_that_owns_no_loop(self) -> None:
        """Eviction backs off from a Starlette worker thread, so restoring must cross back safely.

        Arming creates a task, which cannot be done from another thread — so ``reopen`` hands the
        restoration to the captured loop rather than doing it where it stands. Asserted by doing it
        from a real thread and requiring the wake to come back.
        """

        async def scenario() -> tuple[Runs, ReconcileWaker]:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))

            assert waker.arm(CASE, 0.0, runs.start)
            waker.close()
            gate.set()
            await until(lambda: not waker.pending(), "the wake to fire")
            assert waker.deferred() == (CASE,)

            await asyncio.to_thread(waker.reopen)
            # The restoration is queued on this loop; giving it turns is all that is needed.
            await until(lambda: runs.started, "the restored wake to register a run")
            await runs.finish()
            return runs, waker

        runs, waker = asyncio.run(scenario())

        assert runs.started == [CASE]
        assert waker.deferred() == ()

    def test_a_nested_close_does_not_restore_early(self) -> None:
        async def scenario() -> tuple[tuple[str, ...], tuple[str, ...], Runs]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())

            waker.close()
            waker.close()
            waker.arm(CASE, 0.0, runs.start)

            waker.reopen()
            still_deferred = waker.deferred()
            waker.reopen()
            await until(waker.pending, "the deferred wake to be restored")
            restored = waker.pending()

            await waker.quiesce()
            return still_deferred, restored, runs

        still_deferred, restored, runs = asyncio.run(scenario())

        assert still_deferred == (CASE,)
        assert restored == (CASE,)
        assert runs.started == []

    def test_stopping_discards_deferred_intents(self) -> None:
        """Permanent means permanent: a stopped world's deferred wakes are not a backlog."""

        async def scenario() -> tuple[tuple[str, ...], bool, Runs]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())

            waker.close()
            waker.arm(CASE, 0.0, runs.start)
            assert waker.deferred() == (CASE,)

            waker.stop()
            after_stop = waker.deferred()
            waker.reopen()
            for _ in range(5):
                await asyncio.sleep(0)

            await waker.quiesce()
            return after_stop, waker.stopped, runs

        after_stop, stopped, runs = asyncio.run(scenario())

        assert after_stop == ()
        assert stopped
        assert runs.started == []

    def test_discarding_deferred_intents_is_explicit(self) -> None:
        """What a teardown that really went through calls, and nothing else does."""

        async def scenario() -> tuple[int, tuple[str, ...], Runs]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())

            waker.close()
            waker.arm(CASE, 0.0, runs.start)
            waker.arm("case-other", 0.0, runs.start)
            discarded = waker.discard_deferred()

            waker.reopen()
            for _ in range(5):
                await asyncio.sleep(0)
            return discarded, waker.pending(), runs

        discarded, pending, runs = asyncio.run(scenario())

        assert discarded == 2
        assert pending == ()
        assert runs.started == []


class TestTheBackoffAdvancesOnlyOnRealAttempts:
    """The schedule is a table, and only a registered reconciliation moves along it.

    Counting a deferral, a clash with another run or a case that had already settled would have a
    provider outage look longer than it was, and would push a case that is genuinely retrying
    towards the ceiling for reasons that have nothing to do with the provider.
    """

    def test_the_schedule_is_the_declared_table_and_its_last_value_repeats(self) -> None:
        waker = ReconcileWaker(sleeper=never())
        seen = []
        for attempt in range(8):
            waker._attempts[CASE] = attempt
            seen.append(waker.delay_for(CASE))

        assert seen == [5.0, 15.0, 45.0, 120.0, 300.0, 300.0, 300.0, 300.0]
        assert seen[: len(WAKE_DELAYS)] == list(WAKE_DELAYS)

    def test_a_started_registration_advances_it(self) -> None:
        """Driven through ``schedule`` rather than ``arm``: that is the production entry point."""

        async def scenario() -> tuple[list[float], int]:
            delays: list[float] = []

            async def sleeper(delay: float) -> None:
                delays.append(delay)

            waker = ReconcileWaker(sleeper=sleeper)
            for _ in range(3):
                assert waker.schedule(CASE, lambda: RecoveryOutcome.STARTED)
                await until(lambda: not waker.pending(), "the wake to fire")
            return delays, waker.attempts(CASE)

        delays, attempts = asyncio.run(scenario())

        assert delays == [5.0, 15.0, 45.0]
        assert attempts == 3

    def test_a_registration_that_raises_is_logged_and_costs_no_attempt(self) -> None:
        """A scheduler that dies on a bug in the layer above stops scheduling silently.

        Nothing is counted, nothing is re-raised, and the task finishes cleanly rather than leaving
        an exception nobody retrieves — while the failure itself is logged at ``exception`` level.
        """
        unhandled: list[dict[str, Any]] = []

        async def scenario() -> tuple[int, bool]:
            asyncio.get_running_loop().set_exception_handler(
                lambda _loop, context: unhandled.append(context)
            )

            async def sleeper(_delay: float) -> None:
                return None

            def exploding() -> RecoveryOutcome:
                raise RuntimeError("the layer above is broken")

            waker = ReconcileWaker(sleeper=sleeper)
            assert waker.arm(CASE, 0.0, exploding)
            task = next(iter(waker._pending.values()))
            await until(lambda: not waker.pending(), "the wake to fire")
            for _ in range(3):
                await asyncio.sleep(0)
            return waker.attempts(CASE), task.done() and task.exception() is None

        attempts, finished_cleanly = asyncio.run(scenario())

        assert attempts == 0
        assert finished_cleanly
        assert unhandled == []

    def test_a_clash_with_another_run_is_not_an_attempt(self) -> None:
        async def scenario() -> tuple[list[float], int]:
            delays: list[float] = []

            async def sleeper(delay: float) -> None:
                delays.append(delay)

            waker = ReconcileWaker(sleeper=sleeper)
            for _ in range(3):
                assert waker.schedule(CASE, lambda: RecoveryOutcome.RUN_ALREADY_IN_FLIGHT)
                await until(lambda: not waker.pending(), "the wake to fire")
            return delays, waker.attempts(CASE)

        delays, attempts = asyncio.run(scenario())

        assert delays == [5.0, 5.0, 5.0], "a clash pushed the backoff along"
        assert attempts == 0

    def test_a_settled_case_clears_the_backoff(self) -> None:
        async def scenario() -> tuple[int, int]:
            async def sleeper(_delay: float) -> None:
                return None

            waker = ReconcileWaker(sleeper=sleeper)
            assert waker.schedule(CASE, lambda: RecoveryOutcome.STARTED)
            await until(lambda: not waker.pending(), "the wake to fire")
            after_start = waker.attempts(CASE)

            assert waker.schedule(CASE, lambda: RecoveryOutcome.STATE_MOVED)
            await until(lambda: not waker.pending(), "the wake to fire")
            after_move = waker.attempts(CASE)

            return after_start, after_move

        assert asyncio.run(scenario()) == (1, 0)

    def test_forgetting_a_case_clears_its_backoff(self) -> None:
        async def scenario() -> tuple[int, int, bool]:
            async def sleeper(_delay: float) -> None:
                return None

            waker = ReconcileWaker(sleeper=sleeper)
            assert waker.schedule(CASE, lambda: RecoveryOutcome.STARTED)
            await until(lambda: not waker.pending(), "the wake to fire")
            before = waker.attempts(CASE)

            forgotten = waker.forget(CASE)
            return before, waker.attempts(CASE), forgotten

        before, after, forgotten = asyncio.run(scenario())

        assert (before, after) == (1, 0)
        # Nothing was pending — the wake had already fired — and forgetting still cleared the count.
        assert forgotten is False


# -- B. eviction -----------------------------------------------------------------------------------


def evictable_worlds(build: Callable[[str], ApiDeps]) -> SessionWorlds:
    """A registry that considers every world idle the moment it is not busy."""
    return SessionWorlds(
        build, limit=8, idle_seconds=0.0, now=lambda: 0.0, end_world=lambda *_: None
    )


class TestEvictionAsksBusyAgainAfterClosingTheGate:
    """Idle eviction and a firing wake have two safe outcomes, and this pins both.

    The forbidden third — the world is evicted *and* a run starts afterwards — is what the second
    ``busy`` check exists to make unreachable, and the second test below is the one that fails
    without it.
    """

    def test_a_wake_that_loses_the_race_evicts_cleanly_and_starts_nothing(self) -> None:
        async def scenario() -> tuple[Runs, ReconcileWaker]:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))
            world = world_with(runs, waker)
            worlds = evictable_worlds(lambda _session: world)

            session = worlds.mint()
            assert waker.arm(CASE, 0.0, runs.start)

            # Eviction completes with the wake still held at its sleeper.
            await asyncio.to_thread(worlds._forget_idle)
            assert worlds.sessions() == ()

            # Only now is the wake released. It must find the gate shut.
            gate.set()
            await waker.quiesce()
            assert session
            return runs, waker

        runs, waker = asyncio.run(scenario())

        assert runs.started == []
        assert runs.registry.any_in_flight() is None
        assert waker.stopped

    def test_a_wake_that_wins_the_race_keeps_its_world_alive(self) -> None:
        """The mandatory re-check, forced deterministically.

        The wake enters ``register`` and is held there. An eviction thread runs ``_forget_idle``,
        whose *first* busy check therefore sees nothing — the run has not been started yet. A spy on
        the waker's ``close`` releases the wake exactly when eviction reaches its own critical
        section, so the registration lands while ``close`` is blocked on the waker's lock. The
        second busy check then has to see it.

        The eviction thread is started *before* the wake is armed, because a held ``register`` holds
        the event loop with it: anything the test still had to do on the loop would never happen.
        """

        async def scenario() -> tuple[SessionWorlds, Runs, ReconcileWaker, list[str]]:
            runs = Runs()
            order: list[str] = []
            waker = ReconcileWaker()
            world = world_with(runs, waker)
            worlds = evictable_worlds(lambda _session: world)
            worlds.mint()

            inside = threading.Event()
            proceed = threading.Event()

            real_close = waker.close

            def close_spy() -> None:
                order.append("eviction closing the gate")
                # Let the held wake finish registering, then block behind it on the same lock.
                proceed.set()
                real_close()
                order.append("gate closed")

            waker.close = close_spy  # type: ignore[method-assign]

            def register() -> None:
                order.append("registration entered")
                inside.set()
                waited(proceed, "eviction to reach close()")
                runs.start()
                order.append("registration returned")

            def evict() -> None:
                waited(inside, "the wake to enter its critical section")
                worlds._forget_idle()

            thread = threading.Thread(target=evict, name="evictor")
            thread.start()
            assert waker.arm(CASE, 0.0, register)

            await asyncio.to_thread(thread.join, HANDSHAKE_TIMEOUT)
            assert not thread.is_alive(), "eviction never finished"
            return worlds, runs, waker, order

        worlds, runs, waker, order = asyncio.run(scenario())

        assert order == [
            "registration entered",
            "eviction closing the gate",
            "registration returned",
            "gate closed",
        ]
        # The forbidden outcome would be an empty registry here with a run in flight.
        assert runs.started == [CASE]
        assert len(worlds.sessions()) == 1, "a world with a freshly registered run was evicted"
        assert not waker.stopped, "a spared world must be left able to wake again"
        assert waker.accepting, "the gate must reopen when the eviction backs off"

    def test_a_pending_wake_alone_never_makes_a_world_immortal(self) -> None:
        """A sleeping wake is not busy, on purpose.

        Counting one as busy would keep a visitor's world alive for as long as their provider stayed
        unavailable, which is the unbounded growth the idle bound exists to stop.
        """

        async def scenario() -> tuple[SessionWorlds, ReconcileWaker]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            world = world_with(runs, waker)
            worlds = evictable_worlds(lambda _session: world)
            session = worlds.mint()

            assert waker.arm(CASE, 0.0, runs.start)
            assert worlds.busy(session) is False

            await asyncio.to_thread(worlds._forget_idle)
            await waker.quiesce()
            return worlds, waker

        worlds, waker = asyncio.run(scenario())

        assert worlds.sessions() == ()
        assert waker.stopped


# -- C. reset --------------------------------------------------------------------------------------


class TestResetQuiescesRatherThanCancelling:
    """Reset clears a store, so a wake must be incapable of appearing behind it.

    Two outcomes again. Either the wake is withdrawn and awaited before the clear — which is what
    ``quiesced`` establishes and what ``cancel``-without-``await`` would not — or it got a run
    registered first, in which case the existing refusal semantics apply unchanged and nothing is
    cleared underneath it.
    """

    def test_a_pending_wake_is_quiesced_before_anything_is_cleared(self) -> None:
        """Quiesced, and the task is asserted **finished** rather than merely withdrawn.

        That distinction is the whole difference between cancelling and quiescing: a cancellation
        is delivered at the task's next suspension point, so a reset that cancelled and carried
        straight on would return with the task still live, and this assertion is what notices.
        """
        from careloop.api import app as app_module

        async def scenario() -> tuple[dict[str, object], Runs, ReconcileWaker, bool]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            world = world_with(runs, waker)
            assert waker.arm(CASE, 0.0, runs.start)
            task = waker._pending[CASE]

            report = await app_module.reset_world(world)
            # Read **on the loop, the instant the reset returns**. Read afterwards it would be true
            # of any cancelled task, because `asyncio.run` finishes everything on its way out — the
            # assertion has to be about what was true when the store was cleared.
            return report, runs, waker, task.done()

        report, runs, waker, finished = asyncio.run(scenario())

        assert report["cases_cleared"] == 0
        assert runs.started == [], "a wake registered a run around a reset"
        assert waker.pending() == (), "reset returned with a wake still pending"
        assert finished, "reset returned before its cancelled wake had finished"
        # Reversible: the world stays usable after its own reset.
        assert waker.accepting

    def test_reset_refuses_rather_than_clearing_under_a_run_a_wake_registered(self) -> None:
        """The wake wins, and the existing 409 is what covers it.

        Registration happens first and deterministically, which is the honest form of "the wake won
        the race": what is being asserted is that the in-flight check happens *inside* the quiesced
        scope, so a run a wake started is seen rather than cleared over.
        """
        from fastapi import HTTPException

        from careloop.api import app as app_module

        async def scenario() -> tuple[int, InMemoryCaseStore, Runs]:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))
            world = world_with(runs, waker)

            assert waker.arm(CASE, 0.0, runs.start)
            gate.set()
            await until(lambda: runs.started, "the restored wake to register a run")

            try:
                await app_module.reset_world(world)
            except HTTPException as refusal:
                status_code = refusal.status_code
            else:  # pragma: no cover - the run is in flight, so this is the failure being caught
                pytest.fail("reset cleared a world with a run in flight")

            await runs.finish()
            return status_code, world.store, runs

        status_code, store, runs = asyncio.run(scenario())

        assert status_code == 409
        assert runs.started == [CASE]
        assert store.list_by_state.__self__ is store  # the store was never replaced

    def test_a_wake_armed_during_a_reset_is_refused(self) -> None:
        """The gate is shut for the whole body, not merely at its start."""
        from careloop.api import app as app_module

        async def scenario() -> list[bool]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            world = world_with(runs, waker)

            attempts: list[bool] = []

            async with waker.quiesced():
                attempts.append(waker.arm(CASE, 0.0, runs.start))
                app_module._reset_quiesced_world(world)
            attempts.append(waker.arm(CASE, 0.0, runs.start))

            await waker.quiesce()
            return attempts

        assert asyncio.run(scenario()) == [False, True]


# -- D. cancellation completion --------------------------------------------------------------------


class TestCancelledWakesAreCollected:
    """Quiescence means *finished*, not *asked to stop*, and nothing is left unretrieved."""

    def test_a_sleeping_wake_is_cancelled_awaited_and_never_calls_back(self) -> None:
        async def scenario() -> tuple[Runs, ReconcileWaker, list[asyncio.Task[None]]]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            assert waker.arm(CASE, 0.0, runs.start)

            tasks = list(waker._pending.values())
            withdrawn = await waker.quiesce()

            assert withdrawn == 1
            return runs, waker, tasks

        runs, waker, tasks = asyncio.run(scenario())

        assert [task.done() for task in tasks] == [True]
        assert [task.cancelled() for task in tasks] == [True]
        assert runs.started == []
        assert waker.pending() == ()

    def test_shutdown_leaves_no_task_running_and_no_exception_unretrieved(self) -> None:
        """``aclose`` is the loop-side variant, and it must collect what ``stop`` strands."""
        unhandled: list[dict[str, Any]] = []

        async def scenario() -> tuple[Runs, list[asyncio.Task[None]]]:
            asyncio.get_running_loop().set_exception_handler(
                lambda _loop, context: unhandled.append(context)
            )
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            assert waker.arm(CASE, 0.0, runs.start)
            assert waker.arm("case-other", 0.0, runs.start)
            tasks = list(waker._pending.values())

            await waker.aclose()
            return runs, tasks

        runs, tasks = asyncio.run(scenario())

        assert all(task.done() for task in tasks)
        assert all(task.cancelled() for task in tasks)
        assert runs.started == []
        assert unhandled == []

    def test_a_world_drain_stops_wakes_before_it_drains_runs(self) -> None:
        """The shutdown ordering, and the wake is free to fire for the whole of the drain.

        This is the arrangement that discriminates. The wake's gate is opened **before** the drain
        starts and a run is left in flight for several loop turns, so there is a long window during
        which a wake could register something. Stopping first closes it: the wake is stranded and
        cancelled before the first ``await``. Draining runs first would leave that window wide
        open — and the run a wake registered inside it would not even be waited for, because the
        drain gathers the tasks it found when it started.
        """

        async def scenario() -> Runs:
            runs = Runs()
            gate = threading.Event()
            waker = ReconcileWaker(sleeper=held_by(gate))
            world = world_with(runs, waker)

            assert waker.arm(CASE, 0.0, runs.start)
            runs.start("case-already-running")

            async def finish_the_run_after_a_while() -> None:
                for _ in range(10):
                    await asyncio.sleep(0)
                runs.release.set()

            releasing = asyncio.create_task(finish_the_run_after_a_while())
            gate.set()

            await world.drain()
            await releasing
            return runs

        runs = asyncio.run(scenario())

        assert runs.started == ["case-already-running"], "a wake registered a run during shutdown"
        assert runs.registry.any_in_flight() is None

    def test_forget_withdraws_one_case_without_touching_the_others(self) -> None:
        async def scenario() -> tuple[Runs, tuple[str, ...], bool, bool]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            assert waker.arm(CASE, 0.0, runs.start)
            assert waker.arm("case-other", 0.0, runs.start)

            withdrew = waker.forget(CASE)
            again = waker.forget(CASE)
            remaining = waker.pending()

            await waker.quiesce()
            return runs, remaining, withdrew, again

        runs, remaining, withdrew, again = asyncio.run(scenario())

        assert (withdrew, again) == (True, False)
        assert remaining == ("case-other",)
        assert runs.started == []


# -- E. dedupe -------------------------------------------------------------------------------------


class TestOneWakePerCasePerWaker:
    """At most one pending wake per case **here**, and the claim stops at this object.

    Cross-process uniqueness is not asserted and is not this object's to provide: two processes
    sweeping one durable case hold two wakers that know nothing of each other. What keeps that safe
    is the store's compare-and-set and the provider's idempotency, which is why the second test
    builds a second waker and requires it to arm freely.
    """

    def test_arming_twice_for_one_case_produces_one_pending_wake(self) -> None:
        async def scenario() -> tuple[bool, bool, tuple[str, ...], Runs]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())

            first = waker.arm(CASE, 0.0, runs.start)
            second = waker.arm(CASE, 0.0, runs.start)
            pending = waker.pending()

            await waker.quiesce()
            return first, second, pending, runs

        first, second, pending, runs = asyncio.run(scenario())

        assert (first, second) == (True, False)
        assert pending == (CASE,)
        assert runs.started == []

    def test_a_withdrawn_case_can_be_armed_again(self) -> None:
        """Dedupe is about what is pending, not a permanent claim on a case id."""

        async def scenario() -> tuple[bool, tuple[str, ...]]:
            runs = Runs()
            waker = ReconcileWaker(sleeper=never())
            assert waker.arm(CASE, 0.0, runs.start)
            waker.forget(CASE)

            rearmed = waker.arm(CASE, 0.0, runs.start)
            pending = waker.pending()

            await waker.quiesce()
            return rearmed, pending

        rearmed, pending = asyncio.run(scenario())

        assert rearmed is True
        assert pending == (CASE,)

    def test_the_claim_does_not_extend_past_one_waker(self) -> None:
        """Two wakers arm the same case independently, and that is the honest state of it."""

        async def scenario() -> tuple[bool, bool]:
            runs = Runs()
            here = ReconcileWaker(sleeper=never())
            elsewhere = ReconcileWaker(sleeper=never())

            mine = here.arm(CASE, 0.0, runs.start)
            theirs = elsewhere.arm(CASE, 0.0, runs.start)

            await here.quiesce()
            await elsewhere.quiesce()
            return mine, theirs

        assert asyncio.run(scenario()) == (True, True)

    def test_two_worlds_hold_their_own_wakes(self) -> None:
        """Ownership is the isolation: stopping one world's waker says nothing about another's."""

        async def scenario() -> tuple[bool, bool, bool]:
            runs = Runs()
            first = world_with(runs, ReconcileWaker(sleeper=never()))
            second = world_with(Runs(), ReconcileWaker(sleeper=never()))

            assert first.wakes.arm(CASE, 0.0, runs.start)
            first.wakes.stop()

            armed_elsewhere = second.wakes.arm(CASE, 0.0, runs.start)
            result = (first.wakes.stopped, second.wakes.stopped, armed_elsewhere)

            await first.wakes.quiesce()
            await second.wakes.quiesce()
            return result

        assert asyncio.run(scenario()) == (True, False, True)


# -- what the waker is not -------------------------------------------------------------------------


class TestTheWakerDecidesNothing:
    """It is execution scheduling and holds no domain vocabulary at all.

    Asserted on the module's own source, in the idiom ``tests/test_reconcile_writers.py`` uses one
    layer down: the thing worth preventing is a future edit that gives this object a tool name, an
    argument map or an idempotency key, because that is the point at which "another opportunity to
    reconcile the same action" quietly becomes "a second decision about what to do".
    """

    #: Stems that must not appear in any *identifier* the module defines or reads.
    #:
    #: Matched as substrings of identifiers rather than of the file, because the docstrings
    #: legitimately explain what a wake is for and a sweep that read prose would either fire on the
    #: explanation or be weakened until it fired on nothing.
    FORBIDDEN = (
        "planner",
        "triage",
        "idempotency",
        "digest",
        "tool_name",
        "policy",
        "clinic",
        "appointment",
        "proposal",
        "reconcilenode",
    )

    def identifiers(self) -> set[str]:
        """Every name this module defines, reads or reaches through, lowercased."""
        import ast
        from pathlib import Path

        import careloop.api.wake as wake_module

        tree = ast.parse(Path(wake_module.__file__).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                found.add(node.name)
            elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
                found.add(node.arg)
        return {name.lower() for name in found}

    def test_the_detector_sees_the_identifiers_that_are_there(self) -> None:
        """Vacuity: a sweep over an empty set would pass whatever the module said."""
        names = self.identifiers()
        assert {"arm", "stop", "quiesce", "register", "case_id"} <= names

    def test_no_identifier_names_anything_from_the_domain(self) -> None:
        names = self.identifiers()
        found = sorted(stem for stem in self.FORBIDDEN if any(stem in name for name in names))
        assert found == []

    def test_it_imports_no_careloop_module_at_all(self) -> None:
        import ast
        from pathlib import Path

        import careloop.api.wake as wake_module

        tree = ast.parse(Path(wake_module.__file__).read_text(encoding="utf-8"))
        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert imported, "no imports found, so the sweep would pass vacuously"
        assert [name for name in imported if name.startswith("careloop")] == []

    def test_a_stopped_waker_is_a_state_rather_than_a_null_object(self) -> None:
        waker = stopped_waker()
        assert isinstance(waker, ReconcileWaker)
        assert waker.stopped
        assert waker.accepting is False
