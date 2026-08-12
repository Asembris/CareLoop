"""Edge conditions.

Every condition in CareLoop's graph is a predicate over ``case.state``, read from the store.

**Not over node output.** The SDK threads each node's result text into the next node's input, and
routing on that text would make control flow depend on prose — prose that, one node upstream, was
produced by a model reading attacker-controlled evidence. Reading the case instead means the graph
branches on something only :func:`careloop.state.machine.transition` can have written, and the
transition table constrains what that can be.

**Not over a run-scoped variable either.** The store is the source of truth (invariant 8), so a
condition that consulted an in-memory flag could disagree with the case a node just committed.

The consequence worth knowing when reading ``builder.py``: a state with no satisfied outgoing edge
ends the run. ``AWAITING_HUMAN`` and ``DONE`` are exactly that, so parking and finishing need no
special handling — they are the absence of a next node rather than a signal.
"""

from __future__ import annotations

from collections.abc import Callable

from careloop.graph.context import RunContext
from careloop.state.case import CaseState

#: An edge condition, in the SDK's legacy form: it receives the graph state and ignores it.
EdgeCondition = Callable[[object], bool]


def when_case_is(context: RunContext, *states: CaseState) -> EdgeCondition:
    """An edge that traverses only when the persisted case is in one of ``states``.

    The ``GraphState`` argument is accepted and deliberately unused. It describes the graph's
    execution — which nodes ran, what they returned — and none of that is a business fact.
    """

    def condition(_graph_state: object) -> bool:
        return context.current_case().state in states

    condition.__doc__ = f"case is in {', '.join(sorted(state.value for state in states))}"
    return condition


def when_case_is_not(context: RunContext, *states: CaseState) -> EdgeCondition:
    """The complement of :func:`when_case_is`, for edges that should not fire on terminal states."""

    def condition(_graph_state: object) -> bool:
        return context.current_case().state not in states

    condition.__doc__ = f"case is not in {', '.join(sorted(state.value for state in states))}"
    return condition


__all__ = ["EdgeCondition", "when_case_is", "when_case_is_not"]
