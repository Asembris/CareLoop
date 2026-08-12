"""Shared fixtures.

Fixtures only. Constructors that test modules import live in :mod:`tests.helpers` — see the note
there on why importing this module by name is a trap.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from clinic_helpers import clinic_server
from helpers import FIXED_NOW

from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import Case, InMemoryCaseStore, SqliteCaseStore
from careloop.tools.clinic import ClinicClient
from demoworld.clinic import Clinic


@pytest.fixture
def new_case() -> Case:
    """A freshly opened case, pinned to a fixed clock for deterministic timelines."""
    return Case(
        case_id="case-0001",
        event_id="evt-0001",
        summary="Clinic referral for cardiology follow-up",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


@pytest.fixture
def untrusted_fact() -> Fact:
    """A fact lifted straight out of document text."""
    return Fact(
        key="appointment_date",
        value="2026-09-03",
        trust_level=TrustLevel.UNTRUSTED,
        provenance=Provenance(source_doc="referral.pdf", page=2, extractor="mock_extractor"),
        extracted_at=FIXED_NOW,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path) -> Iterator[object]:
    """Every store implementation, so the conformance suite runs against both."""
    if request.param == "memory":
        made = InMemoryCaseStore()
    else:
        made = SqliteCaseStore(tmp_path / "careloop.sqlite3")
    yield made
    made.close()


@pytest.fixture(scope="session")
def clinic_service() -> Iterator[tuple[str, Clinic]]:
    """Demo World on a loopback port, started once for the whole session.

    Session-scoped because binding a socket per test would be the slowest thing in the suite for no
    added confidence; the world is reset between tests instead, which is also what the demo's own
    reset button does.
    """
    with clinic_server() as bound:
        yield bound


@pytest.fixture
def clinic_state(clinic_service: tuple[str, Clinic]) -> Clinic:
    """The clinic's state, reset. Held so a test can move an appointment out from under CareLoop."""
    _, world = clinic_service
    world.reset()
    return world


@pytest.fixture
def clinic(clinic_service: tuple[str, Clinic], clinic_state: Clinic) -> Iterator[ClinicClient]:
    """A CareLoop client pointed at the running Demo World, over a real socket."""
    base_url, _ = clinic_service
    client = ClinicClient(base_url)
    yield client
    client.close()


@pytest.fixture
def advance() -> object:
    """Return a helper producing distinct, ordered timestamps."""
    counter = {"n": 0}

    def _next() -> datetime:
        counter["n"] += 1
        return FIXED_NOW.replace(minute=counter["n"] % 60)

    return _next
