"""Load-time validation: a malformed or unresolvable rules file must fail startup.

Every rejection here would otherwise be a rule that silently never fires — the worst kind of
policy bug, because the suite stays green while the guard is gone.
"""

from __future__ import annotations

import pytest

from careloop.policy import (
    RULES_PATH,
    PolicyConfigError,
    RuleSet,
    Verdict,
    default_rules,
    load_rules,
    load_rules_from_text,
    unknown_rule_tools,
)
from careloop.tools import REGISTRY

_MINIMAL = """
version: 1
rules:
  - id: deny-something
    effect: DENY
    tools: [send_email]
    when:
      arg_trust_below:
        - arg: to
          level: TRUSTED
          arg_class: DESTINATION
    reason_code: DESTINATION_NOT_TRUSTED
    diagnostic: a diagnostic
"""


def _load(text: str) -> RuleSet:
    return load_rules_from_text(text, REGISTRY)


def _rejects(text: str, match: str) -> None:
    with pytest.raises(PolicyConfigError, match=match):
        _load(text)


class TestShippedRules:
    def test_the_shipped_rules_load_against_the_shipped_registry(self) -> None:
        ruleset = load_rules(RULES_PATH, REGISTRY)
        assert ruleset.version == 1
        assert ruleset.rules

    def test_default_rules_is_the_shipped_file(self) -> None:
        assert default_rules().rule_ids == load_rules(RULES_PATH, REGISTRY).rule_ids

    def test_every_tool_named_by_a_rule_exists(self) -> None:
        assert unknown_rule_tools(REGISTRY, default_rules()) == frozenset()

    def test_rules_carry_a_diagnostic_for_the_timeline(self) -> None:
        assert all(rule.diagnostic.strip() for rule in default_rules().rules)


class TestBaselineParses:
    def test_the_minimal_document_used_by_the_rejection_tests_is_itself_valid(self) -> None:
        """Otherwise every rejection below could be passing for the wrong reason."""
        ruleset = _load(_MINIMAL)
        assert ruleset.rules[0].id == "deny-something"


class TestRegistryResolution:
    def test_unknown_tool_name_is_rejected(self) -> None:
        _rejects(
            _MINIMAL.replace("[send_email]", "[no_such_tool]"),
            "names unknown tool 'no_such_tool'",
        )

    def test_argument_not_declared_by_that_tool_is_rejected(self) -> None:
        _rejects(
            _MINIMAL.replace("arg: to", "arg: recipient"),
            "references argument 'recipient', which tool 'send_email' does not declare",
        )

    def test_argument_declared_by_another_tool_is_still_rejected(self) -> None:
        """``portal_id`` is a real argument name, just not one ``send_email`` has."""
        _rejects(
            _MINIMAL.replace("arg: to", "arg: portal_id"),
            "references argument 'portal_id', which tool 'send_email' does not declare",
        )

    def test_arg_class_contradicting_the_registry_is_rejected(self) -> None:
        _rejects(
            _MINIMAL.replace("arg_class: DESTINATION", "arg_class: VALUE"),
            "asserts send_email.to is VALUE, but the registry declares it DESTINATION",
        )

    def test_args_present_referencing_an_undeclared_argument_is_rejected(self) -> None:
        text = _MINIMAL.replace(
            "      arg_trust_below:\n        - arg: to\n          level: TRUSTED\n"
            "          arg_class: DESTINATION\n",
            "      args_present: [nonexistent]\n",
        )
        _rejects(text, "references argument 'nonexistent'")

    def test_a_rule_naming_several_tools_must_resolve_against_every_one(self) -> None:
        """``to`` exists on ``send_email`` and not on ``upload_document``."""
        _rejects(
            _MINIMAL.replace("[send_email]", "[send_email, upload_document]"),
            "which tool 'upload_document' does not declare",
        )

    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        text = _MINIMAL.replace("[send_email]", "[no_such_tool, another_missing_tool]")
        with pytest.raises(PolicyConfigError) as caught:
            _load(text)
        assert "no_such_tool" in str(caught.value)
        assert "another_missing_tool" in str(caught.value)


class TestSchemaRejection:
    def test_unknown_reason_code_is_rejected(self) -> None:
        _rejects(_MINIMAL.replace("DESTINATION_NOT_TRUSTED", "MADE_UP_CODE"), "malformed rules")

    def test_unknown_case_state_is_rejected_in_case_state_in(self) -> None:
        text = _MINIMAL.replace("    when:\n", "    when:\n      case_state_in: [NOT_A_STATE]\n")
        _rejects(text, "malformed rules")

    def test_unknown_case_state_is_rejected_in_case_state_not_in(self) -> None:
        text = _MINIMAL.replace(
            "    when:\n", "    when:\n      case_state_not_in: [NOT_A_STATE]\n"
        )
        _rejects(text, "malformed rules")

    def test_unknown_trust_level_is_rejected(self) -> None:
        _rejects(_MINIMAL.replace("level: TRUSTED", "level: VERY_TRUSTED"), "malformed rules")

    def test_unknown_effect_is_rejected(self) -> None:
        _rejects(_MINIMAL.replace("effect: DENY", "effect: MAYBE"), "malformed rules")

    def test_unknown_key_on_a_rule_is_rejected(self) -> None:
        _rejects(
            _MINIMAL.replace("    diagnostic:", "    priority: 3\n    diagnostic:"), "priority"
        )

    def test_misspelt_condition_key_is_rejected(self) -> None:
        """A silently-ignored condition key is a rule that never fires."""
        _rejects(_MINIMAL.replace("arg_trust_below", "arg_trust_bellow"), "arg_trust_bellow")

    def test_duplicate_rule_id_is_rejected(self) -> None:
        _rejects(_MINIMAL + _MINIMAL.split("rules:")[1], "declared twice")

    def test_a_rule_must_name_at_least_one_tool(self) -> None:
        _rejects(_MINIMAL.replace("tools: [send_email]", "tools: []"), "malformed rules")

    def test_allow_carrying_a_reason_code_is_rejected(self) -> None:
        _rejects(
            _MINIMAL.replace("effect: DENY", "effect: ALLOW"),
            "must not carry a reason_code",
        )

    def test_refusal_without_a_reason_code_is_rejected(self) -> None:
        _rejects(
            _MINIMAL.replace("    reason_code: DESTINATION_NOT_TRUSTED\n", ""),
            "must carry a reason_code",
        )

    def test_require_human_without_a_reason_code_is_rejected(self) -> None:
        text = _MINIMAL.replace("effect: DENY", "effect: REQUIRE_HUMAN").replace(
            "    reason_code: DESTINATION_NOT_TRUSTED\n", ""
        )
        _rejects(text, "must carry a reason_code")

    def test_missing_diagnostic_is_rejected(self) -> None:
        _rejects(_MINIMAL.replace("    diagnostic: a diagnostic\n", ""), "malformed rules")

    def test_invalid_yaml_is_rejected(self) -> None:
        _rejects("version: 1\nrules: [", "not valid YAML")

    def test_a_missing_file_is_rejected(self, tmp_path) -> None:
        with pytest.raises(PolicyConfigError, match="cannot read rules file"):
            load_rules(tmp_path / "absent.yaml", REGISTRY)


class TestRuleSetAccessors:
    def test_by_effect_preserves_file_order(self) -> None:
        ruleset = default_rules()
        denies = ruleset.by_effect(Verdict.DENY)
        assert [rule.id for rule in denies] == [
            rule.id for rule in ruleset.rules if rule.effect is Verdict.DENY
        ]

    def test_by_effect_partitions_the_rules(self) -> None:
        ruleset = default_rules()
        partitioned = sum(len(ruleset.by_effect(effect)) for effect in Verdict)
        assert partitioned == len(ruleset.rules)
