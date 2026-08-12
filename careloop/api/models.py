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


#: Every response shape, for the import-time no-defaults check and for the sweeps in
#: ``tests/test_api_models.py``. Listed explicitly rather than discovered by walking
#: ``ResponseModel.__subclasses__``, so a model defined and then forgotten is a visible omission.
RESPONSE_MODELS: tuple[type[ResponseModel], ...] = (
    FactView,
    DecisionView,
    ApprovalView,
    ActionView,
    TimelineEventView,
    CaseCard,
    CaseSections,
    CaseDetail,
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
    "CaseCard",
    "CaseDetail",
    "CaseSections",
    "DecisionView",
    "FactView",
    "ResponseFieldHasDefault",
    "ResponseModel",
    "TimelineEventView",
    "check_no_response_defaults",
]
