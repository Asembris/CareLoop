"""The deployed release marker check: wrong, missing, malformed, or cached is a failed deploy.

``scripts/verify_deploy.py`` runs through the distribution at the end of every deploy and on the
hourly schedule. This file drives :func:`~scripts.verify_deploy.verify_the_release_marker` over a
stubbed transport, one wrong answer at a time, because a checker asserted only against a healthy
deployment passes identically when it has stopped checking anything.

One shape needs naming: **a missing marker is not a 404**. The SPA fallback answers the shell with
a 200, so absence arrives here as a 200 whose body is not JSON -- the status check cannot catch it
and the shape check is what does. The negative cases below are the anti-vacuity for the whole
addition: remove any one of the checks and its case goes green having verified nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from scripts import verify_deploy
from scripts.verify_deploy import Failed, verify_the_release_marker

A_COMMIT = "a" * 40


def answering(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    body: Any = None,
    cache: str | None = "no-cache",
) -> None:
    """One scripted answer for ``/release.json``, and a refusal to be asked anything else."""

    def fake_call(base: str, path: str, **_: Any) -> tuple[int, Any, dict[str, str]]:
        assert path == "/release.json", f"the marker check asked for {path}"
        headers = {} if cache is None else {"cache-control": cache}
        return status, body, headers

    monkeypatch.setattr(verify_deploy, "call", fake_call)


class TestACorrectMarkerPasses:
    def test_the_shape_alone_when_nothing_is_expected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answering(monkeypatch, body={"git_sha": A_COMMIT})
        verify_the_release_marker("https://example.invalid")

    def test_the_exact_commit_when_one_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, body={"git_sha": A_COMMIT})
        verify_the_release_marker("https://example.invalid", expected_commit=A_COMMIT)


class TestEveryWrongAnswerFails:
    def test_a_non_200_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answering(monkeypatch, status=503, body=None)
        with pytest.raises(Failed, match="200"):
            verify_the_release_marker("https://example.invalid")

    def test_a_missing_marker_fails_as_the_spa_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 200-with-HTML case: exactly what a deployment without the marker serves."""
        answering(monkeypatch, body=None)
        with pytest.raises(Failed, match="git_sha object"):
            verify_the_release_marker("https://example.invalid")

    @pytest.mark.parametrize(
        "wrong_shape",
        [
            [A_COMMIT],
            {"sha": A_COMMIT},
            {"git_sha": A_COMMIT, "built_at": "2026-08-26"},
            {},
        ],
    )
    def test_anything_but_the_one_shape_fails(
        self, monkeypatch: pytest.MonkeyPatch, wrong_shape: Any
    ) -> None:
        answering(monkeypatch, body=wrong_shape)
        with pytest.raises(Failed, match="git_sha object"):
            verify_the_release_marker("https://example.invalid")

    @pytest.mark.parametrize("malformed", ["A" * 40, "a" * 39, "main", 7, None])
    def test_a_malformed_commit_fails(
        self, monkeypatch: pytest.MonkeyPatch, malformed: Any
    ) -> None:
        answering(monkeypatch, body={"git_sha": malformed})
        with pytest.raises(Failed, match="well-formed"):
            verify_the_release_marker("https://example.invalid")

    def test_a_commit_other_than_the_packed_one_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deploy-time half: a well-formed marker for the wrong release is a failed deploy."""
        answering(monkeypatch, body={"git_sha": "b" * 40})
        with pytest.raises(Failed, match="the commit that was packed"):
            verify_the_release_marker("https://example.invalid", expected_commit=A_COMMIT)

    def test_a_hard_cached_marker_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A marker cached for a year would keep naming the previous release after a deploy."""
        answering(
            monkeypatch,
            body={"git_sha": A_COMMIT},
            cache="public, max-age=31536000, immutable",
        )
        with pytest.raises(Failed, match="revalidated"):
            verify_the_release_marker("https://example.invalid")


class TestTheCheckIsWiredIn:
    """A check that exists and is never called verifies nothing."""

    def test_verify_runs_the_marker_check_with_the_expected_commit(self) -> None:
        """On the AST, because driving the whole suite needs a scripted deployment.

        :func:`verify` is a sequence of live HTTP assertions; stubbing every endpoint to reach the
        last line would re-implement the deployment inside the test. What must hold structurally
        is narrower: ``verify`` calls ``verify_the_release_marker`` and hands it the caller's
        ``expected_commit`` unaltered.
        """
        tree = ast.parse(Path(verify_deploy.__file__).read_text(encoding="utf-8"))
        verify_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "verify"
        )
        calls = [
            node
            for node in ast.walk(verify_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "verify_the_release_marker"
        ]
        assert len(calls) == 1
        arguments = [argument.id for argument in calls[0].args if isinstance(argument, ast.Name)]
        assert arguments == ["base", "expected_commit"]

    def test_the_standalone_checker_accepts_an_expected_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--expected-commit`` reaches :func:`verify`, for a hand-run check after a deploy."""
        seen: dict[str, str | None] = {}

        def record(base: str, expected_commit: str | None = None) -> None:
            seen["expected"] = expected_commit

        monkeypatch.setattr(verify_deploy, "verify", record)
        monkeypatch.setattr(
            "sys.argv",
            ["verify_deploy.py", "https://example.invalid", "--expected-commit", A_COMMIT],
        )

        assert verify_deploy.main() == 0
        assert seen["expected"] == A_COMMIT

    def test_the_scheduled_run_still_needs_no_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``demo-health.yml`` runs this with no flags; the shape check must not demand one."""
        seen: dict[str, str | None] = {"expected": "sentinel"}

        def record(base: str, expected_commit: str | None = None) -> None:
            seen["expected"] = expected_commit

        monkeypatch.setattr(verify_deploy, "verify", record)
        monkeypatch.setattr("sys.argv", ["verify_deploy.py"])

        assert verify_deploy.main() == 0
        assert seen["expected"] is None
