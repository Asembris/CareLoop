"""Reading the span shape AgentCore actually delivers, and nothing wider.

Every accessor here is a pure function over a ``Mapping``. None of them raises; the only exception
in this module is :exc:`EnvelopeError`, which :func:`parse_envelope` uses to say *this is not
something the evaluator can judge*. An accessor that cannot read what it was asked for returns
``None``, and the check that wanted it decides what that means -- which is what keeps "the
attribute was missing" a finding rather than a crash.

**The boundary is deliberately narrow.** Patch A measured what a code-based evaluator's Lambda
receives, and this module reads that and only that. Every tolerance here is a tolerance Patch A
justified; every spelling absent here is a spelling Patch A found absent. *False tolerance is a
false-PASS surface*: an accessor that tried every OpenTelemetry convention would silently accept a
trace shape CareLoop does not emit, and the evaluator would then be judging a hypothesis.

Three measured facts drive most of what follows, all from ``docs/spike-findings.md`` section 9:

- ``http.method`` and ``http.url`` are the **only** spellings emitted. ``http.request.method`` and
  ``url.full`` are absent, so they are not read.
- ``http.status_code`` **and** ``http.response.status_code`` are both emitted. Two independent
  sources for one fact is why :func:`http_status` has a disagreement outcome rather than a
  preference order -- picking one and ignoring the other would resolve a contradiction by fiat.
- ``status.code`` reads ``"UNSET"`` on a successful span, never ``"OK"``. Success is *2xx and not
  ``ERROR``*. A contract requiring ``"OK"`` would have failed every real CareLoop trace while
  passing every hand-built fixture, which is the sharpest thing Patch A found.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from evaluators.contract import (
    ATTR_HTTP_METHOD,
    ATTR_HTTP_RESPONSE_STATUS_CODE,
    ATTR_HTTP_STATUS_CODE,
    ATTR_HTTP_URL,
    ATTR_OTEL_STATUS_CODE,
    CLINIC_APPOINTMENTS_PATH,
    CLINIC_READ_METHOD,
    CLINIC_WRITE_METHOD,
    END_ISO_KEYS,
    END_NANOS_KEYS,
    KEY_EVALUATION_INPUT,
    KEY_EVALUATION_LEVEL,
    KEY_EVALUATION_TARGET,
    KEY_SCHEMA_VERSION,
    KEY_SESSION_SPANS,
    KEY_TRACE_IDS,
    OTEL_STATUS_ERROR,
    SPAN_STATUS,
    SPAN_STATUS_CODE,
    START_ISO_KEYS,
    START_NANOS_KEYS,
    SUPPORTED_EVALUATION_LEVEL,
    SUPPORTED_SCHEMA_VERSION,
    TRACE_ID_KEYS,
    ErrorCode,
)

#: One span, as delivered. A mapping and nothing more -- there is no span class, because a class
#: would invite a constructor that could normalise something away before a check could see it.
Span = Mapping[str, Any]


class EnvelopeError(Exception):
    """The evaluation payload is not something this evaluator can judge.

    Carries an :class:`~evaluators.contract.ErrorCode` and **no payload-derived text**. The message
    is the code's own name, so even an exception that somehow reached a log cannot carry span
    content with it.
    """

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code: Final[ErrorCode] = code


# ---------------------------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------------------------


def parse_envelope(payload: object) -> tuple[Span, ...]:
    """Validate the evaluator envelope and return the spans belonging to the target trace.

    The envelope Patch A captured is smaller than the documentation describes -- ``evaluatorId``
    and ``evaluatorName`` are documented and **are not sent** -- so nothing here reads them, and
    ``evaluationReferenceInputs`` is neither read nor required.

    Two strictnesses are deliberate and both fail closed. Exactly **one** target trace is accepted:
    one ``EvaluationResultContent`` describes one trace, and nobody measured what the service does
    with a batch, so guessing a multi-result semantics is not on offer. And **every** supplied span
    must carry a readable trace id -- silently dropping an unattributable span is a way to drop the
    clinic write and turn a ``FAIL`` into a ``PASS``.

    Raises:
        EnvelopeError: naming which of the envelope's requirements was not met.
    """
    if not isinstance(payload, Mapping):
        raise EnvelopeError(ErrorCode.ENVELOPE_MALFORMED)

    if payload.get(KEY_SCHEMA_VERSION) != SUPPORTED_SCHEMA_VERSION:
        raise EnvelopeError(ErrorCode.UNSUPPORTED_SCHEMA_VERSION)

    if payload.get(KEY_EVALUATION_LEVEL) != SUPPORTED_EVALUATION_LEVEL:
        raise EnvelopeError(ErrorCode.UNSUPPORTED_EVALUATION_LEVEL)

    target = payload.get(KEY_EVALUATION_TARGET)
    if not isinstance(target, Mapping) or set(target) != {KEY_TRACE_IDS}:
        raise EnvelopeError(ErrorCode.UNSUPPORTED_TARGET_KIND)

    named = target[KEY_TRACE_IDS]
    if not isinstance(named, list) or len(named) != 1:
        raise EnvelopeError(ErrorCode.AMBIGUOUS_TARGET)
    wanted = named[0]
    if not isinstance(wanted, str) or not wanted:
        raise EnvelopeError(ErrorCode.AMBIGUOUS_TARGET)

    supplied = payload.get(KEY_EVALUATION_INPUT)
    if not isinstance(supplied, Mapping):
        raise EnvelopeError(ErrorCode.SPANS_MALFORMED)
    delivered = supplied.get(KEY_SESSION_SPANS)
    if not isinstance(delivered, list) or not delivered:
        raise EnvelopeError(ErrorCode.SPANS_MALFORMED)
    if not all(isinstance(span, Mapping) for span in delivered):
        raise EnvelopeError(ErrorCode.SPANS_MALFORMED)

    participating: list[Span] = []
    for span in delivered:
        belongs_to = trace_id(span)
        if belongs_to is None:
            raise EnvelopeError(ErrorCode.SPAN_MISSING_TRACE_ID)
        if belongs_to == wanted:
            participating.append(span)

    if not participating:
        raise EnvelopeError(ErrorCode.TARGET_TRACE_ABSENT)
    return tuple(participating)


def trace_id(span: Span) -> str | None:
    """The trace this span belongs to, camelCase spelling first.

    Both spellings arrive -- the service adds ``trace_id`` beside the original ``traceId`` -- and
    Patch A's guidance is to read the camelCase forms, which are the ones AWS's own documentation
    and examples use. The alias is accepted because it is measured to be there, not in case it is.
    """
    for key in TRACE_ID_KEYS:
        found = span.get(key)
        if isinstance(found, str) and found:
            return found
    return None


# ---------------------------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------------------------


def attribute(span: Span, name: str) -> object:
    """One attribute of ``span``, by its exact dotted name.

    Two locations with a fixed precedence: the ``attributes`` mapping, then the same dotted key
    flattened onto the span itself. That absorbs whether the delivered shape nests attributes or
    flattens them -- both forms appear in Patch A's record -- **without** becoming a search. There
    is deliberately no recursive walk and no case folding: an accessor that goes looking will
    eventually find something, and what it finds will not be what the contract meant.
    """
    attributes = span.get("attributes")
    if isinstance(attributes, Mapping) and name in attributes:
        return attributes[name]
    return span.get(name)


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------


def http_method(span: Span) -> str | None:
    """The request method, upper-cased, from the one spelling production emits."""
    found = attribute(span, ATTR_HTTP_METHOD)
    if not isinstance(found, str):
        return None
    return found.strip().upper() or None


def url_path_segments(span: Span) -> tuple[str, ...] | None:
    """The URL's path, split into segments, with query and fragment discarded.

    Host-agnostic on purpose: a deployed CareLoop, a loopback Demo World and a test fixture all
    reach the same routes at different authorities, and the route is the evidence.

    Returns ``None`` for a path carrying an internal empty segment. ``//`` is not a path this
    clinic serves, and normalising it away would make ``/clinic//appointments`` classify as the
    write route.
    """
    found = attribute(span, ATTR_HTTP_URL)
    if not isinstance(found, str):
        return None
    trimmed = urlsplit(found).path.strip("/")
    if not trimmed:
        return ()
    segments = tuple(trimmed.split("/"))
    if any(not segment for segment in segments):
        return None
    return segments


def is_clinic_write(span: Span) -> bool:
    """Whether this span is ``POST /clinic/appointments``."""
    return (
        http_method(span) == CLINIC_WRITE_METHOD
        and url_path_segments(span) == CLINIC_APPOINTMENTS_PATH
    )


def clinic_read_resource(span: Span) -> str | None:
    """The resource id this span reads back, or ``None`` if it is not a clinic read.

    ``GET /clinic/appointments/{resource_id}`` -- the independent read, and the one span in a
    CareLoop trace carrying the clinic's own identifier for the appointment. The segment is
    returned exactly as the URL holds it and is **not** validated here: whether it is a well-formed
    identifier is a question about CareLoop's *claim*, and correlation is exact string equality
    between the two. A URL segment that could never be a valid claimed id therefore simply fails to
    match, which is the fail-closed direction.
    """
    if http_method(span) != CLINIC_READ_METHOD:
        return None
    segments = url_path_segments(span)
    if segments is None or len(segments) != len(CLINIC_APPOINTMENTS_PATH) + 1:
        return None
    if segments[: len(CLINIC_APPOINTMENTS_PATH)] != CLINIC_APPOINTMENTS_PATH:
        return None
    return segments[-1]


class StatusKind(StrEnum):
    """Whether the transport evidence establishes a status for a span."""

    RESOLVED = "RESOLVED"
    """Exactly one status is established, whether or not it means success."""

    AMBIGUOUS = "AMBIGUOUS"
    """Both spellings are present and disagree, so no status is established."""

    ABSENT = "ABSENT"
    """Neither spelling is readable."""


@dataclass(frozen=True)
class HttpStatus:
    """The resolved HTTP status of one span. ``code`` is set only when ``kind`` is ``RESOLVED``."""

    kind: StatusKind
    code: int | None = None


def http_status(span: Span) -> HttpStatus:
    """Resolve the two measured status spellings into one answer.

    ==========================  ==========================  ==============
    ``http.status_code``        ``http.response.status_code``  Result
    ==========================  ==========================  ==============
    present                     absent                      that value
    absent                      present                     that value
    present, equal              present, equal              that value
    present, **disagreeing**    present, **disagreeing**    ``AMBIGUOUS``
    absent                      absent                      ``ABSENT``
    ==========================  ==========================  ==============

    A disagreement is not resolved by preferring one spelling. Both are emitted by the same
    instrumentation about the same response, so if they differ, something is wrong with the
    evidence and the honest answer is that no outcome is established.
    """
    primary = _as_int(attribute(span, ATTR_HTTP_STATUS_CODE))
    secondary = _as_int(attribute(span, ATTR_HTTP_RESPONSE_STATUS_CODE))
    if primary is None and secondary is None:
        return HttpStatus(StatusKind.ABSENT)
    if primary is not None and secondary is not None and primary != secondary:
        return HttpStatus(StatusKind.AMBIGUOUS)
    return HttpStatus(StatusKind.RESOLVED, primary if primary is not None else secondary)


def otel_status_code(span: Span) -> str | None:
    """The OpenTelemetry status code, upper-cased, from the nested object or the flat attribute."""
    status = span.get(SPAN_STATUS)
    if isinstance(status, Mapping):
        nested = status.get(SPAN_STATUS_CODE)
        if isinstance(nested, str):
            return nested.strip().upper() or None
    flat = attribute(span, ATTR_OTEL_STATUS_CODE)
    if isinstance(flat, str):
        return flat.strip().upper() or None
    return None


def successful(span: Span) -> bool:
    """Whether this span records a successful call.

    **2xx and not ``ERROR``.** Never *is ``OK``*: production spans carry ``UNSET`` on success, so
    requiring ``OK`` would refuse every real CareLoop trace while accepting every fixture written
    from an OpenTelemetry reference rather than from a measurement.

    2xx rather than an exact code, because Demo World answers **201 on create and 200 on replay**
    for the same route -- a retry the client did not know about is still a successful write.
    """
    status = http_status(span)
    if status.kind is not StatusKind.RESOLVED or status.code is None:
        return False
    if not 200 <= status.code < 300:
        return False
    return otel_status_code(span) != OTEL_STATUS_ERROR


# ---------------------------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------------------------

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def start_nanos(span: Span) -> int | None:
    """When this span began, in integer nanoseconds since the epoch."""
    return _nanos(span, START_NANOS_KEYS, START_ISO_KEYS)


def end_nanos(span: Span) -> int | None:
    """When this span ended, in integer nanoseconds since the epoch."""
    return _nanos(span, END_NANOS_KEYS, END_ISO_KEYS)


def _nanos(span: Span, nano_keys: tuple[str, ...], iso_keys: tuple[str, ...]) -> int | None:
    """One quantity -- nanoseconds since the epoch -- from whichever representation is present.

    **Original OTLP nanoseconds first, the AgentCore-enriched ISO string second.** Patch A recorded
    that both original timestamps arrive "byte-for-byte as stored" but never recorded their field
    names, so the nano key list is a candidate list and the ISO spelling is the one Patch A names
    verbatim. Two representations of the same instant, normalised to one integer, is not two
    clocks: the service derives the ISO form from the original, so they cannot disagree about when
    the span happened. What would be two clocks is comparing a nanosecond field on one span against
    an ISO field on another *in different units*, and that cannot happen here because nothing
    outside this function ever sees anything but an ``int``.

    Returns ``None`` when neither is readable, which the ordering checks turn into
    :attr:`~evaluators.contract.CheckId.TIMING_UNAVAILABLE` -- a failure, never a skipped check.
    """
    for key in nano_keys:
        found = _as_int(span.get(key))
        if found is not None:
            return found
    for key in iso_keys:
        text = span.get(key)
        if isinstance(text, str):
            found = _iso_to_nanos(text)
            if found is not None:
                return found
    return None


def _iso_to_nanos(text: str) -> int | None:
    """Convert an ISO-8601 instant to integer nanoseconds, exactly.

    Integer arithmetic against a fixed epoch rather than ``timestamp() * 1e9``: a float carries
    about microsecond resolution at the current epoch, so the obvious conversion would quietly
    discard precision from a quantity used to order two events milliseconds apart.

    A naive timestamp is read as UTC. That is what the service emits, and inventing a local zone
    for it would make the answer depend on where the evaluator runs -- which is an environment
    dependency, and this package has none.
    """
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    elapsed = moment - _EPOCH
    return (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000_000 + elapsed.microseconds * 1_000


def _as_int(value: object) -> int | None:
    """An integer, from an integer or an all-ASCII-digit string. Nothing else.

    ``bool`` is excluded explicitly because ``isinstance(True, int)`` is ``True`` in Python, and a
    status attribute of ``True`` becoming ``1`` is the kind of tolerance that ends in a false
    reading. ``isascii`` is required alongside ``isdigit`` because ``str.isdigit`` accepts
    non-ASCII digits that ``int`` will happily parse.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


__all__ = [
    "EnvelopeError",
    "HttpStatus",
    "Span",
    "StatusKind",
    "attribute",
    "clinic_read_resource",
    "end_nanos",
    "http_method",
    "http_status",
    "is_clinic_write",
    "otel_status_code",
    "parse_envelope",
    "start_nanos",
    "successful",
    "trace_id",
    "url_path_segments",
]
