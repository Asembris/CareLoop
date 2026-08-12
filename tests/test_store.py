"""Conformance suite run against every CaseStore implementation.

Both stores are exercised by the same tests via the parametrised ``store`` fixture, so SQLite and
the in-memory store cannot drift apart — the in-memory store is only trustworthy as a test double
for as long as it behaves identically.
"""

from __future__ import annotations

import threading
from typing import ClassVar

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


def test_clear_removes_every_case_and_reports_how_many(store: CaseStore, new_case: Case) -> None:
    """The demo reset's one primitive. See ``CaseStore.clear`` for who may call it."""
    store.create(new_case)
    store.create(new_case.model_copy(update={"case_id": "case-0002"}))

    assert store.clear() == 2

    for case_id in (new_case.case_id, "case-0002"):
        with pytest.raises(CaseNotFound):
            store.get(case_id)
    assert store.list_by_state(new_case.state) == []


def test_clear_on_an_empty_store_removes_nothing(store: CaseStore) -> None:
    """Pressing reset twice is not an error; it is the same known state twice."""
    assert store.clear() == 0


def test_a_cleared_store_accepts_the_same_case_id_again(store: CaseStore, new_case: Case) -> None:
    """What the demo needs: press a scenario, reset, press it again."""
    store.create(new_case)
    store.clear()
    assert store.create(new_case).case_id == new_case.case_id


def test_clear_leaves_no_timeline_behind(store: CaseStore, new_case: Case) -> None:
    """The case and its timeline are one fact, and half of it is worse than none of it."""
    store.create(new_case)
    store.save(
        transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system"),
        expected_version=0,
    )
    store.clear()

    with pytest.raises(CaseNotFound):
        store.timeline(new_case.case_id)

    store.create(new_case)
    assert store.timeline(new_case.case_id) == []


class TestTheStoreCrossesThreads:
    """A served CareLoop reaches the store from a worker thread, and SQLite minds.

    Found in session C by running a real server rather than by reading the code: Starlette runs
    synchronous route handlers in a thread pool, so ``GET /api/cases`` arrives on a different thread
    from the one that opened the connection, and ``sqlite3`` refused it with a 500. Every test until
    then drove the API over the in-memory store and SQLite from one thread, so the combination a
    deployment actually uses had never been exercised — which is why these are here rather than a
    line in the docstring.
    """

    @staticmethod
    def _in_a_thread(work):
        outcome: list[object] = []

        def run() -> None:
            try:
                outcome.append(work())
            except BaseException as failure:
                outcome.append(failure)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)
        assert outcome, "the worker thread produced nothing"
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    def test_a_case_written_on_one_thread_reads_back_on_another(
        self, store: CaseStore, new_case: Case
    ) -> None:
        store.create(new_case)
        loaded = self._in_a_thread(lambda: store.get(new_case.case_id))
        assert loaded.case_id == new_case.case_id

    def test_a_case_written_on_another_thread_reads_back_here(
        self, store: CaseStore, new_case: Case
    ) -> None:
        self._in_a_thread(lambda: store.create(new_case))
        assert store.get(new_case.case_id).case_id == new_case.case_id

    def test_every_read_survives_the_crossing(self, store: CaseStore, new_case: Case) -> None:
        """The route handlers that broke were the list and the timeline, not only ``get``."""
        store.create(new_case)
        store.save(
            transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system"),
            expected_version=0,
        )

        assert (
            self._in_a_thread(lambda: len(store.list_by_state(CaseState.GATHERING_EVIDENCE))) == 1
        )
        assert self._in_a_thread(lambda: len(store.timeline(new_case.case_id))) == 1


class TestEveryStoreMethodIsSerialised:
    """The lock is not optional on any of them, and a new method must not be the exception.

    The writes are explicit ``BEGIN IMMEDIATE`` … ``COMMIT`` sequences on one connection: two of
    them interleaving would commit half of each, which is worse than the crash that
    ``check_same_thread`` was giving. Asserted on the decorator rather than by racing threads,
    because a race that happens to pass is not evidence.
    """

    METHODS: ClassVar[tuple[str, ...]] = (
        "get",
        "list_by_state",
        "timeline",
        "create",
        "save",
        "clear",
        "close",
    )

    def test_the_list_covers_the_protocol(self) -> None:
        """Vacuity: a method missing from this list is a method the sweep never looks at."""
        protocol_methods = {
            name
            for name in dir(CaseStore)
            if not name.startswith("_") and callable(getattr(CaseStore, name))
        }
        assert protocol_methods == set(self.METHODS)

    def test_every_one_of_them_holds_the_lock(self) -> None:
        unguarded = [
            name
            for name in self.METHODS
            if not hasattr(getattr(SqliteCaseStore, name), "__wrapped__")
        ]
        assert unguarded == []

    def test_the_detector_notices_an_undecorated_method(self) -> None:
        """Vacuity: ``__wrapped__`` is the decorator's fingerprint, so a plain method has none."""

        class Undecorated:
            def get(self) -> None: ...

        assert not hasattr(Undecorated.get, "__wrapped__")


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
