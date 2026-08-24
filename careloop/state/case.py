"""The durable case model.

``Case`` is frozen. Nothing mutates a case in place; every change produces a new instance with an
incremented ``version``. State specifically may only change through
:func:`careloop.state.machine.transition`, which is the sole writer of ``Case.state``.

Freezing is what makes that claim enforceable rather than aspirational: a future caller that tries
``case.state = CaseState.DONE`` raises at runtime instead of quietly succeeding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from careloop.provenance.facts import Fact


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CaseState(StrEnum):
    """Lifecycle states. Transitions are constrained by ``careloop.state.machine.TRANSITIONS``."""

    NEW = "NEW"
    GATHERING_EVIDENCE = "GATHERING_EVIDENCE"
    PLANNING = "PLANNING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    READY_TO_EXECUTE = "READY_TO_EXECUTE"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    DONE = "DONE"
    FAILED = "FAILED"


class TimelineKind(StrEnum):
    """Kinds of audit-timeline entry."""

    CASE_CREATED = "CASE_CREATED"
    CASE_REOPENED = "CASE_REOPENED"
    STATE_TRANSITION = "STATE_TRANSITION"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    EVIDENCE_DUPLICATE_SKIPPED = "EVIDENCE_DUPLICATE_SKIPPED"
    VALIDATION_REFUSED = "VALIDATION_REFUSED"
    FACT_PROMOTED = "FACT_PROMOTED"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    POLICY_ALLOW = "POLICY_ALLOW"
    POLICY_DENY = "POLICY_DENY"
    POLICY_REQUIRE_HUMAN = "POLICY_REQUIRE_HUMAN"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    MANUAL_HANDOFF_REQUIRED = "MANUAL_HANDOFF_REQUIRED"
    """Every available action was refused; CareLoop has stopped and a person must take over.

    Deliberately **not** ``APPROVAL_REQUESTED``, which means *"I have one exact action and need
    your authority for it."* This means *"I have no safe action left."* The two were written under
    one kind until the parking stopped raising an :class:`Approval` for the second, and sharing a
    kind was not merely imprecise: it put a claim in an append-only audit trail that was not true,
    and left the strongest thing CareLoop does indistinguishable from the ordinary thing it does.

    Since a handoff carries no approval, this entry is the whole durable record of it — the case's
    state says a human is needed and this says why.
    """
    EXTERNAL_RECHECK_REQUESTED = "EXTERNAL_RECHECK_REQUESTED"
    """A person asked CareLoop to observe an already-recorded effect again.

    The audit fact behind ``AWAITING_HUMAN → VERIFYING``, which without it would be a state change
    on a parked case that the trail could not explain. It is written only by
    :func:`careloop.graph.runner.request_recheck` and carries the identity of the exact action to
    be re-observed.

    **It requests a look, not an act.** It is deliberately not ``APPROVAL_GRANTED`` — nothing is
    authorised, and the effect it names already happened — and deliberately not ``CASE_REOPENED``,
    which is an inbound event arriving at a finished case. Recording it as either would put a claim
    in an append-only trail that is false in both directions: that a caregiver granted authority,
    or that new work arrived.
    """

    ACTION_PREPARED = "ACTION_PREPARED"
    """The exact previously-authorised logical action is durably frozen and eligible for dispatch.

    **It does not say that any provider request left the process**, and nothing may read it as
    though it did. It says that CareLoop has written down, before anything could reach the outside
    world, *which* action it is about to attempt: the tool, the digest of the arguments policy
    allowed, and the idempotency key those arguments derive. That is the durable intent, and it is
    what lets a later process reuse the identity of an attempt it never saw rather than inventing
    one.

    Written by :class:`careloop.graph.nodes.ExecuteNode` alone, in the **same commit** as
    ``READY_TO_EXECUTE -> EXECUTING``, so "the case is EXECUTING" and "an attempt is durably
    prepared" are one statement rather than two that can disagree. It is written only for an
    action the case does **not** already hold an :class:`ActionRecord` for — duplicate suppression
    is resolved first, so a completed logical action never acquires a stale preparation.

    The pair with :attr:`ACTION_EXECUTED` is what carries the whole saga: this says *which action*,
    that says *what came of it*, and an action is unsettled exactly while the first exists without
    the second. Nothing stores that difference; it is computed by
    :func:`careloop.graph.context.unsettled_preparation`, so there is no phase field to go stale.
    """

    ACTION_EXECUTED = "ACTION_EXECUTED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class ApprovalStatus(StrEnum):
    """Lifecycle of a human decision request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TimelineEvent(BaseModel):
    """One append-only audit entry.

    ``diagnostic`` may hold anything useful to a human reading the audit trail, including raw
    argument values and rule internals. It is never returned to a model — see
    ``careloop.policy`` for the model-visible reason-code vocabulary.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    at: datetime = Field(default_factory=_utcnow)
    kind: TimelineKind
    actor: str
    """Who caused this entry: ``system``, ``triage``, ``planner``, ``policy``, ``human:<id>``."""

    summary: str
    diagnostic: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Approval(BaseModel):
    """A request for a caregiver decision about one exact proposed call, and its outcome.

    **One exact call.** An approval that names no tool and covers no arguments authorises nothing,
    because there is nothing for a caregiver to have decided about — see :attr:`is_actionable`.
    """

    model_config = ConfigDict(frozen=True)

    approval_id: str
    tool_name: str
    args_digest: str
    """Digest of the proposed arguments. The raw args live in the timeline, not here."""

    reason_code: str
    """Model-visible reason code that triggered the request."""

    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None

    @property
    def is_actionable(self) -> bool:
        """Whether this record identifies a call a decision could release.

        Both halves are required and neither is redundant, because both are the identity:
        :meth:`Case.is_approved` matches on ``(tool_name, args_digest)`` together, so a record
        missing either half names no call and can release none. The tool name is also what the
        audit trail and the caregiver's card are written against.

        **Derived rather than stored, and checked rather than validated.** A stored ``actionable``
        flag could disagree with the record it describes; emptiness cannot. And a
        ``model_validator`` refusing an empty ``tool_name`` would make
        :meth:`Case.model_validate_json` raise on cases persisted before manual handoffs stopped
        being written as approvals — turning a legacy row that is merely inert into a case that
        cannot be read at all. So the refusal lives where a decision is made,
        :func:`careloop.graph.runner._decide`, and reading stays total.

        Nothing in CareLoop constructs a non-actionable approval any more. This exists for what is
        already on disk, and as the predicate a second writer would have to defeat on purpose.
        """
        return bool(self.tool_name and self.args_digest)


class ActionRecord(BaseModel):
    """A completed external effect, keyed by idempotency key."""

    model_config = ConfigDict(frozen=True)

    idempotency_key: str
    action_type: str
    args_digest: str
    result: str
    recorded_at: datetime = Field(default_factory=_utcnow)


class Case(BaseModel):
    """The durable source of truth for one unit of caregiver admin work.

    Immutable. Collections are tuples rather than lists so that the freeze is real rather than
    shallow — ``case.facts.append(...)`` fails, it does not silently mutate shared state.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    state: CaseState = CaseState.NEW
    version: int = 0
    """Optimistic-concurrency token. Incremented on every persisted change."""

    event_id: str
    """Identifier of the inbound event that opened the case."""

    caregiver_id: str | None = None
    """Which caregiver this case belongs to. Frozen when the case is opened, never rewritten.

    **Optional, and that is a compatibility decision rather than a soft boundary.** A case
    persisted before ownership existed cannot truthfully claim an owner, and a required field would
    make such a case *unreadable* rather than merely unowned -- the distinction
    :attr:`Approval.is_actionable` already draws, and for the same reason. So reading stays total
    and the refusal lives where authority is decided: a production app refuses to read or move a
    case whose owner is ``None``, because a case nobody owns is a case nobody may act on.

    A reopen does **not** rewrite it, exactly as ``event_id`` is not rewritten: it records who the
    case was opened for, and a follow-up event is not a change of hands. Transferring a case
    between caregivers would be a deliberate operation with its own audit entry, and there is not
    one.

    Not on :class:`ActionRecord` and never to be. An action record is what happened externally,
    keyed by the frozen idempotency key; whose case it was is a property of the case, and putting
    it on the record would make the same fact answerable in two places.
    """

    summary: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    facts: tuple[Fact, ...] = ()
    approvals: tuple[Approval, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    timeline: tuple[TimelineEvent, ...] = ()

    failure_reason: str | None = None

    @property
    def is_at_rest(self) -> bool:
        """Whether the graph will move this case no further.

        ``DONE`` or ``FAILED``. Called ``is_terminal`` until commit 7, and renamed rather than
        redefined when ``DONE`` gained an outgoing transition: a finished case is at rest but no
        longer terminal, and every existing caller meant "at rest". ``FAILED`` is terminal in the
        strict sense and :data:`careloop.state.machine.TERMINAL_STATES` is where that lives.

        Only an inbound event can move a case out of ``DONE``, through
        :func:`careloop.graph.runner.reopen_case`. Nothing inside the graph can.
        """
        return self.state in (CaseState.DONE, CaseState.FAILED)

    def next_seq(self) -> int:
        """Sequence number for the next timeline entry."""
        return len(self.timeline)

    def fact(self, key: str) -> Fact | None:
        """Return the most recently recorded fact for ``key``, if any."""
        for candidate in reversed(self.facts):
            if candidate.key == key:
                return candidate
        return None

    def pending_approval(self) -> Approval | None:
        """Return the outstanding decision request, if one is awaiting a human."""
        for approval in reversed(self.approvals):
            if approval.status is ApprovalStatus.PENDING:
                return approval
        return None

    def action_for(self, idempotency_key: str) -> ActionRecord | None:
        """Return the recorded action for ``idempotency_key``, if the effect already happened."""
        for action in self.actions:
            if action.idempotency_key == idempotency_key:
                return action
        return None

    def approval_for(self, tool_name: str, args_digest: str) -> Approval | None:
        """Return the most recent decision record covering exactly this call, if any.

        **The canonical capability lookup.** ``(tool_name, args_digest)`` is the identity of the
        thing a caregiver was asked about, and both halves must match: a record naming another tool
        is a decision about another action, whatever its arguments hash to.

        Both parameters are required and positional, so no caller can perform a digest-only lookup
        by omitting one — the same reason
        :func:`careloop.policy.engine.resolve_trust` takes its fact key that way.

        The most recent match wins whatever its status, which is what lets a later refusal revoke an
        earlier approval and a later approval supersede an earlier refusal. Scoped to one call
        rather than to one digest: a record for a different tool no longer shadows this one either,
        which is the same defect running the other way.
        """
        for approval in reversed(self.approvals):
            if approval.tool_name == tool_name and approval.args_digest == args_digest:
                return approval
        return None

    def is_approved(self, tool_name: str, args_digest: str) -> bool:
        """Whether a caregiver granted authority for exactly this tool with exactly these arguments.

        **Keyed on the pair, not on the digest alone.** ``args_digest`` covers argument names and
        canonical values and nothing else — not the tool, not the case — so matching on it by itself
        asked *"did a human approve an argument map that looks like this?"* rather than *"did a
        human approve this call?"* An approval granted for one tool then answered ``True`` for any
        other tool invoked with an argument map that canonicalised the same way, and
        :attr:`Approval.tool_name` was stored, audited and rendered without ever being matched.

        Not reachable under the shipped rules — the only tools that can raise a decision request are
        the only tools that can spend one, and their argument names are disjoint — so this was a
        latent authority defect rather than a live exploit. What made it worth closing is that the
        safety rested entirely on registry argument shapes happening not to overlap, which is stated
        in no rule and would have been broken silently by the next tool.
        ``tests/test_approval_capability.py`` reproduces the laundering through the real engine over
        a synthetic registry whose two tools share one complete schema.

        This is the durable answer to "did a human say yes", and the policy engine reads it through
        the ``args_approved`` rule clause. It lives on ``Case`` rather than in the graph because the
        engine may not import the graph, and because the approval is a business fact — invariant 8
        puts business facts here.
        """
        approval = self.approval_for(tool_name, args_digest)
        return approval is not None and approval.status is ApprovalStatus.APPROVED
