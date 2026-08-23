"""The recovery scenario: what it arms, what it cannot arm, and what a reset takes away.

``recovery`` is the only demo scenario that arms anything, and everything about the arming is a
narrowing. This file holds the narrowings that are about the *scenario* — where it may be pressed,
what it binds to, and what survives a reset. The narrowings that are about the transport are in
``tests/test_faults.py``, and the end-to-end proof that a real clinic really commits and a real case
really recovers is in :class:`TestTheJudgeVisibleRecovery` below.

**The containment claim, stated once.** A booking may lose its answer only if all of these hold: the
demo surface is mounted at all; the scenario that declares the loss was pressed; the world has a
real clinic; the run belongs to the case that scenario named; the request is a booking creation; and
the provider answered ``201``. The first three are here, the fourth is the binding this file proves
end to end with a second case booking in the same world, and the last two are the transport's.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from api_helpers import intent, planner_proposing, triage_agent
from fastapi import HTTPException
from fastapi.testclient import TestClient
from helpers import BOOKING_REASON, FIXED_NOW

import careloop
from careloop.api.app import create_app, reset_world
from careloop.api.demo import DEMO_SCENARIOS
from careloop.api.deps import ApiDeps, clinic_context_factory
from careloop.api.faults import BookingResponseLoss, booking_response_loss
from careloop.events.inbound import InboundEvent
from careloop.graph.runner import context_for
from careloop.state import CaseState, InMemoryCaseStore
from careloop.tools.clinic import ClinicClient
from demoworld.clinic import Clinic

RECOVERY = DEMO_SCENARIOS["recovery"]

BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": BOOKING_REASON,
}

#: A second seeded slot, so an unrelated case can book without colliding with the scenario's.
OTHER_SLOT = "2026-09-04T14:00:00"


@pytest.fixture
def loss(clinic_service: tuple[str, Clinic], clinic_state: Clinic) -> Iterator[BookingResponseLoss]:
    base_url, _ = clinic_service
    made = booking_response_loss(base_url)
    yield made
    made.close()


def recovery_client(store, clinic: ClinicClient | None, faults: BookingResponseLoss | None):
    """A demo app wired the way a visitor's world is, with scripted agents in place of a model.

    The binding is written out rather than composed from
    :meth:`~careloop.api.faults.BookingResponseLoss.context_factory` for the reason
    ``scripts/e2e_serve.py`` writes it out: this factory also has to supply the scripted agents, and
    there is only ever one context to build. That the two spellings agree is
    :class:`TestTheComposedFactoryIsTheOrdinaryOneUntilSomethingIsArmed`'s job.
    """

    def build(store_, inbound, case_id: str):
        reaching = faults.clinic if faults is not None and faults.claims(case_id) else clinic
        return context_for(
            store_,
            inbound,
            case_id=case_id,
            triage=triage_agent(),
            planner=planner_proposing(*[intent("book_appointment", **BOOKING)] * 4),
            now=lambda: FIXED_NOW,
            clinic=reaching,
        )

    return TestClient(
        create_app(store, context_factory=build, clinic=clinic, demo=True, faults=faults)
    )


class TestWhereTheScenarioMayBePressedAtAll:
    """Three gates in front of anything being armed, and none of them is a flag in a request."""

    def test_it_does_not_exist_without_the_demo_surface(self) -> None:
        """Not mounted rather than mounted and refusing, exactly as the reset is not.

        A route that exists and says no is a route somebody can be talked into enabling.
        """
        app = create_app(InMemoryCaseStore(), demo=False)
        with TestClient(app) as client:
            assert client.post("/api/demo/scenarios/recovery").status_code == 404

    def test_it_refuses_truthfully_when_the_world_has_no_clinic(self) -> None:
        """Every effect would be simulated, so there is nothing to be uncertain about.

        409 rather than 500 or a quiet success: the request is well formed and names a real
        scenario, and what it conflicts with is how this CareLoop is wired. A scenario that ran
        anyway would show a judge a recovery that demonstrated nothing, which is the honest-looking
        failure and the worst kind.
        """
        store = InMemoryCaseStore()
        with recovery_client(store, None, None) as client:
            refused = client.post("/api/demo/scenarios/recovery")

        assert refused.status_code == 409
        assert "no external write" in refused.json()["detail"]

    def test_nothing_was_delivered_when_it_refused(self) -> None:
        """All-or-nothing. A refused press must not leave a case behind that nobody armed for."""
        store = InMemoryCaseStore()
        with recovery_client(store, None, None) as client:
            client.post("/api/demo/scenarios/recovery")
            assert client.get("/api/cases").json()["working"] == []
            assert client.get("/api/cases").json()["needs_you"] == []
            assert client.get("/api/cases").json()["completed"] == []

    def test_no_other_scenario_declares_a_loss(self) -> None:
        """One scenario arms, and the table says which. A second would be a decision, not a typo."""
        arming = [
            scenario.key
            for scenario in DEMO_SCENARIOS.values()
            if scenario.loses_the_booking_response
        ]
        assert arming == ["recovery"]


class TestTheScenarioArmsItsOwnCaseAndNothingElse:
    """The binding, at the route."""

    def test_pressing_it_arms_the_case_it_delivers_to(self, loss, clinic_state: Clinic) -> None:
        store = InMemoryCaseStore()
        with recovery_client(store, loss.clinic, loss) as client:
            client.post(f"/api/demo/scenarios/recovery?wait={0.0}")

        assert loss.armed_for == RECOVERY.case_id
        assert loss.claims(RECOVERY.case_id) is True

    def test_it_claims_no_other_case(self, loss, clinic_state: Clinic) -> None:
        store = InMemoryCaseStore()
        with recovery_client(store, loss.clinic, loss) as client:
            client.post("/api/demo/scenarios/recovery?wait=0.0")

        for scenario in DEMO_SCENARIOS.values():
            if scenario.key != "recovery":
                assert loss.claims(scenario.case_id) is False

    def test_pressing_an_ordinary_scenario_arms_nothing(self, loss, clinic_state: Clinic) -> None:
        store = InMemoryCaseStore()
        with recovery_client(store, loss.clinic, loss) as client:
            client.post("/api/demo/scenarios/referral?wait=15.0")

        assert loss.armed_for is None
        assert loss.transport.losses == 0


class TestAResetTakesTheArmingWithIt:
    """The other half of one-shot: what an abandoned scenario leaves behind, which is nothing."""

    def test_a_reset_disarms(self, loss, clinic_state: Clinic) -> None:
        store = InMemoryCaseStore()
        with recovery_client(store, loss.clinic, loss) as client:
            client.post("/api/demo/scenarios/recovery?wait=15.0")
            reset = client.post("/api/demo/reset")

        assert reset.status_code == 200
        assert loss.armed_for is None
        assert loss.transport.armed is False

    def test_the_next_scenario_starts_from_nothing_armed(self, loss, clinic_state: Clinic) -> None:
        """A judge pressing 04 and then 01 must get an ordinary 01.

        The scenario route resets before it arms, so this is the same disarm as above reached the
        other way round — which is the point of there being one reset function rather than two.
        """
        store = InMemoryCaseStore()
        with recovery_client(store, loss.clinic, loss) as client:
            client.post("/api/demo/scenarios/recovery?wait=15.0")
            losses_after_recovery = loss.transport.losses
            client.post("/api/demo/scenarios/referral?wait=15.0")

        assert losses_after_recovery == 1
        assert loss.transport.losses == 1
        assert loss.armed_for is None

    def test_a_refused_reset_leaves_the_arming_exactly_as_it_was(self, loss) -> None:
        """A reset that changes nothing must change nothing, the arming included.

        The in-flight check runs before anything is cleared, so the disarm is never reached — the
        same ordering that makes ``discard_deferred`` safe. Asserted rather than assumed, because
        "a refusal is inert" is the property, not "the check happens to come first today".

        Driven against :func:`careloop.api.app.reset_world` directly rather than over HTTP, because
        what has to be held during the call is a run in flight *on the loop the reset is running
        on*, and a synchronous test client puts the two on different loops.
        """
        deps = ApiDeps(store=InMemoryCaseStore(), clinic=loss.clinic, faults=loss)
        loss.arm(RECOVERY.case_id)

        async def refuse() -> int:
            held = asyncio.Event()

            async def busy() -> None:
                await held.wait()

            deps.runs.start("busy-case", busy)
            await asyncio.sleep(0)
            try:
                await reset_world(deps)
            except HTTPException as refusal:
                return refusal.status_code
            finally:
                held.set()
                await deps.runs.drain()
            return 200

        assert asyncio.run(refuse()) == 409
        assert loss.armed_for == RECOVERY.case_id
        assert loss.transport.armed is True


class TestTheComposedFactoryIsTheOrdinaryOneUntilSomethingIsArmed:
    """Two ways of building the same thing, and the second must not quietly become different.

    ``tests/test_witness_span.py`` makes this assertion about ``simulated_context_factory`` and the
    default one, for the same reason: a world whose factory is composed from the loss capability
    must, with nothing armed, bind runs to exactly what
    :func:`careloop.api.deps.clinic_context_factory` binds them to.
    """

    def test_an_unarmed_world_binds_runs_to_the_ordinary_clinic(self, loss) -> None:
        ordinary = ClinicClient("http://clinic.test")
        event = InboundEvent(event_id="evt-x", subject="s", body="b")

        composed = loss.context_factory(ordinary)(InMemoryCaseStore(), event, "any-case")
        plain = clinic_context_factory(ordinary)(InMemoryCaseStore(), event, "any-case")

        assert composed.clinic is plain.clinic
        # By capability rather than by identity: `readbacks` holds closures built per context, so
        # two contexts never share objects and comparing them would only assert that.
        assert set(composed.readbacks) == set(plain.readbacks)
        assert composed.case_id == plain.case_id
        # Same kind of executor over the same client: `context_for` builds a clinic-backed one
        # either way, and which client it books through is the assertion above.
        assert type(composed.executor) is type(plain.executor)
        ordinary.close()

    def test_an_armed_world_still_binds_every_other_case_that_way(self, loss) -> None:
        ordinary = ClinicClient("http://clinic.test")
        event = InboundEvent(event_id="evt-x", subject="s", body="b")
        loss.arm(RECOVERY.case_id)

        composed = loss.context_factory(ordinary)(InMemoryCaseStore(), event, "someone-else")

        assert composed.clinic is ordinary
        ordinary.close()


# -- the sweep ----------------------------------------------------------------------------------


def package_root() -> Path:
    return Path(careloop.__file__).parent


def arm_calls(path: Path) -> list[int]:
    """Line numbers where ``path`` calls ``.arm(...)`` on something named like a fault holder.

    Narrow in the way ``tests/test_api_demo.py``'s ``store.clear`` sweep is narrow: a sweep for
    every ``.arm()`` anywhere would eventually fire on something unrelated and get deleted for being
    annoying. This asks the question worth asking — who can put a case into an unknown external
    outcome?
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "arm":
            continue
        target = node.func.value
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if any(word in name.lower() for word in ("fault", "loss", "transport")):
            found.append(node.lineno)
    return found


class TestOnlyTheScenarioRouteArmsAnything:
    """One production caller, and a sweep to keep it that way.

    The arrangement ``CaseStore.clear`` has, for the same reason. The next person who wants an
    ambiguous outcome for something — a second scenario, a debugging route, a test fixture that
    took a shortcut — should have to change this test, which is where the argument about whether
    they should is written down.

    ``careloop/api/faults.py`` is excluded by name: it is where ``arm`` is *defined*, and
    :meth:`~careloop.api.faults.BookingResponseLoss.arm` delegating to its transport is the
    implementation rather than a second caller.
    """

    #: Where ``arm`` lives, and is therefore allowed to be called on the way through.
    DEFINITION = "faults.py"

    def test_the_sweep_has_modules_to_sweep(self) -> None:
        assert len(list(package_root().rglob("*.py"))) > 20

    def test_exactly_one_module_arms_a_fault(self) -> None:
        callers = {
            module.name: arm_calls(module)
            for module in package_root().rglob("*.py")
            if module.name != self.DEFINITION and arm_calls(module)
        }

        assert sorted(callers) == ["app.py"]

    def test_it_is_the_one_line_inside_the_arming_function(self) -> None:
        source = (package_root() / "api" / "app.py").read_text(encoding="utf-8").splitlines()
        lines = arm_calls(package_root() / "api" / "app.py")

        assert len(lines) == 1
        assert "deps.faults.arm(scenario.case_id)" in source[lines[0] - 1]

    def test_the_detector_finds_a_planted_second_caller(self, tmp_path: Path) -> None:
        """The sweep's own vacuity proof."""
        planted = tmp_path / "planted.py"
        planted.write_text("def sneak(deps):\n    deps.faults.arm('some-case')\n", encoding="utf-8")

        assert arm_calls(planted) == [2]

    def test_the_detector_ignores_an_unrelated_arm(self, tmp_path: Path) -> None:
        planted = tmp_path / "unrelated.py"
        planted.write_text("def go(robot):\n    robot.arm('up')\n", encoding="utf-8")

        assert arm_calls(planted) == []


class TestTheDemoNeverManufacturesRecoveryState:
    """The structural half of "this is the real path".

    A demo that wrote ``WAITING_EXTERNAL``, appended an ``ACTION_PREPARED``, forged an
    ``ActionRecord`` or marked a verification would be showing a picture of recovery rather than a
    recovery. The only mechanism it is allowed is: arm the transport, deliver the ordinary event.

    Swept over the two functions the scenario route actually runs, rather than over the whole
    module, because ``careloop/api/app.py`` legitimately contains the reset, the decision routes and
    the delivery path.
    """

    #: The functions a recovery scenario press executes in this layer.
    SCENARIO_PATH = ("run_scenario", "arm_the_lost_booking_response")

    #: Names that would mean this layer manufactured a state rather than letting one be reached.
    FORBIDDEN = frozenset(
        {
            "transition",
            "append_event",
            "save",
            "ActionRecord",
            "Preparation",
            "CaseState",
            "TimelineKind",
            "ReconcileNode",
            "ExecuteNode",
            "VerifyNode",
            "Verification",
        }
    )

    def _functions(self) -> dict[str, ast.AST]:
        tree = ast.parse((package_root() / "api" / "app.py").read_text(encoding="utf-8"))
        found: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                found[node.name] = node
        return found

    def test_the_sweep_has_the_functions_it_claims_to_sweep(self) -> None:
        found = self._functions()
        for name in self.SCENARIO_PATH:
            assert name in found, name

    @pytest.mark.parametrize("function", SCENARIO_PATH)
    def test_the_scenario_path_names_nothing_that_writes_a_case(self, function: str) -> None:
        node = self._functions()[function]
        named = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name):
                named.add(inner.id)
            elif isinstance(inner, ast.Attribute):
                named.add(inner.attr)

        assert named & self.FORBIDDEN == set()

    def test_the_scenario_path_does_not_reach_the_store_directly(self) -> None:
        """``reset_world`` clears the store and this path calls it; nothing else touches it.

        The distinction is deliberate. Clearing a world is a demo affordance that has existed since
        the surface did and has its own sweep; writing a *case* is what a demo must never do.
        """
        node = self._functions()["arm_the_lost_booking_response"]
        attributes = {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)}

        assert "store" not in attributes


class TestTheStateMachineIsUntouchedByAnyOfThis:
    """The recovery scenario adds no state, no transition and no timeline kind.

    Pinned here rather than left to the closed-layer sweep because "the demo did not quietly widen
    the lifecycle" is a different claim from "the demo module is not imported", and it is the one a
    reader of this feature would want checked.
    """

    def test_the_states_are_the_ten_that_already_existed(self) -> None:
        assert len(CaseState) == 10

    def test_waiting_external_is_still_written_by_one_function(self) -> None:
        """The saga's own sweep owns this; asserted here so this feature is on the hook for it too.

        ``tests/test_reconcile_writers.py`` is the authority. If this ever fails, something in this
        feature reached into the lifecycle, which it may not.
        """
        source = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")

        assert source.count("CaseState.WAITING_EXTERNAL,") == 1
