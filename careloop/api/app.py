"""The routes.

Each one reads the store and projects, or calls a function that already exists. There is no policy
evaluation here, no state transition, no fact promotion, and no third place that decides anything.

Deliberately small. The dashboard has three sections, a detail view, and two buttons; an endpoint
the dashboard will not call is an endpoint nobody will maintain and nobody will notice breaking.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request, Response, status

from careloop.api.access import Access, Actor, CaseAccess
from careloop.api.auth import AuthBoundary, Boundary, Caller, demo_auth
from careloop.api.demo import DEMO_SCENARIOS, DemoScenario
from careloop.api.deps import (
    CAREGIVER_HEADER,
    ApiDeps,
    ContextFactory,
    GraphRunner,
    clinic_context_factory,
    default_context_factory,
)
from careloop.api.faults import BookingResponseLoss
from careloop.api.models import CaseDetail, CaseSections, DeliverEventRequest, RunView
from careloop.api.projections import SECTIONS, case_detail, case_sections, run_view
from careloop.api.runs import RunAlreadyInFlight, UnknownRun
from careloop.api.sessions import SESSION_HEADER, SessionsAtCapacity, SessionWorlds
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
from careloop.identity.directory import Caregiver
from careloop.state.case import Case
from careloop.state.machine import IllegalTransition
from careloop.state.store import CaseNotFound, CaseStore
from careloop.tools.clinic import ClinicClient
from careloop.tools.objectstore import ObjectStoreClient

logger = logging.getLogger(__name__)

API_PREFIX = "/api"


# -- what each route can answer -------------------------------------------------------------------
#
# Every status a route can really return is declared, because an undeclared one is a lie a generated
# request finds immediately: `tests/test_api_schema.py` fails an operation that answers a status its
# own schema does not name. Before that suite existed, ten of the eleven operations declared nothing
# but their success code and 422 while between them reaching 400, 404, 409, 410 and 503.
#
# **Descriptions, deliberately without a body schema.** Every error here is FastAPI's own
# ``{"detail": <string>}``, assembled from a stringified domain exception. Declaring a model for it
# would mean either plumbing a new response model through the import-time projection invariants in
# careloop/api/projections.py — which govern projections of stored cases, which an exception message
# is not — or hand-writing an inline schema here, which is a second source of truth for a shape
# FastAPI already owns. Neither is a declaration fix. What matters about those bodies is that they
# never carry a withheld field, and the recursive sweep in `tests/schema_checks.py` asserts that
# over every response whatever its declared schema, so nothing is lost by leaving the shape
# undeclared.

_SESSION_HEADER_MISSING: Final[str] = (
    "The X-CareLoop-Session header is absent. Reachable only on an app built with `sessions=`, "
    "where every request belongs to one visitor's world and a request naming none belongs to "
    "nobody; a single-world app needs no session and never answers this."
)

_CAREGIVER_HEADER_MISSING: Final[str] = (
    "The X-Caregiver header is absent. An audit identity rather than authentication, which is why "
    "this is a 400 and never a 401 — see careloop.api.deps."
)

#: The one status session resolution can answer that is not about the request being wrong.
#:
#: 410 is used nowhere else in this API, so a client can tell "your world is gone, mint another"
#: from the 409 that means a run is in flight.
_GONE: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_410_GONE: {
        "description": (
            "The presented session was never minted here, or has been idle long enough to be "
            "forgotten. Reachable only on an app built with `sessions=`."
        )
    }
}

#: Declared on every route taking :data:`~careloop.api.deps.Deps`, which is what resolves a session.
#:
#: Not declared globally on the app: ``GET /api/health`` and ``POST /api/sessions`` take no
#: dependencies and cannot answer either, and naming a status a route cannot produce is the same
#: kind of dishonesty as omitting one it can. One schema describes both app shapes, so these are
#: declared unconditionally and a single-world app simply never emits them.
SESSION_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_400_BAD_REQUEST: {"description": _SESSION_HEADER_MISSING},
    **_GONE,
}

#: The same, for the three routes that also record who decided.
#:
#: One 400 with two causes rather than two declarations, because a status may be declared once per
#: operation and both causes really are "a required header is absent".
CAREGIVER_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_400_BAD_REQUEST: {
        "description": f"{_CAREGIVER_HEADER_MISSING} Or: {_SESSION_HEADER_MISSING}"
    },
    **_GONE,
}

#: Shared by ``approve`` and ``reject``, whose reachable statuses are identical.
#:
#: The 409 covers several refusals on purpose and the wording says so: which one applies is a fact
#: about the case's own history, and a caller able to tell them apart by status has been handed a
#: map of the preconditions. The bodies differ; the status does not.
DECISION_ERRORS: Final[dict[int | str, dict[str, Any]]] = CAREGIVER_ERRORS | {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such case, no approval under that id, or no inbound delivery to resume the case "
            "under."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "The request is well formed and names a record the case really carries; what it "
            "conflicts with is the state of that record — a run already in flight, an approval "
            "already decided, an identifier naming two undecided records, a manual handoff that "
            "authorises no call, or a transition the state machine forbids."
        )
    },
}


#: What a caller-driven route answers when a grant no longer covers the work being asked for.
#:
#: Documented on the three routes that can issue or observe an effect. Deliberately **not** on the
#: read routes: reading a case you own is not acting for its patient, and a caregiver whose access
#: was withdrawn should still be able to see what was done in their name.
GRANT_ERRORS: Final[dict[int, dict[str, str]]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": (
            "You own this case, and you are not currently authorised to act for its patient. A "
            "403 rather than a 404 because ownership was already established, so the case's "
            "existence is not a secret and being told is more use than guessing."
        )
    }
}


def declare_required_headers(spec: dict[str, Any], required: frozenset[str]) -> dict[str, Any]:
    """Mark the headers the API really requires as ``required`` in ``spec``, in place.

    **Two layers disagreeing on purpose, reconciled here.** ``X-Caregiver`` and — on an isolated app
    — ``X-CareLoop-Session`` are required: a request without one is refused. But the dependencies
    that enforce them declare ``str | None = None`` and raise :class:`~fastapi.HTTPException`
    themselves, because that is what produces the deliberate **400**. Typing them as required
    parameters instead would hand the refusal to FastAPI's own validation and make it a **422**,
    which is the behaviour change ``careloop.api.deps`` argues against for ``X-Caregiver`` and would
    lose the 400/410 distinction for the session header.

    So the signature stays optional and the *schema* says what is true. Without this the generated
    document declared ``required: false`` for both, and a check that a missing required header is
    rejected had nothing to fire on — vacuous against the one header worth checking.

    Applied by rewriting the generated document rather than by ``openapi_extra`` on each route.
    Measured: ``openapi_extra`` **appends**, so the operation carries two ``x-caregiver`` entries
    with contradictory ``required`` values, which the OpenAPI specification forbids. This is also
    one place rather than one per route, so a fourth route taking the dependency cannot forget it.

    The ``null`` arm is dropped from the schema alongside. ``str | None`` emits
    ``anyOf: [string, null]``, and a header that may not be absent may not be null either; leaving
    it would let a generated request satisfy a required parameter with the value the handler exists
    to refuse.

    Args:
        spec: a generated OpenAPI document. Mutated and returned.
        required: header names, matched case-insensitively.
    """
    wanted = {name.lower() for name in required}
    for operations in spec.get("paths", {}).values():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "header":
                    continue
                if parameter.get("name", "").lower() not in wanted:
                    continue
                parameter["required"] = True
                schema = parameter.get("schema", {})
                if arms := schema.get("anyOf"):
                    kept = next((arm for arm in arms if arm.get("type") != "null"), None)
                    parameter["schema"] = (kept or {"type": "string"}) | {
                        key: value for key, value in schema.items() if key == "title"
                    }
    return spec


#: The OpenAPI name for the bearer scheme, and the only place it is spelled.
BEARER_SCHEME_NAME: Final[str] = "careloopBearer"


def declare_bearer_security(spec: dict[str, Any], *, production: bool) -> dict[str, Any]:
    """Describe the ``Authorization`` header as what it is: a security scheme, not a parameter.

    FastAPI emits it as an ordinary optional header parameter, because that is how the dependency
    reads it -- and the dependency reads it that way on purpose, so that a missing credential is
    CareLoop's deliberate ``401`` rather than FastAPI's ``422``. The same split
    :func:`declare_required_headers` reconciles for ``X-Caregiver``, one layer over.

    Left as a parameter it would also be *generated*: a schema-driven client would invent
    ``Authorization`` values for every operation, which is both wrong about the API and, measured,
    enough to make property-based generation filter almost everything out.

    **Demo mode declares no security at all**, which is truthful rather than tidy: nothing is
    verified there and a document claiming a bearer requirement would describe an app that does not
    exist. That is the same reason the demo's audit identity is a ``400`` and not a ``401``.

    Args:
        spec: a generated OpenAPI document. Mutated and returned.
        production: whether this app actually verifies a credential.
    """
    for operations in spec.get("paths", {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            kept = [
                parameter
                for parameter in operation.get("parameters", [])
                if not (
                    parameter.get("in") == "header"
                    and parameter.get("name", "").lower() == "authorization"
                )
            ]
            if kept:
                operation["parameters"] = kept
            else:
                operation.pop("parameters", None)

    if not production:
        return spec

    components = spec.setdefault("components", {})
    components.setdefault("securitySchemes", {})[BEARER_SCHEME_NAME] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "An OIDC access token. Verified in CareLoop against the configured issuer's JWKS: "
            "signature, issuer, audience, token use and lifetime. A failure is 401 and never a "
            "demo identity."
        ),
    }
    spec["security"] = [{BEARER_SCHEME_NAME: []}]
    return spec


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
    faults: BookingResponseLoss | None = None,
    objects: ObjectStoreClient | None = None,
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
            "faults": faults,
            "objects": objects,
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
        objects=objects,
        demo=demo,
        faults=faults,
    )


def create_app(
    store: CaseStore | None = None,
    *,
    sessions: SessionWorlds | None = None,
    events: EventSimulator | None = None,
    context_factory: ContextFactory | None = None,
    graph_runner: GraphRunner | None = None,
    clinic: ClinicClient | None = None,
    objects: ObjectStoreClient | None = None,
    demo: bool = False,
    static_dir: str | Path | None = None,
    faults: BookingResponseLoss | None = None,
    auth: AuthBoundary | None = None,
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

    ``faults`` is the recovery scenario's ability to lose one clinic answer, and it is a per-world
    collaborator exactly as ``clinic`` is — the isolated builder supplies its own, and passing one
    here alongside ``sessions=`` is refused with the rest. ``None``, which is the default, leaves
    the recovery scenario refusing truthfully rather than pretending to run.

    ``auth`` is the identity boundary: which mode this app runs in and the one verifier that
    establishes principals for it. Omitting it gives the judge-facing demo's synthetic caregiver,
    which is what every offline test and local run has always effectively had. A production app is
    built with :func:`careloop.api.auth.production_auth`, which requires a verifier and a directory
    by signature -- so an app that authenticates nobody while claiming to be production cannot be
    constructed, and there is no fallback from one mode to the other at request time.

    Raises:
        ValueError: if neither ``store`` nor ``sessions`` is given, if both are, or if a per-world
            collaborator is passed alongside ``sessions``.
        AuthModeMismatch: if ``auth`` names a mode its verifier does not establish.
    """
    bound = build_dependencies(
        store,
        sessions=sessions,
        events=events,
        context_factory=context_factory,
        graph_runner=graph_runner,
        clinic=clinic,
        objects=objects,
        demo=demo,
        faults=faults,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Pick up what the last process abandoned, then let this one's runs finish.

        **Startup.** A process that died between dispatching an external effect and recording its
        result left the case ``EXECUTING`` with a durably prepared action, and nothing else in the
        system will ever pick it up: no route reaches those states, and a run scheduled by hand had
        nowhere to go until reconciliation existed. Anything in one of those states *now* is
        orphaned by definition, because this process has scheduled nothing yet. See
        :meth:`careloop.api.deps.ApiDeps.resume_unsettled` for why that is the whole safety argument
        and why no lease is needed.

        This is a *trigger*, not a policy: it starts runs and decides nothing about them. What may
        be re-issued is a declared provider contract read inside the node, and what is re-issued is
        the identity the case already froze.

        **Shutdown.** A run killed mid-flight leaves a case in whatever state its last committed
        node put it in. That was described here as "recoverable, because the store is the truth and
        the next run dispatches on it" — which was **false** for exactly the two states a
        mid-effect kill produces, since ``dispatch`` had no edge for either and the case sat there
        for good. It is true now, and the startup sweep above is what makes it true rather than a
        hope about somebody noticing. Draining is still cheap and still bounded by the SDK's own
        execution timeout, and every world drains, because they are separate stores rather than
        separate importances.

        **The order inside a drain is a world's own contract** — wakes stopped and collected, then
        runs drained — and it lives in :meth:`careloop.api.deps.ApiDeps.drain` so that one world and
        a registry of them cannot come to answer differently. What this function must not do is
        reach past it and drain runs itself: a wake registering a run while the drain is already
        under way is work started after the process decided to stop.
        """
        resumed = bound.resume_unsettled()
        if resumed:
            logger.info("resuming %d case(s) abandoned mid-effect: %s", len(resumed), resumed)
        yield
        await bound.drain()

    app = FastAPI(
        title="CareLoop",
        summary="A read surface over the case store, plus approve, reject and deliver.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.careloop = bound
    # Beside the dependencies rather than among them. An isolated app deliberately holds no
    # app-scoped dependency that resolves to a world; this resolves to no world, holds no case and
    # answers one question about the caller -- and it has to be app-scoped, because a workspace
    # whose owner is checked against a principal cannot be the thing that produces the principal.
    app.state.careloop_auth = auth if auth is not None else demo_auth()

    @app.get(f"{API_PREFIX}/health")
    def health() -> dict[str, str]:
        """Liveness.

        Not a response model, because it is not a projection: it says nothing about any case, so
        there is no stored field for it to name as its source.
        """
        return {"status": "ok"}

    @app.post(
        f"{API_PREFIX}/sessions",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_404_NOT_FOUND: {
                "description": (
                    "This CareLoop serves one world to everybody, so there is no session to mint. "
                    "A frontend takes this as 'carry no session' rather than as an error."
                )
            },
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "description": (
                    "Every world is taken. 503 rather than 429: nothing about this caller is being "
                    "rate limited, and the answer is 'come back in a minute'."
                )
            },
        },
    )
    def open_session(bound_auth: Boundary, who: Caller) -> dict[str, str]:
        """Be given a world of your own. The first thing a browser does, and the only thing it does.

        Not a response model, for the reason ``/api/health`` is not: it projects no stored case, so
        there is no stored field for a response field to name as its source.

        **404 when the app is not isolated**, rather than a route that exists and answers something
        unusable. A frontend takes that as "this backend has one world" and carries no session at
        all, which is exactly right for local development and for the offline suite — and it is the
        same treatment the demo control surface gets, for the same reason.

        **The id is an isolation namespace and never authentication**, which is as true now as it
        was before anything was verified — what changed is that possession of one is no longer
        sufficient. In production the workspace records the verified caregiver who minted it and
        every later request must present the same principal; the id says *which* world, and the
        token says *whose*. In demo mode there is nothing to check and the id is the whole story,
        which is what keeps the judge flow immediate. See :mod:`careloop.api.sessions`.

        Minting requires an authenticated principal in production, so the anonymous capacity
        exhaustion an unauthenticated mint allows is not reachable there.
        """
        if not isinstance(bound, SessionWorlds):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="this CareLoop serves one world to everybody; no session is needed",
            )
        try:
            # **The owner comes from the principal and from nowhere else.** In production that is a
            # verified caregiver, so a workspace belongs to whoever was authenticated when it was
            # minted and to nobody a later request could name. In demo mode it is left unowned,
            # because a synthetic world is isolated rather than authorised and giving it an owner
            # would put a check in front of the judge flow that decides nothing.
            owner = who.caregiver_id if bound_auth.is_production else None
            return {"session": bound.mint(owner)}
        except SessionsAtCapacity as full:
            # 503 rather than 429: nothing about this caller is being rate limited, and the answer
            # is "come back in a minute" rather than "you asked too often".
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(full)
            ) from None

    @app.get(f"{API_PREFIX}/cases", response_model=CaseSections, responses=SESSION_ERRORS)
    def list_cases(access: Access) -> CaseSections:
        """The dashboard: every case this caller owns, in the section its state puts it in."""
        return case_sections(access.sections(dict(SECTIONS)))

    @app.get(
        f"{API_PREFIX}/cases/{{case_id}}",
        response_model=CaseDetail,
        responses=SESSION_ERRORS
        | {status.HTTP_404_NOT_FOUND: {"description": "The store holds no case under that id."}},
    )
    def get_case(case_id: str, access: Access) -> CaseDetail:
        """One case in full: state, evidence with provenance, decisions, effects, audit trail.

        A case this caller does not own answers exactly as a case that does not exist does.
        See :mod:`careloop.api.access`.
        """
        return case_detail(access.case(case_id))

    @app.post(
        f"{API_PREFIX}/events",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        responses=SESSION_ERRORS
        | GRANT_ERRORS
        | {
            status.HTTP_404_NOT_FOUND: {
                "description": "The simulator holds no event under that id."
            },
            status.HTTP_409_CONFLICT: {
                "description": (
                    "The case cannot take this delivery: it already has a run in flight, or it is "
                    "in a state no inbound event may reopen."
                )
            },
        },
    )
    async def deliver_event(
        body: DeliverEventRequest, access: Access, wait: float = 0.0
    ) -> RunView:
        """ "An email just arrived." Deliver an inbound event and start a run.

        Opens the case if it does not exist and reopens it if it does. Which of those applies is
        read off the store rather than decided here, and whether a reopen is allowed at all belongs
        to :func:`careloop.graph.runner.reopen_case` — a case still in flight is refused there and
        surfaces as a 409, so the rule has one home.

        Returns 202 with a run id. ``?wait=<seconds>`` blocks until the run settles, which is what
        a test uses instead of polling; the run is not cancelled if the wait expires.
        """
        access.may_open(body.case_id)
        access.may_open_a_case()
        return await deliver(
            access.deps, body.case_id, body.event_id, caregiver=access.caregiver, wait=wait
        )

    @app.post(
        f"{API_PREFIX}/cases/{{case_id}}/decisions/{{approval_id}}/approve",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DECISION_ERRORS | GRANT_ERRORS,
    )
    async def approve_decision(
        case_id: str, approval_id: str, access: Access, who: Actor, wait: float = 0.0
    ) -> RunView:
        """One click in "Needs You". ``AWAITING_HUMAN`` → ``READY_TO_EXECUTE``, then run.

        Two existing calls in order: :func:`careloop.graph.runner.approve` records the decision,
        and the run that follows finds the case already released and dispatches straight to
        execution. Nothing about *what* happens next is decided here — the gate and the tool
        boundary both check again, and an approval covering different arguments would not release
        this one.
        """
        return await decide(approve, case_id, approval_id, access, who, wait)

    @app.post(
        f"{API_PREFIX}/cases/{{case_id}}/decisions/{{approval_id}}/reject",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DECISION_ERRORS | GRANT_ERRORS,
    )
    async def reject_decision(
        case_id: str, approval_id: str, access: Access, who: Actor, wait: float = 0.0
    ) -> RunView:
        """The other button. ``AWAITING_HUMAN`` → ``PLANNING``, then run.

        A refused proposal is not a failed case: the planner gets another attempt, bounded like
        every other. Running afterwards is what makes that true rather than nominal — without it
        the case would sit in ``PLANNING`` with nothing driving it.
        """
        return await decide(reject, case_id, approval_id, access, who, wait)

    @app.post(
        f"{API_PREFIX}/cases/{{case_id}}/recheck",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        responses=CAREGIVER_ERRORS
        | GRANT_ERRORS
        | {
            status.HTTP_404_NOT_FOUND: {
                "description": (
                    "No such case, or no inbound delivery to resume the case under. The event a "
                    "re-check resumes under comes from the case's own trail, never from the caller."
                )
            },
            status.HTTP_409_CONFLICT: {
                "description": (
                    "The case is not in a shape where asking for another read means anything — "
                    "wrong state, a run in flight, a pending approval, the other kind of handoff, "
                    "a handoff naming no recorded action, a key naming no record, an identity "
                    "mismatch, or an action type nothing can independently observe. One generic "
                    "message for all of them, because a caller who can tell the preconditions "
                    "apart by probing has been handed a map of them."
                )
            },
        },
    )
    async def recheck_case(case_id: str, access: Access, who: Actor, wait: float = 0.0) -> RunView:
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
        deps = access.deps
        case = access.case(case_id)
        # A re-check issues no new effect -- it performs one independent read of an action
        # already carried out. It is gated anyway, because reading a patient's external
        # record is acting for that patient, and a caregiver whose access was withdrawn
        # should stop doing that too. This is **not** the recovery path: reconciliation is
        # driven by the startup sweep and the wake, neither of which can reach a directory.
        access.may_act_on(case)
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

    @app.get(
        f"{API_PREFIX}/runs/{{run_id}}",
        response_model=RunView,
        responses=SESSION_ERRORS
        | {
            status.HTTP_404_NOT_FOUND: {
                "description": (
                    "This process never scheduled a run under that id. Held in memory only, so a "
                    "restart forgets it — which costs a caller the answer to 'how did that run go' "
                    "and nothing durable."
                )
            }
        },
    )
    async def get_run(run_id: str, access: Access, wait: float = 0.0) -> RunView:
        """One run: whether it settled, and which graph nodes it went through.

        The under-the-hood panel's node trace. Held in memory for this process only — see
        :mod:`careloop.api.runs` for why that does not make it a business fact.

        ``?wait=<seconds>`` is the same opt-in the verbs take, so a caller that gave up waiting the
        first time can pick the run back up rather than poll for it.
        """
        deps = access.deps
        try:
            record = deps.runs.get(run_id)
        except UnknownRun:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no run {run_id!r}"
            ) from None
        # A run names its case, so reading one is reading something about that case. The
        # ownership question goes through the same predicate every other read uses, and a
        # run belonging to a case this caller does not own answers as an unknown id does.
        try:
            access.case(record.case_id)
        except HTTPException:
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

    # Which headers this app really requires, which is not the same question for both of them.
    # X-Caregiver is required of a **demo** app and refused by a production one, so it is declared
    # required only where it really is. X-CareLoop-Session is required only when there is a world
    # per visitor: on a single-world app it is genuinely optional, and declaring it required there
    # would have the schema claim something false.
    demo_identity = not app.state.careloop_auth.is_production
    required_headers = frozenset(
        ({CAREGIVER_HEADER} if demo_identity else set())
        | ({SESSION_HEADER} if isinstance(bound, SessionWorlds) else set())
    )

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            described = declare_required_headers(FastAPI.openapi(app), required_headers)
            app.openapi_schema = declare_bearer_security(
                described, production=app.state.careloop_auth.is_production
            )
        return app.openapi_schema

    app.openapi = openapi

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
    """Add ``/api/demo`` to ``app``: reset the world, and press one of the scenarios.

    **Both routes run the normal path underneath.** The scenario route calls the same
    :func:`deliver` that ``POST /api/events`` calls, with a case id and an event id off a table —
    so a demo case is produced by the same code, the same graph and the same gate as one a browser
    delivered, and ``tests/test_api_demo.py`` asserts the two are indistinguishable. There is no
    video-only branch, because a demo that does something the real flow does not is a demo that
    lies.

    **One scenario arms one thing, and it is still the normal path.** ``recovery`` declares that the
    clinic's answer to its case's booking may be discarded once, after the clinic has said it
    created the appointment — see :func:`arm_the_lost_booking_response`. Nothing about the graph,
    the gate, the saga, the wake or the read-back is different for it; what is different is that the
    answer to one real request does not come back, which is a thing that happens to real
    integrations and which CareLoop could not otherwise be watched surviving.
    """

    @app.post(
        f"{API_PREFIX}/demo/reset",
        responses=SESSION_ERRORS
        | {
            status.HTTP_409_CONFLICT: {
                "description": (
                    "A run is in flight. Clearing the store underneath a live graph is a version "
                    "conflict wearing a costume; waiting a second and pressing again is the whole "
                    "remedy."
                )
            }
        },
    )
    async def reset(access: Access) -> dict[str, object]:
        """Clear this world's cases, and put its clinic back to the seeded world.

        Not a response model, for the reason ``/api/health`` is not: it projects no stored case, so
        there is no stored field for a response field to name as its source.

        **Whose world is not a parameter.** ``deps.store`` is the store the presented session
        resolved to, so on an isolated app this cannot empty anybody else's cases — not because it
        checks, but because there is no other store in reach.
        """
        return await reset_world(access.deps)

    @app.post(
        f"{API_PREFIX}/demo/scenarios/{{key}}",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        responses=SESSION_ERRORS
        | {
            status.HTTP_404_NOT_FOUND: {
                "description": (
                    "No demo scenario under that key, or the simulator holds no event the scenario "
                    "names."
                )
            },
            status.HTTP_409_CONFLICT: {
                "description": (
                    "A run is in flight, so the reset this scenario begins with is refused; or the "
                    "case it delivers to is in a state no inbound event may reopen."
                )
            },
        },
    )
    async def run_scenario(key: str, access: Access, wait: float = 0.0) -> RunView:
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

        access.may_open_a_case()
        deps = access.deps
        async with deps.scenarios:
            await reset_world(deps)
            if scenario.loses_the_booking_response:
                arm_the_lost_booking_response(deps, scenario)
            return await deliver(
                deps,
                scenario.case_id,
                scenario.event_id,
                caregiver=access.caregiver,
                wait=wait,
            )


def arm_the_lost_booking_response(deps: ApiDeps, scenario: DemoScenario) -> None:
    """Let one booking made by ``scenario``'s case lose the clinic's answer to it. **The one site.**

    ``tests/test_demo_recovery.py::TestOnlyTheScenarioRouteArmsAnything`` sweeps the package for a
    second caller, in the arrangement ``CaseStore.clear`` already has: an affordance that can put a
    case into an unknown external outcome should be reachable from one place, and a sweep is what
    keeps it that way when the next person needs one for something else.

    **Between the reset and the delivery, and both halves matter.** The reset has just disarmed
    whatever the last scenario left and removed every case, so what is armed here can only be about
    the case the next line creates; and arming before the delivery is what makes it impossible for
    that case's booking to be dispatched with nothing armed. Both happen under ``deps.scenarios``,
    so a visitor pressing twice quickly gets one settled outcome rather than two half-applied ones.

    **It arms a case, not a request.** The binding is
    :meth:`careloop.api.faults.BookingResponseLoss.arm`, and it is enforced by the world's context
    factory handing the losing client only to runs for that case — so an unrelated booking elsewhere
    in this same visitor's world travels a transport containing no fault at all. What the transport
    then adds is that the answer must be a ``201``, which is the provider stating it created the
    appointment.

    **A world with no clinic is told so.** Every effect there is simulated, so there is no external
    write to be ambiguous about and no independent read to settle it with; a scenario that ran
    anyway would show a judge a recovery that demonstrated nothing. Refusing is the truthful answer
    and 409 is the code for it — the request is well formed and names a real scenario, and what it
    conflicts with is how this CareLoop is wired.
    """
    if deps.faults is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"scenario {scenario.key!r} needs an external clinic to lose an answer from, and "
                f"this CareLoop has none bound — every effect here is simulated, so there would be "
                f"no external write to be uncertain about and no independent record to settle it "
                f"against"
            ),
        )
    deps.faults.arm(scenario.case_id)


async def reset_world(deps: ApiDeps) -> dict[str, object]:
    """Clear one world's cases and reseed its clinic.

    One function rather than two call sites, because ``POST /api/demo/reset`` and the reset a
    scenario performs are the same act and a scenario that reset *slightly* differently is a demo
    with a second code path in it.

    **Refused while any run is in flight.** Clearing the store underneath a live graph is a version
    conflict wearing a costume, and the run would keep going against a store nobody is reading.
    Waiting a second and pressing again is the whole remedy.

    **Quiesced rather than merely checked, and the ordering is the whole of it.** A pending
    reconciliation wake is not a run in flight, so the check below would not have seen one — and a
    wake that fired a moment later would start a run against a store this function had just emptied.
    :meth:`careloop.api.wake.ReconcileWaker.quiesced` closes the registration boundary, cancels
    every pending wake and *waits until none is still running* before the body begins; the check
    then happens inside that scope, so it also catches a run some wake registered just before the
    gate closed. Cancelling without awaiting would establish nothing: a cancellation is delivered at
    the task's next suspension point, and a wake that is mid-registration has none.

    A run that won that race keeps the existing semantics exactly — 409, nothing cleared, the world
    left as it was — and the gate reopens on the way out, so a refused reset leaves a working world
    rather than a silenced one.
    """
    async with deps.wakes.quiesced():
        return _reset_quiesced_world(deps)


def _reset_quiesced_world(deps: ApiDeps) -> dict[str, object]:
    """The reset itself, performed with no wake able to appear behind it.

    Split out so the ordering above is a structure rather than a comment: everything here runs
    inside the quiesced scope, and there is no way to call it from outside one by accident that does
    not also mean writing this name.

    **Only this function can say whether the teardown went through**, which is why discarding the
    withdrawn wake intents happens here rather than in the scope. A reset that clears the store has
    destroyed the cases those intents named, so keeping them would schedule runs for cases that no
    longer exist; a reset that is *refused* has destroyed nothing, and its intents are restored when
    the gate reopens — which is what stops a momentary refusal costing a still-waiting case its
    scheduled reconciliation. The scope cannot tell the two apart and does not try.
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
    # The cases those wakes were about are gone, so their intents go with them. After this line and
    # not before it: everything above can still refuse, and a refusal must leave the scheduling it
    # found exactly as it was.
    deps.wakes.discard_deferred()
    # And so does any armed loss, for the same reason and in the same direction: it names a case
    # that no longer exists, and leaving it would have the next scenario's first booking lose an
    # answer nobody asked to lose. The second of the two disarms — the transport consumes its own
    # arming when it fires — so a scenario that ran to completion and one that was abandoned
    # halfway both leave this world in the same state.
    if deps.faults is not None:
        deps.faults.disarm()
    # `put_back`, not `reset`: a session-scoped clinic has its world *ended*, which is the same
    # observable outcome — the next booking gets a freshly seeded world — and is also what keeps
    # this registry and Demo World's agreeing about who is still here. Which of the two happens is
    # settled by how the client was built, so there is nothing for this route to decide.
    #
    # What comes back is reported rather than dropped: it is the one place the coupling between the
    # two registries is observable from outside the process, and `scripts/verify_deploy.py` is what
    # has to observe it. A Demo World deployed without the lifecycle verb, or a CareLoop that
    # stopped calling it, is a silent return to two owners — and saying what happened here is
    # cheaper and narrower than a route for asking about it.
    ended = deps.clinic.put_back() if deps.clinic is not None else None
    # And the object store's half of the same act. It is a *delete* rather than a reseed because
    # there is nothing to reseed: an object store holds only what this world put there, and what
    # makes it safe is that the client cannot address another visitor's namespace.
    #
    # **Load-bearing for the demo rather than housekeeping.** The frozen idempotency key is derived
    # from the case id, the action type and the identifying arguments — every one of which a demo
    # scenario fixes — so a second press of the same button addresses the *same* object. Left in
    # place, the create-once precondition would refuse that write and the visitor would watch a
    # reconciliation instead of a filing. The measured contract is what makes this necessary; see
    # docs/objectstore-contract.md.
    filed = deps.objects.remove_session_objects() if deps.objects is not None else None
    return {
        "cases_cleared": removed,
        "clinic_reset": deps.clinic is not None,
        # `null` on a single-world CareLoop, whose clinic is the default world and has no lifetime.
        "clinic_world_ended": ended,
        # `null` on a CareLoop with no object store, which is every simulated one.
        "objects_removed": filed,
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
    access: CaseAccess,
    who: str,
    wait: float,
) -> RunView:
    """Record a caregiver decision and carry the case forward.

    The in-flight check comes first so the request is all-or-nothing. Recording the approval and
    then failing to schedule would leave a decision made, a case released, and a caller told the
    request was refused — three things that disagree.
    """
    deps = access.deps
    case = access.case(case_id)
    # Before anything is recorded, and before a run is scheduled. Approving releases one
    # exact call into the executor, so this is a caller asking for a **new** external
    # effect and it is the moment the grant has to still hold.
    access.may_act_on(case)
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


async def deliver(
    deps: ApiDeps, case_id: str, event_id: str, *, caregiver: Caregiver, wait: float
) -> RunView:
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
        open_case(deps.store, inbound, case_id=case_id, caregiver=caregiver)
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

    Through :meth:`~careloop.api.deps.ApiDeps.start_run` rather than the registry directly, because
    that is where a finished run's consequences are decided — a case left waiting on an external
    system is given another reconciliation opportunity there, and a route that started a run its own
    way would be a run whose ending nobody was watching.
    """
    try:
        record = deps.start_run(case_id, inbound)
    except RunAlreadyInFlight as clash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(clash)) from None

    return run_view(await deps.runs.wait(record.run_id, wait))


__all__ = [
    "API_PREFIX",
    "arm_the_lost_booking_response",
    "build_dependencies",
    "create_app",
    "decide",
    "declare_required_headers",
    "deliver",
    "load_case",
    "load_event",
    "mount_demo",
    "reset_world",
    "resume_event",
    "schedule",
]
