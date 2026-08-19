"""A visitor cannot lose their external world while CareLoop still believes they are here.

``tests/test_session_isolation.py`` asks whether one visitor can see or break *another*. This asks
something narrower and worse: whether a visitor sitting in front of the demo, doing nothing wrong,
can have the clinic record of an appointment CareLoop really made deleted out from under them — so
that a read-back which should be ``MATCHED`` comes back ``ABSENT`` against a world that was
recreated empty.

**The defect this file exists for was two owners.** CareLoop's
:class:`~careloop.api.sessions.SessionWorlds` expired a world by *idle time*, refreshed on every API
request. Demo World's ``Clinics`` ordered by *clinic access* and dropped its oldest unconditionally.
A visitor polling CareLoop stayed fresh on one side and went stale on the other, and equal
capacities do not help — the disagreement is about **order**, not about size.

So the fix is not a bound. Demo World no longer removes a world on its own at all: CareLoop ends a
session's clinic when it expires it, resets it, or shuts down, and Demo World at capacity refuses a
*new* world rather than taking an existing one. With nothing evicting there is no eviction order,
and there is nothing left for the two registries to disagree about.

``TestTheCoupledLifecycleProof`` is the vacuity proof and is named to survive a tidy-up: it drives
the identical script with the coupling removed and requires the damage to happen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from clinic_helpers import clinic_server
from fastapi.testclient import TestClient
from session_helpers import isolated_client, open_session, scripted_worlds, whose

from careloop.api.deps import ApiDeps
from careloop.api.sessions import SessionWorlds, WorldEnder
from careloop.graph.verify import rehydrate
from careloop.state.memory_store import InMemoryCaseStore
from careloop.tools.clinic import ClinicRefused, ClinicUnavailable

#: Demo World with room for two session worlds, so the third arrival is the interesting one.
#:
#: The audit reproduced this with a two-entry construction and so does this file. Sixty-four worlds
#: would assert the same thing and take a minute to do it.
CROWDED = 2


@pytest.fixture
def crowded_clinic() -> Iterator[str]:
    """Demo World on a loopback socket, holding two session worlds at most.

    A real server rather than a test client, for ``clinic_helpers``' reason: the claim is about two
    processes disagreeing about who is still here, and in-process would exercise everything except
    that.
    """
    with clinic_server(limit=CROWDED) as (base_url, _):
        yield base_url


def press(client: TestClient, session_id: str) -> str:
    """Run the referral scenario in ``session_id``'s world; return the state the case settled in.

    The state rather than an assertion, because one of the things this file measures is a visitor
    whose booking *cannot* land — a clinic with no room left for them is a real outcome, and one
    with an opinion about which visitor a divergence should cost.
    """
    started = client.post("/api/demo/scenarios/referral?wait=20", headers=whose(session_id))
    assert started.status_code == 202, started.text
    return client.get("/api/cases/demo-referral", headers=whose(session_id)).json()["state"]


def book(client: TestClient, worlds: SessionWorlds, session_id: str) -> str:
    """Press the referral scenario and return the clinic's confirmation id for the booking.

    Taken from the case's own ``ActionRecord`` rather than from the clinic, because the question
    this file asks is whether the external record CareLoop *verified against* is still there
    afterwards — so the id has to come from CareLoop's side of the transaction.
    """
    assert press(client, session_id) == "DONE"

    case = worlds.world(session_id).store.get("demo-referral")
    booked = [record for record in case.actions if record.action_type == "book_appointment"]
    assert booked, case.actions
    return rehydrate("book_appointment", booked[-1].result).confirmation_id


def clinic_holds(base_url: str, session_id: str, confirmation_id: str) -> bool:
    """Whether Demo World still holds that appointment in that visitor's world."""
    read = httpx.get(f"{base_url}/clinic/appointments/{confirmation_id}?session={session_id}")
    return read.status_code == 200


class CrossedOrder:
    """What ``crossed_order`` produced, so the assertions read as the audit's own report."""

    def __init__(
        self,
        live: str,
        live_booking: str,
        gone: str,
        gone_booking: str,
        arrival: str,
        arrival_state: str,
    ) -> None:
        self.live = live
        """The visitor CareLoop is still serving."""
        self.live_booking = live_booking
        self.gone = gone
        """The visitor CareLoop expired."""
        self.gone_booking = gone_booking
        self.arrival = arrival
        """The visitor who arrived third, and whose booking is what asks for a clinic."""
        self.arrival_state = arrival_state
        """The state their case settled in. ``DONE`` means Demo World had room for them."""


def crossed_order(client: TestClient, worlds: SessionWorlds, clock: list[float]) -> CrossedOrder:
    """The audit's construction, and the whole of it.

    The first visitor books and then only ever *polls CareLoop*, so they stay fresh on CareLoop's
    clock and untouched on Demo World's. The second books and goes quiet. A third arrives once both
    have aged past the idle window: CareLoop expires the second and keeps the first, and Demo World,
    asked for a world it does not hold, has to make room.

    Which world it takes is the whole question.
    """
    first, second = open_session(client), open_session(client)
    mine, theirs = book(client, worlds, first), book(client, worlds, second)

    # Both have now aged out by CareLoop's clock; only `first` is kept alive, and it is kept alive
    # by an API read that touches no clinic. This is a judge with the dashboard open in front of
    # them, which is the entire population this defect can happen to.
    clock[0] += 3600.0
    assert client.get("/api/cases", headers=whose(first)).status_code == 200

    third = open_session(client)
    assert first in worlds.sessions(), "CareLoop expired the visitor it is still serving"
    assert second not in worlds.sessions(), "CareLoop kept a world nobody had touched in an hour"

    # The arriving visitor's own booking is what asks Demo World for a world it does not hold.
    return CrossedOrder(
        live=first,
        live_booking=mine,
        gone=second,
        gone_booking=theirs,
        arrival=third,
        arrival_state=press(client, third),
    )


def crowded_worlds(clinic_url: str, clock: list[float], **over) -> SessionWorlds:
    """CareLoop worlds against a crowded clinic, on a clock a test drives.

    The cap is deliberately *larger* than Demo World's: this is not a test about CareLoop running
    out of room, and making the two caps equal would let a coincidence stand in for the property.
    """
    return scripted_worlds(clinic_url, limit=8, idle_seconds=60.0, now=lambda: clock[0], **over)


class TestTheCrossedOrderReproducer:
    """Active on one side, the eviction victim on the other. The audit's P1.

    Two assertions rather than one, because "somebody's appointment survived" is not the property:
    what has to be true is that the world Demo World let go is the world *CareLoop let go*, and
    that the visitor CareLoop is still answering for is untouched.
    """

    def test_the_visitor_careloop_is_still_serving_keeps_their_appointment(
        self, crowded_clinic: str
    ) -> None:
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            run = crossed_order(client, worlds, clock)

            assert client.get("/api/cases", headers=whose(run.live)).status_code == 200
            assert clinic_holds(crowded_clinic, run.live, run.live_booking), (
                "the visitor CareLoop is still serving lost the appointment it verified for them"
            )

    def test_the_world_that_made_room_is_the_one_careloop_let_go(self, crowded_clinic: str) -> None:
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            run = crossed_order(client, worlds, clock)

            assert clinic_holds(crowded_clinic, run.live, run.live_booking)
            assert not clinic_holds(crowded_clinic, run.gone, run.gone_booking)

    def test_and_the_arriving_visitor_is_not_refused_to_make_that_true(
        self, crowded_clinic: str
    ) -> None:
        """Nobody pays for it. The room came back because the expired world was given back."""
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            assert crossed_order(client, worlds, clock).arrival_state == "DONE"


class TestTheCoupledLifecycleProof:
    """The vacuity proof, and the reason every test above is worth reading.

    One script, driven twice: once with CareLoop ending the clinic of the world it lets go, and once
    with that ending removed and nothing else changed. A test that passed against both would be
    asserting nothing about the coupling, and this file would be a description rather than evidence.

    **It also separates the two halves of the repair, which turn out to buy different things.** The
    *coupling* is what gives the room back: uncoupled, Demo World is still holding a world for a
    visitor CareLoop forgot an hour ago, and the next arrival cannot have one. The *refusal* — Demo
    World no longer evicting — is what decides who pays when the coupling fails: uncoupled, the
    visitor CareLoop is still serving keeps their appointment anyway, and the cost lands on the
    arriving visitor as a loud 503 instead of on the visitor already here as an appointment that
    silently disappears. Under the old design it was the other way round, which is the defect.

    So the honest reading of these two tests is: without the coupling the demo degrades, and without
    the refusal it degrades *onto the wrong person, quietly*. Both halves are load-bearing and
    neither is a restatement of the other.

    Named to survive a tidy-up. ``end_world`` is injected for exactly this — the default is the real
    one, so forgetting to pass anything gets you the coupling, and the only way to get here is to
    ask for it by name.
    """

    def script(self, clinic_url: str, **over) -> dict[str, object]:
        """Run the crossed order and hand back what happened to all three visitors.

        Read **inside** the client's lifespan, deliberately: leaving it is a shutdown, and shutdown
        ends every world's clinic. Observing afterwards would find nothing left and would report the
        coupled and the uncoupled run as identical, which is the one thing this must not do.
        """
        clock = [1000.0]
        worlds = crowded_worlds(clinic_url, clock, **over)
        with isolated_client(worlds) as client:
            run = crossed_order(client, worlds, clock)
            return {
                "live_kept": clinic_holds(clinic_url, run.live, run.live_booking),
                "expired_given_back": not clinic_holds(clinic_url, run.gone, run.gone_booking),
                "arrival_state": run.arrival_state,
            }

    def test_coupled_nobody_loses_anything(self, crowded_clinic: str) -> None:
        assert self.script(crowded_clinic) == {
            "live_kept": True,
            "expired_given_back": True,
            "arrival_state": "DONE",
        }

    def test_uncoupled_the_expired_visitors_world_is_never_given_back(
        self, crowded_clinic: str
    ) -> None:
        """The divergence, made loud. Demo World holds a world for somebody who left an hour ago.

        The arriving visitor's booking is refused by a clinic with no room, their case ends
        ``FAILED``, and the world nobody can reach is still sitting there. That is the same
        divergence the old design had; what has changed is that it can no longer be paid for by the
        visitor who is still here.
        """
        assert self.script(crowded_clinic, end_world=lambda _id, _world: None) == {
            # The world CareLoop let go is still held over there, so the room never came back.
            "expired_given_back": False,
            "arrival_state": "FAILED",
            # And the visitor who is still here keeps everything: that is the refusal, not the
            # coupling, and under the old design this was the one that went.
            "live_kept": True,
        }


class TestEveryWayAWorldGoesEndsItsClinic:
    """Expiry, reset and shutdown. Three doors out, and one lifecycle behind all of them."""

    def test_expiring_a_world_ends_its_clinic_and_leaves_the_others_alone(
        self, crowded_clinic: str
    ) -> None:
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            staying, going = open_session(client), open_session(client)
            kept, lost = book(client, worlds, staying), book(client, worlds, going)

            clock[0] += 3600.0
            assert client.get("/api/cases", headers=whose(staying)).status_code == 200
            open_session(client)

            assert not clinic_holds(crowded_clinic, going, lost)
            assert clinic_holds(crowded_clinic, staying, kept)

    def test_resetting_a_world_ends_its_clinic_and_leaves_the_others_alone(
        self, crowded_clinic: str
    ) -> None:
        """A reset puts this visitor's world back, and ending it is what putting it back means.

        The observable outcome is the one it always was — the next booking gets a freshly seeded
        world — and the difference is that the entry over there goes with it, so the two registries
        agree about who is here without anything having to stay in step.
        """
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            mine, theirs = open_session(client), open_session(client)
            kept, lost = book(client, worlds, mine), book(client, worlds, theirs)

            cleared = client.post("/api/demo/reset", headers=whose(theirs))
            assert cleared.status_code == 200
            # Reported, because this is the one place the coupling is observable from outside the
            # process — `scripts/verify_deploy.py` reads exactly this against the deployed pair.
            assert cleared.json()["clinic_world_ended"] is True

            assert not clinic_holds(crowded_clinic, theirs, lost)
            assert clinic_holds(crowded_clinic, mine, kept)

    def test_a_second_reset_reports_that_there_was_nothing_left_to_end(
        self, crowded_clinic: str
    ) -> None:
        """Which is what makes the report above worth reading: it can say either thing."""
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            visitor = open_session(client)
            book(client, worlds, visitor)

            first = client.post("/api/demo/reset", headers=whose(visitor)).json()
            second = client.post("/api/demo/reset", headers=whose(visitor)).json()

        assert first["clinic_world_ended"] is True
        assert second["clinic_world_ended"] is False

    def test_a_reset_frees_the_place_it_was_holding(self, crowded_clinic: str) -> None:
        """Which is the reason it is worth ending rather than reseeding: the room comes back."""
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            first, second = open_session(client), open_session(client)
            book(client, worlds, first)
            book(client, worlds, second)
            client.post("/api/demo/reset", headers=whose(second))

            arriving = open_session(client)
            booked = book(client, worlds, arriving)
            assert clinic_holds(crowded_clinic, arriving, booked)

    def test_shutting_down_ends_every_world(self, crowded_clinic: str) -> None:
        """A restart mints all-new ids, so a clinic left behind is unreachable and permanent.

        Driven through the lifespan the deployment uses rather than by calling the registry, because
        what is being asserted is that shutdown really is wired to this.
        """
        clock = [1000.0]
        worlds = crowded_worlds(crowded_clinic, clock)

        with isolated_client(worlds) as client:
            first, second = open_session(client), open_session(client)
            mine, theirs = book(client, worlds, first), book(client, worlds, second)
            assert clinic_holds(crowded_clinic, first, mine)

        assert not clinic_holds(crowded_clinic, first, mine)
        assert not clinic_holds(crowded_clinic, second, theirs)


def bare_worlds(end_world: WorldEnder, **over) -> SessionWorlds:
    """A registry over the cheapest worlds there are, with the ending under a test's control.

    Nothing here books anything: these are tests about what the registry does when the ending fails,
    and a real clinic would only make the failure harder to arrange.
    """
    return SessionWorlds(
        lambda _session_id: ApiDeps(store=InMemoryCaseStore()), end_world=end_world, **over
    )


class TestAFailedEndIsQueuedRatherThanSwallowed:
    """A silent failure here would be the original defect with extra steps.

    So: the eviction happens regardless, the session is *named* on :meth:`SessionWorlds.orphans`,
    and it is retried on the next mint. The divergence is transient and visible rather than
    permanent and silent — and while it lasts it costs one of Demo World's capacity slots, never a
    live visitor's world, because nothing over there evicts.
    """

    def test_careloop_still_lets_the_world_go(self) -> None:
        """Holding a world open here to repair a record over there is the wrong direction."""
        clock = [1000.0]
        worlds = bare_worlds(unreachable, limit=4, idle_seconds=60.0, now=lambda: clock[0])
        expired = worlds.mint()

        clock[0] += 61.0
        worlds.mint()

        assert expired not in worlds.sessions()

    def test_the_session_is_named_rather_than_forgotten(self) -> None:
        clock = [1000.0]
        worlds = bare_worlds(unreachable, limit=4, idle_seconds=60.0, now=lambda: clock[0])
        expired = worlds.mint()

        clock[0] += 61.0
        worlds.mint()

        assert worlds.orphans() == (expired,)

    def test_the_next_mint_retries_it_and_the_two_agree_again(self) -> None:
        attempts: list[str] = []
        answering = [False]

        def ender(session_id: str, _world: ApiDeps) -> None:
            attempts.append(session_id)
            if not answering[0]:
                raise ClinicUnavailable("the clinic could not be reached")

        clock = [1000.0]
        worlds = bare_worlds(ender, limit=4, idle_seconds=60.0, now=lambda: clock[0])
        expired = worlds.mint()

        clock[0] += 61.0
        worlds.mint()
        assert worlds.orphans() == (expired,)

        answering[0] = True
        worlds.mint()

        assert worlds.orphans() == ()
        assert attempts.count(expired) == 2

    def test_one_unreachable_clinic_costs_one_attempt_and_not_twenty(self) -> None:
        """A queue of ends against a service that is down must not cost a timeout each."""
        attempts: list[str] = []

        def ender(session_id: str, _world: ApiDeps) -> None:
            attempts.append(session_id)
            raise ClinicUnavailable("the clinic could not be reached")

        clock = [1000.0]
        worlds = bare_worlds(ender, limit=16, idle_seconds=60.0, now=lambda: clock[0])
        expired = [worlds.mint() for _ in range(5)]

        clock[0] += 61.0
        worlds.mint()

        assert len(attempts) == 1
        assert sorted(worlds.orphans()) == sorted(expired)

    def test_a_clinic_that_answers_and_declines_is_not_retried_forever(self) -> None:
        """An idempotent delete has no reason to refuse, so a refusal is a bug and not a blip."""
        attempts: list[str] = []

        def ender(session_id: str, _world: ApiDeps) -> None:
            attempts.append(session_id)
            raise ClinicRefused("the clinic refused", status_code=400)

        clock = [1000.0]
        worlds = bare_worlds(ender, limit=4, idle_seconds=60.0, now=lambda: clock[0])
        worlds.mint()

        clock[0] += 61.0
        worlds.mint()
        clock[0] += 61.0
        worlds.mint()

        assert worlds.orphans() == ()
        assert len(attempts) == 2

    def test_the_queue_is_bounded(self) -> None:
        """A clinic that never answers must not make this grow for as long as visitors arrive."""
        clock = [1000.0]
        worlds = bare_worlds(unreachable, limit=2, idle_seconds=0.0, now=lambda: clock[0])

        for _ in range(20):
            worlds.mint()

        assert len(worlds.orphans()) <= 2

    def test_a_world_with_no_clinic_has_nothing_to_end(self) -> None:
        """Three of the four effects are simulated; a world with no clinic ends cleanly."""
        clock = [1000.0]
        worlds = SessionWorlds(
            lambda _session_id: ApiDeps(store=InMemoryCaseStore()),
            limit=4,
            idle_seconds=60.0,
            now=lambda: clock[0],
        )
        worlds.mint()

        clock[0] += 61.0
        worlds.mint()

        assert worlds.orphans() == ()

    def test_shutdown_does_not_wait_out_a_clinic_that_is_gone(self) -> None:
        """Shutdown is on a deadline systemd sets; sixty-four timeouts would spend all of it."""
        attempts: list[str] = []

        def ender(session_id: str, _world: ApiDeps) -> None:
            attempts.append(session_id)
            raise ClinicUnavailable("the clinic could not be reached")

        worlds = bare_worlds(ender, limit=8, idle_seconds=3600.0, now=lambda: 1000.0)
        for _ in range(5):
            worlds.mint()

        asyncio.run(worlds.drain())

        assert len(attempts) == 1


def unreachable(_session_id: str, _world: ApiDeps) -> None:
    """A clinic that cannot be reached at all. The failure the retry queue exists for."""
    raise ClinicUnavailable("the clinic could not be reached")
