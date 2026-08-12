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
Demo World, which derives it from the key; the four fields of :class:`AppointmentBooked` are
mapped by name, so anything else the service might grow — a ``booked_at``, a queue position —
never reaches a result. Invariant 9 survives contact with a real service because the boundary is
narrow, not because the service is well-behaved.

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

#: The header the idempotency key travels in.
IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"


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
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    @property
    def base_url(self) -> str:
        """Where this client reaches the clinic."""
        return self._base_url

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

        Raises:
            ClinicUnavailable: if the clinic could not be reached.
            ClinicRefused: if the clinic does not hold this confirmation.
        """
        return _appointment(self._request("GET", f"/clinic/appointments/{confirmation_id}"))

    def reset(self) -> None:
        """Put the clinic back to its seeded world. Demo control only."""
        self._request("POST", "/demo/reset")

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
                f"{_detail(response)}"
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

    Fields the service might grow are dropped here rather than carried, which is what keeps
    invariant 9's "no timestamp on a result" true against a system CareLoop does not control.
    """
    body = response.json()
    return AppointmentBooked(
        confirmation_id=body["confirmation_id"],
        provider_id=body["provider_id"],
        patient_id=body["patient_id"],
        slot_start=body["slot_start"],
    )


def clinic_handlers(client: ClinicClient) -> dict[str, Handler]:
    """The shipped handlers with ``book_appointment`` routed to a real clinic."""
    return {**DEFAULT_HANDLERS, "book_appointment": client.book}


def clinic_executor(client: ClinicClient) -> MockExternalExecutor:
    """An executor whose bookings are real and whose other three effects are not."""
    return MockExternalExecutor(handlers=clinic_handlers(client))


__all__ = [
    "DEFAULT_TIMEOUT",
    "IDEMPOTENCY_HEADER",
    "ClinicClient",
    "ClinicRefused",
    "ClinicUnavailable",
    "clinic_executor",
    "clinic_handlers",
]
