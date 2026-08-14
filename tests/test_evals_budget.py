"""The two ceilings, the response cache, and the schema the judge is asked with.

All offline. The point of testing these without ``deepeval`` installed is that these are the
*decisions* — which model, which credential, what schema, when to stop — and a decision that can
only be tested where a paid extra is installed is a decision nobody checks.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from pydantic import BaseModel, Field

from bench.cost import load_rates
from careloop.llm.providers import MissingCredential
from evals.budget import (
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_SPEND,
    Budget,
    CallCeilingExceeded,
    CallLedger,
    is_credit_metered,
)
from evals.cache import CachedResponse, ResponseCache, response_key
from evals.models import (
    JUDGE_SPEC,
    MAX_CONCURRENT,
    SIMULATOR_SPEC,
    ModelRole,
    client_args_for,
    spec_for,
    strict_schema,
)

EVALS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "evals"


class Nested(BaseModel):
    label: str
    weight: int = 0


class Verdict(BaseModel):
    score: int = Field(description="0 or 1")
    reason: str
    detail: Nested


class TestTheStrictSchemaIsActuallyStrict:
    """``docs/spike-findings.md`` §7.6: NVIDIA accepts a non-compliant strict schema and then
    decodes **unconstrained**, silently. The compliant form has to be built here or the
    ``strict: true`` on the request is decoration."""

    def test_every_object_is_closed(self) -> None:
        schema = strict_schema(Verdict)

        assert schema["additionalProperties"] is False

    def test_every_property_is_required(self) -> None:
        schema = strict_schema(Verdict)

        assert schema["required"] == ["detail", "reason", "score"]

    def test_nested_definitions_are_tightened_too(self) -> None:
        """A nested object left permissive is the gap that lets a judge return an unparseable
        shape while the top level looks constrained."""
        nested = strict_schema(Verdict)["$defs"]["Nested"]

        assert nested["additionalProperties"] is False
        assert nested["required"] == ["label", "weight"]

    def test_a_field_with_a_default_is_still_required(self) -> None:
        """Strict mode has no notion of optional. ``weight`` has a default and must still be
        listed, or the request is not a valid strict schema and NVIDIA will not say so."""
        assert "weight" in strict_schema(Verdict)["$defs"]["Nested"]["required"]

    def test_the_naive_form_would_not_have_been_compliant(self) -> None:
        """Vacuity proof. If ``model_json_schema()`` ever becomes compliant on its own, this
        function stops earning its place — but that has to be shown, not assumed."""
        naive = Verdict.model_json_schema()

        assert "additionalProperties" not in naive
        assert naive["$defs"]["Nested"]["required"] == ["label"]

    def test_the_original_model_schema_is_not_mutated(self) -> None:
        """A deep copy, so tightening the transmitted form cannot leak into anything else that
        asks the same model for its schema."""
        strict_schema(Verdict)

        assert "additionalProperties" not in Verdict.model_json_schema()


class TestTheCallCeiling:
    def test_it_counts_every_call_whoever_made_it(self) -> None:
        ledger = CallLedger(ceiling=10)
        ledger.charge("simulator", 3)
        ledger.charge("judge", 2)

        assert ledger.made == 5
        assert ledger.by_role == {"judge": 2, "simulator": 3}
        assert ledger.remaining == 5

    def test_it_raises_once_the_ceiling_is_passed(self) -> None:
        ledger = CallLedger(ceiling=2)
        ledger.charge("judge")
        ledger.charge("judge")

        with pytest.raises(CallCeilingExceeded, match="ceiling of 2"):
            ledger.charge("judge")

    def test_the_reported_total_is_what_was_spent_not_what_was_permitted(self) -> None:
        """Charged before the check, so the message names reality."""
        ledger = CallLedger(ceiling=1)
        ledger.charge("judge")
        with pytest.raises(CallCeilingExceeded, match="2 provider call"):
            ledger.charge("judge")

        assert ledger.made == 2

    def test_would_exceed_answers_without_charging(self) -> None:
        ledger = CallLedger(ceiling=2)
        ledger.charge("judge")

        assert ledger.would_exceed(2) is True
        assert ledger.made == 1


class TestADollarCeilingCannotSeeACreditMeteredEndpoint:
    """The finding that made the second ceiling necessary. See ``docs/spike-findings.md`` §7.7."""

    def test_the_nvidia_models_are_priced_at_zero(self) -> None:
        rates = load_rates()

        assert is_credit_metered(rates, "meta/llama-3.3-70b-instruct")
        assert is_credit_metered(rates, "nvidia/llama-3.3-nemotron-super-49b-v1.5")

    def test_the_bedrock_target_models_are_not(self) -> None:
        """The spend ceiling is not decoration — it guards the target, which really is priced."""
        rates = load_rates()

        assert not is_credit_metered(rates, "us.amazon.nova-micro-v1:0")
        assert not is_credit_metered(rates, "us.amazon.nova-2-lite-v1:0")

    def test_a_run_of_only_credit_metered_calls_spends_nothing_and_still_stops(self) -> None:
        """The whole argument in one test: unbounded in dollars, bounded in calls."""
        budget = Budget(max_calls=3, max_spend_usd=100.0)
        budget.charge_call("judge")
        budget.charge_call("judge")
        budget.charge_call("judge")

        assert budget.spend.spent_usd == 0.0
        with pytest.raises(CallCeilingExceeded):
            budget.charge_call("judge")

    def test_a_priced_call_charges_both(self) -> None:
        budget = Budget(max_calls=100, max_spend_usd=1.0)
        cost = budget.charge_priced("us.amazon.nova-micro-v1:0", 1_000_000, 0)
        budget.charge_call("target")

        assert cost == pytest.approx(0.035)
        assert budget.spend.spent_usd == pytest.approx(0.035)
        assert budget.calls.made == 1

    def test_the_defaults_are_the_approved_ones(self) -> None:
        assert DEFAULT_MAX_CALLS == 1500
        assert DEFAULT_MAX_SPEND == 2.00


class TestTheResponseCache:
    def test_a_hit_returns_the_recorded_instance(self, tmp_path) -> None:
        cache = ResponseCache(tmp_path)
        key = response_key(("judge", "m", "prompt", "Nested", "{}", "10"))
        cache.put(
            CachedResponse(
                key=key,
                role="judge",
                model_id="m",
                schema_name="Nested",
                ok=True,
                output={"label": "x", "weight": 2},
            )
        )

        stored = cache.get(key)

        assert stored is not None
        assert Nested.model_validate(stored.output).label == "x"

    def test_the_key_covers_the_schema_as_transmitted(self) -> None:
        """Two different schemas for the same class are two different questions.

        §7.6 again: the bare form and the strict-compliant form get different answers out of
        NVIDIA, so keying on the class name alone would serve an unconstrained answer to a
        constrained request.
        """
        bare = response_key(("judge", "m", "p", "Verdict", json.dumps({"a": 1}), "10"))
        strict = response_key(("judge", "m", "p", "Verdict", json.dumps({"a": 2}), "10"))

        assert bare != strict

    def test_the_key_ignores_nothing_that_could_change_the_answer(self) -> None:
        base = ("judge", "m", "p", "V", "{}", "10")
        for index in range(len(base)):
            changed = list(base)
            changed[index] = changed[index] + "-different"

            assert response_key(tuple(changed)) != response_key(base), (
                f"field {index} does not affect the cache key"
            )

    def test_nothing_is_written_until_flush(self, tmp_path) -> None:
        """A run aborted on a ceiling must not leave a cache a later run trusts."""
        cache = ResponseCache(tmp_path)
        cache.put(
            CachedResponse(key="k", role="judge", model_id="m", schema_name="N", ok=True, output={})
        )

        assert list(tmp_path.glob("*.json")) == []
        assert cache.flush() == 1
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_a_disabled_cache_never_reports_a_hit(self, tmp_path) -> None:
        cache = ResponseCache(tmp_path, enabled=False)
        cache.put(
            CachedResponse(key="k", role="judge", model_id="m", schema_name="N", ok=True, output={})
        )

        assert cache.get("k") is None

    def test_a_cached_failure_is_re_raised_rather_than_re_bought(self, tmp_path) -> None:
        cache = ResponseCache(tmp_path)
        cache.put(
            CachedResponse(
                key="k",
                role="judge",
                model_id="m",
                schema_name="N",
                ok=False,
                error_type="APITimeoutError",
                error_message="timed out",
            )
        )
        stored = cache.get("k")

        assert stored is not None and stored.ok is False


class TestTheModelChoicesAreTheMeasuredOnes:
    def test_the_simulator_and_judge_are_different_models(self) -> None:
        """Measured, not stylistic: they were separated on verdict quality, not schema fit."""
        assert SIMULATOR_SPEC != JUDGE_SPEC

    def test_neither_reaches_openai(self) -> None:
        """The session's budget rule. The $5 OpenAI balance is not touched by this harness."""
        assert spec_for(ModelRole.SIMULATOR).provider == "nvidia"
        assert spec_for(ModelRole.JUDGE).provider == "nvidia"

    def test_the_credential_variable_is_nvidias(self) -> None:
        args = client_args_for(ModelRole.JUDGE, {"NVIDIA_API_KEY": "not-a-real-key"})

        assert args["api_key"] == "not-a-real-key"
        assert "integrate.api.nvidia.com" in args["base_url"]

    def test_a_missing_credential_names_the_variable_and_never_a_value(self) -> None:
        with pytest.raises(MissingCredential) as raised:
            client_args_for(ModelRole.JUDGE, {})

        assert "NVIDIA_API_KEY" in str(raised.value)

    def test_concurrency_is_well_below_deepteams_default(self) -> None:
        """The spike measured a 16-worker pool refusing at 16/16 while deepteam retried into it."""
        assert MAX_CONCURRENT < 10


class TestTheOfflineLayerNeedsNoPaidExtra:
    """CI installs ``.[dev]`` alone and must still be able to run the free half.

    Asserted on the *import graph* rather than on a successful import, because on a developer
    machine the extra is installed and a plain ``import`` would pass either way.
    """

    def test_importing_the_decisions_does_not_require_deepeval(self) -> None:
        import evals.budget
        import evals.cache
        import evals.capture
        import evals.corpus
        import evals.models
        import evals.replay
        import evals.targets

        for module in (
            evals.budget,
            evals.cache,
            evals.capture,
            evals.corpus,
            evals.models,
            evals.replay,
            evals.targets,
        ):
            source = (module.__file__ or "").lower()
            assert source, module

    def test_no_offline_module_imports_deepteam_at_module_scope(self) -> None:
        offline = ("capture.py", "corpus.py", "replay.py", "targets.py", "budget.py", "models.py")
        root = EVALS_ROOT
        offenders: list[str] = []

        for name in offline:
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    ("deepteam", "deepeval")
                ):
                    offenders.append(f"{name}: from {node.module}")
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{name}: import {alias.name}"
                        for alias in node.names
                        if alias.name.startswith(("deepteam", "deepeval"))
                    )

        assert offenders == [], (
            f"module-scope deepteam/deepeval import in the offline layer: {offenders}. The CI job "
            f"that prints the headline number installs .[dev] only."
        )

    def test_the_sweep_would_catch_a_module_scope_import(self) -> None:
        """Vacuity proof for the sweep, which passes on an empty file list and on a tree with no
        imports at all."""
        tree = ast.parse("from deepteam import red_team\n")
        found = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("deepteam")
        ]

        assert found
