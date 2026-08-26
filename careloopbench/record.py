"""Writing one live proof down, and refusing to write down a proof that did not hold.

``evidence/live/`` held exactly one record before this module existed, and a person typed it. That
is fine for a record whose ``attestation`` is ``OPERATOR`` and whose whole content is what somebody
observed — but it is the wrong way to file a *tool's* output, for the reason
:mod:`careloopbench.ingest` gives about transcription. So there is now one writer, and an operator's
job is to run the proof and name it::

    python -m careloopbench.record live.objectstore-contract \\
        --raw scratch/objectstore.json \\
        --attestation MEASURED \\
        --environment "real Amazon S3, us-east-1, ..."

**The gate is the point of this module.** Each of the three proof tools writes its ``--out`` file
*and then* returns a non-zero exit code, so a failed run leaves behind a file shaped exactly like a
successful one. :func:`ingest_one` has three outcomes and none of them is ``FAILED`` — by design,
since a stored record is something somebody else measured and this invocation watched nothing —
which means a stored record of a *failed* run would render ``PROVEN_AT``. That is the one way the
live surface could say something untrue, and it is closed here, on the writing side: every raw
output must carry its tool's own ``passed: true``. A proof that did not hold keeps its scratch file
for diagnosis and never becomes evidence.

**The registry owns the destination, the tool name and the surface.** There is no ``--out``, no
``--path``, no ``--surface`` and no ``--digest``. A caller who could choose where a record lands
could file one proof under another's name; a caller who could choose the surface could declare a
proof that never goes stale. Everything a reader will later check the record against is derived
from the :class:`~careloopbench.claims.ArtifactRef` the registry already declares, so the writer
and the reader cannot disagree about what this record is.

**Validation is the reader's, not a second copy of it.** The assembled envelope goes through
:func:`~careloopbench.ingest.read_envelope` — the same required fields, the same attestation
vocabulary, the same credential and JWT refusal — before anything is written, and through
:func:`~careloopbench.ingest.ingest_one` afterwards, so a record that could not be reported is a
record that was never filed. A second schema here would be a second answer to one question.

**Raw means raw.** What the tool wrote is stored verbatim. There is no summary field, no headline
and no per-proof reducer: a sentence somebody composed about a measurement is not the measurement,
and the moment this module knew which proof it was holding it would have become the transcription
layer it exists to replace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from careloopbench.claims import EVERY_CLAIM, ArtifactRef, Claim
from careloopbench.ingest import (
    ATTESTATIONS,
    MEASURED,
    OPERATOR,
    REPO_ROOT,
    ArtifactRefused,
    _git,
    ingest_one,
    read_envelope,
    surface_digest,
)

#: The field every proof tool's structured output must carry, and the whole of the gate.
#:
#: One name across three tools that share nothing else: the object store answers ``satisfied``, the
#: deployed lock sums its rows' own ``ok`` flags, the AgentCore proof compares an expectation. Each
#: keeps its own vocabulary; each also spells the verdict this way, derived from the expression that
#: already decides its exit code. A recorder that had to know which of them it was holding would be
#: a per-proof reducer, which is the thing this module must not become.
VERDICT: Final[str] = "passed"


class RecordingRefused(Exception):
    """What was offered is not something CareLoopBench could file as a live proof."""


def claim_for(proof_id: str, claims: Sequence[Claim] = EVERY_CLAIM) -> Claim:
    """The claim that declares one proof id.

    The registry is the only place a proof's destination, tool and dependency surface are written
    down, so this is the only way to obtain them. A proof id nothing declares is refused rather
    than filed somewhere plausible.
    """
    for claim in claims:
        if claim.artifact is not None and claim.artifact.proof_id == proof_id:
            return claim
    declared = sorted(claim.artifact.proof_id for claim in claims if claim.artifact is not None)
    raise RecordingRefused(f"no claim declares {proof_id!r}; the registry declares {declared}")


def artifact_for(proof_id: str, claims: Sequence[Claim] = EVERY_CLAIM) -> ArtifactRef:
    """The declared reference for one proof id. Never a caller's idea of one."""
    reference = claim_for(proof_id, claims).artifact
    assert reference is not None  # claim_for only ever returns a claim carrying one
    return reference


def raw_that_passed(path: Path) -> dict[str, Any]:
    """One tool's structured output, refused unless the tool itself said the proof held.

    Refused for a missing verdict as well as a false one, and deliberately by identity rather than
    truthiness: ``1``, ``"true"`` and a non-empty list are not a tool saying its proof held, and a
    gate that accepted them would accept the next thing that is not one either.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as missing:
        raise RecordingRefused(f"{path}: cannot be read ({missing})") from missing
    except json.JSONDecodeError as broken:
        raise RecordingRefused(f"{path}: not JSON ({broken})") from broken
    if not isinstance(raw, dict):
        raise RecordingRefused(f"{path}: a tool's output is an object")
    if VERDICT not in raw:
        raise RecordingRefused(
            f"{path}: carries no {VERDICT!r}, so nothing here says the proof held"
        )
    if raw[VERDICT] is not True:
        raise RecordingRefused(f"{path}: {VERDICT}={raw[VERDICT]!r}; a proof that did not hold")
    return raw


def head_commit(*, root: Path = REPO_ROOT) -> str:
    """The commit a locally-run tool measured. ``git`` is asked; nothing is remembered."""
    return _git(["rev-parse", "HEAD"], root=root).strip()


def build_envelope(
    reference: ArtifactRef,
    raws: Sequence[dict[str, Any]],
    *,
    attestation: str,
    environment: str,
    commit: str,
    produced_at: str,
    root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Assemble the record. Every field a reader checks comes from the registry or from ``git``.

    One raw is stored as itself; several are stored under ``runs`` in the order they were given.
    The distinction is deliberately shallow — a proof made of several tool invocations is several
    tool invocations, and flattening them into a merged object would be this module having an
    opinion about what the tool produced.
    """
    return {
        "proof_id": reference.proof_id,
        "tool": reference.tool,
        "attested_commit": commit,
        "attestation": attestation,
        "ingested_at_commit": head_commit(root=root),
        "produced_at": produced_at,
        "environment": environment,
        "surface_digest": surface_digest(reference.surface, root=root),
        "raw": raws[0] if len(raws) == 1 else {"runs": list(raws)},
    }


def write_record(
    proof_id: str,
    raw_paths: Sequence[Path],
    *,
    attestation: str,
    environment: str,
    commit: str | None = None,
    produced_at: str | None = None,
    claims: Sequence[Claim] = EVERY_CLAIM,
    root: Path = REPO_ROOT,
) -> Path:
    """Record one live proof, and answer with the path the registry says it lives at.

    Written through a temporary file in the same directory and moved into place, so a record that
    fails validation cannot half-replace the one already there.
    """
    if attestation not in ATTESTATIONS:
        raise RecordingRefused(f"attestation must be one of {sorted(ATTESTATIONS)}")
    if not environment.strip():
        raise RecordingRefused("a record with no environment says nothing about where it was taken")
    if not raw_paths:
        raise RecordingRefused("a record needs at least one tool output")

    reference = artifact_for(proof_id, claims)
    raws = [raw_that_passed(path) for path in raw_paths]

    if attestation == MEASURED and commit is not None:
        raise RecordingRefused(
            "a MEASURED record names the commit the tool ran against; --commit is for an "
            "OPERATOR record about a build this repository cannot observe"
        )

    envelope = build_envelope(
        reference,
        raws,
        attestation=attestation,
        environment=environment,
        commit=commit or head_commit(root=root),
        produced_at=produced_at or datetime.now(UTC).date().isoformat(),
        root=root,
    )

    destination = root / reference.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_suffix(".staged")
    staged.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        read_envelope(staged)
    except ArtifactRefused:
        staged.unlink(missing_ok=True)
        raise
    os.replace(staged, destination)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="careloopbench.record",
        description="File one live proof, if its own tool says the proof held.",
    )
    parser.add_argument("proof_id", help="the proof id the claim registry declares")
    parser.add_argument(
        "--raw",
        action="append",
        default=[],
        metavar="PATH",
        help="a proof tool's structured output; repeat for a proof made of several runs",
    )
    parser.add_argument("--attestation", choices=sorted(ATTESTATIONS), required=True)
    parser.add_argument("--environment", required=True, help="where the measurement was taken")
    parser.add_argument(
        "--commit",
        help=(
            f"the commit an {OPERATOR}-attested proof was taken against, for a deployed build "
            "this repository cannot observe. Never given for a MEASURED record"
        ),
    )
    parser.add_argument("--produced-at", help="ISO date; defaults to today in UTC")
    arguments = parser.parse_args(argv)

    try:
        written = write_record(
            arguments.proof_id,
            [Path(path) for path in arguments.raw],
            attestation=arguments.attestation,
            environment=arguments.environment,
            commit=arguments.commit,
            produced_at=arguments.produced_at,
        )
    except (RecordingRefused, ArtifactRefused) as refusal:
        print(f"careloopbench.record: {refusal}", file=sys.stderr)
        return 1

    # What a reader will now see. Asked of the reader rather than asserted here: a record this
    # module could write and CareLoopBench could not report would be a record filed and lost.
    resolved = ingest_one(claim_for(arguments.proof_id))
    print(f"wrote {written.relative_to(REPO_ROOT).as_posix()}")
    print(f"{arguments.proof_id}  {resolved.status.value}  {resolved.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
