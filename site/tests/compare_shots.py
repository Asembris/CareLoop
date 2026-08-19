"""Pixel-compare the fidelity screenshot pairs produced by ``fidelity.mjs``.

    node fidelity.mjs --out <dir> && python compare_shots.py <dir>

Every pair is one named state captured from the pristine bundle and from the adapted page, with
the live topology hidden so the comparison measures the change rather than the clock. The bar is
**zero differing pixels**: the adaptation touches a stub head the document swap discards, three
lightbox URLs that are not on screen in any of these states, and two dead preconnects. None of that
can move a pixel, so anything above zero is a real finding and is printed rather than tolerated.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops


def compare(pristine: Path, adapted: Path) -> tuple[int, int, float]:
    """Return (differing pixels, total pixels, percentage)."""
    with Image.open(pristine) as a_img, Image.open(adapted) as b_img:
        a = a_img.convert("RGB")
        b = b_img.convert("RGB")
        if a.size != b.size:
            raise SystemExit(f"size mismatch: {pristine.name} {a.size} vs {adapted.name} {b.size}")
        diff = ImageChops.difference(a, b)
        total = a.size[0] * a.size[1]
        differing = sum(1 for px in diff.getdata() if px != (0, 0, 0))
        return differing, total, (differing / total * 100) if total else 0.0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    out = Path(argv[1])
    pairs = sorted(out.glob("*.pristine.png"))
    if not pairs:
        print(f"no screenshot pairs in {out}")
        return 2

    worst = 0.0
    failures = 0
    print(f"\ncomparing {len(pairs)} states\n")
    print(f"  {'state':30s} {'differing':>12s} {'of total':>12s}  {'%':>8s}")
    for pristine in pairs:
        adapted = pristine.with_name(pristine.name.replace(".pristine.png", ".adapted.png"))
        if not adapted.exists():
            print(f"  {pristine.stem:30s} MISSING COUNTERPART")
            failures += 1
            continue
        differing, total, pct = compare(pristine, adapted)
        state = pristine.name.replace(".pristine.png", "")
        flag = "" if differing == 0 else "   <-- DIFFERS"
        print(f"  {state:30s} {differing:12,d} {total:12,d}  {pct:7.4f}%{flag}")
        worst = max(worst, pct)
        if differing:
            failures += 1

    print()
    if failures:
        print(f"{failures} state(s) differ; worst {worst:.4f}%")
        return 1
    print(f"every state is pixel-identical ({len(pairs)} pairs, 0 differing pixels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
