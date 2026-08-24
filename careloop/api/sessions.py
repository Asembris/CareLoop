"""One world per visitor, for a demo two people can open at once.

The deployed demo was a single shared world: four fixed case ids, one case store, one clinic, and a
reset that any visitor could fire at any moment. Two judges opening the URL together is not a
hypothetical during a three-week judging window, and the failure was silent and maximally bad — B's
scenario reset wiped A's settled case, A's decision card then answered 404, and both competed for
the one seeded slot. Nobody reports that. It just makes the demo look broken to whoever lost.

**Isolation here is a separate world, not a namespaced row.** A session resolves to its own
:class:`~careloop.api.deps.ApiDeps`: its own :class:`~careloop.state.store.CaseStore`, its own
:class:`~careloop.api.runs.RunRegistry`, its own clinic client. So ``POST /api/demo/reset`` calls
``clear()`` on *that* store, and reaching another visitor's cases is not something a route declines
to do — it is something it has no way to express. A shared store filtered by a prefix would put the
whole property on every query remembering to filter, and one forgotten filter is a leak.

**A session id is an isolation namespace and not authentication.** It is minted here and never
verified: nothing is signed, no secret is presented, and anyone who holds one can use it — which is
the same statement ``X-Caregiver`` makes about audit identity. What it buys is that visitors cannot
*collide*, not that they cannot impersonate. It is unguessable so that colliding by accident is
impossible, not because guessing one would be an attack on anything but a demo world holding
synthetic cases.

**A missing session is a 400 and an unknown one is a 410.** The first is the ``X-Caregiver``
reasoning exactly: a required field is absent. The second is its own status on purpose, used nowhere
else in this API, so a client can tell "your world has been forgotten, mint another" from the 409 it
gets when a run is in flight. A judge whose session is evicted mid-visit re-mints and presses again;
what they must never get is a half-answer from a world that is not theirs.

**Bounded, and bounded by use rather than by hope.** Worlds are held until they go idle or until the
cap is reached, and a session with a run in flight is never evicted — a store dropped underneath a
live graph is the version conflict the demo reset already refuses to cause. At capacity, minting is
refused with :class:`SessionsAtCapacity` rather than something being taken from somebody who is
still looking at it.

**And a session has one lifetime, owned here.** A visitor's world is a CareLoop store *and* a Demo
World clinic, and until session V each registry decided on its own when that world ended: this one
by idle time, refreshed on every API request; Demo World's by order of *clinic* access, dropping its
oldest unconditionally. A judge polling the dashboard was alive on this side and the eviction victim
on the other — their appointment disappeared from the external system, and a read-back that should
have been ``MATCHED`` came back ``ABSENT`` against a world recreated empty, with nothing raised
anywhere. Equal capacities never helped: the two disagreed about *order*, not about size.

So this registry ends the visitor's clinic whenever it lets their world go — on expiry, on a demo
reset, and on shutdown — and Demo World removes a world only when it is told to. The coupling runs
CareLoop → Demo World over HTTP, the direction ``POST /demo/reset`` already went, because
``demoworld`` may not import ``careloop`` and that independence is what makes the read-back worth
anything.

**A delete that fails is queued, never swallowed.** The eviction happens regardless — holding a
CareLoop world open because a clinic did not answer would be the wrong direction — and the session
goes on :meth:`SessionWorlds.orphans` to be retried on the next mint, logged as it goes. What that
divergence costs while it lasts is one of Demo World's capacity slots, and never a live visitor's
world: with nothing evicting there, the worst a full clinic can do is refuse somebody arriving.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import wraps
from typing import Final, cast

from careloop.api.deps import ApiDeps
from careloop.events.simulator import EventSimulator
from careloop.graph.witness import NO_WITNESS, Witness
from careloop.tools.clinic import CLINIC_SESSION_HEADER, ClinicUnavailable

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

#: The header a browser carries its world in.
#:
#: A header rather than a cookie because header forwarding through the deployment's CDN is
#: *measured* — ``X-Caregiver`` reaches the origin through it — and cookie forwarding is not, so a
#: cookie would be an assumption that fails in production rather than in a test. One mechanism, so
#: there is never a second place a session could come from.
SESSION_HEADER: Final[str] = "X-CareLoop-Session"


#: How many worlds one process keeps.
#:
#: Measured rather than guessed. A world that has run **all four** demo scenarios — an in-memory
#: SQLite store holding four cases with their timelines, a run registry, a clinic client and its
#: transport — costs **300 KB** of resident memory, averaged over 32 of them built and driven end to
#: end. So the whole cap is about **19 MB**. Measured on the deployed ``t4g.small`` after this
#: shipped: 1841 MB total, **1231 MB available**, the API process at 144 MB and Demo World at 39 MB.
#: The cap is therefore about 1.5% of what is free. This is a bound on unbounded growth rather than
#: a rationing decision, which is why it is set well above any plausible number of simultaneous
#: judges instead of at it.
#:
#: **It may not exceed Demo World's own cap**, and ``tests/test_session_isolation.py`` fails if it
#: does — but that inequality is now defence in depth rather than the property. It used to be the
#: whole guard, and it was a number standing in for a lifecycle: equal capacities do not synchronise
#: eviction *order*, which is what actually took a live visitor's clinic away. The property is
#: ``tests/test_session_lifecycle.py``, and what holds it up is that this registry ends a session's
#: clinic when it lets the session go and Demo World never removes one on its own.
DEFAULT_SESSION_LIMIT: Final[int] = 64

#: How long a world outlives the last request that used it, in seconds.
#:
#: Half an hour is far longer than the three minutes ``docs/judges.md`` describes and short enough
#: that a day of visitors does not accumulate. An idle world is forgotten rather than destroyed: a
#: judge who comes back to a closed laptop re-mints and presses a scenario again.
DEFAULT_SESSION_IDLE_SECONDS: Final[float] = 1800.0


class MissingSession(Exception):
    """Raised when a request that needs a world names none."""


class UnknownSession(Exception):
    """Raised when a session id names no world here.

    Either it was never minted, or it was minted by a process that has since restarted, or it went
    idle and was forgotten. The three are indistinguishable on purpose: all of them mean the same
    thing to a caller, which is "mint another and start again".
    """


class SessionsAtCapacity(Exception):
    """Raised when every world this process holds is still in use.

    A refusal rather than an eviction: taking a world from somebody who is mid-demo to give it to
    somebody arriving is the trade nobody would choose.
    """


#: How a world's Demo World session is ended when this registry lets the world go.
#:
#: Injected rather than reached for, so ``tests/test_session_lifecycle.py`` can drive the identical
#: script with the coupling removed and require the damage to happen. The default is the real one;
#: forgetting to pass anything gets you the coupling.
WorldEnder = Callable[[str, ApiDeps], None]


def end_demo_world(session_id: str, world: ApiDeps) -> None:
    """End that world's Demo World session, over HTTP, the way everything else reaches the clinic.

    A world with no clinic has nothing external to end — every effect in it was simulated — so this
    is a no-op rather than an error. ``session_id`` is not passed on: the client already carries the
    header naming the world it books against, so the thing that made the appointments is the thing
    that ends the world holding them, and there is no second place for the two to disagree.
    """
    if world.clinic is None:
        return
    world.clinic.end_session()


def named(session_id: str) -> str:
    """A session id as it may appear in a log line: the first eight characters and no more.

    **A session id was a credential and this file put whole ones in log lines.** Possession alone
    granted a world, so a WARNING carrying one handed anybody who could read the logs everything
    that world could do -- including a real clinic booking. Production has since made the id an
    isolation *namespace* whose owner is checked against a verified principal, which removes the
    authority; the demo has not, and there a whole id is still worth having.

    Eight characters of a 128-bit token is enough to correlate two lines about the same visitor and
    nowhere near enough to use one. Not a hash, because the point is that an operator reading two
    lines can see they are about the same world, and a hash they would have to compute to compare
    is a hash nobody compares.
    """
    return f"{session_id[:8]}..." if len(session_id) > 8 else session_id


def serialised[M: Callable[..., object]](method: M) -> M:
    """Run ``method`` holding the registry's lock.

    The same arrangement, and the same reasoning, as
    :func:`careloop.state.sqlite_store.serialised`: a served CareLoop reaches this from
    Starlette's worker pool, so two requests are two threads on one registry.

    **Measured, not anticipated.** Two concurrent ``POST /api/sessions`` against a registry
    holding anything idle raised ``KeyError`` out of ``_forget_idle`` and answered 500: it
    snapshots the ids with ``list(self._worlds)`` and then deletes them one at a time, so the
    second thread deletes what the first already did. Reproduced during a full e2e run, and
    every other method mutates or reads the same two structures — ``mint`` decides capacity
    from a length it does not hold, ``world`` calls ``move_to_end`` on an ``OrderedDict``
    another thread may be iterating.

    Applied to every public method rather than to the eviction alone, for the store's reason:
    a read taken mid-eviction is a half-answer about which worlds exist.
    ``tests/test_session_worlds_are_serialised.py`` asserts the decorator is on all of them, so
    a method added later cannot quietly be the one that is not.
    """

    @wraps(method)
    def guarded(self: SessionWorlds, *args: object, **kwargs: object) -> object:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(M, guarded)


class SessionWorlds:
    """Worlds by session id, minted on request and forgotten when they go quiet.

    ``build`` is supplied rather than constructed here, for the reason everything else injectable is
    injected: a registry that builds its own worlds can only be tested against the worlds it builds.
    :func:`demo_session_worlds` is the composition the deployment uses.
    """

    def __init__(
        self,
        build: Callable[[str], ApiDeps],
        *,
        limit: int = DEFAULT_SESSION_LIMIT,
        idle_seconds: float = DEFAULT_SESSION_IDLE_SECONDS,
        now: Callable[[], float] = time.monotonic,
        end_world: WorldEnder = end_demo_world,
    ) -> None:
        self._build = build
        self._limit = limit
        self._idle_seconds = idle_seconds
        self._now = now
        self._end_world = end_world
        self._worlds: OrderedDict[str, ApiDeps] = OrderedDict()
        self._touched: dict[str, float] = {}
        #: Worlds this registry has let go whose clinic it has not managed to end yet.
        self._orphaned: OrderedDict[str, ApiDeps] = OrderedDict()
        #: Which caregiver each session belongs to, where one does. Never populated from anything
        #: a request carried -- see :meth:`mint`.
        self._owners: dict[str, str] = {}
        # Re-entrant: `_mint` holds it and calls `_forget_idle`, which calls `busy`.
        self._lock = threading.RLock()

    def mint(self, owner: str | None = None) -> str:
        """Create a world for ``owner`` and return the id it answers to.

        ``owner`` is a ``caregiver_id`` this registry records against the session and hands back
        from :meth:`owner_of`. It is **not** read from anything the caller sent: a production route
        passes the caregiver its verified principal resolved to, so a workspace is owned by whoever
        was authenticated when it was minted and by nobody a request could name. ``None`` is the
        demo's unowned world, which needs no owner because a synthetic world is isolated rather
        than authorised.

        **Not** :func:`serialised`, and deliberately — the same reasoning :meth:`drain` carries.
        The registry mutation is :meth:`_mint`, which holds the lock; ending the clinics of the
        worlds that mutation let go is an HTTP round trip, and a lock held across one would stall
        every other visitor's request for as long as Demo World takes to answer. Everything that
        touches ``_worlds``, ``_touched`` or ``_orphaned`` is still guarded.

        Raises:
            SessionsAtCapacity: if every world held is still busy.
        """
        session_id = self._mint(owner)
        self._end_forgotten()
        return session_id

    @serialised
    def _mint(self, owner: str | None) -> str:
        """Expire what has gone quiet, then create a world. The guarded half of :meth:`mint`."""
        self._forget_idle()
        if len(self._worlds) >= self._limit:
            raise SessionsAtCapacity(
                f"this demo holds {self._limit} worlds and all of them are in use; try again in a "
                f"few minutes"
            )

        session_id = secrets.token_urlsafe(16)
        self._worlds[session_id] = self._build(session_id)
        self._touched[session_id] = self._now()
        if owner is not None:
            self._owners[session_id] = owner
        return session_id

    @serialised
    def world(self, session_id: str) -> ApiDeps:
        """The world ``session_id`` names.

        Raises:
            UnknownSession: if there is no such world.
        """
        found = self._worlds.get(session_id)
        if found is None:
            raise UnknownSession(
                f"session {session_id!r} has no world here; it was never minted, or it has been "
                f"idle long enough to be forgotten"
            )
        self._worlds.move_to_end(session_id)
        self._touched[session_id] = self._now()
        return found

    @serialised
    def owner_of(self, session_id: str) -> str | None:
        """Which caregiver this workspace belongs to, or ``None`` if nobody.

        A separate question from :meth:`world`, deliberately: resolving a workspace and deciding
        whether this caller may use it are two decisions, and folding them together would make the
        second one something a future caller could skip by calling the first.
        """
        return self._owners.get(session_id)

    @serialised
    def sessions(self) -> tuple[str, ...]:
        """Every session held, least recently used first."""
        return tuple(self._worlds)

    @serialised
    def busy(self, session_id: str) -> bool:
        """Whether that world has a run in flight."""
        world = self._worlds.get(session_id)
        return world is not None and world.runs.any_in_flight() is not None

    @serialised
    def orphans(self) -> tuple[str, ...]:
        """Sessions this registry has let go whose Demo World clinic it could not end.

        Empty in every ordinary run. A name on this list is a real divergence between the two
        registries — Demo World is holding a world for a visitor CareLoop has forgotten — and it is
        here so that divergence is *visible* rather than silent, which is the difference between the
        old defect and a transient failure. Each is retried on the next mint.
        """
        return tuple(self._orphaned)

    def resume_unsettled(self) -> tuple[str, ...]:
        """Schedule recovery for every world's abandoned actions. Returns the case ids.

        The registry-shaped counterpart of :meth:`careloop.api.deps.ApiDeps.resume_unsettled`, so
        the lifespan asks one question and does not have to know which shape it got — the same
        arrangement :meth:`drain` already uses.

        **At startup this finds nothing, and that is correct rather than a stub.** An isolated app
        holds no worlds until a visitor mints one, and every session store is
        ``SqliteCaseStore(":memory:")``, so there is nothing durable for a restart to inherit and no
        case that could have survived the process that abandoned it. The method is right; the
        registry is empty. If a world were ever restored from disk, this would already sweep it.
        """
        resumed: list[str] = []
        with self._lock:
            worlds = list(self._worlds.values())
        for world in worlds:
            resumed.extend(world.resume_unsettled())
        return tuple(resumed)

    async def drain(self) -> None:
        """Close every world down, then end every world's clinic. Orderly shutdown.

        Closing a world down is stopping its wakes and then draining its runs, in that order — see
        :meth:`careloop.api.deps.ApiDeps.drain`, which owns it.

        Not :func:`serialised`, and deliberately: holding a blocking lock across an ``await``
        would stop the event loop from making the progress being waited for. The snapshot is
        taken under the lock, which is the part that races an eviction; draining a world that
        has since been forgotten is harmless, and a world with a run in flight is never
        evicted anyway.

        **The clinics are ended here because a restart mints all-new ids.** Nothing that comes back
        can reach a world minted before the restart — every visitor is answered ``410`` and mints
        another — so a clinic left behind is unreachable and, with Demo World no longer evicting,
        permanent. Best effort: a shutdown is not a place to fail, and a clinic that does not answer
        one is beyond anything this process can still do about it.
        """
        with self._lock:
            worlds = list(self._worlds.items())
        for _, world in worlds:
            # `ApiDeps.drain`, not `world.runs.drain()`: the order in which a world's wakes are
            # stopped and its runs are drained is that world's own contract, and stating it in two
            # places is how the two come apart. This is the shape the lifespan already relies on for
            # a single-world app.
            await world.drain()

        self._end_forgotten()
        for session_id, world in worlds:
            if not self._end_or_orphan(session_id, world, requeue=False):
                # One unreachable clinic short-circuits the rest, for `_end_forgotten`'s reason and
                # one more: shutdown is on a deadline systemd sets, and sixty-four timeouts would
                # spend it all on a service that is already gone.
                return

    @serialised
    def _forget_idle(self) -> None:
        """Drop worlds nobody has touched lately, oldest first, sparing any that is working.

        **Dropped rather than closed.** Closing a store would be the right tidy-up if nothing else
        could be holding it, and a request that has already resolved its world *is* holding it —
        closing underneath one turns a clean re-mint into a 500 from inside SQLite. Letting the last
        reference go frees the same memory a moment later and cannot produce a half-answer.

        **Busy is asked twice, and the second time is the one that matters.** The first ask is the
        old one: a world with a run in flight is not idle and is skipped. But a pending
        reconciliation wake is *not* a run, so between that answer and the eviction a wake could
        start one — against a world about to be forgotten and a clinic session about to be ended.
        Closing the wake gate in between makes the two answers mean something: after
        :meth:`careloop.api.wake.ReconcileWaker.close` returns, no wake here can begin a
        registration, and any wake that was already inside one has finished it — so a run that won
        that race exists in the registry by the time the second ask happens, and this backs off and
        leaves the world alone.

        **A pending wake is deliberately not itself a reason to spare a world.** Counting one as
        busy would make a visitor's world immortal for exactly as long as their provider stayed
        unavailable, which is the failure mode the idle bound exists to prevent. What a stopped
        wake may still cost is a bounded memory tail — its world stays referenced until it wakes and
        finds the gate shut — and what it can no longer do is anything at all.
        """
        deadline = self._now() - self._idle_seconds
        for session_id in list(self._worlds):
            world = self._worlds[session_id]
            if self._touched.get(session_id, 0.0) > deadline or self.busy(session_id):
                continue

            world.wakes.close()
            if self.busy(session_id):
                # A wake got its run registered before the gate shut. That run is legitimate and
                # this is not the place to interfere with it: reopen and leave the world working,
                # and it will be idle again on some later sweep.
                world.wakes.reopen()
                LOGGER.debug(
                    "not evicting %s: a run was registered while the wake gate was closing",
                    named(session_id),
                )
                continue

            # Permanent now that the world is going. `stop` cancels nothing, because this runs in a
            # Starlette worker thread and cancelling a task from off the event loop is not safe.
            world.wakes.stop()
            forgotten = self._worlds.pop(session_id)
            self._touched.pop(session_id, None)
            self._owners.pop(session_id, None)
            self._orphan(session_id, forgotten)

    @serialised
    def _orphan(self, session_id: str, world: ApiDeps) -> None:
        """Queue a world's clinic to be ended, oldest dropped if the queue is full.

        Bounded because a Demo World that never answers would otherwise have this grow for as long
        as visitors keep arriving. Dropping the oldest is safe in both directions the queue can be
        long for: if the clinic comes back holding the same state the retries clear it, and if it
        came back by *restarting* its registry is empty and there was nothing left to clear.
        """
        self._orphaned[session_id] = world
        while len(self._orphaned) > self._limit:
            dropped, _ = self._orphaned.popitem(last=False)
            LOGGER.warning(
                "giving up on ending the Demo World session for %s: %d ends are already queued",
                named(dropped),
                self._limit,
            )

    @serialised
    def _take_orphans(self) -> list[tuple[str, ApiDeps]]:
        """Empty the queue and hand it back, so the retries happen off the lock."""
        taken = list(self._orphaned.items())
        self._orphaned.clear()
        return taken

    def _end_forgotten(self) -> None:
        """Try to end every queued clinic. **Runs outside the lock**; see :meth:`mint`.

        One unreachable clinic short-circuits the rest: if the service did not answer this call it
        will not answer the next one either, and a queue of twenty would otherwise cost twenty
        timeouts on the request that happened to trip it.
        """
        queued = self._take_orphans()
        for index, (session_id, world) in enumerate(queued):
            if not self._end_or_orphan(session_id, world, requeue=True):
                for later_id, later in queued[index + 1 :]:
                    self._orphan(later_id, later)
                return

    def _end_or_orphan(self, session_id: str, world: ApiDeps, *, requeue: bool) -> bool:
        """End one world's clinic. Returns whether the clinic answered at all.

        A clinic that could not be *reached* is queued again when ``requeue``: the divergence is
        real and repairable, and it will be repaired on the next mint. Anything else is the clinic
        *answering* and declining — which an idempotent delete has no reason to do, so it is a bug
        rather than a blip, and retrying it forever would only make the log longer.

        Either way the world stays let go. Holding a CareLoop world open because a clinic did not
        answer would keep memory here to repair a record over there, which is the wrong direction.
        """
        try:
            self._end_world(session_id, world)
        except ClinicUnavailable as unreachable:
            LOGGER.warning(
                "could not reach Demo World to end the session for %s (%s); it will be retried",
                named(session_id),
                unreachable,
            )
            if requeue:
                self._orphan(session_id, world)
            return False
        except Exception:
            LOGGER.exception(
                "Demo World refused to end the session for %s; it is being left as it is",
                named(session_id),
            )
        return True

    @serialised
    def __len__(self) -> int:
        return len(self._worlds)


def demo_session_worlds(
    *,
    clinic_url: str = "",
    upload_bucket: str = "",
    events: EventSimulator | None = None,
    witness: Witness = NO_WITNESS,
    limit: int = DEFAULT_SESSION_LIMIT,
    idle_seconds: float = DEFAULT_SESSION_IDLE_SECONDS,
    directory: object | None = None,
) -> SessionWorlds:
    """The isolated demo's composition: an in-memory store and a session-scoped clinic per visitor.

    Composition and nothing else — no route, no policy, no state. Every world gets the same shape
    the single-session app has always had, which is what keeps "the demo runs the normal path" true
    under isolation.

    **The store is SQLite in memory, not** :class:`~careloop.state.InMemoryCaseStore`. Session C's
    finding was that a suite covering both halves separately had never run the combination a
    deployment uses; keeping the deployed store class is what stops isolation reintroducing that,
    at the cost of a world that does not outlive the process. That trade is stated in
    ``docs/judges.md``: durable cross-process ownership is demonstrated by
    ``scripts/resume_demo.py`` rather than by this URL.

    The clinic client carries :data:`CLINIC_SESSION_HEADER` as a **default header on its own
    transport**, which is what scopes every booking and every read-back without
    ``careloop.tools.clinic`` knowing that sessions exist at all. A transport per world rather than
    one shared and rewritten: the header is per-world state, and mutating a shared client would have
    the world built last decide which clinic every earlier one reaches.

    **Two clients into one world, and the second is why an ambiguous outcome is demonstrable.**
    :func:`careloop.api.faults.booking_response_loss` builds a second client reaching the same
    clinic and the same world over a transport that can discard the answer to one booking. The
    context factory hands it to runs for the case a recovery scenario armed and hands every other
    run the ordinary client, so a booking belonging to any other case here travels a transport that
    contains no fault at all. Two clients rather than one switched between them: which object a run
    holds is settled when the run is built, and a shared client toggled per run would be two
    concurrent runs racing over one flag.

    With nothing armed the composed factory is
    :func:`careloop.api.deps.clinic_context_factory` — ``tests/test_demo_recovery.py`` asserts that
    rather than leaving it implied, for the reason ``simulated_context_factory`` is asserted equal
    to the default one: a second way of building the same thing must not quietly become different.

    The event simulator *is* shared, and that is not an oversight. Its fixtures are read-only
    documents, so there is nothing there for one visitor to change under another.

    The ``witness`` is shared for a stronger reason than convenience: it is stateless and holds
    nothing, and what a claim lands on is whichever span the *run* is executing inside, so a
    per-world instance would differ from a shared one in nothing but count. It is a sink either
    way — a visitor's case, its policy verdicts and its bookings are identical whether one is
    supplied or not.
    """
    # Imported here rather than at module scope: this function is the composition root for the
    # isolated demo, and the registry above must stay usable without dragging a store, a transport
    # and a clinic client into every import of it.
    import httpx

    from careloop.api.deps import provider_context_factory
    from careloop.api.faults import booking_response_loss
    from careloop.state.sqlite_store import SqliteCaseStore
    from careloop.tools.clinic import DEFAULT_TIMEOUT, ClinicClient
    from careloop.tools.objectstore import ObjectStoreClient

    shared_events = events if events is not None else EventSimulator()

    def build(session_id: str) -> ApiDeps:
        clinic = None
        faults = None
        if clinic_url:
            clinic = ClinicClient(
                clinic_url,
                session=session_id,
                client=httpx.Client(timeout=DEFAULT_TIMEOUT),
            )
            faults = booking_response_loss(clinic_url, session=session_id)

        # **The session id is the object namespace.** Not a credential and not verified by anything
        # — exactly what ``X-Demo-Session`` is to the clinic — but it is what keeps two visitors
        # pressing the same button off one object: the frozen idempotency key is derived from the
        # case id and the identifying arguments, both of which a demo scenario fixes, so it is
        # byte-identical across visitors by design.
        objects = ObjectStoreClient(upload_bucket, session=session_id) if upload_bucket else None

        return ApiDeps(
            store=SqliteCaseStore(":memory:"),
            events=shared_events,
            context_factory=provider_context_factory(
                clinic=clinic,
                objects=objects,
                witness=witness,
                losing_clinic=faults,
                directory=directory,
            ),
            clinic=clinic,
            objects=objects,
            faults=faults,
        )

    return SessionWorlds(build, limit=limit, idle_seconds=idle_seconds)


__all__ = [
    "CLINIC_SESSION_HEADER",
    "DEFAULT_SESSION_IDLE_SECONDS",
    "DEFAULT_SESSION_LIMIT",
    "SESSION_HEADER",
    "MissingSession",
    "SessionWorlds",
    "SessionsAtCapacity",
    "UnknownSession",
    "WorldEnder",
    "demo_session_worlds",
    "end_demo_world",
    "named",
]
