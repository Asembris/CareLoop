"""Idempotency keys for external effects.

Every external-effect tool call carries a key derived from ``(case_id, action_type, the canonical
values of the arguments that identify the action)``. The key is what lets a replayed action — a
retried graph node, a resumed case, a duplicate inbound event — be suppressed rather than performed
twice.

**Identity and payload are different sets, and this module is where the difference is spent.**
:func:`idempotency_key` covers only the arguments
:attr:`careloop.tools.registry.ArgSpec.identifies` declares — *which logical action is this* —
while :func:`args_digest` covers **every** argument, because it answers a different question: which
exact call did a caregiver approve, and did the executor receive what policy authorised. The two
must never be folded together and there is deliberately no identity-only digest exposed for
anything else to reach for.

The registry is the only place the identifying set is declared. An **unregistered** action type has
no declaration to narrow by, so every argument identifies it — the strictly finer answer, which is
also exactly what this module did before identity was declarable, and which can only ever refuse a
duplicate rather than manufacture one.

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
from careloop.tools.registry import REGISTRY, ToolRegistry


class UncanonicalizableArgument(Exception):
    """Raised when an argument value has no canonical form, so no key can be derived from it."""


class UnidentifiableCall(Exception):
    """Raised when a call supplies arguments but none of them identifies the action.

    The fail-closed answer to the one shape narrowing identity makes expressible: a registered tool
    called with arguments of which not one is identifying would key exactly as the empty call does,
    so two genuinely different calls would share a key and the second would be suppressed as a
    duplicate of the first. Refusing points the same way :exc:`UncanonicalizableArgument` does —
    a call that cannot be identified does not happen.

    Unreachable through the production path: ``PolicyEngine.evaluate`` refuses an argument map whose
    key set is not exactly what the registry declares, and
    ``PlannerIntentBase._bind_arguments_to_the_chosen_tool`` rebuilds a proposal's arguments as
    exactly ``spec.arg_names``. It is a guard against a hand-built call and against future drift.
    """


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


def identifying_args(
    action_type: str, args: Mapping[str, object], registry: ToolRegistry = REGISTRY
) -> dict[str, str]:
    """The canonical text of the arguments that identify one call of ``action_type``.

    **Every** value is canonicalised first, whatever it identifies, so a value with no canonical
    form still refuses the call rather than being skipped for being descriptive: the argument is
    still transmitted and still digested, and a call CareLoop cannot render is a call it should not
    make.

    **The only thing ever dropped is an argument the tool declares and declares non-identifying.**
    An unregistered action type has no declaration to narrow by and keeps everything; so does an
    argument the tool does not declare, because nobody has said it is not part of the identity and
    dropping it would collapse two calls that differ. Both fall the same way — towards the finer
    key, which can refuse a duplicate but never manufacture one.

    Raises:
        UncanonicalizableArgument: if any value has no canonical form.
        UnidentifiableCall: if arguments were supplied and none of them identifies the action.
    """
    canonical = canonical_args(args)
    spec = registry.get(action_type)
    if spec is None:
        return canonical

    identifying = spec.identifying_args
    declared = spec.arg_names
    selected = {
        name: text
        for name, text in canonical.items()
        if name in identifying or name not in declared
    }
    if canonical and not selected:
        raise UnidentifiableCall(
            f"a call of {action_type!r} was supplied {sorted(canonical)}, none of which identifies "
            f"the action; it would key exactly as the empty call does and the next genuinely "
            f"different call would be suppressed as a duplicate of it"
        )
    return selected


def idempotency_key(
    case_id: str,
    action_type: str,
    args: Mapping[str, object],
    registry: ToolRegistry = REGISTRY,
) -> str:
    """Return the idempotency key for one proposed external effect.

    Stable under argument reordering, and — deliberately — under any change to an argument the
    registry declares non-identifying. Unstable under any change to an identifying value, to an
    identifying argument's name, to the action type, or to the case.

    ``args`` is the whole call: every value is canonicalised, and the identifying subset is what
    reaches the digest. Passing a pre-narrowed map would make the caller a second place the identity
    is decided.
    """
    return _digest(
        {
            "case_id": case_id,
            "action_type": action_type,
            "args": identifying_args(action_type, args, registry),
        }
    )


__all__ = [
    "UncanonicalizableArgument",
    "UnidentifiableCall",
    "args_digest",
    "canonical_args",
    "digest_mapping",
    "idempotency_key",
    "identifying_args",
]
