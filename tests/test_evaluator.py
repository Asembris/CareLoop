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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import evaluators
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
    CheckId,
    ErrorCode,
    NaReason,
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
