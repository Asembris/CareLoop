"""The judge-operable upload: one scenario, one namespace per visitor, and a reset that clears it.

Three properties, and the third is the one that makes the demo repeatable rather than a one-shot.

**A scenario exists.** ``POST /api/demo/scenarios/records`` delivers the claim rejection that asks
for the receipt, through the ordinary delivery path — the same route every other scenario uses, with
nothing armed and nothing special about it.

**A visitor's objects are their own.** The object key carries the session id, and a client has no
parameter for anybody else's namespace, so isolation is structural rather than checked.

**A reset really clears them, and it has to.** The frozen idempotency key is derived from the case
id and the identifying arguments, every one of which a scenario fixes — so a second press addresses
*the same object*. Left in place, the create-once precondition would refuse the write and a judge
would watch a reconciliation instead of a filing. This is the one place where the measured provider
contract reaches all the way out to what somebody sees on screen.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from objectstore_helpers import BUCKET, object_store_client, object_store_server

from careloop.api.demo import DEMO_SCENARIOS
from careloop.tools.documents import default_document_store
from careloop.tools.idempotency import idempotency_key
from careloop.tools.objectstore import DOCUMENT_METADATA, PORTAL_METADATA, ObjectStoreClient
from objectstore.store import ObjectStore

DOCUMENT = "doc-0005-receipt"
PORTAL = "PORTAL-NORTHGATE-RECORDS"
UPLOAD = {"portal_id": PORTAL, "document_id": DOCUMENT}
METADATA = {DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL}


@pytest.fixture
def endpoint() -> Iterator[tuple[str, ObjectStore]]:
    held = ObjectStore()
    with object_store_server(held) as (url, world):
        yield url, world


def client_for(endpoint_url: str, session: str) -> ObjectStoreClient:
    return ObjectStoreClient(BUCKET, session=session, client=object_store_client(endpoint_url))


def file_one(client: ObjectStoreClient, case_id: str) -> str:
    """File the receipt for ``case_id`` exactly as the provider handler would."""
    key = client.object_key(idempotency_key(case_id, "upload_document", UPLOAD))
    client.put_once(
        object_key=key, body=default_document_store().content(DOCUMENT), metadata=METADATA
    )
    return key


class TestTheScenarioExists:
    def test_records_is_one_of_the_scenarios(self) -> None:
        assert "records" in DEMO_SCENARIOS

    def test_it_delivers_the_claim_rejection(self) -> None:
        assert DEMO_SCENARIOS["records"].event_id == "evt-0003-claim-rejection"

    def test_it_has_its_own_case(self) -> None:
        """Fixed, so pressing twice reopens one case rather than accumulating them."""
        assert DEMO_SCENARIOS["records"].case_id == "demo-records"

    def test_it_arms_nothing(self) -> None:
        """The upload demo is the ordinary path. Only ``recovery`` arms a fault."""
        assert not DEMO_SCENARIOS["records"].loses_the_booking_response

    def test_no_two_scenarios_agree_on_everything_a_judge_can_perceive(self) -> None:
        """``test_api_demo.py`` owns this invariant; restated here for the scenario just added."""
        perceptible = [(scenario.event_id, scenario.title) for scenario in DEMO_SCENARIOS.values()]
        assert len(set(perceptible)) == len(perceptible)


class TestOneVisitorsObjectsAreTheirOwn:
    def test_two_visitors_pressing_the_same_button_file_separately(self, endpoint) -> None:
        """The frozen key is byte-identical across them; the namespace is what separates them."""
        url, held = endpoint
        case_id = DEMO_SCENARIOS["records"].case_id

        first = file_one(client_for(url, "sess-a"), case_id)
        second = file_one(client_for(url, "sess-b"), case_id)

        assert first != second
        assert len(held) == 2

    def test_the_frozen_key_really_is_the_same_for_both(self) -> None:
        """Which is what makes the previous test about the namespace rather than about luck."""
        case_id = DEMO_SCENARIOS["records"].case_id
        key = idempotency_key(case_id, "upload_document", UPLOAD)

        assert client_for("http://unused", "sess-a").object_key(key).endswith(key)
        assert client_for("http://unused", "sess-b").object_key(key).endswith(key)


class TestAResetClearsThisWorldsObjects:
    def test_it_removes_what_this_visitor_filed(self, endpoint) -> None:
        url, held = endpoint
        mine = client_for(url, "sess-a")
        file_one(mine, "demo-records")

        assert mine.remove_session_objects() == 1
        assert len(held) == 0

    def test_it_leaves_another_visitors_objects_alone(self, endpoint) -> None:
        url, held = endpoint
        mine, theirs = client_for(url, "sess-a"), client_for(url, "sess-b")
        file_one(mine, "demo-records")
        their_key = file_one(theirs, "demo-records")

        mine.remove_session_objects()

        assert their_key in held
        assert len(held) == 1

    def test_the_scenario_can_be_pressed_again_afterwards(self, endpoint) -> None:
        """**The property the reset exists for.**

        Without the clear, the second press addresses the object the first one made, the
        create-once precondition refuses the write, and the judge watches a reconciliation settle an
        old object instead of a filing. Measured here rather than argued: the second
        ``put_once`` reports that it *created*.
        """
        url, _held = endpoint
        visitor = client_for(url, "sess-a")
        key = visitor.object_key(idempotency_key("demo-records", "upload_document", UPLOAD))
        body = default_document_store().content(DOCUMENT)

        _first, created_first = visitor.put_once(object_key=key, body=body, metadata=METADATA)
        visitor.remove_session_objects()
        _second, created_second = visitor.put_once(object_key=key, body=body, metadata=METADATA)

        assert created_first
        assert created_second, "the reset did not clear the namespace, so this was a replay"

    def test_without_the_reset_the_second_press_is_a_replay(self, endpoint) -> None:
        """The vacuity half: the same sequence without the clear really does replay instead."""
        url, _held = endpoint
        visitor = client_for(url, "sess-a")
        key = visitor.object_key(idempotency_key("demo-records", "upload_document", UPLOAD))
        body = default_document_store().content(DOCUMENT)

        _first, created_first = visitor.put_once(object_key=key, body=body, metadata=METADATA)
        _second, created_second = visitor.put_once(object_key=key, body=body, metadata=METADATA)

        assert created_first
        assert not created_second


class TestTheWorldReportsWhatItCleared:
    def test_a_world_with_no_object_store_reports_nothing_rather_than_zero(self) -> None:
        """``null`` and ``0`` are different facts: no store at all, versus a store that was empty.

        The same distinction ``clinic_world_ended`` already draws, and ``verify_deploy`` reads this
        field for the same reason it reads that one — it is where the coupling between CareLoop and
        a provider is observable from outside the process.
        """
        from careloop.api.deps import ApiDeps
        from careloop.state import InMemoryCaseStore

        deps = ApiDeps(store=InMemoryCaseStore())

        assert deps.objects is None
