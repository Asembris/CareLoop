"""A human approval is a capability for one exact tool call.

The identity of that capability is ``(tool_name, args_digest)``. Until this module existed it was
``args_digest`` alone: :meth:`careloop.state.case.Case.is_approved` took one parameter, there was no
way for a caller to *supply* a tool name, and :attr:`careloop.state.case.Approval.tool_name` was
stored, rendered and audited but never matched. An approval recorded against one tool therefore
answered ``True`` for any other tool invoked with an argument map that canonicalised the same way.

**Not exploitable with the shipped configuration, and that was measured rather than assumed.** Only
``require-human-for-outbound-email`` and ``require-human-for-authority-grant`` can mint an approval,
and only ``send_email`` and ``grant_portal_access`` have rules that read ``args_approved`` — so the
set of tools that can raise a decision request and the set that can spend one are the same two, and
their argument names are disjoint. The only digest they could share is the empty one, which neither
can reach: an argument-less call to either matches a ``DENY`` rule on its untrusted destination
before ``REQUIRE_HUMAN`` is considered.

So the defect was latent. It was also one tool away from being live, and nothing said so: the
safety rested on the shipped registry's argument shapes happening not to overlap, which is stated
in no rule, checked by no test, and would have been broken silently by adding a tool.
:class:`TestThePolicyEngineBindsApprovalToTheToolBeingEvaluated` is the proof, run through the real
:class:`~careloop.policy.engine.PolicyEngine` over a synthetic registry whose two tools declare
*complete and identical* argument schemas — no omitted arguments are involved, so the property is
established independently of what the engine does about partial argument maps.

The production registry is never modified here. A test that manufactured a collision by editing
:data:`careloop.tools.registry.REGISTRY` would be testing a system nobody ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from inspect import signature

import pytest
from helpers import FIXED_NOW, make_case

from careloop.graph.context import approval_for as context_approval_for
from careloop.graph.context import is_approved as context_is_approved
from careloop.policy.engine import PolicyEngine, approved
from careloop.policy.schema import Condition, Rule, RuleSet
from careloop.policy.verdicts import ReasonCode, Verdict
from careloop.provenance.trust import ArgClass
from careloop.state.case import Approval, ApprovalStatus, Case, CaseState
from careloop.tools.idempotency import args_digest
from careloop.tools.registry import ArgSpec, ToolRegistry, ToolSpec

TOOL_A = "tool_a"
TOOL_B = "tool_b"

ARGS = {"x": "same"}
OTHER_ARGS = {"x": "different"}

#: The whole point: one digest, two tools. ``args_digest`` covers argument names and canonical
#: values and nothing else, so two tools with the same complete schema produce the same digest.
DIGEST = args_digest(ARGS)
OTHER_DIGEST = args_digest(OTHER_ARGS)


def approval(
    tool_name: str,
    digest: str,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    *,
    approval_id: str = "apr-1",
    decided_at: datetime | None = None,
) -> Approval:
    """One decision record, decided unless it is pending."""
    settled = status is not ApprovalStatus.PENDING
    return Approval(
        approval_id=approval_id,
        tool_name=tool_name,
        args_digest=digest,
        reason_code=str(ReasonCode.REQUIRES_HUMAN_APPROVAL),
        status=status,
        requested_at=FIXED_NOW,
        decided_at=(decided_at or FIXED_NOW) if settled else None,
        decided_by="rosa" if settled else None,
    )


def case_holding(*approvals: Approval) -> Case:
    """A case carrying these decision records, oldest first."""
    return make_case(state=CaseState.READY_TO_EXECUTE).model_copy(
        update={"approvals": tuple(approvals)}
    )


class TestAnApprovalAuthorisesOneExactToolCall:
    """The four boundaries of the capability, on the canonical implementation."""

    def test_the_approved_tool_and_arguments_are_authorised(self) -> None:
        """The positive boundary. Without this the rest could pass by refusing everything."""
        case = case_holding(approval(TOOL_A, DIGEST))

        assert case.is_approved(TOOL_A, DIGEST)
        assert case.approval_for(TOOL_A, DIGEST) is not None

    def test_a_different_tool_with_the_same_digest_is_not_authorised(self) -> None:
        """The defect. ``tool_b`` must behave as though ``tool_a``'s approval does not exist."""
        case = case_holding(approval(TOOL_A, DIGEST))

        assert not case.is_approved(TOOL_B, DIGEST)
        assert case.approval_for(TOOL_B, DIGEST) is None

    def test_the_same_tool_with_a_different_digest_is_not_authorised(self) -> None:
        """The property that already held, asserted so the repair cannot quietly drop it."""
        case = case_holding(approval(TOOL_A, DIGEST))

        assert not case.is_approved(TOOL_A, OTHER_DIGEST)
        assert case.approval_for(TOOL_A, OTHER_DIGEST) is None

    def test_a_pending_record_is_not_an_approval(self) -> None:
        case = case_holding(approval(TOOL_A, DIGEST, ApprovalStatus.PENDING))

        assert not case.is_approved(TOOL_A, DIGEST)
        assert case.approval_for(TOOL_A, DIGEST) is not None


class TestTheGraphHelpersDecideTheSameWay:
    """``careloop.graph.context`` used to carry a second, independent reversed scan.

    Two implementations of one matching rule is two places for it to drift, and the graph copy was
    the one the execute node consulted. These assert the same four boundaries through the graph
    entry points, so a future divergence fails here rather than in production.
    """

    def test_the_approved_tool_and_arguments_are_authorised(self) -> None:
        case = case_holding(approval(TOOL_A, DIGEST))

        assert context_is_approved(case, TOOL_A, DIGEST)
        assert context_approval_for(case, TOOL_A, DIGEST) is not None

    def test_a_different_tool_with_the_same_digest_is_not_authorised(self) -> None:
        case = case_holding(approval(TOOL_A, DIGEST))

        assert not context_is_approved(case, TOOL_B, DIGEST)
        assert context_approval_for(case, TOOL_B, DIGEST) is None

    def test_the_same_tool_with_a_different_digest_is_not_authorised(self) -> None:
        case = case_holding(approval(TOOL_A, DIGEST))

        assert not context_is_approved(case, TOOL_A, OTHER_DIGEST)


class TestThePolicyEnginePredicateIsToolBound:
    """``approved`` is what ``args_approved`` calls, so it is an authority lookup in its own
    right and gets the same four boundaries."""

    def test_the_approved_call_is_approved(self) -> None:
        case = case_holding(approval(TOOL_A, DIGEST))

        assert approved(TOOL_A, ARGS, case)

    def test_another_tool_with_the_same_arguments_is_not(self) -> None:
        case = case_holding(approval(TOOL_A, DIGEST))

        assert not approved(TOOL_B, ARGS, case)

    def test_an_uncanonicalizable_argument_still_counts_as_unapproved(self) -> None:
        """Fail closed, unchanged: a call that cannot be identified has not been authorised."""
        case = case_holding(approval(TOOL_A, DIGEST))

        assert not approved(TOOL_A, {"x": 1.5}, case)


class TestTemporalSemanticsWithinOneExactCall:
    """Latest-match-wins is preserved. Only *which records participate* changed."""

    def test_a_later_rejection_revokes_an_earlier_approval(self) -> None:
        case = case_holding(
            approval(TOOL_A, DIGEST, ApprovalStatus.APPROVED, approval_id="apr-1"),
            approval(TOOL_A, DIGEST, ApprovalStatus.REJECTED, approval_id="apr-2"),
        )

        assert not case.is_approved(TOOL_A, DIGEST)

    def test_a_later_approval_supersedes_an_earlier_rejection(self) -> None:
        """The reproposal path: refused, proposed again unchanged, then approved."""
        case = case_holding(
            approval(TOOL_A, DIGEST, ApprovalStatus.REJECTED, approval_id="apr-1"),
            approval(TOOL_A, DIGEST, ApprovalStatus.APPROVED, approval_id="apr-2"),
        )

        assert case.is_approved(TOOL_A, DIGEST)

    def test_the_lookup_is_not_find_any_historical_approval(self) -> None:
        """Vacuity for the pair above: a scan that returned the first ``APPROVED`` it found would
        pass ``test_a_later_approval_supersedes_an_earlier_rejection`` and be wrong."""
        case = case_holding(
            approval(TOOL_A, DIGEST, ApprovalStatus.APPROVED, approval_id="apr-1"),
            approval(TOOL_A, DIGEST, ApprovalStatus.REJECTED, approval_id="apr-2"),
        )

        assert case.approval_for(TOOL_A, DIGEST).status is ApprovalStatus.REJECTED


class TestARecordForAnotherToolCannotShadowThisOne:
    """The consequence of tool-binding that runs the other way.

    Under digest-only matching the *most recent* record for a digest won whatever tool it named, so
    a decision about a different call could revoke authority nobody had asked about. This is the
    same defect as the leak and not a separate one — the scan was answering a question about the
    wrong action in both directions.
    """

    def test_a_rejection_for_another_tool_leaves_this_approval_standing(self) -> None:
        case = case_holding(
            approval(TOOL_A, DIGEST, ApprovalStatus.APPROVED, approval_id="apr-1"),
            approval(TOOL_B, DIGEST, ApprovalStatus.REJECTED, approval_id="apr-2"),
        )

        assert case.is_approved(TOOL_A, DIGEST)
        assert not case.is_approved(TOOL_B, DIGEST)

    def test_an_approval_for_another_tool_does_not_revive_this_rejection(self) -> None:
        """The mirror, so the property is not "later records for other tools are ignored when
        that happens to help"."""
        case = case_holding(
            approval(TOOL_A, DIGEST, ApprovalStatus.REJECTED, approval_id="apr-1"),
            approval(TOOL_B, DIGEST, ApprovalStatus.APPROVED, approval_id="apr-2"),
        )

        assert not case.is_approved(TOOL_A, DIGEST)
        assert case.is_approved(TOOL_B, DIGEST)


# -- the end-to-end proof, through the real engine ---------------------------------------------


def _synthetic_registry() -> ToolRegistry:
    """Two tools with complete, identical argument schemas.

    Identical rather than overlapping, so the collision needs no omitted argument. What the engine
    does about a call that leaves a declared argument out is a separate question and is deliberately
    not depended on here.
    """
    return ToolRegistry(
        [
            ToolSpec(
                name=TOOL_A,
                external_effect=True,
                args=(ArgSpec(name="x", arg_class=ArgClass.VALUE),),
            ),
            ToolSpec(
                name=TOOL_B,
                external_effect=True,
                args=(ArgSpec(name="x", arg_class=ArgClass.VALUE),),
            ),
        ]
    )


def _synthetic_rules() -> RuleSet:
    """The shipped human-in-the-loop pair, transposed onto the synthetic tools.

    Structurally ``require-human-for-outbound-email`` and ``allow-approved-outbound-email``: ask
    when the call is not approved, allow when it is. The trust clauses are dropped because they are
    not what is under test — an approval that cannot launder an untrusted destination is
    ``tests/test_policy_engine.py``'s property and is unaffected by this change.
    """
    return RuleSet(
        version=1,
        rules=(
            Rule(
                id="require-human-when-unapproved",
                effect=Verdict.REQUIRE_HUMAN,
                tools=(TOOL_A, TOOL_B),
                when=Condition(args_approved=False),
                reason_code=ReasonCode.REQUIRES_HUMAN_APPROVAL,
                diagnostic="Neither synthetic tool acts without a caregiver decision.",
            ),
            Rule(
                id="allow-when-approved",
                effect=Verdict.ALLOW,
                tools=(TOOL_A, TOOL_B),
                when=Condition(args_approved=True),
                diagnostic="A caregiver approved this exact call.",
            ),
        ),
    )


class TestThePolicyEngineBindsApprovalToTheToolBeingEvaluated:
    """The defect at the layer that decides, not at the helper it delegates to.

    Helper-level tests prove the lookup. This proves that the lookup is what ``args_approved``
    consults and that the engine hands it the tool it is currently evaluating — which is the whole
    causal chain from a caregiver's decision to an ``ALLOW``.
    """

    @pytest.fixture
    def engine(self) -> PolicyEngine:
        return PolicyEngine(_synthetic_rules(), _synthetic_registry())

    def test_an_unapproved_call_asks_a_human(self, engine: PolicyEngine) -> None:
        """The baseline both tools start from."""
        case = make_case(state=CaseState.PLANNING)

        assert engine.evaluate(TOOL_B, ARGS, case).verdict is Verdict.REQUIRE_HUMAN

    def test_the_approved_tool_is_allowed(self, engine: PolicyEngine) -> None:
        case = case_holding(approval(TOOL_A, DIGEST))

        decision = engine.evaluate(TOOL_A, ARGS, case)

        assert decision.verdict is Verdict.ALLOW
        assert decision.matched_rule_id == "allow-when-approved"

    def test_the_other_tool_is_not_allowed_by_it(self, engine: PolicyEngine) -> None:
        """Cross-tool laundering. ``tool_b`` must be decided exactly as it was with no approval on
        the case at all."""
        case = case_holding(approval(TOOL_A, DIGEST))

        decision = engine.evaluate(TOOL_B, ARGS, case)

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.matched_rule_id == "require-human-when-unapproved"

    def test_the_other_tool_is_decided_as_though_the_approval_did_not_exist(
        self, engine: PolicyEngine
    ) -> None:
        """Stated as an equality rather than as a verdict, so the claim does not depend on which
        rule the synthetic file happens to put first."""
        with_approval = engine.evaluate(TOOL_B, ARGS, case_holding(approval(TOOL_A, DIGEST)))
        without = engine.evaluate(TOOL_B, ARGS, make_case(state=CaseState.PLANNING))

        assert with_approval.verdict is without.verdict
        assert with_approval.matched_rule_id == without.matched_rule_id


# -- persistence ---------------------------------------------------------------------------------


class TestPersistedApprovalsGainTheBindingWithoutMigration:
    """``Approval`` already stored ``tool_name``, so a record written before this change is
    reinterpreted rather than rewritten. Nothing migrates and no identifier moves."""

    def test_a_round_tripped_approval_authorises_its_own_tool(self, store) -> None:
        stored = case_holding(approval("send_email", DIGEST))
        store.create(stored)

        assert store.get(stored.case_id).is_approved("send_email", DIGEST)

    def test_a_round_tripped_approval_authorises_no_other_tool(self, store) -> None:
        stored = case_holding(approval("send_email", DIGEST))
        store.create(stored)

        assert not store.get(stored.case_id).is_approved("upload_document", DIGEST)

    def test_the_stored_identifier_is_untouched(self, store) -> None:
        stored = case_holding(approval("send_email", DIGEST, approval_id="apr-legacy-digest"))
        store.create(stored)

        assert store.get(stored.case_id).approvals[0].approval_id == "apr-legacy-digest"

    def test_a_legacy_handoff_record_stays_inert(self, store) -> None:
        """``tool_name=""`` was written by the pre-repair manual handoff. It matched nothing then
        and it matches nothing now, including a call whose tool name is itself empty."""
        legacy = Approval(
            approval_id="apr-no-safe-action:c",
            tool_name="",
            args_digest="",
            reason_code=str(ReasonCode.NO_SAFE_ACTION_AVAILABLE),
            status=ApprovalStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        store.create(case_holding(legacy))
        restored = store.get("case-0001")

        assert not restored.approvals[0].is_actionable
        assert not restored.is_approved("send_email", DIGEST)


# -- structural ----------------------------------------------------------------------------------


class TestEveryAuthorityLookupTakesTheTool:
    """A signature check, for the failure mode behaviour cannot reach.

    The behavioural tests above cover the helpers that exist. What they cannot catch is a *new*
    authority lookup added tool-blind — which is exactly how this defect arrived, since
    ``approval_for`` and ``is_approved`` were written before ``tool_name`` was on ``Approval`` at
    all. Asserted on parameter names via ``inspect.signature`` rather than on source text, so
    renaming a local or reformatting a call cannot turn it red.
    """

    @pytest.mark.parametrize(
        "lookup",
        [
            Case.approval_for,
            Case.is_approved,
            context_approval_for,
            context_is_approved,
            approved,
        ],
    )
    def test_it_declares_a_tool_name_parameter(self, lookup) -> None:
        assert "tool_name" in signature(lookup).parameters

    @pytest.mark.parametrize(
        "lookup", [Case.approval_for, Case.is_approved, context_approval_for, context_is_approved]
    )
    def test_the_tool_name_cannot_be_omitted(self, lookup) -> None:
        """Required and positional, so no caller can perform a digest-only lookup by leaving it
        out — the same reason ``resolve_trust`` takes its fact key that way."""
        parameter = signature(lookup).parameters["tool_name"]

        assert parameter.default is parameter.empty
