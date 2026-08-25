"""``python -m careloopbench`` — run the declared evidence, ingest what was measured elsewhere.

Two tiers and no third::

    python -m careloopbench --tier quick            # property, integration and the replay
    python -m careloopbench --tier core             # the above plus the real-protocol proofs
    python -m careloopbench --tier quick --check    # non-zero if any claim FAILED. What CI runs.
    python -m careloopbench --tier core --out docs/evidence.json --render docs/evidence.md

There is deliberately no ``full``. "Everything" is ``python -m pytest -q``, which CI already runs
on every push; a tier that duplicated it would be the whole-repository rerun this layer exists not
to be.

``--check`` fails on ``FAILED`` and on nothing else. A live proof that has gone ``STALE`` is not a
breakage — it is a true statement about a proof that stopped being about this code — and a tier
that did not touch a claim has not discovered anything about it either.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from careloopbench.claims import CLAIMS, evidence_nodes, selected
from careloopbench.ingest import ArtifactRefused, live_results
from careloopbench.levels import TIERS
from careloopbench.report import build, render_console, render_markdown, to_payload
from careloopbench.runner import EvidenceDoesNotCollect, run_adversarial_replay, run_evidence

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BROKEN = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="careloopbench", description=__doc__)
    parser.add_argument("--tier", choices=sorted(TIERS), default="core")
    parser.add_argument("--out", type=Path, help="write the canonical JSON result here")
    parser.add_argument("--render", type=Path, help="write the Markdown projection here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any claim FAILED (never for STALE or NOT_RUN)",
    )
    arguments = parser.parse_args(argv)

    covered = selected(CLAIMS, TIERS[arguments.tier])
    try:
        run = run_evidence(evidence_nodes(covered))
    except EvidenceDoesNotCollect as broken:
        print(f"careloopbench: {broken}", file=sys.stderr)
        return EXIT_BROKEN

    try:
        live = live_results()
    except ArtifactRefused as refused:
        print(f"careloopbench: {refused}", file=sys.stderr)
        return EXIT_BROKEN

    result = build(
        tier=arguments.tier,
        run=run,
        adversarial=run_adversarial_replay(),
        live=live,
    )
    payload = to_payload(result)

    if arguments.out:
        arguments.out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if arguments.render:
        arguments.render.write_text(render_markdown(payload), encoding="utf-8")

    print(render_console(payload))

    if arguments.check and result.failed:
        print(f"careloopbench: {len(result.failed)} claim(s) FAILED", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
