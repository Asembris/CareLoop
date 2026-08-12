"""The routes.

Each one reads the store and projects, or calls a function that already exists. There is no policy
evaluation here, no state transition, no fact promotion, and no third place that decides anything.

Deliberately small. The dashboard has three sections, a detail view, and two buttons; an endpoint
the dashboard will not call is an endpoint nobody will maintain and nobody will notice breaking.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from careloop.api.deps import ApiDeps, ContextFactory, Deps, sections_from
from careloop.api.models import CaseDetail, CaseSections
from careloop.api.projections import SECTIONS, case_detail, case_sections
from careloop.events.simulator import EventSimulator
from careloop.state.case import Case
from careloop.state.store import CaseNotFound, CaseStore

API_PREFIX = "/api"


def load_case(store: CaseStore, case_id: str) -> Case:
    """One case, or a 404 that names it.

    ``CaseNotFound`` is the store's own vocabulary and is translated here rather than caught
    somewhere generic, so a missing case reads as a missing case rather than as a 500.
    """
    try:
        return store.get(case_id)
    except CaseNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no case {case_id!r}"
        ) from None


def create_app(
    store: CaseStore,
    *,
    events: EventSimulator | None = None,
    context_factory: ContextFactory | None = None,
) -> FastAPI:
    """Build the app over a store.

    Everything the routes need arrives here. Nothing is read from module scope and nothing is
    constructed lazily on first request, so two apps in one process cannot share a store neither of
    them was given.
    """
    app = FastAPI(
        title="CareLoop",
        summary="A read surface over the case store, plus approve, reject and deliver.",
        version="0.1.0",
    )
    bound = ApiDeps(store=store, events=events or EventSimulator())
    if context_factory is not None:
        bound.context_factory = context_factory
    app.state.careloop = bound

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict[str, str]:
        """Liveness.

        Not a response model, because it is not a projection: it says nothing about any case, so
        there is no stored field for it to name as its source.
        """
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/cases", response_model=CaseSections)
    def list_cases(deps: Deps) -> CaseSections:
        """The dashboard: every case, in the section its state puts it in."""
        return case_sections(sections_from(deps.store, dict(SECTIONS)))

    @app.get(f"{API_PREFIX}/cases/{{case_id}}", response_model=CaseDetail)
    def get_case(case_id: str, deps: Deps) -> CaseDetail:
        """One case in full: state, evidence with provenance, decisions, effects, audit trail."""
        return case_detail(load_case(deps.store, case_id))

    return app


__all__ = ["API_PREFIX", "create_app", "load_case"]
