"""The provider committed, the answer was lost, and CareLoop converged on one object.

The upload's counterpart to ``tests/test_saga_chaos.py``, and what
``RECOVERY_CLASS["upload_document"] = IDEMPOTENT_REPLAYABLE`` is worth. The classification is a
claim about a measured provider contract; this is the claim that CareLoop actually behaves the way
the contract permits.

**The truthful sentence, and the tempting false one.** CareLoop issues a **second real
``PutObject``** during reconciliation — under the identity it froze before the first one left the
process. Amazon S3 answers ``412 PreconditionFailed`` because the key is taken, CareLoop reads the
object back, finds its own bytes, and settles. So *"CareLoop did not send the upload twice"* is
false. What is true is that **one object exists, one logical action exists, and nothing was
overwritten** — and the difference between those two sentences is the whole design.

**Why a lost answer rather than a killed process.** ``tests/test_saga_chaos.py`` earns *"a process
can die mid-effect and a later one converges"* with a real child process and a real SQLite file.
This earns the other half — *"nothing had to die"* — through the ambiguity that a create-once store
makes recoverable: a write that landed with its answer discarded is indistinguishable, from
CareLoop's side, from one that never left.

**No model is consulted.** The planner and triage seams raise if touched, and the arguments come
from the durable ``PLAN_PROPOSED`` entry the preparation was derived from.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from helpers import FIXED_NOW
from objectstore_helpers import BUCKET, object_store_client, object_store_server
from saga_helpers import (
    ExplodingPlanner,
    ExplodingTriage,
    executing_with_a_prepared_action,
)

from careloop.graph.providers import object_store_provider
from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass, may_replay
from careloop.graph.runner import context_for, run_case
from careloop.graph.trusted import configured
from careloop.graph.verify import ObservationOutcome, rehydrate
from careloop.provenance.facts import Fact, Provenance
from careloop.provenance.trust import TrustLevel
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.content import content_digest
from careloop.tools.documents import default_document_store
from careloop.tools.idempotency import idempotency_key
from careloop.tools.objectstore import (
    DOCUMENT_METADATA,
    PORTAL_METADATA,
    ObjectIdentityConflict,
    ObjectStoreClient,
    ObjectStoreUnavailable,
)
from objectstore.store import ObjectStore, StoredObject

CASE_ID = "case-0001"
SESSION = "sess-0001"
PORTAL = configured("portal_id") or ""
DOCUMENT = "doc-0005-receipt"
UPLOAD = {"portal_id": PORTAL, "document_id": DOCUMENT}
KEY = idempotency_key(CASE_ID, "upload_document", UPLOAD)


def upload_facts() -> tuple[Fact, ...]:
    """What the case must hold for the upload to have been authorised in the first place."""
    return (
        Fact(
            key="document_id",
            value=DOCUMENT,
            trust_level=TrustLevel.CONTROLLED,
            provenance=Provenance(source_doc="claim-rejection.pdf", extractor="test"),
            extracted_at=FIXED_NOW,
        ),
    )


def held_bytes() -> bytes:
    return default_document_store().content(DOCUMENT)


class LosesTheAnswer:
    """A boto3 client whose next ``put_object`` really happens and then reports nothing.

    **The inner call goes first, always.** The loss is injected only after the store has answered,
    so an ambiguity can never be manufactured over a write that did not land — the same rule
    ``careloop.api.faults.LostResponseTransport`` follows for the clinic, and for the same reason:
    a fault that fired before the request would prove something much weaker.

    One-shot. It consumes its own arming, so the reconciliation's ``PutObject`` reaches the store
    normally and the recovery is the shipped code path rather than a second contrivance.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.armed = True
        self.puts = 0

    def put_object(self, **kwargs: Any) -> Any:
        self.puts += 1
        answer = self._inner.put_object(**kwargs)
        if self.armed:
            self.armed = False
            raise ObjectStoreUnavailable(operation="put_object", code="ConnectionClosed")
        return answer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def world() -> Iterator[tuple[ObjectStore, LosesTheAnswer, ObjectStoreClient]]:
    """A substitute on a socket, a client into it, and a switch that discards one answer."""
    held = ObjectStore()
    with object_store_server(held) as (endpoint, store):
        transport = LosesTheAnswer(object_store_client(endpoint))
        yield store, transport, ObjectStoreClient(BUCKET, session=SESSION, client=transport)


def run(store, client: ObjectStoreClient):
    """One graph run over the durable case, with both model seams primed to raise."""
    from saga_helpers import SAGA_EVENT

    context = context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        now=lambda: FIXED_NOW,
        providers=(object_store_provider(client),),
    )
    asyncio.run(run_case(context))
    return store.get(CASE_ID), context


def run_expecting_a_loud_failure(store, client: ObjectStoreClient):
    """Drive a run whose dispatch raises out of the node, and return the persisted case.

    An :class:`~careloop.tools.objectstore.ObjectIdentityConflict` is CareLoop being wrong about its
    own identity rather than an outcome nobody established, so it is deliberately **not** translated
    into recovery vocabulary: it leaves the node, ``run_case`` marks the case ``FAILED`` and
    re-raises. Catching it here is what lets the test assert on the durable consequences of that,
    which are the interesting half.
    """
    with pytest.raises(ObjectIdentityConflict):
        run(store, client)
    return store.get(CASE_ID)


def stranded(store) -> None:
    executing_with_a_prepared_action(
        store, tool_name="upload_document", args=UPLOAD, case_id=CASE_ID, facts=upload_facts()
    )


class TestTheClassificationIsDeclared:
    def test_the_upload_is_replayable(self) -> None:
        assert RECOVERY_CLASS["upload_document"] is RecoveryClass.IDEMPOTENT_REPLAYABLE

    def test_a_replay_still_needs_an_observer(self) -> None:
        """The class alone is not enough. A replay nothing can read certifies on its own receipt."""
        assert may_replay("upload_document", observers={"upload_document"})
        assert not may_replay("upload_document", observers=frozenset())


class TestALostAnswerConvergesOnOneObject:
    """The whole property, asserted one fact at a time against the store and the durable trail."""

    def test_the_first_attempt_leaves_the_case_waiting(self, world) -> None:
        held, transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        case, _context = run(store, client)

        assert case.state is CaseState.WAITING_EXTERNAL
        assert case.actions == ()
        assert len(held) == 1, "the write landed; only the answer was lost"
        assert transport.puts == 1

    def test_a_later_run_settles_it(self, world) -> None:
        _held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        case, _context = run(store, client)

        assert case.state is CaseState.DONE

    def test_exactly_one_object_exists_afterwards(self, world) -> None:
        held, transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        run(store, client)

        assert len(held) == 1
        assert transport.puts == 2, "the replay is a real request, which is why the count is two"

    def test_the_object_still_holds_the_bytes_the_first_attempt_wrote(self, world) -> None:
        """Nothing was overwritten. The create-once precondition is what guarantees it."""
        held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        run(store, client)

        (key,) = held.keys_under("")
        assert held.get(key).body == held_bytes()

    def test_one_logical_action_under_the_frozen_identity(self, world) -> None:
        _held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        case, _context = run(store, client)

        assert len(case.actions) == 1
        assert case.actions[0].idempotency_key == KEY

    def test_the_object_is_named_by_that_same_identity(self, world) -> None:
        held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        run(store, client)

        (key,) = held.keys_under("")
        assert key == client.object_key(KEY)

    def test_the_recovery_re_claims_the_prepared_attempt(self, world) -> None:
        """``WAITING_EXTERNAL`` → ``EXECUTING`` records that the frozen attempt was re-claimed."""
        _held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        case, _context = run(store, client)

        spine = [
            (entry.payload.get("from"), entry.payload.get("to"))
            for entry in case.timeline
            if entry.kind is TimelineKind.STATE_TRANSITION
        ]
        assert ("EXECUTING", "WAITING_EXTERNAL") in spine
        assert ("WAITING_EXTERNAL", "EXECUTING") in spine

    def test_the_settlement_read_matched(self, world) -> None:
        _held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        case, _context = run(store, client)

        (passed,) = [
            entry for entry in case.timeline if entry.kind is TimelineKind.VERIFICATION_PASSED
        ]
        assert passed.payload["observation"] == ObservationOutcome.MATCHED
        assert passed.payload["read_back"] == "true"

    def test_the_recorded_result_carries_the_stores_own_content(self, world) -> None:
        _held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        case, _context = run(store, client)

        result = rehydrate("upload_document", case.actions[0].result)
        assert result.content_sha256 == content_digest(held_bytes())
        assert result.document_id == DOCUMENT
        assert result.portal_id == PORTAL

    def test_no_model_was_consulted(self, world) -> None:
        """Both seams raise if touched, so reaching ``DONE`` at all is the assertion.

        Counted as well as primed, because a seam that was never constructed proves nothing about a
        seam that was never called.
        """
        _held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        case, _context = run(store, client)

        assert case.state is CaseState.DONE
        assert context_calls(case) == 1, "a second plan was proposed during recovery"


class TestDivergentExistingContentNeverSucceeds:
    """Same key, something else at it. No retry resolves that and none is attempted."""

    def test_the_case_does_not_reach_done(self, world) -> None:
        held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        (key,) = held.keys_under("")
        held.put(
            key,
            StoredObject(
                body=b"%PDF-1.4 somebody else's document",
                metadata={DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL},
            ),
            only_if_absent=False,
        )

        case = run_expecting_a_loud_failure(store, client)

        assert case.state is not CaseState.DONE
        assert case.state is CaseState.FAILED
        assert case.actions == (), "an action was recorded for an object CareLoop did not write"

    def test_the_divergent_object_is_left_exactly_as_it_was(self, world) -> None:
        """CareLoop refuses rather than clobbering whatever is there."""
        held, _transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        (key,) = held.keys_under("")
        intruder = b"%PDF-1.4 somebody else's document"
        held.put(key, StoredObject(body=intruder), only_if_absent=False)

        run_expecting_a_loud_failure(store, client)

        assert held.get(key).body == intruder
        assert len(held) == 1


class TestAbsenceIsNeverALicenceToUploadAgain:
    """``may_replay`` has no parameter for an outcome, and this is the behavioural half."""

    def test_a_vanished_object_does_not_cause_a_second_write(self, world) -> None:
        held, transport, client = world
        store = InMemoryCaseStore()
        stranded(store)

        run(store, client)
        run(store, client)  # settles; the case is DONE with one object
        writes_after_settlement = transport.puts

        (key,) = held.keys_under("")
        held.delete(key)
        run(store, client)

        assert transport.puts == writes_after_settlement, "absence was read as a licence to re-send"
        assert len(held) == 0


def context_calls(case) -> int:
    """How many proposals the durable trail records. One means recovery invented nothing."""
    return len([entry for entry in case.timeline if entry.kind is TimelineKind.PLAN_PROPOSED])
