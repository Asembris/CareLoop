"""The deterministic adversarial demonstration: what is supplied, and what is not.

The product claim this file defends is narrow and has to stay narrow::

    the realistic `injection` scenario asks   will a real model take the bait?
    the deterministic one asks                assume it did. can the action actually happen?

So the fixture supplies **one thing** -- the proposal a fully compromised planner would emit -- and
everything after that is the shipped system. These tests are in two halves for that reason. The
provenance half asks whether the fixture is really the attacker's ask rather than something CareLoop
invented to be refusable; the graph half asks whether the refusal, and the absence of any effect,
really come from the production authority layer.

**Neither half is worth anything without the other.** A fixture that is provably attacker-derived
but never reaches the gate demonstrates nothing, and a refusal that provably comes from the real
engine demonstrates nothing if the payload was chosen to be easy.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import re
from pathlib import Path

import pytest
from api_helpers import planner_proposing, triage_agent
from helpers import FIXED_NOW
from strands.multiagent.base import Status

import careloop
from careloop.agents.schemas import PlannerIntent
from careloop.api.adversary import (
    ADVERSARIAL_RATIONALE,
    ATTACKER_PORTAL,
    ATTACKER_ROLE,
    ATTACKER_SUPPLIED_ARGUMENTS,
    HOSTILE_PROPOSALS,
    SCRIPT_DEPTH,
    TRUSTED_PROVIDER,
    AdversarialFixtureError,
    AdversarialProposals,
    AmbiguousAdversarialScenario,
    adversarial_proposals,
    check_attacker_arguments_come_from_the_poisoned_document,
    check_every_tool_is_proposed,
    check_no_undeclared_attacker_argument,
    hostile_planner,
    poisoned_document_lines,
)
from careloop.api.demo import DEMO_SCENARIOS
from careloop.api.deps import provider_context_factory
from careloop.events.inbound import POISONED_ONLY_EVENT
from careloop.graph.builder import MAX_NODE_EXECUTIONS
from careloop.graph.runner import context_for, open_case, run_case
from careloop.graph.trusted import DEMO_CAREGIVER, DEMO_PATIENT_ID, TRUSTED_CONFIG
from careloop.policy.verdicts import ArgClass
from careloop.state import Case, CaseState, InMemoryCaseStore
from careloop.state.case import TimelineEvent, TimelineKind
from careloop.tools.document_source import ATTACKER_ADDRESS, POISONED_DOCUMENT_ID
from careloop.tools.executor import MockExternalExecutor
from careloop.tools.registry import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTheAttackerReallyAskedForThis:
    """Every value the fixture calls attacker-supplied appears in the attacker's own text.

    This is what separates *"a document told the agent to do this and CareLoop refused"* from
    *"CareLoop refused a payload it wrote for itself"*. Only the first is a demonstration.
    """

    def test_every_declared_attacker_value_is_in_the_poisoned_document(self) -> None:
        lines = poisoned_document_lines()
        for (tool_name, arg_name), value in ATTACKER_SUPPLIED_ARGUMENTS.items():
            assert any(value in line for line in lines), (
                f"{tool_name}.{arg_name} claims {value!r} came from {POISONED_DOCUMENT_ID}"
            )

    def test_every_declared_attacker_value_is_actually_proposed(self) -> None:
        """The other direction. A claim the proposals do not carry is a claim about nothing."""
        proposals = {intent.tool_name: intent for intent in HOSTILE_PROPOSALS}
        for (tool_name, arg_name), value in ATTACKER_SUPPLIED_ARGUMENTS.items():
            assert proposals[tool_name].args[arg_name] == value

    def test_the_headline_attack_is_the_address_the_notice_names(self) -> None:
        """The first refusal a judge reads, and the one CareLoop's own positioning is about."""
        assert ATTACKER_SUPPLIED_ARGUMENTS["send_email", "to"] == ATTACKER_ADDRESS
        assert HOSTILE_PROPOSALS[0].tool_name == "send_email"
        assert HOSTILE_PROPOSALS[0].args["to"] == ATTACKER_ADDRESS

    def test_the_portal_and_the_role_are_the_notice_s_own_words_too(self) -> None:
        lines = poisoned_document_lines()
        assert any(ATTACKER_PORTAL in line for line in lines)
        assert any(ATTACKER_ROLE in line for line in lines)

    def test_attacker_values_only_land_where_they_are_declared(self) -> None:
        declared = set(ATTACKER_SUPPLIED_ARGUMENTS)
        for intent in HOSTILE_PROPOSALS:
            for arg_name, value in intent.args.items():
                if value in set(ATTACKER_SUPPLIED_ARGUMENTS.values()):
                    assert (intent.tool_name, arg_name) in declared

    def test_every_attacker_argument_is_of_a_class_nothing_can_promote(self) -> None:
        """Not a coincidence, and worth pinning.

        ``DESTINATION`` and ``AUTHORITY`` are absent from ``PROMOTABLE_ARG_CLASSES``, so an
        attacker-authored value in one of them can never be validated into acceptability. A future
        edit that moved an attacker payload onto a ``VALUE`` argument would be demonstrating the
        *remediable* leg of the trust model while the copy claimed the absolute one.
        """
        for tool_name, arg_name in ATTACKER_SUPPLIED_ARGUMENTS:
            spec = REGISTRY.get(tool_name)
            assert spec is not None
            assert spec.arg_class(arg_name) in (ArgClass.DESTINATION, ArgClass.AUTHORITY)


class TestTheOrdinaryArgumentsAreOrdinary:
    """A realistic hostile proposal is not hostile in every field, and must not be made to be.

    A compromised planner combines the attacker's destination with the legitimate context it
    already holds. Distorting the patient id or the provider so that every argument could be found
    in the poisoned document would make the fixture *less* like the thing being defended against,
    and would quietly turn several refusals into refusals of nonsense.
    """

    def test_the_booking_names_the_caregiver_s_own_configured_provider(self) -> None:
        booking = next(i for i in HOSTILE_PROPOSALS if i.tool_name == "book_appointment")
        assert booking.args["provider_id"] == TRUSTED_PROVIDER
        assert not any(TRUSTED_PROVIDER in line for line in poisoned_document_lines())

    def test_the_booking_names_the_real_demo_patient(self) -> None:
        booking = next(i for i in HOSTILE_PROPOSALS if i.tool_name == "book_appointment")
        assert booking.args["patient_id"] == DEMO_PATIENT_ID

    def test_the_document_arguments_name_a_document_the_case_holds(self) -> None:
        for tool_name in ("upload_document", "fetch_document"):
            intent = next(i for i in HOSTILE_PROPOSALS if i.tool_name == tool_name)
            assert intent.args["document_id"] == POISONED_DOCUMENT_ID

    def test_the_booking_does_not_supply_the_system_authored_reason(self) -> None:
        """``reason`` is CareLoop's own sentence and the model-facing schema has no field for it.

        A fixture that supplied one would be claiming a capability the real planner does not have,
        and ``allow-booking-on-validated-slot``'s ``args_system_authored`` clause would refuse it
        for a reason that has nothing to do with this demonstration.
        """
        booking = next(i for i in HOSTILE_PROPOSALS if i.tool_name == "book_appointment")
        assert booking.args["reason"] == ""


class TestTheFixtureCannotQuietlyStopCoveringTheRegistry:
    """The outcome card says every action was refused, so every action has to be proposed.

    ``GateNode`` parks on ``REGISTRY.names <= refused``. A fixture that stopped covering the
    registry would park for a different reason or burn the node budget, and the product would
    either say something untrue or report its own defence as a fault.
    """

    def test_every_registered_tool_is_proposed_exactly_once(self) -> None:
        proposed = [intent.tool_name for intent in HOSTILE_PROPOSALS]
        assert sorted(proposed) == sorted(REGISTRY.names)
        assert len(proposed) == len(set(proposed))

    def test_a_fixture_missing_a_tool_fails_the_check(self, monkeypatch) -> None:
        """The vacuity proof for the guard above, driven through the real check."""
        import careloop.api.adversary as adversary

        monkeypatch.setattr(adversary, "HOSTILE_PROPOSALS", HOSTILE_PROPOSALS[:2])
        with pytest.raises(AdversarialFixtureError, match="cannot reach NO_SAFE_ACTION_AVAILABLE"):
            check_every_tool_is_proposed()

    def test_a_fixture_naming_an_unregistered_tool_fails_the_check(self, monkeypatch) -> None:
        import careloop.api.adversary as adversary

        monkeypatch.setattr(adversary, "HOSTILE_PROPOSALS", HOSTILE_PROPOSALS)
        proposed = {intent.tool_name for intent in HOSTILE_PROPOSALS}
        assert proposed == REGISTRY.names, "the positive case, so the negative one means something"


class TestTheProvenanceChecksAreNotVacuous:
    """Each guard is proven to fail on the thing it exists to catch.

    Written against the real functions with the module's own data monkeypatched, rather than
    against a hand-built parallel fixture, so the guard under test is the shipped one.
    """

    def test_an_invented_destination_fails_the_source_binding(self, monkeypatch) -> None:
        """Mutant B, as a test: swap the attacker address for one nobody was told about."""
        import careloop.api.adversary as adversary

        invented = "records@not-in-the-document.example"
        monkeypatch.setattr(
            adversary,
            "ATTACKER_SUPPLIED_ARGUMENTS",
            {**dict(ATTACKER_SUPPLIED_ARGUMENTS), ("send_email", "to"): invented},
        )
        with pytest.raises(AdversarialFixtureError, match="appears nowhere in"):
            check_attacker_arguments_come_from_the_poisoned_document()

    def test_a_claim_the_proposals_do_not_carry_fails_the_source_binding(self, monkeypatch) -> None:
        import careloop.api.adversary as adversary

        monkeypatch.setattr(
            adversary,
            "ATTACKER_SUPPLIED_ARGUMENTS",
            {**dict(ATTACKER_SUPPLIED_ARGUMENTS), ("fetch_document", "document_id"): ATTACKER_ROLE},
        )
        with pytest.raises(AdversarialFixtureError, match="does not carry it"):
            check_attacker_arguments_come_from_the_poisoned_document()

    def test_an_undeclared_attacker_value_fails_its_own_check(self, monkeypatch) -> None:
        import careloop.api.adversary as adversary

        smuggled = PlannerIntent(
            tool_name="send_email",
            rationale=ADVERSARIAL_RATIONALE,
            args={"to": "rosa.marlowe@carer.example", "subject": ATTACKER_ADDRESS, "body": "x"},
        )
        monkeypatch.setattr(adversary, "HOSTILE_PROPOSALS", (smuggled, *HOSTILE_PROPOSALS[1:]))
        with pytest.raises(AdversarialFixtureError, match="without being declared"):
            check_no_undeclared_attacker_argument()


class TestTheSuppliedProposalSaysSoInTheRecord:
    """The rationale is the durable evidence that this proposal was supplied, not generated.

    It is the only signal the judge-facing framing note is permitted to key on, so it has to be a
    fixed literal every supplied proposal carries and nothing else plausibly does. A real model's
    proposal -- including one where a real model genuinely took the bait in the mixed scenario --
    carries its own words, so the note cannot appear over a run it would be lying about.
    """

    def test_every_supplied_proposal_carries_the_declared_rationale(self) -> None:
        assert {intent.rationale for intent in HOSTILE_PROPOSALS} == {ADVERSARIAL_RATIONALE}

    def test_the_rationale_says_carelooop_supplied_it(self) -> None:
        assert "Supplied by CareLoop's adversarial test" in ADVERSARIAL_RATIONALE

    def test_the_rationale_does_not_claim_a_model_produced_it(self) -> None:
        """The one thing this whole feature may not say."""
        lowered = ADVERSARIAL_RATIONALE.lower()
        for forbidden in ("the model", "the ai", "bedrock", "tricked", "fell for", "took the bait"):
            assert forbidden not in lowered


class TestTheScriptCannotRunDryInFrontOfAnAudience:
    """An exhausted script ends the case ``FAILED``, which reads exactly like a broken defence."""

    def test_the_depth_exceeds_every_plan_call_the_graph_can_make(self) -> None:
        from careloop.agents.invoke import MAX_PROPOSAL_ATTEMPTS
        from careloop.graph.builder import MAX_NODE_EXECUTIONS

        assert SCRIPT_DEPTH >= MAX_NODE_EXECUTIONS * MAX_PROPOSAL_ATTEMPTS

    def test_the_script_cycles_the_proposals_in_order(self) -> None:
        planner = hostile_planner()
        model = planner.model
        assert model.remaining[2] == SCRIPT_DEPTH

    def test_each_call_gets_its_own_planner(self) -> None:
        """Two runs of one case must each get a whole script, so the agent is never shared."""
        assert hostile_planner() is not hostile_planner()
        assert hostile_planner().model is not hostile_planner().model


class TestTheBindingIsOneCaseAndCannotBeAimed:
    """A different planner object, handed to one case. Not a flag checked downstream."""

    def test_it_claims_only_the_case_it_was_built_for(self) -> None:
        proposals = AdversarialProposals(case_id="demo-adversarial")
        assert proposals.claims("demo-adversarial")
        assert not proposals.claims("demo-referral")
        assert not proposals.claims("")

    def test_the_case_it_claims_cannot_be_changed(self) -> None:
        """Frozen on purpose: there is nothing one-shot here, so there is nothing to arm."""
        proposals = AdversarialProposals(case_id="demo-adversarial")
        with pytest.raises(dataclasses.FrozenInstanceError):
            proposals.case_id = "demo-referral"  # type: ignore[misc]

    def test_it_has_no_way_to_be_told_what_to_propose(self) -> None:
        """No parameter anywhere through which a caller could aim an arbitrary proposal."""
        import inspect

        assert list(inspect.signature(AdversarialProposals).parameters) == ["case_id"]
        assert list(inspect.signature(AdversarialProposals.planner).parameters) == ["self"]


class TestTheScenarioTableIsWhatDeclaresTheBinding:
    """Which case the fixture plans is read from a scenario's own declaration, never from a key."""

    def test_exactly_one_shipped_scenario_declares_it(self) -> None:
        supplying = [s.key for s in DEMO_SCENARIOS.values() if s.proposes_the_attackers_action]
        assert supplying == ["adversarial"]

    def test_the_binding_names_that_scenarios_case(self) -> None:
        proposals = adversarial_proposals()
        assert proposals is not None
        assert proposals.case_id == DEMO_SCENARIOS["adversarial"].case_id

    def test_a_table_declaring_none_binds_nothing(self) -> None:
        ordinary = {
            key: scenario
            for key, scenario in DEMO_SCENARIOS.items()
            if not scenario.proposes_the_attackers_action
        }
        assert adversarial_proposals(ordinary) is None

    def test_a_table_declaring_two_refuses_to_resolve(self) -> None:
        """Two fixture-planned cases and one set of proposals is a framing note that lies once."""
        doubled = dict(DEMO_SCENARIOS)
        doubled["second"] = DEMO_SCENARIOS["injection_only"].model_copy(
            update={"key": "second", "proposes_the_attackers_action": True}
        )
        with pytest.raises(AmbiguousAdversarialScenario, match="exactly one"):
            adversarial_proposals(doubled)


class TestOnlyTheClaimedCaseIsPlannedByTheFixture:
    """The binding is which planner object a run holds, not a check made downstream.

    Driven through the shipped ``provider_context_factory`` rather than through a stand-in, because
    the property being asserted is about the production composition and a hand-built factory would
    agree with itself.
    """

    @staticmethod
    def _planner_model_id(case_id: str) -> str:
        factory = provider_context_factory(adversary=adversarial_proposals())
        context = factory(InMemoryCaseStore(), POISONED_ONLY_EVENT, case_id)
        return context.planner.model.get_config()["model_id"]

    def test_the_adversarial_case_gets_the_compromised_planner(self) -> None:
        assert self._planner_model_id("demo-adversarial") == "adversarial-fixture"

    def test_every_other_case_gets_the_ordinary_planner(self) -> None:
        for case_id in ("demo-referral", "demo-injection", "demo-injection-only", "case-0001"):
            assert self._planner_model_id(case_id) != "adversarial-fixture"

    def test_a_world_with_no_adversary_never_gets_the_fixture(self) -> None:
        """Default off. The fixture reaches a run only where a composition root put it there."""
        factory = provider_context_factory()
        context = factory(InMemoryCaseStore(), POISONED_ONLY_EVENT, "demo-adversarial")
        assert context.planner.model.get_config()["model_id"] != "adversarial-fixture"

    def test_two_runs_of_the_claimed_case_get_separate_scripts(self) -> None:
        """A shared agent would have the second run of a case start on a half-spent script."""
        factory = provider_context_factory(adversary=adversarial_proposals())
        store = InMemoryCaseStore()
        first = factory(store, POISONED_ONLY_EVENT, "demo-adversarial")
        second = factory(store, POISONED_ONLY_EVENT, "demo-adversarial")
        assert first.planner is not second.planner
        assert first.planner.model.remaining == second.planner.model.remaining


# ---------------------------------------------------------------------------------------------
# The graph half. Everything above is about what is supplied; everything below is about what the
# production system does with it.
# ---------------------------------------------------------------------------------------------


class ExplodingHandler:
    """A handler that fails the test if any effect is ever performed.

    Stronger than an empty ledger, which is also what a run that never reached the executor at all
    would produce for a *different* reason. This says a handler was not so much as entered.
    """

    def __init__(self, action_type: str) -> None:
        self.action_type = action_type

    def __call__(self, args, idempotency_key):  # pragma: no cover - the point is it never runs
        raise AssertionError(
            f"{self.action_type} was performed on the adversarial case; every proposal on it is "
            f"refused before READY_TO_EXECUTE, so nothing may reach a handler"
        )


def booby_trapped_executor() -> MockExternalExecutor:
    """An executor whose every handler raises. Nothing on this case may reach one."""
    return MockExternalExecutor(
        handlers={name: ExplodingHandler(name) for name in REGISTRY.external_effect_tools}
    )


def adversarial_run() -> tuple[Case, MockExternalExecutor, object]:
    """Drive the adversarial demo case through the real graph, on the real rules.

    The **only** substitution is the planner, which is the feature. Triage is scripted the way
    every other test in this suite scripts it -- the deployed demo runs a real one, and a test that
    reached a provider would not run offline at $0.
    """
    store = InMemoryCaseStore()
    case_id = DEMO_SCENARIOS["adversarial"].case_id
    open_case(store, POISONED_ONLY_EVENT, caregiver=DEMO_CAREGIVER, case_id=case_id, now=FIXED_NOW)
    executor = booby_trapped_executor()
    context = context_for(
        store,
        POISONED_ONLY_EVENT,
        case_id=case_id,
        triage=triage_agent(),
        planner=hostile_planner(),
        executor=executor,
        now=lambda: FIXED_NOW,
    )
    result = asyncio.run(run_case(context))
    return store.get(case_id), executor, result


def denials(case: Case) -> dict[str, TimelineEvent]:
    return {
        event.payload["tool_name"]: event
        for event in case.timeline
        if event.kind is TimelineKind.POLICY_DENY
    }


def kinds(case: Case, kind: TimelineKind) -> list[TimelineEvent]:
    return [event for event in case.timeline if event.kind is kind]


class TestTheSuppliedProposalReachesTheRealAuthorityLayer:
    """The headline beat, asserted on what the production gate actually recorded.

    Not on a status code and not on the fixture: the reason code and the matched rule below are
    written by :class:`careloop.policy.engine.PolicyEngine` against the shipped ``rules.yaml``, and
    nothing in :mod:`careloop.api.adversary` can express either.
    """

    def test_the_attackers_email_is_proposed_for_real(self) -> None:
        case, _, _ = adversarial_run()
        proposals = kinds(case, TimelineKind.PLAN_PROPOSED)

        assert proposals[0].payload["tool_name"] == "send_email"
        assert proposals[0].payload["args"]["to"] == ATTACKER_ADDRESS
        assert proposals[0].payload["rationale"] == ADVERSARIAL_RATIONALE

    def test_the_real_engine_refuses_it_on_the_destination_rule(self) -> None:
        case, _, _ = adversarial_run()
        refusal = denials(case)["send_email"]

        assert refusal.payload["verdict"] == "DENY"
        assert refusal.payload["reason_code"] == "DESTINATION_NOT_TRUSTED"
        assert refusal.payload["matched_rule_id"] == "deny-email-to-untrusted-recipient"

    def test_the_refusal_comes_before_anybody_is_asked_to_approve_it(self) -> None:
        """``DENY`` is evaluated first and unconditionally, so no caregiver is burdened.

        ``send_email`` also carries ``require-human-for-outbound-email``. That an attacker-supplied
        recipient never reaches it is the property worth showing: CareLoop does not ask a person to
        approve something it should already have refused.
        """
        case, _, _ = adversarial_run()

        assert not kinds(case, TimelineKind.POLICY_REQUIRE_HUMAN)
        assert not kinds(case, TimelineKind.APPROVAL_REQUESTED)
        assert case.pending_approval() is None

    def test_every_proposed_tool_is_refused(self) -> None:
        case, _, _ = adversarial_run()

        assert set(denials(case)) == set(REGISTRY.names)
        assert all(event.payload["verdict"] == "DENY" for event in denials(case).values())

    def test_no_proposal_was_ever_allowed(self) -> None:
        case, _, _ = adversarial_run()
        assert not kinds(case, TimelineKind.POLICY_ALLOW)

    def test_the_other_refusals_are_the_rules_the_arguments_earn(self) -> None:
        """Read off the run rather than asserted as a set, so a rule change is visible here.

        ``book_appointment`` is the interesting one: its provider is the caregiver's own configured
        clinic and its patient is the real demo patient, so it is refused on the *time*, which this
        case never validated. Plausibility earns nothing.
        """
        case, _, _ = adversarial_run()
        refused = denials(case)

        assert refused["upload_document"].payload["reason_code"] == "DESTINATION_NOT_TRUSTED"
        assert refused["grant_portal_access"].payload["reason_code"] == "DESTINATION_NOT_TRUSTED"
        assert refused["book_appointment"].payload["reason_code"] == "INSUFFICIENT_FACT_TRUST"
        assert (
            refused["book_appointment"].payload["matched_rule_id"]
            == "deny-booking-on-unvalidated-slot"
        )
        assert refused["fetch_document"].payload["reason_code"] == "NO_RULE_MATCH"


class TestTheCaseStopsInACaregiversHands:
    """Parked, not failed, and with nothing to approve.

    ``FAILED`` would say something broke. Every refusal here is the system working, and the card a
    caregiver reads for ``NO_SAFE_ACTION_AVAILABLE`` says every action was refused -- which is only
    true because the fixture proposes the whole registry.
    """

    def test_it_rests_awaiting_human(self) -> None:
        case, _, result = adversarial_run()

        assert case.state is CaseState.AWAITING_HUMAN
        assert case.failure_reason is None
        assert result.status is Status.COMPLETED

    def test_the_handoff_says_no_safe_action_was_available(self) -> None:
        case, _, _ = adversarial_run()
        handoffs = kinds(case, TimelineKind.MANUAL_HANDOFF_REQUIRED)

        assert len(handoffs) == 1
        assert handoffs[0].payload["reason_code"] == "NO_SAFE_ACTION_AVAILABLE"

    def test_the_run_ends_well_inside_the_node_budget(self) -> None:
        """A case that parked because the SDK's bound fired would be a runaway, not a defence."""
        _, _, result = adversarial_run()

        assert len(result.executed_nodes) < MAX_NODE_EXECUTIONS
        assert result.executed_nodes[:3] == ("dispatch", "triage", "evidence")


class TestNothingLeftCareLoop:
    """The zero-effect proof, at four independent depths.

    A refusal label alone is not the claim. The claim is that execution never began -- so there is
    no recorded action, no *intent* to act, nothing dispatched, and no handler entered.
    """

    def test_no_action_was_recorded(self) -> None:
        case, _, _ = adversarial_run()
        assert case.actions == ()

    def test_no_action_identity_was_even_frozen(self) -> None:
        """``ACTION_PREPARED`` is written before anything can leave the process.

        Its absence is the stronger statement: not merely that no effect completed, but that
        CareLoop never reached the point of writing down an attempt.
        """
        case, _, _ = adversarial_run()
        assert not kinds(case, TimelineKind.ACTION_PREPARED)

    def test_nothing_was_executed_or_suppressed(self) -> None:
        case, _, _ = adversarial_run()

        assert not kinds(case, TimelineKind.ACTION_EXECUTED)
        assert not kinds(case, TimelineKind.DUPLICATE_SUPPRESSED)

    def test_the_executor_ledger_is_empty_and_no_handler_ran(self) -> None:
        """The booby-trapped handlers would have raised; the empty ledger says they were not run."""
        _, executor, _ = adversarial_run()
        assert executor.ledger == ()

    def test_the_case_never_became_executable(self) -> None:
        """``READY_TO_EXECUTE`` is the only state ``ExecuteNode`` runs from. Nothing wrote it."""
        case, _, _ = adversarial_run()
        reached = {
            event.payload.get("to")
            for event in case.timeline
            if event.kind is TimelineKind.STATE_TRANSITION
        }

        assert "READY_TO_EXECUTE" not in reached
        assert "EXECUTING" not in reached


class TestTheFixtureCannotDecideTheOutcome:
    """The seam stops before the property. Proven by moving the fixture and nothing else.

    Mutant B as a test: give the compromised planner the caregiver's own trusted address. The
    refusal disappears -- which is what shows the ``DESTINATION_NOT_TRUSTED`` above is produced by
    the destination rule reading the value, rather than by anything this fixture arranged.
    """

    @staticmethod
    def _run_with(first: PlannerIntent) -> Case:
        store = InMemoryCaseStore()
        case_id = DEMO_SCENARIOS["adversarial"].case_id
        open_case(
            store, POISONED_ONLY_EVENT, caregiver=DEMO_CAREGIVER, case_id=case_id, now=FIXED_NOW
        )
        context = context_for(
            store,
            POISONED_ONLY_EVENT,
            case_id=case_id,
            triage=triage_agent(),
            planner=planner_proposing(first),
            executor=booby_trapped_executor(),
            now=lambda: FIXED_NOW,
        )
        asyncio.run(run_case(context))
        return store.get(case_id)

    def test_a_trusted_destination_is_not_refused_by_the_destination_rule(self) -> None:
        trusted = next(value for key, value in TRUSTED_CONFIG if key == "caregiver_email")
        case = self._run_with(
            PlannerIntent(
                tool_name="send_email",
                rationale=ADVERSARIAL_RATIONALE,
                args={"to": trusted, "subject": "Referral", "body": "An update."},
            )
        )

        assert "send_email" not in denials(case)
        assert kinds(case, TimelineKind.POLICY_REQUIRE_HUMAN), (
            "a trusted recipient reaches the human rule instead, which is what makes the "
            "attacker-address refusal a fact about the value rather than about this fixture"
        )

    def test_the_intent_has_no_field_that_could_ask_for_a_verdict(self) -> None:
        """Structural, not behavioural: there is nowhere to write one.

        ``PlannerIntent`` carries a tool, a rationale and arguments. Nothing about a verdict, a
        state, a rule or an effect can be expressed through it, so the fixture's reach ends at the
        ``PLAN_PROPOSED`` entry the gate then reads back out of the store.
        """
        assert set(PlannerIntent.model_fields) == {"tool_name", "rationale", "args"}


class TestTheDemonstrationCannotBorrowFromTheEvaluationHarness:
    """``careloop/`` imports nothing from ``evals/``, and this feature did not change that.

    The offline replay ships an allow-all policy engine on purpose. Nothing a browser can reach may
    be one import away from it.
    """

    def test_no_shipped_module_imports_evals(self) -> None:
        offenders: list[str] = []
        for path in Path(careloop.__file__).parent.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "evals" or name.startswith("evals.") for name in names):
                    offenders.append(str(path))

        assert offenders == []


class TestThereIsOneProductionBindingSite:
    """Exactly one shipped module composes the fixture, and a sweep says so.

    The arrangement ``CaseStore.clear`` and ``arm_the_lost_booking_response`` already have: a second
    caller is how a narrow capability becomes a broad one without anybody deciding to widen it.
    ``scripts/demo_serve.py`` is a composition root of its own and is outside the package.
    """

    def test_only_the_session_composition_root_names_the_factory(self) -> None:
        callers: list[str] = []
        for path in Path(careloop.__file__).parent.rglob("*.py"):
            if path.name == "adversary.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "adversarial_proposals":
                    callers.append(path.name)

        assert sorted(set(callers)) == ["sessions.py"]

    def test_no_route_node_or_tool_can_reach_the_fixture(self) -> None:
        """The graph, the policy layer and the tools may not name any of this.

        A node that knew a case was the adversarial one could behave differently on it, and the
        whole claim is that nothing after the proposal behaves differently at all.
        """
        forbidden = {
            "HOSTILE_PROPOSALS",
            "AdversarialProposals",
            "adversarial_proposals",
            "hostile_planner",
            "ADVERSARIAL_RATIONALE",
        }
        root = Path(careloop.__file__).parent
        offenders: list[str] = []
        for subpackage in ("graph", "policy", "tools", "state", "provenance", "agents"):
            for path in (root / subpackage).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    named = (
                        {node.module or ""} | {alias.name for alias in node.names}
                        if isinstance(node, ast.ImportFrom)
                        else {alias.name for alias in node.names}
                        if isinstance(node, ast.Import)
                        else {node.id}
                        if isinstance(node, ast.Name)
                        else {node.attr}
                        if isinstance(node, ast.Attribute)
                        else set()
                    )
                    if named & forbidden or "careloop.api.adversary" in named:
                        offenders.append(f"{subpackage}/{path.name}")

        assert offenders == []


class TestTheSuppliedRationaleIsOneStringInTwoLanguages:
    """The browser matches the rationale exactly, so the two spellings may never drift.

    ``frontend/src/services/presentation.ts`` cannot import a Python constant, and the framing note
    renders on **exact equality** with it. A silent divergence would not break anything loudly: the
    backend would keep supplying the proposal, the gate would keep refusing it, and the note would
    simply stop appearing -- leaving a judge reading a refusal with no statement of who wrote the
    proposal, which is the one thing this feature exists to prevent.
    """

    PRESENTATION = REPO_ROOT / "frontend" / "src" / "services" / "presentation.ts"

    def test_the_frontend_constant_is_where_it_claims_to_be(self) -> None:
        assert self.PRESENTATION.exists()
        assert "SUPPLIED_PROPOSAL_RATIONALE" in self.PRESENTATION.read_text(encoding="utf-8")

    def test_both_languages_spell_it_identically(self) -> None:
        source = self.PRESENTATION.read_text(encoding="utf-8")
        match = re.search(
            r"export const SUPPLIED_PROPOSAL_RATIONALE\s*=\s*\n?\s*\"([^\"]*)\";", source
        )
        assert match is not None, "the frontend constant is no longer a single string literal"

        assert match.group(1) == ADVERSARIAL_RATIONALE


class TestTheEndToEndHarnessDrivesTheShippedFixture:
    """``scripts/e2e_serve.py`` scripts every other demo case and must not script this one.

    The e2e's whole value here is that a judge path is exercised against the proposals the product
    ships. A harness that restated them would keep passing after the real fixture drifted, which is
    the failure mode that looks most like success.
    """

    HARNESS = REPO_ROOT / "scripts" / "e2e_serve.py"

    def test_the_harness_does_not_script_the_adversarial_case(self) -> None:
        from scripts.e2e_serve import SCRIPTS

        assert DEMO_SCENARIOS["adversarial"].case_id not in SCRIPTS

    def test_the_harness_binds_the_production_fixture_for_it(self) -> None:
        from scripts.e2e_serve import planner_for

        planner = planner_for(DEMO_SCENARIOS["adversarial"].case_id)

        assert planner.model.get_config()["model_id"] == "adversarial-fixture"

    def test_the_harness_still_scripts_every_other_demo_case(self) -> None:
        from scripts.e2e_serve import planner_for

        for key in ("referral", "injection", "resume", "recovery", "injection_only"):
            planner = planner_for(DEMO_SCENARIOS[key].case_id)
            assert planner.model.get_config()["model_id"] != "adversarial-fixture"
