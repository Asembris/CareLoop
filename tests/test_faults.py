"""The one answer a demo may throw away, and everything it may not touch.

:mod:`careloop.api.faults` exists so a judge can watch the ambiguous-outcome recovery the saga was
built for. Everything about it is a narrowing, and each narrowing is asserted here rather than
described:

* it performs the request **before** deciding, so an ambiguity can only ever be manufactured over a
  write the provider really made;
* it fires only on a ``201``, which is the provider *stating* it created the appointment — a
  refusal, a replay, a read and every other route go back untouched;
* it fires **once**, and is a plain transport again immediately afterwards, which is what lets the
  replay that settles the case and the read that verifies it travel the same wrapper;
* it **releases** the answer it discards, because nothing above it will ever see that response and
  a transport-returned response holds a pooled connection until somebody consumes it;
* it is bound to one **case**, and the binding is enforced by handing a different client to every
  other case rather than by a check inside the transport.

The last one is the load-bearing containment property and it is asserted twice: here at the seam,
and end to end in ``tests/test_demo_recovery.py`` where a second case in the same world really does
book while the fault is armed.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from helpers import FIXED_NOW
from saga_helpers import BOOKING_ARGS, CASE_ID, SAGA_EVENT, booking_key

import careloop
from careloop.api.faults import (
    BOOKING_CREATE_PATH,
    IDEMPOTENCY_HEADER,
    BookingResponseLoss,
    LostResponseTransport,
    booking_response_loss,
)
from careloop.state import InMemoryCaseStore
from careloop.tools.clinic import ClinicClient, ClinicRefused, ClinicUnavailable
from demoworld.clinic import Clinic

#: A second slot Demo World seeds, so a test can book twice without a collision.
OTHER_SLOT = "2026-09-04T14:00:00"


@pytest.fixture
def loss(clinic_service: tuple[str, Clinic], clinic_state: Clinic) -> Iterator[BookingResponseLoss]:
    """The loss capability, pointed at a real Demo World over a real socket."""
    base_url, _ = clinic_service
    made = booking_response_loss(base_url)
    yield made
    made.close()


def book(client: ClinicClient, key: str, **overrides: str):
    """One booking through ``client``, on the saga's arguments unless a test says otherwise."""
    return client.book({**BOOKING_ARGS, **overrides}, key)


class TestAnUnarmedWrapperIsAPlainTransport:
    """Default off, and indistinguishable from the transport it wraps."""

    def test_a_booking_goes_through_untouched(self, loss, clinic_state: Clinic) -> None:
        booked = book(loss.clinic, "key-plain")

        assert booked.confirmation_id == "apt-key-plain"
        assert len(clinic_state.appointments()) == 1
        assert loss.transport.losses == 0

    def test_a_read_goes_through_untouched(self, loss) -> None:
        booked = book(loss.clinic, "key-read")

        assert loss.clinic.appointment(booked.confirmation_id) == booked

    def test_nothing_is_armed_until_something_arms_it(self, loss) -> None:
        assert loss.armed_for is None
        assert loss.transport.armed is False


class TestTheLossFiresOnlyAfterTheProviderCommits:
    """The whole of P1. The request happens first, and only a creation is intercepted."""

    def test_the_answer_to_a_created_booking_is_discarded(self, loss, clinic_state: Clinic) -> None:
        loss.arm(CASE_ID)

        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        assert loss.transport.losses == 1

    def test_the_clinic_holds_the_appointment_the_caller_never_heard_about(
        self, loss, clinic_state: Clinic
    ) -> None:
        """The ambiguity is real: the world changed and the caller was told nothing.

        This is the assertion the whole seam exists to be able to make. Read off Demo World's own
        state rather than off anything CareLoop returned, because what CareLoop returned was an
        exception.
        """
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        held = clinic_state.appointments()
        assert len(held) == 1
        assert held[0].confirmation_id == f"apt-{booking_key()[:16]}"
        assert held[0].patient_id == BOOKING_ARGS["patient_id"]

    def test_the_request_really_left_the_process(self, loss) -> None:
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        assert loss.transport.requests == [f"POST {BOOKING_CREATE_PATH}"]

    def test_the_discarded_answer_names_the_identity_that_was_sent(self, loss) -> None:
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        assert loss.transport.lost_keys == [booking_key()]

    def test_a_refused_booking_is_not_intercepted(self, loss, clinic_state: Clinic) -> None:
        """A ``409`` is the provider declining. There is no committed write to be ambiguous about.

        The mutant this kills fires on any status rather than on a creation: it would hand a case
        an unknown outcome for a booking that demonstrably never happened, which is the exact
        opposite of what the scenario claims.
        """
        book(loss.clinic, "key-holds-the-slot")
        loss.arm(CASE_ID)

        with pytest.raises(ClinicRefused) as refusal:
            book(loss.clinic, booking_key())

        assert refusal.value.status_code == httpx.codes.CONFLICT
        assert loss.transport.losses == 0
        assert loss.transport.armed is True

    def test_an_unknown_slot_is_not_intercepted(self, loss) -> None:
        loss.arm(CASE_ID)

        with pytest.raises(ClinicRefused) as refusal:
            book(loss.clinic, booking_key(), slot_start="2099-01-01T00:00:00")

        assert refusal.value.status_code == httpx.codes.NOT_FOUND
        assert loss.transport.losses == 0

    def test_a_replay_the_provider_recognises_is_not_intercepted(self, loss) -> None:
        """``200`` means the provider returned a booking it already had. Nothing new committed."""
        first = book(loss.clinic, booking_key())
        loss.arm(CASE_ID)

        replayed = book(loss.clinic, booking_key())

        assert replayed == first
        assert loss.transport.losses == 0

    def test_a_read_is_never_intercepted(self, loss) -> None:
        """The read-back that settles a recovered case travels this wrapper and must be untouched.

        A seam able to interfere with the independent observation would be removing the evidence
        the scenario exists to produce.
        """
        booked = book(loss.clinic, "key-before-arming")
        loss.arm(CASE_ID)

        assert loss.clinic.appointment(booked.confirmation_id) == booked
        assert loss.transport.losses == 0
        assert loss.transport.armed is True


class TestTheLossIsOneShot:
    """One arming, one discarded answer, and a plain transport from then on."""

    def test_the_next_booking_goes_through(self, loss, clinic_state: Clinic) -> None:
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        second = book(loss.clinic, "key-after", slot_start=OTHER_SLOT)

        assert second.confirmation_id == "apt-key-after"
        assert loss.transport.losses == 1
        assert len(clinic_state.appointments()) == 2

    def test_the_replay_of_the_lost_booking_goes_through(self, loss, clinic_state: Clinic) -> None:
        """The recovery beat, at the seam. The same key comes back and the provider answers it."""
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        replayed = book(loss.clinic, booking_key())

        assert replayed.confirmation_id == f"apt-{booking_key()[:16]}"
        assert len(clinic_state.appointments()) == 1

    def test_the_arming_is_consumed_by_the_firing(self, loss) -> None:
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        assert loss.transport.armed is False

    def test_disarming_is_safe_when_nothing_is_armed(self, loss) -> None:
        loss.disarm()
        loss.disarm()

        assert loss.armed_for is None
        assert loss.transport.armed is False

    def test_disarming_stops_a_pending_loss(self, loss, clinic_state: Clinic) -> None:
        loss.arm(CASE_ID)
        loss.disarm()

        booked = book(loss.clinic, booking_key())

        assert booked.confirmation_id == f"apt-{booking_key()[:16]}"
        assert loss.transport.losses == 0


class RecordingStream(httpx.SyncByteStream):
    """A response body that says whether anything consumed or released it."""

    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body
        self.iterated = False
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        self.iterated = True
        yield self._body

    def close(self) -> None:
        self.closed = True


class ScriptedTransport(httpx.BaseTransport):
    """An inner transport answering with one prepared response, so a test can hold its stream."""

    def __init__(self, status_code: int, stream: RecordingStream) -> None:
        self.status_code = status_code
        self.stream = stream
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=self.status_code,
            headers={"content-type": "application/json"},
            stream=self.stream,
        )

    def close(self) -> None:
        self.closed = True


def booking_request() -> httpx.Request:
    return httpx.Request(
        "POST",
        f"http://clinic.test{BOOKING_CREATE_PATH}",
        headers={IDEMPOTENCY_HEADER: "key-abc"},
        json=BOOKING_ARGS,
    )


class TestTheDiscardedAnswerIsReleased:
    """A transport-returned response holds a pooled connection until somebody consumes it.

    Nothing above :meth:`LostResponseTransport.handle_request` will ever see this response, because
    that method raises instead of returning it — so this is the only place the connection can be
    released, and abandoning the reference would leak one per press.

    Asserted against a stream the test owns rather than against a real socket, because "the pool got
    its connection back" is not observable from outside httpx and "the stream was consumed and
    closed" is exactly what the pool is waiting for.
    """

    def test_the_body_is_drained_and_the_stream_is_closed(self) -> None:
        stream = RecordingStream()
        transport = LostResponseTransport(ScriptedTransport(httpx.codes.CREATED, stream))
        transport.arm()

        with pytest.raises(httpx.ReadError):
            transport.handle_request(booking_request())

        assert stream.iterated is True
        assert stream.closed is True

    def test_a_passed_through_answer_is_left_for_the_client_to_read(self) -> None:
        """The other direction, and it has to hold too.

        Releasing a response this wrapper is handing *back* would consume the body before
        ``httpx.Client`` ever read it. The two behaviours are opposite and both are required, which
        is what makes the assertion above discriminating rather than a truism about closing things.
        """
        stream = RecordingStream()
        transport = LostResponseTransport(ScriptedTransport(httpx.codes.CREATED, stream))

        response = transport.handle_request(booking_request())

        assert response.status_code == httpx.codes.CREATED
        assert stream.iterated is False
        assert stream.closed is False

    def test_a_non_creation_is_left_for_the_client_to_read(self) -> None:
        stream = RecordingStream()
        transport = LostResponseTransport(ScriptedTransport(httpx.codes.CONFLICT, stream))
        transport.arm()

        response = transport.handle_request(booking_request())

        assert response.status_code == httpx.codes.CONFLICT
        assert stream.closed is False

    def test_closing_the_wrapper_closes_what_it_wraps(self) -> None:
        inner = ScriptedTransport(httpx.codes.CREATED, RecordingStream())
        transport = LostResponseTransport(inner)

        transport.close()

        assert inner.closed is True


class TestTheWrapperKnowsTheRouteABookingReallyTakes:
    """A drift check, not a string comparison.

    ``careloop`` may not import ``demoworld`` and this module may not import
    ``careloop.tools.clinic``'s private path spellings, so :data:`BOOKING_CREATE_PATH` is written
    down twice in the repository. What keeps the two honest is driving a real
    :class:`~careloop.tools.clinic.ClinicClient` and reading where it went.
    """

    def test_a_booking_goes_to_the_path_the_wrapper_watches(self) -> None:
        transport = LostResponseTransport(ScriptedTransport(httpx.codes.CREATED, RecordingStream()))
        client = ClinicClient("http://clinic.test", client=httpx.Client(transport=transport))

        # The scripted body is not an appointment, so the typed mapper raises. Where the request
        # went is what this asserts, and it went before anything could be mapped.
        with pytest.raises(KeyError):
            client.book(BOOKING_ARGS, "key-route")

        assert transport.requests == [f"POST {BOOKING_CREATE_PATH}"]

    def test_the_idempotency_header_is_the_one_the_wrapper_reads(self) -> None:
        transport = LostResponseTransport(ScriptedTransport(httpx.codes.CREATED, RecordingStream()))
        transport.arm()
        client = ClinicClient("http://clinic.test", client=httpx.Client(transport=transport))

        with pytest.raises(ClinicUnavailable):
            client.book(BOOKING_ARGS, "key-header")

        assert transport.lost_keys == ["key-header"]


class TestTheLossBelongsToOneCase:
    """The containment property, at the seam.

    An unrelated booking cannot consume this fault because it is never handed the client that
    contains it. That is enforced by which object a run holds, not by a check the transport makes,
    so there is nothing for a future caller to pass or forget.
    """

    def test_the_armed_case_is_given_the_losing_client(self, loss) -> None:
        ordinary = ClinicClient("http://clinic.test")
        loss.arm(CASE_ID)
        build = loss.context_factory(ordinary)

        context = build(InMemoryCaseStore(), SAGA_EVENT, CASE_ID)

        assert context.clinic is loss.clinic
        ordinary.close()

    def test_every_other_case_is_given_the_ordinary_client(self, loss) -> None:
        ordinary = ClinicClient("http://clinic.test")
        loss.arm(CASE_ID)
        build = loss.context_factory(ordinary)

        context = build(InMemoryCaseStore(), SAGA_EVENT, "some-other-case")

        assert context.clinic is ordinary
        ordinary.close()

    def test_no_case_is_claimed_while_nothing_is_armed(self, loss) -> None:
        assert loss.claims(CASE_ID) is False
        assert loss.claims("anything") is False

    def test_the_claim_survives_the_firing_so_the_recovery_run_reads_back_the_same_way(
        self, loss
    ) -> None:
        """The reconciliation run for the same case keeps the same client, deliberately.

        By then the transport has consumed its arming and is a passthrough, so the replay and the
        independent read both really happen. Sending the second run down a different client would
        make the observation that settles the case travel a path the first attempt did not.
        """
        loss.arm(CASE_ID)
        with pytest.raises(ClinicUnavailable):
            book(loss.clinic, booking_key())

        assert loss.claims(CASE_ID) is True
        assert loss.transport.armed is False

    def test_disarming_releases_the_case(self, loss) -> None:
        loss.arm(CASE_ID)
        loss.disarm()

        assert loss.claims(CASE_ID) is False

    def test_arming_a_second_case_replaces_the_first(self, loss) -> None:
        loss.arm(CASE_ID)
        loss.arm("demo-recovery")

        assert loss.claims(CASE_ID) is False
        assert loss.claims("demo-recovery") is True


def package_root() -> Path:
    return Path(careloop.__file__).parent


#: The layers that must not know this module exists.
#:
#: The seam is transport-level fault injection and nothing else. A graph node, a policy rule, a
#: state writer or a tool that could name it would be a demo affordance with reach into the parts
#: of CareLoop whose answers the demo exists to demonstrate rather than to influence.
CLOSED_LAYERS = ("graph", "policy", "state", "tools")


class TestTheFaultSeamIsInvisibleToTheDomain:
    """A sweep, in the arrangement ``tests/test_reconcile_writers.py`` uses.

    ``careloop.api.faults`` imports *from* the graph layer — it has to build a run context — and the
    direction is the whole point: the domain answers the same questions whether or not this module
    was ever written.
    """

    def test_the_sweep_has_modules_to_sweep(self) -> None:
        for layer in CLOSED_LAYERS:
            assert list((package_root() / layer).rglob("*.py")), layer

    @pytest.mark.parametrize("layer", CLOSED_LAYERS)
    def test_no_module_in_a_closed_layer_names_the_fault_seam(self, layer: str) -> None:
        offenders = []
        for module in (package_root() / layer).rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("api.faults"):
                    offenders.append(f"{module.name}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{module.name}:{node.lineno}"
                        for alias in node.names
                        if alias.name.endswith("api.faults")
                    )
        assert offenders == []

    @pytest.mark.parametrize("layer", CLOSED_LAYERS)
    def test_no_module_in_a_closed_layer_names_the_fault_types(self, layer: str) -> None:
        forbidden = {"LostResponseTransport", "BookingResponseLoss", "booking_response_loss"}
        offenders = []
        for module in (package_root() / layer).rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden:
                    offenders.append(f"{module.name}:{node.lineno}")
        assert offenders == []


class TestNothingHereReadsTheClock:
    """The seam is deterministic, exactly as ``careloop/tools/`` is required to be.

    Not the same invariant — this module is not under ``tools/`` — but the same reasoning: a demo
    whose behaviour depended on the time of day would be a demo that behaved differently in front
    of an audience.
    """

    def test_the_module_imports_no_clock_and_no_randomness(self) -> None:
        tree = ast.parse((package_root() / "api" / "faults.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert imported.isdisjoint({"datetime", "time", "random", "secrets", "uuid"})

    def test_the_fixed_clock_is_untouched_by_any_of_this(self) -> None:
        """A guard against this file quietly acquiring a clock of its own."""
        assert FIXED_NOW.year == 2026
