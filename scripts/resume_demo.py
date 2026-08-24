"""Drive one case across three processes, days apart.

The flagship behaviour, as a thing you can run:

===== =============================================================== ================
Phase What happens                                                    The case ends
===== =============================================================== ================
1     A cardiology referral arrives. The planner proposes an email to  ``AWAITING_HUMAN``
      the caregiver. Policy requires a human decision. **Exit.**
2     The caregiver approves. The case resumes from the store, sends   ``DONE``
      the email, verifies it. **Exit.**
3     Weeks later a claim rejection arrives asking for a receipt. The  ``DONE``
      **same case** reopens, reads the new documents, uploads the
      receipt to the trusted portal, verifies it. **Exit.**
===== =============================================================== ================

Each phase is a separate operating-system process. Nothing is shared between them but the SQLite
file: no in-memory case, no warm executor ledger, no Strands session. That is the claim invariant 8
makes, and running it this way is the only way to test it — a resume reconstructed inside one
process proves nothing this behaviour exists for.

``--now`` stamps the timeline, so the audit trail shows the real shape of the story: a Friday, the
following Monday, and a fortnight later. No clock reaches ``careloop/tools/``; the graph stamps the
timeline, exactly as it does in a live run.

Usage::

    python scripts/resume_demo.py --phase 1 --db case.sqlite3 --now 2026-09-04T16:40:00+00:00
    python scripts/resume_demo.py --phase 2 --db case.sqlite3 --now 2026-09-07T09:15:00+00:00
    python scripts/resume_demo.py --phase 3 --db case.sqlite3 --now 2026-09-21T11:02:00+00:00
    python scripts/resume_demo.py --timeline --db case.sqlite3

The model is the offline mock throughout, scripted per phase, so the demo costs nothing and does
the same thing every time. What is being demonstrated is the spine, not the model.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Final

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
    proposal_tool_call_for,
)
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import CLAIM_REJECTION_EVENT, REFERRAL_EVENT, InboundEvent
from careloop.graph.runner import approve, context_for, open_case, reopen_case, run_case
from careloop.graph.trusted import DEMO_CAREGIVER, configured
from careloop.llm.mock import mock_model
from careloop.state.case import Case
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools import RECEIPT_DOCUMENT_ID

CASE_ID: Final[str] = "case-0001"
CARER: Final[str] = "rosa.marlowe@carer.example"

#: The email phase 1 proposes and phase 2 sends. To the caregiver's configured address, so the
#: destination is TRUSTED and the only thing standing in the way is the human decision that
#: ``require-human-for-outbound-email`` insists on for every outbound message.
EMAIL: Final[dict[str, str]] = {
    "to": CARER,
    "subject": "Cardiology referral for Enid Marlowe",
    "body": (
        "The referral has arrived and an outpatient cardiology appointment is being arranged "
        "for 2026-09-03 at 09:30. Arrive by 09:15."
    ),
}

#: The upload phase 3 performs. The portal comes from trusted configuration; the document
#: identifier came out of the claim rejection and was promoted to CONTROLLED only after the
#: cross-document checks agreed — the receipt is one CareLoop holds, and it settles the balance the
#: explanation of benefits left outstanding.
UPLOAD: Final[dict[str, str]] = {
    "portal_id": configured("portal_id") or "",
    "document_id": RECEIPT_DOCUMENT_ID,
}


def triage_agent(summary: str) -> TriageAgent:
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


def intent(tool_name: str, **args: str) -> PlannerIntent:
    return PlannerIntent(tool_name=tool_name, rationale="the next step for this case", args=args)


def planner_agent(*proposals: PlannerIntent) -> PlannerAgent:
    return PlannerAgent(mock_model(proposals=[proposal_tool_call_for(p) for p in proposals]))


def drive(
    store: SqliteCaseStore,
    inbound: InboundEvent,
    *,
    now: datetime,
    summary: str,
    proposals: tuple[PlannerIntent, ...],
) -> Case:
    """Run the graph once, and report what the executor actually did."""
    context = context_for(
        store,
        inbound,
        case_id=CASE_ID,
        triage=triage_agent(summary),
        planner=planner_agent(*proposals),
        now=lambda: now,
    )
    result = asyncio.run(run_case(context))
    print(f"  nodes run      : {' -> '.join(result.executed_nodes)}")
    print(f"  external calls : {context.executor.invocation_count}")
    return result.case


def phase_one(store: SqliteCaseStore, now: datetime) -> Case:
    """A referral arrives. The case parks for a caregiver decision."""
    open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=CASE_ID, now=now)
    case = drive(
        store,
        REFERRAL_EVENT,
        now=now,
        summary="Cardiology referral needs an outpatient appointment.",
        proposals=(intent("send_email", **EMAIL),),
    )
    pending = case.pending_approval()
    if pending is not None:
        print(f"  awaiting       : {pending.tool_name} ({pending.approval_id})")
    return case


def phase_two(store: SqliteCaseStore, now: datetime) -> Case:
    """The caregiver approves. A process that has never seen this case finishes it."""
    pending = store.get(CASE_ID).pending_approval()
    if pending is None:
        raise SystemExit("phase 2 expects a pending approval; run phase 1 first")
    approve(store, CASE_ID, pending.approval_id, decided_by="rosa", now=now)
    print(f"  approved       : {pending.approval_id} by rosa")

    # No proposal is scripted: the case resumes at READY_TO_EXECUTE and the plan node never runs.
    return drive(
        store,
        REFERRAL_EVENT,
        now=now,
        summary="Cardiology referral needs an outpatient appointment.",
        proposals=(),
    )


def phase_three(store: SqliteCaseStore, now: datetime) -> Case:
    """A claim rejection arrives for a case that finished a fortnight ago."""
    reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=now)
    print(f"  reopened       : {CASE_ID} by {CLAIM_REJECTION_EVENT.event_id}")
    return drive(
        store,
        CLAIM_REJECTION_EVENT,
        now=now,
        summary="The insurer rejected the claim and wants the payment receipt.",
        proposals=(intent("upload_document", **UPLOAD),),
    )


PHASES = {1: phase_one, 2: phase_two, 3: phase_three}


def show_timeline(store: SqliteCaseStore) -> None:
    """Print the case's audit trail, which is the thing worth looking at."""
    case = store.get(CASE_ID)
    print(f"case {case.case_id}  state={case.state}  version={case.version}")
    print(f"opened by {case.event_id} at {case.created_at.isoformat()}")
    print()
    for entry in case.timeline:
        print(f"  {entry.seq:>3}  {entry.at.isoformat()}  {entry.kind:<26} {entry.summary}")
    print()
    print(f"  facts   : {len(case.facts)}")
    print(f"  actions : {len(case.actions)}")
    for action in case.actions:
        print(f"            {action.action_type}  key={action.idempotency_key[:16]}")


def report(case: Case) -> None:
    print(f"  case state     : {case.state}")
    print(f"  facts          : {len(case.facts)}")
    print(f"  actions        : {len(case.actions)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="SQLite file holding the case")
    parser.add_argument("--phase", type=int, choices=sorted(PHASES), help="which phase to run")
    parser.add_argument("--now", type=str, help="ISO timestamp to stamp the timeline with")
    parser.add_argument("--timeline", action="store_true", help="print the case timeline and exit")
    args = parser.parse_args(argv)

    store = SqliteCaseStore(args.db)
    try:
        if args.timeline:
            show_timeline(store)
            return 0

        if args.phase is None:
            parser.error("one of --phase or --timeline is required")

        now = datetime.fromisoformat(args.now) if args.now else None
        if now is None:
            parser.error("--now is required so the three phases are days apart on the timeline")

        print(f"phase {args.phase}  at {now.isoformat()}")
        case = PHASES[args.phase](store, now)
        report(case)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
