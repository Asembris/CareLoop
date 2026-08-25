"""The published proof ledger, held to the measurement that produced each number.

``site/site.toml`` carries the six results the dossier's *MEASURED* chapter renders, and until this
session nothing checked any of them. That is not a small gap: ``site/`` is outside
``testpaths = ["tests"]`` by its own gate's design, outside every CI job, and its numbers are
hand-typed — so the one page a reader goes to in order to *avoid* reading everything else was the
one place a figure could go stale unobserved. It had: the repository-suite row read
``2720 passed`` while the suite collects more than twice that.

**Derived, never restated.** Each row below is compared against the thing that makes it true — the
replay's own report, the verifier's own comparison map, pytest's own collection — in the register
``tests/test_docs_claims.py`` already established for the README badge. Growing the corpus, adding
a compared field or adding a test module turns this file red rather than leaving a number behind
that nobody re-derived.

**A derivation has to be a property of the repository rather than of the machine.** The
regression-count row learned that the hard way: asserted against a bare collection, it passed
locally and failed on CI, because an optional extra adds 31 collectable tests. The rule it settled
on is the general one — derive over the base install, and check structurally that the exclusion is
exactly the modules an extra gates.

**Two rows cannot be derived here, and they are named rather than skipped.** ``injection`` and
``e2e`` are measurements against the *deployed* demo on a particular day: they cost real model
calls, they cannot be reproduced offline, and inventing an offline substitute for them would be
worse than leaving them alone. They are listed in :data:`OPERATOR_MEASURED` with the tool and the
date that produced them, and the **set of ledger keys is itself asserted**, so a seventh row cannot
appear without somebody deciding which of the two kinds it is.

This file does not build the site, does not run ``adapt.py`` and changes nothing about how the page
is produced. It is anti-drift and nothing else.
"""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

import pytest

from careloop.graph.verify import COMPARED_ARGUMENTS
from evals.corpus import load_corpus
from evals.replay import replay_corpus

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SITE_CONFIG: Final[Path] = REPO_ROOT / "site" / "site.toml"

#: The rows that are measurements against the deployed demo, with what produced each. They are not
#: derivable in an offline suite and are deliberately not faked; what *is* checked is that they are
#: still present and still shaped like a measurement.
OPERATOR_MEASURED: Final[dict[str, str]] = {
    "injection": "scripts/verify_lock.py, 13 poison-only runs against the deployment, 2026-08-19",
    "e2e": "scripts/verify_lock.py, 25 deployed runs, 2026-08-19",
}

#: ``<n> of <m>`` or ``<n> collected`` — the two shapes the ledger's values take.
MEASUREMENT: Final[re.Pattern[str]] = re.compile(r"^\d+ (?:of \d+|collected)$")


def ledger() -> dict[str, str]:
    return dict(tomllib.loads(SITE_CONFIG.read_text(encoding="utf-8"))["evidence"])


def counterfactual() -> dict[str, str]:
    return dict(tomllib.loads(SITE_CONFIG.read_text(encoding="utf-8"))["counterfactual"])


@pytest.fixture(scope="module")
def replayed() -> tuple[object, object]:
    """The shipped run and the allow-all mutant, from the same corpus, in one place."""

    async def both() -> tuple[object, object]:
        corpus = load_corpus()
        return await replay_corpus(corpus), await replay_corpus(corpus, permissive=True)

    return asyncio.run(both())


def collection_gated_modules() -> tuple[str, ...]:
    """Test modules whose *collection* depends on an optional extra being installed.

    A ``pytest.importorskip`` at module scope removes the whole file from collection when the
    package is absent; the same call inside a test body does not — that one is collected and skips
    at run time. Only the first kind moves a collection count, so only the first kind is found
    here, by AST rather than by grep: walking into a ``class`` or ``def`` would have swept up
    ``tests/test_llm_providers.py``, whose two calls sit inside test methods and change nothing.
    """
    gated: list[str] = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "importorskip"
                for call in ast.walk(node)
            ):
                gated.append(f"tests/{path.name}")
                break
    return tuple(gated)


@pytest.fixture(scope="module")
def collected() -> int:
    """How many tests the base install collects, asked of pytest rather than remembered.

    **The extras are excluded, and that is the whole correction.** A bare collection is not a
    property of this repository — it is a property of the machine running it. A developer with the
    optional ``schema`` extra installed collects ``tests/test_api_schema.py`` and 31 more tests
    than CI's ``check`` job, which installs ``.[dev]`` alone; pinning a published figure to the
    first would make the site red on CI and green locally, which is worse than the stale number it
    replaced. So the count is taken over the base install, which is what the ledger row's own
    ``method`` line names and what CI actually runs.
    """
    ignores = [f"--ignore={module}" for module in collection_gated_modules()]
    finished = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *ignores,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    found = re.search(r"(\d+) tests collected", finished.stdout)
    assert found is not None, finished.stdout[-2000:]
    return int(found.group(1))


class TestTheLedgerIsCompleteAndNothingSlipsIn:
    """Before any value is checked: the set of rows must be the set adapt.py knows about."""

    def test_the_keys_are_exactly_the_six_the_transform_fills(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "site"))
        try:
            from adapt import EVIDENCE_IDS
        finally:
            sys.path.pop(0)

        assert set(ledger()) == set(EVIDENCE_IDS)

    def test_every_row_is_either_derived_here_or_named_as_operator_measured(self) -> None:
        derived = {"unauthorized", "mutation", "readback", "tests"}

        assert set(ledger()) == derived | set(OPERATOR_MEASURED)

    def test_no_row_is_empty(self) -> None:
        """An empty value renders the page's honest pending state; none is pending any more."""
        assert all(value for value in ledger().values())

    def test_every_row_is_shaped_like_a_measurement(self) -> None:
        for key, value in ledger().items():
            assert MEASUREMENT.match(value), f"{key}={value!r}"


class TestTheAdversarialRowsAreTheReplaysOwnNumbers:
    """The headline and its counterfactual, re-derived from the corpus that produces them."""

    def test_the_shipped_result(self, replayed: tuple[object, object]) -> None:
        shipped, _ = replayed

        assert ledger()["unauthorized"] == (
            f"{shipped.unauthorised_actions} of {shipped.attempts}"  # type: ignore[attr-defined]
        )

    def test_the_allow_all_mutant(self, replayed: tuple[object, object]) -> None:
        _, mutant = replayed

        assert ledger()["mutation"] == (
            f"{mutant.unauthorised_actions} of {mutant.attempts}"  # type: ignore[attr-defined]
        )

    def test_the_counterfactual_rows_say_the_same_thing_as_the_ledger(self) -> None:
        """Two places on one page. Neither may move without the other."""
        assert counterfactual()["careloop"] == ledger()["unauthorized"]
        assert counterfactual()["boundary_removed"] == ledger()["mutation"]

    def test_the_contrast_is_still_a_contrast(self, replayed: tuple[object, object]) -> None:
        """Anti-vacuity: identical numbers would satisfy both rows and mean nothing."""
        shipped, mutant = replayed

        assert mutant.unauthorised_actions > shipped.unauthorised_actions  # type: ignore[attr-defined]


class TestTheVerificationRowIsTheComparisonMapsOwnSize:
    """``4 of 4`` is the number of booking fields the read-back really compares."""

    def test_it_matches_the_verifier(self) -> None:
        compared = len(COMPARED_ARGUMENTS["book_appointment"])

        assert ledger()["readback"] == f"{compared} of {compared}"

    def test_the_map_is_not_empty(self) -> None:
        """A comparison over nothing would make the row above true and worthless."""
        assert COMPARED_ARGUMENTS["book_appointment"]


class TestTheRegressionRowIsMetadataAndIsCurrent:
    """The row that was stale, and the reason it is now a collection count.

    It is deliberately *not* a safety number and is not part of any CareLoopBench domain: a total
    test count says how much there is, never that any particular property holds. What it is, is
    reliability metadata about this repository, and metadata that is wrong is worse than metadata
    that is absent — so it is derived from pytest's own collection rather than from a figure
    somebody typed after a run they no longer remember.

    Collection rather than "passed" on purpose. Whether the suite is *green* is established by CI
    on every push, which is what the row's own ``method`` line names; a number this file could
    honestly produce without running 5 000 tests is how many there are.

    **Over the base install, not over whatever the reader happens to have installed.** The first
    version of this asserted a bare collection and went red on CI within the hour: a machine
    carrying the optional ``schema`` extra collects ``tests/test_api_schema.py`` and 31 tests more
    than ``.[dev]`` does. A published figure that depends on which extras are present is not a
    measurement of this repository, so the modules whose collection is gated behind an extra are
    excluded — and :func:`collection_gated_modules` finds them structurally, so a second one cannot
    appear without :class:`TestTheExclusionIsExactlyWhatItClaims` noticing.
    """

    def test_it_is_what_pytest_collects(self, collected: int) -> None:
        assert ledger()["tests"] == f"{collected} collected"

    def test_the_collection_is_not_trivially_small(self, collected: int) -> None:
        """Anti-vacuity: a broken invocation collecting nothing must not satisfy the row."""
        assert collected > len(COMPARED_ARGUMENTS)


class TestTheDeployedRowsAreNamedRatherThanInvented:
    """What an offline suite may honestly say about a measurement it cannot reproduce."""

    def test_each_one_names_the_tool_and_the_day_that_produced_it(self) -> None:
        for key, provenance in OPERATOR_MEASURED.items():
            assert key in ledger()
            assert "verify_lock.py" in provenance
            assert re.search(r"\d{4}-\d{2}-\d{2}", provenance)

    def test_the_configuration_still_points_a_reader_at_that_tool(self) -> None:
        """The receipt beside each row must name the harness, or the provenance is only here."""
        receipts = tomllib.loads(SITE_CONFIG.read_text(encoding="utf-8"))["source_receipts"]

        for key in OPERATOR_MEASURED:
            assert receipts["evidence"][key]["path"] == "scripts/verify_lock.py"


class TestTheExclusionIsExactlyWhatItClaims:
    """The count excludes something, so what it excludes has to be checked rather than trusted."""

    def test_only_the_schema_module_is_gated_at_collection_time(self) -> None:
        """A second gated module is a decision about the published figure, not a detail."""
        assert collection_gated_modules() == ("tests/test_api_schema.py",)

    def test_the_gated_module_really_is_gated_at_module_scope(self) -> None:
        """Vacuity: if this call moved inside a test, excluding the file would drop real tests."""
        source = (REPO_ROOT / "tests" / "test_api_schema.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        at_module_scope = [
            node
            for node in tree.body
            if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "importorskip"
                for call in ast.walk(node)
            )
        ]

        assert at_module_scope

    def test_a_module_gating_inside_a_test_body_is_not_excluded(self) -> None:
        """``tests/test_llm_providers.py`` is the case that would be wrong to exclude.

        Its two ``importorskip`` calls sit inside test methods, so the file is collected either
        way and its tests skip at run time where the ``bench`` extra is absent. Excluding it would
        make the published count 72 lower than what CI collects.
        """
        assert "tests/test_llm_providers.py" not in collection_gated_modules()
