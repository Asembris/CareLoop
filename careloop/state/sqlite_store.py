"""SQLite implementation of :class:`careloop.state.store.CaseStore`.

The case document is stored as JSON in one row; the timeline is *also* projected into its own table.
That duplication is deliberate. The JSON blob keeps load/save trivial and schema-flexible while the
model is still moving, and the projected table makes the audit trail queryable without deserialising
every case — which is what resume and timeline inspection will want.

Compare-and-set is a conditional ``UPDATE ... WHERE version = ?`` inside a transaction, so a losing
writer is rejected by the database rather than by a check-then-act race in Python.

**One connection, shared across threads, serialised by a lock.** ``sqlite3`` refuses a connection
used from a thread other than the one that created it unless told otherwise, and a served CareLoop
crosses threads constantly: Starlette runs synchronous route handlers in a worker pool, so
``GET /api/cases`` reaches the store from a different thread than the one that built it. Session C
found that by running the thing rather than by reading it — every test until then drove the API
over the in-memory store and SQLite from a single thread, so the combination that a deployment
actually uses had never been exercised.

``check_same_thread=False`` alone would not be enough and would be worse than the crash: the writes
here are explicit ``BEGIN IMMEDIATE`` ... ``COMMIT`` sequences, and two of those interleaving on one
connection would commit half of each. The lock is therefore load-bearing rather than defensive, and
it wraps whole operations rather than statements. It does not replace the conditional update — the
database is still what rejects a losing writer, which is what keeps the guarantee true for a second
process the lock knows nothing about.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import cast

from careloop.state.case import Case, CaseState, TimelineEvent
from careloop.state.store import CaseAlreadyExists, CaseNotFound, VersionConflict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id     TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    document    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_state ON cases (state, created_at);

CREATE TABLE IF NOT EXISTS timeline (
    case_id     TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    at          TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    diagnostic  TEXT,
    payload     TEXT    NOT NULL,
    PRIMARY KEY (case_id, seq),
    FOREIGN KEY (case_id) REFERENCES cases (case_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_timeline_kind ON timeline (case_id, kind);
"""


def serialised[M: Callable[..., object]](method: M) -> M:
    """Run ``method`` holding the store's lock.

    Applied to every method of the protocol rather than to the writes alone. A read that ran
    between a write's ``BEGIN IMMEDIATE`` and its ``COMMIT`` would see a case mid-transaction, and
    "the store is the single source of truth" would stop being true for whoever got that answer.

    ``tests/test_store.py::TestEveryStoreMethodIsSerialised`` asserts the decorator is on all of
    them, so a method added later cannot quietly be the one that is not.
    """

    @wraps(method)
    def guarded(self: SqliteCaseStore, *args: object, **kwargs: object) -> object:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(M, guarded)


class SqliteCaseStore:
    """A :class:`~careloop.state.store.CaseStore` backed by SQLite."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self._path != ":memory:":
            # WAL lets readers proceed during a write; pointless for in-memory databases.
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    # -- reads ---------------------------------------------------------------

    @serialised
    def get(self, case_id: str) -> Case:
        """Load a case by id."""
        row = self._conn.execute(
            "SELECT document FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise CaseNotFound(case_id)
        return Case.model_validate_json(row["document"])

    @serialised
    def list_by_state(self, state: CaseState) -> list[Case]:
        """Return all cases in ``state``, oldest first."""
        rows = self._conn.execute(
            "SELECT document FROM cases WHERE state = ? ORDER BY created_at, case_id",
            (str(state),),
        ).fetchall()
        return [Case.model_validate_json(row["document"]) for row in rows]

    @serialised
    def timeline(self, case_id: str) -> list[TimelineEvent]:
        """Return the projected timeline rows for a case, in sequence order."""
        rows = self._conn.execute(
            "SELECT seq, at, kind, actor, summary, diagnostic, payload "
            "FROM timeline WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        if not rows and not self._exists(case_id):
            raise CaseNotFound(case_id)
        return [
            TimelineEvent(
                seq=row["seq"],
                at=row["at"],
                kind=row["kind"],
                actor=row["actor"],
                summary=row["summary"],
                diagnostic=row["diagnostic"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    # -- writes --------------------------------------------------------------

    @serialised
    def create(self, case: Case) -> Case:
        """Persist a new case."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO cases (case_id, version, state, created_at, updated_at, document) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    case.case_id,
                    case.version,
                    str(case.state),
                    case.created_at.isoformat(),
                    case.updated_at.isoformat(),
                    case.model_dump_json(),
                ),
            )
            self._insert_timeline(case, since_seq=0)
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            raise CaseAlreadyExists(case.case_id) from exc
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return case

    @serialised
    def save(self, case: Case, *, expected_version: int) -> Case:
        """Persist a changed case, guarded by ``expected_version``."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT version FROM cases WHERE case_id = ?", (case.case_id,)
            ).fetchone()
            if row is None:
                raise CaseNotFound(case.case_id)
            if row["version"] != expected_version:
                raise VersionConflict(case.case_id, expected_version, row["version"])

            stored_seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS next FROM timeline WHERE case_id = ?",
                (case.case_id,),
            ).fetchone()["next"]

            cursor = self._conn.execute(
                "UPDATE cases SET version = ?, state = ?, updated_at = ?, document = ? "
                "WHERE case_id = ? AND version = ?",
                (
                    case.version,
                    str(case.state),
                    case.updated_at.isoformat(),
                    case.model_dump_json(),
                    case.case_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                # The guarded UPDATE is the real gate; the SELECT above is only for a good message.
                actual = self._conn.execute(
                    "SELECT version FROM cases WHERE case_id = ?", (case.case_id,)
                ).fetchone()
                raise VersionConflict(
                    case.case_id, expected_version, actual["version"] if actual else -1
                )

            self._insert_timeline(case, since_seq=stored_seq)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return case

    @serialised
    def clear(self) -> int:
        """Delete every case and its timeline. See :meth:`~careloop.state.store.CaseStore.clear`.

        One transaction, so a reset cannot leave timeline rows behind for cases that no longer
        exist — the two tables are one fact and half of it is worse than none of it.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            removed = self._conn.execute("DELETE FROM cases").rowcount
            self._conn.execute("DELETE FROM timeline")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return removed

    @serialised
    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # -- internals -----------------------------------------------------------

    def _exists(self, case_id: str) -> bool:
        return (
            self._conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            is not None
        )

    def _insert_timeline(self, case: Case, *, since_seq: int) -> None:
        """Project timeline entries at or after ``since_seq`` into the timeline table."""
        new_events = [event for event in case.timeline if event.seq >= since_seq]
        if not new_events:
            return
        self._conn.executemany(
            "INSERT INTO timeline (case_id, seq, at, kind, actor, summary, diagnostic, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    case.case_id,
                    event.seq,
                    event.at.isoformat(),
                    str(event.kind),
                    event.actor,
                    event.summary,
                    event.diagnostic,
                    json.dumps(event.payload, default=str),
                )
                for event in new_events
            ],
        )
