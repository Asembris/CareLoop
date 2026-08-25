"""One canonical result, and a human view derived from it.

**Nothing here is written down.** Every count in both outputs is a comprehension over the result
set: there is no total, no denominator and no headline typed into this module, and
``tests/test_careloopbench.py`` enforces that by refusing any integer literal other than ``0`` and
``1``. A benchmark whose renderer carried its own numbers would be the hand-maintained ledger this
layer exists to replace, one directory further in.

**The Markdown is a projection of the JSON and never a second source.** ``render_markdown`` takes
the canonical payload — the same mapping that is written to disk — so "the human report agrees
with the machine-readable result" is true by construction and is asserted byte for byte rather
than approximately. Nothing reads the registry a second time to draw a row.

**Statuses are never folded.** A domain's numerator counts ``PROVEN`` and nothing else; every
other status is reported beside it, with its own name, on its own line. ``PROVEN_AT`` is a live
proof standing at its own commit, ``STALE`` is one that stopped being about this code, ``NOT_RUN``
is a claim this tier did not touch and ``NOT_APPLICABLE`` is one that declares itself out of
scope here — and none of the four is a pass.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from careloopbench.claims import CLAIMS, Claim
from careloopbench.levels import PROVEN_STATUSES, SHORT_COMMIT, TIERS, Domain, Status
from careloopbench.runner import AdversarialEvidence, EvidenceRun, ReplaySide

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The name a reader sees for each domain. A label, so the enum stays the identifier.
DOMAIN_TITLE: Final[dict[Domain, str]] = {
    Domain.AUTHORITY_INTEGRITY: "Authority integrity",
    Domain.EXTERNAL_EFFECT_SAFETY: "External-effect safety",
    Domain.SETTLEMENT_INTEGRITY: "Settlement integrity",
    Domain.IDENTITY_AND_ISOLATION: "Identity and isolation",
    Domain.ADVERSARIAL_ROBUSTNESS: "Adversarial robustness",
    Domain.RECOVERY_AND_LIVENESS: "Recovery and liveness",
    Domain.BOUNDARY_FIDELITY: "Boundary fidelity",
}

UNKNOWN_COMMIT: Final[str] = "unknown"


@dataclass(frozen=True)
class ClaimResult:
    """What this invocation is entitled to say about one claim."""

    claim: Claim
    status: Status
    detail: str = ""

    @property
    def is_proven(self) -> bool:
        return self.status in PROVEN_STATUSES


@dataclass(frozen=True)
class BenchResult:
    """One whole invocation. The canonical payload is derived from this and from nothing else."""

    tier: str
    head_commit: str
    registry_digest: str
    results: tuple[ClaimResult, ...]
    adversarial: AdversarialEvidence | None
    tests_run: int

    @property
    def failed(self) -> tuple[ClaimResult, ...]:
        return tuple(result for result in self.results if result.status is Status.FAILED)


def head_commit(root: Path = REPO_ROOT) -> str:
    """The commit this invocation ran against, or ``unknown`` outside a checkout."""
    try:
        finished = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return UNKNOWN_COMMIT
    return finished.stdout.strip() or UNKNOWN_COMMIT


def registry_digest(claims: Sequence[Claim] = CLAIMS) -> str:
    """A digest over what the registry declares.

    Committed alongside the rendered documents so that editing the registry without regenerating
    them is a red test rather than a page that quietly describes a different set of claims.
    """
    lines = [
        "|".join(
            (
                claim.id,
                claim.domain.value,
                claim.level.value,
                claim.question,
                claim.scope,
                ",".join(claim.evidence),
                ",".join(claim.counter_evidence),
                claim.artifact.proof_id if claim.artifact else "",
            )
        )
        for claim in claims
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def evaluate(
    claims: Sequence[Claim],
    *,
    tier: str,
    run: EvidenceRun | None,
    live: Mapping[str, ClaimResult] | None = None,
) -> tuple[ClaimResult, ...]:
    """Turn evidence outcomes into one status per claim.

    A claim is ``PROVEN`` only when every node it declares — evidence *and* counter-evidence —
    passed in this invocation. Anything else about an executed claim is ``FAILED`` or ``NOT_RUN``,
    and the two are kept apart because one means a defect and the other means nobody looked.
    """
    levels = TIERS[tier]
    settled = dict(live or {})
    results: list[ClaimResult] = []

    for claim in claims:
        if not claim.is_executed:
            results.append(
                settled.get(claim.id, ClaimResult(claim, Status.NOT_RUN, "no stored proof"))
            )
            continue
        if claim.level not in levels or run is None:
            results.append(
                ClaimResult(claim, Status.NOT_RUN, f"{claim.level.value} is outside {tier}")
            )
            continue
        failing = run.failing(claim.nodes)
        if failing:
            results.append(ClaimResult(claim, Status.FAILED, "; ".join(failing)))
        else:
            results.append(ClaimResult(claim, Status.PROVEN))

    return tuple(results)


def _side_payload(side: ReplaySide) -> dict[str, Any]:
    return {
        "system": side.system,
        "attempts": side.attempts,
        "unauthorized": side.unauthorized,
        "refused": side.refused,
        "effects": side.effects,
        "errored": side.errored,
    }


def to_payload(result: BenchResult) -> dict[str, Any]:
    """The canonical machine-readable result. Everything else is derived from this."""
    return {
        "careloopbench": {
            "tier": result.tier,
            "generated_at_commit": result.head_commit,
            "registry_digest": result.registry_digest,
            "tests_run": result.tests_run,
        },
        "domains": [
            {
                "domain": domain.value,
                "title": DOMAIN_TITLE[domain],
                "declared": len(_in(result, domain)),
                "proven": len([row for row in _in(result, domain) if row.is_proven]),
                "mutation_backed": len(
                    [row for row in _in(result, domain) if row.claim.mutation_backed]
                ),
                "by_status": {
                    status.value: len([row for row in _in(result, domain) if row.status is status])
                    for status in Status
                    if any(row.status is status for row in _in(result, domain))
                },
            }
            for domain in Domain
        ],
        "claims": [
            {
                "id": row.claim.id,
                "question": row.claim.question,
                "domain": row.claim.domain.value,
                "level": row.claim.level.value,
                "status": row.status.value,
                "detail": row.detail,
                "scope": row.claim.scope,
                "mutation_backed": row.claim.mutation_backed,
                "evidence": list(row.claim.evidence),
                "counter_evidence": list(row.claim.counter_evidence),
                "artifact": (
                    {
                        "proof_id": row.claim.artifact.proof_id,
                        "path": row.claim.artifact.path,
                        "tool": row.claim.artifact.tool,
                        "surface": list(row.claim.artifact.surface),
                    }
                    if row.claim.artifact
                    else None
                ),
            }
            for row in result.results
        ],
        "adversarial": (
            {
                "protected": _side_payload(result.adversarial.protected),
                "allow_all": _side_payload(result.adversarial.allow_all),
                "is_contrasting": result.adversarial.is_contrasting,
            }
            if result.adversarial
            else None
        ),
    }


def _in(result: BenchResult, domain: Domain) -> tuple[ClaimResult, ...]:
    return tuple(row for row in result.results if row.claim.domain is domain)


def build(
    *,
    tier: str,
    claims: Sequence[Claim] = CLAIMS,
    run: EvidenceRun | None,
    adversarial: AdversarialEvidence | None,
    live: Mapping[str, ClaimResult] | None = None,
    root: Path = REPO_ROOT,
) -> BenchResult:
    """Assemble one invocation's result from the pieces the caller gathered."""
    return BenchResult(
        tier=tier,
        head_commit=head_commit(root),
        registry_digest=registry_digest(claims),
        results=evaluate(claims, tier=tier, run=run, live=live),
        adversarial=adversarial,
        tests_run=run.tests if run else 0,
    )


def _bar(rows: Sequence[Mapping[str, Any]], key: str) -> int:
    """The width of the widest value under ``key``, so no column is a written-down number."""
    return max((len(str(row[key])) for row in rows), default=len(key))


def render_markdown(payload: Mapping[str, Any]) -> str:
    """The human view, projected from the canonical payload and from nothing else."""
    header = payload["careloopbench"]
    lines: list[str] = [
        "# CareLoopBench",
        "",
        "Generated by `python -m careloopbench`. **Do not edit this file** — it is a projection of",
        "`docs/evidence.json`, which is a projection of the claim registry in",
        "`careloopbench/claims.py`. Every count below is derived; none is written down.",
        "",
        f"| tier | `{header['tier']}` |",
        "| --- | --- |",
        f"| generated at commit | `{header['generated_at_commit'][:SHORT_COMMIT]}` |",
        f"| registry digest | `{header['registry_digest'][:SHORT_COMMIT]}` |",
        f"| tests executed | {header['tests_run']} |",
        "",
        "A domain's numerator counts `PROVEN` only. `NOT_RUN`, `STALE`, `PROVEN_AT` and",
        "`NOT_APPLICABLE` are none of them a pass and are reported separately.",
        "",
        "## Property domains",
        "",
        "| domain | proven | declared | mutation-backed | other |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for domain in payload["domains"]:
        other = ", ".join(
            f"{status} {count}"
            for status, count in domain["by_status"].items()
            if status != Status.PROVEN.value
        )
        lines.append(
            f"| {domain['title']} | **{domain['proven']}** | {domain['declared']} "
            f"| {domain['mutation_backed']} | {other or '—'} |"
        )

    proven = len([row for row in payload["claims"] if row["status"] == Status.PROVEN.value])
    backed = len([row for row in payload["claims"] if row["mutation_backed"]])
    lines += [
        "",
        f"**{proven} of {len(payload['claims'])} declared properties proven in this invocation.** "
        f"{backed} of {len(payload['claims'])} are mutation-backed: something in the suite asserts "
        "the defect reappears when the defence is removed.",
        "",
    ]

    adversarial = payload["adversarial"]
    if adversarial:
        lines += [
            "## Adversarial replay",
            "",
            "Every committed attack replayed through the real graph, the real `rules.yaml`",
            "and both enforcement points, driven by a planner in full compliance with the",
            "attacker.",
            "",
            "| target | unauthorized | attempts | refused | external effects |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for side in (adversarial["protected"], adversarial["allow_all"]):
            lines.append(
                f"| {side['system']} | **{side['unauthorized']}** | {side['attempts']} "
                f"| {side['refused']} | {side['effects']} |"
            )
        lines += [
            "",
            "The contrast is the evidence; the zero on its own would not be.",
            "",
        ]

    live_rows = [row for row in payload["claims"] if row["artifact"]]
    lines += ["## Live evidence", ""]
    if live_rows:
        lines += [
            "Measured once, elsewhere, by somebody running a tool. **Each row certifies the commit",
            "it names, never this one.**",
            "",
            "| proof | status | evidence | tool | dependency surface |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in live_rows:
            surface = ", ".join(f"`{path}`" for path in row["artifact"]["surface"])
            lines.append(
                f"| {row['artifact']['proof_id']} | **{row['status']}** "
                f"| {row['detail'] or '—'} | `{row['artifact']['tool']}` | {surface} |"
            )
    else:
        lines.append("No live proof is declared in this registry.")
    lines.append("")

    lines += ["## Every claim", ""]
    for domain in payload["domains"]:
        rows = [row for row in payload["claims"] if row["domain"] == domain["domain"]]
        lines += [
            f"### {domain['title']}",
            "",
            "| claim | question | level | status | evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            evidence = row["evidence"] + row["counter_evidence"]
            shown = "<br>".join(f"`{node}`" for node in evidence) or f"`{row['artifact']['path']}`"
            lines.append(
                f"| `{row['id']}` | {row['question']} | {row['level']} "
                f"| **{row['status']}** | {shown} |"
            )
        lines.append("")
        for row in rows:
            lines.append(f"- `{row['id']}` — scope: {row['scope']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_console(payload: Mapping[str, Any]) -> str:
    """The terminal view. Same payload, fewer words."""
    header = payload["careloopbench"]
    width = _bar(payload["domains"], "title")
    lines = [
        "",
        f"CARELOOPBENCH   tier {header['tier']}"
        f"   commit {header['generated_at_commit'][:SHORT_COMMIT]}",
        "",
    ]
    for domain in payload["domains"]:
        other = " ".join(
            f"{status} {count}"
            for status, count in domain["by_status"].items()
            if status != Status.PROVEN.value
        )
        lines.append(
            f"  {domain['title']:<{width}}  {domain['proven']:>3} / {domain['declared']:<3}"
            f"  mutation-backed {domain['mutation_backed']:<3} {other}"
        )

    adversarial = payload["adversarial"]
    if adversarial:
        lines += ["", "  ADVERSARIAL REPLAY"]
        for side in (adversarial["protected"], adversarial["allow_all"]):
            lines.append(
                f"  {side['system']:<{width}}  {side['unauthorized']:>3} / {side['attempts']:<3}"
                f"  unauthorized   effects {side['effects']}"
            )

    live_rows = [row for row in payload["claims"] if row["artifact"]]
    if live_rows:
        lines += ["", "  LIVE EVIDENCE"]
        for row in live_rows:
            lines.append(
                f"  {row['artifact']['proof_id']:<{width}}  {row['status']:<14} {row['detail']}"
            )

    failed = [row for row in payload["claims"] if row["status"] == Status.FAILED.value]
    if failed:
        lines += ["", "  FAILED"]
        lines += [f"  {row['id']}  {row['detail']}" for row in failed]

    lines.append("")
    return "\n".join(lines)


__all__ = [
    "BenchResult",
    "ClaimResult",
    "build",
    "evaluate",
    "head_commit",
    "registry_digest",
    "render_console",
    "render_markdown",
    "to_payload",
]
