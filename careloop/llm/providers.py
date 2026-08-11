"""Which model backs which role.

Three roles, three environment variables, one default. **Unset means the mock**, so a process that
was never told about a real provider cannot reach one by accident — that is what lets CI assert
"$0, offline, no secrets" rather than hope for it.

    CARELOOP_MODEL_TRIAGE       classification of an inbound event into typed Case fields
    CARELOOP_MODEL_PLANNER      proposal of one next action as a typed tool intent
    CARELOOP_MODEL_EXTRACTION   pulling field values out of document text

Each holds a **spec**:

    mock                            the scripted mock provider (default)
    bedrock                         Bedrock, model id derived from the region
    bedrock:<model-id>              Bedrock, explicit model id
    openai:<model>                  OpenAI proper, via OPENAI_API_KEY
    openai:<model>@<base_url>       any OpenAI-compatible gateway, via OPENAI_API_KEY
    nvidia:<model>                  NVIDIA's hosted endpoint, via NVIDIA_API_KEY

Two properties this module exists to hold, both tested:

**Unknown specs fail closed, loudly.** A typo'd ``bedrok:nova`` raises; it does not quietly
degrade to the mock. Silently substituting the mock for a provider the operator asked for would
produce a run that looks real, costs nothing, and means nothing.

**Nothing is constructed at import.** Provider SDKs are imported inside the constructor functions
and a client is built only when :func:`model_for` is called. Importing ``careloop.llm.providers``
reads no credential, opens no socket, and works fine on a machine with no AWS configuration —
which is the machine CI runs on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from strands.models import Model


class UnknownProvider(Exception):
    """Raised when a spec names a provider this module does not implement."""


class MalformedModelSpec(Exception):
    """Raised when a spec has the right provider but an unusable body."""


class MissingCredential(Exception):
    """Raised when a real provider is selected and its credential variable is absent.

    Names the *variable*, never the value. An exception message ends up in logs, in a terminal
    and quite possibly in a bug report.
    """


class ModelRole(StrEnum):
    """The roles CareLoop assigns a model to."""

    TRIAGE = "triage"
    PLANNER = "planner"
    EXTRACTION = "extraction"


#: Which environment variable selects the model for each role.
ROLE_ENV_VAR: Final[Mapping[ModelRole, str]] = {
    ModelRole.TRIAGE: "CARELOOP_MODEL_TRIAGE",
    ModelRole.PLANNER: "CARELOOP_MODEL_PLANNER",
    ModelRole.EXTRACTION: "CARELOOP_MODEL_EXTRACTION",
}

#: Used when a spec selects Bedrock without naming a model.
DEFAULT_BEDROCK_MODEL: Final[str] = "amazon.nova-lite-v1:0"

#: Overrides the derived Bedrock model id for every Bedrock-backed role.
BEDROCK_MODEL_ID_VAR: Final[str] = "CARELOOP_BEDROCK_MODEL_ID"

#: Default endpoint for the ``nvidia`` provider, itself overridable.
DEFAULT_NVIDIA_BASE_URL: Final[str] = "https://integrate.api.nvidia.com/v1"

MOCK_SPEC: Final[str] = "mock"


@dataclass(frozen=True)
class ModelSpec:
    """A parsed spec. Inert: describes a model, constructs nothing."""

    provider: str
    model_id: str = ""
    base_url: str = ""

    @property
    def is_mock(self) -> bool:
        return self.provider == MOCK_SPEC


def parse_spec(text: str) -> ModelSpec:
    """Parse a spec string.

    Raises:
        UnknownProvider: if the provider is not one of mock, bedrock, openai, nvidia.
        MalformedModelSpec: if a provider that requires a model id was given none.
    """
    raw = text.strip()
    if not raw or raw == MOCK_SPEC:
        return ModelSpec(provider=MOCK_SPEC)

    provider, sep, body = raw.partition(":")
    provider = provider.strip().lower()
    if not sep:
        # A bare word. `bedrock` is meaningful — the region supplies the model id — but a bare
        # anything-else is a typo, and a typo must not resolve to a working provider.
        if provider == "bedrock":
            return ModelSpec(provider="bedrock")
        raise UnknownProvider(
            f"model spec {text!r} names no known provider; expected one of "
            f"mock, bedrock, openai, nvidia"
        )

    if provider == "bedrock":
        return ModelSpec(provider="bedrock", model_id=body.strip())

    if provider in ("openai", "nvidia"):
        model_id, _, base_url = body.partition("@")
        model_id = model_id.strip()
        if not model_id:
            raise MalformedModelSpec(f"model spec {text!r} selects {provider} but names no model")
        return ModelSpec(provider=provider, model_id=model_id, base_url=base_url.strip())

    raise UnknownProvider(
        f"model spec {text!r} names unknown provider {provider!r}; expected one of "
        f"mock, bedrock, openai, nvidia"
    )


def spec_for_role(role: ModelRole, env: Mapping[str, str] | None = None) -> ModelSpec:
    """The parsed spec for ``role``. Unset, or set to blank, means the mock."""
    environ = os.environ if env is None else env
    return parse_spec(environ.get(ROLE_ENV_VAR[role], ""))


def bedrock_region_prefix(region: str) -> str:
    """The inference-profile prefix for ``region``.

    Mirrors ``BedrockModel._get_default_model_with_warning`` in ``strands-agents==1.51.0`` exactly,
    including the ``ap`` -> ``apac`` remap and the handling of four-part regions like
    ``us-gov-east-1``. Mirrored rather than imported because it is a private helper; the spike
    recorded the rule and a test pins our copy against the regions it covers.
    """
    prefix = "-".join(region.split("-")[:-2]).lower()
    return {"ap": "apac"}.get(prefix, prefix)


def resolve_bedrock_model_id(spec: ModelSpec, env: Mapping[str, str] | None = None) -> str:
    """The Bedrock model id to use, most specific source winning.

    Order: the spec's own id, then ``CARELOOP_BEDROCK_MODEL_ID``, then the region-prefixed default.
    """
    environ = os.environ if env is None else env
    if spec.model_id:
        return spec.model_id
    override = environ.get(BEDROCK_MODEL_ID_VAR, "").strip()
    if override:
        return override
    region = (environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        raise MissingCredential(
            "Bedrock was selected but no region is configured; set AWS_DEFAULT_REGION "
            f"or give an explicit model id via {BEDROCK_MODEL_ID_VAR}"
        )
    return f"{bedrock_region_prefix(region)}.{DEFAULT_BEDROCK_MODEL}"


def bedrock_session(env: Mapping[str, str] | None = None):
    """A boto3 session built from ``env``, or ``None`` to leave boto3's own chain in charge.

    ``env`` exists so a caller can supply configuration without mutating the process environment —
    the benchmark reads ``.env`` and passes it down. boto3, however, resolves credentials from
    ``os.environ`` and never sees that mapping, so an explicit session is the only way the
    parameter can mean the same thing for Bedrock as it already does for the OpenAI-compatible
    providers. Without this, a caller that passed perfectly good credentials got
    ``NoCredentialsError``.

    Returns ``None`` when the mapping carries no key pair, so the ambient chain (instance profile,
    SSO, shared credentials file) still works and CI's zero-credential run is unaffected.
    """
    environ = os.environ if env is None else env
    access_key = environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if not (access_key and secret_key):
        return None

    import boto3

    return boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=environ.get("AWS_SESSION_TOKEN", "").strip() or None,
        region_name=(environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or "").strip()
        or None,
    )


def _require(env: Mapping[str, str], var: str, provider: str) -> str:
    value = env.get(var, "").strip()
    if not value:
        raise MissingCredential(
            f"provider {provider!r} requires {var}, which is unset or empty. "
            f"Populate it in .env (see .env.example)."
        )
    return value


def openai_client_args(spec: ModelSpec, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Client arguments for an OpenAI-compatible provider: which key, and which endpoint.

    A pure function of the spec and the environment. It imports no SDK, opens no socket and
    constructs nothing, so the decisions it encodes — which credential variable each provider
    uses, where the base URL comes from, and that a missing key raises rather than proceeding —
    are testable with no provider package installed.

    Raises:
        MissingCredential: if the selected provider's key variable is unset or blank.
    """
    environ = os.environ if env is None else env

    if spec.provider == "nvidia":
        api_key = _require(environ, "NVIDIA_API_KEY", "nvidia")
        base_url = (
            spec.base_url or environ.get("NVIDIA_BASE_URL", "").strip() or DEFAULT_NVIDIA_BASE_URL
        )
    elif spec.provider == "openai":
        api_key = _require(environ, "OPENAI_API_KEY", "openai")
        base_url = spec.base_url
    else:
        raise UnknownProvider(f"provider {spec.provider!r} is not OpenAI-compatible")

    client_args = {"api_key": api_key}
    if base_url:
        client_args["base_url"] = base_url
    return client_args


def build_model(spec: ModelSpec, env: Mapping[str, str] | None = None) -> Model:
    """Construct the provider named by ``spec``.

    Provider SDKs are imported here, not at module scope, so that importing this module never
    pulls a client library or reads a credential.
    """
    environ = os.environ if env is None else env

    if spec.is_mock:
        from careloop.llm.mock import MockModel

        return MockModel()

    if spec.provider == "bedrock":
        from strands.models import BedrockModel

        model_id = resolve_bedrock_model_id(spec, environ)
        region = (environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or "").strip()
        session = bedrock_session(environ)
        if session is not None:
            return BedrockModel(model_id=model_id, boto_session=session)
        return BedrockModel(model_id=model_id, region_name=region or None)

    if spec.provider in ("openai", "nvidia"):
        # Resolve first, import second. The credential and endpoint decisions are ours and are
        # worth testing on their own; `strands.models.openai` needs the `openai` package, which is
        # in the `bench` extra and deliberately absent from `dev`. Importing before deciding made
        # every one of those decisions untestable wherever the extra is not installed — which is
        # exactly where CI runs.
        client_args = openai_client_args(spec, environ)

        from strands.models.openai import OpenAIModel

        return OpenAIModel(client_args=client_args, model_id=spec.model_id)

    raise UnknownProvider(f"no constructor for provider {spec.provider!r}")


def model_for(role: ModelRole, env: Mapping[str, str] | None = None) -> Model:
    """The model backing ``role``. Defaults to the mock when the role's variable is unset."""
    return build_model(spec_for_role(role, env), env)


__all__ = [
    "BEDROCK_MODEL_ID_VAR",
    "DEFAULT_BEDROCK_MODEL",
    "DEFAULT_NVIDIA_BASE_URL",
    "MOCK_SPEC",
    "ROLE_ENV_VAR",
    "MalformedModelSpec",
    "MissingCredential",
    "ModelRole",
    "ModelSpec",
    "UnknownProvider",
    "bedrock_region_prefix",
    "bedrock_session",
    "build_model",
    "model_for",
    "openai_client_args",
    "parse_spec",
    "resolve_bedrock_model_id",
    "spec_for_role",
]
