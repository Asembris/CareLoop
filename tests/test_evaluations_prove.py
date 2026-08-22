"""The proof tool's own logic, offline.

``scripts/evaluations_prove.py`` produces the evidence a judge reads, so the two things worth
pinning are the ones that could make that evidence *wrong* rather than absent: the transport facts
it reports beside an AWS verdict, and whether it will call an expectation met when it was not.

**The accessors here are deliberately a second implementation.** The script does not import
``evaluators.spans``, because a transport cross-check computed with the evaluator's own reader
would agree with the evaluator by construction. These tests hold that second reader to the shapes
Patch A measured in production -- nested attributes, both status spellings, camelCase ids.
"""

from __future__ import annotations

from typing import Any

from evaluators.contract import CHECK_EXPLANATIONS, PASS_EXPLANATION, CheckId
from scripts.evaluations_prove import (
    attribute,
    canonical_payload,
    check_expectation,
    claim_attribute_names,
    deduplicate,
    otel_status,
    path_segments,
    scope_of,
    trace_of,
    transport_facts,
)

TRACE = "6a897ddea18354fc7ac20e77875fb8b9"
RESOURCE = "apt-0123456789abcdef"


def httpx_span(span_id: str, method: str, url: str, code: int) -> dict[str, Any]:
    """The delivered shape of a real CareLoop clinic span, as measured in production."""
    return {
        "traceId": TRACE,
        "spanId": span_id,
        "scope": {"name": "opentelemetry.instrumentation.httpx"},
        "startTimeUnixNano": 1787398884025901793,
        "endTimeUnixNano": 1787398884029395862,
        "status": {"code": "UNSET"},
        "attributes": {
            "http.method": method,
            "http.url": url,
            "http.status_code": code,
            "http.response.status_code": code,
        },
    }


def claim_span(**overrides: Any) -> dict[str, Any]:
    attributes = {
        "careloop.effect.type": "book_appointment",
        "careloop.effect.dispatch": "PERFORMED",
        "careloop.effect.fields": "patient_id,provider_id,reason,slot_start",
        "careloop.effect.state_digest": "a" * 64,
        "careloop.observation.mode": "INDEPENDENT_READ",
        "careloop.observation.outcome": "MATCHED",
        "careloop.observation.resource_id": RESOURCE,
        "careloop.observation.state_digest": "a" * 64,
        "careloop.settlement": "VERIFIED",
        "gen_ai.operation.name": "invoke_graph",
    }
    attributes.update(overrides)
    return {
        "traceId": TRACE,
        "spanId": "claim",
        "scope": {"name": "strands.telemetry.tracer"},
        "attributes": attributes,
    }


PERFORMED = [
    claim_span(),
    httpx_span("w1", "POST", "http://127.0.0.1:8100/clinic/appointments", 201),
    httpx_span("r1", "GET", f"http://127.0.0.1:8100/clinic/appointments/{RESOURCE}", 200),
]


class TestTheSecondReaderReadsTheMeasuredShape:
    def test_attributes_are_read_out_of_the_nested_map(self) -> None:
        assert attribute(PERFORMED[1], "http.method") == "POST"

    def test_a_flattened_attribute_is_read_too(self) -> None:
        assert attribute({"http.method": "GET"}, "http.method") == "GET"

    def test_the_camel_case_trace_id_is_preferred(self) -> None:
        assert trace_of({"traceId": "a", "trace_id": "b"}) == "a"

    def test_the_scope_name_is_read_from_the_nested_object(self) -> None:
        assert scope_of(PERFORMED[1]) == "opentelemetry.instrumentation.httpx"

    def test_a_span_with_no_scope_is_named_rather_than_dropped(self) -> None:
        assert scope_of({"traceId": TRACE}) == "<none>"

    def test_the_query_string_is_not_part_of_the_path(self) -> None:
        span = httpx_span("x", "GET", "http://h/clinic/appointments/apt-1?session=abc", 200)
        assert path_segments(span) == ("clinic", "appointments", "apt-1")

    def test_claim_attribute_names_are_names_only(self) -> None:
        names = claim_attribute_names(PERFORMED[0])
        assert len(names) == 9
        assert all(name.startswith("careloop.") for name in names)
        assert RESOURCE not in names


class TestTheTransportFactsDescribeTheTraceHonestly:
    def test_a_performed_trace_reports_one_write_and_one_matching_read(self) -> None:
        facts = transport_facts(PERFORMED)
        assert facts["clinic_write_count"] == 1
        assert facts["clinic_read_count"] == 1
        assert facts["claim_span_count"] == 1
        assert facts["claim_attribute_count"] == 9
        assert facts["reads"][0]["resource_matches_claim"] is True

    def test_a_read_of_another_resource_is_reported_as_not_matching(self) -> None:
        elsewhere = httpx_span("r2", "GET", "http://h/clinic/appointments/apt-somewhere-else", 200)
        facts = transport_facts([PERFORMED[0], PERFORMED[1], elsewhere])
        assert facts["reads"][0]["resource_matches_claim"] is False

    def test_the_appointments_collection_is_not_counted_as_a_read(self) -> None:
        listing = httpx_span("r3", "GET", "http://h/clinic/appointments", 200)
        assert transport_facts([listing])["clinic_read_count"] == 0

    def test_both_status_spellings_are_reported_unreconciled(self) -> None:
        facts = transport_facts(PERFORMED)
        assert facts["writes"][0]["status"] == (201, 201, "UNSET")

    def test_the_otel_status_is_read_from_the_nested_object(self) -> None:
        """Production carries it nested. Reporting ``None`` there would misdescribe the span."""
        assert otel_status({"status": {"code": "UNSET"}}) == "UNSET"
        assert otel_status({"attributes": {"status.code": "ERROR"}}) == "ERROR"
        assert otel_status({}) is None

    def test_a_trace_with_no_claim_reports_no_attribute_names(self) -> None:
        facts = transport_facts([PERFORMED[1], PERFORMED[2]])
        assert facts["claim_span_count"] == 0
        assert facts["claim_attribute_names"] == []

    def test_two_claim_spans_are_reported_rather_than_resolved(self) -> None:
        """Ambiguity is a finding. The tool counts; it never picks one."""
        facts = transport_facts([claim_span(), claim_span(), PERFORMED[1]])
        assert facts["claim_span_count"] == 2
        assert facts["claim_attribute_names"] == []


class TestDeduplicationNeverLosesEvidence:
    def test_one_span_per_id(self) -> None:
        assert len(deduplicate([PERFORMED[1], dict(PERFORMED[1])])) == 1

    def test_a_span_with_no_id_is_kept(self) -> None:
        anonymous = {"traceId": TRACE}
        assert deduplicate([anonymous, dict(anonymous)]) == [anonymous, anonymous]


class TestThePayloadDigestIsOfWhatIsSent:
    def test_the_same_spans_hash_the_same(self) -> None:
        assert canonical_payload(PERFORMED) == canonical_payload([dict(s) for s in PERFORMED])

    def test_a_changed_attribute_changes_the_payload(self) -> None:
        tampered = [dict(span) for span in PERFORMED]
        tampered[0] = claim_span(**{"careloop.observation.outcome": "DIVERGED"})
        assert canonical_payload(tampered) != canonical_payload(PERFORMED)


class TestAnExpectationIsOnlyMetWhenItIsMet:
    """The guard against an evidence record that flatters the run that produced it."""

    def test_a_pass_is_met_by_a_pass(self) -> None:
        result = {
            "label": "PASS",
            "value": 1.0,
            "value_present": True,
            "explanation": PASS_EXPLANATION,
        }
        assert check_expectation("PASS", result) == "met"

    def test_a_pass_is_not_met_by_a_fail(self) -> None:
        result = {"label": "FAIL", "value": 0.0, "value_present": True, "explanation": ""}
        assert check_expectation("PASS", result) == "NOT MET"

    def test_a_named_fail_is_met_only_by_that_check(self) -> None:
        differ = CHECK_EXPLANATIONS[CheckId.STATE_DIGESTS_DIFFER]
        other = CHECK_EXPLANATIONS[CheckId.OBSERVATION_NOT_SUCCESSFUL]
        met = {"label": "FAIL", "value": 0.0, "value_present": True, "explanation": differ}
        wrong = {"label": "FAIL", "value": 0.0, "value_present": True, "explanation": other}
        assert check_expectation("FAIL:STATE_DIGESTS_DIFFER", met) == "met"
        assert check_expectation("FAIL:STATE_DIGESTS_DIFFER", wrong) == "NOT MET"

    def test_not_applicable_requires_the_value_key_to_be_absent(self) -> None:
        without = {"label": "NOT_APPLICABLE", "value_present": False, "value": None}
        with_value = {"label": "NOT_APPLICABLE", "value_present": True, "value": 0.0}
        assert check_expectation("NOT_APPLICABLE", without) == "met"
        assert check_expectation("NOT_APPLICABLE", with_value) == "NOT MET"

    def test_an_error_result_meets_nothing(self) -> None:
        errored = {"label": None, "value_present": False, "errorCode": "INTERNAL_ERROR"}
        for expectation in ("PASS", "NOT_APPLICABLE", "FAIL:STATE_DIGESTS_DIFFER"):
            assert check_expectation(expectation, errored) == "NOT MET"

    def test_no_expectation_asserts_nothing(self) -> None:
        assert check_expectation(None, {"label": "PASS"}) == "not asserted"
