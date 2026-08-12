"""What arrives, and what a case is opened from.

**The events are JSON fixtures, not Python literals.** ``careloop/events/fixtures/*.json``, loaded
at import and shipped as package data. That is deliberate rather than tidy: an inbound event is the
one thing in the system that genuinely comes from outside it, so it should arrive in a data format
rather than as a tuple somebody typed into the module that consumes it. It also means a judge can
add an event without editing Python, and that the demo can be driven from a file.

**An inbound event is untrusted from end to end.** Its subject and body are text somebody else
wrote, and its ``document_ids`` name documents whose contents somebody else wrote. Nothing here is
validated, cleaned, or trusted, and no field of it ever becomes a ``TRUSTED`` fact — that level
comes from :mod:`careloop.graph.trusted` and nowhere else.

``document_ids`` is an attachment list, not content. It selects which documents the evidence node
reads, and the evidence node reads them through the gated toolset, so an event naming a document
CareLoop does not hold fails loudly rather than being ignored. :func:`check_attachments_are_held`
moves that failure from run time to import time.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.tools.documents import DocumentStore, default_document_store

#: Package owning the fixture directory, and the directory's name within it.
FIXTURE_PACKAGE: Final[str] = "careloop.events"
FIXTURE_DIR: Final[str] = "fixtures"

REFERRAL_EVENT_ID: Final[str] = "evt-0001-referral"

#: The event T2 aims at: the same referral, with the insurer's continuity notice attached.
#:
#: The attack does not arrive as a strange message. It arrives as a second attachment on an
#: ordinary one, which is what makes the poisoned fixture worth having.
POISONED_EVENT_ID: Final[str] = "evt-0002-continuity-notice"

#: The follow-up that reopens a finished case, weeks later. T5 is built on it.
#:
#: A claim rejection asking for a document, arriving long after the appointment was arranged and
#: the case reached ``DONE``. Nothing about it is unusual, which is the point: an ordinary piece of
#: post is enough to wake a case up.
CLAIM_REJECTION_EVENT_ID: Final[str] = "evt-0003-claim-rejection"


class UnknownAttachment(Exception):
    """Raised at import when an event names a document CareLoop does not hold."""


class InboundEvent(BaseModel):
    """One thing that arrived in the caregiver's inbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    subject: str
    body: str
    document_ids: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """Subject and body, as triage reads them.

        Joined here rather than in the agent so there is one answer to "what did triage see", and
        so the framing in :func:`careloop.agents.prompts.triage_user_prompt` quotes the whole of
        it rather than a caller's choice of part.
        """
        return f"Subject: {self.subject}\n\n{self.body}"


def load_events() -> dict[str, InboundEvent]:
    """Every packaged event fixture, keyed by id.

    Read in filename order so two runs see the same events in the same sequence. ``extra="forbid"``
    on the model means a fixture carrying a field the model does not declare fails at import rather
    than being silently ignored — a typo in a key would otherwise produce an event missing its
    attachments.
    """
    directory = resources.files(FIXTURE_PACKAGE) / FIXTURE_DIR
    events: dict[str, InboundEvent] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".json"):
            continue
        loaded = InboundEvent.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        if loaded.event_id in events:
            raise ValueError(f"event {loaded.event_id!r} is declared twice")
        if entry.name != f"{loaded.event_id}.json":
            raise ValueError(
                f"{entry.name} declares event_id {loaded.event_id!r}; the filename is the id so a "
                f"fixture can be found without opening every file"
            )
        events[loaded.event_id] = loaded
    if not events:
        raise ValueError(f"no event fixtures found under {FIXTURE_PACKAGE}/{FIXTURE_DIR}")
    return events


EVENTS: Final[dict[str, InboundEvent]] = load_events()

REFERRAL_EVENT: Final[InboundEvent] = EVENTS[REFERRAL_EVENT_ID]
POISONED_EVENT: Final[InboundEvent] = EVENTS[POISONED_EVENT_ID]
CLAIM_REJECTION_EVENT: Final[InboundEvent] = EVENTS[CLAIM_REJECTION_EVENT_ID]


def check_attachments_are_held(
    events: dict[str, InboundEvent] | None = None,
    documents: DocumentStore | None = None,
) -> None:
    """Fail if any event names a document the store does not hold.

    Import-time rather than run-time. The alternative is a case that opens, triages, and then
    fails in the evidence node on a typo — a packaging error surfacing as a broken run.

    Raises:
        UnknownAttachment: naming every event and document involved.
    """
    held = documents if documents is not None else default_document_store()
    missing = sorted(
        f"{loaded.event_id} -> {document_id}"
        for loaded in (events if events is not None else EVENTS).values()
        for document_id in loaded.document_ids
        if document_id not in held
    )
    if missing:
        raise UnknownAttachment(
            "event(s) name documents CareLoop does not hold: " + "; ".join(missing)
        )


check_attachments_are_held()


def event(event_id: str) -> InboundEvent:
    """Return a fixture event by id.

    Raises:
        KeyError: if there is no such event. Raising rather than returning ``None`` keeps a
            missing event from being read as an empty one.
    """
    return EVENTS[event_id]


__all__ = [
    "CLAIM_REJECTION_EVENT",
    "CLAIM_REJECTION_EVENT_ID",
    "EVENTS",
    "FIXTURE_DIR",
    "FIXTURE_PACKAGE",
    "POISONED_EVENT",
    "POISONED_EVENT_ID",
    "REFERRAL_EVENT",
    "REFERRAL_EVENT_ID",
    "InboundEvent",
    "UnknownAttachment",
    "check_attachments_are_held",
    "event",
    "load_events",
]
