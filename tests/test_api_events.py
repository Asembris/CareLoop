"""Delivering an event, and the runs it starts.

The execution-model half of the API. ``POST /api/events`` returns 202 with a run id so the
dashboard's Working section is reachable at all; ``?wait`` is how a test settles a run without
polling or sleeping.
"""

from __future__ import annotations

import asyncio

import pytest
from api_helpers import (
    CASE_ID,
    EMAIL,
    api_client,
    finished_store,
    intent,
    parked_store,
)
from fastapi.testclient import TestClient

from careloop.api.app import create_app
from careloop.api.projections import COMPLETED, NEEDS_YOU, WORKING
from careloop.api.runs import RunAlreadyInFlight, RunRegistry
from careloop.events.inbound import CLAIM_REJECTION_EVENT_ID, REFERRAL_EVENT_ID
from careloop.state import CaseState, InMemoryCaseStore

WAIT = 10.0

SEND_EMAIL = (intent("send_email", **EMAIL),)


def deliver(client, event_id=REFERRAL_EVENT_ID, case_id=CASE_ID, *, wait=WAIT):
    return client.post(f"/api/events?wait={wait}", json={"event_id": event_id, "case_id": case_id})


class TestDeliveringAnEvent:
    def test_an_event_opens_a_case_and_returns_a_run(self) -> None:
        store = InMemoryCaseStore()

        with api_client(store, *SEND_EMAIL) as client:
            response = deliver(client)

        assert response.status_code == 202
        body = response.json()
        assert body["case_id"] == CASE_ID
        assert body["run_id"] == f"run-{CASE_ID}-1"
        assert body["state"] == "SETTLED"
        assert body["error"] is None
        assert store.get(CASE_ID).state is CaseState.AWAITING_HUMAN

    def test_the_run_reports_the_nodes_it_went_through(self) -> None:
        """The under-the-hood panel's node trace."""
        with api_client(InMemoryCaseStore(), *SEND_EMAIL) as client:
            body = deliver(client).json()

        assert body["executed_nodes"][:1] == ["dispatch"]
        assert {"triage", "evidence", "plan", "gate"} <= set(body["executed_nodes"])

    def test_the_case_lands_in_needs_you_straight_after(self) -> None:
        """The demo arc's first two beats, back to back."""
        with api_client(InMemoryCaseStore(), *SEND_EMAIL) as client:
            deliver(client)
            sections = client.get("/api/cases").json()

        assert [card["case_id"] for card in sections[NEEDS_YOU]] == [CASE_ID]
        assert sections[NEEDS_YOU][0]["decision"]["tool_name"] == "send_email"

    def test_an_unknown_event_is_a_404_that_names_it(self) -> None:
        with api_client(InMemoryCaseStore()) as client:
            response = deliver(client, event_id="evt-nope")

        assert response.status_code == 404
        assert "evt-nope" in response.json()["detail"]

    def test_an_unknown_event_opens_no_case(self) -> None:
        """The lookup happens before anything is written."""
        store = InMemoryCaseStore()

        with api_client(store) as client:
            deliver(client, event_id="evt-nope")

        assert store.list_by_state(CaseState.NEW) == []

    def test_a_body_with_an_unexpected_field_is_refused(self) -> None:
        with api_client(InMemoryCaseStore()) as client:
            response = client.post(
                "/api/events",
                json={"event_id": REFERRAL_EVENT_ID, "case_id": CASE_ID, "priority": "high"},
            )

        assert response.status_code == 422

    def test_a_body_missing_the_case_id_is_refused(self) -> None:
        """It is required rather than minted: a demo needs case ids that can be written down."""
        with api_client(InMemoryCaseStore()) as client:
            response = client.post("/api/events", json={"event_id": REFERRAL_EVENT_ID})

        assert response.status_code == 422


class TestDeliveringToACaseThatAlreadyExists:
    def test_a_finished_case_reopens_and_keeps_its_id_and_its_evidence(self) -> None:
        store = finished_store()
        before = store.get(CASE_ID)

        with api_client(store, *SEND_EMAIL) as client:
            response = deliver(client, event_id=CLAIM_REJECTION_EVENT_ID)

        assert response.status_code == 202
        after = store.get(CASE_ID)
        assert after.case_id == before.case_id
        assert len(after.facts) >= len(before.facts)
        assert after.actions[0] == before.actions[0]

    def test_a_case_still_in_flight_is_a_409_rather_than_an_interruption(self) -> None:
        """The rule lives in reopen_case; the API only translates its refusal."""
        with api_client(parked_store()) as client:
            response = deliver(client, event_id=CLAIM_REJECTION_EVENT_ID)

        assert response.status_code == 409
        assert "AWAITING_HUMAN" in response.json()["detail"]

    def test_the_refused_delivery_changed_nothing(self) -> None:
        store = parked_store()
        before = store.get(CASE_ID)

        with api_client(store) as client:
            deliver(client, event_id=CLAIM_REJECTION_EVENT_ID)

        assert store.get(CASE_ID).version == before.version


class TestRuns:
    def test_a_run_is_readable_by_id_afterwards(self) -> None:
        with api_client(InMemoryCaseStore(), *SEND_EMAIL) as client:
            run_id = deliver(client).json()["run_id"]
            body = client.get(f"/api/runs/{run_id}").json()

        assert body["run_id"] == run_id
        assert body["state"] == "SETTLED"
        assert body["executed_nodes"]

    def test_an_unknown_run_is_a_404(self) -> None:
        with api_client(InMemoryCaseStore()) as client:
            response = client.get("/api/runs/run-nope-1")

        assert response.status_code == 404

    def test_not_waiting_returns_while_the_run_is_still_going(self) -> None:
        """What makes Working a real state rather than one nobody can catch the case in."""
        with api_client(InMemoryCaseStore(), *SEND_EMAIL) as client:
            accepted = deliver(client, wait=0).json()

        assert accepted["state"] == "RUNNING"
        assert accepted["executed_nodes"] == []

    def test_the_run_still_finishes_after_an_expired_wait(self) -> None:
        """A caller giving up must not cancel work the store is about to be asked about."""
        store = InMemoryCaseStore()

        with api_client(store, *SEND_EMAIL) as client:
            accepted = deliver(client, wait=0).json()
            assert accepted["state"] == "RUNNING"

            settled = client.get(f"/api/runs/{accepted['run_id']}?wait={WAIT}").json()

        assert settled["state"] == "SETTLED"
        assert settled["executed_nodes"]
        assert store.get(CASE_ID).state is CaseState.AWAITING_HUMAN

    def test_a_second_run_for_one_case_is_refused_while_the_first_is_in_flight(self) -> None:
        """Two concurrent runs would race on the store's compare-and-set."""
        registry = RunRegistry()

        async def exercise() -> None:
            async def slow():
                await asyncio.sleep(0.05)
                raise AssertionError("deliberate: this run is only here to occupy the case")

            registry.start(CASE_ID, slow)
            with pytest.raises(RunAlreadyInFlight, match=CASE_ID):
                registry.start(CASE_ID, slow)
            assert registry.in_flight_for(CASE_ID) is not None

            await registry.drain()
            assert registry.in_flight_for(CASE_ID) is None

        asyncio.run(exercise())

    def test_a_run_that_settled_frees_the_case_for_another(self) -> None:
        """The refusal is about concurrency, not about the case having run before."""
        with api_client(finished_store(), *SEND_EMAIL) as client:
            first = deliver(client, event_id=CLAIM_REJECTION_EVENT_ID)
            assert first.json()["state"] == "SETTLED"

            second = deliver(client, event_id=CLAIM_REJECTION_EVENT_ID)

        assert second.json()["run_id"] == f"run-{CASE_ID}-2"

    def test_a_raising_node_leaves_the_run_errored_and_the_case_failed(self) -> None:
        """The registry records that it happened; the case's timeline records why."""
        store = InMemoryCaseStore()

        # No proposal is scripted, so the plan node's mock model raises MockScriptExhausted.
        with api_client(store) as client:
            body = deliver(client).json()

        assert body["state"] == "ERRORED"
        assert body["error"] == "MockScriptExhausted"
        assert store.get(CASE_ID).state is CaseState.FAILED

    def test_the_error_is_a_type_name_and_never_a_message(self) -> None:
        with api_client(InMemoryCaseStore()) as client:
            body = deliver(client).json()

        assert body["error"].isidentifier()

    def test_the_failed_case_still_appears_on_the_dashboard(self) -> None:
        with api_client(InMemoryCaseStore()) as client:
            deliver(client)
            sections = client.get("/api/cases").json()

        assert [card["case_id"] for card in sections[COMPLETED]] == [CASE_ID]
        assert sections[WORKING] == []


class TestTheRegistryIsNotABusinessFact:
    def test_a_fresh_app_over_the_same_store_answers_every_case_question(self) -> None:
        """Losing the registry costs the run trace and nothing the store owns."""
        store = finished_store()

        with TestClient(create_app(store)) as client:
            body = client.get(f"/api/cases/{CASE_ID}").json()

        assert body["state"] == "DONE"
        assert body["actions"]
        assert body["approvals"][0]["status"] == "APPROVED"
        assert body["timeline"]
