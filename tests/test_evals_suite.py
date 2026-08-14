"""What the suite runs, what it excludes, and the telemetry opt-out that must precede an import.

Offline. The suite's composition is plain data precisely so it can be audited without installing
the paid extra, and the dry run is asserted to buy nothing on a machine with no credentials.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from evals.capture import TARGET_GATED, TARGET_PLANNER
from evals.run import RETRY_FACTOR, estimate_calls, main
from evals.suite import (
    EXCLUDED,
    OWASP_ASI_CATEGORIES_COVERED,
    TARGET_PURPOSE,
    VULNERABILITIES,
    attack_names,
    total_vulnerability_types,
    vulnerability_names,
)

BRIDGE = pathlib.Path(__file__).resolve().parent.parent / "evals" / "bridge.py"

#: The classes and types ``docs/spike-findings.md`` §7.4 confirmed exist in deepteam 1.0.9.
#: Restated here so a typo in the suite table fails offline rather than halfway through a paid run.
CONFIRMED = {
    "ExcessiveAgency": {"functionality", "permissions", "autonomy"},
    "ToolOrchestrationAbuse": {
        "recursive_tool_calls",
        "unsafe_tool_composition",
        "tool_budget_exhaustion",
        "cross_tool_state_leakage",
    },
    "IndirectInstruction": {
        "rag_injection",
        "tool_output_injection",
        "document_embedded_instructions",
        "cross_context_injection",
    },
    "AutonomousAgentDrift": {
        "goal_drift",
        "reward_hacking",
        "agent_collusion",
        "runaway_autonomy",
    },
    "ExternalSystemAbuse": {"data_exfiltration", "communications_spam", "internal_spoofing"},
    "PIILeakage": {
        "api_and_database_access",
        "direct_disclosure",
        "session_leak",
        "social_manipulation",
    },
}

CONFIRMED_ATTACKS = {
    "PromptInjection",
    "ContextPoisoning",
    "GoalRedirection",
    "PermissionEscalation",
}


class TestTheSuiteNamesThingsThatExist:
    """A typo here costs a paid run. Checked against the spike's recorded surface, offline."""

    def test_every_vulnerability_was_confirmed_by_the_spike(self) -> None:
        assert set(vulnerability_names()) == set(CONFIRMED)

    @pytest.mark.parametrize("spec", VULNERABILITIES, ids=lambda s: s.name)
    def test_every_type_was_confirmed_by_the_spike(self, spec) -> None:
        assert set(spec.types) <= CONFIRMED[spec.name], (
            f"{spec.name} names type(s) the spike did not record: "
            f"{sorted(set(spec.types) - CONFIRMED[spec.name])}"
        )

    def test_every_attack_technique_was_confirmed_by_the_spike(self) -> None:
        assert set(attack_names()) == CONFIRMED_ATTACKS

    def test_every_vulnerability_says_which_surface_it_aims_at(self) -> None:
        for spec in VULNERABILITIES:
            assert len(spec.rationale) > 40, spec.name

    def test_the_six_classes_the_session_asked_for_are_all_present(self) -> None:
        assert set(vulnerability_names()) >= {
            "ExcessiveAgency",
            "ToolOrchestrationAbuse",
            "IndirectInstruction",
            "AutonomousAgentDrift",
            "ExternalSystemAbuse",
            "PIILeakage",
        }


class TestTheExclusionsAreStatedRatherThanSilent:
    """A stated exclusion is evidence of judgement; an unstated one looks like an oversight."""

    def test_the_wrong_surface_classes_are_excluded(self) -> None:
        excluded = {item.name for item in EXCLUDED}

        assert {"Bias", "Toxicity", "GraphicContent"} <= excluded

    def test_the_absent_capability_classes_are_excluded(self) -> None:
        excluded = {item.name for item in EXCLUDED}

        assert {"SQLInjection", "ShellInjection", "UnexpectedCodeExecution"} <= excluded

    def test_every_exclusion_gives_a_reason(self) -> None:
        for item in EXCLUDED:
            assert len(item.reason) > 20, item.name

    def test_nothing_is_both_run_and_excluded(self) -> None:
        assert not set(vulnerability_names()) & {item.name for item in EXCLUDED}

    def test_only_the_two_mapping_owasp_categories_are_claimed(self) -> None:
        """Citing ASI_01 and ASI_02 is honest; citing the framework would not be."""
        assert [code for code, _ in OWASP_ASI_CATEGORIES_COVERED] == ["ASI_01", "ASI_02"]


class TestTheTargetPurposeAdvertisesNoDefence:
    """A purpose that named the policy engine would tell the attacker where the wall is, and tell
    the judge to expect a refusal — scoring the wall's reputation rather than the wall."""

    @pytest.mark.parametrize(
        "word",
        ["policy", "gate", "refus", "deny", "trusted", "guard", "block", "approval"],
    )
    def test_it_mentions_no_defence(self, word: str) -> None:
        assert word not in TARGET_PURPOSE.lower()

    def test_it_names_the_four_effectful_capabilities(self) -> None:
        """The simulator has to know what there is to attack, or it aims at nothing."""
        lowered = TARGET_PURPOSE.lower()

        for capability in ("email", "booking", "upload", "access"):
            assert capability in lowered


class TestTheEstimateErrsHigh:
    def test_it_includes_the_retry_factor(self) -> None:
        """deepteam retries three times with backoff, so one schema failure is three calls. An
        estimate that assumed one would understate the worst case, which is the only case an
        estimate shown before spending is for."""
        assert RETRY_FACTOR == 3

        estimate = estimate_calls(attacks_per_type=1, targets=1)

        assert estimate["simulator"] == estimate["attacks"] * 2 * RETRY_FACTOR

    def test_it_scales_with_both_multipliers(self) -> None:
        one = estimate_calls(attacks_per_type=1, targets=1)
        two = estimate_calls(attacks_per_type=2, targets=2)

        assert two["attacks"] == one["attacks"] * 2
        assert two["target"] == one["target"] * 4

    def test_the_type_count_matches_the_table(self) -> None:
        assert total_vulnerability_types() == sum(len(s.types) for s in VULNERABILITIES)


class TestTheDryRunBuysNothing:
    """The default path must be safe on a machine that is fully credentialled."""

    def test_it_exits_clean_without_confirm_spend(self, capsys) -> None:
        code = main(["--attacks-per-type", "1"])
        output = capsys.readouterr().out

        assert code == 0
        assert "DRY RUN" in output
        assert "Nothing was bought" in output

    def test_it_prints_the_call_estimate_before_any_decision(self, capsys) -> None:
        main(["--attacks-per-type", "1"])
        output = capsys.readouterr().out

        assert "Estimated provider calls" in output
        assert "TOTAL" in output

    def test_it_says_plainly_that_dollars_do_not_bound_the_judge(self, capsys) -> None:
        """§7.7. A reader must not take the dollar figure as covering the whole run."""
        main(["--attacks-per-type", "1"])
        output = capsys.readouterr().out

        assert "credit-metered" in output
        assert "--max-calls" in output

    def test_an_estimate_over_the_call_ceiling_refuses_before_spending(self, capsys) -> None:
        code = main(["--attacks-per-type", "50", "--max-calls", "10", "--confirm-spend"])

        assert code == 3
        assert "exceeds --max-calls" in capsys.readouterr().err

    def test_both_targets_are_the_default(self, capsys) -> None:
        main([])

        assert f"{TARGET_PLANNER}, {TARGET_GATED}" in capsys.readouterr().out


class TestTheTelemetryOptOutPrecedesTheImport:
    """``import deepteam`` resolves the operator's public IP and initialises Sentry unless the
    variable is *already* set. A shell export is not enough; the assignment has to be above the
    import statement in the only module that performs it.

    Asserted on the source's AST rather than on behaviour, because behaviour cannot distinguish
    "we opted out" from "somebody else's conftest happened to opt out first".
    """

    def source(self) -> ast.Module:
        return ast.parse(BRIDGE.read_text(encoding="utf-8"))

    def opt_out_lines(self) -> dict[str, int]:
        lines: dict[str, int] = {}
        for node in ast.walk(self.source()):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
                target = node.targets[0]
                if (
                    isinstance(target.value, ast.Attribute)
                    and target.value.attr == "environ"
                    and isinstance(target.slice, ast.Constant)
                ):
                    lines[str(target.slice.value)] = node.lineno
        return lines

    def first_deepteam_import_line(self) -> int:
        for node in ast.walk(self.source()):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("deepteam", "deepeval")
            ):
                return node.lineno
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("deepteam", "deepeval")):
                        return node.lineno
        return 10**6

    @pytest.mark.parametrize(
        "variable", ["DEEPTEAM_TELEMETRY_OPT_OUT", "DEEPEVAL_TELEMETRY_OPT_OUT"]
    )
    def test_the_variable_is_set(self, variable: str) -> None:
        assert variable in self.opt_out_lines()

    @pytest.mark.parametrize(
        "variable", ["DEEPTEAM_TELEMETRY_OPT_OUT", "DEEPEVAL_TELEMETRY_OPT_OUT"]
    )
    def test_it_is_set_above_any_deepteam_import(self, variable: str) -> None:
        assert self.opt_out_lines()[variable] < self.first_deepteam_import_line(), (
            f"{variable} is assigned after the first deepteam/deepeval import in "
            f"{BRIDGE.name}; by then the module-scope telemetry has already run"
        )

    def test_it_assigns_rather_than_calling_setdefault(self) -> None:
        """``setdefault`` would leave an environment that had already set the variable to
        something other than ``YES`` untouched — an opt-out that looks present and does nothing.

        Checked on the AST for a *call*, not on the source for a substring, so the module may go
        on explaining the distinction in prose. A substring sweep here failed on this file's own
        docstring, which is the same trap the ``ClinicClient`` sweep was written to avoid.
        """
        calls = [
            node
            for node in ast.walk(self.source())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
        ]

        assert calls == [], f"{BRIDGE.name} calls setdefault at line(s) {[c.lineno for c in calls]}"

    def test_the_value_is_the_one_deepteam_checks_for(self) -> None:
        """``telemetry_opt_out()`` compares against the literal string ``YES`` and nothing else."""
        values = {
            str(node.slice.value): node
            for node in ast.walk(self.source())
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
        }
        assigned = {
            str(node.targets[0].slice.value): node.value
            for node in ast.walk(self.source())
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
        }

        assert "DEEPTEAM_TELEMETRY_OPT_OUT" in values
        for variable, value in assigned.items():
            if "TELEMETRY_OPT_OUT" in variable:
                assert isinstance(value, ast.Constant) and value.value == "YES", variable
