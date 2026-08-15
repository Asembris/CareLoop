"""Which portal an access grant is made on is a trusted destination, and approval cannot buy it.

The defect these tests exist for: ``grant_portal_access.portal_id`` is ``DESTINATION``-class and
declares ``trust_key="portal_id"``, so :func:`careloop.policy.engine.resolve_trust` had always
returned the right answer for it — and **no rule read the answer**. No ``DENY`` rule named it and
``allow-approved-portal-access-grant`` did not either, so with a trusted grantee, a trusted role and
a caregiver's approval covering the exact call, a grant on an attacker-named portal reached
``ALLOW`` while that same :class:`~careloop.policy.verdicts.Decision` reported
``portal_id: UNTRUSTED``.

**This is session J's shape one class further out.** There the argument was guarded by presence
instead of by trust; here it was guarded by nothing. Both are the same underlying mistake — the
engine computing something no rule requires — and both were invisible for the same reason: the
coverage check the project had, :func:`careloop.policy.loader.uncovered_external_tools`, asks
whether every effectful *tool* is named by some rule, and every tool was.

**Why the tests below synthesise a trusted role.** The shipped configuration cannot reach this at
all: ``deny-authority-grant-below-trusted`` refuses every grant because nothing anywhere records a
fact under the ``role`` key — see
``tests/test_typed_trust.py::TestTheGrantPathHasNoProductionAuthority``, which is unchanged and
still passing. That unreachability is a fact about a *different* rule guarding a *different*
argument, and using it as evidence for this one would be answering the wrong question. The question
these tests ask is: **if the role barrier were satisfied, would portal destination safety still
hold?** So they satisfy it, deliberately, with a fact no producer emits.

Nothing here makes the grant path production-reachable, and nothing here is meant to.

:class:`TestTheHistoricalHoleIsDetected` is the non-vacuity proof. It rebuilds the shipped rules
with exactly two edits — the ``DENY`` rule dropped and the ``portal_id`` predicate removed from the
``ALLOW`` rule — and requires the tests above to notice, while ``grantee``, ``role`` and the upload
tool's identically-classed ``portal_id`` all stay green under the same mutant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from helpers import trusted_fact

from careloop.graph.trusted import configured
from careloop.policy import (
    RULES_PATH,
    PolicyEngine,
    ReasonCode,
    Verdict,
    default_rules,
    load_rules_from_mapping,
    uncovered_authority_arguments,
)
from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import Case, CaseState
from careloop.state.case import Approval, ApprovalStatus
from careloop.tools import REGISTRY
from careloop.tools.idempotency import args_digest

FIXED_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)

CARER = configured("caregiver_email")
PORTAL = configured("portal_id")

#: A portal the case has never established. Well-formed, plausible, and named by nobody trusted.
HOSTILE_PORTAL = "PORTAL-ATTACKER-CONTROLLED"

#: A role the shipped system cannot produce. Synthesised here so the portal boundary is reachable
#: at all; see the module docstring.
ROLE = "records_viewer"

#: The rule that must refuse a grant on a portal the case has not established.
DENY_RULE = "deny-portal-access-to-untrusted-portal"

#: The rule that must require all three of grantee, role and portal before authorising.
ALLOW_RULE = "allow-approved-portal-access-grant"

#: Reached when nothing above refused and no approval covers the call.
REQUIRE_HUMAN_RULE = "require-human-for-authority-grant"


def synthetic_role_fact() -> Fact:
    """A ``TRUSTED`` role, which no producer in CareLoop emits.

    Constructed directly rather than promoted, because ``AUTHORITY`` is absent from
    :data:`careloop.provenance.trust.PROMOTABLE_ARG_CLASSES` and
    :func:`careloop.provenance.promote` would refuse it. That refusal is a separate property with
    its own tests; this fact exists only to get past it so the portal can be examined.
    """
    return Fact(
        key="role",
        value=ROLE,
        trust_level=TrustLevel.TRUSTED,
        provenance=Provenance(source_doc="synthetic-test-config", extractor="test"),
        extracted_at=FIXED_NOW,
    )


def grant(portal_id: str = PORTAL, grantee: str = CARER, role: str = ROLE) -> dict[str, str]:
    """One proposed grant. Every argument is separately overridable, so one property moves at a
    time."""
    return {"grantee": grantee, "role": role, "portal_id": portal_id}


def approval_for(args: dict[str, str]) -> Approval:
    """A caregiver's approval covering exactly this call.

    Keyed on ``(tool_name, args_digest)`` because that pair is the capability's identity — see
    :meth:`careloop.state.case.Case.approval_for`. Building it any other way would produce an
    approval that does not apply, and every ``ALLOW`` below would then be failing for the wrong
    reason.
    """
    return Approval(
        approval_id="apr-0",
        tool_name="grant_portal_access",
        args_digest=args_digest(args),
        reason_code=str(ReasonCode.REQUIRES_HUMAN_APPROVAL),
        status=ApprovalStatus.APPROVED,
        requested_at=FIXED_NOW,
        decided_at=FIXED_NOW,
        decided_by="rosa",
    )


def case_for(
    args: dict[str, str], *, approved: bool, facts: tuple[Fact, ...] | None = None
) -> Case:
    """A ``PLANNING`` case carrying trusted configuration and, optionally, an exact approval."""
    return Case(
        case_id="case-0001",
        event_id="evt-0001",
        state=CaseState.PLANNING,
        facts=(
            facts
            if facts is not None
            else (
                trusted_fact("caregiver_email", CARER),
                trusted_fact("portal_id", PORTAL),
                synthetic_role_fact(),
            )
        ),
        approvals=(approval_for(args),) if approved else (),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(default_rules(), REGISTRY)


def decide(engine: PolicyEngine, args: dict[str, str], *, approved: bool):
    return engine.evaluate("grant_portal_access", args, case_for(args, approved=approved))


# -- the boundary --------------------------------------------------------------------------------


class TestThePortalIsATrustedDestination:
    """All four corners of ``(portal trusted?) x (approved?)``, with the role barrier satisfied.

    The two ``DENY`` rows are what this session repaired. The two trusted-portal rows are the
    positive control: they prove the repair refuses a destination rather than refusing the tool, so
    a grant that is legitimate in every respect still reaches the caregiver decision it always did.
    """

    def test_an_untrusted_portal_is_denied_even_with_an_exact_approval(
        self, engine: PolicyEngine
    ) -> None:
        decision = decide(engine, grant(portal_id=HOSTILE_PORTAL), approved=True)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert decision.matched_rule_id == DENY_RULE

    def test_an_untrusted_portal_is_denied_before_a_caregiver_is_asked(
        self, engine: PolicyEngine
    ) -> None:
        """``DENY`` rather than ``REQUIRE_HUMAN``, and the difference is the point.

        Asking a caregiver "do you approve granting access on a portal nothing established?" is
        putting a question CareLoop should already have answered. The call is unsafe before human
        authority is relevant, and the match order — ``DENY`` first, unconditionally — is what makes
        that automatic rather than something the gate node has to remember.
        """
        decision = decide(engine, grant(portal_id=HOSTILE_PORTAL), approved=False)

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert decision.matched_rule_id == DENY_RULE

    def test_a_trusted_portal_still_reaches_the_caregiver_decision(
        self, engine: PolicyEngine
    ) -> None:
        decision = decide(engine, grant(), approved=False)

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.reason_code is ReasonCode.REQUIRES_HUMAN_APPROVAL
        assert decision.matched_rule_id == REQUIRE_HUMAN_RULE

    def test_a_trusted_portal_with_an_exact_approval_is_allowed(self, engine: PolicyEngine) -> None:
        """The patch narrows; it does not make the tool permanently unusable in this harness."""
        decision = decide(engine, grant(), approved=True)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == ALLOW_RULE


class TestTheRefusalIsAboutThePortalAndNotSomethingElse:
    """Attribution. Each refusal below must be caused by the argument it names and no other."""

    def test_the_role_barrier_is_what_the_synthetic_fact_gets_past(
        self, engine: PolicyEngine
    ) -> None:
        """Vacuity guard, and it has to hold the portal *trusted* to be one.

        Without a role fact the grant is refused ``AUTHORITY_NOT_TRUSTED``, whatever the portal is —
        so every test above would pass while proving nothing whatsoever about ``portal_id``. That is
        exactly why they synthesise a role, and this is the measurement that says so.

        The portal is trusted here deliberately. With a hostile one, the new destination rule fires
        first — it is earlier in file order among the ``DENY`` rules — and the refusal would be
        about the portal again, which is the thing this test is trying to hold constant.
        """
        args = grant()
        decision = engine.evaluate(
            "grant_portal_access",
            args,
            case_for(
                args,
                approved=True,
                facts=(trusted_fact("caregiver_email", CARER), trusted_fact("portal_id", PORTAL)),
            ),
        )

        assert decision.verdict is Verdict.DENY
        assert decision.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED
        assert decision.matched_rule_id == "deny-authority-grant-below-trusted"

    def test_the_engine_resolved_the_portal_as_untrusted_all_along(
        self, engine: PolicyEngine
    ) -> None:
        """The defect was never in trust resolution. It was that nothing required the result."""
        decision = decide(engine, grant(portal_id=HOSTILE_PORTAL), approved=True)

        assert decision.arg_trust["portal_id"] is TrustLevel.UNTRUSTED
        assert decision.arg_trust["grantee"] is TrustLevel.TRUSTED
        assert decision.arg_trust["role"] is TrustLevel.TRUSTED

    def test_an_untrusted_grantee_is_still_refused_for_its_own_reason(
        self, engine: PolicyEngine
    ) -> None:
        """The grantee rule is untouched and still fires first for its own argument."""
        args = grant(grantee="attacker@evil.example")
        decision = decide(engine, args, approved=True)

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == "deny-portal-access-to-untrusted-grantee"

    def test_a_portal_the_case_holds_only_as_controlled_is_not_enough(
        self, engine: PolicyEngine
    ) -> None:
        """``CONTROLLED`` is what a validator earns, and a validator cannot make a destination.

        The distinction the ``DESTINATION`` class exists for: a well-formed portal identifier lifted
        from a document and then format-checked is still not a portal CareLoop may grant access on.
        """
        args = grant(portal_id=HOSTILE_PORTAL)
        case = case_for(
            args,
            approved=True,
            facts=(
                trusted_fact("caregiver_email", CARER),
                Fact(
                    key="portal_id",
                    value=HOSTILE_PORTAL,
                    trust_level=TrustLevel.CONTROLLED,
                    provenance=Provenance(source_doc="notice.pdf", extractor="test"),
                    extracted_at=FIXED_NOW,
                ),
                synthetic_role_fact(),
            ),
        )

        decision = engine.evaluate("grant_portal_access", args, case)

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == DENY_RULE
        assert decision.arg_trust["portal_id"] is TrustLevel.CONTROLLED


class TestApprovalCannotSubstituteForDestinationTrust:
    """An approval is a capability for one exact call. It is not permission to redirect one."""

    def test_the_approval_really_does_cover_this_exact_call(self, engine: PolicyEngine) -> None:
        """Vacuity. If the approval did not match, the ``DENY`` above would be uninformative.

        Proven by moving *only* the portal to a trusted one and watching the same approval produce
        an ``ALLOW`` — so the digest, the tool binding and the status are all correct, and the
        refusal above is about the destination alone.
        """
        assert decide(engine, grant(), approved=True).verdict is Verdict.ALLOW

    def test_approving_an_untrusted_portal_changes_nothing(self, engine: PolicyEngine) -> None:
        with_approval = decide(engine, grant(portal_id=HOSTILE_PORTAL), approved=True)
        without = decide(engine, grant(portal_id=HOSTILE_PORTAL), approved=False)

        assert with_approval.verdict is without.verdict is Verdict.DENY
        assert with_approval.matched_rule_id == without.matched_rule_id == DENY_RULE

    def test_the_allow_rule_requires_the_portal_deterministically_and_not_only_by_approval(
        self,
    ) -> None:
        """Read off the rule itself: both clauses are present, and neither implies the other."""
        rule = next(rule for rule in default_rules().rules if rule.id == ALLOW_RULE)
        required = {predicate.arg: predicate.level for predicate in rule.when.arg_trust_at_least}

        assert rule.when.args_approved is True
        assert required == {
            "grantee": TrustLevel.TRUSTED,
            "role": TrustLevel.TRUSTED,
            "portal_id": TrustLevel.TRUSTED,
        }


class TestTheGuardedTwinIsUnchanged:
    """``upload_document.portal_id`` is the same argument name, of the same class, on the other
    tool that takes one — and it has always been guarded. Its behaviour must not move."""

    def test_an_untrusted_upload_portal_is_still_denied(self, engine: PolicyEngine) -> None:
        args = {"portal_id": HOSTILE_PORTAL, "document_id": "doc-0001-referral"}
        decision = engine.evaluate("upload_document", args, case_for(grant(), approved=False))

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == "deny-upload-to-untrusted-portal"

    def test_a_trusted_upload_portal_is_still_allowed(self, engine: PolicyEngine) -> None:
        args = {"portal_id": PORTAL, "document_id": "doc-0001-referral"}
        case = case_for(
            grant(),
            approved=False,
            facts=(
                trusted_fact("portal_id", PORTAL),
                Fact(
                    key="document_id",
                    value="doc-0001-referral",
                    trust_level=TrustLevel.CONTROLLED,
                    provenance=Provenance(source_doc="rejection.pdf", extractor="test"),
                    extracted_at=FIXED_NOW,
                ),
            ),
        )

        decision = engine.evaluate("upload_document", args, case)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-upload-to-trusted-portal"


# -- the non-vacuity proof -----------------------------------------------------------------------


def historical_ruleset() -> Any:
    """The shipped rules with the portal destination guard removed, and nothing else.

    Built by editing the *parsed* shipped document rather than by copying it, so the mutant cannot
    drift from what ships and cannot differ in some second respect nobody noticed. Two edits, each
    asserted to have actually happened:

    1. ``deny-portal-access-to-untrusted-portal`` is dropped.
    2. ``allow-approved-portal-access-grant`` loses its ``portal_id`` trust predicate.

    Everything else — argument classes, the state clauses, ``args_approved``, the match ordering,
    the loader's validation against the registry — is the shipped system. Same discipline as
    ``tests/test_booking_authority.py::historical_ruleset``.
    """
    raw: Any = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

    before = len(raw["rules"])
    raw["rules"] = [rule for rule in raw["rules"] if rule["id"] != DENY_RULE]
    assert len(raw["rules"]) == before - 1, f"{DENY_RULE} was not in the shipped rules"

    edited = 0
    for rule in raw["rules"]:
        if rule["id"] != ALLOW_RULE:
            continue
        predicates = rule["when"]["arg_trust_at_least"]
        rule["when"]["arg_trust_at_least"] = [
            predicate for predicate in predicates if predicate["arg"] != "portal_id"
        ]
        assert len(rule["when"]["arg_trust_at_least"]) == len(predicates) - 1, (
            f"{ALLOW_RULE} does not require trust of portal_id"
        )
        edited += 1
    assert edited == 1

    return load_rules_from_mapping(raw, REGISTRY)


@pytest.fixture
def unguarded_engine() -> PolicyEngine:
    """The shipped engine with the portal destination guard removed."""
    return PolicyEngine(historical_ruleset(), REGISTRY)


class TestTheHistoricalHoleIsDetected:
    """Remove the guard and the boundary tests must fail.

    A regression suite that passes with the defence disabled was never testing the defence. Each
    method here asserts the *unauthorised* verdict the shipped system produced before this session,
    so if the guard is ever weakened the matching test in
    :class:`TestThePortalIsATrustedDestination` fails — which is why both halves are here.
    """

    def test_the_mutant_differs_from_the_shipped_rules_in_one_property(self) -> None:
        """Bounding the mutant itself. Two edits, and no third."""
        mutant = historical_ruleset()
        shipped = default_rules()

        assert shipped.rule_ids - mutant.rule_ids == {DENY_RULE}
        assert mutant.rule_ids - shipped.rule_ids == set()

        shipped_rules = {rule.id: rule for rule in shipped.rules}
        for rule in mutant.rules:
            if rule.id == ALLOW_RULE:
                continue
            assert rule == shipped_rules[rule.id], f"the mutant also changed {rule.id}"

        grant_rule = next(rule for rule in mutant.rules if rule.id == ALLOW_RULE)
        assert {predicate.arg for predicate in grant_rule.when.arg_trust_at_least} == {
            "grantee",
            "role",
        }
        assert grant_rule.when.args_approved is True

    def test_the_approved_untrusted_portal_reappears(self, unguarded_engine: PolicyEngine) -> None:
        decision = decide(unguarded_engine, grant(portal_id=HOSTILE_PORTAL), approved=True)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == ALLOW_RULE
        assert decision.arg_trust["portal_id"] is TrustLevel.UNTRUSTED

    def test_the_caregiver_is_asked_about_it_again(self, unguarded_engine: PolicyEngine) -> None:
        decision = decide(unguarded_engine, grant(portal_id=HOSTILE_PORTAL), approved=False)

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.matched_rule_id == REQUIRE_HUMAN_RULE

    def test_the_structural_check_reports_the_hole(self) -> None:
        """The generic invariant finds it without knowing anything about this tool."""
        assert uncovered_authority_arguments(REGISTRY, historical_ruleset()) == {
            (ALLOW_RULE, "grant_portal_access", "portal_id")
        }

    def test_the_other_grant_requirements_are_unaffected_by_the_mutation(
        self, unguarded_engine: PolicyEngine
    ) -> None:
        """The mutant must not be permitting everything wholesale.

        Grantee and role are still enforced under it, so the reappearances above are attributable to
        the portal property alone.
        """
        untrusted_grantee = unguarded_engine.evaluate(
            "grant_portal_access",
            grant(grantee="attacker@evil.example"),
            case_for(grant(grantee="attacker@evil.example"), approved=True),
        )
        untrusted_role = unguarded_engine.evaluate(
            "grant_portal_access",
            grant(),
            case_for(
                grant(),
                approved=True,
                facts=(trusted_fact("caregiver_email", CARER), trusted_fact("portal_id", PORTAL)),
            ),
        )

        assert untrusted_grantee.reason_code is ReasonCode.DESTINATION_NOT_TRUSTED
        assert untrusted_role.reason_code is ReasonCode.AUTHORITY_NOT_TRUSTED

    def test_the_upload_twin_is_unaffected_by_the_mutation(
        self, unguarded_engine: PolicyEngine
    ) -> None:
        """The mutation is scoped to one tool's rules, so the identically-classed argument on the
        other tool must not move."""
        decision = unguarded_engine.evaluate(
            "upload_document",
            {"portal_id": HOSTILE_PORTAL, "document_id": "doc-0001-referral"},
            case_for(grant(), approved=False),
        )

        assert decision.verdict is Verdict.DENY
        assert decision.matched_rule_id == "deny-upload-to-untrusted-portal"
