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
              └───────────┘          │                 │
                                     ▼                 │
                                 ┌──────┐              │
                    ┌────────────┤ gate ├──────────────┘
                    │  DENY      └──┬───┘  ALLOW
                    │  (replan)     │
                    │               │ REQUIRE_HUMAN → no edge holds; the run ends parked
                    │               ▼
                    │          ┌─────────┐      ┌────────┐
                    │          │ execute ├─────▶│ verify │
                    │          └─────────┘      └───┬────┘
                    └──────────────────────────────-┘  verification failed → replan
                                                       DONE → no edge holds; the run ends

``plan → gate → plan`` is the cycle the bound exists for. ``AWAITING_HUMAN`` and ``DONE`` end a run
by having no satisfied outgoing edge, which is why neither needs a terminal node.
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
    TriageNode,
    VerifyNode,
)
from careloop.state.case import CaseState

#: Node executions one run may use.
#:
#: A happy path is seven (dispatch, triage, evidence, plan, gate, execute, verify) and a resume
#: after approval is three. Each refused proposal costs two more, so this leaves room for roughly
#: eight replans before the bound fires. Generous rather than tight: the bound is a runaway
#: detector, and a case that failed because a legitimate replan ran out of room would be a bug
#: reported as a policy outcome.
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
VERIFY: Final[str] = "verify"

#: Every node id, in the order a case meets them.
NODE_IDS: Final[tuple[str, ...]] = (DISPATCH, TRIAGE, EVIDENCE, PLAN, GATE, EXECUTE, VERIFY)


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
    builder.add_node(VerifyNode(VERIFY, context), VERIFY)

    # Dispatch routes to wherever the case already is. This is what makes a case parked for a
    # caregiver decision — or reconstructed from the store in a later process — resume correctly
    # rather than starting again from triage.
    builder.add_edge(DISPATCH, TRIAGE, when_case_is(context, CaseState.NEW))
    builder.add_edge(DISPATCH, EVIDENCE, when_case_is(context, CaseState.GATHERING_EVIDENCE))
    builder.add_edge(DISPATCH, PLAN, when_case_is(context, CaseState.PLANNING))
    builder.add_edge(DISPATCH, EXECUTE, when_case_is(context, CaseState.READY_TO_EXECUTE))
    builder.add_edge(DISPATCH, VERIFY, when_case_is(context, CaseState.VERIFYING))

    builder.add_edge(TRIAGE, EVIDENCE, when_case_is(context, CaseState.GATHERING_EVIDENCE))
    builder.add_edge(EVIDENCE, PLAN, when_case_is(context, CaseState.PLANNING))

    # The planner does not decide whether its proposal is worth gating; every proposal is gated.
    builder.add_edge(PLAN, GATE)

    builder.add_edge(GATE, EXECUTE, when_case_is(context, CaseState.READY_TO_EXECUTE))
    # The replan cycle. A denied proposal leaves the case in PLANNING, and this edge takes it back
    # to the planner with the refusal in its context. Nothing bounds how often that can happen
    # except max_node_executions, which is the point.
    builder.add_edge(GATE, PLAN, when_case_is(context, CaseState.PLANNING))

    builder.add_edge(EXECUTE, VERIFY, when_case_is(context, CaseState.VERIFYING))
    builder.add_edge(VERIFY, PLAN, when_case_is(context, CaseState.PLANNING))

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
    "TRIAGE",
    "VERIFY",
    "build_graph",
]
