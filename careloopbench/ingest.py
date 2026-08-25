"""Live proofs: ingested verbatim, never re-run, and never promoted to this commit.

Some of what CareLoop has established cannot be re-established by a test run. A measurement of
Amazon S3's write contract, a trace judged by an AWS evaluator, a login completed through a real
Cognito hosted UI — each happened once, somewhere, against a system nobody here controls. Running
them on every invocation is impossible; pretending they were run is worse. So they are **stored**,
and CareLoopBench's only jobs are to read them, to say which commit each certifies, and to notice
when it has stopped being about this code.

**A live proof certifies the commit it ran against.** That sentence is the whole design. A row is
rendered as ``PROVEN_AT <commit>`` and never as though it had been re-derived against whatever a
reader has checked out. There is no path here that promotes a stored proof to ``PROVEN``.

**Staleness is scoped to a declared dependency surface**, per proof, because the scopes genuinely
differ: a frontend edit does not invalidate a measurement of a provider's write contract, and a
model that staled everything on every commit would train a reader to ignore the signal. The digest
is taken over ``git ls-tree`` at ``HEAD`` — the *commit's* bytes rather than the working tree's,
the same choice ``scripts/deploy_artifact.py`` makes and for the same reason.

**Attestation is honest about its own weakness.** ``MEASURED`` means the stored record was produced
by a tool that could name the commit it ran against. ``OPERATOR`` means a person recorded what they
observed and attests to the commit themselves. CareLoop's deployment publishes no build identifier
on the wire, so several proofs can only be ``OPERATOR`` today; inventing a signing ceremony over an
unverifiable claim would be theatre, and saying which of the two it is costs nothing and is true.

**Nothing credential-shaped may be stored.** :func:`read_envelope` refuses a record carrying a key
or a value that looks like a token, and the repository's own sweep over tracked text files covers
this directory as well.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from careloopbench.claims import EVERY_CLAIM, ArtifactRef, Claim
from careloopbench.levels import SHORT_COMMIT, Status
from careloopbench.report import ClaimResult

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: Every field a stored envelope must carry. Absent any of them, the record is refused rather than
#: defaulted: a proof missing the commit it certifies is a proof that certifies nothing.
REQUIRED: Final[tuple[str, ...]] = (
    "proof_id",
    "tool",
    "attested_commit",
    "attestation",
    "ingested_at_commit",
    "produced_at",
    "environment",
    "surface_digest",
    "raw",
)

#: How the commit on a record was established. See the module docstring.
MEASURED: Final[str] = "MEASURED"
OPERATOR: Final[str] = "OPERATOR"
ATTESTATIONS: Final[frozenset[str]] = frozenset({MEASURED, OPERATOR})

#: Key names a stored proof may never carry. Shape-based and deliberately blunt: this directory
#: holds records of what happened, never the credentials that made it happen.
FORBIDDEN_KEYS: Final[tuple[str, ...]] = (
    "access_token",
    "api_key",
    "authorization",
    "bearer",
    "client_secret",
    "id_token",
    "password",
    "refresh_token",
    "secret",
    "session_token",
    "sub",
    "token",
)

#: Anything shaped like a JWT, wherever it appears in the record.
JWT: Final[re.Pattern[str]] = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.")


class ArtifactRefused(Exception):
    """A stored proof is not something CareLoopBench could report honestly."""


def _git(arguments: Sequence[str], *, root: Path) -> str:
    finished = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode:
        raise ArtifactRefused(f"git {' '.join(arguments)} failed: {finished.stderr.strip()}")
    return finished.stdout


def surface_digest(surface: Sequence[str], *, root: Path = REPO_ROOT) -> str:
    """A digest over the commit's own bytes for every declared path.

    ``git ls-tree -r HEAD`` names each tracked path with the hash of its blob, so this changes
    exactly when the content under the surface changes and never because somebody touched a file
    or has an unrelated edit in the working tree.

    A surface that matches nothing is refused rather than digested. Two empty surfaces would hash
    alike, so a mistyped path would silently become a proof that never goes stale.
    """
    listing = _git(["ls-tree", "-r", "HEAD", "--", *surface], root=root)
    if not listing.strip():
        raise ArtifactRefused(f"surface {list(surface)} matches nothing at HEAD")
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def _refuse_credentials(envelope: Mapping[str, Any]) -> None:
    text = json.dumps(envelope, sort_keys=True)
    if JWT.search(text):
        raise ArtifactRefused("the record carries something shaped like a token")

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if str(key).lower() in FORBIDDEN_KEYS:
                    raise ArtifactRefused(f"the record carries a {key!r} field")
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(envelope)


def read_envelope(path: Path) -> dict[str, Any]:
    """Load one stored proof, refusing anything CareLoopBench could not report honestly."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as broken:
        raise ArtifactRefused(f"{path}: not JSON ({broken})") from broken
    if not isinstance(envelope, dict):
        raise ArtifactRefused(f"{path}: a stored proof is an object")

    missing = tuple(field for field in REQUIRED if field not in envelope)
    if missing:
        raise ArtifactRefused(f"{path}: missing {', '.join(missing)}")
    if envelope["attestation"] not in ATTESTATIONS:
        raise ArtifactRefused(f"{path}: attestation must be one of {sorted(ATTESTATIONS)}")
    if not str(envelope["attested_commit"]).strip():
        raise ArtifactRefused(f"{path}: a proof with no commit certifies nothing")
    _refuse_credentials(envelope)
    return envelope


def _short(commit: str) -> str:
    return str(commit)[:SHORT_COMMIT]


def ingest_one(claim: Claim, *, root: Path = REPO_ROOT) -> ClaimResult:
    """Resolve one live claim against its stored record and the current commit.

    Three outcomes and no fourth. There is deliberately no branch that returns ``PROVEN``: this
    invocation watched nothing here happen, and the strongest true statement is that somebody else
    did, at a commit this row names.
    """
    reference = claim.artifact
    if reference is None:
        raise ArtifactRefused(f"{claim.id}: not a live claim")

    stored = root / reference.path
    if not stored.exists():
        return ClaimResult(claim, Status.NOT_RUN, f"no stored proof; run {reference.tool}")

    envelope = read_envelope(stored)
    if envelope["proof_id"] != reference.proof_id:
        raise ArtifactRefused(
            f"{stored}: records {envelope['proof_id']!r}, the registry declares "
            f"{reference.proof_id!r}"
        )

    commit = _short(envelope["attested_commit"])
    attestation = str(envelope["attestation"]).lower()
    if envelope["surface_digest"] != surface_digest(reference.surface, root=root):
        return ClaimResult(
            claim,
            Status.STALE,
            f"proven at {commit}; {', '.join(reference.surface)} has moved since",
        )
    return ClaimResult(claim, Status.PROVEN_AT, f"proven at {commit} · {attestation}-attested")


def live_results(
    claims: Sequence[Claim] = EVERY_CLAIM, *, root: Path = REPO_ROOT
) -> dict[str, ClaimResult]:
    """Every live claim, resolved. Executed claims are absent, which is what the caller expects."""
    return {claim.id: ingest_one(claim, root=root) for claim in claims if not claim.is_executed}


__all__ = [
    "ATTESTATIONS",
    "MEASURED",
    "OPERATOR",
    "REQUIRED",
    "ArtifactRef",
    "ArtifactRefused",
    "ingest_one",
    "live_results",
    "read_envelope",
    "surface_digest",
]
