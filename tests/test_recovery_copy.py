"""The one thing the recovery story may not say, swept across everything that says it.

Reconciliation **does** send the booking again. It re-issues the identity CareLoop froze before the
first request left the process, which is what makes the provider return the appointment it already
made instead of creating a second one — a lookup that also repairs. The measured shape, from
``tests/test_recovery_proof.py``::

    HTTP POSTs that really left the process   2
    logical action identity                   1
    provider appointments                     1

So there are two claims here, one true and one false, and they are easy to confuse:

``CareLoop created no second appointment``
    True, and it is the claim the product makes.

``CareLoop did not send the request twice``
    **False.** It is the tidier sentence, it is what a reader expects a reliability story to say,
    and writing it would put an untrue statement in front of a judge about the one part of the
    system whose whole value is that it does not lie about what happened.

This file is the guard. It sweeps the product's own surfaces — the frontend the judge reads and the
judge-facing documentation — for sentences that deny a second request, and it is deliberately
phrase-precise rather than keyword-broad: "CareLoop will not repeat the action" is *true* on a
parked case that will never be replayed, "no second appointment" is true everywhere, and a sweep
that flagged either would be deleted the first time it cried wolf.

It lives in the backend suite rather than in ``vitest`` because it is a source sweep and the
question it asks — "is this sentence consistent with what the saga measurably does?" — is answered
by the backend's own evidence, which sits next door.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The surfaces a judge actually reads. Source, not build output.
#:
#: ``CLAUDE.md`` is deliberately absent: it discusses the *defect* this guard exists because of —
#: session Q's "a second autonomous external booking", session E's "each miss sent a second
#: booking" — and those sentences are descriptions of what went wrong, not product claims.
SWEPT: tuple[tuple[str, str], ...] = (
    ("frontend/src", "*.ts"),
    ("frontend/src", "*.tsx"),
    ("docs", "judges.md"),
)


#: Sentences that deny a second request. Every one of them would be false about this system.
#:
#: Written as patterns over a bounded window rather than as whole sentences, so a rewording that
#: keeps the meaning is still caught. Bounded by ``[^.]`` so a match cannot span two sentences and
#: manufacture a claim out of two innocent halves.
FORBIDDEN: tuple[tuple[str, str], ...] = (
    (
        r"\b(?:not|never|no|n't)\b[^.]{0,48}?\bsend(?:s|ing)?\b[^.]{0,24}?\b(?:again|twice)\b",
        "denies that the request is ever sent again — it is, under the frozen identity",
    ),
    (
        r"\b(?:sent|sends|send|sending)\b[^.]{0,24}?\b(?:only once|just once)\b",
        "claims one request, and there are two",
    ),
    (
        r"\bre-?sen[dt]\b[^.]{0,24}?\b(?:never|not)\b|\b(?:never|not|no)\b[^.]{0,24}?\bre-?sen[dt]\b",
        "denies the replay, which is the thing that recovers the case",
    ),
    (
        r"\bwithout\b[^.]{0,32}?\b(?:sending|re-?sending|repeating)\b[^.]{0,24}?\b"
        r"(?:request|booking|post|call)\b",
        "claims the recovery avoided a second request; it avoided a second appointment",
    ),
    (
        r"\bno second\b\s+(?:http\s+)?(?:request|post|call|attempt|dispatch)\b",
        "claims no second request; there is one, and it carries the original key",
    ),
    (
        r"\bexactly one\b[^.]{0,16}?\b(?:request|post|dispatch)\b",
        "claims one request, and there are two",
    ),
)


def swept_files() -> list[Path]:
    found: list[Path] = []
    for folder, pattern in SWEPT:
        found.extend(sorted((REPO_ROOT / folder).rglob(pattern)))
    return found


def without_comments(source: str) -> str:
    """``source`` with its TypeScript comments removed.

    **Copy is what the product says; a comment is what a developer says about it**, and the two
    have to be swept differently. This very file's neighbours explain the rule by *stating the
    false sentence* — "copy claiming CareLoop does not send again would be false" — and a sweep
    that could not tell that apart from the claim itself would fire on every explanation of why it
    exists, which is how a guard gets deleted.

    String literals and JSX text both survive, which is the whole surface a judge can read. The
    ``(?<!:)`` is what keeps ``https://`` from being read as the start of a line comment.
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


class TestNothingClaimsTheRequestWasNotSentAgain:
    """The sweep."""

    def test_the_sweep_has_files_to_sweep(self) -> None:
        """A sweep over nothing passes forever. This is what says it is looking at something."""
        files = swept_files()

        assert len(files) > 20
        assert any(path.name == "presentation.ts" for path in files)
        assert any(path.name == "realApi.ts" for path in files)
        assert any(path.name == "judges.md" for path in files)

    @pytest.mark.parametrize("path", swept_files(), ids=lambda path: path.name)
    def test_no_product_surface_denies_the_second_request(self, path: Path) -> None:
        found = offences(
            path.read_text(encoding="utf-8"), strip_comments=path.suffix in {".ts", ".tsx"}
        )

        assert found == [], f"{path.relative_to(REPO_ROOT)}: {found}"

    def test_the_comment_stripper_does_not_hide_a_string(self) -> None:
        """Its own vacuity proof: the narrowing must not become a hole.

        A false claim inside a string literal or in JSX text is exactly what this exists to catch,
        and stripping comments must not reach either.
        """
        assert offences('const copy = "CareLoop will not send it again";', strip_comments=True)
        assert offences("<p>CareLoop never sends the request twice</p>", strip_comments=True)

    def test_the_comment_stripper_does_reach_a_comment(self) -> None:
        line_comment = "\n".join(["// CareLoop will not send it again", "const x = 1;"])
        assert offences(line_comment, strip_comments=True) == []
        assert offences("/* never sends the request twice */", strip_comments=True) == []

    def test_the_comment_stripper_leaves_a_url_alone(self) -> None:
        """``https://`` is not a line comment, and a stripper that thought so would eat the line."""
        source = 'const at = "https://example.test/x"; const copy = "will not send it again";'

        assert offences(source, strip_comments=True) != []


class TestTheSweepCatchesWhatItIsFor:
    """Its vacuity proof, on the sentences somebody would actually write.

    Each of these is a plausible, tidy, well-intentioned line about this feature, and each is false
    about the system that ships.
    """

    @pytest.mark.parametrize(
        "sentence",
        [
            "CareLoop will not send the booking again.",
            "CareLoop never sends the request twice.",
            "Recovered without sending the booking twice.",
            "The booking was sent only once.",
            "It was never resent.",
            "No second request left the process.",
            "Exactly one request reached the clinic.",
            "CareLoop does not send it again — it checks instead.",
            "recovered without repeating the request",
        ],
    )
    def test_a_false_claim_is_caught(self, sentence: str) -> None:
        assert offences(sentence) != []


class TestTheSweepLeavesTrueSentencesAlone:
    """The other half, and the half that decides whether this guard survives contact with a repo.

    A sweep that flagged these would be deleted the first time somebody hit it, and the product
    would lose the guard along with the noise. Every line here is true about the shipped system.
    """

    @pytest.mark.parametrize(
        "sentence",
        [
            "One appointment — the same action safely replayed.",
            "CareLoop created no second appointment.",
            "Retrying the same frozen booking identity — never a new booking.",
            "CareLoop will not repeat the action. You can ask it to look again.",
            "Nothing was changed. CareLoop will not retry on its own.",
            "CareLoop tried twice to work out a valid next action here and could not.",
            "The booking request was sent and no answer reached CareLoop.",
            "It will only retry the same frozen booking identity, then check the clinic's record.",
            "Two attempts reached the clinic; one appointment exists.",
            "No second appointment was created.",
        ],
    )
    def test_a_true_claim_is_left_alone(self, sentence: str) -> None:
        assert offences(sentence) == []
