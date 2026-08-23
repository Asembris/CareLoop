"""What the substitute holds, and the one rule that makes it create-once.

A plain class with mutable state rather than a model, exactly as :class:`demoworld.clinic.Clinic`
is: this is a service, not a value.

**One rule, and it is the measured one.** A write carrying the conditional header is refused when
the store already holds the key — *whatever the body is*. A write without it overwrites. That
asymmetry is the entire behaviour CareLoop's upload design depends on, and reproducing it faithfully
is the only reason this package exists.

**No clock and no randomness.** The substitute must return byte-identical answers on a replay, for
the reason ``careloop/tools/`` may not read a clock: an idempotency property asserted against a
service whose responses drift is an approximate property. ``last_modified`` is a fixed literal
rather than a time, because CareLoop never reads it and inventing one would be the first place a
clock got in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Final

#: What every stored object reports as its modification time. A constant, not a time; see above.
FIXED_LAST_MODIFIED: Final[str] = "2026-01-01T00:00:00.000Z"

#: The default content type, matching what CareLoop sends.
DEFAULT_CONTENT_TYPE: Final[str] = "application/octet-stream"


class ObjectExists(Exception):
    """Raised when a conditional create meets a key the store already holds.

    Deliberately raised for a matching body as well as a divergent one. S3 refuses both — measured,
    C2 and C3 — and a substitute that quietly accepted a duplicate of identical bytes would let an
    implementation get away with a replay that the real provider rejects.
    """


class NoSuchObject(Exception):
    """Raised when a read names a key the store does not hold."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """One object, exactly as it was written."""

    body: bytes
    content_type: str = DEFAULT_CONTENT_TYPE
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def etag(self) -> str:
        """The quoted MD5 S3 reports for a single-part upload.

        Present for protocol fidelity and read by nothing in CareLoop, which settles on the bytes.
        """
        return f'"{hashlib.md5(self.body, usedforsecurity=False).hexdigest()}"'

    @property
    def checksum_sha256(self) -> bytes:
        """The raw SHA-256 digest. Encoded to base64 at the HTTP boundary, not here."""
        return hashlib.sha256(self.body).digest()


class ObjectStore:
    """Keys to objects, with create-once semantics and a lock around them.

    Serialised for the reason :class:`careloop.state.sqlite_store.SqliteCaseStore` is: a server runs
    this from a worker pool, and *check whether the key exists, then write it* is exactly the
    sequence two threads must not interleave. Without the lock a conditional create could admit two
    writers, which is the one thing this class exists to prevent.
    """

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}
        self._lock = RLock()

    def put(self, key: str, stored: StoredObject, *, only_if_absent: bool) -> None:
        """Write ``key``.

        Raises:
            ObjectExists: if ``only_if_absent`` and the store already holds the key. Nothing is
                written and the existing object is left exactly as it was — the measured behaviour,
                and the half a refusal on its own does not establish.
        """
        with self._lock:
            if only_if_absent and key in self._objects:
                raise ObjectExists(key)
            self._objects[key] = stored

    def get(self, key: str) -> StoredObject:
        """Return what the store holds.

        Raises:
            NoSuchObject: if it holds nothing under ``key``.
        """
        with self._lock:
            found = self._objects.get(key)
        if found is None:
            raise NoSuchObject(key)
        return found

    def delete(self, key: str) -> bool:
        """Remove ``key``. Returns whether it was there, and never raises when it was not."""
        with self._lock:
            return self._objects.pop(key, None) is not None

    def keys_under(self, prefix: str) -> tuple[str, ...]:
        """Every key beginning with ``prefix``, in sorted order."""
        with self._lock:
            return tuple(sorted(key for key in self._objects if key.startswith(prefix)))

    def clear_prefix(self, prefix: str) -> int:
        """Remove every key under ``prefix``. Returns how many went."""
        with self._lock:
            doomed = [key for key in self._objects if key.startswith(prefix)]
            for key in doomed:
                del self._objects[key]
            return len(doomed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._objects)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._objects


__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "FIXED_LAST_MODIFIED",
    "NoSuchObject",
    "ObjectExists",
    "ObjectStore",
    "StoredObject",
]
