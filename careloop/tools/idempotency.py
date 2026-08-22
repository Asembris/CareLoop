"""Idempotency keys for external effects.

Every external-effect tool call carries a key derived from ``(case_id, action_type, canonical
args)``. The key is what lets a replayed action — a retried graph node, a resumed case, a duplicate
inbound event — be suppressed rather than performed twice.

**One canonical form, two totality requirements.** Argument values are reduced through
:func:`careloop.provenance.canonical.canonical_value`, the same reduction the policy engine matches
facts with. Two forms that can disagree about what a value *is* would be one too many, and a
datetime is exactly where they would drift. It is shared through ``provenance`` rather than
imported from ``policy`` so that reusing the data format is not also a dependency on the authority
layer: nothing here evaluates a rule or consults a verdict.

Where the two consumers part company is what to do with a value that has no canonical form.
``canonical_value`` returns ``None``, which the policy engine reads as *unmatchable*, resolving
``UNTRUSTED`` — fail-closed. Keying cannot borrow that reading. Folding every uncanonicalizable
value onto one sentinel would give two genuinely **different** calls the same key, and the executor
would then suppress the second as a duplicate of the first. Fail-closed points the other way here,
so :exc:`UncanonicalizableArgument` is raised instead: a call that cannot be keyed does not happen.

**No clock, no randomness.** Nothing in ``careloop/tools/`` reads the time or a random source. A
key, and the result recorded under it, must be byte-identical on replay for the idempotency
property to be an assertion rather than an approximation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from careloop.provenance.canonical import canonical_value


class UncanonicalizableArgument(Exception):
    """Raised when an argument value has no canonical form, so no key can be derived from it."""


def canonical_args(args: Mapping[str, object]) -> dict[str, str]:
    """Reduce every argument value to its canonical text.

    Raises:
        UncanonicalizableArgument: if any value has no canonical form. Refusing is deliberate;
            see the module docstring.
    """
    canonical: dict[str, str] = {}
    for name, value in args.items():
        text = canonical_value(value)
        if text is None:
            raise UncanonicalizableArgument(
                f"argument {name!r} is a {type(value).__name__}, which has no canonical form; "
                f"keying it would collide distinct calls into one suppressed duplicate"
            )
        canonical[name] = text
    return canonical


def _digest(payload: object) -> str:
    """SHA-256 over a canonical JSON encoding of ``payload``.

    ``sort_keys`` makes the encoding independent of mapping order. JSON quoting makes it
    injective, so ``{"ab": "c"}`` and ``{"a": "bc"}`` cannot flatten onto the same bytes — a
    delimiter-joined encoding would let them.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def digest_mapping(values: Mapping[str, str]) -> str:
    """SHA-256 over the canonical JSON encoding of an already-canonical string mapping.

    The same encoding rule :func:`args_digest` and :func:`idempotency_key` use, exposed so that a
    second consumer cannot grow a second answer to *"what are the canonical bytes of this
    mapping?"*. One rule, three callers.

    Deliberately **not** ``args_digest`` under another name. That function digests a tool's
    *argument map*, which is what the registry says CareLoop may send; a joint-state digest is
    taken over what an external system *holds*. The two coincide for a booking today and are
    different questions, so a caller that means the second must not reach for the first.

    ``values`` are expected to be canonical already — :func:`careloop.provenance.canonical
    .canonical_value` output — so nothing is reduced here and no value can be silently normalised
    on the way in.
    """
    return _digest(dict(values))


def args_digest(args: Mapping[str, object]) -> str:
    """Digest of ``args`` alone, for records that identify arguments without storing them.

    Used by :class:`careloop.state.case.ActionRecord` and
    :class:`careloop.state.case.Approval`, both of which carry an ``args_digest`` rather than the
    raw values.
    """
    return _digest(canonical_args(args))


def idempotency_key(case_id: str, action_type: str, args: Mapping[str, object]) -> str:
    """Return the idempotency key for one proposed external effect.

    Stable under argument reordering; unstable under any change to a value, an argument name, the
    action type, or the case.
    """
    return _digest({"case_id": case_id, "action_type": action_type, "args": canonical_args(args)})


__all__ = [
    "UncanonicalizableArgument",
    "args_digest",
    "canonical_args",
    "digest_mapping",
    "idempotency_key",
]
