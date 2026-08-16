"""The routes.

Each one reads the store and projects, or calls a function that already exists. There is no policy
evaluation here, no state transition, no fact promotion, and no third place that decides anything.

Deliberately small. The dashboard has three sections, a detail view, and two buttons; an endpoint
the dashboard will not call is an endpoint nobody will maintain and nobody will notice breaking.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, status

from careloop.api.demo import DEMO_SCENARIOS
from careloop.api.deps import (
    ApiDeps,
    Caregiver,
    ContextFactory,
    Deps,
    GraphRunner,
    clinic_context_factory,
    default_context_factory,
    sections_from,
)
from careloop.api.models import CaseDetail, CaseSections, DeliverEventRequest, RunView
from careloop.api.projections import SECTIONS, case_detail, case_sections, run_view
from careloop.api.runs import RunAlreadyInFlight, UnknownRun
from careloop.api.sessions import SessionsAtCapacity, SessionWorlds
from careloop.events.inbound import InboundEvent
from careloop.events.simulator import EventSimulator
from careloop.graph.context import latest_delivery
from careloop.graph.runner import (
    AmbiguousApproval,
    ApprovalAlreadyDecided,
    CaseNotRecheckable,
    CaseNotReopenable,
    NoSuchApproval,
    NotAnApprovableAction,
    approve,
    open_case,
    reject,
    reopen_case,
    request_recheck,
)
from careloop.state.case import Case
from careloop.state.machine import IllegalTransition
from careloop.state.store import CaseNotFound, CaseStore
from careloop.tools.clinic import ClinicClient

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


def build_dependencies(
    store: CaseStore | None,
    *,
    sessions: SessionWorlds | None,
    events: EventSimulator | None,
    context_factory: ContextFactory | None,
    graph_runner: GraphRunner | None,
    clinic: ClinicClient | None,
    demo: bool,
) -> ApiDeps | SessionWorlds:
    """What an app is bound to: one set of dependencies, or one per visitor.

    Separate from :func:`create_app` so the exclusivity is a function a test can call rather than an
    argument check buried in route construction. It returns the registry *itself* in isolated mode
    — there is no wrapper holding a shared store beside it, because a shared store that exists is a
    shared store something can reach.

    Raises:
        ValueError: if the two modes are mixed, or neither is chosen.
    """
    if (store is None) == (sessions is None):
        raise ValueError(
            "create_app needs exactly one of store= (one world for everybody) or sessions= (one "
            "world per visitor); it was given both or neither"
        )

    if sessions is not None:
        per_world = {
            "events": events,
            "context_factory": context_factory,
            "graph_runner": graph_runner,
            "clinic": clinic,
        }
        if named := sorted(name for name, value in per_world.items() if value is not None):
            raise ValueError(
                f"sessions= builds each visitor's world, so {', '.join(named)} belongs to the "
                f"builder rather than to the app; passing it here would bind something no route "
                f"would ever reach"
            )
        return sessions

    factory = context_factory
    if factory is None:
        factory = clinic_context_factory(clinic) if clinic is not None else default_context_factory

    # Resolved before construction rather than assigned onto the instance afterwards: the local
    # graph runner is built from whichever factory this app ended up with, and a field patched in
    # after ``__post_init__`` would have been built from the default one.
    return ApiDeps(
        store=store,
        events=events or EventSimulator(),
        context_factory=factory,
        graph_runner=graph_runner,
        clinic=clinic,
        demo=demo,
    )


def create_app(
    store: CaseStore | None = None,
    *,
    sessions: SessionWorlds | None = None,
    events: EventSimulator | None = None,
    context_factory: ContextFactory | None = None,
    graph_runner: GraphRunner | None = None,
    clinic: ClinicClient | None = None,
    demo: bool = False,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """Build the app over a store, or over a world per visitor.

    Everything the routes need arrives here. Nothing is read from module scope and nothing is
    constructed lazily on first request, so two apps in one process cannot share a store neither of
    them was given.

    ``sessions`` replaces ``store`` rather than layering on it: an isolated app holds a
    :class:`~careloop.api.sessions.SessionWorlds` and **no app-scoped dependencies whatsoever**, so
    there is no shared store for a request to reach by forgetting to name a session. Exactly one of
    the two must be given, and everything a world owns — the store, the events, the context factory,
    the graph runner, the clinic — belongs to the builder in that mode and is refused here. An app
    half-bound to both would be an app where "which store did that write go to" has two answers.

    ``clinic`` binds every run to a real external clinic — its bookings are performed over HTTP and
    verified by reading the clinic back. Composition, not decision: the routes are identical either
    way, exactly as they are identical whether the model is the mock or Nova.

    ``demo`` mounts the control surface under ``/api/demo``. **Off by default and explicit**: the
    reset it exposes deletes every case, which is right in front of a judge and wrong everywhere
    else. An app built without it does not have the routes at all rather than having them refuse.

    ``static_dir`` serves a built frontend from this same origin, which is what lets a deployment
    keep the browser same-origin without any CORS middleware — see :mod:`careloop.api.static`.
    Off by default for the same reason ``demo`` is: an app that serves files nobody asked for is
    not the app the tests exercise.

    ``graph_runner`` says *where* a run happens rather than what it is bound to, and supplying one
    replaces local execution entirely — including ``context_factory``, which a remote runner has no
    use for. See :data:`careloop.api.deps.GraphRunner`.

    Raises:
        ValueError: if neither ``store`` nor ``sessions`` is given, if both are, or if a per-world
            collaborator is passed alongside ``sessions``.
    """
    bound = build_dependencies(
        store,
        sessions=sessions,
        events=events,
        context_factory=context_factory,
        graph_runner=graph_runner,
        clinic=clinic,
        demo=demo,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Let in-flight runs finish before the process goes away.

        A run killed mid-flight leaves a case in whatever state its last committed node put it in —
        recoverable, because the store is the truth and the next run dispatches on it, but a graph
        that was two nodes from ``DONE`` would need a human to notice. Draining is cheap and the
        bound on how long it can take is the SDK's own execution timeout. Every world drains, for
        the same reason: they are separate stores, not separate importances.
        """
        yield
        await bound.drain()

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

    @app.post(f"{API_PREFIX}/sessions", status_code=status.HTTP_201_CREATED)
    def open_session() -> dict[str, str]:
        """Be given a world of your own. The first thing a browser does, and the only thing it does.

        Not a response model, for the reason ``/api/health`` is not: it projects no stored case, so
        there is no stored field for a response field to name as its source.

        **404 when the app is not isolated**, rather than a route that exists and answers something
        unusable. A frontend takes that as "this backend has one world" and carries no session at
        all, which is exactly right for local development and for the offline suite — and it is the
        same treatment the demo control surface gets, for the same reason.

        The id is an isolation namespace and not authentication: nothing is verified and anyone
        holding one may use it. See :mod:`careloop.api.sessions`.
        """
        if not isinstance(bound, SessionWorlds):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="this CareLoop serves one world to everybody; no session is needed",
            )
        try:
            return {"session": bound.mint()}
        except SessionsAtCapacity as full:
            # 503 rather than 429: nothing about this caller is being rate limited, and the answer
            # is "come back in a minute" rather than "you asked too often".
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(full)
            ) from None

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
        return await deliver(deps, body.case_id, body.event_id, wait=wait)

    @app.post(
        f"{API_PREFIX}/cases/{{case_id}}/decisions/{{approval_id}}/approve",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def approve_decision(
        case_id: str, approval_id: str, deps: Deps, who: Caregiver, wait: float = 0.0
    ) -> RunView:
        """One click in "Needs You". ``AWAITING_HUMAN`` → ``READY_TO_EXECUTE``, then run.

        Two existing calls in order: :func:`careloop.graph.runner.approve` records the decision,
        and the run that follows finds the case already released and dispatches straight to
        execution. Nothing about *what* happens next is decided here — the gate and the tool
        boundary both check again, and an approval covering different arguments would not release
        this one.
        """
        return await decide(approve, case_id, approval_id, deps, who, wait)

    @app.post(
        f"{API_PREFIX}/cases/{{case_id}}/decisions/{{approval_id}}/reject",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def reject_decision(
        case_id: str, approval_id: str, deps: Deps, who: Caregiver, wait: float = 0.0
    ) -> RunView:
        """The other button. ``AWAITING_HUMAN`` → ``PLANNING``, then run.

        A refused proposal is not a failed case: the planner gets another attempt, bounded like
        every other. Running afterwards is what makes that true rather than nominal — without it
        the case would sit in ``PLANNING`` with nothing driving it.
        """
        return await decide(reject, case_id, approval_id, deps, who, wait)

    @app.post(
        f"{API_PREFIX}/cases/{{case_id}}/recheck",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def recheck_case(case_id: str, deps: Deps, who: Caregiver, wait: float = 0.0) -> RunView:
        """ "I checked the clinic." Ask CareLoop to observe an existing effect again.

        ``AWAITING_HUMAN`` → ``VERIFYING``, then run. The case parked because an effect could not be
        certified; this asks for another independent read of that exact effect, and for nothing
        else. :func:`careloop.graph.runner.request_recheck` decides whether the case is in a shape
        where that means anything, the graph run that follows executes ``dispatch → verify``, and
        the verify node refuses to certify if no observer actually took part.

        **The route takes no body, and that is the security property rather than an omission.**
        Which external object is read is derived entirely from durable case state, so there is no
        parameter here through which a caller could name a tool, an argument, a digest, a
        confirmation id or an action index. A hostile body cannot participate in target selection
        because nothing binds one.

        The in-flight check comes first so the request is all-or-nothing, exactly as
        :func:`decide` does: recording the request and then failing to schedule would leave a case
        released into ``VERIFYING`` with a caller told it was refused.
        """
        case = load_case(deps.store, case_id)
        if (running := deps.runs.in_flight_for(case_id)) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"case {case_id!r} has run {running.run_id} in flight; ask for a re-check once "
                    f"it settles"
                ),
            )
        inbound = resume_event(deps, case)

        try:
            request_recheck(deps.store, case_id, requested_by=who)
        except (CaseNotRecheckable, IllegalTransition) as refusal:
            # 409 for the reason a decision conflict is one: the request is well formed and names a
            # case the store really holds. What it conflicts with is the state of that case.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(refusal)) from None

        return await schedule(deps, case_id, inbound, wait=wait)

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

    @app.middleware("http")
    async def never_cache_the_api(request: Request, call_next: Callable) -> Response:
        """Mark every ``/api`` response uncacheable, at the origin.

        The dashboard polls ``GET /api/cases`` to watch a run proceed, and every response is a
        point-in-time projection of a store that is changing underneath it. FastAPI sets no cache
        headers at all, which leaves a browser — or a CDN in front of this — free to apply
        *heuristic* caching and serve a stale case list. That failure looks exactly like the
        backend having hung mid-run, which is the worst possible thing for it to look like.

        Set here rather than only in the CDN's cache policy because the property belongs to the
        response, not to one deployment's configuration: a second CloudFront behaviour, a proxy or
        a browser back-button would each have to rediscover it otherwise.
        """
        response = await call_next(request)
        if request.url.path.startswith(API_PREFIX):
            response.headers["Cache-Control"] = "no-store"
        return response

    if demo:
        mount_demo(app)
    if static_dir is not None:
        # Imported here rather than at module scope: careloop.api.static reads API_PREFIX from
        # this module, and the catch-all it registers must come after every route above.
        from careloop.api.static import mount_static

        mount_static(app, Path(static_dir))
    return app


# -- the demo control surface -----------------------------------------------------------------


def mount_demo(app: FastAPI) -> None:
    """Add ``/api/demo`` to ``app``: reset the world, and press one of three scenarios.

    **Both routes run the normal path underneath.** The scenario route calls the same
    :func:`deliver` that ``POST /api/events`` calls, with a case id and an event id off a table —
    so a demo case is produced by the same code, the same graph and the same gate as one a browser
    delivered, and ``tests/test_api_demo.py`` asserts the two are indistinguishable. There is no
    video-only branch, because a demo that does something the real flow does not is a demo that
    lies.
    """

    @app.post(f"{API_PREFIX}/demo/reset")
    async def reset(deps: Deps) -> dict[str, object]:
        """Clear this world's cases, and put its clinic back to the seeded world.

        Not a response model, for the reason ``/api/health`` is not: it projects no stored case, so
        there is no stored field for a response field to name as its source.

        **Whose world is not a parameter.** ``deps.store`` is the store the presented session
        resolved to, so on an isolated app this cannot empty anybody else's cases — not because it
        checks, but because there is no other store in reach.
        """
        return reset_world(deps)

    @app.post(
        f"{API_PREFIX}/demo/scenarios/{{key}}",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_scenario(key: str, deps: Deps, wait: float = 0.0) -> RunView:
        """Put this world back and trigger one scenario, through the ordinary delivery path.

        **One request, and that is a repair rather than a convenience.** The frontend used to reset
        and then start, with a retry budget in between, because two scenarios carry the same
        referral and the second would otherwise ask a clinic that already holds the slot. Two
        requests means a window: the reset can land and the start can be refused, leaving a world
        wiped and nothing running, and no client can make that atomic from outside. Here the pair
        happens under this world's own lock, so a visitor pressing twice quickly gets one settled
        outcome rather than two half-applied ones.

        The lock is per world, so it never makes one visitor wait for another.
        """
        scenario = DEMO_SCENARIOS.get(key)
        if scenario is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no demo scenario {key!r}; known: {', '.join(sorted(DEMO_SCENARIOS))}",
            )

        async with deps.scenarios:
            reset_world(deps)
            return await deliver(deps, scenario.case_id, scenario.event_id, wait=wait)


def reset_world(deps: ApiDeps) -> dict[str, object]:
    """Clear one world's cases and reseed its clinic.

    One function rather than two call sites, because ``POST /api/demo/reset`` and the reset a
    scenario performs are the same act and a scenario that reset *slightly* differently is a demo
    with a second code path in it.

    **Refused while any run is in flight.** Clearing the store underneath a live graph is a version
    conflict wearing a costume, and the run would keep going against a store nobody is reading.
    Waiting a second and pressing again is the whole remedy.
    """
    if (running := deps.runs.any_in_flight()) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"run {running.run_id} is in flight for case {running.case_id!r}; reset once it "
                f"settles"
            ),
        )

    removed = deps.store.clear()
    if deps.clinic is not None:
        deps.clinic.reset()
    return {
        "cases_cleared": removed,
        "clinic_reset": deps.clinic is not None,
        "scenarios": sorted(DEMO_SCENARIOS),
    }


def load_event(events: EventSimulator, event_id: str) -> InboundEvent:
    """One inbound event, or a 404 that names it."""
    try:
        return events.get(event_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no event {event_id!r}"
        ) from None


def resume_event(deps: ApiDeps, case: Case) -> InboundEvent:
    """The event a case is carried forward under.

    :func:`careloop.graph.context.latest_delivery` rather than ``case.event_id``: a reopened case
    still names the *referral* there, deliberately, so resuming on that field would hand the run
    the wrong documents. Reading it through the graph layer's named reader is what keeps this
    package out of the timeline.
    """
    delivery = latest_delivery(case)
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"case {case.case_id!r} records no inbound delivery, so there is no event to "
                f"resume it under"
            ),
        )
    return load_event(deps.events, delivery.event_id)


async def decide(
    record: Callable[..., Case],
    case_id: str,
    approval_id: str,
    deps: ApiDeps,
    who: str,
    wait: float,
) -> RunView:
    """Record a caregiver decision and carry the case forward.

    The in-flight check comes first so the request is all-or-nothing. Recording the approval and
    then failing to schedule would leave a decision made, a case released, and a caller told the
    request was refused — three things that disagree.
    """
    case = load_case(deps.store, case_id)
    if (running := deps.runs.in_flight_for(case_id)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"case {case_id!r} has run {running.run_id} in flight; decide once it settles",
        )
    inbound = resume_event(deps, case)

    try:
        record(deps.store, case_id, approval_id, decided_by=who)
    except NoSuchApproval as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from None
    except (
        ApprovalAlreadyDecided,
        AmbiguousApproval,
        NotAnApprovableAction,
        IllegalTransition,
    ) as refusal:
        # 409 rather than 400: the request is well formed and names a record the case really
        # carries. What it conflicts with is the state of that record — already decided; or a
        # manual handoff persisted before those stopped being written as approvals, which
        # authorises no call at all; or an identifier persisted before request identity became
        # unique, which names two records that are both still pending and so answers no question
        # about which one is being decided. A 400 would blame the caller for the case's history.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(refusal)) from None

    return await schedule(deps, case_id, inbound, wait=wait)


async def deliver(deps: ApiDeps, case_id: str, event_id: str, *, wait: float) -> RunView:
    """Deliver an inbound event to a case and start a run. The one delivery path.

    Extracted from the ``POST /api/events`` handler so the demo control surface can press exactly
    this and nothing beside it. Whether the case is opened or reopened is read off the store rather
    than decided here, and whether a reopen is allowed at all belongs to
    :func:`careloop.graph.runner.reopen_case`.
    """
    inbound = load_event(deps.events, event_id)

    try:
        deps.store.get(case_id)
    except CaseNotFound:
        open_case(deps.store, inbound, case_id=case_id)
    else:
        try:
            reopen_case(deps.store, inbound, case_id=case_id)
        except CaseNotReopenable as refusal:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(refusal)) from None

    return await schedule(deps, case_id, inbound, wait=wait)


async def schedule(deps: ApiDeps, case_id: str, inbound: InboundEvent, *, wait: float) -> RunView:
    """Start a graph run for ``case_id`` and report where it got to.

    The only thing this layer originates: *when* a run happens. It decides nothing about what the
    run does, and — since :data:`~careloop.api.deps.GraphRunner` — nothing about where: the runner
    supplies the collaborators and performs the run, and all this knows is that it got a
    :class:`~careloop.graph.runner.RunResult` back.
    """
    try:
        record = deps.runs.start(case_id, lambda: deps.graph_runner(deps.store, inbound, case_id))
    except RunAlreadyInFlight as clash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(clash)) from None

    return run_view(await deps.runs.wait(record.run_id, wait))


__all__ = [
    "API_PREFIX",
    "build_dependencies",
    "create_app",
    "decide",
    "deliver",
    "load_case",
    "load_event",
    "mount_demo",
    "reset_world",
    "resume_event",
    "schedule",
]
