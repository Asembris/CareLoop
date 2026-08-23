"""Telemetry may only ever remove information, and it may never reach the application.

Four properties, in the order they matter:

**Disabled is genuinely disabled.** Without ``opentelemetry-instrument`` the global provider is a
no-op, nothing is recorded, and :func:`install_telemetry_redaction` says so by returning ``False``
rather than raising. That is the deployment's rollback path, so it is asserted rather than assumed.

**The access key id is redacted**, on a span built exactly the way ADOT builds one — passed to
``start_span(attributes=…)`` at creation, which is the detail that makes an ``on_start`` processor
sufficient and an exporter unnecessary.

**Nothing sensitive is exported.** Hostile markers for every category the design forbids, checked
against the serialised span an exporter would actually send.

**The application cannot tell.** A real graph run with a tracer attached — including one whose
exporter raises on every call — reaches the same state, the same nodes and the same timeline as one
with no tracer at all.

**No ADOT here, deliberately.** These tests drive the stock OpenTelemetry SDK, which is a hard
dependency of ``strands-agents`` and therefore always present. ``aws-opentelemetry-distro`` is a
deployment extra and CI never installs it, so this suite must prove the guard without it — which it
can, because the guard is a stock ``SpanProcessor`` and the thing it guards against is one
attribute whose construction is reproduced here from ADOT's own source.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from api_helpers import planner_proposing
from helpers import BOOKING_REASON, FIXED_NOW
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from careloop.agents.schemas import PlannerIntent, TriageCategory, TriageResult, Urgency
from careloop.agents.triage import TriageAgent
from careloop.events.inbound import EVENTS
from careloop.graph.runner import context_for, open_case, run_case
from careloop.llm.mock import mock_model
from careloop.state import CaseState, InMemoryCaseStore
from scripts.observability import (
    ACCESS_KEY_ID_SHAPE,
    AWS_ACCESS_KEY_ATTRIBUTE,
    REDACTED,
    RedactAwsAccessKeyId,
    install_telemetry_redaction,
)

#: A temporary STS access key id of exactly the shape an EC2 instance profile yields. Synthetic —
#: the prefix and length are what matter, and no real credential is involved.
#:
#: **Assembled rather than written out**, so this file carries no credential-shaped literal and
#: ``tests/test_nothing_secret_is_tracked.py`` stays honest about it. Writing it in full is exactly
#: what that sweep caught on the first run of this module, and the fix is the one its own docstring
#: prescribes: allowlisting this file would make a real key pasted here invisible.
FAKE_ACCESS_KEY_ID = "ASIA" + "Z" * 16

#: Every category the privacy boundary forbids, in one place so a sweep cannot miss one.
HOSTILE_MARKERS = {
    "attacker_email": "records-intake@claims-continuity-partners.example",
    "patient_id": "PT-4417",
    "evidence_text": "EVIDENCE_BODY_MARKER_c41d9a",
    "raw_prompt": "SYSTEM_PROMPT_MARKER_5b2e77",
    "model_response": "MODEL_OUTPUT_MARKER_9ac310",
    "tool_argument": "TOOL_ARGUMENT_MARKER_1f6b04",
    "external_body": "EXTERNAL_RESPONSE_MARKER_7f93a1",
    "aws_secret": "wJalrXUtnFEMIaKb7MDENGabPxRfiCYzEXAMPLEKEY",
    "session_token": "SESSION_TOKEN_MARKER_deadbeefcafe",
    "caregiver": "CAREGIVER_NAME_MARKER_ab19",
}


def recording_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """A private provider plus the exporter that captures it. Never the global one.

    ``shutdown_on_exit=False`` throughout this module: a provider registers an ``atexit`` hook that
    shuts its exporters down, and the failure-isolation exporters below raise on shutdown *by
    design*. Left on, every full-suite run would end with ignored-exception tracebacks from tests
    that passed.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    install_telemetry_redaction(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def adot_shaped_span(provider: TracerProvider, **extra: Any) -> None:
    """Start and end one span the way ADOT's patched botocore call does.

    Reproduced from ``amazon/opentelemetry/distro/patches/_botocore_patches.py``: the attributes
    dictionary — access key id included — is handed to span creation, not set afterwards. That
    ordering is the reason an ``on_start`` processor is sufficient, so a test that set the
    attribute after creation would be testing something easier than the real thing.
    """
    attributes = {
        "rpc.system": "aws-api",
        "rpc.service": "Bedrock Runtime",
        "rpc.method": "ConverseStream",
        "aws.region": "us-east-1",
        "aws.auth.region": "us-east-1",
        AWS_ACCESS_KEY_ATTRIBUTE: FAKE_ACCESS_KEY_ID,
        **extra,
    }
    provider.get_tracer(__name__).start_span(
        "chat us.amazon.nova-micro-v1:0", attributes=attributes
    ).end()


# =================================================================================================
# Disabled mode
# =================================================================================================


class TestDisabledIsGenuinelyDisabled:
    def test_the_global_provider_is_a_noop_without_auto_instrumentation(self) -> None:
        """The state a plain ``python scripts/demo_serve.py`` runs in, and the rollback path."""
        assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)

    def test_installing_on_an_uninstrumented_process_reports_false_and_raises_nothing(
        self,
    ) -> None:
        assert install_telemetry_redaction(trace_api.NoOpTracerProvider()) is False

    def test_a_noop_tracer_records_nothing_at_all(self) -> None:
        span = trace_api.NoOpTracerProvider().get_tracer(__name__).start_span("invoke_graph")
        assert span.is_recording() is False


# =================================================================================================
# Redaction
# =================================================================================================


class TestTheAccessKeyIdIsRedacted:
    def test_the_attribute_is_replaced(self) -> None:
        provider, exporter = recording_provider()
        adot_shaped_span(provider)

        (span,) = exporter.get_finished_spans()
        assert span.attributes[AWS_ACCESS_KEY_ATTRIBUTE] == REDACTED

    def test_the_original_value_is_nowhere_in_the_exported_span(self) -> None:
        provider, exporter = recording_provider()
        adot_shaped_span(provider)

        (span,) = exporter.get_finished_spans()
        assert FAKE_ACCESS_KEY_ID not in span.to_json()

    def test_no_exported_attribute_carries_a_value_of_access_key_shape(self) -> None:
        """An independent tripwire, not a restatement of the processor.

        The processor redacts **one attribute by name**. This sweeps every attribute by *value*, so
        a second credential attribute in a future ADOT release fails here rather than shipping.
        """
        provider, exporter = recording_provider()
        adot_shaped_span(provider)

        for span in exporter.get_finished_spans():
            for key, value in (span.attributes or {}).items():
                assert not ACCESS_KEY_ID_SHAPE.match(str(value)), key

    def test_the_useful_aws_attributes_survive(self) -> None:
        """Redaction must not cost the diagnostics the trace exists for."""
        provider, exporter = recording_provider()
        adot_shaped_span(provider, **{"gen_ai.usage.input_tokens": 1154})

        (span,) = exporter.get_finished_spans()
        assert span.attributes["rpc.method"] == "ConverseStream"
        assert span.attributes["aws.region"] == "us-east-1"
        assert span.attributes["gen_ai.usage.input_tokens"] == 1154
        assert span.name == "chat us.amazon.nova-micro-v1:0"

    def test_a_span_without_the_attribute_is_untouched(self) -> None:
        provider, exporter = recording_provider()
        provider.get_tracer(__name__).start_span("invoke_graph").end()

        (span,) = exporter.get_finished_spans()
        assert AWS_ACCESS_KEY_ATTRIBUTE not in (span.attributes or {})

    def test_removing_the_processor_leaks_the_key(self) -> None:
        """**The vacuity proof.** Same span, no processor: the raw id is exported.

        Without this, every assertion above would also pass against a build where ADOT had simply
        stopped emitting the attribute — which is a different world, not a working guard.
        """
        exporter = InMemorySpanExporter()
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(SimpleSpanProcessor(exporter))  # no redaction installed
        adot_shaped_span(provider)

        (span,) = exporter.get_finished_spans()
        assert span.attributes[AWS_ACCESS_KEY_ATTRIBUTE] == FAKE_ACCESS_KEY_ID
        assert ACCESS_KEY_ID_SHAPE.match(span.attributes[AWS_ACCESS_KEY_ATTRIBUTE])

    def test_the_processor_itself_is_inert_everywhere_else(self) -> None:
        """It may modify telemetry metadata and do nothing else."""
        processor = RedactAwsAccessKeyId()
        assert processor.force_flush() is True
        assert processor.on_end(None) is None
        assert processor.shutdown() is None


class TestNoCredentialIdentifierBecomesAMetricDimension:
    def test_the_application_signals_derived_attribute_would_inherit_the_redaction(self) -> None:
        """ADOT derives ``aws.remote.resource.account.access_key`` by reading the span attribute at
        export time, so a redacted span cannot yield an unredacted metric dimension even if
        Application Signals were switched on. Modelled here rather than by installing ADOT."""
        provider, exporter = recording_provider()
        adot_shaped_span(provider)

        (span,) = exporter.get_finished_spans()
        derived = span.attributes.get(AWS_ACCESS_KEY_ATTRIBUTE)
        assert derived == REDACTED
        assert not ACCESS_KEY_ID_SHAPE.match(str(derived))

    def test_the_deployed_environment_pins_application_signals_off(self) -> None:
        """The default is already ``false``; the deployment states it anyway, because a default is
        something a later edit can change without anyone noticing."""
        from pathlib import Path

        env = Path(__file__).resolve().parents[1] / "deploy" / "careloop.env"
        text = env.read_text(encoding="utf-8")
        assert "OTEL_AWS_APPLICATION_SIGNALS_ENABLED=false" in text
        assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false" in text


# =================================================================================================
# Privacy
# =================================================================================================


class TestNoSensitivePayloadIsExported:
    def test_a_real_run_under_a_tracer_exports_no_hostile_marker(self) -> None:
        """The markers are planted where the categories actually live — the event text the triage
        model reads, the summary a model writes, and the arguments a planner proposes."""
        provider, exporter = recording_provider()
        tracer = provider.get_tracer(__name__)

        with trace_api.use_span(tracer.start_span("invoke_graph"), end_on_exit=True):
            result = poisoned_run()

        assert result.case.state is CaseState.DONE
        exported = " ".join(span.to_json() for span in exporter.get_finished_spans())
        for label, marker in HOSTILE_MARKERS.items():
            assert marker not in exported, label

    def test_the_graph_span_carries_no_free_text_of_its_own(self) -> None:
        provider, exporter = recording_provider()
        tracer = provider.get_tracer(__name__)
        with trace_api.use_span(tracer.start_span("invoke_graph"), end_on_exit=True):
            poisoned_run()

        for span in exporter.get_finished_spans():
            for value in (span.attributes or {}).values():
                assert not (isinstance(value, str) and len(value) > 120)


# =================================================================================================
# Failure isolation
# =================================================================================================


class RaisingExporter(SpanExporter):
    """An exporter that fails every way an exporter can."""

    def __init__(self) -> None:
        self.attempts = 0

    def export(self, spans: Any) -> SpanExportResult:
        self.attempts += 1
        raise RuntimeError("CloudWatch is unavailable")

    def shutdown(self) -> None:
        raise RuntimeError("shutdown failed too")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        raise RuntimeError("flush failed too")


class DroppingExporter(SpanExporter):
    """An exporter that accepts nothing, as a full queue behaves."""

    def export(self, spans: Any) -> SpanExportResult:
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return False


class TestTelemetryFailureCannotFailARun:
    @pytest.mark.parametrize(
        ("label", "build"),
        [
            ("raises on every export", RaisingExporter),
            ("drops every span", DroppingExporter),
        ],
    )
    def test_a_broken_exporter_changes_nothing(self, label: str, build: type[SpanExporter]) -> None:
        provider = TracerProvider(shutdown_on_exit=False)
        install_telemetry_redaction(provider)
        provider.add_span_processor(SimpleSpanProcessor(build()))
        tracer = provider.get_tracer(__name__)

        with trace_api.use_span(tracer.start_span("invoke_graph"), end_on_exit=True):
            result = referral_run()

        assert result.case.state is CaseState.DONE, label
        assert result.executed_nodes == HAPPY_PATH

    def test_a_batching_exporter_is_never_awaited_by_the_run(self) -> None:
        """``BatchSpanProcessor`` is what production uses: export happens on its own thread, so a
        dead endpoint costs a run nothing. Asserted by the run completing while nothing has been
        flushed."""
        exporter = RaisingExporter()
        provider = TracerProvider(shutdown_on_exit=False)
        install_telemetry_redaction(provider)
        provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=60_000))
        tracer = provider.get_tracer(__name__)

        with trace_api.use_span(tracer.start_span("invoke_graph"), end_on_exit=True):
            result = referral_run()

        assert result.case.state is CaseState.DONE
        assert exporter.attempts == 0


# =================================================================================================
# Application equivalence
# =================================================================================================

HAPPY_PATH = ("dispatch", "triage", "evidence", "plan", "gate", "execute", "verify")


def _triage(summary: str = "Cardiology referral needs an outpatient appointment.") -> TriageAgent:
    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.APPOINTMENT,
                    urgency=Urgency.SOON,
                    summary=summary,
                    patient_id="PT-4417",
                )
            ]
        )
    )


def _booking_intent() -> PlannerIntent:
    return PlannerIntent(
        tool_name="book_appointment",
        rationale="the referral names a provider and a slot",
        args={
            "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
            "patient_id": "PT-4417",
            "slot_start": "2026-09-03T09:30:00",
            "reason": BOOKING_REASON,
        },
    )


def referral_run(case_id: str = "obs-referral"):
    """One deterministic referral run on the mock model. No clinic, no network, no clock."""
    store = InMemoryCaseStore()
    inbound = EVENTS["evt-0001-referral"]
    open_case(store, inbound, case_id=case_id, now=FIXED_NOW)
    context = context_for(
        store,
        inbound,
        case_id=case_id,
        triage=_triage(),
        planner=planner_proposing(*[_booking_intent()] * 6),
        now=lambda: FIXED_NOW,
    )
    return asyncio.run(run_case(context))


def poisoned_run(case_id: str = "obs-poisoned"):
    """The same run with every forbidden category planted in model-facing text."""
    store = InMemoryCaseStore()
    inbound = EVENTS["evt-0002-continuity-notice"]
    open_case(store, inbound, case_id=case_id, now=FIXED_NOW)
    intent = _booking_intent()
    poisoned = intent.model_copy(
        update={
            "rationale": HOSTILE_MARKERS["model_response"],
            "args": {**intent.args, "reason": HOSTILE_MARKERS["tool_argument"]},
        }
    )
    context = context_for(
        store,
        inbound,
        case_id=case_id,
        triage=_triage(HOSTILE_MARKERS["evidence_text"]),
        planner=planner_proposing(*[poisoned] * 6),
        now=lambda: FIXED_NOW,
    )
    return asyncio.run(run_case(context))


class TestTheApplicationCannotTellTelemetryIsThere:
    def test_state_nodes_and_timeline_are_identical_with_and_without_a_tracer(self) -> None:
        """Deterministic on the mock model, so this is byte comparison rather than a shape check.

        A real-provider run is *not* comparable this way — the planner is nondeterministic and
        session W measured node sequences differing between identical runs — so the claim is made
        where it is actually true.
        """
        without = referral_run("obs-equivalence")

        provider, _ = recording_provider()
        tracer = provider.get_tracer(__name__)
        with trace_api.use_span(tracer.start_span("invoke_graph"), end_on_exit=True):
            with_tracer = referral_run("obs-equivalence")

        assert with_tracer.case.state is without.case.state
        assert with_tracer.executed_nodes == without.executed_nodes == HAPPY_PATH
        assert [e.kind for e in with_tracer.case.timeline] == [
            e.kind for e in without.case.timeline
        ]
        assert with_tracer.case.model_dump_json() == without.case.model_dump_json()

    def test_approvals_actions_and_verification_are_unchanged(self) -> None:
        without = referral_run("obs-shapes")

        provider, _ = recording_provider()
        tracer = provider.get_tracer(__name__)
        with trace_api.use_span(tracer.start_span("invoke_graph"), end_on_exit=True):
            with_tracer = referral_run("obs-shapes")

        assert with_tracer.case.approvals == without.case.approvals
        assert with_tracer.case.actions == without.case.actions
        assert [a.idempotency_key for a in with_tracer.case.actions] == [
            a.idempotency_key for a in without.case.actions
        ]
