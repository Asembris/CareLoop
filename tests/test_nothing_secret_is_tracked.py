"""No path that holds a credential is tracked by git.

This repository is private now and **public at submission**, which makes the git index a
publication list. The realistic accident is not a key pasted into source — it is
``git add -f .env`` on a hurried evening, or a ``.pem`` copied into the tree and swept up by a
``git add -A``. Both are invisible in review and permanent once the history is public.

**A structural check, not a content one.** It asks whether a forbidden *path* is tracked, never
whether a file's bytes look secret-shaped. That is a deliberate division of labour: this repository
is full of credential-shaped strings that are not credentials — 64-character hex idempotency keys,
``apr-0123456789abcdef``, ``sk-do-not-leak-me-0123456789`` in
``tests/test_llm_providers.py`` — and a scanner that cries wolf about those is a scanner somebody
eventually switches off. This one has no false positives to tune away, so it will still be running
in a year.

It lives in the suite rather than in a CI job for the same reason: every developer runs it, on every
change, offline and at zero cost, and there is no separate workflow anybody can disable.

**The exceptions are named and justified**, because a rule with a silent carve-out is a rule nobody
can audit. See :data:`ALLOWED`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: Paths that must never be tracked, as (pattern, why), matched against the repo-relative path.
#:
#: Anchored on basenames and extensions rather than on a directory, so a credential does not become
#: publishable by being moved: ``.keys/careloop-deploy.pem`` and ``notes/backup.pem`` are the same
#: mistake.
FORBIDDEN: Final[tuple[tuple[str, str], ...]] = (
    (r"(^|/)\.env$", "developer credentials: AWS, OpenAI and NVIDIA keys live here"),
    (r"(^|/)\.env\.(?!example$)[^/]+$", "a variant .env — .env.local and friends hold real values"),
    (r"(^|/)\.keys/", "the deploy key directory"),
    (r"\.pem$", "a private key in any location"),
    (r"\.p12$|\.pfx$|\.jks$", "a key store"),
    (r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$", "an SSH private key"),
    (r"\.sqlite3(-wal|-shm)?$", "a case store: synthetic, but it is state and not source"),
)

#: Tracked paths that match a rule above and are correct anyway, each with the reason it is safe.
#:
#: Empty today. It exists so that adding an exception costs a sentence in a place a reviewer reads,
#: rather than a quiet edit to a regex that weakens the rule for everything.
ALLOWED: Final[dict[str, str]] = {}


def tracked_paths() -> list[str]:
    """Every path git is tracking, as the index holds them."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return [name for name in listing.decode("utf-8").split("\0") if name]


def forbidden_among(paths: Iterable[str]) -> dict[str, str]:
    """The paths that must not be tracked, mapped to the reason they must not be.

    Takes its paths as a parameter for the reason the projection checks take their tables: a guard
    that can only run against the shipped tree is a guard whose failure path is untested.
    """
    caught: dict[str, str] = {}
    for path in paths:
        if path in ALLOWED:
            continue
        for pattern, why in FORBIDDEN:
            if re.search(pattern, path):
                caught[path] = why
                break
    return caught


class TestNoCredentialPathIsTracked:
    """The property. One assertion, and it is about the tree that will be published."""

    def test_the_index_holds_nothing_forbidden(self) -> None:
        caught = forbidden_among(tracked_paths())
        assert caught == {}, "git is tracking paths that must never be published: " + "; ".join(
            f"{path} ({why})" for path, why in sorted(caught.items())
        )

    def test_the_example_env_files_are_still_tracked(self) -> None:
        """Vacuity, and a real requirement: the rule must not have banned the examples.

        A check that passed by forbidding every ``.env``-shaped path would also have deleted the
        documentation of which variables exist, and nobody would notice until a new contributor
        could not configure anything.
        """
        tracked = set(tracked_paths())
        assert ".env.example" in tracked
        assert "frontend/.env.example" in tracked
        # Committed on purpose and documented as holding no secret: model ids and URLs only.
        assert "deploy/careloop.env" in tracked


class TestTheDetectorCatchesThePlantedOnes:
    """Vacuity. A sweep nobody has watched fail is a sweep nobody should trust."""

    @pytest.mark.parametrize(
        "planted",
        [
            ".env",
            "frontend/.env",
            ".env.local",
            ".env.production",
            ".keys/careloop-deploy.pem",
            "notes/backup.pem",
            "deploy/id_rsa",
            "state/demo.sqlite3",
            "state/demo.sqlite3-wal",
            "certs/client.p12",
        ],
    )
    def test_each_planted_path_is_caught(self, planted: str) -> None:
        assert forbidden_among([planted]) != {}

    @pytest.mark.parametrize(
        "innocent",
        [
            ".env.example",
            "frontend/.env.example",
            "deploy/careloop.env",
            "careloop/api/sessions.py",
            "docs/architecture.pdf",
            "careloop/tools/fixtures/referral.pdf",
            "README.md",
        ],
    )
    def test_the_innocent_ones_are_not(self, innocent: str) -> None:
        """The other half. A detector that flags everything is not a detector."""
        assert forbidden_among([innocent]) == {}


class TestTheDeployTarballExcludesTheSameThings:
    """Two halves of one property, checked against each other rather than assumed to agree.

    ``deploy/push.sh`` excludes these paths from the tarball it ships and then greps the archive to
    prove it. That covers files that are *present but untracked*, which this sweep cannot see; this
    sweep covers what is *published to GitHub*, which the tarball check cannot see. Neither
    subsumes the other, and a deleted exclude in either is the kind of edit that looks harmless.
    """

    def test_push_still_excludes_every_category_this_sweep_forbids(self) -> None:
        script = (REPO_ROOT / "deploy" / "push.sh").read_text(encoding="utf-8")
        for token in (".keys", ".env", "*.pem", "*.sqlite3"):
            assert token in script, (
                f"deploy/push.sh no longer mentions {token!r}: the tarball may now carry it. "
                f"The two checks are deliberately independent — do not delete one because the "
                f"other exists."
            )

    def test_push_refuses_a_tarball_that_contains_one(self) -> None:
        """The refusal, not just the exclusion: an exclude that silently failed would ship it."""
        script = (REPO_ROOT / "deploy" / "push.sh").read_text(encoding="utf-8")
        assert "refusing to ship" in script
