"""Opening a case, the caregiver decisions, and what a run reports.

The end-to-end scenarios live in ``tests/test_case_run.py``. This module checks the pieces around
them: that a case is seeded before any document is read, that an approval authorises one specific
call rather than a tool, and that the runner reacts to the SDK's bound instead of keeping a count
of its own.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import FIXED_NOW, make_case
from pydantic import ValidationError

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
    proposal_tool_call_for,
)
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import (
    EVENTS,
    POISONED_EVENT,
    POISONED_EVENT_ID,
    REFERRAL_EVENT,
    InboundEvent,
    event,
)
from careloop.graph.context import approval_id_for
from careloop.graph.runner import (
    ApprovalAlreadyDecided,
    NoSuchApproval,
    approve,
    context_for,
    open_case,
    reject,
    run_case,
)
from careloop.graph.trusted import DEMO_CAREGIVER, TRUSTED_CONFIG
from careloop.llm.mock import mock_model
from careloop.policy.verdicts import ReasonCode
from careloop.provenance import TrustLevel
from careloop.state import (
    ApprovalStatus,
    CaseState,
    IllegalTransition,
    InMemoryCaseStore,
    TimelineKind,
)
from careloop.tools.documents import POISONED_DOCUMENT_ID
from careloop.tools.idempotency import args_digest

CARER = "rosa.marlowe@carer.example"
EMAIL = {"to": CARER, "subject": "Referral received", "body": "Arranging the appointment."}


def intent(tool_name: str, **args: str) -> PlannerIntent:
    return PlannerIntent(tool_name=tool_name, rationale="the next step", args=args)


def triage_agent() -> TriageAgent:
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


def parked_case(store: InMemoryCaseStore, case_id: str = "case-0001"):
    """Run a case up to the point a caregiver decision is outstanding."""
    open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=case_id, now=FIXED_NOW)
    context = context_for(
        store,
        REFERRAL_EVENT,
        case_id=case_id,
        triage=triage_agent(),
        planner=PlannerAgent(
            mock_model(proposals=[proposal_tool_call_for(intent("send_email", **EMAIL))])
        ),
        now=lambda: FIXED_NOW,
    )
    return asyncio.run(run_case(context)), context


class TestOpeningACase:
    def test_a_new_case_starts_seeded_with_trusted_configuration(self) -> None:
        store = InMemoryCaseStore()

        case = open_case(
            store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-0001", now=FIXED_NOW
        )

        assert case.state is CaseState.NEW
        assert {fact.key for fact in case.facts} == {key for key, _ in TRUSTED_CONFIG}
        assert {fact.trust_level for fact in case.facts} == {TrustLevel.TRUSTED}

    def test_seeding_happens_before_any_document_is_read(self) -> None:
        """The code path that mints TRUSTED and the one that reads attacker text are separate."""
        store = InMemoryCaseStore()

        case = open_case(
            store, POISONED_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-0001", now=FIXED_NOW
        )

        assert all(fact.provenance.source_doc == "trusted-config" for fact in case.facts)
        assert not any(fact.key.startswith("excerpt:") for fact in case.facts)

    def test_the_case_records_where_it_came_from(self) -> None:
        store = InMemoryCaseStore()

        case = open_case(
            store, POISONED_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-0001", now=FIXED_NOW
        )

        opened = case.timeline[0]
        assert opened.kind is TimelineKind.CASE_CREATED
        assert opened.payload["event_id"] == POISONED_EVENT_ID
        assert POISONED_DOCUMENT_ID in opened.payload["document_ids"]

    def test_the_case_is_persisted(self) -> None:
        store = InMemoryCaseStore()

        open_case(
            store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-0001", now=FIXED_NOW
        )

        assert store.get("case-0001").event_id == REFERRAL_EVENT.event_id


class TestTheInboundEvents:
    def test_triage_reads_the_subject_and_the_body(self) -> None:
        text = REFERRAL_EVENT.text

        assert REFERRAL_EVENT.subject in text
        assert REFERRAL_EVENT.body in text

    def test_the_poisoned_event_attaches_the_poisoned_document(self) -> None:
        assert POISONED_DOCUMENT_ID in POISONED_EVENT.document_ids

    def test_the_clean_event_does_not(self) -> None:
        assert POISONED_DOCUMENT_ID not in REFERRAL_EVENT.document_ids

    def test_an_event_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            REFERRAL_EVENT.subject = "something else"

    def test_looking_up_a_missing_event_raises_rather_than_returning_nothing(self) -> None:
        with pytest.raises(KeyError):
            event("evt-does-not-exist")

    def test_every_declared_event_is_reachable_by_id(self) -> None:
        for event_id in EVENTS:
            assert event(event_id).event_id == event_id

    def test_no_event_body_claims_authority_the_trust_model_would_grant(self) -> None:
        """Events are untrusted text. None of their content becomes a TRUSTED fact."""
        configured = {value for _key, value in TRUSTED_CONFIG}
        for inbound in EVENTS.values():
            assert configured.isdisjoint({inbound.text})


class TestApproval:
    def test_a_decision_request_is_raised_and_the_case_parks(self) -> None:
        store = InMemoryCaseStore()

        result, context = parked_case(store)

        assert result.case.state is CaseState.AWAITING_HUMAN
        assert len(result.case.approvals) == 1
        assert result.case.pending_approval().reason_code == ReasonCode.REQUIRES_HUMAN_APPROVAL
        assert context.executor.invocation_count == 0

    def test_the_approval_covers_the_arguments_rather_than_the_tool(self) -> None:
        store = InMemoryCaseStore()

        result, _ = parked_case(store)

        approval = result.case.pending_approval()
        assert approval.args_digest == args_digest(EMAIL)

    def test_the_id_names_the_request_rather_than_the_arguments(self) -> None:
        """Two identities, and the identifier is the one that says *which time we asked*.

        It was ``approval_id_for(args_digest(EMAIL))`` until session K, which is what made a
        reproposed action arrive under an earlier request's name. See
        ``tests/test_approval_identity.py`` for what that cost and why the seed is the timeline
        position of the ``APPROVAL_REQUESTED`` entry instead.
        """
        store = InMemoryCaseStore()

        result, _ = parked_case(store)

        approval = result.case.pending_approval()
        requested = next(
            event for event in result.case.timeline if event.kind is TimelineKind.APPROVAL_REQUESTED
        )
        assert approval.approval_id == approval_id_for(requested.seq)
        assert args_digest(EMAIL) not in approval.approval_id

    def test_approving_releases_the_case_for_execution(self) -> None:
        store = InMemoryCaseStore()
        result, _ = parked_case(store)

        case = approve(
            store,
            "case-0001",
            result.case.pending_approval().approval_id,
            decided_by="rosa",
            now=FIXED_NOW,
        )

        assert case.state is CaseState.READY_TO_EXECUTE
        assert case.approvals[-1].status is ApprovalStatus.APPROVED
        assert case.approvals[-1].decided_by == "rosa"
        assert case.timeline[-2].kind is TimelineKind.APPROVAL_GRANTED

    def test_rejecting_sends_the_case_back_to_the_planner(self) -> None:
        store = InMemoryCaseStore()
        result, _ = parked_case(store)

        case = reject(
            store,
            "case-0001",
            result.case.pending_approval().approval_id,
            decided_by="rosa",
            now=FIXED_NOW,
        )

        assert case.state is CaseState.PLANNING
        assert case.approvals[-1].status is ApprovalStatus.REJECTED
        assert case.timeline[-2].kind is TimelineKind.APPROVAL_REJECTED

    def test_a_decision_is_made_once(self) -> None:
        store = InMemoryCaseStore()
        result, _ = parked_case(store)
        approval_id = result.case.pending_approval().approval_id
        approve(store, "case-0001", approval_id, decided_by="rosa", now=FIXED_NOW)

        with pytest.raises(ApprovalAlreadyDecided):
            approve(store, "case-0001", approval_id, decided_by="rosa", now=FIXED_NOW)

    def test_an_unknown_approval_is_refused(self) -> None:
        store = InMemoryCaseStore()
        parked_case(store)

        with pytest.raises(NoSuchApproval):
            approve(store, "case-0001", "apr-nonexistent", decided_by="rosa", now=FIXED_NOW)

    def test_a_decision_on_a_case_that_is_not_parked_is_refused_by_the_state_machine(self) -> None:
        """The transition table is the guard; the runner does not need a second one."""
        store = InMemoryCaseStore()
        result, _ = parked_case(store)
        approval_id = result.case.pending_approval().approval_id
        approve(store, "case-0001", approval_id, decided_by="rosa", now=FIXED_NOW)
        # A second, still-pending approval on a case that has already left AWAITING_HUMAN.
        case = store.get("case-0001")
        second = case.approvals[0].model_copy(
            update={"approval_id": "apr-second", "status": ApprovalStatus.PENDING}
        )
        store.save(
            case.model_copy(
                update={"approvals": (*case.approvals, second), "version": case.version}
            ),
            expected_version=case.version,
        )

        with pytest.raises(IllegalTransition):
            approve(store, "case-0001", "apr-second", decided_by="rosa", now=FIXED_NOW)

    def test_the_approval_is_durable(self) -> None:
        """Invariant 8: the decision is a business fact and lives on the case, not in a session."""
        store = InMemoryCaseStore()
        result, _ = parked_case(store)
        approve(
            store,
            "case-0001",
            result.case.pending_approval().approval_id,
            decided_by="rosa",
            now=FIXED_NOW,
        )

        assert store.get("case-0001").is_approved("send_email", args_digest(EMAIL))

    def test_an_approval_does_not_authorise_a_different_call(self) -> None:
        store = InMemoryCaseStore()
        result, _ = parked_case(store)
        approve(
            store,
            "case-0001",
            result.case.pending_approval().approval_id,
            decided_by="rosa",
            now=FIXED_NOW,
        )

        other = {**EMAIL, "body": "something the caregiver never read"}
        assert not store.get("case-0001").is_approved("send_email", args_digest(other))
        assert not store.get("case-0001").is_approved("upload_document", args_digest(EMAIL))


class TestTheRunnerReactsToTheSdkRatherThanCounting:
    def test_a_completed_run_reports_the_nodes_that_ran(self) -> None:
        store = InMemoryCaseStore()

        result, _ = parked_case(store)

        assert result.executed_nodes == ("dispatch", "triage", "evidence", "plan", "gate")
        assert not result.hit_the_bound

    def test_a_bounded_run_reports_that_it_hit_the_bound(self) -> None:
        store = InMemoryCaseStore()
        open_case(
            store, POISONED_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-0001", now=FIXED_NOW
        )
        attacker = "records-intake@claims-continuity-partners.example"
        context = context_for(
            store,
            POISONED_EVENT,
            case_id="case-0001",
            triage=triage_agent(),
            planner=PlannerAgent(
                mock_model(
                    # Distinct every time: a planner that *repeats* a refused proposal is now
                    # parked by the liveness rule and never reaches the bound. A planner that keeps
                    # inventing new ones is the runaway the bound is for.
                    proposals=[
                        proposal_tool_call_for(
                            intent("send_email", to=attacker, subject="s", body=f"body {n}")
                        )
                        for n in range(40)
                    ]
                )
            ),
            now=lambda: FIXED_NOW,
        )

        result = asyncio.run(run_case(context, max_node_executions=10))

        assert result.hit_the_bound
        assert len(result.executed_nodes) == 10

    def test_a_case_that_already_rested_is_not_overwritten(self) -> None:
        """A DONE case re-driven hits no node at all; the runner must leave it alone."""
        store = InMemoryCaseStore()
        result, _ = parked_case(store)
        approve(
            store,
            "case-0001",
            result.case.pending_approval().approval_id,
            decided_by="rosa",
            now=FIXED_NOW,
        )
        finished = asyncio.run(
            run_case(
                context_for(
                    store,
                    REFERRAL_EVENT,
                    case_id="case-0001",
                    triage=triage_agent(),
                    planner=PlannerAgent(mock_model(structured=[])),
                    now=lambda: FIXED_NOW,
                )
            )
        )
        assert finished.case.state is CaseState.DONE

        again = asyncio.run(
            run_case(
                context_for(
                    store,
                    REFERRAL_EVENT,
                    case_id="case-0001",
                    triage=triage_agent(),
                    planner=PlannerAgent(mock_model(structured=[])),
                    now=lambda: FIXED_NOW,
                )
            )
        )

        assert again.case.state is CaseState.DONE
        assert again.case.version == finished.case.version

    def test_the_runner_keeps_no_count_of_its_own(self) -> None:
        """Invariant 5. What it reports is what the SDK's execution order says."""
        import inspect

        from careloop.graph import runner

        source = inspect.getsource(runner)
        assert "+= 1" not in source
        assert "range(" not in source


class TestRunResultCarriesTheCase:
    def test_the_case_comes_back_from_the_store_rather_than_from_memory(self) -> None:
        store = InMemoryCaseStore()

        result, _ = parked_case(store)

        assert result.case == store.get("case-0001")

    def test_a_run_result_is_frozen(self) -> None:
        store = InMemoryCaseStore()
        result, _ = parked_case(store)

        with pytest.raises(ValidationError):
            result.case = make_case()


class TestEventsAreOnlyEverUntrusted:
    def test_no_module_under_events_can_mint_trust(self) -> None:
        from pathlib import Path

        package = Path(__file__).resolve().parent.parent / "careloop" / "events"
        for module in package.rglob("*.py"):
            source = module.read_text(encoding="utf-8")
            assert "TrustLevel.TRUSTED" not in source
            assert "trusted_facts" not in source

    def test_an_inbound_event_declares_only_attachments_not_content(self) -> None:
        assert isinstance(REFERRAL_EVENT, InboundEvent)
        assert all(isinstance(document_id, str) for document_id in REFERRAL_EVENT.document_ids)
