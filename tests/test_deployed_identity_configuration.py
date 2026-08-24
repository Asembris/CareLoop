"""The identity configuration this repository actually deploys, checked offline.

**Configuration is the half a test suite normally cannot see.** Every other identity test in this
repository builds a directory in Python and proves a property about it. None of them says anything
about the two files that decide who the *deployed* CareLoop authenticates:
``deploy/careloop.env`` and ``deploy/caregivers.json``. A subject typed with one character wrong, a
grant naming a patient no fixture is about, or a provider Demo World does not offer slots for are
all green suites and a deployment where nothing works — and the way each of them is discovered is
somebody signing in and being refused, which is the slowest feedback loop this project has.

**What is asserted is a coupling, never a copy.** The granted patient is checked against the
identifier the fixture documents state, the trusted provider against the providers Demo World seeds
slots for, and the configured caregivers file against the path the environment names. A test that
restated the literals would pass through any edit that changed both places, which is exactly the
edit worth catching.

**None of these values is a secret and this file says so by handling them plainly.** An issuer and a
public client id are published in every token the deployment will ever see; a subject is an opaque
identifier the IdP already puts in each one; an address in the directory is *configuration* — the
address CareLoop may write to — rather than a credential. There is no token, password or key here,
and :class:`TestTheDirectoryHoldsNothingThatCouldBeACredential` is what fails if a future field
would be one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from careloop.api.auth import AuthMode
from careloop.identity.config import (
    AUDIENCE_VAR,
    AUTH_MODE_VAR,
    CAREGIVERS_FILE_VAR,
    ISSUER_VAR,
    LOGIN_DOMAIN_VAR,
    PRODUCTION_MODE,
    auth_from_environment,
    browser_login,
    directory_from_file,
)
from careloop.identity.directory import UnknownSubject
from careloop.identity.oidc import ACCESS_TOKEN

#: The committed deployment environment, read the way ``EnvironmentFile`` reads it.
ENV_FILE: Final[Path] = Path("deploy/careloop.env")

#: The committed caregiver directory the deployed process resolves subjects against.
DIRECTORY_FILE: Final[Path] = Path("deploy/caregivers.json")


def deployed_environment() -> dict[str, str]:
    """``deploy/careloop.env`` as a mapping, comments and blanks dropped."""
    return {
        line.partition("=")[0].strip(): line.partition("=")[2].strip()
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


@pytest.fixture(name="environ")
def environ_fixture() -> dict[str, str]:
    return deployed_environment()


@pytest.fixture(name="production")
def production_fixture(environ: dict[str, str]) -> bool:
    """Whether the shipped file asks for production *right now*.

    Exactly one assertion is conditional on this — completeness — because that is the only question
    the mode changes. The file is allowed to be switched back to demo: the public distribution
    serves the judge demo, and a suite that failed on that would be pushing back on the deployment
    rather than describing it.
    """
    return environ.get(AUTH_MODE_VAR) == PRODUCTION_MODE


@pytest.fixture(name="configured")
def configured_fixture(environ: dict[str, str]) -> bool:
    """Whether the production identity values are present, whatever mode is selected.

    **This, and not the mode, is what the rest of the file is conditional on.** The values stay
    wired while the deployment runs as a demo, so that restoring production is one word rather than
    a re-bootstrap — and a capability nothing checks is a capability that rots. Conditioning these
    assertions on the mode would have meant that flipping one line silently stopped testing the
    issuer, the audience, the hosted UI, the caregivers file and the boundary they build, which is
    precisely the state somebody would restore production from and discover was wrong.
    """
    return bool(environ.get(ISSUER_VAR) and environ.get(AUDIENCE_VAR))


class TestTheDeployedEnvironmentIsComplete:
    def test_a_production_deployment_is_complete(
        self, environ: dict[str, str], production: bool
    ) -> None:
        """The one mode-conditional assertion. Incompleteness here is the unit refusing to start."""
        if not production:
            pytest.skip("the shipped file serves the demo; completeness is not its question")
        for required in (ISSUER_VAR, AUDIENCE_VAR, CAREGIVERS_FILE_VAR):
            assert environ.get(required), f"{required} is empty in a production careloop.env"

    def test_the_configured_issuer_and_audience_are_well_formed(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        assert environ[ISSUER_VAR].startswith("https://")
        assert environ[AUDIENCE_VAR]
        assert environ[AUDIENCE_VAR] == environ[AUDIENCE_VAR].strip()

    def test_a_configured_deployment_names_a_caregivers_file(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        assert environ[CAREGIVERS_FILE_VAR]

    def test_the_file_it_names_is_the_one_this_repository_ships(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        """The deployed absolute path and the committed relative path must be the same file.

        The tree is extracted to ``/opt/careloop/app``, so the configured path is that prefix plus
        this repository's own path. Asserted as a suffix rather than as a literal, because the
        install prefix is the deploy script's business and the *file* is this test's.
        """
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        configured = environ[CAREGIVERS_FILE_VAR].replace("\\", "/")
        assert configured.endswith("/" + DIRECTORY_FILE.as_posix())
        assert DIRECTORY_FILE.exists()

    def test_the_login_domain_is_a_hosted_ui_origin_and_nothing_more(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        """Origin only. A path here would produce authorize endpoints that 404."""
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        domain = environ[LOGIN_DOMAIN_VAR]
        if not domain:
            pytest.skip("this deployment offers no browser login, which is a legitimate shape")
        assert domain.startswith("https://")
        assert domain.count("/") == 2, f"{domain!r} carries a path"

    def test_the_published_login_is_derived_from_that_one_variable(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        if not configured or not environ.get(LOGIN_DOMAIN_VAR):
            pytest.skip("this deployment publishes no login")
        published = browser_login(environ)
        assert published["authorization_endpoint"].startswith(environ[LOGIN_DOMAIN_VAR])
        assert published["token_endpoint"].startswith(environ[LOGIN_DOMAIN_VAR])
        assert published["client_id"] == environ[AUDIENCE_VAR]
        assert published["scopes"] == "openid"


class TestTheProofSubjectResolvesToExactlyOneCaregiver:
    def test_the_directory_loads(self) -> None:
        assert directory_from_file(str(DIRECTORY_FILE)) is not None

    def test_every_declared_subject_resolves(self) -> None:
        """A mapping that resolves to nobody is configuration that reads as working and is not."""
        directory = directory_from_file(str(DIRECTORY_FILE))
        declared = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["subjects"]
        assert declared, "a production directory with no subject mapping authenticates nobody"
        for mapping in declared:
            resolved = directory.caregiver_for_subject(mapping["issuer"], mapping["subject"])
            assert resolved.caregiver_id == mapping["caregiver_id"]
            assert resolved.active

    def test_no_two_subjects_resolve_to_the_same_caregiver(self) -> None:
        """One person, one caregiver record. Two would make the audit trail ambiguous."""
        declared = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["subjects"]
        assert len({mapping["caregiver_id"] for mapping in declared}) == len(declared)

    def test_the_issuer_is_part_of_the_identity(self) -> None:
        """The same subject from a different issuer is a different person and must not resolve."""
        directory = directory_from_file(str(DIRECTORY_FILE))
        declared = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["subjects"][0]
        with pytest.raises(UnknownSubject):
            directory.caregiver_for_subject(
                "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_notThisPool",
                declared["subject"],
            )

    def test_an_unmapped_subject_resolves_to_nobody(self) -> None:
        directory = directory_from_file(str(DIRECTORY_FILE))
        declared = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["subjects"][0]
        with pytest.raises(UnknownSubject):
            directory.caregiver_for_subject(
                declared["issuer"], "00000000-0000-0000-0000-000000000000"
            )

    def test_the_mapped_issuer_is_the_one_the_deployment_verifies_against(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        """Two files, one pool. A directory keyed on a pool the verifier does not accept resolves
        nobody, and every request is a 401 for a reason nothing on the surface explains."""
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        declared = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["subjects"]
        assert {mapping["issuer"] for mapping in declared} == {environ[ISSUER_VAR]}


class TestTheGrantsAreTheOnesTheProofNeeds:
    def test_every_granted_patient_is_one_the_fixtures_are_about(self) -> None:
        """The coupling, not a copy: a grant for somebody no document establishes could never be
        exercised, and a grant this repository cannot reach is a grant nobody chose."""
        from careloop.tools.document_source import all_lines

        lines = all_lines()
        for grant in json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["grants"]:
            assert any(f"Patient ID: {grant['patient_id']}" in line for line in lines), (
                f"nothing in the fixture documents establishes {grant['patient_id']}"
            )

    def test_the_caregiver_may_act_for_that_patient_and_for_nobody_else(self) -> None:
        directory = directory_from_file(str(DIRECTORY_FILE))
        raw = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))
        granted = {(grant["caregiver_id"], grant["patient_id"]) for grant in raw["grants"]}
        for caregiver in raw["caregivers"]:
            who = caregiver["caregiver_id"]
            assert directory.may_act_for_anybody(who)
            assert not directory.may_act_for(who, "PT-0000-NOT-GRANTED")
            for patient in {patient for _, patient in granted}:
                assert directory.may_act_for(who, patient) == ((who, patient) in granted)

    def test_no_grant_is_declared_inactive_and_left_in_place(self) -> None:
        """An inactive grant in a deployed file is a mistake or a deletion nobody finished."""
        for grant in json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["grants"]:
            assert grant.get("active", True) is True


class TestTheTrustedDestinationsAreReachable:
    def test_the_trusted_provider_is_one_demo_world_offers_slots_for(self) -> None:
        """The booking proof's coupling. A provider the clinic has never heard of is a trusted
        destination no appointment can be made at, and the failure surfaces as a refused booking
        rather than as anything naming configuration."""
        from demoworld.clinic import SEEDED_SLOTS

        offered = {slot.provider_id for slot in SEEDED_SLOTS}
        for caregiver in json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["caregivers"]:
            provider = caregiver["destinations"].get("provider_id")
            assert provider in offered, f"{provider!r} is not a provider Demo World offers"

    def test_every_caregiver_carries_the_three_destinations_the_tools_draw_on(self) -> None:
        """``send_email.to``, ``book_appointment.provider_id`` and ``upload_document.portal_id``
        each draw trust from one of these keys, so a caregiver missing one has that tool denied."""
        from careloop.tools.registry import REGISTRY

        needed = {
            key
            for tool in REGISTRY
            for key in tool.trust_keys
            if key in {"caregiver_email", "provider_id", "portal_id"}
        }
        for caregiver in json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["caregivers"]:
            assert needed <= set(caregiver["destinations"])

    def test_the_trusted_address_is_not_a_deliverable_mailbox(self) -> None:
        """``.example`` is reserved by RFC 2606 and routes nowhere. A real address here would make
        a synthetic proof send mail to a person."""
        for caregiver in json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))["caregivers"]:
            assert caregiver["destinations"]["caregiver_email"].endswith(".example")

    def test_the_seeded_facts_are_trusted_and_come_from_configuration(self) -> None:
        from careloop.graph.trusted import CONFIG_SOURCE, trusted_facts
        from careloop.provenance.trust import TrustLevel

        directory = directory_from_file(str(DIRECTORY_FILE))
        raw = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))
        for entry in raw["caregivers"]:
            facts = trusted_facts(directory.caregiver(entry["caregiver_id"]))
            assert {fact.key for fact in facts} == set(entry["destinations"])
            assert all(fact.trust_level is TrustLevel.TRUSTED for fact in facts)
            assert all(fact.provenance.source_doc == CONFIG_SOURCE for fact in facts)


class TestTheDemoCaregiverCannotBeReachedByAToken:
    def test_the_deployed_directory_does_not_declare_the_demo_caregiver(self) -> None:
        """The two modes must not meet in the middle even in one process."""
        from careloop.graph.trusted import DEMO_CAREGIVER_ID

        raw = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))
        assert DEMO_CAREGIVER_ID not in {entry["caregiver_id"] for entry in raw["caregivers"]}


class TestTheDirectoryHoldsNothingThatCouldBeACredential:
    @pytest.mark.parametrize(
        "forbidden", ["password", "secret", "client_secret", "token", "private_key", "api_key"]
    )
    def test_no_field_anywhere_is_named_like_one(self, forbidden: str) -> None:
        text = DIRECTORY_FILE.read_text(encoding="utf-8").lower()
        assert f'"{forbidden}"' not in text


class TestTheBoundaryThisEnvironmentBuilds:
    """Built from the configured values with the mode forced, so the *capability* stays checked.

    The deployed mode is the demo's business and is asserted where it belongs. What must not stop
    being true while the demo is being served is that these values still compose a verifier that
    accepts access tokens from the configured pool and nothing else -- which is the whole of what
    "restoring production is one word" means.
    """

    def test_it_is_a_production_boundary_over_the_committed_directory(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        built = auth_from_environment(
            directory=directory_from_file(str(DIRECTORY_FILE)),
            environ={**environ, AUTH_MODE_VAR: PRODUCTION_MODE},
        )

        assert built.mode is AuthMode.PRODUCTION
        assert built.verifier.issuer == environ[ISSUER_VAR]
        assert built.verifier.audience == environ[AUDIENCE_VAR]

    def test_it_verifies_access_tokens_and_not_id_tokens(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        """The composition Patch A pinned, asserted over the values that are really deployed."""
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        built = auth_from_environment(
            directory=directory_from_file(str(DIRECTORY_FILE)),
            environ={**environ, AUTH_MODE_VAR: PRODUCTION_MODE},
        )

        assert built.verifier.token_use == ACCESS_TOKEN

    def test_building_it_reaches_no_network(
        self, environ: dict[str, str], configured: bool
    ) -> None:
        if not configured:
            pytest.skip("this deployment carries no production identity configuration")
        built = auth_from_environment(
            directory=directory_from_file(str(DIRECTORY_FILE)),
            environ={**environ, AUTH_MODE_VAR: PRODUCTION_MODE},
        )

        assert built.verifier.keys._client is None
