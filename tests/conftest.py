"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import Case, CaseState, InMemoryCaseStore, SqliteCaseStore

FIXED_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


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


@pytest.fixture
def advance() -> object:
    """Return a helper producing distinct, ordered timestamps."""
    counter = {"n": 0}

    def _next() -> datetime:
        counter["n"] += 1
        return datetime(2026, 8, 11, 12, counter["n"] % 60, 0, tzinfo=UTC)

    return _next


def make_case(case_id: str = "case-0001", state: CaseState = CaseState.NEW) -> Case:
    """Build a case directly in ``state`` for table-driven tests."""
    return Case(
        case_id=case_id,
        event_id="evt-0001",
        state=state,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
