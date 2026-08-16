"""Demo World's HTTP surface. Four routes, one domain, one control-plane verb.

Three of them are the clinic: list slots, book one, read one back. The fourth is ``/demo/reset``,
which is not part of the domain and is namespaced away from it so the control plane stays legible.
It exists because CareLoop's own demo reset has to be able to put the world back, and doing that
over HTTP is what keeps Demo World a genuinely separate process rather than an object CareLoop
reaches into.

The routes translate :mod:`demoworld.clinic`'s vocabulary into status codes and decide nothing
else. A missing ``Idempotency-Key`` is a **400**, for the same reason a missing ``X-Caregiver`` is
in CareLoop: a required field is absent, and saying anything more would claim a check that is not
happening.

**Every route is scoped to a world, and the scope is optional.** ``X-Demo-Session`` names one; so
does ``?session=``, because a person checking the clinic in a browser cannot set a header and the
whole point of ``GET /clinic/slots`` is that somebody can look for themselves. The header wins where
both are given, and a caller who names neither reaches the default world — which is what a developer
running this by hand has always reached. The id is a namespace and not a credential: Demo World
mints none, verifies none, and will keep a world under whatever string it is handed.

**Nothing here is cacheable, and that is load-bearing rather than tidy.** CareLoop's verification
performs a *separate* ``GET /clinic/appointments/{id}`` and compares it against what it executed,
which is the only thing in the system that observes an external effect independently. Behind a CDN
with no ``Cache-Control``, that read can be answered out of the CDN's memory — so the read-back
would confirm the response to CareLoop's own earlier request, report ``read_back: true``, and show
green while the clinic held something else entirely. Session F recorded the hazard against the
deployed distribution; this is the origin closing it, so a cache policy anywhere in front no longer
gets a vote.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from demoworld.clinic import (
    Appointment,
    BookingRequest,
    Clinic,
    Clinics,
    KeyReused,
    Slot,
    SlotAlreadyTaken,
    UnknownAppointment,
    UnknownSlot,
)

CLINIC_PREFIX = "/clinic"

#: The header CareLoop's idempotency key arrives in.
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: The header naming which world a caller is booking against.
SESSION_HEADER = "X-Demo-Session"

#: The same thing as a query parameter, for a browser that cannot send a header.
SESSION_PARAM = "session"


class SlotList(BaseModel):
    """What ``GET /clinic/slots`` answers with."""

    slots: list[Slot]


class ResetResult(BaseModel):
    """What ``POST /demo/reset`` answers with: the world it restored."""

    slots: list[Slot]


def idempotency_key(idempotency_key: Annotated[str | None, Header()] = None) -> str:
    """The key a booking is recorded under.

    Required, and required *here* rather than optional with a fallback: a booking with no key is a
    booking that cannot be replayed safely, and inventing one server-side would make every retry a
    second appointment.
    """
    if not (idempotency_key or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"the {IDEMPOTENCY_HEADER} header is required: a booking is recorded under the "
                f"key that identifies it, and a retry with no key would book a second appointment"
            ),
        )
    return idempotency_key.strip()


def demo_session(
    x_demo_session: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Query()] = None,
) -> str | None:
    """Which world this request is about, or ``None`` for the default one.

    The header first, then the query parameter. Two ways in rather than one because the two callers
    are different: CareLoop sends a header on every clinic call, and a judge checking the booking
    for themselves has a browser address bar and nothing else. One resolution, read in one place, so
    the routes never have to know there were two.
    """
    return (x_demo_session or session or "").strip() or None


def world_for(request: Request, session: Annotated[str | None, Depends(demo_session)]) -> Clinic:
    """The clinic this request is about.

    Reached through the app's registry rather than through a closure, so the dependency can live at
    module scope where FastAPI can resolve its annotation — and so there is one function, named, in
    which "which world" is decided.
    """
    worlds: Clinics = request.app.state.clinics
    return worlds.world(session)


#: What every route asks for instead of a clinic.
World = Annotated[Clinic, Depends(world_for)]


def create_clinic_app(clinic: Clinic | None = None) -> FastAPI:
    """Build the service over a clinic.

    The clinic is injected for the same reason CareLoop's store is: a service that constructs its
    own state can only be tested against the state it constructs, and a test that wants to move an
    appointment out from under a caller needs to be holding the same object the routes are. What is
    passed here becomes the **default** world — the one a caller who names no session reaches — and
    session worlds are created beside it on first use.
    """
    worlds = Clinics(default=clinic)

    app = FastAPI(
        title="Demo World — Northgate Cardiology",
        summary="A synthetic outpatient booking service. Not part of CareLoop.",
        version="0.1.0",
    )
    app.state.clinics = worlds
    app.state.clinic = worlds.default

    @app.get(f"{CLINIC_PREFIX}/slots", response_model=SlotList)
    def list_slots(world: World) -> SlotList:
        """Every slot nobody has booked."""
        return SlotList(slots=list(world.available()))

    @app.post(
        f"{CLINIC_PREFIX}/appointments",
        response_model=Appointment,
        status_code=status.HTTP_201_CREATED,
    )
    def book_appointment(
        request: BookingRequest,
        response: Response,
        world: World,
        key: Annotated[str, Depends(idempotency_key)],
    ) -> Appointment:
        """Book a slot, or replay the booking already made under this key.

        **201 for a booking, 200 for a replay.** The distinction is in the status rather than in
        the body, so a caller can tell what happened without the record having to carry a field
        about its own history.
        """
        try:
            appointment, created = world.book(request, key)
        except KeyReused as clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(clash)) from None
        except UnknownSlot as missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)
            ) from None
        except SlotAlreadyTaken as taken:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(taken)) from None

        if not created:
            response.status_code = status.HTTP_200_OK
        return appointment

    @app.get(f"{CLINIC_PREFIX}/appointments/{{confirmation_id}}", response_model=Appointment)
    def read_appointment(confirmation_id: str, world: World) -> Appointment:
        """One booking as the clinic holds it *now*.

        Deliberately a separate route from the booking one, and deliberately not a cached echo of
        it. This is what an independent read-back reads, so it has to be able to disagree with what
        the booking returned.
        """
        try:
            return world.appointment(confirmation_id)
        except UnknownAppointment as missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)
            ) from None

    @app.post("/demo/reset", response_model=ResetResult)
    def reset(world: World) -> ResetResult:
        """Put this caller's world back the way it started.

        Scoped like every other route, and that scoping is the whole reason one visitor's reset
        cannot empty another visitor's clinic: what is reset is the object the session resolved to,
        so reaching further is not something this route declines to do — it is something it has no
        way to express.
        """
        world.reset()
        return ResetResult(slots=list(world.available()))

    @app.middleware("http")
    async def never_cache_the_clinic(request: Request, call_next: Callable) -> Response:
        """Mark every response uncacheable, at the origin. See the module docstring.

        Applied to the whole service rather than to the read-back route alone. The independent
        ``GET`` is the one that must never be answered from a cache, and a stale slot list or a
        stale reset would each be their own quiet lie about a world somebody is being shown.
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


__all__ = [
    "CLINIC_PREFIX",
    "IDEMPOTENCY_HEADER",
    "SESSION_HEADER",
    "SESSION_PARAM",
    "ResetResult",
    "SlotList",
    "create_clinic_app",
    "demo_session",
    "idempotency_key",
]
