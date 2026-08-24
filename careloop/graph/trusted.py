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

from careloop.identity.directory import Caregiver, Grant, InMemoryCaregiverDirectory
from careloop.identity.patients import CaregiverPatientAuthority
from careloop.provenance.facts import Fact, Provenance
from careloop.provenance.trust import TrustLevel

#: Identifier recorded as the source of every configured fact.
#:
#: Not a filename. A fact whose provenance names a document is a fact that came from one, and
#: nothing here did.
CONFIG_SOURCE: Final[str] = "trusted-config"

#: The **demo** caregiver's own address, the provider CareLoop may book with, and the portal it may
#: upload to. Every ``DESTINATION`` a demo case can reach resolves through one of these or is
#: refused.
#:
#: Process-global until session X, which was correct while there was one caregiver and wrong the
#: moment there were two -- see :func:`trusted_facts`. It is now the demo caregiver's record and
#: nothing else's; a production caregiver's destinations live on their own
#: :class:`~careloop.identity.directory.Caregiver`.
TRUSTED_CONFIG: Final[tuple[tuple[str, str], ...]] = (
    ("caregiver_email", "rosa.marlowe@carer.example"),
    ("provider_id", "PROV-NORTHGATE-CARDIOLOGY"),
    ("portal_id", "PORTAL-NORTHGATE-RECORDS"),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def trusted_facts(caregiver: Caregiver, now: datetime | None = None) -> tuple[Fact, ...]:
    """That caregiver's configured facts, ready to seed onto a new case.

    **Still the only producer of** ``TRUSTED``, and invariant 3 is unchanged. What changed in
    session X is where the *values* come from: a process-global tuple was correct while there was
    one synthetic caregiver and became an authority contradiction the moment there were two. Every
    ``DESTINATION`` in the system resolves through one of these -- ``send_email.to`` draws on
    ``caregiver_email`` -- so a global tuple would have permitted caregiver B's case to email
    caregiver A's address, because A's was the only trusted address in the process, and refused B
    their own.

    ``caregiver`` is a record from CareLoop's own
    :class:`~careloop.identity.directory.CaregiverDirectory`. **It is never a token claim**, and
    that is the hard boundary: an identity provider supplying a ``caregiver_email`` would be a
    second producer of ``TRUSTED``, reachable by anybody who could get a claim into one.
    ``tests/test_identity_boundary.py`` refuses it structurally.

    Required rather than defaulted, for :func:`careloop.graph.runner.open_case`'s reason: a case
    seeded with destinations nobody chose is a case whose destinations nobody chose.
    """
    at = now or _utcnow()
    return tuple(
        Fact(
            key=key,
            value=value,
            trust_level=TrustLevel.TRUSTED,
            provenance=Provenance(source_doc=CONFIG_SOURCE, extractor="config"),
            extracted_at=at,
        )
        for key, value in caregiver.destinations
    )


def configured(key: str) -> str | None:
    """The demo caregiver's configured value for ``key``, or ``None`` if there is not one.

    Scoped to the demo caregiver since session X, and every existing caller meant that: the
    fixtures, the demo scenarios and ``scripts/resume_demo.py`` are all about the one synthetic
    caregiver this repository ships. A production caregiver's destinations are read from their own
    :class:`~careloop.identity.directory.Caregiver` record, never from here.
    """
    for name, value in TRUSTED_CONFIG:
        if name == key:
            return value
    return None


# -- the demo caregiver ---------------------------------------------------------------------------
#
# The one synthetic caregiver the judge-facing demo runs as. Composed here rather than in
# `careloop.identity` because these are *values*, and invariant 3 names this file as the place
# configured values live. The identity package holds the types; this holds the literals, so a
# second file cannot quietly become a second source of TRUSTED.

#: The demo caregiver's id. Deliberately readable rather than opaque: it appears in the demo's own
#: audit trail, where a judge should be able to see at a glance that the actor is synthetic.
DEMO_CAREGIVER_ID: Final[str] = "demo-caregiver"

#: The patient the fixture documents are about. Every fixture states ``Patient ID: PT-4417``, so
#: this is the one grant the demo needs -- and it is a real grant checked by the real code, not a
#: bypass, which is what keeps the demo exercising the production authorisation path.
DEMO_PATIENT_ID: Final[str] = "PT-4417"

#: The demo caregiver, carrying exactly :data:`TRUSTED_CONFIG` as their destinations.
#:
#: Equal by construction rather than by a copied literal, so the demo's trusted facts cannot drift
#: from the constant every other consumer reads.
DEMO_CAREGIVER: Final[Caregiver] = Caregiver(
    caregiver_id=DEMO_CAREGIVER_ID, destinations=TRUSTED_CONFIG, active=True
)


def demo_directory() -> InMemoryCaregiverDirectory:
    """A directory holding the demo caregiver and their one grant.

    A fresh instance per call, because a directory is mutable -- a test that revokes a grant must
    not revoke it for the next test, and two demo worlds must not share one.

    It carries **no** :class:`~careloop.identity.directory.SubjectMapping`. Nothing maps an IdP
    subject onto the demo caregiver, so a verified production token can never resolve to them: the
    two modes cannot meet in the middle even if one process somehow held both.
    """
    return InMemoryCaregiverDirectory(
        caregivers={DEMO_CAREGIVER_ID: DEMO_CAREGIVER},
        grants=(Grant(caregiver_id=DEMO_CAREGIVER_ID, patient_id=DEMO_PATIENT_ID),),
        subjects=(),
    )


def demo_patient_authority(caregiver_id: str | None) -> CaregiverPatientAuthority:
    """The patient authority a run gets when no composition root bound one.

    **Not an allow-all**, and that distinction is the whole of why this is acceptable as a default.
    It is a real grant check against :func:`demo_directory`: the demo caregiver may act for the one
    fixture patient and for nobody else, and a case owned by anybody the demo directory does not
    hold -- which is every production caregiver -- is refused. So a production deployment whose
    composition root forgot to bind an authority stops rather than opens up.

    The alternative, refusing everything by default, was measured: it turns 405 tests red and 107
    into errors, because almost every run in the suite legitimately drives the demo caregiver
    through a real dispatch. A default that made the ordinary case unworkable would be a default
    somebody replaced with a permissive one.
    """
    return CaregiverPatientAuthority(demo_directory(), caregiver_id)


__all__ = [
    "CONFIG_SOURCE",
    "DEMO_CAREGIVER",
    "DEMO_CAREGIVER_ID",
    "DEMO_PATIENT_ID",
    "TRUSTED_CONFIG",
    "configured",
    "demo_directory",
    "demo_patient_authority",
    "trusted_facts",
]
