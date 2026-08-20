"""What an exception is allowed to say about a system CareLoop does not control.

**The defect this closes was found by measurement, not by reading.** This session stood CareLoop up
under OpenTelemetry auto-instrumentation against a real loopback Demo World and dumped every span.
A slot collision produced an ``exception`` event on the graph span whose ``exception.message`` was
the clinic's own sentence, verbatim, confirmation id and all — because ``ClinicRefused`` built its
message by interpolating the response body, and ``Graph.stream_async`` runs under
``use_span(..., end_on_exit=True)``, whose default ``record_exception=True`` copies
``str(exception)`` into telemetry.

The old reasoning was that a node raising becomes a ``FAILED`` case carrying only the exception's
*type*, so the body "stops there". That was a claim about one consumer, and the exception has more
than one: anything that logs it, any traceback, and any tracer. **The fix is at the exception**, so
the property holds for consumers nobody has written yet — which is the only way it can hold for a
tracer this repository has not adopted.

The marker below is the test's own attacker: a value the *service* chooses and CareLoop must never
repeat. It is checked against the exception, and — the sharp part — against a real OTel span
recorded exactly the way Strands records one.

Nothing here needs AWS, a network, or ADOT. The OpenTelemetry SDK is already in the dependency
closure as a hard requirement of ``strands-agents``, so the span assertions cost nothing to run.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from clinic_helpers import SHUTDOWN_TIMEOUT, STARTUP_TIMEOUT
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from careloop.tools.clinic import AppointmentNotFound, ClinicClient, ClinicRefused
from careloop.tools.idempotency import idempotency_key
from demoworld.clinic import Clinic

#: The value the hostile clinic puts in its error body. Distinctive enough that finding it anywhere
#: is unambiguous, and shaped like the things that must never travel: an address and an identifier.
MARKER = "VERY_PRIVATE_EXTERNAL_VALUE_7f93a1_records-intake@attacker.example_PT-9999"

CASE_ID = "case-0001"

BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": "cardiology review",
}

KEY = idempotency_key(CASE_ID, "book_appointment", BOOKING)


async def _hostile_clinic(scope, receive, send) -> None:
    """An ASGI app that answers every request with a refusal carrying :data:`MARKER`.

    Deliberately not Demo World. Demo World's refusals are well behaved, and the property under
    test is what CareLoop does with text it did not write — so the test supplies a service that
    puts hostile content in the one field a client is most likely to repeat. Both the FastAPI
    ``{"detail": ...}`` shape and a bare body are covered, because the deleted helper read both.
    """
    body = json.dumps({"detail": MARKER, "note": MARKER}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 409,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


@contextmanager
def hostile_clinic() -> Iterator[str]:
    """Run :func:`_hostile_clinic` on a loopback port. Same shape as ``clinic_helpers``."""
    config = uvicorn.Config(_hostile_clinic, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="hostile-clinic", daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("the hostile clinic did not start")
        time.sleep(0.01)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT)


@pytest.fixture
def hostile_client() -> Iterator[ClinicClient]:
    with hostile_clinic() as base_url:
        client = ClinicClient(base_url)
        try:
            yield client
        finally:
            client.close()


def _everything_the_exception_says(failure: BaseException) -> str:
    """Every text surface a logger, a traceback or a tracer could reach on one exception."""
    return " ".join([str(failure), repr(failure), repr(failure.args)])


class TestTheMarkerIsGenuinelyInTheResponse:
    """The vacuity proof, and it has to come first.

    Every assertion below is *"the marker is absent"*, which a test asserting nothing at all would
    also satisfy. This is what makes the rest non-vacuous: the body CareLoop received demonstrably
    contains the marker, and the deleted helper read exactly this field. So the marker's absence
    downstream is the patch working, not the marker never having been there.
    """

    def test_the_response_body_carries_the_marker(self, hostile_client: ClinicClient) -> None:
        response = httpx.post(f"{hostile_client.base_url}/clinic/appointments", json={})

        assert response.status_code == 409
        assert MARKER in response.text
        assert response.json()["detail"] == MARKER


class TestTheProviderBodyNeverReachesTheException:
    def test_a_refusal_says_nothing_the_clinic_wrote(self, hostile_client: ClinicClient) -> None:
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.book(BOOKING, KEY)

        assert MARKER not in _everything_the_exception_says(refusal.value)

    def test_no_fragment_of_the_marker_survives_either(self, hostile_client: ClinicClient) -> None:
        """A truncating or partially-redacting fix would pass the whole-marker check."""
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.book(BOOKING, KEY)

        said = _everything_the_exception_says(refusal.value)
        for fragment in ("VERY_PRIVATE", "7f93a1", "attacker.example", "PT-9999", "records-intake"):
            assert fragment not in said

    def test_the_exception_holds_no_attribute_carrying_it(
        self, hostile_client: ClinicClient
    ) -> None:
        """The body must not be *moved* somewhere quieter — it must not be retained at all.

        A fix that kept the text on a private attribute would pass every string check above and
        leak the first time something serialised the object or rendered it in a debugger.
        """
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.book(BOOKING, KEY)

        held = [str(value) for value in vars(refusal.value).values()]
        assert MARKER not in " ".join(held)
        assert vars(refusal.value) == {"operation": "book_appointment", "status_code": 409}

    def test_the_read_route_is_just_as_quiet(self, hostile_client: ClinicClient) -> None:
        """``appointment`` rebuilds its own exception, so it needs its own assertion.

        It used to do that by stringifying the refusal it caught, which copied the body into a
        second exception. Reconstructing a message from ``str(exception)`` is why that happened.
        """
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.appointment("apt-whatever")

        assert MARKER not in _everything_the_exception_says(refusal.value)

    def test_the_chained_cause_carries_nothing_either(self, hostile_client: ClinicClient) -> None:
        """``raise ... from`` keeps a ``__cause__``, and a traceback renders the whole chain."""
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.appointment("apt-whatever")

        cause = refusal.value.__cause__
        if cause is not None:
            assert MARKER not in _everything_the_exception_says(cause)


class TestTheSafeDiagnosticsRemain:
    """Bounded, CareLoop-owned, and enough to tell one failure from another."""

    def test_a_refused_booking_names_the_operation_and_the_status(
        self, hostile_client: ClinicClient
    ) -> None:
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.book(BOOKING, KEY)

        assert refusal.value.operation == "book_appointment"
        assert refusal.value.status_code == 409
        assert str(refusal.value) == "the clinic refused book_appointment with status 409"

    def test_a_refused_read_names_its_own_operation(self, hostile_client: ClinicClient) -> None:
        """Two failures on one case must still be tellable apart without the clinic's words."""
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.appointment("apt-whatever")

        assert refusal.value.operation == "read_appointment"
        assert refusal.value.status_code == 409

    def test_the_operation_is_not_the_path(self, hostile_client: ClinicClient) -> None:
        """The confirmation id is a path segment, and a path in a message is how ids get out."""
        with pytest.raises(ClinicRefused) as refusal:
            hostile_client.appointment("apt-a-very-specific-identifier")

        assert "apt-a-very-specific-identifier" not in _everything_the_exception_says(refusal.value)
        assert "/clinic/appointments" not in str(refusal.value)


class TestTheRefusalIsStillARefusal:
    """The behaviour half. A privacy fix that made failures quieter would be a downgrade."""

    def test_a_real_demo_world_slot_collision_still_raises(
        self, clinic: ClinicClient, clinic_state: Clinic
    ) -> None:
        """The spike's own reproduction: the second booking of one slot is refused, loudly.

        Against the real Demo World, not the hostile stub, so the failure being asserted is the
        one the deployment actually produces.
        """
        first = clinic.book(BOOKING, KEY)
        other_key = idempotency_key("case-0002", "book_appointment", BOOKING)

        with pytest.raises(ClinicRefused) as refusal:
            clinic.book(BOOKING, other_key)

        assert refusal.value.status_code == 409
        assert refusal.value.operation == "book_appointment"
        assert not isinstance(refusal.value, AppointmentNotFound)
        # The world is unchanged: one appointment, and it is the first one.
        assert len(clinic_state.appointments()) == 1
        assert clinic_state.appointment(first.confirmation_id).slot_start == BOOKING["slot_start"]

    def test_an_absent_appointment_is_still_classified_as_absent(
        self, clinic: ClinicClient
    ) -> None:
        """The read-route 404 classification is untouched by how the message is built."""
        with pytest.raises(AppointmentNotFound) as absent:
            clinic.appointment("apt-nothing")

        assert absent.value.status_code == 404
        assert absent.value.operation == "read_appointment"

    def test_a_booking_404_is_still_not_an_absence(self, clinic: ClinicClient) -> None:
        with pytest.raises(ClinicRefused) as refusal:
            clinic.book({**BOOKING, "slot_start": "2031-01-01T08:00:00"}, KEY)

        assert not isinstance(refusal.value, AppointmentNotFound)
        assert refusal.value.status_code == 404


class TestTheMarkerNeverReachesASpan:
    """**The critical proof**, and the one the spike is the evidence for.

    ``record_exception=True`` is OpenTelemetry's default and is what Strands uses when it wraps a
    graph run. This records the failure exactly that way and then reads the span the way an
    exporter would — every attribute, every event attribute, and the serialised form a backend
    actually receives.

    A real tracer is used rather than a mock: the assertion is about what OpenTelemetry *does*
    with an exception, and a stand-in would be asserting what this test believes instead.
    """

    @staticmethod
    def _record_the_failure(client: ClinicClient) -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer(__name__)

        span = tracer.start_span("invoke_graph")
        with pytest.raises(ClinicRefused), trace_api.use_span(span, end_on_exit=True):
            client.book(BOOKING, KEY)
        return exporter

    def test_the_span_records_the_failure_at_all(self, hostile_client: ClinicClient) -> None:
        """Vacuity guard for this class: assert the exception event exists before asserting it is
        clean. Without this, a tracer that recorded nothing would pass every check below."""
        spans = self._record_the_failure(hostile_client).get_finished_spans()

        assert len(spans) == 1
        events = [event.name for event in spans[0].events]
        assert "exception" in events
        assert spans[0].status.status_code.name == "ERROR"

    def test_no_span_attribute_or_event_carries_the_marker(
        self, hostile_client: ClinicClient
    ) -> None:
        for span in self._record_the_failure(hostile_client).get_finished_spans():
            for key, value in (span.attributes or {}).items():
                assert MARKER not in f"{key}{value}"
            for event in span.events:
                for key, value in (event.attributes or {}).items():
                    assert MARKER not in f"{key}{value}"

    def test_the_exception_message_attribute_is_careloop_vocabulary(
        self, hostile_client: ClinicClient
    ) -> None:
        """The exact attribute the spike found the clinic's sentence in."""
        (span,) = self._record_the_failure(hostile_client).get_finished_spans()
        (event,) = [event for event in span.events if event.name == "exception"]
        attributes = dict(event.attributes or {})

        assert attributes["exception.message"] == (
            "the clinic refused book_appointment with status 409"
        )
        assert attributes["exception.type"].endswith("ClinicRefused")

    def test_the_serialised_span_an_exporter_would_send_is_clean(
        self, hostile_client: ClinicClient
    ) -> None:
        """Whole-span sweep, including the stack trace.

        A stack trace legitimately names files and lines — this patch does not try to suppress
        that. What it must not contain is the clinic's text, which it would if the message were
        still built from the body, because a traceback renders the message.
        """
        for span in self._record_the_failure(hostile_client).get_finished_spans():
            assert MARKER not in span.to_json()
