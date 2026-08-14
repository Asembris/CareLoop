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
- **`model_json_schema()` is not what the provider receives, and the difference is silent.**
  `BedrockModel.structured_output` converts the model through `convert_pydantic_to_tool_spec`,
  whose `_flatten_schema` keeps `type`, `properties`, `title`, `description` and `required` and
  **drops everything else, `$defs` included**. So a discriminated union in a wrapper object is
  transmitted as `oneOf` over `$ref` pointers with nothing to resolve them against, and the same
  union as a `RootModel` is transmitted as `{"type": "object", "properties": {}}` — a schema
  describing a tool that takes no arguments. **Neither raises.** Anything asserted about a schema
  a model must satisfy has to be asserted on the *emitted tool spec*; `tests/test_provider_tool_
  spec.py` does that, non-vacuously against both shapes above. This is session 5's "assert on the
  JSON schema, not on construction" run one layer further out, and it is the reason the planner
  schema is not a union.
- **AgentCore Runtime sends no `Content-Type` header to `/invocations`.** Measured, session E, on a
  deployed runtime. FastAPI binds a typed body only when the request is `application/json`, so a
  Pydantic-modelled `/invocations` returns **422 before any handler runs** — no log line, no
  traceback, and at the caller it surfaces as an opaque `RuntimeClientError (422)` that says
  nothing about its cause. Anything of CareLoop's put behind `/invocations` must read the raw
  request body and parse it itself. This is a constraint on the deployment shape, not a note.
- **AgentCore log groups default to `NEVER EXPIRE`.** `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`
  is created by the service on first invoke, and **every runtime creates its own**, so a redeploy
  under a new id leaves the old group accruing storage forever. Setting `logs:PutRetentionPolicy`
  is part of deploying, not part of tidying up.
- **Two once-per-account gates stand in front of the first `CreateAgentRuntime`, and neither is
  documented as a prerequisite** — AWS publishes `bedrock-agentcore:*` as the answer, so both are
  discoverable only by hitting them. The service-linked role
  `AWSServiceRoleForBedrockAgentCoreRuntimeIdentity` (service name
  `runtime-identity.bedrock-agentcore.amazonaws.com`) must exist, and the calling principal needs
  `bedrock-agentcore:{Create,Get,Update,Delete}WorkloadIdentity` on
  `workload-identity-directory/default` and `.../workload-identity/*`. Both are per *account*, not
  per runtime: a new account rediscovers them, an existing one never sees them again.
- **OpenAI-compatible providers reach structured output through strict Structured Outputs**
  (`client.beta.chat.completions.parse(response_format=...)`), where decoding is constrained so
  every required field *must* be emitted. A model on that path cannot fail to satisfy a schema the
  way a Bedrock model can, so **a cross-provider comparison cannot diagnose a schema**: hold the
  provider constant and vary only the shape. Measured consequence, session B: `gpt-4o-mini`
  satisfied the old flat schema 8/8 while misquoting `slot_start` 8/8 — well-formed proposals that
  the exact-match trust rule denies. Schema fit and transcription fidelity are separate axes and
  are reported as separate columns.
- **"Strict Structured Outputs" is a fact about OpenAI proper and does not transfer to every
  OpenAI-compatible gateway.** Measured, session G: NVIDIA's endpoint accepts a `json_schema` with
  `strict: true` whose *schema* is not strict-compliant — no `additionalProperties: false`, an
  incomplete `required` — and decodes it **unconstrained** rather than rejecting it as OpenAI does.
  The guard fails open and nothing reports it. Emit the compliant form yourself; do not rely on the
  endpoint to demand one. With the compliant form, `meta/llama-3.3-70b-instruct` and
  `nvidia/llama-3.3-nemotron-super-49b-v1.5` satisfied it 6/6 across both request shapes. The live
  risk on that endpoint is **capacity, not schema** — `503 ResourceExhausted (16/16 workers)` and
  120 s timeouts under modest concurrency.
- **A USD ceiling does not bound a credit-metered endpoint.** `build.nvidia.com` is metered in
  credits, not billed per token, so `bench/rates.json` prices it at `0.0` honestly and a
  dollar-denominated cap over it is decoration. Anything spending NVIDIA calls needs a ceiling
  counted in **calls**; `--max-spend` bounds only the Bedrock-backed target.
- **`deepteam==1.0.9` phones home at import and cannot be imported as distributed.**
  `deepteam/telemetry.py` runs `GET https://api.ipify.org` and `sentry_sdk.init(...)` against a
  hard-coded DSN unless `DEEPTEAM_TELEMETRY_OPT_OUT=YES` is already set — so the opt-out belongs
  *above the import statement*, not in the shell. It also imports `sentry_sdk` without declaring
  it, so the `evals` extra pins `sentry-sdk` to make the install work at all. Full detail in
  [`docs/spike-findings.md`](docs/spike-findings.md) §7.

## Layout

```
careloop/
  agents/      Triage + Planner, their Pydantic output schemas and prompts
  graph/       GraphBuilder wiring, deterministic nodes, edge conditions, extraction
  policy/      ReasonCode enum, rules schema, engine, BeforeToolCall hook, rules.yaml
  provenance/  TrustLevel, ArgClass, Fact, promote()
  state/       Case, state machine, CaseStore protocol, SQLite + in-memory stores
  tools/       Tool registry (owns argument classes), executor, idempotency, the Demo World
               clinic client, document sources + fixtures/*.pdf, pdfplumber reader
  events/      Event simulator and fixtures/*.json
  llm/         Mock model provider; real Bedrock / OpenAI-compatible config
  api/         FastAPI read surface + approve/reject/deliver; projections and withholding;
               the demo control surface, mounted only when create_app is asked for it
demoworld/     A synthetic clinic in its own process. Never imports careloop.
scripts/       generate_documents.py (renders the fixture PDFs), resume_demo.py, demo_serve.py
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

**`careloop/api/` decides nothing.** It projects stored cases and calls `open_case`, `reopen_case`,
`approve`, `reject` and `run_case`. The one thing it originates is *when* a run happens, and
scheduling is not deciding. An endpoint that appears to need logic which does not exist is a signal
the logic belongs in the graph layer — that is where `latest_delivery` and `proposal_for` came from.

**Invariant 6 over HTTP moves axis, and the reason is the consumer.** Invariant 6 withholds rule
ids and trust levels from the *model* because denial text re-enters the planner's prompt. A browser
proposes nothing and nothing it renders re-enters a prompt, so fact trust levels, citations and
`matched_rule_id` are exposed on purpose — they are the demo. What the API withholds instead is
**`TimelineEvent.diagnostic`, wholesale**: unbounded free text, a dozen writers, no constraint on
the next one, and its own docstring promises it may carry raw arguments and rule internals.
Withheld rather than filtered, because a filter over free text is the sanitiser this project
refuses everywhere else. `payload` is projected through an **allowlist**, so a new payload key is
invisible until somebody decides about it.

**Response models project; they never restate.** Two import-time checks in
`careloop/api/projections.py`: every stored field is either projected or listed in `WITHHELD` with
a reason, and every response field either names the stored field it comes from or is listed in
`DERIVED`. Response fields carry no default and no `default_factory` — invariant 9's rule for tool
results, here so a response can only say what the projector gave it.

**One tool is real, and the read-back is what makes that worth anything.** `book_appointment` routes
over HTTP to `demoworld/`; the other three effects stay simulated, because one action made genuinely
external is the proof and four is scope creep with a rationale. Verification then performs a
*separate* `GET` and compares observed clinic state against the authorised arguments — never the
booking's own response, which would be re-reading CareLoop's receipt.
`tests/test_verify_readback.py::TestTheIndependenceProof` is the vacuity proof for the whole
arrangement: it moves the appointment behind CareLoop's back and requires verification to fail while
showing the pre-read-back checks still pass. **Anything that makes the read-back cheaper by reusing
what the effect returned deletes the only thing this buys.**

Two structural decisions hold it up. `RunContext` takes a **clinic or an executor, never both** —
one collaborator turns on the real booking *and* the read-back that observes it, so a real effect
verified against a simulated read cannot be built. And the read-back is **deliberately not a
registry tool**: it is never proposed, produces no fact, and its only sink is a timeline-only
diagnostic the API withholds, so registering it would buy a new proposable tool to satisfy a
symmetry that does not apply. `demoworld/` may never import `careloop`, or reading it back stops
being evidence.

**`CaseStore.clear` has exactly one caller**, `POST /api/demo/reset`, which exists only when
`create_app` is asked for the demo surface. It is the one destructive method on the durable source
of truth; a sweep in `tests/test_api_demo.py` fails if a second caller appears, and the reset itself
is refused while any run is in flight.

**The SQLite store is shared across threads and serialised by a lock.** A served CareLoop reaches it
from Starlette's worker pool, and every method of the protocol carries `@serialised` — the writes
are explicit `BEGIN IMMEDIATE` … `COMMIT` sequences on one connection, so two interleaving would
commit half of each. The lock does not replace the conditional update, which is still what rejects a
losing writer for a second process the lock knows nothing about.

**The `X-Caregiver` header is an audit identity, not authentication, and a missing one is `400` —
never `401`.** Nothing is verified and anyone reaching the port can claim any name; `401` would
claim a check that is not happening, while `400` says what is true, which is that a required field
is absent. Do not "upgrade" this to `401` for looking more professional. Real authentication sits
in front of this layer rather than replacing it.

## Commands

```bash
.venv/Scripts/python -m pytest -q      # offline, $0
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
```
