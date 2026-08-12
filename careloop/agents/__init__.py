"""The two LLM agents: Triage and Planner.

Exactly two, and that is architecture rather than convenience — see invariant 1 in ``CLAUDE.md``.
Triage classifies an inbound event into typed case fields; Planner proposes one next action as a
typed tool intent. Everything else in CareLoop is deterministic Python or a typed tool.

Both agents **propose**. Neither acts. That is structural, not a promise: an agent here holds a
model and a system prompt, and nothing else. No tool is registered against either of them, so
there is no call path from an agent to an effect, and no event loop in which one could appear.
"""

from careloop.agents.invoke import (
    NoStructuredOutput,
    WrongStructuredOutputType,
    structured_call,
)
from careloop.agents.planner import PlannerAgent
from careloop.agents.prompts import (
    EVIDENCE_BEGIN,
    EVIDENCE_END,
    EVIDENCE_NOTICE,
    PLANNER_SYSTEM,
    QUOTE_PREFIX,
    TRIAGE_SYSTEM,
    frame_untrusted,
    planner_system_prompt,
    planner_user_prompt,
    render_catalogue,
    render_denials,
    triage_system_prompt,
    triage_user_prompt,
)
from careloop.agents.schemas import (
    ARG_NAMES,
    INTENT_FIELDS,
    NOT_SUPPLIED,
    TOOL_NAMES,
    ArgumentMap,
    EmptyRegistry,
    FrozenArguments,
    PlannerIntent,
    PlannerIntentBase,
    TriageCategory,
    TriageResult,
    Urgency,
    argument_names,
    build_planner_intent,
    tool_names,
)
from careloop.agents.triage import TriageAgent

__all__ = [
    "ARG_NAMES",
    "EVIDENCE_BEGIN",
    "EVIDENCE_END",
    "EVIDENCE_NOTICE",
    "INTENT_FIELDS",
    "NOT_SUPPLIED",
    "PLANNER_SYSTEM",
    "QUOTE_PREFIX",
    "TOOL_NAMES",
    "TRIAGE_SYSTEM",
    "ArgumentMap",
    "EmptyRegistry",
    "FrozenArguments",
    "NoStructuredOutput",
    "PlannerAgent",
    "PlannerIntent",
    "PlannerIntentBase",
    "TriageAgent",
    "TriageCategory",
    "TriageResult",
    "Urgency",
    "WrongStructuredOutputType",
    "argument_names",
    "build_planner_intent",
    "frame_untrusted",
    "planner_system_prompt",
    "planner_user_prompt",
    "render_catalogue",
    "render_denials",
    "structured_call",
    "tool_names",
    "triage_system_prompt",
    "triage_user_prompt",
]
