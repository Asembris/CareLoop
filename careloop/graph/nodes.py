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

**Three nodes park a case for a person, and they mean three different things.** ``plan`` parks when
no valid next action could be formed at all; ``gate`` parks when every action was formed and
refused; ``verify`` parks when an action was carried out and cannot be confirmed. All three write a
``MANUAL_HANDOFF_REQUIRED`` entry, raise **no** ``Approval``, and are told apart by their reason
code — see :func:`_park_with_nothing_proposable`, :func:`_park_with_nothing_safe_to_do` and
:func:`_park_with_nothing_certifiable`.

The node bodies read top to bottom in the order a case moves through them:

``dispatch``  routes a case to wherever it should resume, and writes nothing
``triage``    classifies the inbound event                        NEW → GATHERING_EVIDENCE
``evidence``  reads documents, records facts, validates them      GATHERING_EVIDENCE → PLANNING
``plan``      asks the planner for one action                     PLANNING (no transition)
                                                                  PLANNING → AWAITING_HUMAN
``gate``      decides allow / park / replan                       PLANNING → READY_TO_EXECUTE
                                                                  PLANNING → AWAITING_HUMAN
``execute``   dispatches the tool through both enforcement points READY_TO_EXECUTE → EXECUTING
                                                                  EXECUTING → VERIFYING
``verify``    checks the effect against what was authorised       VERIFYING → DONE
                                                                  VERIFYING → AWAITING_HUMAN

**Two transitions reach ``PLANNING`` and both are pre-effect.** The gate leaves a denied proposal
there for another attempt, and a caregiver's rejection returns a parked case there. ``verify`` does
not and may not: by the time it runs an effect has been carried out, so its failure means "this
cannot be certified" rather than "this did not work", and the case goes to a person.
:data:`careloop.state.machine.TRANSITIONS` refuses ``VERIFYING → PLANNING`` outright, so that is a
property of the state machine rather than of this module remembering.

**Two nodes tell a witness what already happened, and neither can be told anything back.**
``execute`` claims what became of the external effect and ``verify`` claims what an independent
read established and what was concluded from it — both **after** the domain has decided, and both
through :mod:`careloop.graph.witness`, whose two seam functions return ``None`` and never raise. No
line in this module asks whether a witness is present, whether it succeeded, or whether telemetry
is enabled; the default records nothing at all, and a run under it is byte-for-byte the run under
one that records everything. That is invariant-shaped rather than polite: a node that could read a
witness is a node an observability outage could steer.

**A caregiver may send a parked case back to ``verify``, and to nowhere else.**
:func:`careloop.graph.runner.request_recheck` moves ``AWAITING_HUMAN → VERIFYING`` so ``dispatch``
routes into this node again — no new edge, no new node, and pointedly not back to ``plan`` or
``execute``. It is the one entry into ``VERIFYING`` no node writes, and no node may: nothing in this
module names that function, which ``tests/test_recheck_writers.py`` enforces.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from strands.agent import AgentResult
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status
from strands.telemetry.metrics import EventLoopMetrics

from careloop.agents.invoke import RECOVERABLE_PROPOSAL_FAILURES
from careloop.graph.context import (
    Preparation,
    Proposal,
    Refusal,
    RunContext,
    approval_for,
    approval_id_for,
    entered_verifying_from,
    established_patients,
    is_repeat_refusal,
    latest_proposal,
    policy_fingerprint,
    refusals_at,
    unsettled_preparation,
)
from careloop.graph.extract import record_document, validate_and_promote
from careloop.graph.recovery import (
    DispatchRefused,
    DispatchUnavailable,
    dispatch_prepared,
    may_replay,
)
from careloop.graph.verify import (
    ObservationOutcome,
    ReadBackFailed,
    Verification,
    rehydrate,
    verify_action,
)
from careloop.graph.witness import EffectDispatch, witness_effect, witness_observation
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
from careloop.tools.results import ActionResult


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
    """Ask the planner for one next action.

    Two outcomes, and only the first is a proposal.

    A proposal is written to the timeline as ``PLAN_PROPOSED`` and read back from there by the
    gate node, with **no transition** — the case stays ``PLANNING``. It is a proposal and nothing
    else: the planner's schema has no field through which it could ask to be executed, and this
    node has no code that would carry one out.

    **A provider that cannot form one parks the case instead.** ``PLANNING`` →
    ``AWAITING_HUMAN``, through :func:`_park_with_nothing_proposable`. The bound is
    :func:`careloop.agents.invoke.proposal_tool_call`'s and this node does not add to it: by the
    time the exception arrives here the one retry has already been spent, so what is caught is
    *exhaustion*, never a first failure.

    **The caught set is exactly** :data:`careloop.agents.invoke.RECOVERABLE_PROPOSAL_FAILURES`,
    imported rather than restated, because a second list is a second place to drift and the one
    direction that must not drift is wider. Everything else — a throttle, a credential error, a
    wedged call the node timeout kills, a bug in this package — still leaves the node, still
    reaches :func:`careloop.graph.runner.run_case`, and still ends the case ``FAILED``. That is
    the distinction ``FAILED`` exists to carry: something broke, rather than the model failed to
    write down an action.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        try:
            intent = await self.context.planner.propose(case, self.context.denials(case))
        except RECOVERABLE_PROPOSAL_FAILURES as failure:
            case = _park_with_nothing_proposable(case, failure, self.context.now())
            self.context.commit(case, expected_version=base)
            return "plan: no valid next action could be formed; parked for manual handling"

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

        # **The patient boundary, asked here because here is where a case can still be parked.**
        #
        # ``AWAITING_HUMAN`` is reachable from ``PLANNING`` and from nowhere an external effect
        # could be proposed -- which is the same sentence this class's docstring opens with, and it
        # is why the *primary* enforcement point for the patient boundary is this node rather than
        # the executor. By the time the executor runs the case is ``READY_TO_EXECUTE``, whose only
        # exits are ``EXECUTING`` and ``FAILED``, so a refusal there can only be a failure.
        #
        # Asked **before** the policy engine, deliberately. Not because the order changes the
        # outcome -- neither verdict depends on the other -- but because a ``POLICY_ALLOW`` on the
        # trail for a case CareLoop then refused to act on reads as a contradiction, and because
        # asking the cheaper structural question first is what every other node here does.
        #
        # Asked **live**. ``established_patients`` reads the case as the store holds it now, and the
        # authority answers from current state. Both halves matter: the patients are only known
        # because the evidence node has already run, and the answer is only correct because nobody
        # cached it when the run began.
        if not self.context.patient_authority.allows(established_patients(case)):
            case = _park_with_no_patient_authority(case, self.context.now())
            self.context.commit(case, expected_version=base)
            return "gate: parked; this case is not one this run may act on"

        decision = self.context.gate.decide(proposal.tool_name, proposal.args)
        # Computed from the case the engine just judged, before this verdict is appended. Nothing
        # the fingerprint reads is changed by appending a timeline entry, so the order is not
        # load-bearing — but reading it here makes "these are the inputs that produced this
        # verdict" true by construction rather than by inspection.
        fingerprint = policy_fingerprint(case)

        if decision.verdict is Verdict.ALLOW:
            case = _record_decision(
                case, decision, TimelineKind.POLICY_ALLOW, self.context.now(), fingerprint
            )
            case = transition(
                case,
                CaseState.READY_TO_EXECUTE,
                reason=f"policy allowed {proposal.tool_name}",
                actor="policy",
                now=self.context.now(),
            )
        elif decision.verdict is Verdict.REQUIRE_HUMAN:
            case = _record_decision(
                case, decision, TimelineKind.POLICY_REQUIRE_HUMAN, self.context.now(), fingerprint
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
            refusal = Refusal(
                tool_name=decision.tool_name,
                args_digest=args_digest(decision.args),
                reason_code=str(decision.reason_code) if decision.reason_code else "",
                fingerprint=fingerprint,
            )
            # Asked before this refusal is written, or every refusal would repeat itself.
            repeated = is_repeat_refusal(case, refusal)
            case = _record_decision(
                case, decision, TimelineKind.POLICY_DENY, self.context.now(), fingerprint
            )
            if (reason := _nothing_left_to_try(case, fingerprint, repeated=repeated)) is not None:
                case = _park_with_nothing_safe_to_do(case, self.context.now(), reason)
                self.context.commit(case, expected_version=base)
                return f"gate: {reason.summary}; parked for a caregiver"

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

    **Then the durable intent, in the same commit as the state change.** An action that is *not* a
    duplicate gets an ``ACTION_PREPARED`` entry naming the tool, the idempotency key and the
    argument digest, written into the same ``save`` that moves the case to ``EXECUTING``. That entry
    is the whole basis of crash recovery: it freezes the identity of the authorised action before
    anything can reach the outside world, so a process that dies mid-effect leaves behind the exact
    key a later one must reuse rather than a case that has to guess. It claims nothing about the
    provider — see :class:`careloop.graph.context.Preparation`.

    **The order of those two is load-bearing.** Suppression is settled first, so a logical action
    the case already holds a result for never acquires a preparation. A stale preparation on a
    completed action would be an unsettled intent for :class:`ReconcileNode` to find, and it would
    dispatch an effect that had already happened.

    **Then the hook, then the backstop.** The ``BeforeToolCall`` event is fired against the run's
    registry, and the gated toolset checks again inside the call. Both ask the same gate, which
    caches on a key carrying the case version, so the two layers cost one evaluation.

    A refusal *here* is terminal rather than a replan. The gate node already allowed this call; a
    boundary refusal means the two enforcement points disagreed, which is a fault in CareLoop
    rather than a planner that needs another go. ``EXECUTING`` has no edge back to ``PLANNING``
    either, and that is the state machine agreeing.

    **Two of the three exits claim what became of the effect, and the third deliberately does not.**
    The duplicate branch claims ``SUPPRESSED`` and the dispatch claims ``PERFORMED``, each on the
    first line at which the fact is established rather than at a convenient one. A call that
    *raised* — a clinic that could not be reached, or one that refused — claims **nothing**: a
    trace then carries an HTTP write that CareLoop's own evidence does not account for, which is
    exactly what an outside reader should report, and shaping this method's exception handling to
    produce a nicer telemetry story would be telemetry deciding how the domain fails.
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

        # **Derived before anything moves, because it is what the next two decisions are about.**
        # The key is a pure function of the case, the tool and the canonical arguments, so it can be
        # taken here as well as anywhere — and taking it here is what lets duplicate suppression be
        # resolved *before* a preparation is written. A completed logical action must never acquire
        # a preparation: ``ACTION_PREPARED`` means "about to be attempted", and writing one for an
        # effect that already happened would leave a stale intent on the trail for a recovery to
        # find and act on.
        key = idempotency_key(self.context.case_id, proposal.tool_name, proposal.args)

        recorded = case.action_for(key)
        if recorded is not None:
            # Claimed the moment the fact is established, which is here: this branch is taken
            # before the BeforeToolCall hook and before the gated toolset, and it returns, so no
            # HTTP can leave the process for this effect in this run. Nothing below can turn that
            # into a dispatch.
            witness_effect(
                self.context.witness,
                action_type=proposal.tool_name,
                dispatch=EffectDispatch.SUPPRESSED,
                args=proposal.args,
            )
            self.context.effect_claimed = True
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
            # **Both transitions in one commit, and no preparation between them.** The state machine
            # has no ``READY_TO_EXECUTE -> VERIFYING`` edge and must not grow one — ``EXECUTING`` is
            # where an effect is dispatched from, and a route that skipped it would be a second way
            # to reach verification. Passing through it without a separate commit is what keeps the
            # suppression atomic: there is no instant at which this case is durably ``EXECUTING``
            # with nothing prepared, which is exactly the shape ``ReconcileNode`` fails closed on.
            case = transition(
                case,
                CaseState.EXECUTING,
                reason=f"dispatching {proposal.tool_name}",
                actor="system",
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

        # **The second enforcement point for the patient boundary, and the last one there is.**
        #
        # Placed *after* duplicate suppression and *before* the preparation, which is the only
        # window that is correct. After suppression, because a duplicate replays a recorded result
        # and makes no provider call -- it settles an effect that already happened, and refusing it
        # would strand a case over a decision that changes nothing outside. Before the preparation,
        # because ``ACTION_PREPARED`` means *about to be attempted* and reconciliation acts on one:
        # writing a preparation and then refusing would leave an unsettled intent for the startup
        # sweep to find and dispatch, which is this check being routed around by the recovery path.
        #
        # The gate node asks the same question one node earlier and parks properly. This one exists
        # for the path that never reaches the gate -- a case released by an approval resumes
        # ``dispatch -> execute`` -- and for the reason the gated toolset exists beside the
        # ``BeforeToolCall`` hook: either alone would do, so a future edit has to defeat both.
        # Reaching it means the layers disagreed, which is what ``_fail`` means in this node
        # already; the API refuses that path at ``decide`` before any state moves, so it should
        # never fire.
        if not self.context.patient_authority.allows(established_patients(case)):
            return self._fail(
                case,
                base,
                "the run is not authorised to act for the people this case is about",
            )

        # **The durable intent and the state change are one commit.** Everything above has settled
        # that this action has not happened; this is the last line before anything could reach the
        # outside world, and what it writes down is *which* action is about to be attempted. A
        # process that dies at any point after this finds the identity waiting for it; one that dies
        # before it finds a case that never left ``READY_TO_EXECUTE``, which dispatch routes today.
        #
        # Two writes, one ``save``: ``SqliteCaseStore.save`` puts the case document and its new
        # timeline rows in one ``BEGIN IMMEDIATE`` ... ``COMMIT``, so "the case is EXECUTING" and
        # "an attempt is prepared" cannot come apart. Splitting them would create exactly the state
        # this whole patch exists to abolish.
        case = append_event(
            case,
            kind=TimelineKind.ACTION_PREPARED,
            actor="system",
            summary=f"{proposal.tool_name} prepared for dispatch",
            diagnostic=(
                f"the authorised arguments for {proposal.tool_name} key to {key}; that identity is "
                f"frozen here, before any request can leave this process, so a later run can reuse "
                f"it rather than derive a new one. This records what is about to be attempted and "
                f"says nothing about whether it reached anything."
            ),
            payload={
                "tool_name": proposal.tool_name,
                "idempotency_key": key,
                "args_digest": args_digest(proposal.args),
            },
            now=self.context.now(),
        )
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

        cancelled = await fire_before_tool_call(
            self.context.hooks,
            proposal.tool_name,
            proposal.args,
            tool_use_id=tool_use_id(key),
        )
        if cancelled is not None:
            return self._refused(case, base, proposal, "the BeforeToolCall hook cancelled the call")

        try:
            result = dispatch_prepared(
                lambda: self.context.tools.call(proposal.tool_name, proposal.args)
            )
        except PolicyDenied as denied:
            return self._refused(
                case,
                base,
                proposal,
                f"the gated tool refused the call: {denied.decision}",
            )
        except DispatchUnavailable:
            # **Nobody answered, and that is not a failure.** The request may have reached the
            # provider and committed, or may never have arrived; CareLoop cannot tell from here, and
            # the appointment can already exist while this process holds nothing. Reproduced against
            # a real clinic before this branch existed: the exception left the node, ``run_case``
            # marked the case ``FAILED`` — terminal, unrecoverable — and the clinic held the
            # booking, with the case's own ``ACTION_PREPARED`` entry naming the action nobody would
            # ever look for again.
            #
            # So the outcome is recorded as unknown rather than as absent, and the case goes
            # wherever an unknown outcome goes for this action type. That is the same question
            # ``ReconcileNode`` asks after a crash, and it is answered by the same function.
            return _stop_with_the_outcome_unestablished(self, case, base, proposal, key)

        # The call returned, so the effect really was dispatched — that is the fact being claimed,
        # and this is the first line at which it is known. **Before the record and before the
        # commit**, deliberately: if the conditional write below loses a race the external effect
        # still happened, and a claim emitted only after persistence would leave a trace carrying
        # a real external write that CareLoop's own telemetry disowns.
        witness_effect(
            self.context.witness,
            action_type=proposal.tool_name,
            dispatch=EffectDispatch.PERFORMED,
            args=proposal.args,
        )
        self.context.effect_claimed = True

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


class ReconcileNode(DeterministicNode):
    """Find out what became of a durably prepared action nobody recorded the result of.

    ``EXECUTING`` → ``VERIFYING`` / ``WAITING_EXTERNAL`` / ``AWAITING_HUMAN`` / ``FAILED``, and
    ``WAITING_EXTERNAL`` → ``EXECUTING`` on the way back in.

    **The recovery entry point, and the only one.** ``dispatch`` routes here for a case a previous
    process abandoned mid-effect: ``EXECUTING`` with an ``ACTION_PREPARED`` entry and no
    ``ActionRecord`` under its key. Before this node existed such a case had no outgoing edge at
    all — measured, it ran ``("dispatch",)``, returned ``Status.COMPLETED``, committed nothing, and
    sat in ``EXECUTING`` forever while the dashboard showed it as working.

    **Nothing here asks a model anything.** No planner, no triage, no proposal, no new arguments and
    no new identity. Everything it acts on was durably written before anything could leave the
    process: the tool and the arguments from the ``PLAN_PROPOSED`` entry, the idempotency key from
    the ``ACTION_PREPARED`` entry. ``tests/test_reconcile_writers.py`` sweeps this module to keep it
    that way.

    **Two identities, and they must agree before anything moves.** The stored key is read from the
    preparation and the same key is re-derived from the durable proposal. Persisting alone would
    lose drift detection; recomputing alone would lose crash-auditability; requiring agreement loses
    neither, and a disagreement is *the finding* rather than something to resolve by preferring one.
    That is :func:`careloop.graph.context.recheck_target`'s consistency clause applied to the
    problem it was written for.

    **Replay is licensed by the provider's write contract and never by an observation.**
    :func:`careloop.graph.recovery.may_replay` reads a declared class and the run's observers, and
    it is consulted *before* anything is read. A negative read is not a licence: ``ABSENT`` says the
    provider does not hold the effect now, in the world this client addressed, which is a different
    statement from "the write never landed and may be repeated".

    **What a replay is.** The same tool, the same arguments, the same key, through the same gated
    toolset and the same ``BeforeToolCall`` hook the first attempt went through — so the policy
    engine judges it again, with identical inputs, and reaches the identical verdict. Against a
    provider that keys writes durably this is a lookup that also repairs: if the original landed it
    returns the original, and if it did not it makes the booking that should have existed. Either
    way exactly one external effect exists afterwards, and that guarantee is the *provider's*.

    **The case is ``EXECUTING`` while that happens, and that is not incidental.** The shipped rules
    permit an external effect from ``PLANNING``, ``READY_TO_EXECUTE`` and ``EXECUTING`` and refuse
    it everywhere else, so a replay issued from ``WAITING_EXTERNAL`` would be denied. Re-claiming
    the attempt first is what lets the whole recovery run without widening a single rule.

    **Three ways of not knowing, and all of them stop.** A provider that answered and declined, an
    action type that may never be re-attempted, and a run with no channel to the provider all park
    the case for a person with ``EXTERNAL_OUTCOME_NOT_ESTABLISHED`` and **no** ``Approval``. A
    provider that did not answer at all is the one case where waiting is the correct behaviour, and
    it is the only producer of ``WAITING_EXTERNAL``.

    **It claims an effect only when one really was dispatched in this trace.** A replay that returns
    is a genuine ``POST``, so it claims ``PERFORMED`` on the line after the call, exactly as
    ``execute`` does and for the same reason. Every other branch claims **nothing**: CareLoop does
    not know what became of the effect, and ``SUPPRESSED`` and ``NOT_IN_THIS_TRACE`` are both
    positive statements it is in no position to make.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version

        # **Somebody else may have finished this between the edge and here, and that is not a
        # fault.** The dispatch edge selected this case while it was mid-effect; a second worker —
        # another process's startup sweep, an operator running a recovery by hand — can settle it in
        # the gap before this line reads the store. What it leaves behind is a case that has moved
        # on, which is the correct outcome rather than an inconsistency, and the guard below would
        # otherwise read "no unsettled preparation" as one.
        #
        # Reproduced before this branch existed: worker A reconciles to ``VERIFYING`` and worker B,
        # arriving a microsecond later, re-reads the case fresh, finds the preparation settled, and
        # commits ``VERIFYING -> FAILED`` — *successfully*, because reading fresh is exactly what
        # stops the compare-and-set from catching it. A correctly progressing case terminated by the
        # worker that had nothing left to do.
        #
        # Writing nothing is the whole response. The run ends here, ``run_case`` sees a case at a
        # state with no satisfied outgoing edge, and the winner carries on undisturbed.
        if case.state not in (CaseState.EXECUTING, CaseState.WAITING_EXTERNAL):
            return f"reconcile: case is {case.state}; another run settled it"

        prepared = unsettled_preparation(case)
        if prepared is None:
            return self._fail(
                case,
                base,
                "reconciliation found no prepared action for a case that was mid-effect",
            )

        proposal = latest_proposal(case)
        if proposal is None:
            return self._fail(case, base, "reconciliation was reached with no proposal")

        derived = idempotency_key(self.context.case_id, proposal.tool_name, proposal.args)
        if derived != prepared.idempotency_key or proposal.tool_name != prepared.tool_name:
            return self._fail(
                case,
                base,
                "the prepared action and the current proposal are different actions",
            )

        # **The frozen digest, checked beside the frozen key, and it is not the same check.** The
        # key covers the arguments the registry declares *identifying*; the digest covers all of
        # them. Since ``book_appointment.reason`` identifies nothing, a proposal whose descriptive
        # text had drifted would satisfy the clause above and then be re-issued under the recorded
        # key with different details — which Demo World answers ``409 KeyReused``, an external
        # service's own consistency rule standing where CareLoop's should.
        #
        # Unreachable under the shipped flow: a second ``PLAN_PROPOSED`` needs ``PLANNING``, and no
        # post-effect state reaches it. That is a property of today's writers rather than a
        # boundary, which is exactly the shape this repository has closed before, so it is a guard
        # against a persisted or tampered case and against future drift. ``Preparation`` has carried
        # ``args_digest`` since it was written; nothing read it until now.
        if args_digest(proposal.args) != prepared.args_digest:
            return self._fail(
                case,
                base,
                "the prepared action and the current proposal carry different arguments",
            )

        # Structurally unreachable while ``_record_action`` and the transition to ``VERIFYING``
        # share one commit, which they do. Kept because "unreachable" is a property of today's
        # writers, and this is what converges a case whose result was recorded without its
        # transition landing.
        if case.action_for(prepared.idempotency_key) is not None:
            case = transition(
                case,
                CaseState.VERIFYING,
                reason=f"{prepared.tool_name} was already recorded",
                actor="system",
                now=self.context.now(),
            )
            self.context.commit(case, expected_version=base)
            return f"reconcile: {prepared.tool_name} was already recorded"

        if not may_replay(prepared.tool_name, observers=self.context.readbacks):
            return self._park(case, base, prepared, "no safe way to establish the outcome")

        if case.state is CaseState.WAITING_EXTERNAL:
            case = transition(
                case,
                CaseState.EXECUTING,
                reason=f"re-claiming the prepared {prepared.tool_name}",
                actor="system",
                now=self.context.now(),
            )
            self.context.commit(case, expected_version=base)
            base = case.version

        cancelled = await fire_before_tool_call(
            self.context.hooks,
            proposal.tool_name,
            proposal.args,
            tool_use_id=tool_use_id(prepared.idempotency_key),
        )
        if cancelled is not None:
            return self._park(case, base, prepared, "the BeforeToolCall hook cancelled the replay")

        try:
            result = dispatch_prepared(
                lambda: self.context.tools.call(proposal.tool_name, proposal.args)
            )
        except PolicyDenied:
            # Not a fault, and emphatically not proof that nothing happened: an effect may already
            # exist. A refusal here means the replay may not be issued, which leaves the outcome
            # exactly as unknown as it was.
            return self._park(case, base, prepared, "the gated tool refused the replay")
        except DispatchUnavailable:
            # Nobody answered, so the outcome is exactly as unknown as it was before this run. The
            # same fork the execute node reaches when a first attempt's response is lost, answered
            # by the same function — there is one place that decides what an unestablished outcome
            # does, and neither node is it.
            return _stop_with_the_outcome_unestablished(
                self, case, base, proposal, prepared.idempotency_key
            )
        except DispatchRefused:
            # The provider answered and declined. A definite answer that establishes nothing about
            # whether the original write landed — and the sharpest instance, a key already recorded
            # against different arguments, is an identity contradiction no retry can resolve.
            return self._park(case, base, prepared, "the external system refused the replay")

        # A real request left this process and returned, which is the fact being claimed, on the
        # first line at which it is known and before the record and the commit — exactly as the
        # execute node does, and for the same reason.
        witness_effect(
            self.context.witness,
            action_type=proposal.tool_name,
            dispatch=EffectDispatch.PERFORMED,
            args=proposal.args,
        )
        self.context.effect_claimed = True

        case = _record_action(case, proposal, prepared.idempotency_key, result, self.context.now())
        case = transition(
            case,
            CaseState.VERIFYING,
            reason=f"{prepared.tool_name} reconciled under its prepared identity",
            actor="system",
            now=self.context.now(),
        )
        self.context.commit(case, expected_version=base)
        return f"reconcile: {prepared.tool_name} reconciled"

    def _fail(self, case: Case, base: int, reason: str) -> str:
        case = transition(
            case, CaseState.FAILED, reason=reason, actor="system", now=self.context.now()
        )
        self.context.commit(case, expected_version=base)
        return f"reconcile: failed, {reason}"

    def _park(self, case: Case, base: int, prepared: Preparation, detail: str) -> str:
        """Hand the case to a person with the outcome still unknown."""
        case = _park_with_nothing_established(case, prepared, detail, self.context.now())
        self.context.commit(case, expected_version=base)
        return f"reconcile: {prepared.tool_name} could not be established"


class VerifyNode(DeterministicNode):
    """Check the effect against what was authorised. ``VERIFYING`` → ``DONE`` or ``AWAITING_HUMAN``.

    The only path to ``DONE``. See :mod:`careloop.graph.verify` for what "verified" means and,
    just as importantly, what it does not claim.

    **Where the run is bound to a real external system, the check reads it back.** The read is a
    separate request rather than the effect's own response, and a system that cannot be reached or
    does not hold the effect fails verification rather than crashing the run — an effect the world
    cannot show CareLoop is an effect CareLoop cannot claim happened.

    **A failure parks the case for a person; it does not return it to the planner.** It used to, and
    the difference is the whole of this node's lifetime contribution. Every entry into ``VERIFYING``
    is post-effect, so a verification failure is never "that did not work, try again" — it is
    "something was carried out and cannot be certified", and the planner is the wrong thing to hand
    that to. See :func:`_park_with_nothing_certifiable` for what it cost while it was.

    **This node runs for a caregiver's re-check too, and it is the same code either way.** A
    re-check re-enters ``VERIFYING`` from ``AWAITING_HUMAN`` through
    :func:`careloop.graph.runner.request_recheck`, and everything below then does exactly what it
    does on a first pass: the same target derivation, the same read-back, the same comparison, the
    same two outcomes. Nothing is handed a target and nothing takes a different path — which is why
    a re-check cannot certify anything a first pass could not. Two things differ, and only two: a
    re-check that has **no independent observer bound** fails closed rather than falling back to the
    closed-loop checks (see :meth:`_nothing_was_observed`), and a re-check is the one entry into
    this node that must also account for the effect itself, because the execute node is not on that
    run's path.

    **What this node claims, and from where.** After the outcome is committed it records what the
    independent read established, the identifier it was keyed on, the state the world returned, and
    the settlement that follows — every one of them read from a decision already made. On a
    re-check it additionally claims ``NOT_IN_THIS_TRACE`` for the effect, projected from
    ``proposal.args`` through the same function ``execute`` uses. **Not** from the recorded result
    and **not** from what the clinic just returned: the two joint-state digests are evidence only
    while they have independent sources, and a re-check that derived the authorised state from the
    observed one would be comparing a value against itself.
    """

    async def run(self) -> str:
        case = self.context.current_case()
        base = case.version
        # Read once, before anything moves, and threaded down rather than re-derived. It answers
        # two questions this node asks — whether a re-check that read nothing may certify, and
        # whether this run is the one that has to account for the effect at all — and two
        # derivations of one fact is the shape of defect this repository has closed three times.
        entered_from = entered_verifying_from(case)

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
        verification, observed = self._verify(proposal, record, result, entered_from=entered_from)

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
                    "observation": str(verification.outcome),
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
                    "observation": str(verification.outcome),
                },
                now=self.context.now(),
            )
            case = _park_with_nothing_certifiable(case, proposal, key, self.context.now())

        self.context.commit(case, expected_version=base)

        # **After the commit**, because settlement is the one claim whose fact *is* the durable
        # conclusion. DONE or AWAITING_HUMAN has been written before telemetry is allowed to say
        # VERIFIED or NOT_CERTIFIED, so the witness observes a decision rather than anticipating
        # one.
        if not self.context.effect_claimed:
            # **This run reached verification without anything having accounted for the effect**, so
            # the write belongs to an earlier trace and this is the only run that can say so. Two
            # shapes arrive here and they are the same fact: a caregiver's re-check, which never
            # reaches the execute node, and a verification resumed after a process died between the
            # ``VERIFYING`` commit and the read — the crash window that is *structurally* the
            # narrowest in the whole lifecycle and, until this condition was widened, the one that
            # produced a correct trace the settlement evaluator refused.
            #
            # **Asked of the run rather than of the case, because the question is about the trace.**
            # This tested ``entered_from is AWAITING_HUMAN``, which is a durable fact standing in
            # for a run-scoped one: a case that entered ``VERIFYING`` from ``EXECUTING`` looks
            # identical whether the execute node ran a moment ago or in a process that is gone, so
            # the proxy was right for the re-check and silently wrong for the resume. Measured
            # against the frozen evaluator before the change: the resumed run reached ``DONE``
            # correctly, emitted five attributes instead of nine, and was judged ``FAIL``.
            #
            # The effect it accounts for is the one that was *authorised*, which is why this
            # re-projects ``proposal.args`` through exactly the projection the execute node uses.
            # Deriving it from the record, from what the clinic just returned, or from the
            # observation claim would make the two joint-state digests share a source, and their
            # equality is the whole evidence.
            witness_effect(
                self.context.witness,
                action_type=proposal.tool_name,
                dispatch=EffectDispatch.NOT_IN_THIS_TRACE,
                args=proposal.args,
            )
            self.context.effect_claimed = True
        witness_observation(
            self.context.witness,
            action_type=record.action_type,
            verification=verification,
            result=result,
            observed=observed,
        )
        return f"verify: {'passed' if verification.passed else 'failed'}"

    def _verify(
        self,
        proposal: Proposal,
        record: ActionRecord,
        result,
        *,
        entered_from: CaseState | None,
    ) -> tuple[Verification, ActionResult | None]:
        """Verify the recorded effect, reading the external system back where there is one.

        A read-back failure is a *verification* failure, not an exception out of the node. Every
        way of failing to certify still parks the case identically, and that is unchanged: "the
        clinic says something different" and "the clinic cannot show me the appointment at all" are
        both reasons not to call an effect verified, and turning the second into a crash would make
        an integration outage look like a CareLoop fault.

        **What the two no longer share is a description.** The failure's *type* names the evidence
        class — an absence the clinic asserted, or a read that established nothing — and it is
        carried onto the ``Verification`` and from there to the timeline. Read from the type and
        never from the message, so a clinic whose error text claims the observation matched changes
        nothing: see ``tests/test_observation_outcome.py::TestTheOutcomeIsNotReadOutOfText``.

        **A re-check that reads nothing certifies nothing**, and that branch is first. See
        :meth:`_nothing_was_observed` for the hazard it closes and why it is scoped to this one
        episode rather than to ``NOT_OBSERVED`` everywhere.

        **The external record is returned beside the verdict**, rather than being read and dropped.
        It is the one object in the run that carries what the *world* holds, and something outside
        this process cannot check CareLoop's claim without it. Returned rather than re-fetched: a
        second read would be a second observation, and this node performs exactly one. ``None``
        whenever no observation was obtained — a run with no observer bound, and a read that was
        attempted and failed — because a state nobody saw is not a state to report.
        """
        readback = self.context.readbacks.get(record.action_type)
        if readback is None and entered_from is CaseState.AWAITING_HUMAN:
            return self._nothing_was_observed(record), None

        observed = None
        if readback is not None:
            try:
                observed = readback(result)
            except ReadBackFailed as unreadable:
                return (
                    Verification(
                        passed=False,
                        diagnostic=str(unreadable),
                        externally_observed=True,
                        outcome=unreadable.outcome,
                    ),
                    None,
                )

        return (
            verify_action(
                case_id=self.context.case_id,
                action_type=proposal.tool_name,
                args=proposal.args,
                record=record,
                result=result,
                observed=observed,
            ),
            observed,
        )

    def _nothing_was_observed(self, record: ActionRecord) -> Verification:
        """A caregiver asked for another look and this run had nothing to look with.

        **The hazard, measured before this guard existed.** Park a booking the clinic diverged on,
        re-check it in a run with no clinic bound, and the case reaches ``DONE`` reporting
        ``read_back=false`` and ``observation=NOT_OBSERVED`` while the external record is still
        wrong. Nothing is broken in the closed-loop checks and that is exactly the problem: the
        booking *response* was always correct, only the world diverged, so removing the observer
        does not make verification fail — it makes it stop looking. The case would be certified on
        strictly less evidence than the one that already refused it.

        The rule this enforces, stated so it reads as a property rather than a special case:

            a verification may not certify a case whose own trail already records that an
            independent read of this action was attempted and did not confirm it, unless an
            independent read is performed now.

        Certifying without one would be certifying by *removing* evidence, which is the one
        direction a system built on an audit trail must not move.

        **Scoped to the re-check episode, deliberately.** ``NOT_OBSERVED`` is not redefined as
        failure globally: three of the four effect tools have no external system at all, and a
        first-pass simulated run still verifies closed-loop and still reaches ``DONE`` saying
        plainly that it read nothing. ``tests/test_recheck.py::TestTheGuardIsScopedToReChecks`` is
        the control that keeps it that way.

        The reported outcome is literally true on both fields. Nothing was read, so
        ``externally_observed`` is ``False`` — it means *attempted*, and no attempt was possible —
        and :attr:`~careloop.graph.verify.ObservationOutcome.NOT_OBSERVED` means no independent
        observer took part, which is a fact about CareLoop's own wiring rather than a claim about
        the world. Reporting ``UNAVAILABLE`` here would assert an external system was consulted and
        did not answer, by a run that never touched one.

        **This decides nothing about lifecycle**, and that is why it returns rather than
        transitions: :meth:`run` remains the single writer, so a re-check with no observer parks
        through the same ``VERIFICATION_FAILED`` → ``MANUAL_HANDOFF_REQUIRED`` → ``AWAITING_HUMAN``
        path as every other way of failing to certify. No new state and no new reason code —
        ``EXTERNAL_STATE_NOT_CONFIRMED`` already covers it exactly, because what CareLoop knows is
        once again that it cannot confirm the result.
        """
        return Verification(
            passed=False,
            externally_observed=False,
            outcome=ObservationOutcome.NOT_OBSERVED,
            diagnostic=(
                f"a re-check of {record.action_type} was requested, and this run has no "
                f"independent observer bound for it. Nothing was read from any external system, so "
                f"nothing about the recorded effect can be confirmed — and this case is already "
                f"stopped because an earlier independent read failed to confirm it. Certifying it "
                f"now would mean certifying on less evidence than was already refused."
            ),
        )


# -- shared writers --------------------------------------------------------------------------


def _record_decision(
    case: Case, decision: Decision, kind: TimelineKind, now, fingerprint: str
) -> Case:
    """Write one policy verdict to the timeline, diagnostics and all.

    The timeline is where the detail belongs: the matched rule, every argument's class, the trust
    each value resolved to, and the raw arguments. What the planner is later told is rebuilt from
    ``payload["reason_code"]`` alone.

    ``args_digest`` and ``policy_fingerprint`` are recorded so a refusal has a durable identity the
    liveness rule can recognise later — see :func:`careloop.graph.context.is_repeat_refusal`. Both
    are digests of things the entry already carries or the case already holds, so neither adds a
    value to the trail that was not there; recording them means the rule reads the audit trail
    rather than keeping a second, parallel memory of what was refused.
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
            "args_digest": args_digest(decision.args),
            "policy_fingerprint": fingerprint,
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


#: How many refusals one unchanged case may collect before CareLoop stops and asks a person.
#:
#: Sized against the node budget rather than against the registry, which is the whole point of the
#: change. One replan costs two node executions (``plan`` then ``gate``); a run spends three
#: reaching the first one (``dispatch``, ``triage``, ``evidence``). Six refusals therefore cost
#: ``3 + 2 * 6 = 15`` of :data:`careloop.graph.builder.MAX_NODE_EXECUTIONS`, leaving room for the
#: park itself and for a case that arrives mid-flight — so this bound is always reached first and
#: the budget stays what it is meant to be, a runaway detector.
#: ``tests/test_liveness.py`` asserts that arithmetic rather than trusting this comment.
MAX_REFUSALS_PER_FINGERPRINT: Final[int] = 6


class NothingLeftToTry(BaseModel):
    """Why CareLoop stopped replanning. Timeline-only; never shown to a model."""

    model_config = ConfigDict(frozen=True)

    summary: str
    diagnostic: str


def _nothing_left_to_try(
    case: Case, fingerprint: str, *, repeated: bool
) -> NothingLeftToTry | None:
    """Whether replanning can still achieve anything, and if not, why not.

    **Three sufficient conditions, none of them necessary, and the order is the order of strength.**

    ``repeated`` — the planner has proposed a call already refused under identical policy inputs.
    Provably futile: :meth:`careloop.policy.engine.PolicyEngine.evaluate` is pure, so the verdict
    cannot come out differently. This is the condition the old rule was missing, and the one the
    safety gate caught: measured against the deployed model, an injection-only case had the planner
    repeat one refused tool seven times.

    ``refusals_at(...) >= MAX_REFUSALS_PER_FINGERPRINT`` — the planner keeps producing *new*
    proposals and every one is refused. Nothing is provably futile here, so this is a bound rather
    than a deduction, and it is the reason liveness no longer depends on the planner eventually
    naming a particular tool. Scoped to the fingerprint, so it resets on any material change.

    ``_every_tool_has_been_refused`` — kept, because it remains true and remains a good reason to
    stop; it is simply no longer *load-bearing*. It used to be the only condition, and it silently
    assumed the planner would enumerate the whole registry. It does not: in ten real-graph runs the
    deployed model proposed four distinct tools and never ``book_appointment``, so this condition
    was unsatisfiable, the case burned all 24 node executions and ended ``FAILED`` — a defence
    reported as a fault.

    Returns ``None`` when replanning may continue, which is the ordinary case: a denied proposal
    goes back to the planner and the run continues.
    """
    if repeated:
        return NothingLeftToTry(
            summary="the planner repeated a refused proposal",
            diagnostic=(
                "the planner reproposed a call already refused against identical policy inputs "
                "— same tool, same canonical arguments, same reason, same case state, evidence "
                "and approvals. Policy evaluation is pure, so the verdict cannot change and "
                "replanning again would only spend the node budget. Nothing is blacklisted: this "
                "exact call becomes proposable again the moment the case materially changes."
            ),
        )

    refusals = refusals_at(case, fingerprint)
    if refusals >= MAX_REFUSALS_PER_FINGERPRINT:
        return NothingLeftToTry(
            summary=f"{refusals} proposals were refused without the case changing",
            diagnostic=(
                f"{refusals} proposals have now been refused against unchanged policy inputs, "
                f"reaching MAX_REFUSALS_PER_FINGERPRINT={MAX_REFUSALS_PER_FINGERPRINT}. The "
                f"planner is still producing new proposals, so none is provably futile, but "
                f"nothing about the case has moved and a person is better placed than another "
                f"replan. The count is scoped to these exact policy inputs and resets on any "
                f"material change."
            ),
        )

    if _every_tool_has_been_refused(case):
        return NothingLeftToTry(
            summary="every action was refused",
            diagnostic=(
                f"every tool in the registry has been proposed and refused: "
                f"{sorted(REGISTRY.names)}. Kept as a sufficient condition but no longer the only "
                f"one — it assumed the planner would eventually name every tool, and measured "
                f"against a real model it does not."
            ),
        )
    return None


def _every_tool_has_been_refused(case: Case) -> bool:
    """Whether the planner has now proposed every tool there is and been refused each time.

    **No longer the liveness condition** — one of three sufficient conditions in
    :func:`_nothing_left_to_try`, and the weakest. It is kept because it is a true and readable
    statement about a case, and because the scenario it describes really happens; it is no longer
    relied upon, because a planner that stops short of the full catalogue never satisfies it.

    Refusals are counted by tool name rather than by arguments. A tool refused with one set of
    arguments might be allowed with another, so this is the conservative reading: the planner has
    tried everything the catalogue offers *at least once* and has nothing new to reach for.
    """
    refused = {
        event.payload.get("tool_name", "")
        for event in case.timeline
        if event.kind is TimelineKind.POLICY_DENY
    }
    return REGISTRY.names <= refused


def _park_with_nothing_safe_to_do(case: Case, now, reason: NothingLeftToTry) -> Case:
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
            f"{reason.diagnostic} Parked rather than failed — the refusals are the system "
            f"working, and FAILED means something broke. No approval is raised: there is no "
            f"proposal to authorise, and a record that authorises nothing must not be decidable."
        ),
        payload={"reason_code": str(ReasonCode.NO_SAFE_ACTION_AVAILABLE)},
        now=now,
    )
    return transition(
        case,
        CaseState.AWAITING_HUMAN,
        reason=reason.summary,
        actor="policy",
        now=now,
    )


def _park_with_no_patient_authority(case: Case, now) -> Case:
    """Stop because this run may not act for the people the case is about, and hand it to a person.

    ``PLANNING`` → ``AWAITING_HUMAN``. **No approval is raised**, for the reason its three siblings
    raise none: an :class:`~careloop.state.case.Approval` is a capability for one exact call, and
    what is wrong here is not the call. Every argument on the proposal is fine and policy would
    very likely have allowed it; what a caregiver would be being asked is *"may I act for this
    person?"*, which is not a question a button on a proposal can answer and not one a human
    pressing approve should be able to answer for themselves.

    **A sibling rather than a branch**, again for the reason the other three are siblings. Every
    tool was refused; an action was carried out and cannot be confirmed; no action could be written
    down; and now: this case is not one this run may act on. Four different facts, told apart by
    their reason code, which is what a reason code is for.

    **The diagnostic names no caregiver, no grant and no mechanism.** This node cannot see one --
    :mod:`careloop.graph.authority` speaks only of patients, on purpose -- and the withheld
    diagnostic is the wrong place to start describing an identity system even if it could. What the
    trail records is that the boundary refused and which patients it was asked about; who was asking
    is the API layer's fact and lives in its own audit entries.
    """
    established = sorted(established_patients(case))
    case = append_event(
        case,
        kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
        actor="policy",
        summary="this case is not one CareLoop may currently act on; manual follow-up is required",
        diagnostic=(
            f"the run's patient authority was asked about "
            f"{established or 'no established patient'} and refused. Nothing was prepared, "
            f"nothing was dispatched and no external system was reached: this is asked before "
            f"the durable intent is written, so there is no unsettled attempt for a recovery to "
            f"find. Parked rather than failed -- nothing broke, and a person deciding whether "
            f"this case should be worked on at all is exactly the next step. No approval is "
            f"raised: an approval authorises one exact call, and the call is not what was refused."
        ),
        payload={"reason_code": str(ReasonCode.PATIENT_AUTHORITY_DENIED)},
        now=now,
    )
    return transition(
        case,
        CaseState.AWAITING_HUMAN,
        reason="this case is not one this run may act on",
        actor="policy",
        now=now,
    )


def _park_with_nothing_proposable(case: Case, failure: Exception, now) -> Case:
    """Stop because no next action could be formed, and hand the case to a person.

    ``PLANNING`` → ``AWAITING_HUMAN``. **No approval is raised**, for the reason the two parks
    below and above raise none and then one more of its own: there is not merely no proposal worth
    authorising, there is **no proposal at all**. Nothing was written to the timeline, nothing was
    evaluated, and on a case that had already been refused once ``latest_proposal`` still names the
    call policy *denied* — which is precisely the record an approval here would release. That is
    not hypothetical: it is the defect :func:`_park_with_nothing_safe_to_do` documents, one node
    earlier.

    **A sibling rather than a branch, for the reason the other two are siblings.** The three mean
    different things — every action was refused; an action was carried out and cannot be
    confirmed; no action could be written down — and they are told apart by their reason code,
    which is what a reason code is for. Folding them into one writer would buy a diagnostic that is
    false for whichever case it was not written about.

    **Nothing the model produced survives.** The arguments that failed validation are discarded
    inside :func:`careloop.agents.invoke._one_proposal_attempt`; only the *type* of the failure is
    recorded here, and only in the diagnostic the API withholds. That is
    :mod:`careloop.agents.invoke`'s own discipline — its two log lines carry the exception type and
    never its message — and it is kept by construction rather than by everyone remembering: a
    ``ValidationError`` renders ``input_value``, and that input is model output derived from
    document content.

    **The reason code is the product-facing truth and the exception type is not.**
    ``NO_VALID_PROPOSAL_PRODUCED`` is what a caregiver, a projection and a lifecycle read;
    ``MalformedProposal`` is an implementation name and stays in the logs and in this diagnostic.
    So it is deliberately absent from the payload, which is the allowlisted half a response can
    carry.
    """
    case = append_event(
        case,
        kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
        actor="planner",
        summary="no valid next action could be formed; manual follow-up is required",
        diagnostic=(
            f"the planner was asked for one next action and produced nothing that satisfies an "
            f"offered proposal schema. careloop.agents.invoke.proposal_tool_call had already "
            f"spent its one retry — MAX_PROPOSAL_ATTEMPTS attempts against the identical schemas "
            f"and the identical prompts — so this is exhaustion rather than a first failure, and "
            f"asking again is what has just been tried. The last attempt failed with "
            f"{type(failure).__name__}; the arguments it carried were discarded rather than "
            f"recorded, so nothing model-authored is on this case from it. Parked rather than "
            f"failed: nothing broke, nothing was proposed, nothing was evaluated and nothing was "
            f"done. No approval is raised — there is no call to authorise, and the newest proposal "
            f"this case carries, if it carries one at all, is one policy has already refused."
        ),
        payload={"reason_code": str(ReasonCode.NO_VALID_PROPOSAL_PRODUCED)},
        now=now,
    )
    return transition(
        case,
        CaseState.AWAITING_HUMAN,
        reason="no valid next action could be formed",
        actor="planner",
        now=now,
    )


def _park_with_nothing_certifiable(case: Case, proposal: Proposal, key: str, now) -> Case:
    """Stop after an effect that cannot be certified, and hand the case to a person.

    ``VERIFYING`` → ``AWAITING_HUMAN``. **No approval is raised**, for the reason
    :func:`_park_with_nothing_safe_to_do` raises none and then one further reason of its own: not
    only is there no proposal to authorise, there is nothing a decision *could* do. The effect
    already happened. No answer a caregiver posts changes what the clinic holds, and a card offering
    Approve would be inviting somebody to authorise the past.

    **A sibling rather than a branch, because the two handoffs mean opposite things.** That one says
    every available action was refused and nothing was done; this one says something *was* done and
    CareLoop cannot confirm the result. They share a state and a timeline kind because both are "a
    person is needed now", and they are told apart by their reason code — which is what a reason
    code is for. Folding them into one writer would save a few lines and buy a diagnostic that is
    false for whichever case it was not written about.

    **Why this is not a replan, measured rather than argued.** The failure branch used to transition
    to ``PLANNING`` and the cost had two shapes. An identical reproposal was suppressed by
    :meth:`careloop.state.case.Case.action_for`, re-verified against the same divergent state and
    failed again — four cycles, then the SDK's node bound stopped the run and the case ended
    ``FAILED`` because the *framework* ran out of budget, not because CareLoop decided anything. And
    a reproposal differing in one argument was worse: ``book_appointment.reason`` is
    planner-authored free text carrying no ``trust_key`` and named by no rule, so changing it moved
    the argument digest, moved the idempotency key, and defeated duplicate suppression in CareLoop
    *and* in Demo World. That reached ``ALLOW`` and dispatched. Against a clinic that could not
    confirm the first booking it produced a second real one; against a clinic that diverged, the
    second ``POST`` left CareLoop and only the clinic's own slot-collision rule refused it — an
    external service's consistency check standing where an authority decision should have been.

    **Every failure condition lands here, deliberately, and they are no longer indistinguishable
    while doing so.** :class:`careloop.graph.verify.ObservationOutcome` now tells the three apart —
    the read succeeded and disagreed, the external system answered that it holds nothing, the read
    established nothing at all — and the ``VERIFICATION_FAILED`` entry above records which. That
    changed what the trail *says* and deliberately not what the case *does*: retrying the effect is
    wrong for all three, and stopping is the only lifecycle correct for all three today. Whether
    any of them should later be recovered differently is a question the vocabulary makes askable,
    and this function is not where it is answered.

    The ``VERIFICATION_FAILED`` entry is written by the caller and stays: what happened, why it
    could not be certified, and what CareLoop decided are three separate facts and the trail keeps
    all three. The proposal is named here so the diagnostic says which action is unconfirmed, and
    its arguments are deliberately absent — they are already on the ``PLAN_PROPOSED`` entry, and the
    values read back from the outside world stay in the verification diagnostic the API withholds.

    **``key`` is the durable identity of the action that could not be certified**, and it is passed
    in rather than re-derived. :meth:`VerifyNode.run` computed it to *find* the ``ActionRecord`` it
    just verified, so threading it through is what makes "the action this handoff is about" and "the
    action that was verified" the same object by construction instead of by a second derivation that
    could drift.

    Recorded because a tool name is not an identity. A case may carry more than one ``ActionRecord``
    — a reopened case does routinely — so ``{tool_name, reason_code}`` said only *"a booking on this
    case could not be confirmed"*, which is enough for a caregiver's card and not enough for
    anything to act on. The alternatives were each rejected for a reason this repository has already
    paid for: ``args_digest`` covers argument names and canonical values and neither the case nor
    the tool; ``confirmation_id`` is external, lives inside the result JSON and identifies the
    clinic's object rather than CareLoop's action; and "the latest record" is the class of accident
    that produced three separate identity defects here.

    **Nothing consumes it yet, and that is the whole of what this patch claims.** It makes a parked
    case bound to one action rather than to a tool name. Whether anything may later act on that
    binding is a different question with a different answer, and this writes down the fact rather
    than granting the capability.
    """
    case = append_event(
        case,
        kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
        actor="system",
        summary=f"{proposal.tool_name} was carried out but could not be confirmed",
        diagnostic=(
            f"{proposal.tool_name} was performed for this case under idempotency key {key} and "
            f"independent verification could not confirm the result — see the VERIFICATION_FAILED "
            f"entry above for what disagreed or could not be read. Parked rather than replanned: "
            f"the effect has already happened, so proposing anything else would be planning on top "
            f"of an outcome nobody has established. No approval is raised — there is no call to "
            f"authorise, and no decision could change what the external system holds."
        ),
        payload={
            "tool_name": proposal.tool_name,
            "reason_code": str(ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED),
            "idempotency_key": key,
        },
        now=now,
    )
    return transition(
        case,
        CaseState.AWAITING_HUMAN,
        reason=f"{proposal.tool_name} could not be confirmed and needs a person",
        actor="system",
        now=now,
    )


def _stop_with_the_outcome_unestablished(
    node: DeterministicNode, case: Case, base: int, proposal: Proposal, key: str
) -> str:
    """Stop a case whose dispatch established nothing, in whichever way is honest for the action.

    **One question, asked in two places.** ``ExecuteNode`` reaches it when a first attempt's
    response never came back; ``ReconcileNode`` reaches the same fork after a crash. The situation
    is identical — an action was durably prepared, something may or may not have happened, and
    nobody can say which — so the answer is identical, and it lives here rather than being written
    twice with a chance to drift.

    Two outcomes, and which applies is a fact about the **provider**:

    ``WAITING_EXTERNAL``
        The action's declared recovery class permits re-issuing the frozen identity and this run has
        an independent observer for it. CareLoop can settle this itself once something answers, so
        waiting is true rather than a euphemism, and the case is picked up by the next run —
        ``careloop.api.deps.ApiDeps.resume_unsettled`` sweeps exactly this state at startup.

    ``AWAITING_HUMAN``
        It does not, or this run has no observer. CareLoop will never resolve it alone, so a state
        that implied it was waiting would be a lie about who the case is waiting for. It parks with
        ``EXTERNAL_OUTCOME_NOT_ESTABLISHED`` and **no** ``Approval``: the action was already
        authorised, and what is missing is knowledge rather than permission.

    ``key`` is the identity already frozen on the case's ``ACTION_PREPARED`` entry, threaded in from
    the caller that derived it, so the handoff names the same action the preparation does by
    construction rather than by a second derivation.
    """
    prepared = Preparation(
        tool_name=proposal.tool_name, idempotency_key=key, args_digest=args_digest(proposal.args)
    )
    if may_replay(proposal.tool_name, observers=node.context.readbacks):
        case = transition(
            case,
            CaseState.WAITING_EXTERNAL,
            reason=f"the external system did not answer about {proposal.tool_name}",
            actor="system",
            now=node.context.now(),
        )
        node.context.commit(case, expected_version=base)
        return f"{node.id}: {proposal.tool_name} could not be reached"

    case = _park_with_nothing_established(
        case, prepared, "no safe way to establish the outcome", node.context.now()
    )
    node.context.commit(case, expected_version=base)
    return f"{node.node_id}: {proposal.tool_name} could not be established"


def _park_with_nothing_established(case: Case, prepared: Preparation, detail: str, now) -> Case:
    """Stop with the outcome of a dispatched action unknown, and hand the case to a person.

    ``EXECUTING`` → ``AWAITING_HUMAN``. **No approval is raised**, for the reason the two sibling
    parkings raise none and then one of its own: there is nothing a decision could authorise,
    because the action was already authorised and already dispatched. What is missing is not
    permission but knowledge, and no answer a caregiver posts supplies it.

    **The third handoff, and it means a third thing.** ``NO_SAFE_ACTION_AVAILABLE`` says every
    available action was refused and nothing was done. ``EXTERNAL_STATE_NOT_CONFIRMED`` says
    something *was* done, its result was recorded, and an independent read could not confirm it.
    This says something *may* have been done and CareLoop cannot find out — it holds no result at
    all. The three share a state and a timeline kind because all three mean "a person is needed
    now", and they are told apart by their reason code, which is what a reason code is for.

    Reusing ``EXTERNAL_STATE_NOT_CONFIRMED`` would send a caregiver on the wrong errand. Its text
    says "an action already carried out", and here whether it was carried out is the open question:
    the instruction that follows from this is *check whether an appointment exists*, not *check
    whether the appointment is right*.

    **Not replanned, for the reason a failed verification is not replanned**, and with less slack:
    there, at least, an ``ActionRecord`` existed and duplicate suppression could catch an identical
    reproposal. Here there is none, so a reproposal would suppress nothing, and
    ``book_appointment.reason`` is free text that keys differently on a full stop. The state machine
    refuses ``EXECUTING → PLANNING`` outright, so this is a property of the machine rather than of
    this function remembering.

    **``prepared`` carries the identity**, which is why it is passed rather than re-derived: it is
    the same object :class:`ReconcileNode` proved agrees with the current proposal, so "the action
    this handoff is about" and "the action that could not be established" are one thing by
    construction. ``detail`` says which of the ways of not knowing occurred and stays in the
    diagnostic, which the API withholds — the payload carries the closed vocabulary only.
    """
    case = append_event(
        case,
        kind=TimelineKind.MANUAL_HANDOFF_REQUIRED,
        actor="system",
        summary=f"whether {prepared.tool_name} happened could not be established",
        diagnostic=(
            f"{prepared.tool_name} was prepared for this case under idempotency key "
            f"{prepared.idempotency_key} and dispatched, and reconciliation could not establish "
            f"what became of it: {detail}. CareLoop holds no result for this action and will not "
            f"guess — the effect may exist in the external system or may not, and re-attempting it "
            f"is not something an agent may decide. No approval is raised: the action was already "
            f"authorised, and what is missing is knowledge rather than permission."
        ),
        payload={
            "tool_name": prepared.tool_name,
            "reason_code": str(ReasonCode.EXTERNAL_OUTCOME_NOT_ESTABLISHED),
            "idempotency_key": prepared.idempotency_key,
        },
        now=now,
    )
    return transition(
        case,
        CaseState.AWAITING_HUMAN,
        reason=f"whether {prepared.tool_name} happened is not known and needs a person",
        actor="system",
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
