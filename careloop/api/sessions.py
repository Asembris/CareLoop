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
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from careloop.api.deps import ApiDeps
from careloop.events.simulator import EventSimulator

#: The header a browser carries its world in.
#:
#: A header rather than a cookie because header forwarding through the deployment's CDN is
#: *measured* — ``X-Caregiver`` reaches the origin through it — and cookie forwarding is not, so a
#: cookie would be an assumption that fails in production rather than in a test. One mechanism, so
#: there is never a second place a session could come from.
SESSION_HEADER: Final[str] = "X-CareLoop-Session"

#: The header Demo World reads the same value out of.
#:
#: Declared here rather than imported: ``careloop`` may not depend on ``demoworld``, which is the
#: whole basis of the read-back being independent. ``tests/test_session_isolation.py`` asserts the
#: two spellings agree, so this is a drift check rather than a shared constant — the same
#: arrangement as the seeded slot the referral names.
CLINIC_SESSION_HEADER: Final[str] = "X-Demo-Session"

#: How many worlds one process keeps.
#:
#: Measured rather than guessed. A world that has run **all four** demo scenarios — an in-memory
#: SQLite store holding four cases with their timelines, a run registry, a clinic client and its
#: transport — costs **300 KB** of resident memory, averaged over 32 of them built and driven end to
#: end. So the whole cap is about **19 MB**, against a ``t4g.small`` with 2 GB of which roughly
#: 330 MB is in use: about 1% of what is free. This is a bound on unbounded growth rather than a
#: rationing decision, which is why it is set well above any plausible number of simultaneous
#: judges instead of at it.
#:
#: **It may not exceed Demo World's own cap**, and ``tests/test_session_isolation.py`` fails if it
#: does. A CareLoop session maps one-to-one onto a Demo World world; if this side held more, a live
#: visitor's clinic could be evicted and reseeded underneath them, and the read-back of an
#: appointment that really was made would come back ``ABSENT``. Two registries, one bound.
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
    ) -> None:
        self._build = build
        self._limit = limit
        self._idle_seconds = idle_seconds
        self._now = now
        self._worlds: OrderedDict[str, ApiDeps] = OrderedDict()
        self._touched: dict[str, float] = {}

    def mint(self) -> str:
        """Create a world and return the id it answers to.

        Raises:
            SessionsAtCapacity: if every world held is still busy.
        """
        self._forget_idle()
        if len(self._worlds) >= self._limit:
            raise SessionsAtCapacity(
                f"this demo holds {self._limit} worlds and all of them are in use; try again in a "
                f"few minutes"
            )

        session_id = secrets.token_urlsafe(16)
        self._worlds[session_id] = self._build(session_id)
        self._touched[session_id] = self._now()
        return session_id

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

    def sessions(self) -> tuple[str, ...]:
        """Every session held, least recently used first."""
        return tuple(self._worlds)

    def busy(self, session_id: str) -> bool:
        """Whether that world has a run in flight."""
        world = self._worlds.get(session_id)
        return world is not None and world.runs.any_in_flight() is not None

    async def drain(self) -> None:
        """Wait for every world's runs to finish. For orderly shutdown."""
        for world in list(self._worlds.values()):
            await world.runs.drain()

    def _forget_idle(self) -> None:
        """Drop worlds nobody has touched lately, oldest first, sparing any that is working.

        **Dropped rather than closed.** Closing a store would be the right tidy-up if nothing else
        could be holding it, and a request that has already resolved its world *is* holding it —
        closing underneath one turns a clean re-mint into a 500 from inside SQLite. Letting the last
        reference go frees the same memory a moment later and cannot produce a half-answer.
        """
        deadline = self._now() - self._idle_seconds
        for session_id in list(self._worlds):
            if self._touched.get(session_id, 0.0) > deadline or self.busy(session_id):
                continue
            del self._worlds[session_id]
            self._touched.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._worlds)


def demo_session_worlds(
    *,
    clinic_url: str = "",
    events: EventSimulator | None = None,
    limit: int = DEFAULT_SESSION_LIMIT,
    idle_seconds: float = DEFAULT_SESSION_IDLE_SECONDS,
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

    The event simulator *is* shared, and that is not an oversight. Its fixtures are read-only
    documents, so there is nothing there for one visitor to change under another.
    """
    # Imported here rather than at module scope: this function is the composition root for the
    # isolated demo, and the registry above must stay usable without dragging a store, a transport
    # and a clinic client into every import of it.
    import httpx

    from careloop.api.deps import clinic_context_factory, default_context_factory
    from careloop.state.sqlite_store import SqliteCaseStore
    from careloop.tools.clinic import DEFAULT_TIMEOUT, ClinicClient

    shared_events = events if events is not None else EventSimulator()

    def build(session_id: str) -> ApiDeps:
        clinic = None
        if clinic_url:
            clinic = ClinicClient(
                clinic_url,
                client=httpx.Client(
                    timeout=DEFAULT_TIMEOUT,
                    headers={CLINIC_SESSION_HEADER: session_id},
                ),
            )

        return ApiDeps(
            store=SqliteCaseStore(":memory:"),
            events=shared_events,
            context_factory=(
                clinic_context_factory(clinic) if clinic is not None else default_context_factory
            ),
            clinic=clinic,
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
    "demo_session_worlds",
]
