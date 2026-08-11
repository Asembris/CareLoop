"""Constructors shared between test modules.

Kept out of ``conftest.py`` deliberately. ``conftest.py`` is loaded by pytest itself and is not
meant to be imported by test modules — importing it as ``tests.conftest`` only worked because
``python -m pytest`` puts the working directory on ``sys.path``, so the suite failed to collect
under a bare ``pytest``. Anything a test module needs to *import* belongs here; ``conftest.py``
holds fixtures, which pytest injects rather than exports.
"""

from __future__ import annotations

from datetime import UTC, datetime

from careloop.state import Case, CaseState

FIXED_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def make_case(case_id: str = "case-0001", state: CaseState = CaseState.NEW) -> Case:
    """Build a case directly in ``state`` for table-driven tests."""
    return Case(
        case_id=case_id,
        event_id="evt-0001",
        state=state,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
