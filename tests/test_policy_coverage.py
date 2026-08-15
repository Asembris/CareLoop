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

from pathlib import Path
from typing import Any, Final

import pytest

from careloop.policy import (
    PolicyConfigError,
    RuleSet,
    default_rules,
    load_rules_from_mapping,
    uncovered_authority_arguments,
    uncovered_external_tools,
    unknown_rule_tools,
)

# Imported from the module rather than the package on purpose: the three validation steps are not
# part of `careloop.policy`'s surface, so reaching one is a deliberate act that a sweep can see.
from careloop.policy.loader import parse_rules, validate_rules_resolve
from careloop.provenance import ArgClass, TrustLevel
from careloop.tools import REGISTRY, ArgSpec, ToolRegistry, ToolSpec


def _registry_plus(*extra: ToolSpec) -> ToolRegistry:
    return ToolRegistry([*REGISTRY, *extra])


def _ruleset_skipping_coverage(raw: Any, registry: ToolRegistry) -> RuleSet:
    """Build a ruleset with everything except the authority-coverage invariant.

    Needed because half of these tests are *about* an uncovered ruleset, and since session N the
    normal loader refuses to produce one. Two questions are being asked and they need different
    constructors: :func:`uncovered_authority_arguments` is what the check *reports*, tested here,
    and :func:`load_rules_from_mapping` is what production *refuses*, tested in
    :class:`TestProductionLoadingRejectsUncoveredAuthorityArguments` below.

    This is the same composition ``evals/replay.py::permissive_engine`` performs, for the same
    reason and no other; the sweep in :class:`TestOnlyTheEvaluationMutantSkipsCoverage` covers
    production code, where it remains the only one.
    """
    ruleset = parse_rules(raw)
    validate_rules_resolve(ruleset, registry)
    return ruleset


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


def _synthetic_raw(when: dict[str, Any], tool: str = "wire_transfer") -> dict[str, Any]:
    """One ``ALLOW`` rule over ``tool`` with exactly the condition given, unvalidated."""
    return {
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
    }


def _synthetic_rules(when: dict[str, Any], tool: str = "wire_transfer") -> RuleSet:
    """The same rule as a ``RuleSet``, built without the coverage invariant.

    Deliberately not through :func:`load_rules_from_mapping`: most of the conditions below are
    uncovered on purpose, and the normal loader now refuses them — which is itself asserted, a few
    classes down.
    """
    return _ruleset_skipping_coverage(
        _synthetic_raw(when, tool),
        _registry_plus(_ONE_DESTINATION, _ONE_AUTHORITY, _HARMLESS_DESTINATION),
    )


def _trust(arg: str, level: str = "TRUSTED") -> dict[str, Any]:
    return {"arg_trust_at_least": [{"arg": arg, "level": level}]}


#: A ``DENY`` naming the destination beside an ``ALLOW`` that does not. The ``DENY`` would refuse
#: the call today; the ``ALLOW`` is what would authorise it the day somebody edits the ``DENY``.
_DENY_BESIDE_A_PERMISSIVE_ALLOW: Final[dict[str, Any]] = {
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
}

#: One strict ``ALLOW`` and one permissive one over the same tool. The permissive rule is the one
#: that fires, which is why coverage is checked per rule rather than over their union.
_STRICT_BESIDE_PERMISSIVE: Final[dict[str, Any]] = {
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
}


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
        ruleset = _ruleset_skipping_coverage(
            _DENY_BESIDE_A_PERMISSIVE_ALLOW, _registry_plus(_ONE_DESTINATION)
        )

        assert uncovered_authority_arguments(_registry_plus(_ONE_DESTINATION), ruleset) == {
            ("allow-the-new-tool", "wire_transfer", "payee")
        }

    def test_every_allow_rule_is_checked_and_not_merely_their_union(self) -> None:
        """One strict rule beside one permissive one is the arrangement this exists to catch: the
        permissive one is the rule that fires."""
        ruleset = _ruleset_skipping_coverage(
            _STRICT_BESIDE_PERMISSIVE, _registry_plus(_ONE_DESTINATION)
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


class TestProductionLoadingRejectsUncoveredAuthorityArguments:
    """The invariant is enforced when a ruleset is *loaded*, not merely reported by a helper.

    The class above proves what :func:`uncovered_authority_arguments` finds. This one proves that
    finding something is fatal on the path production actually takes — ``default_rules`` →
    ``load_rules`` → ``load_rules_from_text`` → ``load_rules_from_mapping`` — so a ruleset with an
    unguarded destination cannot reach a :class:`~careloop.policy.engine.PolicyEngine` at all.

    Session N shipped the check as a diagnostic and left this open as decision 27, because the one
    ruleset in the repository that violates it on purpose is the security evaluation's allow-all
    mutant. That is resolved by construction rather than by exemption: see
    :class:`TestOnlyTheEvaluationMutantSkipsCoverage`.
    """

    def test_the_shipped_rules_load(self) -> None:
        """The positive anchor. Without it, a loader that rejected everything would pass below."""
        shipped = default_rules()
        raw = {"version": 1, "rules": [rule.model_dump(mode="json") for rule in shipped.rules]}

        assert load_rules_from_mapping(raw, REGISTRY).rule_ids == shipped.rule_ids

    def test_an_uncovered_destination_is_refused_at_load(self) -> None:
        with pytest.raises(PolicyConfigError) as raised:
            load_rules_from_mapping(_synthetic_raw({}), _registry_plus(_ONE_DESTINATION))

        assert "wire_transfer.payee" in str(raised.value)

    def test_the_message_names_the_rule_the_tool_the_argument_and_the_threshold(self) -> None:
        """A precise diagnostic, because a vague one turns a policy mistake into a mystery."""
        with pytest.raises(PolicyConfigError) as raised:
            load_rules_from_mapping(_synthetic_raw({}), _registry_plus(_ONE_DESTINATION))

        message = str(raised.value)
        assert "allow-the-new-tool" in message
        assert "wire_transfer" in message
        assert "payee" in message
        assert str(TrustLevel.TRUSTED) in message
        assert str(ArgClass.DESTINATION) in message

    def test_an_uncovered_authority_argument_is_refused_at_load(self) -> None:
        with pytest.raises(PolicyConfigError) as raised:
            load_rules_from_mapping(
                _synthetic_raw({}, tool="elevate_privileges"), _registry_plus(_ONE_AUTHORITY)
            )

        assert "elevate_privileges.scope" in str(raised.value)

    def test_a_covered_destination_loads(self) -> None:
        ruleset = load_rules_from_mapping(
            _synthetic_raw(_trust("payee")), _registry_plus(_ONE_DESTINATION)
        )

        assert ruleset.rule_ids == {"allow-the-new-tool"}

    def test_a_permissive_allow_beside_a_strict_one_is_refused_at_load(self) -> None:
        with pytest.raises(PolicyConfigError) as raised:
            load_rules_from_mapping(_STRICT_BESIDE_PERMISSIVE, _registry_plus(_ONE_DESTINATION))

        assert "allow-the-convenient-way" in str(raised.value)

    def test_a_deny_rule_does_not_rescue_a_permissive_allow_at_load(self) -> None:
        with pytest.raises(PolicyConfigError):
            load_rules_from_mapping(
                _DENY_BESIDE_A_PERMISSIVE_ALLOW, _registry_plus(_ONE_DESTINATION)
            )

    def test_every_normal_entry_point_enforces_it(self, tmp_path) -> None:
        """``load_rules_from_text`` and ``load_rules`` funnel into the mapping loader, so all three
        refuse. Asserted rather than assumed, because a future entry point that forgot would be the
        whole hole reopened."""
        import yaml as _yaml

        from careloop.policy import load_rules, load_rules_from_text

        text = _yaml.safe_dump(_synthetic_raw({}))
        path = tmp_path / "rules.yaml"
        path.write_text(text, encoding="utf-8")
        registry = _registry_plus(_ONE_DESTINATION)

        with pytest.raises(PolicyConfigError):
            load_rules_from_text(text, registry)
        with pytest.raises(PolicyConfigError):
            load_rules(path, registry)


class TestOnlyTheEvaluationMutantSkipsCoverage:
    """Exactly one place in the repository composes the loading steps and omits coverage.

    There is deliberately no ``skip_validation`` parameter: a flag is something any caller can
    pass, an omission is something a sweep can find, and this is that sweep. It is the same
    discipline as ``tests/test_reopen.py``'s sweep for ``reopen_case`` and
    ``tests/test_api_demo.py``'s for ``CaseStore.clear`` — a property enforced by enumeration
    rather than by everyone remembering.
    """

    #: Production trees. Test modules legitimately build uncovered rulesets to test the check.
    ROOTS = ("careloop", "evals", "bench", "scripts", "demoworld")

    def _modules(self):
        root = Path(__file__).resolve().parents[1]
        for tree in self.ROOTS:
            yield from sorted((root / tree).rglob("*.py"))

    def test_only_the_permissive_engine_calls_the_lower_level_path(self) -> None:
        callers = {
            path.name
            for path in self._modules()
            if "validate_rules_resolve" in path.read_text(encoding="utf-8")
        }

        assert callers == {"loader.py", "replay.py"}, (
            "a module other than evals/replay.py composes rule validation by hand. That is how "
            "the authority-coverage invariant gets skipped without anyone deciding to skip it."
        )

    def test_the_permissive_engine_omits_exactly_that_call(self) -> None:
        """Vacuity: the sweep above would pass if ``replay.py`` also called the coverage check."""
        source = (Path(__file__).resolve().parents[1] / "evals" / "replay.py").read_text(
            encoding="utf-8"
        )

        assert "validate_rules_resolve(" in source
        assert "validate_authority_coverage(ruleset" not in source

    def test_nothing_in_the_loader_offers_a_way_to_turn_it_off(self) -> None:
        """No flag, by construction. A parameter is a thing the next caller can pass.

        Read off the real signatures rather than the source text, so the check is about what the
        functions accept and not about what their prose mentions.
        """
        from inspect import signature

        from careloop.policy import loader

        permitted = {"path", "text", "raw", "registry"}
        for name in ("load_rules", "load_rules_from_text", "load_rules_from_mapping"):
            parameters = set(signature(getattr(loader, name)).parameters)
            assert parameters <= permitted, (
                f"{name} grew the parameter(s) {sorted(parameters - permitted)}; a loader "
                f"switch is a way to turn the coverage invariant off"
            )


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
