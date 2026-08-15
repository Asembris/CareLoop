"""Two identities, and what goes wrong when one name does both jobs.

An ``approval_id`` answers *which occurrence of asking a human is this?* An ``args_digest`` answers
*which exact argument set is involved?* Those are different questions, and CareLoop derived the
first from the second — ``f"apr-{digest[:16]}"`` — so reproposing identical arguments after a
refusal produced a second record wearing the first record's name.

``_decide`` then rebuilt ``case.approvals`` replacing **every** record whose id matched, while
checking ``PENDING`` and ``is_actionable`` on only one of them. The two had different scopes: one
record was inspected and N were rewritten. A caregiver approving the second request silently
converted the first caregiver's *rejection* into their own approval, with their name and their
timestamp on it, in a store whose whole claim is that the audit trail proves what happened.

Nothing about authority moved — ``is_approved`` reads the most recent record matching the call,
which is the one the caregiver actually decided — so this was an audit-integrity defect rather than
an escalation. That is not a reason to leave it: a decision record a later decision can overwrite
is exactly the thing this project says it does not have.

So the repair is two independent halves, and the mutants in the session report prove neither is
decoration:

* a request gets its own identity, ``apr-{seq}``, unique within the case because the timeline is
  append-only; and
* a decision replaces exactly one element **by position**, never by identifier — which is what
  protects the duplicate ids already sitting in persisted cases, where uniqueness cannot help.

**Deliberately out of scope.** This changes request identity only. Which call an approval authorises
is decided by ``approval_for`` and ``is_approved`` and is untouched here — see
:class:`TestTheAuthorisationPathIsUntouched`. Those helpers matched on ``args_digest`` alone when
this module was written and are now bound to ``(tool_name, args_digest)``; that is a separate
repair with its own evidence in ``tests/test_approval_capability.py``, and the assertions below hold
under either, which is the point of keeping them.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from api_helpers import CASE_ID, EMAIL, drive, intent, parked_store, planner_proposing
from helpers import FIXED_NOW

from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.runner import (
    AmbiguousApproval,
    ApprovalAlreadyDecided,
    approve,
    open_case,
    reject,
)
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.case import Approval, ApprovalStatus
from careloop.tools.idempotency import args_digest

FIRST_CARER = "rosa.first"
SECOND_CARER = "rosa.second"

SEND_EMAIL = intent("send_email", **EMAIL)


def reproposal_store() -> InMemoryCaseStore:
    """A store whose case refused one action, then had the identical action proposed again.

    Driven through the real gate on the mock model — no hand-built approval anywhere — because the
    defect is about what the production path *writes*, and a fixture that assembled the two records
    itself would be agreeing with itself about their shape.
    """
    store = InMemoryCaseStore()
    open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
    drive(store, REFERRAL_EVENT, planner_proposing(SEND_EMAIL))

    first = store.get(CASE_ID).pending_approval()
    reject(store, CASE_ID, first.approval_id, decided_by=FIRST_CARER, now=FIXED_NOW)

    drive(store, REFERRAL_EVENT, planner_proposing(SEND_EMAIL))
    return store


def requests(case: Case) -> tuple[Approval, Approval]:
    """The two decision requests on a reproposed case, in the order they were raised."""
    first, second = case.approvals
    return first, second


class TestARepeatedRequestIsANewRequest:
    """Same action, asked twice. One action identity, two request identities."""

    def test_the_same_action_was_asked_about_twice(self) -> None:
        case = reproposal_store().get(CASE_ID)

        assert len(case.approvals) == 2
        assert case.state is CaseState.AWAITING_HUMAN

    def test_the_action_identity_is_the_same_in_both(self) -> None:
        """The digest is what an approval covers, and reproposing the same call must not change it.

        Asserted first, because every claim below is only interesting if the two records really are
        about one action. If the digests differed these would be two different questions and there
        would be nothing to get wrong.
        """
        first, second = requests(reproposal_store().get(CASE_ID))

        assert first.args_digest == second.args_digest == args_digest(EMAIL)
        assert first.tool_name == second.tool_name == "send_email"

    def test_the_request_identity_is_not(self) -> None:
        first, second = requests(reproposal_store().get(CASE_ID))

        assert first.approval_id != second.approval_id

    def test_each_id_names_the_timeline_entry_that_recorded_the_request(self) -> None:
        """Where the uniqueness comes from, stated as a property rather than as two examples.

        ``apr-{seq}`` is the position of the ``APPROVAL_REQUESTED`` entry that records the request.
        The timeline is append-only, so no two requests on one case can share it — uniqueness here
        is structural rather than a collision argument, and the audit entry and the id it carries
        are the same number by construction.
        """
        case = reproposal_store().get(CASE_ID)

        for event in case.timeline:
            if event.kind is TimelineKind.APPROVAL_REQUESTED:
                assert event.payload["approval_id"] == f"apr-{event.seq}"

    def test_every_request_on_the_case_has_its_own_id(self) -> None:
        """The generic form: not two examples, but no duplicates at all."""
        case = reproposal_store().get(CASE_ID)

        ids = [approval.approval_id for approval in case.approvals]
        assert len(set(ids)) == len(ids)


class TestDecidingOneRequestLeavesEveryOtherRecordAlone:
    """The defect proper. A decision about B must not touch A."""

    def test_the_earlier_rejection_survives_the_later_approval(self) -> None:
        store = reproposal_store()
        rejected, pending = requests(store.get(CASE_ID))

        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        after = store.get(CASE_ID).approvals[0]
        assert after.status is ApprovalStatus.REJECTED
        assert after.decided_by == FIRST_CARER
        assert after == rejected

    def test_only_the_decided_record_differs(self) -> None:
        """Full model equality on every other record, so no field can change quietly.

        ``approval_id``, ``tool_name``, ``args_digest``, ``reason_code``, ``status``,
        ``requested_at``, ``decided_at`` and ``decided_by`` are all covered by comparing the models
        themselves rather than by listing the fields — a list would need updating the day a field is
        added, and would silently stop covering it if nobody did.
        """
        store = reproposal_store()
        before = store.get(CASE_ID).approvals
        target = before[1].approval_id

        approve(store, CASE_ID, target, decided_by=SECOND_CARER, now=FIXED_NOW)

        after = store.get(CASE_ID).approvals
        assert len(after) == len(before)
        changed = [i for i, (was, now) in enumerate(zip(before, after, strict=True)) if was != now]
        assert changed == [1]

    def test_the_request_that_was_decided_carries_the_decision(self) -> None:
        """Vacuity: a ``_decide`` that changed nothing at all would pass the two tests above."""
        store = reproposal_store()
        _, pending = requests(store.get(CASE_ID))

        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        decided = store.get(CASE_ID).approvals[1]
        assert decided.status is ApprovalStatus.APPROVED
        assert decided.decided_by == SECOND_CARER
        assert decided.decided_at == FIXED_NOW

    def test_a_refusal_of_the_second_request_leaves_the_first_alone_too(self) -> None:
        """Both verbs go through ``_decide``, so both are the same defect."""
        store = reproposal_store()
        rejected, pending = requests(store.get(CASE_ID))

        reject(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        after = store.get(CASE_ID).approvals[0]
        assert after == rejected
        assert after.decided_by == FIRST_CARER

    def test_the_timeline_ties_each_decision_to_the_request_it_decided(self) -> None:
        store = reproposal_store()
        first, second = requests(store.get(CASE_ID))

        approve(store, CASE_ID, second.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        decisions = {
            event.kind: event.payload["approval_id"]
            for event in store.get(CASE_ID).timeline
            if event.kind in (TimelineKind.APPROVAL_REJECTED, TimelineKind.APPROVAL_GRANTED)
        }
        assert decisions[TimelineKind.APPROVAL_REJECTED] == first.approval_id
        assert decisions[TimelineKind.APPROVAL_GRANTED] == second.approval_id


class TestADecidedRecordIsImmutable:
    def test_a_second_decision_on_the_same_request_is_refused(self) -> None:
        store = reproposal_store()
        _, pending = requests(store.get(CASE_ID))
        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        with pytest.raises(ApprovalAlreadyDecided):
            reject(store, CASE_ID, pending.approval_id, decided_by="rosa.third", now=FIXED_NOW)

    def test_the_refusal_changes_nothing(self) -> None:
        store = reproposal_store()
        _, pending = requests(store.get(CASE_ID))
        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)
        before = store.get(CASE_ID)

        with contextlib.suppress(ApprovalAlreadyDecided):
            reject(store, CASE_ID, pending.approval_id, decided_by="rosa.third", now=FIXED_NOW)

        after = store.get(CASE_ID)
        assert after.approvals == before.approvals
        assert after.version == before.version
        assert after.timeline == before.timeline
        assert after.state is before.state


# -- what is already on disk ---------------------------------------------------------------------

#: The identifier a CareLoop before this repair minted for this action.
#:
#: ``approval_id_for`` was ``f"apr-{digest[:16]}"``, a pure function of the arguments, so every
#: request about this same email in every case was called this. Kept as the thing that code actually
#: produced rather than as an invented string, because the point of the tests below is that a case
#: written by *that* code and read by *this* code behaves safely.
HISTORICAL_ID = f"apr-{args_digest(EMAIL)[:16]}"


def historical(status: str, decided_by: str | None = None) -> dict[str, object]:
    """One approval as the old code persisted it, in JSON rather than as an object."""
    return {
        "approval_id": HISTORICAL_ID,
        "tool_name": "send_email",
        "args_digest": args_digest(EMAIL),
        "reason_code": "REQUIRES_HUMAN_APPROVAL",
        "status": status,
        "requested_at": "2026-08-11T12:00:00Z",
        "decided_at": "2026-08-11T12:00:00Z" if decided_by else None,
        "decided_by": decided_by,
    }


def legacy_store(*approvals: dict[str, object]) -> InMemoryCaseStore:
    """A store holding one parked case whose approvals are the ones given, spliced through JSON.

    Through JSON, not by attaching ``Approval`` objects, for the reason
    ``tests/test_no_safe_action.py`` does the same: that is how a persisted case actually arrives.
    ``SqliteCaseStore`` writes whole-case JSON and reads it back through
    ``Case.model_validate_json`` and ``InMemoryCaseStore`` round-trips deliberately, so a guard
    proven against a live object could still be standing in front of a record no store can produce.
    """
    payload = parked_store().get(CASE_ID).model_dump(mode="json")
    payload["approvals"] = list(approvals)

    store = InMemoryCaseStore()
    store.create(Case.model_validate_json(json.dumps(payload)))
    return store


class TestAHistoricalDuplicateWithOneDecidableRecord:
    """Decided + pending under one id. One record is decidable, so the answer is not a guess.

    This is the shape the defect actually produced, and it is why the repair interprets rather than
    migrates: refusing it would strand every case already carrying one, permanently, for the sake of
    a tidiness nobody benefits from.
    """

    def test_the_pending_record_is_decided(self) -> None:
        store = legacy_store(historical("REJECTED", FIRST_CARER), historical("PENDING"))

        approve(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

        decided = store.get(CASE_ID).approvals[1]
        assert decided.status is ApprovalStatus.APPROVED
        assert decided.decided_by == SECOND_CARER

    def test_the_decided_record_is_untouched(self) -> None:
        store = legacy_store(historical("REJECTED", FIRST_CARER), historical("PENDING"))
        before = store.get(CASE_ID).approvals[0]

        approve(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

        assert store.get(CASE_ID).approvals[0] == before

    def test_the_case_moves_on(self) -> None:
        store = legacy_store(historical("REJECTED", FIRST_CARER), historical("PENDING"))

        approve(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

        assert store.get(CASE_ID).state is CaseState.READY_TO_EXECUTE


class TestAHistoricalDuplicateWithNothingDecidable:
    def test_two_decided_records_refuse_a_third_decision(self) -> None:
        store = legacy_store(
            historical("REJECTED", FIRST_CARER), historical("APPROVED", SECOND_CARER)
        )

        with pytest.raises(ApprovalAlreadyDecided):
            approve(store, CASE_ID, HISTORICAL_ID, decided_by="rosa.third", now=FIXED_NOW)

    def test_neither_record_changes(self) -> None:
        store = legacy_store(
            historical("REJECTED", FIRST_CARER), historical("APPROVED", SECOND_CARER)
        )
        before = store.get(CASE_ID)

        with contextlib.suppress(ApprovalAlreadyDecided):
            approve(store, CASE_ID, HISTORICAL_ID, decided_by="rosa.third", now=FIXED_NOW)

        after = store.get(CASE_ID)
        assert after.approvals == before.approvals
        assert after.version == before.version
        assert after.timeline == before.timeline


class TestAHistoricalDuplicateThatIsGenuinelyAmbiguous:
    """Two pending records under one id. Which request is the caregiver deciding?

    Reachable in legacy data: the pre-repair manual handoff wrote ``apr-no-safe-action:c`` for every
    parking, so a case that parked, was reopened by a later event and parked again holds two. There
    is no correct answer here, so the only safe behaviour is to refuse and move nothing.

    The two assertions below deliberately name no exception: what matters is that nothing moved, and
    stating it that way is what let them run as fail-first evidence against a tree where the refusal
    did not exist yet. :meth:`test_the_refusal_says_what_is_wrong` names the type.
    """

    def test_the_refusal_says_what_is_wrong(self) -> None:
        store = legacy_store(historical("PENDING"), historical("PENDING"))

        with pytest.raises(AmbiguousApproval):
            approve(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

    def test_rejecting_is_equally_ambiguous(self) -> None:
        """Both verbs are ``_decide``, and neither can tell which request it is deciding."""
        store = legacy_store(historical("PENDING"), historical("PENDING"))

        with pytest.raises(AmbiguousApproval):
            reject(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

    def test_nothing_moves(self) -> None:
        store = legacy_store(historical("PENDING"), historical("PENDING"))
        before = store.get(CASE_ID)

        with contextlib.suppress(Exception):
            approve(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

        after = store.get(CASE_ID)
        assert after.approvals == before.approvals
        assert after.version == before.version
        assert after.state is before.state
        assert after.timeline == before.timeline

    def test_no_effect_is_recorded(self) -> None:
        store = legacy_store(historical("PENDING"), historical("PENDING"))

        with contextlib.suppress(Exception):
            approve(store, CASE_ID, HISTORICAL_ID, decided_by=SECOND_CARER, now=FIXED_NOW)

        assert store.get(CASE_ID).actions == ()


class TestTheAuthorisationPathIsUntouched:
    """What this patch deliberately does not change.

    Request identity moved; the binding between an approval and the call it releases did not.
    ``approval_id`` is not consulted by ``is_approved`` at all, and this asserts that rather than
    assuming it — a repair that quietly rerouted authorisation through ``approval_id`` would be a
    much larger change than the one being made, and it would pass every test above.

    What the call is matched *by* has since been narrowed from ``args_digest`` alone to
    ``(tool_name, args_digest)``. These assertions were written before that and pass unchanged
    under both, which is what makes them a statement about ``approval_id`` rather than about the
    digest.
    """

    def test_the_approved_arguments_are_authorised(self) -> None:
        store = reproposal_store()
        _, pending = requests(store.get(CASE_ID))

        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        assert store.get(CASE_ID).is_approved("send_email", args_digest(EMAIL))

    def test_different_arguments_are_not(self) -> None:
        store = reproposal_store()
        _, pending = requests(store.get(CASE_ID))

        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        other = {**EMAIL, "body": "something the caregiver never read"}
        assert not store.get(CASE_ID).is_approved("send_email", args_digest(other))

    def test_the_approved_action_runs_to_completion(self) -> None:
        """End to end, because "the ids changed" must not mean "the case stopped working"."""
        store = reproposal_store()
        _, pending = requests(store.get(CASE_ID))
        approve(store, CASE_ID, pending.approval_id, decided_by=SECOND_CARER, now=FIXED_NOW)

        result, _ = drive(store, REFERRAL_EVENT, planner_proposing())

        assert result.case.state is CaseState.DONE
        assert len(result.case.actions) == 1
        assert result.case.actions[0].args_digest == args_digest(EMAIL)
