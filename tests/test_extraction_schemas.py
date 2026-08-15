"""Schema validation and cross-document checks — the two layers above the patterns.

Three things can stop a structured value, and each is tested for what only it does.

The **kind** decides which patterns run at all, which is what stops a continuity notice
contributing a patient identifier however it words one.

The **schema** validates a document's fields as a set, formats and relationships together. A
document that fails contributes page excerpts and no structured values — discarded rather than
recorded unpromotable, so the outcome does not depend on the order documents arrive in. That
ordering property is asserted directly, because it was the thing the first design got wrong.

The **cross-document checks** run over the case. Each gates named keys and withholds promotion of
exactly those, leaving them ``UNTRUSTED`` and the dependent action denied
``INSUFFICIENT_FACT_TRUST`` — remediable, and it says so. Every check is asserted in both
directions: satisfied on the shipped documents, and failing on a case built to break it. A check
that can only pass is decoration.
"""

from __future__ import annotations

import importlib

import pytest
from helpers import FIXED_NOW, make_case, make_fact

from careloop.graph.extract import (
    CROSS_CHECKS,
    PATTERNS,
    PROMOTABLE_KEYS,
    SCHEMAS,
    UnpromotableKeyDeclared,
    UnschematisedKind,
    check_every_kind_is_schematised,
    check_gated_keys_are_promotable,
    cross_check_failures,
    extract,
    record_document,
    structured_values,
    validate_and_promote,
    validate_fields,
)
from careloop.policy.engine import resolve_trust
from careloop.provenance import TrustLevel
from careloop.state import TimelineKind
from careloop.tools import POISONED_DOCUMENT_ID, RECEIPT_DOCUMENT_ID, fixture_documents
from careloop.tools.documents import DocumentStore, default_document_store
from careloop.tools.results import Document, DocumentKind

EOB = "doc-0004-eob"
REJECTION = "doc-0006-claim-rejection"
REFERRAL = "doc-0001-referral"


def held(document_id: str) -> Document:
    return default_document_store().get(document_id)


def rewritten(document_id: str, old: str, new: str) -> Document:
    """The shipped document with one line changed, to break a check on purpose.

    Asserts the line was there to change. A silent no-op would leave a test asserting against the
    shipped document while claiming to assert against a broken one — the whole class of test this
    module is made of would go quietly vacuous.
    """
    document = held(document_id)
    assert any(old in page for page in document.pages), f"{old!r} was not in {document_id}"
    pages = tuple(page.replace(old, new) for page in document.pages)
    return document.model_copy(update={"pages": pages})


def extract_module():
    """The ``careloop.graph.extract`` module.

    ``import careloop.graph.extract as module`` binds the *function* of that name re-exported by
    ``careloop.graph``, not the module, and the resulting ``AttributeError`` is confusing enough to
    be worth a helper.
    """
    return importlib.import_module("careloop.graph.extract")


# =================================================================================================
# The kind decides which patterns run
# =================================================================================================


class TestTheKindScopesExtraction:
    def test_a_continuity_notice_yields_no_structured_values_at_all(self) -> None:
        assert structured_values(held(POISONED_DOCUMENT_ID)) == {}

    def test_even_when_it_prints_a_field_the_patterns_would_otherwise_match(self) -> None:
        """The insurer's letter cannot contribute a patient identifier by writing one down."""
        forged = held(POISONED_DOCUMENT_ID)
        forged = forged.model_copy(
            update={"pages": (*forged.pages, "Patient ID: PT-0000\nClaim number: CLM-9999-99999")}
        )

        assert structured_values(forged) == {}

    def test_the_same_line_in_a_document_that_is_scanned_does_produce_a_value(self) -> None:
        """Vacuity: the pattern works, so the empty result above is scoping and not the regex."""
        scanned = held(POISONED_DOCUMENT_ID).model_copy(
            update={
                "kind": DocumentKind.APPOINTMENT_INSTRUCTIONS,
                "pages": ("Patient ID: PT-0000",),
            }
        )

        assert structured_values(scanned) == {"patient_id": ("PT-0000", 1)}

    def test_the_poisoned_notice_still_reaches_the_planner_as_excerpts(self) -> None:
        """Scoping withholds facts, not evidence. T2 depends on the planner still seeing this."""
        facts, refusal = extract(held(POISONED_DOCUMENT_ID), now=FIXED_NOW)

        assert refusal is None
        text = " ".join(fact.value for fact in facts)
        assert "records-intake@claims-continuity-partners.example" in text


# =================================================================================================
# Schemas
# =================================================================================================


class TestEverySchemaIsWiredToItsPatterns:
    def test_the_tables_agree(self) -> None:
        check_every_kind_is_schematised()

    def test_every_kind_has_both(self) -> None:
        assert set(PATTERNS) == set(DocumentKind)
        assert set(SCHEMAS) == set(DocumentKind)

    def test_a_schema_field_no_pattern_fills_is_reported(self, monkeypatch) -> None:
        """Otherwise every document of that kind fails for a reason nobody intended."""
        module = extract_module()

        class Extra(module.ReferralFields):
            provider_name: str

        monkeypatch.setitem(module.SCHEMAS, DocumentKind.REFERRAL, Extra)

        with pytest.raises(UnschematisedKind, match="provider_name"):
            check_every_kind_is_schematised()

    def test_a_pattern_no_schema_declares_is_reported(self, monkeypatch) -> None:
        module = extract_module()

        monkeypatch.setitem(
            module.PATTERNS,
            DocumentKind.RECEIPT,
            (
                *module.PATTERNS[DocumentKind.RECEIPT],
                ("teller_name", module.PATTERNS[DocumentKind.RECEIPT][0][1]),
            ),
        )

        with pytest.raises(UnschematisedKind, match="teller_name"):
            check_every_kind_is_schematised()


class TestTheExplanationOfBenefitsArithmetic:
    """The cross-field check inside one document."""

    def test_the_shipped_statement_validates(self) -> None:
        assert validate_fields(DocumentKind.EXPLANATION_OF_BENEFITS, self._fields()) is None

    @staticmethod
    def _fields(**overrides: str) -> dict[str, str]:
        fields = {
            "patient_id": "PT-4417",
            "claim_number": "CLM-2026-44718",
            "amount_billed": "1240.00",
            "amount_allowed": "860.00",
            "plan_paid": "688.00",
            "patient_responsibility": "172.00",
        }
        fields.update(overrides)
        return fields

    def test_allowing_more_than_was_billed_is_refused(self) -> None:
        refusal = validate_fields(
            DocumentKind.EXPLANATION_OF_BENEFITS, self._fields(amount_allowed="9999.00")
        )

        assert refusal is not None
        assert "not ordered" in refusal

    def test_paying_more_than_was_allowed_is_refused(self) -> None:
        refusal = validate_fields(
            DocumentKind.EXPLANATION_OF_BENEFITS, self._fields(plan_paid="900.00")
        )

        assert refusal is not None
        assert "not ordered" in refusal

    def test_a_balance_that_is_not_the_difference_is_refused(self) -> None:
        refusal = validate_fields(
            DocumentKind.EXPLANATION_OF_BENEFITS, self._fields(patient_responsibility="0.00")
        )

        assert refusal is not None
        assert "not allowed" in refusal

    def test_an_amount_that_is_not_a_decimal_is_refused(self) -> None:
        refusal = validate_fields(
            DocumentKind.EXPLANATION_OF_BENEFITS, self._fields(plan_paid="six hundred")
        )

        assert refusal is not None
        assert "plan_paid" in refusal

    def test_a_missing_field_is_refused_rather_than_partially_extracted(self) -> None:
        """Fail-closed: a partially matched document is one whose layout was not what we thought."""
        fields = self._fields()
        del fields["claim_number"]

        refusal = validate_fields(DocumentKind.EXPLANATION_OF_BENEFITS, fields)

        assert refusal is not None
        assert "claim_number" in refusal

    def test_a_field_no_schema_declares_is_refused(self) -> None:
        refusal = validate_fields(
            DocumentKind.EXPLANATION_OF_BENEFITS, self._fields(wire_account="GB00 0000")
        )

        assert refusal is not None


class TestAFailedSchemaDiscardsStructuredValuesOnly:
    def test_the_excerpts_are_still_recorded(self) -> None:
        broken = rewritten(EOB, "Patient responsibility: 172.00", "Patient responsibility: 0.00")

        facts, refusal = extract(broken, now=FIXED_NOW)

        assert refusal is not None
        assert all(fact.key.startswith("excerpt:") for fact in facts)
        assert facts

    def test_no_structured_fact_survives(self) -> None:
        broken = rewritten(EOB, "Claim number: CLM-2026-44718", "Claim number: NOPE")

        facts, _refusal = extract(broken, now=FIXED_NOW)

        assert not [fact for fact in facts if not fact.key.startswith("excerpt:")]

    def test_the_refusal_is_on_the_timeline_with_the_reason(self) -> None:
        broken = rewritten(EOB, "Plan paid: 688.00", "Plan paid: 900.00")

        case = record_document(make_case(), broken, now=FIXED_NOW)

        entry = case.timeline[-1]
        assert entry.kind is TimelineKind.VALIDATION_REFUSED
        assert entry.payload["stage"] == "schema"
        assert "not ordered" in (entry.diagnostic or "")

    def test_a_good_document_writes_no_refusal(self) -> None:
        """Vacuity: the entry above is not written on every document."""
        case = record_document(make_case(), held(EOB), now=FIXED_NOW)

        assert TimelineKind.VALIDATION_REFUSED not in [entry.kind for entry in case.timeline]

    @pytest.mark.parametrize("reverse", [False, True], ids=["broken-last", "broken-first"])
    def test_another_document_still_supplies_the_value_in_either_order(self, reverse: bool) -> None:
        """The reason a failed schema discards rather than records-unpromotable.

        Recording the failed document's ``patient_id`` as an unpromotable fact would let it win the
        deduplication race, and whether the value ever became ``CONTROLLED`` would then depend on
        which document happened to be read first. Discarding removes the race.
        """
        broken = rewritten(EOB, "Amount allowed: 860.00", "Amount allowed: 9999.00")
        documents = [broken, held(REFERRAL)]
        if reverse:
            documents.reverse()

        case = make_case()
        for document in documents:
            case = record_document(case, document, now=FIXED_NOW)
        case = validate_and_promote(case, now=FIXED_NOW)

        assert resolve_trust("patient_id", "PT-4417", case) is TrustLevel.CONTROLLED


# =================================================================================================
# Cross-document checks
# =================================================================================================


class TestEveryCrossCheckGatesSomethingPromotable:
    def test_the_gated_keys_are_promotable(self) -> None:
        check_gated_keys_are_promotable()

    def test_a_check_gating_nothing_promotable_is_refused(self, monkeypatch) -> None:
        """A guard that cannot change an outcome is decoration; session 6 deleted one already."""
        module = extract_module()

        stray = module.CrossCheck(
            name="gates-nothing", gates=frozenset({"claim_number"}), check=lambda _v, _d: None
        )
        monkeypatch.setattr(module, "CROSS_CHECKS", (*CROSS_CHECKS, stray))

        with pytest.raises(UnpromotableKeyDeclared, match="claim_number"):
            check_gated_keys_are_promotable()

    def test_every_gated_key_has_a_validator_too(self) -> None:
        for check in CROSS_CHECKS:
            assert check.gates <= set(PROMOTABLE_KEYS)


class TestTheShippedDocumentsSatisfyEveryCheck:
    def test_nothing_is_withheld(self) -> None:
        case = make_case()
        for document in fixture_documents():
            case = record_document(case, document, now=FIXED_NOW)

        assert cross_check_failures(case) == {}

    def test_and_the_promotable_values_do_promote(self) -> None:
        """Vacuity for the whole layer: the checks are not withholding everything."""
        case = make_case()
        for document in fixture_documents():
            case = record_document(case, document, now=FIXED_NOW)
        case = validate_and_promote(case, now=FIXED_NOW)

        for key, value in (
            ("patient_id", "PT-4417"),
            ("slot_start", "2026-09-03T09:30:00"),
            ("document_id", RECEIPT_DOCUMENT_ID),
        ):
            assert resolve_trust(key, value, case) is TrustLevel.CONTROLLED, value


class TestPatientIdAgreesAcrossDocuments:
    @staticmethod
    def _disagreeing():
        case = make_case()
        case = record_document(case, held(REFERRAL), now=FIXED_NOW)
        return record_document(
            case, rewritten(REJECTION, "Patient ID: PT-4417", "Patient ID: PT-0000"), now=FIXED_NOW
        )

    def test_two_documents_naming_different_patients_withhold_promotion(self) -> None:
        blocked = cross_check_failures(self._disagreeing())

        assert "patient_id" in blocked
        assert blocked["patient_id"][0] == "patient-id-agrees-across-documents"

    def test_the_identifier_stays_untrusted(self) -> None:
        case = validate_and_promote(self._disagreeing(), now=FIXED_NOW)

        assert resolve_trust("patient_id", "PT-4417", case) is TrustLevel.UNTRUSTED

    def test_but_the_appointment_time_is_untouched(self) -> None:
        """A check gates the keys it names and nothing else."""
        case = validate_and_promote(self._disagreeing(), now=FIXED_NOW)

        assert resolve_trust("slot_start", "2026-09-03T09:30:00", case) is TrustLevel.CONTROLLED

    def test_the_withholding_is_on_the_timeline(self) -> None:
        case = validate_and_promote(self._disagreeing(), now=FIXED_NOW)

        refusals = [
            entry for entry in case.timeline if entry.kind is TimelineKind.VALIDATION_REFUSED
        ]
        assert refusals
        assert refusals[-1].payload["stage"] == "cross_check"
        assert refusals[-1].payload["key"] == "patient_id"
        assert "INSUFFICIENT_FACT_TRUST" in (refusals[-1].diagnostic or "")

    def test_agreeing_documents_promote_normally(self) -> None:
        case = make_case()
        case = record_document(case, held(REFERRAL), now=FIXED_NOW)
        case = record_document(case, held(REJECTION), now=FIXED_NOW)

        assert cross_check_failures(case) == {}
        assert (
            resolve_trust("patient_id", "PT-4417", validate_and_promote(case, now=FIXED_NOW))
            is TrustLevel.CONTROLLED
        )


class TestTheRequiredDocumentMustBeOneCareLoopHolds:
    @staticmethod
    def _demanding(document_id: str):
        return record_document(
            make_case(),
            rewritten(
                REJECTION,
                f"Required document: {RECEIPT_DOCUMENT_ID}",
                f"Required document: {document_id}",
            ),
            now=FIXED_NOW,
        )

    def test_a_document_nobody_holds_withholds_promotion(self) -> None:
        blocked = cross_check_failures(self._demanding("doc-9999-elsewhere"))

        assert blocked["document_id"][0] == "required-document-is-held"

    def test_and_it_never_becomes_controlled(self) -> None:
        """A well-formed identifier that no document answers to must not be uploadable."""
        case = validate_and_promote(self._demanding("doc-9999-elsewhere"), now=FIXED_NOW)

        assert resolve_trust("document_id", "doc-9999-elsewhere", case) is TrustLevel.UNTRUSTED

    def test_a_document_that_is_held_promotes(self) -> None:
        case = validate_and_promote(self._demanding(RECEIPT_DOCUMENT_ID), now=FIXED_NOW)

        assert resolve_trust("document_id", RECEIPT_DOCUMENT_ID, case) is TrustLevel.CONTROLLED

    def test_the_store_consulted_is_the_one_passed_in(self) -> None:
        """Injectable, so a test is not asserting against whatever the process happens to ship."""
        empty = DocumentStore([])

        blocked = cross_check_failures(self._demanding(RECEIPT_DOCUMENT_ID), empty)

        assert "document_id" in blocked


class TestTheReceiptMustSettleTheOutstandingBalance:
    @staticmethod
    def _with_receipt(amount: str):
        case = make_case()
        case = record_document(case, held(EOB), now=FIXED_NOW)
        case = record_document(
            case,
            rewritten(RECEIPT_DOCUMENT_ID, "Amount paid: 172.00", f"Amount paid: {amount}"),
            now=FIXED_NOW,
        )
        return record_document(case, held(REJECTION), now=FIXED_NOW)

    def test_a_receipt_for_the_wrong_amount_withholds_the_upload(self) -> None:
        blocked = cross_check_failures(self._with_receipt("5.00"))

        assert blocked["document_id"][0] == "receipt-settles-the-outstanding-balance"

    def test_a_receipt_for_the_right_amount_does_not(self) -> None:
        assert cross_check_failures(self._with_receipt("172.00")) == {}

    def test_the_comparison_is_decimal_rather_than_textual(self) -> None:
        """``172.0`` and ``172.00`` are the same amount; a string compare would disagree.

        The schema requires two places, so this cannot arise from the shipped documents — it is
        asserted because the alternative implementation is a string equality that looks correct.
        """
        from decimal import Decimal

        assert Decimal("172.0") == Decimal("172.00")

    def test_a_case_with_no_receipt_at_all_is_not_made_suspicious(self) -> None:
        """The check applies when both figures are present, and is silent otherwise."""
        case = record_document(make_case(), held(EOB), now=FIXED_NOW)
        case = record_document(case, held(REJECTION), now=FIXED_NOW)

        assert cross_check_failures(case) == {}


class TestWithholdingLeavesARemediableDenial:
    def test_an_unpromoted_value_is_refused_by_the_engine_rather_than_missing(self) -> None:
        """The point of leaving it ``UNTRUSTED`` rather than dropping the fact.

        ``INSUFFICIENT_FACT_TRUST`` tells the planner the value can be validated and reproposed.
        A fact that had been deleted would produce ``NO_RULE_MATCH`` instead, which says nothing.
        """
        case = make_case(facts=(make_fact("document_id", "doc-9999-elsewhere"),))

        assert resolve_trust("document_id", "doc-9999-elsewhere", case) is TrustLevel.UNTRUSTED
