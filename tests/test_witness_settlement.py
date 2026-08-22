"""The production claim, judged by the frozen evaluator, without AWS.

The whole point of Patch D is that production traces satisfy a contract Patch B proved and Patch C
attacked. Every other test in this session checks one half of that: the domain claims the right
facts, the adapter writes the right nine attributes. This checks the join -- it takes the attributes
**a real graph run wrote onto Strands' own graph span** and hands them to
:func:`evaluators.settlement.assess`.

**Nothing about the CareLoop claim is hand-written here.** ``claim_from`` reads the exported span
and passes whatever it finds to ``test_evaluator``'s own ``graph_span`` builder, so an attribute
this session got wrong reaches the evaluator wrong. A fixture that transcribed the nine expected
values would be a test of this file's typing.

**The transport spans are fixtures, and that is the honest limit.**
``opentelemetry-instrumentation-httpx`` ships with the ``observability`` extra and not with ``dev``,
so an offline run records no httpx spans to carry as evidence. They are built with the same measured
Patch A shapes ``tests/test_evaluator.py`` uses -- ``http.method``, ``http.url``, both status
spellings, ``status.code`` of ``UNSET`` -- and parameterised with the **real** confirmation id the
run produced and the **real** loopback URL it reached, so the correlation the evaluator performs is
against a value CareLoop did not choose. What is not proven here is that ADOT's httpx
instrumentation emits those exact keys, which is Patch A's measurement and Patch E's re-measurement,
not this file's.

**The direction of the dependency is unchanged.** This test imports ``careloop`` and ``evaluators``;
neither package imports the other, which ``tests/test_evaluator.py`` and
``tests/test_witness_seam.py`` both sweep for.
"""

from __future__ import annotations

from typing import Any

import pytest
from clinic_helpers import clinic_server
from helpers import FIXED_NOW
from opentelemetry.sdk.trace import ReadableSpan
from test_evaluator import (
    CLINIC,
    RESOURCE_ID,
    bedrock_spans,
    graph_span,
    internal_operation_span,
    payload,
    read_span,
    write_span,
)
from test_verify_readback import (
    BOOKING,
    CASE_ID,
    CannotBeReadBack,
    ChangesTheReason,
    LosesTheRecord,
)
from test_witness_claims import CARER, replay_store, run_booking
from test_witness_span import (
    careloop_attributes,
    graph_span_of,
    recording_provider,
    strands_tracing,
)

from careloop.graph.runner import request_recheck
from careloop.state import CaseState
from careloop.tools.clinic import ClinicClient
from evaluators.contract import CheckId, Label
from evaluators.settlement import assess
from scripts.witness_span import SpanAttributeWitness

# =================================================================================================
# Producing a real claim, then a trace around it
# =================================================================================================


def claim_from(span: ReadableSpan) -> dict[str, Any]:
    """The CareLoop attributes a real run wrote, exactly as they were written."""
    return careloop_attributes(span)


def traced(build_clinic, **kwargs) -> tuple[dict[str, Any], Any, str]:
    """Run a booking under an instrumented process; return its claim, result and confirmation."""
    provider, exporter = recording_provider()
    with clinic_server() as (base_url, world), strands_tracing(provider):
        result, _context = run_booking(
            build_clinic(base_url, world), SpanAttributeWitness(), **kwargs
        )
        confirmation = result.case.actions[-1].idempotency_key[:16]
    return claim_from(graph_span_of(exporter)), result, f"apt-{confirmation}"


def trace_with_write(claim: dict[str, Any], resource: str) -> list[dict[str, Any]]:
    """The six-span shape Patch A measured: a booking that was performed and then read back."""
    return [
        internal_operation_span(),
        graph_span(**claim),
        *bedrock_spans(),
        write_span(),
        read_span(resource=resource),
    ]


def trace_without_write(claim: dict[str, Any], resource: str) -> list[dict[str, Any]]:
    """A trace whose write belongs elsewhere: a suppressed duplicate, or a caregiver's re-check."""
    return [
        internal_operation_span(),
        graph_span(**claim),
        *bedrock_spans(),
        read_span(resource=resource),
    ]


def judge(spans: list[dict[str, Any]]) -> dict[str, Any]:
    return assess(payload(spans))


def explanation_of(check: CheckId) -> str:
    from evaluators.contract import CHECK_EXPLANATIONS

    return CHECK_EXPLANATIONS[check]


# =================================================================================================
# The three shapes that settle
# =================================================================================================


class TestAPerformedBookingSettles:
    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        claim, result, resource = traced(lambda url, _w: ClinicClient(url))
        return claim, result, judge(trace_with_write(claim, resource))

    def test_the_run_really_booked_and_verified(self, judged) -> None:
        _claim, result, _verdict = judged
        assert result.case.state is CaseState.DONE

    def test_the_claim_is_the_nine_attributes(self, judged) -> None:
        claim, _result, _verdict = judged
        assert len(claim) == 9

    def test_the_evaluator_passes_it(self, judged) -> None:
        _claim, _result, verdict = judged
        assert verdict["label"] == Label.PASS.value
        assert verdict["value"] == 1.0

    def test_the_pass_is_not_an_accident_of_the_fixture(self, judged) -> None:
        """Remove the independent read and the same claim must stop settling."""
        claim, _result, _verdict = judged
        without_read = [
            internal_operation_span(),
            graph_span(**claim),
            *bedrock_spans(),
            write_span(),
        ]
        verdict = judge(without_read)
        assert verdict["label"] == Label.FAIL.value
        assert verdict["explanation"] == explanation_of(CheckId.OBSERVATION_ABSENT_FROM_TRACE)


class TestASuppressedDuplicateSettles:
    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        provider, exporter = recording_provider()
        with clinic_server() as (base_url, _world):
            first, _ = run_booking(ClinicClient(base_url), SpanAttributeWitness())
            assert first.case.state is CaseState.DONE
            with strands_tracing(provider):
                result, context = run_booking(
                    ClinicClient(base_url),
                    SpanAttributeWitness(),
                    store=replay_store(first.case),
                )
        claim = claim_from(graph_span_of(exporter))
        resource = f"apt-{result.case.actions[-1].idempotency_key[:16]}"
        return claim, result, context, judge(trace_without_write(claim, resource))

    def test_no_second_effect_was_dispatched(self, judged) -> None:
        _claim, _result, context, _verdict = judged
        assert context.executor.invocation_count == 0

    def test_the_claim_says_the_effect_was_suppressed(self, judged) -> None:
        claim, _result, _context, _verdict = judged
        assert claim["careloop.effect.dispatch"] == "SUPPRESSED"

    def test_the_evaluator_passes_it_with_no_write_in_the_trace(self, judged) -> None:
        _claim, _result, _context, verdict = judged
        assert verdict["label"] == Label.PASS.value

    def test_a_write_beside_that_claim_would_contradict_it(self, judged) -> None:
        """``SUPPRESSED`` forbids a write, and the evaluator is what enforces that."""
        claim, _result, _context, _verdict = judged
        resource = claim["careloop.observation.resource_id"]
        verdict = judge(trace_with_write(claim, resource))
        assert verdict["label"] == Label.FAIL.value
        assert verdict["explanation"] == explanation_of(CheckId.DISPATCH_CONTRADICTS_TRANSPORT)


class TestACaregiversReCheckSettles:
    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        provider, exporter = recording_provider()
        with clinic_server() as (base_url, world):
            parked, context = run_booking(ChangesTheReason(base_url, world), SpanAttributeWitness())
            assert parked.case.state is CaseState.AWAITING_HUMAN
            store = context.store
            world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
            with strands_tracing(provider):
                result, rerun = run_booking(
                    ClinicClient(base_url),
                    SpanAttributeWitness(),
                    store=store,
                    max_node_executions=8,
                )
            confirmation = world.appointments()[0].confirmation_id
        claim = claim_from(graph_span_of(exporter))
        return claim, result, rerun, judge(trace_without_write(claim, confirmation))

    def test_the_re_check_dispatched_nothing(self, judged) -> None:
        _claim, result, rerun, _verdict = judged
        assert result.executed_nodes == ("dispatch", "verify")
        assert rerun.executor.invocation_count == 0

    def test_the_claim_says_the_effect_belongs_to_an_earlier_trace(self, judged) -> None:
        claim, _result, _rerun, _verdict = judged
        assert claim["careloop.effect.dispatch"] == "NOT_IN_THIS_TRACE"

    def test_the_evaluator_passes_it(self, judged) -> None:
        _claim, _result, _rerun, verdict = judged
        assert verdict["label"] == Label.PASS.value

    def test_the_two_digests_agree_because_the_world_was_corrected(self, judged) -> None:
        claim, _result, _rerun, _verdict = judged
        assert claim["careloop.effect.state_digest"] == claim["careloop.observation.state_digest"]


# =================================================================================================
# The shapes that must not settle
# =================================================================================================


class TestADivergentExternalRecordFails:
    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        claim, result, resource = traced(ChangesTheReason)
        return claim, result, judge(trace_with_write(claim, resource))

    def test_the_case_was_parked_rather_than_finished(self, judged) -> None:
        _claim, result, _verdict = judged
        assert result.case.state is CaseState.AWAITING_HUMAN

    def test_the_evaluator_fails_it_on_the_state_digests(self, judged) -> None:
        _claim, _result, verdict = judged
        assert verdict["label"] == Label.FAIL.value
        assert verdict["value"] == 0.0
        assert verdict["explanation"] == explanation_of(CheckId.STATE_DIGESTS_DIFFER)

    def test_the_digests_really_do_differ(self, judged) -> None:
        """The failure is evidence, not a missing attribute."""
        claim, _result, _verdict = judged
        assert claim["careloop.effect.state_digest"] != claim["careloop.observation.state_digest"]
        assert claim["careloop.observation.outcome"] == "DIVERGED"


class TestAClinicThatHoldsNothingFails:
    def test_the_evaluator_refuses_a_read_that_did_not_succeed(self) -> None:
        claim, result, resource = traced(LosesTheRecord)
        assert result.case.state is CaseState.AWAITING_HUMAN
        spans = [
            internal_operation_span(),
            graph_span(**claim),
            *bedrock_spans(),
            write_span(),
            read_span(resource=resource, status=404, response_status=404),
        ]
        verdict = judge(spans)
        assert verdict["label"] == Label.FAIL.value
        assert verdict["explanation"] == explanation_of(CheckId.OBSERVATION_NOT_SUCCESSFUL)

    def test_the_claim_reports_the_absence_rather_than_hiding_it(self) -> None:
        claim, _result, _resource = traced(LosesTheRecord)
        assert claim["careloop.observation.outcome"] == "ABSENT"
        assert claim["careloop.settlement"] == "NOT_CERTIFIED"
        assert "careloop.observation.state_digest" not in claim


class TestAReadThatCouldNotBeCompletedFails:
    def test_the_evaluator_refuses_it_with_no_read_span_in_the_trace(self) -> None:
        claim, result, _resource = traced(CannotBeReadBack)
        assert result.case.state is CaseState.AWAITING_HUMAN
        spans = [internal_operation_span(), graph_span(**claim), *bedrock_spans(), write_span()]
        verdict = judge(spans)
        assert verdict["label"] == Label.FAIL.value
        assert verdict["explanation"] == explanation_of(CheckId.OBSERVATION_ABSENT_FROM_TRACE)

    def test_the_claim_is_still_complete_rather_than_withheld(self) -> None:
        """Patch C protects against a failed trace hiding as ``NOT_APPLICABLE``."""
        claim, _result, _resource = traced(CannotBeReadBack)
        assert claim["careloop.effect.type"] == "book_appointment"
        assert claim["careloop.observation.mode"] == "INDEPENDENT_READ"
        assert claim["careloop.settlement"] == "NOT_CERTIFIED"


class TestASimulatedBookingIsRefused:
    """The honest consequence of running with no clinic bound, and it is refused *early*.

    **Not** ``NO_INDEPENDENT_OBSERVER``, which was the expectation this test was written with and
    which the evaluator corrected. A simulated booking claims ``PERFORMED`` truthfully — the domain
    really did dispatch — and there is no clinic write in the trace to support it, so
    ``DISPATCH_CONTRADICTS_TRANSPORT`` fires several checks before the observer is ever considered.
    That ordering is right: a claim that disagrees with the transport beside it is a more serious
    finding than a missing read, and reporting the milder one would be describing the trace wrongly.

    This is the known limitation Patch D carries rather than papers over. Settlement integrity is a
    property of the **clinic-bound** deployment, which is what ``deploy/careloop.env`` configures;
    a fourth ``Dispatch`` member meaning *simulated* would make the evaluator accept a trace with no
    external effect in it at all, which is the one thing it exists to refuse.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        provider, exporter = recording_provider()
        with strands_tracing(provider):
            result, _ = run_booking(None, SpanAttributeWitness())
        claim = claim_from(graph_span_of(exporter))
        spans = [internal_operation_span(), graph_span(**claim), *bedrock_spans()]
        return claim, result, judge(spans)

    def test_the_case_itself_finished_perfectly_well(self, judged) -> None:
        _claim, result, _verdict = judged
        assert result.case.state is CaseState.DONE

    def test_the_claim_is_truthful_about_both_halves(self, judged) -> None:
        claim, _result, _verdict = judged
        assert claim["careloop.effect.dispatch"] == "PERFORMED"
        assert claim["careloop.observation.mode"] == "NONE"
        assert claim["careloop.observation.outcome"] == "NOT_OBSERVED"

    def test_the_evaluator_refuses_it_on_the_transport_disagreement(self, judged) -> None:
        _claim, _result, verdict = judged
        assert verdict["label"] == Label.FAIL.value
        assert verdict["explanation"] == explanation_of(CheckId.DISPATCH_CONTRADICTS_TRANSPORT)

    def test_it_is_never_reported_as_nothing_to_judge(self, judged) -> None:
        """Patch C's false-``NOT_APPLICABLE`` guard, from this side."""
        _claim, _result, verdict = judged
        assert verdict["label"] != Label.NOT_APPLICABLE.value


# =================================================================================================
# The correlation the evaluator performs is against something CareLoop did not choose
# =================================================================================================


class TestTheResourceCorrelationIsRealRatherThanCircular:
    @staticmethod
    @pytest.fixture(scope="class")
    def judged():
        return traced(lambda url, _w: ClinicClient(url))

    def test_the_claimed_resource_is_the_clinics_own_identifier(self, judged) -> None:
        claim, result, resource = judged
        assert claim["careloop.observation.resource_id"] == resource
        assert resource in result.case.actions[-1].result

    def test_a_read_of_a_different_resource_does_not_support_the_claim(self, judged) -> None:
        claim, _result, _resource = judged
        verdict = judge(trace_with_write(claim, RESOURCE_ID))
        assert verdict["label"] == Label.FAIL.value
        assert verdict["explanation"] == explanation_of(CheckId.RESOURCE_MISMATCH)

    def test_the_fixture_url_and_the_claim_share_nothing_but_the_identifier(self, judged) -> None:
        """The claim carries no URL, so the correlation is between two independent names."""
        claim, _result, _resource = judged
        assert not any(CLINIC in value for value in claim.values())
        assert not any("http" in value for value in claim.values())
