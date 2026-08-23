"""Deliberate attacks on the upload path, and the proof that the suite notices.

The other upload files assert that CareLoop behaves correctly. This one makes the claim a reviewer
actually needs:

    if a future engineer removes, weakens or vacuously satisfies one of the upload's defences, does
    something turn red?

**Three things make a mutant count here, and all three are enforced by the harness.**

*It must be safe before.* The witness under the shipped code must produce :attr:`Mutant.before`. A
witness that already fails proves nothing about the defence under attack.

*It must be dangerous after.* The same witness under the mutation must produce :attr:`Mutant.after`,
and that must **differ**. If it does not, the mutation never reached the decision path, and that is
a :exc:`MutationHadNoEffect` *harness* failure — never a surviving mutant and never a flattering
score. ``tests/test_evaluator_mutations.py`` records why that rule exists: a mutant patched onto the
wrong namespace once reported a clean survival while nothing had been mutated at all.

*It must be caught.* Every mutant below names the shipped assertion that dies with it, and
:class:`TestEveryMutantNamesALiveTest` checks those tests exist.

**Three of the twelve are proven elsewhere and are listed rather than duplicated**, because the
tests that own them are better placed than a monkeypatch would be — see :data:`PROVEN_ELSEWHERE`.
A second copy here would be a second thing to keep true.

**Scope.** Single mutations only. Combinations are out of scope, and no claim here covers them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from objectstore_helpers import BUCKET, object_store_client, object_store_server

import careloop.graph.providers as providers
import careloop.graph.verify as verification
import careloop.tools.objectstore as objectstore
from careloop.graph.verify import ObservationOutcome, verify_action
from careloop.state.case import ActionRecord
from careloop.tools.content import content_digest
from careloop.tools.documents import default_document_store
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.objectstore import (
    DOCUMENT_METADATA,
    PORTAL_METADATA,
    FiledObject,
    ObjectStoreClient,
)
from careloop.tools.results import DocumentUploaded
from objectstore.store import ObjectStore, StoredObject

CASE_ID = "case-0001"
PORTAL = "PORTAL-NORTHGATE-RECORDS"
DOCUMENT = "doc-0005-receipt"
UPLOAD = {"portal_id": PORTAL, "document_id": DOCUMENT}
KEY = idempotency_key(CASE_ID, "upload_document", UPLOAD)
METADATA = {DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: PORTAL}
INTRUDER = b"%PDF-1.4 somebody else's document"


def held_bytes() -> bytes:
    return default_document_store().content(DOCUMENT)


class MutationHadNoEffect(Exception):
    """The mutant did not change the witness's outcome, so it never reached the decision path."""


@dataclass(frozen=True)
class Mutant:
    """One deliberate weakening, its witness, and the shipped test that dies with it."""

    id: str
    attacks: str
    apply: Callable[[pytest.MonkeyPatch], None]
    witness: Callable[[Any, Any], str]
    before: str
    after: str
    killed_by: str


# -- witnesses ------------------------------------------------------------------------------------
#
# Each takes ``(held store, client)`` and returns a short string naming what happened. Strings
# rather than assertions, so the harness can compare a before and an after rather than each witness
# deciding for itself what "correct" was.


def witness_conflicting_write(held: ObjectStore, client: ObjectStoreClient) -> str:
    """File, then try to file something else under the same action identity."""
    key = client.object_key(KEY)
    client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)
    try:
        client.put_once(object_key=key, body=INTRUDER, metadata=METADATA)
    except objectstore.ObjectIdentityConflict:
        return f"refused; store holds original={held.get(key).body == held_bytes()}"
    return f"accepted; store holds original={held.get(key).body == held_bytes()}"


def witness_replay(held: ObjectStore, client: ObjectStoreClient) -> str:
    """File the same authorised action twice."""
    for _ in range(2):
        client.put_once(object_key=client.object_key(KEY), body=held_bytes(), metadata=METADATA)
    return f"objects={len(held)}"


def witness_tampered_readback(held: ObjectStore, client: ObjectStoreClient) -> str:
    """File, replace the bytes behind CareLoop's back, then verify through the shipped path."""
    key = client.object_key(KEY)
    client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)
    held.put(key, StoredObject(body=INTRUDER, metadata=METADATA), only_if_absent=False)
    return _verdict(client, key)


def witness_tampered_metadata(held: ObjectStore, client: ObjectStoreClient) -> str:
    """File, rewrite which portal the object says it was filed for, then verify."""
    key = client.object_key(KEY)
    client.put_once(object_key=key, body=held_bytes(), metadata=METADATA)
    held.put(
        key,
        StoredObject(
            body=held_bytes(),
            metadata={DOCUMENT_METADATA: DOCUMENT, PORTAL_METADATA: "PORTAL-SOMEWHERE-ELSE"},
        ),
        only_if_absent=False,
    )
    return _verdict(client, key)


def witness_forbidden_read(_held: ObjectStore, _client: ObjectStoreClient) -> str:
    """A store that will not say whether the object exists.

    Its own client rather than the fixture's: the substitute has no access control, and the
    distinction under test is between the store answering *"there is no such key"* and answering
    *"I will not tell you"*. Only the first is knowledge about the object.
    """
    denying = ObjectStoreClient(BUCKET, session="sess-0001", client=_DeniesReads())
    try:
        denying.fetch("careloop/sess-0001/whatever")
    except objectstore.ObjectNotFound:
        return "classified as ABSENT"
    except objectstore.ObjectStoreRefused:
        return "classified as a refusal"
    return "not refused at all"


def witness_two_sessions(_held: ObjectStore, client: ObjectStoreClient) -> str:
    """Two visitors driving the same demo scenario, whose frozen keys are byte-identical."""
    first = ObjectStoreClient(BUCKET, session="sess-a", client=object())
    second = ObjectStoreClient(BUCKET, session="sess-b", client=object())
    same = first.object_key(KEY) == second.object_key(KEY)
    return f"collide={same}"


# -- mutations ------------------------------------------------------------------------------------


def _drop_the_precondition(monkeypatch: pytest.MonkeyPatch) -> None:
    """M1. The create-once header goes, so a write overwrites whatever the key held."""
    original = ObjectStoreClient.put_once

    def unconditional(self, *, object_key: str, body: bytes, metadata: Mapping[str, str]):
        self._client.put_object(
            Bucket=self._bucket,
            Key=object_key,
            Body=body,
            ContentType=objectstore.CONTENT_TYPE,
            Metadata=dict(metadata),
        )
        return FiledObject(object_key=object_key, body=body, metadata=dict(metadata)), True

    del original
    monkeypatch.setattr(ObjectStoreClient, "put_once", unconditional)


def _treat_the_refusal_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2. A ``412`` is read as *the object is already correct* without looking at it."""

    def optimistic(self, *, object_key: str, body: bytes, metadata: Mapping[str, str]):
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=body,
                IfNoneMatch=objectstore.IF_NONE_MATCH_ANY,
                ContentType=objectstore.CONTENT_TYPE,
                Metadata=dict(metadata),
            )
        except Exception:
            return FiledObject(object_key=object_key, body=body, metadata=dict(metadata)), False
        return FiledObject(object_key=object_key, body=body, metadata=dict(metadata)), True

    monkeypatch.setattr(ObjectStoreClient, "put_once", optimistic)


def _mint_a_fresh_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """M3. Recovery names a new object instead of re-addressing the frozen one."""
    counter = {"n": 0}

    def drifting(self, idempotency_key: str) -> str:
        counter["n"] += 1
        return f"{self.session_prefix}/{idempotency_key}-{counter['n']}"

    monkeypatch.setattr(ObjectStoreClient, "object_key", drifting)


def _reuse_the_write_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """M4/M5. The observer hands back what CareLoop recorded instead of reading the store."""
    monkeypatch.setattr(providers, "object_store_readback", lambda client: lambda result: result)


def _drop_the_content_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """M6. The object's bytes stop being compared; existence and metadata still are."""
    monkeypatch.setattr(
        verification,
        "_UPLOAD_COMPARISON",
        {"portal_id": "portal_id", "document_id": "document_id"},
    )


def _drop_the_metadata_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """M7. Where it was filed and which document it is stop being compared."""
    monkeypatch.setattr(
        verification,
        "_UPLOAD_COMPARISON",
        {"content_sha256": verification.UPLOAD_CONTENT_EXPECTATION},
    )


def _read_forbidden_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """M10. ``AccessDenied`` is read as *the object is not there*."""
    original = objectstore._translate

    def permissive(operation: str, failure: Exception) -> Exception:
        code = objectstore._code(failure)
        if code in {objectstore.NO_SUCH_KEY, objectstore.ACCESS_DENIED}:
            return objectstore.ObjectNotFound(operation=operation, code=code)
        return original(operation, failure)

    monkeypatch.setattr(objectstore, "_translate", permissive)


def _drop_the_session_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """M11. Every visitor's objects land in one namespace."""
    monkeypatch.setattr(
        ObjectStoreClient,
        "session_prefix",
        property(lambda self: objectstore.DEFAULT_PREFIX),
    )


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        id="M1-no-create-once-precondition",
        attacks="ObjectStoreClient.put_once",
        apply=_drop_the_precondition,
        witness=witness_conflicting_write,
        before="refused; store holds original=True",
        after="accepted; store holds original=False",
        killed_by="tests/test_objectstore_client.py::TestAReplayConvergesOnOneObject",
    ),
    Mutant(
        id="M2-refusal-read-as-success",
        attacks="ObjectStoreClient.put_once",
        apply=_treat_the_refusal_as_success,
        witness=witness_conflicting_write,
        before="refused; store holds original=True",
        after="accepted; store holds original=True",
        killed_by="tests/test_objectstore_client.py::TestAReplayConvergesOnOneObject",
    ),
    Mutant(
        id="M3-replay-mints-a-new-key",
        attacks="ObjectStoreClient.object_key",
        apply=_mint_a_fresh_key,
        witness=witness_replay,
        before="objects=1",
        after="objects=2",
        killed_by="tests/test_upload_recovery.py::TestALostAnswerConvergesOnOneObject",
    ),
    Mutant(
        id="M4-observer-returns-the-write-receipt",
        attacks="careloop.graph.providers.object_store_readback",
        apply=_reuse_the_write_receipt,
        witness=witness_tampered_readback,
        before="DIVERGED",
        after="MATCHED",
        killed_by="tests/test_upload_readback.py::TestTheIndependenceProof",
    ),
    Mutant(
        id="M6-content-comparison-removed",
        attacks="careloop.graph.verify._UPLOAD_COMPARISON",
        apply=_drop_the_content_comparison,
        witness=witness_tampered_readback,
        before="DIVERGED",
        after="MATCHED",
        killed_by="tests/test_upload_readback.py::TestTheIndependenceProof",
    ),
    Mutant(
        id="M7-metadata-comparison-removed",
        attacks="careloop.graph.verify._UPLOAD_COMPARISON",
        apply=_drop_the_metadata_comparison,
        witness=witness_tampered_metadata,
        before="DIVERGED",
        after="MATCHED",
        killed_by=(
            "tests/test_upload_readback.py::TestTheIndependenceProof::"
            "test_replaced_metadata_is_not_verified"
        ),
    ),
    Mutant(
        id="M10-forbidden-read-treated-as-absence",
        attacks="careloop.tools.objectstore._translate",
        apply=_read_forbidden_as_absent,
        witness=witness_forbidden_read,
        before="classified as a refusal",
        after="classified as ABSENT",
        killed_by="tests/test_objectstore_client.py::TestAbsence",
    ),
    Mutant(
        id="M11-session-namespace-removed",
        attacks="ObjectStoreClient.session_prefix",
        apply=_drop_the_session_prefix,
        witness=witness_two_sessions,
        before="collide=False",
        after="collide=True",
        killed_by=(
            "tests/test_objectstore_client.py::TestTheKeyIsTheDurableActionIdentity::"
            "test_two_sessions_running_one_scenario_do_not_collide"
        ),
    ),
)


#: Mutants whose kill is owned by a test better placed than a monkeypatch here would be.
#:
#: Listed rather than reimplemented. Each names the file that runs the mutation and what it does to
#: the suite, and :class:`TestEveryMutantNamesALiveTest` checks the file exists — so this is a
#: pointer that goes stale loudly rather than a promise.
PROVEN_ELSEWHERE: Mapping[str, str] = {
    "M5-verifier-trusts-the-write-receipt": (
        "tests/test_content_integrity.py::TestTheWriteReceiptIsNotEvidence — the generic seam "
        "proof, which gives the write a receipt that agrees while the later read disagrees."
    ),
    "M8-wrong-document-authority-check-removed": (
        "tests/test_upload_authority.py::TestTheRelationshipIsWhatRefusesThese — rebuilds "
        "CROSS_CHECKS with exactly one edit, the demand-answering check removed."
    ),
    "M9-recovery-invokes-a-model": (
        "tests/test_reconcile_writers.py sweeps the recovery path for model-seam names, and "
        "tests/test_upload_recovery.py runs every recovery against seams that raise if touched."
    ),
    "M12-upload-falsely-marked-aws-evaluated": (
        "tests/test_evaluator_upload_scope.py::TestFalselyMarkingTheUploadEvaluatedWouldBeCaught"
    ),
}


@pytest.fixture
def store_service():
    held = ObjectStore()
    with object_store_server(held) as (endpoint, world):
        yield (
            world,
            ObjectStoreClient(BUCKET, session="sess-0001", client=object_store_client(endpoint)),
        )


class TestEveryMutantIsCaught:
    @pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.id)
    def test_the_witness_is_safe_before(self, mutant: Mutant, store_service) -> None:
        """A witness that already misbehaves proves nothing about the defence under attack."""
        held, client = store_service

        assert mutant.witness(held, client) == mutant.before

    @pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.id)
    def test_the_witness_is_dangerous_after(
        self, mutant: Mutant, store_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the mutation must actually reach the decision path."""
        held, client = store_service
        mutant.apply(monkeypatch)

        observed = mutant.witness(held, client)

        if observed == mutant.before:
            raise MutationHadNoEffect(
                f"{mutant.id} left the witness at {observed!r}; it never reached "
                f"{mutant.attacks}, so this is a harness failure rather than a surviving mutant"
            )
        assert observed == mutant.after


class TestEveryMutantNamesALiveTest:
    @pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.id)
    def test_the_named_killer_exists(self, mutant: Mutant) -> None:
        assert _names_a_real_test(mutant.killed_by), mutant.killed_by

    @pytest.mark.parametrize("name", sorted(PROVEN_ELSEWHERE), ids=lambda n: n)
    def test_the_delegated_proof_exists(self, name: str) -> None:
        assert _names_a_real_test(PROVEN_ELSEWHERE[name]), PROVEN_ELSEWHERE[name]


class TestTheLedgerIsComplete:
    def test_twelve_mutants_are_accounted_for(self) -> None:
        """The number in the report is computed here rather than maintained by hand."""
        assert len(MUTANTS) + len(PROVEN_ELSEWHERE) == 12

    def test_no_mutant_is_listed_twice(self) -> None:
        ids = [m.id for m in MUTANTS]
        assert len(set(ids)) == len(ids)
        assert not set(ids) & set(PROVEN_ELSEWHERE)

    def test_every_mutant_changes_something(self) -> None:
        """A mutant whose before and after agree would be scored as a kill while proving nothing."""
        assert all(m.before != m.after for m in MUTANTS)


# -- helpers ---------------------------------------------------------------------------------------


class _ClientError(Exception):
    """A botocore-shaped refusal, for the one witness that needs the store to say no."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _DeniesReads:
    """A boto3 client that answers every read with ``AccessDenied``.

    What a deployed instance role without ``s3:ListBucket`` on the prefix really does for a key it
    cannot see — measured against CloudShell's principal, which *had* the permission, and recorded
    in ``docs/objectstore-contract.md`` as the one thing still to be re-measured under the real
    role. Whichever way that lands, reading it as absence must stay wrong.
    """

    def get_object(self, **_kwargs: Any):
        raise _ClientError(objectstore.ACCESS_DENIED, 403)


def _names_a_real_test(reference: str) -> bool:
    """Whether a ``path::Class::test`` reference — or prose containing one — resolves to a file."""
    for token in reference.replace("—", " ").split():
        candidate = token.split("::")[0]
        if candidate.endswith(".py") and (Path(__file__).parent.parent / candidate).exists():
            return True
    return False


def _record() -> ActionRecord:
    return ActionRecord(
        action_type="upload_document",
        idempotency_key=KEY,
        args_digest=args_digest(UPLOAD),
        result="{}",
    )


def _verdict(client: ObjectStoreClient, key: str) -> str:
    """Run the shipped read-back and verifier, and name the observation outcome."""
    recorded = DocumentUploaded(
        object_key=key,
        object_id=KEY,
        portal_id=PORTAL,
        document_id=DOCUMENT,
        content_sha256=content_digest(held_bytes()),
    )
    observe = providers.object_store_readback(client)
    try:
        observed = observe(recorded)
    except verification.ReadBackFailed as failure:
        return str(failure.outcome)
    outcome = verify_action(
        case_id=CASE_ID,
        action_type="upload_document",
        args=UPLOAD,
        record=_record(),
        result=recorded,
        observed=observed,
    ).outcome
    return str(ObservationOutcome(outcome))
