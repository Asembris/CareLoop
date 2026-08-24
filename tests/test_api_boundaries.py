"""``GET /api/boundaries`` is a read surface, scoped to the caller, that says nothing about them.

The derivation is proven against the real engine in ``tests/test_boundaries.py``. What is asked here
is everything the HTTP layer adds and that proof cannot cover: that the endpoint is reachable where
every other read route is, that two production callers are told about their *own* configuration and
never each other's, and that a response carrying a caregiver's boundary carries no caregiver.

**Nothing here re-derives a standing.** A test that recomputed ``AUTONOMOUS`` from the rules and
compared it to the response would be a third implementation of the same question -- the shape this
whole feature exists to remove from the frontend. The assertions are about *routing, scoping and
withholding*; what the values mean is the other file's job.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from identity_helpers import (
    CAREGIVER_A,
    DESTINATIONS_A,
    DESTINATIONS_B,
    TOKEN_A,
    TOKEN_B,
    bearer,
    isolated_production_client,
    mint,
    production_client,
    workspace,
)

from careloop.api.app import create_app
from careloop.graph.trusted import DEMO_CAREGIVER
from careloop.state import InMemoryCaseStore

BOUNDARIES = "/api/boundaries"


def demo_client() -> TestClient:
    return TestClient(create_app(InMemoryCaseStore()))


def statement(client: TestClient, **kwargs: Any) -> dict[str, Any]:
    response = client.get(BOUNDARIES, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def configured_values(body: dict[str, Any]) -> set[str]:
    return {
        argument["configured_value"]
        for capability in body["capabilities"]
        for argument in capability["arguments"]
        if argument["configured_value"] is not None
    }


def strings_in(value: Any) -> list[str]:
    """Every string anywhere in a decoded JSON body, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, held in value.items() for text in (key, *strings_in(held))]
    if isinstance(value, list):
        return [text for held in value for text in strings_in(held)]
    return []


class TestItIsAnOrdinaryReadRoute:
    """Reachable where the other reads are, and refused where they are refused."""

    def test_the_demo_world_answers_it(self) -> None:
        body = statement(demo_client())
        assert body["capabilities"]
        assert body["refusals"]

    def test_it_describes_every_external_effect_and_only_those(self) -> None:
        from careloop.tools import REGISTRY

        names = {capability["tool_name"] for capability in statement(demo_client())["capabilities"]}
        assert names == set(REGISTRY.external_effect_tools)
        assert "fetch_document" not in names

    def test_a_production_caller_without_a_token_is_refused(self) -> None:
        assert production_client().get(BOUNDARIES).status_code == 401

    def test_a_production_caller_with_a_token_is_answered(self) -> None:
        assert statement(production_client(), headers=bearer(TOKEN_A))["capabilities"]

    def test_an_isolated_app_needs_a_session_like_every_other_read(self) -> None:
        client, _ = isolated_production_client()
        assert client.get(BOUNDARIES, headers=bearer(TOKEN_A)).status_code == 400

        session = mint(client, TOKEN_A)
        assert statement(client, headers=workspace(session, TOKEN_A))["capabilities"]

    def test_a_forgotten_workspace_is_gone(self) -> None:
        client, _ = isolated_production_client()
        assert (
            client.get(BOUNDARIES, headers=workspace("sess-never-minted", TOKEN_A)).status_code
            == 410
        )

    def test_it_is_not_cached(self) -> None:
        """The same no-store treatment every API read gets.

        A boundary held in a browser cache would outlive the configuration it describes, which on
        this surface means showing a caregiver limits that are no longer theirs.
        """
        response = demo_client().get(BOUNDARIES)
        assert "no-store" in response.headers.get("cache-control", "")


class TestTheStatementBelongsToTheCaller:
    """Two production callers, two configurations, and no leakage between them."""

    def test_each_caller_is_told_about_their_own_destinations(self) -> None:
        client = production_client()
        alma = configured_values(statement(client, headers=bearer(TOKEN_A)))
        bo = configured_values(statement(client, headers=bearer(TOKEN_B)))

        assert alma == {value for _, value in DESTINATIONS_A}
        assert bo == {value for _, value in DESTINATIONS_B}
        assert not alma & bo

    def test_one_callers_address_never_appears_in_the_others_statement(self) -> None:
        """The sharp form: ``send_email.to`` is where a shared destination would do real harm.

        Session X made trusted configuration per caregiver precisely because a global tuple would
        have let one caregiver's case email another's address. A boundary page that reported the
        global tuple would be describing that defect back into existence.
        """
        client = production_client()
        bos_address = dict(DESTINATIONS_B)["caregiver_email"]
        almas = statement(client, headers=bearer(TOKEN_A))
        assert bos_address not in strings_in(almas)

    def test_the_demo_statement_is_the_demo_caregivers(self) -> None:
        assert configured_values(statement(demo_client())) == {
            value for _, value in DEMO_CAREGIVER.destinations
        }


class TestItSaysNothingAboutWhoIsAsking:
    """A boundary describes what may be done, never for whom, and never by whom."""

    def test_no_caregiver_identity_reaches_the_response(self) -> None:
        """Substring rather than equality, so an identity embedded in a sentence still trips it."""
        text = strings_in(statement(production_client(), headers=bearer(TOKEN_A)))
        assert not [held for held in text if CAREGIVER_A in held or TOKEN_A in held]

    def test_no_patient_or_grant_is_enumerated(self) -> None:
        """Patient authority is asked, never listed -- see careloop/identity/directory.py."""
        from identity_helpers import PATIENT_A, PATIENT_B

        text = strings_in(statement(production_client(), headers=bearer(TOKEN_A)))
        assert PATIENT_A not in text
        assert PATIENT_B not in text

    def test_no_session_credential_reaches_the_response(self) -> None:
        client, _ = isolated_production_client()
        session = mint(client, TOKEN_A)
        text = strings_in(statement(client, headers=workspace(session, TOKEN_A)))
        assert session not in text

    def test_no_rule_diagnostic_reaches_the_response(self) -> None:
        """Rule ids and reason codes are a *case's* audit trail, not a standing statement.

        The per-case surfaces already carry ``matched_rule_id`` and ``reason_code`` deliberately.
        This one describes configuration, and echoing a rule's free-text diagnostic here would put
        unbounded prose on a response for no question anybody has -- the argument
        ``TimelineEvent.diagnostic`` is withheld under.
        """
        from careloop.policy import default_rules

        text = " ".join(strings_in(statement(demo_client())))
        for rule in default_rules().rules:
            assert rule.diagnostic.strip()[:40] not in text


class TestTheShapeIsStable:
    """What a frontend may rely on, asserted so a projector change cannot quietly move it."""

    def test_every_capability_carries_the_whole_shape(self) -> None:
        for capability in statement(demo_client())["capabilities"]:
            assert set(capability) == {
                "tool_name",
                "standing",
                "unavailable_because",
                "unmet_trust_keys",
                "independently_observed",
                "recovery_class",
                "arguments",
            }
            assert capability["standing"] in {"AUTONOMOUS", "REQUIRES_HUMAN", "UNAVAILABLE"}
            for argument in capability["arguments"]:
                assert set(argument) == {
                    "name",
                    "arg_class",
                    "required_trust",
                    "trust_key",
                    "configured_value",
                    "authored_value",
                    "identifies",
                }

    def test_an_available_capability_names_no_unmet_key(self) -> None:
        for capability in statement(demo_client())["capabilities"]:
            if capability["standing"] == "UNAVAILABLE":
                continue
            assert capability["unmet_trust_keys"] == []
            assert capability["unavailable_because"] is None

    def test_arguments_are_in_the_order_the_registry_declares_them(self) -> None:
        """The recipient of an email is read before its contents, as the decision card does it."""
        from careloop.tools import REGISTRY

        for capability in statement(demo_client())["capabilities"]:
            spec = REGISTRY.get(capability["tool_name"])
            assert [argument["name"] for argument in capability["arguments"]] == [
                arg.name for arg in spec.args
            ]


class TestTheDemoAndProductionStatementsDifferOnlyInConfiguration:
    """The same rules, so the same standings; different caregivers, so different destinations."""

    def test_standings_do_not_depend_on_who_is_asking(self) -> None:
        demo = {c["tool_name"]: c["standing"] for c in statement(demo_client())["capabilities"]}
        production = {
            c["tool_name"]: c["standing"]
            for c in statement(production_client(), headers=bearer(TOKEN_A))["capabilities"]
        }
        assert demo == production

    def test_but_the_destinations_do(self) -> None:
        assert configured_values(statement(demo_client())) != configured_values(
            statement(production_client(), headers=bearer(TOKEN_A))
        )
