"""Constructors shared between test modules.

Kept out of ``conftest.py`` deliberately. ``conftest.py`` is loaded by pytest itself and is not
meant to be imported by test modules — importing it as ``tests.conftest`` only worked because
``python -m pytest`` puts the working directory on ``sys.path``, so the suite failed to collect
under a bare ``pytest``. Anything a test module needs to *import* belongs here; ``conftest.py``
holds fixtures, which pytest injects rather than exports.
"""

from __future__ import annotations

from datetime import UTC, datetime

from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import Case, CaseState

FIXED_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def make_case(
    case_id: str = "case-0001",
    state: CaseState = CaseState.NEW,
    facts: tuple[Fact, ...] = (),
) -> Case:
    """Build a case directly in ``state`` for table-driven tests."""
    return Case(
        case_id=case_id,
        event_id="evt-0001",
        state=state,
        facts=facts,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def make_fact(
    key: str,
    value: str,
    trust_level: TrustLevel = TrustLevel.UNTRUSTED,
    source_doc: str = "referral.pdf",
) -> Fact:
    """Build a fact at a chosen trust level.

    Constructing a ``TRUSTED`` fact directly is legitimate and is how trusted configuration enters
    a case; only :func:`careloop.provenance.promote` is forbidden from producing that level.
    """
    return Fact(
        key=key,
        value=value,
        trust_level=trust_level,
        provenance=Provenance(source_doc=source_doc, extractor="test"),
        extracted_at=FIXED_NOW,
    )


def trusted_fact(key: str, value: str) -> Fact:
    """A fact that came from trusted configuration rather than from document content."""
    return make_fact(key, value, TrustLevel.TRUSTED, source_doc="trusted-config")
