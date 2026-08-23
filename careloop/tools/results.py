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

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DocumentKind(StrEnum):
    """What a document is.

    Metadata, not an extracted value: CareLoop knows a referral is a referral because whoever
    filed it said so, and inferring the kind from the text would make an attacker-controlled
    document able to choose which schema validates it.

    It decides which patterns are applied and which schema the extracted fields are checked
    against, so it is a required field rather than one with a default — a document of unknown kind
    should fail loudly rather than quietly skip validation.
    """

    REFERRAL = "REFERRAL"
    CONTINUITY_NOTICE = "CONTINUITY_NOTICE"
    APPOINTMENT_INSTRUCTIONS = "APPOINTMENT_INSTRUCTIONS"
    EXPLANATION_OF_BENEFITS = "EXPLANATION_OF_BENEFITS"
    CLAIM_REJECTION = "CLAIM_REJECTION"
    RECEIPT = "RECEIPT"


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
    """A slot was reserved with a provider.

    Every declared argument of ``book_appointment`` appears here, because every one of them is
    persisted by the clinic and returned by the independent read-back — so every one of them can be
    compared against the exact value CareLoop executed before the case may reach ``DONE``. Fields
    the *service* might grow are a different matter and are still dropped at the boundary; see
    :func:`careloop.tools.clinic._appointment`.
    """

    confirmation_id: str
    provider_id: str
    patient_id: str
    slot_start: str
    """Canonical text form of the slot, never a ``datetime``, so the record is byte-stable."""

    reason: str
    """Why the booking was made, as CareLoop sent it.

    **Required, with no default and no factory**, like every other result field — and here that is
    load-bearing beyond invariant 9's usual argument. A default would let
    :func:`careloop.tools.clinic._appointment` synthesise a value for a clinic response that
    omitted the field, and verification would then compare what CareLoop executed against a string
    CareLoop itself invented. Where the executed reason is empty — which
    :data:`careloop.agents.schemas.NOT_SUPPLIED` makes reachable — a clinic returning no reason at
    all would verify clean. Absence is a malformed observation, not a value to fill in.

    A consequence worth stating plainly: a booking result recorded before this field existed cannot
    be rehydrated. That is the honest outcome rather than a gap — such a record was never checked
    against the clinic's reason and cannot truthfully satisfy an invariant that says it was. Reads
    and API projections are unaffected, because
    :attr:`careloop.state.case.ActionRecord.result` is opaque text to everything except
    :func:`careloop.graph.verify.rehydrate`.

    Note what this does *not* claim. ``reason`` is planner-authored free text; it declares no
    ``trust_key``, no rule requires its trust, and nothing here says it is trustworthy or
    appropriate. What is checked is fidelity between CareLoop and the world, not provenance.
    """


class DocumentUploaded(ActionResult):
    """A document was filed into a provider's object store.

    Every field is something an independent read of the store can establish, which is what
    distinguishes this from the receipt it used to be. ``upload_id`` — a synthetic identifier
    CareLoop minted from the idempotency key — is gone: the provider never held it, so comparing it
    against an observation compared CareLoop against itself.

    **A record written before those fields existed cannot be rehydrated**, exactly as
    :attr:`AppointmentBooked.reason` documents for a booking, and for the same reason: such a record
    was never checked against a store's own bytes and cannot truthfully satisfy an invariant that
    says it was. There is no default and no migration. Reads and API projections are unaffected,
    because :attr:`careloop.state.case.ActionRecord.result` is opaque text to everything except
    :func:`careloop.graph.verify.rehydrate`.
    """

    object_key: str
    """Where the store holds it. **Chosen by CareLoop from the durable action identity**, which is
    what lets a reconciliation address the object a crashed attempt would have made.

    Carried so the independent read-back has something to be keyed on — the analogue of a clinic's
    ``confirmation_id``, except that the provider mints nothing and CareLoop names the resource.
    """

    object_id: str
    """The key's last segment: the frozen idempotency key, and the bounded resource identifier.

    Separate from :attr:`object_key` because the two are read by different consumers with different
    constraints. The read-back needs the whole key; settlement telemetry needs one bounded token
    matching ``careloop.graph.witness.RESOURCE_ID_PATTERN``, which a key carrying separators does
    not. Deriving the short form here rather than letting a projection parse the long one keeps the
    parsing in one place.
    """

    portal_id: str
    """Which portal the document was filed for, **as the store reports it back**."""

    document_id: str
    """Which held document was filed, as the store reports it back."""

    content_sha256: str
    """Lowercase hexadecimal SHA-256 of the object's bytes.

    On a read-back it is computed from **the bytes the store returned to a fresh read**, and never
    from a digest the store reports about itself. That is the difference between observing an object
    and believing a receipt, and it is why this field exists rather than a provider-reported
    checksum.

    Compared against a :class:`careloop.graph.verify.DerivedExpectation` — a digest computed from
    the document CareLoop holds — rather than against a tool argument. There is no
    ``content_sha256`` argument and there must not be: it would enter the proposal, the authority
    path, ``args_digest`` and the outbound payload in order to answer a question about scrutiny.
    """


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

    kind: DocumentKind
    """What the document is. Required, and never inferred from its text — see
    :class:`DocumentKind`."""

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
    "DocumentKind",
    "DocumentUploaded",
    "EmailSent",
    "ToolResult",
]
