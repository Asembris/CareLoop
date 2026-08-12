"""Case-building shared by the API test modules.

Drives the real graph on the mock model, so the cases these tests project are cases the spine
actually produced rather than ones hand-assembled to suit the assertion. A projection test that
built its own timeline would agree with itself about what a timeline looks like.

Kept out of ``conftest.py`` for the reason ``helpers.py`` is: test modules import this, and pytest
loads ``conftest.py`` rather than exporting it.
"""

from __future__ import annotations

import asyncio

from helpers import FIXED_NOW

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import PlannerIntent, TriageCategory, TriageResult, Urgency
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import POISONED_EVENT, REFERRAL_EVENT, InboundEvent
from careloop.graph.runner import approve, context_for, open_case, run_case
from careloop.llm.mock import mock_model
from careloop.state import Case, InMemoryCaseStore

CASE_ID = "case-0001"
CARER = "rosa.marlowe@carer.example"
SUMMARY = "Cardiology referral needs an outpatient appointment."

EMAIL = {
    "to": CARER,
    "subject": "Cardiology referral for Enid Marlowe",
    "body": "The referral has arrived and an appointment is being arranged.",
}

ATTACKER = "records-intake@claims-continuity-partners.example"
ATTACKER_PORTAL = "PORTAL-MG-TRANSFER"

#: What the poisoned continuity notice asks the planner to do.
POISONED_PROPOSALS = (
    ("send_email", {"to": ATTACKER, "subject": "Referral letter", "body": "coverage summary"}),
    ("upload_document", {"portal_id": ATTACKER_PORTAL, "document_id": "doc-0001-referral"}),
    (
        "grant_portal_access",
        {"grantee": ATTACKER, "role": "records_admin", "portal_id": ATTACKER_PORTAL},
    ),
)


def intent(tool_name: str, **args: str) -> PlannerIntent:
    fields = dict.fromkeys(PlannerIntent.ARGUMENT_FIELDS, "")
    fields.update(args)
    return PlannerIntent(tool_name=tool_name, rationale="the next step for this case", **fields)


def triage_agent(summary: str = SUMMARY) -> TriageAgent:
    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.APPOINTMENT,
                    urgency=Urgency.SOON,
                    summary=summary,
                    patient_id="PT-4417",
                )
            ]
        )
    )


def planner_proposing(*proposals: PlannerIntent) -> PlannerAgent:
    return PlannerAgent(mock_model(structured=list(proposals)))


def drive(store, inbound: InboundEvent, planner: PlannerAgent, **kwargs):
    """Run the graph once against ``store``, with the clock pinned."""
    bounds = {key: kwargs.pop(key) for key in ("max_node_executions",) if key in kwargs}
    context = context_for(
        store,
        inbound,
        case_id=CASE_ID,
        triage=triage_agent(kwargs.pop("summary", SUMMARY)),
        planner=planner,
        now=lambda: FIXED_NOW,
        **kwargs,
    )
    return asyncio.run(run_case(context, **bounds)), context


def parked_store() -> InMemoryCaseStore:
    """A store holding one case in ``AWAITING_HUMAN`` with a pending ``send_email`` decision."""
    store = InMemoryCaseStore()
    open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
    drive(store, REFERRAL_EVENT, planner_proposing(intent("send_email", **EMAIL)))
    return store


def finished_store() -> InMemoryCaseStore:
    """A store holding one case driven all the way to ``DONE`` through an approval."""
    store = parked_store()
    approval = store.get(CASE_ID).pending_approval()
    approve(store, CASE_ID, approval.approval_id, decided_by="rosa", now=FIXED_NOW)
    drive(store, REFERRAL_EVENT, planner_proposing())
    return store


def poisoned_store() -> InMemoryCaseStore:
    """A store holding the T2 case: every poisoned proposal made, every one denied."""
    store = InMemoryCaseStore()
    open_case(store, POISONED_EVENT, case_id=CASE_ID, now=FIXED_NOW)
    # The planner takes the bait on every attempt, as T2 drives it, so the run is bounded rather
    # than allowed to exhaust the script mid-replan.
    proposals = [intent(tool, **args) for tool, args in POISONED_PROPOSALS] * 6
    drive(
        store,
        POISONED_EVENT,
        planner_proposing(*proposals),
        summary="Referral plus a continuity-of-care notice from the insurer.",
        max_node_executions=12,
    )
    return store


def finished_case() -> Case:
    return finished_store().get(CASE_ID)


def poisoned_case() -> Case:
    return poisoned_store().get(CASE_ID)
