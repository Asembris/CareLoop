"""The simulator and judge models, and the schema they must be asked with.

DeepTeam drives two models of its own: a **simulator** that generates attacks and a **judge** that
scores responses. Both default to OpenAI, and both are pointed elsewhere here — the OpenAI balance
is small and not replaceable, and the session's budget rule is that neither of these touches it.

    simulator   ``meta/llama-3.3-70b-instruct``
    judge       ``nvidia/llama-3.3-nemotron-super-49b-v1.5``

The split is measured rather than assumed. ``docs/spike-findings.md`` §7.6 records both models
satisfying a compliant schema 6/6, and the judge chosen for the axis that separated them: asked a
question about a transcript, nemotron answered it on 6 of 6 trials while llama-3.3-70b returned
"incomplete information to make a verdict" on 4 of 6. Attack generation does not need
judge-grade reasoning; every headline number is a judge verdict, so the judge gets the better
reasoner and a generous token budget, because it is a reasoning model and a tight one truncates it
mid-thought.

**The strict schema is emitted here, not delegated.** §7.6's finding is that NVIDIA's endpoint
accepts a ``strict: true`` request whose *schema* is not strict-compliant and then decodes
**unconstrained** — a schema OpenAI would reject outright. The guard fails open and nothing
reports it. :func:`strict_schema` produces the compliant form so that the constraint is real, and
:class:`tests.test_evals_models.TestTheStrictSchemaIsActuallyStrict` fails if it regresses to a
bare ``model_json_schema()``.

**Nothing here imports ``deepeval`` at module scope.** The decisions this module makes — which
model, which credential, which endpoint, what schema — are worth testing on a machine where the
``evals`` extra is not installed, which is the machine CI runs on. That is the same reasoning
``careloop/llm/providers.py`` applies to ``openai_client_args``, and for the same reason: resolve
first, import second.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final

from pydantic import BaseModel

from careloop.llm.providers import ModelSpec, openai_client_args, parse_spec

#: Measured defaults. See the module docstring and ``docs/spike-findings.md`` §7.6.
SIMULATOR_SPEC: Final[str] = "nvidia:meta/llama-3.3-70b-instruct"
JUDGE_SPEC: Final[str] = "nvidia:nvidia/llama-3.3-nemotron-super-49b-v1.5"

#: Generous on purpose. The judge is a reasoning model and spent 300 tokens thinking before
#: emitting anything during the spike, which surfaced as a ``LengthFinishReasonError`` rather than
#: as a schema failure — a truncation misreported as a refusal.
JUDGE_MAX_TOKENS: Final[int] = 4096
SIMULATOR_MAX_TOKENS: Final[int] = 2048

#: Well below deepteam's default of 10. The spike measured one
#: ``503 ResourceExhausted: Worker local total request limit reached (16/16)`` and four 120 s
#: timeouts across roughly 26 calls, against an endpoint that reports a 16-worker pool — and
#: deepteam retries three times with backoff, so high concurrency manufactures the exhaustion it
#: is retrying around. A slower run that completes beats a fast one that reports ``errored``.
MAX_CONCURRENT: Final[int] = 2

#: Per-request timeout. Longer than the spike's 120 s, which the judge occasionally exceeded.
REQUEST_TIMEOUT_SECONDS: Final[float] = 180.0


class ModelRole(StrEnum):
    """The two roles DeepTeam drives, which are not CareLoop's roles."""

    SIMULATOR = "simulator"
    JUDGE = "judge"


def spec_for(role: ModelRole) -> ModelSpec:
    """The parsed provider spec for one DeepTeam role."""
    return parse_spec(SIMULATOR_SPEC if role is ModelRole.SIMULATOR else JUDGE_SPEC)


def max_tokens_for(role: ModelRole) -> int:
    return SIMULATOR_MAX_TOKENS if role is ModelRole.SIMULATOR else JUDGE_MAX_TOKENS


def client_args_for(role: ModelRole, env: Mapping[str, str]) -> dict[str, str]:
    """Which credential and which endpoint, for one role.

    Delegates to :func:`careloop.llm.providers.openai_client_args`, so ``evals`` cannot drift into
    a second answer about where NVIDIA lives or which variable holds its key. It also means the
    ``MissingCredential`` raised when the key is absent names the variable and never the value.
    """
    return openai_client_args(spec_for(role), env)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``model``'s JSON schema in the form strict Structured Outputs actually requires.

    OpenAI's strict mode demands ``additionalProperties: false`` on every object and every property
    listed in ``required``; a schema without them is **rejected**. NVIDIA's endpoint accepts it and
    decodes unconstrained instead — the spike watched a model return an object with an extra key
    under ``strict: true``, with nothing raised anywhere. So the compliant form is built here
    rather than relied upon.

    Applied recursively, including through ``$defs``, because a nested object left permissive is
    exactly the gap that lets a judge return a shape the caller cannot parse.
    """
    schema = copy.deepcopy(model.model_json_schema())
    _tighten(schema)
    return schema


def _tighten(node: Any) -> None:
    """Walk a JSON schema, making every object closed and every property required."""
    if isinstance(node, list):
        for item in node:
            _tighten(item)
        return
    if not isinstance(node, dict):
        return

    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = sorted(properties)

    for key, value in node.items():
        # `required` is a list of names, not of schemas, and recursing into it would do nothing.
        if key != "required":
            _tighten(value)


@lru_cache(maxsize=1)
def _adapter_class() -> type:
    """Define the ``DeepEvalBaseLLM`` adapter, importing deepeval only when one is needed.

    A class statement needs its base at definition time, so this cannot be a module-level class
    without dragging deepeval into every import of this module. Cached, so the class is defined
    once per process and two adapters remain instances of the same type.
    """
    from deepeval.models import DeepEvalBaseLLM

    class NvidiaAdapter(DeepEvalBaseLLM):
        """An OpenAI-compatible NVIDIA model, in the shape deepteam calls.

        The contract, read from ``deepteam/attacks/attack_simulator/utils.py`` and mirrored in
        every metric: ``await model.a_generate(prompt=..., schema=SomePydanticModel)``, returning
        the pydantic instance. A returned ``str`` would be JSON-repaired by deepteam instead;
        returning the instance skips that and makes a failure loud.

        Cost is not reported. deepteam records ``None`` for a non-native model, and NVIDIA's
        developer endpoint is credit-metered rather than billed per token, so there is no dollar
        figure to report and inventing one would be worse than the honest zero. Spend accounting
        is :mod:`evals.budget`'s job, in calls.
        """

        def __init__(self, role: ModelRole, env: Mapping[str, str], cache: Any = None) -> None:
            self.role = role
            self.spec = spec_for(role)
            self.env = dict(env)
            self.cache = cache
            self.max_tokens = max_tokens_for(role)
            self.calls = 0
            self._client: Any = None

        def load_model(self) -> Any:
            if self._client is None:
                from openai import OpenAI

                self._client = OpenAI(
                    **client_args_for(self.role, self.env),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                )
            return self._client

        def get_model_name(self) -> str:
            return self.spec.model_id

        def _call(self, prompt: str, schema: type[BaseModel] | None) -> Any:
            client = self.load_model()
            if schema is None:
                response = client.chat.completions.create(
                    model=self.spec.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.max_tokens,
                )
                return response.choices[0].message.content

            response = client.chat.completions.create(
                model=self.spec.model_id,
                messages=[{"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "strict": True,
                        "schema": strict_schema(schema),
                    },
                },
                max_tokens=self.max_tokens,
            )
            return schema.model_validate_json(response.choices[0].message.content)

        def generate(self, prompt: str, schema: type[BaseModel] | None = None, **_: Any) -> Any:
            self.calls += 1
            if self.cache is not None:
                return self.cache.resolve(self, prompt, schema, self._call)
            return self._call(prompt, schema)

        async def a_generate(
            self, prompt: str, schema: type[BaseModel] | None = None, **_: Any
        ) -> Any:
            # Synchronous inside. The OpenAI client is blocking, and deepteam awaits this from a
            # bounded pool whose size is MAX_CONCURRENT — running the blocking call directly keeps
            # the concurrency exactly what that number says rather than what an event loop decides.
            return self.generate(prompt, schema)

    return NvidiaAdapter


def build_model(role: ModelRole, env: Mapping[str, str], cache: Any = None) -> Any:
    """The adapter DeepTeam will drive for ``role``.

    Raises:
        MissingCredential: if ``NVIDIA_API_KEY`` is absent. Raised here, before any attack is
            generated, rather than surfacing as an errored test case halfway through a run.
    """
    client_args_for(role, env)
    return _adapter_class()(role, env, cache)


__all__ = [
    "JUDGE_MAX_TOKENS",
    "JUDGE_SPEC",
    "MAX_CONCURRENT",
    "REQUEST_TIMEOUT_SECONDS",
    "SIMULATOR_MAX_TOKENS",
    "SIMULATOR_SPEC",
    "ModelRole",
    "build_model",
    "client_args_for",
    "max_tokens_for",
    "spec_for",
    "strict_schema",
]
