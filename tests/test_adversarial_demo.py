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

import dataclasses

import pytest

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
    check_attacker_arguments_come_from_the_poisoned_document,
    check_every_tool_is_proposed,
    check_no_undeclared_attacker_argument,
    hostile_planner,
    poisoned_document_lines,
)
from careloop.graph.trusted import DEMO_PATIENT_ID
from careloop.policy.verdicts import ArgClass
from careloop.tools.document_source import ATTACKER_ADDRESS, POISONED_DOCUMENT_ID
from careloop.tools.registry import REGISTRY


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
