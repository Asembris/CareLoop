"""The identity boundary, before any route consumes it.

Patch 1 of the production identity work. It adds the seam and nothing else: no route reads a
principal yet, nothing is persisted, no graph or provider behaviour moves. What it must already
make structurally true is the property everything later leans on --

    a failed production verification can never resolve to a demo identity

-- and the two halves of that are here. **No fallback exists in the object**, because an
:class:`~careloop.api.auth.AuthBoundary` holds exactly one verifier and refuses at construction if
that verifier does not establish the mode's principal. And **no fallback exists in the codebase**,
because there is exactly one production writer of
:attr:`~careloop.identity.principal.AuthMethod.SYNTHETIC_DEMO`, which the sweep below pins the way
``tests/test_api_demo.py`` pins ``CaseStore.clear``'s one caller.

The second property is that **a production app cannot be built without something to verify with**.
That is a ``TypeError`` at the call site rather than a runtime check, because an app that reached a
request handler and then discovered it had nobody to verify with would already have served
anonymous production traffic.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import careloop
from careloop.api.app import create_app
from careloop.api.auth import (
    AuthBoundary,
    AuthMode,
    AuthModeMismatch,
    _bearer_token,
    demo_auth,
    production_auth,
)
from careloop.graph.trusted import (
    DEMO_CAREGIVER,
    DEMO_CAREGIVER_ID,
    DEMO_PATIENT_ID,
    demo_directory,
)
from careloop.identity.demo import DemoPrincipals
from careloop.identity.directory import (
    Caregiver,
    Grant,
    InMemoryCaregiverDirectory,
    SubjectMapping,
    UnknownCaregiver,
    UnknownSubject,
)
from careloop.identity.principal import AuthenticationFailed, AuthMethod, Principal
from careloop.state import InMemoryCaseStore

#: A verifier that establishes verified principals, for testing the *shape* of production without
#: dragging in a JWT library. Patch 7 supplies the real one; this proves the seam accepts it.
CAREGIVER_A = "cg-a"
CAREGIVER_B = "cg-b"


class StubOidc:
    """Establishes ``VERIFIED_OIDC`` principals from a token the test controls.

    Deliberately not a mock of the real verifier's internals: what is under test here is that the
    boundary accepts a verifier claiming that method and refuses one that does not.
    """

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self._tokens = tokens or {}

    @property
    def auth_method(self) -> AuthMethod:
        return AuthMethod.VERIFIED_OIDC

    def verify(self, credential: str | None) -> Principal:
        who = self._tokens.get(credential or "")
        if who is None:
            raise AuthenticationFailed("unknown token")
        return Principal(caregiver_id=who, auth_method=AuthMethod.VERIFIED_OIDC)


def production_directory() -> InMemoryCaregiverDirectory:
    return InMemoryCaregiverDirectory(
        caregivers={
            CAREGIVER_A: Caregiver(CAREGIVER_A, (("caregiver_email", "a@carer.example"),)),
            CAREGIVER_B: Caregiver(CAREGIVER_B, (("caregiver_email", "b@carer.example"),)),
        },
        grants=(Grant(CAREGIVER_A, "PT-1111"), Grant(CAREGIVER_B, "PT-2222")),
        subjects=(SubjectMapping("https://issuer.example", "sub-a", CAREGIVER_A),),
    )


class TestTheTwoModesCannotMeet:
    """The structural half of "no fallback": the object refuses to hold both stories."""

    def test_a_production_boundary_refuses_a_demo_verifier(self) -> None:
        with pytest.raises(AuthModeMismatch):
            production_auth(verifier=DemoPrincipals("anyone"), directory=production_directory())

    def test_a_demo_boundary_refuses_a_verifying_verifier(self) -> None:
        """The other direction, which matters as much.

        A ``DEMO`` app holding a real verifier would answer some requests and refuse others while
        the world it serves is synthetic -- an identity story that is neither of the two.
        """
        with pytest.raises(AuthModeMismatch):
            AuthBoundary(
                mode=AuthMode.DEMO, verifier=StubOidc(), directory=InMemoryCaregiverDirectory()
            )

    def test_every_mode_declares_what_its_verifier_must_establish(self) -> None:
        """Vacuity: a mode absent from the table is a ``KeyError``, not an unchecked third case."""
        from careloop.api.auth import REQUIRED_METHOD

        assert set(REQUIRED_METHOD) == set(AuthMode)

    def test_production_cannot_be_constructed_without_a_verifier(self) -> None:
        with pytest.raises(TypeError):
            production_auth(directory=production_directory())  # type: ignore[call-arg]

    def test_production_cannot_be_constructed_without_a_directory(self) -> None:
        with pytest.raises(TypeError):
            production_auth(verifier=StubOidc())  # type: ignore[call-arg]


class TestOnlyOneThingMintsASyntheticPrincipal:
    """The codebase half of "no fallback"."""

    def test_exactly_one_production_module_names_the_synthetic_method(self) -> None:
        named = sorted(
            path.relative_to(package_root()).as_posix()
            for path in sorted(package_root().rglob("*.py"))
            if "SYNTHETIC_DEMO" in names_and_constants(path.read_text(encoding="utf-8"))
        )

        assert named == ["api/auth.py", "identity/demo.py", "identity/principal.py"], (
            f"{named} name SYNTHETIC_DEMO. identity/principal.py declares the member, "
            f"identity/demo.py is the one place a synthetic principal is built, and api/auth.py "
            f"only requires it of a demo boundary; anything else is a second way for a synthetic "
            f"identity to come into existence"
        )

    def test_only_the_two_verifiers_construct_one(self) -> None:
        """The stricter half: naming the member is not the same as building a principal with it.

        Two writers, one per mode, and no third. A principal built anywhere else would be an
        identity nothing verified and nothing chose.
        """
        constructing = sorted(
            path.relative_to(package_root()).as_posix()
            for path in sorted(package_root().rglob("*.py"))
            if builds_a_principal(path.read_text(encoding="utf-8"))
        )

        assert constructing == ["identity/demo.py", "identity/oidc.py"], (
            f"{constructing} construct a Principal. Production principals come out of the OIDC "
            f"verifier and synthetic ones out of identity/demo.py; anywhere else is a third source"
        )

    def test_the_sweep_would_notice_a_new_one(self) -> None:
        """Vacuity: the matcher catches both spellings and a bare import."""
        assert "SYNTHETIC_DEMO" in names_and_constants("AuthMethod.SYNTHETIC_DEMO")
        assert "SYNTHETIC_DEMO" in names_and_constants("from x import SYNTHETIC_DEMO")
        assert builds_a_principal("Principal(caregiver_id='x', auth_method=y)")
        assert not builds_a_principal("principal(x)")


class TestTheDemoIsUnchanged:
    """Patch 1 must be invisible to the judge-facing demo."""

    def test_an_app_built_without_an_auth_argument_is_a_demo_app(self) -> None:
        app = create_app(InMemoryCaseStore())
        assert app.state.careloop_auth.mode is AuthMode.DEMO
        assert not app.state.careloop_auth.is_production

    def test_the_demo_boundary_mints_the_demo_caregiver_with_no_credential(self) -> None:
        who = demo_auth().principal(None)
        assert who == Principal(DEMO_CAREGIVER_ID, AuthMethod.SYNTHETIC_DEMO)
        assert not who.is_verified

    def test_a_demo_principal_ignores_whatever_credential_is_presented(self) -> None:
        """A demo that could be talked out of its identity would have a login screen."""
        bound = demo_auth()
        assert bound.principal("Bearer nonsense") == bound.principal(None)

    def test_every_existing_route_still_answers(self) -> None:
        """No route consumes a principal yet, so nothing about the surface may have moved."""
        with TestClient(create_app(InMemoryCaseStore())) as client:
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/cases").status_code == 200


class TestTheDemoDirectoryIsRealRatherThanABypass:
    """The demo runs the production authorisation code, on synthetic records."""

    def test_the_demo_caregiver_carries_exactly_the_trusted_configuration(self) -> None:
        from careloop.graph.trusted import TRUSTED_CONFIG

        assert DEMO_CAREGIVER.destinations == TRUSTED_CONFIG

    def test_the_demo_caregiver_holds_a_grant_for_the_fixture_patient(self) -> None:
        assert demo_directory().may_act_for(DEMO_CAREGIVER_ID, DEMO_PATIENT_ID)

    def test_the_demo_caregiver_holds_no_other_grant(self) -> None:
        assert not demo_directory().may_act_for(DEMO_CAREGIVER_ID, "PT-9999")

    def test_no_identity_provider_subject_maps_onto_the_demo_caregiver(self) -> None:
        """So a verified production token cannot resolve to the synthetic caregiver."""
        assert demo_directory().subjects == ()

    def test_each_call_gets_its_own_directory(self) -> None:
        """A revocation in one world must not be a revocation in the next."""
        first, second = demo_directory(), demo_directory()
        first.revoke_grant(DEMO_CAREGIVER_ID, DEMO_PATIENT_ID)

        assert not first.may_act_for(DEMO_CAREGIVER_ID, DEMO_PATIENT_ID)
        assert second.may_act_for(DEMO_CAREGIVER_ID, DEMO_PATIENT_ID)


class TestTheDirectoryFailsClosed:
    """Every way of not knowing answers the same safe way."""

    def test_an_unknown_caregiver_may_act_for_nobody(self) -> None:
        assert not production_directory().may_act_for("cg-nobody", "PT-1111")

    def test_a_deactivated_caregiver_may_act_for_nobody(self) -> None:
        directory = production_directory()
        directory.deactivate(CAREGIVER_A)
        assert not directory.may_act_for(CAREGIVER_A, "PT-1111")

    def test_a_revoked_grant_stops_answering(self) -> None:
        directory = production_directory()
        directory.revoke_grant(CAREGIVER_A, "PT-1111")
        assert not directory.may_act_for(CAREGIVER_A, "PT-1111")

    def test_a_revoked_grant_keeps_its_record(self) -> None:
        """Revocation is a state change, not a deletion -- see :class:`Grant`."""
        directory = production_directory()
        directory.revoke_grant(CAREGIVER_A, "PT-1111")
        assert any(
            grant.caregiver_id == CAREGIVER_A and grant.patient_id == "PT-1111" and not grant.active
            for grant in directory.grants
        )

    def test_one_caregivers_grant_is_not_anothers(self) -> None:
        directory = production_directory()
        assert directory.may_act_for(CAREGIVER_A, "PT-1111")
        assert not directory.may_act_for(CAREGIVER_B, "PT-1111")

    def test_an_unknown_caregiver_id_raises_rather_than_answering_none(self) -> None:
        with pytest.raises(UnknownCaregiver):
            production_directory().caregiver("cg-nobody")

    def test_a_subject_this_directory_does_not_map_raises(self) -> None:
        with pytest.raises(UnknownSubject):
            production_directory().caregiver_for_subject("https://issuer.example", "sub-unknown")

    def test_a_subject_from_another_issuer_does_not_match(self) -> None:
        """Matching on the subject alone would let one issuer name another's people."""
        with pytest.raises(UnknownSubject):
            production_directory().caregiver_for_subject("https://elsewhere.example", "sub-a")

    def test_a_mapped_subject_resolves_to_its_caregiver(self) -> None:
        found = production_directory().caregiver_for_subject("https://issuer.example", "sub-a")
        assert found.caregiver_id == CAREGIVER_A

    def test_the_unmapped_subject_message_does_not_carry_the_subject(self) -> None:
        """It reaches a server log, and an IdP subject has no business in one."""
        with pytest.raises(UnknownSubject) as raised:
            production_directory().caregiver_for_subject("https://issuer.example", "sub-secret")
        assert "sub-secret" not in str(raised.value)


class TestProductionRefusesRatherThanSynthesises:
    """A production boundary answers with a verified principal or with nothing."""

    def test_a_verified_token_establishes_its_caregiver(self) -> None:
        bound = production_auth(
            verifier=StubOidc({"good": CAREGIVER_A}), directory=production_directory()
        )
        assert bound.principal("Bearer good") == Principal(CAREGIVER_A, AuthMethod.VERIFIED_OIDC)

    @pytest.mark.parametrize(
        "credential",
        [None, "", "good", "Basic good", "Bearer", "Bearer ", "Bearer wrong"],
        ids=["absent", "empty", "no-scheme", "wrong-scheme", "no-token", "blank-token", "unknown"],
    )
    def test_every_bad_credential_raises_rather_than_falling_back(self, credential: str) -> None:
        bound = production_auth(
            verifier=StubOidc({"good": CAREGIVER_A}), directory=production_directory()
        )
        with pytest.raises(AuthenticationFailed):
            bound.principal(credential)

    def test_the_scheme_is_matched_case_insensitively(self) -> None:
        assert _bearer_token("bearer t") == "t"
        assert _bearer_token("BEARER t") == "t"
        assert _bearer_token("BeArEr t") == "t"

    def test_the_token_itself_is_taken_verbatim(self) -> None:
        """Not normalised. Trimming inside a token is how a different credential gets accepted."""
        assert _bearer_token("Bearer a.b-c_d") == "a.b-c_d"


class TestTheProductionSeamReachesTheApp:
    """A production app is buildable and holds what it was given. Routes come in patch 3."""

    def test_an_app_built_with_a_production_boundary_reports_production(self) -> None:
        app = create_app(
            InMemoryCaseStore(),
            auth=production_auth(verifier=StubOidc(), directory=production_directory()),
        )
        assert app.state.careloop_auth.is_production

    def test_the_boundary_dependency_resolves_the_apps_own_boundary(self) -> None:
        bound = production_auth(verifier=StubOidc(), directory=production_directory())
        app = create_app(InMemoryCaseStore(), auth=bound)

        from careloop.api.auth import boundary

        assert boundary(SimpleNamespace(app=app)) is bound  # type: ignore[arg-type]


# -- the sweep's mechanics -------------------------------------------------------------------------


def package_root() -> Path:
    return Path(careloop.__file__).parent


def names_and_constants(source: str) -> set[str]:
    """Every name, attribute and imported alias this module mentions.

    All three forms, so ``AuthMethod.SYNTHETIC_DEMO``, a bare ``SYNTHETIC_DEMO`` and an import
    without a use are all caught -- a module that imports it has already decided it might.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
    return found


def builds_a_principal(source: str) -> bool:
    """Whether this module calls ``Principal(...)``.

    Narrower than naming the class, because a type annotation is not a construction. Matched on the
    callee so ``principal.Principal(...)`` counts and the lowercase dependency function does not.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        callee = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if callee == "Principal":
            return True
    return False
