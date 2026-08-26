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


# -- how many effects are real ---------------------------------------------------------------


#: Where the architecture states how many effects reach a real provider, in prose a reader sees.
#:
#: ``docs/session-log.md`` is deliberately absent, for ``tests/test_upload_vocabulary.py``'s reason:
#: it is a record of what was decided at the time, and editing history so a guard passes falsifies
#: the one document whose value is that it was not edited afterwards.
EFFECT_COUNT_DOCUMENTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs" / "architecture.md",
)

#: A sentence saying how many effects are simulated. Number words, because that is how the prose
#: reads — "the other two effects are simulated" — and a digit here would match nothing.
SIMULATED_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"\b(one|two|three|four)\b[^.`]{0,40}?\beffects?\b[^.]{0,80}?\b(?:are|stay|remain)\b"
    r"[^.]{0,20}?\bsimulated\b",
    re.IGNORECASE,
)

#: A sentence saying how many tools are real.
REAL_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"\b(one|two|three|four)\b\s+tools?\s+(?:is|are)\s+real", re.IGNORECASE
)

NUMBER_WORDS: Final[dict[str, int]] = {"one": 1, "two": 2, "three": 3, "four": 4}


def simulated_effect_count() -> int:
    """How many effect tools have no real provider, derived from the declarations.

    ``REGISTRY.external_effect_tools`` minus
    :data:`careloop.graph.verify.INDEPENDENTLY_OBSERVABLE`. Derived rather than written down,
    because a number in prose is exactly the thing that goes stale — this one did, and said three
    for as long as it took somebody to read it beside the code.
    """
    from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE
    from careloop.tools.registry import REGISTRY

    return len(REGISTRY.external_effect_tools - INDEPENDENTLY_OBSERVABLE)


def real_effect_count() -> int:
    """How many effect tools reach a real, independently observable provider."""
    from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE

    return len(INDEPENDENTLY_OBSERVABLE)


def _claims(pattern: re.Pattern[str], document: Path) -> list[int]:
    return [
        NUMBER_WORDS[found.group(1).lower()]
        for found in pattern.finditer(document.read_text(encoding="utf-8"))
    ]


class TestTheDocumentedEffectCountsAreDerivedFromTheDeclarations:
    """The architecture pages must not go on saying one tool is real after a second one lands.

    **This is the stale claim that motivated the guard.** ``docs/architecture.md`` said *"the other
    three effects are simulated"* and never mentioned the object store at all, and ``CLAUDE.md``
    opened the read-back section with *"one tool is real"* — both true before ``upload_document``
    gained a provider, both false afterwards, and neither attached to anything that could notice.

    Checked against ``REGISTRY.external_effect_tools`` and ``INDEPENDENTLY_OBSERVABLE`` rather than
    against a constant, so a third real provider turns these red instead of leaving the pages
    quietly wrong.
    """

    @pytest.mark.parametrize("document", EFFECT_COUNT_DOCUMENTS, ids=lambda path: path.name)
    def test_the_simulated_count_matches_the_declarations(self, document: Path) -> None:
        stated = _claims(SIMULATED_CLAIM, document)

        assert stated, (
            f"{document.name} no longer says how many effects are simulated. If the sentence was "
            "removed deliberately, remove it from EFFECT_COUNT_DOCUMENTS in the same commit; do "
            "not leave an assertion that passes because there is nothing left to check."
        )
        assert set(stated) == {simulated_effect_count()}

    @pytest.mark.parametrize("document", EFFECT_COUNT_DOCUMENTS, ids=lambda path: path.name)
    def test_no_document_still_claims_a_single_real_provider(self, document: Path) -> None:
        assert set(_claims(REAL_CLAIM, document)) <= {real_effect_count()}

    def test_the_two_counts_partition_the_effect_tools(self) -> None:
        """Anti-vacuity for the derivation itself, not for the prose."""
        from careloop.tools.registry import REGISTRY

        assert real_effect_count() + simulated_effect_count() == len(REGISTRY.external_effect_tools)
        assert real_effect_count() == 2
        assert simulated_effect_count() == 2

    def test_the_patterns_read_the_sentences_they_are_aimed_at(self) -> None:
        """Vacuity from the other end: the wrong prose must match, and be read correctly."""
        assert _claims(SIMULATED_CLAIM, REPO_ROOT / "CLAUDE.md")

        was_wrong = "the other three effects stay simulated, because one action"
        assert [
            NUMBER_WORDS[found.group(1).lower()] for found in SIMULATED_CLAIM.finditer(was_wrong)
        ] == [3]
        assert [
            NUMBER_WORDS[found.group(1).lower()]
            for found in REAL_CLAIM.finditer("**One tool is real, and the read-back**")
        ] == [1]

    def test_the_evaluator_claim_is_not_widened_by_any_of_this(self) -> None:
        """``CareLoopSettlementIntegrity`` stays booking-scoped, whatever the docs now say.

        Two real providers is an architecture claim; what AWS evaluates is a separate, narrower one,
        and the second must not drift on the strength of the first.
        """
        from evaluators.contract import OBSERVABLE_EFFECT_TYPES

        assert frozenset({"book_appointment"}) == OBSERVABLE_EFFECT_TYPES
        assert "NOT_APPLICABLE" in (REPO_ROOT / "docs" / "architecture.md").read_text(
            encoding="utf-8"
        )


class TestTheParkingClaimNamesOnlyTheUnreplayableEffects:
    """The README must not go on listing a replayable effect among the ones that park.

    **This is the stale claim that motivated the guard.** ``README.md`` said *"Only
    ``book_appointment`` can be autonomously recovered after an ambiguous outcome"* and named
    ``upload_document`` beside ``send_email`` and ``grant_portal_access`` as parking with
    ``EXTERNAL_OUTCOME_NOT_ESTABLISHED`` — true before the object store gained a provider, false
    from the moment :data:`careloop.graph.recovery.RECOVERY_CLASS` declared it
    ``IDEMPOTENT_REPLAYABLE``, and attached to nothing that could notice.

    Read from the declaration rather than from a constant, so a third replayable provider turns
    this red instead of leaving the page quietly wrong.
    """

    @staticmethod
    def _parking_sentence(text: str) -> str:
        """The sentence stating what parks, or ``""``. Sentence-scoped so the rest is not swept."""
        for sentence in re.split(r"(?<=[.])\s", text):
            if "EXTERNAL_OUTCOME_NOT_ESTABLISHED" in sentence:
                return sentence
        return ""

    @staticmethod
    def _unreplayable() -> frozenset[str]:
        from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass

        return frozenset(
            name
            for name, klass in RECOVERY_CLASS.items()
            if klass is not RecoveryClass.IDEMPOTENT_REPLAYABLE
        )

    def test_the_readme_still_states_what_parks(self) -> None:
        """Anti-vacuity: an absent sentence satisfies every assertion made over it."""
        assert self._parking_sentence(README.read_text(encoding="utf-8")), (
            "README.md no longer says which effects park with EXTERNAL_OUTCOME_NOT_ESTABLISHED. "
            "If the sentence went deliberately, remove this test in the same commit; do not leave "
            "an assertion that passes because there is nothing left to check."
        )

    def test_it_names_exactly_the_unreplayable_effects(self) -> None:
        from careloop.graph.recovery import RECOVERY_CLASS

        sentence = self._parking_sentence(README.read_text(encoding="utf-8"))
        named = frozenset(name for name in RECOVERY_CLASS if name in sentence)

        assert named == self._unreplayable()

    def test_the_stale_sentence_is_caught(self) -> None:
        """Vacuity from the other end: the wording this guard exists for must fail it."""
        was_wrong = (
            "`send_email`, `upload_document` and `grant_portal_access` park for a person with "
            "`EXTERNAL_OUTCOME_NOT_ESTABLISHED` instead."
        )
        from careloop.graph.recovery import RECOVERY_CLASS

        sentence = self._parking_sentence(was_wrong)

        named = frozenset(name for name in RECOVERY_CLASS if name in sentence)

        assert named != self._unreplayable()


class TestTheDenominatorIsDecomposedWhereTheFrontendReadsIt:
    """``320`` is a cross product, and the document that says so is now load-bearing.

    ``My Boundaries`` renders *"40 hostile payloads x 2 delivery routes x 4 external-effect tools =
    320 evaluated combinations"* from ``SECURITY_EVIDENCE``, and ``frontend/src/data/trust.test.ts``
    pins those four constants to the one sentence in :data:`REPORT` that states them. That makes
    the sentence a hand-carried figure like every other, so it is checked the same way: against the
    corpus, the delivery routes and the registry rather than against itself.

    Grow the corpus, add a delivery route or register a fifth external-effect tool and this turns
    red — which is the whole point, because the product would otherwise go on multiplying three
    numbers that no longer describe the run.
    """

    #: The document's own decomposition, as one sentence, spelled as it is written.
    SPREAD: Final[re.Pattern[str]] = re.compile(
        r"\*\*(\d+) delivery vectors \u00d7 (\d+) external-effect\s+tools\*\* \u2014 "
        r"so (\d+) payloads become (\d+) attempts"
    )

    def factors(self) -> tuple[int, int, int, int]:
        found = self.SPREAD.search(REPORT.read_text(encoding="utf-8"))
        assert found is not None, "docs/security-evaluation.md no longer decomposes 320"
        routes, tools, payloads, attempts = (int(group) for group in found.groups())
        return payloads, routes, tools, attempts

    def test_each_factor_is_what_the_harness_actually_uses(self) -> None:
        from careloop.tools.registry import REGISTRY
        from evals.targets import deliveries

        payloads, routes, tools, _ = self.factors()

        assert payloads == len(load_corpus())
        assert routes == len(deliveries())
        assert tools == len(REGISTRY.external_effect_tools)

    def test_the_factors_multiply_to_the_published_attempts(self, shipped) -> None:
        payloads, routes, tools, attempts = self.factors()

        assert payloads * routes * tools == attempts
        assert attempts == shipped.attempts

    def test_the_corpus_is_smaller_than_the_cross_product(self) -> None:
        """Anti-vacuity: with one route and one tool the decomposition would say nothing."""
        payloads, routes, tools, attempts = self.factors()

        assert payloads < attempts
        assert routes > 1 and tools > 1
