"""What the caregiver is shown is the call they are being asked to authorise.

Session L bound *executable* authority to ``(tool_name, args_digest)``:
:meth:`careloop.state.case.Case.approval_for` matches both halves, so an approval granted for one
tool cannot release another with an argument map that canonicalises the same way. The decision
*surface* was left behind. ``proposal_for`` resolved the proposal a card renders from by
``args_digest`` alone, discarding the tool name the approval carries — so the card was answering
*"which proposal has arguments that hash like this?"* while the approval meant *"this exact call"*.

**This is presentation integrity, not authority.** Nothing here authorises anything: the only
proposal-derived field on :class:`~careloop.api.models.DecisionView` is ``args``, no client computes
approval validity, and the execute node and the policy engine both go through the tool-bound lookup.
It is also **not reachable under the shipped workflow**, for three independent reasons — the only
two tools that can reach ``REQUIRE_HUMAN`` declare disjoint argument names so the sole digest they
could share is the empty map's, which a ``DENY`` on the absent destination refuses first; the
approval is raised inside the same gate execution that read ``latest_proposal``, so it is always the
newest proposal at the moment a card exists; and nothing plans while a case is ``AWAITING_HUMAN``.

None of that is written down in a rule, which is the whole reason to close it. The safety rested on
registry argument shapes and on the order two nodes happen to write the timeline in.

**What can and cannot be observed, stated plainly.** ``Proposal.args`` is ``dict[str, str]`` and
:func:`careloop.provenance.canonical.canonical_value` returns a ``str`` verbatim, so an equal digest
means an equal argument map short of a SHA-256 collision. Two same-digest proposals for different
tools therefore cannot render *different* argument values, and no test here claims they do. The
reachable observable defect is **borrowing**: an approval whose own proposal is absent, but whose
digest is carried by some other tool's proposal, rendered that other tool's arguments instead of
taking the no-match path the module already documents. ``rationale`` is not part of the digest and
not projected into ``DecisionView``, which makes it the right instrument for proving *which record*
the helper selected — at helper level, where record identity is visible.
"""

from __future__ import annotations

from helpers import FIXED_NOW, make_case

from careloop.api.projections import case_detail, decision_view
from careloop.graph.context import proposal_for
from careloop.state.case import Approval, ApprovalStatus, Case, CaseState, TimelineKind
from careloop.state.machine import append_event
from careloop.state.memory_store import InMemoryCaseStore
from careloop.tools.idempotency import args_digest

#: The two tools that can actually reach ``REQUIRE_HUMAN``. Real registry names rather than
#: invented ones: if a later tool ever shares an argument-name subset with one of these, the
#: scenario below stops being synthetic and this file is already speaking the right vocabulary.
TOOL_A = "send_email"
TOOL_B = "grant_portal_access"

#: One argument map, proposed under both tools. A ``PLAN_PROPOSED`` entry records whatever the
#: planner said — the gate refuses undeclared arguments *afterwards* — so an entry naming ``TOOL_B``
#: with these arguments is structurally writable even though it would never be allowed.
ARGS = {"to": "carer@example", "subject": "the claim", "body": "please review"}
DIGEST = args_digest(ARGS)


def _proposed(case: Case, tool_name: str, args: dict[str, str], rationale: str = "") -> Case:
    """Append the ``PLAN_PROPOSED`` entry :class:`careloop.graph.nodes.PlanNode` writes."""
    return append_event(
        case,
        kind=TimelineKind.PLAN_PROPOSED,
        actor="planner",
        summary=f"proposed {tool_name}",
        payload={"tool_name": tool_name, "args": dict(args), "rationale": rationale},
        now=FIXED_NOW,
    )


def _awaiting(case: Case, tool_name: str, digest: str) -> Case:
    """Park ``case`` on a pending decision request for exactly this call."""
    return case.model_copy(
        update={
            "state": CaseState.AWAITING_HUMAN,
            "approvals": (
                *case.approvals,
                Approval(
                    approval_id=f"apr-{case.next_seq()}",
                    tool_name=tool_name,
                    args_digest=digest,
                    reason_code="REQUIRES_HUMAN_APPROVAL",
                    status=ApprovalStatus.PENDING,
                    requested_at=FIXED_NOW,
                ),
            ),
        }
    )


# =================================================================================================
# The property: a card is never populated from another tool's proposal
# =================================================================================================


class TestTheCardCannotBorrowAnotherToolsProposal:
    """The load-bearing proof, and the one form of this that is observable at the API surface.

    An approval for ``TOOL_A`` whose own proposal is not on the timeline must render with no
    arguments — the behaviour ``decision_view`` already documents and
    ``test_an_approval_whose_proposal_is_missing_renders_without_arguments`` already pins. "Not on
    the timeline" has to include *"present only under another tool"*, or the fallback is reachable
    around rather than through.
    """

    def test_a_pending_approval_takes_no_arguments_from_another_tools_proposal(self) -> None:
        case = _proposed(make_case(), TOOL_B, ARGS, rationale="proposed as a portal grant")
        case = _awaiting(case, TOOL_A, DIGEST)

        decision = decision_view(case)

        assert decision is not None
        assert decision.tool_name == TOOL_A
        assert decision.args == {}, (
            "the card borrowed its arguments from a proposal belonging to another tool; an "
            "approval identifies one exact call and a record naming a different tool is a "
            "proposal of a different action, whatever its arguments hash to"
        )

    def test_the_same_holds_with_the_two_tools_exchanged(self) -> None:
        """Identity, not a property of one tool name or one scan direction."""
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="proposed as an email")
        case = _awaiting(case, TOOL_B, DIGEST)

        decision = decision_view(case)

        assert decision is not None
        assert decision.tool_name == TOOL_B
        assert decision.args == {}

    def test_the_card_is_right_when_another_tools_identical_proposal_is_newer(self) -> None:
        """The scenario the audit predicted would show the wrong arguments. **It cannot.**

        Recorded here rather than left out, because reading the code suggests otherwise. The
        approval's own proposal exists and a different tool's is newer, so a digest-only lookup does
        select the wrong *record* — but an equal digest means an equal argument map, and ``args`` is
        the only proposal-derived field on the card. So this passes before the repair, after it, and
        under the mutant that removes tool matching. It discriminates nothing and is not counted as
        evidence; the discriminator for record identity is
        :class:`TestTheExactProposalIsSelectedWhateverTheOrder`, which reads ``rationale``.
        """
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="proposed as an email")
        case = _proposed(case, TOOL_B, ARGS, rationale="proposed as a portal grant")
        case = _awaiting(case, TOOL_A, DIGEST)

        decision = decision_view(case)

        assert decision.tool_name == TOOL_A
        assert decision.args == ARGS


class TestTheCardTheApiActuallyServes:
    """Through the response projection, not only through the helper it calls.

    ``decision_view`` is reached by ``case_card`` and ``case_detail``, and those are what
    ``GET /api/cases`` and ``GET /api/cases/{id}`` return. A property proven only on the helper is a
    property about a function nobody outside this package calls.
    """

    def test_case_detail_serves_a_card_that_borrowed_nothing(self) -> None:
        case = _proposed(make_case(), TOOL_B, ARGS, rationale="proposed as a portal grant")
        case = _awaiting(case, TOOL_A, DIGEST)

        detail = case_detail(case)

        assert detail.decision is not None
        assert detail.decision.tool_name == TOOL_A
        assert detail.decision.args == {}
        assert detail.decision.approval_id == case.pending_approval().approval_id

    def test_case_detail_serves_the_exact_proposal_when_there_is_one(self) -> None:
        """The positive half of the same route, so the assertion above is not passing vacuously."""
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="proposed as an email")
        case = _awaiting(case, TOOL_A, DIGEST)

        detail = case_detail(case)

        assert detail.decision.tool_name == TOOL_A
        assert detail.decision.args == ARGS


# =================================================================================================
# The helper: which record is selected, in both orderings
# =================================================================================================


class TestTheExactProposalIsSelectedWhateverTheOrder:
    """Record identity, which only the helper can show.

    An equal digest means an equal argument map, so ``DecisionView`` cannot distinguish these two
    proposals at all — ``rationale`` can, it is not part of the digest, and it is deliberately not
    projected. Asserting on it here is what makes "the right record was selected" a claim rather
    than an inference.
    """

    def test_the_older_matching_tool_wins_over_a_newer_different_one(self) -> None:
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="rationale A")
        case = _proposed(case, TOOL_B, ARGS, rationale="rationale B")

        found = proposal_for(case, TOOL_A, DIGEST)

        assert found is not None
        assert found.tool_name == TOOL_A
        assert found.rationale == "rationale A"

    def test_and_the_other_way_round_so_it_is_not_the_ordering_doing_the_work(self) -> None:
        case = _proposed(make_case(), TOOL_B, ARGS, rationale="rationale B")
        case = _proposed(case, TOOL_A, ARGS, rationale="rationale A")

        found = proposal_for(case, TOOL_B, DIGEST)

        assert found is not None
        assert found.tool_name == TOOL_B
        assert found.rationale == "rationale B"

    def test_another_tools_proposal_alone_is_no_match_at_all(self) -> None:
        case = _proposed(make_case(), TOOL_B, ARGS, rationale="rationale B")

        assert proposal_for(case, TOOL_A, DIGEST) is None


class TestTheSameToolPathIsUnchanged:
    """Every anchor that must keep behaving exactly as it did.

    These are what stop the repair being "return ``None`` more often": each one passes before the
    change and after it, and the mutant that removes tool matching leaves all of them green. That
    contrast is the measure of what the tool binding is actually doing.
    """

    def test_the_exact_call_resolves(self) -> None:
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="rationale A")

        found = proposal_for(case, TOOL_A, DIGEST)

        assert found is not None
        assert found.args == ARGS

    def test_the_latest_exact_match_wins_when_one_call_was_proposed_twice(self) -> None:
        """Which is what the approval was raised from.

        The gate reads ``latest_proposal`` and raises the request from it in the same execution, so
        latest-wins is not a tie-break chosen here — it is the record the caregiver was asked about.
        Scoping the lookup to one tool narrows the candidates and leaves that unchanged.
        """
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="old")
        case = _proposed(case, TOOL_A, ARGS, rationale="new")

        found = proposal_for(case, TOOL_A, DIGEST)

        assert found is not None
        assert found.rationale == "new"

    def test_a_different_digest_under_the_same_tool_is_no_match(self) -> None:
        other = {**ARGS, "subject": "something else entirely"}
        case = _proposed(make_case(), TOOL_A, other)

        assert proposal_for(case, TOOL_A, DIGEST) is None

        parked = _awaiting(case, TOOL_A, DIGEST)
        assert decision_view(parked).args == {}

    def test_an_approval_no_proposal_covers_still_renders_a_card(self) -> None:
        """Unchanged, and deliberately: a card with nothing on it is a caregiver who can see that
        there is nothing to approve. Hiding it would be the worse failure."""
        case = _awaiting(make_case(), TOOL_A, DIGEST)

        decision = decision_view(case)

        assert decision is not None
        assert decision.args == {}
        assert decision.approval_id == case.pending_approval().approval_id
        assert decision.tool_name == TOOL_A
        assert decision.reason_code == "REQUIRES_HUMAN_APPROVAL"


# =================================================================================================
# Persistence: the stronger lookup reinterprets what is already on disk
# =================================================================================================


class TestNothingOnDiskHadToChange:
    """``Approval`` already stored both halves and ``PLAN_PROPOSED`` already stored the tool name.

    So there is no migration to run and none that could have been needed. A persisted case gains
    the stronger reading, keeps its identifiers byte for byte, and stays readable either way.
    """

    def test_a_stored_case_renders_its_exact_proposal_after_a_round_trip(self) -> None:
        case = _proposed(make_case(), TOOL_A, ARGS, rationale="rationale A")
        case = _awaiting(case, TOOL_A, DIGEST)

        store = InMemoryCaseStore()
        store.create(case)
        restored = store.get(case.case_id)

        assert restored.pending_approval() == case.pending_approval()
        assert decision_view(restored).args == ARGS

    def test_a_stored_case_holding_only_another_tools_proposal_borrows_nothing(self) -> None:
        case = _proposed(make_case(), TOOL_B, ARGS, rationale="rationale B")
        case = _awaiting(case, TOOL_A, DIGEST)

        store = InMemoryCaseStore()
        store.create(case)
        restored = store.get(case.case_id)

        assert restored.pending_approval().tool_name == TOOL_A
        assert restored.pending_approval().args_digest == DIGEST
        assert decision_view(restored).args == {}
