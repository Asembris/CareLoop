"""When the planner cannot form a proposal, the case parks rather than failing.

``tests/test_proposal_tools.py`` owns the question *"what is a valid proposal, and what happens to
an answer that is not one?"* — zero calls, several calls, an unoffered tool, a missing field, a
wrong type, each refused deterministically by its own exception. This file starts where that one
stops: **once :func:`careloop.agents.invoke.proposal_tool_call` has spent its one bounded retry and
still has nothing, what is the case allowed to become?**

Until this session the answer was ``FAILED``. The exception left ``PlanNode``, the SDK re-raised it
fail-fast, and :func:`careloop.graph.runner.run_case`'s crash handler — which exists so a *crashed*
run leaves a case that says so — marked the case ``FAILED`` with ``a graph node raised:
MalformedProposal`` and let the exception out. Nothing unsafe happened; nothing ever could, because
no proposal existed to act on. What was wrong is what a caregiver was then shown: a case filed under
*Completed*, carrying an internal exception name, when the truth is that CareLoop stopped and a
person has to pick this up.

It was measured, not imagined. ``docs/model-benchmark-post-b2.md``: 2 of 36 proposal attempts came
back malformed and both recovered on the retry, both on the replan task carrying a prior
``DESTINATION_NOT_TRUSTED`` refusal; and on the deployed demo the same day, **2 of 28 runs of
scenario 03** ended ``FAILED`` because the retry failed too, both on the replan call after a
refusal. Open decision 35 is what this closes.

So the property is:

    the exhaustion of the planner's bounded retry parks the case for a person, performs no external
    effect, creates no approval, and persists nothing the model wrote.

**Four classes, and they are different questions.**

:class:`TestTheBoundedRetryStillWorks` — the first failure is *not* what parks a case. The retry is
untouched, and a run that recovers on it is byte-for-byte the run it always was.

:class:`TestExhaustionParksTheCase` — the lifecycle. Not ``FAILED``, no approval, no proposal, the
right reason code, the right section, the right version arithmetic.

:class:`TestADeniedProposalCannotBeResurrected` — the production-like shape, and the load-bearing
class. Policy refuses a proposal, the planner replans, and *that* is the call that comes back
malformed. The case therefore parks while ``latest_proposal`` still names a call policy **denied**,
which is exactly the record an approval here would release —
:func:`careloop.graph.nodes._park_with_nothing_safe_to_do` documents that defect one node later.
Everything else in this file could pass while a denied proposal was still reachable; this class is
what fails if one ever is.

:class:`TestNothingElseIsCaught` — the narrowness, which is the real risk in a patch that adds an
``except``. ``tests/test_no_safe_action.py`` states it for the gate's park and it is no less true
here: *"a catch-all here would become the place every unexplained problem ends up, and the two
scenarios that depend on ``FAILED`` meaning something would stop meaning it."*

**The four generation failures are driven as themselves.** ``MockModel`` raises a scripted exception
from ``stream``, so ``proposal_tool_call``'s real retry loop consumes them and its real re-raise is
what reaches the node — which is the seam under test. What a genuinely malformed *answer* looks like
on the wire, and that it classifies to ``MalformedProposal``, is ``test_proposal_tools.py``'s
property and is not restated here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence

import pytest
from helpers import BOOKING_REASON, FIXED_NOW
from strands.multiagent.base import Status

from careloop.agents.invoke import (
    MAX_PROPOSAL_ATTEMPTS,
    RECOVERABLE_PROPOSAL_FAILURES,
    AmbiguousProposalToolCall,
    MalformedProposal,
    NoProposalToolCall,
    NoStructuredOutput,
    UnknownProposalTool,
)
from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
    proposal_tool_call_for,
)
from careloop.agents.triage import TriageAgent
from careloop.api.projections import NEEDS_YOU, decision_view, section_for
from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.context import latest_proposal, manual_handoff, recheck_target
from careloop.graph.runner import (
    CaseNotRecheckable,
    CaseNotReopenable,
    NoSuchApproval,
    RunResult,
    approve,
    context_for,
    open_case,
    reject,
    reopen_case,
    request_recheck,
    run_case,
)
from careloop.graph.trusted import DEMO_CAREGIVER
from careloop.llm.mock import MockScriptExhausted, mock_model
from careloop.policy.verdicts import ReasonCode
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.clinic import ClinicClient
from demoworld.clinic import Clinic

CASE_ID = "case-proposal-exhaustion"
ATTACKER = "records-intake@claims-continuity-partners.example"

#: The nodes a run spends before it ever reaches the planner.
BEFORE_PLANNING = ("dispatch", "triage", "evidence")

#: A booking the shipped rules allow against the referral case, used only where a run has to get
#: past the gate. Its values are the ones ``tests/test_verify_readback.py`` books.
BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": BOOKING_REASON,
}

#: A proposal the shipped rules refuse: an email to an address no trusted configuration names.
#: ``deny-email-to-untrusted-recipient`` answers ``DESTINATION_NOT_TRUSTED``.
POISONED_EMAIL = {
    "to": ATTACKER,
    "subject": "Referral letter",
    "body": "coverage summary",
}


# -- driving one run -----------------------------------------------------------------------------


def _triage() -> TriageAgent:
    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.APPOINTMENT,
                    urgency=Urgency.SOON,
                    summary="Cardiology referral needs an outpatient appointment.",
                    patient_id="PT-4417",
                )
            ]
        )
    )


def _intent(tool_name: str, args: dict[str, str]) -> object:
    return proposal_tool_call_for(
        PlannerIntent(tool_name=tool_name, rationale="the next step for this case", args=args)
    )


def drive(
    *answers: object,
    clinic: ClinicClient | None = None,
    max_node_executions: int = 24,
) -> tuple[RunResult | None, object, Case]:
    """Open a referral case and run it against a planner scripted with ``answers``.

    An answer is either a proposal the planner should return or an ``Exception`` the provider
    should raise, in the order the graph will ask for them. ``MockModel`` handles both, which is
    what lets one driver cover recovery, exhaustion and the denial-then-exhaustion shape.

    The run result is ``None`` when the graph raised, which is the pre-repair behaviour and the
    behaviour every non-recoverable failure keeps. The case is always read back from the store, so
    an assertion about a raising run reads the same durable object as one about a parked run.
    """
    store = InMemoryCaseStore()
    open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=CASE_ID, now=FIXED_NOW)
    context = context_for(
        store,
        REFERRAL_EVENT,
        case_id=CASE_ID,
        triage=_triage(),
        planner=PlannerAgent(mock_model(proposals=list(answers))),
        now=lambda: FIXED_NOW,
        clinic=clinic,
    )
    result: RunResult | None = None
    # Suppressed rather than allowed to propagate: the raising path is one of the behaviours under
    # test, and what every assertion reads is the durable case rather than the exception.
    with contextlib.suppress(Exception):
        result = asyncio.run(run_case(context, max_node_executions=max_node_executions))
    return result, context, store.get(CASE_ID)


def malformed(times: int = MAX_PROPOSAL_ATTEMPTS) -> list[Exception]:
    """One generation failure per attempt the bounded retry will make."""
    return [MalformedProposal(f"attempt {n} did not satisfy the schema") for n in range(times)]


class RecordingStore(InMemoryCaseStore):
    """An in-memory store that remembers the version arithmetic of every save.

    Used by one test, to assert that the park is a single compare-and-set rather than to count
    entries and hope the two agree.
    """

    def __init__(self) -> None:
        super().__init__()
        self.saves: list[tuple[int, int]] = []

    def save(self, case: Case, *, expected_version: int) -> Case:
        saved = super().save(case, expected_version=expected_version)
        self.saves.append((expected_version, saved.version))
        return saved


def kinds(case: Case) -> list[TimelineKind]:
    return [event.kind for event in case.timeline]


def count(case: Case, kind: TimelineKind) -> int:
    return sum(1 for event in case.timeline if event.kind is kind)


def handoff_entry(case: Case):
    """The one ``MANUAL_HANDOFF_REQUIRED`` entry. Asserts there is exactly one."""
    found = [event for event in case.timeline if event.kind is TimelineKind.MANUAL_HANDOFF_REQUIRED]
    assert len(found) == 1, f"expected exactly one handoff, found {len(found)}"
    return found[0]


# -- A and B: the retry is untouched --------------------------------------------------------------


class TestTheBoundedRetryStillWorks:
    """The first failure is not what parks a case, and a recovered run is unchanged.

    This is the regression half. The patch adds an ``except`` around the call the retry lives
    inside, so the thing to prove is that the ``except`` is reached only when the retry has already
    given up — never instead of it.
    """

    def test_a_clean_first_proposal_reaches_the_gate_untouched(self) -> None:
        result, _, case = drive(_intent("book_appointment", BOOKING))

        assert result is not None
        assert count(case, TimelineKind.PLAN_PROPOSED) == 1
        assert count(case, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 0
        assert "gate" in result.executed_nodes

    def test_one_failure_then_a_valid_proposal_recovers_and_parks_nothing(self) -> None:
        """The measured case: 2 of 36 attempts failed and 2 of 2 recovered on the retry."""
        result, context, case = drive(
            MalformedProposal("the first answer did not satisfy the schema"),
            _intent("book_appointment", BOOKING),
        )

        assert result is not None
        assert count(case, TimelineKind.PLAN_PROPOSED) == 1
        assert count(case, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 0
        assert case.state is not CaseState.AWAITING_HUMAN
        # Both attempts were spent on one proposal, which is what "recovered on the retry" means.
        assert context.planner.model.remaining == (0, 0, 0)

    def test_the_recovered_run_costs_exactly_the_two_attempts_the_bound_allows(self) -> None:
        _, context, _ = drive(
            MalformedProposal("first"),
            _intent("book_appointment", BOOKING),
        )
        proposal_calls = [call for call in context.planner.model.calls if call.tool_names]
        assert len(proposal_calls) == MAX_PROPOSAL_ATTEMPTS


# -- C: exhaustion parks ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parked() -> tuple[RunResult | None, object, Case]:
    return drive(*malformed())


class TestExhaustionParksTheCase:
    """The lifecycle, clause by clause.

    Every one of these fails against the pre-repair behaviour.
    """

    def test_the_case_is_parked_and_not_failed(self, parked) -> None:
        _, _, case = parked
        assert case.state is CaseState.AWAITING_HUMAN
        assert case.state is not CaseState.FAILED

    def test_no_failure_reason_is_recorded(self, parked) -> None:
        """``failure_reason`` is set by ``transition`` only on the way into ``FAILED``.

        Its pre-repair value was ``a graph node raised: MalformedProposal`` — an implementation
        name on a projected field, which is the observability half of what was wrong here.
        """
        _, _, case = parked
        assert case.failure_reason is None

    def test_the_run_settles_rather_than_raising(self, parked) -> None:
        result, _, _ = parked
        assert result is not None
        assert result.status is Status.COMPLETED
        assert not result.hit_the_bound

    def test_the_run_stops_at_the_planner(self, parked) -> None:
        """No gate, no execute, no verify. The park ends the run by having no outgoing edge."""
        result, _, _ = parked
        assert result.executed_nodes == (*BEFORE_PLANNING, "plan")

    def test_the_handoff_carries_the_new_reason_code(self, parked) -> None:
        _, _, case = parked
        assert handoff_entry(case).payload["reason_code"] == str(
            ReasonCode.NO_VALID_PROPOSAL_PRODUCED
        )

    def test_the_reason_is_not_the_one_that_means_every_action_was_refused(self, parked) -> None:
        """The distinction the ninth code exists for.

        ``NO_SAFE_ACTION_AVAILABLE`` asserts every tool was proposed and denied. Here nothing was
        proposed and nothing was denied — there is not one ``POLICY_DENY`` on this case — so
        reusing it would put a false statement in an append-only trail.
        """
        _, _, case = parked
        assert count(case, TimelineKind.POLICY_DENY) == 0
        assert handoff_entry(case).payload["reason_code"] != str(
            ReasonCode.NO_SAFE_ACTION_AVAILABLE
        )
        assert handoff_entry(case).payload["reason_code"] != str(
            ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED
        )

    def test_no_proposal_is_persisted(self, parked) -> None:
        """Nothing the model wrote survives — there is no ``PLAN_PROPOSED`` to survive in."""
        _, _, case = parked
        assert count(case, TimelineKind.PLAN_PROPOSED) == 0
        assert latest_proposal(case) is None

    def test_no_approval_is_created(self, parked) -> None:
        """A handoff is a status, not a capability. There is no call for a decision to release."""
        _, _, case = parked
        assert case.approvals == ()
        assert case.pending_approval() is None

    def test_no_action_is_recorded(self, parked) -> None:
        _, _, case = parked
        assert case.actions == ()

    def test_the_executor_is_never_reached(self, parked) -> None:
        _, context, _ = parked
        assert context.executor.invocation_count == 0

    def test_the_bounded_retry_was_spent_and_not_extended(self, parked) -> None:
        _, context, _ = parked
        proposal_calls = [call for call in context.planner.model.calls if call.tool_names]
        assert len(proposal_calls) == MAX_PROPOSAL_ATTEMPTS

    def test_the_timeline_is_the_handoff_then_the_transition(self, parked) -> None:
        """Order matters: the entry explaining the stop is written before the stop."""
        _, _, case = parked
        assert kinds(case)[-2:] == [
            TimelineKind.MANUAL_HANDOFF_REQUIRED,
            TimelineKind.STATE_TRANSITION,
        ]

    def test_the_transition_came_from_planning(self, parked) -> None:
        """``PLANNING -> AWAITING_HUMAN`` was already legal, so the state machine did not change."""
        _, _, case = parked
        move = case.timeline[-1]
        assert move.payload["from"] == str(CaseState.PLANNING)
        assert move.payload["to"] == str(CaseState.AWAITING_HUMAN)
        assert move.actor == "planner"

    def test_the_park_is_one_commit_that_moves_the_version_by_two(self) -> None:
        """One appended entry and one transition, written together or not at all.

        Asserted through the store rather than by arithmetic on a constant: what matters is that
        the plan node performed exactly *one* compare-and-set, so a refusal cannot mean "half
        recorded", and that the one it performed carried both writes.
        """
        store = RecordingStore()
        open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=CASE_ID, now=FIXED_NOW)
        context = context_for(
            store,
            REFERRAL_EVENT,
            case_id=CASE_ID,
            triage=_triage(),
            planner=PlannerAgent(mock_model(proposals=malformed())),
            now=lambda: FIXED_NOW,
        )
        asyncio.run(run_case(context))

        case = store.get(CASE_ID)
        assert case.state is CaseState.AWAITING_HUMAN
        # The plan node's is the last save of the run, and it is one save.
        expected, after = store.saves[-1]
        assert after == expected + 2
        assert after == case.version

    def test_the_diagnostic_names_the_failure_type_and_never_its_message(self, parked) -> None:
        """The type is a CareLoop identifier; the message can be anything the layer built.

        ``careloop.agents.invoke`` logs the type for exactly this reason, and the park keeps that
        discipline. The diagnostic is withheld from the API wholesale either way, so this is about
        what is written down rather than about what escapes.
        """
        _, _, case = parked
        diagnostic = handoff_entry(case).diagnostic
        assert "MalformedProposal" in diagnostic
        assert "attempt 0 did not satisfy the schema" not in diagnostic
        assert "attempt 1 did not satisfy the schema" not in diagnostic

    def test_the_payload_carries_no_implementation_name(self, parked) -> None:
        """The payload is the half a response can carry, so the product vocabulary is all it holds.

        ``NO_VALID_PROPOSAL_PRODUCED`` is what a caregiver, a projection and a lifecycle read;
        ``MalformedProposal`` is an exception class and stays in the logs and the diagnostic.
        """
        _, _, case = parked
        assert handoff_entry(case).payload == {
            "reason_code": str(ReasonCode.NO_VALID_PROPOSAL_PRODUCED)
        }

    @pytest.mark.parametrize(
        "failure",
        [
            NoProposalToolCall("no tool use at all"),
            AmbiguousProposalToolCall("two tools"),
            UnknownProposalTool("a tool that was not offered"),
            MalformedProposal("arguments that do not satisfy the schema"),
        ],
        ids=lambda failure: type(failure).__name__,
    )
    def test_every_recoverable_failure_parks_the_same_way(self, failure) -> None:
        """One code for all four, deliberately.

        They are four facts about a provider and one fact about CareLoop: there is no valid next
        action. The lifecycle makes no distinction between them, so the reason code offers none.
        """
        _, _, case = drive(type(failure)(str(failure)), type(failure)(str(failure)))
        assert case.state is CaseState.AWAITING_HUMAN
        assert handoff_entry(case).payload["reason_code"] == str(
            ReasonCode.NO_VALID_PROPOSAL_PRODUCED
        )

    def test_the_parametrisation_covers_the_whole_recoverable_set(self) -> None:
        """Vacuity: the four above are exactly what the node catches, not a chosen subset."""
        covered = {
            NoProposalToolCall,
            AmbiguousProposalToolCall,
            UnknownProposalTool,
            MalformedProposal,
        }
        assert covered == set(RECOVERABLE_PROPOSAL_FAILURES)


class TestWhatTheCaregiverIsShown:
    """The product surface, which is most of the reason this patch is worth making."""

    def test_the_case_lands_in_the_section_a_person_actually_reads(self, parked) -> None:
        """``FAILED`` is filed under *Completed*; ``AWAITING_HUMAN`` is the whole of *Needs You*."""
        _, _, case = parked
        assert section_for(case.state) == NEEDS_YOU

    def test_a_card_is_rendered_and_it_offers_no_authority(self, parked) -> None:
        """No ``approval_id`` means there is nothing for any client to post a decision to.

        The refusal does not depend on a frontend choosing not to draw a button, which is the
        distinction session C's defect turned on.
        """
        _, _, case = parked
        view = decision_view(case)
        assert view is not None
        assert view.approval_id == ""
        assert view.tool_name == ""
        assert view.args == {}
        assert view.reason_code == str(ReasonCode.NO_VALID_PROPOSAL_PRODUCED)

    def test_the_card_offers_no_re_check_either(self, parked) -> None:
        """Nothing was carried out, so there is no external record to observe again."""
        _, _, case = parked
        assert decision_view(case).recheckable is False
        assert recheck_target(case) is None

    def test_the_handoff_is_readable_as_a_handoff(self, parked) -> None:
        _, _, case = parked
        found = manual_handoff(case)
        assert found is not None
        assert found.reason_code == str(ReasonCode.NO_VALID_PROPOSAL_PRODUCED)
        assert found.tool_name is None
        assert found.idempotency_key is None

    def test_no_projected_text_names_an_implementation_class(self, parked) -> None:
        """What a caregiver can reach must not carry the vocabulary of the layer that failed."""
        _, _, case = parked
        entry = handoff_entry(case)
        assert "MalformedProposal" not in entry.summary
        assert "MalformedProposal" not in str(entry.payload)
        assert "MalformedProposal" not in case.timeline[-1].summary
        assert "MalformedProposal" not in str(case.timeline[-1].payload)


class TestTheParkedCaseCannotBeMoved:
    """What a person may do next, asserted as the negatives it actually is.

    This patch parks the case honestly; it does **not** provide an in-system way to ask CareLoop to
    try again. That is a separate capability with its own lifecycle decision, and pretending it
    exists here would be the same small lie the copy refuses to tell. So these are the assertions
    that the park is *inert* — safe, readable, and unable to walk itself into execution.
    """

    def test_there_is_no_approval_to_approve(self, parked) -> None:
        _, _, case = parked
        store = InMemoryCaseStore()
        store.create(case.model_copy(update={"case_id": "case-approve"}))
        with pytest.raises(NoSuchApproval):
            approve(store, "case-approve", "apr-1", decided_by="ada")

    def test_there_is_no_approval_to_reject(self, parked) -> None:
        _, _, case = parked
        store = InMemoryCaseStore()
        store.create(case.model_copy(update={"case_id": "case-reject"}))
        with pytest.raises(NoSuchApproval):
            reject(store, "case-reject", "apr-1", decided_by="ada")

    def test_a_re_check_is_refused(self, parked) -> None:
        _, _, case = parked
        store = InMemoryCaseStore()
        store.create(case.model_copy(update={"case_id": "case-recheck"}))
        with pytest.raises(CaseNotRecheckable):
            request_recheck(store, "case-recheck", requested_by="ada")

    def test_a_reopen_is_refused_because_only_a_finished_case_reopens(self, parked) -> None:
        _, _, case = parked
        store = InMemoryCaseStore()
        store.create(case.model_copy(update={"case_id": "case-reopen"}))
        with pytest.raises(CaseNotReopenable):
            reopen_case(store, REFERRAL_EVENT, case_id="case-reopen")

    def test_another_run_dispatches_and_commits_nothing(self, parked) -> None:
        """``AWAITING_HUMAN`` has no outgoing edge from ``dispatch``, so the run ends there."""
        _, _, case = parked
        store = InMemoryCaseStore()
        store.create(case.model_copy(update={"case_id": "case-again"}))
        before = store.get("case-again").version

        context = context_for(
            store,
            REFERRAL_EVENT,
            case_id="case-again",
            triage=_triage(),
            planner=PlannerAgent(mock_model(proposals=[])),
            now=lambda: FIXED_NOW,
        )
        result = asyncio.run(run_case(context))

        assert result.executed_nodes == ("dispatch",)
        assert store.get("case-again").version == before
        assert store.get("case-again").state is CaseState.AWAITING_HUMAN


# -- D: the production-like shape ------------------------------------------------------------------


@pytest.fixture(scope="module")
def denied_then_exhausted() -> tuple[RunResult | None, object, Case]:
    """Policy refuses a proposal; the replan that follows cannot be formed.

    This is the shape both measurements found. ``docs/model-benchmark-post-b2.md``: both malformed
    attempts landed on the replan task carrying a prior refusal, 2 of 9 replans against 0 of 27
    clean ones. On the deployed demo: both failures were on the replan call after a refusal.
    """
    return drive(_intent("send_email", POISONED_EMAIL), *malformed())


class TestADeniedProposalCannotBeResurrected:
    """The load-bearing class. A denied call must not become approvable by the case parking.

    ``latest_proposal`` still names the refused email — it is on the timeline, and the timeline is
    append-only, so of course it does. What must not exist is any route from the park to it. The
    defect this guards against is not hypothetical: raising an ``Approval`` with empty identifying
    fields is exactly what the *other* manual handoff used to do, and
    :func:`careloop.graph.runner._decide` moved such a case to ``READY_TO_EXECUTE``, where the
    execute node picked up the proposal policy had just denied and dispatched it.
    """

    def test_the_refusal_happened_and_is_still_on_the_trail(self, denied_then_exhausted) -> None:
        _, _, case = denied_then_exhausted
        denials = [e for e in case.timeline if e.kind is TimelineKind.POLICY_DENY]
        assert len(denials) == 1
        assert denials[0].payload["tool_name"] == "send_email"
        assert denials[0].payload["reason_code"] == str(ReasonCode.DESTINATION_NOT_TRUSTED)

    def test_the_case_parked_on_the_replan_not_on_the_refusal(self, denied_then_exhausted) -> None:
        """The reason code is the one about forming an action, not the one about refusing them."""
        _, _, case = denied_then_exhausted
        assert case.state is CaseState.AWAITING_HUMAN
        assert handoff_entry(case).payload["reason_code"] == str(
            ReasonCode.NO_VALID_PROPOSAL_PRODUCED
        )

    def test_the_denied_proposal_is_still_the_latest_one(self, denied_then_exhausted) -> None:
        """Stated rather than avoided: this is what makes the assertions below the sharp ones."""
        _, _, case = denied_then_exhausted
        proposal = latest_proposal(case)
        assert proposal is not None
        assert proposal.tool_name == "send_email"
        assert proposal.args["to"] == ATTACKER

    def test_no_approval_covers_the_denied_call(self, denied_then_exhausted) -> None:
        _, _, case = denied_then_exhausted
        assert case.approvals == ()
        assert case.pending_approval() is None

    def test_the_card_offers_nothing_to_approve(self, denied_then_exhausted) -> None:
        """And in particular does not render the refused email's arguments under a decision."""
        _, _, case = denied_then_exhausted
        view = decision_view(case)
        assert view is not None
        assert view.approval_id == ""
        assert view.args == {}
        assert ATTACKER not in str(view.args)

    def test_no_decision_can_release_it(self, denied_then_exhausted) -> None:
        """The two verbs that move a parked case, refused before anything moves."""
        _, _, case = denied_then_exhausted
        store = InMemoryCaseStore()
        store.create(case.model_copy(update={"case_id": "case-denied"}))
        before = store.get("case-denied").version

        for verb in (approve, reject):
            with pytest.raises(NoSuchApproval):
                verb(store, "case-denied", "apr-1", decided_by="ada")

        assert store.get("case-denied").version == before
        assert store.get("case-denied").state is CaseState.AWAITING_HUMAN

    def test_nothing_was_allowed_executed_or_suppressed(self, denied_then_exhausted) -> None:
        _, context, case = denied_then_exhausted
        assert count(case, TimelineKind.POLICY_ALLOW) == 0
        assert count(case, TimelineKind.ACTION_EXECUTED) == 0
        assert count(case, TimelineKind.DUPLICATE_SUPPRESSED) == 0
        assert case.actions == ()
        assert context.executor.invocation_count == 0

    def test_the_case_never_reached_ready_to_execute(self, denied_then_exhausted) -> None:
        _, _, case = denied_then_exhausted
        assert [
            event.seq
            for event in case.timeline
            if event.kind is TimelineKind.STATE_TRANSITION
            and event.payload["to"] == str(CaseState.READY_TO_EXECUTE)
        ] == []

    def test_the_replan_cycle_ran_once_and_then_stopped(self, denied_then_exhausted) -> None:
        """plan, gate, plan — and the second plan parks rather than proposing."""
        result, _, case = denied_then_exhausted
        assert result is not None
        assert result.executed_nodes == (*BEFORE_PLANNING, "plan", "gate", "plan")
        assert count(case, TimelineKind.PLAN_PROPOSED) == 1


# -- E: the zero-side-effect proof against a real Demo World ---------------------------------------


class TestNoExternalEffectAgainstARealClinic:
    """The strongest form of "nothing happened": a real Demo World on a loopback socket.

    ``book_appointment`` is the one tool whose effect is genuinely external, so an assertion that
    nothing reached it is an assertion about the world rather than about a mock. Both shapes are
    driven — exhaustion on the first proposal, and exhaustion on the replan after a refusal — and
    the clinic's own state is read afterwards through the object the routes hold.
    """

    def test_nothing_is_booked_when_the_first_proposal_cannot_be_formed(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        before = clinic_state.available()
        _, context, case = drive(*malformed(), clinic=clinic)

        assert case.state is CaseState.AWAITING_HUMAN
        assert clinic_state.appointments() == ()
        assert clinic_state.available() == before
        assert case.actions == ()
        assert context.executor.invocation_count == 0

    def test_nothing_is_booked_when_the_replan_cannot_be_formed(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        before = clinic_state.available()
        _, context, case = drive(_intent("send_email", POISONED_EMAIL), *malformed(), clinic=clinic)

        assert case.state is CaseState.AWAITING_HUMAN
        assert clinic_state.appointments() == ()
        assert clinic_state.available() == before
        assert context.executor.invocation_count == 0

    def test_the_clinic_really_would_have_recorded_a_booking(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """Vacuity. Without this, the two assertions above pass against a clinic nothing can reach.

        The same driver, the same socket, one valid proposal instead of a failure to form one — and
        the appointment appears and the slot is taken.
        """
        before = clinic_state.available()
        _, _, case = drive(_intent("book_appointment", BOOKING), clinic=clinic)

        assert case.state is CaseState.DONE
        assert len(clinic_state.appointments()) == 1
        assert clinic_state.available() != before


# -- F: the catch is narrow ------------------------------------------------------------------------


class TestNothingElseIsCaught:
    """``FAILED`` keeps meaning *something broke*, which is the risk in adding an ``except``.

    Each of these would be swallowed by ``except Exception``, and each is a different kind of thing
    that must never be reported as a planner that could not think of an action.
    """

    def test_an_exhausted_script_still_fails_the_case(self) -> None:
        """The harness's own signal. ``tests/test_e2e_harness.py`` depends on this exact shape.

        A consumed script and a broken CareLoop must not look alike, and the way they are told
        apart is that the first raises a type named for itself all the way out.
        """
        result, _, case = drive()

        assert result is None
        assert case.state is CaseState.FAILED
        assert case.failure_reason == "a graph node raised: MockScriptExhausted"
        assert count(case, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 0

    @pytest.mark.parametrize(
        "failure",
        [
            RuntimeError("the provider throttled this call"),
            ValueError("a credential is missing"),
            NoStructuredOutput("the stream carried no envelope"),
        ],
        ids=lambda failure: type(failure).__name__,
    )
    def test_a_non_recoverable_failure_still_fails_the_case(self, failure) -> None:
        """A throttle, a configuration error and a sibling exception the tuple deliberately omits.

        ``NoStructuredOutput`` is the sharpest of the three: it lives in the same module as the
        four that *are* caught and is not one of them, because triage's seam is a different
        question with a different lifecycle.
        """
        result, _, case = drive(failure)

        assert result is None
        assert case.state is CaseState.FAILED
        assert case.failure_reason == f"a graph node raised: {type(failure).__name__}"
        assert count(case, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 0

    def test_the_omitted_sibling_really_is_a_sibling(self) -> None:
        """Vacuity for the case above: it must be excluded by decision, not by living elsewhere."""
        assert NoStructuredOutput.__module__ == MalformedProposal.__module__
        assert NoStructuredOutput not in RECOVERABLE_PROPOSAL_FAILURES

    def test_a_failure_before_the_planner_still_fails_the_case(self) -> None:
        """Triage runs dry. The park is scoped to one call at one node and reaches nothing else."""
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=CASE_ID, now=FIXED_NOW)
        context = context_for(
            store,
            REFERRAL_EVENT,
            case_id=CASE_ID,
            triage=TriageAgent(mock_model(structured=[])),
            planner=PlannerAgent(mock_model(proposals=malformed())),
            now=lambda: FIXED_NOW,
        )
        with pytest.raises(MockScriptExhausted):
            asyncio.run(run_case(context))

        case = store.get(CASE_ID)
        assert case.state is CaseState.FAILED
        assert count(case, TimelineKind.MANUAL_HANDOFF_REQUIRED) == 0


def test_the_scripted_failures_are_the_ones_the_node_catches() -> None:
    """A last vacuity check over this file's own instrument.

    Every exception this module scripts as *recoverable* has to be a member of the tuple the node
    catches. If the tuple ever narrows, the parking tests above would start passing for the wrong
    reason — the case would fail rather than park — and that is caught elsewhere; this catches the
    opposite mistake, a test scripting something the production code never treated as recoverable.
    """
    scripted: Sequence[Exception] = (
        MalformedProposal("x"),
        NoProposalToolCall("x"),
        AmbiguousProposalToolCall("x"),
        UnknownProposalTool("x"),
    )
    assert all(isinstance(failure, RECOVERABLE_PROPOSAL_FAILURES) for failure in scripted)
