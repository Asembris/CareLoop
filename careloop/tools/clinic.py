"""The one tool that reaches a real external system.

``book_appointment`` routes over HTTP to Demo World, a separate process holding synthetic clinic
state. Everything else stays on the mock handlers, deliberately: one action made genuinely external
is the proof, and four would be scope pretending to be rigour.

**Why bother.** Verification without this is closed-loop — CareLoop's executor checked against
CareLoop's own approval. Honest, documented, and still a tautology in the one place it matters. An
external service whose state actually changes, read back through a *separate* request, is what
turns "the mock reported success" into "the external system changed and CareLoop confirmed it".

**A handler, not a second executor.** :data:`careloop.tools.executor.Handler` is already
``(canonical args, idempotency key) -> ActionResult``, which is exactly what a booking needs, so
:meth:`ClinicClient.book` drops into the handler mapping
:class:`~careloop.tools.executor.MockExternalExecutor` already accepts. The ledger, the replay
suppression and the key-collision guard keep working unchanged, and invariant 7 is untouched —
the key is derived by ``ToolSet._effect`` before a handler ever sees it.

**Two idempotency layers, not one, and they are not redundant.** CareLoop suppresses a replay from
the case's own ``ActionRecord`` before dispatching, and Demo World honours the same key
server-side. The first is what stops CareLoop doing the work twice; the second is what stops a
retry it did not know about — a connection that dropped after the booking landed — becoming a
second appointment. The second layer is the one a real integration cannot do without.

**No clock, no randomness, and nothing that could smuggle one in.** ``confirmation_id`` comes from
Demo World, which derives it from the key; the fields of :class:`AppointmentBooked` are mapped by
name, so anything else the service might grow — a ``booked_at``, a queue position — never reaches a
result. Invariant 9 survives contact with a real service because the boundary is narrow, not
because the service is well-behaved. Narrow means *declared booking arguments only*: every argument
CareLoop sends is carried back so verification can compare it, and nothing else is.

**Transport is ``httpx``**, declared as a dependency rather than borrowed from
``strands-agents``'s own tree. Session 4's lesson was that an undeclared dependency you rely on is
a dependency you find out about on CI.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx

from careloop.tools.executor import DEFAULT_HANDLERS, Handler, MockExternalExecutor
from careloop.tools.results import AppointmentBooked

#: How long any single clinic call may take. Small, and not configurable per call: a graph node
#: dispatches this synchronously, so a slow clinic is a stalled run, and the SDK's node timeout is
#: the thing that should be deciding a run has taken too long.
DEFAULT_TIMEOUT: Final[float] = 10.0

#: How long the call that ends a world may take.
#:
#: Shorter than :data:`DEFAULT_TIMEOUT` because the caller is different in kind: a booking is the
#: work a run exists to do, and ending a world is housekeeping performed on the way through
#: ``POST /api/sessions``. A clinic that is not answering must cost an arriving visitor two seconds,
#: not ten — and the divergence it leaves behind is queued and retried rather than waited out.
END_SESSION_TIMEOUT: Final[float] = 2.0

#: The header the idempotency key travels in.
IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"

#: The header naming which of Demo World's worlds this client reaches.
#:
#: Declared here rather than imported: ``careloop`` may not depend on ``demoworld``, which is the
#: whole basis of the read-back being independent. ``tests/test_session_isolation.py`` asserts the
#: two spellings agree, so this is a drift check rather than a shared constant — the same
#: arrangement as the seeded slot the referral names.
#:
#: It lives beside the client rather than in ``careloop.api.sessions`` because it is a fact about
#: the clinic's protocol: this client is the thing that speaks it, and the one method that ends a
#: world has to know whether it is scoped to one.
CLINIC_SESSION_HEADER: Final[str] = "X-Demo-Session"


class ClinicUnavailable(Exception):
    """Raised when the clinic could not be reached at all.

    Distinct from :class:`ClinicRefused` because the two mean different things about the world: a
    refusal is the clinic answering, and this is nobody answering. Neither is recoverable inside a
    run — a node raising fails the case loudly, which is the right outcome for an integration that
    is down.
    """


class ClinicRefused(Exception):
    """Raised when the clinic answered and declined.

    An unknown slot, a slot somebody else holds, or a key already recorded against a different
    booking. The message carries the clinic's own detail because it is a diagnostic and never
    reaches a model: a node raising is turned into a ``FAILED`` case carrying the exception's
    *type* name, and the text stops there.

    ``status_code`` is carried so that a *caller* can tell what kind of refusal it received without
    parsing the message. Nothing above :mod:`careloop.tools.clinic` reads it — the graph layer sees
    observation vocabulary, not HTTP — and :meth:`ClinicClient.appointment` is the one place it is
    consulted, to decide whether a refusal is the clinic saying it holds no such appointment.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class AppointmentNotFound(ClinicRefused):
    """Raised when the clinic answered that it holds no such confirmation.

    **A definite answer about an object the clinic itself created**, and that is why it is its own
    type. The booking returned a ``confirmation_id`` the clinic minted and stored; a later read of
    that exact id answering ``404`` is the clinic contradicting the receipt CareLoop holds, not a
    failure to obtain an answer. The two are different facts about the world and
    :mod:`careloop.graph.verify` records them differently.

    **Scoped to the read route, deliberately.** ``POST /clinic/appointments`` also answers ``404``,
    for a slot this clinic does not offer — a refusal to book a thing that never existed, which has
    nothing to do with a booking that has vanished. Classifying by status alone in ``_request``
    would fold the two together, so the decision lives in :meth:`ClinicClient.appointment` and
    ``tests/test_clinic_client.py::TestOnlyTheReadRouteMeansTheAppointmentIsAbsent`` is what fails
    if it is ever generalised.

    A subclass rather than a sibling: it *is* the clinic refusing, so everything already catching
    :class:`ClinicRefused` keeps working unchanged.
    """


class ClinicClient:
    """A thin, synchronous client for one Demo World instance.

    Synchronous because :meth:`careloop.tools.callables.ToolSet.call` is, and a tool that has to be
    awaited would change every call site to make one integration convenient.
    """

    def __init__(
        self,
        base_url: str,
        *,
        session: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        if session is not None:
            self._client.headers[CLINIC_SESSION_HEADER] = session
        # Read back off the transport rather than kept only from the argument: a caller that built
        # the transport itself and stamped the header on it has said which world this client
        # reaches just as clearly, and two places to look would be two answers to disagree about.
        self._session: Final[str | None] = self._client.headers.get(CLINIC_SESSION_HEADER)

    @property
    def base_url(self) -> str:
        """Where this client reaches the clinic."""
        return self._base_url

    @property
    def session(self) -> str | None:
        """Which of Demo World's worlds this client reaches, or ``None`` for the default one.

        A namespace and not a credential: Demo World mints none and verifies none. What it decides
        here is whether this client has a world with a *lifetime* — see :meth:`put_back`.
        """
        return self._session

    def book(self, args: Mapping[str, str], idempotency_key: str) -> AppointmentBooked:
        """Book an appointment. :data:`~careloop.tools.executor.Handler`-shaped.

        Raises:
            ClinicUnavailable: if the clinic could not be reached.
            ClinicRefused: if the clinic answered and declined.
        """
        response = self._request(
            "POST",
            "/clinic/appointments",
            json={
                "provider_id": args["provider_id"],
                "patient_id": args["patient_id"],
                "slot_start": args["slot_start"],
                "reason": args["reason"],
            },
            headers={IDEMPOTENCY_HEADER: idempotency_key},
        )
        return _appointment(response)

    def appointment(self, confirmation_id: str) -> AppointmentBooked:
        """Read one booking back, as the clinic holds it *now*.

        **A separate request, and that is the whole point.** Verification calls this rather than
        re-reading what :meth:`book` returned, so what it compares against the authorised arguments
        is observed external state rather than CareLoop's own receipt.

        A ``404`` here is turned into :class:`AppointmentNotFound` — the clinic answering that it
        holds no such confirmation, which is evidence rather than an absence of it. That is a
        statement this route can make and the booking route cannot, so the distinction is drawn
        here and not in :meth:`_request`.

        Raises:
            ClinicUnavailable: if the clinic could not be reached.
            AppointmentNotFound: if the clinic answered that it holds no such confirmation.
            ClinicRefused: if the clinic answered and declined for any other reason.
        """
        try:
            response = self._request("GET", f"/clinic/appointments/{confirmation_id}")
        except ClinicRefused as refusal:
            if refusal.status_code == httpx.codes.NOT_FOUND:
                raise AppointmentNotFound(
                    str(refusal), status_code=refusal.status_code
                ) from refusal
            raise
        return _appointment(response)

    def reset(self) -> None:
        """Reseed the world this client reaches. Every booking forgotten, every slot free again.

        Demo control only, and one of the two primitives :meth:`put_back` chooses between.
        """
        self._request("POST", "/demo/reset")

    def end_session(self) -> bool:
        """End this client's world. Returns whether Demo World was still holding one.

        **The lifecycle verb, and CareLoop is the only caller.** Demo World removes a world only
        when it is told to: it does not know who is still looking at the demo and must not guess.
        Until session V it did guess — it evicted least-recently-used, where "used" meant *the
        clinic* was touched, while CareLoop expired a world by idle time refreshed on every API
        request. A visitor polling the dashboard was alive on one side and the eviction victim on
        the other, and the appointment CareLoop had verified for them vanished.

        Idempotent by contract: ending a world twice is the same successful outcome as ending it
        once, because a caller repairing a divergence must not be told it failed to.

        Raises:
            ValueError: if this client reaches the default world, which has no lifetime to end.
            ClinicUnavailable: if the clinic could not be reached.
            ClinicRefused: if the clinic answered and declined.
        """
        if self._session is None:
            raise ValueError(
                "this client reaches the clinic's default world, which has no lifetime to end; "
                "only a session-scoped client may end the world it is scoped to"
            )
        response = self._request("DELETE", "/demo/session", timeout=END_SESSION_TIMEOUT)
        return bool(response.json()["existed"])

    def put_back(self) -> None:
        """Put the world this client reaches back the way it started.

        A **session-scoped** client ends its world. That is a reset — the next booking gets a
        freshly seeded one — and it is also the thing that keeps CareLoop's registry and Demo
        World's agreeing about who is still here, so a visitor's clinic is only ever removed by the
        component that knows whether they have gone.

        A client with **no** session reseeds the default world instead, which is what a single-world
        CareLoop reaches and which has no lifetime to end.

        The branch lives here rather than at the call site because which world a client reaches is
        already settled by how it was built; the API layer asking about it would be the API layer
        deciding something.
        """
        if self._session is None:
            self.reset()
        else:
            self.end_session()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.HTTPError as unreachable:
            raise ClinicUnavailable(
                f"the clinic at {self._base_url} could not be reached: {type(unreachable).__name__}"
            ) from unreachable

        if response.status_code >= 400:
            raise ClinicRefused(
                f"the clinic refused {method} {path} with {response.status_code}: "
                f"{_detail(response)}",
                status_code=response.status_code,
            )
        return response


def _detail(response: httpx.Response) -> str:
    """The clinic's own explanation, if it gave one in the shape FastAPI uses."""
    try:
        body = response.json()
    except ValueError:
        return response.text
    return str(body.get("detail", body)) if isinstance(body, dict) else str(body)


def _appointment(response: httpx.Response) -> AppointmentBooked:
    """Map a clinic record onto the typed result, by name and by name only.

    **Declared booking arguments are carried; fields the service grows are dropped.** That is the
    distinction, and it is narrower than "keep exactly N fields". A ``booked_at`` or a queue
    position is something CareLoop never sent and has nothing to compare against, so carrying it
    would put a clock on a result for no benefit — which is what keeps invariant 9's "no timestamp
    on a result" true against a system CareLoop does not control. ``reason`` is the opposite:
    CareLoop sends it, the clinic persists it, and this same mapper is what the independent
    read-back returns, so dropping it meant a reason-only divergence could reach ``DONE`` reported
    as verified. It was dropped until this session; see
    ``tests/test_verify_readback.py::TestTheExecutedReasonIsCheckedOnTheWayOutToo``.

    Every field is subscripted rather than fetched with a default. A response missing one is a
    malformed observation and fails loudly, exactly as it always has for the other four.
    """
    body = response.json()
    return AppointmentBooked(
        confirmation_id=body["confirmation_id"],
        provider_id=body["provider_id"],
        patient_id=body["patient_id"],
        slot_start=body["slot_start"],
        reason=body["reason"],
    )


def clinic_handlers(client: ClinicClient) -> dict[str, Handler]:
    """The shipped handlers with ``book_appointment`` routed to a real clinic."""
    return {**DEFAULT_HANDLERS, "book_appointment": client.book}


def clinic_executor(client: ClinicClient) -> MockExternalExecutor:
    """An executor whose bookings are real and whose other three effects are not."""
    return MockExternalExecutor(handlers=clinic_handlers(client))


__all__ = [
    "CLINIC_SESSION_HEADER",
    "DEFAULT_TIMEOUT",
    "END_SESSION_TIMEOUT",
    "IDEMPOTENCY_HEADER",
    "AppointmentNotFound",
    "ClinicClient",
    "ClinicRefused",
    "ClinicUnavailable",
    "clinic_executor",
    "clinic_handlers",
]
