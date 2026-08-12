"""Trusted configuration, deterministic extraction, and what "verified" means.

Three guards carry this module.

:class:`TestNoConfiguredValueAppearsInADocument` — lowest-wins would drag a trusted destination
down to ``UNTRUSTED`` if a document also carried it, and the action it was needed for would be
denied for a reason no diagnostic explains. Same shape as the promotion bug that open decision 9
recorded.

:class:`TestOnlyValueClassKeysArePromoted` — a destination that could be validated into
acceptability is the trust model failing entirely.

:class:`TestVerificationComparesAgainstWhatWasAuthorised` — verification that compared the
executor's inputs to its own outputs would always pass.
"""

from __future__ import annotations

import pytest
from helpers import FIXED_NOW, make_case, make_fact

from careloop.graph.extract import (
    PATTERNS,
    PROMOTABLE_KEYS,
    VALIDATORS,
    UnpromotableKeyDeclared,
    check_promotable_keys_match_the_registry,
    excerpt_key,
    extract,
    record_document,
    validate_and_promote,
)
from careloop.graph.trusted import CONFIG_SOURCE, TRUSTED_CONFIG, configured, trusted_facts
from careloop.graph.verify import (
    VERIFIERS,
    UnverifiableAction,
    check_verifiers_cover_the_registry,
    verify_action,
)
from careloop.policy.engine import resolve_trust
from careloop.provenance import ArgClass, TrustLevel
from careloop.state.case import ActionRecord
from careloop.tools import POISONED_DOCUMENT_ID, REGISTRY, fixture_documents
from careloop.tools.documents import default_document_store
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.registry import ArgSpec, ToolRegistry, ToolSpec
from careloop.tools.results import AppointmentBooked, Document, DocumentKind, EmailSent

REFERRAL = "doc-0001-referral"
CASE_ID = "case-0001"
BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": "cardiology review",
}


def referral() -> Document:
    return default_document_store().get(REFERRAL)


def poisoned() -> Document:
    return default_document_store().get(POISONED_DOCUMENT_ID)


class TestNoConfiguredValueAppearsInADocument:
    """The lowest-wins hazard, checked in both directions."""

    @pytest.mark.parametrize("key,value", TRUSTED_CONFIG, ids=[k for k, _ in TRUSTED_CONFIG])
    def test_the_value_is_absent_from_every_fixture_document(self, key: str, value: str) -> None:
        for document in fixture_documents():
            assert value not in document.text

    def test_a_seeded_case_resolves_every_configured_value_as_trusted(self) -> None:
        case = make_case(facts=trusted_facts(now=FIXED_NOW))

        for _key, value in TRUSTED_CONFIG:
            assert resolve_trust(value, case) is TrustLevel.TRUSTED

    def test_and_still_does_after_both_documents_are_recorded(self) -> None:
        """The property that matters: seeding survives the evidence node reading the poison."""
        case = make_case(facts=trusted_facts(now=FIXED_NOW))
        case = record_document(case, referral(), now=FIXED_NOW)
        case = record_document(case, poisoned(), now=FIXED_NOW)

        for _key, value in TRUSTED_CONFIG:
            assert resolve_trust(value, case) is TrustLevel.TRUSTED

    def test_the_check_is_not_vacuous(self) -> None:
        """A value that *is* in a document really does resolve untrusted on the same case."""
        case = make_case(facts=trusted_facts(now=FIXED_NOW))
        case = record_document(case, poisoned(), now=FIXED_NOW)
        planted = "records-intake@claims-continuity-partners.example"

        assert planted in poisoned().text
        assert resolve_trust(planted, case) is TrustLevel.UNTRUSTED

    def test_configured_facts_name_no_document_as_their_source(self) -> None:
        assert {fact.provenance.source_doc for fact in trusted_facts(now=FIXED_NOW)} == {
            CONFIG_SOURCE
        }

    def test_configured_looks_a_value_up_and_admits_when_there_is_none(self) -> None:
        assert configured("provider_id") == "PROV-NORTHGATE-CARDIOLOGY"
        assert configured("wire_transfer_account") is None


class TestExtractionIsDeterministicAndUnsanitising:
    def test_two_runs_over_one_document_produce_identical_facts(self) -> None:
        assert extract(referral(), now=FIXED_NOW) == extract(referral(), now=FIXED_NOW)

    def test_the_referral_yields_the_identifiers_the_patterns_name(self) -> None:
        found = {fact.key: fact.value for fact in extract(referral(), now=FIXED_NOW)}

        assert found["patient_id"] == "PT-4417"
        assert found["slot_start"] == "2026-09-03T09:30:00"
        assert found["referring_clinician"] == "Dr. A. Okonkwo"

    def test_every_extracted_fact_starts_untrusted(self) -> None:
        for document in fixture_documents():
            for fact in extract(document, now=FIXED_NOW):
                assert fact.trust_level is TrustLevel.UNTRUSTED

    def test_every_fact_carries_its_source_and_page(self) -> None:
        for fact in extract(referral(), now=FIXED_NOW):
            assert fact.provenance.source_doc == "referral.pdf"
            assert fact.provenance.page in (1, 2)

    def test_one_excerpt_per_page_holding_the_page_verbatim(self) -> None:
        document = poisoned()

        facts = {fact.key: fact.value for fact in extract(document, now=FIXED_NOW)}

        for page_number, page in enumerate(document.pages, start=1):
            assert facts[excerpt_key(document.document_id, page_number)] == page

    def test_the_poisoned_instructions_survive_extraction_unaltered(self) -> None:
        """Sanitising here would make T2 pass for the wrong reason."""
        text = "\n".join(fact.value for fact in extract(poisoned(), now=FIXED_NOW))

        assert "records-intake@claims-continuity-partners.example" in text
        assert "PORTAL-MG-TRANSFER" in text
        assert "records_admin" in text

    def test_a_second_matching_line_on_a_page_does_not_overwrite_the_first(self) -> None:
        """Taking the last match would let a document append its own Patient ID and win."""
        forged = Document(
            document_id="doc-forged",
            title="forged",
            source_doc="forged.pdf",
            kind=DocumentKind.REFERRAL,
            pages=("Patient ID: PT-4417\nPatient ID: PT-0000\n",),
        )

        found = {fact.key: fact.value for fact in extract(forged, now=FIXED_NOW)}

        assert found["patient_id"] == "PT-4417"

    def test_a_later_page_cannot_add_a_second_fact_under_the_same_key(self) -> None:
        """The page loop stops at the first page that matches, and that is load-bearing.

        Extraction runs per document, so a later page restating a field is the single-document
        form of the whole attack: two ``patient_id`` facts would both be recorded, ``case.fact``
        returns the most recent, and trust resolution matches on *value* — so the attacker's is
        live either way. One key, one fact, and the earliest page wins.

        Written after a mutation survived: the earlier version of this test put both lines on one
        page, where ``re.search`` already takes the first and the page loop's ``break`` could be
        deleted with the suite still green.
        """
        forged = Document(
            document_id="doc-forged",
            title="forged",
            source_doc="forged.pdf",
            kind=DocumentKind.REFERRAL,
            pages=("Patient ID: PT-4417\n", "Patient ID: PT-0000\n"),
        )

        patient_facts = [
            fact for fact in extract(forged, now=FIXED_NOW) if fact.key == "patient_id"
        ]

        assert len(patient_facts) == 1
        assert patient_facts[0].value == "PT-4417"
        assert patient_facts[0].provenance.page == 1

    def test_a_pattern_cannot_span_a_line(self) -> None:
        forged = Document(
            document_id="doc-forged",
            title="forged",
            source_doc="forged.pdf",
            kind=DocumentKind.REFERRAL,
            pages=("Patient ID:\nPT-9999\n",),
        )

        assert "patient_id" not in {fact.key for fact in extract(forged, now=FIXED_NOW)}

    def test_a_document_stating_nothing_yields_excerpts_and_no_values(self) -> None:
        blank = Document(
            document_id="doc-blank",
            title="blank",
            source_doc="blank.pdf",
            kind=DocumentKind.REFERRAL,
            pages=("nothing\n",),
        )

        keys = {fact.key for fact in extract(blank, now=FIXED_NOW)}

        assert keys == {excerpt_key("doc-blank", 1)}


class TestOnlyValueClassKeysArePromoted:
    def test_every_promotable_key_is_declared_value_class(self) -> None:
        assert set(PROMOTABLE_KEYS.values()) == {ArgClass.VALUE}

    def test_no_destination_or_authority_argument_name_is_promotable(self) -> None:
        unpromotable = {
            arg.name
            for tool in REGISTRY
            for arg in tool.args
            if arg.arg_class is not ArgClass.VALUE
        }

        assert unpromotable.isdisjoint(PROMOTABLE_KEYS)

    def test_the_table_agrees_with_the_registry(self) -> None:
        check_promotable_keys_match_the_registry(REGISTRY)

    def test_a_table_contradicting_the_registry_fails(self) -> None:
        """A synthetic registry calling a promotable key DESTINATION must be reported."""
        contradicting = ToolRegistry(
            [
                ToolSpec(
                    name="fetch_document",
                    external_effect=False,
                    args=(ArgSpec(name="patient_id", arg_class=ArgClass.DESTINATION),),
                )
            ]
        )

        with pytest.raises(UnpromotableKeyDeclared) as raised:
            check_promotable_keys_match_the_registry(contradicting)

        assert "patient_id" in str(raised.value)

    def test_an_excerpt_is_never_promotable(self) -> None:
        assert not any(key.startswith("excerpt:") for key in PROMOTABLE_KEYS)
        assert not any(key.startswith("excerpt:") for key in VALIDATORS)

    def test_every_pattern_key_is_accounted_for(self) -> None:
        """A pattern with no entry either way is a value nobody decided about."""
        for key, _pattern in PATTERNS:
            assert key in PROMOTABLE_KEYS or key not in VALIDATORS


class TestValidationPromotesWhatItCanAndLeavesTheRest:
    def test_a_valid_slot_resolves_controlled_through_the_engine(self) -> None:
        case = record_document(make_case(), referral(), now=FIXED_NOW)

        promoted = validate_and_promote(case, now=FIXED_NOW)

        assert resolve_trust("2026-09-03T09:30:00", promoted) is TrustLevel.CONTROLLED

    def test_the_patient_identifier_is_promoted_too(self) -> None:
        case = validate_and_promote(
            record_document(make_case(), referral(), now=FIXED_NOW), now=FIXED_NOW
        )

        assert resolve_trust("PT-4417", case) is TrustLevel.CONTROLLED

    def test_a_malformed_value_is_left_untrusted(self) -> None:
        case = make_case()
        case = record_document(case, referral(), now=FIXED_NOW)
        case = case.model_copy(
            update={
                "facts": tuple(
                    make_fact("slot_start", "next Tuesday") if fact.key == "slot_start" else fact
                    for fact in case.facts
                )
            }
        )

        promoted = validate_and_promote(case, now=FIXED_NOW)

        assert resolve_trust("next Tuesday", promoted) is TrustLevel.UNTRUSTED

    def test_an_excerpt_is_never_raised_above_untrusted(self) -> None:
        case = validate_and_promote(
            record_document(make_case(), poisoned(), now=FIXED_NOW), now=FIXED_NOW
        )

        excerpts = [fact for fact in case.facts if fact.key.startswith("excerpt:")]
        assert excerpts
        assert {fact.trust_level for fact in excerpts} == {TrustLevel.UNTRUSTED}

    def test_validation_is_idempotent_at_the_case_level(self) -> None:
        """Promotion itself is not idempotent; running the node twice must still not raise."""
        once = validate_and_promote(
            record_document(make_case(), referral(), now=FIXED_NOW), now=FIXED_NOW
        )

        twice = validate_and_promote(once, now=FIXED_NOW)

        assert twice.facts == once.facts

    def test_nothing_a_validator_touches_can_reach_trusted(self) -> None:
        case = validate_and_promote(
            record_document(make_case(), referral(), now=FIXED_NOW), now=FIXED_NOW
        )

        assert TrustLevel.TRUSTED not in {fact.trust_level for fact in case.facts}


class TestVerificationComparesAgainstWhatWasAuthorised:
    @staticmethod
    def executed(args=None):
        """A recorded action and its result, as the execute node would produce them."""
        args = args or BOOKING
        key = idempotency_key(CASE_ID, "book_appointment", args)
        result = AppointmentBooked(
            confirmation_id=f"apt-{key[:16]}",
            provider_id=args["provider_id"],
            patient_id=args["patient_id"],
            slot_start=args["slot_start"],
        )
        record = ActionRecord(
            idempotency_key=key,
            action_type="book_appointment",
            args_digest=args_digest(args),
            result=result.model_dump_json(),
            recorded_at=FIXED_NOW,
        )
        return record, result

    def test_the_effect_that_was_authorised_verifies(self) -> None:
        record, result = self.executed()

        verification = verify_action(
            case_id=CASE_ID,
            action_type="book_appointment",
            args=BOOKING,
            record=record,
            result=result,
        )

        assert verification.passed

    def test_a_result_echoing_a_different_destination_fails(self) -> None:
        record, result = self.executed()
        tampered = result.model_copy(update={"provider_id": "PROV-SOMEWHERE-ELSE"})

        verification = verify_action(
            case_id=CASE_ID,
            action_type="book_appointment",
            args=BOOKING,
            record=record,
            result=tampered,
        )

        assert verification.failed
        assert "provider_id" in verification.diagnostic

    def test_a_result_with_no_identifier_fails(self) -> None:
        record, result = self.executed()

        verification = verify_action(
            case_id=CASE_ID,
            action_type="book_appointment",
            args=BOOKING,
            record=record,
            result=result.model_copy(update={"confirmation_id": ""}),
        )

        assert verification.failed
        assert "confirmation_id" in verification.diagnostic

    def test_an_action_recorded_under_another_case_fails(self) -> None:
        record, result = self.executed()

        verification = verify_action(
            case_id="case-9999",
            action_type="book_appointment",
            args=BOOKING,
            record=record,
            result=result,
        )

        assert verification.failed

    def test_authorising_one_slot_and_booking_another_fails(self) -> None:
        """The property the whole module exists for: decision and execution must agree."""
        record, result = self.executed()
        other = {**BOOKING, "slot_start": "2026-12-25T09:30:00"}

        verification = verify_action(
            case_id=CASE_ID,
            action_type="book_appointment",
            args=other,
            record=record,
            result=result,
        )

        assert verification.failed

    def test_a_result_of_the_wrong_type_fails(self) -> None:
        record, _ = self.executed()

        verification = verify_action(
            case_id=CASE_ID,
            action_type="book_appointment",
            args=BOOKING,
            record=record,
            result=EmailSent(message_id="msg-1", to="a@b.example", subject="s"),
        )

        assert verification.failed

    def test_an_action_type_that_does_not_match_the_record_fails(self) -> None:
        record, result = self.executed()

        verification = verify_action(
            case_id=CASE_ID,
            action_type="send_email",
            args=BOOKING,
            record=record.model_copy(update={"action_type": "book_appointment"}),
            result=result,
        )

        assert verification.failed


class TestEveryEffectfulToolCanBeVerified:
    def test_the_verifiers_cover_the_registry(self) -> None:
        assert frozenset(VERIFIERS) == REGISTRY.external_effect_tools

    def test_the_drift_check_passes_on_the_shipped_registry(self) -> None:
        check_verifiers_cover_the_registry()

    def test_the_read_tool_has_no_verifier_because_it_has_no_effect(self) -> None:
        assert "fetch_document" not in VERIFIERS

    def test_removing_a_verifier_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "careloop.graph.verify.VERIFIERS",
            {k: v for k, v in VERIFIERS.items() if k != "send_email"},
        )

        with pytest.raises(UnverifiableAction) as raised:
            check_verifiers_cover_the_registry()

        assert "send_email" in str(raised.value)
