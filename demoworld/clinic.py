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

**Every method holds the clinic's lock**, for the reason every method of ``SqliteCaseStore`` holds
its own: FastAPI runs synchronous route handlers in a worker pool, so two bookings arriving together
reach :meth:`Clinic.book` on two threads. Booking is a check-then-write — is the slot offered, is it
free, then take it — and two of those interleaving both find the slot free and both take it, which
is a second appointment on a slot the clinic offers once. Reads are guarded too, because ``book``
writes three dictionaries and a read landing between them would see a slot held by an appointment
the same clinic says it does not have.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from functools import wraps
from typing import Final, cast

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


def serialised[M: Callable[..., object]](method: M) -> M:
    """Run ``method`` holding the clinic's lock.

    Applied to every public method rather than to :meth:`Clinic.book` alone. The booking is the
    check-then-write that makes the lock necessary, and a read running between its three writes
    would report a world that never existed — a slot held by a confirmation the clinic does not
    list. ``tests/test_demoworld.py::TestEveryClinicMethodIsSerialised`` asserts the decorator is on
    all of them, so a method added later cannot quietly be the one that is not.

    Re-entrant, because :meth:`Clinic.amend` reads through :meth:`Clinic.appointment`.
    """

    @wraps(method)
    def guarded(self: Clinic, *args: object, **kwargs: object) -> object:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(M, guarded)


class Clinic:
    """The clinic's durable-for-this-process state.

    A plain class rather than a model: a service with mutable state, not a value. Everything it
    hands out is frozen, so nothing it returns can be edited behind its back.
    """

    def __init__(self, slots: tuple[Slot, ...] = SEEDED_SLOTS) -> None:
        self._seed: Final[tuple[Slot, ...]] = slots
        self._lock = threading.RLock()
        self._appointments: dict[str, Appointment] = {}
        self._by_key: dict[str, str] = {}
        self._taken: dict[Slot, str] = {}

    @serialised
    def reset(self) -> None:
        """Restore the seeded world. Every booking is forgotten and every slot is free again."""
        self._appointments = {}
        self._by_key = {}
        self._taken = {}

    @property
    def slots(self) -> tuple[Slot, ...]:
        """Every slot this clinic offers, taken or not.

        The one member with no lock and the only one that needs none: the seed is frozen at
        construction and never written, so there is nothing for a concurrent write to tear.
        """
        return self._seed

    @serialised
    def available(self) -> tuple[Slot, ...]:
        """The slots nobody has booked."""
        return tuple(slot for slot in self._seed if slot not in self._taken)

    @serialised
    def appointments(self) -> tuple[Appointment, ...]:
        """Every booking, in the order the confirmations were minted."""
        return tuple(self._appointments.values())

    @serialised
    def book(self, request: BookingRequest, idempotency_key: str) -> tuple[Appointment, bool]:
        """Book ``request``, or replay the booking already made under ``idempotency_key``.

        Returns the appointment and whether this call is what created it, so a caller can tell a
        booking from a replay without comparing records.

        **The whole check-then-write runs under the lock.** Two callers asking for one slot at the
        same moment are the ordinary case here — the demo's scenarios all name the same slot — and
        without it both would find it free and both would take it.

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
        if (holder := self._held_by(slot)) is not None:
            raise SlotAlreadyTaken(
                f"the slot at {request.slot_start} is already held by appointment {holder}"
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

    @serialised
    def appointment(self, confirmation_id: str) -> Appointment:
        """One booking as the clinic holds it now.

        Raises:
            UnknownAppointment: if there is no such confirmation.
        """
        found = self._appointments.get(confirmation_id)
        if found is None:
            raise UnknownAppointment(f"this clinic holds no appointment {confirmation_id!r}")
        return found

    @serialised
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

    def _held_by(self, slot: Slot) -> str | None:
        """The confirmation holding ``slot``, if any. Read inside the booking's guarded region.

        A named step rather than a dictionary lookup inline, so the moment between "is this slot
        free" and "take it" is a thing a test can stand in — which is what
        ``TestTheBookingLockIsWhatRefusesTheSecondCaller`` does to prove the lock is load-bearing
        rather than hoping a race shows up.
        """
        return self._taken.get(slot)

    @staticmethod
    def _same(appointment: Appointment, request: BookingRequest) -> bool:
        """Whether a recorded booking is the one ``request`` is asking for."""
        return (
            appointment.provider_id == request.provider_id
            and appointment.patient_id == request.patient_id
            and appointment.slot_start == request.slot_start
            and appointment.reason == request.reason
        )


#: How many session worlds one process keeps before the least recently used is forgotten.
#:
#: A clinic is three small dictionaries and a lock — a booked one is a few hundred bytes — so the
#: bound is about not growing without limit rather than about memory pressure. It is well above the
#: number of people who can be looking at a demo at once, and a forgotten world is a world that
#: reseeds, which is what the reset button does anyway.
DEFAULT_WORLD_LIMIT: Final[int] = 64


class Clinics:
    """One clinic per session, and one for anybody who names none.

    **Why a clinic cannot be shared.** Every demo scenario books the slot the referral offers, so
    two visitors running scenarios at the same time ask one clinic for one slot — and the second
    gets a correct refusal that looks, on screen, like the agent failing. Separate worlds are what
    make two visitors independent, and separating the *cases* without separating the *clinic* would
    leave the two competing over the one thing that is genuinely external.

    **A session id is a namespace, not a credential.** Demo World mints none and verifies none: it
    is handed an opaque string by whoever is calling and keeps a world under it, exactly as it keeps
    the default world for a caller who names nothing. Anyone who can reach the port can name any
    world, which is the same statement CareLoop's own ``X-Caregiver`` makes about identity.

    **Least-recently-used, and no clock.** Demo World reads no time and no random source — a
    ``booked_at`` or a minted id would break the byte-identical replay CareLoop asserts through the
    real HTTP path — so worlds are evicted by *order of use* rather than by age. An
    :class:`~collections.OrderedDict` is the whole mechanism.
    """

    def __init__(
        self,
        *,
        default: Clinic | None = None,
        limit: int = DEFAULT_WORLD_LIMIT,
        slots: tuple[Slot, ...] = SEEDED_SLOTS,
    ) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._slots = slots
        self._default: Final[Clinic] = default if default is not None else Clinic(slots)
        self._worlds: OrderedDict[str, Clinic] = OrderedDict()

    @property
    def default(self) -> Clinic:
        """The world a caller who names no session reaches."""
        return self._default

    def world(self, session: str | None) -> Clinic:
        """The clinic ``session`` books against, created on first use.

        ``None`` is the default world rather than a new one: a developer running Demo World by hand
        and a judge reading ``GET /clinic/slots`` in a browser both arrive without a session, and
        minting them a fresh world each time would show them an empty clinic forever.
        """
        if session is None:
            return self._default

        with self._lock:
            existing = self._worlds.get(session)
            if existing is not None:
                self._worlds.move_to_end(session)
                return existing

            created = Clinic(self._slots)
            self._worlds[session] = created
            while len(self._worlds) > self._limit:
                self._worlds.popitem(last=False)
            return created

    def sessions(self) -> tuple[str, ...]:
        """Every session with a world here, least recently used first. For tests and diagnostics."""
        with self._lock:
            return tuple(self._worlds)

    def __len__(self) -> int:
        """How many session worlds are held. The default world is not one of them."""
        with self._lock:
            return len(self._worlds)


__all__ = [
    "DEFAULT_WORLD_LIMIT",
    "SEEDED_SLOTS",
    "Appointment",
    "BookingRequest",
    "Clinic",
    "Clinics",
    "KeyReused",
    "Slot",
    "SlotAlreadyTaken",
    "UnknownAppointment",
    "UnknownSlot",
    "serialised",
]
