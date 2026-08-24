"""P9, P10 and P12 -- the effects still happen, only for the right caller, and carry no identity.

Three properties that are easy to *say* and are asserted here against the provider and the durable
record rather than against a status code.

**P9 -- the two real effects are unchanged.** An authorised caregiver books against a real clinic on
a loopback socket and the read-back returns ``MATCHED``. Identity decided who could ask; it changed
nothing about what was sent, what was recorded, or what was verified.

**P10 -- an unauthorised caller produces zero external effects.** Not "is answered 401" -- the
clinic's own record is empty, the case has no ``ActionRecord``, and no ``ACTION_PREPARED`` entry was
written. A 401 with a booking behind it would be worse than a 200.

**P12 -- identity does not leak.** No ``caregiver_id``, no token, no subject and no email reaches a
provider payload, a span attribute or a log line. The nine frozen telemetry attributes are still
nine, and the assertion is over the *clinic's own stored record* -- what the third party ended up
holding -- rather than over what CareLoop believes it sent.
"""

from __future__ import annotations

import logging

import pytest
from api_helpers import intent
from fastapi.testclient import TestClient
from helpers import BOOKING_REASON, FIXED_NOW
from identity_helpers import (
    CAREGIVER_A,
    DESTINATIONS_A,
    PATIENT_A,
    TOKEN_A,
    TOKEN_B,
    bearer,
    production_boundary,
    two_caregiver_directory,
)

from careloop.api.app import create_app
from careloop.api.deps import patient_authority_for
from careloop.graph.runner import context_for
from careloop.state import CaseState, InMemoryCaseStore
from careloop.tools.clinic import ClinicClient

CASE_ID = "case-0001"
EVENT = {"case_id": CASE_ID, "event_id": "evt-0001-referral"}

BOOKING = {
    "provider_id": dict(DESTINATIONS_A)["provider_id"],
    "patient_id": PATIENT_A,
    "slot_start": "2026-09-03T09:30:00",
    "reason": BOOKING_REASON,
}

#: Every way identity could plausibly escape, in one list, so a new field is checked by every
#: assertion below rather than by whichever one somebody remembers to extend.
IDENTITY_STRINGS = (
    CAREGIVER_A,
    TOKEN_A,
    "sub-alma",
    "Bearer",
    dict(DESTINATIONS_A)["caregiver_email"],
)


def clinic_client(base_url: str) -> ClinicClient:
    return ClinicClient(base_url)


def production_app(store, clinic, directory=None):
    """A production app whose scripted factory binds the same directory the boundary holds.

    Both halves, exactly as a composition root does it: the boundary authenticates the caller and
    the factory binds the run's patient authority. A factory that bound neither would fall back to
    the demo authority and refuse Alma's case, correctly and for the wrong reason.
    """
    held = directory if directory is not None else two_caregiver_directory()

    def build(store_, inbound, case_id):
        return context_for(
            store_,
            inbound,
            case_id=case_id,
            triage=_triage(),
            planner=_planner(),
            now=lambda: FIXED_NOW,
            clinic=clinic,
            **patient_authority_for(held, store_, case_id),
        )

    return create_app(
        store,
        context_factory=build,
        clinic=clinic,
        auth=production_boundary(held),
    )


def _triage():
    from api_helpers import triage_agent

    return triage_agent()


def _planner():
    from api_helpers import planner_proposing

    return planner_proposing(intent("book_appointment", **BOOKING))


# =================================================================================================
# P9 -- an authorised caregiver's real effects are unchanged
# =================================================================================================


class TestTheRealBookingStillHappensForAnAuthorisedCaregiver:
    def test_it_reaches_the_clinic_and_verifies_matched(self) -> None:
        from clinic_helpers import clinic_server

        store = InMemoryCaseStore()
        with clinic_server() as (base_url, world):
            with TestClient(production_app(store, clinic_client(base_url))) as client:
                answered = client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

            assert answered.status_code == 202
            case = store.get(CASE_ID)
            assert case.state is CaseState.DONE
            assert len(case.actions) == 1
            assert len(world.appointments()) == 1

    def test_the_read_back_observed_the_clinics_own_record(self) -> None:
        from clinic_helpers import clinic_server

        store = InMemoryCaseStore()
        with (
            clinic_server() as (base_url, _world),
            TestClient(production_app(store, clinic_client(base_url))) as client,
        ):
            client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

        observations = [
            entry.payload.get("observation")
            for entry in store.get(CASE_ID).timeline
            if "observation" in entry.payload
        ]
        assert "MATCHED" in observations


# =================================================================================================
# P10 -- an unauthorised caller produces zero external effects
# =================================================================================================


class TestAnUnauthorisedCallerReachesNoProvider:
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer forged"}, {"X-Caregiver": CAREGIVER_A}],
        ids=["no-token", "forged-token", "spoofed-header"],
    )
    def test_the_clinic_holds_nothing(self, headers: dict[str, str]) -> None:
        from clinic_helpers import clinic_server

        store = InMemoryCaseStore()
        with clinic_server() as (base_url, world):
            with TestClient(production_app(store, clinic_client(base_url))) as client:
                answered = client.post("/api/events?wait=10", headers=headers, json=EVENT)

            assert answered.status_code == 401
            assert world.appointments() == ()

    def test_no_case_and_no_action_record_exists(self) -> None:
        from clinic_helpers import clinic_server

        from careloop.state.store import CaseNotFound

        store = InMemoryCaseStore()
        with (
            clinic_server() as (base_url, _world),
            TestClient(production_app(store, clinic_client(base_url))) as client,
        ):
            client.post("/api/events?wait=10", json=EVENT)

        with pytest.raises(CaseNotFound):
            store.get(CASE_ID)

    def test_a_caller_without_a_grant_reaches_no_provider_either(self) -> None:
        """Authenticated perfectly well and authorised for nobody. Still zero effects."""
        from clinic_helpers import clinic_server

        directory = two_caregiver_directory()
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, world):
            with TestClient(production_app(store, clinic_client(base_url), directory)) as client:
                # Bo is granted PT-8823 and this case is about PT-4417, but the refusal here is the
                # coarser one: a delivery is refused before a case exists, when no patient is known.
                directory.revoke_grant("cg-bo", "PT-8823")
                answered = client.post("/api/events?wait=10", headers=bearer(TOKEN_B), json=EVENT)

            assert answered.status_code == 403
            assert world.appointments() == ()


# =================================================================================================
# P12 -- identity does not leak
# =================================================================================================


class TestIdentityDoesNotReachTheProvider:
    def test_the_clinics_own_record_carries_no_identity(self) -> None:
        """Asserted on what the third party ended up holding, not on what CareLoop sent."""
        from clinic_helpers import clinic_server

        store = InMemoryCaseStore()
        with clinic_server() as (base_url, world):
            with TestClient(production_app(store, clinic_client(base_url))) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

            held = repr(world.appointments())

        for secret in IDENTITY_STRINGS:
            assert secret not in held, f"the clinic is holding {secret!r}"

    def test_the_booking_carries_only_its_four_declared_arguments(self) -> None:
        from careloop.tools.registry import REGISTRY

        assert set(REGISTRY.get("book_appointment").arg_names) == {
            "provider_id",
            "patient_id",
            "slot_start",
            "reason",
        }

    def test_no_tool_declares_an_identity_argument(self) -> None:
        from careloop.tools.registry import REGISTRY

        every = {
            name for tool in REGISTRY.external_effect_tools for name in REGISTRY.get(tool).arg_names
        }
        # ``subject`` is deliberately absent from this list: ``send_email.subject`` is a domain
        # field with nothing to do with an IdP, and forbidding the word would be a check that
        # catches the wrong thing and gets deleted.
        assert not (
            every
            & {"caregiver_id", "caregiver", "principal", "auth_method", "access_token", "actor"}
        )


class TestIdentityDoesNotReachTelemetry:
    def test_the_frozen_attributes_are_still_exactly_nine(self) -> None:
        import scripts.witness_span as adapter

        attributes = {
            value
            for name, value in vars(adapter).items()
            if name.startswith("ATTR_") and isinstance(value, str)
        }
        assert len(attributes) == 9

    def test_none_of_them_is_about_who_asked(self) -> None:
        import scripts.witness_span as adapter

        attributes = {
            value
            for name, value in vars(adapter).items()
            if name.startswith("ATTR_") and isinstance(value, str)
        }
        for attribute in attributes:
            assert attribute.startswith(
                ("careloop.effect", "careloop.observation", "careloop.settle")
            )

    def test_the_witness_seam_names_no_identity(self) -> None:
        """Over the *names* the module uses, not its prose.

        A docstring that explains why a caregiver's re-check emits a particular claim is exactly
        the kind of writing this repository wants; a substring match over source would forbid it and
        be deleted the first time somebody documented something well.
        """
        import ast
        import inspect

        from careloop.graph import witness

        used = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(ast.parse(inspect.getsource(witness)))
            if isinstance(node, ast.Name | ast.Attribute)
        }
        assert not (used & {"caregiver_id", "Principal", "AuthMethod", "token", "subject"})


class TestIdentityDoesNotReachTheLogs:
    def test_a_session_id_is_never_logged_whole(self, caplog) -> None:
        from careloop.api.sessions import named

        whole = "vJ8Qk2LmNpRsTuVwXyZa0b"
        assert named(whole) == "vJ8Qk2Lm..."
        assert whole not in named(whole)

    def test_a_short_id_is_left_alone(self) -> None:
        """Nothing to redact, and inventing an ellipsis would make two lines look different."""
        from careloop.api.sessions import named

        assert named("short") == "short"

    def test_the_eviction_path_logs_only_a_prefix(self, caplog) -> None:
        from careloop.api.deps import ApiDeps
        from careloop.api.sessions import SessionWorlds

        clock = [0.0]
        worlds = SessionWorlds(
            lambda _s: ApiDeps(store=InMemoryCaseStore()),
            idle_seconds=1.0,
            now=lambda: clock[0],
            end_world=_refusing,
        )
        session = worlds.mint()
        clock[0] = 100.0

        with caplog.at_level(logging.DEBUG, logger="careloop.api.sessions"):
            worlds.mint()

        assert session not in caplog.text

    def test_no_authentication_failure_logs_a_token(self, caplog) -> None:
        from oidc_helpers import token, verifier

        from careloop.identity.principal import AuthenticationFailed

        presented = token(audience="another-client")
        with (
            caplog.at_level(logging.DEBUG, logger="careloop.identity.oidc"),
            pytest.raises(AuthenticationFailed),
        ):
            verifier().verify(presented)

        assert presented not in caplog.text
        assert "sub-alma" not in caplog.text


def _refusing(session_id: str, world) -> None:
    from careloop.tools.clinic import ClinicUnavailable

    raise ClinicUnavailable("deliberate: nothing is listening")
