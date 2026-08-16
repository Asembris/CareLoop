"""Render ``docs/architecture.svg`` to PNG and PDF.

**The SVG stays the editable source; these are derived artefacts**, the same arrangement the fixture
PDFs have — ``careloop/tools/document_source.py`` holds the text and ``generate_documents.py``
renders it. Edit the SVG, run this, commit all three.

They exist because Devpost's architecture field accepts PDF, PPT, PPTX, PNG, JPG and JPEG — and not
SVG. A diagram nobody can upload is a diagram that does not count.

**Rendered by a headless Chromium**, found on PATH or in the usual Windows locations, because that
is the renderer the SVG was drawn against: it uses system font stacks and text anchoring, and a
converter with different font metrics silently re-lays-out every label. This is *not* run by the
test suite and is not a CI step — it needs a browser, and asserting on the bytes of a PNG produced
by whichever Chromium a machine happens to have would be a test about the machine.

    python scripts/export_diagram.py

``--scale`` is the PNG's pixel multiplier. 3 gives 3720 x 2850, which is large enough that the zone
labels and node names stay legible when a judge opens it full-screen.
"""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = REPO_ROOT / "docs" / "architecture.svg"

#: Where a Chromium lives on the platforms this has been run on. PATH first.
CANDIDATES = (
    "chrome",
    "chromium",
    "chromium-browser",
    "msedge",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)


def find_browser() -> str:
    """A Chromium to render with, or a message naming what was looked for."""
    for candidate in CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    raise SystemExit("no Chromium found to render with; looked for: " + ", ".join(CANDIDATES))


def dimensions(svg: str) -> tuple[int, int]:
    """The diagram's own width and height, so nothing here has to be told them twice."""
    width = re.search(r'\swidth="(\d+)"', svg)
    height = re.search(r'\sheight="(\d+)"', svg)
    if not width or not height:
        raise SystemExit(f"{DIAGRAM} declares no width and height to render at")
    return int(width.group(1)), int(height.group(1))


def page(svg: str, width: int, height: int, scale: int) -> str:
    """The SVG on a page of exactly its own size, so nothing is cropped or padded.

    Embedded as a data URI rather than referenced, so the render does not depend on where the
    temporary file was written or on a browser's local-file rules.
    """
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: {width}px {height}px; margin: 0; }}
  html, body {{ margin: 0; padding: 0; background: #ffffff; }}
  img {{ display: block; width: {width * scale}px; height: {height * scale}px; }}
</style></head>
<body><img src="data:image/svg+xml;base64,{encoded}"></body></html>
"""


def render(browser: str, html: Path, flag: str, size: tuple[int, int]) -> None:
    subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--window-size={size[0]},{size[1]}",
            flag,
            html.resolve().as_uri(),
        ],
        check=True,
        capture_output=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the architecture diagram to PNG and PDF.")
    parser.add_argument("--scale", type=int, default=3, help="PNG pixel multiplier")
    arguments = parser.parse_args()

    browser = find_browser()
    svg = DIAGRAM.read_text(encoding="utf-8")
    width, height = dimensions(svg)

    with tempfile.TemporaryDirectory() as work:
        workdir = Path(work)
        png_page = workdir / "png.html"
        png_page.write_text(page(svg, width, height, arguments.scale), encoding="utf-8")
        render(
            browser,
            png_page,
            f"--screenshot={DIAGRAM.with_suffix('.png')}",
            (width * arguments.scale, height * arguments.scale),
        )

        # The PDF is rendered at 1x: it is vector output sized by @page, so a multiplier would only
        # make the paper bigger.
        pdf_page = workdir / "pdf.html"
        pdf_page.write_text(page(svg, width, height, 1), encoding="utf-8")
        render(
            browser,
            pdf_page,
            f"--print-to-pdf={DIAGRAM.with_suffix('.pdf')}",
            (width, height),
        )

    for output in (DIAGRAM.with_suffix(".png"), DIAGRAM.with_suffix(".pdf")):
        if not output.exists():
            raise SystemExit(f"{browser} produced no {output.name}")
        print(f"{output.relative_to(REPO_ROOT)}  {output.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":  # pragma: no cover - a tool, not a call site
    sys.exit(main())
