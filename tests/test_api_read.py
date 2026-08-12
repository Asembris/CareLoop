"""The read surface, over HTTP.

``TestClient`` drives the real app against the real store, in process, with no socket and no server.
The cases come out of the real graph on the mock model, so what is being asserted is what a browser
would actually receive.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from api_helpers import CASE_ID, EMAIL, finished_store, parked_store, poisoned_store
from fastapi.testclient import TestClient
from helpers import FIXED_NOW

from careloop.api.app import create_app
from careloop.api.projections import COMPLETED, NEEDS_YOU, WORKING
from careloop.state import CaseState, InMemoryCaseStore
from careloop.state.machine import transition


def client_over(store) -> TestClient:
    return TestClient(create_app(store))


class TestHealth:
    def test_the_app_serves(self) -> None:
        response = client_over(InMemoryCaseStore()).get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestTheThreeSections:
    def test_an_empty_store_returns_three_empty_sections(self) -> None:
        body = client_over(InMemoryCaseStore()).get("/api/cases").json()

        assert body == {"needs_you": [], "working": [], "completed": []}

    def test_a_parked_case_is_the_one_that_needs_you(self) -> None:
        body = client_over(parked_store()).get("/api/cases").json()

        assert [card["case_id"] for card in body[NEEDS_YOU]] == [CASE_ID]
        assert body[WORKING] == []
        assert body[COMPLETED] == []

    def test_a_finished_case_is_completed(self) -> None:
        body = client_over(finished_store()).get("/api/cases").json()

        assert [card["case_id"] for card in body[COMPLETED]] == [CASE_ID]
        assert body[NEEDS_YOU] == []

    def test_a_failed_case_is_completed_too_and_says_why(self) -> None:
        store = poisoned_store()

        body = client_over(store).get("/api/cases").json()

        card = body[COMPLETED][0]
        assert card["state"] == "FAILED"
        assert card["section"] == COMPLETED
        assert card["failure_reason"]

    @pytest.mark.parametrize(
        ("state", "section"),
        [
            (CaseState.NEW, WORKING),
            (CaseState.GATHERING_EVIDENCE, WORKING),
            (CaseState.PLANNING, WORKING),
            (CaseState.AWAITING_HUMAN, NEEDS_YOU),
            (CaseState.READY_TO_EXECUTE, WORKING),
            (CaseState.DONE, COMPLETED),
            (CaseState.FAILED, COMPLETED),
        ],
        ids=str,
    )
    def test_a_case_lands_in_the_section_its_state_says(self, state, section) -> None:
        """Working is a real state a case can be seen in, not only a state it passes through."""
        store = _store_holding(state)

        body = client_over(store).get("/api/cases").json()

        assert [card["case_id"] for card in body[section]] == ["case-x"]
        assert all(body[other] == [] for other in body if other != section)

    def test_every_case_in_the_store_appears_in_exactly_one_section(self) -> None:
        """The partition, asserted over HTTP rather than only over the table."""
        store = InMemoryCaseStore()
        for index, state in enumerate(CaseState):
            _add(store, f"case-{index}", state)

        body = client_over(store).get("/api/cases").json()

        seen = [card["case_id"] for section in body.values() for card in section]
        assert sorted(seen) == sorted(f"case-{index}" for index, _ in enumerate(CaseState))
        assert len(seen) == len(set(seen))

    def test_the_needs_you_card_names_the_call_the_caregiver_is_approving(self) -> None:
        body = client_over(parked_store()).get("/api/cases").json()

        decision = body[NEEDS_YOU][0]["decision"]
        assert decision["tool_name"] == "send_email"
        assert decision["reason_code"] == "REQUIRES_HUMAN_APPROVAL"
        assert decision["reason_text"]
        assert decision["args"]["to"] == EMAIL["to"]
        assert decision["args"]["subject"] == EMAIL["subject"]
        assert decision["approval_id"]

    def test_a_card_with_nothing_pending_carries_no_decision(self) -> None:
        body = client_over(finished_store()).get("/api/cases").json()

        assert body[COMPLETED][0]["decision"] is None

    def test_cases_are_ordered_by_what_moved_most_recently(self) -> None:
        store = InMemoryCaseStore()
        _add(store, "case-old", CaseState.PLANNING, now=FIXED_NOW)
        _add(store, "case-new", CaseState.PLANNING, now=FIXED_NOW + timedelta(hours=1))

        body = client_over(store).get("/api/cases").json()

        assert [card["case_id"] for card in body[WORKING]] == ["case-new", "case-old"]


class TestCaseDetail:
    def test_a_missing_case_is_a_404_that_names_it(self) -> None:
        response = client_over(InMemoryCaseStore()).get("/api/cases/case-nope")

        assert response.status_code == 404
        assert "case-nope" in response.json()["detail"]

    def test_the_detail_carries_the_whole_case(self) -> None:
        store = finished_store()
        case = store.get(CASE_ID)

        body = client_over(store).get(f"/api/cases/{CASE_ID}").json()

        assert body["case_id"] == CASE_ID
        assert body["state"] == "DONE"
        assert body["section"] == COMPLETED
        assert body["version"] == case.version
        assert len(body["facts"]) == len(case.facts)
        assert len(body["timeline"]) == len(case.timeline)
        assert len(body["actions"]) == len(case.actions)
        assert len(body["approvals"]) == len(case.approvals)

    def test_a_fact_arrives_with_its_trust_level_and_its_citation(self) -> None:
        """The demo beat: a caregiver can see where a value came from and how far it is trusted."""
        store = finished_store()

        body = client_over(store).get(f"/api/cases/{CASE_ID}").json()

        slot = next(fact for fact in body["facts"] if fact["key"] == "slot_start")
        assert slot["trust_level"] == "CONTROLLED"
        assert slot["citation"] == f"{slot['source_doc']} p.{slot['page']}"
        assert slot["validated_by"]

    def test_no_timeline_entry_carries_a_diagnostic(self) -> None:
        store = poisoned_store()

        body = client_over(store).get(f"/api/cases/{CASE_ID}").json()

        assert body["timeline"]
        assert all("diagnostic" not in entry for entry in body["timeline"])

    def test_a_denial_arrives_with_its_rule_id_for_the_under_the_hood_panel(self) -> None:
        store = poisoned_store()

        body = client_over(store).get(f"/api/cases/{CASE_ID}").json()

        denials = [entry for entry in body["timeline"] if entry["kind"] == "POLICY_DENY"]
        assert denials
        assert all(entry["payload"]["matched_rule_id"].startswith("deny-") for entry in denials)
        assert all(entry["payload"]["reason_code"] for entry in denials)

    def test_the_action_arrives_with_the_key_it_happened_once_under(self) -> None:
        store = finished_store()

        body = client_over(store).get(f"/api/cases/{CASE_ID}").json()

        assert len(body["actions"]) == 1
        assert body["actions"][0]["action_type"] == "send_email"
        assert body["actions"][0]["idempotency_key"]


class TestTheAppIsBoundToWhatItWasGiven:
    def test_two_apps_do_not_share_a_store(self) -> None:
        """Nothing is read from module scope, so nothing leaks between apps in one process."""
        empty = client_over(InMemoryCaseStore())
        loaded = client_over(finished_store())

        assert empty.get("/api/cases").json()[COMPLETED] == []
        assert len(loaded.get("/api/cases").json()[COMPLETED]) == 1


def _add(store: InMemoryCaseStore, case_id: str, state: CaseState, *, now=FIXED_NOW):
    """Put a case into ``state`` through the real transition table.

    ``now`` is passed explicitly rather than left to the wall clock. Windows ticks about every
    15ms, so two cases created in one test share a timestamp often enough to make any ordering
    assertion a coin toss — the same clock-resolution trap that moved invariant 9's no-clock guard
    off behavioural comparison and onto the source.
    """
    from careloop.events.inbound import REFERRAL_EVENT
    from careloop.graph.runner import open_case

    case = open_case(store, REFERRAL_EVENT, case_id=case_id, now=now)
    base = case.version
    for step in _route_to(state):
        case = transition(case, step, reason="test", actor="test", now=now)
    return store.save(case, expected_version=base) if case.version != base else case


def _route_to(state: CaseState) -> list[CaseState]:
    """A legal path from ``NEW`` to ``state``, so no test invents an unreachable case."""
    routes: dict[CaseState, list[CaseState]] = {
        CaseState.NEW: [],
        CaseState.GATHERING_EVIDENCE: [CaseState.GATHERING_EVIDENCE],
        CaseState.PLANNING: [CaseState.GATHERING_EVIDENCE, CaseState.PLANNING],
        CaseState.AWAITING_HUMAN: [
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.AWAITING_HUMAN,
        ],
        CaseState.READY_TO_EXECUTE: [
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
        ],
        CaseState.EXECUTING: [
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
        ],
        CaseState.VERIFYING: [
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
        ],
        CaseState.WAITING_EXTERNAL: [
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.WAITING_EXTERNAL,
        ],
        CaseState.DONE: [
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.DONE,
        ],
        CaseState.FAILED: [CaseState.FAILED],
    }
    return routes[state]


def _store_holding(state: CaseState) -> InMemoryCaseStore:
    store = InMemoryCaseStore()
    _add(store, "case-x", state)
    return store
