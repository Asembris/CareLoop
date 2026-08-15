"""Opening a case, running it, and the caregiver decisions that unpark it.

Four functions. Everything they do is deterministic; the only model calls in a run happen inside
the triage and plan nodes.

**Human-in-the-loop is park-and-resume, not the SDK's interrupt mechanism.** That was a choice, so
here is the reasoning rather than just the result.

An approval is a *business fact*. Invariant 8 says the ``CaseStore`` is the source of truth for
approvals and that Strands session persistence holds execution context only, so the approval has
to live on the case whatever else happens. Driving it through ``Interrupt`` as well would give one
fact two homes and two answers to "was this approved?", and the interesting failure is the one
where those answers differ.

Cross-process resume is also the point. The demo case is one parked on a Friday and approved on a
Monday, and what makes that work is :func:`careloop.graph.nodes.DispatchNode` routing on a case
loaded from the store — **a ``CaseStore`` reconstruction across a process boundary, not a Strands
session restore.** Nothing about the graph's own execution state needs to survive; the case
carries everything, which is what invariant 8 was for.

``Case`` already modelled all of it before this commit: ``Approval``, ``pending_approval()``,
``AWAITING_HUMAN``, and edges from it to ``READY_TO_EXECUTE``, ``PLANNING`` and ``FAILED``.

**The loop bound is the SDK's and the runner only reacts to it.** When
``set_max_node_executions`` fires, the graph stops scheduling and returns ``Status.FAILED`` without
raising. :func:`run_case` turns that into a ``FAILED`` case. It counts nothing and re-derives
nothing — a counter here would be the hand-rolled loop bound invariant 5 forbids, wearing a
different hat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from strands.multiagent.base import Status

from careloop.events.inbound import InboundEvent
from careloop.graph.builder import (
    EXECUTION_TIMEOUT,
    MAX_NODE_EXECUTIONS,
    NODE_TIMEOUT,
    build_graph,
)
from careloop.graph.context import RunContext
from careloop.graph.trusted import trusted_facts
from careloop.state.case import ApprovalStatus, Case, CaseState, TimelineKind
from careloop.state.machine import append_event, transition
from careloop.state.store import CaseStore


class NoSuchApproval(Exception):
    """Raised when a decision names an approval the case does not carry."""


class CaseNotReopenable(Exception):
    """Raised when an inbound event tries to reopen a case that is not finished.

    Only ``DONE`` reopens. A case still in flight does not need waking up — the event's documents
    would race the ones the run is already reading — and a ``FAILED`` case is terminal, which is a
    decision about failure this project has not revisited.
    """


class ApprovalAlreadyDecided(Exception):
    """Raised when a decision is recorded against an approval that is no longer pending."""


class NotAnApprovableAction(Exception):
    """Raised when a decision names a record that authorises no call.

    A caregiver decision releases **one exact call**, matched on the digest of its arguments. A
    record naming no tool and covering no arguments is not a request for authority, so there is
    nothing a decision about it could mean — and acting on one is not a no-op: :func:`_decide`
    transitions the case, and ``READY_TO_EXECUTE`` puts whatever the planner last proposed back in
    front of the executor. Before this guard existed, approving one released a case whose last
    proposal had been *denied*, which the tool boundary then refused a second time and recorded as
    a disagreement between CareLoop's two enforcement points. They had not disagreed; nothing had
    ever allowed the call.

    Manual handoffs are no longer written as approvals — see
    :func:`careloop.graph.nodes._park_with_nothing_safe_to_do` — so the only records that can reach
    this are ones persisted before that change. They stay readable and stay parked; what they
    cannot do is move.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RunResult(BaseModel):
    """What one graph run did."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    case: Case
    """The case as the store holds it after the run."""

    status: Any
    """The SDK's ``Status`` for the graph execution."""

    executed_nodes: tuple[str, ...]
    """Node ids in execution order, for reading a run back."""

    @property
    def hit_the_bound(self) -> bool:
        """Whether the SDK stopped the run rather than the case reaching a resting state."""
        return self.status is Status.FAILED


def open_case(
    store: CaseStore,
    inbound: InboundEvent,
    *,
    case_id: str,
    now: datetime | None = None,
) -> Case:
    """Open a new case for ``inbound`` and seed it with trusted configuration.

    Seeding happens here, before any document is read, so the code path that mints ``TRUSTED``
    facts and the code path that touches attacker-controlled text are never the same function.
    """
    at = now or _utcnow()
    case = Case(
        case_id=case_id,
        event_id=inbound.event_id,
        state=CaseState.NEW,
        facts=trusted_facts(now=at),
        created_at=at,
        updated_at=at,
    )
    case = append_event(
        case,
        kind=TimelineKind.CASE_CREATED,
        actor="system",
        summary=f"case opened from {inbound.event_id}",
        diagnostic=(
            f"subject={inbound.subject!r}, documents={list(inbound.document_ids)}; "
            f"seeded {len(case.facts)} trusted configuration fact(s)"
        ),
        payload={
            "event_id": inbound.event_id,
            "document_ids": list(inbound.document_ids),
        },
        now=at,
    )
    return store.create(case)


def reopen_case(
    store: CaseStore,
    inbound: InboundEvent,
    *,
    case_id: str,
    now: datetime | None = None,
) -> Case:
    """Reopen a finished case for a follow-up event. ``DONE`` → ``GATHERING_EVIDENCE``.

    **This is the only way out of ``DONE``, and only an inbound event may take it.** A claim
    rejected weeks after the appointment was arranged is the same piece of work: the same patient,
    the same claim, the same evidence, and a caregiver who would think of it as one thing. Opening
    a second case would split the audit trail in half and lose the idempotency record that stops
    the first case's effects happening twice.

    The agent cannot do this. No graph edge leaves ``DONE``, so no node can route into a reopen,
    and ``tests/test_reopen.py`` fails if any node, agent, tool or policy module so much as names
    this function. The world can; the agent cannot.

    What survives is everything: the case id, every fact with its provenance, every approval, every
    recorded action, and the whole timeline. What changes is the state and one ``CASE_REOPENED``
    entry naming the event that did it. ``Case.event_id`` is **not** rewritten — it records which
    event opened the case, and a reopen is not an opening.

    The follow-up's own documents are read by the next run, because
    :func:`context_for` scopes ``document_ids`` to the event it is given. Documents the case
    already holds are not re-read, and a value it already carries is not recorded twice — see
    :func:`careloop.state.evidence.record_fact`.

    Raises:
        CaseNotFound: if the store does not hold ``case_id``. A reopen names an existing case; a
            missing one is a caller error rather than a reason to create anything.
        CaseNotReopenable: if the case is not ``DONE``.
    """
    at = now or _utcnow()
    case = store.get(case_id)
    base = case.version

    if case.state is not CaseState.DONE:
        raise CaseNotReopenable(
            f"case {case_id!r} is {case.state} and only DONE reopens; an inbound event does not "
            f"interrupt a case that is still in flight"
        )

    case = append_event(
        case,
        kind=TimelineKind.CASE_REOPENED,
        actor="system",
        summary=f"reopened by {inbound.event_id}",
        diagnostic=(
            f"{inbound.event_id} arrived for a case that had reached DONE; subject="
            f"{inbound.subject!r}, documents={list(inbound.document_ids)}. The case keeps its "
            f"id, its evidence, its approvals and its recorded actions, so nothing already done "
            f"can be done again."
        ),
        payload={
            "event_id": inbound.event_id,
            "document_ids": list(inbound.document_ids),
            "opened_by": case.event_id,
        },
        now=at,
    )
    case = transition(
        case,
        CaseState.GATHERING_EVIDENCE,
        reason=f"reopened by {inbound.event_id}",
        actor="system",
        now=at,
    )
    return store.save(case, expected_version=base)


def context_for(
    store: CaseStore,
    inbound: InboundEvent,
    *,
    case_id: str,
    **kwargs: Any,
) -> RunContext:
    """Build a run context bound to one case and one inbound event."""
    return RunContext(
        case_id=case_id,
        store=store,
        event_text=inbound.text,
        document_ids=inbound.document_ids,
        **kwargs,
    )


async def run_case(
    context: RunContext,
    *,
    max_node_executions: int = MAX_NODE_EXECUTIONS,
    execution_timeout: float = EXECUTION_TIMEOUT,
    node_timeout: float = NODE_TIMEOUT,
) -> RunResult:
    """Run the graph until the case reaches a resting state or the SDK stops it.

    A run ends three ways. The case reaches ``DONE`` or ``FAILED``; the case parks in
    ``AWAITING_HUMAN``, where no outgoing edge condition holds; or the SDK's bound fires. Only the
    third needs anything from this function, and all it does is record what the SDK decided.
    """
    graph = build_graph(
        context,
        max_node_executions=max_node_executions,
        execution_timeout=execution_timeout,
        node_timeout=node_timeout,
    )

    try:
        result = await graph.invoke_async(f"run case {context.case_id}")
    except Exception as failure:
        # Node failures are fail-fast in the SDK and re-raised to here. Mark the case before
        # letting the exception out, so a crashed run leaves a case that says so rather than one
        # frozen mid-transition.
        _fail(context, f"a graph node raised: {type(failure).__name__}")
        raise

    if result.status is Status.FAILED:
        _fail(
            context,
            f"the graph stopped after {len(result.execution_order)} node execution(s) without "
            f"the case reaching a resting state",
        )

    return RunResult(
        case=context.current_case(),
        status=result.status,
        executed_nodes=tuple(node.node_id for node in result.execution_order),
    )


def _fail(context: RunContext, reason: str) -> None:
    """Record that a run ended without the case resting, unless it already rested."""
    case = context.current_case()
    if case.is_at_rest:
        return
    context.commit(
        transition(case, CaseState.FAILED, reason=reason, actor="system", now=context.now()),
        expected_version=case.version,
    )


def approve(
    store: CaseStore,
    case_id: str,
    approval_id: str,
    *,
    decided_by: str,
    now: datetime | None = None,
) -> Case:
    """Record a caregiver's approval and release the case for execution.

    ``AWAITING_HUMAN`` → ``READY_TO_EXECUTE``. The next :func:`run_case` enters through the
    dispatch node, which routes on the state this leaves behind — so the resume needs nothing but
    the store, and works from a process that has never seen this case before.
    """
    return _decide(
        store,
        case_id,
        approval_id,
        status=ApprovalStatus.APPROVED,
        kind=TimelineKind.APPROVAL_GRANTED,
        target=CaseState.READY_TO_EXECUTE,
        reason="caregiver approved the proposed action",
        decided_by=decided_by,
        now=now,
    )


def reject(
    store: CaseStore,
    case_id: str,
    approval_id: str,
    *,
    decided_by: str,
    now: datetime | None = None,
) -> Case:
    """Record a caregiver's refusal and send the case back to the planner.

    ``AWAITING_HUMAN`` → ``PLANNING``. A refused proposal is not a failed case: the planner gets
    another attempt, bounded like every other.
    """
    return _decide(
        store,
        case_id,
        approval_id,
        status=ApprovalStatus.REJECTED,
        kind=TimelineKind.APPROVAL_REJECTED,
        target=CaseState.PLANNING,
        reason="caregiver refused the proposed action",
        decided_by=decided_by,
        now=now,
    )


def _decide(
    store: CaseStore,
    case_id: str,
    approval_id: str,
    *,
    status: ApprovalStatus,
    kind: TimelineKind,
    target: CaseState,
    reason: str,
    decided_by: str,
    now: datetime | None,
) -> Case:
    at = now or _utcnow()
    case = store.get(case_id)
    base = case.version

    matching = [approval for approval in case.approvals if approval.approval_id == approval_id]
    if not matching:
        raise NoSuchApproval(f"case {case_id!r} carries no approval {approval_id!r}")

    approval = matching[-1]
    if approval.status is not ApprovalStatus.PENDING:
        raise ApprovalAlreadyDecided(
            f"approval {approval_id!r} is already {approval.status}; a decision is made once"
        )
    # Before any mutation, so a refusal cannot mean "half recorded". The store is untouched, the
    # version does not move, and the caller's request is all-or-nothing.
    if not approval.is_actionable:
        raise NotAnApprovableAction(
            f"approval {approval_id!r} names no tool and covers no arguments, so there is nothing "
            f"for a decision to authorise; the case stays where it is"
        )

    decided = approval.model_copy(
        update={"status": status, "decided_at": at, "decided_by": decided_by}
    )
    case = case.model_copy(
        update={
            "approvals": tuple(
                decided if existing.approval_id == approval_id else existing
                for existing in case.approvals
            )
        }
    )
    case = append_event(
        case,
        kind=kind,
        actor=f"human:{decided_by}",
        summary=f"{decided.tool_name} {status.lower()} by {decided_by}",
        diagnostic=(
            f"approval {approval_id} covering args digest {approval.args_digest} was "
            f"{status} by {decided_by}"
        ),
        payload={
            "approval_id": approval_id,
            "tool_name": decided.tool_name,
            "status": str(status),
            "decided_by": decided_by,
        },
        now=at,
    )
    case = transition(case, target, reason=reason, actor=f"human:{decided_by}", now=at)
    return store.save(case, expected_version=base)


__all__ = [
    "ApprovalAlreadyDecided",
    "CaseNotReopenable",
    "NoSuchApproval",
    "NotAnApprovableAction",
    "RunResult",
    "approve",
    "context_for",
    "open_case",
    "reject",
    "reopen_case",
    "run_case",
]
