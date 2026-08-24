"""What ships to the instance, asserted on the member list rather than on a shell comment.

``deploy/push.sh`` used to pack the repository root with ``tar --exclude='.venv'``. ``tar`` matches
an unanchored exclusion against whole path components, never as a prefix, so ``.venv-evals/`` --
260 MB of the DeepTeam closure, ``openai`` and ``deepeval`` included -- was in every artefact this
project has ever shipped. Nothing failed, nothing warned, and the only symptom was an instance
carrying a second Python environment nobody had decided to put there.

Three properties, and they are not variations of one:

- **The blocklist covers what it claims to**, including the spelling that got through. Asserted
  against constructed paths rather than only against today's tree, so the guard does not quietly
  become a description of one checkout.
- **The blocklist has not gone too wide.** An artefact missing ``deploy/careloop.env`` or the built
  bundle is perfectly sanitised and dead on arrival, and the safest possible artefact is an empty
  one. :data:`~scripts.deploy_artifact.REQUIRED_MEMBERS` is the floor that stops this file passing
  by removing everything.
- **The safety scan fires**, proven with planted names. A scan asserted only against a clean tree
  passes identically when it has stopped matching anything at all.
"""

from __future__ import annotations

import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts import deploy_artifact
from scripts.deploy_artifact import (
    EXCLUDED_NAME_PATTERNS,
    EXCLUDED_PATH_PREFIXES,
    FRONTEND_DIST,
    REQUIRED_MEMBERS,
    Artifact,
    ArtifactRefused,
    build,
    excluded_members,
    is_excluded,
    members,
    missing_required,
    pack,
    scan,
)

ROOT = Path(__file__).resolve().parents[1]

#: Paths that must never reach the host, one per category the audit named. Written as real paths a
#: developer's checkout produces rather than as the patterns that match them, so a pattern rewritten
#: in a way that stops matching goes red here.
MUST_NOT_SHIP: tuple[str, ...] = (
    # The gap, named. `.venv` matched; `.venv-evals` did not.
    ".venv-evals/Lib/site-packages/openai/__init__.py",
    ".venv-evals/bin/python",
    ".venv/Lib/site-packages/boto3/__init__.py",
    ".venv/bin/activate",
    "venv/bin/python",
    # Credentials.
    ".env",
    ".env.local",
    ".env.production",
    ".keys/careloop-deploy.pem",
    ".keys/README.md",
    "careloop-deploy.pem",
    "deploy/keys/server.key",
    ".aws/credentials",
    # Version control.
    ".git/config",
    ".git/HEAD",
    # SQLite, WAL sidecars included.
    "demo.sqlite3",
    "demo.sqlite3-wal",
    "demo.sqlite3-shm",
    "/opt/state/careloop.sqlite3",
    # Caches.
    "careloop/__pycache__/graph.cpython-312.pyc",
    "tests/__pycache__/test_store.cpython-312.pyc",
    ".pytest_cache/v/cache/lastfailed",
    ".ruff_cache/0.6.0/12345",
    ".mypy_cache/3.12/builtins.data.json",
    ".hypothesis/examples/abc",
    "careloop.egg-info/PKG-INFO",
    # node_modules, wherever it is.
    "node_modules/react/index.js",
    "frontend/node_modules/vite/package.json",
    "e2e/node_modules/@playwright/test/index.js",
    # Test-only bulk.
    "tests/test_store.py",
    "tests/helpers.py",
    "e2e/tests/boot.spec.ts",
    "e2e/playwright.config.ts",
    "bench/cache/nova-micro.json",
    "evals/cache/attacks.json",
    "frontend/reference/mockup.html",
    # Vendored archives nothing in the build reads.
    "careloop-motion.zip",
    "careloop-site.zip",
    # The judge-facing dossier, deployed separately and sharing nothing with the application.
    "site/index.html",
    "site/vendor/dossier.bundle.html",
)

#: Paths the host genuinely needs, or that merely resemble something excluded. A blocklist that
#: caught any of these would break a deploy in a way a passing secret scan would not reveal.
MUST_SHIP: tuple[str, ...] = (
    "deploy/careloop.env",
    "deploy/bootstrap.sh",
    "deploy/careloop-api.service",
    "careloop/state/sqlite_store.py",
    "careloop/policy/rules.yaml",
    "careloop/tools/fixtures/demanding-letter.pdf",
    "demoworld/clinic.py",
    "evaluators/spans.py",
    "objectstore/store.py",
    "scripts/demo_serve.py",
    "scripts/observability.py",
    "bench/run.py",
    "evals/replay.py",
    "frontend/dist/index.html",
    "frontend/dist/assets/index-BiHL1Ob1.js",
    "frontend/src/App.tsx",
    "docs/architecture.md",
    "pyproject.toml",
)


def _extract(payload: bytes) -> dict[str, bytes]:
    """Every member of a packed artefact, by name. What the host would actually unpack."""
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as archive:
        for entry in archive:
            handle = archive.extractfile(entry)
            if handle is not None:
                found[entry.name] = handle.read()
    return found


@pytest.fixture(scope="module")
def shipped() -> tuple[str, ...]:
    """The real member list. Skipped rather than faked when the bundle has not been built."""
    if not (ROOT / FRONTEND_DIST / "index.html").exists():
        pytest.skip(f"{FRONTEND_DIST} is not built; run `npm --prefix frontend run build`")
    return members(ROOT)


class TestTheExclusionsCoverWhatTheAuditNamed:
    """Every category, asserted against a path rather than against the pattern that matches it."""

    @pytest.mark.parametrize("path", MUST_NOT_SHIP)
    def test_it_is_excluded(self, path: str) -> None:
        assert is_excluded(path), (
            f"{path!r} would be packed into the deploy artefact. Whatever rule was supposed to "
            "drop it either never existed or has stopped matching."
        )

    def test_the_venv_evals_gap_specifically(self) -> None:
        """The defect this file exists for, stated on its own so a regression names itself."""
        assert is_excluded(".venv-evals/Lib/site-packages/deepteam/telemetry.py")
        assert is_excluded(".venv/Lib/site-packages/deepteam/telemetry.py")
        wildcarded = [
            pattern
            for pattern in EXCLUDED_NAME_PATTERNS
            if pattern.startswith(".venv") and pattern.endswith("*")
        ]
        assert wildcarded, (
            "the virtual-environment exclusion has lost its trailing wildcard, which is exactly "
            "the shape that let .venv-evals through tar's whole-component matching."
        )


class TestTheExclusionsHaveNotGoneTooWide:
    """A sanitised artefact that cannot boot is a worse outcome than the one being fixed."""

    @pytest.mark.parametrize("path", MUST_SHIP)
    def test_it_is_kept(self, path: str) -> None:
        assert not is_excluded(path), (
            f"{path!r} would be dropped from the deploy artefact. An exclusion has gone wide "
            "enough to break the deployment rather than to sanitise it."
        )

    def test_careloop_env_is_not_caught_by_the_dotenv_rules(self) -> None:
        """``deploy/careloop.env`` ends in ``.env`` and is the file both units read."""
        assert not is_excluded("deploy/careloop.env")
        assert is_excluded(".env")

    def test_the_required_members_are_all_shippable(self) -> None:
        for required in REQUIRED_MEMBERS:
            assert not is_excluded(required), (
                f"{required} is on the required list and on the excluded list at once. One of "
                "the two is wrong, and the deploy would refuse itself."
            )


class TestTheRealArtefactIsClean:
    """The tree as it stands, which is the claim a deploy actually rests on."""

    def test_nothing_forbidden_is_in_it(self, shipped: tuple[str, ...]) -> None:
        assert scan(shipped) == ()

    def test_no_untracked_scratch_can_reach_it(self, shipped: tuple[str, ...]) -> None:
        """Everything but the built bundle comes from ``git``, which is the structural half."""
        tracked = set(
            subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                check=True,
                text=True,
            ).stdout.splitlines()
        )
        for name in shipped:
            assert name in tracked or name.startswith(f"{FRONTEND_DIST}/"), (
                f"{name} is in the artefact and is neither tracked at HEAD nor part of the built "
                "bundle. The artefact has stopped being a function of the commit."
            )

    def test_everything_the_host_needs_is_in_it(self, shipped: tuple[str, ...]) -> None:
        assert missing_required(shipped) == ()

    def test_the_bulk_is_gone(self, shipped: tuple[str, ...]) -> None:
        for prefix in EXCLUDED_PATH_PREFIXES:
            assert not [name for name in shipped if name.startswith(prefix)], (
                f"{prefix} is in the artefact despite being excluded wholesale."
            )

    def test_the_exclusions_actually_drop_something(self) -> None:
        """Vacuity: a rule set matching nothing would satisfy every assertion above."""
        assert len(excluded_members(ROOT)) > 50


class TestTheSafetyScanFires:
    """Proven with planted names, because a clean tree proves nothing about a scan."""

    @pytest.mark.parametrize(
        "planted",
        [
            ".env",
            "deploy/.env",
            ".env.production",
            # `.env.example` is a committed template and harmless, and it is still refused: the
            # scan reads a name, never a judgement about what a file happens to contain today.
            # It is excluded from the artefact anyway, so the two rules never disagree in practice.
            ".env.example",
            ".keys/careloop-deploy.pem",
            "careloop-deploy.pem",
            "server.key",
            ".aws/credentials",
            "state/demo.sqlite3",
            "state/demo.sqlite3-wal",
            ".venv/pyvenv.cfg",
            ".venv-evals/pyvenv.cfg",
            "venv/bin/python",
            "frontend/node_modules/vite/index.js",
            ".git/config",
            "careloop/__pycache__/x.pyc",
            "home/.ssh/id_rsa",
        ],
    )
    def test_a_planted_name_is_caught(self, planted: str) -> None:
        assert scan(("README.md", planted, "pyproject.toml")) == (planted,)

    @pytest.mark.parametrize(
        "innocent",
        [
            "deploy/careloop.env",
            "careloop/state/sqlite_store.py",
            "careloop/tools/objectstore.py",
            "docs/deployment-objectstore.md",
            "frontend/dist/assets/index-Bl9T98T_.js",
            "scripts/deploy.py",
        ],
    )
    def test_an_innocent_name_is_not(self, innocent: str) -> None:
        assert scan((innocent,)) == ()


class TestTheBuildRefusesRatherThanShips:
    """Each refusal, exercised on its own so a passing build cannot stand in for all three."""

    def test_a_dirty_tree_is_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ("careloop/graph/nodes.py",))
        with pytest.raises(ArtifactRefused, match="does not match HEAD"):
            build(ROOT, tmp_path / "out.tar.gz")

    def test_a_dirty_tree_is_allowed_when_asked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shipped: tuple[str, ...]
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ("careloop/graph/nodes.py",))
        built = build(ROOT, tmp_path / "out.tar.gz", allow_dirty=True)
        assert built.members == shipped

    def test_a_forbidden_member_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ())
        monkeypatch.setattr(
            deploy_artifact, "members", lambda _: (*REQUIRED_MEMBERS, ".keys/careloop-deploy.pem")
        )
        with pytest.raises(ArtifactRefused, match="must not"):
            build(ROOT, tmp_path / "out.tar.gz")
        assert not (tmp_path / "out.tar.gz").exists()

    def test_a_missing_required_member_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ())
        monkeypatch.setattr(deploy_artifact, "members", lambda _: ("README.md",))
        with pytest.raises(ArtifactRefused, match="cannot boot without"):
            build(ROOT, tmp_path / "out.tar.gz")
        assert not (tmp_path / "out.tar.gz").exists()

    def test_an_unbuilt_frontend_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(deploy_artifact, "FRONTEND_DIST", "frontend/dist-that-is-not-there")
        with pytest.raises(ArtifactRefused, match="does not exist"):
            deploy_artifact.built_frontend(ROOT)


class TestTheContentIsTheCommitsAndNotTheDisks:
    """A tracked member carries the bytes ``HEAD`` records, whatever the checkout looks like."""

    def test_a_tracked_member_comes_from_the_commit(self, tmp_path: Path, shipped) -> None:
        scratch = tmp_path / "planted.txt"
        target = "README.md"
        scratch.write_bytes(b"planted")

        packed = _extract(pack(ROOT, (target, *REQUIRED_MEMBERS)))
        assert packed[f"./{target}"] == deploy_artifact.head_blobs(ROOT)[target]

    def test_shell_scripts_ship_with_unix_line_endings(self, shipped) -> None:
        """A checkout on Windows holds CRLF for these, and a Linux host cannot run them that way."""
        packed = _extract(pack(ROOT, shipped))
        for name, payload in packed.items():
            if name.endswith(".sh") or name.endswith(".service"):
                assert b"\r\n" not in payload, f"{name} would reach the host with CRLF endings"

    def test_the_built_bundle_still_comes_from_disk(self, shipped: tuple[str, ...]) -> None:
        """The one member no commit records, so construction cannot supply it."""
        blobs = deploy_artifact.head_blobs(ROOT)
        bundle = [name for name in shipped if name.startswith(f"{FRONTEND_DIST}/")]
        assert bundle
        assert not [name for name in bundle if name in blobs]


class TestTheBytesAreDeterministic:
    """One commit plus one bundle is one digest, which is what the instance checks against."""

    def test_two_packs_of_one_tree_agree(self, shipped: tuple[str, ...]) -> None:
        assert pack(ROOT, shipped) == pack(ROOT, shipped)

    def test_the_build_reports_the_digest_of_what_it_wrote(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import hashlib

        monkeypatch.setattr(deploy_artifact, "dirty_paths", lambda _: ())
        built: Artifact = build(ROOT, tmp_path / "out.tar.gz")
        written = (tmp_path / "out.tar.gz").read_bytes()
        assert built.sha256 == hashlib.sha256(written).hexdigest()
        assert built.size == len(written)
