"""Evaluate one real CareLoop production trace through Bedrock AgentCore, and record what happened.

    # what traces exist, and what shape each is
    python scripts/evaluations_prove.py --list --window-minutes 30

    # the decisive form: find the trace by the clinic's own resource id, then judge it
    python scripts/evaluations_prove.py --find apt-0123456789abcdef \\
        --evaluator-id CareLoopSettlementIntegrity-xxxxxxxxxx \\
        --scenario "performed" --expect PASS --out proof-performed.json

**The honest shape of the on-demand path, which is not what the documentation suggests.** The
installed service model requires ``evaluationInput.sessionSpans``: AgentCore does **not** fetch the
trace from CloudWatch when ``Evaluate`` is called on demand. So this script reads the real exported
spans out of the ``aws/spans`` log group and hands them over verbatim. The defensible claim is
*AgentCore evaluated real production spans supplied by the operator*, never *AgentCore fetched the
trace itself* -- that is online-evaluation behaviour and is deliberately not built.

Because the operator supplies the payload, the operator has to be auditable. Two things make that
true: this script **never modifies a span** -- there is no repair, no defaulting, no synthesis of a
``careloop.*`` attribute, no normalisation of a status spelling and no invented timestamp -- and it
records the SHA-256 of the exact canonical payload it sent.

**It reads spans with its own accessors, on purpose.** Importing ``evaluators.spans`` would make
the transport cross-check agree with the evaluator by construction: a defect in an accessor would
move both sides together and the "independent" assertion would prove nothing. So the twenty lines
below read literal dotted key names, and the two implementations are only ever compared through
their answers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

REGION: Final[str] = "us-east-1"

#: Where Transaction Search writes exported spans. Read-only, always.
SPANS_LOG_GROUP: Final[str] = "aws/spans"

#: How long Transaction Search may take to index a span before this script calls it absent.
INDEXING_GRACE_SECONDS: Final[float] = 180.0
POLL_SECONDS: Final[float] = 15.0

CLAIM_PREFIX: Final[str] = "careloop."
ATTR_EFFECT_TYPE: Final[str] = "careloop.effect.type"
ATTR_OBSERVATION_RESOURCE_ID: Final[str] = "careloop.observation.resource_id"

CLINIC_PATH: Final[tuple[str, ...]] = ("clinic", "appointments")


class ProofRefused(Exception):
    """The evidence is not in a shape this script is willing to draw a conclusion from."""


# ---------------------------------------------------------------------------------------------
# Reading a span. Deliberately not evaluators.spans -- see the module docstring.
# ---------------------------------------------------------------------------------------------


def attribute(span: Mapping[str, Any], name: str) -> Any:
    """One attribute by its exact dotted name: the ``attributes`` map first, then flattened."""
    attributes = span.get("attributes")
    if isinstance(attributes, Mapping) and name in attributes:
        return attributes[name]
    return span.get(name)


def trace_of(span: Mapping[str, Any]) -> str | None:
    for key in ("traceId", "trace_id"):
        found = span.get(key)
        if isinstance(found, str) and found:
            return found
    return None


def span_id_of(span: Mapping[str, Any]) -> str | None:
    for key in ("spanId", "span_id"):
        found = span.get(key)
        if isinstance(found, str) and found:
            return found
    return None


def scope_of(span: Mapping[str, Any]) -> str:
    scope = span.get("scope")
    if isinstance(scope, Mapping):
        name = scope.get("name")
        if isinstance(name, str):
            return name
    name = span.get("scope.name")
    return name if isinstance(name, str) else "<none>"


def path_segments(span: Mapping[str, Any]) -> tuple[str, ...]:
    url = attribute(span, "http.url")
    if not isinstance(url, str):
        return ()
    trimmed = urlsplit(url).path.strip("/")
    return tuple(trimmed.split("/")) if trimmed else ()


def otel_status(span: Mapping[str, Any]) -> Any:
    """The OpenTelemetry status code, from the nested object or the flat attribute.

    Production spans carry ``{"status": {"code": "UNSET"}}``; reading only the flat spelling would
    report ``null`` for a span that plainly says ``UNSET``, and ``UNSET`` is the whole of Patch A's
    finding that success is *not* ``OK``. An evidence table that misdescribes it is worse than one
    that omits it.
    """
    status = span.get("status")
    if isinstance(status, Mapping) and "code" in status:
        return status["code"]
    return attribute(span, "status.code")


def status_of(span: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """The two HTTP status spellings and the OTel status code, unresolved and unreconciled."""
    return (
        attribute(span, "http.status_code"),
        attribute(span, "http.response.status_code"),
        otel_status(span),
    )


def claim_attribute_names(span: Mapping[str, Any]) -> tuple[str, ...]:
    """Every attribute name on this span beginning ``careloop.``, sorted. Names, never values."""
    attributes = span.get("attributes")
    names = set(attributes) if isinstance(attributes, Mapping) else set()
    names |= {key for key in span if isinstance(key, str)}
    return tuple(sorted(name for name in names if name.startswith(CLAIM_PREFIX)))


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------


def _logs_client() -> Any:
    import boto3

    return boto3.session.Session().client("logs", region_name=REGION)


def _spans_from_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Decode log events into span mappings, discarding anything that is not one."""
    spans: list[dict[str, Any]] = []
    for event in events:
        message = event.get("message")
        if not isinstance(message, str):
            continue
        try:
            decoded = json.loads(message)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, Mapping) and trace_of(decoded) is not None:
            spans.append(dict(decoded))
    return spans


def filter_spans(logs: Any, *, literal: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Every span in the window whose raw record contains ``literal``, following the token."""
    collected: list[Mapping[str, Any]] = []
    token: str | None = None
    while True:
        arguments: dict[str, Any] = {
            "logGroupName": SPANS_LOG_GROUP,
            "startTime": start_ms,
            "endTime": end_ms,
            "filterPattern": f'"{literal}"',
        }
        if token:
            arguments["nextToken"] = token
        page = logs.filter_log_events(**arguments)
        collected.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break
    return _spans_from_events(collected)


def deduplicate(spans: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One span per span id, first occurrence wins, order preserved.

    A span with no readable id is kept rather than dropped: silently discarding an unattributable
    span is how a clinic write disappears and a ``FAIL`` becomes a ``PASS``.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for span in spans:
        identifier = span_id_of(span)
        if identifier is not None:
            if identifier in seen:
                continue
            seen.add(identifier)
        unique.append(dict(span))
    return unique


def resolve_trace(logs: Any, *, literal: str, start_ms: int, end_ms: int) -> str:
    """The one trace whose spans mention ``literal``.

    Raises:
        ProofRefused: if no trace mentions it, or if more than one does. Choosing between two
            candidate traces is deciding the thing this evidence is supposed to establish.
    """
    deadline = time.monotonic() + INDEXING_GRACE_SECONDS
    while True:
        found = filter_spans(logs, literal=literal, start_ms=start_ms, end_ms=end_ms)
        traces = sorted({trace_of(span) for span in found} - {None})
        if len(traces) == 1:
            return str(traces[0])
        if len(traces) > 1:
            raise ProofRefused(f"{literal!r} appears in {len(traces)} traces: {traces}")
        if time.monotonic() >= deadline:
            raise ProofRefused(
                f"no span mentioning {literal!r} appeared in {SPANS_LOG_GROUP} within "
                f"{INDEXING_GRACE_SECONDS:.0f}s"
            )
        time.sleep(POLL_SECONDS)


def collect_trace(logs: Any, *, trace_id: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Every span of one trace, verbatim, deduplicated by span id."""
    spans = filter_spans(logs, literal=trace_id, start_ms=start_ms, end_ms=end_ms)
    belonging = [span for span in spans if trace_of(span) == trace_id]
    if not belonging:
        raise ProofRefused(f"no spans found for trace {trace_id}")
    return deduplicate(belonging)


# ---------------------------------------------------------------------------------------------
# Transport assertions -- computed here, before AWS is asked anything
# ---------------------------------------------------------------------------------------------


def transport_facts(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """What the httpx spans say, independently of what CareLoop claims about them."""
    writes = []
    reads = []
    for span in spans:
        method = attribute(span, "http.method")
        segments = path_segments(span)
        if method == "POST" and segments == CLINIC_PATH:
            writes.append(span)
        elif (
            method == "GET"
            and len(segments) == len(CLINIC_PATH) + 1
            and segments[: len(CLINIC_PATH)] == CLINIC_PATH
        ):
            reads.append(span)

    claims = [span for span in spans if attribute(span, ATTR_EFFECT_TYPE) is not None]
    claim_names: tuple[str, ...] = ()
    claimed_resource = None
    if len(claims) == 1:
        claim_names = claim_attribute_names(claims[0])
        claimed_resource = attribute(claims[0], ATTR_OBSERVATION_RESOURCE_ID)

    scopes: dict[str, int] = {}
    for span in spans:
        scopes[scope_of(span)] = scopes.get(scope_of(span), 0) + 1

    return {
        "span_count": len(spans),
        "scope_counts": dict(sorted(scopes.items())),
        "claim_span_count": len(claims),
        "claim_attribute_names": list(claim_names),
        "claim_attribute_count": len(claim_names),
        "clinic_write_count": len(writes),
        "clinic_read_count": len(reads),
        "writes": [{"status": status_of(span), "scope": scope_of(span)} for span in writes],
        "reads": [
            {
                "status": status_of(span),
                "scope": scope_of(span),
                "resource_matches_claim": path_segments(span)[-1] == claimed_resource,
            }
            for span in reads
        ],
    }


# ---------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------


def canonical_payload(spans: list[dict[str, Any]]) -> bytes:
    """The exact bytes whose digest is recorded. Sorted keys, no whitespace, UTF-8."""
    return json.dumps(spans, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def evaluate(evaluator_id: str, trace_id: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """One on-demand ``Evaluate`` call, with the spans passed through untouched."""
    import boto3

    client = boto3.session.Session().client("bedrock-agentcore", region_name=REGION)
    response = client.evaluate(
        evaluatorId=evaluator_id,
        evaluationInput={"sessionSpans": spans},
        evaluationTarget={"traceIds": [trace_id]},
    )
    results = response.get("evaluationResults") or []
    if len(results) != 1:
        raise ProofRefused(f"expected one evaluation result, got {len(results)}")
    result = results[0]
    return {
        "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
        "label": result.get("label"),
        "value_present": "value" in result,
        "value": result.get("value"),
        "explanation": result.get("explanation"),
        "error_code": result.get("errorCode"),
        "error_message": result.get("errorMessage"),
        "evaluator_id": result.get("evaluatorId"),
        "evaluator_arn": result.get("evaluatorArn"),
    }


def check_expectation(expected: str | None, outcome: dict[str, Any]) -> str:
    """Compare the recorded result against what the scenario said it would be."""
    if expected is None:
        return "not asserted"
    label = outcome.get("label")
    explanation = outcome.get("explanation") or ""
    if expected == "PASS":
        return "met" if label == "PASS" and outcome.get("value") == 1.0 else "NOT MET"
    if expected == "NOT_APPLICABLE":
        met = label == "NOT_APPLICABLE" and not outcome.get("value_present")
        return "met" if met else "NOT MET"
    if expected.startswith("FAIL:"):
        wanted = expected.split(":", 1)[1]
        met = label == "FAIL" and outcome.get("value") == 0.0 and explanation.startswith(wanted)
        return "met" if met else "NOT MET"
    return "NOT MET"


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


def summarise(logs: Any, *, start_ms: int, end_ms: int) -> None:
    """List the distinct traces in the window and the shape of each. Discovery aid only."""
    spans = filter_spans(logs, literal="traceId", start_ms=start_ms, end_ms=end_ms)
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        by_trace.setdefault(str(trace_of(span)), []).append(span)
    print(f"{len(by_trace)} trace(s) in window")
    for trace_id, group in sorted(by_trace.items()):
        facts = transport_facts(deduplicate(group))
        print(
            f"  {trace_id}  spans={facts['span_count']:<3} "
            f"claims={facts['claim_span_count']} "
            f"careloop_attrs={facts['claim_attribute_count']} "
            f"POST={facts['clinic_write_count']} GET={facts['clinic_read_count']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate one real CareLoop trace.")
    parser.add_argument("--find", help="literal that appears only in the target trace's spans")
    parser.add_argument("--trace-id", help="skip discovery and use this trace")
    parser.add_argument("--evaluator-id", help="the AgentCore evaluator to invoke")
    parser.add_argument("--scenario", default="unnamed", help="label for the evidence record")
    parser.add_argument("--expect", help="PASS | NOT_APPLICABLE | FAIL:<CHECK_ID>")
    parser.add_argument("--window-minutes", type=float, default=60.0)
    parser.add_argument(
        "--since",
        help=(
            "ISO-8601 UTC instant to search from. CareLoop's idempotency keys are deterministic, "
            "so one scenario run always names the same clinic resource: bounding the search by "
            "the window of a single run is what makes discovery identify a run rather than a shape"
        ),
    )
    parser.add_argument("--until", help="ISO-8601 UTC instant to search to; defaults to now")
    parser.add_argument("--list", action="store_true", help="summarise the window and stop")
    parser.add_argument("--no-evaluate", action="store_true", help="discovery and transport only")
    parser.add_argument("--out", help="write the evidence record here as JSON")
    arguments = parser.parse_args()

    logs = _logs_client()
    now = datetime.now(UTC)
    if arguments.until:
        end_ms = int(datetime.fromisoformat(arguments.until).timestamp() * 1000)
    else:
        end_ms = int(now.timestamp() * 1000) + 60_000
    if arguments.since:
        start_ms = int(datetime.fromisoformat(arguments.since).timestamp() * 1000)
    else:
        start_ms = end_ms - int(arguments.window_minutes * 60_000)

    try:
        if arguments.list:
            summarise(logs, start_ms=start_ms, end_ms=end_ms)
            return 0

        trace_id = arguments.trace_id
        if trace_id is None:
            if not arguments.find:
                raise ProofRefused("one of --trace-id, --find or --list is required")
            trace_id = resolve_trace(logs, literal=arguments.find, start_ms=start_ms, end_ms=end_ms)
        spans = collect_trace(logs, trace_id=trace_id, start_ms=start_ms, end_ms=end_ms)
        facts = transport_facts(spans)
        payload = canonical_payload(spans)

        record: dict[str, Any] = {
            "scenario": arguments.scenario,
            "captured_at": now.isoformat(),
            "search_window": {
                "start": datetime.fromtimestamp(start_ms / 1000, UTC).isoformat(),
                "end": datetime.fromtimestamp(end_ms / 1000, UTC).isoformat(),
            },
            "trace_id": trace_id,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_bytes": len(payload),
            "transport": facts,
            "expected": arguments.expect,
        }

        print(f"scenario     {arguments.scenario}")
        print(f"trace        {trace_id}")
        print(f"spans        {facts['span_count']}  scopes {facts['scope_counts']}")
        print(
            f"transport    POST={facts['clinic_write_count']} GET={facts['clinic_read_count']}  "
            f"claim spans={facts['claim_span_count']} "
            f"careloop attrs={facts['claim_attribute_count']}"
        )
        print(f"payload      {len(payload)} bytes, sha256 {record['payload_sha256']}")

        if not arguments.no_evaluate:
            if not arguments.evaluator_id:
                raise ProofRefused("--evaluator-id is required unless --no-evaluate is given")
            outcome = evaluate(arguments.evaluator_id, trace_id, spans)
            record["result"] = outcome
            record["expectation"] = check_expectation(arguments.expect, outcome)
            print(
                f"result       {outcome['label']}  value_present={outcome['value_present']}  "
                f"value={outcome['value']}"
            )
            print(f"explanation  {outcome['explanation']}")
            print(f"expectation  {record['expectation']}")

        if arguments.out:
            Path(arguments.out).write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(f"written      {arguments.out}")
    except ProofRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
