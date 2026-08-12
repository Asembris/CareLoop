"""Reading text out of a PDF, one page at a time.

This is the real extraction path: the fixture documents are PDFs on disk, and everything
downstream — the ``UNTRUSTED`` excerpt facts, the structured values, the ``source: referral.pdf
p.2`` citation in the timeline — is built from what comes back through here. The poisoned
continuity notice reaches the planner by this route and no other, which is what makes T2 a test of
the policy engine rather than of a string literal in a fixture list.

**pdfplumber, and why.** MIT, pure Python, and its unit of iteration is the page — so the page
number on a :class:`careloop.provenance.facts.Provenance` comes from the structure of the API
rather than from splitting a blob on a separator and hoping. It also leaves a positional escape
hatch (``extract_words``, ``extract_table``) for a future field that needs column geometry; nothing
uses it yet, and the documents are laid out as label/value lines so that nothing has to.
``pypdfium2`` was rejected as a large native dependency for six small documents, and ``pypdf`` —
lighter still, and a reasonable fallback — gives no positional escape hatch at all.

**Nothing here cleans anything.** The text is returned exactly as extraction produced it. A
sanitiser at this boundary would be a filter that looks like a defence, and it would make T2 pass
for the wrong reason; the authority lives in the policy engine. The one transformation is ``None``
to ``""`` for a page with no text at all, so that a blank page is a blank page rather than a
``TypeError`` three modules away.

**No clock and no randomness**, per invariant 9: extraction of the same bytes yields the same text,
and a :class:`~careloop.tools.results.Document` carries no timestamp. The reader is a pure function
of the file.
"""

from __future__ import annotations

import io
from importlib import resources
from typing import Final

from careloop.tools.results import Document

#: Package owning the fixture directory, and the directory's name within it.
#:
#: Traversed from ``careloop.tools`` rather than named as ``careloop.tools.fixtures`` directly: the
#: fixture directory holds no ``__init__.py`` and is data rather than code, so requiring it to be
#: importable would make packaging harder for no gain. Read through ``importlib.resources`` rather
#: than ``__file__`` arithmetic so the package still works installed as a zip, and so a missing
#: file fails as a missing *resource* rather than as a path that happened not to exist.
FIXTURE_PACKAGE: Final[str] = "careloop.tools"
FIXTURE_DIR: Final[str] = "fixtures"


class DocumentUnreadable(Exception):
    """Raised when a PDF cannot be read, or yields no text at all.

    A document that extracts to nothing is not evidence, and recording it as an empty page would
    put a fact carrying ``""`` on the case — which trust resolution would then match against every
    argument that also happens to be empty. Failing loudly is the only safe reading.
    """


def extract_pages(data: bytes) -> tuple[str, ...]:
    """Every page of ``data``, as text, in page order.

    Raises:
        DocumentUnreadable: if the bytes are not a readable PDF, or no page carries any text.
    """
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = tuple((page.extract_text() or "") for page in pdf.pages)
    except DocumentUnreadable:
        raise
    except Exception as failure:  # pdfplumber raises a variety of parser-specific errors
        raise DocumentUnreadable(f"could not read the PDF: {type(failure).__name__}") from failure

    if not any(page.strip() for page in pages):
        raise DocumentUnreadable("the PDF yielded no text on any page")
    return pages


def read_fixture(source_doc: str) -> tuple[str, ...]:
    """Every page of the packaged fixture named ``source_doc``.

    Raises:
        DocumentUnreadable: if the fixture is absent or unreadable. Absence is a packaging failure
            — the PDFs are declared package data — and it should say so rather than surfacing as a
            case with no evidence.
    """
    try:
        data = (resources.files(FIXTURE_PACKAGE) / FIXTURE_DIR / source_doc).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as failure:
        raise DocumentUnreadable(
            f"fixture {source_doc!r} is not packaged under "
            f"{FIXTURE_PACKAGE}/{FIXTURE_DIR}; run `python scripts/generate_documents.py`"
        ) from failure
    return extract_pages(data)


def read_document(document_id: str, title: str, source_doc: str) -> Document:
    """Read one packaged fixture into a :class:`~careloop.tools.results.Document`."""
    return Document(
        document_id=document_id,
        title=title,
        source_doc=source_doc,
        pages=read_fixture(source_doc),
    )


__all__ = [
    "FIXTURE_DIR",
    "FIXTURE_PACKAGE",
    "DocumentUnreadable",
    "extract_pages",
    "read_document",
    "read_fixture",
]
