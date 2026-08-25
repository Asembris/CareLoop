"""The two things the adversarial demonstration may not say, swept across everything that says it.

The deterministic scenario **substitutes the planner**. A judge watching it is watching CareLoop
refuse an action CareLoop itself proposed, which is a real and strong property -- and it is not the
property the tidier sentence claims::

    ``the model tried to email the records to an attacker``
        **False** on this scenario. No model was asked. The proposal is a fixture in
        ``careloop/api/adversary.py``, and saying otherwise would be attributing behaviour to
        Bedrock that this run measured nothing about.

    ``watch CareLoop block it``
        **False** of the *realistic* scenario, which is the one that still asks a real planner. It
        is free to ignore the poison, it did so in three of six measured deployed runs, and a
        launcher promising a refusal there is promising something the runtime does not control.

So there are two dishonest directions and they are opposite: claiming a model produced the supplied
proposal, and promising a refusal from the run where nothing is supplied. Both are the kind of
sentence a well-meaning editor writes, and both are checked here.

This file is the guard, and it is deliberately phrase-precise rather than keyword-broad. *"A
document tries to instruct the agent"* is true and must stay writable; *"the model may simply ignore
the attack"* is true and is the point of the realistic card; *"if the model ever proposes this, the
same boundary applies"* is the whole thesis. A sweep that flagged any of those would be deleted the
first time somebody hit it, and the product would lose the guard along with the noise.

It lives in the backend suite for the reason ``tests/test_recovery_copy.py`` does: it is a source
sweep whose question -- *"is this sentence consistent with what the code measurably does?"* -- is
answered by the backend's own evidence, which sits next door in ``tests/test_adversarial_demo.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The surfaces a judge actually reads. Source, not build output.
#:
#: ``CLAUDE.md`` is deliberately absent, and ``careloop/api/adversary.py`` and this file are too:
#: all three discuss the false sentences in order to forbid them, which is the distinction
#: :func:`without_comments` draws inside a TypeScript file and which whole-file exclusion draws
#: here.
SWEPT: tuple[tuple[str, str], ...] = (
    ("frontend/src", "*.ts"),
    ("frontend/src", "*.tsx"),
    ("docs", "judges.md"),
)

#: Sentences attributing the supplied proposal to a model.
#:
#: Every one would be false about the deterministic scenario. Bounded by ``[^.]`` so a match cannot
#: span two sentences and manufacture a claim out of two innocent halves.
#:
#: **The subject has to be a model**, which is what the alternation in each pattern is doing. "The
#: attacker tried to" is true and is the story; "the document tries to instruct the agent" is true
#: and is on the card. Only a claim about what the *model* or *the AI* did is refused.
_ACTOR = r"(?:the\s+)?(?:model|ai|llm|planner|agent|bedrock|nova)"

FORBIDDEN: tuple[tuple[str, str], ...] = (
    (
        rf"\b{_ACTOR}\b[^.]{{0,32}}?\b(?:tried|attempted|wanted|decided|chose)\s+to\b",
        "attributes the supplied proposal to a model; no model was asked for it",
    ),
    (
        rf"\b{_ACTOR}\b[^.]{{0,32}}?\b(?:was\s+tricked|fell\s+for|took\s+the\s+bait|"
        r"was\s+fooled|was\s+deceived|got\s+tricked)\b",
        "claims a model was deceived; the deterministic scenario measures nothing about one",
    ),
    (
        rf"\b(?:we|careloop)\b[^.]{{0,24}}?\b(?:caught|stopped|blocked)\s+{_ACTOR}\b",
        "frames the refusal as catching a model, rather than refusing a proposal",
    ),
    (
        rf"\b{_ACTOR}\b[^.]{{0,32}}?\b(?:generated|produced|proposed|wrote|emitted)\b"
        r"[^.]{0,32}?\b(?:attack|malicious|dangerous|unsafe|hostile|poisoned)\b",
        "says a model produced the dangerous proposal; CareLoop supplied it",
    ),
    (
        r"\bwatch\b[^.]{0,32}?\bcareloop\b[^.]{0,16}?\b(?:block|refuse|stop|deny)\b",
        "promises a refusal; on the realistic scenario the runtime does not control one",
    ),
    (
        r"\bwatch\b[^.]{0,32}?\b(?:the\s+)?unsafe\s+(?:branch|action|step)\b",
        "promises an unsafe branch that a well-behaved planner may never produce",
    ),
)


def swept_files() -> list[Path]:
    found: list[Path] = []
    for folder, pattern in SWEPT:
        found.extend(sorted((REPO_ROOT / folder).rglob(pattern)))
    return found


def without_comments(source: str) -> str:
    """``source`` with its TypeScript comments removed.

    Copy is what the product says; a comment is what a developer says about it. This repository
    explains a rule by *stating the false sentence*, and a sweep that could not tell a claim from an
    explanation of the claim is a sweep that gets deleted. String literals and JSX text both
    survive, which is the whole surface a judge can read. The ``(?<!:)`` keeps ``https://`` from
    being read as the start of a line comment.
    """
    without_blocks = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//.*", " ", without_blocks)


def offences(text: str, *, strip_comments: bool = False) -> list[str]:
    """Every forbidden claim in ``text``, as ``"<matched phrase> — <why it is false>"``."""
    flat = " ".join((without_comments(text) if strip_comments else text).split())
    return [
        f"{match.group(0)!r} — {why}"
        for pattern, why in FORBIDDEN
        for match in re.finditer(pattern, flat, flags=re.IGNORECASE)
    ]


class TestNothingClaimsAModelWroteTheSuppliedProposal:
    """The sweep."""

    def test_the_sweep_has_files_to_sweep(self) -> None:
        """A sweep over nothing passes forever. This is what says it is looking at something."""
        files = swept_files()

        assert len(files) > 20
        assert any(path.name == "scenarios.ts" for path in files)
        assert any(path.name == "presentation.ts" for path in files)
        assert any(path.name == "judges.md" for path in files)

    @pytest.mark.parametrize("path", swept_files(), ids=lambda path: path.name)
    def test_no_product_surface_makes_a_dishonest_adversarial_claim(self, path: Path) -> None:
        found = offences(
            path.read_text(encoding="utf-8"), strip_comments=path.suffix in {".ts", ".tsx"}
        )

        assert found == [], f"{path.relative_to(REPO_ROOT)}: {found}"

    def test_the_comment_stripper_does_not_hide_a_string(self) -> None:
        """Its own vacuity proof: the narrowing must not become a hole."""
        assert offences('const copy = "The model tried to email the records";', strip_comments=True)
        assert offences("<p>Watch CareLoop block it</p>", strip_comments=True)

    def test_the_comment_stripper_does_reach_a_comment(self) -> None:
        assert offences("// the model tried to send it\nconst x = 1;", strip_comments=True) == []
        assert offences("/* watch CareLoop block it */", strip_comments=True) == []

    def test_the_comment_stripper_leaves_a_url_alone(self) -> None:
        source = 'const at = "https://example.test/x"; const copy = "the model tried to send";'

        assert offences(source, strip_comments=True) != []


class TestTheSweepCatchesWhatItIsFor:
    """Its vacuity proof, on the sentences somebody would actually write.

    Each is a plausible, tidy, well-intentioned line about this feature, and each is false about
    the system that ships.
    """

    @pytest.mark.parametrize(
        "sentence",
        [
            "The model tried to email the records to an attacker.",
            "The AI attempted to forward the file.",
            "The model was tricked by the document.",
            "The planner fell for the instruction.",
            "The agent took the bait and proposed an unsafe action.",
            "We caught the model before it could act.",
            "CareLoop stopped the model from sending it.",
            "The model generated a malicious proposal.",
            "Nova produced the dangerous action.",
            "Watch CareLoop block it.",
            "Watch the unsafe branch end while the real work continues.",
            "The model decided to grant portal access.",
        ],
    )
    def test_a_false_claim_is_caught(self, sentence: str) -> None:
        assert offences(sentence) != []


class TestTheSweepLeavesTrueSentencesAlone:
    """The other half, and the half that decides whether this guard survives contact with a repo.

    A sweep that flagged these would be deleted the first time somebody hit it. Every line here is
    true about the shipped system, and most of them are lines the product needs to be able to say.
    """

    @pytest.mark.parametrize(
        "sentence",
        [
            "A document tries to instruct the agent.",
            "The model may simply ignore the attack — and that is the system working.",
            "For this test, CareLoop deliberately supplies the dangerous proposal.",
            "If the model ever proposes this action, the same boundary applies.",
            "The attacker wanted the records forwarded to an outside address.",
            "The notice tried to have the referral sent somewhere else.",
            "CareLoop proposes the action the attacker asked for, then refuses it.",
            "Everything after the proposal is the ordinary CareLoop system.",
            "This proposal was supplied by CareLoop, not written by a model.",
            "CareLoop did not ask you to approve this.",
            "A well-behaved planner may never produce an unsafe action here.",
            "The document asks the agent to send records to an outside address.",
            "Nothing was sent, booked or changed.",
            "Watch what happens to the proposal.",
        ],
    )
    def test_a_true_sentence_is_left_alone(self, sentence: str) -> None:
        assert offences(sentence) == []
