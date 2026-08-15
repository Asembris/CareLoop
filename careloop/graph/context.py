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
from careloop.graph.verify import ReadBack, ReadBackFailed
from careloop.policy.gate import PolicyGate
from careloop.policy.gated import GatedToolSet
from careloop.policy.hook import PolicyHook, policy_registry
from careloop.policy.verdicts import ReasonCode
from careloop.state.case import Approval, ApprovalStatus, Case, CaseState, TimelineKind
from careloop.state.store import CaseStore
from careloop.tools.callables import ToolSet
from careloop.tools.clinic import ClinicClient, ClinicRefused, ClinicUnavailable, clinic_executor
from careloop.tools.documents import DocumentStore
from careloop.tools.executor import MockExternalExecutor
from careloop.tools.idempotency import args_digest
from careloop.tools.results import ActionResult


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


def proposal_for(case: Case, digest: str) -> Proposal | None:
    """The most recent proposal whose arguments hash to ``digest``, or ``None``.

    The counterpart of :func:`approval_for`, which finds an approval by the digest of the arguments
    it covers. An approval records only the digest — deliberately, so that a decision made about
    one action cannot authorise another — so anything wanting to *show* a caregiver what they are
    approving has to come back through the timeline to the proposal that digest came from.

    That lookup lives here rather than in the caller for the same reason :func:`latest_proposal`
    does: the timeline is read in one layer, by named functions, and never rummaged in by whoever
    happens to need something out of it.
    """
    for event in reversed(case.timeline):
        if event.kind is not TimelineKind.PLAN_PROPOSED:
            continue
        proposal = Proposal(
            tool_name=event.payload["tool_name"],
            args=dict(event.payload["args"]),
            rationale=event.payload.get("rationale", ""),
        )
        if proposal.digest == digest:
            return proposal
    return None


class Handoff(BaseModel):
    """A case CareLoop has stopped on, with no safe action left to take.

    The counterpart of an :class:`~careloop.state.case.Approval` and deliberately not one. An
    approval is a capability — it releases exactly the call its digest covers. This is a status:
    every tool was proposed and refused, so there is no call, nothing to authorise, and nothing any
    decision could mean. It carries a reason code and a time, which is all a caregiver's card needs
    and all the audit trail has to say.
    """

    model_config = ConfigDict(frozen=True)

    reason_code: str
    at: datetime


def manual_handoff(case: Case) -> Handoff | None:
    """The outstanding manual handoff on ``case``, or ``None``.

    Read from the ``MANUAL_HANDOFF_REQUIRED`` entry rather than from a field, for the reason
    :func:`latest_proposal` and :func:`latest_delivery` are: the timeline already records it,
    durably, in order and append-only, and a second copy on ``Case`` would be a second answer to a
    question the trail already answers.

    Outstanding means ``AWAITING_HUMAN`` with nothing pending. Both clauses are load-bearing rather
    than belt-and-braces. A case that parked, was reopened by a later event and went on to park for
    a genuine approval still carries the old entry, and reporting a handoff *beside* a live decision
    request would put two cards on one case. And the state check is what makes "outstanding" mean
    now rather than ever.
    """
    if case.state is not CaseState.AWAITING_HUMAN or case.pending_approval() is not None:
        return None
    for event in reversed(case.timeline):
        if event.kind is TimelineKind.MANUAL_HANDOFF_REQUIRED:
            return Handoff(reason_code=event.payload["reason_code"], at=event.at)
    return None


class Delivery(BaseModel):
    """The inbound event a case was most recently opened or reopened by."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    document_ids: tuple[str, ...]


def latest_delivery(case: Case) -> Delivery | None:
    """The event that last delivered work to ``case``, or ``None`` if nothing did.

    **Not ``case.event_id``.** That field records which event *opened* the case and is deliberately
    not rewritten by :func:`careloop.graph.runner.reopen_case` — a reopen is not an opening. So a
    case reopened by a claim rejection still names the referral there, and anything rebuilding a
    :class:`RunContext` to carry that case forward needs the later event's documents, not the
    original's.

    Read from the ``CASE_CREATED`` and ``CASE_REOPENED`` entries, which already record both the
    event id and its documents, in order and append-only. Storing it a second time on ``Case``
    would be a second answer to a question the timeline already answers.
    """
    for event in reversed(case.timeline):
        if event.kind in (TimelineKind.CASE_CREATED, TimelineKind.CASE_REOPENED):
            return Delivery(
                event_id=event.payload["event_id"],
                document_ids=tuple(event.payload.get("document_ids", ())),
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


def approval_id_for(seq: int) -> str:
    """A durable identity for one *request* for human authority.

    **Two identities are in play here and they are not the same thing.** ``args_digest`` says which
    exact argument set is involved. This says which *occurrence of asking a caregiver* a decision is
    about. Reproposing identical arguments after a refusal is one action asked twice, which is two
    requests — and it has to be two identifiers, or a decision about the second names the first as
    well.

    It was ``f"apr-{digest[:16]}"`` until this commit, which made the identifier a pure function of
    the action. Session K: a rejected proposal, reproposed unchanged and then approved, produced two
    records under one id, and :func:`careloop.graph.runner._decide` rewrote both — turning one
    caregiver's recorded refusal into a different caregiver's approval, timestamp and name included.
    Nothing about authority moved, because ``is_approved`` reads the most recent record matching the
    digest; what moved was the audit trail, in the one direction an append-only trail must not.

    ``seq`` is the position the ``APPROVAL_REQUESTED`` entry recording this request will occupy on
    the case timeline. The timeline is append-only, so two requests on one case cannot share it:
    **uniqueness here is structural rather than a collision argument**, which is why nothing is
    hashed and nothing is truncated. It is also why there is no clock, no random source and no
    counter — a case replayed from its own history produces the same identifiers, which is what lets
    a test assert on one and what keeps ``evals/`` and ``bench/`` reproducible.

    The case id is deliberately absent. An approval identifier is only ever resolved inside one
    case — the route is ``/api/cases/{case_id}/decisions/{approval_id}/…`` and
    :func:`careloop.graph.runner._decide` scans one case's approvals — so scoping it any wider would
    lengthen every identifier to buy a property nothing reads.

    The digest is deliberately absent too, and that is a change in kind rather than in length. An
    identifier carrying a fragment of the digest reads as though it names the action, which is
    exactly the conflation this repair exists to end. What the action is stays where it belongs: on
    :class:`~careloop.state.case.Approval`, in full, as ``args_digest``.
    """
    return f"apr-{seq}"


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

    executor: MockExternalExecutor | None = None
    """The executor every effect goes through. ``None`` builds one with the simulated handlers."""

    clinic: ClinicClient | None = None
    """A real external clinic, or ``None`` for a fully simulated run.

    **Setting this turns on both halves of the real path at once, and that is the point.** It
    builds the executor whose ``book_appointment`` is a genuine HTTP call *and* the read-back
    verification uses to observe the result independently. A real booking checked against a
    simulated verification would be exactly the tautology this collaborator exists to remove, so
    the combination is made unconstructable rather than discouraged: supplying ``clinic`` and
    ``executor`` together raises.
    """

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
        if self.clinic is not None and self.executor is not None:
            raise ConflictingExecutors(
                "a run is bound to a clinic or to an executor, never to both: the clinic builds "
                "the executor whose bookings are real and the read-back that observes them, and "
                "an executor supplied beside it could make one of those two simulated"
            )
        if self.executor is None:
            self.executor = (
                clinic_executor(self.clinic) if self.clinic is not None else MockExternalExecutor()
            )
        self.readbacks: dict[str, ReadBack] = (
            {} if self.clinic is None else {"book_appointment": _clinic_readback(self.clinic)}
        )

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


class ConflictingExecutors(Exception):
    """Raised when a run is handed both a clinic and an executor."""


def _clinic_readback(client: ClinicClient) -> ReadBack:
    """Observe a booking by asking the clinic what it holds now.

    **A separate GET, keyed on the confirmation the effect returned.** It is not the booking's own
    response replayed, and it is not read from anything CareLoop stored — those would both make the
    check a re-read of CareLoop's receipt. The refusal and the outage are both turned into
    :class:`~careloop.graph.verify.ReadBackFailed`, so a verifier sees one failure vocabulary and
    the graph layer does not have to know what an HTTP client's exceptions are called.
    """

    def observe(result: ActionResult) -> ActionResult:
        try:
            return client.appointment(result.confirmation_id)
        except (ClinicRefused, ClinicUnavailable) as failure:
            raise ReadBackFailed(
                f"the clinic could not confirm {result.confirmation_id}: {failure}"
            ) from failure

    return observe


def _empty_registry():
    """A registry with no callbacks, for runs that deliberately omit the hook."""
    from strands.hooks.registry import HookRegistry

    return HookRegistry()


__all__ = [
    "ConflictingExecutors",
    "Delivery",
    "Handoff",
    "Proposal",
    "RunContext",
    "approval_for",
    "approval_id_for",
    "is_approved",
    "latest_delivery",
    "latest_proposal",
    "manual_handoff",
    "prior_denials",
    "proposal_for",
]
