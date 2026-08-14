"""Recover DeepTeam-generated attack payloads from the committed response cache.

**Why this exists.** Session G's paid run generated attacks and then lost its own results table to
a Windows console encoding error, and a later attempt was defeated by provider throttling. The
generated *payloads*, though, were already in `evals/cache/` — the simulator's responses are cached
at the model boundary, which is what the cache was for. This turns those responses into a committed
corpus without buying a single further call.

**What it cannot recover, and why that is not a defect to be fixed.** The cache records the role,
the model, the schema and the response; it does not record which vulnerability class prompted the
generation. That is not an oversight in the cache — a `DeepEvalBaseLLM` is called with a prompt and
a schema and **nothing else**, so the adapter never learns the vulnerability. Attribution would
require parsing DeepTeam's internal prompt text, which is unstable across releases and would be a
worse thing to depend on than an honest `unattributed`.

So the mined records say `unattributed` for the vulnerability and technique, and carry the
simulator model, the DeepTeam version and the date, which are the provenance facts that *are*
knowable. `docs/security-evaluation.md` counts them separately from the authored baseline.

Run:

    python scripts/mine_attack_cache.py --simulator meta/llama-3.3-70b-instruct \\
        --deepteam-version 1.0.9 --generated-at 2026-08-14
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from evals.cache import CACHE_DIR
from evals.corpus import CORPUS_DIR, AttackOrigin, AttackRecord, write_corpus

#: Cached schemas whose successful output carries an attack payload.
#:
#: ``SyntheticDataList`` is the simulator's baseline attack for a vulnerability type;
#: ``TransformedAttack`` is that attack after an attack technique has been applied to it. Both are
#: genuine generated adversarial inputs and both are kept — the transformed ones are the ones a
#: technique actually produced, and the baselines are what the technique started from.
PAYLOAD_SCHEMAS: dict[str, str] = {
    "SyntheticDataList": "baseline",
    "TransformedAttack": "transformed",
}

#: Payloads shorter than this are not attacks; they are fragments of a failed generation.
MIN_PAYLOAD_CHARS = 40


def payloads_from(entry: dict) -> list[str]:
    """Every attack string in one cached response."""
    output = entry.get("output")
    if not entry.get("ok") or not isinstance(output, dict):
        return []

    if entry["schema_name"] == "SyntheticDataList":
        return [
            item["input"]
            for item in output.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("input"), str)
        ]
    if entry["schema_name"] == "TransformedAttack":
        text = output.get("input")
        return [text] if isinstance(text, str) else []
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True, help="model id that generated these")
    parser.add_argument("--deepteam-version", required=True)
    parser.add_argument("--generated-at", required=True, help="ISO date, e.g. 2026-08-14")
    parser.add_argument("--cache", type=pathlib.Path, default=CACHE_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=CORPUS_DIR / "generated.json")
    args = parser.parse_args(argv)

    seen: set[str] = set()
    records: list[AttackRecord] = []
    failures = 0

    for path in sorted(args.cache.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("schema_name") in PAYLOAD_SCHEMAS and not entry.get("ok"):
            failures += 1
        for payload in payloads_from(entry):
            text = payload.strip()
            if len(text) < MIN_PAYLOAD_CHARS or text in seen:
                continue
            seen.add(text)
            records.append(
                AttackRecord(
                    # Derived from the payload so re-mining the same cache produces the same ids
                    # and the committed file diffs cleanly rather than churning every run.
                    attack_id=f"generated-{path.stem[:12]}-{len(seen):03d}",
                    payload=text,
                    vulnerability="unattributed",
                    vulnerability_type="unattributed",
                    attack_technique=PAYLOAD_SCHEMAS[entry["schema_name"]],
                    origin=AttackOrigin.GENERATED,
                    simulator_model=args.simulator,
                    deepteam_version=args.deepteam_version,
                    generated_at=args.generated_at,
                )
            )

    if not records:
        print("no payloads found in the cache", file=sys.stderr)
        return 1

    written = write_corpus(records, args.out)
    print(f"Recovered {written} generated payload(s) from {len(list(args.cache.glob('*.json')))}")
    print(f"  cached generation failures skipped: {failures}")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
