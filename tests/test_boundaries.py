"""The boundary statement is checked against the engine, never against its own arithmetic.

:mod:`careloop.policy.boundaries` reads the rules and reports what they permit. A test that
asserted its output equalled a table written here would prove the two tables match and nothing
else -- so almost everything below drives the **real** :class:`~careloop.policy.engine.PolicyEngine`
over cases built from the projection's own claims, and requires the engine to agree.

Three claims can each be wrong in a way that matters, and each has its own class:

``AUTONOMOUS``
    Overstating it tells a caregiver CareLoop acts alone where a person is really required, which
    is the harmless direction; *understating* it says a person is needed where CareLoop will in
    fact act by itself, which is worse. :class:`TestAutonomyMatchesTheEngine` refuses both by
    requiring the engine to return exactly the verdict the standing claims.

``UNAVAILABLE``
    The load-bearing one, because the page renders it as a promise. It is proven by building the
    strongest case the shipped configuration can legitimately produce -- every trusted fact the
    caregiver configures, a ``CONTROLLED`` fact under **every** key the registry declares, and a
    caregiver's approval covering the exact call -- and requiring that no case state reaches
    ``ALLOW``. Nothing here manufactures a ``TRUSTED`` fact that production cannot produce, because
    doing so would prove a boundary that does not exist.

``required_trust``
    Both directions again, swept per argument: at the level claimed the engine must authorise, and
    one level below it must not. That is what makes the number neither stronger nor weaker than the
    rules -- session J's defect was a rule that named an argument without requiring its trust, and a
    page that printed a requirement the engine did not enforce would be the same mistake rendered.

**The cross-check earned its keep before it was finished.** The obvious expectation for the
"drop the human gate" mutant is that email becomes autonomous. The engine says otherwise: the only
``ALLOW`` for ``send_email`` demands an approval, an approval exists only because a
``REQUIRE_HUMAN`` verdict raised one, so removing the gate deletes the capability rather than
widening it. That is what :attr:`~careloop.policy.boundaries.Unavailability.NO_ROUTE_TO_A_DECISION`
exists for, and a test comparing the projection against a hand-written table would have agreed with
the wrong answer.

:class:`TestTheDerivationIsGeneric` runs the whole thing over a synthetic registry and a synthetic
ruleset that share no tool name with the shipped ones, so the module is proven as a derivation
rather than as a spelling of today's ``rules.yaml``. :class:`TestTheClaimsAreFalsifiable` carries
the mutants.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import yaml
from helpers import FIXED_NOW, make_case, make_fact, trusted_fact

from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass
from careloop.graph.trusted import DEMO_CAREGIVER
from careloop.policy import PolicyEngine, RuleSet, Verdict, default_rules
from careloop.policy.boundaries import (
    ArgumentBoundary,
    Boundaries,
    Capability,
    CapabilityStanding,
    RefusalPrinciple,
    Unavailability,
    UnclassifiedCapability,
    boundaries,
    check_every_external_effect_is_describable,
    required_trust,
)
from careloop.policy.loader import RULES_PATH, load_rules_from_mapping
from careloop.provenance import Fact, TrustLevel
from careloop.state import Case, CaseState
from careloop.state.case import Approval, ApprovalStatus
from careloop.tools import REGISTRY
from careloop.tools.idempotency import args_digest
from careloop.tools.registry import ArgClass, ArgSpec, ToolRegistry, ToolSpec

DESTINATIONS = DEMO_CAREGIVER.destinations

#: Every state an effect rule permits, so a claim is tested where the rules can actually fire.
EFFECT_STATES = (CaseState.PLANNING, CaseState.READY_TO_EXECUTE, CaseState.EXECUTING)

#: One level down from each, for the "not weaker than the rules" half of the trust sweep.
BELOW: Mapping[TrustLevel, TrustLevel] = {
    TrustLevel.CONTROLLED: TrustLevel.UNTRUSTED,
    TrustLevel.TRUSTED: TrustLevel.CONTROLLED,
}


# -- building a legitimate call out of the projection's own claims --------------------------------


def synthetic_value(argument: ArgumentBoundary) -> str:
    """A plausible value for an argument the caregiver's configuration does not supply.

    Derived from the argument rather than hard-coded, so the builder works over a synthetic
    registry as readily as over the shipped one.
    """
    return f"{argument.trust_key or argument.name}-established-by-this-case"


def legitimate_call(
    capability: Capability,
    destinations: tuple[tuple[str, str], ...] = DESTINATIONS,
    *,
    trust_overrides: Mapping[str, TrustLevel] = {},
) -> tuple[dict[str, str], tuple[Fact, ...]]:
    """The best case a caregiver could honestly present for one capability.

    Every argument gets the value the projection says it must have -- the authored literal, the
    configured destination, or a value the case establishes -- and every fact that value needs is
    recorded at exactly the level the projection claims is required. ``trust_overrides`` lowers one
    argument's fact so the sweep can ask what the engine does one level down.

    Arguments with no required trust get plain text and no fact, which is the honest rendering of
    "the rules say nothing about this" and is what the free-text fields really are.

    It **refuses** a capability with unmet trust keys. Building one would mean seeding a ``TRUSTED``
    fact under a key nothing configures, which production cannot do -- and a test that did it would
    be proving a boundary by assuming it away. :func:`strongest_reachable_case` is what those are
    asked with instead.
    """
    if capability.unmet_trust_keys:
        raise AssertionError(
            f"{capability.tool_name} needs {capability.unmet_trust_keys} at TRUSTED and nothing "
            f"configures it; use strongest_reachable_case rather than manufacturing the fact"
        )

    args: dict[str, str] = {}
    facts: list[Fact] = [trusted_fact(key, value) for key, value in destinations]

    for argument in capability.arguments:
        if argument.authored_value is not None:
            args[argument.name] = argument.authored_value
            continue
        if argument.required_trust is None:
            args[argument.name] = f"free text for {argument.name}"
            continue

        level = trust_overrides.get(argument.name, argument.required_trust)
        if argument.configured_value is not None:
            args[argument.name] = argument.configured_value
            if level is not TrustLevel.TRUSTED:
                # The configured value is already seeded TRUSTED above, and trust resolution takes
                # the *lowest* level among facts sharing the key and value -- so recording a second
                # fact at the lower level is exactly how a downgrade is expressed.
                facts.append(make_fact(argument.trust_key, argument.configured_value, level))
            continue

        value = synthetic_value(argument)
        args[argument.name] = value
        if argument.trust_key is not None and level is not TrustLevel.UNTRUSTED:
            facts.append(make_fact(argument.trust_key, value, level))

    return args, tuple(facts)


def approval_for(tool_name: str, args: Mapping[str, str]) -> Approval:
    """A granted caregiver approval covering exactly this call."""
    return Approval(
        approval_id="apr-1",
        tool_name=tool_name,
        args_digest=args_digest(args),
        reason_code="REQUIRES_HUMAN_APPROVAL",
        status=ApprovalStatus.APPROVED,
        requested_at=FIXED_NOW,
        decided_at=FIXED_NOW,
        decided_by="a caregiver",
    )


def case_for(
    facts: tuple[Fact, ...],
    state: CaseState = CaseState.PLANNING,
    approvals: tuple[Approval, ...] = (),
) -> Case:
    return make_case(state=state, facts=facts).model_copy(update={"approvals": approvals})


def shipped() -> Boundaries:
    return boundaries(default_rules(), REGISTRY, DESTINATIONS)


def engine_for(ruleset: RuleSet, registry: ToolRegistry = REGISTRY) -> PolicyEngine:
    return PolicyEngine(ruleset, registry)


def capabilities_where(
    standing: CapabilityStanding, statement: Boundaries | None = None
) -> list[Capability]:
    found = [
        capability
        for capability in (statement or shipped()).capabilities
        if capability.standing is standing
    ]
    assert found, f"no capability stands {standing}; the sweep below would be vacuous"
    return found


# -- the three claims, each against the engine ----------------------------------------------------


class TestAutonomyMatchesTheEngine:
    """What the page says about who decides is what the engine actually decides."""

    def test_an_autonomous_capability_is_allowed_with_nobody_asked(self) -> None:
        for capability in capabilities_where(CapabilityStanding.AUTONOMOUS):
            args, facts = legitimate_call(capability)
            for state in EFFECT_STATES:
                decision = engine_for(default_rules()).evaluate(
                    capability.tool_name, args, case_for(facts, state)
                )
                assert decision.verdict is Verdict.ALLOW, (
                    f"{capability.tool_name} is projected AUTONOMOUS but the engine answered "
                    f"{decision.verdict} in {state} with no approval held"
                )

    def test_a_human_gated_capability_stops_for_a_person(self) -> None:
        for capability in capabilities_where(CapabilityStanding.REQUIRES_HUMAN):
            args, facts = legitimate_call(capability)
            decision = engine_for(default_rules()).evaluate(
                capability.tool_name, args, case_for(facts)
            )
            assert decision.verdict is Verdict.REQUIRE_HUMAN, (
                f"{capability.tool_name} is projected REQUIRES_HUMAN but the engine answered "
                f"{decision.verdict} with no approval held"
            )

    def test_a_human_gated_capability_proceeds_on_the_exact_approval(self) -> None:
        for capability in capabilities_where(CapabilityStanding.REQUIRES_HUMAN):
            args, facts = legitimate_call(capability)
            case = case_for(facts, approvals=(approval_for(capability.tool_name, args),))
            decision = engine_for(default_rules()).evaluate(capability.tool_name, args, case)
            assert decision.verdict is Verdict.ALLOW, (
                f"{capability.tool_name} is projected REQUIRES_HUMAN, so an approval for these "
                f"exact arguments must release it; the engine answered {decision.verdict}"
            )

    def test_nothing_autonomous_is_also_human_gated(self) -> None:
        """The two standings are claims about the same question and cannot both be true."""
        for capability in shipped().capabilities:
            if capability.standing is not CapabilityStanding.AUTONOMOUS:
                continue
            args, facts = legitimate_call(capability)
            decision = engine_for(default_rules()).evaluate(
                capability.tool_name, args, case_for(facts)
            )
            assert decision.verdict is not Verdict.REQUIRE_HUMAN


def strongest_reachable_case(
    capability: Capability,
    state: CaseState,
    destinations: tuple[tuple[str, str], ...] = DESTINATIONS,
) -> tuple[dict[str, str], Case]:
    """Everything a real case could hold, short of trust production cannot make.

    Trusted facts for every destination the caregiver configures, a ``CONTROLLED`` fact under
    **every** key the registry declares -- which is the most validation can ever earn -- and a
    caregiver's approval covering the exact call. ``TRUSTED`` is deliberately not synthesised for an
    unmet key: that is precisely the thing the boundary says nothing can supply, and supplying it
    would be assuming the answer.
    """
    configured = dict(destinations)
    args = {
        argument.name: (
            argument.authored_value or argument.configured_value or synthetic_value(argument)
        )
        for argument in capability.arguments
    }
    facts = [trusted_fact(key, value) for key, value in destinations]
    facts += [
        make_fact(argument.trust_key, args[argument.name], TrustLevel.CONTROLLED)
        for argument in capability.arguments
        if argument.trust_key is not None and argument.trust_key not in configured
    ]
    return args, case_for(
        tuple(facts), state, approvals=(approval_for(capability.tool_name, args),)
    )


class TestUnavailabilityIsAPromise:
    """Nothing this configuration can legitimately produce reaches an unavailable capability."""

    def test_no_state_and_no_approval_reaches_allow(self) -> None:
        for capability in capabilities_where(CapabilityStanding.UNAVAILABLE):
            for state in CaseState:
                args, case = strongest_reachable_case(capability, state)
                decision = engine_for(default_rules()).evaluate(capability.tool_name, args, case)
                assert decision.verdict is not Verdict.ALLOW, (
                    f"{capability.tool_name} is projected UNAVAILABLE but reached ALLOW in {state} "
                    f"from facts the shipped configuration can legitimately produce"
                )

    def test_the_reason_names_a_key_nothing_configures(self) -> None:
        configured = dict(DESTINATIONS)
        for capability in capabilities_where(CapabilityStanding.UNAVAILABLE):
            if capability.unavailable_because is not Unavailability.NO_TRUSTED_SOURCE:
                continue
            assert capability.unmet_trust_keys
            for key in capability.unmet_trust_keys:
                assert key not in configured

    def test_the_shipped_grant_path_is_the_one_that_is_unavailable(self) -> None:
        """The derivation reproduces, from data, what the repository proves separately.

        ``tests/test_typed_trust.py::TestTheGrantPathHasNoProductionAuthority`` establishes that no
        producer emits a fact under ``role``. Nothing here reads that test; both look at the same
        configuration and must agree, which is the point of asserting it in two places.
        """
        unavailable = {
            capability.tool_name: capability
            for capability in capabilities_where(CapabilityStanding.UNAVAILABLE)
        }
        assert set(unavailable) == {"grant_portal_access"}
        assert unavailable["grant_portal_access"].unmet_trust_keys == ("role",)


class TestRequiredTrustIsNeitherStrongerNorWeaker:
    """Each claimed level is exactly where the engine's answer changes."""

    def constrained(self) -> list[tuple[Capability, ArgumentBoundary]]:
        found = [
            (capability, argument)
            for capability in shipped().capabilities
            if capability.standing is not CapabilityStanding.UNAVAILABLE
            for argument in capability.arguments
            if argument.required_trust is not None
        ]
        assert found
        return found

    def verdict_for(self, capability: Capability, overrides: Mapping[str, TrustLevel]) -> Verdict:
        args, facts = legitimate_call(capability, trust_overrides=overrides)
        approvals = (
            (approval_for(capability.tool_name, args),)
            if capability.standing is CapabilityStanding.REQUIRES_HUMAN
            else ()
        )
        return (
            engine_for(default_rules())
            .evaluate(capability.tool_name, args, case_for(facts, approvals=approvals))
            .verdict
        )

    def test_at_the_claimed_level_the_engine_authorises(self) -> None:
        for capability, argument in self.constrained():
            assert self.verdict_for(capability, {}) is Verdict.ALLOW, (
                f"{capability.tool_name}.{argument.name} is projected to require "
                f"{argument.required_trust}, and a call meeting every projected requirement was "
                f"not authorised -- the projection is stronger than the rules"
            )

    def test_one_level_below_the_engine_refuses(self) -> None:
        for capability, argument in self.constrained():
            lowered = BELOW[argument.required_trust]
            assert self.verdict_for(capability, {argument.name: lowered}) is not Verdict.ALLOW, (
                f"{capability.tool_name}.{argument.name} is projected to require "
                f"{argument.required_trust} but the engine authorised it at {lowered} -- the "
                f"projection is weaker than the rules"
            )

    def test_an_unconstrained_argument_really_is_unconstrained(self) -> None:
        """``None`` says the rules require no *trust* of the argument, and means it.

        Checked by varying the value rather than a fact, because there is no fact to vary: an
        argument with no ``trust_key`` draws trust from nothing at all.
        """
        checked = 0
        for capability in shipped().capabilities:
            if capability.standing is CapabilityStanding.UNAVAILABLE:
                continue
            for argument in capability.arguments:
                if argument.required_trust is not None or argument.authored_value is not None:
                    continue
                args, facts = legitimate_call(capability)
                args[argument.name] = "anything at all, from anywhere at all"
                approvals = (
                    (approval_for(capability.tool_name, args),)
                    if capability.standing is CapabilityStanding.REQUIRES_HUMAN
                    else ()
                )
                decision = engine_for(default_rules()).evaluate(
                    capability.tool_name, args, case_for(facts, approvals=approvals)
                )
                assert decision.verdict is Verdict.ALLOW
                checked += 1
        assert checked, "no unconstrained argument was exercised; the claim would be vacuous"


class TestTheAuthoredValueIsWhatTheRulesDemand:
    """A ``SYSTEM``-authored argument is reported, and the engine really requires that literal."""

    def authored(self) -> list[tuple[Capability, ArgumentBoundary]]:
        found = [
            (capability, argument)
            for capability in shipped().capabilities
            for argument in capability.arguments
            if argument.authored_value is not None
        ]
        assert found
        return found

    def test_the_projected_literal_is_the_one_that_is_authorised(self) -> None:
        for capability, argument in self.authored():
            args, facts = legitimate_call(capability)
            assert args[argument.name] == argument.authored_value
            assert (
                engine_for(default_rules())
                .evaluate(capability.tool_name, args, case_for(facts))
                .verdict
                is Verdict.ALLOW
            )

    def test_anything_else_in_that_field_is_refused(self) -> None:
        for capability, argument in self.authored():
            args, facts = legitimate_call(capability)
            args[argument.name] = "a sentence a model wrote"
            assert (
                engine_for(default_rules())
                .evaluate(capability.tool_name, args, case_for(facts))
                .verdict
                is not Verdict.ALLOW
            )


class TestTheStatementDescribesWhatItSaysItDescribes:
    """The parts that are reads rather than derivations, checked against their sources."""

    def test_only_external_effects_are_described(self) -> None:
        assert {capability.tool_name for capability in shipped().capabilities} == set(
            REGISTRY.external_effect_tools
        )

    def test_observation_and_recovery_come_from_their_own_tables(self) -> None:
        from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE

        for capability in shipped().capabilities:
            assert capability.independently_observed == (
                capability.tool_name in INDEPENDENTLY_OBSERVABLE
            )
            assert capability.recovery_class is RECOVERY_CLASS[capability.tool_name]

    def test_a_configured_value_is_the_caregivers_own(self) -> None:
        configured = dict(DESTINATIONS)
        for capability in shipped().capabilities:
            for argument in capability.arguments:
                if argument.configured_value is None:
                    continue
                assert configured[argument.trust_key] == argument.configured_value

    def test_a_second_caregiver_gets_their_own_destinations(self) -> None:
        """The statement is per caregiver, which is what session X made true of trust itself."""
        theirs = (
            ("caregiver_email", "someone.else@carer.example"),
            ("provider_id", "PROV-SOUTHGATE-RENAL"),
            ("portal_id", "PORTAL-SOUTHGATE-RECORDS"),
        )
        statement = boundaries(default_rules(), REGISTRY, theirs)
        values = {
            argument.configured_value
            for capability in statement.capabilities
            for argument in capability.arguments
            if argument.configured_value is not None
        }
        assert values == {value for _, value in theirs}
        assert not values & {value for _, value in DESTINATIONS}

    def test_the_refusals_are_the_ones_this_configuration_implies(self) -> None:
        assert shipped().refusals == (
            RefusalPrinciple.UNTRUSTED_DESTINATION,
            RefusalPrinciple.UNEARNED_VALUE_TRUST,
            RefusalPrinciple.DOCUMENT_AUTHORITY,
            RefusalPrinciple.SECOND_RECORDED_EFFECT,
        )


# -- the same derivation over configuration it has never seen -------------------------------------

ALPHA = ToolSpec(
    name="tool_alpha",
    description="An autonomous effect.",
    external_effect=True,
    args=(
        ArgSpec(name="target", arg_class=ArgClass.DESTINATION, trust_key="alpha_target"),
        ArgSpec(name="subject_id", arg_class=ArgClass.VALUE, trust_key="alpha_subject"),
    ),
)
BETA = ToolSpec(
    name="tool_beta",
    description="An effect a person must release.",
    external_effect=True,
    args=(ArgSpec(name="target", arg_class=ArgClass.DESTINATION, trust_key="beta_target"),),
)
GAMMA = ToolSpec(
    name="tool_gamma",
    description="An effect nothing configured can authorise.",
    external_effect=True,
    args=(
        ArgSpec(name="target", arg_class=ArgClass.DESTINATION, trust_key="gamma_target"),
        ArgSpec(name="power", arg_class=ArgClass.AUTHORITY, trust_key="gamma_power"),
    ),
)

SYNTHETIC_REGISTRY = ToolRegistry([ALPHA, BETA, GAMMA])

SYNTHETIC_RULES: dict[str, Any] = {
    "version": 1,
    "rules": [
        {
            "id": "deny-alpha-untrusted-target",
            "effect": "DENY",
            "tools": ["tool_alpha"],
            "when": {"arg_trust_below": [{"arg": "target", "level": "TRUSTED"}]},
            "reason_code": "DESTINATION_NOT_TRUSTED",
            "diagnostic": "alpha target below TRUSTED",
        },
        {
            "id": "require-human-for-beta",
            "effect": "REQUIRE_HUMAN",
            "tools": ["tool_beta"],
            "when": {"args_approved": False},
            "reason_code": "REQUIRES_HUMAN_APPROVAL",
            "diagnostic": "beta always asks",
        },
        {
            "id": "allow-alpha",
            "effect": "ALLOW",
            "tools": ["tool_alpha"],
            "when": {
                "case_state_in": ["PLANNING", "READY_TO_EXECUTE", "EXECUTING"],
                "arg_trust_at_least": [
                    {"arg": "target", "level": "TRUSTED"},
                    {"arg": "subject_id", "level": "CONTROLLED"},
                ],
            },
            "diagnostic": "alpha on a trusted target and a validated subject",
        },
        {
            "id": "allow-beta",
            "effect": "ALLOW",
            "tools": ["tool_beta"],
            "when": {
                "case_state_in": ["PLANNING", "READY_TO_EXECUTE", "EXECUTING"],
                "args_approved": True,
                "arg_trust_at_least": [{"arg": "target", "level": "TRUSTED"}],
            },
            "diagnostic": "beta once a person has released it",
        },
        {
            "id": "allow-gamma",
            "effect": "ALLOW",
            "tools": ["tool_gamma"],
            "when": {
                "case_state_in": ["PLANNING", "READY_TO_EXECUTE", "EXECUTING"],
                "arg_trust_at_least": [
                    {"arg": "target", "level": "TRUSTED"},
                    {"arg": "power", "level": "TRUSTED"},
                ],
            },
            "diagnostic": "gamma needs a configured power",
        },
    ],
}

#: Configuration for the synthetic world: two of the three destinations, and no ``gamma_power``.
SYNTHETIC_DESTINATIONS = (
    ("alpha_target", "ALPHA-TARGET-1"),
    ("beta_target", "BETA-TARGET-1"),
    ("gamma_target", "GAMMA-TARGET-1"),
)

SYNTHETIC_OBSERVABLE = frozenset({"tool_alpha"})
SYNTHETIC_RECOVERY: Mapping[str, RecoveryClass] = {
    "tool_alpha": RecoveryClass.IDEMPOTENT_REPLAYABLE,
    "tool_beta": RecoveryClass.NOT_REPLAYABLE,
    "tool_gamma": RecoveryClass.NOT_REPLAYABLE,
}


def synthetic_statement() -> Boundaries:
    return boundaries(
        load_rules_from_mapping(SYNTHETIC_RULES, SYNTHETIC_REGISTRY),
        SYNTHETIC_REGISTRY,
        SYNTHETIC_DESTINATIONS,
        observable=SYNTHETIC_OBSERVABLE,
        recovery=SYNTHETIC_RECOVERY,
    )


class TestTheDerivationIsGeneric:
    """It reports a configuration it has never seen, and the engine agrees about that one too."""

    def statement(self) -> dict[str, Capability]:
        return {
            capability.tool_name: capability for capability in synthetic_statement().capabilities
        }

    def test_each_synthetic_tool_lands_where_its_rules_put_it(self) -> None:
        standings = {name: c.standing for name, c in self.statement().items()}
        assert standings == {
            "tool_alpha": CapabilityStanding.AUTONOMOUS,
            "tool_beta": CapabilityStanding.REQUIRES_HUMAN,
            "tool_gamma": CapabilityStanding.UNAVAILABLE,
        }
        assert self.statement()["tool_gamma"].unmet_trust_keys == ("gamma_power",)

    def test_the_synthetic_engine_agrees(self) -> None:
        ruleset = load_rules_from_mapping(SYNTHETIC_RULES, SYNTHETIC_REGISTRY)
        engine = engine_for(ruleset, SYNTHETIC_REGISTRY)
        for capability in synthetic_statement().capabilities:
            if capability.standing is CapabilityStanding.UNAVAILABLE:
                args, case = strongest_reachable_case(
                    capability, CaseState.PLANNING, SYNTHETIC_DESTINATIONS
                )
                assert (
                    engine.evaluate(capability.tool_name, args, case).verdict is not Verdict.ALLOW
                )
                continue

            args, facts = legitimate_call(capability, SYNTHETIC_DESTINATIONS)
            unapproved = engine.evaluate(capability.tool_name, args, case_for(facts)).verdict
            approved = engine.evaluate(
                capability.tool_name,
                args,
                case_for(facts, approvals=(approval_for(capability.tool_name, args),)),
            ).verdict

            if capability.standing is CapabilityStanding.AUTONOMOUS:
                assert unapproved is Verdict.ALLOW
            else:
                assert unapproved is Verdict.REQUIRE_HUMAN
                assert approved is Verdict.ALLOW

    def test_required_trust_is_read_from_the_synthetic_rules(self) -> None:
        alpha = self.statement()["tool_alpha"]
        levels = {argument.name: argument.required_trust for argument in alpha.arguments}
        assert levels == {
            "target": TrustLevel.TRUSTED,
            "subject_id": TrustLevel.CONTROLLED,
        }

    def test_observation_and_recovery_follow_the_tables_they_are_given(self) -> None:
        statement = self.statement()
        assert statement["tool_alpha"].independently_observed
        assert not statement["tool_beta"].independently_observed
        assert statement["tool_alpha"].recovery_class is RecoveryClass.IDEMPOTENT_REPLAYABLE


class TestConstraintsAreNotInventedFromPartialCoverage:
    """A rule that leaves an argument unconstrained is reported as leaving it unconstrained.

    Session J's shape, asked of the projection rather than of the rules: with two authorising paths
    where only one names an argument, the weaker path is the one that can fire, so claiming the
    stronger one's requirement would print a guard that is not there.
    """

    def two_paths(self) -> RuleSet:
        rules = dict(SYNTHETIC_RULES)
        rules["rules"] = [
            *SYNTHETIC_RULES["rules"],
            {
                "id": "allow-alpha-without-a-validated-subject",
                "effect": "ALLOW",
                "tools": ["tool_alpha"],
                "when": {
                    "case_state_in": ["PLANNING", "READY_TO_EXECUTE", "EXECUTING"],
                    "arg_trust_at_least": [{"arg": "target", "level": "TRUSTED"}],
                },
                "diagnostic": "a second, weaker path",
            },
        ]
        return load_rules_from_mapping(rules, SYNTHETIC_REGISTRY)

    def test_the_unconstrained_argument_is_reported_unconstrained(self) -> None:
        assert required_trust(self.two_paths(), "tool_alpha", "subject_id") is None

    def test_and_the_engine_really_does_authorise_without_it(self) -> None:
        ruleset = self.two_paths()
        statement = boundaries(
            ruleset,
            SYNTHETIC_REGISTRY,
            SYNTHETIC_DESTINATIONS,
            observable=SYNTHETIC_OBSERVABLE,
            recovery=SYNTHETIC_RECOVERY,
        )
        alpha = next(c for c in statement.capabilities if c.tool_name == "tool_alpha")
        args, facts = legitimate_call(alpha, SYNTHETIC_DESTINATIONS)
        decision = engine_for(ruleset, SYNTHETIC_REGISTRY).evaluate(
            "tool_alpha", args, case_for(facts)
        )
        assert decision.verdict is Verdict.ALLOW
        assert decision.arg_trust["subject_id"] is TrustLevel.UNTRUSTED

    def test_a_conditional_deny_is_not_read_as_a_universal_floor(self) -> None:
        """A ``DENY`` that fires only in some states says nothing about every path."""
        rules = dict(SYNTHETIC_RULES)
        rules["rules"] = [
            {
                "id": "deny-beta-target-while-new",
                "effect": "DENY",
                "tools": ["tool_beta"],
                "when": {
                    "case_state_in": ["NEW"],
                    "arg_trust_below": [{"arg": "target", "level": "TRUSTED"}],
                },
                "reason_code": "DESTINATION_NOT_TRUSTED",
                "diagnostic": "only while NEW",
            },
            *SYNTHETIC_RULES["rules"],
        ]
        ruleset = load_rules_from_mapping(rules, SYNTHETIC_REGISTRY)
        # allow-beta still requires TRUSTED, so the level is unchanged -- what is being asserted is
        # that the conditional DENY contributed nothing, which the alpha subject makes visible.
        assert required_trust(ruleset, "tool_beta", "target") is TrustLevel.TRUSTED


# -- the mutants ----------------------------------------------------------------------------------


def shipped_rules_raw() -> dict[str, Any]:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def without_rule(rule_id: str) -> RuleSet:
    raw = shipped_rules_raw()
    raw["rules"] = [rule for rule in raw["rules"] if rule["id"] != rule_id]
    return load_rules_from_mapping(raw, REGISTRY)


class TestTheClaimsAreFalsifiable:
    """Each mutant moves exactly the claim it should, and the tests above are what notice."""

    def email(self, ruleset: RuleSet) -> Capability:
        statement = boundaries(ruleset, REGISTRY, DESTINATIONS)
        return next(c for c in statement.capabilities if c.tool_name == "send_email")

    def test_dropping_the_email_human_gate_leaves_it_unreachable_rather_than_autonomous(
        self,
    ) -> None:
        """The mutant that would most harm a caregiver, and the answer the engine actually gives.

        The obvious expectation is *"email becomes autonomous"*, and it is wrong. The only ``ALLOW``
        for ``send_email`` demands ``args_approved: true``, and an approval exists only because a
        ``REQUIRE_HUMAN`` verdict raised one -- so removing the gate does not widen the capability,
        it deletes it. The projection says so, and the engine below confirms it, which is the whole
        reason this file drives the engine rather than comparing two tables.
        """
        mutated = without_rule("require-human-for-outbound-email")
        email = self.email(mutated)
        assert email.standing is CapabilityStanding.UNAVAILABLE
        assert email.unavailable_because is Unavailability.NO_ROUTE_TO_A_DECISION

        # Asked without an approval, deliberately. An approval is not something a test may hand a
        # case here: it exists only because a REQUIRE_HUMAN verdict raised one, and the verdict
        # below is what shows none ever would. Granting one anyway would prove the capability
        # reachable by assuming the very step the mutant removed.
        args, facts = legitimate_call(email)
        decision = engine_for(mutated).evaluate("send_email", args, case_for(facts))
        assert decision.verdict is Verdict.DENY
        assert decision.verdict is not Verdict.REQUIRE_HUMAN

        # The shipped configuration says something different, which is what the sweeps assert.
        assert self.email(default_rules()).standing is CapabilityStanding.REQUIRES_HUMAN

    def test_ignoring_the_approval_clause_collapses_the_standing(self) -> None:
        """``args_approved`` is the clause the two standings turn on."""
        raw = shipped_rules_raw()
        for rule in raw["rules"]:
            rule.get("when", {}).pop("args_approved", None)
        assert self.email(load_rules_from_mapping(raw, REGISTRY)).standing is (
            CapabilityStanding.UNAVAILABLE
        )

    def test_configuring_a_role_would_make_the_grant_path_available(self) -> None:
        """Availability tracks the caregiver's configuration and nothing else."""
        with_role = (*DESTINATIONS, ("role", "records_viewer"))
        statement = boundaries(default_rules(), REGISTRY, with_role)
        grant = next(c for c in statement.capabilities if c.tool_name == "grant_portal_access")
        assert grant.standing is CapabilityStanding.REQUIRES_HUMAN
        assert grant.unmet_trust_keys == ()

    def test_omitting_a_destination_removes_the_value_and_the_availability(self) -> None:
        """Dropping a destination understates, and does so visibly rather than silently."""
        without_provider = tuple(
            (key, value) for key, value in DESTINATIONS if key != "provider_id"
        )
        statement = boundaries(default_rules(), REGISTRY, without_provider)
        booking = next(c for c in statement.capabilities if c.tool_name == "book_appointment")
        assert booking.standing is CapabilityStanding.UNAVAILABLE
        assert booking.unmet_trust_keys == ("provider_id",)

    def test_weakening_the_patient_clause_to_presence_drops_the_requirement(self) -> None:
        """Session J's mutant, asked of the projection: the page stops claiming the guard."""
        raw = shipped_rules_raw()
        for rule in raw["rules"]:
            if rule["id"] == "deny-booking-for-unverified-patient":
                rule["when"]["arg_trust_below"] = [
                    {"arg": "patient_id", "level": "UNTRUSTED", "arg_class": "VALUE"}
                ]
            if rule["id"] == "allow-booking-on-validated-slot":
                rule["when"]["arg_trust_at_least"] = [
                    predicate
                    for predicate in rule["when"]["arg_trust_at_least"]
                    if predicate["arg"] != "patient_id"
                ]
                rule["when"]["args_present"] = ["patient_id"]
        statement = boundaries(load_rules_from_mapping(raw, REGISTRY), REGISTRY, DESTINATIONS)
        booking = next(c for c in statement.capabilities if c.tool_name == "book_appointment")
        patient = next(a for a in booking.arguments if a.name == "patient_id")
        assert patient.required_trust is TrustLevel.UNTRUSTED
        # The shipped configuration claims the real one.
        shipped_patient = next(
            a
            for c in shipped().capabilities
            if c.tool_name == "book_appointment"
            for a in c.arguments
            if a.name == "patient_id"
        )
        assert shipped_patient.required_trust is TrustLevel.CONTROLLED


class TestTheCoherenceCheckHasAFailurePath:
    """The import-time guard is driven over a table it should refuse."""

    def test_an_unclassified_external_effect_is_refused(self) -> None:
        with pytest.raises(UnclassifiedCapability) as raised:
            check_every_external_effect_is_describable(
                SYNTHETIC_REGISTRY, {"tool_alpha": RecoveryClass.NOT_REPLAYABLE}
            )
        assert "tool_beta" in str(raised.value)

    def test_the_shipped_tables_pass_it(self) -> None:
        check_every_external_effect_is_describable()
