# Architecture

The diagram is the deliverable; this page is its caption. Depth lives in the ADRs and
[`session-log.md`](session-log.md).

[![CareLoop architecture: three trust zones, one policy engine, an independent read-back](architecture.svg)](architecture.svg)

*Click to open full size. Hand-authored SVG — it is text, so it diffs and it is edited rather than
re-exported.*

## The three zones

**UNTRUSTED — what the world sends, and what the models say.** Inbound email, its PDF attachments,
the text pdfplumber pulls out of them, and — deliberately in the same zone — `TriageResult` and
`PlannerIntent`. A model's output is data: typed, read, and gated exactly like an attacker's
document. Instructions found in either never gain authority, however they are phrased. This is the
zone that makes the central claim testable: a continuity notice telling the agent to email records
to an outside address is *read*, and it changes nothing about what may happen.

**CONTROLLED — what validation has earned the right to promote.** A `Fact` carries
`{source_doc, page, trust_level}` and may be promoted `UNTRUSTED → CONTROLLED` by deterministic
validation. Only `VALUE`-class arguments are promotable at all; `DESTINATION` and `AUTHORITY` —
recipients, addresses, URLs, permission changes — never are, whatever a validator says. Trust
resolves to the *lowest* level among facts matching a value, so a poisoned duplicate cannot be
laundered by a legitimate twin.

**TRUSTED — application code, the policy engine, `rules.yaml`.** Trust is never manufactured by
validation. It comes from configuration a person wrote, or from a person pressing approve.

Zone colours are claims about *authority*, not about code quality. UNTRUSTED does not mean badly
written; it means nothing there may decide anything, whoever wrote it.

## Two agents, and why not three

**Triage** classifies an inbound event into typed `Case` fields. **Planner** proposes one next
action as a typed tool intent. That is every model call CareLoop makes. Everything else —
extraction, validation, promotion, gating, dispatch, verification, state — is deterministic Python
or a typed tool.

The constraint is load-bearing rather than minimalist. Each agent is a `Model.structured_output`
call against a schema; neither owns a tool, so neither can act. Adding a third agent is an
architecture change, argued as one. Three fields are deliberately *not* extracted at all — a
referral's clinical reason as prose, whether a letter is informational or is asking for something,
and a provider's identity from letterhead — because they cannot be done deterministically, and
reaching for a model to read them would be that same architecture change slipped in as an
improvement to extraction.

Whether the proposal agents should be `strands.Agent` instances was evaluated by execution and
rejected: [ADR 0001](adr/0001-strands-agent-for-the-proposal-agents.md).

## Where the gate sits

One engine, one `rules.yaml`, three call sites — the `gate` node before execution, the Strands
`BeforeToolCall` hook at dispatch, and the `@gated_tool` wrapper on every external-effect tool. The
two at the tool boundary are independent: `tests/test_case_run.py` runs the attack with only the
hook, then with only the wrapper, then with neither — and the last one lands, which is what makes
the first two mean anything.

Argument classes belong to the **tool registry**, not to `rules.yaml`; rules may reference a class
but never define one, and load-time validation fails startup if a rule names a tool or argument the
registry does not have. What comes back to the planner after a refusal is a closed `ReasonCode` and
nothing else — no argument values, no trust levels, no rule ids — because denial text re-enters the
planner's prompt, and an interpolated reason would be a self-inflicted injection channel.

**One disclosure, stated because the imprecise version would be an overclaim.** CareLoop fires the
SDK's real `BeforeToolCallEvent` **itself, from its own execute node, against a real
`HookRegistry`**. An SDK event loop does not fire it, because there is no event loop to fire it:
both agents drive the model provider directly and neither holds a tool. The event type, the
registry and the `cancel_tool` contract are genuine SDK machinery, and the hook provider would plug
unchanged into a `strands.Agent` the day one exists. What is CareLoop's is the *firing*.

Why there is no probabilistic guardrail in front of any of this:
[ADR 0003](adr/0003-no-runtime-guardrail-layer.md).

## The read-back

`DONE` is reachable only from `VERIFYING`. Verification means one specific thing: **the effect
performed is the effect authorised** — the idempotency key re-derived from the approved arguments
equals the key the action was recorded under, and every field the typed result echoes back equals
the argument policy allowed.

For `book_appointment` it means more, and the diagram draws that as a separate arrow on purpose.
Verification performs its **own `GET`** against Demo World and compares what the clinic holds now
against what policy authorised — never the booking's own response, which would be re-reading
CareLoop's receipt. Move the appointment behind CareLoop's back and every closed-loop check still
passes while verification fails; `tests/test_verify_readback.py::TestTheIndependenceProof` is that
sentence as a test.

**What it does not prove.** It checks the effect performed against the effect authorised. It does
not prove the outside world did the right thing, that the appointment suits the patient, or that
the clinic will honour it. The other three effects are simulated, and for those verification closes
the loop between decision and execution rather than between execution and the world. `read_back` on
a `VERIFICATION_PASSED` entry is `true` only when an external system was actually read, so the
weaker claim can never be read as the stronger one.

## Deployed

One `t4g.small` behind CloudFront, serving `/` and `/api/*` from uvicorn on `:8000` and `/clinic/*`
from `demoworld` on `:8100`. **One origin, so no `CORSMiddleware` exists anywhere in `careloop/`.**
Two processes, because Demo World is a system CareLoop does not own and `demoworld/` may never
import `careloop` — routing both through one CDN does not couple them. Bedrock is reached through
an instance profile; there are no keys on the box.

Every graph node commits its transition to the store before it finishes, which is what lets a
browser polling `GET /api/cases` watch a case move. Moving graph execution onto Bedrock AgentCore
Runtime was planned, costed and measured against a deployed runtime — 6.47 s cold, 0.72 s warm —
and rejected because a single terminal commit takes that away:
[ADR 0002](adr/0002-graph-execution-stays-on-the-host.md). The `GraphRunner` seam is built anyway,
so the rejection is falsifiable rather than rhetorical.

---

| | |
|---|---|
| the invariants, in full | [`../CLAUDE.md`](../CLAUDE.md) |
| what was measured and when | [`session-log.md`](session-log.md) |
| SDK behaviour verified by execution | [`spike-findings.md`](spike-findings.md) |
| the adversarial evidence | [`security-evaluation.md`](security-evaluation.md) |
| which model, and why | [`model-benchmark.md`](model-benchmark.md) |
