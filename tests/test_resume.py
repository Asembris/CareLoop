"""T5 — one case, three processes, days apart.

The flagship behaviour: a case suspended, the process gone, and later an external event reopens the
**same case** and completes it.

**These are real operating-system processes.** Each phase is a ``subprocess`` running
``scripts/resume_demo.py``, and each one exits before the next begins. Nothing survives but the
SQLite file — no in-memory case, no warm executor ledger, no Strands session, no module state. T3
already replays across a serialise/rehydrate boundary inside one process; this is the stronger
claim, and it is stronger for exactly the reason T3's is stronger than an in-memory replay. A
resume reconstructed inside one process proves nothing this behaviour exists for.

The story, and the dates the audit trail carries:

* **Friday 4 September** — a cardiology referral arrives. The planner proposes an email to the
  caregiver, policy requires a human decision, the case parks at ``AWAITING_HUMAN``. The process
  exits with nothing sent.
* **Monday 7 September** — a new process. The caregiver approves; the case resumes from the store
  and sends the email. ``DONE``.
* **Monday 21 September** — a new process. A claim rejection arrives asking for the payment
  receipt. The **same case id** reopens, reads three new documents, uploads the receipt to the
  trusted portal, verifies it. ``DONE`` again, with two actions on the record and the email not
  sent a second time.

What is asserted, per the deliverable: same case id, prior evidence intact with its provenance, no
duplicate external effects, and a timeline continuous across both boundaries.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import careloop
from careloop.events.inbound import CLAIM_REJECTION_EVENT_ID, REFERRAL_EVENT_ID
from careloop.provenance import TrustLevel
from careloop.state.case import Case, CaseState, TimelineKind
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools import RECEIPT_DOCUMENT_ID

# Three interpreter starts, each importing Strands and parsing PDFs. Generous, and still a runaway
# detector: the whole thing takes a couple of seconds.
pytestmark = pytest.mark.timeout(180)

CASE_ID = "case-0001"
REPO_ROOT = Path(careloop.__file__).parent.parent
DEMO = REPO_ROOT / "scripts" / "resume_demo.py"

FRIDAY = "2026-09-04T16:40:00+00:00"
MONDAY = "2026-09-07T09:15:00+00:00"
FORTNIGHT_LATER = "2026-09-21T11:02:00+00:00"

PHASES = ((1, FRIDAY), (2, MONDAY), (3, FORTNIGHT_LATER))


def run_phase(phase: int, database: Path, when: str) -> str:
    """Run one phase in its own process and return what it printed.

    Fails with the child's own stderr rather than an exit code, because a traceback three
    processes away is otherwise very hard to read.
    """
    completed = subprocess.run(
        [sys.executable, str(DEMO), "--phase", str(phase), "--db", str(database), "--now", when],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        f"phase {phase} exited {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    return completed.stdout


def load(database: Path) -> Case:
    """Read the case back, in this process, from the file the children wrote."""
    store = SqliteCaseStore(database)
    try:
        return store.get(CASE_ID)
    finally:
        store.close()


@pytest.fixture(scope="module")
def resumed(tmp_path_factory) -> tuple[Path, dict[int, str], dict[int, Case]]:
    """Run the three phases, each in its own process, and capture the case after each.

    Module-scoped: three interpreter starts is enough to pay for once. Every test below reads the
    same recorded outcome, so no test can accidentally depend on having run the phases itself.
    """
    database = tmp_path_factory.mktemp("resume") / "case.sqlite3"
    output: dict[int, str] = {}
    snapshots: dict[int, Case] = {}
    for phase, when in PHASES:
        output[phase] = run_phase(phase, database, when)
        snapshots[phase] = load(database)
    return database, output, snapshots


class TestTheProcessBoundaryIsReal:
    """Without this, everything below is a test of one process talking to itself."""

    def test_each_phase_ran_in_a_separate_interpreter(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, output, _snapshots = resumed

        for phase, when in PHASES:
            assert f"phase {phase}" in output[phase]
            assert when in output[phase]

    def test_nothing_but_the_file_carries_the_case_forward(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """The child processes share no memory with this one, and the file is what is left."""
        database, _output, _snapshots = resumed

        assert database.is_file()
        assert load(database).case_id == CASE_ID

    def test_a_phase_run_against_an_empty_database_cannot_succeed(self, tmp_path) -> None:
        """Vacuity: phase 2 depends on what phase 1 left behind, rather than rebuilding it.

        If phase 2 could reconstruct the case itself, the boundary would be decorative.
        """
        empty = tmp_path / "nothing.sqlite3"
        completed = subprocess.run(
            [sys.executable, str(DEMO), "--phase", "2", "--db", str(empty), "--now", MONDAY],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
            check=False,
        )

        assert completed.returncode != 0


class TestPhaseOneParksAndExits:
    def test_the_case_is_awaiting_a_human(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        assert snapshots[1].state is CaseState.AWAITING_HUMAN
        assert snapshots[1].pending_approval() is not None

    def test_nothing_external_happened_yet(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, output, snapshots = resumed

        assert snapshots[1].actions == ()
        assert "external calls : 0" in output[1]

    def test_the_evidence_carries_its_page_citations(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """The demo beat, in the audit trail rather than in a docstring."""
        _database, _output, snapshots = resumed

        summaries = [entry.summary for entry in snapshots[1].timeline]
        assert any("source: referral.pdf p.1" in summary for summary in summaries)
        assert any("source: referral.pdf p.2" in summary for summary in summaries)


class TestPhaseTwoResumesInAFreshProcess:
    def test_the_case_finishes(self, resumed: tuple[Path, dict[int, str], dict[int, Case]]) -> None:
        _database, _output, snapshots = resumed

        assert snapshots[2].state is CaseState.DONE

    def test_it_reached_done_through_verifying(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """Invariant 4 across a process boundary, where it would be easiest to lose."""
        _database, _output, snapshots = resumed

        into_done = [
            entry.payload
            for entry in snapshots[2].timeline
            if entry.kind is TimelineKind.STATE_TRANSITION and entry.payload["to"] == "DONE"
        ]
        assert [move["from"] for move in into_done] == ["VERIFYING"]

    def test_the_email_was_sent_exactly_once(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, output, snapshots = resumed

        assert "external calls : 1" in output[2]
        assert [action.action_type for action in snapshots[2].actions] == ["send_email"]

    def test_the_run_never_re_planned(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """It resumed at execution, so the planner was never asked anything in this process."""
        _database, output, _snapshots = resumed

        assert "dispatch -> execute -> verify" in output[2]


class TestPhaseThreeReopensTheSameCase:
    def test_it_is_the_same_case_id(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        assert snapshots[3].case_id == snapshots[1].case_id == CASE_ID

    def test_it_reopened_rather_than_recreated(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """Four things a recreated case could not have.

        A fresh case would carry the follow-up as its opening event, a later ``created_at``, an
        empty action list, and a timeline starting at zero. Each is asserted, so "reopened" is not
        resting on the case id alone — an id is easy to reuse.
        """
        _database, _output, snapshots = resumed
        first, third = snapshots[1], snapshots[3]

        assert third.event_id == REFERRAL_EVENT_ID
        assert third.created_at == first.created_at
        assert len(third.actions) == 2
        assert third.timeline[: len(first.timeline)] == first.timeline

    def test_the_reopen_names_the_event_that_caused_it(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        reopened = [
            entry for entry in snapshots[3].timeline if entry.kind is TimelineKind.CASE_REOPENED
        ]
        assert len(reopened) == 1
        assert reopened[0].payload["event_id"] == CLAIM_REJECTION_EVENT_ID
        assert reopened[0].payload["opened_by"] == REFERRAL_EVENT_ID

    def test_the_case_finishes_again(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        assert snapshots[3].state is CaseState.DONE

    def test_the_upload_was_authorised_by_evidence_the_new_documents_supplied(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """The reopened case did real work: a value extracted, validated and cross-checked.

        ``document_id`` is only ``CONTROLLED`` because the claim rejection named a document
        CareLoop holds *and* the receipt settles the balance the benefits statement left
        outstanding. Without both, the upload would have been denied ``INSUFFICIENT_FACT_TRUST``.
        """
        _database, _output, snapshots = resumed

        by_key = {fact.key: fact for fact in snapshots[3].facts}
        assert by_key["document_id"].value == RECEIPT_DOCUMENT_ID
        assert by_key["document_id"].trust_level is TrustLevel.CONTROLLED
        assert by_key["document_id"].provenance.source_doc == "claim-rejection.pdf"


class TestPriorEvidenceSurvivesWithItsProvenance:
    def test_every_fact_from_the_first_phase_is_still_there(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        before = {(fact.key, fact.value) for fact in snapshots[1].facts}
        after = {(fact.key, fact.value) for fact in snapshots[3].facts}

        assert before <= after

    def test_and_carries_the_same_provenance_it_did_three_weeks_earlier(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """Invariant 3 across two process boundaries: ``{source_doc, page, trust_level}`` intact."""
        _database, _output, snapshots = resumed

        before = {fact.key: fact for fact in snapshots[1].facts}
        after = {fact.key: fact for fact in snapshots[3].facts}

        for key, fact in before.items():
            assert after[key].provenance == fact.provenance, key
            assert after[key].trust_level is fact.trust_level, key

    def test_the_validated_slot_is_still_controlled_and_still_says_which_page(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        slot = next(fact for fact in snapshots[3].facts if fact.key == "slot_start")

        assert slot.trust_level is TrustLevel.CONTROLLED
        assert slot.validated_by == "iso_datetime"
        assert slot.citation == "referral.pdf p.2"

    def test_the_follow_up_did_not_re_record_what_the_case_already_knew(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """Open decision 9's third face, in the run that motivated it.

        Three of the follow-up's documents restate ``Patient ID: PT-4417``. Recording any of them
        would have put an ``UNTRUSTED`` copy beside the ``CONTROLLED`` original and, under
        lowest-wins, silently un-validated a value the case had held for weeks.
        """
        _database, _output, snapshots = resumed

        patient_facts = [fact for fact in snapshots[3].facts if fact.key == "patient_id"]
        assert len(patient_facts) == 1
        assert patient_facts[0].trust_level is TrustLevel.CONTROLLED

        skipped = [
            entry
            for entry in snapshots[3].timeline
            if entry.kind is TimelineKind.EVIDENCE_DUPLICATE_SKIPPED
        ]
        assert len(skipped) >= 3


class TestNoDuplicateExternalEffects:
    def test_the_third_process_performed_exactly_one_effect(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """Its executor ledger started empty, so the email could have been sent again."""
        _database, output, _snapshots = resumed

        assert "external calls : 1" in output[3]

    def test_the_case_carries_two_actions_and_no_more(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        assert [action.action_type for action in snapshots[3].actions] == [
            "send_email",
            "upload_document",
        ]

    def test_the_email_action_is_byte_identical_to_the_one_recorded_a_fortnight_ago(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """Invariant 9 across the boundary: no clock in a result, so replay is comparable."""
        _database, _output, snapshots = resumed

        before = snapshots[2].actions[0]
        after = next(
            action for action in snapshots[3].actions if action.action_type == "send_email"
        )

        assert after == before

    def test_redelivering_the_same_follow_up_performs_nothing_new(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]], tmp_path
    ) -> None:
        """A fourth process, the same letter arriving twice.

        The strongest form of the property: suppression survives two process boundaries and an
        empty ledger, because the durable record is ``Case.actions`` and not the executor's.

        Run against a **copy** of the database. Everything else in this module reads one recorded
        outcome, and a test that appended to it would make its neighbours depend on execution
        order.
        """
        import shutil

        database, _output, snapshots = resumed
        copy = tmp_path / database.name
        for sibling in database.parent.glob(f"{database.name}*"):
            shutil.copy2(sibling, tmp_path / sibling.name)

        output = run_phase(3, copy, "2026-09-28T09:00:00+00:00")
        after = load(copy)

        assert "external calls : 0" in output
        assert len(after.actions) == len(snapshots[3].actions)
        assert TimelineKind.DUPLICATE_SUPPRESSED in [entry.kind for entry in after.timeline]


class TestTheTimelineIsContinuousAcrossTheBoundaries:
    def test_sequence_numbers_are_dense_and_ordered(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        assert [entry.seq for entry in snapshots[3].timeline] == list(
            range(len(snapshots[3].timeline))
        )

    def test_nothing_written_by_an_earlier_process_was_rewritten(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        assert snapshots[3].timeline[: len(snapshots[1].timeline)] == snapshots[1].timeline
        assert snapshots[3].timeline[: len(snapshots[2].timeline)] == snapshots[2].timeline

    def test_time_moves_forward_and_spans_all_three_days(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """The gap is the story, and the audit trail is where a judge can see it."""
        _database, _output, snapshots = resumed

        stamps = [entry.at for entry in snapshots[3].timeline]

        assert stamps == sorted(stamps)
        assert {stamp.date().isoformat() for stamp in stamps} == {
            "2026-09-04",
            "2026-09-07",
            "2026-09-21",
        }

    def test_the_projected_timeline_table_agrees_with_the_case_document(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        """``SqliteCaseStore`` writes the timeline twice; across three processes they must agree."""
        database, _output, snapshots = resumed

        store = SqliteCaseStore(database)
        try:
            projected = store.timeline(CASE_ID)
        finally:
            store.close()

        assert [entry.seq for entry in projected] == [entry.seq for entry in snapshots[3].timeline]
        assert [entry.kind for entry in projected] == [
            entry.kind for entry in snapshots[3].timeline
        ]

    def test_the_whole_story_is_on_it_in_order(
        self, resumed: tuple[Path, dict[int, str], dict[int, Case]]
    ) -> None:
        _database, _output, snapshots = resumed

        kinds = [entry.kind for entry in snapshots[3].timeline]
        for expected in (
            TimelineKind.CASE_CREATED,
            TimelineKind.APPROVAL_REQUESTED,
            TimelineKind.APPROVAL_GRANTED,
            TimelineKind.ACTION_EXECUTED,
            TimelineKind.VERIFICATION_PASSED,
            TimelineKind.CASE_REOPENED,
        ):
            assert expected in kinds, expected

        assert kinds.index(TimelineKind.VERIFICATION_PASSED) < kinds.index(
            TimelineKind.CASE_REOPENED
        )
        assert kinds.index(TimelineKind.CASE_REOPENED) < len(kinds) - 1
