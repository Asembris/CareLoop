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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Final

from fastapi import Depends, Header, HTTPException, Request, status

from careloop.api.runs import RunAlreadyInFlight, RunRegistry
from careloop.api.wake import ReconcileWaker
from careloop.events.inbound import InboundEvent
from careloop.events.simulator import EventSimulator
from careloop.graph.context import RunContext, latest_delivery
from careloop.graph.runner import RunResult, context_for, run_case
from careloop.graph.witness import NO_WITNESS, Witness
from careloop.state.case import Case, CaseState
from careloop.state.store import CaseStore
from careloop.tools.clinic import ClinicClient

logger = logging.getLogger(__name__)

#: Who is recording a decision. An audit identity, never a credential — see the module docstring.
#:
#: Named here rather than spelled inline so the message below, the schema declaration in
#: :func:`careloop.api.app.declare_required_headers` and the tests all read one spelling.
CAREGIVER_HEADER: Final[str] = "X-Caregiver"

#: Builds the run context one graph run is driven through.
#:
#: The default uses whatever :mod:`careloop.llm.providers` resolves from the environment, which is
#: the mock unless something says otherwise. A test injects one carrying scripted agents; the demo
#: could inject one carrying Nova. Nothing about the routes changes either way.
ContextFactory = Callable[[CaseStore, InboundEvent, str], RunContext]


def default_context_factory(store: CaseStore, inbound: InboundEvent, case_id: str) -> RunContext:
    return context_for(store, inbound, case_id=case_id)


def simulated_context_factory(witness: Witness = NO_WITNESS) -> ContextFactory:
    """:func:`default_context_factory`, with somewhere for a run to record what it did.

    Exists so the no-clinic path can be given a witness at all. Behaviourally identical to
    :func:`default_context_factory` when it is not, which ``tests/test_witness_span.py`` asserts
    rather than leaves implied — two ways of building the same thing is one more than necessary,
    and the second must not quietly become different.
    """

    def build(store: CaseStore, inbound: InboundEvent, case_id: str) -> RunContext:
        return context_for(store, inbound, case_id=case_id, witness=witness)

    return build


def clinic_context_factory(clinic: ClinicClient, witness: Witness = NO_WITNESS) -> ContextFactory:
    """A factory binding every run to a real external clinic.

    One collaborator turns on both the executor whose bookings are performed over HTTP and the
    read-back verification observes them with — see
    :class:`careloop.graph.context.RunContext`, which refuses to be given one without the other.

    ``witness`` is where a run records a bounded statement of what it did, and it is a **sink**:
    the routes, the graph and the policy layer are identical whichever one arrives, and the default
    records nothing. It is threaded here rather than through ``create_app`` because a witness is
    something a run is bound to, exactly like the clinic and the agents — and the API layer, which
    decides nothing, should not grow a parameter for it.
    """

    def build(store: CaseStore, inbound: InboundEvent, case_id: str) -> RunContext:
        return context_for(store, inbound, case_id=case_id, clinic=clinic, witness=witness)

    return build


#: How one graph run is performed, given the store, the event and the case it belongs to.
#:
#: One level out from :data:`ContextFactory`, and the two answer different questions. A context
#: factory says *what a run is bound to* — which agents, which clinic, which clock — and still
#: assumes the graph executes in this process. A ``GraphRunner`` says *where the run happens at
#: all*, so an implementation is free to perform it somewhere else entirely and hand back the
#: :class:`~careloop.graph.runner.RunResult` the caller expects.
#:
#: **The seam exists so that claim is falsifiable rather than asserted.** Session F evaluated moving
#: graph execution onto Bedrock AgentCore Runtime and declined it on measurement — see
#: `docs/adr/0002-graph-execution-stays-on-the-host.md`. An ADR saying "we could move it" is worth
#: nothing if moving it would mean rewriting the API layer; with this in place, a remote runner is
#: a new implementation of one type and the routes do not change. Nothing about the authority layer,
#: the trust model or the state machine is on this seam, and nothing about them should ever be.
GraphRunner = Callable[[CaseStore, InboundEvent, str], Awaitable[RunResult]]


def local_graph_runner(context_factory: ContextFactory) -> GraphRunner:
    """Run the graph in this process, which is what CareLoop ships.

    The composition of :data:`ContextFactory` and
    :func:`careloop.graph.runner.run_case`, and deliberately nothing more — a runner that did
    anything else would be the API layer deciding something.
    """

    async def run(store: CaseStore, inbound: InboundEvent, case_id: str) -> RunResult:
        return await run_case(context_factory(store, inbound, case_id))

    return run


@dataclass
class ApiDeps:
    """The collaborators the routes are bound to.

    Holds no case and no run state. Anything durable is in the store; anything about a run in
    flight is in the run registry, which is execution context rather than a business fact.
    """

    store: CaseStore
    events: EventSimulator = field(default_factory=EventSimulator)
    context_factory: ContextFactory = default_context_factory
    runs: RunRegistry = field(default_factory=RunRegistry)

    wakes: ReconcileWaker = field(default_factory=ReconcileWaker)
    """Reconciliation wakes pending in this world. Execution context, exactly as :attr:`runs` is.

    **Nothing arms one yet.** The object is here so the lifecycles that tear a world down — the demo
    reset, idle eviction and shutdown — already close the boundary a wake would have to cross, and
    so that closing it is reviewable on its own rather than arriving underneath the scheduling
    policy that will use it. See :mod:`careloop.api.wake`.

    Per world for the reason :attr:`runs` is: a reset reaches the wakes belonging to the store it is
    about to clear and has no way to express reaching another visitor's.
    """

    graph_runner: GraphRunner | None = None
    """How a run is performed. ``None`` builds the local one over :attr:`context_factory`.

    Filled in :meth:`__post_init__` and never ``None`` afterwards, the same shape
    :class:`~careloop.graph.context.RunContext` uses for its executor. Supplying one *replaces*
    local execution rather than layering on it, so a runner given here is the only thing that runs
    the graph — which is why :attr:`context_factory` is meaningless alongside a remote one and is
    not consulted by it.
    """

    clinic: ClinicClient | None = None
    """The external clinic, if there is one. Held so ``POST /api/demo/reset`` can put it back.

    Runs reach it through :attr:`context_factory`, not through this field: a route that took a
    clinic out of here and used it would be the API doing something, and the API decides nothing.
    """

    demo: bool = False
    """Whether the demo control surface is mounted. Read at build time; no route consults it."""

    scenarios: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held while a scenario puts this world back and starts it again.

    Scheduling, not deciding — the same kind of thing the run registry does. Pressing a scenario is
    a reset *and* a delivery, and no client can make two requests atomic from outside; without this
    a visitor pressing twice quickly could have the second reset land between the first's reset and
    its delivery. Per world, so one visitor's press never blocks another's.
    """

    def __post_init__(self) -> None:
        if self.graph_runner is None:
            self.graph_runner = local_graph_runner(self.context_factory)

    async def drain(self) -> None:
        """Close this world down: stop its wakes, then wait for its runs to finish.

        Here so that shutdown asks the same question of one world and of a registry of them —
        :meth:`careloop.api.sessions.SessionWorlds.drain` is the same method over every world it
        holds, and the lifespan does not have to know which shape it got.

        **The order is the contract and it is not interchangeable.** ``aclose`` establishes the stop
        boundary before anything is awaited, so no wake can register a run while
        :meth:`careloop.api.runs.RunRegistry.drain` is already draining — which would be work
        started after the process decided to stop, on a store whose resources are about to be
        released. Draining first and stopping afterwards would leave exactly that window open, and
        it is wide: a drain waits for whole graph runs.

        A run a wake registered *before* the boundary is not cancelled and is not meant to be. It is
        an ordinary in-flight run, and the drain below is what waits for it.
        """
        await self.wakes.aclose()
        await self.runs.drain()

    def resume_unsettled(self) -> tuple[str, ...]:
        """Schedule a run for every case this world abandoned mid-effect. Returns their ids.

        **The recovery trigger, and the smallest one that works.** A process that died between
        dispatching an external effect and recording its result left the case ``EXECUTING`` with a
        durably prepared action; one whose reconciliation could not reach the provider left it
        ``WAITING_EXTERNAL``. Both now route to reconciliation — but only when something runs the
        graph, and until this existed nothing ever would: ``POST /api/events`` refuses anything that
        is not ``DONE``, ``approve`` and ``reject`` need an approval the execute node never raises,
        and a re-check needs ``AWAITING_HUMAN``. The case was correct, recoverable, and unreachable.

        **Anything in these two states at startup is orphaned by definition.** This process has no
        runs yet, so nothing here can be in flight; a case claiming to be mid-effect is claiming it
        about a process that is gone. That is what makes the sweep safe without a lease, a claim
        table or a heartbeat — and where a *second* process starts beside a live one, the case
        version's compare-and-set is what rejects the loser, which
        ``tests/test_saga_concurrency.py`` measures rather than assumes.

        **It schedules and decides nothing**, which is the whole of what this layer is allowed to
        do. Whether the action may be re-issued is
        :func:`careloop.graph.recovery.may_replay`'s answer, read inside the node from a declared
        provider contract; what is re-issued is the identity the case froze. Nothing here reads a
        tool name, an argument or a key.

        A case with no durable delivery is skipped rather than raising: it cannot be carried forward
        under any event, and a startup that failed because one row was odd would take the whole
        process down for a case nobody can run anyway.
        """
        scheduled: list[str] = []
        for state in (CaseState.EXECUTING, CaseState.WAITING_EXTERNAL):
            for case in self.store.list_by_state(state):
                delivery = latest_delivery(case)
                if delivery is None:
                    logger.warning(
                        "case %s is %s with no recorded delivery; it cannot be resumed",
                        case.case_id,
                        state,
                    )
                    continue
                try:
                    inbound = self.events.get(delivery.event_id)
                except KeyError:
                    logger.warning(
                        "case %s names event %s, which this process does not have",
                        case.case_id,
                        delivery.event_id,
                    )
                    continue
                case_id = case.case_id

                # Bound as defaults rather than captured, because the thunk is invoked by an
                # ``asyncio`` task that starts after this loop has moved on — late binding would
                # resume every case under the last one's event.
                def start(event: InboundEvent = inbound, target: str = case_id):
                    return self.graph_runner(self.store, event, target)

                try:
                    self.runs.start(case_id, start)
                except RunAlreadyInFlight:
                    # Not reachable from a startup sweep, which runs before anything is scheduled.
                    # Reachable if this is ever called on a live process, and skipping is right
                    # there too: something is already working on this case.
                    continue
                scheduled.append(case_id)
        return tuple(scheduled)


def deps(
    request: Request,
    x_careloop_session: Annotated[str | None, Header()] = None,
) -> ApiDeps:
    """The dependencies this request's routes receive.

    One app, two shapes. A single-world app holds an :class:`ApiDeps` and every request gets it —
    which is what local development and the offline suite use, and nothing about them changed. An
    isolated app holds a :class:`~careloop.api.sessions.SessionWorlds` instead and **no app-scoped
    dependencies at all**, so the only way to a store is through the session the request presented.
    That is the isolation: not a filter every query has to remember, but the absence of anything
    else to reach.

    A missing header is a **400** — the ``X-Caregiver`` reasoning, a required field is absent — and
    an unknown one a **410**, which is used nowhere else in this API so a client can tell "your
    world is gone, mint another" from the 409 that means a run is in flight.
    """
    # Imported here rather than at module scope: sessions.py builds on ApiDeps, so the dependency
    # runs one way between these two modules and this is the direction that would close the loop.
    from careloop.api.sessions import SESSION_HEADER, SessionWorlds, UnknownSession

    held = request.app.state.careloop
    if not isinstance(held, SessionWorlds):
        return held

    session_id = (x_careloop_session or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"the {SESSION_HEADER} header is required: this demo gives every visitor their own "
                f"world so two people can use it at once, and a request that names none belongs to "
                f"nobody. POST /api/sessions to be given one."
            ),
        )

    try:
        return held.world(session_id)
    except UnknownSession:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "that world is gone — it was never minted here, or it has been idle long enough "
                "to be forgotten. POST /api/sessions for a new one and start again."
            ),
        ) from None


Deps = Annotated[ApiDeps, Depends(deps)]


def caregiver(x_caregiver: Annotated[str | None, Header()] = None) -> str:
    """Who is recording a decision, for the audit trail.

    Not authentication — see the module docstring for why this is a 400 rather than a 401.
    """
    if not (x_caregiver or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"the {CAREGIVER_HEADER} header is required: a decision is recorded against "
                f"whoever made it, and the audit trail has nowhere to put an anonymous one"
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
    "CAREGIVER_HEADER",
    "ApiDeps",
    "Caregiver",
    "ContextFactory",
    "Deps",
    "GraphRunner",
    "caregiver",
    "clinic_context_factory",
    "default_context_factory",
    "deps",
    "local_graph_runner",
    "sections_from",
    "simulated_context_factory",
]
