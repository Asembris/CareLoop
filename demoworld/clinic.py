"""The clinic's world: slots, appointments, and the one rule that makes a booking real.

A synthetic outpatient booking system. Not part of CareLoop, and it never imports it — this is the
*outside world*, and a world that imported the agent could not be used to prove the agent reached
something outside itself. The direction is swept in ``tests/test_demoworld.py``.

**One domain only.** Appointments. Not calendars, not claims, not receipts. Demo World exists to
make exactly one CareLoop action genuinely external and independently observable; a second domain
would be a second product.

**Synthetic data.** One patient, one provider, three offered slots, all invented. No PHI, no real
providers, and nothing here reaches a network of its own.

**No clock and no randomness**, which is not required of this package the way invariant 9 requires
it of ``careloop/tools/`` but is worth more here than the realism it costs. ``confirmation_id``
derives from the idempotency key, so a reset and a re-run produce byte-identical records — which is
what lets CareLoop's replay assertion stay exact all the way through the real HTTP path instead of
stopping at the boundary, and what makes the demo do the same thing every time it is filmed. A
``booked_at`` would buy nothing and cost both.

**State lives for as long as the process does.** It persists across requests and across CareLoop's
executor being rebuilt, which is what the booking claim needs; it does not survive Demo World being
restarted. That is a stated limitation rather than a durability claim, and the reset route makes it
uninteresting — a judge who restarts the service gets the seeded world back, which is what the
button does anyway.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict


class Slot(BaseModel):
    """One bookable appointment slot.

    Identified by ``(provider_id, slot_start)`` rather than by a synthetic slot id. Those two are
    exactly the arguments CareLoop's ``book_appointment`` carries, so a booking names a slot in the
    vocabulary the caller already has — a separate id would be a third identifier for the same
    thing and something for the two sides to disagree about.
    """

    model_config = ConfigDict(frozen=True)

    provider_id: str
    slot_start: str


class Appointment(BaseModel):
    """One booked appointment, as the clinic holds it."""

    model_config = ConfigDict(frozen=True)

    confirmation_id: str
    provider_id: str
    patient_id: str
    slot_start: str
    reason: str


class BookingRequest(BaseModel):
    """What a caller asks for. The idempotency key arrives beside this, in a header."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    patient_id: str
    slot_start: str
    reason: str

    @property
    def slot(self) -> Slot:
        return Slot(provider_id=self.provider_id, slot_start=self.slot_start)


#: The world as it starts, and as ``reset`` restores it.
#:
#: The first slot is the one ``doc-0001-referral`` offers. Demo World declares it independently
#: rather than importing it, because the world may not import the agent — and
#: ``tests/test_demoworld.py`` asserts the two agree, so the coupling is a drift check rather than
#: a shared constant. The other two exist so "available slots" is a list with something on it after
#: the demo books the first.
SEEDED_SLOTS: Final[tuple[Slot, ...]] = (
    Slot(provider_id="PROV-NORTHGATE-CARDIOLOGY", slot_start="2026-09-03T09:30:00"),
    Slot(provider_id="PROV-NORTHGATE-CARDIOLOGY", slot_start="2026-09-03T11:15:00"),
    Slot(provider_id="PROV-NORTHGATE-CARDIOLOGY", slot_start="2026-09-04T14:00:00"),
)


class UnknownSlot(Exception):
    """Raised when a booking names a slot this clinic does not offer."""


class SlotAlreadyTaken(Exception):
    """Raised when a booking names a slot another booking already holds."""


class KeyReused(Exception):
    """Raised when one idempotency key is presented for two different bookings.

    The mirror of :class:`careloop.tools.executor.IdempotencyKeyCollision`, and refused for the
    same reason: returning the first booking for a genuinely different second one would report an
    appointment that was never made as one that was.
    """


class UnknownAppointment(Exception):
    """Raised when a read names a confirmation this clinic does not hold."""


class Clinic:
    """The clinic's durable-for-this-process state.

    A plain class rather than a model: a service with mutable state, not a value. Everything it
    hands out is frozen, so nothing it returns can be edited behind its back.
    """

    def __init__(self, slots: tuple[Slot, ...] = SEEDED_SLOTS) -> None:
        self._seed: Final[tuple[Slot, ...]] = slots
        self._appointments: dict[str, Appointment] = {}
        self._by_key: dict[str, str] = {}
        self._taken: dict[Slot, str] = {}

    def reset(self) -> None:
        """Restore the seeded world. Every booking is forgotten and every slot is free again."""
        self._appointments = {}
        self._by_key = {}
        self._taken = {}

    @property
    def slots(self) -> tuple[Slot, ...]:
        """Every slot this clinic offers, taken or not."""
        return self._seed

    def available(self) -> tuple[Slot, ...]:
        """The slots nobody has booked."""
        return tuple(slot for slot in self._seed if slot not in self._taken)

    def appointments(self) -> tuple[Appointment, ...]:
        """Every booking, in the order the confirmations were minted."""
        return tuple(self._appointments.values())

    def book(self, request: BookingRequest, idempotency_key: str) -> tuple[Appointment, bool]:
        """Book ``request``, or replay the booking already made under ``idempotency_key``.

        Returns the appointment and whether this call is what created it, so a caller can tell a
        booking from a replay without comparing records.

        Raises:
            KeyReused: if the key is recorded against different arguments.
            UnknownSlot: if this clinic does not offer the slot.
            SlotAlreadyTaken: if a different booking holds it.
        """
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            recorded = self._appointments[existing]
            if not self._same(recorded, request):
                raise KeyReused(
                    f"idempotency key {idempotency_key} is already recorded against appointment "
                    f"{recorded.confirmation_id}, which was booked with different details"
                )
            return recorded, False

        slot = request.slot
        if slot not in self._seed:
            raise UnknownSlot(
                f"this clinic offers no slot at {request.slot_start} with {request.provider_id}"
            )
        if slot in self._taken:
            raise SlotAlreadyTaken(
                f"the slot at {request.slot_start} is already held by appointment "
                f"{self._taken[slot]}"
            )

        appointment = Appointment(
            confirmation_id=f"apt-{idempotency_key[:16]}",
            provider_id=request.provider_id,
            patient_id=request.patient_id,
            slot_start=request.slot_start,
            reason=request.reason,
        )
        self._appointments[appointment.confirmation_id] = appointment
        self._by_key[idempotency_key] = appointment.confirmation_id
        self._taken[slot] = appointment.confirmation_id
        return appointment, True

    def appointment(self, confirmation_id: str) -> Appointment:
        """One booking as the clinic holds it now.

        Raises:
            UnknownAppointment: if there is no such confirmation.
        """
        found = self._appointments.get(confirmation_id)
        if found is None:
            raise UnknownAppointment(f"this clinic holds no appointment {confirmation_id!r}")
        return found

    def amend(self, confirmation_id: str, **changes: str) -> Appointment:
        """Change a booking the way a clinic's own front desk would.

        **Nothing in CareLoop can cause this, and that is the point.** It is how the divergence
        between "what CareLoop was authorised to do" and "what the external system actually holds"
        is created in a test — someone at the desk moved the appointment, and the only way to find
        out is to read it back. Without a way to make that happen, a read-back test would only ever
        confirm the clinic agreeing with itself.

        Raises:
            UnknownAppointment: if there is no such confirmation.
        """
        amended = self.appointment(confirmation_id).model_copy(update=changes)
        self._appointments[confirmation_id] = amended
        return amended

    @staticmethod
    def _same(appointment: Appointment, request: BookingRequest) -> bool:
        """Whether a recorded booking is the one ``request`` is asking for."""
        return (
            appointment.provider_id == request.provider_id
            and appointment.patient_id == request.patient_id
            and appointment.slot_start == request.slot_start
            and appointment.reason == request.reason
        )


__all__ = [
    "SEEDED_SLOTS",
    "Appointment",
    "BookingRequest",
    "Clinic",
    "KeyReused",
    "Slot",
    "SlotAlreadyTaken",
    "UnknownAppointment",
    "UnknownSlot",
]
