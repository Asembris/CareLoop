"""What one graph run is bound to.

A :class:`RunContext` holds the collaborators every node needs and nothing a node could decide
with. It is the only mutable thing in the graph, and even it is mutable only in the sense that the
store behind it moves on.

**There is no case on this object.** Nodes read the case from :attr:`RunContext.store` at the top
of every execution and write it back before they finish, so the store is the single source of
truth exactly as invariant 8 says. That is not bookkeeping pedantry: the
:class:`~careloop.policy.gate.PolicyGate` is built over :meth:`RunContext.current_case`, so the
verdict a tool call gets is computed against what is *persisted*, not against a copy some node is
holding. A node that transitioned a case without saving would be a node whose policy checks ran
against the previous state.

**Denials are read back from the timeline, not carried in a variable.** The planner's context of
"what has already been refused" comes from the case's own ``POLICY_DENY`` entries, so a case
resumed in a new process plans with the same knowledge as one that never stopped. Only the
:class:`~careloop.policy.verdicts.ReasonCode` travels; the rule id and the arguments stay where
they were written.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from careloop.agents.planner import PlannerAgent
from careloop.agents.triage import TriageAgent
from careloop.policy.gate import PolicyGate
from careloop.policy.gated import GatedToolSet
from careloop.policy.hook import PolicyHook, policy_registry
from careloop.policy.verdicts import ReasonCode
from careloop.state.case import Approval, ApprovalStatus, Case, TimelineKind
from careloop.state.store import CaseStore
from careloop.tools.callables import ToolSet
from careloop.tools.documents import DocumentStore
from careloop.tools.executor import MockExternalExecutor
from careloop.tools.idempotency import args_digest


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Proposal(BaseModel):
    """One action the planner proposed, read back from the timeline.

    Not a field on :class:`~careloop.state.case.Case`. The last ``PLAN_PROPOSED`` entry already
    records it durably and in order, and the timeline is append-only — a proposal held in a
    separate mutable field could disagree with the audit trail about what was proposed.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    args: dict[str, str]
    rationale: str = ""

    @property
    def digest(self) -> str:
        """Digest of the proposed arguments, as an approval records it."""
        return args_digest(self.args)


def latest_proposal(case: Case) -> Proposal | None:
    """The most recent proposal on ``case``, or ``None`` if nothing has been proposed."""
    for event in reversed(case.timeline):
        if event.kind is TimelineKind.PLAN_PROPOSED:
            return Proposal(
                tool_name=event.payload["tool_name"],
                args=dict(event.payload["args"]),
                rationale=event.payload.get("rationale", ""),
            )
    return None


def prior_denials(case: Case) -> tuple[tuple[str, ReasonCode], ...]:
    """Every refusal on ``case``, as the planner is allowed to know it.

    ``(tool_name, ReasonCode)`` and nothing else. The matched rule, the argument that failed and
    the trust it resolved to are in the same timeline entries and stay there — see invariant 6.
    Duplicates are collapsed, in first-seen order, so a planner that repeats one proposal four
    times is not handed four identical lines to pattern-match on.
    """
    seen: list[tuple[str, ReasonCode]] = []
    for event in case.timeline:
        if event.kind is not TimelineKind.POLICY_DENY:
            continue
        pair = (event.payload["tool_name"], ReasonCode(event.payload["reason_code"]))
        if pair not in seen:
            seen.append(pair)
    return tuple(seen)


def approval_id_for(digest: str) -> str:
    """A deterministic approval identifier, derived from the arguments it covers.

    No clock and no randomness, so a case replayed from its own history produces the same
    identifiers — which is what lets a test assert on one.
    """
    return f"apr-{digest[:16]}"


def approval_for(case: Case, digest: str) -> Approval | None:
    """The most recent approval covering ``digest``, whatever its status."""
    for approval in reversed(case.approvals):
        if approval.args_digest == digest:
            return approval
    return None


def is_approved(case: Case, digest: str) -> bool:
    """Whether a caregiver granted an approval covering exactly these arguments."""
    approval = approval_for(case, digest)
    return approval is not None and approval.status is ApprovalStatus.APPROVED


@dataclass
class RunContext:
    """Everything one graph run is bound to.

    Built once per run. ``case_id`` is fixed here rather than flowing through node inputs, for the
    same reason it is a ``ToolSet`` constructor argument: nothing that a model can influence should
    be able to say which case an effect belongs to.
    """

    case_id: str
    store: CaseStore
    event_text: str = ""
    document_ids: tuple[str, ...] = ()

    triage: TriageAgent = field(default_factory=TriageAgent)
    planner: PlannerAgent = field(default_factory=PlannerAgent)
    executor: MockExternalExecutor = field(default_factory=MockExternalExecutor)

    documents: DocumentStore | None = None
    """The document store the toolset reads and the cross-document checks consult.

    ``None`` means the shipped fixtures. Injectable so a test can assert against a store it built
    rather than against whatever the process happens to ship — the ``required-document-is-held``
    check in :mod:`careloop.graph.extract` reads it, and a check whose input cannot be varied is a
    check that can only be tested one way.
    """

    now: Callable[[], datetime] = _utcnow
    """Injectable clock. ``careloop/tools/`` may not read one; the graph stamps the timeline."""

    register_hook: bool = True
    """Whether the policy hook is registered.

    Present so the layered-enforcement claim is testable rather than asserted: with this ``False``
    the ``BeforeToolCall`` hook is absent and the gated toolset must still refuse the same calls.
    """

    def __post_init__(self) -> None:
        self.gate = PolicyGate(self.current_case)
        self.hook = PolicyHook(self.gate)
        self.hooks = policy_registry(self.hook) if self.register_hook else _empty_registry()
        self.tools = GatedToolSet(
            ToolSet(self.case_id, executor=self.executor, documents=self.documents), self.gate
        )

    def current_case(self) -> Case:
        """The case as the store holds it now."""
        return self.store.get(self.case_id)

    def commit(self, case: Case, *, expected_version: int) -> Case:
        """Persist a changed case, guarded by the version the caller read."""
        return self.store.save(case, expected_version=expected_version)

    def denials(self, case: Case) -> Sequence[tuple[str, ReasonCode]]:
        """What the planner is told about earlier refusals."""
        return prior_denials(case)


def _empty_registry():
    """A registry with no callbacks, for runs that deliberately omit the hook."""
    from strands.hooks.registry import HookRegistry

    return HookRegistry()


__all__ = [
    "Proposal",
    "RunContext",
    "approval_for",
    "approval_id_for",
    "is_approved",
    "latest_proposal",
    "prior_denials",
]
