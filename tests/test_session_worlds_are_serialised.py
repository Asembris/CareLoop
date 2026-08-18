"""The session registry is reached from a thread pool, and it is serialised for it.

A served CareLoop resolves every request's world through one :class:`SessionWorlds`, and Starlette
runs sync endpoints in a worker pool — so "two visitors arrive at once" is literally two threads on
one ``OrderedDict``. This is the same statement ``tests/test_store.py`` makes about the SQLite
store, in the same shape, for the same reason.

**Found by running, not by reading.** Two concurrent ``POST /api/sessions`` against a registry
holding anything idle raised ``KeyError`` out of ``_forget_idle`` and answered 500. The eviction
takes a snapshot of the ids and then deletes them one at a time, so whichever thread arrives second
deletes an id the first already removed.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from typing import ClassVar

import pytest

from careloop.api.deps import ApiDeps
from careloop.api.sessions import SessionWorlds
from careloop.state.memory_store import InMemoryCaseStore


def a_world(_session_id: str) -> ApiDeps:
    """The cheapest thing that satisfies the shape. Nothing here exercises a world."""
    return ApiDeps(store=InMemoryCaseStore())


class TestEverySessionMethodIsSerialised:
    """The lock is not optional on any of them, and a new method must not be the exception.

    Asserted on the decorator rather than by racing threads, because a race that happens to pass is
    not evidence — the timing test below is the reproduction, and this is the invariant.
    """

    METHODS: ClassVar[tuple[str, ...]] = ("mint", "world", "sessions", "busy", "_forget_idle")

    def test_the_list_covers_everything_that_touches_the_registry(self) -> None:
        """Vacuity: a method missing from this list is a method the sweep never looks at.

        ``drain`` is excluded by name and with a reason: it is a coroutine, and holding a blocking
        lock across its ``await`` would stop the loop from making the progress it is waiting for.
        It takes its snapshot under the lock instead, which is the part that races an eviction.
        """
        touching = {
            name
            for name in dir(SessionWorlds)
            if callable(getattr(SessionWorlds, name))
            and (not name.startswith("_") or name in {"_forget_idle", "__len__"})
            and name not in {"drain"}
        }
        assert touching == {*self.METHODS, "__len__"}

    def test_every_one_of_them_holds_the_lock(self) -> None:
        unguarded = [
            name
            for name in (*self.METHODS, "__len__")
            if not hasattr(getattr(SessionWorlds, name), "__wrapped__")
        ]
        assert unguarded == []

    def test_the_detector_notices_an_undecorated_method(self) -> None:
        """Vacuity: ``__wrapped__`` is the decorator's fingerprint, so a plain method has none."""

        class Undecorated:
            def mint(self) -> None: ...

        assert not hasattr(Undecorated.mint, "__wrapped__")


@pytest.fixture
def switching_hard() -> Iterator[None]:
    """Make the interpreter change threads as often as it can, for the duration of one test.

    Without this the reproduction below does not reproduce, and that is worth stating plainly: at
    the default five-millisecond switch interval one thread finishes a whole eviction sweep inside
    its own slice, so the window between the snapshot and the deletes almost never opens. A served
    CareLoop opens it constantly — every request does I/O and yields — which is why this was found
    by running the e2e suite and not by running the unit tests.

    Restored in a ``finally``: it is a process-wide setting and leaving it lowered would slow every
    test after this one.
    """
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-7)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


@pytest.mark.usefixtures("switching_hard")
def test_concurrent_mints_over_an_evictable_registry_never_raise() -> None:
    """The reproduction, kept because the invariant above cannot show what it costs.

    ``idle_seconds=0`` with a clock that never advances makes *everything* evictable on every call,
    so each mint walks the whole list — the condition the failure needs, reached without sleeping.
    Measured against a registry with the decorators stripped: eight threads, five runs out of five,
    seven of the eight raising ``KeyError`` from ``_forget_idle``.
    """
    worlds = SessionWorlds(a_world, limit=4096, idle_seconds=0.0, now=lambda: 1.0)
    for _ in range(200):
        worlds.mint()

    failures: list[BaseException] = []
    start = threading.Barrier(8)

    def mint_hard() -> None:
        try:
            start.wait()
            for _ in range(200):
                worlds.mint()
        except BaseException as error:  # The point is precisely that nothing escapes.
            failures.append(error)

    threads = [threading.Thread(target=mint_hard) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == [], f"minting raised under concurrency: {failures[:3]!r}"


def test_a_busy_world_is_still_never_evicted() -> None:
    """The property the lock must not have changed, restated where the lock was added.

    Everything is evictable by the clock, so only ``busy`` can save this world. The run registry is
    told it has something in flight rather than actually given one: scheduling a real run would
    make this a test about the run registry, and what is being checked is that ``_forget_idle``
    still consults it while holding the lock.
    """
    worlds = SessionWorlds(a_world, limit=4, idle_seconds=0.0, now=lambda: 1.0)
    held = worlds.mint()
    worlds.world(held).runs.any_in_flight = lambda: object()  # type: ignore[method-assign]

    worlds.mint()
    assert held in worlds.sessions()


@pytest.mark.parametrize("method", ["mint", "world", "sessions", "busy"])
def test_the_public_surface_still_answers_normally(method: str) -> None:
    """The decorator is a wrapper, not a rewrite: every method still does its own job."""
    worlds = SessionWorlds(a_world, limit=4, idle_seconds=3600.0, now=lambda: 1.0)
    session = worlds.mint()

    if method == "mint":
        assert session in worlds.sessions()
    elif method == "world":
        assert worlds.world(session) is worlds.world(session)
    elif method == "sessions":
        assert worlds.sessions() == (session,)
    else:
        assert worlds.busy(session) is False
