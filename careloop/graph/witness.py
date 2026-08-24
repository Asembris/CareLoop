"""What CareLoop is willing to say about an external effect, and to whom.

A **witness** is a sink. The graph decides, acts and commits exactly as it did before; afterwards
it hands a bounded, closed-vocabulary statement of what already happened to whatever is listening.
Nothing here can influence policy, trust, an approval, a dispatch, an idempotency key, a case
state, a verification, a settlement or an HTTP response — and the arrangement below is what makes
that structural rather than a promise.

**The dependency runs one way and stops here.** This module imports no OpenTelemetry, no
``scripts`` and no ``evaluators``; it does not know that a span exists. A deployment layer
implements :class:`Witness` over whatever it uses to record telemetry — see
``scripts/witness_span.py`` — and the application never learns which one it got. The default is
:data:`NO_WITNESS`, so CareLoop runs identically with nothing listening at all, which is what every
offline test and every local run actually does.

**Raw values stop at the protocol, not at the module.** The projections below are handed a
proposal's arguments and an external system's record, because computing a safe fact requires the
unsafe one. What crosses :class:`Witness` is only :class:`EffectClaim` and
:class:`ObservationClaim`, whose every field is a closed enum member, a fixed field-*name* string,
a SHA-256 digest or a bounded identifier. A patient id, a slot, a reason, a URL, a response body,
a diagnostic, an exception message and a case id have no field to travel in.

**Two claims, two sources, and the separation is the evidence.** The effect claim's digest is taken
over the arguments **policy authorised and the executor dispatched**. The observation claim's
digest is taken over the record the clinic returned to an **independent** read. They are built by
two functions from two objects, and :func:`_observation_claim` has no parameter through which the
effect digest could arrive — so an equality between the two is *observed* and cannot be
manufactured. That is the whole reason a settlement verdict means anything, and the reason the
re-check path re-projects the original proposal rather than reusing what it just read back.

**One failure boundary.** :func:`witness_effect` and :func:`witness_observation` are the only two
places in the application that catch :class:`Exception` for telemetry. A witness that raises, a
projection with a bug in it and an adapter whose span rejects an attribute all end the same way:
one ``DEBUG`` line naming the exception *type*, and the case carries on unchanged. Nothing above
these two functions handles telemetry failure, and nothing below them is allowed to escape.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from careloop.graph.verify import (
    OBSERVABLE_STATE,
    ObservableEffect,
    ObservationOutcome,
    Verification,
)
from careloop.provenance.canonical import canonical_value
from careloop.tools.idempotency import digest_mapping
from careloop.tools.results import ActionResult

logger = logging.getLogger(__name__)


#: How a field set is written down. One encoding, so a reader never has to guess which it got.
FIELD_SEPARATOR: Final[str] = ","

#: What an external resource identifier may look like before CareLoop will repeat it.
#:
#: RFC 3986 unreserved characters, allowlisted rather than denylisted, with a length ceiling. This
#: is the one value in a claim that an **external service** chooses, so it is bounded here rather
#: than trusted: a clinic answering with a megabyte, a newline or a path separator produces no
#: identifier at all. Failing to name the resource fails settlement closed, which is the safe
#: direction; repeating whatever arrived would put an unbounded third-party string into published
#: telemetry, which is not.
RESOURCE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~-]{1,64}")


class EffectDispatch(StrEnum):
    """What happened to the external effect **in this trace**.

    Three lifecycles this graph actually reaches, told apart by the code path taken rather than by
    inspecting transport afterwards. CareLoop knows whether it dispatched; something reading the
    trace later can check that claim against the HTTP beside it, and a claim derived *from* that
    HTTP would make the check circular.
    """

    PERFORMED = "PERFORMED"
    """A node dispatched the call and it returned. A provider write is in this trace.

    Claimed by ``ExecuteNode`` on a first attempt and by ``ReconcileNode`` on a replay, on the line
    after the call in both — a replay is a real request, whatever the provider does with it.

    **A provider write, not a clinic write**, and the wording was narrower than the claim from the
    day a second real integration existed: an ``upload_document`` dispatch is an S3 ``PutObject``
    and belongs to this member exactly as a booking does. Nothing here names either, and nothing
    may: what a reader can check against the transport beside it is that *a* write happened.
    """

    SUPPRESSED = "SUPPRESSED"
    """The case already held the effect, so the duplicate branch returned before the executor.

    No HTTP can have left the process: that branch is taken before the ``BeforeToolCall`` hook and
    before the gated toolset is called, and it returns.
    """

    NOT_IN_THIS_TRACE = "NOT_IN_THIS_TRACE"
    """This run reached verification without anything in it having accounted for the effect.

    The write belongs to an earlier trace. Same transport expectation as :attr:`SUPPRESSED` and a
    different fact about why.

    **Two shapes arrive here and they are one fact**: a caregiver's re-check, which runs dispatch
    then verify and never reaches the execute node, and a verification resumed after a process died
    between the ``VERIFYING`` commit and the read. The condition was written as the first alone —
    read off a durable state — and a resumed verification then emitted no effect claim at all, five
    attributes instead of nine, which the frozen evaluator correctly judged ``FAIL``. It is a
    run-scoped flag now, because *"did the write happen in this trace"* is a run-scoped question.
    """


class ObservationMode(StrEnum):
    """Whether an independent observer took part in the verdict."""

    INDEPENDENT_READ = "INDEPENDENT_READ"
    """A separate read of the external system was made — successfully or not."""

    NONE = "NONE"
    """No independent read took part, so the verdict rests on CareLoop's own receipt."""


class Settlement(StrEnum):
    """What CareLoop concluded about the effect, after the conclusion was durably written."""

    VERIFIED = "VERIFIED"
    NOT_CERTIFIED = "NOT_CERTIFIED"


@dataclass(frozen=True, slots=True)
class EffectClaim:
    """What CareLoop says it did with an external effect, and to what intended state."""

    effect_type: str
    """The registry's own name for the action. A closed set, never free text."""

    dispatch: EffectDispatch

    fields: str
    """The externally held field **names**, canonically ordered and joined.

    Names and never values. It is what stops a state-digest comparison being vacuous: two digests
    over an empty joint state are trivially equal, so the field set has to be pinned before the
    equality means anything.
    """

    state_digest: str
    """Digest of the joint state CareLoop authorised and executed. Lowercase 64-character hex."""


@dataclass(frozen=True, slots=True)
class ObservationClaim:
    """What an independent read of the external system established, and what followed from it."""

    mode: ObservationMode

    outcome: ObservationOutcome
    """CareLoop's own verification vocabulary, carried through unchanged and never collapsed."""

    settlement: Settlement
    """Read from a verdict the domain already reached and committed. Never decided here."""

    resource_id: str | None
    """The external system's identifier for the effect, or ``None`` if it is not safe to repeat.

    Present even when the read failed: a ``404`` on a confirmation id is evidence, and naming the
    resource is what lets that ``404`` be recognised as a read of *this* effect.
    """

    state_digest: str | None
    """Digest of the joint state the external system returned, or ``None`` if it returned none.

    Absent rather than invented. A digest of a state nobody observed would be a claim about the
    world made by a read that did not happen.
    """


class Witness(Protocol):
    """Somewhere to put a claim. Two methods, both returning nothing.

    Deliberately with no way to answer, report success, or be asked whether it is enabled. A
    witness that could be interrogated is a witness a caller could branch on, and the first branch
    is where telemetry starts deciding things.
    """

    def record_effect(self, claim: EffectClaim) -> None: ...

    def record_observation(self, claim: ObservationClaim) -> None: ...


@dataclass(frozen=True, slots=True)
class NullWitness:
    """A witness that does nothing at all. The application default.

    No state, no I/O, no branch. Frozen and field-less so it is a legal dataclass default on
    :class:`~careloop.graph.context.RunContext` without a factory, and so two of them are
    interchangeable.

    It exists instead of ``None`` for one reason: with it, the graph never asks whether a witness
    is present, and *"is telemetry on"* is not a question any production line of CareLoop can be
    written to answer.
    """

    def record_effect(self, claim: EffectClaim) -> None:
        return None

    def record_observation(self, claim: ObservationClaim) -> None:
        return None


#: The default every run gets unless a composition root supplies another.
NO_WITNESS: Final[Witness] = NullWitness()


# -- projections -------------------------------------------------------------------------------
#
# Pure functions of domain values. Each returns ``None`` when there is nothing truthful to claim,
# which is always the fail-closed direction: an absent claim fails a settlement evaluation, and an
# invented one would pass it.


def _authorised_state(
    observable: ObservableEffect, args: Mapping[str, str]
) -> dict[str, str] | None:
    """The externally held values **as CareLoop authorised and executed them**.

    Read from the proposal the policy engine allowed, and from nothing else — not from the recorded
    result, not from the external system's answer, not from a confirmation id. This is one half of
    the pair whose equality is the settlement evidence, and a half sourced from the other half
    would be evidence of nothing.

    Refuses rather than defaults. A missing argument or a value with no canonical form yields
    ``None``, because filling one in would have CareLoop compare what it did against a string it
    invented — the mistake :class:`careloop.tools.results.AppointmentBooked` already documents one
    layer down.
    """
    state: dict[str, str] = {}
    for field in observable.state_fields:
        if field not in args:
            return None
        text = canonical_value(args[field])
        if text is None:
            return None
        state[field] = text
    return state


def _observed_state(observable: ObservableEffect, observed: ActionResult) -> dict[str, str] | None:
    """The same field names, valued from **what the external system returned to a separate read**.

    Deliberately a second function over a second object rather than a parameter on the first. The
    two digests must not be able to share a source, and the cheapest way to guarantee that is for
    neither projection to be able to see the other's input.
    """
    state: dict[str, str] = {}
    for field in observable.state_fields:
        text = canonical_value(getattr(observed, field, None))
        if text is None:
            return None
        state[field] = text
    return state


def _resource_id(observable: ObservableEffect, result: ActionResult) -> str | None:
    """The external identifier, if it is one bounded token this project is willing to repeat."""
    found = getattr(result, observable.resource_id_field, None)
    if not isinstance(found, str) or RESOURCE_ID_PATTERN.fullmatch(found) is None:
        return None
    return found


def _effect_claim(
    *, action_type: str, dispatch: EffectDispatch, args: Mapping[str, str]
) -> EffectClaim | None:
    """Project one effect claim, or ``None`` if this action type holds no observable state.

    Gated on :data:`careloop.graph.verify.OBSERVABLE_STATE` rather than on the registry's effect
    tools. Three of the four effects are simulated by design and have no external system to hold a
    joint state, so a claim about one would be a claim nothing could ever check — and digesting an
    email's arguments would put a subject and a body through a hash for no reader.
    """
    observable = OBSERVABLE_STATE.get(action_type)
    if observable is None:
        return None
    state = _authorised_state(observable, args)
    if state is None:
        return None
    return EffectClaim(
        effect_type=action_type,
        dispatch=dispatch,
        fields=FIELD_SEPARATOR.join(observable.state_fields),
        state_digest=digest_mapping(state),
    )


def _observation_claim(
    *,
    action_type: str,
    verification: Verification,
    result: ActionResult,
    observed: ActionResult | None,
) -> ObservationClaim | None:
    """Project one observation claim, or ``None`` if this action type holds no observable state.

    **There is no parameter here through which an effect digest could arrive**, and that is the
    signature's job rather than an accident of it. ``result`` supplies the bounded resource id and
    nothing else; ``observed`` supplies the observed state and nothing else; ``verification``
    supplies a verdict the domain already reached.

    Every field is read from something already decided. ``mode`` follows
    :attr:`~careloop.graph.verify.Verification.externally_observed`, which means *an independent
    read took part in this verdict* — including one that was attempted and failed. It is
    deliberately not *"a read-back was configured for this run"*: a clinic that could not be
    reached was still consulted, and reporting that as ``NONE`` would be untrue in the direction
    that hides a failure.
    """
    observable = OBSERVABLE_STATE.get(action_type)
    if observable is None:
        return None
    state = None if observed is None else _observed_state(observable, observed)
    return ObservationClaim(
        mode=(
            ObservationMode.INDEPENDENT_READ
            if verification.externally_observed
            else ObservationMode.NONE
        ),
        outcome=verification.outcome,
        settlement=Settlement.VERIFIED if verification.passed else Settlement.NOT_CERTIFIED,
        resource_id=_resource_id(observable, result),
        state_digest=None if state is None else digest_mapping(state),
    )


# -- the seam ----------------------------------------------------------------------------------


def witness_effect(
    witness: Witness, *, action_type: str, dispatch: EffectDispatch, args: Mapping[str, str]
) -> None:
    """Record what happened to an external effect. Returns nothing and never raises.

    ``args`` are the arguments **policy authorised**, whichever node is calling. The execute node
    passes the proposal it is dispatching; the verify node, on a caregiver's re-check, passes that
    same proposal's arguments again — not the recorded result and not what the clinic just
    returned, so the effect digest has one source in every dispatch mode.
    """
    try:
        claim = _effect_claim(action_type=action_type, dispatch=dispatch, args=args)
        if claim is not None:
            witness.record_effect(claim)
    except Exception as failure:
        logger.debug("witness effect recording failed: %s", type(failure).__name__)


def witness_observation(
    witness: Witness,
    *,
    action_type: str,
    verification: Verification,
    result: ActionResult,
    observed: ActionResult | None,
) -> None:
    """Record what an independent read established. Returns nothing and never raises.

    Emits the observation half only. It has no ``dispatch`` parameter and builds no
    :class:`EffectClaim`, so a trace's effect claim can only come from a caller that holds the
    authorised arguments.
    """
    try:
        claim = _observation_claim(
            action_type=action_type,
            verification=verification,
            result=result,
            observed=observed,
        )
        if claim is not None:
            witness.record_observation(claim)
    except Exception as failure:
        logger.debug("witness observation recording failed: %s", type(failure).__name__)


__all__ = [
    "FIELD_SEPARATOR",
    "NO_WITNESS",
    "RESOURCE_ID_PATTERN",
    "EffectClaim",
    "EffectDispatch",
    "NullWitness",
    "ObservationClaim",
    "ObservationMode",
    "Settlement",
    "Witness",
    "witness_effect",
    "witness_observation",
]
