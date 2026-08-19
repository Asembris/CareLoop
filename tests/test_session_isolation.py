"""One visitor cannot observe or destroy another visitor's state.

The deployed demo was one world: four fixed case ids, one store, one clinic, and a reset any
visitor could fire. Two judges opening the URL during a three-week judging window is ordinary, and
the failure was silent — B's reset wiped A's settled case, A's decision card then 404ed, and both
raced for the one seeded slot.

**These tests are about the property, not the mechanism.** They open two sessions, run the demo
through them, and ask whether either can see or break the other. The mechanism — a world per
session — is asserted only where it is the thing that makes the property structural rather than
careful.

``TestTheIsolationProof`` is the vacuity proof and is named to survive a tidy-up: it drives the
identical script against a single-world app and requires the damage to happen. A test that passes
against both shapes would be testing nothing.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from api_helpers import CARER, factory_proposing
from fastapi.testclient import TestClient
from session_helpers import (
    isolated_client,
    open_session,
    scripted_factory,
    scripted_worlds,
    whose,
)

import demoworld
from careloop.api.app import build_dependencies, create_app
from careloop.api.deps import ApiDeps
from careloop.api.sessions import (
    CLINIC_SESSION_HEADER,
    DEFAULT_SESSION_LIMIT,
    SESSION_HEADER,
    SessionsAtCapacity,
    SessionWorlds,
    UnknownSession,
    demo_session_worlds,
)
from careloop.state import CaseState
from careloop.state.sqlite_store import SqliteCaseStore


def session_worlds(*proposals, **over) -> SessionWorlds:
    """Worlds whose agents are scripted, so a test drives the real graph on the mock model.

    The store is the deployed one — SQLite, in memory — rather than ``InMemoryCaseStore``, because
    that is what :func:`careloop.api.sessions.demo_session_worlds` builds and session C's lesson was
    that the combination a deployment uses is the one nothing had exercised.
    """

    def build(session_id: str) -> ApiDeps:
        return ApiDeps(
            store=SqliteCaseStore(":memory:"),
            context_factory=factory_proposing(*proposals),
        )

    return SessionWorlds(build, **over)


class TestTwoVisitorsAtOnce:
    """The whole target: two people use the demo simultaneously and neither notices the other."""

    def test_neither_sees_the_others_cases(self) -> None:
        with isolated_client(scripted_worlds()) as client:
            first, second = open_session(client), open_session(client)

            client.post("/api/demo/scenarios/resume?wait=10", headers=whose(first))
            client.post("/api/demo/scenarios/referral?wait=10", headers=whose(second))

            mine = client.get("/api/cases", headers=whose(first)).json()
            theirs = client.get("/api/cases", headers=whose(second)).json()

        assert [card["case_id"] for card in mine["needs_you"]] == ["demo-resume"]
        assert mine["completed"] == []
        assert [card["case_id"] for card in theirs["completed"]] == ["demo-referral"]
        assert theirs["needs_you"] == []

    def test_a_case_belonging_to_another_visitor_is_a_404(self) -> None:
        """Not a redacted projection — the case is not in this visitor's store to project."""
        with isolated_client(scripted_worlds()) as client:
            first, second = open_session(client), open_session(client)
            client.post("/api/demo/scenarios/referral?wait=10", headers=whose(first))

            assert client.get("/api/cases/demo-referral", headers=whose(first)).status_code == 200
            assert client.get("/api/cases/demo-referral", headers=whose(second)).status_code == 404

    def test_scenarios_pressed_at_the_same_moment_both_settle(self) -> None:
        """Two judges pressing together, which is the case the shared demo could not survive."""
        with isolated_client(scripted_worlds()) as client:
            sessions = [open_session(client), open_session(client), open_session(client)]

            def press(session_id: str) -> int:
                return client.post(
                    "/api/demo/scenarios/referral?wait=20", headers=whose(session_id)
                ).status_code

            with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
                statuses = list(pool.map(press, sessions))

            assert statuses == [202] * len(sessions)
            for session_id in sessions:
                detail = client.get("/api/cases/demo-referral", headers=whose(session_id)).json()
                assert detail["state"] == "DONE"


class TestAResetReachesOneWorldOnly:
    """A reset is the sharpest edge on the demo: it deletes every case it can reach.

    Under isolation it can reach one visitor's cases, and that is structural rather than checked —
    ``deps.store`` *is* the world the request named, so there is no other store in the process a
    route could pass to ``clear()``.
    """

    def test_a_reset_leaves_the_other_visitors_settled_case_alone(self) -> None:
        with isolated_client(scripted_worlds()) as client:
            first, second = open_session(client), open_session(client)
            client.post("/api/demo/scenarios/referral?wait=10", headers=whose(first))

            cleared = client.post("/api/demo/reset", headers=whose(second))
            assert cleared.status_code == 200
            assert cleared.json()["cases_cleared"] == 0

            kept = client.get("/api/cases/demo-referral", headers=whose(first))
            assert kept.status_code == 200
            assert kept.json()["state"] == "DONE"

    def test_a_pending_decision_survives_another_visitors_reset_and_is_still_decidable(
        self,
    ) -> None:
        """The failure that made this worst: a stale card whose Approve silently 404s."""
        with isolated_client(scripted_worlds()) as client:
            first, second = open_session(client), open_session(client)
            client.post("/api/demo/scenarios/resume?wait=10", headers=whose(first))

            parked = client.get("/api/cases/demo-resume", headers=whose(first)).json()
            approval_id = parked["decision"]["approval_id"]
            assert parked["state"] == "AWAITING_HUMAN"

            client.post("/api/demo/reset", headers=whose(second))
            client.post("/api/demo/scenarios/referral?wait=10", headers=whose(second))

            decided = client.post(
                f"/api/cases/demo-resume/decisions/{approval_id}/approve?wait=10",
                headers={**whose(first), "X-Caregiver": CARER},
            )

        assert decided.status_code == 202


class TestTheIsolationProof:
    """The vacuity proof, and the reason every test above is worth reading.

    One script, driven twice: once against an app that gives each visitor a world, and once against
    an app that serves one world to everybody — which is what the deployed demo was. The isolated
    run must survive it and the shared run must not. A test that passed against both would be
    asserting nothing about isolation, and this file would be a description rather than evidence.

    The damage is the one that was actually reachable on the public URL: visitor B presses a
    scenario, which resets the world it can reach, and visitor A's parked case — with a decision
    card on screen and an Approve button under a judge's cursor — is gone.
    """

    def script(self, client: TestClient, first: dict[str, str], second: dict[str, str]) -> int:
        """A parks for a decision; B presses a scenario; A tries to decide. Returns A's status."""
        client.post("/api/demo/scenarios/resume?wait=10", headers=first)
        parked = client.get("/api/cases/demo-resume", headers=first).json()
        approval_id = parked["decision"]["approval_id"]

        client.post("/api/demo/scenarios/referral?wait=10", headers=second)

        return client.post(
            f"/api/cases/demo-resume/decisions/{approval_id}/approve?wait=10",
            headers={**first, "X-Caregiver": CARER},
        ).status_code

    def test_worlds_apart_the_decision_still_stands(self) -> None:
        with isolated_client(scripted_worlds()) as client:
            first, second = whose(open_session(client)), whose(open_session(client))
            assert self.script(client, first, second) == 202

    def test_one_world_between_them_destroys_it(self) -> None:
        """Namespacing removed. The same script, and the caregiver's decision is a 404.

        This is the deployed defect reproduced rather than described: nothing errors, nothing is
        logged, and the judge who lost the race sees a decision card whose button does nothing.
        """
        store = SqliteCaseStore(":memory:")
        app = create_app(store, context_factory=scripted_factory(), demo=True)
        with TestClient(app) as client:
            assert self.script(client, {}, {}) == 404
            assert store.list_by_state(CaseState.AWAITING_HUMAN) == []


class TestTwoWorldsBookAgainstTwoClinics:
    """One seeded slot each, booked over real HTTP, verified by reading each clinic back.

    The clinic is where a shared demo hurt most: every scenario books the slot the referral offers,
    so the second visitor got a correct ``SlotAlreadyTaken`` and a ``FAILED`` case that reads like
    the agent breaking. Driven against Demo World on a loopback socket, because the claim is about
    two visitors reaching a real external service — in-process would exercise everything except
    the thing being asserted.
    """

    def test_both_visitors_book_the_same_slot_and_both_are_verified(
        self, clinic_service, clinic_state
    ) -> None:
        clinic, _ = clinic_service
        with isolated_client(scripted_worlds(clinic)) as client:
            first, second = open_session(client), open_session(client)

            def press(session_id: str) -> int:
                return client.post(
                    "/api/demo/scenarios/referral?wait=20", headers=whose(session_id)
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(press, [first, second]))

            assert statuses == [202, 202]
            for session_id in (first, second):
                detail = client.get("/api/cases/demo-referral", headers=whose(session_id)).json()
                assert detail["state"] == "DONE"
                verified = [
                    event for event in detail["timeline"] if event["kind"] == "VERIFICATION_PASSED"
                ]
                # Read back against the clinic itself, not against CareLoop's own receipt.
                assert verified and verified[-1]["payload"]["read_back"] == "true"

            # Each world holds one appointment and one fewer free slot: two bookings, two clinics.
            for session_id in (first, second):
                free = httpx.get(f"{clinic}/clinic/slots?session={session_id}").json()["slots"]
                assert len(free) == len(demoworld.SEEDED_SLOTS) - 1

            # And the untouched default world proves the two above were genuinely separate.
            assert len(httpx.get(f"{clinic}/clinic/slots").json()["slots"]) == len(
                demoworld.SEEDED_SLOTS
            )


class TestTheTwoModesAreExclusive:
    """An app is bound to one store or to a registry of them, never to both.

    The isolation is that an isolated app has **no app-scoped store at all**. A shared store that
    exists alongside the worlds is a shared store some future route reaches by forgetting to name a
    session, which is precisely the class of defect this session exists to close.
    """

    def test_an_isolated_app_holds_no_dependencies_of_its_own(self) -> None:
        worlds = session_worlds()
        app = create_app(sessions=worlds)
        assert app.state.careloop is worlds
        assert not isinstance(app.state.careloop, ApiDeps)

    def test_a_single_world_app_is_unchanged(self) -> None:
        app = create_app(SqliteCaseStore(":memory:"))
        assert isinstance(app.state.careloop, ApiDeps)

    def test_neither_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            create_app()

    def test_both_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            create_app(SqliteCaseStore(":memory:"), sessions=session_worlds())

    @pytest.mark.parametrize(
        "collaborator",
        ["events", "context_factory", "graph_runner", "clinic"],
    )
    def test_a_per_world_collaborator_alongside_sessions_is_refused(
        self, collaborator: str
    ) -> None:
        """Each of these belongs to the builder. Bound here, it would be bound to nothing."""
        arguments: dict[str, object] = {
            "sessions": session_worlds(),
            "events": None,
            "context_factory": None,
            "graph_runner": None,
            "clinic": None,
            "demo": False,
            collaborator: object(),
        }
        with pytest.raises(ValueError, match="belongs to the builder"):
            build_dependencies(None, **arguments)  # type: ignore[arg-type]


class TestBeingGivenAWorld:
    """Judges do nothing: the first request the page makes mints one, and that is the whole ritual.

    A missing session is a **400** — the ``X-Caregiver`` reasoning, a required field is absent —
    and never a 401, which would claim a check that is not happening. Nothing here is verified.
    """

    def test_minting_returns_an_opaque_id(self) -> None:
        with isolated_client(session_worlds()) as client:
            minted = open_session(client)
            assert len(minted) >= 16
            assert minted not in {"", "default"}

    def test_two_visitors_are_given_different_worlds(self) -> None:
        worlds = session_worlds()
        with isolated_client(worlds) as client:
            first, second = open_session(client), open_session(client)
            assert first != second
            assert worlds.world(first).store is not worlds.world(second).store

    def test_a_request_naming_no_session_is_refused_with_400(self) -> None:
        with isolated_client(session_worlds()) as client:
            refused = client.get("/api/cases")
            assert refused.status_code == 400
            assert SESSION_HEADER in refused.json()["detail"]

    def test_a_request_naming_a_world_that_is_gone_is_410(self) -> None:
        """Its own status, used nowhere else, so a client re-mints on exactly this."""
        with isolated_client(session_worlds()) as client:
            refused = client.get("/api/cases", headers=whose("never-minted-here"))
            assert refused.status_code == 410

    def test_health_needs_no_session(self) -> None:
        """Liveness is about the process, not about a visitor."""
        with isolated_client(session_worlds()) as client:
            assert client.get("/api/health").status_code == 200

    def test_a_single_world_app_has_no_session_route(self) -> None:
        """404 rather than an unusable route: a frontend reads that as "carry no session"."""
        with TestClient(create_app(SqliteCaseStore(":memory:"))) as client:
            assert client.post("/api/sessions").status_code == 404
            assert client.get("/api/cases").status_code == 200


class TestWorldsAreBounded:
    """A demo host is a ``t4g.small``. Unbounded worlds is a memory leak with a nice story."""

    def test_minting_past_the_limit_is_refused_rather_than_taking_somebody_elses(self) -> None:
        worlds = session_worlds(limit=2)
        worlds.mint()
        worlds.mint()

        with pytest.raises(SessionsAtCapacity):
            worlds.mint()

    def test_capacity_is_a_503(self) -> None:
        with isolated_client(session_worlds(limit=1)) as client:
            open_session(client)
            refused = client.post("/api/sessions")
            assert refused.status_code == 503

    def test_an_idle_world_is_forgotten_and_its_place_reused(self) -> None:
        clock = [1000.0]
        worlds = session_worlds(limit=1, idle_seconds=60.0, now=lambda: clock[0])
        stale = worlds.mint()

        clock[0] += 61.0
        fresh = worlds.mint()

        assert worlds.sessions() == (fresh,)
        with pytest.raises(UnknownSession):
            worlds.world(stale)

    def test_a_world_still_in_use_is_not_forgotten(self) -> None:
        clock = [1000.0]
        worlds = session_worlds(limit=4, idle_seconds=60.0, now=lambda: clock[0])
        first = worlds.mint()

        clock[0] += 30.0
        worlds.world(first)
        clock[0] += 40.0
        worlds.mint()

        assert first in worlds.sessions()

    def test_an_evicted_visitor_gets_the_same_clean_410(self) -> None:
        """Evicted mid-visit is the same answer as never minted: re-mint and start again.

        The world is dropped rather than closed, so a request that had already resolved one cannot
        find its store shut underneath it — that would be a 500 from inside SQLite where a 410 was
        the honest answer.
        """
        clock = [1000.0]
        worlds = session_worlds(limit=1, idle_seconds=60.0, now=lambda: clock[0])
        with isolated_client(worlds) as client:
            evicted = open_session(client)
            clock[0] += 61.0
            open_session(client)

            gone = client.get("/api/cases", headers=whose(evicted))
            assert gone.status_code == 410
            assert client.post("/api/demo/reset", headers=whose(evicted)).status_code == 410


class TestNeitherRegistryOutlivesTheOther:
    """CareLoop may not hold more worlds than Demo World does. Defence in depth, and no more.

    This inequality used to be the *whole* guard on the two registries agreeing, and it was a number
    standing in for a lifecycle. Equal capacities do not synchronise eviction **order**, and order
    was the disagreement: CareLoop expired by idle time refreshed on every API request, Demo World
    dropped its least-recently-touched *clinic*, and a visitor polling the dashboard was alive on
    one side and the victim on the other. ``tests/test_session_lifecycle.py`` owns the property now
    — CareLoop ends a session's clinic when it lets the session go, and Demo World removes a world
    only when told to.

    Kept because it is still true and still worth being true: with the coupling working, Demo World
    holds at most what CareLoop holds, so the clinic is never the binding constraint on how many
    judges can be looking at the demo at once.
    """

    def test_careloop_holds_no_more_worlds_than_the_clinic_does(self) -> None:
        assert DEFAULT_SESSION_LIMIT <= demoworld.DEFAULT_WORLD_LIMIT


class TestTheClinicHeaderDoesNotDrift:
    """CareLoop may not import Demo World, so the header is spelled twice.

    The same arrangement as the seeded slot the referral names: a disagreement has to be loud,
    because a silent one would scope CareLoop's bookings to a world nobody is looking at.
    """

    def test_both_sides_spell_it_the_same_way(self) -> None:
        assert CLINIC_SESSION_HEADER == demoworld.SESSION_HEADER

    def test_a_built_world_sends_it(self) -> None:
        worlds = demo_session_worlds(clinic_url="http://127.0.0.1:1/")
        session_id = worlds.mint()
        clinic = worlds.world(session_id).clinic

        assert clinic is not None
        assert clinic._client.headers[CLINIC_SESSION_HEADER] == session_id

    def test_each_world_gets_its_own_transport(self) -> None:
        """One shared client rewritten per world would have the last one built win for all."""
        worlds = demo_session_worlds(clinic_url="http://127.0.0.1:1/")
        first, second = worlds.mint(), worlds.mint()

        one, two = worlds.world(first).clinic, worlds.world(second).clinic
        assert one is not None and two is not None
        assert one._client.headers[CLINIC_SESSION_HEADER] == first
        assert two._client.headers[CLINIC_SESSION_HEADER] == second

    def test_no_clinic_is_bound_without_a_url(self) -> None:
        """Every effect simulated, exactly as a single-world app with no clinic behaves."""
        worlds = demo_session_worlds()
        assert worlds.world(worlds.mint()).clinic is None
