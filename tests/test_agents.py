"""The two agents, on the mock, and the structural claims made about them.

The behavioural half is thin on purpose: whether a model classifies well was the benchmark's
question, and asking it again of a scripted mock would only measure the script. What is worth
asserting here is that the agent asks for the right schema with the right prompts, propagates a
provider failure instead of inventing a result, and cannot reach anything that acts.

The structural half is the load-bearing part. Three sweeps read source rather than behaviour,
because each states something about what the code *cannot* do, and a behavioural test can only
ever sample what it happens to do.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from helpers import make_case, make_fact

from careloop.agents import invoke as invoke_module
from careloop.agents.invoke import (
    NoStructuredOutput,
    WrongStructuredOutputType,
    structured_call,
)
from careloop.agents.planner import PlannerAgent
from careloop.agents.prompts import (
    EVIDENCE_BEGIN,
    planner_system_prompt,
    planner_user_prompt,
    triage_system_prompt,
)
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
)
from careloop.agents.triage import TriageAgent
from careloop.llm.mock import MockModel, MockScriptExhausted, MockScriptTypeMismatch, mock_model
from careloop.policy.verdicts import ReasonCode
from careloop.state.case import CaseState
from careloop.tools.documents import POISONED_DOCUMENT_ID, default_document_store

POISON = default_document_store().get(POISONED_DOCUMENT_ID).text

AGENT_PACKAGE = Path(invoke_module.__file__).parent


def a_triage_result() -> TriageResult:
    return TriageResult(
        category=TriageCategory.APPOINTMENT,
        urgency=Urgency.SOON,
        summary="Book the cardiology consultation.",
        patient_id="PT-4417",
    )


def an_intent(tool_name: str = "fetch_document", **args: str):
    return PlannerIntent(
        tool_name=tool_name,
        rationale="because",
        args=args or {"document_id": "doc-0001-referral"},
    )


def agent_modules() -> list[Path]:
    return sorted(AGENT_PACKAGE.glob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Every module name ``path`` imports, read from its syntax tree."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def imports_matching(path: Path, banned: tuple[str, ...]) -> set[str]:
    """The imports of ``path`` that are, or sit under, one of ``banned``."""
    return {
        imported
        for imported in imported_modules(path)
        for prefix in banned
        if imported == prefix or imported.startswith(f"{prefix}.")
    }


def modules_naming(root: Path, needle: str, allowed: frozenset[Path] = frozenset()) -> list[str]:
    """Every module under ``root``, except ``allowed``, whose source contains ``needle``.

    Source text rather than imports: both sweeps built on this are about a *name* appearing at
    all, and an attribute call like ``model.structured_output(...)`` is not an import.
    """
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path not in allowed and needle in path.read_text(encoding="utf-8")
    )


class TestTriageAgent:
    def test_returns_the_scripted_classification(self):
        expected = a_triage_result()
        agent = TriageAgent(mock_model(structured=[expected]))
        assert asyncio.run(agent.classify("Referral received.")) is expected

    def test_asks_for_the_triage_schema_with_the_triage_system_prompt(self):
        model = mock_model(structured=[a_triage_result()])
        asyncio.run(TriageAgent(model).classify("Referral received."))
        (call,) = model.calls
        assert call.kind == "structured_output"
        assert call.output_model_name == "TriageResult"
        assert call.system_prompt == triage_system_prompt()

    def test_calls_the_model_exactly_once(self):
        model = mock_model(structured=[a_triage_result()])
        asyncio.run(TriageAgent(model).classify("Referral received."))
        assert len(model.calls) == 1
        assert model.remaining == (0, 0)

    def test_the_event_reaches_the_user_message_and_not_the_system_prompt(self):
        model = mock_model(structured=[a_triage_result()])
        asyncio.run(TriageAgent(model).classify(POISON))
        (call,) = model.calls
        user_text = call.messages[0]["content"][0]["text"]
        assert "records-intake@claims-continuity-partners.example" in user_text
        assert EVIDENCE_BEGIN in user_text
        assert "records-intake@claims-continuity-partners.example" not in call.system_prompt

    def test_an_exhausted_provider_raises_rather_than_returning_a_default(self):
        agent = TriageAgent(mock_model(structured=[]))
        with pytest.raises(MockScriptExhausted):
            asyncio.run(agent.classify("Referral received."))

    def test_a_wrong_typed_answer_raises(self):
        agent = TriageAgent(mock_model(structured=[an_intent()]))
        with pytest.raises(MockScriptTypeMismatch):
            asyncio.run(agent.classify("Referral received."))

    def test_a_provider_failure_propagates(self):
        agent = TriageAgent(mock_model(structured=[RuntimeError("provider is down")]))
        with pytest.raises(RuntimeError, match="provider is down"):
            asyncio.run(agent.classify("Referral received."))


class TestPlannerAgent:
    def test_returns_the_scripted_intent(self):
        expected = an_intent()
        agent = PlannerAgent(mock_model(structured=[expected]))
        case = make_case(state=CaseState.PLANNING)
        assert asyncio.run(agent.propose(case)) is expected

    def test_asks_for_the_intent_schema_with_the_planner_system_prompt(self):
        model = mock_model(structured=[an_intent()])
        asyncio.run(PlannerAgent(model).propose(make_case(state=CaseState.PLANNING)))
        (call,) = model.calls
        assert call.output_model_name == "PlannerIntent"
        assert call.system_prompt == planner_system_prompt()

    def test_the_user_message_is_the_case_projection(self):
        case = make_case(state=CaseState.PLANNING, facts=(make_fact("patient_id", "PT-4417"),))
        model = mock_model(structured=[an_intent()])
        asyncio.run(PlannerAgent(model).propose(case))
        (call,) = model.calls
        assert call.messages[0]["content"][0]["text"] == planner_user_prompt(case, ())

    def test_prior_denials_reach_the_user_message_as_reason_text_only(self):
        case = make_case(state=CaseState.PLANNING)
        model = mock_model(structured=[an_intent()])
        denials = [("send_email", ReasonCode.DESTINATION_NOT_TRUSTED)]
        asyncio.run(PlannerAgent(model).propose(case, denials))
        user_text = model.calls[0].messages[0]["content"][0]["text"]
        assert "Recipients must come from approved configuration." in user_text
        assert "DESTINATION_NOT_TRUSTED" not in user_text

    def test_offers_the_model_no_tools(self):
        """A tool offered is a tool that could be called. The planner offers none."""
        model = mock_model(structured=[an_intent()])
        asyncio.run(PlannerAgent(model).propose(make_case(state=CaseState.PLANNING)))
        assert model.calls[0].tool_names == ()

    def test_a_wrong_typed_answer_raises(self):
        agent = PlannerAgent(mock_model(structured=[a_triage_result()]))
        with pytest.raises(MockScriptTypeMismatch):
            asyncio.run(agent.propose(make_case(state=CaseState.PLANNING)))


class TestStructuredCall:
    def test_no_output_envelope_raises_rather_than_returning_none(self):
        class SilentModel(MockModel):
            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                yield {"messageStart": {"role": "assistant"}}

        with pytest.raises(NoStructuredOutput):
            asyncio.run(
                structured_call(SilentModel(), TriageResult, system_prompt="s", user_prompt="u")
            )

    def test_a_wrong_typed_envelope_raises_even_when_the_provider_does_not_check(self):
        """Real providers do not have the mock's script-boundary type check; this is for them."""

        class LyingModel(MockModel):
            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                yield {"output": an_intent()}

        with pytest.raises(WrongStructuredOutputType):
            asyncio.run(
                structured_call(LyingModel(), TriageResult, system_prompt="s", user_prompt="u")
            )

    def test_the_type_error_names_types_and_never_values(self):
        class LyingModel(MockModel):
            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                yield {"output": an_intent("send_email", to="attacker@evil.example")}

        with pytest.raises(WrongStructuredOutputType) as caught:
            asyncio.run(
                structured_call(LyingModel(), TriageResult, system_prompt="s", user_prompt="u")
            )
        assert "attacker@evil.example" not in str(caught.value)


class TestAgentsDefaultToTheMock:
    def test_triage_defaults_to_the_mock_when_no_provider_is_configured(self, monkeypatch):
        monkeypatch.delenv("CARELOOP_MODEL_TRIAGE", raising=False)
        assert isinstance(TriageAgent().model, MockModel)

    def test_planner_defaults_to_the_mock_when_no_provider_is_configured(self, monkeypatch):
        monkeypatch.delenv("CARELOOP_MODEL_PLANNER", raising=False)
        assert isinstance(PlannerAgent().model, MockModel)


class TestOneCallSite:
    """Every model call in CareLoop goes through ``agents/invoke.py``."""

    #: ``invoke.py`` makes the call; ``llm/mock.py`` implements the provider side of it.
    ALLOWED = frozenset({AGENT_PACKAGE / "invoke.py", AGENT_PACKAGE.parent / "llm" / "mock.py"})

    def test_no_other_module_under_careloop_names_structured_output(self):
        assert modules_naming(AGENT_PACKAGE.parent, "structured_output", self.ALLOWED) == []

    def test_the_sweep_is_not_vacuous(self, tmp_path):
        """The same sweep, run over a module that does make its own call, must report it."""
        (tmp_path / "rogue.py").write_text(
            "async def go(model, schema):\n    return model.structured_output(schema, [])\n",
            encoding="utf-8",
        )
        assert modules_naming(tmp_path, "structured_output") == ["rogue.py"]

    def test_the_allowed_call_site_really_does_make_the_call(self):
        """And that the sweep is not passing over a package where nobody calls a model at all."""
        source = (AGENT_PACKAGE / "invoke.py").read_text(encoding="utf-8")
        assert "model.structured_output(" in source


class TestAgentsCannotReachAnythingThatActs:
    """Agents propose. The absence of a route to an effect is what makes that structural."""

    FORBIDDEN = (
        "careloop.tools.callables",
        "careloop.tools.executor",
        "careloop.tools.documents",
        "careloop.state.machine",
        "careloop.policy.engine",
        "careloop.policy.loader",
    )

    @pytest.mark.parametrize("module", agent_modules(), ids=lambda path: path.name)
    def test_no_agent_module_imports_a_way_to_act(self, module):
        assert imports_matching(module, self.FORBIDDEN) == set()

    def test_the_import_sweep_is_not_vacuous(self):
        """It must fire against a module that does import one of them."""
        callables = AGENT_PACKAGE.parent / "tools" / "callables.py"
        assert imports_matching(callables, self.FORBIDDEN) != set()

    def test_the_agents_import_the_registry_but_not_the_bodies(self):
        """The catalogue and the intent schema need declarations, never implementations."""
        imported = set().union(*(imported_modules(module) for module in agent_modules()))
        assert "careloop.tools.registry" in imported
        assert "careloop.tools.callables" not in imported

    def test_an_agent_holds_a_model_and_nothing_else(self):
        for agent in (TriageAgent(mock_model()), PlannerAgent(mock_model())):
            assert list(vars(agent)) == ["_model"]

    def test_no_agent_module_imports_bench(self):
        """The benchmark's schemas are a frozen historical artifact, not a dependency."""
        for module in agent_modules():
            assert imports_matching(module, ("bench",)) == set()


class TestExtractionIsDeterministicByDecision:
    """``ModelRole.EXTRACTION`` is reserved and unused. See ``docs/session-log.md``.

    Invariant 1 permits two LLM agents. Wiring a third to this role is the convenient way to break
    it, so "unused" is asserted rather than remembered: the only module allowed to name the role
    is the one that defines it.
    """

    def test_only_the_provider_module_names_the_extraction_role(self):
        allowed = frozenset({AGENT_PACKAGE.parent / "llm" / "providers.py"})
        assert modules_naming(AGENT_PACKAGE.parent, "EXTRACTION", allowed) == []

    def test_the_sweep_is_not_vacuous(self, tmp_path):
        """The same sweep, over a module that does wire a third agent to the role."""
        (tmp_path / "extractor.py").write_text(
            "from careloop.llm.providers import ModelRole\n\nMODEL_ROLE = ModelRole.EXTRACTION\n",
            encoding="utf-8",
        )
        assert modules_naming(tmp_path, "EXTRACTION") == ["extractor.py"]

    def test_the_role_still_exists_so_the_check_is_about_a_real_thing(self):
        from careloop.llm.providers import ModelRole

        assert ModelRole.EXTRACTION.value == "extraction"

    def test_careloop_declares_exactly_two_agents(self):
        import careloop.agents as agents

        exported = {name for name in agents.__all__ if name.endswith("Agent")}
        assert exported == {"TriageAgent", "PlannerAgent"}
