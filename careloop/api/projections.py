"""Turning stored shapes into response shapes, and deciding what is withheld.

**Invariant 6 over HTTP, and why the line moves.**

Invariant 6 exists because denial text is echoed back into the planner's context: an interpolated
reason is a self-inflicted injection channel, and naming the matched rule hands the component being
tempted a map of the defence. A browser is a different consumer. It proposes nothing, and nothing it
renders re-enters a prompt. So the line here is drawn on a different axis — not *what would help an
attacker plan*, but **what is unbounded free text that nobody constrains**.

That is why things invariant 6 withholds from the model are exposed here on purpose. Fact trust
levels and ``referral.pdf p.2`` citations are the thing the dashboard exists to show; the matched
rule id on a gate verdict is the declarative-policy claim made visible. What is withheld is
:attr:`~careloop.state.case.TimelineEvent.diagnostic`, wholesale, and the reason is the field's own
documented contract: *"may hold anything useful to a human reading the audit trail, including raw
argument values and rule internals."* Roughly a dozen call sites write it, nothing constrains what
the next one puts there, and it is read by whoever opens the server-side audit trail rather than by
a browser.

It is withheld rather than filtered. A filter over free text is the sanitiser this project refuses
everywhere else — ``fetch_document`` does not clean the poisoned fixture and the prompt framing
quotes untrusted content rather than escaping it, both because a filter that looks like a defence is
worse than none. ``payload`` is projected through an **allowlist** for the same reason in the other
direction: a payload key nobody has decided about is invisible to the API until somebody does.

**The two properties, both enforced at import rather than only in tests.**

Nothing vanishes silently — every field of every stored model is either projected into some
response model or listed in :data:`WITHHELD` with a reason. Adding a field to ``Case`` without
deciding fails on import.

Nothing is invented — every field of every response model either names the stored field it comes
from or is listed in :data:`DERIVED` with what it is derived from. Together with the no-default rule
in :mod:`careloop.api.models`, a response can only say what the store held.

**The timeline is read here and nowhere else in this package.** Anything needing something out of
it calls a named reader in :mod:`careloop.graph.context` — ``proposal_for``, ``latest_delivery`` —
so the withholding decision stays in one place instead of becoming a convention that the next route
handler quietly works around. ``tests/test_api_projections.py`` sweeps for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel

from careloop.api.models import (
    RESPONSE_MODELS,
    ActionView,
    ApprovalView,
    CaseCard,
    CaseDetail,
    CaseSections,
    DecisionView,
    FactView,
    ResponseModel,
    RunView,
    TimelineEventView,
)
from careloop.api.runs import RunRecord
from careloop.graph.context import proposal_for
from careloop.policy.verdicts import ReasonCode, reason_text
from careloop.provenance.facts import Fact, Provenance
from careloop.state.case import ActionRecord, Approval, Case, CaseState, TimelineEvent
from careloop.state.machine import AT_REST_STATES

# -- the three sections ---------------------------------------------------------------------------

NEEDS_YOU: Final[str] = "needs_you"
WORKING: Final[str] = "working"
COMPLETED: Final[str] = "completed"

#: The dashboard's three sections, as sets of case states.
#:
#: ``completed`` is :data:`~careloop.state.machine.AT_REST_STATES` rather than a fresh list, because
#: that name already means exactly "the graph will move this case no further" and a second list
#: would be a second answer. ``FAILED`` lands there with ``DONE`` and is told apart by the ``state``
#: field every card carries — a failed case is finished, not in progress, and the caregiver needs it
#: on screen rather than filed under a fourth heading nobody looks at.
#:
#: ``working`` is defined as *the rest* so the partition cannot develop a hole: a new
#: :class:`~careloop.state.case.CaseState` joins it automatically and
#: :func:`check_sections_partition_every_state` still passes, which is the safe direction — a case
#: visible in the wrong column beats a case that has silently vanished from the dashboard.
SECTIONS: Final[Mapping[str, frozenset[CaseState]]] = MappingProxyType(
    {
        NEEDS_YOU: frozenset({CaseState.AWAITING_HUMAN}),
        COMPLETED: frozenset(AT_REST_STATES),
        WORKING: frozenset(CaseState) - frozenset({CaseState.AWAITING_HUMAN}) - AT_REST_STATES,
    }
)

#: The order the dashboard reads top to bottom.
SECTION_ORDER: Final[tuple[str, ...]] = (NEEDS_YOU, WORKING, COMPLETED)


class SectionsAreNotAPartition(Exception):
    """Raised at import when a case state would land in no section or in two."""


def check_sections_partition_every_state(
    sections: Mapping[str, frozenset[CaseState]] | None = None,
) -> None:
    """Fail at import unless every case state lands in exactly one section.

    A state in no section is a case that has vanished from the dashboard: the store still holds it,
    every endpoint still reports it individually, and the one screen a caregiver actually looks at
    does not list it. That is the failure worth making impossible, and it is exactly what adding an
    eleventh ``CaseState`` would cause if the partition were three hand-written lists.

    Raises:
        SectionsAreNotAPartition: naming the states that are missing or duplicated.
    """
    table = sections if sections is not None else SECTIONS
    every = frozenset(CaseState)
    covered = frozenset().union(*table.values()) if table else frozenset()

    missing = sorted(every - covered)
    unknown = sorted(covered - every)
    duplicated = sorted(
        state for state in covered if sum(1 for members in table.values() if state in members) > 1
    )

    problems = []
    if missing:
        problems.append(f"in no section: {', '.join(missing)}")
    if duplicated:
        problems.append(f"in more than one section: {', '.join(duplicated)}")
    if unknown:
        problems.append(f"not a CaseState: {', '.join(unknown)}")
    if problems:
        raise SectionsAreNotAPartition("; ".join(problems))


check_sections_partition_every_state()


def section_for(state: CaseState) -> str:
    """Which of the three dashboard sections a case in ``state`` belongs to."""
    for name in SECTION_ORDER:
        if state in SECTIONS[name]:
            return name
    raise SectionsAreNotAPartition(f"{state} belongs to no section")  # pragma: no cover


# -- the payload allowlist ------------------------------------------------------------------------

#: Timeline payload keys the API will serialize.
#:
#: An allowlist, not a denylist. Every key here was looked at and decided about; a key a future
#: writer adds is dropped until somebody adds it, and ``tests/test_api_projections.py`` fails when a
#: shipped writer emits one that is not listed, so "dropped" cannot go unnoticed.
#:
#: ``matched_rule_id`` is here deliberately. Invariant 6 keeps it from the planner because it maps
#: the defence for the component being tempted; a judge reading the under-the-hood panel is who the
#: named declarative rule was written for.
TIMELINE_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # who and what
        "tool_name",
        "approval_id",
        "event_id",
        "document_ids",
        "opened_by",
        "key",
        "source_doc",
        "page",
        # the planner's proposal, which the decision card and the audit trail both show
        "args",
        "rationale",
        # policy verdicts — the under-the-hood panel
        "verdict",
        "reason_code",
        "matched_rule_id",
        # state machine
        "from",
        "to",
        "reason",
        "status",
        "decided_by",
        # evidence and validation
        "category",
        "urgency",
        "patient_id",
        "kind",
        "stage",
        "check",
        "trust_level",
        "held_trust_level",
        "from_trust_level",
        "to_trust_level",
        "arg_class",
        "validated_by",
        # effects
        "idempotency_key",
        "outcome",
    }
)


# -- what is withheld, and what is derived --------------------------------------------------------

#: Stored models the projection reads from, by name.
SOURCE_MODELS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "Case": Case,
        "Fact": Fact,
        "Provenance": Provenance,
        "Approval": Approval,
        "ActionRecord": ActionRecord,
        "TimelineEvent": TimelineEvent,
    }
)

#: Stored fields the API deliberately does not serialize, and why.
WITHHELD: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "TimelineEvent": MappingProxyType(
            {
                "diagnostic": (
                    "Unbounded free text. Its own docstring says it may hold anything useful to a "
                    "human reading the audit trail, including raw argument values and rule "
                    "internals; roughly a dozen call sites write it and nothing constrains the "
                    "next one. Withheld wholesale rather than filtered, because a filter over free "
                    "text is the sanitiser this project refuses everywhere else."
                )
            }
        ),
        "Fact": MappingProxyType(
            {
                "extracted_at": (
                    "Misleading under its own name: promote() resets it, so it means 'when this "
                    "fact last changed trust level', not 'when it was extracted'. The timeline "
                    "already records both the recording and the promotion, in order, with the "
                    "citation on each."
                )
            }
        ),
    }
)

#: For every response field, the stored field it comes from — ``"Model.field"``, dotted to reach a
#: nested model. Everything not listed here must appear in :data:`DERIVED`.
SOURCES: Final[Mapping[type[ResponseModel], Mapping[str, str]]] = MappingProxyType(
    {
        FactView: MappingProxyType(
            {
                "key": "Fact.key",
                "value": "Fact.value",
                "trust_level": "Fact.trust_level",
                "validated_by": "Fact.validated_by",
                "source_doc": "Fact.provenance.source_doc",
                "page": "Fact.provenance.page",
                "extractor": "Fact.provenance.extractor",
            }
        ),
        ApprovalView: MappingProxyType(
            {
                "approval_id": "Approval.approval_id",
                "tool_name": "Approval.tool_name",
                "args_digest": "Approval.args_digest",
                "reason_code": "Approval.reason_code",
                "status": "Approval.status",
                "requested_at": "Approval.requested_at",
                "decided_at": "Approval.decided_at",
                "decided_by": "Approval.decided_by",
            }
        ),
        DecisionView: MappingProxyType(
            {
                "approval_id": "Approval.approval_id",
                "tool_name": "Approval.tool_name",
                "reason_code": "Approval.reason_code",
                "requested_at": "Approval.requested_at",
            }
        ),
        ActionView: MappingProxyType(
            {
                "idempotency_key": "ActionRecord.idempotency_key",
                "action_type": "ActionRecord.action_type",
                "args_digest": "ActionRecord.args_digest",
                "result": "ActionRecord.result",
                "recorded_at": "ActionRecord.recorded_at",
            }
        ),
        TimelineEventView: MappingProxyType(
            {
                "seq": "TimelineEvent.seq",
                "at": "TimelineEvent.at",
                "kind": "TimelineEvent.kind",
                "actor": "TimelineEvent.actor",
                "summary": "TimelineEvent.summary",
                "payload": "TimelineEvent.payload",
            }
        ),
        CaseCard: MappingProxyType(
            {
                "case_id": "Case.case_id",
                "state": "Case.state",
                "summary": "Case.summary",
                "version": "Case.version",
                "created_at": "Case.created_at",
                "updated_at": "Case.updated_at",
                "failure_reason": "Case.failure_reason",
                "fact_count": "Case.facts",
                "action_count": "Case.actions",
            }
        ),
        CaseDetail: MappingProxyType(
            {
                "case_id": "Case.case_id",
                "state": "Case.state",
                "version": "Case.version",
                "event_id": "Case.event_id",
                "summary": "Case.summary",
                "created_at": "Case.created_at",
                "updated_at": "Case.updated_at",
                "failure_reason": "Case.failure_reason",
                "facts": "Case.facts",
                "approvals": "Case.approvals",
                "actions": "Case.actions",
                "timeline": "Case.timeline",
            }
        ),
        CaseSections: MappingProxyType({}),
        RunView: MappingProxyType({}),
    }
)

#: Response fields computed from stored data rather than copied out of one field, and how.
#:
#: Every one is a pure function of what the store holds. Nothing here reaches a clock, a default or
#: a value the case does not carry — that is what stops "derived" becoming the hole in the
#: nothing-is-invented property.
DERIVED: Final[Mapping[type[ResponseModel], Mapping[str, str]]] = MappingProxyType(
    {
        FactView: MappingProxyType({"citation": "Fact.provenance.citation, a property"}),
        ApprovalView: MappingProxyType({"reason_text": "reason_text(Approval.reason_code)"}),
        DecisionView: MappingProxyType(
            {
                "reason_text": "reason_text(Approval.reason_code)",
                "args": (
                    "the PLAN_PROPOSED entry whose arguments hash to Approval.args_digest, via "
                    "graph.context.proposal_for"
                ),
            }
        ),
        ActionView: MappingProxyType({}),
        TimelineEventView: MappingProxyType({}),
        CaseCard: MappingProxyType(
            {
                "section": "section_for(Case.state)",
                "decision": "Case.pending_approval() and the proposal its digest covers",
            }
        ),
        CaseDetail: MappingProxyType(
            {
                "section": "section_for(Case.state)",
                "decision": "Case.pending_approval() and the proposal its digest covers",
            }
        ),
        CaseSections: MappingProxyType(
            {
                "needs_you": "cases whose state is in SECTIONS['needs_you']",
                "working": "cases whose state is in SECTIONS['working']",
                "completed": "cases whose state is in SECTIONS['completed']",
            }
        ),
        # Not a projection of the store at all. A run is execution context — see careloop.api.runs
        # for why that is consistent with invariant 8 rather than a hole in it — so every field is
        # declared derived, and the table says so out loud rather than by omission.
        RunView: MappingProxyType(
            {
                "run_id": "RunRecord.run_id, from the in-process run registry",
                "case_id": "RunRecord.case_id, from the in-process run registry",
                "state": "RunRecord.state, from the in-process run registry",
                "executed_nodes": "RunRecord.executed_nodes, from the in-process run registry",
                "error": "RunRecord.error, the raised type's name and never its message",
            }
        ),
    }
)


class UndecidedField(Exception):
    """Raised at import when a stored or response field has had no decision made about it."""


def _path_fields(path: str, models: Mapping[str, type[BaseModel]]) -> list[tuple[str, str]]:
    """Every ``(model, field)`` a dotted source path reads on its way down.

    ``"Fact.provenance.source_doc"`` reads ``Fact.provenance`` *and* ``Provenance.source_doc``, and
    both count as decided. Walking the whole chain rather than the first segment is what lets a
    nested model be flattened into a response without its own fields going undecided.

    Raises:
        UndecidedField: if any segment is not a field of the model it is read from.
    """
    model_name, *segments = path.split(".")
    model = models.get(model_name)
    if model is None:
        raise UndecidedField(f"{path!r} names no stored model")

    # Named by the table key rather than by ``model.__name__``, so a test may stand a subclass in
    # for a stored model without every path in SOURCES ceasing to resolve.
    current = model_name
    reached: list[tuple[str, str]] = []
    for position, segment in enumerate(segments):
        fields = getattr(model, "model_fields", {})
        if segment not in fields:
            raise UndecidedField(f"{path!r}: {current} has no field {segment!r}")
        reached.append((current, segment))
        if position < len(segments) - 1:
            model = fields[segment].annotation
            current = getattr(model, "__name__", str(model))
    return reached


def check_every_stored_field_is_decided(
    sources: Mapping[type[ResponseModel], Mapping[str, str]] | None = None,
    withheld: Mapping[str, Mapping[str, str]] | None = None,
    models: Mapping[str, type[BaseModel]] | None = None,
) -> None:
    """Fail at import unless every stored field is projected or explicitly withheld.

    This is the half that stops a field quietly disappearing. Adding ``Case.assigned_to`` and
    forgetting the API means the dashboard silently never shows it; the fix is not to remember, it
    is for the import to fail until somebody writes down which it is.

    The tables are parameters so a test can drive the check over a deliberately broken one. A guard
    that can only ever be run against the shipped tables is a guard whose failure path is untested.

    Raises:
        UndecidedField: naming every undecided field, and every field that is both.
    """
    source_table = SOURCES if sources is None else sources
    withheld_table = WITHHELD if withheld is None else withheld
    model_table = SOURCE_MODELS if models is None else models

    problems: list[str] = []
    for name, model in model_table.items():
        projected = {
            field
            for table in source_table.values()
            for path in table.values()
            for reached, field in _path_fields(path, model_table)
            if reached == name
        }
        held_back = set(withheld_table.get(name, {}))
        stored = set(model.model_fields)

        undecided = sorted(stored - projected - held_back)
        both = sorted(projected & held_back)
        phantom = sorted((projected | held_back) - stored)

        if undecided:
            problems.append(
                f"{name}: {', '.join(undecided)} neither projected nor withheld — decide, then "
                f"add to SOURCES or to WITHHELD with a reason"
            )
        if both:
            problems.append(f"{name}: {', '.join(both)} both projected and withheld")
        if phantom:
            problems.append(f"{name}: {', '.join(phantom)} is not a field of {name}")

    if problems:
        raise UndecidedField("; ".join(problems))


def check_every_response_field_has_a_source(
    sources: Mapping[type[ResponseModel], Mapping[str, str]] | None = None,
    derived: Mapping[type[ResponseModel], Mapping[str, str]] | None = None,
    models: Mapping[str, type[BaseModel]] | None = None,
) -> None:
    """Fail at import unless every response field names where it comes from.

    The half that stops the API inventing something. A response field that appears in neither
    :data:`SOURCES` nor :data:`DERIVED` is a value with no stated origin, which is precisely the
    "second source of truth" a projection is supposed not to be.

    Raises:
        UndecidedField: naming every response field with no declared origin.
    """
    source_table = SOURCES if sources is None else sources
    derived_table = DERIVED if derived is None else derived
    model_table = SOURCE_MODELS if models is None else models

    problems: list[str] = []
    if sources is None and (unlisted := sorted(set(RESPONSE_MODELS) - set(source_table))):
        # Otherwise a response model could escape the check entirely by having no entry, which is
        # the same hole in the other direction: no declared origin at all rather than one missing.
        problems.append(
            f"{', '.join(model.__name__ for model in unlisted)} declare no SOURCES entry, so "
            f"nothing checks where their fields come from — add one, empty if every field is "
            f"derived"
        )

    for view, view_sources in source_table.items():
        view_derived = set(derived_table.get(view, {}))
        declared = set(view_sources) | view_derived
        fields = set(view.model_fields)

        if orphan := sorted(fields - declared):
            problems.append(
                f"{view.__name__}: {', '.join(orphan)} names no stored field and is not declared "
                f"derived — a response may not introduce a value the store never held"
            )
        if phantom := sorted(declared - fields):
            problems.append(f"{view.__name__}: {', '.join(phantom)} is not a field of it")
        if overlap := sorted(set(view_sources) & view_derived):
            problems.append(f"{view.__name__}: {', '.join(overlap)} is both sourced and derived")

        for path in view_sources.values():
            _path_fields(path, model_table)

    if problems:
        raise UndecidedField("; ".join(problems))


check_every_stored_field_is_decided()
check_every_response_field_has_a_source()


# -- the projections ------------------------------------------------------------------------------


def fact_view(fact: Fact) -> FactView:
    """One fact, with the trust level and the citation the dashboard renders."""
    return FactView(
        key=fact.key,
        value=fact.value,
        trust_level=str(fact.trust_level),
        citation=fact.citation,
        source_doc=fact.provenance.source_doc,
        page=fact.provenance.page,
        extractor=fact.provenance.extractor,
        validated_by=fact.validated_by,
    )


def _reason_text_for(code: str) -> str:
    """The caregiver-facing text for a stored reason code.

    Stored as a plain string on ``Approval``, so a code the enum no longer carries renders as
    itself rather than raising on a read. An unreadable card is worse than an unfamiliar one.
    """
    try:
        return reason_text(ReasonCode(code))
    except ValueError:
        return code


def approval_view(approval: Approval) -> ApprovalView:
    """One decision request and its outcome."""
    return ApprovalView(
        approval_id=approval.approval_id,
        tool_name=approval.tool_name,
        args_digest=approval.args_digest,
        reason_code=approval.reason_code,
        reason_text=_reason_text_for(approval.reason_code),
        status=str(approval.status),
        requested_at=approval.requested_at,
        decided_at=approval.decided_at,
        decided_by=approval.decided_by,
    )


def action_view(action: ActionRecord) -> ActionView:
    """One performed external effect, with the key it happened exactly once under."""
    return ActionView(
        idempotency_key=action.idempotency_key,
        action_type=action.action_type,
        args_digest=action.args_digest,
        result=action.result,
        recorded_at=action.recorded_at,
    )


def timeline_event_view(event: TimelineEvent) -> TimelineEventView:
    """One audit entry, without its diagnostic and with its payload allowlisted."""
    return TimelineEventView(
        seq=event.seq,
        at=event.at,
        kind=str(event.kind),
        actor=event.actor,
        summary=event.summary,
        payload={
            key: value for key, value in event.payload.items() if key in TIMELINE_PAYLOAD_FIELDS
        },
    )


def decision_view(case: Case) -> DecisionView | None:
    """The outstanding decision, as a "Needs You" card, or ``None`` if none is pending.

    The arguments come back through :func:`careloop.graph.context.proposal_for`, keyed on the digest
    the approval records. An approval deliberately stores only that digest — a decision made about
    one action must not authorise another — so showing the caregiver what they are approving means
    going back to the proposal the digest came from.

    An approval whose proposal is not on the timeline renders with no arguments. Unreachable in a
    real run, since the gate writes the approval from the proposal it has just read, and left
    visible rather than hidden because a card with nothing on it is a caregiver who can see there is
    nothing to approve.
    """
    approval = case.pending_approval()
    if approval is None:
        return None

    proposal = proposal_for(case, approval.args_digest)
    return DecisionView(
        approval_id=approval.approval_id,
        tool_name=approval.tool_name,
        reason_code=approval.reason_code,
        reason_text=_reason_text_for(approval.reason_code),
        args=dict(proposal.args) if proposal is not None else {},
        requested_at=approval.requested_at,
    )


def case_card(case: Case) -> CaseCard:
    """One case as a dashboard row, carrying its decision if it has one."""
    return CaseCard(
        case_id=case.case_id,
        state=str(case.state),
        section=section_for(case.state),
        summary=case.summary,
        version=case.version,
        created_at=case.created_at,
        updated_at=case.updated_at,
        failure_reason=case.failure_reason,
        fact_count=len(case.facts),
        action_count=len(case.actions),
        decision=decision_view(case),
    )


def case_detail(case: Case) -> CaseDetail:
    """One case in full."""
    return CaseDetail(
        case_id=case.case_id,
        state=str(case.state),
        section=section_for(case.state),
        version=case.version,
        event_id=case.event_id,
        summary=case.summary,
        created_at=case.created_at,
        updated_at=case.updated_at,
        failure_reason=case.failure_reason,
        decision=decision_view(case),
        facts=[fact_view(fact) for fact in case.facts],
        approvals=[approval_view(approval) for approval in case.approvals],
        actions=[action_view(action) for action in case.actions],
        timeline=[timeline_event_view(event) for event in case.timeline],
    )


def case_sections(cases: dict[str, list[Case]]) -> CaseSections:
    """The dashboard, from cases already grouped by section name."""
    return CaseSections(
        needs_you=[case_card(case) for case in cases.get(NEEDS_YOU, [])],
        working=[case_card(case) for case in cases.get(WORKING, [])],
        completed=[case_card(case) for case in cases.get(COMPLETED, [])],
    )


def run_view(record: RunRecord) -> RunView:
    """One scheduled run, as the under-the-hood panel reads it."""
    return RunView(
        run_id=record.run_id,
        case_id=record.case_id,
        state=str(record.state),
        executed_nodes=list(record.executed_nodes),
        error=record.error,
    )


def payload_keys_in(case: Case) -> set[str]:
    """Every timeline payload key ``case`` carries.

    Used by the drift check that keeps :data:`TIMELINE_PAYLOAD_FIELDS` honest: a key a shipped
    writer emits but the allowlist does not name is a field silently missing from the API, and the
    point of an allowlist is that dropping something is a decision rather than an accident.
    """
    return {key for event in case.timeline for key in event.payload}


__all__: list[str] = [
    "COMPLETED",
    "DERIVED",
    "NEEDS_YOU",
    "SECTIONS",
    "SECTION_ORDER",
    "SOURCES",
    "SOURCE_MODELS",
    "TIMELINE_PAYLOAD_FIELDS",
    "WITHHELD",
    "WORKING",
    "SectionsAreNotAPartition",
    "UndecidedField",
    "action_view",
    "approval_view",
    "case_card",
    "case_detail",
    "case_sections",
    "check_every_response_field_has_a_source",
    "check_every_stored_field_is_decided",
    "check_sections_partition_every_state",
    "decision_view",
    "fact_view",
    "payload_keys_in",
    "run_view",
    "section_for",
    "timeline_event_view",
]
