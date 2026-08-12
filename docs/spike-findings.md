# Spike findings — Strands Agents SDK surfaces

Pinned against **`strands-agents==1.51.0`**, Python 3.12.0, Windows.

These are the four SDK surfaces the CareLoop design depends on, plus the Bedrock model-id question.
Each was verified by executing code, not by reading signatures.

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
