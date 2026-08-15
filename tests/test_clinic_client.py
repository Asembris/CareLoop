"""One tool made real: ``book_appointment`` over a socket to a service CareLoop does not own.

Every test here runs against Demo World on a loopback port — a real client, a real connection, a
real server in its own event loop. Driving the ASGI app in-process would exercise Demo World and
skip the claim.

What is asserted, in order: a booking changes external state; the executor's ledger still means
what it meant; the idempotency key CareLoop derives is honoured at *both* layers; a refusal from
the clinic is loud; and the typed result is narrow enough that a service growing a field cannot
put a clock on it.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
import pytest

from careloop.tools.callables import ToolSet
from careloop.tools.clinic import (
    AppointmentNotFound,
    ClinicClient,
    ClinicRefused,
    ClinicUnavailable,
    _appointment,
    clinic_executor,
    clinic_handlers,
)
from careloop.tools.executor import DEFAULT_HANDLERS, ExecutionOutcome, MockExternalExecutor
from careloop.tools.idempotency import idempotency_key
from careloop.tools.results import AppointmentBooked
from demoworld.clinic import Clinic, Slot

CASE_ID = "case-0001"

BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": "cardiology review",
}

KEY = idempotency_key(CASE_ID, "book_appointment", BOOKING)


def booking_toolset(clinic: ClinicClient, case_id: str = CASE_ID) -> ToolSet:
    """A toolset whose bookings are real and whose other three effects are not."""
    return ToolSet(case_id, executor=clinic_executor(clinic))


class TestABookingChangesTheOutsideWorld:
    def test_the_slot_is_gone_from_the_clinic_after_a_booking(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        taken = Slot(provider_id=BOOKING["provider_id"], slot_start=BOOKING["slot_start"])
        assert taken in clinic_state.available()

        clinic.book(BOOKING, KEY)

        assert taken not in clinic_state.available()
        assert len(clinic_state.appointments()) == 1

    def test_the_clinic_holds_what_careloop_asked_for(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        booked = clinic.book(BOOKING, KEY)
        held = clinic_state.appointment(booked.confirmation_id)

        assert held.provider_id == BOOKING["provider_id"]
        assert held.patient_id == BOOKING["patient_id"]
        assert held.slot_start == BOOKING["slot_start"]

    def test_the_result_is_the_typed_one_the_mock_would_have_returned(
        self, clinic: ClinicClient
    ) -> None:
        """The type does not change when the effect becomes real, and nor does the caller."""
        booked = clinic.book(BOOKING, KEY)
        assert isinstance(booked, AppointmentBooked)
        assert booked.provider_id == BOOKING["provider_id"]
        assert booked.reason == BOOKING["reason"]
        assert booked.confirmation_id


class TestThroughTheToolset:
    """The route a graph node takes. Nothing above the executor knows the effect became real."""

    def test_calling_the_tool_books_with_the_clinic(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        tools = booking_toolset(clinic)
        result = tools.call("book_appointment", BOOKING)

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert len(clinic_state.appointments()) == 1
        assert tools.executor.invocation_count == 1

    def test_the_key_the_clinic_sees_is_the_key_careloop_derived(
        self, clinic: ClinicClient
    ) -> None:
        """Invariant 7 reaches the external system rather than stopping at the executor."""
        tools = booking_toolset(clinic)
        result = tools.call("book_appointment", BOOKING)

        assert result.idempotency_key == KEY
        assert result.result.confirmation_id == f"apt-{KEY[:16]}"

    def test_the_other_three_effects_are_still_simulated(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """One tool made real is the proof; four would be scope creep with a rationale."""
        tools = booking_toolset(clinic)
        tools.call("send_email", {"to": "rosa@carer.example", "subject": "hello", "body": "hi"})
        tools.call("upload_document", {"portal_id": "PORTAL-X", "document_id": "doc-0001-referral"})

        assert tools.executor.invocation_count == 2
        assert clinic_state.appointments() == ()

    def test_only_the_booking_handler_is_replaced(self, clinic: ClinicClient) -> None:
        handlers = clinic_handlers(clinic)
        assert set(handlers) == set(DEFAULT_HANDLERS)
        assert handlers["book_appointment"] is not DEFAULT_HANDLERS["book_appointment"]
        assert all(
            handlers[name] is DEFAULT_HANDLERS[name]
            for name in DEFAULT_HANDLERS
            if name != "book_appointment"
        )


class TestIdempotencyReachesTheClinic:
    """Two layers, and they are not redundant.

    CareLoop suppresses a replay from its own ledger before dispatching; Demo World honours the same
    key server-side. The first stops CareLoop doing the work twice. The second is what stops a retry
    CareLoop does not know it made — a connection dropped after the booking landed — becoming a
    second appointment, and it is the layer a real integration cannot do without.
    """

    def test_the_executors_own_ledger_suppresses_before_the_clinic_is_reached(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        tools = booking_toolset(clinic)
        first = tools.call("book_appointment", BOOKING)
        second = tools.call("book_appointment", BOOKING)

        assert first.outcome is ExecutionOutcome.EXECUTED
        assert second.outcome is ExecutionOutcome.DUPLICATE_SUPPRESSED
        assert second.result == first.result
        assert len(clinic_state.appointments()) == 1

    def test_a_fresh_executor_replays_the_clinics_original_rather_than_booking_again(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """The second layer, alone. A new executor has an empty ledger and knows nothing.

        This is the T3 property carried through real HTTP: the same case, the same arguments, a
        process that has never seen either, and still one appointment in the world — because the key
        CareLoop derives is deterministic and the clinic honours it.
        """
        first = booking_toolset(clinic).call("book_appointment", BOOKING)
        second = booking_toolset(clinic).call("book_appointment", BOOKING)

        assert len(clinic_state.appointments()) == 1
        assert second.result == first.result
        assert second.result.model_dump_json() == first.result.model_dump_json()

    def test_two_cases_booking_the_same_slot_are_two_different_calls(
        self, clinic: ClinicClient
    ) -> None:
        """``case_id`` scopes the key, so the second case is not a replay — the clinic refuses it
        because the slot is taken, which is the correct answer rather than a silent success."""
        booking_toolset(clinic, "case-0001").call("book_appointment", BOOKING)
        with pytest.raises(ClinicRefused):
            booking_toolset(clinic, "case-0002").call("book_appointment", BOOKING)


class TestWhenTheClinicSaysNo:
    def test_a_slot_the_clinic_does_not_offer_is_refused(self, clinic: ClinicClient) -> None:
        with pytest.raises(ClinicRefused) as refusal:
            clinic.book({**BOOKING, "slot_start": "2031-01-01T08:00:00"}, KEY)
        assert "404" in str(refusal.value)

    def test_a_refusal_records_no_effect(self, clinic: ClinicClient, clinic_state: Clinic) -> None:
        """A booking that did not happen must not appear in the ledger as one that did."""
        tools = booking_toolset(clinic)
        with pytest.raises(ClinicRefused):
            tools.call("book_appointment", {**BOOKING, "slot_start": "2031-01-01T08:00:00"})

        assert tools.executor.invocation_count == 0
        assert tools.executor.ledger == ()
        assert clinic_state.appointments() == ()

    def test_a_clinic_that_is_not_there_is_a_different_exception(self) -> None:
        """A refusal is the clinic answering; this is nobody answering, and they are not the same
        thing to say about the world."""
        nowhere = ClinicClient("http://127.0.0.1:1", timeout=1.0)
        with pytest.raises(ClinicUnavailable):
            nowhere.book(BOOKING, KEY)
        nowhere.close()


class TestTheReadBackIsASeparateRequest:
    def test_an_appointment_can_be_read_back(self, clinic: ClinicClient) -> None:
        booked = clinic.book(BOOKING, KEY)
        assert clinic.appointment(booked.confirmation_id) == booked

    def test_the_read_back_sees_a_change_the_booking_response_cannot(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """The client-level half of the independence proof.

        If ``appointment`` returned a cached copy of the booking, this would pass with the original
        slot. The graph-level half — verification failing on the difference — is in
        ``tests/test_verify_readback.py``.
        """
        booked = clinic.book(BOOKING, KEY)
        clinic_state.amend(booked.confirmation_id, slot_start="2026-09-04T14:00:00")

        observed = clinic.appointment(booked.confirmation_id)
        assert observed.slot_start == "2026-09-04T14:00:00"
        assert observed != booked

    def test_reading_an_appointment_the_clinic_does_not_hold_is_refused(
        self, clinic: ClinicClient
    ) -> None:
        with pytest.raises(ClinicRefused):
            clinic.appointment("apt-nothing")


class TestOnlyTheReadRouteMeansTheAppointmentIsAbsent:
    """A 404 means two unrelated things to this integration, and the client keeps them apart.

    ``GET /clinic/appointments/{id}`` answering 404 is the clinic saying it holds no such
    appointment — a definite statement about an object CareLoop already has a receipt for, minted
    and returned by the clinic itself. ``POST /clinic/appointments`` answering 404 is the clinic
    refusing a booking for a slot it does not offer, which is about a thing that never existed.

    Collapsing the two would make an unknown slot indistinguishable from a vanished appointment.
    So the classification lives on :meth:`ClinicClient.appointment` rather than in ``_request``,
    and this class is what fails if it is ever generalised to every status the transport sees.

    :class:`AppointmentNotFound` subclasses :class:`ClinicRefused` deliberately: it *is* a refusal,
    and everything already catching the broad type keeps working unchanged.
    """

    def test_reading_a_confirmation_the_clinic_does_not_hold_is_an_absence(
        self, clinic: ClinicClient
    ) -> None:
        with pytest.raises(AppointmentNotFound):
            clinic.appointment("apt-nothing")

    def test_an_absence_is_still_a_refusal(self, clinic: ClinicClient) -> None:
        """Vacuity for every existing handler: the vocabulary was narrowed, not replaced."""
        with pytest.raises(ClinicRefused) as refusal:
            clinic.appointment("apt-nothing")

        assert isinstance(refusal.value, AppointmentNotFound)
        assert refusal.value.status_code == 404

    def test_booking_a_slot_the_clinic_does_not_offer_is_not_an_absence(
        self, clinic: ClinicClient
    ) -> None:
        """**The non-regression that keeps the classification read-route specific.**"""
        with pytest.raises(ClinicRefused) as refusal:
            clinic.book({**BOOKING, "slot_start": "2031-01-01T08:00:00"}, KEY)

        assert not isinstance(refusal.value, AppointmentNotFound)
        assert refusal.value.status_code == 404
        assert "404" in str(refusal.value)

    def test_a_refusal_the_clinic_gives_for_another_reason_is_not_an_absence_either(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """A 409 on the booking route stays exactly what it was."""
        clinic.book(BOOKING, KEY)

        with pytest.raises(ClinicRefused) as refusal:
            clinic.book(BOOKING, "a-different-key-for-a-taken-slot")

        assert not isinstance(refusal.value, AppointmentNotFound)
        assert refusal.value.status_code == 409


class TestTheBoundaryIsNarrow:
    """Invariant 9 survives contact with a service CareLoop does not control.

    Not because the service is well-behaved — it could grow a ``booked_at`` tomorrow — but because
    the boundary maps **declared booking arguments by name** and drops everything else.

    That is the distinction, and it is sharper than "keep exactly N fields". A field the service
    grows is one CareLoop never sent and cannot verify, so carrying it would put a clock or a queue
    position on a result for nothing. ``reason`` is the opposite: CareLoop sends it, the clinic
    persists it, and the read-back returns it, so it is a value CareLoop has something to compare
    against. It was dropped anyway until this session, which is what let a reason-only divergence
    reach ``DONE`` — see ``tests/test_verify_readback.py``.
    """

    RESPONSE: ClassVar[dict[str, object]] = {
        "confirmation_id": "apt-0000",
        "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
        "patient_id": "PT-4417",
        "slot_start": "2026-09-03T09:30:00",
        "reason": "cardiology review",
        "booked_at": "2026-08-12T10:00:00+00:00",
        "queue_position": 4,
    }

    def test_a_field_the_service_grows_never_reaches_the_result(self) -> None:
        result = _appointment(httpx.Response(200, json=self.RESPONSE))

        assert set(result.model_dump()) == {
            "confirmation_id",
            "provider_id",
            "patient_id",
            "slot_start",
            "reason",
        }
        assert "booked_at" not in result.model_dump_json()
        assert "queue_position" not in result.model_dump_json()

    def test_the_declared_reason_does_reach_the_result(self) -> None:
        """The half the field set alone does not say: it is carried, not merely named."""
        result = _appointment(httpx.Response(200, json=self.RESPONSE))

        assert result.reason == "cardiology review"

    def test_the_result_type_forbids_the_extra_field_outright(self) -> None:
        """Vacuity: the mapping is narrow *and* the model would refuse the field anyway."""
        assert "booked_at" not in AppointmentBooked.model_fields

    def test_a_response_omitting_reason_is_a_malformed_observation(self) -> None:
        """Absence is not a value to fill in.

        A ``body.get("reason", "")`` would make CareLoop compare the value it executed against a
        string it invented — and where the executed reason is itself empty, which
        ``NOT_SUPPLIED`` makes reachable, a clinic returning no reason at all would verify clean.
        So the mapper subscripts it exactly as it subscripts the other four required fields, and a
        response missing one fails the same loud way.

        What happens to that failure further up — it escapes the verify node rather than becoming a
        ``ReadBackFailed`` — is pre-existing behaviour for every other required field and is not
        changed here.
        """
        missing = {key: value for key, value in self.RESPONSE.items() if key != "reason"}

        with pytest.raises(KeyError):
            _appointment(httpx.Response(200, json=missing))

    def test_the_same_is_true_of_a_field_that_was_always_required(self) -> None:
        """Vacuity: the failure above is the boundary's standing contract, not a reason special
        case."""
        missing = {key: value for key, value in self.RESPONSE.items() if key != "patient_id"}

        with pytest.raises(KeyError):
            _appointment(httpx.Response(200, json=missing))


class TestTheExecutorIsStillTheOnlyRouteOut:
    def test_a_clinic_backed_executor_is_still_an_executor(self, clinic: ClinicClient) -> None:
        """Nothing above the executor changes shape when one handler becomes real."""
        executor = clinic_executor(clinic)
        assert isinstance(executor, MockExternalExecutor)
        assert executor.action_types == frozenset(DEFAULT_HANDLERS)

    def test_the_ledger_still_records_one_entry_per_effect(self, clinic: ClinicClient) -> None:
        tools = booking_toolset(clinic)
        tools.call("book_appointment", BOOKING)
        tools.call("book_appointment", BOOKING)

        assert tools.executor.invocation_count == 1
        assert tools.executor.suppressed_count == 1
        assert len(tools.executor.ledger) == 1
