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

from typing import Any

from careloop.policy import (
    RuleSet,
    default_rules,
    load_rules_from_mapping,
    uncovered_authority_arguments,
    uncovered_external_tools,
    unknown_rule_tools,
)
from careloop.provenance import ArgClass, TrustLevel
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


#: A tool whose one destination a rule may or may not remember to guard. Every synthetic case
#: below is built from this, so the only thing varying between them is the *rule*.
_ONE_DESTINATION = ToolSpec(
    name="wire_transfer",
    description="A newly added tool with somewhere for its effect to land.",
    external_effect=True,
    args=(
        ArgSpec(name="payee", arg_class=ArgClass.DESTINATION, trust_key="caregiver_email"),
        ArgSpec(name="memo", arg_class=ArgClass.VALUE),
    ),
)

_ONE_AUTHORITY = ToolSpec(
    name="elevate_privileges",
    description="A newly added tool that confers powers.",
    external_effect=True,
    args=(ArgSpec(name="scope", arg_class=ArgClass.AUTHORITY, trust_key="role"),),
)

#: The same shape with no effect outside CareLoop. Coverage tracks effects, not tool count.
_HARMLESS_DESTINATION = _ONE_DESTINATION.model_copy(
    update={"name": "draft_transfer", "external_effect": False}
)


def _synthetic_rules(when: dict[str, Any], tool: str = "wire_transfer") -> RuleSet:
    """One ``ALLOW`` rule over ``tool`` with exactly the condition given."""
    return load_rules_from_mapping(
        {
            "version": 1,
            "rules": [
                {
                    "id": "allow-the-new-tool",
                    "effect": "ALLOW",
                    "tools": [tool],
                    "when": when,
                    "diagnostic": "d",
                }
            ],
        },
        _registry_plus(_ONE_DESTINATION, _ONE_AUTHORITY, _HARMLESS_DESTINATION),
    )


def _trust(arg: str, level: str = "TRUSTED") -> dict[str, Any]:
    return {"arg_trust_at_least": [{"arg": arg, "level": level}]}


class TestEveryAuthorityBearingArgumentIsRequiredByEveryAllowRule:
    """Vacuity test 1, one level finer.

    :func:`uncovered_external_tools` asks whether every effectful *tool* is named by some rule, and
    in session N every tool was — while ``grant_portal_access.portal_id``, a ``DESTINATION``
    argument declaring a ``trust_key``, was named by no rule at all. The engine resolved its trust
    and nothing read the answer, so a grant on an attacker-named portal reached ``ALLOW``. This is
    the check that asks the question the other one does not.

    Deliberately scoped to ``DESTINATION`` and ``AUTHORITY``. Extending it to ``VALUE`` would demand
    ``TRUSTED`` of ``patient_id``, ``slot_start`` and ``document_id`` — which are required at
    ``CONTROLLED`` on purpose, that being exactly what a passed validator earns — and would demand
    it of ``subject``, ``body`` and ``reason``, which declare no ``trust_key`` and for which such a
    rule fails at load. The narrowness is the design, not a compromise.
    """

    def test_the_shipped_rules_cover_every_authority_bearing_argument(self) -> None:
        assert uncovered_authority_arguments(REGISTRY, default_rules()) == frozenset()

    def test_the_shipped_registry_actually_has_authority_bearing_arguments(self) -> None:
        """Without this, coverage of an empty set would satisfy the test above."""
        found = {
            (tool.name, arg.name)
            for tool in REGISTRY
            if tool.external_effect
            for arg in tool.args
            if arg.arg_class in (ArgClass.DESTINATION, ArgClass.AUTHORITY)
        }

        assert found == {
            ("send_email", "to"),
            ("book_appointment", "provider_id"),
            ("upload_document", "portal_id"),
            ("grant_portal_access", "grantee"),
            ("grant_portal_access", "role"),
            ("grant_portal_access", "portal_id"),
        }

    def test_an_allow_rule_that_ignores_a_destination_is_reported(self) -> None:
        """The other direction, on a tool the shipped rules have never heard of."""
        assert uncovered_authority_arguments(
            _registry_plus(_ONE_DESTINATION), _synthetic_rules({})
        ) == {("allow-the-new-tool", "wire_transfer", "payee")}

    def test_requiring_the_destination_clears_it(self) -> None:
        assert (
            uncovered_authority_arguments(
                _registry_plus(_ONE_DESTINATION), _synthetic_rules(_trust("payee"))
            )
            == frozenset()
        )

    def test_an_authority_argument_is_held_to_the_same_standard(self) -> None:
        registry = _registry_plus(_ONE_AUTHORITY)
        ignored = _synthetic_rules({}, tool="elevate_privileges")
        required = _synthetic_rules(_trust("scope"), tool="elevate_privileges")

        assert uncovered_authority_arguments(registry, ignored) == {
            ("allow-the-new-tool", "elevate_privileges", "scope")
        }
        assert uncovered_authority_arguments(registry, required) == frozenset()

    def test_a_value_argument_is_not_reported(self) -> None:
        """``memo`` is ``VALUE``-class and left entirely unguarded. That is allowed."""
        uncovered = uncovered_authority_arguments(
            _registry_plus(_ONE_DESTINATION), _synthetic_rules(_trust("payee"))
        )

        assert all(argument != "memo" for _rule, _tool, argument in uncovered)

    def test_a_tool_with_no_external_effect_is_not_reported(self) -> None:
        assert (
            uncovered_authority_arguments(
                _registry_plus(_HARMLESS_DESTINATION), _synthetic_rules({}, tool="draft_transfer")
            )
            == frozenset()
        )

    def test_a_trust_clause_below_the_required_level_does_not_count(self) -> None:
        """A token clause is not coverage. ``UNTRUSTED`` is true of every value there is, and
        ``CONTROLLED`` is a level a validator can reach — neither is what a destination needs."""
        registry = _registry_plus(_ONE_DESTINATION)

        for level in (TrustLevel.UNTRUSTED, TrustLevel.CONTROLLED):
            assert uncovered_authority_arguments(
                registry, _synthetic_rules(_trust("payee", str(level)))
            ) == {("allow-the-new-tool", "wire_transfer", "payee")}, level

    def test_presence_does_not_count(self) -> None:
        """Session J's lesson, restated structurally: naming an argument is not guarding it."""
        assert uncovered_authority_arguments(
            _registry_plus(_ONE_DESTINATION), _synthetic_rules({"args_present": ["payee"]})
        ) == {("allow-the-new-tool", "wire_transfer", "payee")}

    def test_an_approval_clause_does_not_count(self) -> None:
        """Human approval and deterministic trust are different dimensions.

        An approval says a caregiver was asked about one exact call. It says nothing about where
        that call lands, and a rule that leans on it instead of on destination trust is asking a
        person to authorise something CareLoop should have refused.
        """
        assert uncovered_authority_arguments(
            _registry_plus(_ONE_DESTINATION), _synthetic_rules({"args_approved": True})
        ) == {("allow-the-new-tool", "wire_transfer", "payee")}

    def test_a_deny_rule_elsewhere_does_not_satisfy_the_allow_requirement(self) -> None:
        """Both halves are required, deliberately.

        The ``DENY`` states the refusal; the ``ALLOW`` clause is what stops the positive path
        depending on the ``DENY`` still being there tomorrow. Session N is the argument: the grant's
        portal was missing from both at once, and either alone would have refused the call.
        """
        ruleset = load_rules_from_mapping(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "deny-the-new-tool",
                        "effect": "DENY",
                        "tools": ["wire_transfer"],
                        "when": {"arg_trust_below": [{"arg": "payee", "level": "TRUSTED"}]},
                        "reason_code": "DESTINATION_NOT_TRUSTED",
                        "diagnostic": "d",
                    },
                    {
                        "id": "allow-the-new-tool",
                        "effect": "ALLOW",
                        "tools": ["wire_transfer"],
                        "when": {},
                        "diagnostic": "d",
                    },
                ],
            },
            _registry_plus(_ONE_DESTINATION),
        )

        assert uncovered_authority_arguments(_registry_plus(_ONE_DESTINATION), ruleset) == {
            ("allow-the-new-tool", "wire_transfer", "payee")
        }

    def test_every_allow_rule_is_checked_and_not_merely_their_union(self) -> None:
        """One strict rule beside one permissive one is the arrangement this exists to catch: the
        permissive one is the rule that fires."""
        ruleset = load_rules_from_mapping(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "allow-the-careful-way",
                        "effect": "ALLOW",
                        "tools": ["wire_transfer"],
                        "when": _trust("payee"),
                        "diagnostic": "d",
                    },
                    {
                        "id": "allow-the-convenient-way",
                        "effect": "ALLOW",
                        "tools": ["wire_transfer"],
                        "when": {},
                        "diagnostic": "d",
                    },
                ],
            },
            _registry_plus(_ONE_DESTINATION),
        )

        assert uncovered_authority_arguments(_registry_plus(_ONE_DESTINATION), ruleset) == {
            ("allow-the-convenient-way", "wire_transfer", "payee")
        }

    def test_a_tool_no_allow_rule_names_is_vacuously_covered(self) -> None:
        """Nothing can authorise it, so there is no rule to be wrong. That gap is
        :func:`uncovered_external_tools`'s to report, and it does."""
        registry = _registry_plus(_ONE_DESTINATION)

        assert uncovered_authority_arguments(registry, default_rules()) == frozenset()
        assert uncovered_external_tools(registry, default_rules()) == {"wire_transfer"}


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
