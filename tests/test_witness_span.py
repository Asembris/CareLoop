"""What actually lands on a span, driven by a real graph run under a real OpenTelemetry SDK.

``tests/test_witness_claims.py`` proves the domain hands the right facts to a witness. This proves
the last hop: that the deployment adapter turns those facts into exactly nine attributes, that they
all land on the **one** span the graph is executing inside, and that every way of having no usable
span leaves both the run and the process unharmed.

**The nine are asserted as a set, by namespace and not by name.** The sweep is
``{k for k in span.attributes if k.startswith("careloop.")}``, so a tenth attribute fails this file
whatever somebody decides to call it -- including ``careloop.effect.action_key``, which is
additionally named and asserted absent because it is the one that was deliberately dropped.

**The graph span is Strands' own, and that took finding out.**
:func:`strands_tracing` binds ``strands.telemetry``'s tracer to this test's provider, so
``invoke_graph`` is created by ``Graph.stream_async`` exactly as it is in production and the
attributes are asserted where production would put them. The obvious harness -- opening a span
called ``invoke_graph`` by hand and running the graph inside it -- **silently tests nothing**:
Strands takes its tracer from the *global* provider, which is a no-op in the offline suite, so its
own ``trace_api.use_span(...)`` replaces the hand-rolled span with a ``NonRecordingSpan`` for the
whole of the run and every attribute is discarded. Measured, not reasoned about; the exact-nine
assertion is what caught it. Patch A's capture agrees from the other side, where every httpx span
carries ``parentSpanId`` equal to ``invoke_graph``'s.

The SDK machinery is otherwise the same ``tests/test_observability.py`` already uses: an in-memory
exporter on a provider built for the test, never the global one.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
import strands.telemetry.tracer as strands_tracer
from clinic_helpers import clinic_server
from helpers import FIXED_NOW
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_verify_readback import BOOKING, CASE_ID, CannotBeReadBack, ChangesTheReason
from test_witness_claims import AUTHORISED_DIGEST, CANONICAL_FIELDS, CARER, run_booking

from careloop.api.deps import ContextFactory, default_context_factory, simulated_context_factory
from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.runner import request_recheck
from careloop.graph.verify import ObservationOutcome, Verification
from careloop.graph.witness import (
    NO_WITNESS,
    EffectDispatch,
    NullWitness,
    witness_effect,
    witness_observation,
)
from careloop.state import CaseState, InMemoryCaseStore
from careloop.tools.clinic import ClinicClient
from careloop.tools.results import AppointmentBooked
from scripts.witness_span import (
    ATTR_EFFECT_DISPATCH,
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_STATE_DIGEST,
    ATTR_EFFECT_TYPE,
    ATTR_OBSERVATION_MODE,
    ATTR_OBSERVATION_OUTCOME,
    ATTR_OBSERVATION_RESOURCE_ID,
    ATTR_OBSERVATION_STATE_DIGEST,
    ATTR_SETTLEMENT,
    CLAIM_ATTRIBUTES,
    CLAIM_NAMESPACE,
    SpanAttributeWitness,
    span_witness,
)

GRAPH_SPAN = "invoke_graph"


class _RefusesEveryAttribute(trace_api.NonRecordingSpan):
    """A span that raises on ``set_attribute``, which no real one does and a broken one might."""

    def set_attribute(self, key: str, value: Any) -> None:
        raise RuntimeError("this span refuses attributes")


_HOSTILE_SPAN = _RefusesEveryAttribute(trace_api.INVALID_SPAN_CONTEXT)

_BOOKED = AppointmentBooked(confirmation_id="apt-c98a9981caefc548", **BOOKING)

_MATCHED = Verification(
    passed=True,
    diagnostic="verified against the external system",
    externally_observed=True,
    outcome=ObservationOutcome.MATCHED,
)


def recording_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """A provider of this test's own, never the global one."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@contextlib.contextmanager
def strands_tracing(provider: TracerProvider) -> Iterator[None]:
    """Make Strands record its graph span on ``provider`` for the duration.

    ``strands.telemetry.get_tracer()`` caches one module-level ``Tracer`` that captured the global
    provider when it was first built. Rebinding that singleton is the smallest way to reproduce an
    instrumented process without touching OpenTelemetry's global state -- which the offline suite
    asserts is a no-op, and which is not restorable once ``set_tracer_provider`` has been called.

    Restored on the way out, so nothing here leaks into a neighbouring test.
    """
    previous = strands_tracer._tracer_instance
    bound = strands_tracer.Tracer()
    bound.tracer_provider = provider
    bound.tracer = provider.get_tracer(bound.service_name)
    strands_tracer._tracer_instance = bound
    try:
        yield
    finally:
        strands_tracer._tracer_instance = previous


def graph_span_of(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = [span for span in exporter.get_finished_spans() if span.name == GRAPH_SPAN]
    assert len(spans) == 1, f"expected one {GRAPH_SPAN} span, found {len(spans)}"
    return spans[0]


def careloop_attributes(span: ReadableSpan) -> dict[str, Any]:
    return {
        name: value
        for name, value in (span.attributes or {}).items()
        if name.startswith(CLAIM_NAMESPACE)
    }


def traced_booking(build_clinic, **kwargs) -> tuple[ReadableSpan, Any, InMemorySpanExporter]:
    """One booking run under an instrumented process, and the graph span it produced."""
    provider, exporter = recording_provider()
    with clinic_server() as (base_url, world), strands_tracing(provider):
        result, _context = run_booking(
            build_clinic(base_url, world), SpanAttributeWitness(), **kwargs
        )
    return graph_span_of(exporter), result, exporter


@pytest.fixture(scope="module")
def settled() -> tuple[ReadableSpan, Any, InMemorySpanExporter]:
    return traced_booking(lambda url, _w: ClinicClient(url))


# =================================================================================================
# The exact nine
# =================================================================================================


class TestASettledBookingCarriesExactlyTheNineAttributes:
    def test_the_attribute_set_is_the_frozen_nine(self, settled) -> None:
        span, _result, _exporter = settled
        assert set(careloop_attributes(span)) == set(CLAIM_ATTRIBUTES)

    def test_there_is_no_tenth_attribute_under_any_name(self, settled) -> None:
        span, _result, _exporter = settled
        assert len(careloop_attributes(span)) == 9

    def test_no_correlation_identifier_of_careloops_own_is_emitted(self, settled) -> None:
        """``careloop.effect.action_key`` was dropped by name and must not return."""
        span, _result, _exporter = settled
        names = set(span.attributes or {})
        assert "careloop.effect.action_key" not in names
        assert not any("action_key" in name for name in names)
        assert not any("idempotency" in name for name in names)

    def test_every_value_is_a_string(self, settled) -> None:
        span, _result, _exporter = settled
        for name, value in careloop_attributes(span).items():
            assert isinstance(value, str), name

    def test_the_values_are_the_ones_the_domain_established(self, settled) -> None:
        span, result, _exporter = settled
        attributes = careloop_attributes(span)
        assert result.case.state is CaseState.DONE
        assert attributes == {
            ATTR_EFFECT_TYPE: "book_appointment",
            ATTR_EFFECT_DISPATCH: "PERFORMED",
            ATTR_EFFECT_FIELDS: CANONICAL_FIELDS,
            ATTR_EFFECT_STATE_DIGEST: AUTHORISED_DIGEST,
            ATTR_OBSERVATION_MODE: "INDEPENDENT_READ",
            ATTR_OBSERVATION_OUTCOME: "MATCHED",
            ATTR_OBSERVATION_RESOURCE_ID: attributes[ATTR_OBSERVATION_RESOURCE_ID],
            ATTR_OBSERVATION_STATE_DIGEST: AUTHORISED_DIGEST,
            ATTR_SETTLEMENT: "VERIFIED",
        }

    def test_both_digests_are_lowercase_sixty_four_hex(self, settled) -> None:
        span, _result, _exporter = settled
        attributes = careloop_attributes(span)
        for name in (ATTR_EFFECT_STATE_DIGEST, ATTR_OBSERVATION_STATE_DIGEST):
            digest = attributes[name]
            assert len(digest) == 64 and set(digest) <= set("0123456789abcdef"), name

    def test_the_resource_id_is_a_single_safe_token(self, settled) -> None:
        span, _result, _exporter = settled
        resource = careloop_attributes(span)[ATTR_OBSERVATION_RESOURCE_ID]
        assert resource.startswith("apt-")
        assert not any(character in resource for character in "/?# %\n")
        assert len(resource) <= 64

    def test_the_span_is_the_one_strands_opens_for_the_graph(self, settled) -> None:
        """Not a span this test invented: the harness would silently pass if it were."""
        span, _result, _exporter = settled
        assert span.attributes["gen_ai.operation.name"] == "invoke_graph"
        assert span.attributes["gen_ai.system"] == "strands-agents"


class TestTheOptionalAttributesAreOmittedRatherThanEmptied:
    def test_a_failed_read_carries_no_observed_digest(self) -> None:
        span, result, _exporter = traced_booking(CannotBeReadBack)
        attributes = careloop_attributes(span)
        assert result.case.state is CaseState.AWAITING_HUMAN
        assert ATTR_OBSERVATION_STATE_DIGEST not in attributes
        assert attributes[ATTR_OBSERVATION_OUTCOME] == "UNAVAILABLE"
        assert attributes[ATTR_SETTLEMENT] == "NOT_CERTIFIED"

    def test_the_resource_is_still_named_on_a_failed_read(self) -> None:
        span, _result, _exporter = traced_booking(CannotBeReadBack)
        assert ATTR_OBSERVATION_RESOURCE_ID in careloop_attributes(span)

    def test_a_diverging_read_carries_two_different_digests(self) -> None:
        span, result, _exporter = traced_booking(ChangesTheReason)
        attributes = careloop_attributes(span)
        assert result.case.state is CaseState.AWAITING_HUMAN
        assert attributes[ATTR_EFFECT_STATE_DIGEST] == AUTHORISED_DIGEST
        assert attributes[ATTR_OBSERVATION_STATE_DIGEST] != AUTHORISED_DIGEST
        assert attributes[ATTR_OBSERVATION_OUTCOME] == "DIVERGED"
        assert attributes[ATTR_SETTLEMENT] == "NOT_CERTIFIED"

    def test_a_simulated_run_says_plainly_that_it_read_nothing(self) -> None:
        provider, exporter = recording_provider()
        with strands_tracing(provider):
            run_booking(None, SpanAttributeWitness())
        attributes = careloop_attributes(graph_span_of(exporter))
        assert attributes[ATTR_OBSERVATION_MODE] == "NONE"
        assert attributes[ATTR_OBSERVATION_OUTCOME] == "NOT_OBSERVED"
        assert attributes[ATTR_SETTLEMENT] == "VERIFIED"
        assert ATTR_OBSERVATION_STATE_DIGEST not in attributes


# =================================================================================================
# One span, and it is the graph's
# =================================================================================================


class TestEverythingLandsOnTheOneSpanTheGraphRunsInside:
    def test_the_claim_is_on_the_graph_span_and_on_no_other(self) -> None:
        provider, exporter = recording_provider()
        tracer = provider.get_tracer(__name__)
        with clinic_server() as (base_url, _world), strands_tracing(provider):
            with trace_api.use_span(tracer.start_span("a span of no interest"), end_on_exit=True):
                pass
            run_booking(ClinicClient(base_url), SpanAttributeWitness())

        carrying = [span for span in exporter.get_finished_spans() if careloop_attributes(span)]
        assert len(exporter.get_finished_spans()) > 1
        assert len(carrying) == 1
        assert carrying[0].name == GRAPH_SPAN

    def test_exactly_one_span_in_the_trace_claims_an_effect(self) -> None:
        """Patch B refuses a trace whose effect is claimed twice, so this is the binding one."""
        _span, _result, exporter = traced_booking(lambda url, _w: ClinicClient(url))
        claiming = [
            span
            for span in exporter.get_finished_spans()
            if ATTR_EFFECT_TYPE in (span.attributes or {})
        ]
        assert len(claiming) == 1

    def test_a_re_check_also_claims_once_and_on_the_graph_span(self) -> None:
        provider, exporter = recording_provider()
        with clinic_server() as (base_url, world):
            parked, context = run_booking(ChangesTheReason(base_url, world), NO_WITNESS)
            assert parked.case.state is CaseState.AWAITING_HUMAN
            store = context.store
            world.amend(world.appointments()[0].confirmation_id, reason=BOOKING["reason"])
            request_recheck(store, CASE_ID, requested_by=CARER, now=FIXED_NOW)
            with strands_tracing(provider):
                result, _ = run_booking(
                    ClinicClient(base_url),
                    SpanAttributeWitness(),
                    store=store,
                    max_node_executions=8,
                )

        attributes = careloop_attributes(graph_span_of(exporter))
        assert result.executed_nodes == ("dispatch", "verify")
        assert set(attributes) == set(CLAIM_ATTRIBUTES)
        assert attributes[ATTR_EFFECT_DISPATCH] == "NOT_IN_THIS_TRACE"
        assert attributes[ATTR_EFFECT_STATE_DIGEST] == AUTHORISED_DIGEST
        assert attributes[ATTR_SETTLEMENT] == "VERIFIED"

    def test_two_runs_are_two_spans_and_neither_carries_the_others_claim(self) -> None:
        provider, exporter = recording_provider()
        with clinic_server() as (base_url, _world), strands_tracing(provider):
            for _ in range(2):
                run_booking(ClinicClient(base_url), SpanAttributeWitness())

        graphs = [s for s in exporter.get_finished_spans() if s.name == GRAPH_SPAN]
        assert len(graphs) == 2
        for graph in graphs:
            assert len(careloop_attributes(graph)) == 9


# =================================================================================================
# Failure isolation
# =================================================================================================


class TestNoUsableSpanIsNeverAProblem:
    def test_an_uninstrumented_process_records_nothing_and_raises_nothing(self) -> None:
        """The ordinary local case: the global provider is a no-op, so the span is not recording."""
        with clinic_server() as (base_url, world):
            result, context = run_booking(ClinicClient(base_url), SpanAttributeWitness())
            held = world.appointments()
        assert result.case.state is CaseState.DONE
        assert context.executor.invocation_count == 1
        assert len(held) == 1

    def test_the_span_a_node_sees_there_really_is_not_recording(self) -> None:
        """Vacuity proof for the test above."""
        assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)
        assert not trace_api.get_current_span().is_recording()

    @pytest.mark.parametrize(
        ("label", "span"),
        [
            ("a span that refuses every attribute", _HOSTILE_SPAN),
            (
                "a span that records nothing",
                trace_api.NonRecordingSpan(trace_api.INVALID_SPAN_CONTEXT),
            ),
            ("no span at all", trace_api.INVALID_SPAN),
        ],
    )
    def test_the_adapter_survives_whatever_span_it_resolves(
        self, label: str, span: trace_api.Span
    ) -> None:
        """**The adapter does not catch; the seam does**, and this is what proves it covers this.

        Asserted at the seam rather than around a whole graph run, because the two ways of forcing
        a hostile span into a run are both worse. Patching ``trace_api.get_current_span`` is not
        narrow enough -- Strands resolves the parent of its own graph span through the same
        function, so the patch breaks the framework instead of the thing under test, which is how
        this test was first written and what it actually measured. Attaching a hostile span around
        the run does nothing, because Strands replaces the context with its own graph span.

        The run-level half of the property is already held by
        ``tests/test_witness_claims.py``'s ``ThrowingWitness``, which fails at exactly these two
        call sites and leaves every business fact identical.
        """
        with trace_api.use_span(span, end_on_exit=False):
            witness_effect(
                SpanAttributeWitness(),
                action_type="book_appointment",
                dispatch=EffectDispatch.PERFORMED,
                args=BOOKING,
            )
            witness_observation(
                SpanAttributeWitness(),
                action_type="book_appointment",
                verification=_MATCHED,
                result=_BOOKED,
                observed=_BOOKED,
            )

    def test_the_hostile_span_really_does_refuse(self) -> None:
        """Vacuity proof: the first row above has to be a span that would break an unguarded
        adapter."""
        with pytest.raises(RuntimeError):
            _HOSTILE_SPAN.set_attribute("careloop.effect.type", "book_appointment")

    def test_the_adapter_itself_holds_nothing(self) -> None:
        assert not vars(SpanAttributeWitness())


# =================================================================================================
# Composition
# =================================================================================================


class TestWhichWitnessAProcessGets:
    def test_an_uninstrumented_process_is_given_none(self) -> None:
        assert isinstance(span_witness(trace_api.NoOpTracerProvider()), NullWitness)

    def test_the_global_provider_is_uninstrumented_in_this_suite(self) -> None:
        """Vacuity proof: the branch above is the one an offline run actually takes."""
        assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)
        assert isinstance(span_witness(), NullWitness)

    def test_a_real_sdk_provider_is_given_a_span_writing_one(self) -> None:
        provider, _exporter = recording_provider()
        assert isinstance(span_witness(provider), SpanAttributeWitness)

    def test_asking_has_no_side_effect_on_the_provider(self) -> None:
        """Unlike ``install_telemetry_redaction``, which is why its boolean was not reused."""
        provider, exporter = recording_provider()
        before = len(provider._active_span_processor._span_processors)
        span_witness(provider)
        span_witness(provider)
        assert len(provider._active_span_processor._span_processors) == before
        assert exporter.get_finished_spans() == ()

    def test_the_simulated_factory_matches_the_default_one_when_given_no_witness(self) -> None:
        """Two ways of building one thing, and the second must not have become different."""
        store = InMemoryCaseStore()
        built: ContextFactory = simulated_context_factory()
        one = built(store, REFERRAL_EVENT, CASE_ID)
        two = default_context_factory(store, REFERRAL_EVENT, CASE_ID)

        assert one.witness is two.witness is NO_WITNESS
        assert one.case_id == two.case_id
        assert one.clinic is two.clinic is None
        assert one.readbacks == two.readbacks == {}
        assert one.event_text == two.event_text
        assert one.document_ids == two.document_ids
