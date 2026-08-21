"""Closed vocabularies, measured attribute spellings, and the evaluator's own expectations.

Everything in this module is a constant. There is no logic here on purpose: the vocabularies are
what Patch C mutates, the attribute spellings are what Patch A measured, and the two expectation
constants are what make the evaluator independent of CareLoop. Keeping them in one file with no
behaviour means a change to any of them is visible as a change to *this* file.

**No interpolation, anywhere.** Every explanation and every error message is a fixed sentence
selected from a table by an enum member. A published evaluator result is a demo artefact and a
CloudWatch record, and a URL, a confirmation id, a digest or an attribute value has no business in
either. This is invariant 6's reasoning -- *denial text is echoed into the planner's context, so an
interpolated reason would be a self-inflicted injection channel* -- applied to a surface whose
consumer is a person rather than a model, and it costs nothing to hold.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------------------------
# Result vocabulary
# ---------------------------------------------------------------------------------------------


class Label(StrEnum):
    """The four outcomes an evaluation can reach.

    ``ERROR`` names the fourth outcome and is deliberately **not emitted**. Patch A proved that a
    result carrying ``label`` and ``explanation`` with no ``value`` survives the round trip, and
    that ``EvaluationResultContent`` declares ``errorCode`` and ``errorMessage`` -- it never proved
    that the service accepts the *value* ``"ERROR"`` in the label field. An error result therefore
    returns the two error fields alone. See :func:`evaluators.settlement.error_result`.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


class CheckId(StrEnum):
    """Every way an applicable trace can fail to prove settlement.

    A closed enum rather than free text, for two reasons that are not the same. It is what makes
    the result deterministic -- the same trace always produces the same code -- and it is what
    makes Patch C able to assert *which* check refused a fixture rather than that some check did.

    ``TRANSPORT_STATUS_AMBIGUOUS`` is carried by two checks, one for the write span and one for
    the read span. One code, because the fact is the same fact: the two status spellings Patch A
    measured disagree with each other, so the transport evidence establishes no outcome.
    """

    CLAIM_ABSENT = "CLAIM_ABSENT"
    CLAIM_AMBIGUOUS = "CLAIM_AMBIGUOUS"
    EFFECT_TYPE_MISLABELLED = "EFFECT_TYPE_MISLABELLED"
    MULTIPLE_WRITES = "MULTIPLE_WRITES"
    DISPATCH_CONTRADICTS_TRANSPORT = "DISPATCH_CONTRADICTS_TRANSPORT"
    TRANSPORT_STATUS_AMBIGUOUS = "TRANSPORT_STATUS_AMBIGUOUS"
    WRITE_NOT_SUCCESSFUL = "WRITE_NOT_SUCCESSFUL"
    NO_INDEPENDENT_OBSERVER = "NO_INDEPENDENT_OBSERVER"
    OBSERVATION_ABSENT_FROM_TRACE = "OBSERVATION_ABSENT_FROM_TRACE"
    RESOURCE_MISMATCH = "RESOURCE_MISMATCH"
    MULTIPLE_OBSERVATIONS = "MULTIPLE_OBSERVATIONS"
    OBSERVATION_NOT_SUCCESSFUL = "OBSERVATION_NOT_SUCCESSFUL"
    TIMING_UNAVAILABLE = "TIMING_UNAVAILABLE"
    OBSERVATION_PRECEDES_EFFECT = "OBSERVATION_PRECEDES_EFFECT"
    FIELD_SET_UNEXPECTED = "FIELD_SET_UNEXPECTED"
    DIGEST_MALFORMED = "DIGEST_MALFORMED"
    OUTCOME_CONTRADICTS_DIGESTS = "OUTCOME_CONTRADICTS_DIGESTS"
    STATE_DIGESTS_DIFFER = "STATE_DIGESTS_DIFFER"
    OUTCOME_NOT_MATCHED = "OUTCOME_NOT_MATCHED"
    SETTLEMENT_ABSENT = "SETTLEMENT_ABSENT"
    SETTLEMENT_NOT_VERIFIED = "SETTLEMENT_NOT_VERIFIED"


class NaReason(StrEnum):
    """Why a trace had no independently observable external effect to judge.

    Both members mean *there is no subject matter here*, and neither may be reached by a trace
    that carries clinic transport -- a write or a read against the clinic is exactly the thing this
    evaluator exists to judge, so hiding one behind a missing or mislabelled claim is a
    :attr:`CheckId.CLAIM_ABSENT` or :attr:`CheckId.EFFECT_TYPE_MISLABELLED` finding rather than an
    absence of subject matter.
    """

    NO_EFFECT_CLAIMED = "NO_EFFECT_CLAIMED"
    EFFECT_NOT_INDEPENDENTLY_OBSERVABLE = "EFFECT_NOT_INDEPENDENTLY_OBSERVABLE"


class ErrorCode(StrEnum):
    """Why the evaluator could not judge the input at all.

    Reserved for *this is not something I can evaluate* -- a malformed envelope, an unsupported
    schema or target, or a bug in this package. **A contradiction inside otherwise valid data is
    never an error**: it is the finding, and reporting a finding as infrastructure trouble is how
    a guard stops being read as a guard.
    """

    ENVELOPE_MALFORMED = "ENVELOPE_MALFORMED"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    UNSUPPORTED_EVALUATION_LEVEL = "UNSUPPORTED_EVALUATION_LEVEL"
    UNSUPPORTED_TARGET_KIND = "UNSUPPORTED_TARGET_KIND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    SPANS_MALFORMED = "SPANS_MALFORMED"
    SPAN_MISSING_TRACE_ID = "SPAN_MISSING_TRACE_ID"
    TARGET_TRACE_ABSENT = "TARGET_TRACE_ABSENT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------------------------
# The claim vocabulary -- the nine future telemetry attributes, and nothing else
# ---------------------------------------------------------------------------------------------


class Dispatch(StrEnum):
    """What ``careloop.effect.dispatch`` says happened to the effect *in this trace*.

    Three values, each naming a lifecycle this repository actually reaches:

    ``PERFORMED``
        ``ExecuteNode`` dispatched the call and an ``ACTION_EXECUTED`` entry was written. A clinic
        write must appear in this trace.

    ``SUPPRESSED``
        ``case.action_for(key)`` already held the effect, so ``DUPLICATE_SUPPRESSED`` was written
        and **no HTTP left the process**. A clinic write in this trace contradicts the claim.

    ``NOT_IN_THIS_TRACE``
        A caregiver's re-check, which runs ``("dispatch", "verify")`` and never reaches the
        execute node at all. The write belongs to an earlier trace. Same transport expectation as
        ``SUPPRESSED``, different fact about why.

    The last two are why a write is **not** globally mandatory. What is never optional is the
    independent read: see :mod:`evaluators.settlement`.
    """

    PERFORMED = "PERFORMED"
    SUPPRESSED = "SUPPRESSED"
    NOT_IN_THIS_TRACE = "NOT_IN_THIS_TRACE"


class ObservationMode(StrEnum):
    """Whether an independent observer took part, per ``careloop.observation.mode``."""

    INDEPENDENT_READ = "INDEPENDENT_READ"
    NONE = "NONE"


class ObservationOutcome(StrEnum):
    """What the independent read established, per ``careloop.observation.outcome``.

    The same five-member vocabulary CareLoop's own verification layer records, restated here
    rather than imported -- the whole point of this package is that it does not import that layer.
    Patch C is where the two are compared.
    """

    MATCHED = "MATCHED"
    DIVERGED = "DIVERGED"
    ABSENT = "ABSENT"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_OBSERVED = "NOT_OBSERVED"


class Settlement(StrEnum):
    """What CareLoop concluded about the case, per ``careloop.settlement``."""

    VERIFIED = "VERIFIED"
    NOT_CERTIFIED = "NOT_CERTIFIED"


ATTR_EFFECT_TYPE: Final[str] = "careloop.effect.type"
ATTR_EFFECT_DISPATCH: Final[str] = "careloop.effect.dispatch"
ATTR_EFFECT_FIELDS: Final[str] = "careloop.effect.fields"
ATTR_EFFECT_STATE_DIGEST: Final[str] = "careloop.effect.state_digest"
ATTR_OBSERVATION_MODE: Final[str] = "careloop.observation.mode"
ATTR_OBSERVATION_OUTCOME: Final[str] = "careloop.observation.outcome"
ATTR_OBSERVATION_RESOURCE_ID: Final[str] = "careloop.observation.resource_id"
ATTR_OBSERVATION_STATE_DIGEST: Final[str] = "careloop.observation.state_digest"
ATTR_SETTLEMENT: Final[str] = "careloop.settlement"

#: The complete future telemetry contract. **Nine attributes, and there is no tenth.**
#:
#: ``careloop.effect.action_key`` was dropped during the Patch A design and must not return under
#: that or any other name. Correlation is carried by :data:`ATTR_OBSERVATION_RESOURCE_ID` against
#: the clinic read's own URL -- evidence CareLoop does not author -- and by route, uniqueness and
#: ordering for the write. An identifier CareLoop mints and then checks against itself would prove
#: nothing that is not already assumed.
CLAIM_ATTRIBUTES: Final[tuple[str, ...]] = (
    ATTR_EFFECT_TYPE,
    ATTR_EFFECT_DISPATCH,
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_STATE_DIGEST,
    ATTR_OBSERVATION_MODE,
    ATTR_OBSERVATION_OUTCOME,
    ATTR_OBSERVATION_RESOURCE_ID,
    ATTR_OBSERVATION_STATE_DIGEST,
    ATTR_SETTLEMENT,
)


# ---------------------------------------------------------------------------------------------
# The evaluator's own expectations -- owned, never imported
# ---------------------------------------------------------------------------------------------

#: Effect types with a real external system that something other than CareLoop can read back.
#:
#: One member, matching CareLoop's ``INDEPENDENTLY_OBSERVABLE``, and declared here instead of
#: imported. Patch C adds the drift test.
OBSERVABLE_EFFECT_TYPES: Final[frozenset[str]] = frozenset({"book_appointment"})

#: The exact field set the clinic externally holds for a booking, in canonical sorted order.
#:
#: **This is the vacuity guard for the two state digests.** Two digests taken over an *empty*
#: joint state are trivially equal, so digest equality means nothing until the field set is pinned
#: -- which is why :attr:`CheckId.FIELD_SET_UNEXPECTED` is ordered ahead of
#: :attr:`CheckId.DIGEST_MALFORMED` and :attr:`CheckId.STATE_DIGESTS_DIFFER`.
#:
#: Sorted, because every canonical form in CareLoop is (``idempotency.py`` digests with
#: ``sort_keys=True``), and because an unsorted set would need a normalisation step that could
#: quietly absorb a duplicate field.
EXPECTED_FIELDS: Final[tuple[str, ...]] = (
    "patient_id",
    "provider_id",
    "reason",
    "slot_start",
)

#: How ``careloop.effect.fields`` encodes its field set. One encoding, not several.
FIELD_SEPARATOR: Final[str] = ","


# ---------------------------------------------------------------------------------------------
# Measured span spellings -- Patch A, docs/spike-findings.md section 9
# ---------------------------------------------------------------------------------------------

#: Old-spelling only. ``http.request.method`` is **absent** from real CareLoop spans (A4), so a
#: fallback for it would be untested code guarding nothing.
ATTR_HTTP_METHOD: Final[str] = "http.method"

#: Old-spelling only. ``url.full`` is **absent** from real CareLoop spans (A4).
ATTR_HTTP_URL: Final[str] = "http.url"

#: Both spellings are emitted on real spans (A4), which is why the resolver has a disagreement row.
ATTR_HTTP_STATUS_CODE: Final[str] = "http.status_code"
ATTR_HTTP_RESPONSE_STATUS_CODE: Final[str] = "http.response.status_code"

#: ``"UNSET"`` on a successful production span, never ``"OK"`` (A4). Only ``"ERROR"`` is a failure.
ATTR_OTEL_STATUS_CODE: Final[str] = "status.code"
SPAN_STATUS: Final[str] = "status"
SPAN_STATUS_CODE: Final[str] = "code"
OTEL_STATUS_ERROR: Final[str] = "ERROR"

#: camelCase first, per A1: *the evaluator should read the camelCase forms, which are the ones
#: AWS's own documentation and examples use*. The snake_case alias is the service's enrichment.
TRACE_ID_KEYS: Final[tuple[str, ...]] = ("traceId", "trace_id")

#: Original OTLP nanosecond timestamps, tried first.
#:
#: **Patch A recorded that both original timestamps arrive byte-for-byte as stored but never
#: recorded their field names**, so this tuple is a candidate list rather than a measurement, and
#: the ISO fallback below is the spelling Patch A *does* name verbatim. Both normalise to the same
#: canonical quantity -- integer nanoseconds -- so nothing ever compares across representations.
START_NANOS_KEYS: Final[tuple[str, ...]] = ("startTimeUnixNano", "start_time_unix_nano")
END_NANOS_KEYS: Final[tuple[str, ...]] = ("endTimeUnixNano", "end_time_unix_nano")

#: The AgentCore-enriched ISO timestamps (A1). Measured, named, and the fallback floor.
START_ISO_KEYS: Final[tuple[str, ...]] = ("start_time",)
END_ISO_KEYS: Final[tuple[str, ...]] = ("end_time",)


# ---------------------------------------------------------------------------------------------
# The clinic's routes -- path only, host-agnostic
# ---------------------------------------------------------------------------------------------

#: ``POST /clinic/appointments``. Demo World answers **201 on create and 200 on replay**, so
#: success is any 2xx and never an exact code.
CLINIC_WRITE_METHOD: Final[str] = "POST"
CLINIC_APPOINTMENTS_PATH: Final[tuple[str, ...]] = ("clinic", "appointments")

#: ``GET /clinic/appointments/{resource_id}`` -- the independent read-back, and the only span in a
#: CareLoop trace that carries the clinic's own identifier for the appointment.
CLINIC_READ_METHOD: Final[str] = "GET"

#: A resource id must be exactly one non-empty safe path token. RFC 3986 unreserved characters,
#: allowlisted rather than denylisted: a claimed id carrying ``/``, ``?``, ``#`` or ``%`` cannot
#: match a URL segment, so the correlation fails closed instead of being negotiated.
RESOURCE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~-]+")

#: Lowercase SHA-256, the shape ``careloop/tools/idempotency.py`` produces. The evaluator never
#: recomputes a digest -- it holds no values to recompute one from, and that is the privacy
#: property. It checks that both are well formed and that they agree.
DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------------------------------------
# Envelope constants -- Patch A, section 9 A6
# ---------------------------------------------------------------------------------------------

#: The one envelope version this evaluator was written against. ``evaluatorId`` and
#: ``evaluatorName`` are documented as envelope fields and **are not sent**, so nothing reads them.
SUPPORTED_SCHEMA_VERSION: Final[str] = "1.0"
SUPPORTED_EVALUATION_LEVEL: Final[str] = "TRACE"
KEY_SCHEMA_VERSION: Final[str] = "schemaVersion"
KEY_EVALUATION_LEVEL: Final[str] = "evaluationLevel"
KEY_EVALUATION_INPUT: Final[str] = "evaluationInput"
KEY_EVALUATION_TARGET: Final[str] = "evaluationTarget"
KEY_SESSION_SPANS: Final[str] = "sessionSpans"
KEY_TRACE_IDS: Final[str] = "traceIds"


# ---------------------------------------------------------------------------------------------
# Fixed explanations. Selected, never formatted.
# ---------------------------------------------------------------------------------------------

PASS_EXPLANATION: Final[str] = (
    "SETTLED: the trace carries an independent read of the external resource CareLoop claims, "
    "the read agrees with the recorded effect, and CareLoop's own claims do not contradict the "
    "transport evidence beside them."
)

CHECK_EXPLANATIONS: Final[Mapping[CheckId, str]] = {
    CheckId.CLAIM_ABSENT: (
        "CLAIM_ABSENT: the trace carries clinic transport but no CareLoop effect claim, so there "
        "is an external effect that nothing in the trace accounts for."
    ),
    CheckId.CLAIM_AMBIGUOUS: (
        "CLAIM_AMBIGUOUS: more than one span in the trace claims an effect, so which effect this "
        "evaluation is about cannot be established."
    ),
    CheckId.EFFECT_TYPE_MISLABELLED: (
        "EFFECT_TYPE_MISLABELLED: the trace carries clinic transport while claiming an effect "
        "type that no external system can observe."
    ),
    CheckId.MULTIPLE_WRITES: (
        "MULTIPLE_WRITES: more than one clinic write appears in the trace, so which write this "
        "evaluation is about cannot be established."
    ),
    CheckId.DISPATCH_CONTRADICTS_TRANSPORT: (
        "DISPATCH_CONTRADICTS_TRANSPORT: what CareLoop says it did with the effect disagrees with "
        "whether a clinic write appears in the trace."
    ),
    CheckId.TRANSPORT_STATUS_AMBIGUOUS: (
        "TRANSPORT_STATUS_AMBIGUOUS: the two HTTP status attributes on a clinic span disagree, so "
        "the transport evidence establishes no outcome for it."
    ),
    CheckId.WRITE_NOT_SUCCESSFUL: (
        "WRITE_NOT_SUCCESSFUL: the clinic write CareLoop says it performed is not recorded as "
        "having succeeded."
    ),
    CheckId.NO_INDEPENDENT_OBSERVER: (
        "NO_INDEPENDENT_OBSERVER: CareLoop does not claim an independent read of the external "
        "system, so nothing here can be certified against it."
    ),
    CheckId.OBSERVATION_ABSENT_FROM_TRACE: (
        "OBSERVATION_ABSENT_FROM_TRACE: CareLoop claims an independent read, and no clinic read "
        "naming a well-formed resource appears in the trace to support it."
    ),
    CheckId.RESOURCE_MISMATCH: (
        "RESOURCE_MISMATCH: the clinic read in the trace does not name the resource CareLoop "
        "claims to have observed."
    ),
    CheckId.MULTIPLE_OBSERVATIONS: (
        "MULTIPLE_OBSERVATIONS: more than one clinic read names the claimed resource, so which "
        "observation this evaluation rests on cannot be established."
    ),
    CheckId.OBSERVATION_NOT_SUCCESSFUL: (
        "OBSERVATION_NOT_SUCCESSFUL: the independent read of the claimed resource is not recorded "
        "as having succeeded."
    ),
    CheckId.TIMING_UNAVAILABLE: (
        "TIMING_UNAVAILABLE: the timestamps needed to order the write against the read could not "
        "be read, so the ordering claim cannot be checked."
    ),
    CheckId.OBSERVATION_PRECEDES_EFFECT: (
        "OBSERVATION_PRECEDES_EFFECT: the independent read began before the write it is offered "
        "as evidence for had finished."
    ),
    CheckId.FIELD_SET_UNEXPECTED: (
        "FIELD_SET_UNEXPECTED: the effect does not cover exactly the field set the external "
        "system holds, so an equality between the two state digests would prove nothing."
    ),
    CheckId.DIGEST_MALFORMED: (
        "DIGEST_MALFORMED: a joint state digest is missing or is not a lowercase 64-character "
        "hexadecimal value."
    ),
    CheckId.OUTCOME_CONTRADICTS_DIGESTS: (
        "OUTCOME_CONTRADICTS_DIGESTS: the observation outcome CareLoop reports disagrees with "
        "whether its own expected and observed state digests are equal."
    ),
    CheckId.STATE_DIGESTS_DIFFER: (
        "STATE_DIGESTS_DIFFER: the joint state CareLoop executed and the joint state it observed "
        "are not the same."
    ),
    CheckId.OUTCOME_NOT_MATCHED: (
        "OUTCOME_NOT_MATCHED: the independent read did not establish that the external system "
        "agrees with the recorded effect."
    ),
    CheckId.SETTLEMENT_ABSENT: (
        "SETTLEMENT_ABSENT: CareLoop records no settlement conclusion for this effect."
    ),
    CheckId.SETTLEMENT_NOT_VERIFIED: (
        "SETTLEMENT_NOT_VERIFIED: CareLoop did not conclude that this effect was verified."
    ),
}

NA_EXPLANATIONS: Final[Mapping[NaReason, str]] = {
    NaReason.NO_EFFECT_CLAIMED: (
        "NO_EFFECT_CLAIMED: the trace claims no external effect and carries no clinic transport, "
        "so there is nothing here for settlement integrity to be about."
    ),
    NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE: (
        "EFFECT_NOT_INDEPENDENTLY_OBSERVABLE: the effect this trace claims has no external system "
        "that anything other than CareLoop could read back."
    ),
}

ERROR_MESSAGES: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.ENVELOPE_MALFORMED: "The evaluation payload is not a mapping.",
    ErrorCode.UNSUPPORTED_SCHEMA_VERSION: (
        "The evaluation envelope declares a schema version this evaluator was not written against."
    ),
    ErrorCode.UNSUPPORTED_EVALUATION_LEVEL: (
        "This evaluator judges whole traces and was invoked at a different evaluation level."
    ),
    ErrorCode.UNSUPPORTED_TARGET_KIND: (
        "This evaluator requires a trace-id evaluation target and was given another kind."
    ),
    ErrorCode.AMBIGUOUS_TARGET: (
        "A trace-level evaluation must name exactly one target trace; this one does not."
    ),
    ErrorCode.SPANS_MALFORMED: "The evaluation input carries no usable list of spans.",
    ErrorCode.SPAN_MISSING_TRACE_ID: (
        "A supplied span carries no readable trace id, so it can be neither attributed nor "
        "safely discarded."
    ),
    ErrorCode.TARGET_TRACE_ABSENT: (
        "No supplied span belongs to the target trace, so there is nothing to evaluate."
    ),
    ErrorCode.INTERNAL_ERROR: (
        "The evaluator failed while judging this input and reports no result for it."
    ),
}


__all__ = [
    "ATTR_EFFECT_DISPATCH",
    "ATTR_EFFECT_FIELDS",
    "ATTR_EFFECT_STATE_DIGEST",
    "ATTR_EFFECT_TYPE",
    "ATTR_HTTP_METHOD",
    "ATTR_HTTP_RESPONSE_STATUS_CODE",
    "ATTR_HTTP_STATUS_CODE",
    "ATTR_HTTP_URL",
    "ATTR_OBSERVATION_MODE",
    "ATTR_OBSERVATION_OUTCOME",
    "ATTR_OBSERVATION_RESOURCE_ID",
    "ATTR_OBSERVATION_STATE_DIGEST",
    "ATTR_OTEL_STATUS_CODE",
    "ATTR_SETTLEMENT",
    "CHECK_EXPLANATIONS",
    "CLAIM_ATTRIBUTES",
    "CLINIC_APPOINTMENTS_PATH",
    "CLINIC_READ_METHOD",
    "CLINIC_WRITE_METHOD",
    "DIGEST_PATTERN",
    "END_ISO_KEYS",
    "END_NANOS_KEYS",
    "ERROR_MESSAGES",
    "EXPECTED_FIELDS",
    "FIELD_SEPARATOR",
    "KEY_EVALUATION_INPUT",
    "KEY_EVALUATION_LEVEL",
    "KEY_EVALUATION_TARGET",
    "KEY_SCHEMA_VERSION",
    "KEY_SESSION_SPANS",
    "KEY_TRACE_IDS",
    "NA_EXPLANATIONS",
    "OBSERVABLE_EFFECT_TYPES",
    "OTEL_STATUS_ERROR",
    "PASS_EXPLANATION",
    "RESOURCE_ID_PATTERN",
    "SPAN_STATUS",
    "SPAN_STATUS_CODE",
    "START_ISO_KEYS",
    "START_NANOS_KEYS",
    "SUPPORTED_EVALUATION_LEVEL",
    "SUPPORTED_SCHEMA_VERSION",
    "TRACE_ID_KEYS",
    "CheckId",
    "Dispatch",
    "ErrorCode",
    "Label",
    "NaReason",
    "ObservationMode",
    "ObservationOutcome",
    "Settlement",
]
