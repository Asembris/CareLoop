"""The published numbers, asserted against the measurement that produced them.

``README.md`` carries a shields.io badge reading ``0 / 320``, and both it and
``docs/security-evaluation.md`` state the figure in prose. A badge is a static string: nothing
stops the corpus growing, or a rule changing, and leaving a number behind that nobody re-derived.
That is precisely the shape of claim this project refuses everywhere else — a figure somebody
wrote down once, still being shown to a reader long after it stopped being true.

So the numbers are not compared against a constant here. They are compared against
:func:`evals.replay.replay_corpus` run on the committed corpus, which is the same call
``python -m evals.replay`` and :mod:`tests.test_evals_replay` make. Grow the corpus and this file
goes red; the README cannot go quietly stale instead.

**Two claims, and the second is what stops this passing vacuously.** A test that only checked
"every number found matches the measurement" would pass on a README that had lost the numbers
altogether, because an empty set of matches satisfies every assertion over it. So the count of
matches is asserted too: the claim must still be *present* before it can be checked.

**The animated hero states the headline as well, and an SVG is the easiest place of all for a
number to go stale.** ``docs/readme-hero.svg`` carries ``0 / 320 UNAUTHORIZED EFFECTS`` in its
evidence strip, above the fold, where it is the first evidence a judge sees and the last thing
anybody would think to re-derive. It is held to the same standard as the badge and the prose: the
figure is compared against :func:`evals.replay.replay_corpus`, not against a constant. Unlike the
prose, the hero is checked against the **shipped** result only - it is not a place where the
allow-all mutant's number would ever be a correct thing to display.

The hero is read as *text* rather than as markup, so restyling the strip cannot quietly move the
number out of range of the assertion, and the README is checked to still reference the file - a
guard over an asset nothing displays would be a guard over nothing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Final

import pytest

from evals.corpus import load_corpus
from evals.replay import replay_corpus

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
README: Final[Path] = REPO_ROOT / "README.md"
REPORT: Final[Path] = REPO_ROOT / "docs" / "security-evaluation.md"
HERO: Final[Path] = REPO_ROOT / "docs" / "readme-hero.svg"

#: The badge's two numbers, as shields.io encodes them: ``0%20%2F%20320`` is ``0 / 320``.
BADGE: Final[re.Pattern[str]] = re.compile(r"unauthorized%20actions-(\d+)%20%2F%20(\d+)-")

#: Every prose or console statement of the headline, in either document.
HEADLINE: Final[re.Pattern[str]] = re.compile(r"(\d+) of (\d+) adversarial attempts")

#: The hero's evidence strip, matched against the SVG's *text* rather than its markup. The number
#: is a ``<tspan>`` today so it can be set larger than the words beside it; matching the markup
#: would tie this assertion to that styling decision and go quiet the moment somebody changed it.
HERO_HEADLINE: Final[re.Pattern[str]] = re.compile(r"(\d+) / (\d+) UNAUTHORIZED EFFECTS")


def hero_text() -> str:
    """The hero's rendered text: comments dropped, then every tag removed.

    Comments go first and separately. The file documents its own palette and timeline in a long
    header comment, and text that is never drawn must not be able to satisfy - or break - an
    assertion about what a reader sees.
    """
    without_comments = re.sub(r"<!--.*?-->", " ", HERO.read_text(encoding="utf-8"), flags=re.DOTALL)
    return re.sub(r"<[^>]+>", "", without_comments)


#: Below this, the documents have stopped making the claim rather than making it correctly.
MINIMUM_HEADLINE_STATEMENTS: Final[int] = 3


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def shipped(corpus):
    """What the shipped ``rules.yaml`` actually produces. The badge's numerator and denominator."""
    return asyncio.run(replay_corpus(corpus))


@pytest.fixture(scope="module")
def mutant(corpus):
    """The same replay with the authority layer removed, which the README also quotes."""
    return asyncio.run(replay_corpus(corpus, permissive=True))


class TestTheSecurityBadgeCannotDrift:
    """The README's badge is the measurement, not a number resembling it."""

    def test_the_badge_is_present(self) -> None:
        assert BADGE.search(README.read_text(encoding="utf-8")), (
            "README.md no longer carries the unauthorized-actions badge. If it was removed "
            "deliberately, remove this assertion in the same commit; do not leave a test that "
            "passes because there is nothing left to check."
        )

    def test_the_badge_matches_the_replay(self, shipped) -> None:
        found = BADGE.search(README.read_text(encoding="utf-8"))
        assert found is not None
        numerator, denominator = int(found.group(1)), int(found.group(2))

        assert (numerator, denominator) == (shipped.unauthorised_actions, shipped.attempts), (
            f"README badge says {numerator} / {denominator}; the replay measures "
            f"{shipped.unauthorised_actions} / {shipped.attempts}. Update the badge — the "
            "measurement is the source of truth, not the other way round."
        )


class TestEveryPublishedHeadlineMatches:
    """Both documents state the figure in words. Every statement of it must be a measured one."""

    @pytest.mark.parametrize("document", [README, REPORT], ids=lambda p: p.name)
    def test_the_claim_is_still_made(self, document: Path) -> None:
        statements = HEADLINE.findall(document.read_text(encoding="utf-8"))
        assert len(statements) >= 1, f"{document.name} no longer states the headline at all."

    def test_the_documents_state_it_enough_to_be_worth_checking(self) -> None:
        total = sum(
            len(HEADLINE.findall(path.read_text(encoding="utf-8"))) for path in (README, REPORT)
        )
        assert total >= MINIMUM_HEADLINE_STATEMENTS, (
            f"Only {total} statement(s) of the headline across both documents. The assertions "
            "below are satisfied by an empty set, so this count is what stops them passing "
            "vacuously."
        )

    @pytest.mark.parametrize("document", [README, REPORT], ids=lambda p: p.name)
    def test_every_statement_is_one_of_the_two_measured_results(
        self, document: Path, shipped, mutant
    ) -> None:
        measured = {
            (shipped.unauthorised_actions, shipped.attempts),
            (mutant.unauthorised_actions, mutant.attempts),
        }
        for numerator, denominator in HEADLINE.findall(document.read_text(encoding="utf-8")):
            claimed = (int(numerator), int(denominator))
            assert claimed in measured, (
                f"{document.name} claims {claimed[0]} of {claimed[1]} adversarial attempts. "
                f"The replay measures {sorted(measured)} — shipped rules and the allow-all "
                "mutant. Nothing else is a result."
            )


class TestTheHeroCannotDriftEither:
    """The number a judge sees first is the number the replay produced."""

    def test_the_hero_still_states_the_headline(self) -> None:
        assert HERO_HEADLINE.search(hero_text()), (
            "docs/readme-hero.svg no longer states the unauthorized-effects headline. If the "
            "evidence strip was removed deliberately, remove this class in the same commit; do "
            "not leave a test that passes because there is nothing left to check."
        )

    def test_the_hero_matches_the_replay(self, shipped) -> None:
        found = HERO_HEADLINE.search(hero_text())
        assert found is not None
        numerator, denominator = int(found.group(1)), int(found.group(2))

        assert (numerator, denominator) == (shipped.unauthorised_actions, shipped.attempts), (
            f"docs/readme-hero.svg says {numerator} / {denominator}; the replay measures "
            f"{shipped.unauthorised_actions} / {shipped.attempts}. Update the hero - the "
            "measurement is the source of truth, not the other way round."
        )

    def test_the_readme_still_shows_the_hero(self) -> None:
        assert "docs/readme-hero.svg" in README.read_text(encoding="utf-8"), (
            "README.md no longer references docs/readme-hero.svg, so the assertions above now "
            "guard a claim nobody is being shown. Point them at whatever replaced it, or remove "
            "them with the hero."
        )
