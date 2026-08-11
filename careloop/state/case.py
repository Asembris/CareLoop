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
    STATE_TRANSITION = "STATE_TRANSITION"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    FACT_PROMOTED = "FACT_PROMOTED"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    POLICY_ALLOW = "POLICY_ALLOW"
    POLICY_DENY = "POLICY_DENY"
    POLICY_REQUIRE_HUMAN = "POLICY_REQUIRE_HUMAN"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
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
    """A request for a caregiver decision, and its outcome."""

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

    summary: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    facts: tuple[Fact, ...] = ()
    approvals: tuple[Approval, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    timeline: tuple[TimelineEvent, ...] = ()

    failure_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether the case has reached a state it can never leave."""
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
