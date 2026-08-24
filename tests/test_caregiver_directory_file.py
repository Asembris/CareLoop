"""The directory production resolves a verified subject against, read from configuration.

The last code-side piece before AWS. Once a user pool exists, wiring it up is three environment
variables and this file -- no new code, which is what makes the AWS bootstrap a *configuration*
step rather than another patch.

**Every way of getting it wrong stops the process.** A missing file, unparseable JSON, a record
without its identifiers, and -- the one that would otherwise look like working configuration -- a
grant or a subject mapping naming a caregiver the file does not declare. That last one is a silent
no-op: it reads as configured access and is discovered by somebody being refused.

**Nothing in it is a credential.** Caregiver ids, their trusted destinations, their grants and the
opaque IdP subjects those map from. An address here is *configuration* -- where CareLoop may write
-- and a subject is an identifier the issuer already puts in every token. No token, no password, no
key, and the error messages deliberately quote the path rather than the contents.
"""

from __future__ import annotations

import json

import pytest

from careloop.identity.config import (
    AUDIENCE_VAR,
    AUTH_MODE_VAR,
    CAREGIVERS_FILE_VAR,
    ISSUER_VAR,
    PRODUCTION_MODE,
    IncompleteAuthConfiguration,
    MalformedDirectory,
    auth_from_environment,
    directory_from_environment,
    directory_from_file,
)

WHOLE = {
    "caregivers": [
        {
            "caregiver_id": "cg-rosa",
            "destinations": {
                "caregiver_email": "rosa@carer.example",
                "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
                "portal_id": "PORTAL-NORTHGATE-RECORDS",
            },
        },
        {"caregiver_id": "cg-idle", "active": False, "destinations": {}},
    ],
    "grants": [
        {"caregiver_id": "cg-rosa", "patient_id": "PT-4417"},
        {"caregiver_id": "cg-rosa", "patient_id": "PT-0001", "active": False},
    ],
    "subjects": [
        {"issuer": "https://issuer.example", "subject": "sub-rosa", "caregiver_id": "cg-rosa"}
    ],
}


def written(tmp_path, payload) -> str:
    path = tmp_path / "caregivers.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestAWellFormedFileLoads:
    def test_the_caregiver_carries_its_destinations(self, tmp_path) -> None:
        directory = directory_from_file(written(tmp_path, WHOLE))

        assert directory.caregiver("cg-rosa").destinations == (
            ("caregiver_email", "rosa@carer.example"),
            ("provider_id", "PROV-NORTHGATE-CARDIOLOGY"),
            ("portal_id", "PORTAL-NORTHGATE-RECORDS"),
        )

    def test_an_active_grant_answers(self, tmp_path) -> None:
        assert directory_from_file(written(tmp_path, WHOLE)).may_act_for("cg-rosa", "PT-4417")

    def test_an_inactive_grant_does_not(self, tmp_path) -> None:
        assert not directory_from_file(written(tmp_path, WHOLE)).may_act_for("cg-rosa", "PT-0001")

    def test_an_inactive_caregiver_may_act_for_nobody(self, tmp_path) -> None:
        assert not directory_from_file(written(tmp_path, WHOLE)).may_act_for("cg-idle", "PT-4417")

    def test_active_defaults_to_true(self, tmp_path) -> None:
        """The ordinary entry is a live one, and a file that had to say so everywhere is one where
        a missing ``false`` is invisible."""
        assert directory_from_file(written(tmp_path, WHOLE)).caregiver("cg-rosa").active

    def test_a_mapped_subject_resolves(self, tmp_path) -> None:
        directory = directory_from_file(written(tmp_path, WHOLE))

        assert (
            directory.caregiver_for_subject("https://issuer.example", "sub-rosa").caregiver_id
            == "cg-rosa"
        )

    def test_an_empty_directory_is_valid_and_authorises_nobody(self, tmp_path) -> None:
        directory = directory_from_file(written(tmp_path, {}))

        assert directory.caregivers == {}
        assert not directory.may_act_for("anyone", "PT-4417")


class TestEveryWayOfGettingItWrongStopsTheProcess:
    def test_a_missing_file(self, tmp_path) -> None:
        with pytest.raises(MalformedDirectory, match="does not exist"):
            directory_from_file(str(tmp_path / "absent.json"))

    def test_unparseable_json(self, tmp_path) -> None:
        path = tmp_path / "caregivers.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(MalformedDirectory, match="could not be read as JSON"):
            directory_from_file(str(path))

    def test_a_json_document_that_is_not_an_object(self, tmp_path) -> None:
        with pytest.raises(MalformedDirectory, match="not a JSON object"):
            directory_from_file(written(tmp_path, ["cg-rosa"]))

    def test_a_caregiver_without_an_id(self, tmp_path) -> None:
        with pytest.raises(MalformedDirectory, match="missing a required field"):
            directory_from_file(written(tmp_path, {"caregivers": [{"destinations": {}}]}))

    def test_a_grant_without_a_patient(self, tmp_path) -> None:
        payload = {"caregivers": WHOLE["caregivers"], "grants": [{"caregiver_id": "cg-rosa"}]}

        with pytest.raises(MalformedDirectory, match="missing a required field"):
            directory_from_file(written(tmp_path, payload))

    def test_a_grant_naming_a_caregiver_the_file_does_not_declare(self, tmp_path) -> None:
        """The one that otherwise reads as working configuration."""
        payload = {"caregivers": [], "grants": [{"caregiver_id": "cg-ghost", "patient_id": "PT-1"}]}

        with pytest.raises(MalformedDirectory, match="cg-ghost"):
            directory_from_file(written(tmp_path, payload))

    def test_a_subject_mapping_naming_a_caregiver_the_file_does_not_declare(self, tmp_path) -> None:
        payload = {
            "caregivers": [],
            "subjects": [{"issuer": "i", "subject": "s", "caregiver_id": "cg-ghost"}],
        }

        with pytest.raises(MalformedDirectory, match="cg-ghost"):
            directory_from_file(written(tmp_path, payload))

    def test_a_parse_failure_does_not_quote_the_file(self, tmp_path) -> None:
        """A parse error can quote the line it failed on, and that line is somebody's identifier."""
        path = tmp_path / "caregivers.json"
        path.write_text('{"caregivers": [{"caregiver_id": "cg-secret-person"} ', encoding="utf-8")

        with pytest.raises(MalformedDirectory) as raised:
            directory_from_file(str(path))

        assert "cg-secret-person" not in str(raised.value)


class TestTheEnvironmentReachesIt:
    def test_an_unset_variable_describes_no_directory(self) -> None:
        assert directory_from_environment({}) is None

    def test_a_set_variable_loads_the_file(self, tmp_path) -> None:
        found = directory_from_environment({CAREGIVERS_FILE_VAR: written(tmp_path, WHOLE)})

        assert found is not None
        assert found.caregiver("cg-rosa").caregiver_id == "cg-rosa"

    def test_production_reads_it_without_being_handed_one(self, tmp_path) -> None:
        built = auth_from_environment(
            environ={
                AUTH_MODE_VAR: PRODUCTION_MODE,
                ISSUER_VAR: "https://issuer.example",
                AUDIENCE_VAR: "client-abc",
                CAREGIVERS_FILE_VAR: written(tmp_path, WHOLE),
            }
        )

        assert built.is_production
        assert built.directory.caregiver("cg-rosa").caregiver_id == "cg-rosa"

    def test_production_without_one_still_refuses_to_start(self) -> None:
        with pytest.raises(IncompleteAuthConfiguration, match=CAREGIVERS_FILE_VAR):
            auth_from_environment(
                environ={
                    AUTH_MODE_VAR: PRODUCTION_MODE,
                    ISSUER_VAR: "https://issuer.example",
                    AUDIENCE_VAR: "client-abc",
                }
            )

    def test_demo_reads_no_file_at_all(self, tmp_path) -> None:
        """A leftover variable does not half-configure a demo."""
        built = auth_from_environment(environ={CAREGIVERS_FILE_VAR: str(tmp_path / "absent.json")})

        assert not built.is_production

    def test_a_directory_passed_in_wins_over_the_file(self, tmp_path) -> None:
        """So a test, or a future composition root, can supply one without a filesystem."""
        from identity_helpers import two_caregiver_directory

        supplied = two_caregiver_directory()
        built = auth_from_environment(
            directory=supplied,
            environ={
                AUTH_MODE_VAR: PRODUCTION_MODE,
                ISSUER_VAR: "https://issuer.example",
                AUDIENCE_VAR: "client-abc",
                CAREGIVERS_FILE_VAR: written(tmp_path, WHOLE),
            },
        )

        assert built.directory is supplied
