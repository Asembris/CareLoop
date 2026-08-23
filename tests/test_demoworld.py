"""Demo World on its own: the clinic's rules, and the two things it must never become.

Driven through ``TestClient`` rather than a socket, because these tests are about what the service
*decides* — a slot it does not offer, a slot somebody else holds, a key it has already seen. The
socket matters for the claim that CareLoop reaches the world over real HTTP, and that is asserted
where it belongs, in ``tests/test_clinic_client.py``.

Two sweeps here are not about the clinic at all. Demo World may not import CareLoop, or it stops
being an external system; and it may not read a clock or a random source, or the byte-identical
replay CareLoop asserts on stops holding through the real path.
"""

from __future__ import annotations

import ast
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from helpers import BOOKING_REASON

import demoworld
from careloop.graph.trusted import configured
from careloop.tools.document_source import DOCUMENT_SOURCES
from demoworld.app import (
    IDEMPOTENCY_HEADER,
    SESSION_HEADER,
    SESSION_PARAM,
    create_clinic_app,
    demo_session,
)
from demoworld.clinic import SEEDED_SLOTS, Clinic, Clinics, WorldsAtCapacity

KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
OTHER_KEY = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

BOOKING = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": BOOKING_REASON,
}


@pytest.fixture
def world() -> Clinic:
    return Clinic()


@pytest.fixture
def client(world: Clinic) -> TestClient:
    return TestClient(create_clinic_app(world))


def book(
    client: TestClient,
    key: str = KEY,
    headers: dict[str, str] | None = None,
    **changes: str,
):
    return client.post(
        "/clinic/appointments",
        json={**BOOKING, **changes},
        headers={IDEMPOTENCY_HEADER: key, **(headers or {})},
    )


class TestBooking:
    def test_a_booking_takes_the_slot_out_of_the_available_list(self, client: TestClient) -> None:
        booked_slot = {
            "provider_id": BOOKING["provider_id"],
            "slot_start": BOOKING["slot_start"],
        }
        before = client.get("/clinic/slots").json()["slots"]
        assert booked_slot in before

        assert book(client).status_code == 201

        after = client.get("/clinic/slots").json()["slots"]
        assert booked_slot not in after
        assert len(after) == len(before) - 1

    def test_the_confirmation_echoes_what_was_asked_for(self, client: TestClient) -> None:
        booked = book(client).json()
        assert booked["provider_id"] == BOOKING["provider_id"]
        assert booked["patient_id"] == BOOKING["patient_id"]
        assert booked["slot_start"] == BOOKING["slot_start"]
        assert booked["confirmation_id"]

    def test_a_slot_this_clinic_does_not_offer_is_a_404(self, client: TestClient) -> None:
        refused = book(client, slot_start="2031-01-01T08:00:00")
        assert refused.status_code == 404

    def test_a_slot_somebody_else_holds_is_a_409(self, client: TestClient) -> None:
        assert book(client).status_code == 201
        clash = book(client, key=OTHER_KEY, patient_id="PT-9999")
        assert clash.status_code == 409

    def test_a_booking_with_no_idempotency_key_is_a_400(self, client: TestClient) -> None:
        """The same reasoning as CareLoop's missing ``X-Caregiver``: a required field is absent."""
        refused = client.post("/clinic/appointments", json=BOOKING)
        assert refused.status_code == 400
        assert IDEMPOTENCY_HEADER in refused.json()["detail"]


class TestIdempotency:
    """The key CareLoop already derives is honoured here, or a retry books twice."""

    def test_the_same_key_replays_the_original_rather_than_booking_again(
        self, client: TestClient, world: Clinic
    ) -> None:
        first = book(client)
        second = book(client)

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json() == first.json()
        assert len(world.appointments()) == 1

    def test_a_replay_does_not_consume_a_second_slot(self, client: TestClient) -> None:
        book(client)
        remaining = len(client.get("/clinic/slots").json()["slots"])
        book(client)
        assert len(client.get("/clinic/slots").json()["slots"]) == remaining

    def test_one_key_for_two_different_bookings_is_refused(self, client: TestClient) -> None:
        """Returning the first booking for a genuinely different second one would report an
        appointment that was never made as one that was."""
        assert book(client).status_code == 201
        clash = book(client, patient_id="PT-0001")
        assert clash.status_code == 409

    def test_the_confirmation_id_derives_from_the_key_and_nothing_else(
        self, client: TestClient
    ) -> None:
        """No clock, no counter. A reset and a re-run produce the same record, byte for byte."""
        first = book(client).json()
        assert client.post("/demo/reset").status_code == 200
        again = book(client).json()
        assert again == first


class TestReadBack:
    def test_a_booking_can_be_read_back_by_its_confirmation(self, client: TestClient) -> None:
        booked = book(client).json()
        read = client.get(f"/clinic/appointments/{booked['confirmation_id']}")
        assert read.status_code == 200
        assert read.json() == booked

    def test_an_unknown_confirmation_is_a_404(self, client: TestClient) -> None:
        assert client.get("/clinic/appointments/apt-nothing").status_code == 404

    def test_the_read_reflects_a_later_change_rather_than_the_booking_response(
        self, client: TestClient, world: Clinic
    ) -> None:
        """The mechanism the independence proof rests on.

        ``amend`` is the clinic's own front desk moving an appointment — nothing in CareLoop can
        cause it. If the read route echoed the booking instead of reading current state, this would
        pass with the original values and the whole read-back would be a cached receipt.
        """
        booked = book(client).json()
        world.amend(booked["confirmation_id"], slot_start="2026-09-04T14:00:00")

        read = client.get(f"/clinic/appointments/{booked['confirmation_id']}").json()
        assert read["slot_start"] == "2026-09-04T14:00:00"
        assert read != booked


class TestReset:
    def test_reset_frees_every_slot_and_forgets_every_booking(
        self, client: TestClient, world: Clinic
    ) -> None:
        book(client)
        assert len(world.appointments()) == 1

        restored = client.post("/demo/reset")
        assert restored.status_code == 200
        assert restored.json()["slots"] == [slot.model_dump() for slot in SEEDED_SLOTS]
        assert world.appointments() == ()

    def test_the_slot_is_bookable_again_after_a_reset(self, client: TestClient) -> None:
        assert book(client).status_code == 201
        client.post("/demo/reset")
        assert book(client, key=OTHER_KEY).status_code == 201


class TestOneWorldPerSession:
    """Two visitors, one seeded slot each, and neither can see or empty the other's clinic.

    Every demo scenario books the slot the referral offers, so a shared clinic makes the second
    visitor's booking fail with a correct refusal that reads, on screen, like the agent breaking.
    """

    def test_two_sessions_book_the_same_slot_and_both_succeed(self, client: TestClient) -> None:
        first = book(client, headers={SESSION_HEADER: "session-a"})
        second = book(client, headers={SESSION_HEADER: "session-b"})

        assert [first.status_code, second.status_code] == [201, 201]
        # The same case in two worlds derives the same key, so the two confirmations agree. That is
        # determinism rather than a collision: they are appointments in different clinics.
        assert first.json() == second.json()

    def test_a_session_sees_only_its_own_slots(self, client: TestClient) -> None:
        book(client, headers={SESSION_HEADER: "session-a"})

        mine = client.get("/clinic/slots", headers={SESSION_HEADER: "session-a"}).json()["slots"]
        theirs = client.get("/clinic/slots", headers={SESSION_HEADER: "session-b"}).json()["slots"]

        assert len(mine) == len(SEEDED_SLOTS) - 1
        assert theirs == [slot.model_dump() for slot in SEEDED_SLOTS]

    def test_a_session_cannot_read_another_sessions_appointment(self, client: TestClient) -> None:
        confirmation = book(client, headers={SESSION_HEADER: "session-a"}).json()["confirmation_id"]
        path = f"/clinic/appointments/{confirmation}"

        assert client.get(path, headers={SESSION_HEADER: "session-a"}).status_code == 200
        assert client.get(path, headers={SESSION_HEADER: "session-b"}).status_code == 404

    def test_a_reset_empties_only_the_world_that_asked(self, client: TestClient) -> None:
        book(client, headers={SESSION_HEADER: "session-a"})
        book(client, headers={SESSION_HEADER: "session-b"})

        assert client.post("/demo/reset", headers={SESSION_HEADER: "session-a"}).status_code == 200

        gone = client.get("/clinic/slots", headers={SESSION_HEADER: "session-a"}).json()["slots"]
        kept = client.get("/clinic/slots", headers={SESSION_HEADER: "session-b"}).json()["slots"]
        assert gone == [slot.model_dump() for slot in SEEDED_SLOTS]
        assert len(kept) == len(SEEDED_SLOTS) - 1

    def test_a_caller_naming_no_session_reaches_the_default_world(
        self, client: TestClient, world: Clinic
    ) -> None:
        """The developer's world and the judge's browser, unchanged by any of this."""
        book(client)
        assert len(world.appointments()) == 1
        assert client.get("/clinic/slots").json()["slots"] == [
            slot.model_dump() for slot in SEEDED_SLOTS[1:]
        ]

    def test_a_browser_can_name_a_world_in_the_query_string(self, client: TestClient) -> None:
        """``?session=`` exists because ``GET /clinic/slots`` is meant to be checked by a person."""
        book(client, headers={SESSION_HEADER: "session-a"})

        by_query = client.get(f"/clinic/slots?{SESSION_PARAM}=session-a").json()["slots"]
        by_header = client.get("/clinic/slots", headers={SESSION_HEADER: "session-a"}).json()[
            "slots"
        ]
        assert by_query == by_header

    def test_the_header_wins_when_both_name_a_world(self) -> None:
        assert demo_session(x_demo_session="from-header", session="from-query") == "from-header"
        assert demo_session(session="from-query") == "from-query"
        assert demo_session() is None
        assert demo_session(x_demo_session="   ") is None


class TestWorldsAreEndedOnlyWhenTheOwnerSaysSo:
    """Nothing here decides that a visitor has gone. Bounded by refusing, not by taking.

    This registry used to evict least-recently-used, where "used" meant the *clinic* was touched,
    while CareLoop expired a world by idle time refreshed on every API request. Two owners with
    incompatible orders, and the victim was a visitor who was still there — see
    ``tests/test_session_lifecycle.py`` for the reproduction and :class:`demoworld.clinic.Clinics`
    for the reasoning. With nothing evicting there is no eviction order left to disagree about.
    """

    def test_a_world_survives_being_used(self) -> None:
        worlds = Clinics(limit=2)
        first = worlds.world("a")
        assert worlds.world("a") is first

    def test_nothing_is_taken_when_the_limit_is_reached(self) -> None:
        """The arriving visitor is refused; the two already here keep everything."""
        worlds = Clinics(limit=2)
        original = worlds.world("a")
        worlds.world("b")

        with pytest.raises(WorldsAtCapacity):
            worlds.world("c")

        assert worlds.sessions() == ("a", "b")
        assert worlds.world("a") is original

    def test_a_session_already_holding_a_world_is_never_refused(self) -> None:
        """The cap bounds how many worlds exist, not how often somebody may use theirs."""
        worlds = Clinics(limit=1)
        held = worlds.world("a")
        assert worlds.world("a") is held

    def test_ending_a_world_frees_its_place(self) -> None:
        worlds = Clinics(limit=1)
        worlds.world("a")

        assert worlds.end("a") is True
        assert worlds.sessions() == ()
        assert worlds.world("b") is not None

    def test_ending_is_idempotent_and_says_whether_there_was_one(self) -> None:
        """A retry after a partial success has to look like success, or repair looks like
        failure."""
        worlds = Clinics(limit=2)
        worlds.world("a")

        assert worlds.end("a") is True
        assert worlds.end("a") is False
        assert worlds.end("never-created") is False

    def test_an_ended_world_reseeds_rather_than_returning(self) -> None:
        worlds = Clinics(limit=2)
        original = worlds.world("a")
        worlds.end("a")
        assert worlds.world("a") is not original

    def test_reading_creates_nothing(self) -> None:
        """Without this, and without eviction, a URL and a loop would fill the registry forever."""
        worlds = Clinics(limit=2)

        for name in ("a", "b", "c", "d"):
            assert worlds.view(name).available() == SEEDED_SLOTS

        assert worlds.sessions() == ()
        assert len(worlds) == 0

    def test_a_read_of_a_world_that_exists_is_that_world(self) -> None:
        worlds = Clinics(limit=2)
        held = worlds.world("a")
        assert worlds.view("a") is held

    def test_there_is_no_order_of_use_to_disagree_about(self) -> None:
        """Using a world does not reorder anything, because nothing consults an order."""
        worlds = Clinics(limit=3)
        worlds.world("a")
        worlds.world("b")
        worlds.world("a")
        worlds.world("c")

        assert worlds.sessions() == ("a", "b", "c")

    def test_the_default_world_is_not_one_of_them(self) -> None:
        worlds = Clinics(limit=1)
        worlds.world("a")
        assert len(worlds) == 1
        assert worlds.default is worlds.world(None)
        assert worlds.default is worlds.view(None)


class TestEndingAWorldOverHttp:
    """``DELETE /demo/session`` — the lifecycle verb, and the only way a world is removed."""

    def test_it_ends_the_world_the_caller_names(self, client: TestClient) -> None:
        book(client, headers={SESSION_HEADER: "session-a"})
        confirmation = book(client, headers={SESSION_HEADER: "session-a"}).json()["confirmation_id"]

        ended = client.delete("/demo/session", headers={SESSION_HEADER: "session-a"})
        assert ended.status_code == 200
        assert ended.json() == {"session": "session-a", "existed": True}

        gone = client.get(
            f"/clinic/appointments/{confirmation}", headers={SESSION_HEADER: "session-a"}
        )
        assert gone.status_code == 404

    def test_it_leaves_every_other_world_alone(self, client: TestClient) -> None:
        mine = book(client, headers={SESSION_HEADER: "session-a"}).json()["confirmation_id"]
        book(client, headers={SESSION_HEADER: "session-b"})

        client.delete("/demo/session", headers={SESSION_HEADER: "session-b"})

        kept = client.get(f"/clinic/appointments/{mine}", headers={SESSION_HEADER: "session-a"})
        assert kept.status_code == 200

    def test_ending_a_world_that_is_not_here_is_still_a_200(self, client: TestClient) -> None:
        """Idempotent: a caller repairing a divergence must not be told it failed to."""
        ended = client.delete("/demo/session", headers={SESSION_HEADER: "never-booked"})
        assert ended.status_code == 200
        assert ended.json() == {"session": "never-booked", "existed": False}

    def test_a_browser_can_name_the_world_in_the_query_string(self, client: TestClient) -> None:
        book(client, headers={SESSION_HEADER: "session-a"})
        ended = client.delete(f"/demo/session?{SESSION_PARAM}=session-a")
        assert ended.json() == {"session": "session-a", "existed": True}

    def test_naming_no_session_is_a_400_and_the_default_world_is_untouched(
        self, client: TestClient, world: Clinic
    ) -> None:
        """The default world has no lifetime to end, and this must end nothing else quietly."""
        book(client)
        refused = client.delete("/demo/session")

        assert refused.status_code == 400
        assert SESSION_HEADER in refused.json()["detail"]
        assert len(world.appointments()) == 1


class TestAFullClinicRefusesTheArrivingVisitor:
    """At the cap, somebody arriving is refused — never somebody already here.

    The whole trade, stated where it is observable: the alternative is deleting the world of a
    visitor who is still looking at the demo, which is what this replaced.
    """

    def test_a_new_world_is_refused_with_503(self) -> None:
        client = TestClient(create_clinic_app(Clinic(), limit=1))
        assert book(client, headers={SESSION_HEADER: "session-a"}).status_code == 201

        refused = book(client, headers={SESSION_HEADER: "session-b"})
        assert refused.status_code == 503

    def test_the_visitor_who_is_already_here_is_untouched(self) -> None:
        client = TestClient(create_clinic_app(Clinic(), limit=1))
        confirmation = book(client, headers={SESSION_HEADER: "session-a"}).json()["confirmation_id"]

        book(client, headers={SESSION_HEADER: "session-b"})

        kept = client.get(
            f"/clinic/appointments/{confirmation}", headers={SESSION_HEADER: "session-a"}
        )
        assert kept.status_code == 200

    def test_a_read_is_never_refused_by_the_cap(self) -> None:
        """Reads create nothing, so a full registry has nothing to refuse them for."""
        client = TestClient(create_clinic_app(Clinic(), limit=1))
        book(client, headers={SESSION_HEADER: "session-a"})

        read = client.get("/clinic/slots", headers={SESSION_HEADER: "session-b"})
        assert read.status_code == 200
        assert read.json()["slots"] == [slot.model_dump() for slot in SEEDED_SLOTS]

    def test_ending_a_world_makes_room_for_the_next_visitor(self) -> None:
        client = TestClient(create_clinic_app(Clinic(), limit=1))
        book(client, headers={SESSION_HEADER: "session-a"})
        client.delete("/demo/session", headers={SESSION_HEADER: "session-a"})

        assert book(client, headers={SESSION_HEADER: "session-b"}).status_code == 201


class TestNothingHereIsCacheable:
    """A cached read-back is the one failure that deletes the evidence and still shows green.

    CareLoop verifies a booking with a *separate* ``GET`` on the confirmation id. Answered out of a
    CDN's memory, that read confirms CareLoop's own earlier request rather than the clinic's state,
    reports ``read_back: true``, and passes while the clinic holds something else. Session F found
    that this origin set no ``Cache-Control`` at all; it sets one now, so no cache policy in front
    of it gets a vote.
    """

    def test_every_route_says_no_store(self, client: TestClient) -> None:
        booked = book(client)
        paths = [
            "/clinic/slots",
            f"/clinic/appointments/{booked.json()['confirmation_id']}",
        ]
        for path in paths:
            assert client.get(path).headers["Cache-Control"] == "no-store", path

        assert booked.headers["Cache-Control"] == "no-store"
        assert client.post("/demo/reset").headers["Cache-Control"] == "no-store"

    def test_a_refusal_says_it_too(self, client: TestClient) -> None:
        """The 404 a read-back gets when the clinic holds nothing is an answer worth not caching."""
        missing = client.get("/clinic/appointments/apt-nothing")
        assert missing.status_code == 404
        assert missing.headers["Cache-Control"] == "no-store"


class _NoLock:
    """A lock that guards nothing, for the mutant below. Not used by anything shipped."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool:
        return False


class RacingClinic(Clinic):
    """A clinic that makes the booking race happen rather than hoping for it.

    Every caller waits at a barrier *inside* the guarded region, in the gap between having read
    that the slot is free and acting on it. That is where a check-then-write is unsafe, and the
    wait is placed **after** the read rather than before it: released together before the read,
    the threads still lose the race to whichever one the interpreter happens to run to completion
    first, and the mutant passes for the wrong reason. Measured, not reasoned about — the first
    version of this test synced before the read and reported the shipped behaviour.

    With the lock in place the second caller cannot reach that gap until the first has left it, so
    the barrier times out and the calls are serialised — which is the property. With
    ``guarded=False`` the lock is replaced by one that guards nothing, both callers hold a reading
    of "free" at the same moment, and both book.

    Nothing is reimplemented here: the mutant swaps the lock and wraps one named step, so what the
    vacuity proof exercises is the shipped booking logic minus the shipped lock.
    """

    def __init__(self, *, callers: int, guarded: bool, timeout: float = 0.25) -> None:
        super().__init__()
        if not guarded:
            self._lock = _NoLock()  # type: ignore[assignment]
        self._gate = threading.Barrier(callers)
        self._timeout = timeout

    def _held_by(self, slot):  # type: ignore[no-untyped-def]
        holder = super()._held_by(slot)
        with contextlib.suppress(threading.BrokenBarrierError):
            self._gate.wait(timeout=self._timeout)
        return holder


def book_concurrently(clinic: Clinic, keys: tuple[str, ...]) -> list[int]:
    """Post one booking per key at the same time, and return the status codes."""
    client = TestClient(create_clinic_app(clinic))
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        futures = [pool.submit(lambda k=key: book(client, key=k).status_code) for key in keys]
        return [future.result() for future in futures]


class TestTheBookingLockIsWhatRefusesTheSecondCaller:
    """One slot, two callers, one appointment — and the proof that the lock is what does it.

    Booking is a check-then-write, and FastAPI runs synchronous handlers in a worker pool, so two
    judges pressing the same scenario at the same moment really do arrive on two threads. This is
    the race that produces a second appointment on a slot the clinic offers once.
    """

    KEYS = (KEY, OTHER_KEY)

    def test_two_callers_asking_for_one_slot_get_one_appointment(self) -> None:
        clinic = RacingClinic(callers=len(self.KEYS), guarded=True)
        assert sorted(book_concurrently(clinic, self.KEYS)) == [201, 409]
        assert len(clinic.appointments()) == 1

    def test_without_the_lock_both_callers_book_it(self) -> None:
        """The vacuity proof. Remove the lock and the clinic sells one slot twice.

        Named so it survives a tidy-up: it is not testing a mutant for its own sake, it is the
        evidence that the assertion above is about the lock rather than about two calls that
        happened not to overlap.
        """
        clinic = RacingClinic(callers=len(self.KEYS), guarded=False)
        assert sorted(book_concurrently(clinic, self.KEYS)) == [201, 201]
        assert len(clinic.appointments()) == 2

    def test_ten_callers_racing_for_one_slot_still_produce_one(self, world: Clinic) -> None:
        """The same claim without the barrier, over the shipped clinic and ten real threads."""
        keys = tuple(f"{index:064x}" for index in range(10))
        statuses = book_concurrently(world, keys)

        assert statuses.count(201) == 1
        assert set(statuses) == {201, 409}
        assert len(world.appointments()) == 1


class TestEveryClinicMethodIsSerialised:
    """The lock is not optional on any of them, and a new method must not be the exception.

    Asserted on the decorator rather than by racing threads, for the reason
    ``tests/test_store.py::TestEveryStoreMethodIsSerialised`` is: a race that happens to pass is
    not evidence. The race that *is* evidence is the barrier pair above.
    """

    METHODS: ClassVar[tuple[str, ...]] = (
        "amend",
        "appointment",
        "appointments",
        "available",
        "book",
        "reset",
    )

    def test_the_list_covers_every_public_method(self) -> None:
        """Vacuity: a method missing from this list is a method the sweep never looks at.

        ``slots`` is excluded and is the only exclusion: it is a property over a tuple frozen at
        construction, so there is no write for a reader to tear.
        """
        public = {
            name
            for name in dir(Clinic)
            if not name.startswith("_") and callable(getattr(Clinic, name))
        }
        assert public == set(self.METHODS)

    def test_every_one_of_them_holds_the_lock(self) -> None:
        unguarded = [
            name for name in self.METHODS if not hasattr(getattr(Clinic, name), "__wrapped__")
        ]
        assert unguarded == []

    def test_the_detector_notices_an_undecorated_method(self) -> None:
        """Vacuity: ``__wrapped__`` is the decorator's fingerprint, so a plain method has none."""

        class Undecorated:
            def book(self) -> None: ...

        assert not hasattr(Undecorated.book, "__wrapped__")


class TestTheSeededWorldMatchesTheReferral:
    """A drift check, not a shared constant.

    Demo World may not import CareLoop, so the slot the referral offers is declared twice. That is
    fine as long as a disagreement is loud: without this, editing the referral's offered
    appointment would leave the demo booking a slot the clinic does not have, and the failure would
    surface as a 404 in front of a judge rather than as a red test.
    """

    def offered_appointment(self) -> str:
        referral = next(
            source for source in DOCUMENT_SOURCES if source.document_id == "doc-0001-referral"
        )
        lines = [line for page in referral.pages for line in page]
        offered = next(line for line in lines if line.startswith("Offered appointment:"))
        return offered.split(":", 1)[1].strip()

    def test_the_referral_really_does_offer_an_appointment(self) -> None:
        """Vacuity: if the line vanished, the check below would have nothing to compare."""
        assert self.offered_appointment() == "2026-09-03T09:30:00"

    def test_the_clinic_offers_the_slot_the_referral_names(self) -> None:
        assert self.offered_appointment() in {slot.slot_start for slot in SEEDED_SLOTS}

    def test_the_clinic_offers_it_with_the_configured_provider(self) -> None:
        """The booking carries the ``TRUSTED`` provider id, so the clinic has to know that one."""
        provider = configured("provider_id")
        assert provider is not None
        assert provider in {slot.provider_id for slot in SEEDED_SLOTS}


def imported_modules(path: Path) -> set[str]:
    """Every module name ``path`` imports, read from its syntax tree."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def imports_matching(path: Path, banned: tuple[str, ...]) -> set[str]:
    """The imports of ``path`` that are, or sit under, one of ``banned``."""
    return {
        imported
        for imported in imported_modules(path)
        for prefix in banned
        if imported == prefix or imported.startswith(f"{prefix}.")
    }


def demoworld_modules() -> list[Path]:
    return sorted(Path(demoworld.__file__).parent.glob("*.py"))


class TestDemoWorldIsNotPartOfCareLoop:
    """The world may not import the agent.

    Verifying an external effect proves something only if the system read back is not the system
    doing the reading. An import in this direction would not break a test today and would quietly
    turn the read-back into the agent consulting itself.
    """

    def test_the_package_has_the_modules_this_sweep_expects(self) -> None:
        assert {path.name for path in demoworld_modules()} == {
            "__init__.py",
            "__main__.py",
            "app.py",
            "clinic.py",
        }

    def test_no_demoworld_module_imports_careloop(self) -> None:
        offenders = {
            path.name: sorted(found)
            for path in demoworld_modules()
            if (found := imports_matching(path, ("careloop",)))
        }
        assert offenders == {}

    def test_the_detector_finds_the_import_where_one_exists(self) -> None:
        """Vacuity: this very test module imports CareLoop, so the sweep must flag it."""
        assert imports_matching(Path(__file__), ("careloop",)) == {
            "careloop.graph.trusted",
            "careloop.tools.document_source",
        }


class TestNoClockAndNoRandomness:
    """Not required of this package the way invariant 9 requires it of ``careloop/tools/``.

    It is kept anyway because CareLoop's replay assertion runs *through* here on the real path: a
    ``booked_at`` on an appointment record, or a randomly minted confirmation id, would make a
    replayed booking differ from the original in a field nobody thought was load-bearing.
    """

    FORBIDDEN = ("datetime", "time", "random", "secrets", "uuid")

    def test_no_demoworld_module_imports_a_clock_or_a_random_source(self) -> None:
        offenders = {
            path.name: sorted(found)
            for path in demoworld_modules()
            if (found := imports_matching(path, self.FORBIDDEN))
        }
        assert offenders == {}

    def test_the_detector_finds_a_clock_where_one_exists(self) -> None:
        """Vacuity: ``careloop/state/case.py`` stamps times, so the sweep must flag it."""
        case = Path(demoworld.__file__).parent.parent / "careloop" / "state" / "case.py"
        assert imports_matching(case, self.FORBIDDEN) == {"datetime"}

    def test_no_appointment_field_is_filled_in_from_ambient_state(self) -> None:
        """A default or a factory is where a timestamp or a random id would enter."""
        for name, field in demoworld.Appointment.model_fields.items():
            assert field.is_required(), f"Appointment.{name} carries a default"
