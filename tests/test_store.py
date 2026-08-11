"""Conformance suite run against every CaseStore implementation.

Both stores are exercised by the same tests via the parametrised ``store`` fixture, so SQLite and
the in-memory store cannot drift apart — the in-memory store is only trustworthy as a test double
for as long as it behaves identically.
"""

from __future__ import annotations

import pytest

from careloop.state import (
    Case,
    CaseAlreadyExists,
    CaseNotFound,
    CaseState,
    CaseStore,
    SqliteCaseStore,
    TimelineKind,
    VersionConflict,
    append_event,
    transition,
)


def test_store_satisfies_the_protocol(store: CaseStore) -> None:
    assert isinstance(store, CaseStore)


def test_create_then_get_round_trips(store: CaseStore, new_case: Case) -> None:
    store.create(new_case)
    loaded = store.get(new_case.case_id)
    assert loaded.case_id == new_case.case_id
    assert loaded.state is new_case.state
    assert loaded.summary == new_case.summary


def test_get_unknown_case_raises(store: CaseStore) -> None:
    with pytest.raises(CaseNotFound):
        store.get("case-does-not-exist")


def test_create_twice_raises(store: CaseStore, new_case: Case) -> None:
    store.create(new_case)
    with pytest.raises(CaseAlreadyExists):
        store.create(new_case)


def test_save_persists_a_transition(store: CaseStore, new_case: Case) -> None:
    store.create(new_case)
    moved = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system")
    store.save(moved, expected_version=new_case.version)
    assert store.get(new_case.case_id).state is CaseState.GATHERING_EVIDENCE


def test_save_with_stale_version_raises(store: CaseStore, new_case: Case) -> None:
    """Two writers read version 0; the second to save must lose."""
    store.create(new_case)
    writer_a = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="a", actor="t")
    writer_b = transition(new_case, CaseState.FAILED, reason="b", actor="t")

    store.save(writer_a, expected_version=0)
    with pytest.raises(VersionConflict) as caught:
        store.save(writer_b, expected_version=0)

    assert caught.value.expected == 0
    assert caught.value.actual == writer_a.version
    assert store.get(new_case.case_id).state is CaseState.GATHERING_EVIDENCE


def test_save_unknown_case_raises(store: CaseStore, new_case: Case) -> None:
    with pytest.raises(CaseNotFound):
        store.save(new_case, expected_version=0)


def test_list_by_state(store: CaseStore, new_case: Case) -> None:
    store.create(new_case)
    other = Case(case_id="case-0002", event_id="evt-0002")
    store.create(other)
    moved = transition(other, CaseState.GATHERING_EVIDENCE, reason="x", actor="t")
    store.save(moved, expected_version=other.version)

    assert [case.case_id for case in store.list_by_state(CaseState.NEW)] == ["case-0001"]
    assert [case.case_id for case in store.list_by_state(CaseState.GATHERING_EVIDENCE)] == [
        "case-0002"
    ]
    assert store.list_by_state(CaseState.DONE) == []


def test_timeline_is_append_only_across_saves(store: CaseStore, new_case: Case) -> None:
    store.create(new_case)
    case = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system")
    store.save(case, expected_version=0)
    case = append_event(case, kind=TimelineKind.EVIDENCE_RECORDED, actor="tools", summary="fields")
    store.save(case, expected_version=1)
    case = transition(case, CaseState.PLANNING, reason="evidence complete", actor="graph")
    store.save(case, expected_version=2)

    events = store.timeline(new_case.case_id)
    assert [event.seq for event in events] == [0, 1, 2]
    assert [event.kind for event in events] == [
        TimelineKind.STATE_TRANSITION,
        TimelineKind.EVIDENCE_RECORDED,
        TimelineKind.STATE_TRANSITION,
    ]


def test_timeline_survives_reload(store: CaseStore, new_case: Case) -> None:
    store.create(new_case)
    case = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system")
    store.save(case, expected_version=0)
    assert len(store.get(new_case.case_id).timeline) == 1


def test_facts_and_provenance_survive_a_round_trip(
    store: CaseStore, new_case: Case, untrusted_fact
) -> None:
    store.create(new_case.model_copy(update={"facts": (untrusted_fact,)}))
    loaded = store.get(new_case.case_id)
    assert len(loaded.facts) == 1
    assert loaded.facts[0].provenance.source_doc == "referral.pdf"
    assert loaded.facts[0].provenance.page == 2
    assert loaded.facts[0].trust_level is untrusted_fact.trust_level


def test_sqlite_persists_across_connections(tmp_path, new_case: Case) -> None:
    """The durability claim, checked against a real file rather than a live object."""
    path = tmp_path / "careloop.sqlite3"
    first = SqliteCaseStore(path)
    first.create(new_case)
    first.save(
        transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system"),
        expected_version=0,
    )
    first.close()

    second = SqliteCaseStore(path)
    try:
        reloaded = second.get(new_case.case_id)
        assert reloaded.state is CaseState.GATHERING_EVIDENCE
        assert len(second.timeline(new_case.case_id)) == 1
    finally:
        second.close()


def test_sqlite_rolls_back_a_conflicted_write(tmp_path, new_case: Case) -> None:
    """A losing compare-and-set must leave no timeline rows behind."""
    store = SqliteCaseStore(tmp_path / "careloop.sqlite3")
    try:
        store.create(new_case)
        winner = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="a", actor="t")
        loser = transition(new_case, CaseState.FAILED, reason="b", actor="t")
        store.save(winner, expected_version=0)
        with pytest.raises(VersionConflict):
            store.save(loser, expected_version=0)

        events = store.timeline(new_case.case_id)
        assert len(events) == 1
        assert events[0].payload["to"] == CaseState.GATHERING_EVIDENCE
    finally:
        store.close()
