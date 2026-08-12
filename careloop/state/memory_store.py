"""In-memory :class:`~careloop.state.store.CaseStore` for tests.

Semantics match :class:`~careloop.state.sqlite_store.SqliteCaseStore` exactly, including
compare-and-set and the exceptions raised, so the same conformance suite runs against both. Cases
are round-tripped through JSON on the way in and out, which keeps this store honest about
serialisability rather than letting tests pass on live object references the SQLite store could
never reproduce.
"""

from __future__ import annotations

from careloop.state.case import Case, CaseState, TimelineEvent
from careloop.state.store import CaseAlreadyExists, CaseNotFound, VersionConflict


class InMemoryCaseStore:
    """A dict-backed case store."""

    def __init__(self) -> None:
        self._cases: dict[str, str] = {}
        self._versions: dict[str, int] = {}

    # -- reads ---------------------------------------------------------------

    def get(self, case_id: str) -> Case:
        """Load a case by id."""
        document = self._cases.get(case_id)
        if document is None:
            raise CaseNotFound(case_id)
        return Case.model_validate_json(document)

    def list_by_state(self, state: CaseState) -> list[Case]:
        """Return all cases in ``state``, oldest first."""
        cases = [Case.model_validate_json(doc) for doc in self._cases.values()]
        matching = [case for case in cases if case.state is state]
        return sorted(matching, key=lambda case: (case.created_at, case.case_id))

    def timeline(self, case_id: str) -> list[TimelineEvent]:
        """Return the timeline for a case, in sequence order."""
        return sorted(self.get(case_id).timeline, key=lambda event: event.seq)

    # -- writes --------------------------------------------------------------

    def create(self, case: Case) -> Case:
        """Persist a new case."""
        if case.case_id in self._cases:
            raise CaseAlreadyExists(case.case_id)
        self._cases[case.case_id] = case.model_dump_json()
        self._versions[case.case_id] = case.version
        return case

    def save(self, case: Case, *, expected_version: int) -> Case:
        """Persist a changed case, guarded by ``expected_version``."""
        if case.case_id not in self._cases:
            raise CaseNotFound(case.case_id)
        stored = self._versions[case.case_id]
        if stored != expected_version:
            raise VersionConflict(case.case_id, expected_version, stored)
        self._cases[case.case_id] = case.model_dump_json()
        self._versions[case.case_id] = case.version
        return case

    def clear(self) -> int:
        """Delete every case. See :meth:`careloop.state.store.CaseStore.clear` for who may."""
        removed = len(self._cases)
        self._cases = {}
        self._versions = {}
        return removed

    def close(self) -> None:
        """No-op; present for interface parity."""
        return
