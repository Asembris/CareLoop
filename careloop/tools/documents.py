"""The document store behind ``fetch_document``.

``fetch_document`` is the one tool with no external effect: it reads content CareLoop already
holds. It therefore does not route through the executor, takes no idempotency key, and appears in
no ledger — reading twice is not a duplicated effect, and pretending otherwise would blur what the
ledger means.

**The store is backed by real PDFs.** Six of them, under ``careloop/tools/fixtures/``, rendered
from :mod:`careloop.tools.document_source` and read through :mod:`careloop.tools.pdf`. Until
commit 7 the pages were string literals in this module, which made every claim about extraction —
page-level provenance, the poisoned payload surviving the parse — a claim about a Python tuple.
Now the whole path runs: bytes on disk, pdfplumber, page text, facts.

Real document *retrieval* is still out of scope; what matters is that documents are the system's
primary source of **untrusted** content, and that the content arrives the way it would in
production rather than the way a test would like it to.

One fixture is adversarial on purpose. See :data:`POISONED_DOCUMENT_ID`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from functools import lru_cache
from typing import Final

from careloop.tools.document_source import (
    DOCUMENT_SOURCES,
    POISONED_DOCUMENT_ID,
    RECEIPT_DOCUMENT_ID,
)
from careloop.tools.pdf import read_document
from careloop.tools.results import Document


class DocumentNotFound(Exception):
    """Raised when a document id is not held by the store."""


class DocumentStore:
    """An immutable collection of :class:`Document`, keyed by ``document_id``."""

    def __init__(self, documents: Iterable[Document]) -> None:
        by_id: dict[str, Document] = {}
        for document in documents:
            if document.document_id in by_id:
                raise ValueError(f"document {document.document_id!r} is declared twice")
            by_id[document.document_id] = document
        self._by_id: Final[dict[str, Document]] = by_id

    def __contains__(self, document_id: object) -> bool:
        return document_id in self._by_id

    def __iter__(self) -> Iterator[Document]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, document_id: str) -> Document:
        """Return the document, or raise.

        Raises:
            DocumentNotFound: if the store does not hold ``document_id``. Raising rather than
                returning ``None`` keeps a missing document from being read as an empty one.
        """
        document = self._by_id.get(document_id)
        if document is None:
            raise DocumentNotFound(f"no document {document_id!r}")
        return document

    @property
    def ids(self) -> frozenset[str]:
        """Every document id the store holds."""
        return frozenset(self._by_id)


@lru_cache(maxsize=1)
def fixture_documents() -> tuple[Document, ...]:
    """Every shipped document, read from its PDF, in declaration order.

    Cached, and a function rather than the module constant it used to be. Parsing six PDFs at
    import time would put file I/O in the import graph of anything that touches
    ``careloop.tools``; caching keeps the parse to once per process, which is what it was when the
    pages were literals.

    The cache holds :class:`Document` instances, which are frozen, so sharing them between stores
    is safe.
    """
    return tuple(
        read_document(
            document_id=declared.document_id,
            title=declared.title,
            source_doc=declared.source_doc,
        )
        for declared in DOCUMENT_SOURCES
    )


@lru_cache(maxsize=1)
def default_document_store() -> DocumentStore:
    """The store built from the shipped fixtures."""
    return DocumentStore(fixture_documents())


__all__ = [
    "POISONED_DOCUMENT_ID",
    "RECEIPT_DOCUMENT_ID",
    "DocumentNotFound",
    "DocumentStore",
    "default_document_store",
    "fixture_documents",
]
