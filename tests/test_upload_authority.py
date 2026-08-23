"""An upload's document must be the one this case is required to send, not merely a held one.

The defect these tests exist for. ``upload_document`` is authorised by
``allow-upload-to-trusted-portal``, which requires ``portal_id`` at ``TRUSTED`` and ``document_id``
at ``CONTROLLED``. Both clauses were correct and both were being read. What decided the second one
was a chain that never mentioned the case: ``document_id`` is lifted from a ``Required document:``
line printed by a ``CLAIM_REJECTION``, promoted once ``document_id_format`` accepts its shape and
``required-document-is-held`` finds *something* under it in CareLoop's store.

A ``Required document:`` line is untrusted text. So a document filed as a claim rejection could
name **any document in the entire store** and have it promoted and authorised for upload — the
poisoned continuity notice, a referral belonging to another case, anything CareLoop holds. All four
shapes below reached ``ALLOW`` against the shipped ``rules.yaml``, the shipped ``REGISTRY``, the
shipped ``TRUSTED_CONFIG`` and the shipped extractor before ``required-document-answers-the-demand``
was written.

**Two things were already working and are not what this is about.** Trust is key-bound, so a
document the case never demanded has no ``document_id`` fact and resolves ``UNTRUSTED`` — a planner
cannot simply name a different held document and be believed. And ``portal_id`` is
``DESTINATION``-class and guarded by a ``DENY`` and by the ``ALLOW``, so where the upload lands was
never in question. The hole was one layer in from both: *which* document the case is entitled to
send, given that the demand itself arrives as attacker-authorable text.

:class:`TestTheRelationshipIsWhatRefusesThese` is the non-vacuity proof. It rebuilds
:data:`careloop.graph.extract.CROSS_CHECKS` with exactly one edit — the new check removed — and
requires the wrong-document tests to notice.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

from careloop.graph.extract import record_document, validate_and_promote
from careloop.graph.trusted import configured, trusted_facts
from careloop.policy import PolicyEngine, ReasonCode, Verdict, default_rules
from careloop.provenance import TrustLevel
from careloop.state import CaseState
from careloop.state.case import Case
from careloop.tools.documents import default_document_store
from careloop.tools.results import Document, DocumentKind

#: The extraction **module**, reached through ``sys.modules`` rather than as an attribute.
#:
#: ``careloop.graph`` re-exports the ``extract`` *function* under that same name, so
#: ``careloop.graph.extract`` resolves to the function and monkeypatching it would silently
#: patch nothing. The tests below mutate a module constant, so they need the module itself.
#: The ``from`` import above is what puts it in ``sys.modules``.
extraction = sys.modules["careloop.graph.extract"]

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

PORTAL = configured("portal_id")
RECEIPT = "doc-0005-receipt"
POISONED = "doc-0002-continuity-notice"
INSTRUCTIONS = "doc-0003-appointment-instructions"
REFERRAL = "doc-0001-referral"

#: The claim rejection and the explanation of benefits the shipped fixtures carry.
#:
#: Attached in every scenario below, so the *only* thing that varies between a permitted upload and
#: a refused one is which document the demand names and what that document established here.
CLAIM_CONTEXT = ("doc-0004-eob",)


def demand(demanded: str) -> Document:
    """A claim rejection whose ``Required document`` line names whatever we like.

    Built in the test rather than added as a seventh fixture PDF, deliberately. The point is that
    this line is *content*: it arrives from outside, nothing validates its meaning, and an attacker
    who can get a document filed as a rejection chooses it. A fixture would make the attacker's
    lever look like part of CareLoop.

    Every other field is well formed and internally consistent, so the document passes
    ``ClaimRejectionFields`` and the identifier passes ``document_id_format``. Nothing here is
    malformed; that is the whole difficulty.
    """
    return Document(
        document_id="doc-0009-demand",
        title="Claim decision notice",
        source_doc="demand.pdf",
        kind=DocumentKind.CLAIM_REJECTION,
        pages=(
            "\n".join(
                (
                    "# MEDIGUARD HEALTH",
                    "Claim decision notice",
                    "",
                    "Patient ID: PT-4417",
                    "Claim number: CLM-2026-44718",
                    "Decision: REJECTED",
                    "Rejection code: R-217",
                    "",
                    f"Required document: {demanded}",
                )
            ),
        ),
    )


def case_demanding(demanded: str, *attached: str) -> Case:
    """A case that read the demand and ``attached``, through the real extractor and promoter."""
    store = default_document_store()
    case = Case(
        case_id="case-upload",
        event_id="evt-0003-claim-rejection",
        state=CaseState.GATHERING_EVIDENCE,
        facts=trusted_facts(NOW),
        created_at=NOW,
        updated_at=NOW,
    )
    case = record_document(case, demand(demanded), now=NOW)
    for document_id in (*CLAIM_CONTEXT, *attached):
        case = record_document(case, store.get(document_id), now=NOW)
    case = validate_and_promote(case, now=NOW, documents=store)
    return case.model_copy(update={"state": CaseState.PLANNING})


def upload(case: Case, document_id: str, portal_id: str = PORTAL):
    """The shipped engine's verdict on uploading ``document_id`` to ``portal_id``."""
    return PolicyEngine(default_rules()).evaluate(
        "upload_document", {"portal_id": portal_id, "document_id": document_id}, case
    )


def trust_of(case: Case, key: str, value: str) -> TrustLevel | None:
    """The recorded trust level of one fact, or ``None`` if the case holds no such fact."""
    for fact in case.facts:
        if fact.key == key and fact.value == value:
            return fact.trust_level
    return None


class TestTheDemandedDocumentMustAnswerTheDemand:
    """A, B and C: the correct document, a wrong held one, and a poisoned demand."""

    def test_the_demanded_receipt_is_still_authorised(self) -> None:
        """A. The path the product exists for stays open.

        The rejection demands the receipt, the case read the receipt, the receipt established what
        was paid, and that amount settles the balance the explanation of benefits states. Every
        clause is about this case, and the upload is permitted autonomously exactly as before.
        """
        case = case_demanding(RECEIPT, RECEIPT)

        assert trust_of(case, "document_id", RECEIPT) is TrustLevel.CONTROLLED
        assert upload(case, RECEIPT).verdict is Verdict.ALLOW

    def test_a_held_and_read_document_that_answers_nothing_is_refused(self) -> None:
        """B. **The load-bearing regression.**

        The attendance instructions are held, are attached to this case, are read, satisfy their
        schema and agree about the patient — everything ``required-document-is-held`` and
        ``document_id_format`` ask. They are still not proof that anybody paid anything, so they
        are not the document this case is entitled to send.
        """
        case = case_demanding(INSTRUCTIONS, INSTRUCTIONS)

        assert trust_of(case, "document_id", INSTRUCTIONS) is TrustLevel.UNTRUSTED
        assert upload(case, INSTRUCTIONS).verdict is Verdict.DENY

    def test_a_document_this_case_never_read_is_refused(self) -> None:
        """B, in the shape that shows the check is about *this case* rather than the store.

        The referral is a perfectly legitimate document CareLoop holds. It was never attached here,
        so it established nothing here, and a letter naming it does not make it this case's to send.
        """
        case = case_demanding(REFERRAL)

        assert trust_of(case, "document_id", REFERRAL) is TrustLevel.UNTRUSTED
        assert upload(case, REFERRAL).verdict is Verdict.DENY

    @pytest.mark.parametrize("attached", [(), (POISONED,)], ids=["unattached", "attached"])
    def test_a_demand_naming_the_poisoned_notice_is_refused(
        self, attached: tuple[str, ...]
    ) -> None:
        """C. Untrusted evidence naming a valid held document does not authorise sending it.

        Both halves matter. Unattached, the notice established nothing here. **Attached**, it was
        genuinely read — and still established nothing, because ``CONTINUITY_NOTICE`` maps to no
        patterns and yields page excerpts alone. Excerpts are absent from
        :attr:`~careloop.graph.extract.CaseEvidence.facts` on purpose: a check that counted them
        would be satisfied by the attacker's own prose.
        """
        case = case_demanding(POISONED, *attached)

        assert trust_of(case, "document_id", POISONED) is TrustLevel.UNTRUSTED
        assert upload(case, POISONED).verdict is Verdict.DENY

    def test_the_refusal_is_the_remediable_one_and_names_no_rule(self) -> None:
        """What a refused upload actually reports, pinned rather than left to drift.

        ``document_id`` below ``CONTROLLED`` matches no ``ALLOW`` rule and there is deliberately no
        ``DENY`` rule keyed on it, so the call falls through to deny-by-default. That is the same
        answer a malformed capability gets, and it tells a planner nothing about *why* the content
        of a descriptive relationship was what refused it.
        """
        decision = upload(case_demanding(INSTRUCTIONS, INSTRUCTIONS), INSTRUCTIONS)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.NO_RULE_MATCH
        assert decision.matched_rule_id is None


class TestTheDestinationStillDecidesSeparately:
    """D. Getting the document right does not make the destination anybody's choice."""

    def test_the_correct_document_to_an_untrusted_portal_is_still_denied(self) -> None:
        """The portal the poisoned notice advertises, with the genuinely correct document."""
        decision = upload(case_demanding(RECEIPT, RECEIPT), RECEIPT, portal_id="PORTAL-MG-TRANSFER")

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED

    def test_the_two_questions_are_answered_by_different_rules(self) -> None:
        """A destination refusal and a document refusal are not the same finding.

        ``DENY`` is evaluated first and unconditionally, so the destination is settled before the
        document relationship is consulted at all — which is why a case that is wrong in both ways
        reports the destination.
        """
        both_wrong = upload(case_demanding(POISONED), POISONED, portal_id="PORTAL-MG-TRANSFER")

        assert both_wrong.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert both_wrong.matched_rule_id == "deny-upload-to-untrusted-portal"


class TestTheRelationshipIsWhatRefusesThese:
    """E. The non-vacuity proof. Remove the check and the refusals go away.

    Built by filtering the shipped tuple rather than by restating it, so the mutant cannot drift
    from the thing it is a mutant of, and so a check renamed here fails loudly instead of silently
    mutating nothing.
    """

    @pytest.fixture
    def without_the_relationship(self, monkeypatch: pytest.MonkeyPatch) -> None:
        removed = tuple(
            check
            for check in extraction.CROSS_CHECKS
            if check.name != "required-document-answers-the-demand"
        )
        assert len(removed) == len(extraction.CROSS_CHECKS) - 1, (
            "the check being mutated is not there"
        )
        monkeypatch.setattr(extraction, "CROSS_CHECKS", removed)

    @pytest.mark.parametrize(
        "demanded", [INSTRUCTIONS, POISONED], ids=["held-but-wrong", "poisoned"]
    )
    def test_without_it_the_wrong_document_reaches_allow(
        self, without_the_relationship: None, demanded: str
    ) -> None:
        """The defect, reproduced. This is what the shipped system did before the check existed."""
        case = case_demanding(demanded, demanded)

        assert trust_of(case, "document_id", demanded) is TrustLevel.CONTROLLED
        assert upload(case, demanded).verdict is Verdict.ALLOW

    def test_without_it_a_document_this_case_never_read_reaches_allow(
        self, without_the_relationship: None
    ) -> None:
        """The sharpest shape: a letter naming a document from somewhere else entirely."""
        case = case_demanding(REFERRAL)

        assert upload(case, REFERRAL).verdict is Verdict.ALLOW

    def test_the_mutant_changes_nothing_else(self, without_the_relationship: None) -> None:
        """Bounding the mutant. The legitimate upload and the destination guard are untouched.

        Without this, a mutant that broke everything would satisfy the tests above for the wrong
        reason — the refusals would be gone because nothing works, not because the relationship is
        what refuses them.
        """
        assert upload(case_demanding(RECEIPT, RECEIPT), RECEIPT).verdict is Verdict.ALLOW
        assert (
            upload(
                case_demanding(RECEIPT, RECEIPT), RECEIPT, portal_id="PORTAL-MG-TRANSFER"
            ).verdict
            is Verdict.DENY
        )


class TestTheCheckIsWiredWhereTheOthersAre:
    """The declaration itself, so a check that stops being registered fails here."""

    def test_it_gates_the_document_identifier_and_nothing_else(self) -> None:
        check = next(
            check
            for check in extraction.CROSS_CHECKS
            if check.name == "required-document-answers-the-demand"
        )
        assert check.gates == frozenset({"document_id"})

    def test_the_missing_document_case_is_reported_by_the_check_whose_subject_it_is(self) -> None:
        """A demanded document CareLoop does not hold is still ``required-document-is-held``.

        The new check skips an unheld id rather than reporting it as unanswered, so the two
        findings stay distinguishable in the timeline: *we do not have it* and *it does not answer
        the demand* are different things to tell a caregiver.
        """
        case = case_demanding("doc-4242-nonexistent")
        blocked = extraction.cross_check_failures(case, default_document_store())

        assert blocked["document_id"][0] == "required-document-is-held"
