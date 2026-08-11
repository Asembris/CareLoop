"""Vacuity test 1 — every externally effective tool is governed by policy.

Deny-by-default already means an ungoverned tool cannot be called, so this is not about safety at
runtime. It is about noticing. A tool added without a rule would fail every call at the default
deny, which reads in the timeline as "the planner keeps proposing something odd" rather than "we
forgot to write policy". This test names the omission at the point it happens.

The check is asserted in **both directions**. A coverage assertion that only ever runs against a
registry it already covers passes just as happily when it is broken, and that is precisely the
failure mode the project's working agreement calls vacuous.
"""

from __future__ import annotations

from careloop.policy import default_rules, uncovered_external_tools, unknown_rule_tools
from careloop.provenance import ArgClass
from careloop.tools import REGISTRY, ArgSpec, ToolRegistry, ToolSpec


def _registry_plus(*extra: ToolSpec) -> ToolRegistry:
    return ToolRegistry([*REGISTRY, *extra])


_UNGOVERNED = ToolSpec(
    name="wire_transfer",
    description="A newly added tool that nobody wrote policy for.",
    external_effect=True,
    args=(ArgSpec(name="payee", arg_class=ArgClass.DESTINATION),),
)

_HARMLESS = ToolSpec(
    name="read_note",
    description="A newly added tool with no effect outside CareLoop.",
    external_effect=False,
    args=(ArgSpec(name="note_id", arg_class=ArgClass.VALUE),),
)


class TestEveryExternalEffectToolHasPolicy:
    def test_the_shipped_registry_is_fully_covered(self) -> None:
        assert uncovered_external_tools(REGISTRY, default_rules()) == frozenset()

    def test_the_check_is_not_vacuous(self) -> None:
        """The other direction. An external-effect tool with no rule must be reported."""
        assert uncovered_external_tools(_registry_plus(_UNGOVERNED), default_rules()) == {
            "wire_transfer"
        }

    def test_a_tool_without_external_effect_needs_no_rule(self) -> None:
        """Coverage tracks effects on the outside world, not tool count."""
        assert uncovered_external_tools(_registry_plus(_HARMLESS), default_rules()) == frozenset()

    def test_the_check_reports_every_uncovered_tool_not_just_one(self) -> None:
        second = _UNGOVERNED.model_copy(update={"name": "issue_refund"})
        assert uncovered_external_tools(_registry_plus(_UNGOVERNED, second), default_rules()) == {
            "wire_transfer",
            "issue_refund",
        }

    def test_the_shipped_registry_actually_contains_external_effect_tools(self) -> None:
        """Without this, full coverage of an empty set would satisfy the test above."""
        assert len(REGISTRY.external_effect_tools) >= 4


class TestNoRuleNamesAToolThatDoesNotExist:
    """The converse drift: rules outliving the tools they govern."""

    def test_the_shipped_rules_name_only_registered_tools(self) -> None:
        assert unknown_rule_tools(REGISTRY, default_rules()) == frozenset()

    def test_the_check_is_not_vacuous(self) -> None:
        pruned = ToolRegistry(tool for tool in REGISTRY if tool.name != "send_email")
        assert unknown_rule_tools(pruned, default_rules()) == {"send_email"}


class TestEveryRuleCanEverFire:
    """A rule that no call could satisfy is a guard that is present but not working."""

    def test_every_rule_names_at_least_one_tool(self) -> None:
        assert all(rule.tools for rule in default_rules().rules)

    def test_every_argument_a_rule_guards_is_declared_by_every_tool_it_names(self) -> None:
        for rule in default_rules().rules:
            for tool_name in rule.tools:
                spec = REGISTRY.get(tool_name)
                assert spec is not None
                assert rule.when.referenced_args <= spec.arg_names, (
                    f"rule {rule.id} guards an argument {tool_name} does not have"
                )

    def test_every_rule_is_reachable_in_the_shipped_set(self) -> None:
        """No rule is shadowed by an unconditional earlier rule of the same effect.

        An unconditional rule swallows everything after it for the tools it names, so a later rule
        on those tools can never be reported as the matched rule — its diagnostic would never
        reach the timeline.
        """
        ruleset = default_rules()
        for effect in {rule.effect for rule in ruleset.rules}:
            unconditional: set[str] = set()
            for rule in ruleset.by_effect(effect):
                shadowed = unconditional & set(rule.tools)
                assert not shadowed, f"rule {rule.id} is unreachable for {sorted(shadowed)}"
                if rule.when == type(rule.when)():
                    unconditional.update(rule.tools)
