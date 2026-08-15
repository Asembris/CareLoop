"""Loading and validating ``rules.yaml`` against the tool registry.

Validation happens at load, not at evaluation. A rule naming a tool that does not exist, or an
argument that tool does not declare, is a rule that silently never fires — the suite stays green
and the guard is gone. Failing startup turns that from an invisible hole into an outage, which is
the trade this project wants.

A rule requiring *trust* of an argument that declares no
:attr:`~careloop.tools.registry.ArgSpec.trust_key` is the same failure wearing a different hat, and
is refused here for the same reason. Nothing can establish trust for such an argument, so the
predicate resolves ``UNTRUSTED`` for ever: written as ``arg_trust_at_least`` it is a rule that can
never fire, and written as ``arg_trust_below`` it is one that always does. Both are configuration
mistakes about authority, and both should be an outage rather than a surprise three nodes later.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from careloop.policy.schema import RuleSet
from careloop.tools.registry import REGISTRY, ToolRegistry

#: The shipped rules file.
RULES_PATH: Final[Path] = Path(__file__).with_name("rules.yaml")


class PolicyConfigError(Exception):
    """Raised when a rules file is malformed or does not resolve against the registry.

    Raised at load time only. The engine never raises it, because by the time the engine runs the
    rules have already been proven to resolve.
    """


def _validate_against_registry(ruleset: RuleSet, registry: ToolRegistry) -> None:
    """Raise :class:`PolicyConfigError` listing every way ``ruleset`` fails to resolve.

    Every problem is collected rather than raised on the first one: a rules file with three
    mistakes should report three, not three consecutive startup failures.
    """
    problems: list[str] = []

    for rule in ruleset.rules:
        for tool_name in rule.tools:
            spec = registry.get(tool_name)
            if spec is None:
                problems.append(f"rule {rule.id!r} names unknown tool {tool_name!r}")
                continue

            for arg_name in sorted(rule.when.referenced_args):
                if arg_name not in spec.arg_names:
                    problems.append(
                        f"rule {rule.id!r} references argument {arg_name!r}, "
                        f"which tool {tool_name!r} does not declare"
                    )

            for predicate in (*rule.when.arg_trust_below, *rule.when.arg_trust_at_least):
                if predicate.arg not in spec.arg_names:
                    continue  # already reported as an undeclared argument above
                if spec.trust_key(predicate.arg) is None:
                    problems.append(
                        f"rule {rule.id!r} requires trust of {tool_name}.{predicate.arg}, "
                        f"which declares no trust_key; nothing can establish trust for it, so "
                        f"the rule could only ever resolve UNTRUSTED and never match"
                    )

            for predicate in rule.when.arg_class_assertions:
                declared = spec.arg_class(predicate.arg)
                if declared is None:
                    continue  # already reported as an undeclared argument above
                if declared is not predicate.arg_class:
                    problems.append(
                        f"rule {rule.id!r} asserts {tool_name}.{predicate.arg} is "
                        f"{predicate.arg_class}, but the registry declares it {declared}; "
                        f"the registry owns argument classes"
                    )

    if problems:
        raise PolicyConfigError(
            "rules do not resolve against the tool registry:\n  " + "\n  ".join(problems)
        )


def load_rules_from_mapping(raw: Any, registry: ToolRegistry) -> RuleSet:
    """Validate an already-parsed rules document against ``registry``."""
    try:
        ruleset = RuleSet.model_validate(raw)
    except ValidationError as exc:
        raise PolicyConfigError(f"malformed rules document: {exc}") from exc

    _validate_against_registry(ruleset, registry)
    return ruleset


def load_rules_from_text(text: str, registry: ToolRegistry) -> RuleSet:
    """Parse and validate a rules document held in memory. Used by tests."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"rules document is not valid YAML: {exc}") from exc
    return load_rules_from_mapping(raw, registry)


def load_rules(path: Path = RULES_PATH, registry: ToolRegistry = REGISTRY) -> RuleSet:
    """Read, parse and validate a rules file.

    Raises:
        PolicyConfigError: if the file is missing, is not valid YAML, does not match the schema,
            or names a tool or argument the registry does not have.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyConfigError(f"cannot read rules file {path}: {exc}") from exc
    return load_rules_from_text(text, registry)


@lru_cache(maxsize=1)
def default_rules() -> RuleSet:
    """The shipped rules, validated against the shipped registry.

    Cached rather than evaluated at import so that a broken rules file fails when policy is first
    needed — at startup — instead of turning every import of the package into a landmine.
    """
    return load_rules()


def uncovered_external_tools(registry: ToolRegistry, ruleset: RuleSet) -> frozenset[str]:
    """Externally effective tools that no rule names.

    Not a load-time failure, because a rules file being incomplete is a policy gap rather than a
    corrupt file, and the gap is what ``tests/test_policy_coverage.py`` exists to catch. Kept here
    rather than in the test so the check itself can be tested in both directions.
    """
    return frozenset(registry.external_effect_tools - ruleset.tools_named)


def unknown_rule_tools(registry: ToolRegistry, ruleset: RuleSet) -> frozenset[str]:
    """Tool names mentioned by rules that the registry does not declare."""
    return frozenset(ruleset.tools_named - registry.names)


__all__ = [
    "RULES_PATH",
    "PolicyConfigError",
    "default_rules",
    "load_rules",
    "load_rules_from_mapping",
    "load_rules_from_text",
    "uncovered_external_tools",
    "unknown_rule_tools",
]
