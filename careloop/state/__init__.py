"""Durable case state: the model, the state machine, and storage."""

from careloop.state.case import (
    ActionRecord,
    Approval,
    ApprovalStatus,
    Case,
    CaseState,
    TimelineEvent,
    TimelineKind,
)
from careloop.state.evidence import (
    FactNotOnCase,
    PromotionIneffective,
    promote_fact,
    record_fact,
)
from careloop.state.machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    append_event,
    is_legal,
    transition,
)
from careloop.state.memory_store import InMemoryCaseStore
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.state.store import CaseAlreadyExists, CaseNotFound, CaseStore, VersionConflict

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ActionRecord",
    "Approval",
    "ApprovalStatus",
    "Case",
    "CaseAlreadyExists",
    "CaseNotFound",
    "CaseState",
    "CaseStore",
    "FactNotOnCase",
    "IllegalTransition",
    "InMemoryCaseStore",
    "PromotionIneffective",
    "SqliteCaseStore",
    "TimelineEvent",
    "TimelineKind",
    "VersionConflict",
    "append_event",
    "is_legal",
    "promote_fact",
    "record_fact",
    "transition",
]
