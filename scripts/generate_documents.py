"""Render the fixture PDFs from their declared source.

``careloop/tools/document_source.py`` holds every line of every fixture document. This script turns
that text into the PDFs under ``careloop/tools/fixtures/``, which is what
``careloop/tools/pdf.py`` reads at runtime. The PDFs are committed so that an ordinary
``pip install careloop`` carries them, and they are regenerable so that no reviewer has to take a
binary on trust.

**Output is byte-identical across runs.** ``reportlab``'s ``invariant=1`` suppresses the
``/CreationDate`` and trailer ``/ID`` stamps, both of which are clocks. Verified by execution
against ``reportlab==5.0.0`` before this script was written: with the flag, two runs produce the
same sha256; without it, they do not. That is what makes ``--check`` a real drift detector rather
than a comparison that can only ever fail.

**A line that would not fit is an error, not a line that runs off the page.** ``drawString`` does
not wrap and does not clip: an over-long line leaves the page but stays in the content stream, so
extraction would still find it and nothing would look wrong until a human opened the file. Since
the poisoned notice's exfiltration address must survive extraction as one contiguous token, the
generator refuses to write a document containing a line it cannot lay out.

Usage::

    python scripts/generate_documents.py            # write the PDFs
    python scripts/generate_documents.py --check    # fail if the committed PDFs have drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from careloop.tools.document_source import DOCUMENT_SOURCES, DocumentSource

#: A4, in points.
PAGE_SIZE: Final[tuple[float, float]] = (595.27, 841.89)

MARGIN_LEFT: Final[float] = 56.0
MARGIN_RIGHT: Final[float] = 56.0
TOP_BASELINE: Final[float] = 780.0
LEADING: Final[float] = 14.0

FONT: Final[str] = "Helvetica"
FONT_BOLD: Final[str] = "Helvetica-Bold"
FONT_SIZE: Final[float] = 10.0

#: Widest line the frame admits.
USABLE_WIDTH: Final[float] = PAGE_SIZE[0] - MARGIN_LEFT - MARGIN_RIGHT

#: Where the rendered PDFs live, as package data.
FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "careloop" / "tools" / "fixtures"


class LineTooWide(Exception):
    """Raised when a declared line does not fit the page frame.

    ``drawString`` would happily place it and let it run past the margin, where it stays in the
    content stream and is still extracted. The document would be wrong only to a human eye, which
    is the worst place for it to be wrong.
    """


def render(document: DocumentSource) -> bytes:
    """Render one document to PDF bytes, deterministically.

    Raises:
        LineTooWide: if any line exceeds the usable frame width.
    """
    # Imported here rather than at module scope: reportlab is a `dev` dependency, and importing it
    # eagerly would make this module unimportable in a tree that only needs to read the PDFs.
    import io

    from reportlab.pdfgen.canvas import Canvas

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=PAGE_SIZE, invariant=1)
    canvas.setTitle(document.title)
    canvas.setAuthor("CareLoop synthetic fixture")
    canvas.setSubject(str(document.kind))

    for page_number in range(1, document.page_count + 1):
        baseline = TOP_BASELINE
        for text, bold in document.rendered(page_number):
            if text:
                font = FONT_BOLD if bold else FONT
                width = canvas.stringWidth(text, font, FONT_SIZE)
                if width > USABLE_WIDTH:
                    raise LineTooWide(
                        f"{document.source_doc} page {page_number}: {text!r} is {width:.1f}pt "
                        f"wide and the frame is {USABLE_WIDTH:.1f}pt; split the line in "
                        f"DOCUMENT_SOURCES rather than letting it run off the page"
                    )
                canvas.setFont(font, FONT_SIZE)
                canvas.drawString(MARGIN_LEFT, baseline, text)
            baseline -= LEADING
        canvas.showPage()

    canvas.save()
    return buffer.getvalue()


def render_all() -> dict[str, bytes]:
    """Every document, keyed by its filename."""
    return {document.source_doc: render(document) for document in DOCUMENT_SOURCES}


def write_all(destination: Path = FIXTURES_DIR) -> list[Path]:
    """Write every rendered document into ``destination``, returning the paths written."""
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, data in render_all().items():
        path = destination / filename
        path.write_bytes(data)
        written.append(path)
    return written


def drifted(destination: Path = FIXTURES_DIR) -> list[str]:
    """Filenames whose committed bytes differ from what the source renders to now.

    A missing file counts as drift. So does an extra one: a PDF in the fixtures directory that no
    longer has a declared source is a document nothing can regenerate, which is exactly the opaque
    binary this arrangement exists to avoid.
    """
    rendered = render_all()
    problems = [
        filename
        for filename, data in rendered.items()
        if not (destination / filename).is_file() or (destination / filename).read_bytes() != data
    ]
    problems += [
        path.name for path in sorted(destination.glob("*.pdf")) if path.name not in rendered
    ]
    return sorted(problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if the committed PDFs differ from the source",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=FIXTURES_DIR,
        help=f"destination directory (default: {FIXTURES_DIR})",
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = drifted(args.out)
        if problems:
            print("fixture PDFs have drifted from their source:", file=sys.stderr)
            for filename in problems:
                print(f"  {filename}", file=sys.stderr)
            print(
                "run `python scripts/generate_documents.py` and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"{len(DOCUMENT_SOURCES)} fixture PDFs match their source")
        return 0

    for path in write_all(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
