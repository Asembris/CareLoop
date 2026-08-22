"""Constructors the saga suites share.

Kept out of ``conftest.py`` for the reason ``helpers.py`` is: anything a test module *imports*
belongs in a module, and ``conftest.py`` holds fixtures pytest injects.

Everything here builds a case by hand, standing in for the nodes that would ordinarily have got it
into that shape. That is deliberate rather than lazy — a crash-recovery test has to start from a
case in a state no completed run ever leaves behind, and driving the graph into it would mean
crashing the graph to set up a test about crashing the graph.
"""

from __future__ import annotations

from helpers import FIXED_NOW

from careloop.events.inbound import InboundEvent
from careloop.graph.runner import open_case
from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.machine import append_event, transition
from careloop.state.store import CaseStore
from careloop.tools.idempotency import args_digest, idempotency_key

CASE_ID = "case-saga"

PROVIDER = "PROV-NORTHGATE-CARDIOLOGY"
SLOT = "2026-09-03T09:30:00"
PATIENT = "PT-4417"

#: The booking every saga test recovers. The slot is the one Demo World seeds first.
BOOKING_ARGS: dict[str, str] = {
    "provider_id": PROVIDER,
    "patient_id": PATIENT,
    "slot_start": SLOT,
    "reason": "cardiology review",
}

EMAIL_ARGS: dict[str, str] = {
    "to": "caregiver@example.test",
    "subject": "Referral update",
    "body": "the appointment is arranged",
}

SAGA_EVENT = InboundEvent(
    event_id="evt-saga",
    subject="Cardiology referral",
    body="Please arrange the outpatient appointment.",
)


def booking_key(case_id: str = CASE_ID, args: dict[str, str] | None = None) -> str:
    """The idempotency key a booking on ``case_id`` derives. The saga's logical action identity."""
    return idempotency_key(case_id, "book_appointment", args or BOOKING_ARGS)


def controlled_fact(key: str, value: str) -> Fact:
    """A fact validation promoted, as the evidence node leaves it.

    ``CONTROLLED`` rather than ``TRUSTED`` because that is what ``allow-booking-on-validated-slot``
    requires of ``patient_id`` and ``slot_start``, and because nothing in CareLoop produces a
    trusted patient. Built directly rather than promoted, for the reason every other hand-built
    fixture here is: the saga suites start from states no completed run leaves behind.
    """
    return Fact(
        key=key,
        value=value,
        trust_level=TrustLevel.CONTROLLED,
        provenance=Provenance(source_doc="doc-0001-referral", extractor="test"),
        extracted_at=FIXED_NOW,
    )


#: The validated evidence a booking needs to reach ``ALLOW`` autonomously.
#:
#: ``provider_id`` is not here: it comes from trusted configuration, which ``open_case`` already
#: seeds. Without these two the shipped rules refuse the booking at the tool boundary, and a saga
#: test built on a refused booking would be proving recovery over an effect that never happens.
VALIDATED_BOOKING_FACTS: tuple[Fact, ...] = (
    controlled_fact("patient_id", PATIENT),
    controlled_fact("slot_start", SLOT),
)


def ready_to_execute(
    store: CaseStore,
    *,
    tool_name: str = "book_appointment",
    args: dict[str, str] | None = None,
    case_id: str = CASE_ID,
    facts: tuple[Fact, ...] = VALIDATED_BOOKING_FACTS,
) -> Case:
    """A case sitting in ``READY_TO_EXECUTE`` with ``tool_name`` proposed and allowed.

    The state ``ExecuteNode`` is entered from. Built by hand, standing in for a gate that allowed
    the call — the arguments live on a ``PLAN_PROPOSED`` entry exactly as the plan node writes them,
    because that entry is what every recovery path reads the authorised arguments back out of.
    """
    payload_args = dict(args if args is not None else BOOKING_ARGS)
    case = open_case(store, SAGA_EVENT, case_id=case_id, now=FIXED_NOW)
    base = case.version
    if facts:
        case = case.model_copy(update={"facts": (*case.facts, *facts)})
    case = append_event(
        case,
        kind=TimelineKind.PLAN_PROPOSED,
        actor="planner",
        summary=f"proposed {tool_name}",
        payload={"tool_name": tool_name, "args": payload_args, "rationale": "the next step"},
        now=FIXED_NOW,
    )
    for state in (CaseState.GATHERING_EVIDENCE, CaseState.PLANNING, CaseState.READY_TO_EXECUTE):
        case = transition(case, state, reason="setup", actor="test", now=FIXED_NOW)
    return store.save(case, expected_version=base)


def executing_with_a_prepared_action(
    store: CaseStore,
    *,
    tool_name: str = "book_appointment",
    args: dict[str, str] | None = None,
    case_id: str = CASE_ID,
    key: str | None = None,
    facts: tuple[Fact, ...] = VALIDATED_BOOKING_FACTS,
) -> Case:
    """A case abandoned mid-effect: ``EXECUTING``, one ``ACTION_PREPARED``, no ``ActionRecord``.

    Exactly what a process that died between preparing a dispatch and recording its result leaves
    behind, and the state every crash point A-E converges on. The preparation and the transition go
    into one ``save``, which is the property ``ExecuteNode`` guarantees and this reproduces rather
    than assumes.
    """
    payload_args = dict(args if args is not None else BOOKING_ARGS)
    case = ready_to_execute(
        store, tool_name=tool_name, args=payload_args, case_id=case_id, facts=facts
    )
    base = case.version
    case = append_event(
        case,
        kind=TimelineKind.ACTION_PREPARED,
        actor="system",
        summary=f"{tool_name} prepared for dispatch",
        payload={
            "tool_name": tool_name,
            "idempotency_key": key or idempotency_key(case_id, tool_name, payload_args),
            "args_digest": args_digest(payload_args),
        },
        now=FIXED_NOW,
    )
    case = transition(case, CaseState.EXECUTING, reason="setup", actor="test", now=FIXED_NOW)
    return store.save(case, expected_version=base)


def kinds(case: Case) -> list[TimelineKind]:
    """Every timeline entry kind, in order."""
    return [event.kind for event in case.timeline]


def count(case: Case, kind: TimelineKind) -> int:
    """How many entries of one kind the case carries."""
    return sum(1 for event in case.timeline if event.kind is kind)


def prepared_payloads(case: Case) -> list[dict]:
    """The payload of every ``ACTION_PREPARED`` entry, in order."""
    return [
        dict(event.payload) for event in case.timeline if event.kind is TimelineKind.ACTION_PREPARED
    ]


class CountingStore:
    """A :class:`~careloop.state.store.CaseStore` that records how often it was written to.

    The only way to assert that two writes share one commit. ``save`` is the commit boundary in both
    shipped stores — :meth:`careloop.state.sqlite_store.SqliteCaseStore.save` wraps its case
    document update and its timeline inserts in one ``BEGIN IMMEDIATE`` ... ``COMMIT`` — so
    counting calls counts transactions, and a patch that split a preparation away from its
    transition shows up here as one extra.
    """

    def __init__(self, inner: CaseStore | None = None) -> None:
        self.inner: CaseStore = inner if inner is not None else InMemoryCaseStore()
        self.saves: list[Case] = []

    def get(self, case_id: str) -> Case:
        return self.inner.get(case_id)

    def list_by_state(self, state: CaseState) -> list[Case]:
        return self.inner.list_by_state(state)

    def timeline(self, case_id: str):
        return self.inner.timeline(case_id)

    def create(self, case: Case) -> Case:
        return self.inner.create(case)

    def save(self, case: Case, *, expected_version: int) -> Case:
        saved = self.inner.save(case, expected_version=expected_version)
        self.saves.append(saved)
        return saved

    def clear(self) -> int:
        return self.inner.clear()

    def close(self) -> None:
        self.inner.close()


class ExplodingPlanner:
    """A planner that fails the test if a recovery path so much as asks it for a proposal."""

    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, *args: object, **kwargs: object):
        self.calls += 1
        raise AssertionError("recovery asked the planner for a proposal")


class ExplodingTriage:
    """A triage agent that fails the test if a recovery path asks it to classify anything."""

    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, *args: object, **kwargs: object):
        self.calls += 1
        raise AssertionError("recovery asked triage to classify the event")


__all__ = [
    "BOOKING_ARGS",
    "CASE_ID",
    "EMAIL_ARGS",
    "PATIENT",
    "PROVIDER",
    "SAGA_EVENT",
    "SLOT",
    "VALIDATED_BOOKING_FACTS",
    "CountingStore",
    "ExplodingPlanner",
    "ExplodingTriage",
    "booking_key",
    "controlled_fact",
    "count",
    "executing_with_a_prepared_action",
    "kinds",
    "prepared_payloads",
    "ready_to_execute",
]
