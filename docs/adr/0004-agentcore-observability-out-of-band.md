# ADR 0004 — AgentCore Observability, out of band

**Status:** Accepted (AgentCore **Observability** on the existing EC2 host; AgentCore **Runtime**
remains rejected — [ADR 0002](0002-graph-execution-stays-on-the-host.md) is unchanged)
**Date:** 2026-08-20
**Pinned against:** `strands-agents==1.51.0`, `aws-opentelemetry-distro==0.19.0`,
`opentelemetry-{api,sdk}==1.44.0`, `opentelemetry-instrumentation==0.65b0`, Python 3.12, `us-east-1`
**Evidence:** measured in this session against real Bedrock and a loopback Demo World; see
*The measured spike* below
**Seam:** [`scripts/observability.py`](../../scripts/observability.py),
[`deploy/careloop.env`](../../deploy/careloop.env),
[`deploy/careloop-api.service`](../../deploy/careloop-api.service),
[`tests/test_observability.py`](../../tests/test_observability.py)

## The distinction this ADR exists to make

ADR 0002 rejected moving graph execution to AgentCore Runtime, and **nothing here reopens it.** Its
deciding criterion is untouched: every graph node commits its transition before it finishes, which
is what lets a browser polling `GET /api/cases` watch a case move, and a runtime that commits once
at the end removes that. The graph still runs in the API process.

Observability is a different question with a different answer because it has a different shape.
Runtime migration moves *where authority executes*. Observability adds a **one-way outward
projection** of what already happened:

```
CareLoop execution ──telemetry only──▶ OpenTelemetry / ADOT ──▶ CloudWatch / AgentCore
```

and never

```
telemetry ──▶ CareLoop authority
```

`CaseStore` and the append-only timeline remain the domain truth. **CloudWatch is an operational
projection and is authoritative for nothing.** Nothing in the telemetry path can alter a `Case`,
route the graph, decide policy, resolve trust, raise or decide an approval, change an idempotency
key, dispatch a tool, influence verification, alter a prompt or become a source of domain fact —
and the reason is structural rather than promised: the only CareLoop-authored code in the whole
integration is one `SpanProcessor` that overwrites one attribute, it lives in `scripts/` rather
than in the package, and nothing under `careloop/` imports it.

## The measured spike

Run before any of this was written, under `opentelemetry-instrument` with the ADOT distro, against
real Bedrock (Nova Micro + Nova 2 Lite) and a real Demo World on a loopback socket.

**What Strands emits on its own is almost nothing.** A complete seven-node run — triage, evidence,
two planner calls, a policy denial, a replan, a handoff — produced **exactly one span**:

```
invoke_graph  kind=CLIENT  root
  gen_ai.operation.name = invoke_graph
  gen_ai.system         = strands-agents
  event gen_ai.user.message  content = "run case <case_id>"
```

CareLoop reaches `Model.stream` and `Model.structured_output` directly and constructs no
`strands.Agent`, so the SDK's model spans, tool spans, event-loop spans and every one of its
metrics — all of which are emitted by the agent event loop — are never reached. Node-level spans do
not exist in this version at all: `Graph._execute_node` creates none.

**What makes it worth doing is ADOT, underneath Strands.** `opentelemetry-instrumentation-botocore`
instruments `converse_stream`, which is what `BedrockModel.stream` calls, and
`opentelemetry-instrumentation-httpx` instruments the clinic transport. Measured trace:

```
invoke_graph                                             3062 ms   root
├── chat us.amazon.nova-micro-v1:0                       1198 ms   in 1154 / out 42 tokens
├── chat us.amazon.nova-2-lite-v1:0                      1842 ms   in 2758 / out 163 tokens
├── POST /clinic/appointments                               5 ms   201
└── GET  /clinic/appointments/apt-7887ff189a23e777          3 ms   200
```

The last two lines are the reason this is worth having: the booking and the **independent
read-back** appear as two separate calls to the same external service, in AWS's own tooling, by a
system that cannot fake it. The poisoned-only scenario showed six `chat` spans with input tokens
climbing 2696 → 2772 as refusals accumulated in the planner prompt, zero actions executed, case
`AWAITING_HUMAN` — the replan cycle and its cost, visible with no custom instrumentation.

**Latency was not distinguishable from Bedrock's own variance** (individual model calls varied
1.0–2.2 s; per-scenario deltas were +23 to +259 ms at n=1, and one scenario was *faster* with
telemetry on because the planner took a different path). Final state was identical in all five
scenarios. **Startup is the real, reproducible cost: ≈ +1139 ms** (1772 → 2911 ms median, in-process
markers), on x86. That number is not evidence about ARM and is not treated as any.

## ADOT 0.19.0, and why not upstream OpenTelemetry

`aws-opentelemetry-distro==0.19.0` is the newest release (2026-07-23) and pins
`opentelemetry-{api,sdk,exporter-otlp-proto-http}==1.44.0` and
`opentelemetry-instrumentation==0.65b0` — **exactly the versions `strands-agents==1.51.0` already
resolves**. Measured on a clean install: **64 packages added, zero existing versions moved.**

Upstream OpenTelemetry produced the same *local* span set without the access-key attribute, which
looked like a free win and is not one. ADOT supplies two things upstream has no equivalent for:
**SigV4-signed OTLP** (`OTLPAwsSpanExporter` + `aws_sigv4_session_factory`, signing `service="xray"`
against `https://xray.<region>.amazonaws.com/v1/traces`) and **X-Ray-compatible trace ids**
(`AwsXRayIdGenerator`; X-Ray trace ids embed an epoch, upstream's are random). AWS also states
plainly that *"the ADOT Collector is not supported for agent observability"* — outside AgentCore
Runtime it is the ADOT SDK or the Lambda layer, and nothing else. So upstream would export nothing
to AWS, populate no Transaction Search and appear nowhere in GenAI Observability. **Local span
equivalence was the wrong test.**

Both distros register competing entry points, and with neither variable set the loader picks one
silently — measured, when a run believed to be upstream was in fact ADOT. `OTEL_PYTHON_DISTRO` and
`OTEL_PYTHON_CONFIGURATOR` are therefore pinned explicitly rather than left to auto-selection.

## The access-key-id finding, and why it is redacted rather than accepted

ADOT's botocore patch attaches the calling credential's access key id to **every** AWS span:

```python
credentials = instance._get_credentials()
if credentials is not None:
    access_key = credentials.access_key
    if access_key is not None:
        attributes[AWS_AUTH_ACCESS_KEY] = access_key
```

Unconditionally. Every environment variable the distro reads was enumerated: **there is no switch**,
and 0.19.0 is the newest release. The attribute is ADOT's own — upstream does not emit it.

**What it is, stated accurately rather than by reflex.** An access key id is the *public* half of a
credential pair. AWS prints example ids in its own documentation, records the real one in CloudTrail
for essentially every API call, and states that *"you must use both the access key ID and secret
access key together to authenticate your requests."* The secret access key and the session token are
**never** read by ADOT. On the deployed host the value is a temporary `ASIA…` id from the instance
profile — verified this session by taking real STS credentials and observing the `ASIA` prefix, a
session token and an expiry — rotated automatically and worthless once expired.

Measured: the **only** thing the id discloses on its own is the **AWS account id**, recoverable
offline from the id itself (decoded and matched against `sts:GetCallerIdentity` exactly). Anyone who
can read these traces is already inside the account, where every log-group ARN carries the account
id in plaintext, so the marginal disclosure to a legitimate reader is **zero**. The residual risk is
therefore **LOW**, and confined to telemetry leaving the account.

**So this is not a vulnerability and the redaction does not claim to close one.** It is metadata
CareLoop has no use for, in a project that will *publish* its traces — the repository goes public at
submission and a screenshot of a span panel is the deliverable. Removing it costs ten lines of stock
OpenTelemetry API. Accepting it would also have been defensible; doing neither would not.

`scripts/observability.py` registers one `SpanProcessor` whose `on_start` overwrites the attribute
with `[redacted]`. ADOT passes the attribute to `start_as_current_span(attributes=…)`, so it exists
the moment the span does and the SDK calls every processor's `on_start` while the span is still
recording — which is what makes an exporter unnecessary. **No custom exporter, no rebuilding of an
immutable `ReadableSpan`, no monkey-patching, no vendoring, no fork, no edit to an installed
package, and nothing inside `careloop/`.** Overwriting rather than deleting is not a preference: the
OpenTelemetry API has no attribute removal.

The guard is narrow on purpose — one attribute, by name. A value-shaped sweep across all attributes
was considered and rejected because it would turn the regex assertion in `tests/test_observability.py`
into a restatement of the processor rather than an independent tripwire for a second credential
attribute in a future release.

## Application Signals is off, twice

ADOT's metric generator copies the access key id into `aws.remote.resource.account.access_key` — a
credential identifier as a **metric dimension**. It is inert: `_is_application_signals_enabled()`
defaults to `false`, and — checked, because it would have been an easy trap —
`AGENT_OBSERVABILITY_ENABLED` and Application Signals are independent flags with no code path
linking them. The spike's captured spans carried no `aws.remote.*` attribute at all.

It is nevertheless pinned `OTEL_AWS_APPLICATION_SIGNALS_ENABLED=false`, because a default is
something a dependency bump can change silently. And the redaction covers the path anyway: the
generator reads the *span* attribute at export time, so by the time it looks the value is already
`[redacted]`. **Two independent reasons a credential identifier cannot become a metric dimension**,
which is the right number for a property stated this strongly. `case_id` and `run_id` are subject to
the same rule: span attributes only, never dimensions.

## The privacy boundary

Nothing exported carries a prompt, a model response, a system prompt, evidence or document text, a
patient identifier, a caregiver identity, an email address, a tool argument, a tool result, a
session token, a credential, or arbitrary external response text. Two settings are pinned rather
than inherited, because both libraries default the *unsafe* way in at least one direction:

- `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` — the botocore GenAI path reads this
  with a `"false"` default; it is written down because this is the line that must never change.
- `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes=` — **Strands' own redaction is
  opt-in and off by default.** Absent this token, `gen_ai.input.messages`, `gen_ai.output.messages`
  and `gen_ai.system_instructions` are emitted unredacted. Nothing today constructs a
  `strands.Agent`, so nothing today fills them; this is what stops the day one does from being a
  content leak.

**That second line has a real cost and it is accepted rather than hidden**: an empty allowlist
redacts everything sensitive, including the graph span's one event, whose content is
`run case <case_id>`. Traces are therefore correlated by trace id rather than by case id. The
alternative — allowlisting `gen_ai.input.messages` — would unredact *all* input messages, not just
that one, which is not a trade worth making for a correlation the trace id already provides.

**The external-response-body leak this would otherwise have carried is already closed.** The spike
found it: a clinic refusal built its exception message by interpolating the response body, and
`Graph.stream_async` runs under `use_span(..., end_on_exit=True)`, whose default
`record_exception=True` copies `str(exception)` into an OpenTelemetry `exception.message`. Fixed at
the source in commit `f89d16e` — `ClinicRefused` now carries only a CareLoop-owned operation name
and an HTTP status — with `tests/test_exception_privacy.py` holding it, including an OTel span-level
regression. That fix was a prerequisite for this ADR, not a consequence of it.

## FastAPI instrumentation is disabled, deliberately

`RunRegistry.start` schedules a run on an `asyncio` task that inherits the request's OTel context,
and the request returns `202` immediately while the run continues for seconds. Instrumented, the
graph span would be parented to a **server span that has already ended** — a ~20 ms parent over a
multi-second child, a waterfall that misleads rather than informs.

`OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` therefore excludes `fastapi` and `starlette`, leaving
`invoke_graph` as the trace root, which is the honest shape. The alternative — changing
`RunRegistry` or its context propagation to produce a prettier trace — is application complexity
bought with nothing, and it is exactly the kind of change this integration is not allowed to make.
`sqlite3`, `system_metrics` and `logging` are excluded too, each for its own stated reason in
`careloop.env`.

**Demo World is not instrumented and must not be.** It is the external system the read-back
independently observes; putting it in CareLoop's telemetry pipeline would couple the observer to the
observed, which is the same category of mistake as verifying a booking by re-reading its own receipt.

## No custom node spans

None, and not because they were forgotten. Strands 1.51.0 does expose `BeforeNodeCallEvent`,
`AfterNodeCallEvent` and the multi-agent invocation events, and they would work. `BeforeNodeCallEvent`
is `_Interruptible` — it carries the capability to cancel node execution — and subscribing an
observability component to an interruptible event on the authority plane's own bus makes "telemetry
has no authority" a matter of discipline rather than of construction. If node-level semantics are
ever justified, the seam is CareLoop's own `DeterministicNode.invoke_async`, which wraps rather than
decides. That is a separate decision to be taken **after** looking at a real CloudWatch trace, not
before.

## Retention, IAM and cost

**14 days, set explicitly.** `/aws/bedrock-agentcore/runtimes/*` log groups default to **NEVER
EXPIRE** and every runtime creates its own — a trap already recorded in `CLAUDE.md` about AgentCore
Runtime that applies verbatim here, because this plan creates a log group under exactly that prefix
by hand. `logs:PutRetentionPolicy` is part of deploying, not of tidying up.

Least privilege, on the **existing EC2 instance profile** — no static AWS credentials on the
instance, which is what keeps the `ASIA…` shape true:

| Action | Resource | Why |
|---|---|---|
| `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:PutRetentionPolicy` | `arn:aws:logs:us-east-1:<acct>:log-group:/aws/bedrock-agentcore/runtimes/careloop*` | log-group scoped |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords`, `xray:GetSamplingRules`, `xray:GetSamplingTargets` | `*` | **unavoidable** — X-Ray does not support resource-level scoping on these |
| `cloudwatch:PutMetricData` | `*`, condition `cloudwatch:namespace = bedrock-agentcore` | CloudWatch metrics have no ARN; the namespace condition is the available scoping |

The four `xray:*` wildcards are an IAM service limitation, not laziness, and are the same set AWS
publishes for this path. `bedrock:InvokeModel` is unchanged.

**Cost is negligible**: roughly six spans per run, one ~20-character attribute on AWS spans, no
metric dimensions, no cardinality effect. The real cost risk is an unbounded-retention log group,
which the retention policy closes. Sampling is 100% head, no tail sampling and no custom sampler:
demo traffic is single-digit runs per minute, and a sampled-away trace is the one a judge just
clicked.

## Fail-open, and the one place that is not automatic

Export is asynchronous. `BatchSpanProcessor` enqueues and returns; no request handler awaits
delivery. CloudWatch unavailable, OTLP endpoint unreachable, exporter raising, queue full, or
credentials lacking telemetry permission all leave a run reaching its normal outcome — asserted in
`tests/test_observability.py` against an exporter that raises on every call and one that drops
everything, including a `BatchSpanProcessor` configuration where the run completes with **zero**
export attempts made.

**Startup is the exception.** `opentelemetry-instrument` wraps `ExecStart`, so the launcher is a
*boot* dependency: if it is missing or throws during configuration, the service does not start, and
`Restart=always` turns that into a crash loop. Three things cover it: `bootstrap.sh` fails loudly
with the reason if `opentelemetry-instrument` is not installed, rather than leaving systemd to
report a missing executable; the uninstrumented `ExecStart` is kept in the unit, commented, as a
one-line rollback; and **a cold boot of the instrumented unit must be observed before this is
treated as safe.** That is a deployment gate, and it has not been run.

## The performance gate — not yet passed

The x86 spike number (**+1139 ms startup**) is not evidence about a burstable ARM `t4g.small` with
2 vCPU, and is not treated as any. Before this integration is called complete, the deployed instance
must be measured, observability OFF versus ON, interleaved repeated runs, over: a successful
referral; a human handoff; a policy denial and replan; and an external booking with its independent
read-back. Measuring median and p95 end-to-end latency, process startup, RSS, CPU, Bedrock span
latency and trace completeness.

| Metric | Maximum acceptable |
|---|---|
| median end-to-end | +5% or +100 ms, whichever is smaller |
| p95 end-to-end | +10% |
| per-node commit interval | +50 ms absolute, if measurable |
| process startup | +2000 ms |
| steady-state RSS | +150 MB |
| sustained CPU | +10 percentage points |

A breach disables the offending instrumentation or stops the rollout. It is not rationalised
afterwards. Real-model runs are **not** byte-comparable — the planner is nondeterministic and node
sequences differ between identical runs — so the comparison is statistical and the equivalence
claim is made only where it is actually true, on the mock model, in `tests/test_observability.py`.

## What has not happened

**No production CloudWatch trace has been observed.** Nothing in this repository claims one exists.
No AWS account mutation was made by this change: Transaction Search is not enabled, the log group is
not created, no IAM was modified and no retention policy was set. Those are one-time account steps
recorded in `deploy/README.md`, to be run deliberately.

## What would change the answer

- The startup or latency gate fails on the real instance — then the instrumentation set is trimmed,
  or the wrapper is reverted to the commented `ExecStart`, and this ADR records the measurement.
- ADOT gains a supported switch for the access-key attribute — then `scripts/observability.py` is
  deleted in favour of it.
- A real CloudWatch trace turns out to be less useful than the spike predicts — then the honest
  outcome is removal, not custom spans layered on to rescue it.
