"""What the API is allowed to say.

Every model here is a **projection** of something the ``CaseStore`` already holds. None of them
carries a field default and none carries a ``default_factory``, which is the same rule invariant 9
puts on tool results and it is here for the same reason: a response field must only be able to say
what the projector passed it. A default would let a response report a value the store never held,
and "the API is a read surface" would stop being checkable.

``check_no_response_defaults`` enforces it at import time rather than in a test, so a model that
grows a default fails on import instead of at the end of a suite run.

Nothing in this module reads a ``Case``. The mapping from stored shapes to these ones lives in
:mod:`careloop.api.projections`, together with the decision about what is withheld.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic_core import PydanticUndefined


class ResponseModel(BaseModel):
    """Base for every API response shape.

    Frozen, because a response that a route handler can edit after projection is a second place for
    the store's answer to change. ``extra="forbid"`` because a projector that passes an unexpected
    keyword should fail loudly rather than have it silently dropped or silently attached.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class FactView(ResponseModel):
    """One piece of evidence, with the provenance invariant 3 requires.

    ``trust_level`` and ``citation`` are exposed deliberately. Invariant 6 keeps trust levels away
    from the *model*; a browser is a different consumer, and the trust badge next to
    ``referral.pdf p.2`` is the thing the dashboard exists to show.
    """

    key: str
    value: str
    trust_level: str
    citation: str
    source_doc: str
    page: int | None
    extractor: str
    validated_by: str | None


class DecisionView(ResponseModel):
    """A pending caregiver decision, as a "Needs You" card renders it.

    ``args`` is the whole point: a caregiver cannot approve what they cannot see. The arguments
    come from the proposal the approval's digest covers, so the card names exactly the call that
    will be made if the button is pressed.
    """

    approval_id: str
    tool_name: str
    reason_code: str
    reason_text: str
    args: dict[str, str]
    requested_at: datetime

    recheckable: bool
    """Whether the **stored case** is structurally eligible for a caregiver to ask for a re-check.

    Computed by :func:`careloop.graph.context.recheck_target`, the same resolver
    :func:`careloop.graph.runner.request_recheck` reads — so the button this drives and the endpoint
    that validates the click cannot disagree about whether a handoff is bound to a valid target.

    **What it claims** is narrow and exact: the case is stopped on an unconfirmed effect, that
    handoff durably names an existing :class:`~careloop.state.case.ActionRecord`, the action's type
    has a real independent observer, and the current proposal re-derives the same identity.

    **What it does not claim, and cannot.** It does not promise that no run will start between this
    response being rendered and the button being pressed; that a
    :class:`~careloop.graph.context.RunContext` with an observer bound will actually be built for
    the run that follows; that the external system will answer; or that the verification will pass.
    A projector reads durable state, and none of those four are durable state — the first is
    execution context and the rest are facts about a run that has not happened. The endpoint and the
    verify node remain authoritative, and both re-check everything.

    So this is an **affordance hint**, deliberately: it exists to stop a client reverse-engineering
    eligibility from ``reason_code``, which would offer a button on a pre-2A handoff, on a handoff
    naming no record, and on an action type nothing can observe.
    """


class ApprovalView(ResponseModel):
    """One decision request and its outcome, pending or not."""

    approval_id: str
    tool_name: str
    args_digest: str
    reason_code: str
    reason_text: str
    status: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by: str | None


class ActionView(ResponseModel):
    """One performed external effect.

    ``idempotency_key`` is exposed because "this happened exactly once, under this key" is a claim
    a judge should be able to read off the screen rather than take on trust.
    """

    idempotency_key: str
    action_type: str
    args_digest: str
    result: str
    recorded_at: datetime


class TimelineEventView(ResponseModel):
    """One audit entry, without its diagnostic.

    See :mod:`careloop.api.projections` for why ``diagnostic`` is withheld wholesale rather than
    filtered, and why ``payload`` is projected through an allowlist.
    """

    seq: int
    at: datetime
    kind: str
    actor: str
    summary: str
    payload: dict[str, Any]


class CaseCard(ResponseModel):
    """A case as one row of the dashboard's three sections."""

    case_id: str
    state: str
    section: str
    summary: str
    version: int
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None
    fact_count: int
    action_count: int
    decision: DecisionView | None


class CaseSections(ResponseModel):
    """The whole dashboard: every case, partitioned into the three sections.

    The partition is total and disjoint over :class:`~careloop.state.case.CaseState` — see
    :data:`careloop.api.projections.SECTIONS`.
    """

    needs_you: list[CaseCard]
    working: list[CaseCard]
    completed: list[CaseCard]


class CaseDetail(ResponseModel):
    """One case in full: state, evidence, decisions, effects and the audit trail."""

    case_id: str
    state: str
    section: str
    version: int
    event_id: str
    summary: str
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None
    decision: DecisionView | None
    facts: list[FactView]
    approvals: list[ApprovalView]
    actions: list[ActionView]
    timeline: list[TimelineEventView]


class RunView(ResponseModel):
    """A scheduled graph run.

    Every field comes from the in-process run registry rather than from the store, which is why
    every one of them is declared derived in :data:`careloop.api.projections.DERIVED`. A run is
    execution context; nothing here answers a question the case cannot — see
    :mod:`careloop.api.runs`.
    """

    run_id: str
    case_id: str
    state: str
    executed_nodes: list[str]
    error: str | None


class BoundaryArgumentView(ResponseModel):
    """One argument of one capability, as the boundary statement describes it.

    Every field is a read or a derivation over ``rules.yaml`` and the tool registry — see
    :mod:`careloop.policy.boundaries`. Nothing here is a claim about a case.
    """

    name: str
    arg_class: str

    required_trust: str | None
    """The trust every authorising path demands, or ``None`` when the rules demand none.

    ``None`` is an answer rather than a gap: ``send_email.subject`` declares no ``trust_key`` and no
    rule names it, and a page that printed a requirement there would be asserting a guard that does
    not exist.
    """

    trust_key: str | None
    """Which fact this argument draws its trust from.

    Safe to expose because it is the same key :class:`FactView` already carries on every piece of
    evidence — it is what lets a caregiver connect *"the provider must be trusted"* to the trusted
    fact on their own case, rather than a new identifier invented for this surface.
    """

    configured_value: str | None
    """The caller's own configured destination, when this argument draws on one.

    Their own address or their own portal, from CareLoop's directory. Never another caregiver's, and
    never a value a document supplied.
    """

    authored_value: str | None
    """The fixed literal CareLoop composes for this argument, when it composes one."""

    identifies: bool
    """Whether this argument is part of which logical action a call is."""


class CapabilityView(ResponseModel):
    """One external-effect tool, as the caller's current configuration permits it."""

    tool_name: str
    standing: str
    unavailable_because: str | None

    unmet_trust_keys: list[str]
    """Fact keys an argument needs at ``TRUSTED`` that this caller does not configure.

    The whole of why a capability is out of reach, and the reason the copy can be specific without
    being a rule browser. Empty for anything that is not unavailable for that reason.
    """

    independently_observed: bool
    recovery_class: str
    arguments: list[BoundaryArgumentView]


class BoundariesView(ResponseModel):
    """What CareLoop may do for this caller, derived from what enforces it.

    Not a projection of a :class:`~careloop.state.case.Case` — see
    :data:`careloop.api.projections.DERIVED`, where every field says so. It answers *what are the
    standing limits*, which is a question about configuration rather than about any one case, and it
    is the surface that replaced a page of hand-written claims.
    """

    capabilities: list[CapabilityView]
    refusals: list[str]


class RequestModel(BaseModel):
    """Base for request bodies.

    ``extra="forbid"`` so a caller's typo is a 422 rather than a silently ignored field — a
    ``case_id`` misspelled into oblivion would open a case under whatever the default was.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class DeliverEventRequest(RequestModel):
    """ "An email just arrived": deliver an inbound event to a case.

    ``case_id`` is required rather than minted here. Generating one needs a clock or a random
    source, and a demo whose case ids change every run is a demo whose URLs cannot be written down.
    Which case an event belongs to is the caller's to say; whether that case may accept it is the
    graph layer's, and ``reopen_case`` already refuses one that is still in flight.
    """

    event_id: str
    case_id: str


#: Every response shape, for the import-time no-defaults check and for the sweeps in
#: ``tests/test_api_models.py``. Listed explicitly rather than discovered by walking
#: ``ResponseModel.__subclasses__``, so a model defined and then forgotten is a visible omission.
RESPONSE_MODELS: tuple[type[ResponseModel], ...] = (
    FactView,
    BoundaryArgumentView,
    BoundariesView,
    CapabilityView,
    DecisionView,
    ApprovalView,
    ActionView,
    TimelineEventView,
    CaseCard,
    CaseSections,
    CaseDetail,
    RunView,
)


class ResponseFieldHasDefault(Exception):
    """Raised at import when a response field could report a value the store never held."""


def check_no_response_defaults(models: tuple[type[ResponseModel], ...] | None = None) -> None:
    """Fail at import if any response field carries a default or a ``default_factory``.

    The same rule invariant 9 puts on tool result fields, for a related reason. There it stops a
    result inventing a timestamp; here it stops a response inventing anything at all. A field with
    no default can only hold what the projector gave it, which is what makes "response models
    project from ``Case``, they do not restate it" a property rather than an intention.

    Raises:
        ResponseFieldHasDefault: naming every offending field.
    """
    offenders = sorted(
        f"{model.__name__}.{name}"
        for model in (models if models is not None else RESPONSE_MODELS)
        for name, field in model.model_fields.items()
        if field.default is not PydanticUndefined or field.default_factory is not None
    )
    if offenders:
        raise ResponseFieldHasDefault(
            "response field(s) carry a default, so they could report a value the store never "
            "held: " + ", ".join(offenders)
        )


check_no_response_defaults()


__all__ = [
    "RESPONSE_MODELS",
    "ActionView",
    "ApprovalView",
    "BoundariesView",
    "BoundaryArgumentView",
    "CapabilityView",
    "CaseCard",
    "CaseDetail",
    "CaseSections",
    "DecisionView",
    "DeliverEventRequest",
    "FactView",
    "RequestModel",
    "ResponseFieldHasDefault",
    "ResponseModel",
    "RunView",
    "TimelineEventView",
    "check_no_response_defaults",
]
