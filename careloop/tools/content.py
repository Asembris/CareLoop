"""The digest of content — bytes CareLoop holds, or bytes an external system returned.

**One function, deliberately, because it has exactly two callers and they must never disagree.**
An integrity check compares what CareLoop was authorised to send against what a provider turns out
to be holding. Both sides are bytes, and both sides are reduced here. Two digest functions would be
two answers to one question, which is the shape :mod:`careloop.provenance.canonical` and
:func:`careloop.tools.idempotency.digest_mapping` already exist to prevent one layer over.

**Bytes in, never text.** There is no ``str`` overload and there must not be one: accepting text
would mean choosing an encoding here, and a caller with a different idea of the encoding would
compute a different digest for content that is byte-for-byte identical. It also removes the place
where somebody would eventually add stripping, case folding or unicode normalisation — every one of
which is a laundering surface, and none of which an integrity check can survive. Two documents that
differ by one byte are two documents.

**Not an action identity, which is why this is not in** :mod:`careloop.tools.idempotency`. That
module answers *which logical action is this*, from a case, an action type and the arguments the
registry declares identifying. A content digest answers *is what they hold what we sent*. Folding
the two would make a resource's address depend on its contents, or its contents on its address, and
neither is true of a create-once store keyed by durable action identity.

**No clock and no randomness**, like everything else under ``careloop/tools/`` — enforced by the
sweep in ``tests/test_tool_callables.py``, which this module is picked up by automatically.
"""

from __future__ import annotations

import hashlib
from typing import Final

#: How :func:`content_digest` reduces bytes, named so a claim about it can be written down.
#:
#: Spelled once. A caller that needs to *say* which algorithm produced a digest — a provider
#: request that must name it, a document that describes the comparison — reads this rather than
#: repeating the string, so the name and the implementation cannot drift apart.
CONTENT_DIGEST_ALGORITHM: Final[str] = "sha256"

#: Length of the hexadecimal form, so a malformed digest is recognisable without recomputing one.
CONTENT_DIGEST_LENGTH: Final[int] = 64


def content_digest(data: bytes) -> str:
    """Lowercase hexadecimal SHA-256 of exactly these bytes.

    Nothing is normalised, decoded, trimmed or interpreted on the way in. The value is a fact about
    the bytes and about nothing else, which is what lets one side of a comparison be computed from
    a document CareLoop holds and the other from a body a provider returned, with neither having
    seen the other.

    Hexadecimal rather than base64 because it is the form every other digest in this project takes
    — ``careloop.tools.idempotency`` and ``careloop.graph.witness`` both produce lowercase hex — and
    one spelling means a value can be compared, logged and read without a conversion step nobody
    remembers is there. A provider that reports its own digest in some other encoding is converted
    at that provider's boundary, where the encoding is a fact about the protocol.
    """
    return hashlib.sha256(data).hexdigest()


__all__ = ["CONTENT_DIGEST_ALGORITHM", "CONTENT_DIGEST_LENGTH", "content_digest"]
