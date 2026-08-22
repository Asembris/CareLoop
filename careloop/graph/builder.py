"""Wiring the graph, and bounding it.

**The bounds come from the SDK.** ``set_max_node_executions``, ``set_execution_timeout`` and
``set_node_timeout``, per invariant 5 — there is no counter in CareLoop, and the replan cycle is a
real cycle in the graph rather than a loop in Python. When the bound is reached the SDK stops
scheduling nodes and returns a ``GraphResult`` with ``Status.FAILED``, raising nothing; turning
that into a ``FAILED`` case is the runner's job and it is a reaction to the SDK's verdict, not a
second opinion about it.

Verified by execution rather than by reading: a two-node cycle with ``set_max_node_executions(5)``
runs exactly five nodes and reports ``FAILED``.

**The shape.**

::

                    ┌──────────┐
                    │ dispatch │  entry, and the resume point
                    └────┬─────┘
         NEW  ┌──────────┼────────────┬─────────────────┐
              ▼          ▼            ▼                 ▼
          ┌────────┐ ┌──────────┐ ┌──────┐        ┌─────────┐
          │ triage │ │ evidence │ │ plan │        │ execute │
          └───┬────┘ └────┬─────┘ └──┬───┘        └────┬────┘
              └───────────┘       ▲  │                 │
                        DENY      │  ▼                 │
                     (replan)     │ ┌──────┐           │
                                  └─┤ gate ├───────────┘
                                    └──┬───┘  ALLOW
                                       │
                                       │ REQUIRE_HUMAN → no edge holds; the run ends parked
                                       ▼
                                  ┌─────────┐      ┌────────┐
                                  │ execute ├─────▶│ verify │
                                  └─────────┘      └────────┘
                                                    nothing leaves verify:
                                                    DONE           → the run ends
                                                    AWAITING_HUMAN → the run ends parked

``plan → gate → plan`` is the cycle the bound exists for, and it is now the only cycle in the graph.
``AWAITING_HUMAN`` and ``DONE`` end a run by having no satisfied outgoing edge, which is why neither
needs a terminal node — and ``plan`` reaches ``AWAITING_HUMAN`` too, when the planner's own bounded
retry produced no proposal to gate. That is the only outcome of ``plan`` that is not a proposal, and
it is why ``plan → gate`` carries a condition; see the edge itself.

**A case abandoned mid-effect resumes through ``reconcile``.** ``dispatch`` routes ``EXECUTING``
and ``WAITING_EXTERNAL`` there — a durably prepared action whose result nobody recorded — and that
node re-issues the frozen identity, or stops. Neither state had an outgoing edge before it existed,
so a case that crashed mid-effect ran ``("dispatch",)`` and sat where it was for good.

**There was a second cycle, ``verify → plan``, and it was a defect rather than a feature.** A failed
verification returned the case to the planner. Every entry into ``VERIFYING`` is post-effect, so
what that actually did was replan on top of an action already carried out: an identical reproposal
was suppressed and re-verified into the same failure until this file's ``MAX_NODE_EXECUTIONS``
stopped the run — making the bound the *domain* outcome rather than a runaway detector — and a
reproposal differing in one free-text argument keyed differently, defeated duplicate suppression and
executed a second real external effect. The node now parks the case for a person, and this file no
longer offers a way back.
"""

from __future__ import annotations

from typing import Final

from strands.multiagent import GraphBuilder
from strands.multiagent.graph import Graph

from careloop.graph.conditions import when_case_is
from careloop.graph.context import RunContext
from careloop.graph.nodes import (
    DispatchNode,
    EvidenceNode,
    ExecuteNode,
    GateNode,
    PlanNode,
    ReconcileNode,
    TriageNode,
    VerifyNode,
)
from careloop.state.case import CaseState

#: Node executions one run may use.
#:
#: A happy path is seven (dispatch, triage, evidence, plan, gate, execute, verify), a resume
#: after approval is three, and a crash recovery is three (dispatch, reconcile, verify). Each
#: refused proposal costs two more, so this leaves room for roughly eight replans before the
#: bound fires. Generous rather than tight: the bound is a runaway
#: detector, and a case that failed because a legitimate replan ran out of room would be a bug
#: reported as a policy outcome.
#:
#: **It is a runaway detector again.** While ``verify -> plan`` existed it was also the thing that
#: ended a failed verification: the case cycled plan/gate/execute/verify until this number ran out
#: and ``run_case`` marked it ``FAILED``, so a domain outcome was being decided by a budget. Nothing
#: reaches it on that path now — a verification failure parks the case in seven nodes — and the only
#: cycle left is the gate's replan, which is what this was sized for.
#:
#: **Reconciliation adds no cycle.** ``outgoing(reconcile)`` is the one edge to ``verify``; its
#: other three endings satisfy no condition and stop the run. A case left in ``WAITING_EXTERNAL``
#: is re-attempted by a *later run*, not by another lap of this one, so nothing here counts
#: retries and invariant 5 needs nothing new.
MAX_NODE_EXECUTIONS: Final[int] = 24

#: Wall-clock ceiling for one run, in seconds. Offline on the mock this is milliseconds of work;
#: against a real provider it bounds a stalled model call rather than a busy loop.
EXECUTION_TIMEOUT: Final[float] = 120.0

#: Ceiling for any single node. Lower than the run, so one wedged model call fails its node
#: instead of consuming the whole run's budget.
NODE_TIMEOUT: Final[float] = 45.0

DISPATCH: Final[str] = "dispatch"
TRIAGE: Final[str] = "triage"
EVIDENCE: Final[str] = "evidence"
PLAN: Final[str] = "plan"
GATE: Final[str] = "gate"
EXECUTE: Final[str] = "execute"
RECONCILE: Final[str] = "reconcile"
VERIFY: Final[str] = "verify"

#: Every node id, in the order a case meets them.
#:
#: ``reconcile`` sits between ``execute`` and ``verify`` because that is where a case meets it: it
#: is the recovery entry for an effect ``execute`` prepared and did not get to record, and what it
#: hands on is a result for ``verify`` to check. A happy path never touches it.
NODE_IDS: Final[tuple[str, ...]] = (
    DISPATCH,
    TRIAGE,
    EVIDENCE,
    PLAN,
    GATE,
    EXECUTE,
    RECONCILE,
    VERIFY,
)


def build_graph(
    context: RunContext,
    *,
    max_node_executions: int = MAX_NODE_EXECUTIONS,
    execution_timeout: float = EXECUTION_TIMEOUT,
    node_timeout: float = NODE_TIMEOUT,
) -> Graph:
    """Build the bounded case graph for one run context."""
    builder = GraphBuilder()

    builder.add_node(DispatchNode(DISPATCH, context), DISPATCH)
    builder.add_node(TriageNode(TRIAGE, context), TRIAGE)
    builder.add_node(EvidenceNode(EVIDENCE, context), EVIDENCE)
    builder.add_node(PlanNode(PLAN, context), PLAN)
    builder.add_node(GateNode(GATE, context), GATE)
    builder.add_node(ExecuteNode(EXECUTE, context), EXECUTE)
    builder.add_node(ReconcileNode(RECONCILE, context), RECONCILE)
    builder.add_node(VerifyNode(VERIFY, context), VERIFY)

    # Dispatch routes to wherever the case already is. This is what makes a case parked for a
    # caregiver decision — or reconstructed from the store in a later process — resume correctly
    # rather than starting again from triage.
    builder.add_edge(DISPATCH, TRIAGE, when_case_is(context, CaseState.NEW))
    builder.add_edge(DISPATCH, EVIDENCE, when_case_is(context, CaseState.GATHERING_EVIDENCE))
    builder.add_edge(DISPATCH, PLAN, when_case_is(context, CaseState.PLANNING))
    builder.add_edge(DISPATCH, EXECUTE, when_case_is(context, CaseState.READY_TO_EXECUTE))
    # This edge also carries a caregiver's re-check. careloop.graph.runner.request_recheck commits
    # AWAITING_HUMAN -> VERIFYING before a run is scheduled, so the resume routes here exactly as it
    # would for a case persisted mid-verification. That is why Patch 2C added no edge: re-entering
    # verification is already what dispatch does, and giving the re-check its own route — or a
    # verify self-edge — would have been a second way into the same node with its own conditions to
    # get wrong.
    builder.add_edge(DISPATCH, VERIFY, when_case_is(context, CaseState.VERIFYING))
    # **The recovery route, and the whole of it.** A case abandoned mid-effect is EXECUTING with a
    # durably prepared action and no recorded result; one whose reconciliation could not reach the
    # provider is WAITING_EXTERNAL with the same prepared action. Both mean "an authorised action
    # was dispatched and nobody knows what became of it", so both go to the same node — and neither
    # goes anywhere else. Before this edge existed, either state satisfied no outgoing condition
    # at all: the run ran ("dispatch",), reported COMPLETED, and left the case stuck for good.
    #
    # Pointedly **not** to `plan` and **not** to `execute`. To `plan` is the session-Q defect, which
    # TRANSITIONS now refuses outright. To `execute` would put a second route into the node that
    # dispatches effects, with its own conditions to get wrong, and the point of reconciliation is
    # that it consults a durable record *before* deciding whether a dispatch may happen at all.
    builder.add_edge(
        DISPATCH,
        RECONCILE,
        when_case_is(context, CaseState.EXECUTING, CaseState.WAITING_EXTERNAL),
    )

    builder.add_edge(TRIAGE, EVIDENCE, when_case_is(context, CaseState.GATHERING_EVIDENCE))
    builder.add_edge(EVIDENCE, PLAN, when_case_is(context, CaseState.PLANNING))

    # **Every proposal is gated, and the condition is what makes that checkable.**
    #
    # The planner still does not decide whether its proposal is worth checking — it has no way to
    # express that and this condition gives it none. What the condition asks is whether there is a
    # proposal at all. ``PlanNode`` leaves the case in ``PLANNING`` when it produced one and parks
    # it in ``AWAITING_HUMAN`` when the bounded retry produced nothing, and those are its only two
    # outcomes, so "the case is still PLANNING" and "a proposal was written" are the same statement.
    #
    # It was unconditional until the plan node could park, and then it had to change. Measured
    # both ways: an unconditional edge routes a *parked* case straight into the gate, where a
    # first-proposal exhaustion was overwritten with ``FAILED`` by the gate's own no-proposal
    # branch, and an exhaustion after a refusal had the gate re-judge the proposal it had already
    # denied — on a case a person had just been handed. Same reasoning as the absent
    # ``verify -> plan`` edge: an edge is what stops the graph routing somewhere, and a node's own
    # guard is not a substitute for one.
    builder.add_edge(PLAN, GATE, when_case_is(context, CaseState.PLANNING))

    builder.add_edge(GATE, EXECUTE, when_case_is(context, CaseState.READY_TO_EXECUTE))
    # The replan cycle. A denied proposal leaves the case in PLANNING, and this edge takes it back
    # to the planner with the refusal in its context. Nothing bounds how often that can happen
    # except max_node_executions, which is the point.
    builder.add_edge(GATE, PLAN, when_case_is(context, CaseState.PLANNING))

    builder.add_edge(EXECUTE, VERIFY, when_case_is(context, CaseState.VERIFYING))
    # A reconciliation that established the outcome has recorded a result and moved the case to
    # VERIFYING, so what follows is an ordinary verification — the same node, the same independent
    # read, the same two outcomes. Its other three endings (WAITING_EXTERNAL, AWAITING_HUMAN,
    # FAILED) satisfy no outgoing condition and end the run, exactly as the gate's parking does.
    builder.add_edge(RECONCILE, VERIFY, when_case_is(context, CaseState.VERIFYING))
    # **Nothing leaves verify.** There was a verify -> plan edge here, and removing it is half of
    # what stops a failed verification becoming a second external effect; the other half is
    # TRANSITIONS refusing VERIFYING -> PLANNING, so a node that tried to route around this edge
    # could not write the state the edge would have needed. Verification now ends a run whichever
    # way it goes: DONE and AWAITING_HUMAN both have no satisfied outgoing edge, exactly as
    # AWAITING_HUMAN already did at the gate.

    builder.set_entry_point(DISPATCH)
    builder.set_max_node_executions(max_node_executions)
    builder.set_execution_timeout(execution_timeout)
    builder.set_node_timeout(node_timeout)
    # Nodes hold no state between executions — they read the case from the store every time — so
    # this changes nothing about behaviour. It keeps the graph's own completed-node bookkeeping
    # accurate across a revisit, which is what the replan cycle is.
    builder.reset_on_revisit(True)

    return builder.build()


__all__ = [
    "DISPATCH",
    "EVIDENCE",
    "EXECUTE",
    "EXECUTION_TIMEOUT",
    "GATE",
    "MAX_NODE_EXECUTIONS",
    "NODE_IDS",
    "NODE_TIMEOUT",
    "PLAN",
    "RECONCILE",
    "TRIAGE",
    "VERIFY",
    "build_graph",
]
