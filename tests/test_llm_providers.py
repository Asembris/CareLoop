"""Role-to-provider selection.

The property that matters most is negative: a process that was not told about a real provider
cannot reach one. That is asserted directly, and it is asserted with an *empty* environment rather
than the ambient one, so the result does not depend on whose machine the suite runs on.
"""

from __future__ import annotations

import socket

import pytest
from strands.models import Model

from careloop.llm.mock import MockModel
from careloop.llm.providers import (
    BEDROCK_MODEL_ID_VAR,
    DEFAULT_NVIDIA_BASE_URL,
    ROLE_ENV_VAR,
    MalformedModelSpec,
    MissingCredential,
    ModelRole,
    ModelSpec,
    UnknownProvider,
    bedrock_region_prefix,
    build_model,
    model_for,
    parse_spec,
    resolve_bedrock_model_id,
    spec_for_role,
)

EMPTY: dict[str, str] = {}


class TestTheDefaultIsTheMock:
    @pytest.mark.parametrize("role", list(ModelRole))
    def test_unset_resolves_to_mock(self, role: ModelRole):
        assert spec_for_role(role, EMPTY).is_mock

    @pytest.mark.parametrize("role", list(ModelRole))
    def test_unset_builds_a_mock_model(self, role: ModelRole):
        assert isinstance(model_for(role, EMPTY), MockModel)

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_is_treated_as_unset(self, blank: str):
        env = {ROLE_ENV_VAR[ModelRole.PLANNER]: blank}
        assert spec_for_role(ModelRole.PLANNER, env).is_mock

    def test_explicit_mock_is_the_mock(self):
        env = {ROLE_ENV_VAR[ModelRole.TRIAGE]: "mock"}
        assert isinstance(model_for(ModelRole.TRIAGE, env), MockModel)

    def test_resolving_every_default_opens_no_socket(self, monkeypatch):
        # Vacuity: this passes trivially unless something actually tries to connect. The guard is
        # proven by the converse test below, which shows the patch does fire when a socket opens.
        def explode(*args, **kwargs):
            raise AssertionError("resolving a default model opened a socket")

        monkeypatch.setattr(socket.socket, "connect", explode)
        monkeypatch.setattr(socket, "create_connection", explode)
        for role in ModelRole:
            assert isinstance(model_for(role, EMPTY), Model)

    def test_the_socket_guard_itself_fires(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("socket opened")

        monkeypatch.setattr(socket, "create_connection", explode)
        with pytest.raises(AssertionError, match="socket opened"):
            socket.create_connection(("127.0.0.1", 9))


class TestRolesAreIndependent:
    def test_setting_one_role_does_not_move_another(self):
        env = {ROLE_ENV_VAR[ModelRole.PLANNER]: "bedrock:us.amazon.nova-lite-v1:0"}
        assert spec_for_role(ModelRole.PLANNER, env).provider == "bedrock"
        assert spec_for_role(ModelRole.TRIAGE, env).is_mock
        assert spec_for_role(ModelRole.EXTRACTION, env).is_mock

    def test_each_role_reads_a_distinct_variable(self):
        assert len(set(ROLE_ENV_VAR.values())) == len(ModelRole)

    def test_every_role_has_a_variable(self):
        assert set(ROLE_ENV_VAR) == set(ModelRole)


class TestSpecParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("mock", ModelSpec(provider="mock")),
            ("", ModelSpec(provider="mock")),
            ("bedrock", ModelSpec(provider="bedrock")),
            (
                "bedrock:us.amazon.nova-lite-v1:0",
                ModelSpec(provider="bedrock", model_id="us.amazon.nova-lite-v1:0"),
            ),
            (
                "bedrock:global.amazon.nova-2-lite-v1:0",
                ModelSpec(provider="bedrock", model_id="global.amazon.nova-2-lite-v1:0"),
            ),
            ("openai:gpt-4o-mini", ModelSpec(provider="openai", model_id="gpt-4o-mini")),
            (
                "openai:some-model@https://gateway.example/v1",
                ModelSpec(
                    provider="openai",
                    model_id="some-model",
                    base_url="https://gateway.example/v1",
                ),
            ),
            (
                "nvidia:meta/llama-3.3-70b-instruct",
                ModelSpec(provider="nvidia", model_id="meta/llama-3.3-70b-instruct"),
            ),
        ],
    )
    def test_parses(self, text: str, expected: ModelSpec):
        assert parse_spec(text) == expected

    def test_a_bedrock_model_id_keeps_its_colons(self):
        # `us.amazon.nova-lite-v1:0` contains the separator character. Splitting on every colon
        # rather than the first would truncate the version suffix and silently target a different
        # model id, which the provider would then reject far from here.
        assert parse_spec("bedrock:us.amazon.nova-lite-v1:0").model_id.endswith(":0")

    def test_provider_name_is_case_insensitive(self):
        assert parse_spec("BEDROCK:x").provider == "bedrock"

    def test_surrounding_whitespace_is_ignored(self):
        assert parse_spec("  openai:gpt-4o-mini  ") == ModelSpec(
            provider="openai", model_id="gpt-4o-mini"
        )


class TestUnknownSpecsFailClosed:
    """A typo must never resolve to a working provider, and never to the mock."""

    @pytest.mark.parametrize(
        "text", ["bedrok:nova", "gpt-4o-mini", "anthropic:claude", "openai2:x"]
    )
    def test_unknown_provider_raises(self, text: str):
        with pytest.raises(UnknownProvider):
            parse_spec(text)

    @pytest.mark.parametrize("text", ["bedrok:nova", "anthropic:claude"])
    def test_unknown_provider_does_not_degrade_to_mock(self, text: str):
        # The dangerous failure mode: a run that looks real, costs nothing, and proves nothing.
        with pytest.raises(UnknownProvider):
            build_model(parse_spec(text), EMPTY)

    @pytest.mark.parametrize("text", ["openai:", "nvidia:", "openai:@https://x.example/v1"])
    def test_a_provider_with_no_model_raises(self, text: str):
        with pytest.raises(MalformedModelSpec):
            parse_spec(text)

    def test_a_bad_spec_on_one_role_does_not_silently_fall_back(self):
        env = {ROLE_ENV_VAR[ModelRole.TRIAGE]: "bedrok:nova"}
        with pytest.raises(UnknownProvider):
            model_for(ModelRole.TRIAGE, env)


class TestBedrockModelId:
    @pytest.mark.parametrize(
        ("region", "prefix"),
        [
            ("us-east-1", "us"),
            ("us-west-2", "us"),
            ("eu-west-1", "eu"),
            ("ap-southeast-2", "apac"),
            ("ap-northeast-1", "apac"),
            ("us-gov-east-1", "us-gov"),
        ],
    )
    def test_prefix_derivation_matches_the_sdk_rule(self, region: str, prefix: str):
        assert bedrock_region_prefix(region) == prefix

    def test_region_supplies_the_default_model_id(self):
        spec = ModelSpec(provider="bedrock")
        env = {"AWS_DEFAULT_REGION": "us-east-1"}
        assert resolve_bedrock_model_id(spec, env) == "us.amazon.nova-lite-v1:0"

    def test_aws_region_wins_over_aws_default_region(self):
        spec = ModelSpec(provider="bedrock")
        env = {"AWS_REGION": "eu-west-1", "AWS_DEFAULT_REGION": "us-east-1"}
        assert resolve_bedrock_model_id(spec, env).startswith("eu.")

    def test_the_override_variable_beats_the_derived_default(self):
        spec = ModelSpec(provider="bedrock")
        env = {"AWS_DEFAULT_REGION": "us-east-1", BEDROCK_MODEL_ID_VAR: "us.amazon.nova-pro-v1:0"}
        assert resolve_bedrock_model_id(spec, env) == "us.amazon.nova-pro-v1:0"

    def test_an_explicit_spec_id_beats_the_override_variable(self):
        spec = ModelSpec(provider="bedrock", model_id="global.amazon.nova-2-lite-v1:0")
        env = {"AWS_DEFAULT_REGION": "us-east-1", BEDROCK_MODEL_ID_VAR: "us.amazon.nova-pro-v1:0"}
        assert resolve_bedrock_model_id(spec, env) == "global.amazon.nova-2-lite-v1:0"

    def test_no_region_and_no_id_raises_rather_than_guessing(self):
        with pytest.raises(MissingCredential):
            resolve_bedrock_model_id(ModelSpec(provider="bedrock"), EMPTY)


class TestCredentialsAreRequiredAndNeverEchoed:
    def test_openai_without_a_key_raises(self):
        with pytest.raises(MissingCredential, match="OPENAI_API_KEY"):
            build_model(ModelSpec(provider="openai", model_id="gpt-4o-mini"), EMPTY)

    def test_nvidia_without_a_key_raises(self):
        with pytest.raises(MissingCredential, match="NVIDIA_API_KEY"):
            build_model(ModelSpec(provider="nvidia", model_id="meta/llama-3.3-70b-instruct"), EMPTY)

    @pytest.mark.parametrize("var", ["OPENAI_API_KEY", "NVIDIA_API_KEY"])
    def test_a_blank_key_counts_as_missing(self, var: str):
        provider = "openai" if var == "OPENAI_API_KEY" else "nvidia"
        with pytest.raises(MissingCredential):
            build_model(ModelSpec(provider=provider, model_id="m"), {var: "   "})

    def test_the_error_names_the_variable_not_the_value(self):
        # Same discipline as policy reason purity: a message that echoes a secret ends up in a
        # log, a terminal, and quite possibly a bug report.
        secret = "sk-do-not-leak-me-0123456789"
        with pytest.raises(MissingCredential) as excinfo:
            build_model(ModelSpec(provider="nvidia", model_id="m"), {"OPENAI_API_KEY": secret})
        assert secret not in str(excinfo.value)
        assert "NVIDIA_API_KEY" in str(excinfo.value)


class TestRealProviderConstruction:
    """Construction only. Nothing here calls a provider; no network, no credentials spent."""

    def test_bedrock_builds_with_the_resolved_model_id(self):
        env = {"AWS_DEFAULT_REGION": "us-east-1"}
        model = build_model(ModelSpec(provider="bedrock"), env)
        assert model.get_config()["model_id"] == "us.amazon.nova-lite-v1:0"

    def test_openai_builds_with_the_named_model(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        model = build_model(ModelSpec(provider="openai", model_id="gpt-4o-mini"), env)
        assert model.get_config()["model_id"] == "gpt-4o-mini"

    def test_nvidia_defaults_to_the_nvidia_endpoint(self):
        env = {"NVIDIA_API_KEY": "nvapi-test"}
        model = build_model(ModelSpec(provider="nvidia", model_id="meta/llama-3.3-70b"), env)
        assert model.client_args["base_url"] == DEFAULT_NVIDIA_BASE_URL

    def test_nvidia_base_url_is_overridable_by_environment(self):
        env = {"NVIDIA_API_KEY": "nvapi-test", "NVIDIA_BASE_URL": "https://alt.example/v1"}
        model = build_model(ModelSpec(provider="nvidia", model_id="m"), env)
        assert model.client_args["base_url"] == "https://alt.example/v1"

    def test_a_spec_base_url_beats_the_environment(self):
        env = {"NVIDIA_API_KEY": "nvapi-test", "NVIDIA_BASE_URL": "https://alt.example/v1"}
        spec = ModelSpec(provider="nvidia", model_id="m", base_url="https://spec.example/v1")
        assert build_model(spec, env).client_args["base_url"] == "https://spec.example/v1"

    def test_plain_openai_gets_no_base_url(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        model = build_model(ModelSpec(provider="openai", model_id="gpt-4o-mini"), env)
        assert "base_url" not in model.client_args


class TestTheSuiteItselfUsesNoRealProvider:
    def test_the_ambient_environment_does_not_select_a_real_provider(self):
        # If a developer exports CARELOOP_MODEL_PLANNER in their shell, the rest of the suite
        # would quietly start resolving a paid provider. This is the tripwire for that.
        import os

        configured = {
            var: os.environ[var]
            for var in ROLE_ENV_VAR.values()
            if os.environ.get(var, "").strip() not in ("", "mock")
        }
        assert not configured, (
            f"role variables select a real provider in this environment: {sorted(configured)}. "
            "The suite must run on the mock."
        )
