"""Losing one provider answer on purpose, so a judge can watch CareLoop find out what happened.

CareLoop's strongest reliability property is invisible in the product. A provider that commits an
external write whose answer never reaches the caller is the one situation the whole saga exists for
— the identity is frozen before anything leaves the process, the case waits rather than failing, a
wake arrives on its own, and the frozen identity is re-issued so the provider returns the
appointment it already made. Every piece of that ships and is proven. None of it was reachable from
a browser: nothing an HTTP client can press produces an ambiguous outcome, which is why
``e2e/tests/unreachable-states.spec.ts`` has to mock the post-effect states.

This module is the smallest thing that makes it reachable, and it is deliberately shaped so that it
can demonstrate the property without being able to fake it.

**The loss is injected below CareLoop, at the transport.** :class:`LostResponseTransport` wraps a
real :class:`httpx.BaseTransport`. Every line of :class:`careloop.tools.clinic.ClinicClient`, the
executor, the nodes, the policy engine and the verifier runs exactly as it does in production; what
changes is that one answer never gets back up the stack. ``ClinicClient._request`` maps the
resulting :class:`httpx.HTTPError` to ``ClinicUnavailable``, ``dispatch_prepared`` maps that to
``DispatchUnavailable``, and the ordinary saga takes it from there. Injecting the loss by
subclassing ``ClinicClient`` — the shape ``tests/test_saga_chaos.py::LosingClinic`` uses — would
have CareLoop's own object lie to itself; this leaves the object alone.

**It fires only after the provider says it created the appointment.** The inner transport performs
the request first, and the loss is injected only when the answer is ``201``. That is what makes
"the external write really landed" a structural property of this seam rather than something a test
hopes for: Demo World answers ``201`` for a creation and ``200`` for a replay of a known key, so a
``201`` *is* the provider stating it committed. A ``409``, a ``404``, a read, a reset and every
other route pass through untouched, so no ambiguity is ever manufactured over a write that did not
happen — and the independent read-back that later settles the case travels the same wrapper and is
never interfered with.

**The intercepted answer is disposed of, not abandoned.** A transport-returned
:class:`httpx.Response` owns a live connection until its stream is consumed or closed, and nothing
above this function will ever see it, so this function is the only thing that can release it. It
sets the request on the response — which the transport layer does not do and
:meth:`httpx.Response.close` requires — then reads and closes it. Dropping the reference instead
would leak a pooled connection per press.

**What it is not.** It knows nothing about cases, states, ``ActionRecord``, policy, the planner,
verification or settlement, and it must never learn: it is a transport that loses one answer. Which
*action* may lose an answer is decided one layer up, by :class:`BookingResponseLoss`, and which
*run* is handed the losing client is decided one layer above that, by the world's context factory.

**Honesty is part of the feature.** The demo card says plainly that CareLoop throws the clinic's
reply away. The ambiguity is genuine — nothing above the transport received an answer, so the
case's own trail is true when it says the external system did not answer — but a product that
implied a spontaneous network fault would be claiming something that did not happen, which is the
one thing this repository refuses everywhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

import httpx

from careloop.events.inbound import InboundEvent
from careloop.graph.context import RunContext
from careloop.graph.runner import context_for
from careloop.graph.witness import NO_WITNESS, Witness
from careloop.state.store import CaseStore
from careloop.tools.clinic import DEFAULT_TIMEOUT, ClinicClient

#: The path a booking is created at.
#:
#: Spelled here rather than imported, for the reason
#: :data:`careloop.tools.clinic.CLINIC_SESSION_HEADER` is spelled there rather than imported from
#: ``demoworld``: this is a fact about the clinic's protocol and the two packages may not depend on
#: each other. ``tests/test_faults.py`` drives a real :class:`~careloop.tools.clinic.ClinicClient`
#: through a recording transport and asserts this is the path a booking actually takes, so it is a
#: behavioural drift check rather than a string somebody has to remember to update.
BOOKING_CREATE_PATH: Final[str] = "/clinic/appointments"

#: The header the idempotency key travels in, as this wrapper reads it off the wire.
#:
#: Recorded when a loss fires so a test can assert that the identity whose answer was lost is the
#: identity later replayed. Observation only: nothing here decides anything from it, and the
#: wrapper would behave identically if the header were absent.
IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"


class LostResponseTransport(httpx.BaseTransport):
    """A real transport that discards the answer to one successful booking creation.

    Unarmed, it is a pure passthrough and cannot be told from the transport it wraps.

    Armed, it still performs every request for real. The single request it treats differently is a
    ``POST`` to :data:`BOOKING_CREATE_PATH` whose answer is ``201``: the provider has said it
    created the appointment, so the write has demonstrably landed, and only then is the answer
    released and a synthetic :class:`httpx.ReadError` raised in its place. The fault is consumed at
    the same moment, so the wrapper is a passthrough again from the next request onward — including
    the replay that settles the case and the independent read that verifies it.

    ``inner`` is owned: :meth:`close` closes it, so a client built over this wrapper releases its
    pool exactly as one built over a bare transport does.
    """

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport()
        self._armed = False
        self.losses = 0
        """How many answers this wrapper has discarded. One, at most, per arming."""

        self.lost_keys: list[str] = []
        """The idempotency key of each discarded answer, read off the request. Observation only."""

        self.requests: list[str] = []
        """``"METHOD /path"`` for every request that reached this wrapper, in order."""

    @property
    def armed(self) -> bool:
        """Whether the next successful booking creation will have its answer discarded."""
        return self._armed

    def arm(self) -> None:
        """Discard the answer to the next successful booking creation, and then stop."""
        self._armed = True

    def disarm(self) -> None:
        """Go back to being a plain transport. Safe to call when already disarmed."""
        self._armed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Perform ``request``, and discard its answer if this is the one loss.

        The inner transport goes first, always. Deciding before the request would mean injecting an
        ambiguous outcome over a write that never happened, which is the one thing this seam must
        not be able to do.
        """
        self.requests.append(f"{request.method} {request.url.path}")
        response = self._inner.handle_request(request)

        if not (self._armed and self._is_a_booking_creation(request)):
            return response
        if response.status_code != httpx.codes.CREATED:
            # The provider answered and did not create anything — a slot it does not offer, a slot
            # somebody else holds, or a replay of a key it already knows. There is no committed
            # write to be ambiguous about, so the answer goes back untouched and the case fails or
            # succeeds on its own merits.
            return response

        # From here the provider has committed. Everything below is disposal and bookkeeping; none
        # of it can fail in a way that would leave the caller believing the booking succeeded.
        self._release(response, request)
        self._armed = False
        self.losses += 1
        self.lost_keys.append(request.headers.get(IDEMPOTENCY_HEADER, ""))
        raise httpx.ReadError(
            "the clinic's answer was discarded before it reached CareLoop", request=request
        )

    def close(self) -> None:
        """Release the wrapped transport's pool."""
        self._inner.close()

    @staticmethod
    def _is_a_booking_creation(request: httpx.Request) -> bool:
        """Whether this request is the one route that creates an appointment.

        Reads and every other route are excluded here rather than by luck. The read-back that
        settles a recovered case travels this same wrapper, and a seam that could interfere with it
        would be removing the evidence the whole scenario exists to produce.
        """
        return request.method == "POST" and request.url.path == BOOKING_CREATE_PATH

    @staticmethod
    def _release(response: httpx.Response, request: httpx.Request) -> None:
        """Consume and close an answer nothing above this will ever see.

        A transport hands back a response whose body is still on the wire and whose connection is
        still checked out of the pool; ``httpx.Client`` is what normally reads and closes it, and
        it never will here because this function raises instead of returning. So this is the only
        place the connection can be released.

        ``response.request`` is set first because the transport layer does not set it and
        :meth:`httpx.Response.read` and :meth:`httpx.Response.close` both enter a request context
        that raises without one. ``read`` drains the body — which is what returns the connection to
        the pool — and ``close`` is the belt to its braces: idempotent, and correct even if a future
        httpx stops closing on read completion.
        """
        response.request = request
        response.read()
        response.close()


@dataclass
class BookingResponseLoss:
    """One visitor world's ability to lose the answer to one recovery scenario's booking.

    **The binding is a case id, and it is enforced by handing out a different client rather than by
    checking one.** :attr:`clinic` is a second :class:`~careloop.tools.clinic.ClinicClient` reaching
    the same world of the same clinic over the wrapped transport; :meth:`context_factory` hands it
    to runs for the armed case and hands every other run the ordinary client. So a booking belonging
    to any other case in this same visitor world travels a transport that contains no fault at all,
    and cannot consume this one whatever it does. That is a structural property of which object the
    run holds, not a rule somebody remembered to write.

    Two conditions therefore both have to hold before an answer is lost: the run must belong to the
    armed case, and the request must be a booking creation the provider answered ``201`` to. The
    first is this class's; the second is :class:`LostResponseTransport`'s.

    **Per world**, exactly as the run registry and the reconciliation waker are: it is reached
    through the :class:`~careloop.api.deps.ApiDeps` the presented session resolved to, so there is
    no way to express arming somebody else's.

    **Default off, one-shot, and cleared by a reset.** Nothing is armed until a recovery scenario
    arms it; the transport consumes the arming when it fires; and
    ``careloop.api.app._reset_quiesced_world`` disarms, so a reset during or after a scenario leaves
    nothing behind for the next one to trip over.
    """

    clinic: ClinicClient
    """The loss-injecting client. Handed only to runs for :attr:`armed_for`."""

    transport: LostResponseTransport
    """The wrapper inside :attr:`clinic`. Held so a test can read what actually happened."""

    _case_id: str | None = field(default=None, init=False)

    @property
    def armed_for(self) -> str | None:
        """The case whose booking may lose its answer, or ``None`` when nothing is armed."""
        return self._case_id

    def arm(self, case_id: str) -> None:
        """Let one booking made by ``case_id`` lose its answer.

        Replaces any previous arming rather than accumulating: a scenario resets the world
        immediately before arming, so the case the previous arming named no longer exists.
        """
        self._case_id = case_id
        self.transport.arm()

    def disarm(self) -> None:
        """Forget the arming entirely. Safe to call when nothing is armed."""
        self._case_id = None
        self.transport.disarm()

    def claims(self, case_id: str) -> bool:
        """Whether runs for ``case_id`` are the ones that may lose an answer.

        **Not consuming.** The reconciliation run for the same case is claimed too, so a recovered
        case's replay and its independent read-back both travel the wrapper — by then a passthrough,
        because the transport consumed the arming when it fired. Sending the second run down a
        different client would make the read that settles the case travel a path the first one did
        not, which is a difference with no reason to exist.
        """
        return self._case_id is not None and case_id == self._case_id

    def context_factory(
        self, ordinary: ClinicClient, witness: Witness = NO_WITNESS
    ) -> Callable[[CaseStore, InboundEvent, str], RunContext]:
        """A context factory that hands the losing client to the armed case and nothing else.

        This is where the binding is enforced. ``case_id`` is already a parameter of every context
        factory — see :data:`careloop.api.deps.ContextFactory`, which this signature is spelled out
        to match rather than imported from, since that module depends on this one — so no new
        identifier, header, parameter or piece of run-scoped state had to be invented to correlate
        the fault with the scenario that armed it.
        """

        def build(store: CaseStore, inbound: InboundEvent, case_id: str) -> RunContext:
            clinic = self.clinic if self.claims(case_id) else ordinary
            return context_for(store, inbound, case_id=case_id, clinic=clinic, witness=witness)

        return build

    def close(self) -> None:
        """Release the losing client's connection pool."""
        self.clinic.close()


def booking_response_loss(
    clinic_url: str, *, session: str | None = None, timeout: float = DEFAULT_TIMEOUT
) -> BookingResponseLoss:
    """The loss capability for one world, wired to the same clinic and the same world as its peer.

    A second :class:`httpx.Client` over its own pool rather than a wrapper around a shared one: the
    two clients have independent lifetimes and closing either must not reach into the other, and a
    pool that is never used costs nothing until it is.
    """
    transport = LostResponseTransport()
    client = ClinicClient(
        clinic_url,
        session=session,
        client=httpx.Client(timeout=timeout, transport=transport),
    )
    return BookingResponseLoss(clinic=client, transport=transport)


__all__ = [
    "BOOKING_CREATE_PATH",
    "IDEMPOTENCY_HEADER",
    "BookingResponseLoss",
    "LostResponseTransport",
    "booking_response_loss",
]
