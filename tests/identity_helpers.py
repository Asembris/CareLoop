"""Building a production-mode CareLoop for tests, without AWS and without a JWT library.

The verifier here is a stand-in for the shape of one, not for its cryptography:
``tests/test_oidc_verifier.py`` exercises the real claim checking against a locally minted key. What
these helpers are for is everything *above* the verifier -- workspace ownership, case ownership,
decision actors and grants -- which needs a principal to exist and does not care how it was
established.

Kept out of ``conftest.py`` for the reason ``helpers.py`` and ``api_helpers.py`` are: test modules
import this, and pytest loads ``conftest.py`` rather than exporting it.
"""

from __future__ import annotations

from collections.abc import Iterable

from api_helpers import factory_proposing
from fastapi.testclient import TestClient

from careloop.api.app import create_app
from careloop.api.deps import ApiDeps
from careloop.api.sessions import SessionWorlds
from careloop.identity.directory import (
    Caregiver,
    Grant,
    InMemoryCaregiverDirectory,
    SubjectMapping,
)
from careloop.identity.principal import AuthenticationFailed, AuthMethod, Principal
from careloop.state import InMemoryCaseStore

ISSUER = "https://issuer.example/pool"

#: Two caregivers with disjoint destinations and disjoint patients, so every cross-owner assertion
#: is about ownership rather than about two names for one thing.
CAREGIVER_A = "cg-alma"
CAREGIVER_B = "cg-bo"

PATIENT_A = "PT-4417"
PATIENT_B = "PT-8823"

DESTINATIONS_A = (
    ("caregiver_email", "alma@carer.example"),
    ("provider_id", "PROV-NORTHGATE-CARDIOLOGY"),
    ("portal_id", "PORTAL-NORTHGATE-RECORDS"),
)
DESTINATIONS_B = (
    ("caregiver_email", "bo@carer.example"),
    ("provider_id", "PROV-SOUTHBANK-RESPIRATORY"),
    ("portal_id", "PORTAL-SOUTHBANK-RECORDS"),
)

#: An outbound email Alma's own configured address may receive.
#:
#: Spelled from :data:`DESTINATIONS_A` rather than repeated, because after per-caregiver trusted
#: configuration an email to anybody else is correctly *denied* -- which is the point of that patch
#: and would otherwise look like a broken fixture.
EMAIL_TO_A = {
    "to": dict(DESTINATIONS_A)["caregiver_email"],
    "subject": "Cardiology referral for Enid Marlowe",
    "body": "The referral has arrived and an appointment is being arranged.",
}

TOKEN_A = "token-alma"
TOKEN_B = "token-bo"


class StubVerifier:
    """Establishes ``VERIFIED_OIDC`` principals from a fixed token table.

    Deliberately dumb. It asserts nothing about signatures, and a test that wanted to prove
    something about them would be testing this class rather than CareLoop.
    """

    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = dict(tokens)

    @property
    def auth_method(self) -> AuthMethod:
        return AuthMethod.VERIFIED_OIDC

    def verify(self, credential: str | None) -> Principal:
        who = self.tokens.get(credential or "")
        if who is None:
            raise AuthenticationFailed("this stub does not know that token")
        return Principal(caregiver_id=who, auth_method=AuthMethod.VERIFIED_OIDC)


def two_caregiver_directory() -> InMemoryCaregiverDirectory:
    """Alma and Bo, each granted their own patient and neither granted the other's."""
    return InMemoryCaregiverDirectory(
        caregivers={
            CAREGIVER_A: Caregiver(CAREGIVER_A, DESTINATIONS_A),
            CAREGIVER_B: Caregiver(CAREGIVER_B, DESTINATIONS_B),
        },
        grants=(Grant(CAREGIVER_A, PATIENT_A), Grant(CAREGIVER_B, PATIENT_B)),
        subjects=(
            SubjectMapping(ISSUER, "sub-alma", CAREGIVER_A),
            SubjectMapping(ISSUER, "sub-bo", CAREGIVER_B),
        ),
    )


def stub_verifier() -> StubVerifier:
    return StubVerifier({TOKEN_A: CAREGIVER_A, TOKEN_B: CAREGIVER_B})


def production_boundary(directory: InMemoryCaregiverDirectory | None = None):
    from careloop.api.auth import production_auth

    return production_auth(
        verifier=stub_verifier(), directory=directory or two_caregiver_directory()
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def production_client(
    store=None,
    *proposals,
    directory: InMemoryCaregiverDirectory | None = None,
    demo: bool = False,
) -> TestClient:
    """A single-world production app, driven by scripted agents on the mock model.

    The scripted factory is handed the same directory the boundary holds, which is what a production
    composition root does -- without it the run would fall back to the demo patient authority and
    refuse every case owned by Alma or Bo, correctly and unhelpfully.
    """
    held = directory if directory is not None else two_caregiver_directory()
    return TestClient(
        create_app(
            store if store is not None else InMemoryCaseStore(),
            context_factory=factory_proposing(*proposals, directory=held) if proposals else None,
            auth=production_boundary(held),
            demo=demo,
        )
    )


def isolated_production_client(
    *proposals: object,
    directory: InMemoryCaregiverDirectory | None = None,
    worlds: dict[str, ApiDeps] | None = None,
) -> tuple[TestClient, SessionWorlds]:
    """A world-per-visitor production app, so workspace ownership can be exercised."""
    kept = worlds if worlds is not None else {}
    held = directory if directory is not None else two_caregiver_directory()

    def build(session_id: str) -> ApiDeps:
        world = ApiDeps(
            store=InMemoryCaseStore(),
            context_factory=factory_proposing(*proposals, directory=held) if proposals else None,
        )
        kept[session_id] = world
        return world

    registry = SessionWorlds(build, end_world=lambda *_: None)
    app = create_app(sessions=registry, auth=production_boundary(held))
    return TestClient(app), registry


def mint(client: TestClient, token: str) -> str:
    """Be given a workspace as ``token``'s caregiver."""
    response = client.post("/api/sessions", headers=bearer(token))
    response.raise_for_status()
    return response.json()["session"]


def workspace(session: str, token: str) -> dict[str, str]:
    return {"X-CareLoop-Session": session, **bearer(token)}


def timeline_kinds(case) -> list[str]:
    return [str(entry.kind) for entry in case.timeline]


def all_cases(store) -> Iterable:
    from careloop.state import CaseState

    for state in CaseState:
        yield from store.list_by_state(state)
