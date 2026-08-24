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
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.agents.planner import PlannerAgent
from careloop.agents.triage import TriageAgent
from careloop.graph.authority import AUTHORISES_NOBODY, PatientAuthority
from careloop.graph.providers import (
    ExternalProvider,
    clinic_provider,
    merged_handlers,
    merged_readbacks,
)
from careloop.graph.verify import (
    INDEPENDENTLY_OBSERVABLE,
    ReadBack,
)
from careloop.graph.witness import NO_WITNESS, Witness
from careloop.policy.gate import PolicyGate
from careloop.policy.gated import GatedToolSet
from careloop.policy.hook import PolicyHook, policy_registry
from careloop.policy.verdicts import ReasonCode
from careloop.provenance.trust import TrustLevel, at_least
from careloop.state.case import ActionRecord, Approval, Case, CaseState, TimelineKind
from careloop.state.store import CaseStore
from careloop.tools.callables import ToolSet
from careloop.tools.clinic import ClinicClient
from careloop.tools.documents import DocumentStore
from careloop.tools.executor import MockExternalExecutor
from careloop.tools.idempotency import args_digest, idempotency_key


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


def proposal_for(case: Case, tool_name: str, digest: str) -> Proposal | None:
    """The most recent proposal of exactly this call, or ``None``.

    The presentation counterpart of :func:`approval_for`, and keyed the same way. An approval
    records ``(tool_name, args_digest)`` because that pair is the identity of the thing a caregiver
    was asked about; anything wanting to *show* them what they are approving has to come back
    through the timeline to the proposal that pair came from, and it has to match both halves.

    **It matched only the digest until this commit.** ``args_digest`` covers argument names and
    canonical values and nothing else — not the tool, not the case — so the card was asking *"which
    proposal has arguments that hash like this?"* while the approval meant *"this exact call"*, and
    a proposal belonging to a different tool could supply the arguments rendered under this
    approval's name. Session L bound the authority lookup to the pair and deliberately left this
    one alone so that property had a single attributable cause; this is the other half.

    **Presentation, not authority, and the distinction is worth keeping sharp.** Nothing routed
    through here authorises anything: :meth:`careloop.state.case.Case.is_approved` is what the
    policy engine reads and :meth:`~careloop.state.case.Case.approval_for` is what the execute node
    consults, and both were already tool-bound. What this decides is what a person is shown before
    they grant authority, which is not the same thing and is not nothing either.

    It was also unreachable under the shipped configuration, by accident rather than by design —
    the only two tools that can reach ``REQUIRE_HUMAN`` declare disjoint argument names, the
    approval is raised from the newest proposal in the same gate execution that read it, and
    nothing plans while a case is ``AWAITING_HUMAN``. All three are facts about registry shape and
    node write order, stated in no rule. That is what makes this worth closing rather than noting.

    The latest exact match wins, unchanged: the gate reads :func:`latest_proposal` and raises the
    request from it, so the newest proposal of one call *is* the record the caregiver was asked
    about. Binding the tool narrows the candidates and leaves the tie-break alone.

    Both parameters are required and positional, so no caller can fall back to a digest-only lookup
    by omitting one — the same reason :meth:`careloop.state.case.Case.approval_for` takes them that
    way. There is deliberately no digest-only variant anywhere; one would be the hole this closes,
    reopened under a different name.

    That the lookup lives here rather than in the caller is for the reason :func:`latest_proposal`
    is: the timeline is read in one layer, by named functions, and never rummaged in by whoever
    happens to need something out of it.
    """
    for event in reversed(case.timeline):
        if event.kind is not TimelineKind.PLAN_PROPOSED:
            continue
        if event.payload["tool_name"] != tool_name:
            continue
        proposal = Proposal(
            tool_name=event.payload["tool_name"],
            args=dict(event.payload["args"]),
            rationale=event.payload.get("rationale", ""),
        )
        if proposal.digest == digest:
            return proposal
    return None


class Preparation(BaseModel):
    """One durably frozen logical action identity, read back from the timeline.

    The subject of the whole recovery path. It is what ``ACTION_PREPARED`` records: the tool, the
    idempotency key the authorised arguments derive, and the digest of those arguments — written
    before anything could reach the outside world, so a later process can reuse the identity of an
    attempt it never saw rather than invent one.

    **It asserts nothing about the provider.** A preparation is compatible with every one of "the
    request was never made", "it was made and never arrived", "it arrived and was not committed",
    "it was committed and the response was lost", and "the response arrived and was not recorded".
    Deciding which requires reading the external system; that is
    :class:`careloop.graph.nodes.ReconcileNode`'s job and not this model's.

    A read model, never persisted — the same arrangement as :class:`Proposal`, :class:`Handoff`,
    :class:`Delivery` and :class:`RecheckTarget`, and for the same reason: the timeline already
    records it, append-only and in order, and a second copy on ``Case`` would be a second answer to
    a question the trail already answers.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    idempotency_key: str
    args_digest: str


def unsettled_preparation(case: Case) -> Preparation | None:
    """The newest prepared action on ``case`` whose outcome is not durably known, or ``None``.

    **Unsettled is computed, never stored**, and that is the point of the design. A prepared action
    is settled exactly when the case holds an :class:`~careloop.state.case.ActionRecord` under its
    key, so the unsettled set is ``{prepared keys} - {recorded keys}`` and there is no phase field
    anywhere that could go stale, be edited, or disagree with the trail it summarises.

    **The newest preparation is inspected and the scan then stops**, rather than searching backwards
    for the newest *unsettled* one. Earlier preparations are settled by construction —
    ``ExecuteNode`` prepares only from ``READY_TO_EXECUTE``, and an unsettled preparation leaves the
    case in a state with no path back there that raises no approval — so the two readings coincide
    today. They are not equally safe if that ever changes: stopping answers ``None``, which
    :class:`careloop.graph.nodes.ReconcileNode` treats as an inconsistency and fails closed on,
    while searching would hand a recovery an *older* action and re-dispatch an episode that had
    already been left behind. Fail closed, and never resurrect.

    ``None`` is the correct and expected answer for every case persisted before ``ACTION_PREPARED``
    existed: no attempt was ever recorded, which is true rather than a gap to be papered over. It
    is also the answer for an ordinary finished case, whose preparations are all settled.
    """
    for event in reversed(case.timeline):
        if event.kind is not TimelineKind.ACTION_PREPARED:
            continue
        key = event.payload["idempotency_key"]
        if case.action_for(key) is not None:
            return None
        return Preparation(
            tool_name=event.payload["tool_name"],
            idempotency_key=key,
            args_digest=event.payload["args_digest"],
        )
    return None


class Handoff(BaseModel):
    """A case CareLoop has stopped on and handed to a person.

    The counterpart of an :class:`~careloop.state.case.Approval` and deliberately not one. An
    approval is a capability — it releases exactly the call its digest covers. This is a status:
    CareLoop stopped, and no decision a caregiver posts against it authorises anything.

    **Two different stoppings share this shape**, told apart by :attr:`reason_code`.
    ``NO_SAFE_ACTION_AVAILABLE`` means every tool was proposed and refused and *nothing was carried
    out*; ``EXTERNAL_STATE_NOT_CONFIRMED`` means something *was* carried out and could not be
    certified. The two identity fields below exist for the second and are absent for the first,
    which is why both are optional.
    """

    model_config = ConfigDict(frozen=True)

    reason_code: str
    at: datetime

    tool_name: str | None
    """Which tool the handoff is about, or ``None`` when it is about no single call.

    Optional because :func:`careloop.graph.nodes._park_with_nothing_safe_to_do` writes a payload of
    ``{"reason_code": ...}`` and nothing else — *every* tool was refused, so naming one would be
    false. Modelled as ``str | None`` rather than defaulted to ``""`` so that "this handoff is about
    no particular call" and "somebody forgot to record which" stay distinguishable, and so that a
    handoff persisted before this field existed stays readable rather than acquiring an identity it
    never had.
    """

    idempotency_key: str | None
    """The exact action this handoff could not certify, or ``None`` when there is none.

    ``None`` is correct and expected for two entirely different populations, and neither is a
    defect: a ``NO_SAFE_ACTION_AVAILABLE`` handoff never performed an action, and a handoff written
    before the key was recorded never observed one. Both are refused a re-check by
    :func:`recheck_target`, with no fallback — see its docstring for why deriving one would be
    worse than refusing.
    """


class RecheckTarget(BaseModel):
    """The exact recorded effect a caregiver's re-check would observe again.

    **A read model, never persisted.** It is derived from the timeline and the case's own action
    ledger every time it is asked for, so it cannot drift from them the way a stored field could;
    it appears in no store, in no API response and in no projection sweep. What crosses the API
    boundary is one boolean — see :attr:`careloop.api.models.DecisionView.recheckable` — and never
    this object or the :class:`~careloop.state.case.ActionRecord` inside it.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    idempotency_key: str
    action: ActionRecord


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
            return Handoff(
                reason_code=event.payload["reason_code"],
                at=event.at,
                # ``.get`` rather than subscription, because these two keys are legitimately absent
                # from two different populations of entry — see the fields' own docstrings. The
                # reason code is subscripted because every writer has always emitted it and a
                # handoff without one is a malformed record rather than an older shape.
                tool_name=event.payload.get("tool_name"),
                idempotency_key=event.payload.get("idempotency_key"),
            )
    return None


def entered_verifying_from(case: Case) -> CaseState | None:
    """The state ``case``'s most recent entry into ``VERIFYING`` came from, or ``None``.

    **The re-check episode discriminator**, and the reason it is shaped this way is worth stating.
    :class:`careloop.graph.nodes.VerifyNode` has to know whether the verification it is performing
    is a first pass or a caregiver's re-check, because a re-check that reads nothing must not
    certify while an ordinary simulated run legitimately does. That is a fact about *this episode*,
    not about the case as a whole — a case can be re-checked, park, and be re-checked again.

    Read from the ``STATE_TRANSITION`` entry :func:`careloop.state.machine.transition` wrote, for
    the reasons every other reader in this module reads the timeline: it is durable, so it survives
    the process boundary a resume crosses; it is written by the only writer of ``Case.state``, so
    nothing else can forge it; and it is not a run-scoped flag, which
    :mod:`careloop.graph.conditions` and this module's own docstring both refuse. It is emphatically
    not parsed out of any diagnostic.

    ``AWAITING_HUMAN`` is the answer if and only if
    :func:`careloop.graph.runner.request_recheck` made the move, since that is the only writer of
    that transition and a structural sweep in ``tests/test_recheck_writers.py`` keeps it so.
    ``EXECUTING`` and ``WAITING_EXTERNAL`` are the ordinary post-effect entries.
    """
    for event in reversed(case.timeline):
        if event.kind is not TimelineKind.STATE_TRANSITION:
            continue
        if event.payload["to"] == str(CaseState.VERIFYING):
            return CaseState(event.payload["from"])
    return None


def recheck_target(case: Case) -> RecheckTarget | None:
    """The exact effect a caregiver may ask CareLoop to observe again, or ``None``.

    **The single source of truth for static re-check eligibility.** Both
    :func:`careloop.graph.runner.request_recheck` and
    :func:`careloop.api.projections.decision_view` read this and nothing else, so the button a
    caregiver is shown and the endpoint that validates their click cannot answer *"is this handoff
    bound to a valid re-check target?"* differently. Reimplementing either half would be two
    answers to one question, which is the shape of defect this repository has closed three times.

    **Durable and static only.** It reads the stored case and nothing else, so it deliberately
    cannot and does not answer two things that matter just as much and belong elsewhere: whether a
    run is in flight right now — execution context, checked by the API — and whether the run that
    follows will actually have an observer bound — a fact about a :class:`RunContext` that only the
    verify node can establish. See :attr:`careloop.api.models.DecisionView.recheckable` for exactly
    what the resulting boolean does and does not claim.

    Every clause below is necessary and each refuses a different thing:

    ``AWAITING_HUMAN`` with nothing pending, and a handoff — the case really is stopped and waiting
    for a person, rather than mid-flight or waiting on a decision. The first two are re-stated here
    rather than left to :func:`manual_handoff`'s own guards, so that a later change to what
    "outstanding" means cannot silently widen what may be re-checked.

    ``EXTERNAL_STATE_NOT_CONFIRMED`` — a ``NO_SAFE_ACTION_AVAILABLE`` handoff means every tool was
    refused and *nothing happened*, so there is no external result for an observation to be about.

    A recorded ``idempotency_key`` naming a real :class:`~careloop.state.case.ActionRecord` — the
    identity chain the whole operation rests on. **No fallback of any kind**: not the latest action,
    not the latest booking, not the tool name, not the argument digest. A handoff written before the
    key was recorded cannot truthfully support an operation whose defining property is exact action
    identity, and inventing one would make the audit trail assert something nobody observed.

    An action type in :data:`careloop.graph.verify.INDEPENDENTLY_OBSERVABLE` — re-reading a
    simulated effect would compare CareLoop's record against CareLoop's record and certify on the
    strength of it, which is the no-observer hazard with its paperwork in order.

    A current proposal whose derived key equals the handoff's — **the consistency clause, and the
    subtle one.** :class:`careloop.graph.nodes.VerifyNode` resolves its own target by re-deriving
    ``idempotency_key(case_id, latest_proposal.tool_name, latest_proposal.args)``, and that
    derivation is deliberately *not* overridden by this operation: handing the node a target would
    make the re-check a second, competing way to say which action is being verified. Instead this
    proves the two agree before anything is scheduled, so "the action the handoff names" and "the
    action the verifier will read" are the same object by construction. If they disagree, the
    disagreement is the finding — refuse, and never silently prefer one.
    """
    if case.state is not CaseState.AWAITING_HUMAN or case.pending_approval() is not None:
        return None

    handoff = manual_handoff(case)
    if handoff is None or handoff.reason_code != str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED):
        return None
    if handoff.idempotency_key is None:
        return None

    record = case.action_for(handoff.idempotency_key)
    if record is None or record.action_type not in INDEPENDENTLY_OBSERVABLE:
        return None

    proposal = latest_proposal(case)
    if proposal is None:
        return None
    if idempotency_key(case.case_id, proposal.tool_name, proposal.args) != handoff.idempotency_key:
        return None

    return RecheckTarget(
        tool_name=record.action_type,
        idempotency_key=handoff.idempotency_key,
        action=record,
    )


class Delivery(BaseModel):
    """The inbound event a case was most recently opened or reopened by."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    document_ids: tuple[str, ...]


#: The trust level a patient identifier must reach before it can name who an effect affects.
#:
#: Read from the same place ``deny-booking-for-unverified-patient`` requires it, because these are
#: two halves of one question and a second spelling is a second thing to keep in step.
ESTABLISHED_PATIENT_TRUST: Final[TrustLevel] = TrustLevel.CONTROLLED


def established_patients(case: Case) -> frozenset[str]:
    """Every patient identifier this case has deterministically established.

    Scoped to ``CONTROLLED`` or higher on purpose. An untrusted ``patient_id`` scraped out of a
    document cannot reach an effect -- ``deny-booking-for-unverified-patient`` refuses it and the
    ``ALLOW`` clause requires the same level -- so treating one as a patient this case is *about*
    would let an attacker-authored page decide which caregiver may work on it. That is the
    denial-of-service shape the other direction, and it is worth stating because the stricter
    reading looks safer.

    Empty for a case whose documents have not been read yet, which is the ordinary state of a case
    at the moment it is opened.
    """
    return frozenset(
        fact.value
        for fact in case.facts
        if fact.key == "patient_id" and at_least(fact.trust_level, ESTABLISHED_PATIENT_TRUST)
    )


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


class Refusal(BaseModel):
    """One refusal, as the liveness rule identifies it.

    Four parts, and each is load-bearing. ``tool_name`` and ``args_digest`` are the existing
    identity of an exact call — the same pair an :class:`~careloop.state.case.Approval` is keyed on
    — so this reuses the project's action identity rather than inventing a second one.
    ``reason_code`` distinguishes two refusals of the same call for different reasons.
    ``fingerprint`` is what makes the identity *conditional*: it says which case the verdict was
    reached against, so a refusal stops matching the moment the inputs to that verdict change.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    args_digest: str
    reason_code: str
    fingerprint: str


def policy_fingerprint(case: Case) -> str:
    """Digest of everything a policy verdict can depend on.

    **Deliberately minimal, and minimal is the safe direction.** The engine reads exactly three
    things about a case: its state (``case_state_in`` / ``case_state_not_in``), its facts (through
    :func:`careloop.policy.engine.resolve_trust`, under each argument's declared ``trust_key``) and
    its approvals (``args_approved``). Nothing else can change a verdict, so nothing else belongs
    here.

    Including more would be actively worse rather than merely redundant. This fingerprint is what
    decides whether a repeated proposal counts as *no progress*; a component that churns without
    affecting a verdict — the timeline, the action ledger, a version counter — would make every
    fingerprint unique, no repeat would ever be recognised, and the liveness rule would quietly
    stop working while still looking present.

    Reuses :func:`careloop.tools.idempotency.args_digest` rather than hashing here, so there is one
    canonicalisation in the system and this cannot drift from the one approvals and idempotency
    keys already use.
    """
    facts = "|".join(sorted(f"{fact.key}={fact.value}@{fact.trust_level}" for fact in case.facts))
    approvals = "|".join(
        sorted(
            f"{approval.tool_name}:{approval.args_digest}:{approval.status}"
            for approval in case.approvals
        )
    )
    return args_digest({"state": str(case.state), "facts": facts, "approvals": approvals})


def prior_refusals(case: Case) -> tuple[Refusal, ...]:
    """Every refusal recorded on ``case``, in order.

    Read from the ``POLICY_DENY`` timeline entries rather than from a field, for the reason
    :func:`latest_proposal` gives: the timeline is the durable, ordered, append-only record of what
    happened, and a second derivation could disagree with the audit trail.

    Entries written before this rule existed carry no ``args_digest`` and no ``policy_fingerprint``
    and are skipped. That is the fail-safe direction: an unidentifiable past refusal cannot match a
    new one, so a legacy case gets a fresh replan budget rather than being parked on evidence
    nobody recorded.
    """
    found: list[Refusal] = []
    for event in case.timeline:
        if event.kind is not TimelineKind.POLICY_DENY:
            continue
        digest = event.payload.get("args_digest")
        fingerprint = event.payload.get("policy_fingerprint")
        if not digest or not fingerprint:
            continue
        found.append(
            Refusal(
                tool_name=event.payload["tool_name"],
                args_digest=digest,
                reason_code=event.payload.get("reason_code", ""),
                fingerprint=fingerprint,
            )
        )
    return tuple(found)


def refusals_at(case: Case, fingerprint: str) -> int:
    """How many refusals ``case`` has collected against these exact policy inputs.

    Counts only refusals at ``fingerprint``, so the count resets whenever the case materially
    changes — new evidence, a promotion, a caregiver's decision, a state transition. A case that
    keeps *making progress* therefore never runs this budget down.
    """
    return sum(1 for refusal in prior_refusals(case) if refusal.fingerprint == fingerprint)


def is_repeat_refusal(case: Case, refusal: Refusal) -> bool:
    """Whether ``refusal`` has already happened on ``case`` under identical policy inputs.

    This is the deterministic half of the liveness rule, and it is deterministic because
    :meth:`careloop.policy.engine.PolicyEngine.evaluate` is pure: the same tool, the same canonical
    arguments and the same case give an equal ``Decision`` every time. So a proposal that repeats
    one already refused at the same fingerprint is *provably* futile — not heuristically stuck, not
    probably looping. Replanning again cannot produce a different verdict, and the case belongs
    with a person.

    It is scoped to the fingerprint precisely so a legitimate reconsideration is not caught: the
    same call after new trusted evidence, a promotion, an approval or a re-check is a different
    question and is evaluated on its merits. Nothing is blacklisted, and no tool is ever refused
    permanently.
    """
    return refusal in prior_refusals(case)


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


def approval_for(case: Case, tool_name: str, digest: str) -> Approval | None:
    """The most recent decision record covering exactly this call, whatever its status.

    **Delegates rather than reimplements.** This and :func:`is_approved` each carried their own
    ``reversed(case.approvals)`` scan until the lookup became tool-bound, which meant one matching
    rule written down twice — and the graph's copy was the one the execute node consulted, so a
    drift between them would have been a drift in what authority means. There is now exactly one
    implementation, on :class:`~careloop.state.case.Case`, where invariant 8 puts business facts and
    where the policy engine can reach it without importing the graph.
    """
    return case.approval_for(tool_name, digest)


def is_approved(case: Case, tool_name: str, digest: str) -> bool:
    """Whether a caregiver granted authority for exactly this tool with exactly these arguments."""
    return case.is_approved(tool_name, digest)


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
    the combination is made unconstructable rather than discouraged: supplying a provider and
    ``executor`` together raises.

    Kept as its own field rather than folded into :attr:`providers` because it is what every
    existing composition root and test passes, and because the clinic is the one provider whose
    absence has a settled meaning throughout the suite. It is converted to an
    :class:`~careloop.graph.providers.ExternalProvider` on the way in, so nothing below this line
    treats it specially.
    """

    providers: tuple[ExternalProvider, ...] = ()
    """Every other real external system this run is bound to.

    The generalisation of :attr:`clinic`. Handlers and read-backs are merged across all of them, so
    "which action types are really external" and "which of those can be independently observed" are
    answers this object assembles rather than facts a lifecycle spells out. Two providers claiming
    one action type raises :class:`~careloop.graph.providers.ConflictingProviders`.
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

    patient_authority: PatientAuthority = AUTHORISES_NOBODY
    """Whether this run may cause an external effect for the people its case is about.

    **The default denies, and that is the whole of the safety argument.** A run nobody bound an
    authority to dispatches nothing at all -- not because a caller remembered a flag, but because
    the only implementation :mod:`careloop.graph.authority` ships is the refusing one. An allow-all
    default is a default that ends up in production; there is deliberately none to reach for, and
    ``tests/test_patient_authority.py`` sweeps the package to keep one from appearing.

    **Asked live at the point of dispatch, never snapshotted.** The composition root binds an object
    that closes over a caregiver id and a directory and answers from current state. A set of
    authorised patients computed when the run started would be a revocation that had not happened
    yet -- and, worse, would have been computed before the case had established a patient at all,
    which is precisely the first-run hole this exists to close.

    **The graph never learns what is behind it.** The protocol takes patient identifiers and answers
    yes or no; a principal, a caregiver, a directory and a token all stay outside, where
    ``tests/test_identity_boundary.py`` requires them to be.

    **Not consulted on the recovery path.** ``ReconcileNode`` settles an already-issued effect
    rather than issuing a new one, so it does not ask and structurally cannot -- an appointment that
    may exist does not stop existing because a grant was withdrawn.
    """

    witness: Witness = NO_WITNESS
    """Where a bounded statement of what already happened is recorded. A **sink**, never an input.

    The one collaborator on this object that nothing in the graph may consult. Every other field
    here changes what a run *does*; this one changes only what a run *says about itself* afterwards,
    and the default records nothing at all. A deployment supplies
    ``scripts.witness_span.SpanAttributeWitness``; every test and every local run keeps
    :data:`~careloop.graph.witness.NO_WITNESS`, so importing CareLoop never requires a telemetry
    stack.

    It is a :class:`~careloop.graph.witness.NullWitness` rather than ``None`` on purpose: with a
    real object always present, no node can be written to ask whether telemetry is enabled, and
    "is anybody listening" stops being a question the domain is able to branch on.
    """

    def __post_init__(self) -> None:
        #: Whether this run has already told the witness what became of the effect.
        #:
        #: **Telemetry only, and structurally so.** It is set at the two ``witness_effect`` call
        #: sites and read at one more, and nothing else may touch it —
        #: ``tests/test_saga_telemetry.py`` sweeps for that. No lifecycle, no verdict, no policy and
        #: no dispatch consults it, so an observability change cannot steer a case; that is the same
        #: rule :class:`~careloop.graph.witness.NullWitness` exists to make unaskable, applied to
        #: the one fact a trace-scoped claim genuinely needs.
        #:
        #: It is run-scoped because the question is run-scoped. *"Did the write happen in this
        #: trace?"* is not something the durable trail can answer — a case that entered
        #: ``VERIFYING`` from ``EXECUTING`` looks identical whether the execute node ran a
        #: microsecond ago or in a process that died last night — and
        #: :attr:`careloop.graph.witness.EffectDispatch.NOT_IN_THIS_TRACE` is a claim about *this
        #: trace*. Reading a durable state as a proxy for it is what made a resumed verification
        #: emit no effect claim at all.
        #:
        #: One-way, and only ever set to ``True``. A flag that could be cleared would be a flag a
        #: later edit could use to suppress a claim.
        self.effect_claimed: bool = False

        #: Every real external system this run reaches, the clinic among them.
        #:
        #: Assembled once so that "is this run bound to anything real" is one question with one
        #: answer, and so the two things a provider brings — a real write and the read that observes
        #: it — can never be supplied separately.
        self.bound: tuple[ExternalProvider, ...] = (
            self.providers
            if self.clinic is None
            else (*self.providers, clinic_provider(self.clinic))
        )

        if self.bound and self.executor is not None:
            raise ConflictingExecutors(
                "a run is bound to providers or to an executor, never to both: a provider builds "
                "the executor whose effects are real and the read-backs that observe them, and "
                "an executor supplied beside one could make either of those simulated"
            )
        if self.executor is None:
            self.executor = (
                MockExternalExecutor(handlers=merged_handlers(self.bound))
                if self.bound
                else MockExternalExecutor()
            )
        self.readbacks: dict[str, ReadBack] = merged_readbacks(self.bound)

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
    """Raised when a run is handed both a real provider and an executor."""


def _empty_registry():
    """A registry with no callbacks, for runs that deliberately omit the hook."""
    from strands.hooks.registry import HookRegistry

    return HookRegistry()


__all__ = [
    "ESTABLISHED_PATIENT_TRUST",
    "ConflictingExecutors",
    "Delivery",
    "Handoff",
    "Preparation",
    "Proposal",
    "RecheckTarget",
    "RunContext",
    "approval_for",
    "approval_id_for",
    "entered_verifying_from",
    "established_patients",
    "is_approved",
    "latest_delivery",
    "latest_proposal",
    "manual_handoff",
    "prior_denials",
    "proposal_for",
    "recheck_target",
    "unsettled_preparation",
]
