"""The two LLM agents: Triage and Planner.

Exactly two, and that is architecture rather than convenience — see invariant 1 in ``CLAUDE.md``.
Triage classifies an inbound event into typed case fields; Planner proposes one next action as a
typed tool intent. Everything else in CareLoop is deterministic Python or a typed tool.

Both agents **propose**. Neither acts. That is structural, not a promise: an agent here holds a
model and a system prompt, and nothing else. No tool is registered against either of them, so
there is no call path from an agent to an effect, and no event loop in which one could appear.
"""

from careloop.agents.schemas import (
    ARG_NAMES,
    INTENT_FIELDS,
    NOT_SUPPLIED,
    TOOL_NAMES,
    EmptyRegistry,
    PlannerIntent,
    PlannerIntentBase,
    ReservedArgumentName,
    TriageCategory,
    TriageResult,
    Urgency,
    argument_names,
    build_planner_intent,
    tool_names,
)

__all__ = [
    "ARG_NAMES",
    "INTENT_FIELDS",
    "NOT_SUPPLIED",
    "TOOL_NAMES",
    "EmptyRegistry",
    "PlannerIntent",
    "PlannerIntentBase",
    "ReservedArgumentName",
    "TriageCategory",
    "TriageResult",
    "Urgency",
    "argument_names",
    "build_planner_intent",
    "tool_names",
]
