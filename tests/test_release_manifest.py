"""The release marker: a built bundle names the commit it belongs to, and nothing can relabel it.

The provenance gap this closes was the middle of the deployment chain. The artefact was already a
deterministic function of one commit plus one built bundle, and the instance already verified its
digest before unpacking -- but the commit existed only in an S3 key the deploy deletes and an SSM
comment nobody can read back, so the running release could not name its source. Now
``frontend/dist/release.json`` carries exactly ``{"git_sha": <commit>}``: stamped by
``scripts/deploy.py`` immediately **after** a successful frontend build, required and validated by
``scripts/deploy_artifact.py`` before anything is packed, required again by the instance's
preflight, and served at ``/release.json`` by the existing static path -- no new route, no runtime
change.

The load-bearing half is what may *not* happen. ``--no-build`` deploys ``dist`` as it stands, so if
anything on that path rewrote the marker, a bundle built for commit A would be relabelled as commit
B and shipped -- false provenance manufactured by the very tool that exists to prevent it. So the
stamp has exactly one call site, inside the build, and the artefact builder *refuses* a mismatch
rather than repairing it. ``TestABundleCannotBeRelabeled`` is the pair that goes red if the
mismatch guard is removed, and it asserts the marker on disk is untouched by the refusal.
"""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import deploy, deploy_artifact
from scripts.deploy_artifact import (
    FRONTEND_DIST,
    RELEASE_MANIFEST,
    REQUIRED_MEMBERS,
    ArtifactRefused,
    build,
    head_commit,
    release_manifest_commit,
    write_release_manifest,
)

ROOT = Path(__file__).resolve().parents[1]

A_COMMIT = "a" * 40
ANOTHER_COMMIT = "b" * 40


@pytest.fixture
def marker_root(tmp_path: Path) -> Path:
    """A root with a ``frontend/dist`` and nothing else, for the writer and reader alone."""
    (tmp_path / FRONTEND_DIST).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def real_dist() -> Iterator[Path]:
    """The repository's own ``dist``, restored byte for byte however a test leaves it.

    The refusal tests below drive :func:`build` against the real ``ROOT``, because a synthetic
    repository would prove the guard over a tree shaped like nothing the deploy ever packs. A
    developer's real marker legitimately names the last *deployed* commit rather than ``HEAD``,
    so everything this fixture touches is put back exactly.
    """
    dist = ROOT / FRONTEND_DIST
    placeholder = dist / "index.html"
    stood_in = not placeholder.exists()
    if stood_in:
        dist.mkdir(parents=True, exist_ok=True)
        placeholder.write_bytes(b"<!doctype html><!-- stand-in for a built bundle -->")
    marker = dist / RELEASE_MANIFEST.rsplit("/", 1)[1]
    previous = marker.read_bytes() if marker.exists() else None
    try:
        yield dist
    finally:
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_bytes(previous)
        if stood_in:
            placeholder.unlink(missing_ok=True)
            with suppress(OSError):
                dist.rmdir()


class TestTheMarkerIsExactlyTheCommit:
    """One field, one shape, one byte sequence per commit. Nothing time- or machine-derived."""

    def test_the_writer_produces_exactly_the_git_sha_object(self, marker_root: Path) -> None:
        write_release_manifest(marker_root, A_COMMIT)
        written = (marker_root / RELEASE_MANIFEST).read_bytes()
        assert written == b'{"git_sha": "' + A_COMMIT.encode() + b'"}\n'
        assert json.loads(written) == {"git_sha": A_COMMIT}

    def test_two_stamps_of_one_commit_are_byte_identical(self, marker_root: Path) -> None:
        """No timestamp, no counter, no randomness: the artefact digest depends on this."""
        write_release_manifest(marker_root, A_COMMIT)
        first = (marker_root / RELEASE_MANIFEST).read_bytes()
        write_release_manifest(marker_root, A_COMMIT)
        assert (marker_root / RELEASE_MANIFEST).read_bytes() == first

    @pytest.mark.parametrize(
        "malformed",
        ["HEAD", "main", "a" * 39, "a" * 41, "A" * 40, "g" * 40, ""],
    )
    def test_the_writer_refuses_a_malformed_commit(self, marker_root: Path, malformed: str) -> None:
        with pytest.raises(ArtifactRefused, match="not a 40-hex commit"):
            write_release_manifest(marker_root, malformed)
        assert not (marker_root / RELEASE_MANIFEST).exists()


class TestTheReaderRefusesRatherThanGuesses:
    """A marker that cannot say its commit certifies nothing, and no default stands in."""

    def test_a_valid_marker_answers_its_commit(self, marker_root: Path) -> None:
        write_release_manifest(marker_root, A_COMMIT)
        assert release_manifest_commit(marker_root) == A_COMMIT

    def test_a_missing_marker_is_refused(self, marker_root: Path) -> None:
        with pytest.raises(ArtifactRefused, match="does not exist"):
            release_manifest_commit(marker_root)

    def test_a_marker_that_is_not_json_is_refused(self, marker_root: Path) -> None:
        (marker_root / RELEASE_MANIFEST).write_bytes(b"not json at all")
        with pytest.raises(ArtifactRefused, match="not valid JSON"):
            release_manifest_commit(marker_root)

    @pytest.mark.parametrize(
        "wrong_shape",
        [
            "[]",
            '"a string"',
            "{}",
            '{"sha": "' + A_COMMIT + '"}',
            '{"git_sha": "' + A_COMMIT + '", "built_at": "2026-08-26"}',
        ],
    )
    def test_anything_but_the_one_shape_is_refused(
        self, marker_root: Path, wrong_shape: str
    ) -> None:
        """Extra fields are refused, not ignored: a ``built_at`` would be two digests per commit."""
        (marker_root / RELEASE_MANIFEST).write_text(wrong_shape, encoding="utf-8")
        with pytest.raises(ArtifactRefused, match="exactly"):
            release_manifest_commit(marker_root)

    @pytest.mark.parametrize(
        "malformed",
        ['"' + "A" * 40 + '"', '"main"', '"' + "a" * 12 + '"', "7"],
    )
    def test_a_malformed_commit_is_refused(self, marker_root: Path, malformed: str) -> None:
        (marker_root / RELEASE_MANIFEST).write_text(
            '{"git_sha": ' + malformed + "}", encoding="utf-8"
        )
        with pytest.raises(ArtifactRefused, match="not a 40-hex commit"):
            release_manifest_commit(marker_root)


class TestABundleCannotBeRelabeled:
    """The ``--no-build`` safety, which is the whole point of validating rather than rewriting.

    ``--no-build`` skips :func:`scripts.deploy.build_frontend`, so nothing on that path stamps
    anything -- these guards in :func:`build` are the entire safety. The passing case and the
    refusing case share one tree and differ only in the marker, so the refusal is attributable to
    the mismatch alone: remove the guard, and ``test_a_marker_naming_another_commit_is_refused``
    goes red while the passing case stays green.
    """

    def test_a_marker_naming_head_is_packed(
        self, monkeypatch: pytest.MonkeyPatch, real_dist: Path, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ())
        write_release_manifest(ROOT, head_commit(ROOT))

        built = build(ROOT, tmp_path / "out.tar.gz")

        assert RELEASE_MANIFEST in built.members

    def test_a_marker_naming_another_commit_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, real_dist: Path, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ())
        write_release_manifest(ROOT, ANOTHER_COMMIT)

        with pytest.raises(ArtifactRefused, match="different commit"):
            build(ROOT, tmp_path / "out.tar.gz")

        assert not (tmp_path / "out.tar.gz").exists()
        assert release_manifest_commit(ROOT) == ANOTHER_COMMIT, (
            "the refusal rewrote the marker. A stale bundle must never be relabelled; the only "
            "correct outcomes are a rebuild or a refusal."
        )

    def test_a_missing_marker_is_refused_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch, real_dist: Path, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ())
        (real_dist / "release.json").unlink(missing_ok=True)

        with pytest.raises(ArtifactRefused, match="does not exist"):
            build(ROOT, tmp_path / "out.tar.gz")

        assert not (tmp_path / "out.tar.gz").exists()

    def test_the_marker_is_a_required_member(self) -> None:
        """The floor check: an exclusion rule that dropped the marker refuses the artefact."""
        assert RELEASE_MANIFEST in REQUIRED_MEMBERS

    def test_the_only_production_stamp_is_after_the_frontend_build(self) -> None:
        """One call site, inside :func:`~scripts.deploy.build_frontend`, proven on the AST.

        This is the structural half of "``--no-build`` does not rewrite a marker": the claim is
        not that some path remembers not to stamp, it is that no path outside the build *can*.
        """
        tree = ast.parse(Path(deploy.__file__).read_text(encoding="utf-8"))
        call_sites: list[str] = []
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "write_release_manifest"
                ):
                    call_sites.append(function.name)
        assert call_sites == ["build_frontend"]


class TestTheStampHappensAfterTheBuild:
    """The build replaces ``dist``, so a stamp written first is a stamp Vite deletes."""

    def test_the_marker_survives_a_build_that_replaces_dist(
        self, monkeypatch: pytest.MonkeyPatch, real_dist: Path
    ) -> None:
        """A fake ``npm run build`` that wipes the marker, exactly as a real one would."""
        real_run = subprocess.run

        def fake_npm(argv: list[str], **keywords: object) -> object:
            if argv[:1] == ["git"]:
                return real_run(argv, **keywords)  # type: ignore[arg-type]
            assert argv == ["npm", "run", "build"]
            (real_dist / "release.json").unlink(missing_ok=True)
            (real_dist / "index.html").write_bytes(b"<!doctype html><!-- freshly built -->")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_npm)

        deploy.build_frontend(ROOT)

        assert release_manifest_commit(ROOT) == head_commit(ROOT)

    def test_a_failed_build_stamps_nothing(
        self, monkeypatch: pytest.MonkeyPatch, real_dist: Path
    ) -> None:
        """A marker for a bundle that was never produced would be provenance for nothing."""
        (real_dist / "release.json").unlink(missing_ok=True)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))

        with pytest.raises(deploy.DeploymentRefused, match="build failed"):
            deploy.build_frontend(ROOT)

        assert not (real_dist / "release.json").exists()


class TestTheInstanceRequiresTheMarkerToo:
    """The third enforcement point, on the bytes that actually arrived."""

    def test_the_preflight_names_the_marker_as_required(self) -> None:
        script = deploy.preflight_script(bucket="b", key="deploy/k.tar.gz", sha256="a" * 64)
        assert "./frontend/dist/release.json" in script
        assert script.index("for required in") < script.index("./frontend/dist/release.json")
