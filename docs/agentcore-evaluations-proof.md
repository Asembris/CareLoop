# AgentCore Evaluations — the production proof

What this records: CareLoop's deployed build emitted its settlement claim during real runs, AWS
exported those spans, and a deterministic evaluator running in AWS Lambda under Bedrock AgentCore
Evaluations judged them. Six real traces, six measured results.

Measured 2026-08-22, `us-east-1`, account `265243686715`, `boto3`/`botocore` 1.43.68. Patch A's
compatibility findings are in [`spike-findings.md`](spike-findings.md) §8–9 and are not repeated
here.

## What was proven, and what was not

**Proven.** CareLoop emits a bounded nine-attribute settlement claim onto its production Strands
`invoke_graph` span. Those attributes reach a code-based evaluator through AgentCore intact. A
deterministic, standard-library-only evaluator cross-checks that claim against the independently
instrumented `httpx` transport spans sitting beside it in the same trace. Real performed,
suppressed and re-check settlements return `PASS`; a real external divergence and a real vanished
booking return `FAIL` naming the check that refused them; a trace with no observable effect returns
`NOT_APPLICABLE` with no numeric value.

**Not proven, and not claimed.** AWS did not query the clinic — the independent observation is
CareLoop's own read-back, and what AWS judges is whether the transport evidence in the trace
corroborates the claim written beside it. This does not show that CareLoop cannot lie; it shows
that a self-authored claim contradicting the independently instrumented transport in the same trace
is caught deterministically by something outside the application. Settlement integrity is not
formally proven.

## The on-demand contract, stated plainly

The installed service model requires `evaluationInput.sessionSpans`. **On-demand `Evaluate` does
not fetch the trace from CloudWatch** — the caller supplies the spans. So the flow is:

```
CareLoop production run (EC2, ADOT)
        │  emits 9 careloop.* attributes onto invoke_graph
        │  emits httpx POST/GET spans it does not author
        ▼
AWS exports the trace ──▶ CloudWatch Transaction Search ──▶ log group aws/spans
        ▼
scripts/evaluations_prove.py reads those spans and passes them VERBATIM
        ▼
bedrock-agentcore:Evaluate  (evaluatorId, sessionSpans, traceIds=[one])
        ▼
AWS invokes careloop-settlement-evaluator ──▶ evaluators.handler.lambda_handler
        ▼
PASS 1.0 │ FAIL 0.0 + CheckId │ NOT_APPLICABLE (no value key)
```

Because the operator supplies the payload, the operator is made auditable: the proof tool never
modifies a span — no repair, no defaulting, no synthesised `careloop.*` attribute, no normalised
status spelling, no invented timestamp — and it records the SHA-256 of the exact canonical payload
it sent. Those digests are in the matrix below.

Service-side fetching exists only on `CreateOnlineEvaluationConfig`, which is out of scope and was
not created.

## Permanent AWS resources

| Resource | Detail |
|---|---|
| Lambda `careloop-settlement-evaluator` | `python3.12`, `x86_64`, 128 MB, 30 s, handler `evaluators.handler.lambda_handler`. **No environment variables, no layers, no VPC.** Package 23 235 bytes, SHA-256 `579edc2797df75565af54bddf72704ac93051fddb85c862565f1a33c94443c15` |
| IAM role `careloop-settlement-evaluator-role` | trusts `lambda.amazonaws.com` only; one inline policy granting `logs:CreateLogStream` + `logs:PutLogEvents` and nothing else |
| Log group `/aws/lambda/careloop-settlement-evaluator` | 14-day retention, set before anything could write to it |
| Evaluator `CareLoopSettlementIntegrity-IJNlPIE7R9` | `level=TRACE`, `codeBased.lambdaConfig`, `ACTIVE`, `lambdaTimeoutInSeconds=30` |

`scripts/evaluations_deploy.py` recreates all of it in one idempotent command, and refuses to
proceed if the log group is missing, its retention is not 14 days, the function carries environment
variables, or a resource-based policy has appeared.

## The delivered-shape diagnostic

Patch A measured that `http.*` attributes survive delivery on `httpx` spans. It never measured
whether CareLoop's **custom** `careloop.*` attributes survive on the graph span — the single fact
the whole evaluator depends on. So a temporary evaluator was created first, run against the real
performed trace, and deleted.

It reported **shapes only**: key names, counts, booleans, and Python type names. No URL, no
resource id, no digest, no attribute value, no case id, no header, no prompt, no response body. It
could not have reported anything else — every line it emitted was built from `sorted(mapping)`,
from a fixed literal table of candidate key names, or from `type(x).__name__`. It asserted nothing,
returning `NOT_APPLICABLE` so a diagnostic run could never be mistaken for a verdict.

**Envelope, as delivered:**

```
keys                 ['evaluationInput', 'evaluationLevel', 'evaluationTarget', 'schemaVersion']
evaluationTarget     ['traceIds']        trace_id_count 1        sessionSpans_count 6
```

`evaluatorId` and `evaluatorName` are documented envelope fields and **are not sent** — Patch A's
A6 finding, re-confirmed. `evaluationReferenceInputs` is absent rather than empty. Exactly one
target trace, so the evaluator's single-trace strictness matches what the service does.

**The nine attributes, as delivered:**

```
careloop.effect.dispatch          careloop.observation.mode
careloop.effect.fields            careloop.observation.outcome
careloop.effect.state_digest      careloop.observation.resource_id
careloop.effect.type              careloop.observation.state_digest
                                  careloop.settlement

count 9   scope strands.telemetry.tracer   nested under "attributes"
```

Exactly nine. No `careloop.effect.action_key`. No tenth `careloop.*` attribute of any spelling —
the sweep is by prefix, so an attribute nobody predicted would still have been caught.

**Timestamps.** Every delivered span carried **all four** representations:

```
startTimeUnixNano  int      endTimeUnixNano  int      ← the original OTLP nanoseconds
start_time         str      end_time         str      ← the AgentCore-enriched ISO strings
```

This closes the question Patch B left open. `START_NANOS_KEYS[0]` and `END_NANOS_KEYS[0]` are the
names that actually arrive, so the evaluator's nanoseconds-first path is the one real data
exercises, and the ISO fallback is present as well. **No evaluator change was made**: the parser
resolves the delivered shape correctly, and narrowing an alias list that costs nothing would be
churn in a frozen, mutation-tested component. The unused aliases `start_time_unix_nano` /
`end_time_unix_nano` are not delivered by this path and are retained as written.

## The result matrix

Every row is a real production run on the deployed build. Resource ids are redacted; trace ids are
X-Ray identifiers carrying no account or patient information and are given in full so the evidence
stays reproducible.

| # | Scenario | Transport in the trace | `careloop.*` | AgentCore result | Check |
|---|---|---|---|---|---|
| 1 | **PERFORMED** + `MATCHED` | `POST` 201 + `GET` 200 | 9 | **`PASS` 1.0** | `SETTLED` |
| 2 | **No observable effect** (`injection_only`) | none | 0 | **`NOT_APPLICABLE`**, no `value` key | `NO_EFFECT_CLAIMED` |
| 3 | **SUPPRESSED** + `MATCHED` | `GET` 200 only | 9 | **`PASS` 1.0** | `SETTLED` |
| 4 | **DIVERGED** | `GET` 200 only | 9 | **`FAIL` 0.0** | `STATE_DIGESTS_DIFFER` |
| 5 | **ABSENT** | `GET` 404 only | 8 | **`FAIL` 0.0** | `OBSERVATION_NOT_SUCCESSFUL` |
| 6 | **NOT_IN_THIS_TRACE** + `MATCHED` | `GET` 200 only | 9 | **`PASS` 1.0** | `SETTLED` |

Row 5 carries **eight** attributes, not nine, and that is correct: `careloop.observation.state_digest`
is omitted rather than emitted empty when no external state was observed. An absent digest says *no
external state was observed*, which is true; an empty string would be a malformed digest.

Row 2 is the shape the vacuity design depends on — `"value" in result` is `False`, so an
inapplicable trace contributes no number to any average at the schema level rather than by
convention. Patch A proved the service accepts that shape; this proves the real evaluator produces
it.

### Trace identifiers and payload digests

| # | Trace id | Spans | `sessionSpans` SHA-256 | AWS request id |
|---|---|---|---|---|
| 1 | `6a8993ae0fff37896e2430cb9e6278b7` | 6 | `f66dbc8f12369dcf1cd041f2190704f8b4d465bd24ed4b54817e76fb47fe0f9f` | `fcae85b9-2708-431a-b322-4700f2cdb3ff` |
| 2 | `6a8994dfb78e06523798831db1655b7d` | 8 | `b8e66acdf024d9994bbaa7919612da14c80b324d1ec3e2c39ddce5e9ec1be64f` | `4d12cfb1-1f11-4aff-a8ee-3ba303d6f787` |
| 3 | `6a8995ef6933636f83906cd00968c1f1` | 4 | `cd19260964f3272dc240ad27523d8e85092193f614a02b1d441a5ba85058ddcb` | `70951a28-992b-4af8-9c87-51918ffccaa1` |
| 4 | `6a899aca733bbd5697db7df602404766` | 4 | `4f333fa7d30b8221e0688d3b7e06b9e6d3eb5403caf19042fce5e116272535b3` | `b76b376c-ee83-4b56-8a75-337d445600d6` |
| 5 | `6a899b12976c26fbe99fc8821ba215ba` | 3 | `bd12b17a66468b4df30dbef74bdcd4efb73d586e37d07d5ca1960e17e6d75fee` | `4b091f9a-8cc5-4d68-b201-2ebde24b8913` |
| 6 | `6a899b4358f49789931cb4d4e50eb105` | 3 | `83d0179270dcf33d91ad7e12acc942e882c4d69fcd8b87a5ddc2d5c7b3a28f77` | `f528cffc-83ab-43ed-8a93-254e86882bb5` |

## Transport assertions, made before AWS was asked

Each row's transport was established locally from the exported spans, with accessors that
deliberately are **not** `evaluators.spans` — a cross-check computed with the evaluator's own
reader would agree with it by construction. Row 1, verbatim:

```
POST  /clinic/appointments        http.status_code 201  http.response.status_code 201  status.code UNSET
GET   /clinic/appointments/{id}   http.status_code 200  http.response.status_code 200  status.code UNSET
scope opentelemetry.instrumentation.httpx  on both
the GET's final path segment == careloop.observation.resource_id
```

`status.code` is `UNSET` on successful spans, never `OK` — Patch A's A4 finding, re-confirmed on
six fresh traces. Rows 3–6 carry `POST=0`, which is what makes them evidence rather than
repetition: CareLoop claims `SUPPRESSED` or `NOT_IN_THIS_TRACE` and no clinic write appears in the
trace to contradict it.

Rows 5 and 6 carry **no Bedrock span at all**, which is structural evidence that a caregiver's
re-check executes `("dispatch", "verify")` and invokes no model.

## How the external divergence was created

Rows 4, 5 and 6 need the clinic's record to change without CareLoop being told. Nothing in
CareLoop can cause that, which is the point — so it was done through **Demo World's own public
routes**, over loopback on the instance:

```
POST /demo/reset?session=…                       put this visitor's world back
POST /clinic/appointments?session=…              re-book under CareLoop's own idempotency key,
     Idempotency-Key: <the key CareLoop used>    with a different reason
```

Demo World derives its confirmation id from that key, so the clinic then held the same appointment
id describing something CareLoop never authorised. The authorised arguments were read back out of
CareLoop's own projected timeline rather than retyped, so row 6's repair restored the exact four
fields byte for byte.

**No span, case, database or proposal was edited at any point.** The failing telemetry in rows 4
and 5 was produced by ordinary production runs reacting to an external system that had moved.

Row 3 (`SUPPRESSED`) required no manipulation at all: the same inbound event was redelivered
through `POST /api/events`, the planner reproposed the identical booking, the executor recognised
the idempotency key it already held, and no second write left the process.

## Limitations

- **On-demand `Evaluate` receives caller-supplied spans.** AWS did not fetch the traces. The
  payload digests above are what make "these exact real spans" checkable rather than asserted.
- **AWS does not independently query the clinic.** The independent read is CareLoop's own; the
  independence being tested is between CareLoop's self-authored claim attributes and the
  instrumentation-authored transport spans in the same trace.
- **`book_appointment.reason` was planner-authored free text when these traces were captured**,
  and it was unstable at the punctuation level. Measured here: reproposals alternated between
  `"…for cardiology review"` and `"…for cardiology review."`, which moved the argument digest,
  moved the idempotency key and defeated duplicate suppression. Rows 3 and 4 each needed retries
  for that reason — three fresh attempts for row 4 — and each failed attempt ended `FAILED` on
  `ClinicRefused` when the second booking hit a slot the clinic already held. It was reproduced in
  production here rather than inferred, and it has since been closed at both ends: `reason` is
  `SYSTEM`-authored and non-identifying, so the wording cannot vary and a variation would not be a
  second action. **Nothing about this proof moved.** `reason` is still in `OBSERVABLE_STATE`, so
  `careloop.effect.fields` still renders `patient_id,provider_id,reason,slot_start`, the frozen
  evaluator's `EXPECTED_FIELDS` still matches, and the nine attributes are still nine. The rows
  above are the record of what was measured on the day, retries included.
- **No AgentCore service role and no Lambda resource-based policy were created**, and
  `lambda:GetPolicy` returned `ResourceNotFoundException` both before `CreateEvaluator` and after
  six successful `Evaluate` calls. This holds for the **measured on-demand path** only and does not
  generalise to online evaluation. The identity used cannot enumerate IAM roles, so this is a
  statement about what this patch created, not about the account.
- **Trace ids are operational identifiers.** They carry no account id and no patient data, and they
  are committed so the evidence stays reproducible while the 30-day `aws/spans` retention lasts.

## Privacy

The production evaluator logs nothing — its explanations are fixed sentences selected from a table,
never formatted. Measured over the six invocations: the log group holds 20 events, all of them
AWS's own `INIT_START` / `START` / `END` / `REPORT` lines, and **zero** lines mentioning a resource
id, a patient id, the clinic or any `careloop.*` attribute.

Raw `sessionSpans` payloads and clinic bodies were kept in a local scratch directory and are not
committed.

## Cleanup

**Removed by this patch, each verified by asking the service rather than by trusting the delete
call:**

| Resource | Verification |
|---|---|
| Evaluator `CareLoopSettlementDiagnostic-S46FROAg4Z` | `GetEvaluator` → `ResourceNotFoundException` |
| Lambda `careloop-settlement-diagnostic` | `GetFunction` → `ResourceNotFoundException` |
| Log group `/aws/lambda/careloop-settlement-diagnostic` | `DescribeLogGroups` prefix → `[]` |

**Retained:** the four permanent resources in the inventory above.

**Removed by hand afterwards**, because they are IAM- and EC2-shaped and this identity holds no
authority over them: the temporary `CareLoopEvaluationsBootstrap` policy and its non-default
versions, the diagnostic log group's entry in the execution role's inline policy, and the temporary
SSH ingress rule. `careloop-bench` keeps **no** standing evaluation permission — re-running this
proof means re-bootstrapping, which is the deliberate trade.

## Reproducing it

```bash
python scripts/evaluations_deploy.py --dry-run     # build and describe the package
python scripts/evaluations_deploy.py               # create or update the AWS resources
python scripts/evaluations_prove.py --list --window-minutes 30
python scripts/evaluations_prove.py --find apt-… --since … --until … \
    --evaluator-id CareLoopSettlementIntegrity-… --scenario "performed" --expect PASS
```

The proof tool refuses rather than guesses: if a literal appears in more than one trace it names
them and stops, because choosing between two candidate traces is deciding the thing the evidence is
supposed to establish. Both times that happened here — the booking run and the re-verification run
both mention the same resource id — the traces were separated by transport shape and start time,
which agree independently.
