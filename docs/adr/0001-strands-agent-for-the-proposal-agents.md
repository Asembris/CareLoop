# ADR 0001 — `strands.Agent` for the proposal agents

**Status:** Rejected
**Date:** 2026-08-12
**Pinned against:** `strands-agents==1.51.0`, Python 3.12, Windows
**Evidence:** [`scripts/spike_agent.py`](../../scripts/spike_agent.py),
[`docs/live-runs/spike-agent-live.json`](../live-runs/spike-agent-live.json)

## Context

Commit 5 dropped `strands.Agent` for direct `Model.structured_output` on two grounds:
`Agent.structured_output` is deprecated, and its replacement forces an internal
`StructuredOutputTool` through the event loop where CareLoop's deny-by-default gate would refuse
it. Both objections were real and both still hold.

What neither had evaluated is the **non-deprecated modern path** —
`invoke_async(prompt, structured_output_model=Schema)` reading `AgentResult.structured_output` — on
agents owning **zero tools**, with the policy hook **never attached to them**. If those agents own
no effectful tools, there may be nothing for the gate to collide with, and the original objection
may dissolve.

The reason to ask is not aesthetic. Technical Implementation asks how thoroughly the project uses
Strands Agents, and a judge grepping for `strands.Agent` currently finds nothing.

Six probes, run against `MockModel` and against Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`). Every
claim below was produced by execution against the pinned version, not by reading the docs site.

## Decision

**Rejected.** The proposal agents stay on `careloop/agents/invoke.py` driving
`Model.structured_output` directly.

Three of the eight gate criteria fail, and one of the three fails badly enough on its own.

## The gate

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Authority boundary unchanged | **FAIL** | probe 3 — a tool reaches `BeforeToolCall` on a zero-tool agent |
| 2 | Zero cross-case leakage | **FAIL as written**, passes only under fresh-agent-per-invocation | probe 2 |
| 3 | Zero effectful tools owned | pass | probe 3 — the registered tool is SDK-internal, not effectful |
| 4 | Structured output at least as reliable | pass on this sample | probe 1 — valid `TriageResult` and `PlannerIntent`, one call each |
| 5 | Graph integration stays clean | not reached | — |
| 6 | Suite green | **FAIL** | probe 1 — the shipped `MockModel` cannot drive this path at all |
| 7 | No material cost or latency regression | pass | probe 4 — +30 input tokens (~2%), latency within noise |
| 8 | Genuinely more idiomatic, not cosmetic | partial | see below |
| + | **Cost of migration to `MockModel`** | **the deciding row** | 57 lines of new emission logic in the object all 1408 tests depend on |

### Criterion 1 — a tool does reach the hook, and denying it is a paid runaway

An `Agent` constructed with `tools=[]` and invoked with `structured_output_model=TriageResult`
registers a **dynamic tool named after the Pydantic class** — `TriageResult` — for the duration of
the invocation, offers its spec to the provider, and executes it as an ordinary tool. Measured:

```
agent.tools declared:                              []
tool registry before invocation:                   empty
tool names that reached BeforeToolCallEvent:       ['TriageResult']
tool spec names offered to the provider:           ['TriageResult']
tool registry after invocation:                    empty (cleanup runs)
```

So "the agents own no tools" would stop being true. It would become "the agents own no tools
except during an invocation, when they own one, named after a schema."

Today CareLoop's hook is not attached to these agents, so nothing would fire. That is a
configuration away from being false, and **the failure mode when it becomes false is not a clean
denial.** A hook that cancels the structured-output tool — which a deny-by-default engine holding
no rule for `TriageResult` would do — leaves the event loop without the output it is waiting for,
so it forces again. Unbounded, against Nova 2 Lite:

```
with a deny-everything hook attached: EventLoopException — maximum recursion depth exceeded
deny-everything run made 245 provider call(s) and the hook fired 244 time(s)
```

**245 paid calls before Python's recursion limit ended it.** Not an exception, not a refusal — a
billed runaway that terminates on a stack overflow. Invariant 5 says loop bounds come from Strands
graph primitives; this loop is inside a single node execution, where no graph primitive can see it.

The probe now caps that sub-probe at three turns, which produces `stop_reason=limit_turns` and
`structured_output=None`. That is the *bounded* failure, and it is available — but it means an
invocation-level limit becomes load-bearing for a safety property, on a path that exists only to
parse a schema.

### Criterion 2 — leakage is real, and the fix is what we already have

Two unrelated cases, each carrying a canary token, measured on **what the provider received**
rather than on `agent.messages`:

| Pattern | Result |
|---|---|
| One `Agent` reused across both cases | **LEAKS** — case A's canary appears in case B's request |
| A fresh `Agent` per invocation, one shared model object | clean |
| `NullConversationManager` | **LEAKS** |

Identical offline and live, so it is the SDK resending prior turns rather than a model repeating
them.

**`NullConversationManager` does not do what its name suggests, and this is worth recording
whatever the verdict.** Its `apply_management` is a no-op that *preserves the full history*. It is
the "manage nothing" manager, not the "remember nothing" manager — the opposite of what it sounds
like for this purpose. Read from source and confirmed by execution.

The pattern that passes is a fresh `Agent` per invocation. That is achievable — and it is worth
naming what it amounts to: constructing a throwaway object per call, holding no state between
calls, to make one structured-output request. `careloop/agents/invoke.py` is that, without the
object.

### Criterion 6 — the shipped `MockModel` cannot drive this path

The forced structured-output path requires the model to emit `toolUse` stream events.
`MockModel.stream` emits text only. Run against it unmodified:

```
shipped MockModel: StructuredOutputException after 2 stream call(s)
  — The model failed to invoke the structured output tool even after it was forced.
```

`ToolEmittingMockModel` in the spike is what it takes to fix that: **57 lines** of new emission
logic — tool-spec matching, `toolUse` block assembly, a use-id counter, and a fall-through to the
existing text behaviour so an exhausted script still fails the way it does today.

Those 57 lines would land in `careloop/llm/mock.py`. That file is the object **every one of 1408
tests depends on**, and its strictness — an exhausted script raises, a wrong-typed answer raises, a
scripted exception is raised — is load-bearing for the reason session 4 recorded: each of those is
a test that would otherwise pass for the wrong reason. Adding a second emission path to it is a
scoped session's work with its own mutation pass, not the tail of a spike.

This is not one of the eight criteria as written. It belongs under "no material regression", and it
is the deciding row.

### Criterion 7 — cost and latency are genuinely fine

Measured live, one triage call each:

| Path | Provider calls | Tokens in/out | Latency |
|---|---|---|---|
| Direct (`Model.structured_output`) | 1 | 1526 / 82 | 1431 ms |
| Agent (`structured_output_model=`) | 1 | 1556 / 82 | 1585 ms |

Nova 2 Lite emitted the tool call on the first attempt, so the forced-mode re-prompt at
`event_loop.py:361` never fired and no request carried a `tool_choice`. The +30 input tokens are
the `StructuredOutputTool` spec. **Neither cost nor latency is a reason to reject**, and saying so
matters: the rejection rests on the authority boundary and the mock, not on a performance claim the
data would not support.

### Criterion 8 — idiomatic, partially

Using `strands.Agent` would be more recognisably idiomatic and a grep for it would succeed. Against
that: CareLoop already uses the SDK's `GraphBuilder`, its graph bounds, its `MultiAgentBase` node
contract, its real `BeforeToolCallEvent` and `HookRegistry`, and its `Model` provider interface.
The claim that the project does not use Strands thoroughly is answerable on those, without putting
an event loop behind a call whose entire job is to parse one schema.

## Two things settled explicitly, either way

**Migrating the proposal agents would not make the SDK's event loop fire the policy hook.** These
are separate concerns and conflating them is the trap. The effectful tools — `send_email`,
`book_appointment`, `upload_document`, `grant_portal_access` — belong to no agent. They are
dispatched by `careloop/graph/nodes.py`, which fires a real `BeforeToolCallEvent` against a real
`HookRegistry` itself. An agent owning zero effectful tools generates no `BeforeToolCall` event for
any of them, before or after this migration. The only tool that would newly reach a hook is
`StructuredOutputTool`, which is the problem rather than the goal.

**Making the executor a `strands.Agent` owning the effectful tools is REJECTED, and not for
reasons that expire.** It is the apparent fix — the SDK would then fire the hook natively, and
"Strands blocks the attack" would become sayable. It puts an LLM in the execution path, deciding
which tool to call, after policy has already decided what may happen. That trades invariant 2 —
the LLM never holds authority — for a narrative about SDK depth. The current arrangement is
disclosed honestly in `policy/hook.py`, the README and `spike-findings.md` §2: **CareLoop fires the
SDK's real hook from its own node.** An accurate description of a sound design beats an impressive
description of an unsound one, and this project cannot afford a claim someone can check and find
inflated.

Written down here so a later session finds it before rediscovering it as a good idea.

## What would change the answer

The rejection is falsifiable. It would be worth revisiting if:

- The SDK grows a structured-output path that does not route through a tool — then criterion 1
  dissolves rather than being worked around.
- A cancelled structured-output tool comes to fail cleanly and boundedly instead of re-prompting to
  a stack overflow — that alone would demote criterion 1 from "fail" to "watch".
- `MockModel` acquires `toolUse` emission for an independent reason, so criterion 6 is already
  paid for.

Nothing here is about the modern path being deprecated, unavailable or broken. It works: it
returned a valid `TriageResult` and a valid `PlannerIntent` on the first attempt against a real
model, with no deprecation warning. It is simply not worth what it costs here.

## Consequence for open decision 16

Probe 6 answers the blocking unknown. **Token usage is available on the path CareLoop already
runs**, not only on the agent path:

```
AgentResult.metrics.accumulated_usage:  {'inputTokens': 1556, 'outputTokens': 87, 'totalTokens': 1643}
direct path, from the stream metadata:  {'inputTokens': 1526, 'outputTokens': 84, 'totalTokens': 1610}
```

`Model.structured_output` wraps its stream in an `{"event": ...}` envelope, so the usage sits one
level down rather than at the top — which is why a first pass of the probe reported zero for the
direct path and had to be corrected. Cost per case therefore does **not** require migrating to
`strands.Agent`. It requires the graph layer reading usage off the envelope it already consumes.
The remaining questions in open decision 16 are unchanged: whether usage belongs on `Case` under
invariant 8, and what a mock run should report rather than zero.
