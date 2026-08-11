"""What tools return.

Every tool returns a typed result. None returns free text, and that is a structural choice rather
than a stylistic one: a free-text return is a channel through which the content of an external
system — a bounce message, a portal error page, a document body — reaches the planner as prose it
can read as instruction. A typed result has named fields with known meanings, and a graph node
decides what, if anything, of it the model ever sees.

Results are frozen and carry **no timestamp and no random identifier**. Synthetic ids derive from
the idempotency key, so replaying a suppressed duplicate returns a value byte-identical to the
first. An idempotency property asserted against a result that carries a clock is an approximate
property.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ToolResult(BaseModel):
    """Base of every typed tool return value."""

    model_config = ConfigDict(frozen=True)


class ActionResult(ToolResult):
    """Base of every result produced by an external effect.

    Separate from :class:`ToolResult` so that "this value came out of the executor" is a type-level
    fact. ``fetch_document`` reads and returns a :class:`Document`, which is deliberately not an
    ``ActionResult``.
    """


class EmailSent(ActionResult):
    """An email was accepted for delivery."""

    message_id: str
    to: str
    subject: str


class AppointmentBooked(ActionResult):
    """A slot was reserved with a provider."""

    confirmation_id: str
    provider_id: str
    patient_id: str
    slot_start: str
    """Canonical text form of the slot, never a ``datetime``, so the record is byte-stable."""


class DocumentUploaded(ActionResult):
    """A document was delivered to a provider portal."""

    upload_id: str
    portal_id: str
    document_id: str


class AccessGranted(ActionResult):
    """A person was granted a role on a provider portal."""

    grant_id: str
    grantee: str
    portal_id: str
    role: str


class Document(ToolResult):
    """A document CareLoop already holds.

    ``pages`` is a tuple so a :class:`careloop.provenance.facts.Provenance` page number refers to
    something real. The text is content from the outside world: it is evidence, never instruction.
    """

    document_id: str
    title: str
    source_doc: str
    """Filename or message id the content came from, recorded on facts extracted from it."""

    pages: tuple[str, ...]

    @property
    def text(self) -> str:
        """Every page, joined. Convenience for extractors that do not care about pagination."""
        return "\n".join(self.pages)


__all__ = [
    "AccessGranted",
    "ActionResult",
    "AppointmentBooked",
    "Document",
    "DocumentUploaded",
    "EmailSent",
    "ToolResult",
]
