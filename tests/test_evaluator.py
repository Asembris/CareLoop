"""The contract floor for the ``CareLoopSettlementIntegrity`` evaluator.

Patch B builds a pure deterministic evaluator against the AgentCore span contract Patch A measured
in production. This file holds the **floor** and nothing more: the independence boundary, the
no-interpolation guarantee, one real-shaped trace that must settle, one of each non-passing shape,
handler totality and determinism.

**The full behavioural matrix, the mutation proofs, the vacuity proofs and the drift checks against
CareLoop's own registry belong to Patch C and are deliberately absent.** What Patch B owes Patch C
is a structure that can be attacked check by check -- ``CHECKS`` is an ordered tuple of small named
pure functions, applicability is its own function, and every constant it decides against is a named
module-level value.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import evaluators
import evaluators.settlement as settlement
from evaluators.contract import (
    ATTR_EFFECT_DISPATCH,
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_STATE_DIGEST,
    ATTR_EFFECT_TYPE,
    ATTR_HTTP_METHOD,
    ATTR_HTTP_RESPONSE_STATUS_CODE,
    ATTR_HTTP_STATUS_CODE,
    ATTR_HTTP_URL,
    ATTR_OBSERVATION_MODE,
    ATTR_OBSERVATION_OUTCOME,
    ATTR_OBSERVATION_RESOURCE_ID,
    ATTR_OBSERVATION_STATE_DIGEST,
    ATTR_SETTLEMENT,
    CHECK_EXPLANATIONS,
    CLAIM_ATTRIBUTES,
    ERROR_MESSAGES,
    NA_EXPLANATIONS,
    PASS_EXPLANATION,
    CheckId,
    ErrorCode,
    NaReason,
)
from evaluators.handler import lambda_handler
from evaluators.settlement import (
    Evidence,
    applicability,
    assess,
)
from evaluators.spans import (
    EnvelopeError,
    StatusKind,
    attribute,
    clinic_read_resource,
    end_nanos,
    http_method,
    http_status,
    is_clinic_write,
    otel_status_code,
    parse_envelope,
    start_nanos,
    successful,
    url_path_segments,
)

# --------------------------------------------------------------------------------------------
# The independence boundary
# --------------------------------------------------------------------------------------------

#: The only top-level modules anything under ``evaluators/`` may import.
#:
#: Deliberately an allowlist rather than a denylist of the forbidden ones. A denylist answers
#: "did somebody import boto3", which is the question we already know to ask; an allowlist answers
#: "did somebody import anything at all that we have not thought about", which is the one that
#: catches the import nobody predicted. Everything here is pure standard library with no network,
#: no filesystem, no environment and no clock.
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "evaluators",
        "re",
        "typing",
        "urllib",
    }
)

#: Named individually so a failure says which prohibition was broken, and so that a future edit to
#: :data:`ALLOWED_IMPORTS` cannot silently re-admit one of them.
FORBIDDEN_IMPORTS = frozenset(
    {
        "boto3",
        "botocore",
        "careloop",
        "demoworld",
        "evals",
        "httpx",
        "opentelemetry",
        "os",
        "pathlib",
        "random",
        "requests",
        "secrets",
        "socket",
        "strands",
        "subprocess",
        "sys",
        "tempfile",
        "time",
        "urllib3",
        "uuid",
    }
)

EVALUATOR_SOURCES = sorted(Path(evaluators.__file__).parent.glob("*.py"))


def _imported_roots(source: Path) -> set[str]:
    """Every top-level module name ``source`` imports, however it imports it."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class TestTheEvaluatorImportsNothingItMustNot:
    """``evaluators/`` is standard library only, and that is what makes it evidence.

    An evaluator that imported CareLoop's constants would be checking CareLoop against itself --
    the same category error as verifying a booking by re-reading its own receipt. The rule is
    enforced by a sweep rather than by a convention because a convention is what the next session
    does not know about.
    """

    def test_there_are_evaluator_modules_to_sweep(self) -> None:
        """The sweep below is vacuous if it finds no files. This is what stops that."""
        assert EVALUATOR_SOURCES, "no modules found under evaluators/ to sweep"

    @pytest.mark.parametrize("source", EVALUATOR_SOURCES, ids=lambda path: path.name)
    def test_only_allowlisted_modules_are_imported(self, source: Path) -> None:
        unexpected = _imported_roots(source) - ALLOWED_IMPORTS
        assert not unexpected, (
            f"{source.name} imports {sorted(unexpected)}, which is outside the evaluator's "
            f"standard-library-only boundary. If one of these is genuinely needed, that is a "
            f"design change and not a test fix."
        )

    @pytest.mark.parametrize("source", EVALUATOR_SOURCES, ids=lambda path: path.name)
    def test_the_named_prohibitions_are_absent(self, source: Path) -> None:
        """The allowlist already covers these. Naming them says *why* they are covered."""
        broken = _imported_roots(source) & FORBIDDEN_IMPORTS
        assert not broken, f"{source.name} imports {sorted(broken)}"

    def test_the_prohibitions_and_the_allowlist_do_not_overlap(self) -> None:
        """A module cannot be both permitted and forbidden; that would make one list a lie."""
        assert not (ALLOWED_IMPORTS & FORBIDDEN_IMPORTS)


class TestTheNineAttributeContractIsWhatItSays:
    """The telemetry contract is exactly nine attributes, and ``action_key`` is not one of them."""

    def test_there_are_exactly_nine(self) -> None:
        assert len(CLAIM_ATTRIBUTES) == 9
        assert len(set(CLAIM_ATTRIBUTES)) == 9

    def test_every_attribute_is_namespaced_to_careloop(self) -> None:
        assert all(name.startswith("careloop.") for name in CLAIM_ATTRIBUTES)

    def test_the_effect_namespace_carries_exactly_its_four_approved_attributes(self) -> None:
        """No fifth effect attribute, whatever it is called."""
        effect_attributes = {
            name for name in CLAIM_ATTRIBUTES if name.startswith("careloop.effect.")
        }
        assert effect_attributes == {
            ATTR_EFFECT_TYPE,
            ATTR_EFFECT_DISPATCH,
            ATTR_EFFECT_FIELDS,
            ATTR_EFFECT_STATE_DIGEST,
        }

    def test_no_action_key_under_that_or_any_other_name(self) -> None:
        """``careloop.effect.action_key`` was dropped in the Patch A design and stays dropped.

        Asserted as *the package declares exactly these nine ``careloop.`` strings and no tenth*
        rather than as a literal search for the word. A literal search is both too weak and too
        strong: it misses a replacement identifier under a different spelling, which is the actual
        hazard, and it fires on the prose that forbids the original -- and a prohibition that
        cannot name what it forbids is not worth writing down.

        Docstrings and comments are unaffected because the filter is ``startswith("careloop.")``,
        which selects attribute-name constants and not sentences that happen to mention one.
        """
        declared: set[str] = set()
        for source in EVALUATOR_SOURCES:
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                is_text = isinstance(node, ast.Constant) and isinstance(node.value, str)
                if is_text and node.value.startswith("careloop."):
                    declared.add(node.value)
        assert declared == set(CLAIM_ATTRIBUTES), (
            f"the package declares CareLoop attribute names "
            f"{sorted(declared - set(CLAIM_ATTRIBUTES))} beyond the nine-attribute contract, or "
            f"is missing {sorted(set(CLAIM_ATTRIBUTES) - declared)}"
        )


# --------------------------------------------------------------------------------------------
# The no-interpolation guarantee
# --------------------------------------------------------------------------------------------


class TestEveryCodeHasOneFixedExplanation:
    """A result may only say things this repository wrote in advance.

    Invariant 6 keeps interpolated denial text away from the planner because it re-enters a
    prompt. Here the consumer is a person and a CloudWatch record rather than a model, and the
    reason is the neighbouring one: an evaluator result is a published artefact, so a URL, a
    confirmation id, a digest or an attribute value must not be able to reach it.
    """

    def test_every_check_has_an_explanation(self) -> None:
        assert set(CHECK_EXPLANATIONS) == set(CheckId)

    def test_every_na_reason_has_an_explanation(self) -> None:
        assert set(NA_EXPLANATIONS) == set(NaReason)

    def test_every_error_code_has_a_message(self) -> None:
        assert set(ERROR_MESSAGES) == set(ErrorCode)

    @pytest.mark.parametrize(
        "table",
        [CHECK_EXPLANATIONS, NA_EXPLANATIONS, ERROR_MESSAGES],
        ids=["checks", "na", "error"],
    )
    def test_no_entry_can_interpolate_anything(self, table: dict[Any, str]) -> None:
        """No format placeholder of any kind, in any entry.

        The mechanical half of the guarantee: an entry with no ``{}``, no ``%`` and no ``$``
        cannot be turned into a template by a later call to ``format``, ``%`` or
        ``Template.substitute``.
        """
        for code, sentence in table.items():
            assert isinstance(sentence, str), code
            for placeholder in ("{", "}", "%", "$"):
                assert placeholder not in sentence, f"{code} carries {placeholder!r}"

    @pytest.mark.parametrize(
        "table",
        [CHECK_EXPLANATIONS, NA_EXPLANATIONS, ERROR_MESSAGES],
        ids=["checks", "na", "error"],
    )
    def test_entries_are_distinct(self, table: dict[Any, str]) -> None:
        """Two codes sharing a sentence would make the result ambiguous to a reader."""
        assert len(set(table.values())) == len(table)


# --------------------------------------------------------------------------------------------
# Fixtures built from the measured delivered span shape
# --------------------------------------------------------------------------------------------
#
# Not from an OpenTelemetry reference. Patch A read a real production CareLoop trace out of
# ``aws/spans`` and passed it to a throwaway code-based evaluator, and what the Lambda received is
# what these builders reproduce: camelCase ids beside the service's snake_case aliases, ISO
# ``start_time`` / ``end_time`` beside the original nanosecond fields, ``duration_ms``, ``events``,
# ``span_events``, ``source = "adot_cw"``, ``session_id = "default_session"``, ``langfuse = None``,
# and -- the sharp one -- ``status.code = "UNSET"`` on a successful span rather than ``"OK"``.
#
# All six delivered scopes are reproduced, including the two Bedrock spans and the unscoped X-Ray
# ``InternalOperation`` wrapper, because Patch A proved **nothing is filtered**. A fixture carrying
# only the spans the evaluator cares about would not be testing that.

TRACE_ID = "1-68a71bd0-9f3c4d5e6a7b8c9d0e1f2a3b"
OTHER_TRACE_ID = "1-68a71bd0-00000000000000000000ffff"
RESOURCE_ID = "apt-c98a9981caefc548"
CLINIC = "http://10.0.0.5:8100"
STATE_DIGEST = "3f8a" * 16
OTHER_DIGEST = "b71c" * 16

INTERNAL_SPAN_ID = "1111111111111111"
GRAPH_SPAN_ID = "2222222222222222"

#: A booking takes about 5 ms and the read-back about 3 ms, with the graph's own work between
#: them -- the shape Patch A's captured waterfall shows.
BASE_NANOS = 1_755_780_000_000_000_000
WRITE_START = BASE_NANOS + 1_200_000_000
WRITE_END = WRITE_START + 5_000_000
READ_START = WRITE_END + 40_000_000
READ_END = READ_START + 3_000_000

#: Sentinel for "this attribute is not present at all", as distinct from present-and-empty.
ABSENT = object()


def _iso(nanos: int) -> str:
    """The ISO rendering the service derives from the same instant."""
    return datetime.fromtimestamp(nanos // 1000 / 1_000_000, tz=UTC).isoformat()


def _span(
    span_id: str,
    *,
    scope: str | None,
    attributes: dict[str, Any],
    start: int,
    end: int,
    parent: str | None = GRAPH_SPAN_ID,
    trace: str = TRACE_ID,
) -> dict[str, Any]:
    """One span in the shape AgentCore delivers it."""
    span: dict[str, Any] = {
        "traceId": trace,
        "trace_id": trace,
        "spanId": span_id,
        "span_id": span_id,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "start_time": _iso(start),
        "end_time": _iso(end),
        "duration_ms": (end - start) / 1_000_000,
        "events": [],
        "span_events": [],
        "source": "adot_cw",
        "session_id": "default_session",
        "langfuse": None,
        "status": {"code": "UNSET"},
        "attributes": attributes,
    }
    if parent is not None:
        span["parentSpanId"] = parent
        span["parent_span_id"] = parent
    if scope is not None:
        span["scope"] = {"name": scope, "version": "0.65b0"}
    return span


def claim_attributes(**overrides: Any) -> dict[str, Any]:
    """The nine future telemetry attributes, on a trace that should settle.

    Pass ``ABSENT`` for any of them to leave it off the span entirely, which is a different input
    from present-and-wrong and is checked separately.
    """
    claim: dict[str, Any] = {
        ATTR_EFFECT_TYPE: "book_appointment",
        ATTR_EFFECT_DISPATCH: "PERFORMED",
        ATTR_EFFECT_FIELDS: "patient_id,provider_id,reason,slot_start",
        ATTR_EFFECT_STATE_DIGEST: STATE_DIGEST,
        ATTR_OBSERVATION_MODE: "INDEPENDENT_READ",
        ATTR_OBSERVATION_OUTCOME: "MATCHED",
        ATTR_OBSERVATION_RESOURCE_ID: RESOURCE_ID,
        ATTR_OBSERVATION_STATE_DIGEST: STATE_DIGEST,
        ATTR_SETTLEMENT: "VERIFIED",
    }
    claim.update(overrides)
    return {name: value for name, value in claim.items() if value is not ABSENT}


def internal_operation_span() -> dict[str, Any]:
    """The synthetic X-Ray wrapper: no scope, no parent.

    Present in every fixture on purpose. It is why an evaluator must not identify the root by an
    absent ``parentSpanId`` -- and the reason this package never identifies a root at all.
    """
    return _span(
        INTERNAL_SPAN_ID,
        scope=None,
        attributes={},
        start=BASE_NANOS,
        end=READ_END + 10_000_000,
        parent=None,
    )


def graph_span(**overrides: Any) -> dict[str, Any]:
    """``invoke_graph``, carrying the claim. Not parentless, and not identified as a root."""
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "invoke_graph",
        "gen_ai.system": "strands-agents",
        "aws.span.kind": "LOCAL_ROOT",
    }
    attributes.update(claim_attributes(**overrides))
    return _span(
        GRAPH_SPAN_ID,
        scope="strands.telemetry.tracer",
        attributes=attributes,
        start=BASE_NANOS + 1_000_000,
        end=READ_END + 5_000_000,
        parent=INTERNAL_SPAN_ID,
    )


def bedrock_spans() -> list[dict[str, Any]]:
    """The two model calls. Irrelevant evidence that must be tolerated, not filtered."""
    return [
        _span(
            f"33333333333333{index}0",
            scope="opentelemetry.instrumentation.botocore.bedrock-runtime",
            attributes={
                "gen_ai.system": "aws.bedrock",
                "aws.auth.account.access_key": "[redacted]",
            },
            start=BASE_NANOS + 10_000_000 * index,
            end=BASE_NANOS + 10_000_000 * index + 900_000_000,
        )
        for index in (1, 2)
    ]


def write_span(
    *,
    status: Any = 201,
    response_status: Any = 201,
    otel: str | None = "UNSET",
    start: int = WRITE_START,
    end: int = WRITE_END,
    path: str = "/clinic/appointments",
    trace: str = TRACE_ID,
    span_id: str = "4444444444444444",
) -> dict[str, Any]:
    """``POST /clinic/appointments`` as httpx instrumentation records it."""
    attributes: dict[str, Any] = {ATTR_HTTP_METHOD: "POST", ATTR_HTTP_URL: f"{CLINIC}{path}"}
    if status is not ABSENT:
        attributes[ATTR_HTTP_STATUS_CODE] = status
    if response_status is not ABSENT:
        attributes[ATTR_HTTP_RESPONSE_STATUS_CODE] = response_status
    span = _span(
        span_id,
        scope="opentelemetry.instrumentation.httpx",
        attributes=attributes,
        start=start,
        end=end,
        trace=trace,
    )
    span["status"] = {} if otel is None else {"code": otel}
    return span


def read_span(
    *,
    resource: str = RESOURCE_ID,
    status: Any = 200,
    response_status: Any = 200,
    otel: str | None = "UNSET",
    start: int = READ_START,
    end: int = READ_END,
    trace: str = TRACE_ID,
    span_id: str = "5555555555555555",
) -> dict[str, Any]:
    """``GET /clinic/appointments/{resource_id}`` -- the independent read-back."""
    attributes: dict[str, Any] = {
        ATTR_HTTP_METHOD: "GET",
        ATTR_HTTP_URL: f"{CLINIC}/clinic/appointments/{resource}",
    }
    if status is not ABSENT:
        attributes[ATTR_HTTP_STATUS_CODE] = status
    if response_status is not ABSENT:
        attributes[ATTR_HTTP_RESPONSE_STATUS_CODE] = response_status
    span = _span(
        span_id,
        scope="opentelemetry.instrumentation.httpx",
        attributes=attributes,
        start=start,
        end=end,
        trace=trace,
    )
    span["status"] = {} if otel is None else {"code": otel}
    return span


def settled_trace(**overrides: Any) -> list[dict[str, Any]]:
    """The six spans Patch A measured, on a booking that settled."""
    return [
        internal_operation_span(),
        graph_span(**overrides),
        *bedrock_spans(),
        write_span(),
        read_span(),
    ]


def payload(
    spans: list[Any],
    *,
    trace_ids: Any = None,
    level: Any = "TRACE",
    version: Any = "1.0",
    target: Any = None,
) -> dict[str, Any]:
    """The envelope AWS actually sends.

    ``evaluatorId`` and ``evaluatorName`` are documented and are **not** sent, so they are absent
    here too; ``evaluationReferenceInputs`` is absent rather than present and empty.
    """
    chosen = target
    if chosen is None:
        chosen = {"traceIds": [TRACE_ID] if trace_ids is None else trace_ids}
    return {
        "evaluationInput": {"sessionSpans": spans},
        "evaluationLevel": level,
        "evaluationTarget": chosen,
        "schemaVersion": version,
    }


# --------------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------------


class TestTheEnvelopeIsValidatedBeforeAnythingIsJudged:
    """Every way the payload can fail to be an evaluable input, and its code."""

    def test_a_real_shaped_envelope_yields_the_traces_spans(self) -> None:
        assert len(parse_envelope(payload(settled_trace()))) == 6

    def test_spans_from_other_traces_are_excluded(self) -> None:
        extra = write_span(trace=OTHER_TRACE_ID, span_id="6666666666666666")
        spans = parse_envelope(payload([*settled_trace(), extra]))
        assert len(spans) == 6
        assert extra not in spans

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            (lambda: None, ErrorCode.ENVELOPE_MALFORMED),
            (lambda: [], ErrorCode.ENVELOPE_MALFORMED),
            (lambda: "not a mapping", ErrorCode.ENVELOPE_MALFORMED),
            (
                lambda: payload(settled_trace(), version="1.1"),
                ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            ),
            (lambda: payload(settled_trace(), version=1.0), ErrorCode.UNSUPPORTED_SCHEMA_VERSION),
            (
                lambda: payload(settled_trace(), level="SESSION"),
                ErrorCode.UNSUPPORTED_EVALUATION_LEVEL,
            ),
            (
                lambda: payload(settled_trace(), level="TOOL_CALL"),
                ErrorCode.UNSUPPORTED_EVALUATION_LEVEL,
            ),
            (
                lambda: payload(settled_trace(), target={"spanIds": [GRAPH_SPAN_ID]}),
                ErrorCode.UNSUPPORTED_TARGET_KIND,
            ),
            (
                lambda: payload(settled_trace(), target={"traceIds": [TRACE_ID], "spanIds": []}),
                ErrorCode.UNSUPPORTED_TARGET_KIND,
            ),
            (lambda: payload(settled_trace(), target={}), ErrorCode.UNSUPPORTED_TARGET_KIND),
            (
                lambda: payload(settled_trace(), trace_ids=[TRACE_ID, OTHER_TRACE_ID]),
                ErrorCode.AMBIGUOUS_TARGET,
            ),
            (lambda: payload(settled_trace(), trace_ids=[]), ErrorCode.AMBIGUOUS_TARGET),
            (lambda: payload(settled_trace(), trace_ids=[""]), ErrorCode.AMBIGUOUS_TARGET),
            (lambda: payload(settled_trace(), trace_ids=TRACE_ID), ErrorCode.AMBIGUOUS_TARGET),
            (lambda: payload([]), ErrorCode.SPANS_MALFORMED),
            (lambda: payload(["not a span"]), ErrorCode.SPANS_MALFORMED),
            (
                lambda: {
                    "evaluationInput": {},
                    "evaluationLevel": "TRACE",
                    "evaluationTarget": {"traceIds": [TRACE_ID]},
                    "schemaVersion": "1.0",
                },
                ErrorCode.SPANS_MALFORMED,
            ),
            (
                lambda: payload([*settled_trace(), {"spanId": "7777777777777777"}]),
                ErrorCode.SPAN_MISSING_TRACE_ID,
            ),
            (
                lambda: payload(settled_trace(), trace_ids=[OTHER_TRACE_ID]),
                ErrorCode.TARGET_TRACE_ABSENT,
            ),
        ],
    )
    def test_a_malformed_envelope_names_its_own_refusal(
        self, build: Any, expected: ErrorCode
    ) -> None:
        with pytest.raises(EnvelopeError) as refusal:
            parse_envelope(build())
        assert refusal.value.code is expected

    def test_the_evaluator_id_and_name_are_never_required(self) -> None:
        """Documented as envelope fields, measured absent. Nothing may key on them."""
        assert "evaluatorId" not in payload(settled_trace())
        assert len(parse_envelope(payload(settled_trace()))) == 6

    def test_reference_inputs_may_be_absent_or_present(self) -> None:
        with_them = payload(settled_trace())
        with_them["evaluationReferenceInputs"] = {"anything": "at all"}
        assert len(parse_envelope(with_them)) == len(parse_envelope(payload(settled_trace())))

    def test_an_unattributable_span_is_never_silently_dropped(self) -> None:
        """Dropping it is how the clinic write disappears and a FAIL becomes a PASS."""
        with pytest.raises(EnvelopeError) as refusal:
            parse_envelope(payload([*settled_trace(), {"attributes": {}}]))
        assert refusal.value.code is ErrorCode.SPAN_MISSING_TRACE_ID


class TestAttributesAreReadWhereTheyActuallyAre:
    """Two locations, one fixed precedence, and no searching."""

    def test_attributes_are_read_from_the_attributes_mapping(self) -> None:
        assert attribute(write_span(), ATTR_HTTP_METHOD) == "POST"

    def test_a_flattened_dotted_key_on_the_span_is_the_fallback(self) -> None:
        assert attribute({"attributes": {}, ATTR_HTTP_METHOD: "POST"}, ATTR_HTTP_METHOD) == "POST"

    def test_the_attributes_mapping_wins_when_both_carry_the_name(self) -> None:
        span = {"attributes": {ATTR_HTTP_METHOD: "POST"}, ATTR_HTTP_METHOD: "DELETE"}
        assert attribute(span, ATTR_HTTP_METHOD) == "POST"

    def test_a_missing_attribute_is_none_rather_than_an_error(self) -> None:
        assert attribute(write_span(), "careloop.nothing.here") is None

    def test_nothing_nested_deeper_is_found(self) -> None:
        """False tolerance is a false-PASS surface, so the accessor does not go looking."""
        assert attribute({"attributes": {"http": {"method": "POST"}}}, ATTR_HTTP_METHOD) is None


class TestTheMeasuredHttpSpellingsAndNoOthers:
    """``http.method`` and ``http.url`` only. A4 measured the new spellings absent."""

    def test_the_old_method_spelling_is_read(self) -> None:
        assert http_method(write_span()) == "POST"

    def test_the_new_method_spelling_is_not_read(self) -> None:
        span = {"attributes": {"http.request.method": "POST", ATTR_HTTP_URL: f"{CLINIC}/x"}}
        assert http_method(span) is None

    def test_the_new_url_spelling_is_not_read(self) -> None:
        span = {"attributes": {ATTR_HTTP_METHOD: "GET", "url.full": f"{CLINIC}/clinic/x/y"}}
        assert url_path_segments(span) is None

    def test_the_method_is_upper_cased_and_trimmed(self) -> None:
        assert http_method({"attributes": {ATTR_HTTP_METHOD: " post "}}) == "POST"

    def test_a_non_string_method_reads_as_absent(self) -> None:
        assert http_method({"attributes": {ATTR_HTTP_METHOD: 7}}) is None


class TestUrlPathsAreClassifiedByRouteAlone:
    """Host-agnostic, query-free, and deny-by-default on a malformed path."""

    def test_query_and_fragment_are_discarded(self) -> None:
        span = {
            "attributes": {
                ATTR_HTTP_METHOD: "POST",
                ATTR_HTTP_URL: f"{CLINIC}/clinic/appointments?session=abc#frag",
            }
        }
        assert is_clinic_write(span)

    @pytest.mark.parametrize(
        "host", ["http://127.0.0.1:8100", "https://clinic.example.test", "http://[::1]:8100"]
    )
    def test_the_host_does_not_matter(self, host: str) -> None:
        span = {
            "attributes": {ATTR_HTTP_METHOD: "POST", ATTR_HTTP_URL: f"{host}/clinic/appointments"}
        }
        assert is_clinic_write(span)

    def test_a_read_yields_its_resource_id(self) -> None:
        assert clinic_read_resource(read_span()) == RESOURCE_ID

    def test_a_write_is_not_a_read_and_a_read_is_not_a_write(self) -> None:
        assert clinic_read_resource(write_span()) is None
        assert not is_clinic_write(read_span())

    def test_the_collection_route_is_not_a_read(self) -> None:
        span = {
            "attributes": {ATTR_HTTP_METHOD: "GET", ATTR_HTTP_URL: f"{CLINIC}/clinic/appointments"}
        }
        assert clinic_read_resource(span) is None

    def test_a_deeper_path_is_not_a_read(self) -> None:
        span = {
            "attributes": {
                ATTR_HTTP_METHOD: "GET",
                ATTR_HTTP_URL: f"{CLINIC}/clinic/appointments/{RESOURCE_ID}/notes",
            }
        }
        assert clinic_read_resource(span) is None

    def test_an_internal_empty_segment_is_refused_rather_than_normalised(self) -> None:
        span = {
            "attributes": {
                ATTR_HTTP_METHOD: "POST",
                ATTR_HTTP_URL: f"{CLINIC}/clinic//appointments",
            }
        }
        assert url_path_segments(span) is None
        assert not is_clinic_write(span)

    @pytest.mark.parametrize(
        ("method", "path"), [("GET", "/clinic/slots"), ("POST", "/demo/reset")]
    )
    def test_other_clinic_routes_are_neither(self, method: str, path: str) -> None:
        span = {"attributes": {ATTR_HTTP_METHOD: method, ATTR_HTTP_URL: f"{CLINIC}{path}"}}
        assert not is_clinic_write(span)
        assert clinic_read_resource(span) is None


class TestTheTwoStatusSpellingsAreResolvedDeterministically:
    """The truth table from the approved plan, row by row."""

    def test_both_present_and_equal(self) -> None:
        resolved = http_status(write_span(status=201, response_status=201))
        assert (resolved.kind, resolved.code) == (StatusKind.RESOLVED, 201)

    def test_only_the_old_spelling(self) -> None:
        resolved = http_status(write_span(status=201, response_status=ABSENT))
        assert (resolved.kind, resolved.code) == (StatusKind.RESOLVED, 201)

    def test_only_the_new_spelling(self) -> None:
        resolved = http_status(write_span(status=ABSENT, response_status=201))
        assert (resolved.kind, resolved.code) == (StatusKind.RESOLVED, 201)

    def test_both_present_and_disagreeing(self) -> None:
        resolved = http_status(write_span(status=201, response_status=500))
        assert resolved.kind is StatusKind.AMBIGUOUS
        assert resolved.code is None

    def test_neither_readable(self) -> None:
        resolved = http_status(write_span(status=ABSENT, response_status=ABSENT))
        assert resolved.kind is StatusKind.ABSENT

    def test_an_all_digit_string_is_accepted(self) -> None:
        resolved = http_status(write_span(status="201", response_status="201"))
        assert (resolved.kind, resolved.code) == (StatusKind.RESOLVED, 201)

    def test_a_boolean_is_not_an_integer_status(self) -> None:
        """``isinstance(True, int)`` is True in Python, and a status of ``1`` is not a status."""
        resolved = http_status(write_span(status=True, response_status=ABSENT))
        assert resolved.kind is StatusKind.ABSENT

    def test_a_non_ascii_digit_string_is_not_accepted(self) -> None:
        resolved = http_status(write_span(status="٢٠١", response_status=ABSENT))
        assert resolved.kind is StatusKind.ABSENT


class TestSuccessIsTwoHundredsAndNotError:
    """A4's sharpest finding, held as a test: production spans carry ``UNSET`` on success."""

    def test_unset_on_a_2xx_is_successful(self) -> None:
        assert successful(write_span(otel="UNSET"))

    def test_ok_on_a_2xx_is_also_successful(self) -> None:
        """Accepted, but never required. Requiring it would refuse every real CareLoop trace."""
        assert successful(write_span(otel="OK"))

    def test_an_absent_otel_status_on_a_2xx_is_successful(self) -> None:
        assert successful(write_span(otel=None))

    def test_error_is_not_successful_however_good_the_http_status(self) -> None:
        assert not successful(write_span(status=201, response_status=201, otel="ERROR"))

    def test_a_replay_two_hundred_is_successful(self) -> None:
        """Demo World answers 201 on create and 200 on replay for the same route."""
        assert successful(write_span(status=200, response_status=200))

    @pytest.mark.parametrize("code", [199, 300, 301, 400, 404, 409, 500, 503])
    def test_anything_outside_2xx_is_not_successful(self, code: int) -> None:
        assert not successful(write_span(status=code, response_status=code))

    def test_an_ambiguous_status_is_not_successful(self) -> None:
        assert not successful(write_span(status=201, response_status=500))

    def test_an_absent_status_is_not_successful(self) -> None:
        assert not successful(write_span(status=ABSENT, response_status=ABSENT))

    def test_the_flat_otel_spelling_is_read_when_there_is_no_nested_status(self) -> None:
        span = write_span(otel=None)
        del span["status"]
        span["attributes"]["status.code"] = "ERROR"
        assert otel_status_code(span) == "ERROR"
        assert not successful(span)


class TestTimingNormalisesToOneIntegerQuantity:
    """Nanoseconds first, the measured ISO enrichment second, and never a silent skip."""

    def test_the_nanosecond_fields_are_preferred(self) -> None:
        assert start_nanos(write_span()) == WRITE_START
        assert end_nanos(write_span()) == WRITE_END

    def test_an_integer_nanosecond_field_is_accepted_as_well_as_a_string(self) -> None:
        span = write_span()
        span["startTimeUnixNano"] = WRITE_START
        assert start_nanos(span) == WRITE_START

    def test_the_snake_case_alias_is_accepted(self) -> None:
        span = write_span()
        del span["startTimeUnixNano"]
        span["start_time_unix_nano"] = str(WRITE_START)
        assert start_nanos(span) == WRITE_START

    def test_the_iso_enrichment_is_the_fallback_and_agrees_with_the_nanoseconds(self) -> None:
        span = write_span()
        del span["startTimeUnixNano"]
        del span["endTimeUnixNano"]
        assert start_nanos(span) == WRITE_START
        assert end_nanos(span) == WRITE_END

    def test_a_naive_iso_timestamp_is_read_as_utc(self) -> None:
        span = write_span()
        del span["startTimeUnixNano"]
        span["start_time"] = _iso(WRITE_START).replace("+00:00", "")
        assert start_nanos(span) == WRITE_START

    def test_an_unreadable_timestamp_is_none_rather_than_a_guess(self) -> None:
        span = write_span()
        del span["startTimeUnixNano"]
        span["start_time"] = "not a timestamp"
        assert start_nanos(span) is None

    def test_a_span_with_no_timing_at_all_reads_none(self) -> None:
        span = write_span()
        for key in ("startTimeUnixNano", "endTimeUnixNano", "start_time", "end_time"):
            del span[key]
        assert start_nanos(span) is None
        assert end_nanos(span) is None

    def test_precision_finer_than_a_float_survives(self) -> None:
        """``timestamp() * 1e9`` would quietly discard this; integer arithmetic does not."""
        span = write_span()
        del span["startTimeUnixNano"]
        span["start_time"] = "2026-08-21T12:00:00.123456+00:00"
        assert start_nanos(span) == 1_787_313_600_123_456_000
        # The half that matters: the microsecond tail is intact rather than rounded away.
        assert start_nanos(span) % 1_000_000_000 == 123_456_000


# --------------------------------------------------------------------------------------------
# The decision procedure
# --------------------------------------------------------------------------------------------


def recheck_trace(**overrides: Any) -> list[dict[str, Any]]:
    """A trace with an independent read and **no write** -- a re-check or a suppressed duplicate.

    The write belongs to an earlier trace. The read does not, and never may.
    """
    settings: dict[str, Any] = {ATTR_EFFECT_DISPATCH: "NOT_IN_THIS_TRACE"}
    settings.update(overrides)
    return [internal_operation_span(), graph_span(**settings), *bedrock_spans(), read_span()]


def simulated_trace(**overrides: Any) -> list[dict[str, Any]]:
    """A trace whose effect has no external system at all -- one of the three simulated tools."""
    settings: dict[str, Any] = {
        ATTR_EFFECT_TYPE: "send_email",
        ATTR_EFFECT_DISPATCH: "PERFORMED",
        ATTR_OBSERVATION_MODE: "NONE",
        ATTR_OBSERVATION_OUTCOME: "NOT_OBSERVED",
    }
    settings.update(overrides)
    return [internal_operation_span(), graph_span(**settings), *bedrock_spans()]


def quiet_trace() -> list[dict[str, Any]]:
    """A trace that proposed nothing, executed nothing and touched no external system."""
    return [internal_operation_span(), graph_span(**dict.fromkeys(CLAIM_ATTRIBUTES, ABSENT))]


def verdict(spans: list[Any]) -> dict[str, Any]:
    """Judge a trace through the whole public path, envelope included."""
    return assess(payload(spans))


class TestOneRealShapedTraceSettles:
    """The measured six-span production shape, with a claim that should pass, and it does.

    If this test ever needs a fixture change to keep passing, that change is a change to what the
    evaluator believes production looks like, and it belongs in ``docs/spike-findings.md`` before
    it belongs here.
    """

    def test_it_passes(self) -> None:
        assert verdict(settled_trace()) == {
            "label": "PASS",
            "value": 1.0,
            "explanation": PASS_EXPLANATION,
        }

    def test_the_bedrock_and_wrapper_spans_are_tolerated_rather_than_filtered(self) -> None:
        """Patch A proved nothing is filtered. The evaluator must cope with all six spans."""
        spans = settled_trace()
        assert len(spans) == 6
        assert verdict(spans)["label"] == "PASS"

    def test_no_root_is_ever_identified(self) -> None:
        """``invoke_graph`` is not parentless, so a trace with no parentless span still settles.

        The claim is located by attribute presence. Giving every span a parent removes the only
        thing a root-by-absent-parent heuristic could have keyed on, and changes nothing.
        """
        spans = settled_trace()
        for span in spans:
            span["parentSpanId"] = "9999999999999999"
            span["parent_span_id"] = "9999999999999999"
        assert verdict(spans)["label"] == "PASS"

    def test_a_claim_on_any_span_is_found(self) -> None:
        """Patch D chooses where to put the nine attributes; Patch B does not care which span."""
        spans = [
            internal_operation_span(),
            graph_span(**dict.fromkeys(CLAIM_ATTRIBUTES, ABSENT)),
            *bedrock_spans(),
            write_span(),
            read_span(),
        ]
        spans[-1]["attributes"].update(claim_attributes())
        assert verdict(spans)["label"] == "PASS"


class TestTheNonPassFloor:
    """One of each shape a result can take, asserted exactly."""

    def test_a_digest_mismatch_fails(self) -> None:
        result = verdict(settled_trace(**{ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST}))
        assert result["label"] == "FAIL"
        assert result["value"] == 0.0
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.OUTCOME_CONTRADICTS_DIGESTS]

    def test_an_effect_with_no_external_system_is_not_applicable(self) -> None:
        result = verdict(simulated_trace())
        assert result == {
            "label": "NOT_APPLICABLE",
            "explanation": NA_EXPLANATIONS[NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE],
        }

    def test_a_trace_claiming_nothing_and_touching_nothing_is_not_applicable(self) -> None:
        result = verdict(quiet_trace())
        assert result == {
            "label": "NOT_APPLICABLE",
            "explanation": NA_EXPLANATIONS[NaReason.NO_EFFECT_CLAIMED],
        }

    @pytest.mark.parametrize(
        "spans", [lambda: simulated_trace(), lambda: quiet_trace()], ids=["simulated", "quiet"]
    )
    def test_not_applicable_omits_the_value_key_entirely(self, spans: Any) -> None:
        """Absent, never ``None`` and never ``0.0``.

        Patch A proved the omission survives the round trip uncoerced. That is what keeps an
        inapplicable trace out of every average at the schema level rather than by convention, so
        it is asserted as key absence rather than as a falsy value.
        """
        result = verdict(spans())
        assert "value" not in result
        assert result.get("value", "missing") == "missing"

    def test_the_wrong_evaluation_level_is_an_error(self) -> None:
        result = assess(payload(settled_trace(), level="SESSION"))
        assert result == {
            "errorCode": "UNSUPPORTED_EVALUATION_LEVEL",
            "errorMessage": ERROR_MESSAGES[ErrorCode.UNSUPPORTED_EVALUATION_LEVEL],
        }

    def test_an_error_result_carries_no_label_and_no_value(self) -> None:
        """Only fields Patch A proved ``EvaluationResultContent`` declares."""
        result = assess(payload(settled_trace(), level="SESSION"))
        assert set(result) == {"errorCode", "errorMessage"}


class TestClinicTransportCanNeverBeHiddenAsNotApplicable:
    """The two rules that stop *not applicable* becoming a way to say nothing happened."""

    def test_a_write_with_no_claim_at_all_fails(self) -> None:
        spans = [internal_operation_span(), *bedrock_spans(), write_span(), read_span()]
        result = verdict(spans)
        assert result["label"] == "FAIL"
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.CLAIM_ABSENT]

    def test_a_read_alone_with_no_claim_also_fails(self) -> None:
        spans = [internal_operation_span(), *bedrock_spans(), read_span()]
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.CLAIM_ABSENT]

    def test_clinic_transport_beside_an_unobservable_effect_type_fails(self) -> None:
        spans = simulated_trace()
        spans.extend([write_span(), read_span()])
        result = verdict(spans)
        assert result["label"] == "FAIL"
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.EFFECT_TYPE_MISLABELLED]

    def test_two_claim_spans_are_a_finding_rather_than_a_choice(self) -> None:
        spans = settled_trace()
        spans[-1]["attributes"].update(claim_attributes())
        result = verdict(spans)
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.CLAIM_AMBIGUOUS]


class TestReCheckAndSuppressedEffectSemantics:
    """A write may be historical. An observation may not.

    This is the question the plan gate singled out. CareLoop legitimately reaches ``VERIFYING``
    with no HTTP write in the trace two ways -- a duplicate suppressed from its own ledger, and a
    caregiver's re-check that runs ``("dispatch", "verify")`` and never reaches the execute node --
    so making the write mandatory would refuse correct traces. What keeps that from becoming a
    false ``PASS`` is that everything on the observation side is still required, **in this trace**.
    """

    @pytest.mark.parametrize("dispatch", ["SUPPRESSED", "NOT_IN_THIS_TRACE"])
    def test_a_trace_with_no_write_can_settle(self, dispatch: str) -> None:
        assert verdict(recheck_trace(**{ATTR_EFFECT_DISPATCH: dispatch}))["label"] == "PASS"

    @pytest.mark.parametrize("dispatch", ["SUPPRESSED", "NOT_IN_THIS_TRACE"])
    def test_but_only_with_an_independent_read_in_this_trace(self, dispatch: str) -> None:
        """Drop the read and the same claim fails. The observation is never historical."""
        spans = recheck_trace(**{ATTR_EFFECT_DISPATCH: dispatch})
        without_read = [span for span in spans if clinic_read_resource(span) is None]
        result = verdict(without_read)
        assert result["label"] == "FAIL"
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.OBSERVATION_ABSENT_FROM_TRACE]

    @pytest.mark.parametrize("dispatch", ["SUPPRESSED", "NOT_IN_THIS_TRACE"])
    def test_a_write_in_the_trace_contradicts_both_of_them(self, dispatch: str) -> None:
        spans = recheck_trace(**{ATTR_EFFECT_DISPATCH: dispatch})
        spans.append(write_span())
        result = verdict(spans)
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.DISPATCH_CONTRADICTS_TRANSPORT]

    def test_performed_without_a_write_contradicts_the_transport(self) -> None:
        result = verdict(recheck_trace(**{ATTR_EFFECT_DISPATCH: "PERFORMED"}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.DISPATCH_CONTRADICTS_TRANSPORT]

    @pytest.mark.parametrize("dispatch", [ABSENT, "", "PERFORMED_MAYBE", 7, None, True])
    def test_a_dispatch_outside_the_vocabulary_never_settles(self, dispatch: Any) -> None:
        result = verdict(recheck_trace(**{ATTR_EFFECT_DISPATCH: dispatch}))
        assert result["label"] == "FAIL"

    @pytest.mark.parametrize("dispatch", ["SUPPRESSED", "NOT_IN_THIS_TRACE"])
    def test_the_read_must_still_have_succeeded(self, dispatch: str) -> None:
        spans = [
            internal_operation_span(),
            graph_span(**{ATTR_EFFECT_DISPATCH: dispatch}),
            *bedrock_spans(),
            read_span(status=404, response_status=404),
        ]
        assert (
            verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.OBSERVATION_NOT_SUCCESSFUL]
        )

    @pytest.mark.parametrize("dispatch", ["SUPPRESSED", "NOT_IN_THIS_TRACE"])
    def test_the_read_must_still_name_the_claimed_resource(self, dispatch: str) -> None:
        spans = [
            internal_operation_span(),
            graph_span(**{ATTR_EFFECT_DISPATCH: dispatch}),
            *bedrock_spans(),
            read_span(resource="apt-somebody-elses"),
        ]
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.RESOURCE_MISMATCH]

    @pytest.mark.parametrize("dispatch", ["SUPPRESSED", "NOT_IN_THIS_TRACE"])
    def test_every_semantic_requirement_still_applies(self, dispatch: str) -> None:
        """Ordering is inapplicable without a write. Nothing else is relaxed."""
        for override, expected in (
            ({ATTR_OBSERVATION_MODE: "NONE"}, CheckId.NO_INDEPENDENT_OBSERVER),
            ({ATTR_EFFECT_FIELDS: "patient_id"}, CheckId.FIELD_SET_UNEXPECTED),
            ({ATTR_EFFECT_STATE_DIGEST: "nothex"}, CheckId.DIGEST_MALFORMED),
            ({ATTR_OBSERVATION_OUTCOME: "ABSENT"}, CheckId.OUTCOME_NOT_MATCHED),
            ({ATTR_SETTLEMENT: "NOT_CERTIFIED"}, CheckId.SETTLEMENT_NOT_VERIFIED),
        ):
            settings = {ATTR_EFFECT_DISPATCH: dispatch, **override}
            result = verdict(recheck_trace(**settings))
            assert result["explanation"] == CHECK_EXPLANATIONS[expected], override


class TestTheOrderingsThatAreLoadBearing:
    """Three orderings that Patch C must not be able to change without a test going red."""

    def test_the_field_set_is_pinned_before_any_digest_is_trusted(self) -> None:
        """Two digests over an empty joint state are equal, so this ordering is the vacuity guard.

        The claim below is wrong in both ways at once. Reporting the digest failure would mean the
        evaluator compared digests it had no reason to believe covered anything.
        """
        result = verdict(
            settled_trace(
                **{
                    ATTR_EFFECT_FIELDS: "",
                    ATTR_EFFECT_STATE_DIGEST: STATE_DIGEST,
                    ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST,
                    ATTR_OBSERVATION_OUTCOME: "ABSENT",
                }
            )
        )
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.FIELD_SET_UNEXPECTED]

    def test_an_empty_field_set_with_equal_digests_still_fails(self) -> None:
        """The vacuity itself, stated directly."""
        result = verdict(settled_trace(**{ATTR_EFFECT_FIELDS: ""}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.FIELD_SET_UNEXPECTED]

    def test_the_field_set_must_be_exactly_the_expected_one(self) -> None:
        for wrong in ("patient_id,provider_id,reason", "patient_id,provider_id,reason,slot", ""):
            result = verdict(settled_trace(**{ATTR_EFFECT_FIELDS: wrong}))
            assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.FIELD_SET_UNEXPECTED], wrong

    def test_the_self_contradiction_check_is_independently_reachable(self) -> None:
        """``DIVERGED`` over equal digests is a claim disagreeing with itself."""
        result = verdict(settled_trace(**{ATTR_OBSERVATION_OUTCOME: "DIVERGED"}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.OUTCOME_CONTRADICTS_DIGESTS]

    def test_the_plain_digest_disagreement_is_independently_reachable(self) -> None:
        """An outcome that claims no agreement, over digests that do not agree."""
        result = verdict(
            settled_trace(
                **{
                    ATTR_OBSERVATION_OUTCOME: "ABSENT",
                    ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST,
                }
            )
        )
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.STATE_DIGESTS_DIFFER]

    def test_the_outcome_requirement_is_independently_reachable(self) -> None:
        """Equal digests, and an outcome that still does not claim the read established them."""
        for outcome in ("ABSENT", "UNAVAILABLE", "NOT_OBSERVED"):
            result = verdict(settled_trace(**{ATTR_OBSERVATION_OUTCOME: outcome}))
            assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.OUTCOME_NOT_MATCHED], outcome

    def test_multiple_writes_is_decided_before_anything_says_the_write(self) -> None:
        spans = settled_trace()
        spans.append(write_span(span_id="8888888888888888"))
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.MULTIPLE_WRITES]


class TestAmbiguityFailsRatherThanChoosing:
    """Every place two candidates could be picked between, and none of them is."""

    def test_two_reads_of_the_claimed_resource(self) -> None:
        spans = settled_trace()
        spans.append(read_span(span_id="8888888888888888", start=READ_END, end=READ_END + 1_000))
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.MULTIPLE_OBSERVATIONS]

    def test_a_second_read_of_a_different_resource_is_not_ambiguity(self) -> None:
        """Only reads naming the claimed resource participate; the rest are other evidence."""
        spans = settled_trace()
        spans.append(read_span(resource="apt-unrelated", span_id="8888888888888888"))
        assert verdict(spans)["label"] == "PASS"

    def test_the_writes_two_status_spellings_disagreeing(self) -> None:
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(status=201, response_status=500),
            read_span(),
        ]
        assert (
            verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.TRANSPORT_STATUS_AMBIGUOUS]
        )

    def test_the_reads_two_status_spellings_disagreeing(self) -> None:
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(),
            read_span(status=200, response_status=503),
        ]
        assert (
            verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.TRANSPORT_STATUS_AMBIGUOUS]
        )


class TestOrderingAgainstTheWriteIsCheckedAndNeverSkipped:
    """Timing is required where an ordering claim exists, and absent where one does not."""

    def test_a_read_that_began_before_the_write_finished_fails(self) -> None:
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(),
            read_span(start=WRITE_END - 1, end=WRITE_END + 1_000_000),
        ]
        assert (
            verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.OBSERVATION_PRECEDES_EFFECT]
        )

    def test_a_read_beginning_exactly_when_the_write_finished_is_allowed(self) -> None:
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(),
            read_span(start=WRITE_END, end=WRITE_END + 1_000_000),
        ]
        assert verdict(spans)["label"] == "PASS"

    @pytest.mark.parametrize("missing", ["write", "read"])
    def test_unreadable_timing_is_a_failure_and_never_a_skip(self, missing: str) -> None:
        spans = settled_trace()
        target = spans[4] if missing == "write" else spans[5]
        for key in ("startTimeUnixNano", "endTimeUnixNano", "start_time", "end_time"):
            del target[key]
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.TIMING_UNAVAILABLE]

    def test_with_no_write_in_the_trace_there_is_no_ordering_relation_to_assert(self) -> None:
        """A scope rather than an exemption -- everything on the read side still applies."""
        spans = recheck_trace()
        read = spans[-1]
        for key in ("startTimeUnixNano", "start_time"):
            del read[key]
        assert verdict(spans)["label"] == "PASS"


class TestTheWriteMustHaveSucceeded:
    """A performed write that the transport did not record as succeeding settles nothing."""

    @pytest.mark.parametrize("code", [400, 404, 409, 500, 503])
    def test_a_refused_write_fails(self, code: int) -> None:
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(status=code, response_status=code),
            read_span(),
        ]
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.WRITE_NOT_SUCCESSFUL]

    def test_an_otel_error_on_a_2xx_write_fails(self) -> None:
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(otel="ERROR"),
            read_span(),
        ]
        assert verdict(spans)["explanation"] == CHECK_EXPLANATIONS[CheckId.WRITE_NOT_SUCCESSFUL]

    def test_a_replayed_two_hundred_write_still_settles(self) -> None:
        """Demo World answers 200 when the idempotency key replays an existing booking."""
        spans = [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(status=200, response_status=200),
            read_span(),
        ]
        assert verdict(spans)["label"] == "PASS"


class TestCorrelationIsAnEqualityBetweenTwoNamesForOneThing:
    """The claimed resource id against the read's own URL, and nothing looser."""

    def test_a_claim_naming_a_resource_the_trace_never_read_fails(self) -> None:
        result = verdict(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: "apt-never-read"}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.RESOURCE_MISMATCH]

    def test_a_suffix_of_the_read_resource_does_not_match_it(self) -> None:
        """Exact segment equality, never ``endswith``."""
        result = verdict(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: RESOURCE_ID[4:]}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.RESOURCE_MISMATCH]

    @pytest.mark.parametrize(
        "claimed",
        ["", "apt-a/apt-b", "apt-a?x=1", "apt-a#f", "apt a", "../appointments/apt-a", "%2e%2e"],
    )
    def test_a_claimed_id_that_is_not_one_safe_token_is_refused(self, claimed: str) -> None:
        result = verdict(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: claimed}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.OBSERVATION_ABSENT_FROM_TRACE]

    @pytest.mark.parametrize("claimed", [ABSENT, None, 7, ["apt-a"]])
    def test_a_claimed_id_that_is_not_a_string_is_refused(self, claimed: Any) -> None:
        result = verdict(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: claimed}))
        assert result["explanation"] == CHECK_EXPLANATIONS[CheckId.OBSERVATION_ABSENT_FROM_TRACE]


class TestTheCheckListIsStructuredForPatchC:
    """What Patch B owes Patch C: a list of small named things, each replaceable on its own."""

    def test_there_are_nineteen_ordered_checks(self) -> None:
        assert len(settlement.CHECKS) == 19

    def test_every_check_is_a_distinct_named_function(self) -> None:
        assert len(set(settlement.CHECKS)) == len(settlement.CHECKS)
        assert all(
            callable(check) and check.__name__.startswith("_") for check in settlement.CHECKS
        )

    def test_every_check_takes_evidence_alone_and_returns_a_code_or_nothing(self) -> None:
        """A settled trace must leave every check silent, one at a time."""
        spans = parse_envelope(payload(settled_trace()))
        evidence = applicability(spans)
        assert isinstance(evidence, Evidence)
        for check in settlement.CHECKS:
            assert check(evidence) is None, check.__name__

    def test_applicability_is_reachable_on_its_own(self) -> None:
        """Phase A is a separate seam and can be attacked without going through ``assess``."""
        assert isinstance(applicability(parse_envelope(payload(settled_trace()))), Evidence)
        assert applicability(parse_envelope(payload(quiet_trace()))) is NaReason.NO_EFFECT_CLAIMED


class TestNoPayloadValueReachesTheResult:
    """The behavioural half of the no-interpolation guarantee.

    The table check proves no entry *can* interpolate. This proves that nothing does -- over a
    passing trace, a failing one, an inapplicable one and an unevaluable one, with every value the
    payload carries searched for in the rendered result.
    """

    @pytest.mark.parametrize(
        "build",
        [
            lambda: payload(settled_trace()),
            lambda: payload(settled_trace(**{ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST})),
            lambda: payload(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: "apt-SENTINEL-XYZ"})),
            lambda: payload(recheck_trace()),
            lambda: payload(simulated_trace()),
            lambda: payload(quiet_trace()),
            lambda: payload(settled_trace(), level="SESSION"),
            lambda: payload([{"traceId": TRACE_ID, "attributes": {"http.url": CLINIC}}]),
        ],
    )
    def test_nothing_from_the_payload_appears_in_the_result(self, build: Any) -> None:
        rendered = json.dumps(assess(build()))
        for value in (
            RESOURCE_ID,
            "apt-SENTINEL-XYZ",
            STATE_DIGEST,
            OTHER_DIGEST,
            CLINIC,
            TRACE_ID,
            "adot_cw",
            "default_session",
            "book_appointment",
            "invoke_graph",
        ):
            assert value not in rendered, value

    def test_the_result_says_only_things_the_repository_wrote_in_advance(self) -> None:
        """Every explanation a result can carry comes from one of the three tables."""
        known = (
            {PASS_EXPLANATION}
            | set(CHECK_EXPLANATIONS.values())
            | set(NA_EXPLANATIONS.values())
            | set(ERROR_MESSAGES.values())
        )
        for build in (
            lambda: payload(settled_trace()),
            lambda: payload(settled_trace(**{ATTR_SETTLEMENT: "NOT_CERTIFIED"})),
            lambda: payload(simulated_trace()),
            lambda: payload(settled_trace(), version="9.9"),
        ):
            result = assess(build())
            for field in ("explanation", "errorMessage"):
                if field in result:
                    assert result[field] in known


# --------------------------------------------------------------------------------------------
# The Lambda handler
# --------------------------------------------------------------------------------------------


class TestTheHandlerIsTotal:
    """No input makes it raise, and no exception text reaches what it returns."""

    @pytest.mark.parametrize(
        "event",
        [None, {}, [], "", 0, True, {"schemaVersion": "1.0"}, {"evaluationInput": None}],
        ids=repr,
    )
    def test_a_malformed_event_returns_an_error_rather_than_raising(self, event: Any) -> None:
        result = lambda_handler(event, None)
        assert "errorCode" in result
        assert result["errorMessage"] in set(ERROR_MESSAGES.values())

    def test_it_delegates_rather_than_deciding(self) -> None:
        """Its whole job. A real envelope must produce exactly what ``assess`` produces."""
        envelope = payload(settled_trace())
        assert lambda_handler(envelope, None) == assess(envelope)

    def test_an_unexpected_failure_becomes_a_fixed_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one broad catch, proven to catch and proven not to leak.

        The exception's message is a value a payload could plausibly have carried. What comes back
        names none of it.
        """

        def explode(_: Any) -> dict[str, Any]:
            raise RuntimeError(f"leaked {RESOURCE_ID} and {STATE_DIGEST}")

        monkeypatch.setattr("evaluators.handler.assess", explode)
        result = lambda_handler(payload(settled_trace()), None)
        assert result == {
            "errorCode": "INTERNAL_ERROR",
            "errorMessage": ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR],
        }
        rendered = json.dumps(result)
        assert RESOURCE_ID not in rendered
        assert STATE_DIGEST not in rendered
        assert "RuntimeError" not in rendered

    def test_even_a_bare_exception_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(_: Any) -> dict[str, Any]:
            raise KeyboardInterrupt  # not an Exception subclass; must still not be swallowed

        monkeypatch.setattr("evaluators.handler.assess", explode)
        with pytest.raises(KeyboardInterrupt):
            lambda_handler(payload(settled_trace()), None)

    def test_the_context_is_never_read(self) -> None:
        """Nothing depends on the invocation, so any context object gives the same answer."""
        envelope = payload(settled_trace())
        answers = [
            lambda_handler(envelope, None),
            lambda_handler(envelope, object()),
            lambda_handler(envelope, {"aws_request_id": "whatever"}),
            lambda_handler(envelope),
        ]
        assert all(answer == answers[0] for answer in answers)


class TestTheResultIsDeterministicAndSerialisable:
    """Same payload, same answer -- and an answer AWS can carry."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: payload(settled_trace()),
            lambda: payload(recheck_trace()),
            lambda: payload(settled_trace(**{ATTR_SETTLEMENT: "NOT_CERTIFIED"})),
            lambda: payload(simulated_trace()),
            lambda: payload(quiet_trace()),
            lambda: payload(settled_trace(), level="SESSION"),
        ],
    )
    def test_the_same_payload_always_gives_the_same_result(self, build: Any) -> None:
        """No clock, no randomness, no environment, no set iteration reaching the output."""
        first = assess(build())
        for _ in range(5):
            assert assess(build()) == first

    @pytest.mark.parametrize(
        "build",
        [
            lambda: payload(settled_trace()),
            lambda: payload(recheck_trace()),
            lambda: payload(simulated_trace()),
            lambda: payload(quiet_trace()),
            lambda: payload(settled_trace(), version="9.9"),
        ],
    )
    def test_the_result_is_json_serialisable_and_holds_plain_types(self, build: Any) -> None:
        result = assess(build())
        assert json.loads(json.dumps(result)) == result
        for key, value in result.items():
            assert type(key) is str
            assert type(value) in (str, float), key

    def test_span_ordering_in_the_payload_does_not_change_the_answer(self) -> None:
        """The spans arrive in whatever order the service assembled them."""
        spans = settled_trace()
        assert assess(payload(list(reversed(spans)))) == assess(payload(spans))

    def test_the_four_result_shapes_are_exactly_these_and_no_others(self) -> None:
        """A structural statement about the whole surface, not about one path through it."""
        shapes = {
            frozenset(assess(build()))
            for build in (
                lambda: payload(settled_trace()),
                lambda: payload(settled_trace(**{ATTR_SETTLEMENT: "NOT_CERTIFIED"})),
                lambda: payload(simulated_trace()),
                lambda: payload(settled_trace(), level="SESSION"),
            )
        }
        assert shapes == {
            frozenset({"label", "value", "explanation"}),
            frozenset({"label", "explanation"}),
            frozenset({"errorCode", "errorMessage"}),
        }
