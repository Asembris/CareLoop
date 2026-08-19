"""The trust model.

Two orthogonal ideas live here:

``TrustLevel``
    How much we trust a *value*. Anything lifted out of a document or an email starts
    ``UNTRUSTED``. Validation can raise it to ``CONTROLLED``. ``TRUSTED`` is not reachable from
    here at all: it is what :mod:`careloop.graph.trusted` seeds onto a new case and nothing else.

``ArgClass``
    What a tool *argument* controls. A ``VALUE`` argument affects the content of an action; a
    ``DESTINATION`` or ``AUTHORITY`` argument affects who receives it or what powers are granted.

The load-bearing rule of the whole system is the interaction between the two: validation may raise
the trust of a value, but it may never make a document-sourced value acceptable as a
``DESTINATION`` or ``AUTHORITY`` argument. Those must come from trusted configuration. See
:data:`PROMOTABLE_ARG_CLASSES`.

**A caregiver's approval is not a third way to reach ``TRUSTED``, and saying so was wrong for long
enough to be worth a paragraph.** An :class:`~careloop.state.case.Approval` is a capability for one
exact ``(tool_name, args_digest)``: deciding it releases that call and nothing else. It writes no
:class:`~careloop.provenance.facts.Fact`, and no fact's ``trust_level`` moves when a person presses
approve. The policy engine reads the two through different predicates — ``arg_trust_at_least``
resolves trust from ``case.facts``, ``args_approved`` looks up an approval — precisely because they
are different dimensions, which is also why an approval says a caregiver was asked about one exact
call and never says where that call lands.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class TrustLevel(StrEnum):
    """How far a value may be trusted."""

    UNTRUSTED = "UNTRUSTED"
    """Extracted from document or email content. Assume an adversary wrote it."""

    CONTROLLED = "CONTROLLED"
    """Extracted, then validated by a deterministic checker (format, range, cross-reference)."""

    TRUSTED = "TRUSTED"
    """Originates from trusted configuration — :mod:`careloop.graph.trusted`, and nowhere else.

    Not reachable by promotion (:func:`careloop.provenance.facts.promote` refuses it outright) and
    not reachable by a human decision, which produces an approval rather than a fact.
    """


class ArgClass(StrEnum):
    """What a tool argument controls."""

    VALUE = "VALUE"
    """Content of an action: dates, identifiers, amounts, free text."""

    DESTINATION = "DESTINATION"
    """Where an effect lands: recipients, addresses, URLs, payment targets."""

    AUTHORITY = "AUTHORITY"
    """What powers are granted: permission changes, role assignments, delegations."""


#: Argument classes whose values may be satisfied by promotion.
#:
#: ``DESTINATION`` and ``AUTHORITY`` are deliberately absent, and that absence is the point. A
#: validator can confirm that ``attacker@evil.example`` is a well-formed address; it cannot make it
#: a legitimate recipient. Widening this set defeats the trust model.
PROMOTABLE_ARG_CLASSES: Final[frozenset[ArgClass]] = frozenset({ArgClass.VALUE})


_TRUST_ORDER: Final[dict[TrustLevel, int]] = {
    TrustLevel.UNTRUSTED: 0,
    TrustLevel.CONTROLLED: 1,
    TrustLevel.TRUSTED: 2,
}


class PromotionForbidden(Exception):
    """Raised when a promotion would violate the trust model.

    This is not a validation failure. It means the promotion could never be legitimate, however
    thorough the validator, because of the class of argument the value is destined for.
    """


def at_least(level: TrustLevel, minimum: TrustLevel) -> bool:
    """Return whether ``level`` meets or exceeds ``minimum``."""
    return _TRUST_ORDER[level] >= _TRUST_ORDER[minimum]


def check_promotable(arg_class: ArgClass) -> None:
    """Raise :class:`PromotionForbidden` if ``arg_class`` may never be satisfied by promotion."""
    if arg_class not in PROMOTABLE_ARG_CLASSES:
        raise PromotionForbidden(
            f"{arg_class} arguments can never be promoted by validation; "
            f"they must come from trusted config"
        )
