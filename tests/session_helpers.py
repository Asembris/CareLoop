"""Worlds shaped like the deployed ones, with scripted agents in place of a model.

Shared by the two files that drive per-visitor isolation end to end —
``tests/test_session_isolation.py``, which asks whether one visitor can see or break another, and
``tests/test_session_lifecycle.py``, which asks whether a visitor can lose the *external* world
CareLoop believes they still have. Both need the same composition: the deployed store, a real
clinic client per world, and a planner that proposes what the case it is planning for needs.

Kept out of ``conftest.py`` for the reason ``helpers.py`` is: test modules import this, and pytest
loads ``conftest.py`` rather than exporting it.

**The script is keyed on the case rather than on the order presses arrive in**, so the *same* script
drives an isolated app, a single-world app, and a run against a clinic that is about to be crowded.
Two runs then differ in exactly one thing, which is what makes a proof out of a pair of tests.
"""

from __future__ import annotations

import httpx
from api_helpers import EMAIL, intent, planner_proposing, triage_agent
from fastapi.testclient import TestClient
from helpers import FIXED_NOW

from careloop.api.app import create_app
from careloop.api.demo import DEMO_SCENARIOS
from careloop.api.deps import ApiDeps
from careloop.api.sessions import CLINIC_SESSION_HEADER, SESSION_HEADER, SessionWorlds
from careloop.graph.runner import context_for
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import DEFAULT_TIMEOUT, ClinicClient

BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": "cardiology review",
}

#: What the planner proposes, by the case it is planning for.
SCRIPTS = {
    DEMO_SCENARIOS["referral"].case_id: intent("book_appointment", **BOOKING),
    DEMO_SCENARIOS["resume"].case_id: intent("send_email", **EMAIL),
    DEMO_SCENARIOS["injection"].case_id: intent("book_appointment", **BOOKING),
}


def scripted_factory(clinic: ClinicClient | None = None):
    """A context factory whose planner proposes what the case it is planning for needs."""

    def build(store, inbound, case_id: str):
        return context_for(
            store,
            inbound,
            case_id=case_id,
            triage=triage_agent(),
            planner=planner_proposing(SCRIPTS[case_id]),
            now=lambda: FIXED_NOW,
            clinic=clinic,
        )

    return build


def scripted_worlds(clinic_url: str = "", **over) -> SessionWorlds:
    """Worlds shaped like the deployed ones, with scripted agents in place of a model.

    The store is the deployed one — SQLite, in memory — rather than ``InMemoryCaseStore``, because
    that is what :func:`careloop.api.sessions.demo_session_worlds` builds and session C's lesson was
    that the combination a deployment uses is the one nothing had exercised.

    ``over`` reaches :class:`~careloop.api.sessions.SessionWorlds` itself, so a test can pin the
    cap, the idle window and the clock.
    """

    def build(session_id: str) -> ApiDeps:
        clinic = (
            ClinicClient(
                clinic_url,
                client=httpx.Client(
                    timeout=DEFAULT_TIMEOUT, headers={CLINIC_SESSION_HEADER: session_id}
                ),
            )
            if clinic_url
            else None
        )
        return ApiDeps(
            store=SqliteCaseStore(":memory:"),
            context_factory=scripted_factory(clinic),
            clinic=clinic,
        )

    return SessionWorlds(build, **over)


def isolated_client(worlds: SessionWorlds) -> TestClient:
    return TestClient(create_app(sessions=worlds, demo=True))


def open_session(client: TestClient) -> str:
    minted = client.post("/api/sessions")
    assert minted.status_code == 201
    return minted.json()["session"]


def whose(session_id: str) -> dict[str, str]:
    return {SESSION_HEADER: session_id}


__all__ = [
    "BOOKING",
    "SCRIPTS",
    "isolated_client",
    "open_session",
    "scripted_factory",
    "scripted_worlds",
    "whose",
]
