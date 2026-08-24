"""The bytes that reach the host, and the guarantee that nothing else does.

    python scripts/deploy_artifact.py --list             # what would ship, and what would not
    python scripts/deploy_artifact.py --out careloop.tar.gz

**Two independent reasons a secret cannot ship, because one of them is a blocklist.** The member
list starts as the tree ``git`` records at ``HEAD`` -- so ``.venv``, ``.venv-evals``, ``.env``,
``.keys/`` and every cache directory are absent for the structural reason that they are untracked,
whatever anybody remembers to write down. :data:`EXCLUDED_NAME_PATTERNS` and
:data:`EXCLUDED_PATH_PREFIXES` are then applied on top, for the tracked paths a host has no use for
and as defence in depth over the first. :func:`scan` reads the *final* member list and refuses on
anything matching :data:`FORBIDDEN`, which is a third pass sharing no code with either.

**The gap this closes was a real one, and it was quiet.** ``deploy/push.sh`` excluded ``.venv``,
which ``tar`` matches as a whole path component and never as a prefix -- so ``.venv-evals/``, 260 MB
of the DeepTeam closure including ``openai`` and ``deepeval``, was packed into every deploy and
extracted over ``/opt/careloop/app``. Nothing failed. The instance simply carried a second Python
environment nobody had decided to put there.

**An exclusion list can also break a deployment, so it is checked in both directions.**
:data:`REQUIRED_MEMBERS` names what the host cannot boot without -- the units, ``careloop.env``,
``bootstrap.sh``, the built bundle -- and :func:`build` refuses an artefact missing any of them.
Without that half, the safest possible artefact is an empty one.

**The bytes are deterministic.** Fixed member order, fixed mtimes, no owner names, and a gzip header
carrying no timestamp: one commit plus one built bundle produces one SHA-256. That is what lets the
deploy tell the instance which artefact to expect and have the instance *check* rather than trust.

Standard library only, and no AWS anywhere. What goes into the artefact is a question that can be
answered on a machine holding no credentials, which is what makes it testable.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Final

#: The built frontend. Untracked by design -- ``dist/`` is gitignored -- so it is added explicitly
#: rather than arriving with the tracked tree, and its absence is an error rather than a smaller
#: artefact. Node is never installed on the instance: the bundle that ships is the bundle that was
#: built and tested here.
FRONTEND_DIST: Final[str] = "frontend/dist"

#: Whole directories a running host has no use for, as repository-relative prefixes.
#:
#: ``tests`` and ``e2e`` for the reason ``push.sh`` already gave about ``e2e``: test code is not
#: what is being served, it names ports and hosts the deployment does not use, and the one thing
#: worse than shipping it is shipping it and having somebody run it there. The instance installs
#: ``.[observability]`` and not ``.[dev]``, so neither could run there anyway.
#:
#: ``site`` is the judge-facing dossier, deployed separately as a GitHub project site and sharing
#: nothing with the application -- 2.8 MB of tracked assets this host would never serve.
EXCLUDED_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "e2e/",
    "tests/",
    "site/",
    "bench/cache/",
    "evals/cache/",
    "frontend/reference/",
    "frontend/node_modules/",
)

#: Path components excluded wherever they appear, as globs.
#:
#: Matched against **every component** of a path rather than against the whole of it. ``.venv*`` is
#: what covers ``.venv-evals``; the trailing star is the whole fix and it is not a tidy-up.
EXCLUDED_NAME_PATTERNS: Final[tuple[str, ...]] = (
    # Version control and its metadata.
    ".git",
    ".gitmodules",
    # Every virtual-environment spelling this repository has used, plus the ones it has not.
    ".venv*",
    "venv",
    "virtualenv",
    # Credentials. Untracked already; named here so the blocklist stands on its own.
    ".env",
    ".env.*",
    ".keys",
    ".aws",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    # The durable source of truth, WAL sidecars included. A developer's scratch database must
    # never land on top of the server's.
    "*.sqlite3",
    "*.sqlite3-*",
    "*.db",
    # Caches and build metadata.
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".hypothesis",
    ".coverage",
    "htmlcov",
    "*.egg-info",
    ".tox",
    "node_modules",
    # Editor, OS and agent scratch.
    ".idea",
    ".vscode",
    ".DS_Store",
    "Thumbs.db",
    ".agents",
    ".claude",
    # Vendored archives nothing in the build reads, and any artefact of a previous pack.
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
)

#: What the instance cannot boot without. The exclusions above are checked against this, because a
#: blocklist that removed one of these would produce a deploy that is perfectly sanitised and dead.
REQUIRED_MEMBERS: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "README.md",
    "deploy/bootstrap.sh",
    "deploy/careloop.env",
    "deploy/careloop-api.service",
    "deploy/careloop-demoworld.service",
    "careloop/__init__.py",
    "careloop/policy/rules.yaml",
    "demoworld/__init__.py",
    "scripts/__init__.py",
    "scripts/demo_serve.py",
    "scripts/observability.py",
    f"{FRONTEND_DIST}/index.html",
)

#: The final pass. Read against the member list the artefact will actually contain, sharing no code
#: with the exclusion logic above -- a defect in one is not a defect in both.
FORBIDDEN: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"(^|/)\.keys(/|$)"),
    re.compile(r"(^|/)\.aws(/|$)"),
    re.compile(r"\.pem$"),
    re.compile(r"\.(key|p12|pfx)$"),
    re.compile(r"(^|/)id_(rsa|ed25519)"),
    re.compile(r"\.sqlite3"),
    re.compile(r"(^|/)\.venv"),
    re.compile(r"(^|/)venv/"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)__pycache__(/|$)"),
)

#: Fixed for every entry, so the archive depends on the sources and on nothing else. The same
#: reasoning as ``scripts/evaluations_deploy.py``'s ``ZIP_EPOCH``: an artefact whose digest moves
#: with a file's mtime cannot be named in a report or checked on the far end.
TAR_EPOCH: Final[int] = 0


class ArtifactRefused(Exception):
    """A precondition this module checks rather than assumes did not hold."""


@dataclass(frozen=True)
class Artifact:
    """One built artefact, and everything a deploy needs to say about it."""

    path: Path
    commit: str
    sha256: str
    size: int
    members: tuple[str, ...]


def git(root: Path, *arguments: str) -> str:
    """One git invocation, refused rather than guessed at if git is not there."""
    try:
        finished = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
        )
    except FileNotFoundError as missing:  # pragma: no cover - git is present wherever this runs
        raise ArtifactRefused(
            "git is not on PATH; the artefact is built from the tracked tree"
        ) from missing
    except subprocess.CalledProcessError as failed:
        raise ArtifactRefused(
            f"git {' '.join(arguments)} failed: {failed.stderr.strip()}"
        ) from failed
    return finished.stdout


def head_commit(root: Path) -> str:
    """The exact commit the artefact will be built from."""
    return git(root, "rev-parse", "HEAD").strip()


def dirty_paths(root: Path) -> tuple[str, ...]:
    """Everything ``git status`` reports as not matching ``HEAD``.

    Load-bearing rather than tidy. The member *list* comes from ``HEAD`` and the member *content*
    comes from the working tree, so those two agreeing is what makes "this artefact is that commit"
    a true sentence instead of a hopeful one.
    """
    reported = git(root, "status", "--porcelain")
    return tuple(line[3:].strip() for line in reported.splitlines() if line.strip())


def is_excluded(path: str) -> bool:
    """Whether a repository-relative POSIX path is kept out of the artefact."""
    for prefix in EXCLUDED_PATH_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return any(
        fnmatch.fnmatch(component, pattern)
        for component in path.split("/")
        for pattern in EXCLUDED_NAME_PATTERNS
    )


def tracked_at_head(root: Path) -> tuple[str, ...]:
    """Every path the commit records, as repository-relative POSIX paths."""
    listed = git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    return tuple(sorted(entry for entry in listed.split("\0") if entry))


def built_frontend(root: Path) -> tuple[str, ...]:
    """Every file of the built bundle. Untracked by design, so enumerated from disk."""
    dist = root / FRONTEND_DIST
    if not dist.is_dir():
        raise ArtifactRefused(
            f"{FRONTEND_DIST} does not exist. Build the frontend before packing: "
            "npm --prefix frontend ci && npm --prefix frontend run build"
        )
    return tuple(
        sorted(
            found.relative_to(root).as_posix()
            for found in dist.rglob("*")
            if found.is_file() and not is_excluded(found.relative_to(root).as_posix())
        )
    )


def members(root: Path) -> tuple[str, ...]:
    """Exactly what the artefact contains, in the order it will contain it."""
    tracked = tuple(path for path in tracked_at_head(root) if not is_excluded(path))
    return tuple(sorted(set(tracked) | set(built_frontend(root))))


def excluded_members(root: Path) -> tuple[str, ...]:
    """The tracked paths the exclusions drop -- the other side of what ``--list`` prints."""
    return tuple(path for path in tracked_at_head(root) if is_excluded(path))


def scan(names: tuple[str, ...]) -> tuple[str, ...]:
    """Every member matching something that must never leave this machine.

    A pure function over a list of names, so it can be -- and is -- asserted against planted strings
    rather than only against whatever happens to be in the tree today.
    """
    return tuple(name for name in names if any(rule.search(name) for rule in FORBIDDEN))


def missing_required(names: tuple[str, ...]) -> tuple[str, ...]:
    """Everything the host needs that the artefact does not carry."""
    present = set(names)
    return tuple(required for required in REQUIRED_MEMBERS if required not in present)


def pack(root: Path, names: tuple[str, ...]) -> bytes:
    """A deterministic ``tar.gz`` of exactly ``names``.

    ``mtime=0`` on the gzip header as well as on every entry: the header carries a timestamp of its
    own, and leaving it to default makes two builds of one tree produce two digests.
    """
    raw = BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name in names:
            source = root / name
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = source.stat().st_size
            info.mtime = TAR_EPOCH
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            with source.open("rb") as handle:
                archive.addfile(info, handle)

    compressed = BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=TAR_EPOCH) as zipped:
        zipped.write(raw.getvalue())
    return compressed.getvalue()


def build(root: Path, out: Path, *, allow_dirty: bool = False) -> Artifact:
    """Build, check and write the artefact. Raises rather than shipping something doubtful.

    Raises:
        ArtifactRefused: the tree does not match ``HEAD``, the bundle is missing, a member the host
            needs is absent, or a member matches :data:`FORBIDDEN`.
    """
    dirty = dirty_paths(root)
    if dirty and not allow_dirty:
        listed = ", ".join(dirty[:5]) + ("..." if len(dirty) > 5 else "")
        raise ArtifactRefused(
            f"the working tree does not match HEAD ({len(dirty)} path(s): {listed}). "
            "The artefact is named after a commit, so it is built from one. Commit, stash, or "
            "pass --allow-dirty and accept that the name is then a guess."
        )

    commit = head_commit(root)
    names = members(root)

    absent = missing_required(names)
    if absent:
        raise ArtifactRefused(
            "the artefact is missing something the host cannot boot without: "
            + ", ".join(absent)
            + ". An exclusion has gone too wide."
        )

    forbidden = scan(names)
    if forbidden:
        raise ArtifactRefused(
            "refusing to ship: the artefact contains something it must not: " + ", ".join(forbidden)
        )

    payload = pack(root, names)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)

    return Artifact(
        path=out,
        commit=commit,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        members=names,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the sanitised deploy artefact from the tracked tree at HEAD."
    )
    parser.add_argument("--out", type=Path, help="where to write the artefact")
    parser.add_argument(
        "--list", action="store_true", help="print what ships and what does not, and write nothing"
    )
    parser.add_argument(
        "--allow-dirty", action="store_true", help="pack a tree that does not match HEAD"
    )
    arguments = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]

    try:
        if arguments.list:
            included, dropped = members(root), excluded_members(root)
            print(f"commit   {head_commit(root)}")
            print(f"included {len(included)} member(s)")
            print(f"excluded {len(dropped)} tracked path(s) by rule")
            for name in dropped[:20]:
                print(f"   - {name}")
            if len(dropped) > 20:
                print(f"   ... and {len(dropped) - 20} more")
            print(f"forbidden matches: {len(scan(included))}")
            return 0

        if arguments.out is None:
            parser.error("one of --out or --list is required")
        built = build(root, arguments.out, allow_dirty=arguments.allow_dirty)
    except ArtifactRefused as refused:
        print(f"deploy_artifact: {refused}", file=sys.stderr)
        return 1

    print(f"commit  {built.commit}")
    print(f"sha256  {built.sha256}")
    print(f"size    {built.size / 1024:.0f} KB in {len(built.members)} member(s)")
    print(f"path    {built.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
