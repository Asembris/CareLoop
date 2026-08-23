"""The object-store client, against a real socket and a real boto3 client.

Every test here drives :class:`~careloop.tools.objectstore.ObjectStoreClient` through HTTP to the
substitute in ``objectstore/``, whose behaviours are held to the measured S3 contract by
``tests/test_objectstore_contract.py``. Nothing is stubbed: the requests are signed, the errors are
parsed out of XML, and the bytes travel.

Three properties this file owns, and they are the three a second provider has to earn:

**The key is the durable action identity.** Not the content, not a name, not anything a model
supplied. A key derived from content could not be computed before the content was read back, and
the frozen-identity convergence the saga depends on would have nothing to aim at.

**A ``412`` is settled by looking, not by assuming.** The client answers a refused conditional
create with a fresh read and a content comparison, so a replay converges on the object the first
attempt made — and a key holding something else fails closed instead of being reported as success.

**Nothing leaks.** Keys carry no patient data; metadata is two deterministic fields; exceptions
carry an operation and a provider error code and no third-party text at all.
"""

from __future__ import annotations

import pytest
from objectstore_helpers import BUCKET, object_store_client, object_store_server

from careloop.tools.content import content_digest
from careloop.tools.documents import default_document_store
from careloop.tools.idempotency import idempotency_key
from careloop.tools.objectstore import (
    DOCUMENT_METADATA,
    PORTAL_METADATA,
    ObjectIdentityConflict,
    ObjectNotFound,
    ObjectStoreClient,
    ObjectStoreRefused,
    ObjectStoreUnavailable,
)
from careloop.tools.provider import ProviderRefused, ProviderUnavailable
from objectstore.store import ObjectStore, StoredObject

CASE_ID = "case-0001"
PORTAL = "PORTAL-NORTHGATE-RECORDS"
DOCUMENT = "doc-0005-receipt"
ARGS = {"portal_id": PORTAL, "document_id": DOCUMENT}
SESSION = "sess-0001"

METADATA = {DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL}


def held_bytes() -> bytes:
    return default_document_store().content(DOCUMENT)


def frozen_key() -> str:
    return idempotency_key(CASE_ID, "upload_document", ARGS)


@pytest.fixture
def store_and_client():
    """A running substitute, the store behind it, and a client pointed at it."""
    store = ObjectStore()
    with object_store_server(store) as (endpoint, held):
        yield held, ObjectStoreClient(BUCKET, session=SESSION, client=object_store_client(endpoint))


class TestTheKeyIsTheDurableActionIdentity:
    def test_the_key_ends_with_the_frozen_idempotency_key(self) -> None:
        client = ObjectStoreClient(BUCKET, session=SESSION, client=object())

        assert client.object_key(frozen_key()).endswith(f"/{frozen_key()}")

    def test_the_key_is_stable_for_one_action(self) -> None:
        """Two derivations of one action address one object. There is no second answer."""
        client = ObjectStoreClient(BUCKET, session=SESSION, client=object())

        assert client.object_key(frozen_key()) == client.object_key(frozen_key())

    def test_the_key_does_not_depend_on_the_content(self) -> None:
        """A content-derived address could not be computed before the content was read back."""
        client = ObjectStoreClient(BUCKET, session=SESSION, client=object())

        assert content_digest(held_bytes()) not in client.object_key(frozen_key())

    def test_two_sessions_running_one_scenario_do_not_collide(self) -> None:
        """The frozen key is byte-identical across visitors; the namespace is what separates them.

        ``idempotency_key`` covers the case, the action type and the identifying arguments — all of
        which a demo scenario fixes — so without the session segment two visitors pressing the same
        button would address one object.
        """
        first = ObjectStoreClient(BUCKET, session="sess-a", client=object())
        second = ObjectStoreClient(BUCKET, session="sess-b", client=object())

        assert first.object_key(frozen_key()) != second.object_key(frozen_key())

    def test_a_client_cannot_express_another_sessions_namespace(self) -> None:
        """Which is why a reset cannot reach one. There is no parameter for somebody else's."""
        client = ObjectStoreClient(BUCKET, session="sess-a", client=object())

        assert client.session_prefix.endswith("/sess-a")
        assert client.object_key(frozen_key()).startswith(f"{client.session_prefix}/")

    @pytest.mark.parametrize("secret", ["PT-4417", "Enid", "Marlowe", "receipt.pdf", CASE_ID])
    def test_no_patient_or_case_data_reaches_the_key(self, secret: str) -> None:
        client = ObjectStoreClient(BUCKET, session=SESSION, client=object())

        assert secret not in client.object_key(frozen_key())


class TestTheFirstWriteCreates:
    def test_it_creates_and_reports_that_it_did(self, store_and_client) -> None:
        _held, client = store_and_client
        key = client.object_key(frozen_key())

        filed, created = client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        assert created
        assert filed.object_key == key
        assert filed.content_sha256 == content_digest(held_bytes())

    def test_a_fresh_read_returns_the_bytes_and_the_metadata(self, store_and_client) -> None:
        _held, client = store_and_client
        key = client.object_key(frozen_key())
        client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        observed = client.fetch(key)

        assert observed.body == held_bytes()
        assert observed.metadata[DOCUMENT_METADATA] == DOCUMENT
        assert observed.metadata[PORTAL_METADATA] == PORTAL

    def test_the_observed_digest_is_computed_from_what_came_back(self, store_and_client) -> None:
        """Never read from what the provider says about the object. The independence half."""
        held, client = store_and_client
        key = client.object_key(frozen_key())
        client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        held.put(key, StoredObject(body=b"tampered", metadata=METADATA), only_if_absent=False)

        assert client.fetch(key).content_sha256 == content_digest(b"tampered")


class TestAReplayConvergesOnOneObject:
    def test_the_same_key_and_the_same_bytes_settle_against_what_is_there(
        self, store_and_client
    ) -> None:
        """The reconciliation shape: refused, read, and found to be CareLoop's own object."""
        held, client = store_and_client
        key = client.object_key(frozen_key())

        first, created_first = client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)
        second, created_second = client.put_once(
            object_key=key, body=held_bytes(), metadata=METADATA
        )

        assert created_first
        assert not created_second
        assert second.content_sha256 == first.content_sha256
        assert len(held) == 1

    def test_the_replayed_result_is_byte_identical(self, store_and_client) -> None:
        _held, client = store_and_client
        key = client.object_key(frozen_key())

        first, _ = client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)
        second, _ = client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        assert second.body == first.body
        assert second.object_key == first.object_key

    def test_the_same_key_with_different_bytes_fails_closed(self, store_and_client) -> None:
        """CareLoop's frozen identity naming an object it did not write is a contradiction."""
        held, client = store_and_client
        key = client.object_key(frozen_key())
        client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        with pytest.raises(ObjectIdentityConflict):
            client.put_once(object_key=key, body=b"something else", metadata=METADATA)

        assert held.get(key).body == held_bytes()

    def test_an_externally_replaced_object_is_a_conflict_rather_than_an_overwrite(
        self, store_and_client
    ) -> None:
        """Somebody else took the key. CareLoop refuses rather than clobbering them."""
        held, client = store_and_client
        key = client.object_key(frozen_key())
        held.put(key, StoredObject(body=b"not ours"), only_if_absent=False)

        with pytest.raises(ObjectIdentityConflict):
            client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        assert held.get(key).body == b"not ours"


class TestAbsence:
    def test_a_missing_key_is_a_definite_answer(self, store_and_client) -> None:
        _held, client = store_and_client

        with pytest.raises(ObjectNotFound):
            client.fetch(client.object_key(frozen_key()))

    def test_a_deleted_object_is_absent_rather_than_unavailable(self, store_and_client) -> None:
        held, client = store_and_client
        key = client.object_key(frozen_key())
        client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)
        held.delete(key)

        with pytest.raises(ObjectNotFound):
            client.fetch(key)

    def test_absence_is_a_refusal_rather_than_an_unavailability(self) -> None:
        """The store *answered*. Nothing about this says a later attempt would fare better."""
        assert issubclass(ObjectNotFound, ObjectStoreRefused)
        assert not issubclass(ObjectNotFound, ObjectStoreUnavailable)


class TestTheExceptionsCarryNothingOfTheProvidersOwn:
    """``ClinicRefused`` records what it cost to learn this: an exception escaping a node is copied
    verbatim into span telemetry, so anything interpolated here is text a third party chose."""

    def test_unreachable_names_the_operation_and_a_code_only(self) -> None:
        failure = ObjectStoreUnavailable(operation="get_object", code="EndpointConnectionError")

        assert failure.operation == "get_object"
        assert failure.code == "EndpointConnectionError"

    @pytest.mark.parametrize(
        "secret", [BUCKET, "PT-4417", "careloop/sess-0001", "https://s3.amazonaws.com"]
    )
    def test_no_bucket_key_or_endpoint_appears_in_the_message(self, secret: str) -> None:
        for failure in (
            ObjectStoreUnavailable(operation="put_object", code="Timeout"),
            ObjectStoreRefused(operation="put_object", code="AccessDenied"),
            ObjectNotFound(operation="get_object", code="NoSuchKey"),
        ):
            assert secret not in str(failure)

    def test_a_real_refusal_from_the_provider_carries_no_provider_prose(
        self, store_and_client
    ) -> None:
        """Driven through the substitute, so the message is whatever a real refusal produces."""
        _held, client = store_and_client

        with pytest.raises(ObjectNotFound) as raised:
            client.fetch(client.object_key(frozen_key()))

        assert str(raised.value) == "the object store refused get_object (NoSuchKey)"

    def test_both_kinds_are_provider_neutral_so_recovery_can_translate_them(self) -> None:
        """Which is what makes a lost upload response leave the case waiting rather than failed."""
        assert issubclass(ObjectStoreUnavailable, ProviderUnavailable)
        assert issubclass(ObjectStoreRefused, ProviderRefused)

    def test_an_identity_conflict_is_neither(self) -> None:
        """The store behaved correctly. What is wrong is CareLoop's belief, and it must fail loudly.

        Reported as an unknown outcome it would park the case for a person to resolve by waiting,
        which is not something waiting resolves.
        """
        assert not issubclass(ObjectIdentityConflict, ProviderUnavailable)
        assert not issubclass(ObjectIdentityConflict, ProviderRefused)


class TestResetRemovesOnlyThisSessionsObjects:
    def test_it_removes_what_this_client_filed(self, store_and_client) -> None:
        held, client = store_and_client
        key = client.object_key(frozen_key())
        client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)

        assert client.remove_session_objects() == 1
        assert key not in held

    def test_it_leaves_another_sessions_objects_alone(self, store_and_client) -> None:
        held, client = store_and_client
        mine = client.object_key(frozen_key())
        theirs = mine.replace(f"/{SESSION}/", "/sess-other/")
        client.put_once(object_key=mine, body=held_bytes(), metadata=METADATA)
        held.put(theirs, StoredObject(body=b"theirs"), only_if_absent=True)

        client.remove_session_objects()

        assert theirs in held
        assert mine not in held

    def test_resetting_an_empty_namespace_is_not_an_error(self, store_and_client) -> None:
        _held, client = store_and_client

        assert client.remove_session_objects() == 0


class TestTheDocumentBytesAreWhatCareLoopHolds:
    def test_the_store_hands_back_the_packaged_file(self) -> None:
        content = default_document_store().content(DOCUMENT)

        assert content.startswith(b"%PDF")

    def test_two_reads_are_byte_identical(self) -> None:
        """So a replay transmits exactly what the first attempt did."""
        store = default_document_store()

        assert store.content(DOCUMENT) == store.content(DOCUMENT)

    def test_an_unheld_document_raises_rather_than_returning_nothing(self) -> None:
        from careloop.tools.documents import DocumentNotFound

        with pytest.raises(DocumentNotFound):
            default_document_store().content("doc-9999-elsewhere")
