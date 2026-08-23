"""The substitute's HTTP surface: the S3 REST operations CareLoop's upload path uses, and no others.

Five routes, path-style addressing, and error bodies in S3's own XML so that ``botocore`` populates
``Error.Code`` the way it does against Amazon. That last part is the fiddly half and the important
one: CareLoop's client classifies a refusal by its **code**, never by its message, so a substitute
returning a plain 412 with no body would let an implementation pass here and fail against S3.

**Nothing is authenticated.** boto3 signs; this reads the signature and ignores it. See the package
docstring for why that is a deliberate scope boundary rather than an omission.

**No clock, no randomness.** Request ids are constants and modification times are a fixed literal —
see :mod:`objectstore.store`.

``HEAD`` deserves a note. ``botocore`` cannot read an error code out of a ``HEAD`` response, because
the protocol forbids a body, so an absent key surfaces there as a bare ``404`` rather than as
``NoSuchKey``. That is the real provider's behaviour too, and it is exactly why CareLoop settles
absence on ``GetObject``: the read that can say *why* is the read worth classifying on.
"""

from __future__ import annotations

import base64
from typing import Final
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from objectstore.store import (
    DEFAULT_CONTENT_TYPE,
    FIXED_LAST_MODIFIED,
    NoSuchObject,
    ObjectExists,
    ObjectStore,
    StoredObject,
)

#: The header a conditional create travels in, and the only value CareLoop ever sends in it.
IF_NONE_MATCH: Final[str] = "if-none-match"
ANY_ETAG: Final[str] = "*"

#: Prefix S3 carries user metadata under. Case-insensitive on the wire, lower-cased here.
METADATA_PREFIX: Final[str] = "x-amz-meta-"

#: The header the provider reports a stored SHA-256 in, base64-encoded.
CHECKSUM_HEADER: Final[str] = "x-amz-checksum-sha256"

#: A constant, because a request id nothing reads is not worth a random source.
REQUEST_ID: Final[str] = "0000000000000000"

XML_NAMESPACE: Final[str] = "http://s3.amazonaws.com/doc/2006-03-01/"


def _error(code: str, message: str, status: int, *, key: str = "") -> Response:
    """One refusal, in the shape ``botocore`` parses an error code out of."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{escape(code)}</Code><Message>{escape(message)}</Message>"
        f"<Key>{escape(key)}</Key><RequestId>{REQUEST_ID}</RequestId></Error>"
    )
    return Response(content=body, status_code=status, media_type="application/xml")


def _metadata_of(request: Request) -> dict[str, str]:
    return {
        name[len(METADATA_PREFIX) :]: value
        for name, value in request.headers.items()
        if name.lower().startswith(METADATA_PREFIX)
    }


def _object_headers(stored: StoredObject) -> dict[str, str]:
    headers = {
        "ETag": stored.etag,
        "Last-Modified": FIXED_LAST_MODIFIED,
        CHECKSUM_HEADER: base64.b64encode(stored.checksum_sha256).decode("ascii"),
        "x-amz-server-side-encryption": "AES256",
        "x-amz-request-id": REQUEST_ID,
    }
    headers.update({f"{METADATA_PREFIX}{name}": value for name, value in stored.metadata.items()})
    return headers


def create_object_store_app(store: ObjectStore | None = None) -> FastAPI:
    """Serve one :class:`~objectstore.store.ObjectStore` over the S3 subset CareLoop uses.

    The store is a parameter so a test can hold the same object the routes hold — the arrangement
    ``demoworld.app.create_clinic_app`` uses, and for the same reason: proving a read-back is
    independent means being able to change what the provider holds behind CareLoop's back.
    """
    held = store if store is not None else ObjectStore()
    app = FastAPI(title="CareLoop object store substitute", docs_url=None, redoc_url=None)
    app.state.store = held

    @app.put("/{bucket}/{key:path}")
    async def put_object(bucket: str, key: str, request: Request) -> Response:
        """Create-once when the conditional header is present; overwrite when it is not.

        The measured asymmetry, and the reason ``IfNoneMatch="*"`` is load-bearing rather than
        defensive. A duplicate of *identical* bytes is refused too: S3 refuses it, so a substitute
        that accepted it would license a replay the real provider rejects.
        """
        body = await request.body()
        conditional = request.headers.get(IF_NONE_MATCH) == ANY_ETAG
        stored = StoredObject(
            body=body,
            content_type=request.headers.get("content-type", DEFAULT_CONTENT_TYPE),
            metadata=_metadata_of(request),
        )
        try:
            held.put(key, stored, only_if_absent=conditional)
        except ObjectExists:
            return _error(
                "PreconditionFailed",
                "At least one of the pre-conditions you specified did not hold",
                412,
                key=key,
            )
        return Response(status_code=200, headers=_object_headers(stored))

    @app.get("/{bucket}/{key:path}")
    async def get_object(bucket: str, key: str, request: Request) -> Response:
        """A fresh read of the bytes. **The primary settlement mechanism**, so it returns them."""
        if request.query_params.get("list-type") == "2":
            return _list_objects(held, bucket, request.query_params.get("prefix", ""))
        try:
            stored = held.get(key)
        except NoSuchObject:
            return _error("NoSuchKey", "The specified key does not exist.", 404, key=key)
        return Response(
            content=stored.body,
            status_code=200,
            media_type=stored.content_type,
            headers=_object_headers(stored),
        )

    @app.head("/{bucket}/{key:path}")
    async def head_object(bucket: str, key: str) -> Response:
        """Metadata and the reported checksum, with no body. See the module docstring on ``404``."""
        try:
            stored = held.get(key)
        except NoSuchObject:
            return Response(status_code=404, headers={"x-amz-request-id": REQUEST_ID})
        headers = _object_headers(stored)
        headers["Content-Length"] = str(len(stored.body))
        headers["Content-Type"] = stored.content_type
        return Response(status_code=200, headers=headers)

    @app.delete("/{bucket}/{key:path}")
    async def delete_object(bucket: str, key: str) -> Response:
        """Idempotent, like S3's: deleting what is not there is not an error."""
        held.delete(key)
        return Response(status_code=204, headers={"x-amz-request-id": REQUEST_ID})

    @app.get("/{bucket}")
    async def list_bucket(bucket: str, request: Request) -> Response:
        """``ListObjectsV2``, which is how a caller counts what the provider actually holds."""
        return _list_objects(held, bucket, request.query_params.get("prefix", ""))

    return app


def _list_objects(store: ObjectStore, bucket: str, prefix: str) -> Response:
    keys = store.keys_under(prefix)
    contents = "".join(
        f"<Contents><Key>{escape(key)}</Key>"
        f"<LastModified>{FIXED_LAST_MODIFIED}</LastModified>"
        f"<ETag>{escape(store.get(key).etag)}</ETag>"
        f"<Size>{len(store.get(key).body)}</Size>"
        f"<StorageClass>STANDARD</StorageClass></Contents>"
        for key in keys
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult xmlns="{XML_NAMESPACE}">'
        f"<Name>{escape(bucket)}</Name><Prefix>{escape(prefix)}</Prefix>"
        f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>1000</MaxKeys>"
        f"<IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
    )
    return Response(content=body, status_code=200, media_type="application/xml")


__all__ = ["create_object_store_app"]
