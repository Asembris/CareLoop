"""The contract floor for the ``CareLoopSettlementIntegrity`` evaluator.

Patch B builds a pure deterministic evaluator against the AgentCore span contract Patch A measured
in production. This file holds the **floor** and nothing more: the independence boundary, the
no-interpolation guarantee, one real-shaped trace that must settle, one of each non-passing shape,
handler totality and determinism.

**The full behavioural matrix, the mutation proofs, the vacuity proofs and the drift checks against
CareLoop's own registry belong to Patch C and are deliberately absent.** What Patch B owes Patch C
is a structure that can be attacked check by check -- ``CHECKS`` is an ordered tuple of small named
pure functions, applicability is its own function, and every constant it decides against is a named
module-level value.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import evaluators
from evaluators.contract import (
    ATTR_EFFECT_DISPATCH,
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_STATE_DIGEST,
    ATTR_EFFECT_TYPE,
    CHECK_EXPLANATIONS,
    CLAIM_ATTRIBUTES,
    ERROR_MESSAGES,
    NA_EXPLANATIONS,
    CheckId,
    ErrorCode,
    NaReason,
)

# --------------------------------------------------------------------------------------------
# The independence boundary
# --------------------------------------------------------------------------------------------

#: The only top-level modules anything under ``evaluators/`` may import.
#:
#: Deliberately an allowlist rather than a denylist of the forbidden ones. A denylist answers
#: "did somebody import boto3", which is the question we already know to ask; an allowlist answers
#: "did somebody import anything at all that we have not thought about", which is the one that
#: catches the import nobody predicted. Everything here is pure standard library with no network,
#: no filesystem, no environment and no clock.
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "evaluators",
        "re",
        "typing",
        "urllib",
    }
)

#: Named individually so a failure says which prohibition was broken, and so that a future edit to
#: :data:`ALLOWED_IMPORTS` cannot silently re-admit one of them.
FORBIDDEN_IMPORTS = frozenset(
    {
        "boto3",
        "botocore",
        "careloop",
        "demoworld",
        "evals",
        "httpx",
        "opentelemetry",
        "os",
        "pathlib",
        "random",
        "requests",
        "secrets",
        "socket",
        "strands",
        "subprocess",
        "sys",
        "tempfile",
        "time",
        "urllib3",
        "uuid",
    }
)

EVALUATOR_SOURCES = sorted(Path(evaluators.__file__).parent.glob("*.py"))


def _imported_roots(source: Path) -> set[str]:
    """Every top-level module name ``source`` imports, however it imports it."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class TestTheEvaluatorImportsNothingItMustNot:
    """``evaluators/`` is standard library only, and that is what makes it evidence.

    An evaluator that imported CareLoop's constants would be checking CareLoop against itself --
    the same category error as verifying a booking by re-reading its own receipt. The rule is
    enforced by a sweep rather than by a convention because a convention is what the next session
    does not know about.
    """

    def test_there_are_evaluator_modules_to_sweep(self) -> None:
        """The sweep below is vacuous if it finds no files. This is what stops that."""
        assert EVALUATOR_SOURCES, "no modules found under evaluators/ to sweep"

    @pytest.mark.parametrize("source", EVALUATOR_SOURCES, ids=lambda path: path.name)
    def test_only_allowlisted_modules_are_imported(self, source: Path) -> None:
        unexpected = _imported_roots(source) - ALLOWED_IMPORTS
        assert not unexpected, (
            f"{source.name} imports {sorted(unexpected)}, which is outside the evaluator's "
            f"standard-library-only boundary. If one of these is genuinely needed, that is a "
            f"design change and not a test fix."
        )

    @pytest.mark.parametrize("source", EVALUATOR_SOURCES, ids=lambda path: path.name)
    def test_the_named_prohibitions_are_absent(self, source: Path) -> None:
        """The allowlist already covers these. Naming them says *why* they are covered."""
        broken = _imported_roots(source) & FORBIDDEN_IMPORTS
        assert not broken, f"{source.name} imports {sorted(broken)}"

    def test_the_prohibitions_and_the_allowlist_do_not_overlap(self) -> None:
        """A module cannot be both permitted and forbidden; that would make one list a lie."""
        assert not (ALLOWED_IMPORTS & FORBIDDEN_IMPORTS)


class TestTheNineAttributeContractIsWhatItSays:
    """The telemetry contract is exactly nine attributes, and ``action_key`` is not one of them."""

    def test_there_are_exactly_nine(self) -> None:
        assert len(CLAIM_ATTRIBUTES) == 9
        assert len(set(CLAIM_ATTRIBUTES)) == 9

    def test_every_attribute_is_namespaced_to_careloop(self) -> None:
        assert all(name.startswith("careloop.") for name in CLAIM_ATTRIBUTES)

    def test_the_effect_namespace_carries_exactly_its_four_approved_attributes(self) -> None:
        """No fifth effect attribute, whatever it is called."""
        effect_attributes = {
            name for name in CLAIM_ATTRIBUTES if name.startswith("careloop.effect.")
        }
        assert effect_attributes == {
            ATTR_EFFECT_TYPE,
            ATTR_EFFECT_DISPATCH,
            ATTR_EFFECT_FIELDS,
            ATTR_EFFECT_STATE_DIGEST,
        }

    def test_no_action_key_under_that_or_any_other_name(self) -> None:
        """``careloop.effect.action_key`` was dropped in the Patch A design and stays dropped.

        Asserted as *the package declares exactly these nine ``careloop.`` strings and no tenth*
        rather than as a literal search for the word. A literal search is both too weak and too
        strong: it misses a replacement identifier under a different spelling, which is the actual
        hazard, and it fires on the prose that forbids the original -- and a prohibition that
        cannot name what it forbids is not worth writing down.

        Docstrings and comments are unaffected because the filter is ``startswith("careloop.")``,
        which selects attribute-name constants and not sentences that happen to mention one.
        """
        declared: set[str] = set()
        for source in EVALUATOR_SOURCES:
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                is_text = isinstance(node, ast.Constant) and isinstance(node.value, str)
                if is_text and node.value.startswith("careloop."):
                    declared.add(node.value)
        assert declared == set(CLAIM_ATTRIBUTES), (
            f"the package declares CareLoop attribute names "
            f"{sorted(declared - set(CLAIM_ATTRIBUTES))} beyond the nine-attribute contract, or "
            f"is missing {sorted(set(CLAIM_ATTRIBUTES) - declared)}"
        )


# --------------------------------------------------------------------------------------------
# The no-interpolation guarantee
# --------------------------------------------------------------------------------------------


class TestEveryCodeHasOneFixedExplanation:
    """A result may only say things this repository wrote in advance.

    Invariant 6 keeps interpolated denial text away from the planner because it re-enters a
    prompt. Here the consumer is a person and a CloudWatch record rather than a model, and the
    reason is the neighbouring one: an evaluator result is a published artefact, so a URL, a
    confirmation id, a digest or an attribute value must not be able to reach it.
    """

    def test_every_check_has_an_explanation(self) -> None:
        assert set(CHECK_EXPLANATIONS) == set(CheckId)

    def test_every_na_reason_has_an_explanation(self) -> None:
        assert set(NA_EXPLANATIONS) == set(NaReason)

    def test_every_error_code_has_a_message(self) -> None:
        assert set(ERROR_MESSAGES) == set(ErrorCode)

    @pytest.mark.parametrize(
        "table",
        [CHECK_EXPLANATIONS, NA_EXPLANATIONS, ERROR_MESSAGES],
        ids=["checks", "na", "error"],
    )
    def test_no_entry_can_interpolate_anything(self, table: dict[Any, str]) -> None:
        """No format placeholder of any kind, in any entry.

        The mechanical half of the guarantee: an entry with no ``{}``, no ``%`` and no ``$``
        cannot be turned into a template by a later call to ``format``, ``%`` or
        ``Template.substitute``.
        """
        for code, sentence in table.items():
            assert isinstance(sentence, str), code
            for placeholder in ("{", "}", "%", "$"):
                assert placeholder not in sentence, f"{code} carries {placeholder!r}"

    @pytest.mark.parametrize(
        "table",
        [CHECK_EXPLANATIONS, NA_EXPLANATIONS, ERROR_MESSAGES],
        ids=["checks", "na", "error"],
    )
    def test_entries_are_distinct(self, table: dict[Any, str]) -> None:
        """Two codes sharing a sentence would make the result ambiguous to a reader."""
        assert len(set(table.values())) == len(table)
