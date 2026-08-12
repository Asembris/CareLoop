"""T1 over HTTP — the whole arc a browser drives.

The direct-call T1 in ``test_case_run.py`` is unchanged and stays the primary statement of the
property. This is the same arc through the surface a judge will actually touch:

    an email arrives → the case appears in Working → a decision card appears in Needs You →
    one click approves → the case lands in Completed with a citation-bearing timeline

**Nothing here calls a runner function.** Only ``TestClient`` requests move the case, and
:class:`TestTheArcReallyIsDrivenOverHttp` sweeps this module's own syntax tree to keep that true —
a test that reached for ``approve`` directly would be asserting the graph works, which is already
tested, rather than that the API drives it.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest
from api_helpers import CASE_ID, EMAIL, api_client, intent

from careloop.events.inbound import REFERRAL_EVENT_ID
from careloop.state import InMemoryCaseStore

WAIT = 10.0
CARER = "rosa"
HEADERS = {"X-Caregiver": CARER}


@pytest.fixture
def client():
    """An app over an empty store, with the planner scripted to propose the caregiver email."""
    with api_client(InMemoryCaseStore(), intent("send_email", **EMAIL)) as opened:
        yield opened


def deliver(client, *, wait=WAIT):
    return client.post(
        f"/api/events?wait={wait}",
        json={"event_id": REFERRAL_EVENT_ID, "case_id": CASE_ID},
    )


def approve_the_card(client, card):
    return client.post(
        f"/api/cases/{CASE_ID}/decisions/{card['decision']['approval_id']}/approve?wait={WAIT}",
        headers=HEADERS,
    )


class TestT1OverHttp:
    """The four beats, in order, each asserted where a judge would look."""

    def test_1_an_email_arrives_and_the_case_appears(self, client) -> None:
        accepted = deliver(client)

        assert accepted.status_code == 202
        assert accepted.json()["case_id"] == CASE_ID
        assert client.get(f"/api/cases/{CASE_ID}").status_code == 200

    def test_2_the_case_is_visible_in_working_while_the_run_is_going(self) -> None:
        """The beat the 202 exists for.

        A synchronous endpoint would return only once the case had already parked, so Working
        would be a column nobody could ever catch a case in. Held at the triage node rather than
        raced, so this asserts the property instead of winning a coin toss — see
        ``api_helpers.PausedTriage``.
        """
        gate = threading.Event()

        with api_client(InMemoryCaseStore(), intent("send_email", **EMAIL), gate=gate) as client:
            accepted = deliver(client, wait=0)
            assert accepted.json()["state"] == "RUNNING"

            mid_run = client.get("/api/cases").json()

            gate.set()
            settled = client.get(f"/api/runs/{accepted.json()['run_id']}?wait={WAIT}").json()
            after = client.get("/api/cases").json()

        assert [card["case_id"] for card in mid_run["working"]] == [CASE_ID]
        assert mid_run["needs_you"] == []
        assert mid_run["completed"] == []

        assert settled["state"] == "SETTLED"
        assert after["working"] == []
        assert [card["case_id"] for card in after["needs_you"]] == [CASE_ID]

    def test_3_a_decision_card_appears_in_needs_you(self, client) -> None:
        deliver(client)

        sections = client.get("/api/cases").json()

        assert sections["working"] == []
        assert len(sections["needs_you"]) == 1
        card = sections["needs_you"][0]
        assert card["case_id"] == CASE_ID
        assert card["state"] == "AWAITING_HUMAN"
        assert card["summary"]

    def test_4_the_card_names_the_tool_the_reason_and_the_arguments(self, client) -> None:
        """What a caregiver is being asked to agree to, and nothing they have to guess at."""
        deliver(client)

        decision = client.get("/api/cases").json()["needs_you"][0]["decision"]

        assert decision["tool_name"] == "send_email"
        assert decision["reason_code"] == "REQUIRES_HUMAN_APPROVAL"
        assert decision["reason_text"] == (
            "This action needs the caregiver's decision. Stop and await it."
        )
        assert decision["args"] == {**decision["args"], **EMAIL}

    def test_5_nothing_has_been_sent_yet(self, client) -> None:
        deliver(client)

        assert client.get(f"/api/cases/{CASE_ID}").json()["actions"] == []

    def test_6_one_click_lands_the_case_in_completed(self, client) -> None:
        deliver(client)
        card = client.get("/api/cases").json()["needs_you"][0]

        response = approve_the_card(client, card)

        sections = client.get("/api/cases").json()
        assert response.status_code == 202
        assert sections["needs_you"] == []
        assert sections["working"] == []
        assert [done["case_id"] for done in sections["completed"]] == [CASE_ID]
        assert sections["completed"][0]["state"] == "DONE"

    def test_7_the_effect_happened_exactly_once(self, client) -> None:
        deliver(client)
        approve_the_card(client, client.get("/api/cases").json()["needs_you"][0])

        body = client.get(f"/api/cases/{CASE_ID}").json()

        assert len(body["actions"]) == 1
        assert body["actions"][0]["action_type"] == "send_email"
        assert body["actions"][0]["idempotency_key"]

    def test_8_the_timeline_carries_the_citation(self, client) -> None:
        """The claim the whole project rests on, readable off the screen.

        ``referral.pdf p.2`` on an evidence entry: this value came from that page of that document,
        and a caregiver can check it.
        """
        deliver(client)
        approve_the_card(client, client.get("/api/cases").json()["needs_you"][0])

        body = client.get(f"/api/cases/{CASE_ID}").json()

        cited = [entry for entry in body["timeline"] if ".pdf p." in entry["summary"]]
        assert cited
        slot = next(fact for fact in body["facts"] if fact["key"] == "slot_start")
        assert slot["citation"].endswith(f"p.{slot['page']}")
        assert slot["trust_level"] == "CONTROLLED"

    def test_9_the_audit_trail_is_ordered_and_complete(self, client) -> None:
        deliver(client)
        approve_the_card(client, client.get("/api/cases").json()["needs_you"][0])

        body = client.get(f"/api/cases/{CASE_ID}").json()

        assert [entry["seq"] for entry in body["timeline"]] == list(range(len(body["timeline"])))
        kinds = [entry["kind"] for entry in body["timeline"]]
        for kind in (
            "CASE_CREATED",
            "EVIDENCE_RECORDED",
            "FACT_PROMOTED",
            "PLAN_PROPOSED",
            "POLICY_REQUIRE_HUMAN",
            "APPROVAL_REQUESTED",
            "APPROVAL_GRANTED",
            "ACTION_EXECUTED",
            "VERIFICATION_PASSED",
        ):
            assert kind in kinds, f"{kind} missing from the arc"

    def test_10_no_diagnostic_reached_the_browser_anywhere_in_the_arc(self, client) -> None:
        deliver(client)
        approve_the_card(client, client.get("/api/cases").json()["needs_you"][0])

        detail = client.get(f"/api/cases/{CASE_ID}").text
        sections = client.get("/api/cases").text

        assert "diagnostic" not in detail
        assert "diagnostic" not in sections


class TestTheArcReallyIsDrivenOverHttp:
    """Otherwise this is the direct-call T1 with some HTTP decoration around it."""

    FORBIDDEN = frozenset(
        {
            "open_case",
            "reopen_case",
            "approve",
            "reject",
            "run_case",
            "transition",
            "append_event",
            "parked_store",
            "finished_store",
            "poisoned_store",
            "drive",
        }
    )

    def test_this_module_names_no_runner_function(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        found = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                found.update(alias.name for alias in node.names)

        offenders = sorted(found & self.FORBIDDEN)

        assert offenders == [], (
            f"{offenders} move the case without going through HTTP. The point of this module is "
            f"that a browser can drive the whole arc; a direct call proves the graph works, which "
            f"tests/test_case_run.py already does."
        )

    def test_the_detector_would_notice(self, tmp_path: Path) -> None:
        """Vacuity: the sweep above is worth nothing if it cannot see a direct call."""
        offending = tmp_path / "cheating.py"
        offending.write_text(
            "from careloop.graph.runner import approve\n\n\ndef go(store):\n"
            "    return approve(store, 'c', 'a', decided_by='rosa')\n",
            encoding="utf-8",
        )

        found = set()
        for node in ast.walk(ast.parse(offending.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.ImportFrom):
                found.update(alias.name for alias in node.names)

        assert "approve" in found & self.FORBIDDEN

    def test_the_direct_call_t1_is_still_there(self) -> None:
        """This module extends T1 rather than replacing it."""
        original = (Path(__file__).parent / "test_case_run.py").read_text(encoding="utf-8")

        assert "class TestT1HappyPath:" in original
