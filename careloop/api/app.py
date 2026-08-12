"""The routes.

Each one reads the store and projects, or calls a function that already exists. There is no policy
evaluation here, no state transition, no fact promotion, and no third place that decides anything.

Deliberately small. The dashboard has three sections, a detail view, and two buttons; an endpoint
the dashboard will not call is an endpoint nobody will maintain and nobody will notice breaking.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from careloop.api.deps import ApiDeps, ContextFactory, Deps, sections_from
from careloop.api.models import CaseDetail, CaseSections, DeliverEventRequest, RunView
from careloop.api.projections import SECTIONS, case_detail, case_sections, run_view
from careloop.api.runs import RunAlreadyInFlight, UnknownRun
from careloop.events.inbound import InboundEvent
from careloop.events.simulator import EventSimulator
from careloop.graph.runner import CaseNotReopenable, open_case, reopen_case, run_case
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
    bound = ApiDeps(store=store, events=events or EventSimulator())
    if context_factory is not None:
        bound.context_factory = context_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Let in-flight runs finish before the process goes away.

        A run killed mid-flight leaves a case in whatever state its last committed node put it in —
        recoverable, because the store is the truth and the next run dispatches on it, but a graph
        that was two nodes from ``DONE`` would need a human to notice. Draining is cheap and the
        bound on how long it can take is the SDK's own execution timeout.
        """
        yield
        await bound.runs.drain()

    app = FastAPI(
        title="CareLoop",
        summary="A read surface over the case store, plus approve, reject and deliver.",
        version="0.1.0",
        lifespan=lifespan,
    )
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

    @app.post(
        f"{API_PREFIX}/events",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def deliver_event(body: DeliverEventRequest, deps: Deps, wait: float = 0.0) -> RunView:
        """ "An email just arrived." Deliver an inbound event and start a run.

        Opens the case if it does not exist and reopens it if it does. Which of those applies is
        read off the store rather than decided here, and whether a reopen is allowed at all belongs
        to :func:`careloop.graph.runner.reopen_case` — a case still in flight is refused there and
        surfaces as a 409, so the rule has one home.

        Returns 202 with a run id. ``?wait=<seconds>`` blocks until the run settles, which is what
        a test uses instead of polling; the run is not cancelled if the wait expires.
        """
        inbound = load_event(deps.events, body.event_id)

        try:
            deps.store.get(body.case_id)
        except CaseNotFound:
            open_case(deps.store, inbound, case_id=body.case_id)
        else:
            try:
                reopen_case(deps.store, inbound, case_id=body.case_id)
            except CaseNotReopenable as refusal:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(refusal)
                ) from None

        return await schedule(deps, body.case_id, inbound, wait=wait)

    @app.get(f"{API_PREFIX}/runs/{{run_id}}", response_model=RunView)
    async def get_run(run_id: str, deps: Deps, wait: float = 0.0) -> RunView:
        """One run: whether it settled, and which graph nodes it went through.

        The under-the-hood panel's node trace. Held in memory for this process only — see
        :mod:`careloop.api.runs` for why that does not make it a business fact.

        ``?wait=<seconds>`` is the same opt-in the verbs take, so a caller that gave up waiting the
        first time can pick the run back up rather than poll for it.
        """
        try:
            deps.runs.get(run_id)
        except UnknownRun:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no run {run_id!r}"
            ) from None
        return run_view(await deps.runs.wait(run_id, wait))

    return app


def load_event(events: EventSimulator, event_id: str) -> InboundEvent:
    """One inbound event, or a 404 that names it."""
    try:
        return events.get(event_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no event {event_id!r}"
        ) from None


async def schedule(deps: ApiDeps, case_id: str, inbound: InboundEvent, *, wait: float) -> RunView:
    """Start a graph run for ``case_id`` and report where it got to.

    The only thing this layer originates: *when* a run happens. It decides nothing about what the
    run does — the context factory supplies the collaborators and
    :func:`careloop.graph.runner.run_case` does the rest.
    """
    try:
        record = deps.runs.start(
            case_id, lambda: run_case(deps.context_factory(deps.store, inbound, case_id))
        )
    except RunAlreadyInFlight as clash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(clash)) from None

    return run_view(await deps.runs.wait(record.run_id, wait))


__all__ = ["API_PREFIX", "create_app", "load_case", "load_event", "schedule"]
