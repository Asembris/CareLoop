"""Deterministic document extraction, and the validators that promote what it finds.

**No model runs here, and that is an architecture decision rather than an optimisation.** Invariant
1 permits exactly two LLM agents, and its second sentence says what everything else is:
deterministic Python or a typed tool. Session 5 closed this by deciding that the third model role
stays wired and unused; a third agent reading documents into facts would be an invariant 1 change
and has to be argued as one. (The role's enum member is not named here in full: a sweep in
``tests/test_agents.py`` fails if any module outside ``careloop/llm/providers.py`` mentions it, and
this module — the one a future author would be tempted to wire it into — is exactly where that
sweep has to keep working.)

**This module is where untrusted text enters the system, and it is where it enters the planner's
context.** Two kinds of fact come out of a document:

*Structured values*, lifted by the patterns in :data:`PATTERNS` — a patient identifier, an offered
appointment time. These are what actions are built from, and each one starts ``UNTRUSTED``.

*Page excerpts*, one fact per page, holding the page text verbatim. These exist so the planner can
read the evidence it is planning against, and they are the reason T2 is a real temptation rather
than a staged one: :func:`careloop.agents.prompts.planner_user_prompt` renders every fact's value
inside the quoted evidence block, so the poisoned continuity notice's three requests reach the
planner in full. Nothing is stripped, escaped or sanitised on the way — a filter that looks like a
defence would make T2 pass for the wrong reason, and the authority lives in the policy engine.

**Only ``VALUE``-class keys are promotable.** :data:`PROMOTABLE_KEYS` says so explicitly and
:func:`check_promotable_keys_match_the_registry` fails at import if the table's belief about a key
contradicts the tool registry. ``promote_fact`` would refuse a ``DESTINATION`` anyway; the check is
here so the contradiction is caught where it is written rather than where it is used. Excerpts are
absent from the table and can never be promoted at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Final

from careloop.provenance.facts import Fact, Provenance
from careloop.provenance.trust import ArgClass, TrustLevel
from careloop.state.case import Case
from careloop.state.evidence import promote_fact, record_fact
from careloop.tools.registry import REGISTRY, ToolRegistry
from careloop.tools.results import Document

#: Name recorded on every fact this module produces.
EXTRACTOR: Final[str] = "deterministic_extractor"

#: Prefix of a page-excerpt fact's key. ``excerpt:<document_id>:p<page>``.
EXCERPT_PREFIX: Final[str] = "excerpt:"


class UnpromotableKeyDeclared(Exception):
    """Raised at import when :data:`PROMOTABLE_KEYS` contradicts the tool registry."""


#: Structured values lifted from document text, as ``(fact key, pattern)``.
#:
#: Anchored and line-oriented on purpose. A loose pattern over attacker-controlled text is a way to
#: extract whatever the attacker wants extracted, and an extractor that finds a ``Patient ID:``
#: line the insurer's notice invented would put the attacker's value in the same key the referral's
#: went in. Every pattern captures a single token or a single line, and never spans one.
PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("patient_id", re.compile(r"^Patient ID:[ \t]*(\S+)[ \t]*$", re.MULTILINE)),
    ("slot_start", re.compile(r"^Offered appointment:[ \t]*(\S+)[ \t]*$", re.MULTILINE)),
    ("referring_clinician", re.compile(r"^Referring clinician:[ \t]*(.+?)[ \t]*$", re.MULTILINE)),
)

#: Keys that may be promoted, and the argument class each is destined for.
#:
#: ``VALUE`` only. ``DESTINATION`` and ``AUTHORITY`` are absent from
#: :data:`careloop.provenance.trust.PROMOTABLE_ARG_CLASSES` and no entry here could change that;
#: the table exists so a rule about *which values get validated* is written down rather than
#: implied by whichever validators happen to exist.
PROMOTABLE_KEYS: Final[Mapping[str, ArgClass]] = {
    "patient_id": ArgClass.VALUE,
    "slot_start": ArgClass.VALUE,
}

#: A validator answers one question: is this text well-formed for its key?
#:
#: Format only. A validator can confirm that a string is an ISO datetime; it cannot confirm that
#: the appointment is the right one, and it must never be asked to. Confirming *shape* is what
#: raises ``UNTRUSTED`` to ``CONTROLLED``, and ``CONTROLLED`` is still below ``TRUSTED``.
Validator = Callable[[str], bool]


def _is_patient_id(text: str) -> bool:
    return re.fullmatch(r"PT-\d{4}", text) is not None


def _is_iso_datetime(text: str) -> bool:
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


#: Key to ``(validator name, validator)``. The name is recorded on the promoted fact, so the audit
#: trail says which check justified the trust level rather than merely that one did.
VALIDATORS: Final[Mapping[str, tuple[str, Validator]]] = {
    "patient_id": ("patient_id_format", _is_patient_id),
    "slot_start": ("iso_datetime", _is_iso_datetime),
}


def check_promotable_keys_match_the_registry(registry: ToolRegistry = REGISTRY) -> None:
    """Fail if a promotable key is classified differently by the tool registry.

    The registry owns argument classes; this table only *references* one, exactly as ``rules.yaml``
    references them. A key the registry calls ``DESTINATION`` appearing here as ``VALUE`` is a
    contradiction, and the whole trust model rests on that classification being in one place.

    Raises:
        UnpromotableKeyDeclared: on any disagreement.
    """
    wrong: list[str] = []
    for key, declared in PROMOTABLE_KEYS.items():
        for tool in registry:
            actual = tool.arg_class(key)
            if actual is not None and actual is not declared:
                wrong.append(f"{key}: table says {declared}, {tool.name} declares {actual}")
    if wrong:
        raise UnpromotableKeyDeclared(
            "promotable key table contradicts the tool registry: " + "; ".join(sorted(wrong))
        )


check_promotable_keys_match_the_registry()


def excerpt_key(document_id: str, page: int) -> str:
    """The fact key holding one page of one document."""
    return f"{EXCERPT_PREFIX}{document_id}:p{page}"


def extract(document: Document, now: datetime | None = None) -> tuple[Fact, ...]:
    """Every fact one document yields, all ``UNTRUSTED``.

    Page excerpts first, in page order, then structured values in :data:`PATTERNS` order. The
    ordering is fixed so two runs over one document produce identical facts and a case's timeline
    is comparable across runs.

    A pattern that matches more than once on a page yields only its first match. Taking the first
    is deterministic; taking the last would let a document append a second ``Patient ID:`` line and
    overwrite the real one, which is the whole shape of the attack this system is about.
    """
    at = now or datetime.now(UTC)
    facts: list[Fact] = []

    for page_number, page in enumerate(document.pages, start=1):
        facts.append(
            Fact(
                key=excerpt_key(document.document_id, page_number),
                value=page,
                trust_level=TrustLevel.UNTRUSTED,
                provenance=Provenance(
                    source_doc=document.source_doc, page=page_number, extractor=EXTRACTOR
                ),
                extracted_at=at,
            )
        )

    for key, pattern in PATTERNS:
        for page_number, page in enumerate(document.pages, start=1):
            found = pattern.search(page)
            if found is None:
                continue
            facts.append(
                Fact(
                    key=key,
                    value=found.group(1),
                    trust_level=TrustLevel.UNTRUSTED,
                    provenance=Provenance(
                        source_doc=document.source_doc, page=page_number, extractor=EXTRACTOR
                    ),
                    extracted_at=at,
                )
            )
            break

    return tuple(facts)


def record_document(case: Case, document: Document, now: datetime | None = None) -> Case:
    """Record every fact ``document`` yields onto ``case``, without promoting any of them."""
    for fact in extract(document, now=now):
        case = record_fact(case, fact, now=now)
    return case


def validate_and_promote(case: Case, now: datetime | None = None) -> Case:
    """Promote every ``UNTRUSTED`` fact on ``case`` whose validator passes.

    Walks the case's own facts rather than a list handed in, so a fact recorded by some other
    route is validated on the same terms. A fact whose key has no validator, or whose validator
    refuses, is left exactly as it is — the action depending on it is then denied
    ``INSUFFICIENT_FACT_TRUST``, which is remediable and says so.

    Promotion **replaces** the untrusted original; see :mod:`careloop.state.evidence` for why
    appending beside it makes the promotion silently invisible.
    """
    for fact in _promotable(case.facts):
        validator_name, validator = VALIDATORS[fact.key]
        if not validator(fact.value):
            continue
        case = promote_fact(
            case,
            fact,
            arg_class=PROMOTABLE_KEYS[fact.key],
            validator=validator_name,
            now=now,
        )
    return case


def _promotable(facts: Iterable[Fact]) -> list[Fact]:
    """Untrusted facts whose key is both promotable and has a validator."""
    return [
        fact
        for fact in facts
        if fact.trust_level is TrustLevel.UNTRUSTED
        and fact.key in PROMOTABLE_KEYS
        and fact.key in VALIDATORS
    ]


__all__ = [
    "EXCERPT_PREFIX",
    "EXTRACTOR",
    "PATTERNS",
    "PROMOTABLE_KEYS",
    "VALIDATORS",
    "UnpromotableKeyDeclared",
    "Validator",
    "check_promotable_keys_match_the_registry",
    "excerpt_key",
    "extract",
    "record_document",
    "validate_and_promote",
]
