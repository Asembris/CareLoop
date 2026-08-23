"""Constructors shared between test modules.

Kept out of ``conftest.py`` deliberately. ``conftest.py`` is loaded by pytest itself and is not
meant to be imported by test modules — importing it as ``tests.conftest`` only worked because
``python -m pytest`` puts the working directory on ``sys.path``, so the suite failed to collect
under a bare ``pytest``. Anything a test module needs to *import* belongs here; ``conftest.py``
holds fixtures, which pytest injects rather than exports.
"""

from __future__ import annotations

from datetime import UTC, datetime

from careloop.policy import ReasonCode, Verdict, default_rules
from careloop.provenance import ArgClass, Fact, Provenance, TrustLevel
from careloop.state import Case, CaseState
from careloop.tools import REGISTRY
from careloop.tools.registry import AUTHORED_VALUES

FIXED_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)

#: The exact text CareLoop composes for ``book_appointment.reason``.
#:
#: Read from the registry rather than spelled here, so a fixture and the shipped declaration cannot
#: drift: ``allow-booking-on-validated-slot`` requires this exact value through
#: ``args_system_authored``, so a booking fixture carrying anything else matches no ALLOW rule and
#: is refused — which is the property, not an inconvenience. Tests that mean to supply something
#: else say so at the call site.
BOOKING_REASON = AUTHORED_VALUES["book_appointment", "reason"]


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


# -- the leak detector ---------------------------------------------------------------------------
#
# Shared rather than duplicated. Two enforcement points now produce model-visible text — the
# denial the planner is shown in its next prompt, and the ``cancel_tool`` string the
# ``BeforeToolCall`` hook sets — and a second copy of this token set is a second place for one of
# them to drift out of invariant 6's reach.


def forbidden_tokens() -> frozenset[str]:
    """Every structured token invariant 6 forbids from reaching a model.

    Argument *names* are deliberately absent: invariant 6 does not list them, and the model
    already knows them because it is the caller.
    """
    return frozenset(
        frozenset(level.value for level in TrustLevel)
        | frozenset(cls.value for cls in ArgClass)
        | frozenset(verdict.value for verdict in Verdict)
        | frozenset(code.value for code in ReasonCode)
        | frozenset(state.value for state in CaseState)
        | default_rules().rule_ids
        | REGISTRY.names
    )


def leaked_tokens(text: str, forbidden: frozenset[str] | None = None) -> frozenset[str]:
    """Structured tokens present in ``text``.

    Case-sensitive on purpose. An interpolated ``StrEnum`` renders as its uppercase member value,
    which is the actual signature of a leak; matching case-insensitively would flag ordinary
    English ("value", "done", "new") and force the reason texts into contortions that buy nothing.

    Plain substring matching, so tokens that contain one another both report — ``UNTRUSTED``
    also trips ``TRUSTED``. Over-detection is the safe direction for a leak detector: it can
    reject an innocent string, never accept a leaking one.
    """
    return frozenset(
        token
        for token in (forbidden if forbidden is not None else forbidden_tokens())
        if token in text
    )
