"""Nothing that holds a credential is tracked by git — by path, or by shape.

This repository is private now and **public at submission**, which makes the git index a
publication list. Two accidents put something on it: ``git add -f .env`` on a hurried evening or a
``.pem`` swept up by ``git add -A``, and a key pasted into a file that is *already* tracked. Neither
implies the other, so both are checked, and both are invisible in review and permanent once the
history is public.

**The path half is structural; the shape half is deliberately narrow.** The path check asks only
whether a forbidden *path* is tracked. The content check looks for vendor-prefixed credentials and
nothing resembling an entropy heuristic, because this repository
is full of credential-shaped strings that are not credentials — 64-character hex idempotency keys,
``apr-0123456789abcdef``, confirmation ids, and an ``sk-``-prefixed fixture in
``tests/test_llm_providers.py`` — and a scanner that cries wolf about those is a scanner somebody
eventually switches off. Neither half here has a false positive to tune away, so both will still be
running in a year.

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

#: Credential *shapes* that are worth failing a build over, as (name, pattern).
#:
#: **Deliberately only the high-confidence ones**, and the omission is the design. gitleaks 8.28 was
#: run over this repository before this was written: **230 findings across the whole history, every
#: one a false positive**, and **zero** true positives in any tracked file. All 230 came from its
#: ``generic-api-key`` heuristic firing on ``"key": "<hash>"`` inside ``bench/cache/*.json`` and
#: ``evals/cache/*.json`` — captured model I/O, which is the most valuable thing in the repo to
#: scan and the thing an allowlist would have had to exclude wholesale. The only true positives it
#: found anywhere were ``.env`` and ``.keys/*.pem``, which are not tracked, which the path sweep
#: above already covers, and which it found by scanning the working directory rather than git.
#:
#: So the entropy heuristics are left out and the vendor prefixes kept. A credential pasted into a
#: tracked file is still caught; a hex digest in a fixture is not. That is the trade that keeps this
#: at zero false positives, and a check with zero false positives is one nobody ever turns off.
CREDENTIAL_SHAPES: Final[tuple[tuple[str, str], ...]] = (
    ("aws-access-key-id", r"\b(?:AKIA|ASIA|AIDA|AROA|AGPA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b"),
    ("openai-key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("nvidia-key", r"\bnvapi-[A-Za-z0-9_-]{20,}"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    ("google-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("slack-token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    ("private-key-block", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("aws-secret-assignment", r"(?i)aws_secret_access_key\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}"),
)

#: ``(path, shape)`` pairs that fire and are fine, each with the reason.
#:
#: One entry, and it is a fixture whose entire purpose is to be a credential-shaped string that a
#: test then proves is never echoed. Named individually rather than by directory, so a *second*
#: secret-shaped literal in the same file would still fail.
ALLOWED_SHAPES: Final[dict[tuple[str, str], str]] = {
    ("tests/test_llm_providers.py", "openai-key"): (
        "a deliberate fake — the test asserts a configured key is never echoed into an error, so "
        "it needs something key-shaped to not find"
    ),
}

#: Extensions whose bytes are not text and would only produce accidental matches.
BINARY: Final[re.Pattern[str]] = re.compile(
    r"\.(png|jpe?g|gif|ico|pdf|woff2?|ttf|otf|zip|gz|sqlite3|lock)$", re.I
)


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


def credential_shapes_in(text: str, path: str) -> list[str]:
    """The credential shapes present in ``text``, minus any allowlisted for ``path``."""
    return [
        name
        for name, pattern in CREDENTIAL_SHAPES
        if re.search(pattern, text) and (path, name) not in ALLOWED_SHAPES
    ]


def shapes_among(paths: Iterable[str]) -> dict[str, list[str]]:
    """Every tracked text file carrying a credential shape, mapped to which shapes."""
    caught: dict[str, list[str]] = {}
    for path in paths:
        if BINARY.search(path):
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if found := credential_shapes_in(text, path):
            caught[path] = found
    return caught


class TestNoCredentialShapeIsTracked:
    """The content half: a real key pasted into a file that *is* tracked.

    The path sweep below catches the file being committed; this catches the value being pasted into
    one that already is. Different accidents, and neither implies the other.
    """

    def test_no_tracked_file_carries_a_credential_shape(self) -> None:
        caught = shapes_among(tracked_paths())
        assert caught == {}, (
            "tracked files carry credential-shaped strings: "
            + "; ".join(f"{path} ({', '.join(found)})" for path, found in sorted(caught.items()))
            + ". If it is a fixture, add it to ALLOWED_SHAPES with a reason. If it is real, it is "
            "already public to anyone with repository access — rotate it first, then remove it."
        )

    @pytest.mark.parametrize(
        "planted",
        [
            # Assembled at runtime rather than written out, so **this file** carries no credential
            # shape either and the sweep above stays honest about the module that defines it. The
            # first version of this test wrote the literals in full and the sweep duly flagged
            # itself, which was the correct answer to the wrong question — allowlisting this file
            # would have made a real key pasted here invisible.
            "AKIA" + "IOSFODNN7EXAMPLE",
            "sk-" + "proj-abcdefghijklmnopqrstuvwxyz0123",
            "nvapi" + "-abcdefghijklmnopqrstuvwxyz0123",
            "ghp" + "_abcdefghijklmnopqrstuvwxyz0123456789",
            "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
            "xoxb" + "-1234567890-abcdefghij",
        ],
    )
    def test_the_detector_catches_a_planted_credential(self, planted: str) -> None:
        """Vacuity, one shape at a time. A content check nobody has watched fail is decoration."""
        assert credential_shapes_in(f"token = '{planted}'\n", "careloop/api/app.py") != []

    @pytest.mark.parametrize(
        "innocent",
        [
            # Every one of these is real text from this repository, and gitleaks' generic-api-key
            # rule flagged the shape of the last two 230 times across the history.
            "KEY = '0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0'",
            "approval_id = 'apr-0123456789abcdef'",
            "confirmation_id = 'apt-e6e8ab9c23aed3ee'",
            '{"key": "04f44dd56daef68dc8d8880dd9e16ebd454bbbf4579d85a38f268380e2720a93"}',
            "CARELOOP_MODEL_PLANNER=bedrock:us.amazon.nova-2-lite-v1:0",
        ],
    )
    def test_the_things_this_repo_is_full_of_are_not_flagged(self, innocent: str) -> None:
        """The half that decides whether anyone keeps this switched on."""
        assert credential_shapes_in(innocent, "tests/helpers.py") == []

    def test_the_allowlist_is_still_needed(self) -> None:
        """Vacuity for the allowlist: an entry that no longer fires is an entry to delete."""
        for (path, name), _ in ALLOWED_SHAPES.items():
            text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
            pattern = dict(CREDENTIAL_SHAPES)[name]
            assert re.search(pattern, text), (
                f"{path} no longer carries a {name}; remove its ALLOWED_SHAPES entry rather than "
                f"leaving a permanent exception for something that is gone"
            )


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


class TestTheDeployArtefactExcludesTheSameThings:
    """Two halves of one property, checked against each other rather than assumed to agree.

    The deploy artefact excludes these paths and then scans its own member list to prove it. That
    covers files that are *present but untracked*, which this sweep cannot see; this sweep covers
    what is *published to GitHub*, which the artefact check cannot see. Neither subsumes the other,
    and a deleted exclusion in either is the kind of edit that looks harmless.

    The rules used to live in ``deploy/push.sh``'s ``tar --exclude`` list, which is where the
    ``.venv-evals`` hole was; they are now :mod:`scripts.deploy_artifact`, with
    ``tests/test_deploy_artifact.py`` holding the detail. This class keeps asking the question from
    *this* side, because that independence is the point of it.
    """

    def test_every_category_this_sweep_forbids_is_excluded_from_the_artefact(self) -> None:
        from scripts.deploy_artifact import is_excluded

        for path in (".keys/deploy.pem", ".env", "careloop-deploy.pem", "demo.sqlite3"):
            assert is_excluded(path), (
                f"the deploy artefact would now carry {path!r}. The two checks are deliberately "
                "independent - do not delete one because the other exists."
            )

    def test_the_artefact_refuses_rather_than_shipping_one(self) -> None:
        """The refusal, not just the exclusion: an exclusion that silently failed would ship it."""
        from scripts.deploy_artifact import scan

        assert scan((".keys/deploy.pem",)) == (".keys/deploy.pem",)

    def test_the_instance_refuses_it_too(self) -> None:
        """A third pass, on the host, over the bytes that actually arrived."""
        from scripts.deploy import preflight_script

        assert "refusing to unpack" in preflight_script(
            bucket="b", key="deploy/k.tar.gz", sha256="a" * 64
        )
