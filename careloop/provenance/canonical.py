"""The canonical text form of a value.

A :class:`~careloop.provenance.facts.Fact` records its value as text. Anything that wants to be
compared against a fact — or keyed alongside one — must therefore be reduced to text first, and
there must be exactly one answer to what that text is. Two reductions that can disagree about what
a value *is* would let a value be trusted under one spelling and keyed under another.

This lives in ``provenance`` rather than in ``policy`` or ``tools`` because it belongs to neither
consumer. The policy engine matches argument values against recorded facts with it; tool
idempotency keys derive from it. Both import it; neither owns it.

The two consumers differ only in what they do with a value that has **no** canonical form. The
policy engine reads ``None`` as unmatchable and resolves ``UNTRUSTED``, which fails closed for it.
:mod:`careloop.tools.idempotency` refuses to key at all, because folding distinct values onto one
sentinel would fail *open* — two different actions sharing a key, the second suppressed as a
duplicate of the first.
"""

from __future__ import annotations

from datetime import date, datetime


def canonical_value(value: object) -> str | None:
    """Return the comparison form of a value, or ``None`` if it has none.

    The reduction is closed and deliberately unforgiving:

    ``str``
        Used verbatim. No case-folding, no whitespace trimming, no unicode normalisation.
        Normalisation is a laundering surface — homoglyphs, casing, a trailing dot — and a
        fail-closed false denial costs a replan, while a false trust costs the whole property.
    ``bool``
        ``"true"`` / ``"false"``. Checked before ``int``, which it subclasses.
    ``int``
        Decimal, no separators.
    ``datetime`` / ``date``
        ISO 8601, **without timezone conversion**. Two spellings of one instant do not match, for
        the same reason two spellings of one address do not.
    anything else
        ``None``. The value has no comparison form. ``float`` is excluded on purpose: its repr is
        unstable enough to be both a laundering hazard and a false-denial hazard, and no argument
        needs one.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return None


__all__ = ["canonical_value"]
