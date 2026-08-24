"""Every repository path this source cites has to exist.

This project explains itself by pointing at evidence. A docstring says *what* is true and then names
the file that measures it, and a reader — or a later author deciding whether a declaration is still
earned — follows the pointer. A citation that does not resolve is worse than no citation: it reads
as proof, and there is nothing behind it.

**It happened to the load-bearing one.** :class:`careloop.graph.recovery.RecoveryClass`'s
``IDEMPOTENT_REPLAYABLE`` member is the declaration that permits an automatic second dispatch of a
real external effect, and it cited a clinic replay test — twice, in the member docstring and beside
:data:`~careloop.graph.recovery.RECOVERY_CLASS` — for the clinic half of that claim. No such file
has ever existed. The measurement is real and lives in ``tests/test_demoworld.py::TestIdempotency``;
only the pointer was wrong, which is exactly why nothing noticed.
``careloop/tools/document_source.py`` carried a second instance of the same shape.

**Scope: ``.py`` and ``.md`` only, and that is a decision rather than a convenience.** Evidence in
this repository is a test or a measurement write-up, so those are the two extensions a citation can
have. Anything else path-shaped in source — a unit file, a shell script, a remote path a deploy
writes on the target host — is a *location*, not a proof, and sweeping it turns this into a parser
that has to understand prose. ``deploy/push.sh`` is the concrete case: two deploy modules discuss
what that script used to do, correctly, about a file removed when the deploy moved to SSM.

The match is deliberately dumb — a known top-level directory, a path body, one of two extensions —
so it cannot be argued with. A ``::ClassName`` suffix is left off the end by the extension boundary,
which is what lets a citation name the exact test class without naming a file that does not exist.
Globs and brace forms match nothing, because the characters they need are outside the path body.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: The trees a citation may name. Everything in the repository, so a pointer cannot escape the
#: sweep by aiming somewhere unusual.
CITED_ROOTS: Final[tuple[str, ...]] = (
    "bench",
    "careloop",
    "demoworld",
    "deploy",
    "docs",
    "e2e",
    "evals",
    "evaluators",
    "frontend",
    "objectstore",
    "scripts",
    "tests",
)

#: What a citation looks like. See the module docstring for why the extensions are these two.
CITATION: Final[re.Pattern[str]] = re.compile(
    r"\b((?:" + "|".join(CITED_ROOTS) + r")/[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:py|md))"
)

#: The source trees swept. Every Python package this repository ships, test code excluded — a test
#: naming a fixture it builds is not making a claim about where a property is proven.
SWEPT_TREES: Final[tuple[str, ...]] = (
    "careloop",
    "demoworld",
    "evals",
    "evaluators",
    "objectstore",
    "scripts",
)

#: The floor the sweep must clear before its "nothing is broken" is worth anything.
#:
#: A test that only checked "every citation found resolves" passes on a repository that has lost
#: its citations altogether, because an empty set satisfies every assertion over it. The count was
#: 152 when this was written; the floor is set below that so ordinary editing does not trip it and
#: far enough above zero that a collapse would.
MINIMUM_CITATIONS: Final[int] = 120


def swept_files() -> tuple[Path, ...]:
    """Every module the sweep reads."""
    found: list[Path] = []
    for tree in SWEPT_TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            found.append(path)
    return tuple(found)


def citations_in(text: str) -> frozenset[str]:
    """Every repository path ``text`` cites, deduplicated."""
    return frozenset(CITATION.findall(text))


def broken_citations() -> dict[str, list[str]]:
    """Cited paths that do not exist, keyed by the module citing them."""
    broken: dict[str, list[str]] = {}
    for path in swept_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        unresolved = sorted(
            cited for cited in citations_in(text) if not (REPO_ROOT / cited).exists()
        )
        if unresolved:
            broken[path.relative_to(REPO_ROOT).as_posix()] = unresolved
    return broken


def total_citations() -> int:
    """How many citations the sweep is checking, counted per module."""
    return sum(
        len(citations_in(path.read_text(encoding="utf-8", errors="replace")))
        for path in swept_files()
    )


class TestEveryCitationResolves:
    def test_nothing_points_at_a_file_that_does_not_exist(self) -> None:
        assert broken_citations() == {}

    def test_the_sweep_is_checking_enough_to_mean_something(self) -> None:
        """Anti-vacuity. An empty sweep satisfies the assertion above and proves nothing."""
        assert total_citations() >= MINIMUM_CITATIONS

    def test_it_is_reading_the_modules_it_claims_to(self) -> None:
        swept = {path.relative_to(REPO_ROOT).as_posix() for path in swept_files()}

        assert "careloop/graph/recovery.py" in swept
        assert "careloop/tools/document_source.py" in swept
        assert "evaluators/settlement.py" in swept


class TestTheSweepWouldNotice:
    """Vacuity, from the other end: a fabricated citation has to be reported."""

    def test_a_fabricated_test_file_is_caught(self) -> None:
        assert citations_in("see ``tests/test_nothing_measures_this.py`` for the proof") == {
            "tests/test_nothing_measures_this.py"
        }
        assert not (REPO_ROOT / "tests/test_nothing_measures_this.py").exists()

    def test_a_real_citation_is_recognised_and_resolves(self) -> None:
        found = citations_in("``docs/objectstore-contract.md`` records the measurement")

        assert found == {"docs/objectstore-contract.md"}
        assert (REPO_ROOT / "docs/objectstore-contract.md").exists()

    def test_a_class_suffix_does_not_become_part_of_the_path(self) -> None:
        """A citation may name the exact test class without naming a file called that."""
        assert citations_in("``tests/test_demoworld.py::TestIdempotency`` measures all three") == {
            "tests/test_demoworld.py"
        }

    def test_globs_and_brace_forms_are_not_citations(self) -> None:
        """A pattern names a set of files; only a path names one, and only one can be checked."""
        assert citations_in("careloop/tools/fixtures/*.pdf") == frozenset()
        assert citations_in("docs/architecture.{png,pdf}") == frozenset()


class TestTheCapabilityEvidenceIsNamedCorrectly:
    """The instance this sweep was written for, pinned by name.

    The recovery class is the declaration that lets CareLoop re-dispatch a real external effect on
    its own. Both halves of it cite a measurement, and both citations have to land.
    """

    @staticmethod
    def _recovery_source() -> str:
        return (REPO_ROOT / "careloop/graph/recovery.py").read_text(encoding="utf-8")

    def test_the_clinic_contract_is_cited_where_it_is_actually_measured(self) -> None:
        source = self._recovery_source()

        assert "tests/test_demoworld.py" in source
        assert "test_clinic_replay" not in source

    def test_the_object_store_contract_is_cited_too(self) -> None:
        source = self._recovery_source()

        assert "docs/objectstore-contract.md" in source
        assert "tests/test_objectstore_contract.py" in source
