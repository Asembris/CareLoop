"""The graph's nodes.

Seven of them, each a thin :class:`~strands.multiagent.base.MultiAgentBase` wrapper around plain
Python. The wrapper exists because ``GraphBuilder.add_node`` rejects plain callables — spike
finding 5 — and it does exactly two things: run the body, and wrap what it returns in the
``AgentResult`` the graph's input builder requires. If the wrapper ever accumulates logic, the
"deterministic nodes" claim erodes, so :class:`DeterministicNode` has no branches at all.

**Every state change goes through** :func:`careloop.state.machine.transition`. Nodes never assign
``Case.state``; they could not, since ``Case`` is frozen. What a node decides is *which* legal
transition to make, and the transition table decides whether that is allowed.

**Every node reads the case from the store and writes it back.** Not for tidiness: the policy gate
is built over the store, so a node that transitioned without saving would have its own tool calls
judged against the previous state.

**Two nodes call a model.** ``triage`` and ``plan``, once each, and neither can act on what comes
back — the gate node decides, and it is not the node that asked.

The node bodies read top to bottom in the order a case moves through them:

``dispatch``  routes a case to wherever it should resume, and writes nothing
``triage``    classifies the inbound event                        NEW → GATHERING_EVIDENCE
``evidence``  reads documents, records facts, validates them      GATHERING_EVIDENCE → PLANNING
``plan``      asks the planner for one action                     PLANNING (no transition)
``gate``      decides allow / park / replan                       PLANNING → READY_TO_EXECUTE
                                                                  PLANNING → AWAITING_HUMAN
``execute``   dispatches the tool through both enforcement points READY_TO_EXECUTE → EXECUTING
                                                                  EXECUTING → VERIFYING
``verify``    checks the effect against what was authorised       VERIFYING → DONE | PLANNING
"""

from __future__ import annotations

from typing import Any

from strands.agent import AgentResult
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status
from strands.telemetry.metrics import EventLoopMetrics

from careloop.graph.context import (
    Proposal,
    RunContext,
    approval_for,
    approval_id_for,
    latest_proposal,
)
from careloop.graph.extract import record_document, validate_and_promote
from careloop.graph.verify import ReadBackFailed, Verification, rehydrate, verify_action
from careloop.policy.gate import PolicyDenied
from careloop.policy.hook import fire_before_tool_call, tool_use_id
from careloop.policy.verdicts import Decision, ReasonCode, Verdict
from careloop.state.case import (
    ActionRecord,
    Approval,
    ApprovalStatus,
    Case,
    CaseState,
    TimelineKind,
)
from careloop.state.machine import append_event, transition
from careloop.tools.executor import ExecutionResult
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.registry import REGISTRY


def _agent_result(text: str) -> AgentResult:
    """The minimal ``AgentResult`` the graph's input builder accepts.

    ``NodeResult.result`` must be an ``AgentResult``, a nested ``MultiAgentResult`` or an
    ``Exception``; a bare ``str`` fails deep inside the graph rather than at the call site. Spike
    finding 5 cost an iteration on exactly this.
    """
    return AgentResult(
        stop_reason="end_turn",
        message={"role": "assistant", "content": [{"text": text}]},
        metrics=EventLoopMetrics(),
        state={},
    )


class DeterministicNode(MultiAgentBase):
    """A graph node whose body is ordinary Python.

    No model, no tools, no branching in the wrapper. Subclasses implement :meth:`run` and return a
    one-line summary for the execution trace — never argument values, since a node summary is the
    one thing here that gets rendered into another node's input.
    """

    def __init__(self, node_id: str, context: RunContext) -> None:
        super().__init__()
        self.id = node_id
        self.context = context

    async def run(self) -> str:
        """Do the node's work and return a summary line."""
        raise NotImplementedError

    async def invoke_async(
        self, task: Any, invocation_state: dict[str, Any] | None = None, **kwargs: Any
    ) -> MultiAgentResult:
        """Run the body. ``task`` is ignored: business facts come from the store, not the graph.

        The graph threads each node's output text into the next node's input. CareLoop does not
        read it — a node that took its instructions from the previous node's prose would be a node
        an upstream model could steer.
        """
        summary = await self.run()
        return MultiAgentResult(
            status=Status.COMPLETED,
            results={
                self.id: NodeResult(
                    result=_agent_result(summary), status=Status.COMPLETED, execution_count=1
                )
            },
            execution_count=1,
        )


class DispatchNode(DeterministicNode):
    """The entry point, and the resume point.

    Writes nothing. It exists because ``GraphBuilder`` entry points cannot carry conditions, so
    routing a case that is already part-way through — one resumed after a caregiver approved
    something, days later, in another process — needs a node whose outgoing edges can.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        return f"dispatch: case is {case.state}"


class TriageNode(DeterministicNode):
    """Classify the inbound event. ``NEW`` → ``GATHERING_EVIDENCE``.

    **Triage records no facts, and that is deliberate.** Its classification goes to ``case.summary``
    and to the timeline, never to ``case.facts``. Two reasons, and the second was found by building
    it the other way first:

    Trust resolution matches on *value*, lowest wins. A triage-emitted ``UNTRUSTED`` fact holding
    the same string as a configured ``TRUSTED`` one would drag the legitimate value down — session
    5's delta D5, which is why ``TriageResult`` carries no destination.

    And the same collision breaks promotion from the other side. The evidence node extracts
    ``patient_id`` deterministically and validates it to ``CONTROLLED``; a triage-emitted duplicate
    of that value left at ``UNTRUSTED`` makes the promotion invisible, and
    :func:`careloop.state.evidence.promote_fact` refuses it outright. Model output therefore never
    becomes a policy input: every fact the engine reads was produced by deterministic code.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        result = await self.context.triage.classify(self.context.event_text)

        case = case.model_copy(update={"summary": result.summary})
        case = append_event(
            case,
            kind=TimelineKind.EVIDENCE_RECORDED,
            actor="triage",
            summary=f"classified as {result.category}, urgency {result.urgency}",
            diagnostic=(
                f"triage read the inbound event and returned category={result.category}, "
                f"urgency={result.urgency}, patient_id={result.patient_id!r}. None of this "
                f"becomes a fact: the evidence node extracts deterministically."
            ),
            payload={
                "category": str(result.category),
                "urgency": str(result.urgency),
                "patient_id": result.patient_id,
            },
            now=self.context.now(),
        )
        case = transition(
            case,
            CaseState.GATHERING_EVIDENCE,
            reason="inbound event classified",
            actor="triage",
            now=self.context.now(),
        )
        self.context.commit(case, expected_version=base)
        return f"triage: {result.category}/{result.urgency}"


class EvidenceNode(DeterministicNode):
    """Read the case's documents and record what they say.

    ``GATHERING_EVIDENCE`` → ``PLANNING``.

    Documents are read through the gated toolset, so even the read path gets a verdict. What comes
    back is recorded verbatim: page excerpts as ``UNTRUSTED`` facts, plus the structured values
    :data:`careloop.graph.extract.PATTERNS` finds. Validation then promotes the ``VALUE``-class
    ones to ``CONTROLLED`` **in place of** their untrusted originals.

    **This is where untrusted text enters the planner's context.** The excerpt facts are rendered
    into the next planning prompt inside the quoted evidence block, which is what makes the
    poisoned continuity notice a real temptation rather than a staged one. Nothing is sanitised on
    the way through; the authority lives in the policy engine, not in a filter.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        for document_id in self.context.document_ids:
            document = self.context.tools.call("fetch_document", {"document_id": document_id})
            case = record_document(case, document, now=self.context.now())

        case = validate_and_promote(
            case, now=self.context.now(), documents=self.context.tools.documents
        )
        case = transition(
            case,
            CaseState.PLANNING,
            reason=f"evidence gathered from {len(self.context.document_ids)} document(s)",
            actor="evidence",
            now=self.context.now(),
        )
        self.context.commit(case, expected_version=base)
        return f"evidence: {len(case.facts)} fact(s) on the case"


class PlanNode(DeterministicNode):
    """Ask the planner for one next action. No transition — the case stays ``PLANNING``.

    The proposal is written to the timeline as ``PLAN_PROPOSED`` and read back from there by the
    gate node. It is a proposal and nothing else: the planner's schema has no field through which
    it could ask to be executed, and this node has no code that would carry one out.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        intent = await self.context.planner.propose(case, self.context.denials(case))

        case = append_event(
            case,
            kind=TimelineKind.PLAN_PROPOSED,
            actor="planner",
            summary=f"proposed {intent.tool_name}",
            diagnostic=f"planner rationale: {intent.rationale}; args: {intent.args}",
            payload={
                "tool_name": intent.tool_name,
                "args": dict(intent.args),
                "rationale": intent.rationale,
            },
            now=self.context.now(),
        )
        self.context.commit(case, expected_version=base)
        return f"plan: proposed {intent.tool_name}"


class GateNode(DeterministicNode):
    """Decide what happens to the proposal. The demo-visible authority step.

    Three outcomes, and the state machine is why the decision has to happen here rather than at
    the tool boundary: ``AWAITING_HUMAN`` is reachable from ``PLANNING`` and from nowhere else an
    external effect could be proposed. By the time a call is being dispatched the case is
    ``EXECUTING``, and there is no legal edge from there to a parked case.

    ``ALLOW``          → ``READY_TO_EXECUTE``. The tool boundary will check again.
    ``REQUIRE_HUMAN``  → an ``Approval`` and ``AWAITING_HUMAN``. The run ends here; no outgoing
                         edge condition holds, so the graph completes with the case parked.
    ``DENY``           → nothing moves. The replan edge takes the case back to the planner, which
                         is told the tool and the reason code and nothing more.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        proposal = latest_proposal(case)
        if proposal is None:
            case = transition(
                case,
                CaseState.FAILED,
                reason="the gate was reached with no proposal to decide",
                actor="policy",
                now=self.context.now(),
            )
            self.context.commit(case, expected_version=base)
            return "gate: nothing to decide"

        decision = self.context.gate.decide(proposal.tool_name, proposal.args)

        if decision.verdict is Verdict.ALLOW:
            case = _record_decision(case, decision, TimelineKind.POLICY_ALLOW, self.context.now())
            case = transition(
                case,
                CaseState.READY_TO_EXECUTE,
                reason=f"policy allowed {proposal.tool_name}",
                actor="policy",
                now=self.context.now(),
            )
        elif decision.verdict is Verdict.REQUIRE_HUMAN:
            case = _record_decision(
                case, decision, TimelineKind.POLICY_REQUIRE_HUMAN, self.context.now()
            )
            case = _request_approval(case, proposal, decision, self.context.now())
            case = transition(
                case,
                CaseState.AWAITING_HUMAN,
                reason=f"{proposal.tool_name} needs a caregiver decision",
                actor="policy",
                now=self.context.now(),
            )
        else:
            case = _record_decision(case, decision, TimelineKind.POLICY_DENY, self.context.now())
            if _every_tool_has_been_refused(case):
                case = _park_with_nothing_safe_to_do(case, self.context.now())
                self.context.commit(case, expected_version=base)
                return "gate: every action was refused; parked for a caregiver"

        self.context.commit(case, expected_version=base)
        return f"gate: {decision.verdict} for {proposal.tool_name}"


class ExecuteNode(DeterministicNode):
    """Dispatch the approved action through both enforcement points.

    ``READY_TO_EXECUTE`` → ``EXECUTING`` → ``VERIFYING`` (or ``FAILED``).

    **The durable ledger comes first.** Before anything is dispatched, ``case.action_for(key)`` is
    consulted. If the effect already happened — in this run, in a previous one, or in a process
    that exited days ago — nothing is invoked and the recorded result is replayed. The executor's
    own ledger is an in-run optimisation behind that, not the source of truth; open decision 7
    closes here, and invariant 8 is why.

    **Then the hook, then the backstop.** The ``BeforeToolCall`` event is fired against the run's
    registry, and the gated toolset checks again inside the call. Both ask the same gate, which
    caches on a key carrying the case version, so the two layers cost one evaluation.

    A refusal *here* is terminal rather than a replan. The gate node already allowed this call; a
    boundary refusal means the two enforcement points disagreed, which is a fault in CareLoop
    rather than a planner that needs another go. ``EXECUTING`` has no edge back to ``PLANNING``
    either, and that is the state machine agreeing.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        proposal = latest_proposal(case)
        if proposal is None:
            return self._fail(case, base, "the executor was reached with no proposal")

        approval_check = self._approval_problem(case, proposal)
        if approval_check is not None:
            return self._fail(case, base, approval_check)

        case = transition(
            case,
            CaseState.EXECUTING,
            reason=f"dispatching {proposal.tool_name}",
            actor="system",
            now=self.context.now(),
        )
        # Committed before the call so both enforcement points judge the state the case is
        # actually in. The gate reads the store, not this local copy.
        self.context.commit(case, expected_version=base)
        base = case.version

        key = idempotency_key(self.context.case_id, proposal.tool_name, proposal.args)

        recorded = case.action_for(key)
        if recorded is not None:
            case = append_event(
                case,
                kind=TimelineKind.DUPLICATE_SUPPRESSED,
                actor="system",
                summary=f"{proposal.tool_name} already performed for this case",
                diagnostic=(
                    f"idempotency key {key} is already recorded on the case; the effect was not "
                    f"repeated and the recorded result was replayed"
                ),
                payload={"tool_name": proposal.tool_name, "idempotency_key": key},
                now=self.context.now(),
            )
            case = transition(
                case,
                CaseState.VERIFYING,
                reason=f"{proposal.tool_name} was already performed",
                actor="system",
                now=self.context.now(),
            )
            self.context.commit(case, expected_version=base)
            return f"execute: {proposal.tool_name} suppressed as a duplicate"

        cancelled = await fire_before_tool_call(
            self.context.hooks,
            proposal.tool_name,
            proposal.args,
            tool_use_id=tool_use_id(key),
        )
        if cancelled is not None:
            return self._refused(case, base, proposal, "the BeforeToolCall hook cancelled the call")

        try:
            result = self.context.tools.call(proposal.tool_name, proposal.args)
        except PolicyDenied as denied:
            return self._refused(
                case,
                base,
                proposal,
                f"the gated tool refused the call: {denied.decision}",
            )

        case = _record_action(case, proposal, key, result, self.context.now())
        case = transition(
            case,
            CaseState.VERIFYING,
            reason=f"{proposal.tool_name} performed",
            actor="system",
            now=self.context.now(),
        )
        self.context.commit(case, expected_version=base)
        return f"execute: {proposal.tool_name} performed"

    def _approval_problem(self, case: Case, proposal: Proposal) -> str | None:
        """Why this proposal may not be executed under the case's approvals, if it may not.

        Two questions, both answered from the durable record rather than by re-deriving policy.

        An outstanding decision stops everything: a case released for execution while a caregiver
        is still being asked something is a contradiction, whatever the question was about.

        Otherwise, the only proposals that need an approval are the ones the gate **parked**, and
        an approval exists for exactly those — :func:`~careloop.graph.nodes._request_approval`
        records it against the tool and the argument digest at the moment the gate decides
        ``REQUIRE_HUMAN``. So an approval covering exactly this call must be ``APPROVED``, and a
        proposal with no approval at all is one the gate allowed outright.

        **This was case-scoped until commit 7 and that was a latent bug**, found by running the
        resume rather than by reading the code. The old rule was "a case with any approval needs an
        approval for every action", which holds for a case that does one thing and breaks the
        moment a case does two: a reopened case whose first action needed a caregiver and whose
        second is autonomous was refused for want of an approval nobody should have been asked for.
        Call-scoping matches what an approval means everywhere else — it authorises one exact
        tool call, not a tool and not a case.

        The lookup names ``proposal.tool_name`` as well as the digest. Passing the digest alone
        asked whether *some* decision had been made about an argument map that looks like this one,
        which is a different question and answers it about the wrong action in both directions: a
        refusal recorded for another tool would have blocked this dispatch, and an approval recorded
        for another tool would have satisfied ``args_approved`` at the boundary a few lines below.

        What this does **not** do is decide whether an unapproved action may proceed. That is
        ``rules.yaml``'s ``args_approved`` clause, evaluated at the tool boundary a few lines
        below. This guard's own contribution is to refuse *before* the case transitions to
        ``EXECUTING``, and to name the approval rather than report a disagreement between two
        enforcement points.
        """
        if case.pending_approval() is not None:
            return "an approval is still outstanding"

        approval = approval_for(case, proposal.tool_name, proposal.digest)
        if approval is not None and approval.status is not ApprovalStatus.APPROVED:
            return "the caregiver did not approve the call being executed"
        return None

    def _fail(self, case: Case, base: int, reason: str) -> str:
        case = transition(
            case, CaseState.FAILED, reason=reason, actor="system", now=self.context.now()
        )
        self.context.commit(case, expected_version=base)
        return f"execute: failed, {reason}"

    def _refused(self, case: Case, base: int, proposal: Proposal, detail: str) -> str:
        """A refusal at the tool boundary after the gate allowed. A fault, not a replan."""
        case = append_event(
            case,
            kind=TimelineKind.POLICY_DENY,
            actor="policy",
            summary=f"{proposal.tool_name} refused at the tool boundary",
            diagnostic=(
                f"{detail}. The gate node allowed this call while the case was PLANNING and the "
                f"tool boundary refused it while EXECUTING; two enforcement points disagreeing is "
                f"a CareLoop fault, not a planner that needs another attempt."
            ),
            payload={"tool_name": proposal.tool_name, "reason_code": "NO_RULE_MATCH"},
            now=self.context.now(),
        )
        case = transition(
            case,
            CaseState.FAILED,
            reason=f"{proposal.tool_name} refused at the tool boundary",
            actor="policy",
            now=self.context.now(),
        )
        self.context.commit(case, expected_version=base)
        return f"execute: {proposal.tool_name} refused at the tool boundary"


class VerifyNode(DeterministicNode):
    """Check the effect against what was authorised. ``VERIFYING`` → ``DONE`` or ``PLANNING``.

    The only path to ``DONE``. See :mod:`careloop.graph.verify` for what "verified" means and,
    just as importantly, what it does not claim.

    **Where the run is bound to a real external system, the check reads it back.** The read is a
    separate request rather than the effect's own response, and a system that cannot be reached or
    does not hold the effect fails verification rather than crashing the run — an effect the world
    cannot show CareLoop is an effect CareLoop cannot claim happened.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        proposal = latest_proposal(case)
        if proposal is None:
            case = transition(
                case,
                CaseState.FAILED,
                reason="verification was reached with no proposal",
                actor="system",
                now=self.context.now(),
            )
            self.context.commit(case, expected_version=base)
            return "verify: nothing to verify"

        key = idempotency_key(self.context.case_id, proposal.tool_name, proposal.args)
        record = case.action_for(key)
        if record is None:
            case = transition(
                case,
                CaseState.FAILED,
                reason="verification found no recorded action for the authorised arguments",
                actor="system",
                now=self.context.now(),
            )
            self.context.commit(case, expected_version=base)
            return "verify: no action recorded"

        result = rehydrate(record.action_type, record.result)
        verification = self._verify(proposal, record, result)

        if verification.passed:
            case = append_event(
                case,
                kind=TimelineKind.VERIFICATION_PASSED,
                actor="system",
                summary=f"{proposal.tool_name} verified",
                diagnostic=verification.diagnostic,
                payload={
                    "tool_name": proposal.tool_name,
                    "read_back": str(verification.externally_observed).lower(),
                },
                now=self.context.now(),
            )
            case = transition(
                case,
                CaseState.DONE,
                reason=f"{proposal.tool_name} performed and verified",
                actor="system",
                now=self.context.now(),
            )
        else:
            case = append_event(
                case,
                kind=TimelineKind.VERIFICATION_FAILED,
                actor="system",
                summary=f"{proposal.tool_name} did not verify",
                diagnostic=verification.diagnostic,
                payload={
                    "tool_name": proposal.tool_name,
                    "read_back": str(verification.externally_observed).lower(),
                },
                now=self.context.now(),
            )
            case = transition(
                case,
                CaseState.PLANNING,
                reason=f"{proposal.tool_name} did not verify",
                actor="system",
                now=self.context.now(),
            )

        self.context.commit(case, expected_version=base)
        return f"verify: {'passed' if verification.passed else 'failed'}"

    def _verify(self, proposal: Proposal, record: ActionRecord, result) -> Verification:
        """Verify the recorded effect, reading the external system back where there is one.

        A read-back failure is a *verification* failure, not an exception out of the node. The two
        outcomes look the same from here on purpose: "the clinic says something different" and "the
        clinic cannot show me the appointment at all" are both reasons not to call an effect
        verified, and turning the second into a crash would make an integration outage look like a
        CareLoop fault.
        """
        observed = None
        readback = self.context.readbacks.get(record.action_type)
        if readback is not None:
            try:
                observed = readback(result)
            except ReadBackFailed as unreadable:
                return Verification(
                    passed=False, diagnostic=str(unreadable), externally_observed=True
                )

        return verify_action(
            case_id=self.context.case_id,
            action_type=proposal.tool_name,
            args=proposal.args,
            record=record,
            result=result,
            observed=observed,
        )


# -- shared writers --------------------------------------------------------------------------


def _record_decision(case: Case, decision: Decision, kind: TimelineKind, now) -> Case:
    """Write one policy verdict to the timeline, diagnostics and all.

    The timeline is where the detail belongs: the matched rule, every argument's class, the trust
    each value resolved to, and the raw arguments. What the planner is later told is rebuilt from
    ``payload["reason_code"]`` alone.
    """
    return append_event(
        case,
        kind=kind,
        actor="policy",
        summary=f"{decision.verdict} for {decision.tool_name}",
        diagnostic=(
            f"{decision.diagnostic} [rule={decision.matched_rule_id or 'default'}, "
            f"classes={decision.arg_classes}, trust={decision.arg_trust}, args={decision.args}]"
        ),
        payload={
            "tool_name": decision.tool_name,
            "verdict": str(decision.verdict),
            "reason_code": str(decision.reason_code) if decision.reason_code else "",
            "matched_rule_id": decision.matched_rule_id or "",
        },
        now=now,
    )


def _request_approval(case: Case, proposal: Proposal, decision: Decision, now) -> Case:
    """Park the proposal as a pending caregiver decision.

    The approval carries the tool name and the *digest* of the arguments, not the arguments
    themselves. Both halves are the capability's identity and both are matched:
    :meth:`careloop.state.case.Case.is_approved` requires them together, so recording only the tool
    name would let a later proposal for the same tool execute under this decision, and recording
    only the digest would let another tool do so with an argument map that hashes the same.

    **The identifier is the request, not the action.** ``case.next_seq()`` is the position the
    ``APPROVAL_REQUESTED`` entry written below will occupy, so the record and the audit entry that
    announces it carry the same number by construction, and two requests on one case cannot collide
    — see :func:`careloop.graph.context.approval_id_for` for why that used to be derived from the
    digest and what it cost.
    """
    digest = proposal.digest
    approval = Approval(
        approval_id=approval_id_for(case.next_seq()),
        tool_name=proposal.tool_name,
        args_digest=digest,
        reason_code=str(decision.reason_code),
        status=ApprovalStatus.PENDING,
        requested_at=now,
    )
    case = case.model_copy(update={"approvals": (*case.approvals, approval)})
    return append_event(
        case,
        kind=TimelineKind.APPROVAL_REQUESTED,
        actor="policy",
        summary=f"caregiver decision requested for {proposal.tool_name}",
        diagnostic=(
            f"approval {approval.approval_id} covers args digest {digest}; "
            f"proposed args were {proposal.args}"
        ),
        payload={
            "approval_id": approval.approval_id,
            "tool_name": proposal.tool_name,
            "reason_code": approval.reason_code,
        },
        now=now,
    )


def _every_tool_has_been_refused(case: Case) -> bool:
    """Whether the planner has now proposed every tool there is and been refused each time.

    **Narrow on purpose.** This is not "the case is stuck"; it is one specific, checkable
    condition, and it is the only one that parks a case this way. A generic fallback for anything
    that fails to converge would quietly become the place every unexplained problem ends up, and
    ``FAILED`` would stop meaning "something broke" — which is the meaning the malformed and bound
    scenarios depend on.

    Refusals are counted by tool name rather than by arguments. A tool refused with one set of
    arguments might be allowed with another, so this is the conservative reading: the planner has
    tried everything the catalogue offers *at least once* and has nothing new to reach for. It is
    also exactly what was observed live — given a poisoned case, the planner works through the
    whole catalogue and is refused at each step.
    """
    refused = {
        event.payload.get("tool_name", "")
        for event in case.timeline
        if event.kind is TimelineKind.POLICY_DENY
    }
    return REGISTRY.names <= refused


def _park_with_nothing_safe_to_do(case: Case, now) -> Case:
    """Stop, and hand the case to a person. **No approval is raised, because there is nothing to
    approve.**

    "Every available action was correctly refused" is the system working exactly as designed, and
    on a poisoned case it is the best available outcome. Reporting a defence as a fault would be
    wrong in the product and wrong in the audit trail, so this parks rather than fails: the agent
    hit a wall, refused everything, and said so.

    **This used to raise an ``Approval`` with both identifying fields empty, and that was the
    defect.** The intent was right — the docstring said approving it released nothing — but an
    ``Approval`` is not a notification, and every consumer downstream reads a pending one as a
    decision waiting to be made. :func:`careloop.graph.runner._decide` did exactly that: it moved
    the case to ``READY_TO_EXECUTE``, where the execute node picked up ``latest_proposal`` — which
    at this point is by construction a proposal policy has just **denied** — and dispatched it. The
    tool boundary refused it a second time and the case ended ``FAILED``, with a timeline entry
    blaming a disagreement between CareLoop's two enforcement points. They had never disagreed.
    Nothing had ever allowed that call; the runner had released a case with no authority behind it.

    So the handoff is a **status**, not a capability, and it lives where statuses live: the case's
    state and one timeline entry. There is no approval id, so there is nothing for any client to
    post a decision to — the refusal does not depend on a caregiver being shown the right buttons.

    What a caregiver sees is unchanged: the case is still ``AWAITING_HUMAN``, still in the "Needs
    You" section, and :func:`careloop.api.projections.decision_view` still renders a card. It reads
    the entry written here rather than an approval, which is why removing the approval cost the UI
    nothing.
    """
    case = append_event(
        case,
        kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
        actor="policy",
        summary="no safe action is available; manual follow-up is required",
        diagnostic=(
            f"every tool in the registry has been proposed and refused: "
            f"{sorted(REGISTRY.names)}. Parked rather than failed — the refusals are the system "
            f"working, and FAILED means something broke. No approval is raised: there is no "
            f"proposal to authorise, and a record that authorises nothing must not be decidable."
        ),
        payload={"reason_code": str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)},
        now=now,
    )
    return transition(
        case,
        CaseState.AWAITING_HUMAN,
        reason="every available action was refused",
        actor="policy",
        now=now,
    )


def _record_action(case: Case, proposal: Proposal, key: str, result: ExecutionResult, now) -> Case:
    """Write the durable record of one performed effect, then note it on the timeline."""
    record = ActionRecord(
        idempotency_key=key,
        action_type=proposal.tool_name,
        args_digest=args_digest(proposal.args),
        result=result.result.model_dump_json(),
        recorded_at=now,
    )
    case = case.model_copy(update={"actions": (*case.actions, record)})

    kind = TimelineKind.ACTION_EXECUTED if result.executed else TimelineKind.DUPLICATE_SUPPRESSED
    return append_event(
        case,
        kind=kind,
        actor="system",
        summary=f"{proposal.tool_name} {'performed' if result.executed else 'suppressed'}",
        diagnostic=(
            f"outcome={result.outcome}, key={key}, args={proposal.args}, result={record.result}"
        ),
        payload={
            "tool_name": proposal.tool_name,
            "idempotency_key": key,
            "outcome": str(result.outcome),
        },
        now=now,
    )


__all__ = [
    "DeterministicNode",
    "DispatchNode",
    "EvidenceNode",
    "ExecuteNode",
    "GateNode",
    "PlanNode",
    "TriageNode",
    "VerifyNode",
]
