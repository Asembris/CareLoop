"""The judge-facing evidence hierarchy, held to the measurements it summarises.

``docs/judges.md`` is the one document written for somebody who has thirty seconds and a live URL.
That makes it the surface most likely to acquire a tidier sentence than the truth: a denominator
rounded into a claim about unique attacks, a percentage invented out of a ledger's numerator, a
sentence saying every AWS proof is current when two of them have deliberately never been run.

Each guard here is written the way this repository writes the others — a phrase-precise sweep with
its own vacuity half, over authored surfaces only. Three things follow from "authored":

* ``docs/evidence.md`` is **generated** by ``python -m careloopbench`` and is not swept. It carries
  its raw counts truthfully and by construction; forbidding it from stating its own numerator would
  be forbidding the ledger from being a ledger.
* ``CLAUDE.md`` and this file are not swept either, for the reason
  :mod:`tests.test_adversarial_copy` gives: both discuss the false sentences in order to forbid
  them, and a sweep that cannot tell a claim from an explanation of the claim is a sweep somebody
  deletes the first time it fires.
* Everything a judge actually reads *is* swept, and the sweep asserts it read those files rather
  than trusting a glob.

Nothing here changes what any evidence means. It stops the summaries drifting away from it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from careloop.tools.registry import REGISTRY
from careloopbench.claims import LIVE_CLAIMS
from evals.corpus import load_corpus
from evals.targets import deliveries

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
JUDGES: Final[Path] = REPO_ROOT / "docs" / "judges.md"
README: Final[Path] = REPO_ROOT / "README.md"

#: The authored surfaces a judge reads. Source, never generated output.
SWEPT: Final[tuple[tuple[str, str], ...]] = (
    ("docs", "judges.md"),
    (".", "README.md"),
    ("frontend/src", "*.ts"),
    ("frontend/src", "*.tsx"),
)

#: Test files ship in no build and no judge reads one — the exclusion
#: :mod:`tests.test_adversarial_copy` already makes, for the same reason.
NOT_A_PRODUCT_SURFACE: Final[tuple[str, ...]] = (".test.ts", ".test.tsx")


def swept_files() -> list[Path]:
    found: list[Path] = []
    for folder, pattern in SWEPT:
        base = REPO_ROOT / folder
        candidates = sorted(base.rglob(pattern)) if "*" in pattern else [base / pattern]
        found.extend(
            path
            for path in candidates
            if path.is_file() and not path.name.endswith(NOT_A_PRODUCT_SURFACE)
        )
    return found


# --------------------------------------------------------------------------------------------
# Guard 1 — the denominator may not be inflated into a claim about distinct attacks.
# --------------------------------------------------------------------------------------------

#: Sentences that turn 320 evaluated combinations into 320 separate attacks.
#:
#: The corpus holds **40** payloads. Each is replayed across every delivery route and every
#: external-effect tool, which is what makes the measurement a ceiling rather than a sample — and
#: which is exactly what these phrasings erase. "320 attacks" is included bare, because a reader
#: has no way to hear it as anything but 320 of them.
INFLATED: Final[tuple[tuple[str, str], ...]] = (
    (
        r"\b320\b[^.]{0,24}?\b(?:unique|distinct|different|separate|individual)\b",
        "claims 320 attacks that differ from one another; there are 40 payloads",
    ),
    (
        r"\b(?:unique|distinct|different|separate|individual)\b[^.]{0,24}?\b320\b",
        "claims 320 attacks that differ from one another; there are 40 payloads",
    ),
    (
        r"\b320\b\s+(?:prompt[- ]injections?|exploits?|payloads?|attacks?|vulnerabilities)\b",
        "counts 320 of something the corpus has 40 of",
    ),
)


class TestTheAdversarialDenominatorIsNotInflated:
    """320 is a cross product. Every phrasing that reads it as a headcount is refused."""

    @pytest.mark.parametrize("path", swept_files(), ids=lambda path: path.name)
    def test_no_judge_surface_counts_320_separate_attacks(self, path: Path) -> None:
        body = path.read_text(encoding="utf-8")
        for pattern, why in INFLATED:
            found = re.search(pattern, body, re.IGNORECASE)
            assert found is None, f"{path.name}: {found.group(0)!r} {why}"  # type: ignore[union-attr]

    def test_the_sweep_is_reading_the_surfaces_it_claims_to(self) -> None:
        """Vacuity. A sweep over an empty file list passes over anything."""
        names = {path.name for path in swept_files()}

        assert "judges.md" in names
        assert "README.md" in names
        assert len(swept_files()) > 20

    @pytest.mark.parametrize(
        "sentence",
        [
            "CareLoop refused 320 unique attacks.",
            "320 distinct attacks were evaluated.",
            "We ran 320 different attacks through the graph.",
            "320 prompt injections, none of which reached an effect.",
            "The suite replays 320 exploits.",
            "320 attacks, zero unauthorized actions.",
            "It survived 320 separate attacks.",
        ],
    )
    def test_an_inflated_sentence_is_caught(self, sentence: str) -> None:
        """The other half. A guard nothing can trip is a guard over nothing."""
        assert any(re.search(pattern, sentence, re.IGNORECASE) for pattern, _ in INFLATED)

    @pytest.mark.parametrize(
        "sentence",
        [
            "40 hostile payloads x 2 delivery routes x 4 external-effect tools = 320 evaluated "
            "combinations.",
            "Unauthorized external actions: 0 of 320 adversarial attempts.",
            "With the authority layer removed and nothing else changed: 320 of 320.",
            "320 evaluated combinations, and not one produced an external effect.",
            "The same 320 attempts produce the opposite result.",
        ],
    )
    def test_a_true_sentence_survives(self, sentence: str) -> None:
        """The sweep has to leave the accurate phrasings alone, or it gets deleted."""
        assert not any(re.search(pattern, sentence, re.IGNORECASE) for pattern, _ in INFLATED)


# --------------------------------------------------------------------------------------------
# Guard 2 — the decomposition is derived from the corpus, the routes and the registry.
# --------------------------------------------------------------------------------------------


def decomposition() -> tuple[int, int, int]:
    """``(payloads, delivery routes, external-effect tools)``, asked of the harness itself."""
    return len(load_corpus()), len(deliveries()), len(REGISTRY.external_effect_tools)


class TestTheDecompositionIsWhatTheHarnessActuallyDoes:
    """``40 x 2 x 4 = 320`` is three measurements and a multiplication, not four constants."""

    def test_the_three_factors_multiply_to_the_published_denominator(self) -> None:
        payloads, routes, tools = decomposition()

        assert payloads * routes * tools == 320

    def test_the_judge_guide_states_each_factor(self) -> None:
        """Growing the corpus, adding a route or registering a fifth effect tool turns this red."""
        payloads, routes, tools = decomposition()
        body = JUDGES.read_text(encoding="utf-8")

        stated = re.search(
            r"(\d+) hostile payloads .{0,4} (\d+) delivery routes .{0,4} "
            r"(\d+) external-effect tools = \*\*(\d+) evaluated combinations\*\*",
            body,
        )
        assert stated is not None, "the judge guide no longer decomposes the denominator"
        assert tuple(int(g) for g in stated.groups()) == (
            payloads,
            routes,
            tools,
            payloads * routes * tools,
        )

    def test_none_of_the_factors_is_trivial(self) -> None:
        """A cross product with a 1 in it is not a cross product."""
        assert all(factor > 1 for factor in decomposition())


# --------------------------------------------------------------------------------------------
# Guard 3 — the guide's route onward has to resolve.
# --------------------------------------------------------------------------------------------

#: The destinations a judge must be able to reach from the guide, and why each is load-bearing.
#:
#: A *set* rather than a count. "At least fifteen links" is numerology: it passes on fifteen links
#: to the wrong places and fails on fourteen to the right ones.
REQUIRED_DESTINATIONS: Final[dict[str, str]] = {
    "../README.md": "the system in one page",
    "security-evaluation.md": "how the adversarial result is produced and what it does not cover",
    "evidence.md": "the property ledger",
    "architecture.md": "the trust zones and the read-back",
    "agentcore-evaluations-proof.md": "the AWS evaluation and its limits",
}


def links_in(body: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"\]\(([^)\s]+)\)", body)]


class TestTheJudgeGuideRoutesOnward:
    """A guided tour that dead-ends is a guided tour that hides the evidence behind it."""

    @pytest.mark.parametrize("destination", sorted(REQUIRED_DESTINATIONS))
    def test_it_is_linked(self, destination: str) -> None:
        targets = {link.split("#")[0] for link in links_in(JUDGES.read_text(encoding="utf-8"))}

        assert destination in targets, (
            f"docs/judges.md no longer links {destination} ({REQUIRED_DESTINATIONS[destination]})"
        )

    @pytest.mark.parametrize("path", [JUDGES, README], ids=lambda p: p.name)
    def test_every_relative_link_resolves(self, path: Path) -> None:
        broken = [
            link
            for link in links_in(path.read_text(encoding="utf-8"))
            if not link.startswith(("http://", "https://", "#"))
            and not (path.parent / link.split("#")[0]).exists()
        ]

        assert broken == [], f"{path.name} links files that are not there: {broken}"

    def test_the_resolver_would_notice_a_broken_one(self) -> None:
        """Vacuity for the pair above: the existence check has to be able to fail."""
        assert not (JUDGES.parent / "no-such-document.md").exists()

    def test_the_readme_offers_the_ledger(self) -> None:
        """The one inbound edge CareLoopBench had none of before this."""
        assert "](docs/evidence.md)" in README.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Guard 4 — the ledger's numerator is not a safety percentage.
# --------------------------------------------------------------------------------------------

#: Readings of the property ledger that turn its numerator into a grade.
#:
#: ``63 of 67`` counts claims whose evidence ran and passed *in one invocation*. ``PROVEN_AT``,
#: ``STALE``, ``NOT_RUN`` and ``NOT_APPLICABLE`` are none of them a failure, so a denominator that
#: includes them is not a pass rate and the difference is not a count of defects.
MISREAD_LEDGER: Final[tuple[tuple[str, str], ...]] = (
    (
        r"\b9[0-9](?:\.\d+)?\s?%\s?(?:safe|proven|secure|passing|coverage)\b",
        "invents a pass rate out of a denominator that is not one",
    ),
    (
        r"\b63\s?(?:of|/)\s?67\b[^.]{0,24}?\b(?:safe|secure|proven safe|pass rate)\b",
        "grades the ledger",
    ),
    (
        r"\b63\s?(?:of|/)\s?67\s+tests\b",
        "calls declared properties tests",
    ),
    (
        r"\b(?:the\s+)?(?:remaining\s+)?4\s+(?:failures|failing|failed)\b",
        "calls the remainder failures; not one of the four is a failure",
    ),
)


class TestTheLedgerCannotBeGradedOnAJudgeSurface:
    """The ledger may state its own counts. A summary of it may not turn them into a score."""

    @pytest.mark.parametrize("path", swept_files(), ids=lambda path: path.name)
    def test_no_authored_surface_reads_it_as_a_percentage(self, path: Path) -> None:
        body = path.read_text(encoding="utf-8")
        for pattern, why in MISREAD_LEDGER:
            found = re.search(pattern, body, re.IGNORECASE)
            assert found is None, f"{path.name}: {found.group(0)!r} {why}"  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "sentence",
        [
            "CareLoop is 94% safe.",
            "The bench reports 94% proven.",
            "63/67 safe, which is most of it.",
            "63 of 67 tests pass.",
            "The remaining 4 failures are live proofs.",
        ],
    )
    def test_a_graded_sentence_is_caught(self, sentence: str) -> None:
        assert any(re.search(pattern, sentence, re.IGNORECASE) for pattern, _ in MISREAD_LEDGER)

    def test_the_generated_ledger_is_deliberately_not_swept(self) -> None:
        """It states its own numerator, truthfully, and must go on being able to."""
        assert (REPO_ROOT / "docs" / "evidence.md").exists()
        assert all(path.name != "evidence.md" for path in swept_files())


# --------------------------------------------------------------------------------------------
# Guard 5 — a proof nobody has re-run may not be summarised as current.
# --------------------------------------------------------------------------------------------


def live_proofs_without_a_stored_record() -> tuple[str, ...]:
    """Declared live proofs with no envelope on disk, read from the registry rather than a doc."""
    return tuple(
        claim.artifact.proof_id
        for claim in LIVE_CLAIMS
        if claim.artifact is not None and not (REPO_ROOT / claim.artifact.path).exists()
    )


#: Summaries that would be false while any live proof is unrun. Narrow on purpose: this is a
#: statement about *all* of them, which is the only shape a normal sentence never accidentally has.
CLAIMS_EVERYTHING_IS_CURRENT: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(?:all|every)\b[^.]{0,32}?\blive proofs?\b[^.]{0,24}?\b(?:current|fresh)\b", re.I
    ),
    re.compile(
        r"\bevery\b[^.]{0,24}?\bAWS proof\b[^.]{0,32}?\b(?:refreshed|current|re-?run)\b", re.I
    ),
    re.compile(r"\b(?:all|every)\b[^.]{0,32}?\bproduction proofs?\b[^.]{0,24}?\bcurrent\b", re.I),
)


class TestUnrunLiveProofAreNotSummarisedAsCurrent:
    """The freshness statuses stay canonical. What is checked is the prose beside them."""

    def test_there_really_is_something_unrun(self) -> None:
        """Vacuity, and a live inventory: this guard is only meaningful while one is pending."""
        assert live_proofs_without_a_stored_record(), (
            "every declared live proof now has a stored record. Keep this guard, but it is no "
            "longer the thing standing between the docs and an overclaim — say so here."
        )

    @pytest.mark.parametrize("path", swept_files(), ids=lambda path: path.name)
    def test_no_authored_surface_says_they_all_are(self, path: Path) -> None:
        if not live_proofs_without_a_stored_record():
            pytest.skip("nothing is pending, so nothing can be overclaimed about")
        body = path.read_text(encoding="utf-8")
        for rule in CLAIMS_EVERYTHING_IS_CURRENT:
            found = rule.search(body)
            assert found is None, (
                f"{path.name}: {found.group(0)!r} while "  # type: ignore[union-attr]
                f"{live_proofs_without_a_stored_record()} has no stored record"
            )

    @pytest.mark.parametrize(
        "sentence",
        [
            "All live proofs are current.",
            "Every AWS proof has been refreshed.",
            "All production proofs are current as of today.",
            "Every live proof is fresh.",
        ],
    )
    def test_an_overclaim_is_caught(self, sentence: str) -> None:
        assert any(rule.search(sentence) for rule in CLAIMS_EVERYTHING_IS_CURRENT)

    @pytest.mark.parametrize(
        "sentence",
        [
            "Each live row names the commit it certifies, and one has never been run.",
            "A live proof can be stale because the code it depended on has moved.",
            "The object-store contract was measured against real Amazon S3.",
            "Live AWS proofs carry their own freshness.",
        ],
    )
    def test_honest_prose_is_left_alone(self, sentence: str) -> None:
        assert not any(rule.search(sentence) for rule in CLAIMS_EVERYTHING_IS_CURRENT)


# --------------------------------------------------------------------------------------------
# Guard 6 — the zero never appears without the counterfactual that gives it meaning.
# --------------------------------------------------------------------------------------------

#: The shipped result, in every spelling a judge surface uses.
ZERO: Final[re.Pattern[str]] = re.compile(r"\b0\s?(?:of|/)\s?320\b")

#: The allow-all mutant's result, likewise.
MUTANT: Final[re.Pattern[str]] = re.compile(r"\b320\s?(?:of|/)\s?320\b")


class TestTheZeroIsNeverShownAlone:
    """Zero is what a broken harness reports too. The contrast is what makes it evidence."""

    @pytest.mark.parametrize("path", swept_files(), ids=lambda path: path.name)
    def test_a_surface_stating_the_zero_states_the_mutant_too(self, path: Path) -> None:
        body = path.read_text(encoding="utf-8")
        if ZERO.search(body) is None:
            pytest.skip("this surface does not state the shipped result")

        assert MUTANT.search(body) is not None, (
            f"{path.name} states 0 of 320 and not the allow-all result beside it. The zero on its "
            "own is also what an empty corpus and a broken harness report."
        )

    def test_the_surfaces_that_state_it_are_the_ones_expected(self) -> None:
        """Vacuity: a skip-everything version of the test above would pass silently."""
        stating = {
            path.name for path in swept_files() if ZERO.search(path.read_text(encoding="utf-8"))
        }

        assert {"judges.md", "README.md"} <= stating, stating

    def test_the_panels_own_data_carries_both_halves(self) -> None:
        """The product renders the pair out of one object, so the pairing is structural there.

        ``SecurityEvidencePanel`` composes both numerals from ``SECURITY_EVIDENCE`` rather than
        from literals, which is why the sweep above finds no ``0 of 320`` under ``frontend/src``.
        What has to hold instead is that the *source* of both numerals is still one object holding
        both fields — delete either and the panel cannot render the contrast.
        """
        source = (REPO_ROOT / "frontend" / "src" / "data" / "trust.ts").read_text(encoding="utf-8")

        assert "shippedUnauthorized" in source
        assert "withoutAuthorityUnauthorized" in source
