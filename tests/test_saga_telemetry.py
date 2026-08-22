"""Can a recovered case still tell the truth in nine attributes?

``CareLoopSettlementIntegrity`` is frozen. It has been deployed, run in AWS against six real traces,
and its 73 designated mutants are killed; nothing in the saga may change a check, a vocabulary, or
the attribute count. What the saga *does* change is the set of traces CareLoop produces, so the
question this file answers is the compatibility one: **is every valid recovery history representable
in the vocabulary that already exists?**

It is, and one of them was not being represented. Two shapes are new and one was already broken:

``PERFORMED`` — a reconciliation that re-issues the frozen identity really does ``POST``. The write
is in the trace, so the claim that a write is in the trace is true, and the evaluator's
``_dispatch_contradicts_transport`` wants exactly that. **A recovery ``POST`` is never
``SUPPRESSED``**: that value means the case already held the effect and the duplicate branch
returned before the hook, so a trace with an HTTP write beside it is a claim contradicting its own
transport.

``NOT_IN_THIS_TRACE`` — a verification resumed after a crash reads the clinic and dispatches
nothing, which is the value's documented meaning: *the write belongs to an earlier trace*. It was
not what the code tested. ``VerifyNode`` asked whether the case entered ``VERIFYING`` from
``AWAITING_HUMAN``, which is a durable fact standing in for a run-scoped one: a case that entered
``VERIFYING`` from ``EXECUTING`` looks identical whether the execute node ran a moment ago or in a
process that died overnight. So a resumed verification emitted **no effect claim at all**.
Measured against the frozen evaluator before the repair::

    resumed nodes: ('dispatch', 'verify')   state: DONE
    claim attribute count: 5
    effect.dispatch: None
    VERDICT: FAIL

A correct trace — the case reached ``DONE`` through an independent read that matched — judged a
failure, because CareLoop said nothing about an effect while clinic transport sat beside it in the
same trace. The evaluator was right and CareLoop was wrong, which is why the repair is in
``careloop`` and not in ``evaluators``.

**This was reachable before the saga.** ``dispatch -> verify`` has routed a case persisted
mid-verification since long before any of this; nobody had traced that path. The saga makes it
routine, which is what turned a latent defect into one worth a patch.

:class:`TestARecoveredVerificationIsRepresentable` is the repair.
:class:`TestNothingAboutTheEvaluatorMoved` is the half that keeps the claim honest.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from clinic_helpers import clinic_server
from helpers import FIXED_NOW
from saga_helpers import (
    CASE_ID,
    SAGA_EVENT,
    ExplodingPlanner,
    ExplodingTriage,
    executing_with_a_prepared_action,
)
from test_evaluator import (
    ABSENT,
    bedrock_spans,
    graph_span,
    internal_operation_span,
    payload,
    read_span,
)
from test_evaluator import write_span as clinic_write_span
from test_witness_span import (
    careloop_attributes,
    graph_span_of,
    recording_provider,
    strands_tracing,
)

from careloop.graph.nodes import ReconcileNode
from careloop.graph.runner import context_for, run_case
from careloop.graph.witness import EffectDispatch
from careloop.state import CaseState, InMemoryCaseStore
from careloop.tools.clinic import ClinicClient
from evaluators.contract import CheckId, Label
from evaluators.settlement import assess
from scripts.witness_span import SpanAttributeWitness

#: The nine attribute names, so a claim can be rendered with the *missing* ones actually missing.
#:
#: ``test_evaluator.graph_span`` fills any attribute it is not given from a passing default, right
#: for a fixture and wrong here: it would have silently supplied the four effect attributes a
#: broken run omitted and hidden the very failure this file exists to catch. Every name CareLoop did
#: not write is passed as ``ABSENT`` instead, so the span carries exactly what the process emitted.
CLAIM_NAMES = (
    "careloop.effect.type",
    "careloop.effect.dispatch",
    "careloop.effect.fields",
    "careloop.effect.state_digest",
    "careloop.observation.mode",
    "careloop.observation.outcome",
    "careloop.observation.resource_id",
    "careloop.observation.state_digest",
    "careloop.settlement",
)


def exactly_what_was_emitted(claim: dict[str, Any]) -> dict[str, Any]:
    """Render a real claim faithfully, with anything the run omitted left off the span."""
    return {name: claim.get(name, ABSENT) for name in CLAIM_NAMES}


def trace(claim: dict[str, Any], resource: str, *, write: bool) -> list[dict[str, Any]]:
    """The measured span shape around one real claim.

    The transport spans are fixtures for the reason ``tests/test_witness_settlement.py`` states:
    ``opentelemetry-instrumentation-httpx`` ships with the ``observability`` extra rather than with
    ``dev``, so an offline run records no HTTP spans. They carry the **real** confirmation id the
    run produced, so the correlation the evaluator performs is against a value CareLoop did not
    choose.
    """
    spans = [
        internal_operation_span(),
        graph_span(**exactly_what_was_emitted(claim)),
        *bedrock_spans(),
    ]
    if write:
        spans.append(clinic_write_span())
    spans.append(read_span(resource=resource))
    return spans


def judge(spans: list[dict[str, Any]]) -> dict[str, Any]:
    return assess(payload(spans))


def _context(store, clinic, witness):
    return context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        clinic=clinic,
        witness=witness,
        now=lambda: FIXED_NOW,
    )


class TestARecoveryThatReplaysClaimsPerformed:
    """A reconciliation issues a real ``POST``, so it says so."""

    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        provider, exporter = recording_provider()
        with clinic_server() as (base_url, world):
            store = InMemoryCaseStore()
            executing_with_a_prepared_action(store)
            clinic = ClinicClient(base_url)
            with strands_tracing(provider):
                result = asyncio.run(
                    run_case(
                        _context(store, clinic, SpanAttributeWitness()), max_node_executions=12
                    )
                )
            confirmation = world.appointments()[0].confirmation_id
            clinic.close()
        claim = careloop_attributes(graph_span_of(exporter))
        return claim, result, judge(trace(claim, confirmation, write=True))

    def test_the_recovery_really_dispatched(self, judged) -> None:
        _claim, result, _verdict = judged

        assert result.executed_nodes == ("dispatch", "reconcile", "verify")
        assert result.case.state is CaseState.DONE

    def test_the_claim_says_performed_and_not_suppressed(self, judged) -> None:
        """The distinction the brief singles out, and the one an outside reader can check.

        ``SUPPRESSED`` asserts no HTTP write can be in this trace, because the branch that claims it
        returns before the hook and before the gated toolset. A recovery ``POST`` beside that claim
        would be a claim contradicting its own transport, which is a finding rather than a nuance.
        """
        claim, _result, _verdict = judged

        assert claim["careloop.effect.dispatch"] == EffectDispatch.PERFORMED.value
        assert claim["careloop.effect.dispatch"] != EffectDispatch.SUPPRESSED.value

    def test_the_evaluator_passes_it_unchanged(self, judged) -> None:
        _claim, _result, verdict = judged

        assert verdict["label"] == Label.PASS.value

    def test_calling_it_suppressed_would_be_caught(self, judged) -> None:
        """Mutation 6, run rather than described.

        Relabelling the recovery's own claim — changing nothing else, keeping the same real write in
        the same real trace — is refused by the check that reconciles the two.
        """
        claim, _result, _verdict = judged
        lied = {**claim, "careloop.effect.dispatch": EffectDispatch.SUPPRESSED.value}

        verdict = judge(trace(lied, claim["careloop.observation.resource_id"], write=True))

        assert verdict["label"] == Label.FAIL.value
        assert CheckId.DISPATCH_CONTRADICTS_TRANSPORT.value in verdict["explanation"]


class TestARecoveredVerificationIsRepresentable:
    """The compatibility repair. A crash between the ``VERIFYING`` commit and the read."""

    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        """Reconcile in one process, die, and verify in another. One ``GET``, no ``POST``.

        The first process is driven a node at a time so it stops exactly where a crash would leave
        it: the result recorded, the case committed as ``VERIFYING``, and the independent read never
        made. The second is an ordinary instrumented run, and what it executes —
        ``("dispatch", "verify")`` — is the whole of what a resumed verification is.
        """
        provider, exporter = recording_provider()
        with clinic_server() as (base_url, world):
            store = InMemoryCaseStore()
            executing_with_a_prepared_action(store)

            first = ClinicClient(base_url)
            asyncio.run(
                ReconcileNode("reconcile", _context(store, first, SpanAttributeWitness())).run()
            )
            crashed_at = store.get(CASE_ID).state
            first.close()

            second = ClinicClient(base_url)
            with strands_tracing(provider):
                result = asyncio.run(
                    run_case(
                        _context(store, second, SpanAttributeWitness()), max_node_executions=12
                    )
                )
            confirmation = world.appointments()[0].confirmation_id
            second.close()
        claim = careloop_attributes(graph_span_of(exporter))
        return claim, result, crashed_at, judge(trace(claim, confirmation, write=False))

    def test_the_first_process_stopped_mid_verification(self, judged) -> None:
        """The setup assertion. Without it this proves recovery from the wrong crash point."""
        _claim, _result, crashed_at, _verdict = judged

        assert crashed_at is CaseState.VERIFYING

    def test_the_resumed_run_verified_and_finished(self, judged) -> None:
        _claim, result, _crashed_at, _verdict = judged

        assert result.executed_nodes == ("dispatch", "verify")
        assert result.case.state is CaseState.DONE

    def test_it_emits_all_nine_attributes(self, judged) -> None:
        """The repair, stated as the number that was wrong. It emitted five."""
        claim, _result, _crashed_at, _verdict = judged

        assert set(claim) == set(CLAIM_NAMES)

    def test_the_claim_says_the_write_belongs_to_an_earlier_trace(self, judged) -> None:
        claim, _result, _crashed_at, _verdict = judged

        assert claim["careloop.effect.dispatch"] == EffectDispatch.NOT_IN_THIS_TRACE.value

    def test_the_evaluator_passes_it_unchanged(self, judged) -> None:
        """The whole point: a correct recovery trace satisfies the frozen contract."""
        _claim, _result, _crashed_at, verdict = judged

        assert verdict["label"] == Label.PASS.value

    def test_omitting_the_effect_claim_is_what_used_to_fail(self, judged) -> None:
        """The measured before-state, kept as evidence rather than as a sentence in a docstring.

        Strip the four effect attributes from the very claim this run produced and the evaluator
        refuses the trace — which is what it did for every resumed verification until the condition
        in ``VerifyNode`` was widened.
        """
        claim, _result, _crashed_at, _verdict = judged
        silent = {
            name: value for name, value in claim.items() if not name.startswith("careloop.effect.")
        }

        verdict = judge(trace(silent, claim["careloop.observation.resource_id"], write=False))

        assert verdict["label"] == Label.FAIL.value
        assert CheckId.CLAIM_ABSENT.value in verdict["explanation"]

    def test_the_two_digests_have_independent_sources(self, judged) -> None:
        """They agree, and agreeing is only evidence because neither was derived from the other.

        The effect digest is projected from the authorised proposal; the observation digest from
        what the clinic returned to a separate read. A resumed verification re-projects the
        *proposal* for exactly this reason — deriving it from the record or from what was just read
        back would make the equality a tautology.
        """
        claim, _result, _crashed_at, _verdict = judged

        assert claim["careloop.effect.state_digest"] == claim["careloop.observation.state_digest"]


class TestNothingAboutTheEvaluatorMoved:
    """The frozen contract, asserted from this side of the boundary."""

    def test_the_vocabulary_is_still_the_three_dispatch_values(self) -> None:
        assert {member.value for member in EffectDispatch} == {
            "PERFORMED",
            "SUPPRESSED",
            "NOT_IN_THIS_TRACE",
        }

    def test_the_attribute_set_is_still_nine(self) -> None:
        assert len(CLAIM_NAMES) == 9

    def test_careloop_still_spells_no_effect_attribute_of_its_own(self) -> None:
        """Invariant: the attribute names live in the adapter and nowhere in the package."""
        from pathlib import Path

        import careloop

        for module in Path(careloop.__file__).parent.rglob("*.py"):
            assert "careloop.effect" not in module.read_text(encoding="utf-8"), module

    def test_careloop_still_imports_nothing_of_the_evaluators(self) -> None:
        """The direction of the dependency, unchanged by the saga.

        Asserted on the *imports* rather than on the text: ``graph/witness.py`` says in prose that
        it imports no ``evaluators``, and a substring sweep would fail on the sentence promising the
        property it is checking.
        """
        import ast
        from pathlib import Path

        import careloop

        for module in Path(careloop.__file__).parent.rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(
                        alias.name.split(".")[0] == "evaluators" for alias in node.names
                    ), module
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] != "evaluators", module


class TestTheFlagIsTelemetryAndNothingElse:
    """``RunContext.effect_claimed`` may be written at a claim site and read at one more.

    A run-scoped flag is exactly the shape this repository refuses for *routing* — edge conditions
    read the store precisely so control flow cannot depend on something a node happened to set. It
    is admissible here for one reason: nothing that decides anything may read it. The sweep is what
    keeps that true, because the flag's only defence is that no branch consults it.
    """

    @staticmethod
    def _readers_and_writers() -> tuple[list[str], list[str]]:
        import ast
        from pathlib import Path

        import careloop

        reads: list[str] = []
        writes: list[str] = []
        for module in Path(careloop.__file__).parent.rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            relative = str(module.relative_to(Path(careloop.__file__).parent)).replace("\\", "/")
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "effect_claimed":
                            writes.append(relative)
                elif isinstance(node, ast.AnnAssign):
                    target = node.target
                    if isinstance(target, ast.Attribute) and target.attr == "effect_claimed":
                        writes.append(relative)
                elif (
                    isinstance(node, ast.Attribute)
                    and node.attr == "effect_claimed"
                    and isinstance(node.ctx, ast.Load)
                ):
                    reads.append(relative)
        return reads, writes

    def test_only_the_nodes_and_its_own_declaration_write_it(self) -> None:
        _reads, writes = self._readers_and_writers()

        assert set(writes) == {"graph/nodes.py", "graph/context.py"}

    def test_only_the_nodes_read_it(self) -> None:
        reads, _writes = self._readers_and_writers()

        assert set(reads) == {"graph/nodes.py"}

    def test_it_is_written_once_per_claim_site(self) -> None:
        """Four claim sites, four writes. A fifth would be a claim nobody accounts for."""
        _reads, writes = self._readers_and_writers()

        assert writes.count("graph/nodes.py") == 4

    def test_it_is_read_exactly_once(self) -> None:
        """One branch, in the verify node's telemetry tail. A second would be a second decision."""
        reads, _writes = self._readers_and_writers()

        assert len(reads) == 1

    def test_no_policy_tool_or_lifecycle_module_mentions_it(self) -> None:
        from pathlib import Path

        import careloop

        root = Path(careloop.__file__).parent
        for area in ("policy", "tools", "agents", "api", "state"):
            for module in (root / area).rglob("*.py"):
                assert "effect_claimed" not in module.read_text(encoding="utf-8"), module

    def test_the_sweep_would_notice_a_new_reader(self) -> None:
        """Vacuity: a matcher that found nothing would make every assertion above pass."""
        reads, writes = self._readers_and_writers()

        assert reads and writes
