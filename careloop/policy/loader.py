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

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from careloop.policy.schema import RuleSet
from careloop.policy.verdicts import Verdict
from careloop.provenance.trust import ArgClass, TrustLevel, at_least
from careloop.tools.registry import REGISTRY, ToolRegistry

#: The shipped rules file.
RULES_PATH: Final[Path] = Path(__file__).with_name("rules.yaml")

#: The trust an argument of each class must be required to reach before any rule may authorise a
#: call carrying it.
#:
#: ``DESTINATION`` and ``AUTHORITY`` only, and ``TRUSTED`` for both. That is not a policy choice
#: made here — it is :data:`careloop.provenance.trust.PROMOTABLE_ARG_CLASSES` read from the other
#: end. Those two classes are precisely the ones validation may never raise, so ``TRUSTED`` is the
#: only level they can legitimately reach, and a rule requiring anything less of one is requiring
#: something no legitimate value could fail and no illegitimate value could not meet.
#:
#: ``VALUE`` is deliberately absent and must stay absent. ``patient_id``, ``slot_start`` and
#: ``document_id`` are required at ``CONTROLLED`` on purpose — that is exactly what a passed
#: validator earns — and ``subject``, ``body`` and ``reason`` declare no ``trust_key`` at all, so a
#: rule requiring their trust would fail in :func:`_validate_against_registry` a few lines below.
#: Widening this mapping to ``VALUE`` would not strengthen the trust model; it would delete it.
REQUIRED_AUTHORITY_TRUST: Final[Mapping[ArgClass, TrustLevel]] = {
    ArgClass.DESTINATION: TrustLevel.TRUSTED,
    ArgClass.AUTHORITY: TrustLevel.TRUSTED,
}


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


def uncovered_authority_arguments(
    registry: ToolRegistry, ruleset: RuleSet
) -> frozenset[tuple[str, str, str]]:
    """``(rule id, tool, argument)`` for every authority-bearing argument an ``ALLOW`` rule ignores.

    The coverage question one level finer than :func:`uncovered_external_tools`. That one asks
    whether every externally effective *tool* is named by some rule; this asks whether every
    ``DESTINATION`` and ``AUTHORITY`` *argument* of such a tool has its trust required by **every**
    ``ALLOW`` rule that could authorise a call to it.

    **Per rule, not per ruleset, and that is the whole strength of it.** A union-of-all-rules
    reading would be satisfied by one strict rule sitting beside one permissive one, which is the
    arrangement the check exists to catch: the permissive rule is the one that fires.

    **A ``DENY`` rule elsewhere does not count**, deliberately. ``deny-upload-to-untrusted-portal``
    and ``allow-upload-to-trusted-portal`` both name ``portal_id`` and neither is redundant — the
    ``DENY`` states the refusal, and the ``ALLOW`` clause is what stops the positive path depending
    on the ``DENY`` still being there tomorrow. Requiring both is defence in depth rather than
    belt-and-braces, and session N is why: ``grant_portal_access.portal_id`` was missing from both
    at once, and either one alone would have refused the call.

    **``args_approved`` does not count either.** Human approval and deterministic trust are
    different dimensions — an approval says a caregiver was asked about one exact call, and says
    nothing about where that call lands. ``rules.yaml`` states the property plainly:
    ``allow-approved-portal-access-grant`` is approved by a caregiver but never *because* approved
    by one.

    A predicate satisfies the requirement only at or above
    :data:`REQUIRED_AUTHORITY_TRUST`, so naming an argument at ``UNTRUSTED`` — a clause that is
    true of every value there is — does not buy coverage.

    Empty means covered. A tool no ``ALLOW`` rule names is vacuously covered, because nothing can
    authorise it; deny-by-default is what refuses it, and :func:`uncovered_external_tools` is what
    notices that nobody wrote policy for it.
    """
    uncovered: set[tuple[str, str, str]] = set()
    for rule in ruleset.by_effect(Verdict.ALLOW):
        for tool_name in rule.tools:
            spec = registry.get(tool_name)
            if spec is None or not spec.external_effect:
                continue
            for arg in spec.args:
                required = REQUIRED_AUTHORITY_TRUST.get(arg.arg_class)
                if required is None:
                    continue
                if not any(
                    predicate.arg == arg.name and at_least(predicate.level, required)
                    for predicate in rule.when.arg_trust_at_least
                ):
                    uncovered.add((rule.id, tool_name, arg.name))
    return frozenset(uncovered)


__all__ = [
    "REQUIRED_AUTHORITY_TRUST",
    "RULES_PATH",
    "PolicyConfigError",
    "default_rules",
    "load_rules",
    "load_rules_from_mapping",
    "load_rules_from_text",
    "uncovered_authority_arguments",
    "uncovered_external_tools",
    "unknown_rule_tools",
]
