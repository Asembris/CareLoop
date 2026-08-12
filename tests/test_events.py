"""Inbound events, loaded from JSON, and the scenarios that order them.

Two guards carry this module.

**The JSON files are the source.** Not a convenience copy of something declared in Python — if the
fixture changes, the loaded event changes, and a test asserts exactly that rather than assuming it.

**Every attachment names a document CareLoop holds.** Checked at import, so a typo in an event
fixture fails on ``import careloop.events`` rather than three nodes into a run, where it would look
like a broken evidence node.
"""

from __future__ import annotations

import json

import pytest

from careloop.events.inbound import (
    CLAIM_REJECTION_EVENT,
    CLAIM_REJECTION_EVENT_ID,
    EVENTS,
    FIXTURE_DIR,
    FIXTURE_PACKAGE,
    POISONED_EVENT,
    REFERRAL_EVENT,
    InboundEvent,
    UnknownAttachment,
    check_attachments_are_held,
    event,
    load_events,
)
from careloop.events.simulator import (
    SCENARIOS,
    EventSimulator,
    UnknownScenario,
    check_scenarios_name_real_events,
)
from careloop.tools import POISONED_DOCUMENT_ID, RECEIPT_DOCUMENT_ID
from careloop.tools.documents import DocumentStore, default_document_store


def fixture_path(event_id: str):
    from importlib import resources

    return resources.files(FIXTURE_PACKAGE) / FIXTURE_DIR / f"{event_id}.json"


class TestTheFixturesAreTheSource:
    def test_every_event_comes_from_a_file(self) -> None:
        for event_id in EVENTS:
            assert fixture_path(event_id).is_file()

    def test_the_loaded_event_is_what_the_file_says(self) -> None:
        """Not a Python copy that happens to agree with the JSON."""
        raw = json.loads(fixture_path(CLAIM_REJECTION_EVENT_ID).read_text(encoding="utf-8"))

        assert CLAIM_REJECTION_EVENT.subject == raw["subject"]
        assert CLAIM_REJECTION_EVENT.body == raw["body"]
        assert list(CLAIM_REJECTION_EVENT.document_ids) == raw["document_ids"]

    def test_editing_a_fixture_changes_the_event(self, tmp_path, monkeypatch) -> None:
        """Vacuity for the test above: the loader really does read the file.

        Written against a rebuilt event rather than by patching the package directory, because the
        property under test is that the JSON is parsed into the model — not that
        ``importlib.resources`` works.
        """
        raw = json.loads(fixture_path(CLAIM_REJECTION_EVENT_ID).read_text(encoding="utf-8"))
        raw["subject"] = "something else entirely"

        assert InboundEvent.model_validate(raw).subject == "something else entirely"
        assert CLAIM_REJECTION_EVENT.subject != "something else entirely"

    def test_a_fixture_with_an_undeclared_field_is_refused(self) -> None:
        """A typo in a key would otherwise produce an event quietly missing its attachments."""
        raw = json.loads(fixture_path(CLAIM_REJECTION_EVENT_ID).read_text(encoding="utf-8"))
        raw["attachments"] = ["doc-0005-receipt"]

        with pytest.raises(Exception, match="attachments"):
            InboundEvent.model_validate(raw)

    def test_the_filename_is_the_event_id(self) -> None:
        """So a fixture can be found without opening every file."""
        for event_id, loaded in EVENTS.items():
            assert loaded.event_id == event_id

    def test_loading_twice_gives_the_same_events(self) -> None:
        assert load_events() == EVENTS

    def test_events_are_frozen(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            REFERRAL_EVENT.subject = "edited"


class TestTheThreeEvents:
    def test_the_referral_brings_the_letter_and_the_instructions(self) -> None:
        assert REFERRAL_EVENT.document_ids == (
            "doc-0001-referral",
            "doc-0003-appointment-instructions",
        )

    def test_the_poisoned_event_is_an_ordinary_one_with_a_second_attachment(self) -> None:
        """The attack does not arrive as a strange message."""
        assert POISONED_DOCUMENT_ID in POISONED_EVENT.document_ids
        assert "doc-0001-referral" in POISONED_EVENT.document_ids

    def test_the_claim_rejection_brings_the_document_it_demands(self) -> None:
        """The resume needs the receipt on the case, not merely named by the rejection."""
        assert "doc-0006-claim-rejection" in CLAIM_REJECTION_EVENT.document_ids
        assert RECEIPT_DOCUMENT_ID in CLAIM_REJECTION_EVENT.document_ids
        assert "doc-0004-eob" in CLAIM_REJECTION_EVENT.document_ids

    def test_triage_reads_subject_and_body_together(self) -> None:
        assert REFERRAL_EVENT.subject in REFERRAL_EVENT.text
        assert REFERRAL_EVENT.body in REFERRAL_EVENT.text

    def test_no_event_names_a_case(self) -> None:
        """Untrusted input does not get to choose what it reopens; the store answers that."""
        assert "case_id" not in InboundEvent.model_fields

    def test_lookup_raises_rather_than_returning_none(self) -> None:
        assert event(CLAIM_REJECTION_EVENT_ID) is CLAIM_REJECTION_EVENT
        with pytest.raises(KeyError):
            event("evt-9999-nonexistent")


class TestEveryAttachmentIsHeld:
    def test_the_shipped_events_all_check_out(self) -> None:
        check_attachments_are_held()

    def test_an_event_naming_an_unheld_document_is_reported(self) -> None:
        """Vacuity: an import-time guard that cannot fire is decoration."""
        stray = InboundEvent(
            event_id="evt-9999-stray",
            subject="s",
            body="b",
            document_ids=("doc-9999-elsewhere",),
        )

        with pytest.raises(UnknownAttachment, match="doc-9999-elsewhere"):
            check_attachments_are_held({stray.event_id: stray})

    def test_the_store_consulted_is_the_one_passed_in(self) -> None:
        with pytest.raises(UnknownAttachment):
            check_attachments_are_held(EVENTS, DocumentStore([]))

    def test_and_the_real_store_holds_them(self) -> None:
        held = default_document_store()
        for loaded in EVENTS.values():
            for document_id in loaded.document_ids:
                assert document_id in held


class TestTheSimulator:
    @pytest.fixture
    def simulator(self) -> EventSimulator:
        return EventSimulator()

    def test_it_can_deliver_every_shipped_event(self, simulator: EventSimulator) -> None:
        assert simulator.event_ids == frozenset(EVENTS)

    def test_a_scenario_is_delivered_in_order(self, simulator: EventSimulator) -> None:
        """The order is the demo: a referral, then weeks later a rejection."""
        delivered = [loaded.event_id for loaded in simulator.deliver("resume")]

        assert delivered == ["evt-0001-referral", CLAIM_REJECTION_EVENT_ID]

    def test_every_scenario_is_non_empty(self) -> None:
        for name in SCENARIOS:
            assert EventSimulator().scenario(name)

    def test_an_unknown_scenario_raises_and_says_what_exists(
        self, simulator: EventSimulator
    ) -> None:
        """A typo returning nothing would be a demo that runs and does nothing."""
        with pytest.raises(UnknownScenario, match="resume"):
            simulator.scenario("resumee")

    def test_scenarios_name_events_that_exist(self) -> None:
        check_scenarios_name_real_events()

    def test_a_scenario_naming_a_missing_event_is_reported(self) -> None:
        with pytest.raises(UnknownScenario, match="happy"):
            check_scenarios_name_real_events({})

    def test_the_simulator_holds_no_case_state(self) -> None:
        """It answers what arrived, not what should happen to it."""
        assert not [name for name in dir(EventSimulator) if "case" in name.lower()]
