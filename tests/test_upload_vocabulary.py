"""What an upload's resource identity is, and the one thing it may not be called.

The second external effect will file a document into create-once object storage. Two mechanisms sit
close together in that design and describing them as one is the easy mistake:

``RESOURCE IDENTITY``
    Where the object lives. Derived from the **durable action identity** — the idempotency key
    frozen into ``ACTION_PREPARED`` before anything leaves the process — so that a recovery
    re-issuing that identity addresses the object the first attempt would have made, whatever the
    document turns out to contain.

``CONTENT INTEGRITY``
    Whether what the provider is holding is what CareLoop sent. Established afterwards, by fetching
    the bytes back and reducing them with :func:`careloop.tools.content.content_digest`. See
    ``tests/test_content_integrity.py``.

**The design is not content-addressed, and calling it that would be a specific false claim rather
than a loose one.** In a content-addressed store the digest *is* the address: the same bytes always
land in the same place, and an object cannot be at an address that does not describe it, so
integrity is a property of addressing and needs no separate check. None of that is true here. The
address is a function of the case, the action type and the arguments policy authorised; identical
bytes filed for two different cases are two objects at two addresses; and an object can perfectly
well sit at its address holding something else, **which is exactly the failure the read-back
exists to catch**. A reader told the store was content-addressed would reasonably conclude that
check was redundant.

The term also does not survive contact with recovery. A resource named after its contents could not
be addressed by an attempt that has not yet read those contents back, so the frozen-identity
convergence the saga depends on would have nothing to aim at.

This guard is phrase-precise rather than keyword-broad, in the arrangement
``tests/test_recovery_copy.py`` uses and for its reason: a sweep that flagged every mention of a
digest, or every sentence containing "content", would be deleted the first week. It catches claims
about *addressing*, and leaves claims about *integrity* alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the architecture is stated. Source and design documentation, not build output.
#:
#: ``docs/session-log.md`` is deliberately absent. It is a record of what was decided and said at
#: the time, including wording later corrected — this correction among them — and editing history
#: so a vocabulary guard passes would be falsifying the one document whose value is that it was not
#: edited afterwards. ``CLAUDE.md`` is absent for ``test_recovery_copy.py``'s reason: it explains
#: rules by stating the sentence they forbid, which is what this module's own docstring does too.
SWEPT: tuple[tuple[str, str], ...] = (
    ("careloop", "*.py"),
    ("demoworld", "*.py"),
    ("evaluators", "*.py"),
    ("scripts", "*.py"),
    ("docs", "architecture.md"),
    ("docs", "judges.md"),
    ("frontend/src", "*.ts"),
    ("frontend/src", "*.tsx"),
)

#: This file, which necessarily contains every phrase it forbids.
EXEMPT: frozenset[str] = frozenset({Path(__file__).name})

#: Claims that the resource's address is a function of its contents.
#:
#: Each is a phrase somebody would plausibly write about this design while believing it accurate,
#: and each is false about it. ``content hash``/``content digest`` alone are **not** here: those are
#: correct names for the integrity value and appear throughout
#: :mod:`careloop.tools.content`, so forbidding them would forbid describing the thing that is
#: actually true.
REJECTED: tuple[re.Pattern[str], ...] = (
    re.compile(r"content[- ]address", re.IGNORECASE),
    re.compile(r"content[- ](hash|digest)[- ]addressed", re.IGNORECASE),
    re.compile(r"addressed by (its )?(content|sha-?256|hash|digest)", re.IGNORECASE),
    re.compile(
        r"(content|object) (hash|digest) is the (resource|object) (identity|address|key)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(key|address)(ed)? (is )?derived from (the )?(content|bytes|digest)", re.IGNORECASE
    ),
)

#: The vocabulary this design is described in, kept beside what it replaces so the pair is one
#: decision rather than a prohibition with nothing on the other side of it.
ACCEPTED: tuple[str, ...] = (
    "create-once",
    "durable action identity",
    "object identity",
    "fresh provider read",
    "content integrity",
)


def swept_files() -> list[Path]:
    found = [
        path
        for directory, pattern in SWEPT
        for path in sorted((REPO_ROOT / directory).rglob(pattern))
        if path.name not in EXEMPT
    ]
    return found


def offences(text: str) -> list[str]:
    return [match.group(0) for pattern in REJECTED for match in pattern.finditer(text)]


class TestNothingClaimsTheAddressDescribesTheContents:
    def test_the_sweep_has_files_to_sweep(self) -> None:
        """Vacuity. A sweep over nothing passes over anything."""
        assert len(swept_files()) > 50

    @pytest.mark.parametrize(
        "path", swept_files(), ids=lambda path: str(path.relative_to(REPO_ROOT))
    )
    def test_no_architectural_surface_calls_the_design_content_addressed(self, path: Path) -> None:
        assert offences(path.read_text(encoding="utf-8")) == []


class TestTheGuardCatchesWhatItIsFor:
    """The other half. A guard nobody can trip is a guard over nothing."""

    @pytest.mark.parametrize(
        "sentence",
        [
            "The upload is content-addressed, so a duplicate is free.",
            "Objects are content addressed by SHA-256.",
            "The object key is derived from the content digest.",
            "The content hash is the resource identity.",
            "The object digest is the object key.",
            "Each object is addressed by its SHA-256.",
            "The address is derived from the bytes.",
        ],
    )
    def test_a_false_claim_is_caught(self, sentence: str) -> None:
        assert offences(sentence) != []

    @pytest.mark.parametrize(
        "sentence",
        [
            "The object key is derived from the durable action identity.",
            "A create-once store refuses to overwrite what it already holds.",
            "Content integrity is established by a fresh provider read.",
            "CareLoop compares the content digest of the bytes it fetched back.",
            "content_digest reduces bytes to lowercase hexadecimal SHA-256.",
            "The digest is not an action identity, which is why it is not in idempotency.py.",
            "Two documents that differ by one byte are two documents.",
            "The resource identity and the content integrity are separate mechanisms.",
        ],
    )
    def test_a_true_claim_is_left_alone(self, sentence: str) -> None:
        assert offences(sentence) == []

    def test_the_accepted_vocabulary_is_not_itself_forbidden(self) -> None:
        """The replacement terms must survive the guard, or the correction contradicts itself."""
        for term in ACCEPTED:
            assert offences(term) == []


def flattened(path: Path) -> str:
    """One long line, lowercased. A wrapped sentence is the same sentence.

    The assertions below are about *what a module says*, and a module says it across whatever line
    width ``ruff format`` chose. Matching the raw text would make an editorial rewrap look like a
    deleted guarantee.
    """
    return " ".join(path.read_text(encoding="utf-8").split()).lower()


class TestTheTwoMechanismsAreDocumentedApart:
    """The positive half: the accepted wording is actually written down where it is decided."""

    def test_the_digest_module_says_it_is_not_an_action_identity(self) -> None:
        source = flattened(REPO_ROOT / "careloop" / "tools" / "content.py")

        assert "not an action identity" in source
        assert "create-once store keyed by durable action identity" in source

    def test_the_readback_contract_requires_a_fresh_request(self) -> None:
        source = flattened(REPO_ROOT / "careloop" / "graph" / "verify.py")

        assert "must come from a fresh request" in source
        assert "not from the response the effect itself produced" in source
