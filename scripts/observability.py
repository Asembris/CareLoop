"""The one thing CareLoop adds to its own telemetry, and it only ever removes.

**Deployment-layer, deliberately.** This module sits beside ``demo_serve.py`` rather than under
``careloop/`` because observability has no authority and must be structurally incapable of
acquiring any. Nothing here reads a ``Case``, a rule, a fact or an approval; nothing here is
imported by the package; and the single class below implements exactly one method that does
anything at all. ``scripts/`` is importable by the test suite by design — see
``scripts/__init__.py`` — which is what lets the guard be proven without shipping it in the
distribution.

**Why it exists.** ``aws-opentelemetry-distro`` patches botocore to attach the calling
credential's access key id to every AWS span::

    # amazon/opentelemetry/distro/patches/_botocore_patches.py
    credentials = instance._get_credentials()
    if credentials is not None:
        access_key = credentials.access_key
        if access_key is not None:
            attributes[AWS_AUTH_ACCESS_KEY] = access_key

Unconditionally: there is no environment variable, no configuration and no newer release that
turns it off — 0.19.0 is the newest, and every environment variable the distro reads was
enumerated before this module was written.

**What that identifier is, stated honestly, because the reason matters more than the reflex.** An
access key id is the *public* half of a credential pair; AWS prints example ids in its own
documentation and records the real one in CloudTrail for essentially every API call, and its own
guidance is that *"you must use both the access key ID and secret access key together to
authenticate your requests"*. On the deployed host it is a temporary ``ASIA…`` id from the
instance profile, rotated automatically and worthless once expired. Measured this session: the
only thing it discloses on its own is the **AWS account id**, which is recoverable offline from
the id itself and is already in plaintext in every log-group ARN a trace reader touches.

So this is not a vulnerability, and this module does not claim to close one. It is metadata
CareLoop has no use for, in a project that intends to *publish* its traces — the repository goes
public at submission and a screenshot of a span panel is the deliverable. Removing something
unused before it becomes published evidence costs ten lines. That is the whole argument.

**Narrow on purpose.** One attribute name, redacted by name. A value-shaped sweep over every
attribute was considered and rejected: it would make the regex assertion in
``tests/test_observability.py`` a restatement of this file rather than an independent tripwire.
The named guard is what holds today, and the test is what notices if the class ever widens.

The Application Signals metric path is covered for free and not by accident. ADOT's metric
generator derives ``aws.remote.resource.account.access_key`` by reading the *span* attribute at
export time, so by the time it looks, the value here is already ``[redacted]`` — a credential
identifier cannot become a metric dimension even if somebody turns Application Signals on. It is
pinned off as well; both, because one is a default somebody can change.
"""

from __future__ import annotations

import re
from typing import Any, Final

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider

#: What the access key id is replaced with. A fixed literal, not a hash and not a prefix: a
#: truncation still discloses the account id, which is the only thing the full value discloses.
REDACTED: Final[str] = "[redacted]"

#: The attribute ADOT attaches to every AWS span.
AWS_ACCESS_KEY_ATTRIBUTE: Final[str] = "aws.auth.account.access_key"

#: The shape of an AWS access key id — ``AKIA`` (long-lived IAM user) or ``ASIA`` (temporary STS,
#: which is what an EC2 instance profile yields) followed by sixteen base32 characters.
#:
#: **Not used by the processor.** It is here for the test that sweeps exported spans for a value of
#: this shape under *any* attribute name, which is how a second credential attribute in a future
#: ADOT release would be noticed rather than silently exported.
ACCESS_KEY_ID_SHAPE: Final[re.Pattern[str]] = re.compile(r"^(?:AKIA|ASIA)[A-Z0-9]{16}$")


class RedactAwsAccessKeyId(SpanProcessor):
    """Overwrites :data:`AWS_ACCESS_KEY_ATTRIBUTE` before a span can be exported.

    **Overwrite at ``on_start``, which is why no exporter is involved.** ADOT passes the attribute
    to ``start_as_current_span(attributes=…)``, so it is present the moment the span exists; the
    SDK then calls every registered processor's ``on_start`` while the span is still recording, and
    ``set_attribute`` replaces the value in place. Overwriting rather than deleting is not a
    preference — the OpenTelemetry API has no attribute removal — and it is precisely what keeps
    this out of the territory the design review ruled out: no custom exporter, no rebuilding of an
    immutable ``ReadableSpan``, no patching of ADOT, no vendored or forked instrumentation.

    Registration order does not matter. ``on_start`` runs for every processor when the span begins;
    export happens at ``on_end``, strictly later.

    Every other method is empty by contract, and that is the point: this class can read no domain
    state, reach no store, and change nothing about a span except the value of one attribute whose
    name is a constant in this file.
    """

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        """Redact the attribute if this span has it. Touches nothing else."""
        attributes = span.attributes or {}
        if AWS_ACCESS_KEY_ATTRIBUTE in attributes:
            span.set_attribute(AWS_ACCESS_KEY_ATTRIBUTE, REDACTED)

    def on_end(self, span: ReadableSpan) -> None:
        """Nothing. A span is immutable here, and there is nothing left to do."""

    def shutdown(self) -> None:
        """Nothing to release."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Nothing is buffered, so a flush is trivially complete."""
        return True


def install_telemetry_redaction(provider: object | None = None) -> bool:
    """Register :class:`RedactAwsAccessKeyId` on the global tracer provider.

    Returns whether it was installed. ``False`` is the ordinary answer when the process is **not**
    running under ``opentelemetry-instrument``: the global provider is then OpenTelemetry's
    no-op ``ProxyTracerProvider``, no span is ever recorded, and there is nothing to redact. That
    is the disabled mode the deployment falls back to, and it is a return value rather than an
    exception because an uninstrumented CareLoop is a supported configuration, not a fault.

    There is deliberately no ``try``/``except`` around the registration. The only way this can fail
    is the branch it already tests; wrapping it would convert a real bug in a future edit into a
    silent no-op, and a redaction that silently does not happen is worse than none, because the
    deployment would believe it was covered.

    Args:
        provider: the provider to register on. Defaults to the global one; supplied by tests so
            they never mutate global state.
    """
    target = provider if provider is not None else trace_api.get_tracer_provider()
    if not isinstance(target, TracerProvider):
        return False
    target.add_span_processor(RedactAwsAccessKeyId())
    return True


__all__ = [
    "ACCESS_KEY_ID_SHAPE",
    "AWS_ACCESS_KEY_ATTRIBUTE",
    "REDACTED",
    "RedactAwsAccessKeyId",
    "install_telemetry_redaction",
]
