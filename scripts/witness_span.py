"""The one place a CareLoop claim becomes an OpenTelemetry attribute.

**Deployment-layer, for the reason** ``scripts/observability.py`` **is.** It sits beside that module
rather than under ``careloop/`` because telemetry has no authority and must be structurally
incapable of acquiring any: nothing here reads a ``Case``, a rule, a fact or an approval, and
nothing under ``careloop/`` imports this file — ``tests/test_witness_seam.py`` sweeps the package
for exactly that. ``scripts/`` is importable by the test suite by design, which is what lets the
adapter be proven without shipping it in the distribution.

**What it does is a translation and nothing else.** :class:`careloop.graph.witness.Witness` is a
domain protocol carrying typed claims; a span carries dotted string keys. Every attribute *name*
CareLoop emits is spelled here and nowhere else, so the application owns the facts and the
deployment owns the encoding. That is why ``careloop/`` contains no string literal beginning
``careloop.effect`` or ``careloop.observation``.

**Nine attributes and there is no tenth.** ``careloop.effect.action_key`` was dropped during the
design and must not return under that or any other name. Correlation is carried by
:data:`ATTR_OBSERVATION_RESOURCE_ID` against the clinic read's own URL — evidence CareLoop does not
author — and by route, uniqueness and ordering for the write. An identifier CareLoop minted and then
checked against itself would prove nothing that was not already assumed.

**The span is resolved at emission time and never held.** Strands wraps the whole graph execution in
``trace_api.use_span(invoke_graph, end_on_exit=True)``, so ``get_current_span()`` inside a node body
*is* ``invoke_graph`` — measured from the other side by the Patch A capture, in which every child
span carries ``parentSpanId`` equal to it. An adapter that captured a span at construction would
have captured one from before any run existed, and one that went looking for a parentless span would
be wrong for a different reason: Transaction Search wraps ``invoke_graph`` in a synthetic
``InternalOperation``.

**There is no de-duplication here.** One trace carries one claim because the graph executes
``execute`` and ``verify`` at most once each and their effect claims are mutually exclusive, not
because this class remembers what it wrote. A cache would be adapter state standing in for a
structural property, with its own way of being wrong; ``tests/test_witness_claims.py`` and
``tests/test_witness_span.py`` hold the property instead.

**Failure is somebody else's problem, deliberately.** There is no ``try``/``except`` below. The
seam in :mod:`careloop.graph.witness` contains every telemetry failure for *every* implementation,
so catching here as well would put a second boundary in front of the first and hide a real bug in
this file from the only test that could see it. A non-recording span — which is what an
uninstrumented process yields — already accepts ``set_attribute`` as a no-op, so the ordinary
disabled case needs no handling at all.
"""

from __future__ import annotations

from typing import Final

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from careloop.graph.witness import NO_WITNESS, EffectClaim, ObservationClaim, Witness

ATTR_EFFECT_TYPE: Final[str] = "careloop.effect.type"
ATTR_EFFECT_DISPATCH: Final[str] = "careloop.effect.dispatch"
ATTR_EFFECT_FIELDS: Final[str] = "careloop.effect.fields"
ATTR_EFFECT_STATE_DIGEST: Final[str] = "careloop.effect.state_digest"
ATTR_OBSERVATION_MODE: Final[str] = "careloop.observation.mode"
ATTR_OBSERVATION_OUTCOME: Final[str] = "careloop.observation.outcome"
ATTR_OBSERVATION_RESOURCE_ID: Final[str] = "careloop.observation.resource_id"
ATTR_OBSERVATION_STATE_DIGEST: Final[str] = "careloop.observation.state_digest"
ATTR_SETTLEMENT: Final[str] = "careloop.settlement"

#: The complete telemetry contract. Nine names, and a test asserts the emitted set equals this.
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

#: The prefix everything CareLoop emits shares, so a test can sweep for a tenth attribute by shape
#: rather than by knowing what it would be called.
CLAIM_NAMESPACE: Final[str] = "careloop."


class SpanAttributeWitness:
    """Writes a claim onto whichever span the graph is currently executing inside.

    Stateless: no span, no counter, no configuration. Two instances are interchangeable and one may
    be shared by every session in the process, which is what the composition root does.

    Enum members are written as their ``.value`` rather than relying on ``str()`` of a ``StrEnum``.
    The two agree today; naming the value is the assertion that what reaches a trace is the
    vocabulary and not a repr somebody could change.

    The two optional attributes are omitted rather than emitted empty. An absent
    ``careloop.observation.state_digest`` says *no external state was observed*, which is true;
    an empty string would be a malformed digest, and a reader would have to guess which was meant.
    """

    def record_effect(self, claim: EffectClaim) -> None:
        span = trace_api.get_current_span()
        span.set_attribute(ATTR_EFFECT_TYPE, claim.effect_type)
        span.set_attribute(ATTR_EFFECT_DISPATCH, claim.dispatch.value)
        span.set_attribute(ATTR_EFFECT_FIELDS, claim.fields)
        span.set_attribute(ATTR_EFFECT_STATE_DIGEST, claim.state_digest)

    def record_observation(self, claim: ObservationClaim) -> None:
        span = trace_api.get_current_span()
        span.set_attribute(ATTR_OBSERVATION_MODE, claim.mode.value)
        span.set_attribute(ATTR_OBSERVATION_OUTCOME, claim.outcome.value)
        span.set_attribute(ATTR_SETTLEMENT, claim.settlement.value)
        if claim.resource_id is not None:
            span.set_attribute(ATTR_OBSERVATION_RESOURCE_ID, claim.resource_id)
        if claim.state_digest is not None:
            span.set_attribute(ATTR_OBSERVATION_STATE_DIGEST, claim.state_digest)


def span_witness(provider: object | None = None) -> Witness:
    """A span-writing witness when this process records spans, and otherwise none at all.

    **Deliberately not** ``install_telemetry_redaction()``'s return value, which was the obvious
    thing to reuse and is the wrong thing. That function answers *"was the redaction processor
    registered?"* — it has a side effect, and its boolean carries two facts at once, so a future
    change to how redaction is installed would silently decide whether CareLoop emits settlement
    evidence. Two unrelated features would then share one switch, which is the shape of coupling
    this repository keeps finding and removing. This asks its own question and changes nothing.

    The question is the smallest true one: **is the global tracer provider a real SDK provider?**
    Without ``opentelemetry-instrument`` it is OpenTelemetry's no-op ``ProxyTracerProvider``, no
    span is ever recorded, and a witness would spend two SHA-256 digests per run writing into a
    void. With one, ``get_current_span()`` inside a graph run resolves to ``invoke_graph``.

    This is a **composition-time** decision and the only one of its kind. Once a run holds a
    :class:`~careloop.graph.witness.Witness`, nothing branches on which one it got — see
    ``tests/test_witness_seam.py::TestNothingBranchesOnAWitness``. Returning
    :data:`~careloop.graph.witness.NO_WITNESS` here is an optimisation, not a safety mechanism:
    :class:`SpanAttributeWitness` against a no-op provider is already harmless.

    Args:
        provider: the provider to inspect. Defaults to the global one; supplied by tests so they
            never depend on process-wide state.
    """
    target = provider if provider is not None else trace_api.get_tracer_provider()
    if not isinstance(target, TracerProvider):
        return NO_WITNESS
    return SpanAttributeWitness()


__all__ = [
    "ATTR_EFFECT_DISPATCH",
    "ATTR_EFFECT_FIELDS",
    "ATTR_EFFECT_STATE_DIGEST",
    "ATTR_EFFECT_TYPE",
    "ATTR_OBSERVATION_MODE",
    "ATTR_OBSERVATION_OUTCOME",
    "ATTR_OBSERVATION_RESOURCE_ID",
    "ATTR_OBSERVATION_STATE_DIGEST",
    "ATTR_SETTLEMENT",
    "CLAIM_ATTRIBUTES",
    "CLAIM_NAMESPACE",
    "SpanAttributeWitness",
    "span_witness",
]
