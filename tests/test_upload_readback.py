"""An upload is verified against the store's own bytes, fetched separately.

The second effect's counterpart to ``tests/test_verify_readback.py``, and the file that decides
whether "genuinely external" is a claim CareLoop has earned twice or once.

**What makes this different from booking's proof.** A clinic hands back a record whose fields
CareLoop compares against the arguments it sent; every value on both sides is a string somebody
transmitted. An object store hands back *bytes*, and what is compared is a digest this process
computed over them against a digest this process computed over the document it holds. Neither side
of that equality is a value the provider asserted about itself, which is why
:class:`TestTheWriteReceiptCannotSettleIt` can move the object behind CareLoop's back and require
the verdict to change while every field the store reports still agrees.

:class:`TestTheIndependenceProof` is the vacuity proof for the whole deliverable, exactly as its
namesake is for the clinic. Everything else here could pass while the read-back was quietly a copy
of what the write returned; that class is what fails if it ever becomes one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from helpers import FIXED_NOW
from objectstore_helpers import BUCKET, object_store_client, object_store_server

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
    proposal_tool_call_for,
)
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import CLAIM_REJECTION_EVENT_ID, EVENTS
from careloop.graph.providers import object_store_provider
from careloop.graph.runner import context_for, open_case, run_case
from careloop.graph.verify import (
    INDEPENDENTLY_OBSERVABLE,
    ObservationOutcome,
    verify_action,
)
from careloop.llm.mock import mock_model
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.case import ActionRecord
from careloop.tools.content import content_digest
from careloop.tools.document_source import ATTACKER_ADDRESS
from careloop.tools.documents import default_document_store
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.objectstore import DOCUMENT_METADATA, PORTAL_METADATA, ObjectStoreClient
from careloop.tools.results import DocumentUploaded
from objectstore.store import ObjectStore, StoredObject

CASE_ID = "case-0001"
SESSION = "sess-0001"
PORTAL = "PORTAL-NORTHGATE-RECORDS"
DOCUMENT = "doc-0005-receipt"
UPLOAD = {"portal_id": PORTAL, "document_id": DOCUMENT}
KEY = idempotency_key(CASE_ID, "upload_document", UPLOAD)

EVENT = EVENTS.get(CLAIM_REJECTION_EVENT_ID)


def held_bytes() -> bytes:
    return default_document_store().content(DOCUMENT)


# -- driving one run ----------------------------------------------------------------------------


def triage_agent() -> TriageAgent:
    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.BILLING,
                    urgency=Urgency.SOON,
                    summary="A rejected claim asks for the receipt to be supplied.",
                    patient_id="PT-4417",
                )
            ]
        )
    )


def planner_upload(repeats: int = 3) -> PlannerAgent:
    """A planner that proposes filing the receipt the rejection demands.

    ``repeats`` above one deliberately: the mock raises rather than inventing an answer when its
    script runs out, so a script sized to the fixed behaviour would report ``MockScriptExhausted``
    in front of any regression that planned again — a confusing failure standing where the real one
    should be.
    """
    return PlannerAgent(
        mock_model(
            proposals=[
                proposal_tool_call_for(
                    PlannerIntent(
                        tool_name="upload_document",
                        rationale="the rejection asks for the receipt",
                        args=UPLOAD,
                    )
                )
            ]
            * repeats
        )
    )


@pytest.fixture
def store_service() -> Iterator[tuple[ObjectStore, ObjectStoreClient]]:
    """A substitute on a loopback socket, the objects it holds, and a client into it."""
    held = ObjectStore()
    with object_store_server(held) as (endpoint, world):
        yield (
            world,
            ObjectStoreClient(BUCKET, session=SESSION, client=object_store_client(endpoint)),
        )


def file_the_receipt(client: ObjectStoreClient | None = None, *, max_node_executions: int = 24):
    """Open the claim-rejection case and run it to rest, filing through ``client`` if given."""
    store = InMemoryCaseStore()
    open_case(store, EVENT, case_id=CASE_ID, now=FIXED_NOW)
    context = context_for(
        store,
        EVENT,
        case_id=CASE_ID,
        triage=triage_agent(),
        planner=planner_upload(),
        now=lambda: FIXED_NOW,
        providers=() if client is None else (object_store_provider(client),),
    )
    asyncio.run(run_case(context, max_node_executions=max_node_executions))
    return store.get(CASE_ID), context


def kinds(case) -> list[TimelineKind]:
    return [entry.kind for entry in case.timeline]


# -- T1: the ordinary path ------------------------------------------------------------------------


class TestARealFilingIsIndependentlyVerified:
    def test_the_case_reaches_done(self, store_service) -> None:
        _held, client = store_service

        case, _context = file_the_receipt(client)

        assert case.state is CaseState.DONE

    def test_exactly_one_object_exists(self, store_service) -> None:
        held, client = store_service

        file_the_receipt(client)

        assert len(held) == 1

    def test_the_object_holds_the_document_careloop_was_authorised_to_send(
        self, store_service
    ) -> None:
        held, client = store_service

        file_the_receipt(client)

        (key,) = held.keys_under("")
        assert held.get(key).body == held_bytes()

    def test_the_object_is_named_by_the_frozen_action_identity(self, store_service) -> None:
        held, client = store_service

        file_the_receipt(client)

        (key,) = held.keys_under("")
        assert key == client.object_key(KEY)

    def test_the_metadata_is_exactly_the_two_declared_arguments(self, store_service) -> None:
        held, client = store_service

        file_the_receipt(client)

        (key,) = held.keys_under("")
        assert dict(held.get(key).metadata) == {
            DOCUMENT_METADATA: DOCUMENT,
            PORTAL_METADATA: PORTAL,
        }

    def test_the_verification_records_that_it_read_the_store(self, store_service) -> None:
        _held, client = store_service

        case, _context = file_the_receipt(client)

        passed = [e for e in case.timeline if e.kind is TimelineKind.VERIFICATION_PASSED]
        assert len(passed) == 1
        assert passed[0].payload["read_back"] == "true"
        assert passed[0].payload["observation"] == ObservationOutcome.MATCHED

    def test_one_action_was_recorded(self, store_service) -> None:
        _held, client = store_service

        case, _context = file_the_receipt(client)

        assert len(case.actions) == 1
        assert case.actions[0].idempotency_key == KEY

    def test_the_upload_is_declared_independently_observable(self) -> None:
        assert "upload_document" in INDEPENDENTLY_OBSERVABLE


# -- T4 / the independence proof ------------------------------------------------------------------


class TestTheIndependenceProof:
    """Move the object behind CareLoop's back; verification must refuse.

    **The vacuity proof for the whole deliverable.** If the read-back ever became a copy of what
    the write returned, every other test in this file would still pass and this one would not.
    """

    def test_replaced_content_is_not_verified(self, store_service) -> None:
        held, client = store_service

        # File normally, then substitute the bytes the store holds. Nothing CareLoop recorded
        # changes; only the world does.
        case, _context = file_the_receipt(client)
        assert case.state is CaseState.DONE  # the run itself succeeded

        (key,) = held.keys_under("")
        held.put(
            key,
            StoredObject(
                body=b"%PDF-1.4 a different document entirely",
                metadata={DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL},
            ),
            only_if_absent=False,
        )

        observed = client.fetch(key)
        recorded = DocumentUploaded(
            object_key=key,
            object_id=KEY,
            portal_id=PORTAL,
            document_id=DOCUMENT,
            content_sha256=content_digest(held_bytes()),
        )
        verification = verify_action(
            case_id=CASE_ID,
            action_type="upload_document",
            args=UPLOAD,
            record=_record(),
            result=recorded,
            observed=_uploaded_from(observed),
        )

        assert verification.failed
        assert verification.outcome is ObservationOutcome.DIVERGED

    def test_the_closed_loop_checks_still_pass_while_it_fails(self, store_service) -> None:
        """The half that makes the previous test mean something.

        The key re-derives, the digest matches, and every field CareLoop recorded is exactly what
        policy authorised. Only the *world* disagrees, which is precisely the class of failure a
        closed-loop verification cannot see.
        """
        held, client = store_service
        case, _context = file_the_receipt(client)
        (key,) = held.keys_under("")

        recorded = DocumentUploaded(
            object_key=key,
            object_id=KEY,
            portal_id=PORTAL,
            document_id=DOCUMENT,
            content_sha256=content_digest(held_bytes()),
        )
        closed_loop = verify_action(
            case_id=CASE_ID,
            action_type="upload_document",
            args=UPLOAD,
            record=_record(),
            result=recorded,
        )

        assert closed_loop.passed
        assert closed_loop.outcome is ObservationOutcome.NOT_OBSERVED
        assert case.state is CaseState.DONE

    def test_replaced_metadata_is_not_verified(self, store_service) -> None:
        """A store that kept the bytes and rewrote where they were filed for."""
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        held.put(
            key,
            StoredObject(
                body=held_bytes(),
                metadata={DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: "PORTAL-SOMEWHERE-ELSE"},
            ),
            only_if_absent=False,
        )

        verification = _verify_against(client, key)

        assert verification.failed
        assert verification.outcome is ObservationOutcome.DIVERGED

    def test_a_different_held_document_is_not_verified(self, store_service) -> None:
        """The store holds a perfectly valid CareLoop document — just not this one."""
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        held.put(
            key,
            StoredObject(
                body=default_document_store().content("doc-0001-referral"),
                metadata={DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL},
            ),
            only_if_absent=False,
        )

        verification = _verify_against(client, key)

        assert verification.failed
        assert verification.outcome is ObservationOutcome.DIVERGED


class TestTheWriteReceiptCannotSettleIt:
    """What the store *says* about the object is not what decides the verdict.

    The substitute reports a checksum on every read, computed over what it is holding. This class
    replaces the bytes and requires the verdict to follow the bytes rather than any field the
    provider populated — which is the difference between reading the world back and believing a
    receipt.
    """

    def test_the_observed_digest_follows_the_bytes(self, store_service) -> None:
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        held.put(key, StoredObject(body=b"different"), only_if_absent=False)

        assert client.fetch(key).content_sha256 == content_digest(b"different")

    def test_a_successful_write_does_not_make_a_later_divergence_verify(
        self, store_service
    ) -> None:
        """The write really did succeed. The case still may not be certified afterwards."""
        held, client = store_service
        case, _context = file_the_receipt(client)
        assert case.state is CaseState.DONE

        (key,) = held.keys_under("")
        held.put(
            key,
            StoredObject(
                body=b"tampered",
                metadata={DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL},
            ),
            only_if_absent=False,
        )

        assert _verify_against(client, key).failed


# -- T5: absence ----------------------------------------------------------------------------------


class TestAbsenceIsADefiniteAnswer:
    def test_a_deleted_object_makes_verification_fail_as_absent(self, store_service) -> None:
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")
        held.delete(key)

        verification = _verify_against(client, key)

        assert verification.failed
        assert verification.outcome is ObservationOutcome.ABSENT

    def test_absence_is_reported_as_an_attempted_observation(self, store_service) -> None:
        """A read was made and answered. ``externally_observed`` means attempted, not succeeded."""
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")
        held.delete(key)

        assert _verify_against(client, key).externally_observed


# -- T3: the observer could not be reached ---------------------------------------------------------


class TestAnUnreachableStoreCertifiesNothing:
    def test_write_success_is_not_settlement(self, store_service) -> None:
        """Stated as a property rather than as a scenario: the two are different questions."""
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        assert len(held) == 1  # the write happened
        held.delete(key)

        assert _verify_against(client, key).failed  # and settlement still refuses


class TestTheStoreIsUnreachable:
    """The observer cannot be reached at all — a different fact from an absent object."""

    def test_verification_reports_unavailable_rather_than_absent(self) -> None:
        held = ObjectStore()
        with object_store_server(held) as (endpoint, _world):
            client = ObjectStoreClient(
                BUCKET, session=SESSION, client=object_store_client(endpoint)
            )
            case, _context = file_the_receipt(client)
            assert case.state is CaseState.DONE
            (key,) = held.keys_under("")

        # The server is gone; the client is pointed at a port nothing is listening on.
        verification = _verify_against(client, key)

        assert verification.failed
        assert verification.outcome is ObservationOutcome.UNAVAILABLE


# -- T9: nothing a document says reaches the provider ----------------------------------------------


class TestNothingModelAuthoredReachesTheStore:
    """Read off the **store's own record**, not off what CareLoop believes it sent.

    The upload path has no model-authored outbound content at all — the bytes come from the
    document store, the key from the frozen identity, the content type from a literal — so this
    asserts a property rather than exercising a filter.
    """

    CANARIES = (
        ATTACKER_ADDRESS.encode(),
        b"records intake team",
        b"PORTAL-MG-TRANSFER",
        b"records_admin",
    )

    def test_the_object_carries_no_instruction_from_the_poisoned_notice(
        self, store_service
    ) -> None:
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        body = held.get(key).body
        for canary in self.CANARIES:
            assert canary not in body

    def test_the_key_and_metadata_carry_nothing_a_model_composed(self, store_service) -> None:
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        rendered = key + "".join(f"{k}={v}" for k, v in held.get(key).metadata.items())
        for text in ("PT-4417", "Enid", "Marlowe", "MediGuard", CASE_ID):
            assert text not in rendered

    def test_the_only_bytes_filed_are_a_document_careloop_holds(self, store_service) -> None:
        held, client = store_service
        file_the_receipt(client)
        (key,) = held.keys_under("")

        store = default_document_store()
        assert held.get(key).body in {store.content(i) for i in store.ids}


# -- helpers ---------------------------------------------------------------------------------------


def _record() -> ActionRecord:
    return ActionRecord(
        action_type="upload_document",
        idempotency_key=KEY,
        args_digest=args_digest(UPLOAD),
        result="{}",
    )


def _uploaded_from(observed) -> DocumentUploaded:
    return DocumentUploaded(
        object_key=observed.object_key,
        object_id=observed.object_key.rsplit("/", 1)[-1],
        portal_id=observed.metadata.get(PORTAL_METADATA, ""),
        document_id=observed.metadata.get(DOCUMENT_METADATA, ""),
        content_sha256=observed.content_sha256,
    )


def _verify_against(client: ObjectStoreClient, key: str):
    """Run the *shipped* read-back and verifier against whatever the store now holds."""
    from careloop.graph.providers import object_store_readback
    from careloop.graph.verify import ReadBackFailed, Verification

    recorded = DocumentUploaded(
        object_key=key,
        object_id=KEY,
        portal_id=PORTAL,
        document_id=DOCUMENT,
        content_sha256=content_digest(held_bytes()),
    )
    observe = object_store_readback(client)
    try:
        observed = observe(recorded)
    except ReadBackFailed as failure:
        return Verification(
            passed=False,
            diagnostic=str(failure),
            externally_observed=True,
            outcome=failure.outcome,
        )
    return verify_action(
        case_id=CASE_ID,
        action_type="upload_document",
        args=UPLOAD,
        record=_record(),
        result=recorded,
        observed=observed,
    )
