"""Trusted configuration — the only source of ``TRUSTED`` facts.

Invariant 3 says ``TRUSTED`` comes only from trusted config, and that validation can never
manufacture it. This module is that sentence: a literal in Python, not a document, not an
extraction, not anything an attacker can write.

**It used to read "trusted config or a human decision", and the second half was never true.** A
caregiver's approval is a capability for one exact ``(tool_name, args_digest)`` — it releases that
call and writes no :class:`~careloop.provenance.facts.Fact`, so no fact's trust level has ever
moved because somebody pressed approve. Trust and approval are separate dimensions the policy
engine reads through separate predicates, which is why ``args_approved`` never satisfies a rule
about where an effect lands. Nothing in the code was wrong; five files said it was.

**Seeded when the case is opened, never by the evidence node.** The evidence node reads documents;
if it could also mint ``TRUSTED`` facts there would be one code path that both touches
attacker-controlled text and produces the trust level nothing else can reach. Keeping the seeding
at case creation means document content and trusted configuration never meet in the same function.

**No value here also appears in a document, and correctness no longer depends on that.** It used
to. Trust resolution took the lowest level among facts whose *value* matched, under any key at all,
so a configured destination that also appeared in the poisoned continuity notice would have been
dragged to ``UNTRUSTED`` and the legitimate action denied for a reason no diagnostic explained —
the same lowest-wins consequence that made fact promotion silently invisible before commit 6.

Resolution is now scoped to the argument's declared
:attr:`~careloop.tools.registry.ArgSpec.trust_key`, so only a fact recorded under *this same key*
can lower one of these, and an excerpt or an unrelated field carrying the same literal cannot touch
it. The disjointness check in ``tests/test_graph_evidence.py`` stays as defence in depth and
because a configured literal turning up in a document is worth knowing about — but it is no longer
load-bearing, and nothing here relies on these strings being globally unique.

There is deliberately **no allowlist**. These are ordinary :class:`~careloop.provenance.facts.Fact`
objects on the case, which is what lets invariant 6's "no allowlist contents reach the model" be
true because there is nothing to leak rather than because something is being withheld.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from careloop.provenance.facts import Fact, Provenance
from careloop.provenance.trust import TrustLevel

#: Identifier recorded as the source of every configured fact.
#:
#: Not a filename. A fact whose provenance names a document is a fact that came from one, and
#: nothing here did.
CONFIG_SOURCE: Final[str] = "trusted-config"

#: The caregiver's own address, the provider CareLoop may book with, and the portal it may upload
#: to. Every ``DESTINATION`` in the system resolves through one of these or is refused.
TRUSTED_CONFIG: Final[tuple[tuple[str, str], ...]] = (
    ("caregiver_email", "rosa.marlowe@carer.example"),
    ("provider_id", "PROV-NORTHGATE-CARDIOLOGY"),
    ("portal_id", "PORTAL-NORTHGATE-RECORDS"),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def trusted_facts(now: datetime | None = None) -> tuple[Fact, ...]:
    """The configured facts, ready to seed onto a new case."""
    at = now or _utcnow()
    return tuple(
        Fact(
            key=key,
            value=value,
            trust_level=TrustLevel.TRUSTED,
            provenance=Provenance(source_doc=CONFIG_SOURCE, extractor="config"),
            extracted_at=at,
        )
        for key, value in TRUSTED_CONFIG
    )


def configured(key: str) -> str | None:
    """The configured value for ``key``, or ``None`` if there is not one."""
    for name, value in TRUSTED_CONFIG:
        if name == key:
            return value
    return None


__all__ = ["CONFIG_SOURCE", "TRUSTED_CONFIG", "configured", "trusted_facts"]
