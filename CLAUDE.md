# CLAUDE.md

Standing context for CareLoop. Read this before proposing or writing anything.

## Project

**CareLoop** — an autonomous administrative agent for family caregivers, built on the
**Strands Agents SDK** for the AWS **"Agents for Humans"** hackathon (Everyday Agents track,
submission **2026-09-14**).

Positioning: *an autonomous back office that acts, but never beyond its authority.* The interesting
claim is not that an agent can book an appointment — it is that a document telling the agent to
email everything to an attacker cannot make it happen, and that the audit trail proves it.

## Architecture invariants (non-negotiable)

These are load-bearing. If a change appears to require breaking one, stop and raise it rather than
working around it.

1. **Exactly two LLM agents: Triage and Planner.** Triage classifies an inbound event into typed
   `Case` fields. Planner proposes one next action as a typed tool intent. Everything else is
   deterministic Python or a typed tool. Adding a third agent is an architecture change, not an
   implementation detail.
2. **The LLM never holds authority.** A deterministic policy engine returns
   `ALLOW` / `DENY` / `REQUIRE_HUMAN` for every tool call, enforced at two independent points: the
   Strands `BeforeToolCall` hook (primary, via `cancel_tool`) and a `@gated_tool` decorator on every
   external-effect tool (backstop, holds even if the hook is never registered). Rules live in
   `rules.yaml`. Never encode a rule in Python.
3. **Trust model.** Instructions found in documents and email never gain authority. Facts promote
   `UNTRUSTED → CONTROLLED` through validation. Only `VALUE`-class arguments are promotable;
   `DESTINATION` and `AUTHORITY` never are, whatever the validator says. `TRUSTED` comes only from
   trusted config or a human decision — validation cannot manufacture it. Promotion is **not**
   idempotent. Every stored fact carries `{source_doc, page, trust_level}`.

   **Lowest-wins has one standing consequence, and it has now bitten three times:** trust resolves
   to the *lowest* level among every fact whose value matches, so **any path that can add a second
   fact at an existing `(key, value)` can silently downgrade trust.** Nothing raises, the timeline
   looks correct, and every action depending on that value is denied for a reason no diagnostic
   explains. Lowest-wins itself is not negotiable — it is what stops a poisoned duplicate being
   laundered by a legitimate twin. So the rule is about writers: a promotion **replaces** its
   original rather than appending beside it, a model never emits a fact, and `record_fact` refuses
   a value the case already holds. Any new writer of `case.facts` has to answer this question
   before it ships, and the answer belongs in a test that asserts through `resolve_trust` — "a
   `CONTROLLED` fact exists on the case" passes in both worlds and distinguishes neither.
4. **`Case` is frozen.** `transition()` is the only writer of `Case.state`. `DONE` is reachable only
   from `VERIFYING`, which makes "did not finish via the unsafe path" structural rather than
   observational.
5. **Loop bounds come from Strands graph primitives** — `set_max_node_executions`,
   `set_execution_timeout`, `set_node_timeout`. Never hand-roll a counter.
6. **Model-visible denial reasons come from a closed `ReasonCode` enum**, with no interpolation
   ever. No argument values, trust levels, rule ids, or allowlist contents reach the model. Full
   diagnostics go to the timeline only. Denial text is echoed into the planner's context, so an
   interpolated reason would be a self-inflicted injection channel.
7. **Every external-effect tool call carries an idempotency key** derived from
   `(case_id, action_type, canonical args)`. `case_id` is bound when the toolset is constructed and
   is never a tool argument — a model that could supply it could scope two identical actions apart,
   or collapse two cases' actions together.
8. **`CaseStore` is the durable source of truth** — state, evidence and provenance, approvals,
   action history, pending work, append-only timeline. Strands session persistence holds execution
   context only and is never consulted for business facts.
9. **No clock and no randomness anywhere in `careloop/tools/`.** Synthetic identifiers derive from
   the idempotency key; results and ledger entries carry no timestamp; time is stamped by whoever
   writes the timeline. A replayed action must return a *byte-identical* result, which is what
   makes T3 an assertion rather than an approximation — and the property dies the first time
   someone adds a `sent_at` for convenience. Enforced structurally: no module under `tools/` may
   import `datetime`, `time`, `random`, `secrets` or `uuid`, and no result field may carry a
   default or a `default_factory`.
10. **One canonical value form**, `provenance.canonical.canonical_value`, shared by the policy
    engine and by idempotency keys. The consumers differ only in totality: a value with no
    canonical form resolves `UNTRUSTED` for policy (fail closed) and *raises* for keying, since
    folding it onto a sentinel would collide two distinct actions into one suppressed duplicate
    (fail open).

## Working agreement

- **Plan first, wait for approval, then implement.** Report plan deltas explicitly; never adapt
  silently.
- **Multiple small commits per task.** Never push. Pushing is manual and happens only on explicit
  request.
- **Commit messages: one subject line.** No body, no bullet points, no trailers.
- **Tests green and `ruff` clean before moving to the next commit.** Never claim something works
  without a passing test; show failing output honestly.
- **Guards must be vacuity-proven.** For every defence, there is a test that fails when the defence
  is removed. A test that passes with the guard disabled was never testing the guard.
- **Tests run offline at $0 on the mock model.** Real providers are wired but never exercised in
  tests.
- Repo is **private now, public at submission**. Licence **Apache-2.0**.

## Verified environment facts

Pinned against **`strands-agents==1.51.0`**, Python 3.12, Windows. Verified by execution, not by
reading signatures. Full detail in [`docs/spike-findings.md`](docs/spike-findings.md).

- `BeforeToolCallEvent.cancel_tool: bool | str` cancels a tool natively — no tool substitution
  needed. The cancellation string is returned to the planner as an **error tool result**, which is
  what feeds the bounded replan cycle. It is therefore model-visible: see invariant 6.
- `GraphBuilder.add_node` rejects plain callables. Deterministic nodes subclass **`MultiAgentBase`**
  (one abstract method, `invoke_async`). `NodeResult.result` must be an `AgentResult`, a nested
  `MultiAgentResult`, or an `Exception` — a bare `str` fails deep inside the graph's input builder,
  not at the call site.
- `Model.structured_output` is an **async generator**, not a coroutine. It must
  `yield {"output": instance}`.
- Bedrock uses **region-prefixed inference-profile model ids** (`us.amazon.nova-lite-v1:0`). The
  bare `amazon.nova-lite-v1:0` fails on accounts that require a profile; the SDK derives the prefix
  the same way.

## Layout

```
careloop/
  agents/      Triage + Planner, their Pydantic output schemas and prompts
  graph/       GraphBuilder wiring, deterministic nodes, edge conditions, extraction
  policy/      ReasonCode enum, rules schema, engine, BeforeToolCall hook, rules.yaml
  provenance/  TrustLevel, ArgClass, Fact, promote()
  state/       Case, state machine, CaseStore protocol, SQLite + in-memory stores
  tools/       Tool registry (owns argument classes), mock executor, idempotency,
               document sources + fixtures/*.pdf, pdfplumber reader
  events/      Event simulator and fixtures/*.json
  llm/         Mock model provider; real Bedrock / OpenAI-compatible config
scripts/       generate_documents.py (renders the fixture PDFs), resume_demo.py
tests/
docs/
```

**Fixture PDFs are derived, never authored.** `careloop/tools/document_source.py` holds every line
of every document; `scripts/generate_documents.py` renders it deterministically; a test regenerates
and compares byte for byte. Editing a PDF directly puts a binary in the repo that nothing can
reproduce, which is the one thing the arrangement exists to prevent.

**A case leaves `DONE` only through `reopen_case`, and only an inbound event may call it.** No
graph edge exits `DONE`, and a sweep in `tests/test_reopen.py` fails if any node, agent, tool or
policy module names the function. The agent can never leave `DONE`; the world can.

**Argument classes belong to the tool registry**, not to `rules.yaml`. Two declaration sites means
one place to forget a `DESTINATION`. Rules may reference classes; they may never define them, and
load-time validation fails startup if a rule names a tool or argument the registry does not have.

## Commands

```bash
.venv/Scripts/python -m pytest -q      # offline, $0
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
```
