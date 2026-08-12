"""The HTTP surface: a read/render projection over the ``CaseStore``, plus three verbs.

The API decides nothing. It projects stored cases into response shapes and calls functions that
already exist — :func:`careloop.graph.runner.open_case`, ``reopen_case``, ``approve``, ``reject``
and ``run_case``. No policy is evaluated here, no case is transitioned here, no fact is promoted
here. If an endpoint appears to need logic that does not exist, that is a signal the logic belongs
in the graph layer, not that the API should grow a second copy of it.

The one thing this layer originates is *when* a run happens, and scheduling is not deciding.

**This package is allowed to name ``reopen_case``**, unlike everything under ``agents/``, ``tools/``
and ``policy/`` and unlike the graph's own nodes — see ``tests/test_reopen.py``. The rule is that
the agent can never leave ``DONE`` and the world can; an inbound HTTP delivery *is* the world, and
this is the channel it arrives through.
"""

from __future__ import annotations

from careloop.api.app import API_PREFIX, create_app, load_case, load_event, schedule
from careloop.api.deps import (
    ApiDeps,
    ContextFactory,
    caregiver,
    default_context_factory,
    sections_from,
)
from careloop.api.models import (
    RESPONSE_MODELS,
    ActionView,
    ApprovalView,
    CaseCard,
    CaseDetail,
    CaseSections,
    DecisionView,
    DeliverEventRequest,
    FactView,
    RequestModel,
    ResponseFieldHasDefault,
    ResponseModel,
    RunView,
    TimelineEventView,
    check_no_response_defaults,
)
from careloop.api.projections import (
    COMPLETED,
    DERIVED,
    NEEDS_YOU,
    SECTION_ORDER,
    SECTIONS,
    SOURCE_MODELS,
    SOURCES,
    TIMELINE_PAYLOAD_FIELDS,
    WITHHELD,
    WORKING,
    SectionsAreNotAPartition,
    UndecidedField,
    action_view,
    approval_view,
    case_card,
    case_detail,
    case_sections,
    check_every_response_field_has_a_source,
    check_every_stored_field_is_decided,
    check_sections_partition_every_state,
    decision_view,
    fact_view,
    payload_keys_in,
    run_view,
    section_for,
    timeline_event_view,
)
from careloop.api.runs import (
    RunAlreadyInFlight,
    RunRecord,
    RunRegistry,
    RunState,
    UnknownRun,
)

__all__ = [
    "API_PREFIX",
    "COMPLETED",
    "DERIVED",
    "NEEDS_YOU",
    "RESPONSE_MODELS",
    "SECTIONS",
    "SECTION_ORDER",
    "SOURCES",
    "SOURCE_MODELS",
    "TIMELINE_PAYLOAD_FIELDS",
    "WITHHELD",
    "WORKING",
    "ActionView",
    "ApiDeps",
    "ApprovalView",
    "CaseCard",
    "CaseDetail",
    "CaseSections",
    "ContextFactory",
    "DecisionView",
    "DeliverEventRequest",
    "FactView",
    "RequestModel",
    "ResponseFieldHasDefault",
    "ResponseModel",
    "RunAlreadyInFlight",
    "RunRecord",
    "RunRegistry",
    "RunState",
    "RunView",
    "SectionsAreNotAPartition",
    "TimelineEventView",
    "UndecidedField",
    "UnknownRun",
    "action_view",
    "approval_view",
    "caregiver",
    "case_card",
    "case_detail",
    "case_sections",
    "check_every_response_field_has_a_source",
    "check_every_stored_field_is_decided",
    "check_no_response_defaults",
    "check_sections_partition_every_state",
    "create_app",
    "decision_view",
    "default_context_factory",
    "fact_view",
    "load_case",
    "load_event",
    "payload_keys_in",
    "run_view",
    "schedule",
    "section_for",
    "sections_from",
    "timeline_event_view",
]
