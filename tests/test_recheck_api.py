"""The re-check over HTTP, and the boundary that stops a caller aiming it.

``tests/test_recheck.py`` owns what a re-check *does*. This file owns the two things that only
exist at the edge:

**The caller cannot choose what CareLoop reads.** That is structural rather than validated — the
handler has no body parameter, so there is nowhere for a tool name, an argument, a digest, a
confirmation id or an action index to enter. :class:`TestTheClientCannotAimTheReCheck` posts a body
carrying all of them and asserts the confirmation id actually fetched is still the handoff's, which
is the assertion that matters; a status code alone would pass against a route that read the body and
happened to ignore one field.

**Acceptance and outcome are different questions.** A request is refused before anything moves when
the *case* is not in a shape where looking again means anything. A request is *accepted* and then
parks when the *world* will not answer — a clinic that is unreachable is not a malformed request,
and rejecting one at the API would move a domain verdict into the routing layer.

Modelled on ``tests/test_api_decisions.py``, which is the same shape one verb over.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from api_helpers import CARER, finished_store, parked_store
from fastapi.testclient import TestClient
from test_post_effect_lifecycle import count, entries
from test_recheck import RecordsWhatItWasAsked, park
from test_verify_readback import BOOKING, CASE_ID, ChangesTheReason

from careloop.api.app import create_app
from careloop.api.projections import decision_view
from careloop.events.simulator import EventSimulator
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.clinic import ClinicClient
from careloop.tools.results import AppointmentBooked
from demoworld.clinic import Clinic

RECHECK = f"/api/cases/{CASE_ID}/recheck"


def app_over(store: InMemoryCaseStore, clinic: ClinicClient | None = None) -> TestClient:
    """A client over a store, bound to a clinic when the scenario needs one observed."""
    return TestClient(create_app(store, events=EventSimulator(), clinic=clinic))


def parked_booking(base_url: str, world: Clinic):
    """A store holding one case stopped on a booking the clinic disagrees about."""
    parked, store = park(ChangesTheReason, world, base_url)
    return parked, store


def nothing_safe_store() -> tuple[InMemoryCaseStore, str]:
    """The other kind of handoff: every tool proposed, every one refused, and nothing carried out.

    Built through ``test_no_safe_action.drive``, the canonical producer of a
    ``NO_SAFE_ACTION_AVAILABLE`` parking, rather than through a hand-assembled case — what these
    assertions are about is that the *real* other handoff is refused a re-check. Its case id is its
    own, so it is returned rather than assumed.
    """
    from test_no_safe_action import CASE_ID as NO_SAFE_CASE_ID
    from test_no_safe_action import EVERY_TOOL_REFUSED, drive

    parked, context = drive(*EVERY_TOOL_REFUSED)
    assert parked.state is CaseState.AWAITING_HUMAN
    return context.store, NO_SAFE_CASE_ID


# -- the request that works ------------------------------------------------------------------------


class TestARequestedReCheckRunsAndCanComplete:
    """One POST, and the case finishes through the ordinary verifier."""

    def test_a_corrected_record_completes_the_case(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)
        world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])

        with app_over(store, ClinicClient(base_url)) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER}, params={"wait": 5.0})

        assert response.status_code == 202
        body = response.json()
        assert body["state"] == "SETTLED"
        assert body["executed_nodes"] == ["dispatch", "verify"]
        assert store.get(CASE_ID).state is CaseState.DONE

    def test_it_records_the_caregiver_who_asked(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)
        world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])

        with app_over(store, ClinicClient(base_url)) as client:
            client.post(RECHECK, headers={"X-Caregiver": CARER}, params={"wait": 5.0})

        asked = entries(store.get(CASE_ID), TimelineKind.EXTERNAL_RECHECK_REQUESTED)
        assert len(asked) == 1
        assert asked[0].actor == f"human:{CARER}"

    def test_a_still_wrong_record_is_still_a_202_and_parks_again(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """**Acceptance is not certification.** The world disagreeing is a verdict, not a 4xx."""
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)

        with app_over(store, ChangesTheReason(base_url, world)) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER}, params={"wait": 5.0})

        assert response.status_code == 202
        after = store.get(CASE_ID)
        assert after.state is CaseState.AWAITING_HUMAN
        assert count(after, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 2

    def test_a_run_with_no_observer_is_accepted_and_refuses_to_certify(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """Observer *availability* is a property of the run, so it cannot be an acceptance check.

        The app here is built with no clinic at all — the deployment-wiring mistake the guard
        exists for. The request is well formed and the case is genuinely eligible, so it is
        accepted; the verification then reads nothing and refuses.
        """
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)

        with app_over(store, None) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER}, params={"wait": 5.0})

        assert response.status_code == 202
        assert store.get(CASE_ID).state is CaseState.AWAITING_HUMAN
        assert count(store.get(CASE_ID), TimelineKind.VERIFICATION_PASSED) == 0


# -- the client trust boundary --------------------------------------------------------------------


class TestTheClientCannotAimTheReCheck:
    """**The security property of this route.** Nothing a caller sends selects the target.

    Structural rather than validated: the handler declares no body parameter, so there is no field
    for any of these to bind to. The assertion is on the external object actually fetched, because
    that is what a caregiver is relying on — a status code would pass against a route that read the
    body and merely happened not to use one of the keys.
    """

    HOSTILE: ClassVar[dict] = {
        "tool_name": "send_email",
        "args": {"to": "attacker@evil.example", "subject": "x", "body": "x"},
        "confirmation_id": "apt-somebody-elses-appointment",
        "idempotency_key": "0" * 64,
        "args_digest": "f" * 64,
        "action_index": 1,
    }

    def test_a_hostile_body_does_not_redirect_the_observation(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, store = parked_booking(base_url, world)
        world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])
        observer = RecordsWhatItWasAsked(base_url)

        with app_over(store, observer) as client:
            response = client.post(
                RECHECK,
                headers={"X-Caregiver": CARER},
                params={"wait": 5.0},
                json=self.HOSTILE,
            )

        booked = AppointmentBooked.model_validate_json(parked.actions[0].result)
        assert response.status_code == 202
        assert observer.observed == [booked.confirmation_id]
        assert "apt-somebody-elses-appointment" not in observer.observed

    def test_the_recorded_request_names_the_servers_own_target(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, store = parked_booking(base_url, world)

        with app_over(store, ChangesTheReason(base_url, world)) as client:
            client.post(
                RECHECK,
                headers={"X-Caregiver": CARER},
                params={"wait": 5.0},
                json=self.HOSTILE,
            )

        asked = entries(store.get(CASE_ID), TimelineKind.EXTERNAL_RECHECK_REQUESTED)[0]
        assert asked.payload["tool_name"] == "book_appointment"
        assert asked.payload["idempotency_key"] == parked.actions[0].idempotency_key
        assert asked.payload["idempotency_key"] != self.HOSTILE["idempotency_key"]

    def test_no_new_action_is_recorded_whatever_the_body_asked_for(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, store = parked_booking(base_url, world)

        with app_over(store, ChangesTheReason(base_url, world)) as client:
            client.post(
                RECHECK,
                headers={"X-Caregiver": CARER},
                params={"wait": 5.0},
                json=self.HOSTILE,
            )

        after = store.get(CASE_ID)
        assert after.actions == parked.actions
        assert count(after, TimelineKind.ACTION_EXECUTED) == 1
        assert count(after, TimelineKind.PLAN_PROPOSED) == 1
        assert count(after, TimelineKind.POLICY_ALLOW) == 1
        assert len(world.appointments()) == 1


# -- the caregiver header -------------------------------------------------------------------------


class TestTheCaregiverHeaderIsRequiredButIsNotAuthentication:
    """The same 400-not-401 rule the decision routes follow, for the same stated reason."""

    @pytest.mark.parametrize("headers", [{}, {"X-Caregiver": "   "}], ids=["missing", "blank"])
    def test_it_is_a_400(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic, headers
    ) -> None:
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)

        with app_over(store, ClinicClient(base_url)) as client:
            response = client.post(RECHECK, headers=headers)

        assert response.status_code == 400
        assert "X-Caregiver" in response.json()["detail"]

    def test_nothing_moves_when_the_header_is_missing(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        parked, store = parked_booking(base_url, world)

        with app_over(store, ClinicClient(base_url)) as client:
            client.post(RECHECK)

        after = store.get(CASE_ID)
        assert (after.version, after.state) == (parked.version, parked.state)
        assert after.timeline == parked.timeline


# -- what the route refuses -----------------------------------------------------------------------


class TestRequestsThatCannotBeAccepted:
    """Every refusal, and the proof that each leaves the case byte-for-byte where it was."""

    def test_a_missing_case_is_a_404(self) -> None:
        with app_over(InMemoryCaseStore()) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER})

        assert response.status_code == 404

    def test_a_pending_approval_case_is_a_409(self) -> None:
        """A re-check endpoint must not become an alternate decision endpoint."""
        store = parked_store()
        before = store.get(CASE_ID)

        with app_over(store) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER})

        assert response.status_code == 409
        after = store.get(CASE_ID)
        assert (after.version, after.state) == (before.version, before.state)
        assert after.timeline == before.timeline
        assert after.approvals == before.approvals

    def test_a_nothing_safe_to_do_handoff_is_a_409(self) -> None:
        """Every tool was refused and nothing happened, so there is no effect to observe."""
        store, case_id = nothing_safe_store()
        before = store.get(case_id)

        with app_over(store) as client:
            response = client.post(f"/api/cases/{case_id}/recheck", headers={"X-Caregiver": CARER})

        assert response.status_code == 409
        after = store.get(case_id)
        assert (after.version, after.state) == (before.version, before.state)
        assert after.timeline == before.timeline

    def test_a_finished_case_is_a_409(self) -> None:
        store = finished_store()
        before = store.get(CASE_ID)
        assert before.state is CaseState.DONE

        with app_over(store) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER})

        assert response.status_code == 409
        assert store.get(CASE_ID).version == before.version

    def test_a_run_in_flight_is_a_409_and_nothing_is_recorded(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The existing registry guard, reused. No second locking mechanism."""
        base_url, world = clinic_service
        parked, store = parked_booking(base_url, world)

        with app_over(store, ClinicClient(base_url)) as client:
            deps = client.app.state.careloop
            deps.runs._records["run-held"] = deps.runs._records.get("run-held") or _running_record(
                CASE_ID
            )

            response = client.post(RECHECK, headers={"X-Caregiver": CARER})

        assert response.status_code == 409
        after = store.get(CASE_ID)
        assert (after.version, after.state) == (parked.version, parked.state)
        assert after.timeline == parked.timeline

    def test_a_case_still_in_verification_is_a_409(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """The double-click refusal, driven off durable state rather than off a race.

        The first request commits ``AWAITING_HUMAN → VERIFYING``; the second arrives at a case that
        is no longer parked and is refused by the state precondition. Set up here by committing the
        first request through the runner directly, so the assertion is about the guard rather than
        about how quickly a background task happens to run —
        ``tests/test_recheck.py::TestARequestIsMadeOnce`` covers the same boundary at the layer that
        owns it.
        """
        from careloop.graph.runner import request_recheck

        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)
        request_recheck(store, CASE_ID, requested_by=CARER)
        mid_flight = store.get(CASE_ID)
        assert mid_flight.state is CaseState.VERIFYING

        with app_over(store, ChangesTheReason(base_url, world)) as client:
            response = client.post(RECHECK, headers={"X-Caregiver": CARER})

        assert response.status_code == 409
        after = store.get(CASE_ID)
        assert after.version == mid_flight.version
        assert after.timeline == mid_flight.timeline
        assert count(after, TimelineKind.EXTERNAL_RECHECK_REQUESTED) == 1


def _running_record(case_id: str):
    from careloop.api.runs import RunRecord, RunState

    return RunRecord(
        run_id="run-held",
        case_id=case_id,
        state=RunState.RUNNING,
        executed_nodes=(),
        error=None,
    )


# -- what the dashboard is told -------------------------------------------------------------------


class TestTheDashboardIsToldWhetherAReCheckIsPossible:
    """``recheckable`` reaches the browser, and it is server truth rather than a code lookup."""

    def test_a_post_effect_handoff_offers_one(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)

        with app_over(store, ClinicClient(base_url)) as client:
            detail = client.get(f"/api/cases/{CASE_ID}").json()

        assert detail["decision"]["reason_code"] == "EXTERNAL_STATE_NOT_CONFIRMED"
        assert detail["decision"]["recheckable"] is True
        assert detail["decision"]["approval_id"] == ""

    def test_an_ordinary_decision_does_not(self) -> None:
        with app_over(parked_store()) as client:
            detail = client.get(f"/api/cases/{CASE_ID}").json()

        assert detail["decision"]["reason_code"] == "REQUIRES_HUMAN_APPROVAL"
        assert detail["decision"]["recheckable"] is False

    def test_a_nothing_safe_to_do_handoff_does_not(self) -> None:
        store, case_id = nothing_safe_store()

        with app_over(store) as client:
            detail = client.get(f"/api/cases/{case_id}").json()

        assert detail["decision"]["reason_code"] == "NO_SAFE_ACTION_AVAILABLE"
        assert detail["decision"]["recheckable"] is False

    def test_the_dashboard_card_carries_it_too(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)

        with app_over(store, ClinicClient(base_url)) as client:
            sections = client.get("/api/cases").json()

        card = sections["needs_you"][0]
        assert card["decision"]["recheckable"] is True

    def test_the_flag_and_the_endpoint_agree(
        self, clinic_service: tuple[str, Clinic], clinic_state: Clinic
    ) -> None:
        """One resolver, two readers — asserted rather than assumed.

        A case the projector calls re-checkable must be one the endpoint accepts, and a case it
        does not must be one the endpoint refuses. Anything else means the button and the route are
        answering different questions, which is the defect the shared resolver exists to prevent.

        It also pins the liveness half: a re-check that still could not confirm leaves the case
        parked and **re-checkable again**. One attempt does not spend the recovery path, which is
        the whole point of closing the dead end rather than narrowing it.
        """
        base_url, world = clinic_service
        _parked, store = parked_booking(base_url, world)
        eligible = decision_view(store.get(CASE_ID)).recheckable

        with app_over(store, ChangesTheReason(base_url, world)) as client:
            first = client.post(RECHECK, headers={"X-Caregiver": CARER}, params={"wait": 5.0})
            still = decision_view(store.get(CASE_ID)).recheckable
            second = client.post(RECHECK, headers={"X-Caregiver": CARER}, params={"wait": 5.0})

        assert (eligible, first.status_code) == (True, 202)
        assert (still, second.status_code) == (True, 202)
        assert count(store.get(CASE_ID), TimelineKind.EXTERNAL_RECHECK_REQUESTED) == 2
        assert count(store.get(CASE_ID), TimelineKind.ACTION_EXECUTED) == 1

    def test_a_case_needing_nothing_has_no_decision_at_all(self) -> None:
        with app_over(finished_store()) as client:
            detail = client.get(f"/api/cases/{CASE_ID}").json()

        assert detail["decision"] is None


# -- the demo surface is untouched ----------------------------------------------------------------


def test_the_route_exists_without_the_demo_surface() -> None:
    """A re-check is ordinary product behaviour, not a demo control."""
    with app_over(InMemoryCaseStore()) as client:
        paths = {route.path for route in client.app.routes}

    assert "/api/cases/{case_id}/recheck" in paths
    assert not any(path.startswith("/api/demo") for path in paths)
