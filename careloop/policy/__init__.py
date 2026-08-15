"""The deterministic authority layer: the LLM proposes, this decides."""

from careloop.policy.engine import (
    PolicyEngine,
    canonical_value,
    default_engine,
    resolve_trust,
)
from careloop.policy.gate import (
    PolicyDenied,
    PolicyGate,
    decision_cache_key,
    facts_fingerprint,
)
from careloop.policy.gated import (
    GATED_MARKER,
    GatedToolSet,
    ReservedToolName,
    check_no_reserved_collision,
    gated_tool,
)
from careloop.policy.hook import (
    PolicyHook,
    fire_before_tool_call,
    policy_registry,
    tool_use_id,
)
from careloop.policy.loader import (
    REQUIRED_AUTHORITY_TRUST,
    RULES_PATH,
    PolicyConfigError,
    default_rules,
    load_rules,
    load_rules_from_mapping,
    load_rules_from_text,
    uncovered_authority_arguments,
    uncovered_external_tools,
    unknown_rule_tools,
)
from careloop.policy.schema import ArgTrust, Condition, Rule, RuleSet
from careloop.policy.verdicts import REASON_TEXT, Decision, ReasonCode, Verdict, reason_text

__all__ = [
    "GATED_MARKER",
    "REASON_TEXT",
    "REQUIRED_AUTHORITY_TRUST",
    "RULES_PATH",
    "ArgTrust",
    "Condition",
    "Decision",
    "GatedToolSet",
    "PolicyConfigError",
    "PolicyDenied",
    "PolicyEngine",
    "PolicyGate",
    "PolicyHook",
    "ReasonCode",
    "ReservedToolName",
    "Rule",
    "RuleSet",
    "Verdict",
    "canonical_value",
    "check_no_reserved_collision",
    "decision_cache_key",
    "default_engine",
    "default_rules",
    "facts_fingerprint",
    "fire_before_tool_call",
    "gated_tool",
    "load_rules",
    "load_rules_from_mapping",
    "load_rules_from_text",
    "policy_registry",
    "reason_text",
    "resolve_trust",
    "tool_use_id",
    "uncovered_authority_arguments",
    "uncovered_external_tools",
    "unknown_rule_tools",
]
