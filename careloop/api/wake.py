"""Pending reconciliation wake-ups, and the lifecycle boundary that makes them safe to cancel.

A case parked in ``WAITING_EXTERNAL`` holds a durably prepared action nobody answered about. It
converges on the *next run of the case* — :class:`careloop.graph.nodes.ReconcileNode` re-claims the
frozen identity and asks again — and until this module existed the only thing that started such a
run was the startup sweep in :meth:`careloop.api.deps.ApiDeps.resume_unsettled`. A process that
stayed up therefore left the case parked until it was restarted.

**This decides only *when* another run happens.** Not what is attempted, not whether it is
authorised, not which arguments or which key, not whether the effect succeeded, and not whether the
case is settled. All of those are the saga's, and the thing scheduled here is an ordinary graph run
over durable state that already exists. The two triggers are complementary rather than alternative:
the startup sweep covers a process that died, and this covers a process that did not.

**What this is allowed to know.** A case id and an opaque thunk. It holds no tool name, no
arguments, no idempotency key, no policy verdict and no graph node; it invokes no model and reaches
no provider. It is the same kind of object :class:`careloop.api.runs.RunRegistry` is — execution
context, never a business fact — and invariant 8 is untouched by it, which is why nothing here is
persisted and a restart simply starts the schedule again.

**The hard part is not the timer; it is stopping one.** A visitor's world can be destroyed by three
independent things — a demo reset clearing the store, an idle eviction dropping the world and
ending its clinic, and process shutdown — and each of them runs concurrently with a wake that may
be about to start a run in that very world. A wake that registers a run *after* its world has been
cleared would drive the graph against a store somebody just emptied; one that registers after
eviction would reach a clinic session Demo World has already been told to forget. Neither is
prevented by a flag checked somewhere before scheduling: between the check and the scheduling is
exactly where the race lives.

So the guarantee this module provides is a **linearization point** rather than a check:

    Once :meth:`ReconcileWaker.stop` returns, no wake held by this waker can register another run.

It is established by doing the check *and* the registration inside one critical section, so a wake
is either wholly before the boundary — in which case its run is registered and visible to whatever
is about to tear the world down, which must therefore look again — or wholly after it, in which
case it does nothing at all. There is no third possibility and no window between the two.

**Stopping is permanent; closing is not, and the difference is where liveness lives.** An eviction
that finds a run in flight backs off, and a refused reset leaves the world exactly as it was — so
both close the gate *reversibly*. A wake that becomes due inside such a window must therefore be
**deferred rather than spent**: it keeps its intent, does not count as a reconciliation attempt, and
becomes eligible again the moment the gate reopens. Consuming it instead would mean a temporary
lifecycle event silently costing a still-waiting case its only scheduled opportunity, which is the
defect this module exists to prevent wearing different clothes.

**One lock, and it is never held across an ``await``.** ``threading`` rather than ``asyncio``
because two of the callers are not on the event loop: a served CareLoop evicts idle worlds from
Starlette's worker pool, exactly as :class:`careloop.state.sqlite_store.SqliteCaseStore` is reached
from it. That is the reasoning :func:`careloop.api.sessions.serialised` already records, and the
same discipline applies — the lock covers dictionary operations and the registration callback, all
of them synchronous, and every cancellation and every wait happens outside it. Restoring a deferred
wake from a worker thread goes through ``call_soon_threadsafe`` rather than touching a task, for the
same reason :meth:`stop` cancels nothing.

**The claim is per waker, and the docstrings say so wherever it is made.** At most one pending wake
per case *in this waker*. Two processes sweeping one durable case are not covered here and must not
be described as if they were: what makes that safe is the case version's compare-and-set in the
store and the provider's own idempotency, which is where those guarantees have always lived.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Final

logger = logging.getLogger(__name__)

#: How long each successive reconciliation opportunity waits, in seconds. The last value repeats.
#:
#: **Process-local, deterministic, and deliberately not persisted.** No jitter and no random source,
#: because what makes two wakers safe against each other is the store's compare-and-set and the
#: provider's idempotency rather than their delays being spread out — and a schedule a test can
#: predict is worth more here than one that looks sophisticated. A restart starts again at the first
#: delay, which is correct: a new process has new information about the world, and the startup sweep
#: has just asked for itself.
#:
#: **Unbounded, with the interval capped.** There is no maximum attempt count and no escalation to
#: ``AWAITING_HUMAN`` because time has passed: a timeout is not evidence about the external world,
#: and a case that stops being retried because a budget ran out would be a case CareLoop had quietly
#: given up on while its state still said it was waiting. Five minutes is where the interval settles
#: so a provider that is down for a day costs a bounded number of attempts.
WAKE_DELAYS: Final[tuple[float, ...]] = (5.0, 15.0, 45.0, 120.0, 300.0)

#: What a deferred wake waits before trying again once the gate reopens.
#:
#: Zero, because the wait was already served: the wake became *due* inside the closed window, and
#: making it serve its delay a second time would turn a lifecycle event into a scheduling penalty.
RESUME_IMMEDIATELY: Final[float] = 0.0


class RecoveryOutcome(StrEnum):
    """What came of one attempt to give a case another reconciliation opportunity.

    **A return value rather than an exception or a log line**, because liveness decisions are made
    from it: whether this counted as an attempt, whether the backoff advances, whether the intent is
    kept. Reading any of that out of a raised exception would make the scheduler's behaviour depend
    on which failure happened to be raised where, and reading it out of a log would make it depend
    on nothing at all.

    The waker itself produces exactly one member — :attr:`TEMPORARILY_CLOSED` — and *reads* three:
    :attr:`STARTED` advances the backoff, and the two that mean the case has moved on clear it. The
    rest are the caller's to produce and the waker deliberately draws no conclusion from them.
    """

    STARTED = "STARTED"
    """A recovery run was registered. This is the only member that counts as an attempt.

    No wake is armed on the strength of it: the run that was just started will inspect the case's
    durable state when it finishes and arm one only if it is still waiting. Arming here as well
    would be a second scheduler for one case.
    """

    CASE_GONE = "CASE_GONE"
    """The store no longer holds the case. Nothing to reconcile and nothing to remember."""

    STATE_MOVED = "STATE_MOVED"
    """The case is no longer in a state a reconciliation means anything for.

    Something else settled it — another run, a caregiver, a sweep in another process. The honest
    response is to forget, not to fail: the wake was a stale opportunity rather than a mistake.
    """

    RUN_ALREADY_IN_FLIGHT = "RUN_ALREADY_IN_FLIGHT"
    """A run for this case is already going, so this one is not started.

    **Not an attempt**, and nothing is re-armed here. The run that is already going owns what
    happens next: when it finishes it inspects the case and arms a wake if it is still waiting.
    Counting this would advance a backoff on the strength of somebody else's work.
    """

    TEMPORARILY_CLOSED = "TEMPORARILY_CLOSED"
    """The world's registration gate was closed, so the wake was deferred rather than spent.

    Produced by the waker itself. The intent is kept and becomes eligible again when the gate
    reopens; the backoff does not advance, because nothing was attempted.
    """

    RECOVERY_CONTEXT_UNAVAILABLE = "RECOVERY_CONTEXT_UNAVAILABLE"
    """The case cannot be carried forward under any event this process holds.

    The startup sweep's existing behaviour, unchanged and deliberately so: it logs and moves on
    rather than inventing a delivery, and neither does this. Nothing is re-armed, because the next
    wake would find exactly the same thing.
    """


#: How a wake waits before it registers anything.
#:
#: Injected so a test can drive the lifecycle without spending wall-clock time on it, and so a race
#: can be reproduced deterministically rather than by hoping a sleep is long enough.
Sleeper = Callable[[float], Awaitable[None]]

#: What a wake does when it fires: give one case another reconciliation opportunity.
#:
#: Opaque on purpose. A wake that could *build* the work it schedules would be a second place the
#: identity of a recovered action could be decided, and this repository has closed three defects of
#: that exact shape. The caller supplies a thunk that already knows which case it is about, and the
#: thunk reports back with a :class:`RecoveryOutcome`.
Registration = Callable[[], RecoveryOutcome]


class ReconcileWaker:
    """Wakes pending in one world, with a stop that cannot be raced.

    One per :class:`careloop.api.deps.ApiDeps`, exactly as one
    :class:`careloop.api.runs.RunRegistry` is, so an isolated demo's per-visitor worlds get their
    own without anything having to look one up. Ownership *is* the isolation: a reset, an eviction
    or a shutdown reaches the wakes belonging to the world it is tearing down and has no way to
    express reaching another's.

    **Which methods may be called from which thread**, because the answer is not the same for all
    of them and getting it wrong is how a cancellation from the wrong thread corrupts a loop:

    ``stop``, ``close``, ``reopen``, ``pending``, ``deferred``, ``attempts``, ``stopped``
        Any thread. ``reopen`` may have work to hand back to the event loop and does it through
        ``call_soon_threadsafe``; the rest take the lock and touch plain structures.
        :meth:`careloop.api.sessions.SessionWorlds._forget_idle` runs in a worker thread and needs
        exactly these.

    ``arm``, ``schedule``, ``forget``, ``cancel_all``, ``quiesce``, ``quiesced``,
    ``discard_deferred``, ``aclose``
        The event loop only. They create or cancel :class:`asyncio.Task` objects, which is not safe
        from another thread.
    """

    def __init__(self, *, sleeper: Sleeper = asyncio.sleep) -> None:
        self._sleeper = sleeper
        # Re-entrant so a registration callback that reaches back in — to `forget` the case it has
        # just started a run for, say — does not deadlock against the lock its own wake is holding.
        self._lock = threading.RLock()
        self._pending: dict[str, asyncio.Task[None]] = {}
        #: What each pending or deferred wake would do, kept so a deferred one can be restored.
        #:
        #: A thunk and a case id, and nothing else. Explicitly **not** the arguments, the key, the
        #: tool or any provider data: the durable action lives in the store, and a scheduler holding
        #: a copy of it would be a second place it could be decided.
        self._intent: dict[str, Registration] = {}
        self._deferred: dict[str, Registration] = {}
        self._stranded: set[asyncio.Task[None]] = set()
        self._attempts: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = 0
        self._stopped = False

    # -- state ---------------------------------------------------------------

    @property
    def stopped(self) -> bool:
        """Whether this waker has been permanently stopped. Safe from any thread."""
        with self._lock:
            return self._stopped

    @property
    def accepting(self) -> bool:
        """Whether a wake armed right now could still register a run. Safe from any thread.

        A read of the gate, and **not** something to check before scheduling: by the time a caller
        acted on it another thread may have closed the gate. The only safe use is reporting. What
        makes arming safe is that :meth:`arm` re-reads this under the same lock that mutates it.
        """
        with self._lock:
            return not self._stopped and self._closed == 0

    def pending(self) -> tuple[str, ...]:
        """The cases with a wake still to fire here, in the order they were armed.

        Safe from any thread. **Not** a durable fact and never consulted as one: a wake is an
        opportunity to re-run a case, and whether the case needs one is what its stored state says.
        """
        with self._lock:
            return tuple(self._pending)

    def deferred(self) -> tuple[str, ...]:
        """The cases whose wake is waiting for the gate to reopen. Safe from any thread."""
        with self._lock:
            return tuple(self._deferred)

    def attempts(self, case_id: str) -> int:
        """How many reconciliation runs this waker has registered for ``case_id``.

        Operational and process-local. It advances only on :attr:`RecoveryOutcome.STARTED`, so a
        deferred wake, a wake that found another run going and a wake that found the case settled
        all leave it alone — none of them attempted anything.
        """
        with self._lock:
            return self._attempts.get(case_id, 0)

    def delay_for(self, case_id: str) -> float:
        """How long this case's next wake waits, from :data:`WAKE_DELAYS`. Safe from any thread."""
        with self._lock:
            return WAKE_DELAYS[min(self._attempts.get(case_id, 0), len(WAKE_DELAYS) - 1)]

    # -- arming --------------------------------------------------------------

    def schedule(self, case_id: str, register: Registration) -> bool:
        """Arm this case's next wake at its own place in the backoff. Event loop only.

        The production entry point. :meth:`arm` is the primitive underneath and takes the delay
        explicitly, which is what a test drives; going through here is what makes the schedule come
        from one table rather than from whichever caller last thought about it.
        """
        return self.arm(case_id, self.delay_for(case_id), register)

    def arm(self, case_id: str, delay: float, register: Registration) -> bool:
        """Schedule one wake for ``case_id`` after ``delay`` seconds. Event loop only.

        Returns whether a wake is now *scheduled to fire*. It is ``False`` — with nothing raised —
        in three cases, and they mean different things:

        * this waker is **stopped**, so the world is gone and nothing is kept;
        * the gate is **closed**, so the intent is **deferred** and restored when it reopens;
        * a wake for this case is **already pending**, so there is nothing to add.

        **The dedupe claim is exactly this waker's**, and it is structural rather than checked: the
        pending wakes are a mapping keyed by case id, so a second wake for a case that has one is
        not something the code declines to create, it is something the structure cannot hold. What
        that buys is that a world cannot accumulate wakes for one case however many runs settle on
        it. What it does **not** buy, and must never be described as buying, is uniqueness across
        processes: two processes sweeping one durable case each hold their own waker and know
        nothing of each other. The store's compare-and-set rejects the losing writer and the
        provider's idempotency keeps one effect one effect; neither of those is this object's doing.
        """
        with self._lock:
            # Captured **before** the gate is consulted, and that is not tidiness. A wake deferred
            # here is restored later by `reopen`, which may be called from a worker thread and has
            # to hand the arming back to a loop — so the one path that produces a deferral must be
            # the path that records which loop that is. Capturing it only on the way to a successful
            # arm left a waker whose *first* arm was refused with nothing to restore onto, and the
            # deferred wake sat there for good.
            self._loop = asyncio.get_running_loop()

            if self._stopped:
                return False
            if case_id in self._pending:
                return False
            if self._closed:
                # Deferred rather than refused. A reset that is about to be refused, or an eviction
                # that is about to back off, must not cost a still-waiting case its opportunity.
                self._deferred[case_id] = register
                logger.debug("wake for case %s deferred: the world's gate is closed", case_id)
                return False

            self._deferred.pop(case_id, None)
            task = asyncio.create_task(self._wake(case_id, delay, register), name=f"wake-{case_id}")
            self._pending[case_id] = task
            self._intent[case_id] = register
            logger.debug(
                "reconciliation wake armed for case %s in %.1fs (attempt %d)",
                case_id,
                delay,
                self._attempts.get(case_id, 0) + 1,
            )
            return True

    async def _wake(self, case_id: str, delay: float, register: Registration) -> None:
        """Wait, then register the run — the second half inside the critical section.

        **This is the whole safety argument, so it is worth reading as one statement.** The gate is
        checked and ``register`` is called while the lock is held, which means a concurrent
        :meth:`stop` cannot interleave between them. A wake therefore either completes its
        registration *before* ``stop`` returns — and ``stop``'s caller, which is about to destroy or
        clear this world, is obliged to look for the run it just let through — or it observes the
        closed gate and does nothing whatsoever. A flag consulted before the lock, or a registration
        performed after releasing it, would reopen precisely the window this exists to close.

        ``register`` is called with the lock held and must not await anything, reach a provider, run
        a graph or ask a model; it inspects durable state, starts a run and returns. The wait above
        it happens outside the lock, which is where a cancellation can reach it.
        """
        await self._sleeper(delay)

        with self._lock:
            current = asyncio.current_task()
            if self._pending.get(case_id) is not current:
                # Withdrawn, cancelled or superseded while sleeping. Whoever dropped this task from
                # the mapping has already decided it must not act, possibly from a thread that
                # could not safely cancel a task — so noticing it here is the other half of that
                # decision rather than a belt-and-braces check.
                logger.debug("wake for case %s was withdrawn before it fired", case_id)
                return
            del self._pending[case_id]

            if self._stopped:
                self._intent.pop(case_id, None)
                logger.debug("wake for case %s fired after its world was stopped", case_id)
                return

            if self._closed:
                self._deferred[case_id] = self._intent.pop(case_id, register)
                logger.debug(
                    "wake for case %s deferred: it fell due while the gate was closed", case_id
                )
                return

            self._intent.pop(case_id, None)
            try:
                outcome = register()
            except Exception:
                # **Logged loudly and not re-raised, and that is a scheduler's judgement.** A
                # registration that raises is a bug in the layer above — it is supposed to report
                # outcomes — but letting it out of here kills the task with an exception nobody
                # retrieves and, worse, silently ends this case's scheduling while its state still
                # says it is waiting. Nothing is counted as an attempt, so the backoff does not
                # advance on a failure to attempt anything, and the next run that settles on this
                # case arms again.
                logger.exception("registering a reconciliation for case %s raised", case_id)
                return
            self._record(case_id, outcome)

    def _record(self, case_id: str, outcome: RecoveryOutcome) -> None:
        """Update the process-local backoff from what the registration reported. Lock held."""
        if outcome is RecoveryOutcome.STARTED:
            self._attempts[case_id] = self._attempts.get(case_id, 0) + 1
            logger.debug(
                "reconciliation registered for case %s (attempt %d)",
                case_id,
                self._attempts[case_id],
            )
            return
        if outcome in (RecoveryOutcome.CASE_GONE, RecoveryOutcome.STATE_MOVED):
            self._attempts.pop(case_id, None)
            self._deferred.pop(case_id, None)
        logger.debug("wake for case %s registered nothing: %s", case_id, outcome)

    def forget(self, case_id: str) -> bool:
        """Withdraw this case's wake and its backoff. Event loop only. Returns whether one existed.

        What a run calls when it finds the case is no longer waiting. Withdrawal is by dropping the
        intent, not by cancelling: the cancellation is how the task stops *sleeping* rather than how
        it is stopped from *acting*. The attempt count goes with it, so a case that waits again
        later starts at the first delay rather than inheriting an old episode's backoff.
        """
        with self._lock:
            task = self._pending.pop(case_id, None)
            self._intent.pop(case_id, None)
            had_deferral = self._deferred.pop(case_id, None) is not None
            self._attempts.pop(case_id, None)
        if task is not None:
            task.cancel()
        return task is not None or had_deferral

    # -- closing the gate ----------------------------------------------------

    def close(self) -> None:
        """Refuse further registrations until :meth:`reopen`. Safe from any thread.

        The reversible half of :meth:`stop`, and it exists because two of the three teardowns are
        conditional: an eviction that finds a run in flight must leave the world alive and working,
        and a reset that is refused must leave it exactly as it was. A permanent stop in either
        place would silence a world that is still being used.

        Nested closes are counted, so an inner scope cannot reopen the gate an outer one is relying
        on.

        **A wake that becomes due inside this window is deferred, not spent.** It keeps its intent,
        does not count as an attempt, and is restored by :meth:`reopen`. That is what stops a
        momentary lifecycle event — an eviction that backs off, a reset that is refused — from
        costing a still-waiting case the only opportunity it had scheduled.
        """
        with self._lock:
            self._closed += 1

    def reopen(self) -> None:
        """Undo one :meth:`close` and restore whatever was deferred. Safe from any thread.

        **The restoration is handed to the event loop rather than performed here**, when this is
        called from anywhere else. Arming creates an :class:`asyncio.Task`, which is not safe from
        another thread, and the caller that needs this most is idle eviction backing off in a
        Starlette worker thread. ``call_soon_threadsafe`` is the whole mechanism: it is
        non-blocking, so an evicting thread is not made to wait on the loop, and it is the same
        boundary-crossing the rest of this module refuses to make without.

        A stopped waker stays stopped, and a nested close leaves the gate shut — in both cases
        nothing is restored, which is why the check happens under the lock rather than in the
        caller.
        """
        with self._lock:
            self._closed = max(0, self._closed - 1)
            if self._stopped or self._closed or not self._deferred:
                return
            loop = self._loop

        if loop is None:  # pragma: no cover - nothing was ever armed, so nothing was deferred
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            self._restore_deferred()
            return
        try:
            loop.call_soon_threadsafe(self._restore_deferred)
        except RuntimeError:
            # The loop is closed, so the process is going away and so is this world. Nothing is
            # lost that a restart's own sweep will not find in the store.
            logger.debug("could not restore deferred wakes: the event loop is gone")

    def _restore_deferred(self) -> None:
        """Re-arm every deferred wake. Event loop only; see :meth:`reopen`.

        Held under the lock across the arming so the set cannot be closed, stopped or added to
        halfway through — ``arm`` takes the same re-entrant lock and does nothing that blocks.
        """
        with self._lock:
            if self._stopped or self._closed:
                return
            restored = tuple(self._deferred.items())
            self._deferred.clear()
            for case_id, register in restored:
                self.arm(case_id, RESUME_IMMEDIATELY, register)
            if restored:
                logger.debug("restored %d deferred reconciliation wake(s)", len(restored))

    def discard_deferred(self) -> int:
        """Drop every deferred intent without restoring it. Returns how many. Event loop only.

        For a teardown that **succeeded** — a demo reset that really cleared the store — where the
        cases those intents named no longer exist. Restoring them would schedule runs for cases that
        are gone: harmless, since the registration would answer :attr:`RecoveryOutcome.CASE_GONE`,
        but it would also be this layer pretending not to know something it does know.

        Deliberately separate from :meth:`quiesced`, because only the body of that scope can tell
        whether the teardown went through. A reset that is refused calls nothing and its intents are
        restored on the way out.
        """
        with self._lock:
            discarded = len(self._deferred)
            self._deferred.clear()
            # The backoff goes too. It counts attempts against cases that no longer exist, and a
            # case created later under a reused id would otherwise inherit a stranger's schedule.
            self._attempts.clear()
        return discarded

    def stop(self) -> None:
        """Close the gate permanently. Safe from any thread, and this is why it exists.

        **The contract, stated exactly.** After this returns, no wake held by this waker can
        register another run — not one that was sleeping, not one that was about to check, not one
        that had already woken, and not one that was deferred. A wake that had *already entered* the
        registration critical section when this was called completes it, and this call does not
        return until it has: the lock is what serialises the two. That is the case the caller must
        then handle, which is why every lifecycle that stops a waker looks again for an active run
        afterwards instead of assuming there is none.

        **It cancels nothing**, deliberately. The caller that needs this most is not on the event
        loop — idle eviction runs in a Starlette worker thread — and cancelling a task from another
        thread is not safe. A stopped wake therefore sleeps out the rest of its delay and then finds
        the gate closed and returns, which leaves a bounded memory tail (its world stays referenced
        until it wakes) and **no capability**: it cannot register a run, so it cannot reach a store,
        a graph or a provider. :meth:`aclose` is the event-loop variant that also collects the
        tasks.
        """
        with self._lock:
            # One flag, not two. Setting the reversible counter as well would be a second answer to
            # "is the gate shut", and the two would have to be kept agreeing forever by hand — which
            # is the shape of defect this repository keeps closing. `_stopped` dominates `_closed`
            # everywhere the gate is read.
            self._stopped = True
            # Keep a reference. A task nobody holds may be garbage collected mid-flight, and the
            # one thing worse than a wake that lingers is one that disappears unpredictably.
            self._stranded.update(self._pending.values())
            self._pending.clear()
            self._intent.clear()
            self._deferred.clear()
            self._attempts.clear()

    # -- quiescence ----------------------------------------------------------

    def cancel_all(self) -> tuple[asyncio.Task[None], ...]:
        """Withdraw and cancel every pending wake, **keeping their intents**. Loop only.

        Withdrawal happens under the lock and cancellation outside it, in that order, so a wake that
        is *inside* its critical section is not left half-handled: it finishes registering, finds
        itself already dropped from the mapping on the way out, and the cancellation it then
        receives has nothing left to interrupt.

        The intents move to the deferred set rather than being dropped, because the only caller is
        the *reversible* scope below and a world that survives it must keep its scheduling. A
        teardown that really destroys the world calls :meth:`stop` or :meth:`discard_deferred`,
        both of which clear them.
        """
        with self._lock:
            tasks = tuple(self._pending.values())
            for case_id in tuple(self._pending):
                self._deferred[case_id] = self._intent.pop(case_id, self._deferred.get(case_id))
            self._pending.clear()
        for task in tasks:
            task.cancel()
        return tasks

    async def quiesce(self) -> int:
        """Cancel every pending wake and wait until none is still running. Event loop only.

        Returns how many pending wakes were withdrawn. **Waiting is the point**: a caller that
        cancels and then proceeds has established nothing, because a cancellation is a request
        delivered at the task's next suspension point and a wake that is mid-registration has none.
        Awaiting them is what turns *"asked them to stop"* into *"none of them is running"*.

        Stranded wakes — ones a previous :meth:`stop` left sleeping — are collected here too, so a
        caller that stops and then quiesces is left with no wake task running at all.

        Gathered with ``return_exceptions`` so a cancellation is a result rather than something
        raised at whoever is tearing the world down, and so one wake that failed cannot leave the
        others unawaited with their exceptions unretrieved.
        """
        tasks = self.cancel_all()
        stranded = self._take_stranded()
        for task in stranded:
            task.cancel()
        outstanding = [task for task in (*tasks, *stranded) if not task.done()]
        if outstanding:
            await asyncio.gather(*outstanding, return_exceptions=True)
        return len(tasks)

    @contextlib.asynccontextmanager
    async def quiesced(self) -> AsyncIterator[int]:
        """Hold the gate closed, with every wake cancelled and awaited, for the body. Loop only.

        The shape a *reversible* teardown needs, and the ordering is the whole of it: the gate
        closes first so nothing new can be registered, then the pending wakes are cancelled and
        awaited so nothing already scheduled is still running, and only then does the body run. A
        body that clears a store or reseeds a clinic is therefore doing it with no wake able to
        appear behind it — and a body that decides *not* to proceed leaves the world working,
        because the gate reopens on the way out **and the intents it withdrew are restored with
        it**.

        A body that really did destroy what those intents named calls :meth:`discard_deferred`; this
        scope cannot tell, and guessing would either resurrect scheduling for cases that are gone or
        strand cases that are still waiting.

        What the body must still do is look for a run a wake registered *before* the gate closed.
        That one is legitimately in flight and this cannot cancel it; the number yielded is how many
        wakes were withdrawn, never a promise that nothing is running.
        """
        self.close()
        try:
            yield await self.quiesce()
        finally:
            self.reopen()

    async def aclose(self) -> None:
        """Stop permanently, then cancel and collect every wake. Event loop only.

        Shutdown's variant. :meth:`stop` comes first, so the boundary is established before anything
        is awaited and a wake cannot slip a registration in while the collection is under way; the
        cancellation and the gather are safe here because a shutdown, unlike an eviction, runs on
        the loop.
        """
        self.stop()
        stranded = self._take_stranded()
        for task in stranded:
            task.cancel()
        outstanding = [task for task in stranded if not task.done()]
        if outstanding:
            await asyncio.gather(*outstanding, return_exceptions=True)
        if stranded:
            logger.debug("cancelled %d pending reconciliation wake(s) on shutdown", len(stranded))

    def _take_stranded(self) -> tuple[asyncio.Task[None], ...]:
        with self._lock:
            tasks = tuple(self._stranded)
            self._stranded.clear()
        return tasks


def stopped_waker() -> ReconcileWaker:
    """A :class:`ReconcileWaker` already past its stop boundary.

    For a caller that must supply one and has no loop to schedule on. Not a null object standing in
    for the real thing — it is *stopped*, which is a state the real one also reaches — so nothing
    has to branch on which it got.
    """
    waker = ReconcileWaker()
    waker.stop()
    return waker


__all__ = [
    "RESUME_IMMEDIATELY",
    "WAKE_DELAYS",
    "ReconcileWaker",
    "RecoveryOutcome",
    "Registration",
    "Sleeper",
    "stopped_waker",
]
