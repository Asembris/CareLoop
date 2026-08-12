"""The CaseStore interface.

The store is the durable source of truth: case state, evidence and provenance, approvals, action
history and the append-only timeline. Strands session persistence holds execution context only and
is never consulted for business facts.

The interface is deliberately shaped for a conditional-write key-value store so that DynamoDB can
replace SQLite without touching callers: single-item reads by primary key, and writes guarded by an
expected version rather than by holding a lock.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from careloop.state.case import Case, CaseState, TimelineEvent


class CaseNotFound(KeyError):
    """Raised when a case id has no stored case."""


class CaseAlreadyExists(Exception):
    """Raised when creating a case whose id is already taken."""


class VersionConflict(Exception):
    """Raised when a compare-and-set write loses to a concurrent writer.

    The caller should re-read the case and re-apply its change; it must not retry blindly, because
    the state it decided against may no longer hold.
    """

    def __init__(self, case_id: str, expected: int, actual: int) -> None:
        self.case_id = case_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"case {case_id!r} changed underneath this write: expected version {expected}, "
            f"store holds {actual}"
        )


@runtime_checkable
class CaseStore(Protocol):
    """Durable storage for cases."""

    def create(self, case: Case) -> Case:
        """Persist a new case.

        Raises:
            CaseAlreadyExists: if the case id is taken.
        """
        ...

    def get(self, case_id: str) -> Case:
        """Load a case.

        Raises:
            CaseNotFound: if no such case exists.
        """
        ...

    def save(self, case: Case, *, expected_version: int) -> Case:
        """Persist a changed case if the stored version still matches ``expected_version``.

        Raises:
            VersionConflict: if the stored version has moved on.
            CaseNotFound: if the case was deleted underneath the caller.
        """
        ...

    def list_by_state(self, state: CaseState) -> list[Case]:
        """Return all cases currently in ``state``, oldest first."""
        ...

    def timeline(self, case_id: str) -> list[TimelineEvent]:
        """Return the append-only timeline for a case, in sequence order."""
        ...

    def clear(self) -> int:
        """Delete every case and its timeline, returning how many were removed.

        **The one destructive method on the durable source of truth, and it exists for exactly one
        caller**: ``POST /api/demo/reset``, which is only mounted when the app is built with the
        demo flag on. A judge poking at a live demo has to be able to put it back to a known state,
        and the alternative — rebuilding the app around a fresh store — would leave a run already
        in flight writing to a store nobody is reading any more.

        Nothing else in ``careloop/`` may call it, and
        ``tests/test_api_demo.py::TestNothingElseClearsTheStore`` sweeps for the call rather than
        trusting the docstring. The API refuses the reset while any run is in flight, for the same
        reason: a store cleared underneath a live graph is a version conflict wearing a costume.
        """
        ...

    def close(self) -> None:
        """Release any underlying resources."""
        ...
