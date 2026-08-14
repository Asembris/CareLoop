# Spike findings — SDK and tooling surfaces

Pinned against **`strands-agents==1.51.0`**, Python 3.12.0, Windows.

Sections 1–6 are the four SDK surfaces the CareLoop design depends on, plus the Bedrock model-id
question. Section 7 is `deepteam==1.0.9`, added in session G for the adversarial evaluation under
`evals/`. Each was verified by executing code, not by reading signatures.

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
