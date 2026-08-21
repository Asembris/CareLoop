# Spike findings — SDK and tooling surfaces

Pinned against **`strands-agents==1.51.0`**, Python 3.12.0, Windows.

Sections 1–6 are the four SDK surfaces the CareLoop design depends on, plus the Bedrock model-id
question. Section 7 is `deepteam==1.0.9`, added in session G for the adversarial evaluation under
`evals/`. Sections 8 and 9 are the AgentCore Evaluations compatibility spike: §8 is the IAM-blocked first
attempt, kept as evidence, and §9 is the rerun that resolved every question. Each section was
verified by executing code, not by reading signatures.

## 1. GraphBuilder — CONFIRMED

All required primitives exist as first-class builder methods:

| Need | Method | Status |
|---|---|---|
| Conditional edges | `add_edge(from, to, condition: Callable[[GraphState], bool])` | confirmed |
| Loop bound | `set_max_node_executions(n)` | confirmed |
| Graph timeout | `set_execution_timeout(seconds)` | confirmed |
| Per-node timeout | `set_node_timeout(seconds)` | confirmed |
| Cycle revisiting | `reset_on_revisit(bool)` | confirmed |

Verified empirically: an edge whose condition returns `False` does not execute its target, and a
deliberately non-converging two-node cycle with `set_max_node_executions(6)` terminated after
exactly 6 node executions with `Status.FAILED` — no hand-rolled counter involved.

## 2. Policy gate — CONFIRMED, then CORRECTED in commit 6

`BeforeToolCallEvent` has a first-class **`cancel_tool: bool | str`** field. Setting it to a string
prevents the tool from executing at all. The plan assumed we might have to substitute a stand-in
tool; that is unnecessary.

Verified empirically: a tool whose body appends to a module-level list was cancelled from the hook,
and the list stayed empty. The model then received:

```json
{"toolUseId": "tu_1", "status": "error",
 "content": [{"text": "DENY: policy rule comms.send_document.deny_untrusted_destination"}]}
```

Two consequences were drawn from this, and **the first was wrong**:

- ~~The denial reason is surfaced **to the planner as an error tool result**, so a denied plan feeds
  the bounded replan cycle naturally rather than dead-ending.~~
- Because the reason string reaches the model, it must never contain anything the model should not
  see. Rule `reason` fields are written for that audience. **This half stands**, and is now
  enforced: `cancel_tool` carries a literal from the closed `ReasonCode` vocabulary and
  `tests/test_policy_hook.py` runs the reason-purity leak detector over it. The spike's own example
  wrote `"DENY: policy rule comms.send_document.deny_untrusted_destination"` into that field, which
  leaks a rule id; that string is now a test fixture for what must not survive.

### The correction

**The spike assumed an event loop that commit 5 removed.** Everything above was verified inside a
`strands.Agent` running tools. CareLoop has no such agent: `careloop/agents/invoke.py` drives the
model provider's structured-output generator directly (see §3), so **neither agent holds a tool and
no tool result is ever returned to a model.**

So there is no error-tool-result channel, and a denial does not reach the planner that way. The
replan channel is `prior_denials` into `careloop.agents.prompts.planner_user_prompt`, which carries
a `(tool_name, ReasonCode)` pair and renders it through `reason_text`. That is strictly better for
invariant 6 — what arrives is a literal from a closed enum, on a path with no SDK in it.

**What CareLoop actually does**, verified by execution against `strands-agents==1.51.0`: the
execute node builds a real `BeforeToolCallEvent`, fires it against a real `HookRegistry`, and reads
`cancel_tool` back. `HookRegistry.invoke_callbacks_async` dispatches on the event's type and never
reads `event.agent`, so `agent=None` and `selected_tool=None` work — the latter is a value the
field's own type already admits. A `PolicyHook` written this way would plug unchanged into a real
`Agent`. **What is CareLoop's is the firing, not the machinery**, and `careloop/policy/hook.py`,
the README and `docs/session-log.md` all say so; letting it read as "Strands blocks the attack"
would be a claim someone could check and find inflated.

**Layering, as built.** Three enforcement points, not two, all reading the same `rules.yaml`: the
`gate` node routes the proposal while the case is `PLANNING`; the `BeforeToolCall` hook and the
gated tool wrapper both fire at the tool boundary while it is `EXECUTING`. The wrapper lives in
`careloop/policy/gated.py` rather than beside the tool bodies, because `careloop/tools/` may not
import `careloop.policy` — a sweep enforces that, and a tool that cannot import the policy layer
cannot decide its own authority.

The decision is cached per call, but **not** on `(case_id, tool, args)` as planned: that key does
not identify the facts the decision was computed against, which is a TOCTOU window between the two
points the architecture claims are independent. The key also carries `case.version`, `case.state`
and a fingerprint over the case's facts.

## 3. `Agent.structured_output` — CONFIRMED, then CORRECTED in commit 5

The method exists as recorded: `Agent.structured_output(output_model: type[T], prompt=None) -> T`,
with `structured_output_async` alongside it. **What the spike missed is that both are deprecated in
`strands-agents==1.51.0`, and that the replacement is not equivalent.** CareLoop does not use
either; `careloop/agents/invoke.py` drives `Model.structured_output` directly.

Two independent reasons, the second the more serious:

**The deprecation is enforced against us.** Both methods `warnings.warn(..., DeprecationWarning,
stacklevel=2)`, so the warning is attributed to the *caller* — a module under `careloop.` — and
`pyproject.toml` carries `filterwarnings = ["error::DeprecationWarning:careloop.*"]`. Calling
either from library code raises.

**The non-deprecated replacement routes structured output through a tool call.** Passing
`structured_output_model=` to an agent invocation builds a `StructuredOutputContext`, which
registers an internal `StructuredOutputTool` and forces the model to call it. That tool would then
arrive at the `BeforeToolCall` policy hook of finding 2 — a deny-by-default engine that has never
heard of it, holds no rule naming it, and would refuse it. An SDK-internal tool tripping CareLoop's
own gate is a failure that surfaces deep inside graph execution looking like nothing in particular.
It would also require `MockModel` to emit `toolUse` stream events, and would move CareLoop off the
path the model benchmark measured.

What survives unchanged is finding 4: `Agent.structured_output_async` reaches the provider by
calling `self.model.structured_output(output_model, messages, system_prompt=...)`, which is exactly
the async-generator contract below and exactly what `MockModel` implements. Driving it directly
loses nothing, and leaves both agents with no tools and no event loop.

## 4. Custom model provider — CONFIRMED, with one correction to the plan

`strands.models.Model` abstract methods are `update_config`, `get_config`, `stream`,
`structured_output`.

Correction: **`Model.structured_output` is an async *generator*, not a coroutine.** It must
`yield {"output": <instance>}` — not `return` the instance. The plan said "returns the scripted
Pydantic instance directly"; the mock will yield it in the required envelope.

`stream()` is an async iterable of Bedrock-converse-shaped `StreamEvent` dicts. A tool call is:

```
{"messageStart": {"role": "assistant"}}
{"contentBlockStart": {"start": {"toolUse": {"toolUseId": ..., "name": ...}}}}
{"contentBlockDelta": {"delta": {"toolUse": {"input": "<json string>"}}}}
{"contentBlockStop": {}}
{"messageStop": {"stopReason": "tool_use"}}
```

## 5. Graph nodes — PLAN CORRECTION

`GraphBuilder.add_node(executor: AgentBase | MultiAgentBase, node_id)` does **not** accept plain
callables. The plan's "deterministic node callables" cannot be passed directly.

Resolution: deterministic nodes subclass **`MultiAgentBase`**, which has exactly one abstract
method, `invoke_async(task, invocation_state=None) -> MultiAgentResult`. This is a thin wrapper —
the node body stays plain Python with no model attached, so the "everything else is deterministic
Python" principle is unaffected. `AgentBase` was rejected as the base: it requires both
`invoke_async` and `stream_async`.

One constructor detail that cost a spike iteration: `NodeResult.result` must be an `AgentResult`,
a nested `MultiAgentResult`, or an `Exception` — passing a bare `str` raises
`AttributeError: 'str' object has no attribute 'results'` inside the graph's input builder, not at
the call site. Deterministic nodes wrap their output in a minimal `AgentResult`.

## 6. Bedrock model id — CORRECTION 5 CONFIRMED

`BedrockModel._get_default_model_with_warning()` builds its default id from a region-derived prefix
(`us` / `eu` / `apac`, via `prefix_inference_map`), i.e. the SDK itself targets the
**inference-profile form**. The bare `amazon.nova-lite-v1:0` is the wrong shape for accounts that
require a profile.

`careloop/llm/providers.py` will therefore default to `us.amazon.nova-lite-v1:0`, derive the prefix
from the resolved region using the same rule, and allow a full override via
`CARELOOP_BEDROCK_MODEL_ID`. Not exercised in tests — no AWS credentials are present in this
environment, and the suite runs entirely on the mock provider.

## 7. DeepTeam 1.0.9 — CONFIRMED, with three findings that change how it is used

Verified in a throwaway virtualenv against `deepteam==1.0.9`, which resolved `deepeval==4.1.8`,
`openai==3.0.0`, `pydantic==2.13.4`, `posthog`, `grpcio` and `opentelemetry-sdk`. That dependency
set is why the package belongs to an extra of its own and never to `dev`: CI's "no provider SDK is
installed" step exists to keep the default suite unable to reach a provider, and `openai` arrives
transitively here.

### 7.1 `import deepteam` fails on a clean install — undeclared `sentry_sdk`

```
File "deepteam/telemetry.py", line 7, in <module>
    import sentry_sdk
ModuleNotFoundError: No module named 'sentry_sdk'
```

`deepteam/telemetry.py` imports `sentry_sdk` at module scope and `deepteam/__init__.py` reaches
that module transitively, but `sentry-sdk` appears nowhere in the distribution's `requires_dist`.
A `pip install deepteam==1.0.9` into an empty environment therefore produces a package that cannot
be imported. `sentry-sdk` is pinned explicitly in the `evals` extra as a consequence — not as a
preference, as the only way the extra installs something that works.

### 7.2 Importing it makes a network call and sends your public IP to a third party

`deepteam/telemetry.py`, executed at import unless opted out:

```python
def telemetry_opt_out():
    return os.getenv("DEEPTEAM_TELEMETRY_OPT_OUT") == "YES"

if not telemetry_opt_out():
    anonymous_public_ip = get_anonymous_public_ip()      # GET https://api.ipify.org
    sentry_sdk.init(dsn="https://...@o4506098477236224.ingest.sentry.io/...", ...)
```

`blocked_by_firewall()` in the same module opens a socket to `www.google.com:80`. So the default
behaviour of the tool being used to evaluate this system's security is to resolve the operator's
public IP address and register a crash reporter against a hard-coded DSN, before any evaluation
runs.

**`DEEPTEAM_TELEMETRY_OPT_OUT=YES` and `DEEPEVAL_TELEMETRY_OPT_OUT=YES` must therefore be set
before the import, not before the run**, and setting them in a shell is not good enough — an
`import deepteam` at the top of a module executes before any statement in that module could set
them. `evals/` sets both in the single module that imports deepteam, above the import, and a test
asserts the ordering. Opting out is checked rather than trusted because the failure is silent:
nothing reports that the call was made.

### 7.3 `ignore_errors` defaults to `True`

The published documentation says `False`. The measured signature says otherwise:

```python
ignore_errors: bool = True
```

An attack whose target callback raises is then counted into `RedTeamingOverview.errored` and the
run carries on. That is survivable — the count is visible — but a run reporting "0 failures" with
a non-zero `errored` is reporting that the judge never saw those attacks, not that they passed.
Errored counts are reported alongside pass and fail in `docs/security-evaluation.md` for that
reason.

### 7.4 The API surface, confirmed

Everything the harness depends on exists and has the shape the plan assumed.

| Surface | Measured |
|---|---|
| `deepteam.test_case` | `RTTurn`, `ToolCall`, `RTTestCase` |
| `RTTurn` | `role: Literal["user","assistant"]`, `content: str`, `tools_called: list[ToolCall] \| None`, `turn_level_attack`, `metadata`, … |
| `ToolCall` | `name: str`, `input_parameters: dict \| None`, `output: Any \| None`, `description`, `reasoning`, `type` |
| `model_callback` | `Callable[[str, list[RTTurn] \| None], RTTurn]`; both sync and awaited call sites exist, so an async callback is supported |
| `red_team()` returns | `RiskAssessment(overview: RedTeamingOverview, test_cases: list[RTTestCase])`, a pydantic model with `model_dump_json` and `save()` |
| `RedTeamingOverview` | `vulnerability_type_results`, `attack_method_results`, `errored: int`, `run_duration: float`, `cvss_score` |

All six vulnerability classes the session named exist, `ExternalSystemAbuse` included:

| Vulnerability | Types |
|---|---|
| `ExcessiveAgency` | `functionality`, `permissions`, `autonomy` |
| `ToolOrchestrationAbuse` | `recursive_tool_calls`, `unsafe_tool_composition`, `tool_budget_exhaustion`, `cross_tool_state_leakage` |
| `IndirectInstruction` | `rag_injection`, `tool_output_injection`, `document_embedded_instructions`, **`cross_context_injection`** |
| `AutonomousAgentDrift` | `goal_drift`, `reward_hacking`, `agent_collusion`, `runaway_autonomy` |
| `ExternalSystemAbuse` | `data_exfiltration`, `communications_spam`, `internal_spoofing` |
| `PIILeakage` | **`api_and_database_access`**, **`direct_disclosure`**, `session_leak`, `social_manipulation` |

**The table above lists enum *values*, and the first version of it listed member names.** The
spike printed `str(t)` for each type, which renders as `IndirectInstructionType.CROSS_CONTENT_
INJECTION`, and the suite table was then written by lower-casing those. Three of the twenty-two
disagree with their own member name:

| member name | actual value |
|---|---|
| `IndirectInstructionType.CROSS_CONTENT_INJECTION` | `cross_context_injection` (content -> context) |
| `PIILeakageType.DATABASE_ACCESS` | `api_and_database_access` |
| `PIILeakageType.DIRECT` | `direct_disclosure` |

`Vulnerability(types=[...])` validates against the **value**, so all three raised at construction.
This is an instrument defect of exactly the shape session B recorded twice: the measurement was
wrong in a direction that looked plausible, and reading it more carefully would not have caught it
— only executing against the real constructor did. **It cost nothing**, because `red_team()` is
reached after the vulnerabilities are instantiated and the run aborted at 0 provider calls and
$0.00, which is the dry-run-then-ceiling arrangement doing its job.

The four attack techniques the session named all exist under `deepteam.attacks.single_turn`:
`ContextPoisoning`, `GoalRedirection`, `PermissionEscalation`, `PromptInjection`.

`OWASP_ASI_2026` exists in `deepteam.frameworks` and takes `categories=["ASI_01".."ASI_10"]`,
exposing `.vulnerabilities` and `.attacks`. **It is not adopted whole.** Its full set drags in
`SQLInjection`, `ShellInjection`, `UnexpectedCodeExecution`, `DebugAccess`, `Ethics`,
`IntellectualProperty` and `Misinformation` — surfaces CareLoop does not have, since it executes no
code, runs no database and generates no content. Padding the report with categories that pass
because the capability is absent would weaken it. The two categories that map cleanly are recorded
so the report can cite the framework without claiming coverage it does not have:

- **ASI_01** (agent goal hijack) — `ExcessiveAgency`, `GoalTheft`, `IndirectInstruction`,
  `RecursiveHijacking`, `Robustness`
- **ASI_02** (tool misuse) — `BFLA`, `ExcessiveAgency`, `ToolOrchestrationAbuse`

### 7.5 A custom model is accepted, and it must satisfy one specific contract

`simulator_model` and `evaluation_model` are annotated `DeepEvalBaseLLM` and default to the string
`"gpt-4o-mini"`. `deepeval.metrics.utils.initialize_model(probe)` returns **the same object** with
`using_native_model=False`, so a custom subclass is passed straight through rather than being
re-resolved by name.

The contract a non-native model must satisfy, read from
`deepteam/attacks/attack_simulator/utils.py` and mirrored in every metric:

```python
res = await model.a_generate(prompt=..., schema=SomePydanticModel)
# a pydantic instance is used as-is; a str is run through trimAndLoadJson and fed to schema(**data);
# a TypeError from the schema kwarg falls back to a_generate(prompt) and JSON-parses the text
```

Two consequences for the budget guard. **Cost is `None` for a non-native model** — deepteam's own
`simulation_cost` and `evaluation_cost` fields stay empty, so spend accounting has to be ours. And
a failure is retried **three times with exponential backoff**, so one schema failure is three
calls, which the estimate must assume rather than discover.

### 7.6 NVIDIA and strict Structured Outputs — the premise holds, for a different reason

`CLAUDE.md` records that OpenAI-compatible providers reach structured output through *strict*
Structured Outputs. That is a fact about OpenAI proper, and session G's plan leaned on it to choose
NVIDIA for the judge. It does not transfer unexamined, and the first probe appeared to refute it:
`meta/llama-3.3-70b-instruct` returned an object carrying an **extra key** (`"verdict "`) that
`strict: True` should have made unrepresentable.

That first reading was wrong, and the correction is the finding. The probe had sent a bare
`model_json_schema()`, which omits `additionalProperties: false` and does not list every property
in `required` — **a schema OpenAI proper rejects outright as invalid for strict mode.** NVIDIA
accepted it and decoded unconstrained.

> **NVIDIA's endpoint does not validate a strict-mode schema for strict-mode compliance. A schema
> OpenAI would refuse is accepted and silently downgraded to unconstrained decoding.** The guard is
> fail-open, and the only defence is to emit the compliant form ourselves rather than trusting the
> endpoint to demand it.

Re-probed with `additionalProperties: false` and a complete `required`, three trials against each
of two request forms (`client.beta.chat.completions.parse`, and
`response_format={"type": "json_schema", "strict": true}`):

| Model | schema-ok | tools correctly named | extra keys returned |
|---|---|---|---|
| `meta/llama-3.3-70b-instruct` | **6/6** | 6/6 | 0/6 |
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | **6/6** | 6/6 | 0/6 |
| `openai/gpt-oss-120b` | 3/6 | 3/6 | 0/6 |

`gpt-oss-120b`'s three failures were **all `APITimeoutError` at 120 s**, not schema failures.

**The real operational risk is capacity, not schema fit.** Across roughly 26 probe calls the shared
endpoint produced one `503 ResourceExhausted: Worker local total request limit reached (16/16)`
and four 120 s timeouts. `red_team()` defaults to `max_concurrent=10` and retries three times with
backoff, which against a 16-worker shared endpoint is a way to manufacture the exhaustion it is
retrying around. The evals runner therefore sets `max_concurrent` low and leans on the response
cache for reruns.

One model was removed from consideration by the probe rather than by judgement:
`qwen/qwen3-next-80b-a3b-instruct` answers **`410 Gone — end of life 2026-07-27`**.

Schema fit and verdict quality are separate axes here exactly as session B found them to be for
Bedrock. Both surviving models satisfied the schema perfectly; their *verdicts* differed, with
`llama-3.3-70b` returning "incomplete information to make a verdict" on 4 of 6 trials and emitting
tool names with trailing whitespace, while `nemotron-super-49b` answered the question asked on 6 of
6. That is a judge-selection observation, not a schema one, and it is why the two roles are not
given the same model.

### 7.7 A USD ceiling does not guard an NVIDIA run

`bench/rates.json` already prices `meta/llama-3.3-70b-instruct` at `0.0`, with the reason stated in
the entry: the `build.nvidia.com` developer endpoint is **credit-metered, not billed per token**,
so there is no dollar figure to charge against a dollar ceiling. Recording a price would be
inventing one.

The consequence had not been drawn before and is drawn here. **`--max-spend` is not a guard for the
simulator and the judge.** It guards the *target*, whose triage and planner calls are Bedrock and
genuinely priced per token. A run bounded only in dollars is unbounded in NVIDIA credits.
`evals/run.py` therefore carries **two** ceilings — `--max-spend` in USD over the Bedrock target
calls, and `--max-calls` over every provider call the run makes at all — because a ceiling that
cannot see the resource it is spending is decoration.

## 8. AgentCore Evaluations — Patch A, PARTIAL: one question answered, five blocked

> **Superseded by §9, and kept deliberately.** This is the record of the first, IAM-blocked
> attempt. Its permission inventory is what unblocked the rerun; its one error — the
> `bedrock-agentcore-control:` action prefix — is corrected in §9 rather than edited away here.

**Status: the spike could not be completed, and the reason is credentials rather than AWS.** One of
the six questions (A6) is answered definitively and offline. The other five each require either
creating a throwaway Lambda and evaluator or reading span content, and the only AWS identity
reachable from this workstation is authorised for neither. **Nothing here disproves the
`CareLoopSettlementIntegrity` design, and nothing here licenses recutting it** — an unrun experiment
is not a negative result, and the distinction matters because A1 is an architecture gate whose
failure would mean a materially larger telemetry patch.

Region `us-east-1`. `boto3==1.43.68`, `botocore==1.43.68`. No AWS CLI on the workstation; every call
below was made through boto3.

### What the identity can and cannot do

Probed read-only, by execution. The identity is the benchmark IAM user the model benchmark uses; it
is a long-lived `AKIA…` key in the untracked `.env`, and it is the only credential on the machine —
there is no `~/.aws/config`, no `~/.aws/credentials`, and the SSO cache is empty.

| Probe | Result |
|---|---|
| `sts:GetCallerIdentity` | allowed — an IAM **user**, not the EC2 instance profile |
| `logs:DescribeLogGroups` | **allowed** |
| `logs:DescribeLogStreams`, `logs:FilterLogEvents`, `logs:StartQuery` | `AccessDeniedException` |
| `lambda:ListFunctions` | `AccessDeniedException` |
| `iam:ListRoles` | `AccessDenied` |
| `bedrock-agentcore-control:ListEvaluators` | `AccessDeniedException` |
| `bedrock-agentcore:Evaluate` | `AccessDeniedException` |
| `xray:GetTraceSegmentDestination` | `AccessDeniedException` |

So the throwaway Lambda cannot be created, its execution role cannot be created, the throwaway
evaluator cannot be created, `Evaluate` cannot be called, and span **content** cannot be read. Group
*metadata* can be, which is the one live thing this session could establish.

### A6 — current SDK and control-plane shape — **ANSWERED**

Read out of the service models shipped in the installed `botocore==1.43.68`, which is what the SDK
actually sends rather than what a documentation page describes — the same discipline §6 applied to
the Bedrock model id and §4 to the emitted tool spec.

| | |
|---|---|
| Control plane | `bedrock-agentcore-control`, API version **2023-06-05**, `rest-json` |
| Data plane | `bedrock-agentcore`, API version **2024-02-28**, `rest-json` |

```
CreateEvaluator   POST   /evaluators/create
GetEvaluator      GET    /evaluators/{evaluatorId}
DeleteEvaluator   DELETE /evaluators/{evaluatorId}
Evaluate          POST   /evaluations/evaluate/{evaluatorId}
```

All four exist in the current SDK. **No legacy tooling and no `agentcore` CLI is required** — the
starter toolkit is a convenience over exactly these operations.

`CreateEvaluator` requires `evaluatorName`, `evaluatorConfig` and `level`; `level` is a closed enum
`TOOL_CALL | TRACE | SESSION`. `evaluatorConfig` is a **union** of `llmAsAJudge` and `codeBased`, and
`codeBased` is itself a union whose one member is `lambdaConfig`:

```
evaluatorConfig.codeBased.lambdaConfig.lambdaArn                REQUIRED
evaluatorConfig.codeBased.lambdaConfig.lambdaTimeoutInSeconds   optional, integer 1–300
```

Three constraints that change how the design is written, none of which is stated in the prose
documentation:

- **`evaluatorName` matches `[a-zA-Z][a-zA-Z0-9_]{0,47}`** — letters, digits and underscores only,
  48 characters maximum. A hyphenated name is rejected at the API. `CareLoopSettlementIntegrity` is
  27 characters and valid; `careloop-settlement-integrity` would not be.
- **`evaluationTarget` is a union** — `traceIds` **xor** `spanIds`, never both, each 1–10 items. A
  trace-level evaluator therefore names at most ten traces per call.
- **`Span` is a `document` type with no declared members**, and `sessionSpans` is 1–1000 of them. The
  API layer imposes no span schema at all, so it can neither require `session.id` nor filter by
  scope. Whatever A1 and A2 turn out to be, they are facts about the *service's* processing, not
  about request validation — which is why neither can be settled by reading the model.

`EvaluationResultContent` carries `label`, `value`, `explanation`, `errorCode`, `errorMessage`,
`tokenUsage` and `context`. **`value` is optional in the wire model** and `context.spanContext`
carries `sessionId`, `traceId`, `spanId`. That is consistent with the N/A-without-`value` design and
with `session.id` being optional, but consistency is not proof: A5 and A2 are about whether the
service *coerces* an absent `value` and whether it *accepts* a session-less span set, and only a live
call can answer either.

### A1, A2, A3, A5 — **BLOCKED, not answered**

All four need a throwaway code-based evaluator, and creating one needs `lambda:CreateFunction`,
`iam:CreateRole` and `bedrock-agentcore-control:CreateEvaluator`. None is available.

The A2 experiment was built and attempted anyway against `Builtin.Helpfulness`, on a hand-built
three-span set — a `strands.telemetry.tracer` root plus an `opentelemetry.instrumentation.httpx`
`POST` and `GET` — carrying **no `session.id` anywhere**, with `evaluationTarget={"traceIds":[…]}`.
It returned `AccessDeniedException` on `bedrock-agentcore:Evaluate` before any service processing, so
it establishes the permission state and nothing about session identity.

**A1 remains the gate.** The question is whether a code-based evaluator's Lambda receives supplied
spans whose scopes are `opentelemetry.instrumentation.httpx` and `botocore` — scopes the service
documents as *excluded from generic framework classification*. CareLoop's entire independent evidence
is httpx spans, so a filtered answer means the design must be recut. **That question is still open.**

### A4 — real production HTTP attribute spellings — **BLOCKED**

Reading a real CareLoop span needs `logs:StartQuery` or `logs:FilterLogEvents` on `aws/spans`. Both
are denied. The synthetic spans above use the old spelling on the reasoning that
`OTEL_SEMCONV_STABILITY_OPT_IN` carries only the `gen_ai_unredacted_attributes=` token, and that
variable also selects HTTP semantic-convention stability — but **that is a prediction from the pinned
configuration, not a measurement, and it is exactly what A4 exists to check.** The evaluator must
read both spellings until a real span says otherwise.

### What the log-group metadata did establish

Group metadata is readable, and it independently corroborates ADR 0004's completion record from a
direction the ADR does not itself claim:

| Log group | Retention | Holds data |
|---|---|---|
| `aws/spans` | 30 days | yes |
| `/aws/bedrock-agentcore/runtimes/careloop` | **14 days** | yes |
| `/aws/application-signals/data` | never expire | yes |

`aws/spans` exists and is non-empty, which is what Transaction Search being enabled and ingesting
looks like from outside. The CareLoop agent log group carries **exactly the 14-day retention
`deploy/README.md` prescribes**, which is what the one-time account setup having been run
deliberately looks like. Neither is a substitute for A4: these are byte counts and retention
settings, not span contents.

`/aws/application-signals/data` is created by the Transaction Search resource policy and is empty of
CareLoop metrics — Application Signals remains off, as pinned.

### The exact least-privilege policy this spike needs

Scoped to throwaway names so the grant cannot touch anything CareLoop owns. `iam:PassRole` is
conditioned on the Lambda service so the role cannot be passed anywhere else.

```json
{"Version": "2012-10-17", "Statement": [
  {"Sid": "SpikeLambda", "Effect": "Allow",
   "Action": ["lambda:CreateFunction", "lambda:GetFunction", "lambda:InvokeFunction",
              "lambda:DeleteFunction", "lambda:AddPermission", "lambda:RemovePermission",
              "lambda:GetPolicy"],
   "Resource": "arn:aws:lambda:us-east-1:<ACCOUNT>:function:careloop-evalspike-*"},
  {"Sid": "SpikeRole", "Effect": "Allow",
   "Action": ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole",
              "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies"],
   "Resource": "arn:aws:iam::<ACCOUNT>:role/careloop-evalspike-*"},
  {"Sid": "SpikePassRole", "Effect": "Allow", "Action": "iam:PassRole",
   "Resource": "arn:aws:iam::<ACCOUNT>:role/careloop-evalspike-*",
   "Condition": {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}},
  {"Sid": "SpikeEvaluator", "Effect": "Allow",
   "Action": ["bedrock-agentcore-control:CreateEvaluator",
              "bedrock-agentcore-control:GetEvaluator",
              "bedrock-agentcore-control:ListEvaluators",
              "bedrock-agentcore-control:DeleteEvaluator"],
   "Resource": "*"},
  {"Sid": "SpikeEvaluate", "Effect": "Allow", "Action": "bedrock-agentcore:Evaluate",
   "Resource": "*"},
  {"Sid": "SpikeReadSpans", "Effect": "Allow",
   "Action": ["logs:StartQuery", "logs:GetQueryResults", "logs:FilterLogEvents",
              "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
   "Resource": ["arn:aws:logs:us-east-1:<ACCOUNT>:log-group:aws/spans:*",
                "arn:aws:logs:us-east-1:<ACCOUNT>:log-group:/aws/bedrock-agentcore/runtimes/careloop:*"]},
  {"Sid": "SpikeLambdaLogs", "Effect": "Allow",
   "Action": ["logs:PutRetentionPolicy", "logs:DeleteLogGroup", "logs:DescribeLogGroups"],
   "Resource": "arn:aws:logs:us-east-1:<ACCOUNT>:log-group:/aws/lambda/careloop-evalspike-*"}
]}
```

`bedrock-agentcore-control:CreateEvaluator` and `bedrock-agentcore:Evaluate` are `Resource: "*"`
because an evaluator's ARN is minted by the create call and cannot be named in advance, and because
`Evaluate` authorises against the evaluator ARN — the same shape, and the same reason, as the four
unavoidable `xray:*` wildcards ADR 0004 records. **Every statement is deletable in one edit when the
spike ends; none of it belongs on the EC2 instance profile, which must gain nothing.**

`SpikeReadSpans` is read-only over two named groups. `SpikeLambdaLogs` exists because a Lambda log
group defaults to NEVER EXPIRE and the spike must be able to set retention and then delete it — the
trap ADR 0004 records for AgentCore log groups, which applies verbatim to Lambda's.

### What was created, and what changed

**Nothing.** No `Create*` call was made against any AWS service — every creation surface answered
`AccessDenied` to a read-only probe first, so nothing was attempted. Log-group metadata was
re-listed after the probes and is byte-identical to before: same four groups, same retention, same
`storedBytes`. EC2 IAM, observability IAM, Transaction Search, CloudWatch retention, AgentCore
Observability, security groups, production systemd and the CareLoop deployment are all untouched, and
no code outside this document changed.

Cleanup is therefore trivially proven rather than performed: there is nothing to remove.

### What has to happen before Patch A can be rerun

One thing: an AWS identity carrying the policy above. Everything else is written and ready — the
probe harness, the synthetic session-less span set, and the diagnostic Lambda's contract (report the
envelope's `schemaVersion`, `evaluationLevel` and `evaluationTarget`, the span count, the **distinct
`scope.name` values received**, whether the target trace id is present, and a `label` with no `value`
for the A5 arm; call nothing, mutate nothing).

Until A1 is measured, the nine-attribute `CareLoopSettlementIntegrity` contract stands as designed
and unimplemented. It is not approved for implementation and it is not recut.

## 9. AgentCore Evaluations — Patch A rerun: A1–A6 resolved by execution

Temporary least-privilege permissions were provisioned externally onto the existing benchmark
identity, and the spike was rerun. Region `us-east-1`, `boto3==1.43.68`/`botocore==1.43.68`, no AWS
CLI. Two throwaway resources were created, and every temporary resource and permission the spike
used has since been removed — see *Resources and permissions* below. §8's inventory of what
was needed proved correct except for one action-prefix error, corrected below.

**The correction §8 needs, and it would have silently granted nothing.** §8 wrote
`bedrock-agentcore-control:CreateEvaluator`. **The IAM action prefix is `bedrock-agentcore:` for
both planes.** Measured: the control-plane service model declares `signingName: bedrock-agentcore`
while its `endpointPrefix` is `bedrock-agentcore-control`, and AWS's published evaluations policy
lists `bedrock-agentcore:CreateEvaluator`. A policy written against the endpoint prefix authorises
nothing and fails as an ordinary `AccessDeniedException`, which looks exactly like a missing grant.

### A1 — transport spans reach the Lambda — **PASS. The architecture gate is open.**

A **real production CareLoop trace** was read out of `aws/spans` and passed to a throwaway
TRACE-level code-based evaluator. The Lambda logged what it received:

```
spanCount 6   tracesInInput 1   targetTraceInSpans [true]
scopes:
   opentelemetry.instrumentation.httpx                      2   ← the POST and the GET
   opentelemetry.instrumentation.botocore.bedrock-runtime   2
   strands.telemetry.tracer                                 1
   <none>                                                   1   ← the X-Ray InternalOperation span
```

**Nothing is filtered.** All six spans arrived, including both httpx spans and both botocore spans.
The documented exclusion of `httpx`, `botocore` and the other transport scopes applies to the
service's *generic-framework classification* — how it finds prompts, responses and tool calls for an
LLM-judge evaluator — and **not** to what a code-based evaluator's Lambda is handed. A code-based
evaluator receives the supplied span set.

Attributes survive intact, which is the half that actually matters: the delivered `POST` span still
carries `http.method`, `http.url`, `http.status_code`, `http.response.status_code`, `scope.name`,
`parentSpanId`, both timestamps and `status`, byte-for-byte as stored.

**Spans are enriched, not passed through verbatim, and an evaluator must read the delivered shape.**
The service adds, per span: `trace_id`, `span_id`, `parent_span_id` (snake_case aliases beside the
camelCase originals), `start_time` and `end_time` as ISO strings, `duration_ms`, `events`,
`span_events`, `source` (observed value `"adot_cw"`), `session_id`, and `langfuse` (observed
`null` — a third-party vendor slot). None of this is documented. Reading `traceId` or `trace_id`
both work today; the evaluator should read the camelCase forms, which are the ones AWS's own
documentation and examples use.

### A2 — no `session.id` — **PASS. Not required.**

The real trace carries no `session.id` anywhere — CareLoop sets none, because the OTel-baggage
mechanism AWS documents for non-Runtime hosts is not wired and does not need to be. The call
succeeded with `evaluationTarget={"traceIds":[…]}` and the target resolved (`targetTraceInSpans
[true]`).

The service **substitutes a placeholder rather than rejecting**, and uses two different ones: the
delivered spans carry `session_id: "default_session"`, while the result's
`context.spanContext.sessionId` reads `"unknown"`. Neither is an error and neither needs handling.
**`session.id` is not mandatory for CareLoop's on-demand design**, and the baggage change §8 held in
reserve is not needed.

A second call with entirely **synthetic** spans and a fabricated trace id was also accepted, so the
API's willingness to take a hand-built span set is confirmed rather than assumed. Captured traces
remain the right input for the eventual settlement proof; this only settles what the substrate
permits.

### A3 — invocation authorization — **PASS, and the documentation is misleading**

Measured, in this order: `lambda:GetPolicy` on the function returned `ResourceNotFoundException`
(**no resource-based policy**); no AgentCore service execution role was created; `CreateEvaluator`
succeeded and went straight to `ACTIVE` **without validating invokability**; `Evaluate` then ran and
the Lambda executed — two invocations confirmed in its own CloudWatch log stream. `lambda:GetPolicy`
was re-checked afterwards and still returned `ResourceNotFoundException`, so **the service did not
create one either**.

So for **on-demand** evaluation, neither of the two mechanisms the documentation points at is
required. Neither a Lambda resource-based policy for `bedrock-agentcore.amazonaws.com`, nor an
`AgentCoreEvaluationRole`, existed at any point.

The only Lambda-invoke authority in existence was the **caller's own identity-based
`lambda:InvokeFunction`** on `careloop-evalspike-*`. That is consistent with on-demand `Evaluate`
invoking the evaluator's Lambda under the caller's credentials, and it is consistent with the API
shape: neither `Evaluate` nor `CreateEvaluator` has a role parameter — only
`CreateOnlineEvaluationConfig` takes `evaluationExecutionRoleArn`. **Stated as the honest limit of
the measurement:** this session could not revoke its own identity permission, so caller-identity
invocation is strongly indicated rather than isolated. What *is* isolated is the negative, and it is
the part the design needed: **no resource policy and no service role are required.**

The practical rule for Patch D/E: give the `Evaluate` caller `lambda:InvokeFunction` on the
evaluator's function. Do not provision an `AgentCoreEvaluationRole` for the on-demand path. The
code-based-evaluator documentation's `LambdaInvokeStatement`, attached to a *service execution
role*, describes the **online** path, where such a role genuinely exists.

### A4 — real HTTP attribute spellings — **PASS, and half of §8's prediction was wrong**

From a real production booking span in `aws/spans`, verbatim:

```
http.method                 = "POST"                                          old spelling ONLY
http.url                    = "http://…/clinic/appointments"                  old spelling ONLY
http.status_code            = 201   ┐
http.response.status_code   = 201   ┘                                         BOTH spellings
status.code                 = "UNSET"
scope.name                  = "opentelemetry.instrumentation.httpx"  version "0.65b0"
```

`http.request.method` and `url.full` are **absent**. §8 predicted the old spelling throughout, from
the pinned `OTEL_SEMCONV_STABILITY_OPT_IN`; method and URL match that prediction and **status does
not** — both spellings are emitted. An evaluator that read only `http.request.method` would match
nothing.

**`status.code` is `"UNSET"`, not `"OK"`, on successful spans.** This is the sharper finding: the
draft evaluator contract's success check was written as *2xx and `status.code` not `ERROR`*, which
survives `UNSET`, but a contract requiring `"OK"` would have failed every real CareLoop trace while
passing every hand-built fixture. Fixtures must carry `UNSET`.

The read-back's correlation premise is confirmed on real data: the `GET` span's URL is
`…/clinic/appointments/apt-c98a9981caefc548`, so the confirmation id is present in a span CareLoop
does not author.

Two structural facts for the eventual evaluator. The four child spans — both Bedrock calls, the
`POST` and the `GET` — all carry `parentSpanId` equal to `invoke_graph`'s `spanId`, so the trace
shape is exactly as designed. But **`invoke_graph` is not parentless**: Transaction Search wraps it
in a synthetic `InternalOperation` span with no `scope`, and `invoke_graph` carries
`aws.span.kind = LOCAL_ROOT`. An evaluator must not identify the root by absence of `parentSpanId`.

**The production privacy claims recorded in ADR 0004 hold in the raw span data**, checked here
because the data was in hand: both Bedrock spans carry `aws.auth.account.access_key = [redacted]`;
no `AKIA`/`ASIA`-shaped value appears anywhere in the trace; `gen_ai.input.messages` and
`gen_ai.output.messages` are absent; the graph span carries no events. This is an independent
confirmation from the exported bytes rather than from the exporting process.

### A5 — `NOT_APPLICABLE` with no `value` — **PASS**

The evaluator returned `{"label": "NOT_APPLICABLE", "explanation": …}` with no `value` key. The
`Evaluate` response came back with:

```
result keys : ['context', 'evaluatorArn', 'evaluatorId', 'evaluatorName', 'explanation', 'label']
label       : "NOT_APPLICABLE"
"value" in result : False
errorCode   : absent
```

**No coercion to `0.0`, no default, no error.** The omission survives the round trip, which is what
the vacuity design depends on: an N/A trace contributes no number to any average, at the schema
level rather than by convention.

### A6 — current SDK and API shape — **PASS, completed live**

§8's offline reading is confirmed by execution. `CreateEvaluator`, `GetEvaluator`, `Evaluate` and
`DeleteEvaluator` all succeeded against `bedrock-agentcore-control` (2023-06-05) and
`bedrock-agentcore` (2024-02-28) in `us-east-1` with the pinned boto3. No `agentcore` CLI and no
starter toolkit was installed or needed.

```
create_evaluator(evaluatorName='CareLoopEvalSpike', level='TRACE',
                 evaluatorConfig={'codeBased': {'lambdaConfig': {
                     'lambdaArn': …, 'lambdaTimeoutInSeconds': 30}}})
  → evaluatorId 'CareLoopEvalSpike-<10 chars>', status ACTIVE immediately
get_evaluator    → level TRACE, status ACTIVE, lockedForModification False, config echoed
evaluate         → evaluationResults[…]
delete_evaluator → status DELETING; GetEvaluator then ResourceNotFoundException
```

Confirmed live: the hyphen restriction is real — `evaluatorName` took `CareLoopEvalSpike` and the
service appended its own suffix to form `…/evaluator/CareLoopEvalSpike-uj4uzr7xyh`, so ARN-prefix
scoping works while a hyphenated *name* would not.

**The envelope AWS actually sends is smaller than documented.** Captured from the Lambda's own log:

```json
{"evaluationInput": {"sessionSpans": [...]},
 "evaluationLevel": "TRACE",
 "evaluationTarget": {"traceIds": ["..."]},
 "schemaVersion": "1.0"}
```

`evaluatorId` and `evaluatorName` are **documented as envelope fields and are not sent**;
`evaluationReferenceInputs` is absent when none is supplied rather than present and empty. An
evaluator must not key on either, and must use `.get()` for `evaluationReferenceInputs`.

No `tokenUsage` appeared on any code-based result, consistent with a code evaluator consuming no
model tokens.

### Resources and permissions: all temporary state removed

**Nothing from this spike remains.** It was torn down in two halves, by whoever held the authority
to do it, and each half was verified by a call that asks the service rather than by the deleting
call reporting success.

Created by the spike and deleted by it:

| Resource | Verification |
|---|---|
| Lambda `careloop-evalspike-fn` (python3.12, 128 MB, 30 s, stdlib only) | `GetFunction` → `ResourceNotFoundException` |
| Evaluator `CareLoopEvalSpike-uj4uzr7xyh` (TRACE, `codeBased`) | `GetEvaluator` → `ResourceNotFoundException` |

Provisioned externally and removed externally, outside this identity's remit:

| Resource | Verification |
|---|---|
| IAM role `careloop-evalspike-lambda-role` (and its inline policy) | deleted → `NoSuchEntity` |
| Log group `/aws/lambda/careloop-evalspike-fn` | deleted → log-group lookup returns `[]` |
| Managed policy `CareLoopEvalSpikeTemporary` | detached, then deleted → `NoSuchEntity` |

**The benchmark identity is back to what it was.** Its attached managed policies are again
`CareLoopAgentCoreDeploy` alone and its inline policies `careloop-bench-bedrock` alone. Confirmed
from this side by re-probing the three surfaces the temporary grant had opened: `lambda:GetFunction`,
`iam:GetRole` and `bedrock-agentcore:GetEvaluator` all answer `AccessDenied` again — the same
refusals §8 recorded, which is the shape of a grant that has genuinely gone rather than one that was
merely stopped being used. The spike log-group prefix returns `[]`.

**Production is untouched.** The four pre-existing log groups are byte-identical to the pre-spike
listing — `aws/spans` 20114 bytes / 30 days, `/aws/bedrock-agentcore/runtimes/careloop` 17545 bytes
/ 14 days, and the other two unchanged — so nothing this spike did wrote a span, moved a retention
setting or disturbed the trace data ADR 0004 rests on. No EC2 IAM, observability IAM, Transaction
Search setting, retention policy, security group, systemd unit or CareLoop deployment was changed at
all. `aws/spans` was read, never written.

### What this changes in the SettlementIntegrity design

Nothing structural. The nine-attribute contract stands as designed and unimplemented. Four
adjustments are now known and belong to Patch B rather than to a redesign:

1. Fixtures and the span accessor must use `http.method` / `http.url` (old spelling only) and accept
   either status spelling.
2. `status.code` on a successful span is `UNSET`. Success is *2xx and not `ERROR`*, never *is `OK`*.
3. The root is identified by `gen_ai.operation.name == "invoke_graph"` or
   `aws.span.kind == "LOCAL_ROOT"`, never by absence of a parent.
4. The Lambda reads the **delivered** span shape, tolerating the injected `session_id`, `source`,
   `langfuse`, snake_case aliases and ISO time fields, and must not require `evaluatorId` or
   `evaluatorName` in its envelope.
