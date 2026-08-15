"""Recording evidence on a case, and promoting it.

Two functions, and the second exists because the obvious spelling of it is a silent bug.

:func:`careloop.provenance.facts.promote` returns a **new** ``Fact`` rather than raising the trust
of an existing one — deliberately, so a reference held elsewhere cannot be upgraded underneath its
holder. A caller that appends that new fact to ``case.facts`` therefore leaves the ``UNTRUSTED``
original sitting beside it with the same value.

That is fatal, because trust resolution takes the **lowest** level among every fact sharing the
promoted fact's key *and* value. Lowest-wins is what stops a poisoned duplicate being laundered by
a legitimate twin, and it cannot be relaxed. So with both facts present the engine reads the
original, the promotion has
no effect whatsoever, and every action depending on that value is denied
``INSUFFICIENT_FACT_TRUST`` — while the timeline shows a ``FACT_PROMOTED`` entry and the case
carries a visibly ``CONTROLLED`` fact. It reads as a policy bug and it is not one.

:func:`promote_fact` therefore **replaces** the original rather than appending beside it, and it is
the only promotion API a node is given. Appending is not a mistake a caller can make here, because
this function takes the case rather than a bare fact and rebuilds ``case.facts`` at the original's
index.

Two guards make the failure loud rather than silent if it is ever reintroduced by another route:
the fact being promoted must already be on the case, and the promotion must be *effective* — if
some other fact still carries the same value at a lower level, the promotion would resolve to
nothing and :exc:`PromotionIneffective` is raised instead.

**The assertion that matters is at the engine.** "A ``CONTROLLED`` fact exists on the case" passes
while the engine denies. Only ``resolve_trust(value, case)`` can tell the two apart, and that is
what ``tests/test_evidence.py`` asserts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from careloop.provenance.facts import Fact, promote
from careloop.provenance.trust import ArgClass, TrustLevel, at_least
from careloop.state.case import Case, TimelineKind
from careloop.state.machine import append_event


class FactNotOnCase(Exception):
    """Raised when promoting a fact the case does not hold.

    Promotion rebuilds ``case.facts`` around the original, so there has to be an original. A
    caller reaching this has almost certainly promoted a bare fact and meant to append the result,
    which is the bug this module exists to prevent.
    """


class PromotionIneffective(Exception):
    """Raised when a promotion would be invisible to trust resolution.

    Lowest-wins means a promoted value is only as trusted as the *least* trusted fact carrying the
    same key and the same value. If another fact on the case still holds that pair at a lower
    level, the promotion changes nothing that the policy engine can see. Failing here is the whole
    point: the alternative is a case that looks correct in the timeline and is denied for a reason
    no diagnostic explains.

    Scoped to the key deliberately, and it must stay exactly as wide as
    :func:`careloop.policy.engine.resolve_trust` — no wider and no narrower. Narrower would let a
    genuinely invisible promotion through; wider would refuse an effective one because an unrelated
    field was spelled the same. ``tests/test_evidence.py`` asserts both directions.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _index_of(case: Case, fact: Fact) -> int:
    """Position of ``fact`` within ``case.facts``.

    Identity first, then equality, so a fact rehydrated from the store — a different object with
    the same fields — is still found.

    Raises:
        FactNotOnCase: if the case does not hold it.
    """
    for index, candidate in enumerate(case.facts):
        if candidate is fact or candidate == fact:
            return index
    raise FactNotOnCase(
        f"fact {fact.key!r} is not recorded on case {case.case_id!r}; promotion rebuilds the "
        f"case's facts around the original, so the original has to be there"
    )


def already_recorded(case: Case, fact: Fact) -> Fact | None:
    """The fact already on ``case`` carrying this ``(key, value)``, if there is one."""
    for candidate in case.facts:
        if candidate.key == fact.key and candidate.value == fact.value:
            return candidate
    return None


def record_fact(
    case: Case,
    fact: Fact,
    *,
    actor: str = "evidence",
    now: datetime | None = None,
) -> Case:
    """Return a copy of ``case`` carrying ``fact``, with an ``EVIDENCE_RECORDED`` entry.

    The summary renders through ``Fact.__str__``, which omits the value, plus the fact's
    :attr:`~careloop.provenance.facts.Fact.citation` — ``source: referral.pdf p.2``. That citation
    is invariant 3's provenance requirement made visible: an audit trail that says a value was
    recorded but not where it came from is not an audit trail. The value itself goes to the
    diagnostic, which is timeline-only and is where raw content belongs.

    **A fact the case already holds at this exact ``(key, value)`` is not recorded twice.** This is
    open decision 9's third face, after append-versus-replace and triage-emitted duplicates, and it
    is guarded here rather than at the caller so that no second call site can forget it. Under
    lowest-wins, a second ``UNTRUSTED`` copy of a value the case already carries at ``CONTROLLED``
    silently downgrades it, and every action depending on that value is then denied for a reason no
    diagnostic explains. The follow-up documents in the resume make this concrete: a claim
    rejection legitimately restates ``Patient ID: PT-4417``, which the referral already supplied
    and validation already promoted.

    The skip is written to the timeline rather than performed quietly, because an invisible
    trust-safety mechanism is the same hazard in a different place.
    """
    at = now or _utcnow()

    duplicate = already_recorded(case, fact)
    if duplicate is not None:
        return append_event(
            case,
            kind=TimelineKind.EVIDENCE_DUPLICATE_SKIPPED,
            actor=actor,
            summary=f"{fact.key} already recorded — source: {fact.citation}",
            diagnostic=(
                f"{fact.key}={fact.value!r} from {fact.citation} was not recorded a second time: "
                f"the case already carries it at {duplicate.trust_level} from "
                f"{duplicate.citation}. Recording it again would add a fact at "
                f"{fact.trust_level}, and trust resolution takes the lowest level among facts "
                f"whose value matches — so the duplicate would silently downgrade the original."
            ),
            payload={
                "key": fact.key,
                "source_doc": fact.provenance.source_doc,
                "page": fact.provenance.page,
                "held_trust_level": str(duplicate.trust_level),
            },
            now=at,
        )

    with_fact = case.model_copy(update={"facts": (*case.facts, fact)})
    return append_event(
        with_fact,
        kind=TimelineKind.EVIDENCE_RECORDED,
        actor=actor,
        summary=f"recorded {fact} — source: {fact.citation}",
        diagnostic=(
            f"{fact.key}={fact.value!r} at {fact.trust_level} from "
            f"{fact.provenance.source_doc} page {fact.provenance.page} "
            f"via {fact.provenance.extractor}"
        ),
        payload={
            "key": fact.key,
            "trust_level": str(fact.trust_level),
            "source_doc": fact.provenance.source_doc,
            "page": fact.provenance.page,
        },
        now=at,
    )


def promote_fact(
    case: Case,
    fact: Fact,
    *,
    arg_class: ArgClass,
    validator: str,
    to: TrustLevel = TrustLevel.CONTROLLED,
    actor: str = "evidence",
    now: datetime | None = None,
) -> Case:
    """Return a copy of ``case`` with ``fact`` **replaced** by its promoted form.

    ``arg_class`` is the class of the tool argument the value is destined for, and it is required
    for the reason :func:`careloop.provenance.facts.promote` requires it: no caller may raise the
    trust of a value without stating what it will be used to control. ``DESTINATION`` and
    ``AUTHORITY`` are refused there, whatever the validator says.

    Raises:
        FactNotOnCase: if the case does not hold ``fact``.
        PromotionIneffective: if another fact on the case would still hold the same value at a
            lower level, so trust resolution could not see the promotion.
        PromotionForbidden: from :func:`promote`, for a forbidden class, a forbidden target level,
            or a fact that is not ``UNTRUSTED`` — promotion is not idempotent.
    """
    at = now or _utcnow()
    index = _index_of(case, fact)
    promoted = promote(fact, to=to, arg_class=arg_class, validator=validator, now=at)

    facts = (*case.facts[:index], promoted, *case.facts[index + 1 :])

    # Scoped to the promoted fact's own key, because that is exactly what resolve_trust consults.
    # A fact under an unrelated key carrying the same literal does not shadow this promotion and
    # never did anything to it — under the old value-only resolver it *appeared* to, which made
    # this check wider than the thing it models. Wider is not safer here: it would refuse a
    # perfectly effective promotion because some other field happened to be spelled the same.
    shadowing = sorted(
        f"{other.key}={other.trust_level}"
        for position, other in enumerate(facts)
        if position != index
        and other.key == promoted.key
        and other.value == promoted.value
        and not at_least(other.trust_level, to)
    )
    if shadowing:
        raise PromotionIneffective(
            f"promoting {fact.key!r} to {to} would be invisible: fact(s) "
            f"{', '.join(shadowing)} carry the same key and value below {to}, and trust "
            f"resolution takes the lowest level among matching facts"
        )

    with_promotion = case.model_copy(update={"facts": facts})
    return append_event(
        with_promotion,
        kind=TimelineKind.FACT_PROMOTED,
        actor=actor,
        summary=f"promoted {promoted}",
        diagnostic=(
            f"{promoted.key}={promoted.value!r} raised {fact.trust_level} -> "
            f"{promoted.trust_level} by validator {validator!r} for a {arg_class} argument; "
            f"the untrusted original was replaced, not appended beside"
        ),
        payload={
            "key": promoted.key,
            "from_trust_level": str(fact.trust_level),
            "to_trust_level": str(promoted.trust_level),
            "arg_class": str(arg_class),
            "validated_by": validator,
        },
        now=at,
    )


__all__ = [
    "FactNotOnCase",
    "PromotionIneffective",
    "already_recorded",
    "promote_fact",
    "record_fact",
]
