"""Pending reconciliation wake-ups, and the lifecycle boundary that makes them safe to cancel.

A case parked in ``WAITING_EXTERNAL`` holds a durably prepared action nobody answered about. It
converges on the *next run of the case* — :class:`careloop.graph.nodes.ReconcileNode` re-claims the
frozen identity and asks again — and today the only thing that starts such a run is the startup
sweep in :meth:`careloop.api.deps.ApiDeps.resume_unsettled`. A process that stays up therefore
leaves the case parked until it is restarted.

This module is the *machinery* for closing that gap, and deliberately not the closing of it.
Nothing in ``careloop/`` calls :meth:`ReconcileWaker.arm` yet: the scheduling policy — when a wake
is armed, how long it waits, what it does when a provider stays down — is the next patch, and it
belongs on top of a lifecycle that has already been proven race-safe rather than underneath one
being written at the same time.

**What this is allowed to know.** A case id and an opaque thunk. It holds no tool name, no
arguments, no idempotency key, no policy verdict and no graph node; it invokes no model and reaches
no provider. It is the same kind of object :class:`careloop.api.runs.RunRegistry` is — execution
context, never a business fact — and invariant 8 is untouched by it. What a wake schedules is
supplied by its caller, so this module cannot decide *what* is re-attempted even by accident.

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

**One lock, and it is never held across an ``await``.** ``threading`` rather than ``asyncio``
because two of the callers are not on the event loop: a served CareLoop evicts idle worlds from
Starlette's worker pool, exactly as :class:`careloop.state.sqlite_store.SqliteCaseStore` is reached
from it. That is the reasoning :func:`careloop.api.sessions.serialised` already records, and the
same discipline applies — the lock covers dictionary operations and the registration callback, all
of them synchronous, and every cancellation and every wait happens outside it.

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

logger = logging.getLogger(__name__)

#: How a wake waits before it registers anything.
#:
#: Injected so a test can drive the lifecycle without spending wall-clock time on it, and so a race
#: can be reproduced deterministically rather than by hoping a sleep is long enough. The delay is
#: the caller's to choose; this module has no opinion about it and deliberately holds no schedule.
Sleeper = Callable[[float], Awaitable[None]]

#: What a wake does when it fires: start a run for the case it was armed for.
#:
#: Opaque on purpose. A wake that could *build* the work it schedules would be a second place the
#: identity of a recovered action could be decided, and this repository has closed three defects of
#: that exact shape. The caller supplies a thunk that already knows which case it is about.
Registration = Callable[[], object]


class ReconcileWaker:
    """Wakes pending in one world, with a stop that cannot be raced.

    One per :class:`careloop.api.deps.ApiDeps`, exactly as one
    :class:`careloop.api.runs.RunRegistry` is, so an isolated demo's per-visitor worlds get their
    own without anything having to look one up. Ownership *is* the isolation: a reset, an eviction
    or a shutdown reaches the wakes belonging to the world it is tearing down and has no way to
    express reaching another's.

    **Which methods may be called from which thread**, because the answer is not the same for all
    of them and getting it wrong is how a cancellation from the wrong thread corrupts a loop:

    ``stop``, ``close``, ``reopen``, ``pending``, ``stopped``
        Any thread. They take the lock, mutate or read plain structures, and touch no task and no
        event loop. :meth:`careloop.api.sessions.SessionWorlds._forget_idle` runs in a worker
        thread and needs exactly these.

    ``arm``, ``forget``, ``cancel_all``, ``quiesce``, ``quiesced``, ``aclose``
        The event loop only. They create or cancel :class:`asyncio.Task` objects, which is not safe
        from another thread.
    """

    def __init__(self, *, sleeper: Sleeper = asyncio.sleep) -> None:
        self._sleeper = sleeper
        # Re-entrant so a registration callback that reaches back in — to `forget` the case it has
        # just started a run for, say — does not deadlock against the lock its own wake is holding.
        self._lock = threading.RLock()
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._stranded: set[asyncio.Task[None]] = set()
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

    # -- arming --------------------------------------------------------------

    def arm(self, case_id: str, delay: float, register: Registration) -> bool:
        """Schedule one wake for ``case_id`` after ``delay`` seconds. Event loop only.

        Returns whether a wake was armed. It is ``False`` — with nothing scheduled and nothing
        raised — when this waker is stopped, when its gate is closed, or when a wake for this case
        is already pending. A refusal is an ordinary answer rather than an error: every caller of
        this is trying to create an *opportunity*, and a world being torn down has no need of one.

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
            if self._stopped or self._closed:
                return False
            if case_id in self._pending:
                return False
            task = asyncio.create_task(self._wake(case_id, delay, register), name=f"wake-{case_id}")
            self._pending[case_id] = task
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

        ``register`` is called with the lock held and must not await anything or block on anything
        slow; it starts a run and returns. The wait above it happens outside the lock, which is
        where a cancellation can reach it.
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

            if self._stopped or self._closed:
                logger.debug("wake for case %s fired after its world was closed", case_id)
                return

            register()

    def forget(self, case_id: str) -> bool:
        """Withdraw the pending wake for one case. Event loop only. Returns whether there was one.

        The single-case counterpart of :meth:`cancel_all`, for a caller that has learned this case
        no longer needs one — it settled, it was decided, it is gone. Dropping it from the mapping
        is what makes the withdrawal effective; the cancellation is how the task stops *sleeping*
        rather than how it is stopped from *acting*.
        """
        with self._lock:
            task = self._pending.pop(case_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    # -- closing the gate ----------------------------------------------------

    def close(self) -> None:
        """Refuse further registrations until :meth:`reopen`. Safe from any thread.

        The reversible half of :meth:`stop`, and it exists because two of the three teardowns are
        conditional: an eviction that finds a run in flight must leave the world alive and working,
        and a reset that is refused must leave it exactly as it was. A permanent stop in either
        place would silence a world that is still being used.

        Nested closes are counted, so an inner scope cannot reopen the gate an outer one is relying
        on.

        **A wake that fires while the gate is closed is spent, not deferred**, and whoever wires the
        scheduling policy on top of this needs to know it: it withdraws itself, declines to register
        and is gone, so a world whose teardown backed off can be left with one fewer pending wake
        than it had. That is bounded and safe — the case is still ``WAITING_EXTERNAL``, still
        durable, still swept at startup, and still armed again by whatever settles on it next — but
        it means **a wake must never be treated as a promise**. Making one re-arm itself here would
        be a retry decision, which is policy and does not belong in the machinery.
        """
        with self._lock:
            self._closed += 1

    def reopen(self) -> None:
        """Undo one :meth:`close`. Safe from any thread; a stopped waker stays stopped."""
        with self._lock:
            self._closed = max(0, self._closed - 1)

    def stop(self) -> None:
        """Close the gate permanently. Safe from any thread, and this is why it exists.

        **The contract, stated exactly.** After this returns, no wake held by this waker can
        register another run — not one that was sleeping, not one that was about to check, and not
        one that had already woken. A wake that had *already entered* the registration critical
        section when this was called completes it, and this call does not return until it has: the
        lock is what serialises the two. That is the case the caller must then handle, which is why
        every lifecycle that stops a waker looks again for an active run afterwards instead of
        assuming there is none.

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
            # everywhere the gate is read, and a mutant that removed a redundant second write here
            # would be equivalent and prove nothing.
            self._stopped = True
            # Keep a reference. A task nobody holds may be garbage collected mid-flight, and the
            # one thing worse than a wake that lingers is one that disappears unpredictably.
            self._stranded.update(self._pending.values())
            self._pending.clear()

    # -- quiescence ----------------------------------------------------------

    def cancel_all(self) -> tuple[asyncio.Task[None], ...]:
        """Withdraw and cancel every pending wake. Event loop only. Returns the cancelled tasks.

        Withdrawal happens under the lock and cancellation outside it, in that order, so a wake that
        is *inside* its critical section is not left half-handled: it finishes registering, finds
        itself already dropped from the mapping on the way out, and the cancellation it then
        receives has nothing left to interrupt.
        """
        with self._lock:
            tasks = tuple(self._pending.values())
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
        because the gate reopens on the way out.

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


__all__ = ["ReconcileWaker", "Registration", "Sleeper", "stopped_waker"]
