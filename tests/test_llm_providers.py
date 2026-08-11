"""Role-to-provider selection.

The property that matters most is negative: a process that was not told about a real provider
cannot reach one. That is asserted directly, and it is asserted with an *empty* environment rather
than the ambient one, so the result does not depend on whose machine the suite runs on.
"""

from __future__ import annotations

import socket
import sys

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
    openai_client_args,
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
    """Asserted against ``openai_client_args``, not ``build_model``.

    These are decisions CareLoop makes, not things the OpenAI SDK does, and they must hold on a
    machine with no provider package installed — which is the machine CI runs on, since `openai`
    lives in the ``bench`` extra and not in ``dev``. Testing them through ``build_model`` made them
    depend on an import that CI does not have, and they failed there for a reason that had nothing
    to do with what they assert.
    """

    def test_openai_without_a_key_raises(self):
        with pytest.raises(MissingCredential, match="OPENAI_API_KEY"):
            openai_client_args(ModelSpec(provider="openai", model_id="gpt-4o-mini"), EMPTY)

    def test_nvidia_without_a_key_raises(self):
        with pytest.raises(MissingCredential, match="NVIDIA_API_KEY"):
            openai_client_args(ModelSpec(provider="nvidia", model_id="meta/llama-3.3-70b"), EMPTY)

    @pytest.mark.parametrize("var", ["OPENAI_API_KEY", "NVIDIA_API_KEY"])
    def test_a_blank_key_counts_as_missing(self, var: str):
        provider = "openai" if var == "OPENAI_API_KEY" else "nvidia"
        with pytest.raises(MissingCredential):
            openai_client_args(ModelSpec(provider=provider, model_id="m"), {var: "   "})

    def test_the_error_names_the_variable_not_the_value(self):
        # Same discipline as policy reason purity: a message that echoes a secret ends up in a
        # log, a terminal, and quite possibly a bug report.
        secret = "sk-do-not-leak-me-0123456789"
        with pytest.raises(MissingCredential) as excinfo:
            openai_client_args(
                ModelSpec(provider="nvidia", model_id="m"), {"OPENAI_API_KEY": secret}
            )
        assert secret not in str(excinfo.value)
        assert "NVIDIA_API_KEY" in str(excinfo.value)

    def test_each_provider_reads_its_own_key_variable(self):
        # Sending OpenAI's key to NVIDIA would produce an auth error that reads like a model
        # failure, which is the whole reason `nvidia:` is a provider rather than a base URL.
        env = {"OPENAI_API_KEY": "sk-openai", "NVIDIA_API_KEY": "nvapi-nvidia"}
        assert openai_client_args(ModelSpec(provider="openai", model_id="m"), env)["api_key"] == (
            "sk-openai"
        )
        assert openai_client_args(ModelSpec(provider="nvidia", model_id="m"), env)["api_key"] == (
            "nvapi-nvidia"
        )


class TestEndpointSelection:
    """Also on ``openai_client_args``, for the same reason: no provider package required."""

    def test_nvidia_defaults_to_the_nvidia_endpoint(self):
        args = openai_client_args(
            ModelSpec(provider="nvidia", model_id="meta/llama-3.3-70b"), {"NVIDIA_API_KEY": "k"}
        )
        assert args["base_url"] == DEFAULT_NVIDIA_BASE_URL

    def test_nvidia_base_url_is_overridable_by_environment(self):
        env = {"NVIDIA_API_KEY": "k", "NVIDIA_BASE_URL": "https://alt.example/v1"}
        args = openai_client_args(ModelSpec(provider="nvidia", model_id="m"), env)
        assert args["base_url"] == "https://alt.example/v1"

    def test_a_spec_base_url_beats_the_environment(self):
        env = {"NVIDIA_API_KEY": "k", "NVIDIA_BASE_URL": "https://alt.example/v1"}
        spec = ModelSpec(provider="nvidia", model_id="m", base_url="https://spec.example/v1")
        assert openai_client_args(spec, env)["base_url"] == "https://spec.example/v1"

    def test_plain_openai_gets_no_base_url(self):
        args = openai_client_args(
            ModelSpec(provider="openai", model_id="gpt-4o-mini"), {"OPENAI_API_KEY": "k"}
        )
        assert "base_url" not in args

    def test_a_non_openai_provider_is_rejected(self):
        with pytest.raises(UnknownProvider):
            openai_client_args(ModelSpec(provider="bedrock", model_id="m"), EMPTY)


class TestResolutionNeedsNoProviderPackage:
    """The regression guard for the CI failure this class was written after.

    `openai` is in the ``bench`` extra, never in ``dev``, so CI installs a tree without it. Ten
    tests failed there with ``ModuleNotFoundError`` while passing locally, because the local
    environment had the extra installed. Blocking the import here reproduces CI's tree inside a
    test, so the difference cannot go unnoticed again on a developer machine.
    """

    @staticmethod
    def _without_openai(monkeypatch):
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "openai" or name.startswith("openai."):
                raise ModuleNotFoundError("No module named 'openai'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        for module in [m for m in sys.modules if m == "openai" or m.startswith("openai.")]:
            monkeypatch.delitem(sys.modules, module, raising=False)
        for module in [m for m in sys.modules if m.startswith("strands.models.openai")]:
            monkeypatch.delitem(sys.modules, module, raising=False)

    def test_the_block_itself_works(self, monkeypatch):
        self._without_openai(monkeypatch)
        with pytest.raises(ModuleNotFoundError):
            import openai  # noqa: F401

    def test_a_missing_key_still_raises_missing_credential(self, monkeypatch):
        self._without_openai(monkeypatch)
        with pytest.raises(MissingCredential, match="OPENAI_API_KEY"):
            build_model(ModelSpec(provider="openai", model_id="gpt-4o-mini"), EMPTY)

    def test_endpoint_selection_still_works(self, monkeypatch):
        self._without_openai(monkeypatch)
        args = openai_client_args(
            ModelSpec(provider="nvidia", model_id="m"), {"NVIDIA_API_KEY": "k"}
        )
        assert args["base_url"] == DEFAULT_NVIDIA_BASE_URL

    def test_the_mock_default_still_resolves(self, monkeypatch):
        self._without_openai(monkeypatch)
        for role in ModelRole:
            assert isinstance(model_for(role, EMPTY), MockModel)

    def test_bedrock_still_resolves(self, monkeypatch):
        self._without_openai(monkeypatch)
        model = build_model(ModelSpec(provider="bedrock"), {"AWS_DEFAULT_REGION": "us-east-1"})
        assert model.get_config()["model_id"] == "us.amazon.nova-lite-v1:0"


class TestRealProviderConstruction:
    """Construction only. Nothing here calls a provider; no network, no credentials spent."""

    def test_bedrock_builds_with_the_resolved_model_id(self):
        env = {"AWS_DEFAULT_REGION": "us-east-1"}
        model = build_model(ModelSpec(provider="bedrock"), env)
        assert model.get_config()["model_id"] == "us.amazon.nova-lite-v1:0"

    def test_openai_builds_with_the_named_model(self):
        # The only assertion that genuinely needs the SDK class, so it is the only one that skips
        # where the `bench` extra is not installed.
        pytest.importorskip("openai", reason="provider SDK lives in the bench extra, not dev")
        env = {"OPENAI_API_KEY": "sk-test"}
        model = build_model(ModelSpec(provider="openai", model_id="gpt-4o-mini"), env)
        assert model.get_config()["model_id"] == "gpt-4o-mini"
        assert model.client_args["api_key"] == "sk-test"

    def test_nvidia_builds_against_its_endpoint(self):
        pytest.importorskip("openai", reason="provider SDK lives in the bench extra, not dev")
        env = {"NVIDIA_API_KEY": "nvapi-test"}
        model = build_model(ModelSpec(provider="nvidia", model_id="meta/llama-3.3-70b"), env)
        assert model.client_args["base_url"] == DEFAULT_NVIDIA_BASE_URL


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


class TestBedrockCredentialsComeFromTheMapping:
    """``env`` must mean the same thing for Bedrock as it does for the OpenAI providers.

    boto3 resolves credentials from ``os.environ`` and never sees a mapping passed in Python, so
    without an explicit session a caller that supplied perfectly good keys gets
    ``NoCredentialsError``. The benchmark reads ``.env`` into a mapping precisely to avoid
    mutating the process environment, so this is the difference between working and not.
    """

    def test_a_key_pair_in_the_mapping_produces_a_session_carrying_it(self):
        from careloop.llm.providers import bedrock_session

        session = bedrock_session(
            {
                "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "AWS_DEFAULT_REGION": "us-east-1",
            }
        )
        assert session is not None
        credentials = session.get_credentials()
        assert credentials.access_key == "AKIAEXAMPLE"
        assert session.region_name == "us-east-1"

    def test_a_session_token_is_carried_when_present(self):
        from careloop.llm.providers import bedrock_session

        session = bedrock_session(
            {
                "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "AWS_SESSION_TOKEN": "token",
                "AWS_DEFAULT_REGION": "us-east-1",
            }
        )
        assert session.get_credentials().token == "token"

    def test_no_key_pair_leaves_boto3s_own_chain_in_charge(self):
        # Returning a credential-less session would break instance profiles and SSO, and would
        # break CI, which runs with no credentials at all and must not start failing differently.
        from careloop.llm.providers import bedrock_session

        assert bedrock_session({"AWS_DEFAULT_REGION": "us-east-1"}) is None

    def test_a_blank_key_pair_is_treated_as_absent(self):
        from careloop.llm.providers import bedrock_session

        assert bedrock_session({"AWS_ACCESS_KEY_ID": "  ", "AWS_SECRET_ACCESS_KEY": "  "}) is None

    def test_build_model_uses_the_mapping_credentials(self):
        env = {
            "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        model = build_model(ModelSpec(provider="bedrock"), env)
        assert model.get_config()["model_id"] == "us.amazon.nova-lite-v1:0"
