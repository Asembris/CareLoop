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
from careloop.graph.context import RunContext, recheck_target
from careloop.graph.trusted import trusted_facts
from careloop.state.case import ApprovalStatus, Case, CaseState, TimelineKind
from careloop.state.machine import append_event, transition
from careloop.state.store import CaseStore, VersionConflict


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


class AmbiguousApproval(Exception):
    """Raised when an identifier names more than one decidable request, so nothing can be decided.

    An approval identifier answers *which occurrence of asking a caregiver is this decision about?*
    If two records under one id are both ``PENDING``, that question has no answer, and there is no
    safe way to guess: deciding "the last one" would silently leave a live request behind under an
    id nothing can now reach.

    Nothing writes this shape any more — :func:`careloop.graph.context.approval_id_for` derives an
    identifier from the timeline position of the request rather than from the arguments, so two
    requests on one case cannot collide. It exists for what is already on disk. The pre-repair
    manual handoff called every parking ``apr-no-safe-action:c``, so a case that parked, was
    reopened by a later event and parked again carries two identical identifiers, both pending.

    Refusing rather than repairing is the point. Rewriting persisted records to make them unique
    would be a migration that edits history to make history tidy, which is the opposite of what an
    append-only audit trail is for.
    """


class CaseNotRecheckable(Exception):
    """Raised when a case is not in a shape where re-observing an existing effect means anything.

    **One exception and one message for every refused precondition**, which is deliberate rather
    than lazy. The reasons a re-check may be refused — wrong state, a decision still outstanding,
    the other kind of handoff, a handoff that recorded no action identity, a key naming no record,
    an action type nothing can observe independently, a proposal that keys to a different action —
    are all *"this case is not one where looking again is a thing that can be done"*, and a caller
    who can tell them apart by probing has been handed a map of the preconditions. The closed
    :class:`careloop.policy.verdicts.ReasonCode` vocabulary exists for the same reason one layer
    down. The full picture is on the case's own timeline, which is the audit surface.

    Tests distinguish the causes by constructing them, which is where the distinction belongs.
    """


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
    except VersionConflict:
        # **Losing a race is not a failure of the case, and calling it one destroys a good case.**
        #
        # Every node reads a version and commits against it, so a second writer working the same
        # case rejects one of them at the database. That writer is not broken — the case is simply
        # *ahead* of what this run read, and the winner has already moved it somewhere correct.
        # Falling through to ``_fail`` below would then read the case fresh, find it in whatever
        # state the winner left it, see that it is not at rest, and commit ``-> FAILED`` on top: a
        # correctly progressing case terminated on behalf of the run that lost.
        #
        # Unreachable while one process ran one case at a time — the API refuses a second in-flight
        # run per case — and reachable the moment anything sweeps for abandoned work, which is what
        # ``EXECUTING`` recovery is for. The exception still leaves this function, because *this
        # run* genuinely did not complete and its caller should know; what does not happen is a
        # write.
        raise
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
    """Record that a run ended without the case resting, unless it already rested.

    **Two guards, and the second is not redundant.** ``is_at_rest`` covers a case the run itself
    finished; the ``VersionConflict`` below covers a case *somebody else* moved between the read on
    the line above and the write on the line after it. The first is a check-then-act and the second
    is what makes it safe, exactly as :meth:`careloop.state.sqlite_store.SqliteCaseStore.save`'s
    guarded ``UPDATE`` is what makes its own ``SELECT`` safe rather than a race.

    Both point the same way: a run that lost may not write. A failure recorded here would be this
    run's opinion about a case another run has already moved on, and the caller still gets the
    original exception either way.
    """
    case = context.current_case()
    if case.is_at_rest:
        return
    try:
        context.commit(
            transition(case, CaseState.FAILED, reason=reason, actor="system", now=context.now()),
            expected_version=case.version,
        )
    except VersionConflict:
        return


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


def request_recheck(
    store: CaseStore,
    case_id: str,
    *,
    requested_by: str,
    now: datetime | None = None,
) -> Case:
    """Record that a caregiver asked for an existing effect to be observed again.

    ``AWAITING_HUMAN`` → ``VERIFYING``. The next :func:`run_case` enters through the dispatch node,
    which routes on the state this leaves behind — so the re-check needs no new graph edge, and
    ``outgoing(verify)`` stays empty.

    **This recovers knowledge, never authority**, and every line of it is shaped by that. It
    proposes nothing, asks no model, evaluates no policy, raises no :class:`Approval`, dispatches no
    tool, and performs no external read itself; the graph run the caller schedules afterwards does
    the looking. What it writes is one audit entry and one transition.

    The target is resolved entirely from durable server-side state by
    :func:`careloop.graph.context.recheck_target` — **there is deliberately no parameter through
    which a caller could name one.** That is the client trust boundary made structural rather than
    validated: a browser cannot pass a tool, an argument, a digest, a confirmation id or an action
    index because the signature has nowhere to put them.

    Note what is *not* checked here. Whether a run is already in flight is execution context and
    belongs to the API layer, which refuses before calling this; whether the run that follows
    actually has an observer bound is a fact about a :class:`~careloop.graph.context.RunContext`
    that only :class:`careloop.graph.nodes.VerifyNode` can establish, and it fails closed there.
    Keeping *"is this case bound to a re-checkable action"* apart from *"can this request be
    scheduled right now"* is what lets the projector answer the first without pretending to answer
    the second.

    Everything is checked before anything moves, so a refusal cannot mean "half recorded" — no
    timeline entry, no transition, no version bump.

    Raises:
        CaseNotFound: if the store does not hold ``case_id``.
        CaseNotRecheckable: if the case carries no post-effect handoff bound to an existing,
            independently observable action whose identity the current proposal re-derives.
    """
    at = now or _utcnow()
    case = store.get(case_id)
    base = case.version

    target = recheck_target(case)
    if target is None:
        raise CaseNotRecheckable(
            f"case {case_id!r} carries no confirmable external effect to observe again; a re-check "
            f"needs a case stopped on an unconfirmed action that is durably identified and can be "
            f"read back independently"
        )

    case = append_event(
        case,
        kind=TimelineKind.EXTERNAL_RECHECK_REQUESTED,
        actor=f"human:{requested_by}",
        summary=f"{requested_by} asked for {target.tool_name} to be checked again",
        diagnostic=(
            f"{requested_by} asked CareLoop to independently observe the effect recorded under "
            f"idempotency key {target.idempotency_key} once more. Nothing is authorised by this: "
            f"the effect already happened, the recorded action is unchanged, and the only thing "
            f"that follows is another read of the external system's own record of it."
        ),
        payload={
            "tool_name": target.tool_name,
            "idempotency_key": target.idempotency_key,
        },
        now=at,
    )
    case = transition(
        case,
        CaseState.VERIFYING,
        reason=f"{requested_by} asked for the external record of {target.tool_name} to be re-read",
        actor=f"human:{requested_by}",
        now=at,
    )
    return store.save(case, expected_version=base)


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
    """Record one caregiver decision against exactly one pending request.

    **The target is found once and replaced by position.** That is the whole shape of this function
    and it is worth saying plainly, because the version before it looked equivalent and was not: it
    inspected ``matching[-1]`` and then rebuilt ``case.approvals`` replacing *every* record whose
    ``approval_id`` matched. One record was checked and N were rewritten, so the guards and the
    mutation had different scopes.

    Session K measured what that cost. A caregiver rejected a proposal, the planner reproposed the
    identical arguments, and because the identifier was derived from the arguments the second
    request arrived under the first request's name. Approving the second rewrote the first: an
    ``APPROVED`` status, a second caregiver's name and a fresh timestamp landed on a record whose
    whole purpose was to say that somebody else had refused. Authority never moved — ``is_approved``
    reads the most recent record matching the *digest*, which is the one that was actually decided —
    so this was audit integrity rather than escalation, in a system whose central claim is that the
    audit trail proves what happened.

    :func:`careloop.graph.context.approval_id_for` now makes new identifiers unique, so on anything
    written after this commit there is only ever one match. **Positional replacement is kept anyway,
    and that is deliberate**: unique identifiers cannot help the duplicates already persisted, and a
    rebuild keyed on the identifier would silently rewrite them the first time one is decided. The
    two halves are independently load-bearing and each has its own mutant.

    Everything is checked before anything moves, so a refusal cannot mean "half recorded" — no
    approval mutated, no timeline entry, no transition, no version bump.

    Raises:
        NoSuchApproval: the case carries no record under this identifier.
        AmbiguousApproval: more than one record under it is still pending, so which request is being
            decided has no answer. Only reachable for identifiers persisted before this commit.
        ApprovalAlreadyDecided: every record under it has been decided. A decision is made once.
        NotAnApprovableAction: the pending record authorises no call — see :class:`Approval`.
    """
    at = now or _utcnow()
    case = store.get(case_id)
    base = case.version

    matching = [
        index
        for index, approval in enumerate(case.approvals)
        if approval.approval_id == approval_id
    ]
    if not matching:
        raise NoSuchApproval(f"case {case_id!r} carries no approval {approval_id!r}")

    pending = [
        index for index in matching if case.approvals[index].status is ApprovalStatus.PENDING
    ]
    if len(pending) > 1:
        raise AmbiguousApproval(
            f"approval {approval_id!r} names {len(pending)} requests that are still pending, so "
            f"there is no answer to which one a decision is about; the case stays where it is"
        )
    if not pending:
        raise ApprovalAlreadyDecided(
            f"approval {approval_id!r} is already {case.approvals[matching[-1]].status}; a "
            f"decision is made once"
        )

    decided_index = pending[0]
    approval = case.approvals[decided_index]
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
                decided if index == decided_index else existing
                for index, existing in enumerate(case.approvals)
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
    "AmbiguousApproval",
    "ApprovalAlreadyDecided",
    "CaseNotRecheckable",
    "CaseNotReopenable",
    "NoSuchApproval",
    "NotAnApprovableAction",
    "RunResult",
    "approve",
    "context_for",
    "open_case",
    "reject",
    "reopen_case",
    "request_recheck",
    "run_case",
]
