"""The second tool that reaches a real external system.

``upload_document`` files a document into Amazon S3 — a create-once store, keyed by the durable
action identity CareLoop froze before anything left the process. The provider contract this client
is written against was measured, not assumed; every claim below is recorded in
[`docs/objectstore-contract.md`](../../docs/objectstore-contract.md).

**Why a second one at all.** One genuinely external effect proves an integration. Two prove an
*architecture*, and only if the second provider offers none of the affordances the first did. The
clinic keys writes by an idempotency header and replays the original record; S3 has no notion of an
idempotency key, no notion of an action, refuses a duplicate outright, and hands back nothing. Every
guarantee CareLoop needs has to be assembled here out of primitives that were not designed for it.

**The precondition is load-bearing and is not an optimisation.** ``IfNoneMatch="*"`` makes a write
create-once: measured, a write *without* it silently overwrites whatever the key already held. It is
therefore present on every write this module makes, including a replay, and there is deliberately no
parameter to omit it. A replay that dropped it would destroy the very evidence a reconciliation
exists to read.

**A ``412`` is neither failure nor success, and that is the whole of the reconciliation design.** It
says the key is taken; it says nothing about *by what*. So :meth:`ObjectStoreClient.put_once`
answers it with a fresh ``GetObject`` and compares the bytes it holds against the bytes the store
returned. Equal, and this is the object CareLoop's own frozen identity already created — the write
is settled, idempotently, with no second object. Different, and CareLoop's identity names something
it did not put there, which is a contradiction no retry resolves and which fails closed.

**That read is not the verification read.** It settles the *write*; the independent observation
happens later, in another node, keyed on the recorded result. Two GETs at two times answering two
questions. Collapsing them would make settlement rest on a read the dispatch path already had in
hand, which is the tautology the whole read-back arrangement exists to remove.

**No patient data reaches the provider's namespace.** The key is a prefix, an opaque session id
and the frozen idempotency key — a SHA-256 hex digest. Metadata is exactly two deterministic
fields, both of which are already destinations or identifiers the case established. The document's
title, its filename, the patient and the case id are none of the store's business and none of them
are sent.

**No clock and no randomness**, per invariant 9. Everything this module sends is derived from values
the caller already froze, so a replay transmits byte-identical bytes to a byte-identical key.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from careloop.tools.content import content_digest
from careloop.tools.provider import ProviderRefused, ProviderUnavailable

#: The prefix every object CareLoop files sits under. One namespace, so a lifecycle rule and an
#: IAM policy can both name it without either having to enumerate anything.
DEFAULT_PREFIX: Final[str] = "careloop"

#: What a session-less client files under. A named constant rather than an empty segment, because a
#: key with two consecutive separators is a different key from the one anybody meant.
DEFAULT_SESSION: Final[str] = "default"

#: The conditional-create header's value. There is no other value CareLoop ever sends.
IF_NONE_MATCH_ANY: Final[str] = "*"

#: The checksum algorithm asked of the provider. Corroboration, never the settlement proof.
CHECKSUM_ALGORITHM: Final[str] = "SHA256"

#: What CareLoop declares it is sending. A fixed literal: the model chooses which held document is
#: filed, never what kind of thing it is.
CONTENT_TYPE: Final[str] = "application/pdf"

#: The metadata keys the provider is told, and the only ones.
#:
#: Both are values the case already established and both are already visible to the destination:
#: ``document-id`` is the identifier the demanding letter printed, and ``portal-id`` is where the
#: upload was authorised to land. Neither is patient data, neither is free text, and neither is
#: model-authored — they are the tool's two declared arguments, transmitted so that an independent
#: read can compare them.
DOCUMENT_METADATA: Final[str] = "document-id"
PORTAL_METADATA: Final[str] = "portal-id"

#: Provider error codes this client gives meaning to. Read from the structured response, never from
#: a message: a store whose prose changed would otherwise change what CareLoop believes.
PRECONDITION_FAILED: Final[str] = "PreconditionFailed"
NO_SUCH_KEY: Final[str] = "NoSuchKey"
ACCESS_DENIED: Final[str] = "AccessDenied"


class ObjectStoreUnavailable(ProviderUnavailable):
    """The object store could not be reached, or could not answer.

    Carries the **operation** CareLoop was attempting and the provider's own **error code**, and
    nothing else. No bucket, no key, no message, no ARN. ``ClinicRefused`` documents what that cost
    to learn: an exception escaping a node is copied into an OpenTelemetry ``exception.message``
    attribute verbatim, so anything interpolated here is published telemetry chosen by a service
    CareLoop does not control.
    """

    def __init__(self, *, operation: str, code: str) -> None:
        super().__init__(f"the object store could not serve {operation} ({code})")
        self.operation = operation
        self.code = code


class ObjectStoreRefused(ProviderRefused):
    """The object store answered and declined. Same two fields, same reason."""

    def __init__(self, *, operation: str, code: str) -> None:
        super().__init__(f"the object store refused {operation} ({code})")
        self.operation = operation
        self.code = code


class ObjectNotFound(ObjectStoreRefused):
    """The store answered that it holds no object under that key.

    **A definite answer about a key CareLoop chose and wrote**, which is why it is its own type:
    a later read of that exact key answering ``NoSuchKey`` is the store contradicting the receipt
    CareLoop holds, rather than a failure to obtain an answer.

    Deliberately **not** raised for ``AccessDenied``. A store that will not tell CareLoop whether an
    object exists has said something about the *caller*, not about the object — and S3 answers
    exactly that way for a missing key when the caller lacks list permission on the prefix. Reading
    it as absence would turn a misconfiguration into a confident claim about the world.
    """


class ObjectIdentityConflict(Exception):
    """The store holds this key, and what it holds is not what CareLoop was going to write.

    **Not a provider error**, which is why it is not one of the two above: the store behaved
    correctly and refused an overwrite. What is wrong is CareLoop's belief that this key names its
    own action. A retry cannot resolve that and a person has to look, so it fails the case loudly
    rather than being reported as an outcome nobody established.
    """


@dataclass(frozen=True, slots=True)
class FiledObject:
    """What the store holds under one key, as a fresh read established it.

    ``content_sha256`` is computed **here, from the bytes the provider returned**, and never read
    from anything the provider reports about itself. That is the whole difference between observing
    an object and believing a receipt.
    """

    object_key: str
    body: bytes
    metadata: Mapping[str, str]

    @property
    def content_sha256(self) -> str:
        return content_digest(self.body)


class ObjectStoreClient:
    """A thin, synchronous client for one bucket and one session's namespace within it.

    Synchronous because :meth:`careloop.tools.callables.ToolSet.call` is, exactly as
    :class:`careloop.tools.clinic.ClinicClient` is.

    ``session`` scopes the key namespace. It is not a credential and the store verifies nothing: it
    is there so two visitors driving the same demo case produce two objects rather than colliding on
    one. The frozen idempotency key is derived from ``(case_id, action_type, identifying args)`` and
    is *byte-identical* across visitors running the same scenario, which is a property worth having
    and a collision waiting to happen without this.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        session: str | None = None,
        client: Any | None = None,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._session = session or DEFAULT_SESSION
        if client is not None:
            self._client = client
        else:  # pragma: no cover - exercised only against a real endpoint
            import boto3

            self._client = boto3.session.Session().client(
                "s3", region_name=region, endpoint_url=endpoint_url
            )

    @property
    def bucket(self) -> str:
        """Which bucket this client files into."""
        return self._bucket

    @property
    def session(self) -> str:
        """Which namespace within the bucket this client's keys sit under."""
        return self._session

    @property
    def session_prefix(self) -> str:
        """Everything this client may write, as one prefix. What a reset removes."""
        return f"{self._prefix}/{self._session}"

    def object_key(self, idempotency_key: str) -> str:
        """Where the action identified by ``idempotency_key`` files its object.

        **Derived from the durable action identity and from nothing else.** The key is chosen before
        the bytes are looked at, which is what lets a reconciliation address the object a crashed
        attempt would have created. Had the address instead been a function of what the object
        contains, it could not have been computed until the content was already read back, and the
        frozen-identity convergence the saga depends on would have nothing to aim at. This store is
        create-once, keyed by action identity; content integrity is a separate mechanism, settled
        afterwards by :meth:`fetch`.

        The last segment is the frozen key verbatim: a SHA-256 hex digest, carrying no patient
        identifier, no document title, no filename and no case id. It is also what the settlement
        telemetry names the resource by, so it must stay one bounded token.
        """
        return f"{self.session_prefix}/{idempotency_key}"

    def put_once(
        self, *, object_key: str, body: bytes, metadata: Mapping[str, str]
    ) -> tuple[FiledObject, bool]:
        """File ``body`` at ``object_key``, or settle against what is already there.

        Returns the object and whether **this call** created it.

        Three outcomes, and the middle one is the design:

        The key was free — the object is created and returned. The store's own reported checksum is
        corroborated against the bytes that were sent, so a provider that stored something other
        than what it was handed is caught here rather than at verification time.

        The key was taken and holds **these exact bytes** — the conditional write is refused, a
        fresh read establishes what is there, and the result is the same object the frozen identity
        already created. No second object exists, nothing was overwritten, and the outcome the
        crash left open is now settled.

        The key was taken and holds **something else** — :class:`ObjectIdentityConflict`. CareLoop's
        action identity names an object it did not write.

        Raises:
            ObjectIdentityConflict: as above.
            ObjectStoreRefused: the store answered and declined for any other reason.
            ObjectStoreUnavailable: the store could not be reached or could not answer.
        """
        try:
            created = self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=body,
                IfNoneMatch=IF_NONE_MATCH_ANY,
                ChecksumAlgorithm=CHECKSUM_ALGORITHM,
                ContentType=CONTENT_TYPE,
                Metadata=dict(metadata),
            )
        except Exception as failure:
            code = _code(failure)
            if code != PRECONDITION_FAILED:
                raise _translate("put_object", failure) from failure
            existing = self.fetch(object_key)
            if existing.content_sha256 != content_digest(body):
                raise ObjectIdentityConflict(
                    "the object store already holds this action's key with different content; "
                    "the identity CareLoop froze names an object it did not write"
                ) from failure
            return existing, False

        reported = created.get("ChecksumSHA256")
        if reported is not None and reported != _base64_digest(body):
            raise ObjectIdentityConflict(
                "the object store acknowledged a write whose reported checksum is not the checksum "
                "of the bytes CareLoop sent"
            )
        return FiledObject(object_key=object_key, body=body, metadata=dict(metadata)), True

    def fetch(self, object_key: str) -> FiledObject:
        """Read one object back, as the store holds it **now**.

        **A separate request that returns the bytes**, which is what makes an integrity check an
        observation rather than a re-reading of CareLoop's own receipt. The digest is computed from
        what came back; nothing the provider says about the object is taken as the answer.

        Raises:
            ObjectNotFound: the store answered that it holds no such key.
            ObjectStoreRefused: it answered and declined for any other reason — including
                ``AccessDenied``, which is a statement about the caller and never about the object.
            ObjectStoreUnavailable: it could not be reached or could not answer.
        """
        try:
            read = self._client.get_object(Bucket=self._bucket, Key=object_key)
            body = read["Body"].read()
        except Exception as failure:
            raise _translate("get_object", failure) from failure
        return FiledObject(
            object_key=object_key, body=body, metadata=dict(read.get("Metadata", {}) or {})
        )

    def remove_session_objects(self) -> int:
        """Delete everything under this client's namespace. Returns how many objects went.

        Demo control only, and the object-store half of what ``POST /api/demo/reset`` already does
        for the clinic. It is scoped to :attr:`session_prefix` by construction: this client has no
        way to express somebody else's namespace, so one visitor's reset cannot reach another's.

        Idempotent — resetting a namespace that holds nothing is the same successful outcome as
        resetting one that holds something.
        """
        removed = 0
        try:
            listing = self._client.list_objects_v2(
                Bucket=self._bucket, Prefix=f"{self.session_prefix}/"
            )
            for entry in listing.get("Contents", ()):
                self._client.delete_object(Bucket=self._bucket, Key=entry["Key"])
                removed += 1
        except Exception as failure:
            raise _translate("remove_session_objects", failure) from failure
        return removed

    def close(self) -> None:
        """Release the underlying connection pool, if the client holds one."""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


def _base64_digest(body: bytes) -> str:
    """The content digest in the encoding the provider reports it in. Converted only here."""
    import hashlib

    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def _code(failure: Exception) -> str:
    """The provider's own error code, or ``""`` when the failure carries none.

    Read from the structured response. A client that told two refusals apart by their prose would
    be a client the provider could reword out from under.
    """
    response = getattr(failure, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str):
            return code
    return ""


def _translate(operation: str, failure: Exception) -> Exception:
    """Turn one provider failure into CareLoop's vocabulary, carrying no third-party text.

    A failure that carries a provider error code is the provider *answering*; one that does not is
    a transport failure, where nothing was established. ``NoSuchKey`` is narrowed further because it
    is a definite statement about an object, which ``AccessDenied`` — deliberately left as an
    ordinary refusal — is not.
    """
    code = _code(failure)
    if not code:
        return ObjectStoreUnavailable(operation=operation, code=type(failure).__name__)
    if code == NO_SUCH_KEY:
        return ObjectNotFound(operation=operation, code=code)
    return ObjectStoreRefused(operation=operation, code=code)


__all__ = [
    "ACCESS_DENIED",
    "CHECKSUM_ALGORITHM",
    "CONTENT_TYPE",
    "DEFAULT_PREFIX",
    "DEFAULT_SESSION",
    "DOCUMENT_METADATA",
    "IF_NONE_MATCH_ANY",
    "NO_SUCH_KEY",
    "PORTAL_METADATA",
    "PRECONDITION_FAILED",
    "FiledObject",
    "ObjectIdentityConflict",
    "ObjectNotFound",
    "ObjectStoreClient",
    "ObjectStoreRefused",
    "ObjectStoreUnavailable",
]
