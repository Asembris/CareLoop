"""What the app is bound to, and who is asking.

Everything injectable is injected here rather than reached for at module scope, for the reason
:class:`~careloop.graph.context.RunContext` does the same: a surface that constructs its own store
can only be tested against the one it constructs.

**The caregiver identity is a stub and says so.** ``X-Caregiver`` supplies ``decided_by`` for
``approve`` and ``reject``, which is an *audit* fact — "who said yes" is part of the trail invariant
8 keeps. It is not authentication: nothing is verified, no secret is presented, and anyone who can
reach the port can claim any name. A missing header is therefore **400**, not 401. 401 means "you
have not authenticated", and returning it would claim a check that is not happening; 400 says what
is true, which is that a required field is absent. Real authentication is a later session's work and
would sit in front of this, not replace it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from careloop.events.inbound import InboundEvent
from careloop.events.simulator import EventSimulator
from careloop.graph.context import RunContext
from careloop.graph.runner import context_for
from careloop.state.case import Case, CaseState
from careloop.state.store import CaseStore

#: Builds the run context one graph run is driven through.
#:
#: The default uses whatever :mod:`careloop.llm.providers` resolves from the environment, which is
#: the mock unless something says otherwise. A test injects one carrying scripted agents; the demo
#: could inject one carrying Nova. Nothing about the routes changes either way.
ContextFactory = Callable[[CaseStore, InboundEvent, str], RunContext]


def default_context_factory(store: CaseStore, inbound: InboundEvent, case_id: str) -> RunContext:
    return context_for(store, inbound, case_id=case_id)


@dataclass
class ApiDeps:
    """The collaborators the routes are bound to.

    Holds no case and no run state. Anything durable is in the store; anything about a run in
    flight is in the run registry, which is execution context rather than a business fact.
    """

    store: CaseStore
    events: EventSimulator = field(default_factory=EventSimulator)
    context_factory: ContextFactory = default_context_factory


def deps(request: Request) -> ApiDeps:
    """The app's dependencies, as the routes receive them."""
    return request.app.state.careloop


Deps = Annotated[ApiDeps, Depends(deps)]


def caregiver(x_caregiver: Annotated[str | None, Header()] = None) -> str:
    """Who is recording a decision, for the audit trail.

    Not authentication — see the module docstring for why this is a 400 rather than a 401.
    """
    if not (x_caregiver or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "the X-Caregiver header is required: a decision is recorded against whoever made "
                "it, and the audit trail has nowhere to put an anonymous one"
            ),
        )
    return x_caregiver.strip()


Caregiver = Annotated[str, Depends(caregiver)]


def sections_from(
    store: CaseStore, sections: dict[str, frozenset[CaseState]]
) -> dict[str, list[Case]]:
    """Load every case, grouped by dashboard section, most recently active first.

    One ``list_by_state`` call per state rather than a new store method. The interface is shaped
    for a conditional-write key-value store and a section-aware query would be a persistence-layer
    decision; a demo holds a handful of cases, and this keeps DynamoDB replacing SQLite without
    touching callers.

    One ordering rule for all three columns rather than three, because a caregiver scanning the
    dashboard is looking for what moved, and "what moved" is the same question in each.
    """
    return {
        name: sorted(
            (case for state in states for case in store.list_by_state(state)),
            key=lambda case: (case.updated_at, case.case_id),
            reverse=True,
        )
        for name, states in sections.items()
    }


__all__ = [
    "ApiDeps",
    "Caregiver",
    "ContextFactory",
    "Deps",
    "caregiver",
    "default_context_factory",
    "deps",
    "sections_from",
]
