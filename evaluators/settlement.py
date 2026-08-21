"""The ordered deterministic decision procedure, and the four result shapes it can reach.

**The question.** Given one production trace, does what CareLoop *claims* about an external effect
agree with the transport evidence sitting beside the claim in the same trace? CareLoop authors the
claim attributes. It does not author the httpx spans -- those are written by instrumentation
underneath it, and delivered by AWS -- so the two are independent, and a disagreement between them
is a finding no amount of CareLoop-side correctness can hide.

**Two phases.** :func:`applicability` decides whether there is anything here to judge. Then
:data:`CHECKS` -- an ordered tuple of small named pure functions -- runs until one refuses. First
failure wins, and it is the first in *this* order, which is a decision rather than an accident.

**Three orderings are load-bearing and must survive Patch C:**

- :attr:`~evaluators.contract.CheckId.FIELD_SET_UNEXPECTED` comes **before** the digest checks.
  Two digests taken over an empty joint state are trivially equal, so digest equality proves
  nothing until the field set is pinned to exactly what the external system holds. Reorder these
  and the digest comparison becomes decorative.
- :attr:`~evaluators.contract.CheckId.OUTCOME_CONTRADICTS_DIGESTS` comes **before**
  ``STATE_DIGESTS_DIFFER`` and ``OUTCOME_NOT_MATCHED``. Placed after them it could never fire, and
  a guard that cannot fire is not a guard. Placed here, all three are independently reachable:
  this one catches a claim that contradicts *itself*, ``STATE_DIGESTS_DIFFER`` catches unequal
  digests under an outcome that does not claim agreement, and ``OUTCOME_NOT_MATCHED`` catches
  those outcomes with equal digests.
- :attr:`~evaluators.contract.CheckId.MULTIPLE_WRITES` comes **first**, because every later check
  that says "the write" needs there to be at most one.

**Ambiguity is always a finding and never a choice.** Two claim spans, two writes, two matching
reads, two disagreeing status spellings, two target traces -- each has its own code, and none is
resolved by picking a candidate. An evaluator that picks would be deciding the thing it was asked
to observe.

**Redundancy is deliberate in three places.** ``WRITE_NOT_SUCCESSFUL`` re-establishes that exactly
one write exists, and ``OBSERVATION_NOT_SUCCESSFUL`` re-establishes that exactly one matching read
exists, rather than assuming the earlier checks ran. Removing one check should turn its own tests
red without opening a hole that a *second* removal could walk through -- the same reasoning that
gives CareLoop's policy layer two enforcement points.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from evaluators.contract import (
    ATTR_EFFECT_DISPATCH,
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_STATE_DIGEST,
    ATTR_EFFECT_TYPE,
    ATTR_OBSERVATION_MODE,
    ATTR_OBSERVATION_OUTCOME,
    ATTR_OBSERVATION_RESOURCE_ID,
    ATTR_OBSERVATION_STATE_DIGEST,
    ATTR_SETTLEMENT,
    CHECK_EXPLANATIONS,
    DIGEST_PATTERN,
    ERROR_MESSAGES,
    EXPECTED_FIELDS,
    FIELD_SEPARATOR,
    NA_EXPLANATIONS,
    OBSERVABLE_EFFECT_TYPES,
    PASS_EXPLANATION,
    RESOURCE_ID_PATTERN,
    CheckId,
    Dispatch,
    ErrorCode,
    Label,
    NaReason,
    ObservationMode,
    ObservationOutcome,
    Settlement,
)
from evaluators.spans import (
    EnvelopeError,
    Span,
    StatusKind,
    attribute,
    clinic_read_resource,
    end_nanos,
    http_status,
    is_clinic_write,
    parse_envelope,
    start_nanos,
    successful,
)

# ---------------------------------------------------------------------------------------------
# What a check sees
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """The nine claim attributes, exactly as the span carried them.

    Deliberately **un-normalised**: every value is whatever was on the span, including ``None``
    for an attribute that was absent. Each check validates the attributes it owns, so "the value
    was missing" and "the value was wrong" are decided by the check that has an opinion about
    them rather than by a constructor that ran before any check could look.
    """

    effect_type: object
    dispatch: object
    fields: object
    effect_state_digest: object
    observation_mode: object
    observation_outcome: object
    observation_resource_id: object
    observation_state_digest: object
    settlement: object


@dataclass(frozen=True)
class ClinicRead:
    """One ``GET /clinic/appointments/{resource_id}`` span and the id its URL names."""

    span: Span
    resource_id: str


@dataclass(frozen=True)
class Evidence:
    """One claim and the clinic transport in the same trace. All a check is ever given."""

    claim: Claim
    writes: tuple[Span, ...]
    reads: tuple[ClinicRead, ...]


#: A check reads the evidence and either refuses with a reason or says nothing.
Check = Callable[[Evidence], CheckId | None]


# ---------------------------------------------------------------------------------------------
# Phase A -- is there anything here to judge?
# ---------------------------------------------------------------------------------------------


def applicability(spans: tuple[Span, ...]) -> Evidence | CheckId | NaReason:
    """Decide whether the trace has an independently observable external effect to judge.

    Returns the :class:`Evidence` when it does, a :class:`~evaluators.contract.CheckId` when the
    trace is a finding in its own right, and a :class:`~evaluators.contract.NaReason` when there is
    genuinely no subject matter.

    **Clinic transport can never be hidden behind a missing or mislabelled claim.** A trace that
    booked or read an appointment and says nothing about it is ``CLAIM_ABSENT``, and one that did
    so while claiming an unobservable effect type is ``EFFECT_TYPE_MISLABELLED``. Both are
    failures, because *not applicable* is a statement that nothing external happened, and the
    transport says otherwise.

    The claim is located by **attribute presence** and never by position in the span tree. That is
    what makes Patch A's "``invoke_graph`` is not parentless" finding inapplicable rather than
    handled: this evaluator identifies no root, so it cannot identify one wrongly.
    """
    claims = [span for span in spans if attribute(span, ATTR_EFFECT_TYPE) is not None]
    writes = tuple(span for span in spans if is_clinic_write(span))

    reads: list[ClinicRead] = []
    for span in spans:
        resource = clinic_read_resource(span)
        if resource is not None:
            reads.append(ClinicRead(span, resource))

    transport = bool(writes or reads)

    if not claims:
        return CheckId.CLAIM_ABSENT if transport else NaReason.NO_EFFECT_CLAIMED
    if len(claims) > 1:
        return CheckId.CLAIM_AMBIGUOUS

    claim = _claim_from(claims[0])
    observable = isinstance(claim.effect_type, str) and claim.effect_type in OBSERVABLE_EFFECT_TYPES
    if not observable:
        if transport:
            return CheckId.EFFECT_TYPE_MISLABELLED
        return NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE

    return Evidence(claim=claim, writes=writes, reads=tuple(reads))


def _claim_from(span: Span) -> Claim:
    """Read the nine attributes off the claim span, verbatim."""
    return Claim(
        effect_type=attribute(span, ATTR_EFFECT_TYPE),
        dispatch=attribute(span, ATTR_EFFECT_DISPATCH),
        fields=attribute(span, ATTR_EFFECT_FIELDS),
        effect_state_digest=attribute(span, ATTR_EFFECT_STATE_DIGEST),
        observation_mode=attribute(span, ATTR_OBSERVATION_MODE),
        observation_outcome=attribute(span, ATTR_OBSERVATION_OUTCOME),
        observation_resource_id=attribute(span, ATTR_OBSERVATION_RESOURCE_ID),
        observation_state_digest=attribute(span, ATTR_OBSERVATION_STATE_DIGEST),
        settlement=attribute(span, ATTR_SETTLEMENT),
    )


# ---------------------------------------------------------------------------------------------
# Reading the claim -- small helpers each check shares
# ---------------------------------------------------------------------------------------------


def _member(vocabulary: Any, value: object) -> Any:
    """The vocabulary member ``value`` names, or ``None`` if it names none.

    ``isinstance(value, str)`` first, so a non-string attribute cannot be coerced into a member by
    the enum's own value lookup.
    """
    if not isinstance(value, str):
        return None
    try:
        return vocabulary(value)
    except ValueError:
        return None


def _dispatch(evidence: Evidence) -> Dispatch | None:
    return _member(Dispatch, evidence.claim.dispatch)


def _claimed_resource(evidence: Evidence) -> str | None:
    """The claimed resource id, if it is a single well-formed path token.

    Validated as a whole-string match against an allowlist of RFC 3986 unreserved characters. A
    claimed id carrying a separator could otherwise be spelled to match more of a URL than it
    should, and correlation would stop being an equality between two names for one thing.
    """
    claimed = evidence.claim.observation_resource_id
    if not isinstance(claimed, str):
        return None
    if RESOURCE_ID_PATTERN.fullmatch(claimed) is None:
        return None
    return claimed


def _matching_reads(evidence: Evidence) -> tuple[ClinicRead, ...]:
    """Every clinic read in the trace whose URL names exactly the claimed resource."""
    claimed = _claimed_resource(evidence)
    if claimed is None:
        return ()
    return tuple(read for read in evidence.reads if read.resource_id == claimed)


def _observed_span(evidence: Evidence) -> Span | None:
    """The one read this evaluation rests on, or ``None`` if there is not exactly one."""
    matching = _matching_reads(evidence)
    return matching[0].span if len(matching) == 1 else None


def _the_write(evidence: Evidence) -> Span | None:
    """The one clinic write in the trace, or ``None`` if there is not exactly one."""
    return evidence.writes[0] if len(evidence.writes) == 1 else None


def _declared_fields(evidence: Evidence) -> tuple[str, ...] | None:
    """The claimed field set, split on the one encoding this contract defines.

    A comma-joined string and nothing else. Accepting a list as well would be a second encoding
    for one fact, and Patch D is written against this contract rather than the other way round.
    """
    declared = evidence.claim.fields
    if not isinstance(declared, str):
        return None
    return tuple(part.strip() for part in declared.split(FIELD_SEPARATOR))


def _well_formed_digests(evidence: Evidence) -> tuple[str, str] | None:
    """Both joint state digests, if both are lowercase 64-character hexadecimal."""
    expected = evidence.claim.effect_state_digest
    observed = evidence.claim.observation_state_digest
    for digest in (expected, observed):
        if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
            return None
    return str(expected), str(observed)


def _digests_agree(evidence: Evidence) -> bool:
    """Whether the two digests are well formed *and* equal. Malformed is never equal."""
    both = _well_formed_digests(evidence)
    return both is not None and both[0] == both[1]


# ---------------------------------------------------------------------------------------------
# The checks, in order
# ---------------------------------------------------------------------------------------------


def _multiple_writes(evidence: Evidence) -> CheckId | None:
    """More than one clinic write makes "the write" undefined for every check after this."""
    if len(evidence.writes) > 1:
        return CheckId.MULTIPLE_WRITES
    return None


def _dispatch_contradicts_transport(evidence: Evidence) -> CheckId | None:
    """What CareLoop says it did with the effect, against whether a write is in the trace.

    Owns the attribute outright: an absent or unrecognised ``careloop.effect.dispatch`` fails here,
    because a claim that says nothing about dispatch cannot be reconciled with the transport, which
    is exactly what this check asserts.

    ``SUPPRESSED`` and ``NOT_IN_THIS_TRACE`` both forbid a write. That is what keeps the write from
    being globally mandatory -- a duplicate CareLoop suppressed from its own ledger, and a
    caregiver's re-check that never reaches the execute node, are both traces where no HTTP write
    belongs. What neither of them relaxes is the independent read.
    """
    dispatch = _dispatch(evidence)
    if dispatch is None:
        return CheckId.DISPATCH_CONTRADICTS_TRANSPORT
    if dispatch is Dispatch.PERFORMED and not evidence.writes:
        return CheckId.DISPATCH_CONTRADICTS_TRANSPORT
    if dispatch is not Dispatch.PERFORMED and evidence.writes:
        return CheckId.DISPATCH_CONTRADICTS_TRANSPORT
    return None


def _write_status_ambiguous(evidence: Evidence) -> CheckId | None:
    """The write's two status spellings disagree, so its outcome is not established."""
    if _dispatch(evidence) is not Dispatch.PERFORMED:
        return None
    write = _the_write(evidence)
    if write is None:
        return None
    if http_status(write).kind is StatusKind.AMBIGUOUS:
        return CheckId.TRANSPORT_STATUS_AMBIGUOUS
    return None


def _write_not_successful(evidence: Evidence) -> CheckId | None:
    """A write CareLoop says it performed must be recorded as having succeeded.

    Re-establishes that exactly one write exists rather than assuming
    :func:`_dispatch_contradicts_transport` ran, so removing that check alone cannot open a path
    to ``PASS``.
    """
    if _dispatch(evidence) is not Dispatch.PERFORMED:
        return None
    write = _the_write(evidence)
    if write is None or not successful(write):
        return CheckId.WRITE_NOT_SUCCESSFUL
    return None


def _no_independent_observer(evidence: Evidence) -> CheckId | None:
    """Settlement requires an independent read, in every dispatch mode without exception.

    This is the transport-level echo of CareLoop's own rule that a re-check which reads nothing
    certifies nothing. Absent, unrecognised and ``NONE`` all fail here.
    """
    if _member(ObservationMode, evidence.claim.observation_mode) is not (
        ObservationMode.INDEPENDENT_READ
    ):
        return CheckId.NO_INDEPENDENT_OBSERVER
    return None


def _observation_absent_from_trace(evidence: Evidence) -> CheckId | None:
    """A claimed independent read must have a clinic read in this trace behind it.

    Owns the resource id's well-formedness, because an id that could name nothing cannot be
    supported by any read. **In this trace**, always: a re-check may rest on a write from an
    earlier trace, never on an observation from one.
    """
    if _claimed_resource(evidence) is None:
        return CheckId.OBSERVATION_ABSENT_FROM_TRACE
    if not evidence.reads:
        return CheckId.OBSERVATION_ABSENT_FROM_TRACE
    return None


def _resource_mismatch(evidence: Evidence) -> CheckId | None:
    """The trace read *something*, and it is not the resource CareLoop claims it observed."""
    if not _matching_reads(evidence):
        return CheckId.RESOURCE_MISMATCH
    return None


def _multiple_observations(evidence: Evidence) -> CheckId | None:
    """Two reads of the claimed resource make "the observation" undefined."""
    if len(_matching_reads(evidence)) > 1:
        return CheckId.MULTIPLE_OBSERVATIONS
    return None


def _read_status_ambiguous(evidence: Evidence) -> CheckId | None:
    """The read's two status spellings disagree, so its outcome is not established."""
    read = _observed_span(evidence)
    if read is None:
        return None
    if http_status(read).kind is StatusKind.AMBIGUOUS:
        return CheckId.TRANSPORT_STATUS_AMBIGUOUS
    return None


def _observation_not_successful(evidence: Evidence) -> CheckId | None:
    """Exactly one matching read, and it succeeded.

    The positive requirement, stated here rather than inferred from the three checks above. It is
    what makes a ``PASS`` impossible if all of them were removed together.
    """
    read = _observed_span(evidence)
    if read is None or not successful(read):
        return CheckId.OBSERVATION_NOT_SUCCESSFUL
    return None


def _timing_unavailable(evidence: Evidence) -> CheckId | None:
    """The timestamps the ordering claim consumes must be readable.

    Exactly two of them -- the write's end and the read's start -- because those are what
    :func:`_observation_precedes_effect` compares. A third requirement with no consumer would be a
    rule nothing enforces.

    **Never a silent skip.** When a write is in this trace, unreadable timing is a failure. When
    no write is in this trace -- ``SUPPRESSED`` or ``NOT_IN_THIS_TRACE`` -- there is no ordering
    relation between two events in this trace to assert, so there is nothing to fail to check.
    That is a scope, not an exemption: the read itself is still required, still has to name the
    claimed resource, and still has to have succeeded.
    """
    if _dispatch(evidence) is not Dispatch.PERFORMED:
        return None
    write = _the_write(evidence)
    read = _observed_span(evidence)
    if write is None or read is None:
        return None
    if end_nanos(write) is None or start_nanos(read) is None:
        return CheckId.TIMING_UNAVAILABLE
    return None


def _observation_precedes_effect(evidence: Evidence) -> CheckId | None:
    """A read offered as evidence for a write must not have begun before that write finished."""
    if _dispatch(evidence) is not Dispatch.PERFORMED:
        return None
    write = _the_write(evidence)
    read = _observed_span(evidence)
    if write is None or read is None:
        return None
    finished = end_nanos(write)
    began = start_nanos(read)
    if finished is None or began is None:
        return None
    if began < finished:
        return CheckId.OBSERVATION_PRECEDES_EFFECT
    return None


def _field_set_unexpected(evidence: Evidence) -> CheckId | None:
    """The effect must cover exactly the field set the external system holds.

    **The vacuity guard for both digest checks**, and the reason it is ordered ahead of them: two
    digests over an empty joint state are equal, so an evaluator that compared digests without
    pinning the field set would accept a claim that committed to nothing.
    """
    if _declared_fields(evidence) != EXPECTED_FIELDS:
        return CheckId.FIELD_SET_UNEXPECTED
    return None


def _digest_malformed(evidence: Evidence) -> CheckId | None:
    """Both joint state digests must be lowercase 64-character hexadecimal."""
    if _well_formed_digests(evidence) is None:
        return CheckId.DIGEST_MALFORMED
    return None


def _outcome_contradicts_digests(evidence: Evidence) -> CheckId | None:
    """The reported outcome must agree with CareLoop's own digests about agreement.

    Ordered ahead of the two checks that would otherwise subsume it, so that it can fire at all.
    A claim reporting ``MATCHED`` over unequal digests, or ``DIVERGED`` over equal ones, is
    internally inconsistent -- a different and more serious fact than a straightforward
    disagreement with the external system.
    """
    outcome = _member(ObservationOutcome, evidence.claim.observation_outcome)
    agree = _digests_agree(evidence)
    if outcome is ObservationOutcome.MATCHED and not agree:
        return CheckId.OUTCOME_CONTRADICTS_DIGESTS
    if outcome is ObservationOutcome.DIVERGED and agree:
        return CheckId.OUTCOME_CONTRADICTS_DIGESTS
    return None


def _state_digests_differ(evidence: Evidence) -> CheckId | None:
    """The joint state executed and the joint state observed must be the same."""
    if not _digests_agree(evidence):
        return CheckId.STATE_DIGESTS_DIFFER
    return None


def _outcome_not_matched(evidence: Evidence) -> CheckId | None:
    """The independent read must have established agreement, not merely have happened."""
    if _member(ObservationOutcome, evidence.claim.observation_outcome) is not (
        ObservationOutcome.MATCHED
    ):
        return CheckId.OUTCOME_NOT_MATCHED
    return None


def _settlement_absent(evidence: Evidence) -> CheckId | None:
    """CareLoop must record some settlement conclusion, from the closed vocabulary."""
    if _member(Settlement, evidence.claim.settlement) is None:
        return CheckId.SETTLEMENT_ABSENT
    return None


def _settlement_not_verified(evidence: Evidence) -> CheckId | None:
    """And that conclusion must be that the effect was verified."""
    if _member(Settlement, evidence.claim.settlement) is not Settlement.VERIFIED:
        return CheckId.SETTLEMENT_NOT_VERIFIED
    return None


#: The decision procedure. Ordered, first-failure-wins, and replaceable element by element.
#:
#: Patch C's principal seam. Removing, reordering or replacing one entry is a mutation whose blast
#: radius should be exactly the tests written for it -- which is why every check is a module-level
#: named function taking only :class:`Evidence` and returning only a
#: :class:`~evaluators.contract.CheckId` or ``None``.
CHECKS: Final[tuple[Check, ...]] = (
    _multiple_writes,
    _dispatch_contradicts_transport,
    _write_status_ambiguous,
    _write_not_successful,
    _no_independent_observer,
    _observation_absent_from_trace,
    _resource_mismatch,
    _multiple_observations,
    _read_status_ambiguous,
    _observation_not_successful,
    _timing_unavailable,
    _observation_precedes_effect,
    _field_set_unexpected,
    _digest_malformed,
    _outcome_contradicts_digests,
    _state_digests_differ,
    _outcome_not_matched,
    _settlement_absent,
    _settlement_not_verified,
)


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------


def pass_result() -> dict[str, Any]:
    """A settled trace."""
    return {"label": Label.PASS.value, "value": 1.0, "explanation": PASS_EXPLANATION}


def fail_result(check: CheckId) -> dict[str, Any]:
    """An applicable trace that did not prove settlement, named by the check that refused it."""
    return {"label": Label.FAIL.value, "value": 0.0, "explanation": CHECK_EXPLANATIONS[check]}


def not_applicable_result(reason: NaReason) -> dict[str, Any]:
    """A trace with no independently observable external effect.

    **The ``value`` key is absent, not ``None``.** Patch A proved the omission survives the round
    trip uncoerced, which is what keeps an inapplicable trace out of every average at the schema
    level rather than by convention.
    """
    return {"label": Label.NOT_APPLICABLE.value, "explanation": NA_EXPLANATIONS[reason]}


def error_result(code: ErrorCode) -> dict[str, Any]:
    """An input this evaluator cannot judge.

    Carries ``errorCode`` and ``errorMessage`` and **no label**. Both fields are declared members
    of ``EvaluationResultContent``; a label value of ``"ERROR"`` is not something Patch A proved
    the service accepts, and inventing a shape nobody measured is the one thing this package is
    least entitled to do. :attr:`~evaluators.contract.Label.ERROR` still names the outcome for
    anything reasoning about it in Python.
    """
    return {"errorCode": code.value, "errorMessage": ERROR_MESSAGES[code]}


def assess(payload: Mapping[str, Any] | object) -> dict[str, Any]:
    """Judge one AgentCore evaluation payload. Pure, total for expected inputs, deterministic.

    No clock, no randomness, no environment and no I/O, so the same payload always produces the
    same dictionary. That is not a nicety: an evaluator whose answer could move is one whose
    ``PASS`` says nothing about the trace.
    """
    try:
        spans = parse_envelope(payload)
    except EnvelopeError as refusal:
        return error_result(refusal.code)

    outcome = applicability(spans)
    if isinstance(outcome, NaReason):
        return not_applicable_result(outcome)
    if isinstance(outcome, CheckId):
        return fail_result(outcome)

    for check in CHECKS:
        refused = check(outcome)
        if refused is not None:
            return fail_result(refused)
    return pass_result()


__all__ = [
    "CHECKS",
    "Check",
    "Claim",
    "ClinicRead",
    "Evidence",
    "applicability",
    "assess",
    "error_result",
    "fail_result",
    "not_applicable_result",
    "pass_result",
]
