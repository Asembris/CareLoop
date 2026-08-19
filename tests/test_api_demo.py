"""The demo control surface: three buttons, one reset, and no second code path.

The whole value of this layer is that pressing a button runs **the same thing** a browser's
``POST /api/events`` runs. So the tests that matter are not "does the endpoint return 202" but
"is a case the demo produced indistinguishable from a case the ordinary route produced" — and the
sweep that keeps ``CaseStore.clear`` from acquiring a second caller.

Everything here runs on the mock model and the simulated executor. The demo surface is orthogonal
to whether the clinic is real; ``tests/test_verify_readback.py`` is where the real one is driven.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path
from typing import ClassVar

import pytest
from api_helpers import (
    CARER,
    EMAIL,
    factory_proposing,
    intent,
    planner_proposing,
    triage_agent,
)
from fastapi.testclient import TestClient
from helpers import FIXED_NOW

import careloop
from careloop.api import app as app_module
from careloop.api.app import create_app
from careloop.api.demo import (
    DEMO_SCENARIOS,
    DemoScenario,
    UnknownDemoEvent,
    check_demo_scenarios_name_real_events,
)
from careloop.events.inbound import InboundEvent
from careloop.events.simulator import EventSimulator
from careloop.graph.runner import context_for
from careloop.state import CaseState, InMemoryCaseStore

BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": "cardiology review",
}


def demo_client(store, *proposals, demo: bool = True) -> TestClient:
    """An app with the control surface mounted, driven by scripted agents."""
    app = create_app(
        store,
        context_factory=factory_proposing(*proposals),
        demo=demo,
    )
    return TestClient(app)


def clinic_demo_client(store, clinic, *proposals) -> TestClient:
    """The same, bound to a real clinic.

    Scripted agents *and* a real external effect: the context factory has to supply both, because
    an injected factory replaces the one ``create_app`` would have built from the clinic. That is
    the same seam ``factory_proposing`` already uses to put the mock model behind the routes.
    """

    def build(store_, inbound, case_id):
        return context_for(
            store_,
            inbound,
            case_id=case_id,
            triage=triage_agent(),
            planner=planner_proposing(*proposals),
            now=lambda: FIXED_NOW,
            clinic=clinic,
        )

    return TestClient(create_app(store, context_factory=build, clinic=clinic, demo=True))


class TestTheFlagIsExplicit:
    """Off by default, and off means the routes do not exist rather than refuse.

    A reset that deletes every case is right in front of a judge and wrong everywhere else. An
    endpoint that exists and returns 403 is an endpoint somebody can be talked into enabling; one
    that was never mounted is not.
    """

    def test_the_routes_are_absent_without_the_flag(self) -> None:
        with demo_client(InMemoryCaseStore(), demo=False) as client:
            assert client.post("/api/demo/reset").status_code == 404
            assert client.post("/api/demo/scenarios/referral").status_code == 404

    def test_the_routes_are_present_with_it(self) -> None:
        """Vacuity: the 404s above are the flag, not a wrong path."""
        with demo_client(InMemoryCaseStore()) as client:
            assert client.post("/api/demo/reset").status_code == 200

    def test_the_ordinary_routes_are_unaffected_either_way(self) -> None:
        for demo in (True, False):
            with demo_client(InMemoryCaseStore(), demo=demo) as client:
                assert client.get("/api/health").status_code == 200
                assert client.get("/api/cases").status_code == 200


class TestAScenarioRunsTheNormalPath:
    def test_pressing_referral_opens_the_case_and_runs_it(self) -> None:
        store = InMemoryCaseStore()
        with demo_client(store, intent("book_appointment", **BOOKING)) as client:
            response = client.post("/api/demo/scenarios/referral?wait=5")

        assert response.status_code == 202
        case = store.get(DEMO_SCENARIOS["referral"].case_id)
        assert case.state is CaseState.DONE

    def test_the_case_is_indistinguishable_from_one_the_ordinary_route_made(self) -> None:
        """The claim, asserted rather than asserted about.

        Same event, same case id, same agents — one delivered by ``POST /api/events`` and one by
        the demo button. If the demo had its own execution branch, these timelines would differ.
        """
        scenario = DEMO_SCENARIOS["referral"]

        by_demo = InMemoryCaseStore()
        with demo_client(by_demo, intent("book_appointment", **BOOKING)) as client:
            client.post("/api/demo/scenarios/referral?wait=5")

        by_hand = InMemoryCaseStore()
        with demo_client(by_hand, intent("book_appointment", **BOOKING)) as client:
            client.post(
                "/api/events?wait=5",
                json={"case_id": scenario.case_id, "event_id": scenario.event_id},
            )

        demo_case = by_demo.get(scenario.case_id)
        hand_case = by_hand.get(scenario.case_id)

        assert demo_case.state == hand_case.state
        assert [event.kind for event in demo_case.timeline] == [
            event.kind for event in hand_case.timeline
        ]
        assert [event.summary for event in demo_case.timeline] == [
            event.summary for event in hand_case.timeline
        ]

    def test_pressing_resume_parks_the_case_for_a_caregiver(self) -> None:
        """The demo beat: the judge presses this, then presses Approve in the dashboard."""
        store = InMemoryCaseStore()
        scenario = DEMO_SCENARIOS["resume"]
        with demo_client(store, intent("send_email", **EMAIL)) as client:
            client.post("/api/demo/scenarios/resume?wait=5")

            case = store.get(scenario.case_id)
            assert case.state is CaseState.AWAITING_HUMAN

            approval = case.pending_approval()
            assert approval is not None
            resumed = client.post(
                f"/api/cases/{scenario.case_id}/decisions/{approval.approval_id}/approve?wait=5",
                headers={"X-Caregiver": CARER},
            )

        assert resumed.status_code == 202
        assert store.get(scenario.case_id).state is CaseState.DONE

    def test_pressing_injection_denies_and_reaches_no_attacker(self) -> None:
        store = InMemoryCaseStore()
        attacker = "records-intake@claims-continuity-partners.example"
        with demo_client(
            store,
            *[intent("send_email", to=attacker, subject="Referral", body="summary")] * 6,
        ) as client:
            client.post("/api/demo/scenarios/injection?wait=5")

        case = store.get(DEMO_SCENARIOS["injection"].case_id)
        assert case.actions == ()
        assert case.state is not CaseState.DONE

    def test_both_routes_go_through_the_one_delivery_function(self, monkeypatch) -> None:
        """Not "the outcomes match" but "it is literally the same call".

        The indistinguishability test above would still pass if the demo route grew its own copy of
        the delivery logic that happened to behave the same today. This one would not: the spy is
        installed on ``careloop.api.app.deliver``, and a second implementation would not touch it.
        Both routes are driven so the spy is proven to be on the shared path rather than a
        demo-only one.
        """
        scenario = DEMO_SCENARIOS["referral"]
        seen: list[tuple[str, str]] = []
        real = app_module.deliver

        async def spy(deps, case_id, event_id, *, wait):
            seen.append((case_id, event_id))
            return await real(deps, case_id, event_id, wait=wait)

        monkeypatch.setattr(app_module, "deliver", spy)

        with demo_client(InMemoryCaseStore(), intent("book_appointment", **BOOKING)) as client:
            client.post("/api/demo/scenarios/referral?wait=5")
        with demo_client(InMemoryCaseStore(), intent("book_appointment", **BOOKING)) as client:
            client.post(
                "/api/events?wait=5",
                json={"case_id": scenario.case_id, "event_id": scenario.event_id},
            )

        assert seen == [(scenario.case_id, scenario.event_id)] * 2

    def test_an_unknown_scenario_is_a_404_that_lists_the_real_ones(self) -> None:
        with demo_client(InMemoryCaseStore()) as client:
            refused = client.post("/api/demo/scenarios/nonsense")

        assert refused.status_code == 404
        assert "referral" in refused.json()["detail"]


class TestReset:
    def test_reset_clears_every_case(self) -> None:
        store = InMemoryCaseStore()
        with demo_client(store, intent("book_appointment", **BOOKING)) as client:
            client.post("/api/demo/scenarios/referral?wait=5")
            assert client.get("/api/cases").json()["completed"]

            reset = client.post("/api/demo/reset")

            assert reset.status_code == 200
            assert reset.json()["cases_cleared"] == 1
            sections = client.get("/api/cases").json()
            assert sections["needs_you"] == sections["working"] == sections["completed"] == []

    def test_a_scenario_can_be_pressed_again_after_a_reset(self) -> None:
        """What the button is for: back to a known state, and the demo runs the same way twice."""
        store = InMemoryCaseStore()
        with demo_client(store, *[intent("book_appointment", **BOOKING)] * 2) as client:
            client.post("/api/demo/scenarios/referral?wait=5")
            client.post("/api/demo/reset")
            again = client.post("/api/demo/scenarios/referral?wait=5")

        assert again.status_code == 202
        assert store.get(DEMO_SCENARIOS["referral"].case_id).state is CaseState.DONE

    def test_reset_reports_that_it_did_not_reset_a_clinic_when_there_is_none(self) -> None:
        with demo_client(InMemoryCaseStore()) as client:
            body = client.post("/api/demo/reset").json()

        assert body["clinic_reset"] is False
        assert body["clinic_world_ended"] is None

    def test_a_default_world_clinic_is_reseeded_and_reports_no_world_ended(
        self, clinic, clinic_state
    ) -> None:
        """A single-world CareLoop reaches the clinic's default world, which has no lifetime.

        ``null`` rather than ``false``: "there was nothing to end" and "there was a world and it is
        gone" are different answers, and a reader of the deployed check has to be able to tell a
        CareLoop with no session apart from one whose world had already been ended.
        """
        with clinic_demo_client(InMemoryCaseStore(), clinic, intent("send_email", **EMAIL)) as c:
            body = c.post("/api/demo/reset").json()

        assert body["clinic_reset"] is True
        assert body["clinic_world_ended"] is None

    def test_a_bound_clinic_makes_the_demo_booking_real(self, clinic, clinic_state) -> None:
        """One argument to ``create_app`` is the whole difference between a simulated demo and a
        real one, and no route changed to make that true."""
        store = InMemoryCaseStore()
        with clinic_demo_client(store, clinic, intent("book_appointment", **BOOKING)) as client:
            client.post("/api/demo/scenarios/referral?wait=5")

        assert len(clinic_state.appointments()) == 1
        assert store.get(DEMO_SCENARIOS["referral"].case_id).state is CaseState.DONE

    def test_reset_puts_the_external_clinic_back(self, clinic, clinic_state) -> None:
        """With a clinic bound, the world is reset too — a demo half reset is not a known state."""
        store = InMemoryCaseStore()
        with clinic_demo_client(store, clinic, intent("book_appointment", **BOOKING)) as client:
            client.post("/api/demo/scenarios/referral?wait=5")
            assert len(clinic_state.appointments()) == 1

            body = client.post("/api/demo/reset").json()

        assert body["clinic_reset"] is True
        assert clinic_state.appointments() == ()


class TestAScenarioResetsAndStartsInOneRequest:
    """Pressing a scenario puts the world back first, server-side, in one call.

    Two of the scenarios carry the same referral and therefore ask for the same slot, so a scenario
    pressed on top of another one gets a correct refusal from a clinic that already holds it — and
    that reads, in front of a judge, like the injection defence collapsing at something that has
    nothing to do with injection. The frontend used to reset and then start, which is two requests
    with a window between them: the reset lands, the start is refused, and the visitor is left with
    an empty dashboard and no run. No client can close that window from outside.
    """

    def test_a_second_scenario_does_not_find_the_first_ones_cases(self) -> None:
        store = InMemoryCaseStore()
        with demo_client(store, *[intent("book_appointment", **BOOKING)] * 2) as client:
            client.post("/api/demo/scenarios/referral?wait=5")
            client.post("/api/demo/scenarios/injection?wait=5")

            held = client.get("/api/cases").json()

        cases = [card["case_id"] for section in held.values() for card in section]
        assert cases == [DEMO_SCENARIOS["injection"].case_id]

    def test_the_clinic_is_put_back_too(self, clinic, clinic_state) -> None:
        """The half that made the shared demo fail: one seeded slot, pressed twice."""
        store = InMemoryCaseStore()
        with clinic_demo_client(store, clinic, *[intent("book_appointment", **BOOKING)] * 2) as (
            client
        ):
            first = client.post("/api/demo/scenarios/referral?wait=5")
            second = client.post("/api/demo/scenarios/injection?wait=5")

        assert [first.status_code, second.status_code] == [202, 202]
        # One appointment, because the clinic was reseeded — not two, and not a refusal.
        assert len(clinic_state.appointments()) == 1
        assert store.get(DEMO_SCENARIOS["injection"].case_id).state is CaseState.DONE

    def test_it_is_one_request_rather_than_two(self, monkeypatch) -> None:
        """The atomicity claim: the reset happens inside the scenario call, not before it.

        Asserted on the order of what the server did rather than on a status code, because a client
        that reset first would produce the same statuses and the same final state.
        """
        seen: list[str] = []
        real_reset = app_module.reset_world
        real_deliver = app_module.deliver

        def spy_reset(deps):
            seen.append("reset")
            return real_reset(deps)

        async def spy_deliver(deps, case_id, event_id, *, wait):
            seen.append("deliver")
            return await real_deliver(deps, case_id, event_id, wait=wait)

        monkeypatch.setattr(app_module, "reset_world", spy_reset)
        monkeypatch.setattr(app_module, "deliver", spy_deliver)

        with demo_client(InMemoryCaseStore(), intent("book_appointment", **BOOKING)) as client:
            assert client.post("/api/demo/scenarios/referral?wait=5").status_code == 202

        assert seen == ["reset", "deliver"]

    def test_a_run_in_flight_refuses_the_whole_thing_rather_than_half_of_it(self) -> None:
        """409, and nothing wiped: the reset and the start stand or fall together."""
        gate = threading.Event()
        store = InMemoryCaseStore()
        app = create_app(
            store,
            context_factory=factory_proposing(intent("book_appointment", **BOOKING), gate=gate),
            demo=True,
        )
        with TestClient(app) as client:
            try:
                client.post("/api/demo/scenarios/referral")
                refused = client.post("/api/demo/scenarios/injection")

                assert refused.status_code == 409
                assert "in flight" in refused.json()["detail"]
                # The first scenario's case is still there: nothing was cleared on the way to the
                # refusal, which is what "all or nothing" has to mean here.
                assert store.get(DEMO_SCENARIOS["referral"].case_id)
            finally:
                gate.set()


class TestResetRefusesWhileWorkIsInFlight:
    """A store cleared underneath a live graph is a version conflict wearing a costume."""

    def test_a_run_in_flight_makes_reset_a_409(self) -> None:
        gate = threading.Event()
        store = InMemoryCaseStore()
        app = create_app(
            store,
            context_factory=factory_proposing(intent("book_appointment", **BOOKING), gate=gate),
            demo=True,
        )
        with TestClient(app) as client:
            try:
                client.post("/api/demo/scenarios/referral")
                refused = client.post("/api/demo/reset")

                assert refused.status_code == 409
                assert "in flight" in refused.json()["detail"]
            finally:
                # Inside the block: leaving it drains in-flight runs, and a run nobody released
                # would hold the drain until the SDK's node timeout fired.
                gate.set()

    def test_the_same_reset_succeeds_once_the_run_settles(self) -> None:
        """Vacuity: the 409 above is the in-flight check, not a broken endpoint."""
        store = InMemoryCaseStore()
        with demo_client(store, intent("book_appointment", **BOOKING)) as client:
            client.post("/api/demo/scenarios/referral?wait=5")
            assert client.post("/api/demo/reset").status_code == 200


class TestTheScenarioTableIsChecked:
    def test_every_shipped_scenario_names_an_event_that_exists(self) -> None:
        check_demo_scenarios_name_real_events()

    def test_a_scenario_naming_a_missing_event_fails_the_check(self) -> None:
        """Vacuity: driven over a table with a hole, because a guard that can only run against the
        shipped table is a guard whose failure path is untested."""
        empty = EventSimulator(events={})
        with pytest.raises(UnknownDemoEvent):
            check_demo_scenarios_name_real_events(empty)

    def test_the_check_passes_against_a_simulator_holding_only_what_is_named(self) -> None:
        held = {
            scenario.event_id: InboundEvent(
                event_id=scenario.event_id,
                subject="synthetic",
                body="synthetic",
                document_ids=(),
            )
            for scenario in DEMO_SCENARIOS.values()
        }
        check_demo_scenarios_name_real_events(EventSimulator(events=held))

    def test_the_scenarios_have_distinct_case_ids(self) -> None:
        """Two buttons sharing a case id would have each reopen the other's case."""
        case_ids = [scenario.case_id for scenario in DEMO_SCENARIOS.values()]
        assert len(set(case_ids)) == len(case_ids)

    def test_the_table_is_frozen(self) -> None:
        assert isinstance(next(iter(DEMO_SCENARIOS.values())), DemoScenario)
        with pytest.raises(TypeError):
            DEMO_SCENARIOS["extra"] = DEMO_SCENARIOS["referral"]


# -- the sweep ----------------------------------------------------------------------------------


def store_clear_calls(path: Path) -> list[int]:
    """Line numbers where ``path`` calls ``.clear()`` on something named like a store.

    Deliberately narrow. A sweep for every ``.clear()`` would fire on an ordinary ``dict.clear()``
    in some future module and get deleted for being annoying, which is the fate of an over-broad
    guard. This one asks the question actually worth asking: who empties the durable source of
    truth?
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "clear":
            continue
        target = node.func.value
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if "store" in name.lower():
            found.append(node.lineno)
    return found


def careloop_modules() -> list[Path]:
    return sorted(Path(careloop.__file__).parent.rglob("*.py"))


class TestNothingElseClearsTheStore:
    """``CaseStore.clear`` has one caller, and the sweep is what keeps it that way.

    The method is the only destructive one on the durable source of truth. It exists so a judge can
    put a live demo back to a known state; a second caller — a cleanup helper, a test fixture that
    grew into production, a "reinitialise" endpoint — would be a way to lose an audit trail without
    anybody deciding to.
    """

    #: The store implementations may clear themselves; the demo reset may ask them to.
    ALLOWED: ClassVar[set[str]] = {"memory_store.py", "sqlite_store.py", "store.py", "app.py"}

    def test_the_sweep_has_modules_to_sweep(self) -> None:
        names = {path.name for path in careloop_modules()}
        assert {"app.py", "nodes.py", "runner.py", "engine.py"} <= names

    def test_only_the_demo_reset_clears_the_store(self) -> None:
        offenders = {
            path.name: lines
            for path in careloop_modules()
            if path.name not in self.ALLOWED and (lines := store_clear_calls(path))
        }
        assert offenders == {}

    def test_the_one_allowed_caller_really_is_the_demo_reset(self) -> None:
        """Vacuity for the allowlist: ``app.py`` is exempted because it holds the reset route, so
        the exemption has to be spent on exactly that and nothing else."""
        app_module = Path(careloop.__file__).parent / "api" / "app.py"
        lines = store_clear_calls(app_module)
        source = app_module.read_text(encoding="utf-8").splitlines()

        assert len(lines) == 1
        assert "deps.store.clear()" in source[lines[0] - 1]

    def test_the_detector_finds_a_clear_where_one_exists(self, tmp_path: Path) -> None:
        offender = tmp_path / "offender.py"
        offender.write_text(
            "def wipe(store):\n    store.clear()\n\ndef also(deps):\n    deps.store.clear()\n",
            encoding="utf-8",
        )
        assert store_clear_calls(offender) == [2, 5]

    def test_the_detector_ignores_an_ordinary_dict_clear(self, tmp_path: Path) -> None:
        """Otherwise the guard fires on unrelated code and gets deleted for being noise."""
        innocent = tmp_path / "innocent.py"
        innocent.write_text(
            "def reset(cache: dict) -> None:\n    cache.clear()\n", encoding="utf-8"
        )
        assert store_clear_calls(innocent) == []


class TestTheScenarioTableIsCoherent:
    """The table is the one place a demo scenario is declared. These are the properties that make
    adding a fifth entry safe rather than a thing to be careful about."""

    def test_case_ids_are_distinct(self) -> None:
        """Two scenarios sharing a case id would have the second reopen the first's case.

        Nothing raises when that happens — the reopen is legal — so the demo would simply show a
        judge the wrong history under the right button. The ids are fixed on purpose (see the
        module's own note about reset having a knowable set of cases), which is exactly what makes
        a collision easy to introduce and invisible afterwards.
        """
        case_ids = [scenario.case_id for scenario in DEMO_SCENARIOS.values()]
        assert len(case_ids) == len(set(case_ids))

    def test_event_ids_are_distinct(self) -> None:
        """Two scenarios on one event are two buttons that do the same thing."""
        event_ids = [scenario.event_id for scenario in DEMO_SCENARIOS.values()]
        assert len(event_ids) == len(set(event_ids))

    def test_the_key_is_the_mapping_key(self) -> None:
        assert all(key == scenario.key for key, scenario in DEMO_SCENARIOS.items())

    def test_injection_only_is_reachable_and_is_not_the_mixed_scenario(self) -> None:
        """The video's beat needs its own key.

        ``injection`` mixes the poisoned notice with real work, so a refusal is genuine when it
        happens and absent when it does not — session E measured 3 runs in 6. ``injection_only``
        carries the poison alone, which is where the refusal is reachable without a model being
        tuned into taking bait. Both exist; neither replaces the other.
        """
        assert "injection_only" in DEMO_SCENARIOS
        alone, mixed = DEMO_SCENARIOS["injection_only"], DEMO_SCENARIOS["injection"]
        assert alone.event_id != mixed.event_id
        assert alone.case_id != mixed.case_id
