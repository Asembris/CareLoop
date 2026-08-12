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
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel

from demoworld.clinic import (
    Appointment,
    BookingRequest,
    Clinic,
    KeyReused,
    Slot,
    SlotAlreadyTaken,
    UnknownAppointment,
    UnknownSlot,
)

CLINIC_PREFIX = "/clinic"

#: The header CareLoop's idempotency key arrives in.
IDEMPOTENCY_HEADER = "Idempotency-Key"


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


def create_clinic_app(clinic: Clinic | None = None) -> FastAPI:
    """Build the service over a clinic.

    The clinic is injected for the same reason CareLoop's store is: a service that constructs its
    own state can only be tested against the state it constructs, and a test that wants to move an
    appointment out from under a caller needs to be holding the same object the routes are.
    """
    world = clinic if clinic is not None else Clinic()

    app = FastAPI(
        title="Demo World — Northgate Cardiology",
        summary="A synthetic outpatient booking service. Not part of CareLoop.",
        version="0.1.0",
    )
    app.state.clinic = world

    @app.get(f"{CLINIC_PREFIX}/slots", response_model=SlotList)
    def list_slots() -> SlotList:
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
    def read_appointment(confirmation_id: str) -> Appointment:
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
    def reset() -> ResetResult:
        """Put the world back the way it started."""
        world.reset()
        return ResetResult(slots=list(world.available()))

    return app


__all__ = [
    "CLINIC_PREFIX",
    "IDEMPOTENCY_HEADER",
    "ResetResult",
    "SlotList",
    "create_clinic_app",
    "idempotency_key",
]
