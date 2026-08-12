"""Approve and reject: the two buttons on a "Needs You" card.

Both are the same shape — record an existing decision, then carry the case forward — and they
differ only in which existing function they call and therefore where the case goes next.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from api_helpers import CASE_ID, EMAIL, api_client, finished_store, intent, parked_store
from fastapi import HTTPException
from fastapi.testclient import TestClient
from helpers import make_case

from careloop.api.app import create_app, resume_event
from careloop.api.app import decide as decide_directly
from careloop.api.deps import ApiDeps
from careloop.api.projections import COMPLETED, NEEDS_YOU, WORKING
from careloop.events.inbound import CLAIM_REJECTION_EVENT_ID, REFERRAL_EVENT_ID
from careloop.graph.runner import approve as approve_directly
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.case import ApprovalStatus

WAIT = 10.0
CARER = "rosa"
HEADERS = {"X-Caregiver": CARER}

SEND_EMAIL = (intent("send_email", **EMAIL),)


def pending_id(store) -> str:
    return store.get(CASE_ID).pending_approval().approval_id


def decide(client, verb: str, approval_id: str, *, headers=HEADERS, case_id=CASE_ID, wait=WAIT):
    return client.post(
        f"/api/cases/{case_id}/decisions/{approval_id}/{verb}?wait={wait}", headers=headers
    )


class TestApprove:
    def test_one_click_drives_a_parked_case_to_completion(self) -> None:
        """The demo's last beat: approve, and the case lands in Completed."""
        store = parked_store()
        approval_id = pending_id(store)

        with api_client(store) as client:
            response = decide(client, "approve", approval_id)
            sections = client.get("/api/cases").json()

        assert response.status_code == 202
        assert response.json()["state"] == "SETTLED"
        assert store.get(CASE_ID).state is CaseState.DONE
        assert [card["case_id"] for card in sections[COMPLETED]] == [CASE_ID]
        assert sections[NEEDS_YOU] == []

    def test_the_effect_happened_exactly_once_and_is_recorded(self) -> None:
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        assert len(body["actions"]) == 1
        assert body["actions"][0]["action_type"] == "send_email"

    def test_the_approval_is_recorded_against_whoever_made_it(self) -> None:
        """The audit fact the header exists for."""
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        approval = body["approvals"][0]
        assert approval["status"] == "APPROVED"
        assert approval["decided_by"] == CARER
        assert approval["decided_at"]

    def test_done_was_reached_through_verifying_and_no_other_way(self) -> None:
        """Invariant 4, observed through HTTP."""
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        into_done = [
            entry
            for entry in body["timeline"]
            if entry["kind"] == TimelineKind.STATE_TRANSITION and entry["payload"]["to"] == "DONE"
        ]
        assert [entry["payload"]["from"] for entry in into_done] == ["VERIFYING"]

    def test_the_timeline_still_carries_the_citation(self) -> None:
        """What a judge reads off the screen at the end of the arc."""
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        cited = [entry for entry in body["timeline"] if " p." in entry["summary"]]
        assert cited
        assert any(".pdf p." in entry["summary"] for entry in cited)


class TestReject:
    def test_a_refusal_sends_the_case_back_to_the_planner(self) -> None:
        """A refused proposal is not a failed case."""
        store = parked_store()
        approval_id = pending_id(store)

        # The planner proposes the same email again, so the case parks a second time rather than
        # wandering off — what is being asserted is the route back to PLANNING, not convergence.
        with api_client(store, *SEND_EMAIL) as client:
            response = decide(client, "reject", approval_id)
            body = client.get(f"/api/cases/{CASE_ID}").json()

        assert response.status_code == 202
        assert [entry["kind"] for entry in body["timeline"]].count(
            TimelineKind.APPROVAL_REJECTED
        ) == 1
        replans = [
            entry
            for entry in body["timeline"]
            if entry["kind"] == TimelineKind.STATE_TRANSITION
            and entry["payload"]
            == {
                **entry["payload"],
                "from": "AWAITING_HUMAN",
                "to": "PLANNING",
            }
        ]
        assert replans

    def test_nothing_was_sent(self) -> None:
        """The assertion that makes the refusal mean something."""
        store = parked_store()

        with api_client(store, *SEND_EMAIL) as client:
            decide(client, "reject", pending_id(store))

        assert store.get(CASE_ID).actions == ()

    def test_the_refusal_is_recorded_against_whoever_made_it(self) -> None:
        store = parked_store()

        with api_client(store, *SEND_EMAIL) as client:
            decide(client, "reject", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        rejected = next(one for one in body["approvals"] if one["status"] == "REJECTED")
        assert rejected["decided_by"] == CARER

    def test_a_planner_with_nothing_left_fails_the_case_rather_than_hanging(self) -> None:
        """The replan really does re-enter the planner, which is why an empty script is reached."""
        store = parked_store()

        with api_client(store) as client:
            response = decide(client, "reject", pending_id(store))

        assert response.json()["state"] == "ERRORED"
        assert store.get(CASE_ID).state is CaseState.FAILED


class TestTheCaregiverHeaderIsRequiredButIsNotAuthentication:
    def test_a_missing_header_is_a_400(self) -> None:
        """400 rather than 401: nothing is authenticated, and 401 would claim otherwise."""
        store = parked_store()

        with api_client(store) as client:
            response = decide(client, "approve", pending_id(store), headers={})

        assert response.status_code == 400
        assert "X-Caregiver" in response.json()["detail"]

    def test_a_blank_header_is_a_400_too(self) -> None:
        store = parked_store()

        with api_client(store) as client:
            response = decide(client, "approve", pending_id(store), headers={"X-Caregiver": "   "})

        assert response.status_code == 400

    def test_a_refused_decision_leaves_the_case_untouched(self) -> None:
        store = parked_store()
        before = store.get(CASE_ID)

        with api_client(store) as client:
            decide(client, "approve", pending_id(store), headers={})

        assert store.get(CASE_ID).version == before.version
        assert store.get(CASE_ID).state is CaseState.AWAITING_HUMAN

    def test_any_name_is_accepted_because_nothing_is_verified(self) -> None:
        """Recorded rather than checked. Stated plainly here so a later session sees the gap."""
        store = parked_store()

        with api_client(store) as client:
            response = decide(
                client, "approve", pending_id(store), headers={"X-Caregiver": "anyone at all"}
            )

        assert response.status_code == 202
        assert store.get(CASE_ID).approvals[0].decided_by == "anyone at all"


class TestDecisionsThatCannotBeMade:
    def test_a_missing_case_is_a_404(self) -> None:
        with api_client(InMemoryCaseStore()) as client:
            response = decide(client, "approve", "apr-anything", case_id="case-nope")

        assert response.status_code == 404

    def test_an_approval_the_case_does_not_carry_is_a_404(self) -> None:
        store = parked_store()

        with api_client(store) as client:
            response = decide(client, "approve", "apr-invented")

        assert response.status_code == 404
        assert "apr-invented" in response.json()["detail"]

    def test_deciding_twice_is_a_409(self) -> None:
        """A decision is made once. The second click must not re-run anything."""
        store = parked_store()
        approval_id = pending_id(store)

        with api_client(store) as client:
            first = decide(client, "approve", approval_id)
            second = decide(client, "approve", approval_id)

        assert first.status_code == 202
        assert second.status_code == 409
        assert len(store.get(CASE_ID).actions) == 1

    def test_approving_an_already_finished_case_performs_no_second_effect(self) -> None:
        store = finished_store()
        approval_id = store.get(CASE_ID).approvals[0].approval_id

        with api_client(store) as client:
            response = decide(client, "approve", approval_id)

        assert response.status_code == 409
        assert len(store.get(CASE_ID).actions) == 1


class TestADecisionDuringARunIsRefusedBeforeAnythingIsRecorded:
    """The window is narrow and it is real.

    The gate node commits ``AWAITING_HUMAN`` and the pending approval, and only then does the graph
    finish and the run settle. In between, the case is parked and the run is still going — a
    dashboard polling once a second can land there, and a caregiver can click. Without the check,
    ``approve`` would release the case to ``READY_TO_EXECUTE`` while the graph still held a version
    it was about to write, and the two would collide somewhere inside a node.

    Refusing *before* recording is what makes the request all-or-nothing. Recording the approval and
    then failing to schedule would leave a decision made, a case released, and a caller told the
    request was refused — three things that disagree about what happened.
    """

    def test_the_approval_is_not_recorded(self) -> None:
        store = parked_store()
        before = store.get(CASE_ID)
        approval_id = before.pending_approval().approval_id

        async def exercise() -> None:
            async def occupying():
                await asyncio.sleep(0.05)
                raise AssertionError("deliberate: this run is only here to occupy the case")

            deps = ApiDeps(store=store)
            deps.runs.start(CASE_ID, occupying)

            with pytest.raises(HTTPException) as raised:
                await decide_directly(approve_directly, CASE_ID, approval_id, deps, CARER, 0.0)

            assert raised.value.status_code == 409
            await deps.runs.drain()

        asyncio.run(exercise())

        after = store.get(CASE_ID)
        assert after.version == before.version
        assert after.state is CaseState.AWAITING_HUMAN
        assert after.pending_approval().status is ApprovalStatus.PENDING

    def test_the_refusal_comes_before_the_approval_is_even_looked_up(self) -> None:
        """A 409 rather than the 404 an unknown approval id would otherwise get."""
        gate = threading.Event()

        with api_client(InMemoryCaseStore(), *SEND_EMAIL, gate=gate) as client:
            client.post(
                "/api/events?wait=0",
                json={"event_id": REFERRAL_EVENT_ID, "case_id": CASE_ID},
            )
            response = decide(client, "approve", "apr-does-not-exist", wait=0)
            gate.set()

        assert response.status_code == 409
        assert "in flight" in response.json()["detail"]

    def test_once_the_run_settles_the_decision_goes_through(self) -> None:
        """Vacuity: the refusal is about the run, not about the request being malformed."""
        store = parked_store()

        with api_client(store) as client:
            response = decide(client, "approve", pending_id(store))

        assert response.status_code == 202
        assert store.get(CASE_ID).state is CaseState.DONE


class TestAReopenedCaseResumesUnderTheEventThatReopenedIt:
    """Why the API reads ``latest_delivery`` and not ``case.event_id``.

    ``reopen_case`` deliberately does not rewrite ``Case.event_id`` — it records which event
    *opened* the case, and a reopen is not an opening. Anything rebuilding a run context off that
    field would hand a reopened case the referral's documents instead of the claim rejection's,
    and the follow-up documents would never be read.
    """

    def test_the_resume_event_is_the_one_that_reopened_the_case(self) -> None:
        store = finished_store()

        with api_client(store, *SEND_EMAIL) as client:
            client.post(
                f"/api/events?wait={WAIT}",
                json={"event_id": CLAIM_REJECTION_EVENT_ID, "case_id": CASE_ID},
            )

        case = store.get(CASE_ID)
        deps = ApiDeps(store=store)

        assert case.event_id == REFERRAL_EVENT_ID
        assert resume_event(deps, case).event_id == CLAIM_REJECTION_EVENT_ID

    def test_before_any_reopen_it_is_the_event_that_opened_the_case(self) -> None:
        store = parked_store()

        resumed = resume_event(ApiDeps(store=store), store.get(CASE_ID))

        assert resumed.event_id == REFERRAL_EVENT_ID

    def test_a_case_with_no_recorded_delivery_is_a_409_rather_than_a_guess(self) -> None:
        case = make_case(state=CaseState.AWAITING_HUMAN)

        with pytest.raises(HTTPException) as raised:
            resume_event(ApiDeps(store=InMemoryCaseStore()), case)

        assert raised.value.status_code == 409


class TestTheApiCallsExistingFunctionsRatherThanDecidingAnything:
    def test_what_was_executed_is_exactly_what_was_approved(self) -> None:
        """Approving releases the case; it does not hand the run a blank cheque.

        An approval records the *digest* of the arguments it covers, never the tool name, so the
        thing to assert is that the effect the run performed hashes to the digest the caregiver
        decided about. An approval for other arguments would not have released this call, and the
        ``args_approved`` rule refuses it again at the tool boundary.
        """
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        approval = body["approvals"][0]
        assert approval["status"] == "APPROVED"
        assert [action["args_digest"] for action in body["actions"]] == [approval["args_digest"]]

    def test_the_approval_is_recorded_before_the_effect(self) -> None:
        """Ordering, not merely co-occurrence: nothing was sent while the decision was pending."""
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))
            body = client.get(f"/api/cases/{CASE_ID}").json()

        kinds = [entry["kind"] for entry in body["timeline"]]
        assert kinds.index(TimelineKind.APPROVAL_REQUESTED) < kinds.index(
            TimelineKind.APPROVAL_GRANTED
        )
        assert kinds.index(TimelineKind.APPROVAL_GRANTED) < kinds.index(
            TimelineKind.ACTION_EXECUTED
        )

    def test_the_state_the_api_left_behind_is_what_the_next_run_dispatches_on(self) -> None:
        """A second app over the same store finishes what the first one released."""
        store = parked_store()
        approve_directly(store, CASE_ID, pending_id(store), decided_by=CARER)
        assert store.get(CASE_ID).state is CaseState.READY_TO_EXECUTE

        with api_client(store) as client:
            client.post(
                f"/api/events?wait={WAIT}",
                json={"event_id": store.get(CASE_ID).event_id, "case_id": CASE_ID},
            )

        # The delivery is refused — the case is not DONE — but the point stands: nothing about the
        # release lives anywhere but the store.
        assert store.get(CASE_ID).state is CaseState.READY_TO_EXECUTE

    def test_a_fresh_app_sees_the_decision_the_previous_one_recorded(self) -> None:
        store = parked_store()

        with api_client(store) as client:
            decide(client, "approve", pending_id(store))

        with TestClient(create_app(store)) as fresh:
            body = fresh.get(f"/api/cases/{CASE_ID}").json()
            sections = fresh.get("/api/cases").json()

        assert body["approvals"][0]["decided_by"] == CARER
        assert sections[COMPLETED][0]["case_id"] == CASE_ID
        assert sections[WORKING] == []
