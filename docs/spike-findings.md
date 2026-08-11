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

## 2. Policy gate — CONFIRMED, and better than planned

`BeforeToolCallEvent` has a first-class **`cancel_tool: bool | str`** field. Setting it to a string
prevents the tool from executing at all. The plan assumed we might have to substitute a stand-in
tool; that is unnecessary.

Verified empirically: a tool whose body appends to a module-level list was cancelled from the hook,
and the list stayed empty. The model then received:

```json
{"toolUseId": "tu_1", "status": "error",
 "content": [{"text": "DENY: policy rule comms.send_document.deny_untrusted_destination"}]}
```

Two consequences worth keeping:

- The denial reason is surfaced **to the planner as an error tool result**, so a denied plan feeds
  the bounded replan cycle naturally rather than dead-ending.
- Because the reason string reaches the model, it must never contain anything the model should not
  see. Rule `reason` fields are written for that audience.

**Layering (per correction 1):** the hook is the primary, demo-visible gate. Every external-effect
tool is *also* wrapped by a `@gated_tool` decorator that calls the same engine, so the invariant
holds even if the hook is never registered. The decision is computed once per
`(case_id, tool, args)` and cached for the duration of the call, so layering costs no double
evaluation.

## 3. `Agent.structured_output` — CONFIRMED

`Agent.structured_output(output_model: type[T], prompt=None) -> T`. Synchronous wrapper available;
`structured_output_async` also exists. This is the path for `TriageResult` and `PlannerIntent`.

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
