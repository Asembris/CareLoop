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
   trusted config — `careloop/graph/trusted.py`, and nothing else — and validation cannot
   manufacture it. Promotion is **not** idempotent. Every stored fact carries
   `{source_doc, page, trust_level}`.

   **A human decision is not a second source of `TRUSTED`, and this sentence said it was.** It read
   "trusted config or a human decision" until session W, and four other files had copied it: the
   `TrustLevel.TRUSTED` docstring, `graph/trusted.py`, `docs/architecture.md` and the submission
   diagram, plus four `rules.yaml` diagnostics. Nothing in the code ever behaved that way —
   `promote()` refuses `TRUSTED` outright and `trusted_facts()` is the only producer — so this was
   a documentation defect throughout, and the reason to fix it at the source rather than where an
   auditor happened to look is that it demonstrably regrows. What an `Approval` does is release one
   exact `(tool_name, args_digest)`; it writes no `Fact`, and no fact's trust level moves when a
   person presses approve. Deterministic trust and human approval are separate dimensions read by
   separate predicates (`arg_trust_at_least` against `case.facts`, `args_approved` against
   `case.approvals`) — which is precisely why the session-N rule below says an approval can never
   satisfy a requirement about *where* an effect lands.

   **Trust is bound to `(fact key, canonical value)`, never to a literal on its own.** An argument
   draws trust only from facts recorded under the fact key it declares in the tool registry
   (`ArgSpec.trust_key`); an argument declaring no key resolves `UNTRUSTED` outright, and a rule
   requiring trust of one fails at load. There is **no value-only fallback anywhere**, and adding
   one would reopen the whole hole. Session I found what the missing half cost: with matching by
   value alone, a validated `patient_id` satisfied `upload_document.document_id`, a `document_id`
   satisfied `book_appointment.slot_start` and reached the clinic over HTTP, and — the serious one
   — the configured `portal_id`, named as a *role*, resolved `TRUSTED` and defeated
   `deny-authority-grant-below-trusted`, the system's only `AUTHORITY` guard, on a case carrying
   nothing but trusted configuration. No promotion was involved in any of them, which is why the
   "AUTHORITY is not promotable" argument did not cover it. The planner's own prompt renders every
   fact's key *and value*, so it is handed the configured literals and can put any of them in any
   argument.

   **Lowest-wins is scoped to one key, and that scoping is the invariant.** Trust resolves to the
   *lowest* level among every fact sharing the argument's fact key **and** canonical value, so
   **any path that can add a second fact at an existing `(key, value)` can silently downgrade
   trust.** Nothing raises, the timeline looks correct, and every action depending on that value is
   denied for a reason no diagnostic explains. Lowest-wins itself is not negotiable — it is what
   stops a poisoned duplicate being laundered by a legitimate twin:

   ```
   provider_id = X TRUSTED  +  provider_id = X UNTRUSTED   →  UNTRUSTED   (a duplicate; caught)
   provider_id = X TRUSTED  +  patient_id  = X UNTRUSTED   →  TRUSTED     (not a duplicate at all)
   ```

   Cross-key contamination was never protective — it was an accident that also made every
   configured literal a false-denial hazard if a document happened to repeat it. Do **not** rely on
   literals being globally unique as a security property; keeping trusted values distinct from
   document values remains sensible as defence in depth and nothing depends on it.

   So the writer rule stands, scoped the same way: a promotion **replaces** its original rather
   than appending beside it, a model never emits a fact, and `record_fact` refuses a `(key, value)`
   the case already holds. `promote_fact`'s shadowing check must stay exactly as wide as
   `resolve_trust` — no wider and no narrower. Any new writer of `case.facts` has to answer this
   before it ships, and the answer belongs in a test that asserts through `resolve_trust` — "a
   `CONTROLLED` fact exists on the case" passes in both worlds and distinguishes neither.
4. **`Case` is frozen.** `transition()` is the only writer of `Case.state`. `DONE` is reachable only
   from `VERIFYING`, which makes "did not finish via the unsafe path" structural rather than
   observational — and `VERIFYING` reaches no state from which an effect can be proposed, which
   makes "did not act again on an outcome it could not certify" structural in the same way. No
   post-effect state reaches `PLANNING`: `EXECUTING`, `WAITING_EXTERNAL` and `VERIFYING` all refuse
   it, so "did not replan on top of a dispatched action" is one property of the table rather than
   three places to remember.
5. **Loop bounds come from Strands graph primitives** — `set_max_node_executions`,
   `set_execution_timeout`, `set_node_timeout`. Never hand-roll a counter.
6. **Model-visible denial reasons come from a closed `ReasonCode` enum**, with no interpolation
   ever. No argument values, trust levels, rule ids, or allowlist contents reach the model. Full
   diagnostics go to the timeline only. Denial text is echoed into the planner's context, so an
   interpolated reason would be a self-inflicted injection channel.
7. **Every external-effect tool call carries an idempotency key** derived from
   `(case_id, action_type, canonical args)`. `case_id` is bound when the toolset is constructed and
   is never a tool argument — a model that could supply it could scope two identical actions apart,
   or collapse two cases' actions together. That key is **durably frozen before anything can leave
   the process**, in the same commit that enters `EXECUTING`, so a later process reuses the identity
   of an attempt it never saw rather than deriving a new one. It serves as both the logical action
   identity and the provider idempotency key, deliberately: two values derived identically would be
   two answers to one question. A run id, a trace id or a process id is none of these and may never
   become one.
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
  graph/       GraphBuilder wiring, deterministic nodes, edge conditions, extraction,
               recovery classes and the dispatch seam
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
evaluators/    The AgentCore code evaluator. Standard library only, imports no careloop, not in
               the wheel; the Lambda is packaged from these five modules and nothing else.
scripts/       generate_documents.py (renders the fixture PDFs), resume_demo.py, demo_serve.py,
               export_diagram.py (renders docs/architecture.{png,pdf} from the SVG),
               observability.py + witness_span.py (the deployment-layer telemetry seam),
               evaluations_deploy.py + evaluations_prove.py (the AWS evaluator and its proof)
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

**A case leaves a post-effect handoff only through `request_recheck`, and only a person may call
it.** Same arrangement, same reason: `tests/test_recheck_writers.py` fails if any agent-facing
module names it, and a second sweep pins `graph/runner.py` and `graph/nodes.py` as the only writers
of `transition(..., VERIFYING)`. The agent can never ask to look again; a caregiver can.

**A prepared attempt is re-claimed only by `ReconcileNode`, and only from a state that means the
outcome is unknown.** `WAITING_EXTERNAL → EXECUTING` puts a case back in front of the executor, so
`tests/test_reconcile_writers.py` is the third instance of the same arrangement: the table says the
move is legal, and a sweep says exactly one node can make it. Two more sweeps sit beside it — one
function is the only writer of `WAITING_EXTERNAL`, and the recovery path may not name a model seam.

**Argument classes belong to the tool registry**, not to `rules.yaml`. Two declaration sites means
one place to forget a `DESTINATION`. Rules may reference classes; they may never define them, and
load-time validation fails startup if a rule names a tool or argument the registry does not have.

**A rule that names an argument without requiring its trust is not guarding it.** Session J: every
booking argument had a correct `trust_key`, `resolve_trust` returned the right answer for every one
of them, and `allow-booking-on-validated-slot` guarded `patient_id` with `args_present` — which the
engine implements as "not absent and not empty". So the engine *computed* the trust and no rule read
it, and a booking with a configured provider and a validated slot reached `ALLOW` carrying an
invented identifier, one borrowed from an unrelated fact key, one that never validated, or the real
one misquoted. `book_appointment` is the one tool whose effect is genuinely external, so all four
reached the clinic over HTTP and were recorded there as the person being seen; Demo World validates
the slot and the idempotency key and never the patient. This was **independent of session I** —
that repair decides how trust is computed, and this was whether it is required.

So: **the exact patient identifier must be deterministically established by the current case at
`CONTROLLED` or higher before CareLoop books, and the case cannot reach `DONE` if the clinic reports
a different one.** `deny-booking-for-unverified-patient` is the first half and the read-back's
existing `patient_id` comparison is the second; `tests/test_booking_authority.py` and
`tests/test_verify_readback.py::TestTheAuthorisedPatientIsCheckedOnTheWayOutToo` pin them, and a
targeted mutant that restores presence-only semantics — and changes nothing else — proves the first
is what refuses those calls. `CONTROLLED`, not `TRUSTED`: nothing produces a trusted patient, so
requiring it would make booking unreachable. The presence clause was removed rather than kept
alongside, because the trust clause strictly dominates it and the weaker one is what let the
identifier through. **`args_present` is a presence check and nothing more; any new rule about who or
what an action affects belongs in `arg_trust_at_least`.**

**A rule that does not name an argument at all is not guarding it either, and that is where the
next one was.** Session N: `grant_portal_access.portal_id` is `DESTINATION`-class and declares
`trust_key: portal_id`, so `resolve_trust` had always returned the right answer for it — and no
rule read the answer. No `DENY` rule named it and `allow-approved-portal-access-grant` did not
either. With a trusted grantee, a trusted role and a caregiver's approval covering the exact call, a
grant on `PORTAL-ATTACKER-CONTROLLED` reached `ALLOW` while that same `Decision` reported
`portal_id: UNTRUSTED`. Session J's shape one class further out: there the argument was guarded by
presence instead of by trust, here by nothing.

It was unreachable in the shipped configuration — but only because `deny-authority-grant-below-
trusted` refuses every grant for want of a trusted `role`, which is **a different rule guarding a
different argument**. An accident is not a boundary, and this one is one deliberate change away from
being gone: the moment anybody invents a trusted role to make the grant path reachable, they have no
reason to look at `portal_id`.

So: **every `DESTINATION` and `AUTHORITY` argument of every external-effect tool must have its trust
required at `TRUSTED` by every `ALLOW` rule that can authorise that tool.** Both halves are written,
as with the booking patch and for the opposite reason — the `DENY` states the refusal, and the
`ALLOW` clause is what stops the positive path depending on the `DENY` still being there tomorrow.
Neither `args_present` nor `args_approved` satisfies this: human approval and deterministic trust
are different dimensions, and an approval says a caregiver was asked about one exact call, never
where that call lands. The scope is deliberately narrow — `VALUE` is excluded, because `patient_id`,
`slot_start` and `document_id` are required at `CONTROLLED` on purpose and `subject`, `body` and
`reason` declare no `trust_key` at all, so requiring their trust fails at load.

**The production policy loader rejects a ruleset that violates this coverage.** Loading is three
named steps in `careloop/policy/loader.py` — `parse_rules` (schema), `validate_rules_resolve` (does
each rule mean anything against the registry), `validate_authority_coverage` (does the ruleset leave
an authority-bearing argument unguarded) — and `load_rules_from_mapping` composes all three, with
every other loader funnelling into it. So an unguarded destination is an outage at startup rather
than a gap somebody notices later. There is **no parameter anywhere that turns the third step off**;
`uncovered_authority_arguments` remains available as the pure diagnostic behind it.

**The allow-all security-evaluation mutant is intentionally not production-loadable.** After this
invariant, "the rules permit everything" and "the ruleset is invalid" are the same statement, so
`evals/replay.py::permissive_engine` composes the three steps by hand and omits exactly the third —
its violation being the property that mutant exists to measure. Schema, tool and argument
resolution, trust keys and argument classes all still apply to it. That omission is one missing call
at one call site rather than a flag, because a flag is something the next caller can pass and an
omission is something a sweep can find:
`tests/test_policy_coverage.py::TestOnlyTheEvaluationMutantSkipsCoverage` fails if a second appears,
and `tests/test_evals_replay.py::TestTheBypassIsOnlyTheCoverageInvariant` proves the eval path still
refuses everything else. The three steps are deliberately not re-exported from `careloop.policy`, so
reaching one means importing the module by name.

`tests/test_grant_authority.py` carries the behavioural boundary and
`tests/test_policy_coverage.py` the structural one, exercised against synthetic registries so the
invariant is generic rather than a spelling of this one tool. **The rules repair and the loader
invariant are separate properties with separate evidence**: a mutant restoring the historical hole
in `rules.yaml` turns seven behavioural assertions red, and a mutant removing only the loader's
coverage call turns eight loader-specific tests red while every runtime portal outcome stays
correct.

**The grant path is still unreachable in production and this changed nothing about that.** No
producer emits a fact under the `role` key; `tests/test_typed_trust.py::TestTheGrantPathHasNoProduction
Authority` is unchanged and still passing. The tests above synthesise a trusted role on purpose, to
ask the only question that matters here: *if the role barrier were satisfied, would portal
destination safety still hold?*

**A policy verdict is only meaningful for a complete tool call, and half of that was unenforced.**
`PolicyEngine.evaluate` refuses an argument map whose keys are not exactly the ones the registry
declares — **both** directions, before trust is resolved, before an approval is consulted and before
any rule is matched. Undeclared arguments were always refused that way; arguments the tool declares
and the call omits were not. Session P: ten of the thirteen declared arguments were denied when
omitted, but only because a rule happened to name each of them and read the absence as `UNTRUSTED`
through `trust.get(arg, UNTRUSTED)`. The three that no rule names — `send_email.subject`,
`send_email.body` and `book_appointment.reason`, which carry no `trust_key` and by design cannot,
since a rule requiring their trust fails at load — went the other way. A booking missing its `reason`
reached `ALLOW` under `allow-booking-on-validated-slot` **autonomously**, on the one tool whose effect
is genuinely external; an email missing its `subject` or `body` reached `REQUIRE_HUMAN`, and reached
`ALLOW` under `allow-approved-outbound-email` once a caregiver's approval covered the incomplete map.
The policy layer considered a malformed capability approved.

This is a **policy-contract defect, not an authorisation bypass**, and the distinction is the honest
one rather than a softening. Nothing incomplete could be proposed — `PlannerIntentBase._bind_
arguments_to_the_chosen_tool` rebuilds a proposal's arguments as exactly `spec.arg_names`, filling
omissions with `NOT_SUPPLIED`, and `PlanNode` is the only production writer of `PLAN_PROPOSED`. And
nothing incomplete could execute: `ToolSet.<tool>(**args)` raises `TypeError` before the executor is
reached, measured end to end at zero ledger entries. What was wrong is that the authority layer
issued verdicts about objects that are not tool calls, with an argument-binding accident in another
layer as the only thing between one of those verdicts and an effect — session N's shape, where the
safety came from somewhere else and was written down in no rule.

**Key presence, and nothing else.** `{"body": ""}` is a *complete* `send_email` call: the model named
the argument and put nothing in it, which is a statement about the **value**, and the trust rules and
`args_present` already answer those. Completeness reads no truthiness, consults no canonical form,
and does not overlap `args_present` — whose live scope narrows from "supplied and non-empty" to
"non-empty and canonicalisable" and which is not touched. So `patient_id=""` is still a booking that
names a patient badly and is still refused by `deny-booking-for-unverified-patient`; a booking with
no `patient_id` key is not a complete call for any rule to have an opinion about. Session J's
authority requirement is untouched: all four shapes it refuses — invented, borrowed from another fact
key, unvalidated, misquoted — supply the argument. **Whether an empty `reason` should book is the
open content question and is deliberately still open**; an empty reason still reaches `ALLOW`.

The refusal is deliberately indistinguishable from the other structural ones — `DENY` /
`NO_RULE_MATCH` / no matched rule, with `arg_classes` and `arg_trust` left empty, which is the
assertion that the branch ran before trust resolution rather than a proxy for it. **No new
`ReasonCode`**: the vocabulary is coarse on purpose so a planner cannot map the policy surface by
probing it, and *"you omitted an argument"* is precisely the hint that would help one construct a
well-formed call. The undeclared branch stays first, so a map wrong in both directions reports the
extra argument; collapsing the two into one set-mismatch check would lose a real distinction in the
audit trail.

**One diagnostic moved, deliberately.** `send_email` with no `to` was refused
`DESTINATION_NOT_TRUSTED` by `deny-email-to-untrusted-recipient` and is now refused `NO_RULE_MATCH`
as incomplete — earlier and stricter, but a different model-visible reason. A mapping with no
recipient is not an email to an untrusted recipient. `Condition.arg_trust_below`'s docstring and
`_condition_holds`'s comment used to assert the old behaviour and now say what is actually true: the
resolver still defaults an absent lookup to `UNTRUSTED` as defence in depth, and `evaluate` no longer
reaches it for a declared argument. Destination trust stays non-vacuous through a *present* hostile
recipient.

`tests/test_policy_completeness.py` owns the property, over the shipped registry and over a synthetic
`tool_a(a, b)` behind an unconditional `ALLOW` so it reads as an invariant rather than a spelling of
`rules.yaml` — `VALUE`-class arguments there on purpose, so the authority-coverage loader is not
dragged into a test about something else. A mutant disabling **only** the new branch turns 41
assertions red — 38 in that file plus the three retargeted tests — while the synthetic `{a,b} → ALLOW`
floor, the `{a,b,c}` undeclared row, all thirteen empty-value rows, every complete-call anchor and the
other 2170 tests stay green. The eval headline does not move: 0 of 320 shipped, 320 of 320 allow-all,
before and after. That mutant removes authorisation *rules*; it does not remove malformed-call
validation, and it should not.

**The read-back compares every booking argument the clinic exposes, and `reason` was the one it did
not.** `book_appointment` independently re-reads the clinic's appointment by its exact confirmation
id and compares provider, patient, slot **and reason** against the exact value CareLoop executed
before the case may reach `DONE`. Until session O the fourth was missing at both ends:
`AppointmentBooked` had no such field, `ClinicClient._appointment` dropped it from a response that
carried it, and `_verify_booking` had nothing to compare — so a clinic that kept the appointment and
rewrote only *why* it was made reached `VERIFICATION_PASSED` and `DONE`, with the timeline reporting
`read_back: true`. Reproduced end to end against Demo World on a loopback socket before the repair.

Note the phrasing: `reason` is checked against **the exact value CareLoop executed**, never "the
value policy authorised." The whole call passes policy, but this argument carries no trust predicate
of its own and by design cannot — it declares no `trust_key`, so a rule requiring its trust fails at
load. Completion-time fidelity to an external record and authorisation-time provenance are different
questions, and only the first is closed. Session J's shape was *whether trust is required*, session
N's was *whether an argument is named by a rule at all*; this is neither. It is whether an argument
CareLoop deliberately sends to a third party is ever looked at again.

The scope is exactly the clinic's read-back. `reason` is carried because Demo World persists it and
returns it on `GET /clinic/appointments/{confirmation_id}`, so there is an authoritative external
record to compare against; a field the *service* grows — a `booked_at`, a queue position — is still
dropped at the boundary, because CareLoop never sent it and has nothing to check it against. That is
the distinction `_appointment` encodes, and it is narrower than "keep exactly four fields."
`tests/test_verify_readback.py::TestTheExecutedReasonIsCheckedOnTheWayOutToo` holds the behavioural
half and `tests/test_clinic_client.py::TestTheBoundaryIsNarrow` the mapping half; a mutant deleting
only `"reason": "reason"` from `_verify_booking` — leaving the model, both producers and the mapper
intact — turns three assertions red while the typed mapper, the simulated executor, the malformed
observation and every provider, patient and slot mismatch stay green. Carrying the field is not the
same property as comparing it, and each has its own evidence.

**`AppointmentBooked.reason` is required, and a pre-patch booking result therefore cannot be
rehydrated.** Absence is a malformed observation, not a value to default: `body.get("reason", "")`
would have CareLoop compare what it executed against a string it invented, and since
`NOT_SUPPLIED` makes an empty executed reason reachable, a clinic returning no reason at all would
have verified clean. The compatibility boundary is narrow and measured rather than asserted:
existing cases stay **loadable and projectable in every state** — `ActionRecord.result` is opaque
text to everything except `careloop.graph.verify.rehydrate`, whose only production caller is the
verify node — and what breaks is precisely the two paths that re-verify a legacy record, a case
persisted mid-`VERIFYING` and a reopened `DONE` case reproposing the identical booking. Both fail
closed. There is no migration and no legacy adapter, because a record written before the reason was
ever observed cannot truthfully satisfy an invariant that says it was.

**What that patch did not claim, and what this one closes.** `reason` remained planner-authored
free text with no trust key, no content constraint, no length bound and no human review before an
autonomous booking, and it could be influenced by document evidence. That patch verified external
fidelity, not provenance. The provenance half and the identity half are both closed under *Four
questions about one argument* below, and each keeps its own causal evidence.

**Once an external effect has been attempted, a failed verification cannot return the case to
autonomous planning.** `VerifyNode` parks it in `AWAITING_HUMAN` through a manual handoff carrying
**no `Approval`** — a `MANUAL_HANDOFF_REQUIRED` entry with `EXTERNAL_STATE_NOT_CONFIRMED`, written by
`_park_with_nothing_certifiable`. The graph has **no `verify → plan` edge** and the state machine
**forbids `VERIFYING → PLANNING`**; both halves are load-bearing, because the edge stops the graph
routing there and the transition rule stops a future node writing the state at all.

It used to route to `PLANNING`, and the standing account of the cost — open decision 18, "it burns a
node budget but books nothing twice" — was **true only because the reproposal was identical**. Every
entry into `VERIFYING` is post-effect: it is reachable only from `EXECUTING` and `WAITING_EXTERNAL`,
`ExecuteNode` transitions there after recording an action or suppressing a duplicate of one, and
`VerifyNode` fails the case outright when no `ActionRecord` exists. So "a proposal that did not verify
deserves another attempt" was never a description of a reachable state. `book_appointment.reason` was
then planner-authored free text carrying no `trust_key` and named by no rule, so a reproposal changing
only *that* moved the argument digest, moved the idempotency key, and defeated duplicate suppression in
CareLoop **and** in Demo World. Measured through the real graph on a loopback clinic: against a clinic
that could not confirm the first booking, **two `ACTION_EXECUTED` entries, two `ActionRecord`s, two
executor invocations, two confirmation ids** — a second autonomous external booking issued after
CareLoop already knew it could not certify the first. Against a diverging clinic, the second `POST`
left CareLoop and only Demo World's own slot-collision rule refused it, which is an external service's
consistency check standing where an authority decision should have been.

**Every way of failing to certify shares this lifecycle, deliberately, and they are no longer
indistinguishable while doing so.** Retrying the *effect* is wrong for all of them and stopping is
the only lifecycle correct for all of them today, so the case does the same thing whichever occurred
— but what the trail *says* is now specific; see the observation vocabulary below. **A malformed
observation does not use this path yet**: a 200 whose body omits a required field raises out of the
node, `run_case` marks the case `FAILED` and re-raises. That already fails closed, never replans and
issues no second effect; routing it here is a separate patch.

**CareLoop knew four things about the outside world and recorded one.** A read that succeeded and
disagreed, a read the clinic answered by saying it holds no such appointment, a read that could not
be completed, and a verification where no independent read happened all arrived at
`Verification(passed=False, externally_observed=True)` plus free text. The distinction between the
middle two survived exactly as long as one `except` clause in `VerifyNode._verify`; the distinction
between the first two never existed, because `ClinicClient._request` raised one `ClinicRefused` for
every status from 400 to 599, so a `404` on the exact confirmation id and a `503` were the same
object by the time anything could look.

So `Verification` carries **`outcome: ObservationOutcome`** — `MATCHED` / `DIVERGED` / `ABSENT` /
`UNAVAILABLE` / `NOT_OBSERVED` — recorded on both verification timeline entries as the `observation`
payload key. `passed`, `diagnostic` and `externally_observed` keep their exact prior meanings;
`externally_observed` still means the read was *attempted*, and redefining it would be a separate
compatibility decision rather than a clarification. **`outcome` answers "what did the read produce";
`passed` answers "may this be certified".** They are not the same question: a closed-loop failure
with no observation is `NOT_OBSERVED` and not `DIVERGED`, because reporting a disagreement with a
system that was never consulted would put a false statement in the trail.

**`NOT_OBSERVED` is not `UNAVAILABLE`.** The first says no independent observer took part, which is
what a simulated effect means and is a fact about CareLoop's wiring. The second claims an external
system was consulted and did not answer — a claim about the world, made by a run that never touched
one.

**A `GET` 404 on the exact confirmation id is `ABSENT`, not `UNAVAILABLE`, and the classification is
read-route specific.** The clinic minted that id, stored it and returned it, so a later read of it
answering 404 is the clinic *contradicting* the receipt CareLoop holds — positive evidence, not a
failure to obtain any. Demo World is deterministic with no eventual consistency, so a retry returns
identical bytes; classifying it as unavailable would hide a definitive external contradiction behind
"still could not read". But `POST /clinic/appointments` also answers 404, for a slot the clinic does
not offer, and that is a refusal to book a thing that never existed. Classifying by status in
`_request` would fold the two together, so `AppointmentNotFound` is raised by `ClinicClient.
appointment` alone and `tests/test_clinic_client.py::TestOnlyTheReadRouteMeansTheAppointmentIsAbsent`
is what fails if it is ever generalised. It subclasses `ClinicRefused`, so everything already
catching the broad type is untouched.

**HTTP stops at `_clinic_readback`.** `AppointmentNotFound → ObservationAbsent`, everything else →
`ObservationUnavailable`, both still `ReadBackFailed`; the originating exception is kept as
`__cause__` rather than discarded, because deciding which *causes* are worth retrying is a judgement
this patch deliberately does not make. **The outcome is read from the failure's type and never from
its message** — a clinic whose error text claims the observation matched changes nothing, which
`tests/test_observation_outcome.py::TestTheOutcomeIsNotReadOutOfText` proves with a payload that
says exactly that. `observation` is safe to expose where the diagnostic beside it is not: it is one
of five names CareLoop chose, never a value it was told.

**No new `ReasonCode`, and that is a decision.** `EXTERNAL_STATE_NOT_CONFIRMED` still covers all
three failing outcomes, because an outcome says what the read produced and a reason code says why
CareLoop stopped and asked a person — and the answer to the second is identical in every case. A
second code would offer a caregiver a distinction the lifecycle does not make.

**The handoff now names the action it could not certify.** `MANUAL_HANDOFF_REQUIRED` carried
`{tool_name, reason_code}`, which says only *"a booking on this case could not be confirmed"* — a
tool name is not an identity, and a reopened case routinely carries more than one `ActionRecord`. It
now also carries the **`idempotency_key`** of the exact record that failed, threaded through from
the value `VerifyNode.run` already derived to *find* that record, so "the action this handoff is
about" and "the action that was verified" are the same object by construction rather than by a
second derivation that could drift. `args_digest` covers argument names and canonical values and
neither the case nor the tool; `confirmation_id` names the clinic's object rather than CareLoop's
action; "the latest record" is the class of accident that produced three identity defects here. A
mutant deleting only `"idempotency_key": key` turns six assertions red while every observation
classification, every lifecycle assertion and the whole happy path stay green; a mutant deleting
only the read-route 404 branch turns four red and moves nothing else.

Nothing consumes the key, and no lifecycle branches on the outcome. **Both halves are
representation** — what CareLoop knows, and which action it knows it about — written down so that
deciding what may be done about either is a separate patch with its own evidence. Cases parked
before this carry neither and stay exactly as they are: readable, projectable, and correctly unable
to satisfy an identity nobody recorded.

**Something consumes the key now, and it recovers knowledge rather than authority.** A parked
`EXTERNAL_STATE_NOT_CONFIRMED` case was safe and *permanently* stopped: measured, `approve` and
`reject` raise `NoSuchApproval` because the parking raises none, `reopen_case` refuses anything that
is not `DONE`, `POST /api/events` is that refusal over HTTP, and a bare `run_case` executes
`("dispatch",)` and commits nothing. A caregiver who went to the clinic, found the record wrong and
corrected it had no supported way to say so. `request_recheck(store, case_id, requested_by=…)` —
`careloop/graph/runner.py`, beside `open_case`, `reopen_case`, `approve` and `reject` — writes one
`EXTERNAL_RECHECK_REQUESTED` entry and moves `AWAITING_HUMAN → VERIFYING`. The API then schedules an
ordinary run, which executes **`("dispatch", "verify")`** and nothing else.

**No graph edge was added and none may be.** `dispatch → verify` already routes a case persisted
mid-verification, so re-entering verification is a thing the graph already did; `outgoing(verify)`
is still empty, `("verify", "plan")` is still absent, and `VERIFYING → PLANNING` is still illegal.
A `verify` self-edge would have been a second route into one node with its own conditions to get
wrong.

**Every entry into `VERIFYING` is still post-effect, with three sources rather than two.**
`EXECUTING` and `WAITING_EXTERNAL` are `ExecuteNode` moving on after recording an action or
suppressing a duplicate of one. The third is the guarded human re-check, and what keeps the claim
true is not the table but `recheck_target`: it refuses unless the handoff names an `ActionRecord`
the case really holds, so a case where nothing was carried out cannot take that edge. Do **not**
weaken this to "`AWAITING_HUMAN` may always enter `VERIFYING`".

**The target is resolved from durable server state, and the client cannot aim it.** The chain is
`MANUAL_HANDOFF_REQUIRED.idempotency_key → case.action_for(key) → ActionRecord →
rehydrate(action_type, result) → confirmation_id → the read-back`. `request_recheck` has **no
parameter** for a tool, arguments, a digest, a confirmation id or an action index, and the route
declares no body — the property is structural rather than validated, and it is asserted on the
confirmation id actually fetched rather than on a status code. Never "the latest action", "the
latest booking", `tool_name` alone or `args_digest` alone; this repository has closed three
digest-only identity defects already.

**`VerifyNode` is not handed a target, and that is the subtle half.** It resolves its own by
re-deriving `idempotency_key(case_id, latest_proposal.tool_name, latest_proposal.args)`, exactly as
on a first pass. So `recheck_target` **proves the two agree before scheduling** — if the handoff's
key and the proposal-derived key differ, the disagreement is the finding and the request is refused,
never silently resolved to one of them. Unreachable under the shipped flow, since a parked case
never plans again; it is a fail-closed guard against a persisted or tampered case and against future
drift, in the same register as session N's unreachable-but-closed finding.

**One resolver, two readers.** `careloop.graph.context.recheck_target(case) -> RecheckTarget | None`
is the single source of truth for static eligibility, and both `request_recheck` and
`decision_view` read it. The button a caregiver is shown and the endpoint that validates their click
therefore cannot disagree. It is durable-state-only by construction, which is why it deliberately
answers neither "is a run in flight" (execution context, checked by the API) nor "will the next run
have an observer" (a fact about a `RunContext`). `DecisionView.recheckable` claims exactly what the
resolver knows and no more; the API doc-comment says so, and the endpoint re-validates everything.

**A re-check that reads nothing certifies nothing, and this is the load-bearing guard.** Reproduced
before the repair: force the transition, run with `clinic=None`, and the case reaches `DONE`
reporting `read_back=false` and `observation=NOT_OBSERVED` while Demo World still holds
`ATTACKER SUPPLIED REASON`. Nothing is broken in the closed-loop checks and that is the problem —
the booking *response* was always correct and only the world diverged, so dropping the observer does
not make verification fail, it makes it *stop looking*. The rule, which is a property rather than a
special case: **a verification may not certify a case whose own trail already records that an
independent read of this action was attempted and did not confirm it, unless an independent read is
performed now.** Certifying without one is certifying by *removing* evidence.

`VerifyNode._verify` returns `Verification(passed=False, externally_observed=False,
outcome=NOT_OBSERVED)` in that case and transitions nothing — `run` stays the single lifecycle
writer, so it parks through the same `VERIFICATION_FAILED` → `MANUAL_HANDOFF_REQUIRED` →
`AWAITING_HUMAN` path as every other failure to certify. **No new state and no new `ReasonCode`**:
what CareLoop knows is once again that it cannot confirm the result. Both reported fields are
literally true — nothing was attempted, so `externally_observed` is `False`, and `NOT_OBSERVED` is a
fact about CareLoop's wiring; `UNAVAILABLE` would claim an external system was consulted by a run
that never touched one.

**`NOT_OBSERVED` is emphatically not redefined as failure globally.** Three of the four effect tools
have no external system, and a first-pass simulated run still verifies closed-loop and still reaches
`DONE` saying plainly that it read nothing. The guard is scoped by
`entered_verifying_from(case) is AWAITING_HUMAN` — read from the `STATE_TRANSITION` entry
`transition()` wrote, so it is durable, survives a process boundary, describes *this* episode rather
than "a re-check happened once", and is not a `RunContext` flag or a parse of any diagnostic.

**Capability and availability are separate and both are needed.**
`careloop.graph.verify.INDEPENDENTLY_OBSERVABLE` — `frozenset({"book_appointment"})`, beside
`VERIFIERS` and `RESULT_TYPES` — says an action type *can* be observed by something other than
CareLoop's own receipt; `RunContext.readbacks` says whether this run *has* that observer. A **subset**
of `REGISTRY.external_effect_tools` on purpose, checked at import: three tools are simulated by
design, so equality would force three fake observers into existence. A structural test pins
`frozenset(clinic-bound RunContext.readbacks) == INDEPENDENTLY_OBSERVABLE`, so the declaration and
the wiring cannot drift. The tool name lives there and **not** in `runner.py`, the state machine or a
route.

**Acceptance and outcome are different questions.** Wrong state, a pending approval, the other kind
of handoff, a handoff with no recorded identity, a key naming no record, an identity mismatch, an
unobservable action type, a run in flight and a case with no durable delivery are all refused before
anything moves — `409`, one generic `CaseNotRecheckable` message, because a caller who can tell the
preconditions apart by probing has been handed a map of them. A clinic that disagrees, holds nothing,
cannot be reached, or a run with no observer bound are **not** request errors: the request was
already accepted, and they become ordinary verification outcomes and a second handoff. Moving those
into the handler would put a domain verdict in the routing layer.

**The transition is legal and narrowly reachable.** Measured while planning: adding `VERIFYING` to
`TRANSITIONS[AWAITING_HUMAN]` moved none of the suite's 2279 assertions, so the legality was
entirely unpinned. `tests/test_recheck_writers.py` is the `reopen_case` arrangement applied again —
no agent-facing module may name `request_recheck`, and `graph/runner.py` and `graph/nodes.py` are
the only modules that write `transition(..., VERIFYING)` at all, so a node cannot route around the
operation by writing the state itself.

Three mutants, each red in its own place and green everywhere else. Resolving the target by
`case.actions[-1]` with the consistency guard dropped turns **5** identity assertions red. Routing
the request to `PLANNING` instead of `VERIFYING` turns **20** red across three files — and measured,
it produced `("dispatch", "plan", "gate", "execute", "verify")`, a second `PLAN_PROPOSED`, a second
`POLICY_ALLOW` and one `DUPLICATE_SUPPRESSED`, which is exactly why `DUPLICATE_SUPPRESSED == 0` is
the sharp assertion rather than `ACTION_EXECUTED == 1`: both of the latter survived that mutant, and
the effect path was re-entered and saved only by idempotency. Removing **only** the no-observer
branch turns exactly **4** red — the no-observer tests — while every observed re-check, every
simulated run and all four observation classifications stay green.

**What this does not do.** It creates no `Proposal`, invokes no planner and no policy verdict,
raises and consumes no `Approval`, never reaches `ExecuteNode`, records no `ActionRecord`, changes no
argument and mints no new idempotency identity — measured as unchanged counts across every scenario.
It performs exactly **one** observation episode: `ABSENT` and `UNAVAILABLE` each get one read and
stop, with the observer's own attempt count asserted, so Patch 2B's automatic retry stays deferred
rather than arriving by accident. A malformed 200 still raises out of the node and leaves the case
`FAILED`; that is Patch 3 and it already fails closed. Pre-2A handoffs and `NO_SAFE_ACTION_AVAILABLE`
handoffs are refused with no fallback, no migration and no guess — `Handoff.tool_name` and
`Handoff.idempotency_key` are both `str | None` because a `NO_SAFE_ACTION_AVAILABLE` payload
legitimately carries neither, and defaulting either would have a record assert an identity nobody
observed.

`EXTERNAL_STATE_NOT_CONFIRMED` is the eighth `ReasonCode` and the second that parks a *case* rather
than refusing a call, so no rule carries it and the engine never emits it. It is named for what
CareLoop knows rather than for what happened, because one code covers both evidence states; reusing
`NO_SAFE_ACTION_AVAILABLE` would have said every action was refused, which is close to the opposite of
what occurred and would have put a false statement in an append-only trail. The timeline keeps all
three facts separately — `ACTION_EXECUTED` (what happened), `VERIFICATION_FAILED` (why it could not be
certified), `MANUAL_HANDOFF_REQUIRED` (what CareLoop decided) — and `MAX_NODE_EXECUTIONS` is a runaway
detector again rather than the thing that decides a domain outcome.

`tests/test_post_effect_lifecycle.py` owns the property, over reason drift, patient drift and a 404
read-back, with the second-effect prevention as its load-bearing class. A mutant restoring **all
three** halves — the node's transition, the graph edge, and the transition-table entry, which is what
it takes to recreate the bug rather than trip over one of its own guards — turns 17 tests red across
four files while every `verify_action` comparison test, the whole happy path, idempotency, and every
policy and approval suite stay green.

**A model that cannot form an action does not break the case; it stops it.** `PlanNode` catches
exactly `careloop.agents.invoke.RECOVERABLE_PROPOSAL_FAILURES` around the one call to
`planner.propose`, and parks the case `PLANNING` → `AWAITING_HUMAN` through
`_park_with_nothing_proposable`, carrying `NO_VALID_PROPOSAL_PRODUCED`. The bound is
`proposal_tool_call`'s and nothing here adds to it: by the time the exception arrives the one retry
has been spent, so what is caught is **exhaustion**, never a first failure, and
`MAX_PROPOSAL_ATTEMPTS` is untouched.

It used to end `FAILED`. Nothing unsafe ever happened on that path and nothing could — no proposal
exists to act on — but `FAILED` means *something broke*, it is filed under **Completed** where a
caregiver reads it as finished, and its `failure_reason` was the literal string
`a graph node raised: MalformedProposal`, an exception class on a projected field. Measured on the
deployed demo, 2026-08-19: 2 of 28 `resume` runs, both on the replan call after a refusal.

**The caught set is imported, never restated.** A second list is a second place to drift, and the
one direction that must not drift is wider: a throttle, a credential error, a node timeout, an
exhausted test script or a bug in this package still leaves the node, still reaches `run_case`, and
still ends the case `FAILED`. `NoStructuredOutput` lives in the same module and is deliberately not
in the tuple — triage's seam is a different question with a different lifecycle, and it is
untouched. A mutant widening the catch to `Exception` turns 14 tests red, four of them in files
written long before this patch.

**`plan → gate` carries a condition now, and it had to.** It was the graph's one unconditional edge.
Measured with it left alone: a first-proposal exhaustion parked and was then routed straight into
the gate, whose no-proposal branch overwrote the park with `FAILED`; and an exhaustion *after* a
refusal had the gate re-judge the proposal it had already denied, on a case a person had just been
handed. `when_case_is(context, PLANNING)` asks whether there is a proposal, never whether to check
one — `PlanNode` has exactly two outcomes, a proposal with the case left `PLANNING` or no proposal
with the case parked, so "still `PLANNING`" and "a proposal was written" are the same statement.
Same reasoning as the absent `verify → plan` edge: an edge is what stops the graph routing
somewhere, and a node's own guard is not a substitute for one.

**No approval, and the denied-proposal case is why that matters here more than anywhere.** On a case
that was refused once and then could not replan, `latest_proposal` still names the call policy
**denied** — which is exactly the record an approval would release, one node earlier than the defect
`_park_with_nothing_safe_to_do` documents below. So the park raises none, `approve` and `reject`
answer `NoSuchApproval`, and nothing can walk the case into execution.

**The ninth `ReasonCode`, and the third that parks a case rather than refusing a call**, so no rule
carries it and the engine never emits it. Not `NO_SAFE_ACTION_AVAILABLE`: that asserts every tool
was proposed and refused, and here *nothing was proposed and nothing was refused* — there is not one
`POLICY_DENY` from the cycle that parked. One code covers all four generation failures on purpose;
they are four facts about a provider and one fact about CareLoop, and the lifecycle makes no
distinction between them.

**The exception type stays out of the product vocabulary.** `NO_VALID_PROPOSAL_PRODUCED` is what a
caregiver, a projection and a lifecycle read; `MalformedProposal` is an implementation name and
lives in `invoke.py`'s two log lines and in the handoff's **diagnostic**, which the API withholds
wholesale. It is deliberately **not** a payload key: the payload is the allowlisted half a response
can carry, and the old `failure_reason` string is not worth reproducing as public API surface. The
*type* only, never `str(failure)` — a `ValidationError` renders `input_value`, and that input is
model output derived from document content.

**What this does not do.** It creates no recovery. There is no approval to grant, no external effect
to re-check, and `reopen_case` still takes only a finished case, so a parked case is inert: a later
run executes `("dispatch",)` and commits nothing. A caregiver-triggered `request_replan` —
`AWAITING_HUMAN → PLANNING` is already a legal transition — would close that and is deliberately a
separate patch, because it recovers *authority to act* rather than knowledge and deserves its own
evidence. The card says so rather than implying a button that is not there.
`tests/test_proposal_exhaustion.py` owns the property; five mutants each turn their own tests red,
including one that reverts the edge to unconditional.

**An `Approval` is a capability, and a manual handoff is not one.** An approval says a caregiver was
asked about *one exact call*, identified by `(tool_name, args_digest)`; deciding it releases that
call and nothing else. `NO_SAFE_ACTION_AVAILABLE` — every tool in the registry proposed and refused
— is a **status**, so it raises no `Approval` at all: `_park_with_nothing_safe_to_do` writes a
`MANUAL_HANDOFF_REQUIRED` timeline entry and leaves the case `AWAITING_HUMAN`, and
`decision_view` projects the caregiver's card from that entry. It used to raise an approval with
both identifying fields empty, whose docstring correctly said approving it released nothing and
whose behaviour did the opposite: `_decide` moved the case to `READY_TO_EXECUTE`, where the execute
node picked up `latest_proposal` — by construction a proposal policy had just **denied** — and
dispatched it. The tool boundary refused it a second time, the case ended `FAILED`, and the timeline
blamed a disagreement between the two enforcement points that had never happened. Reachable by any
HTTP client; the only thing stopping it was a frontend choosing not to draw a button.

So: **the parking creates no approval, and `_decide` refuses any approval that is not
`is_actionable`.** Two layers, because the second is what makes cases persisted before the change
inert — they still deserialize, still render, still sit parked, and return `409` before any state or
version moves. No migration; `Approval` deliberately grew no validator, since one would make an old
case *unreadable* rather than merely undecidable. `tests/test_approval_shape.py` sweeps for the one
remaining production writer of `Approval(...)`, which is how the next notification gets stopped from
being encoded as one.

**Two identities, and one name used to do both jobs.** `approval_id` says *which occurrence of
asking a caregiver* a decision is about; `args_digest` says *which exact argument set* is involved.
`approval_id_for` derived the first from the second — `f"apr-{digest[:16]}"` — so a proposal
rejected and then reproposed unchanged came back under the first request's name, and `_decide`
rebuilt `case.approvals` replacing **every** record whose id matched while checking `PENDING` and
`is_actionable` on only one of them. One record inspected, N rewritten. Approving the second request
turned the first caregiver's recorded *rejection* into the second caregiver's approval, name and
timestamp included. Authority never moved — `is_approved` reads the most recent record matching the
digest, which is the one actually decided — so this was audit integrity, in the one direction an
append-only trail must not move. Open decision 20, now closed.

So: **a request identity is `apr-{seq}`, and a decision replaces exactly one record by position.**
`seq` is the timeline position of the `APPROVAL_REQUESTED` entry recording the request, so
uniqueness within a case is structural rather than a collision argument, no clock or random source
is involved, and a replayed case mints the same identifiers. Positional replacement is kept
*because* unique ids cannot help the duplicates already persisted: an id naming one decided and one
pending record decides the pending one and leaves the other byte-for-byte alone, and an id naming
two pending records raises `AmbiguousApproval` and moves nothing. No migration and no rewritten
history — persisted ids stay exactly as they are. The two halves are independently load-bearing and
have separate mutants; `tests/test_approval_identity.py` holds both.

**Human approval is a capability for one exact tool call, and both halves are matched.** Approval
lookup is keyed by the stored `tool_name` **and** the exact canonical `args_digest`; approvals for
other tools do not participate, even when their argument digests are identical. `approval_id`
identifies the occurrence of asking; `(tool_name, args_digest)` identifies the call the answer
applies to, and the two are never folded together.

Until session L that pair was half enforced. `args_digest` covers argument names and canonical
values and *nothing else* — not the tool, not the case — so `approval_for` and `is_approved` were
asking "did a human approve an argument map that looks like this?", and `Approval.tool_name` was
stored, audited and rendered without ever being matched. Not reachable under the shipped rules: the
only tools that can raise a decision request, `send_email` and `grant_portal_access`, are the only
tools whose rules read `args_approved`, and their argument names are disjoint — so the sole digest
they could share is the empty one, which a `DENY` on the untrusted destination refuses before
`REQUIRE_HUMAN` is ever considered. **Do not restate that as a defence.** It was an accident of
registry shape, written down in no rule, and one same-shaped tool away from being live: a synthetic
registry whose two tools declare one identical schema laundered `tool_a`'s approval into an `ALLOW`
for `tool_b` through the real engine. The measurement that used to justify the shape — "no two tools
share an argument-name set" — was also weaker than it read, since `evaluate` refuses only
*undeclared* arguments and three shipped pairs share a name.

So there is now **one** capability lookup, `Case.approval_for(tool_name, args_digest)`; the graph
helpers delegate to it rather than carrying a second reversed scan, and `approved` takes the tool
the engine is currently evaluating. `rules.yaml` did not change and must not: `args_approved` is a
predicate the engine interprets, and naming the tool in YAML would make it a second source of truth
a rule could contradict. Latest-match-wins is unchanged *within* one exact call, and the records for
other tools that used to participate could shadow as well as leak — a rejection of `tool_b` revoked
`tool_a`'s standing approval. `tests/test_approval_capability.py` holds both directions, and a
mutant that removes tool matching **and nothing else** turns nine of its assertions red while every
same-tool positive stays green. Nothing else in the suite moved, which is the honest measure of what
was covering this before: nothing was.

No migration, and none was possible to need — `Approval` already stored both fields, so a persisted
record gains the stronger reading and keeps its identifier byte for byte.

**The decision surface resolves the proposal by the same capability identity, and the three
identities stay distinct.** `approval_id` names the *occurrence of asking*; `(tool_name,
args_digest)` names the *exact capability*; `proposal_for(case, tool_name, digest)` returns the
*exact proposal reconstructed for that capability*. A proposal belonging to another tool cannot
supply proposal-derived display data for a pending decision, however its arguments hash.

Session M closed the half session L deliberately left open. `decision_view` held
`Approval.tool_name` and passed only `Approval.args_digest` to `proposal_for`, so the card asked
"which proposal has arguments that hash like this?" while the approval meant "this exact call".
**It authorised nothing** — `Case.approval_for` and `Case.is_approved` were already tool-bound, and
`args` is the only proposal-derived field on `DecisionView` — so this was human-consent integrity,
not the authority path, and it was **not reachable under the shipped workflow** for three
independent reasons: the two `REQUIRE_HUMAN` tools declare disjoint argument names, the approval is
raised from the newest proposal inside the same gate execution that read it, and nothing plans while
a case is `AWAITING_HUMAN`. All three are facts about registry shape and node write order, stated in
no rule, which is the reason to close it rather than note it.

What the repair could *not* fix is worth stating, because reading the code suggests otherwise.
`Proposal.args` is `dict[str, str]` and `canonical_value` returns a `str` verbatim, so **an equal
digest is an equal argument map** short of a SHA-256 collision: two same-digest proposals for
different tools never rendered *different* values, and `rationale` is not projected. The one
observable form was **borrowing** — an approval whose own proposal is absent taking another tool's
arguments instead of the documented no-match `args = {}` — and that is what
`tests/test_decision_surface.py` proves through `decision_view` and through `case_detail`. Record
identity is proven at helper level on `rationale`, which the digest does not cover. A mutant that
removes the tool guard **and nothing else** turns seven of that file's tests red while every
same-tool positive, the different-digest case and the no-match case stay green, and the four closed
approval suites plus `test_api_projections.py` and `test_graph_wiring.py` — 191 tests — do not move.

`proposal_for` takes both halves positionally and there is **no digest-only variant anywhere**;
adding one would reopen this under a different name. No migration: `Approval` already stored both
fields and `PLAN_PROPOSED` already stored the tool name, so the stronger lookup is purely
interpretive.

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

**Two tools are real, and the read-back is what makes that worth anything.** `book_appointment`
routes over HTTP to `demoworld/` and `upload_document` files into Amazon S3; the other two effects,
`send_email` and `grant_portal_access`, stay simulated, because the point is proven by two
integrations that offer CareLoop nothing in common and not by making all four external. Verification then performs a
*separate* read, keyed on the identifier the effect returned, and compares observed external state
against **every declared argument** — never the effect's own response, which would be re-reading
CareLoop's receipt. `tests/test_verify_readback.py::TestTheIndependenceProof` and
`tests/test_upload_readback.py::TestTheIndependenceProof` are the vacuity proofs: each moves the
external record behind CareLoop's back and requires verification to fail while showing the
pre-read-back checks still pass. **Anything that makes a read-back cheaper by reusing what the
effect returned deletes the only thing this buys.**

Two structural decisions hold it up. `RunContext` takes **providers or an executor, never both** —
one collaborator turns on the real write *and* the read-back that observes it, so a real effect
verified against a simulated read cannot be built. And a read-back is **deliberately not a registry
tool**: it is never proposed, produces no fact, and its only sink is a timeline-only diagnostic the
API withholds, so registering it would buy a new proposable tool to satisfy a symmetry that does not
apply. `demoworld/` may never import `careloop`, or reading it back stops being evidence.

**`RunContext.clinic` is the one surviving provider-named field, and it is inert.** It predates
`ExternalProvider`, is converted by `clinic_provider` in `__post_init__`, and nothing below that
line reads it — `bound` is assembled identically for the clinic and for anything else.
`tests/test_provider_seams.py::TestTheOneToleratedProviderCoupling` pins the allowance to an *exact*
set of names, so the obvious way to wire the next provider — a second field beside it — fails there.
The object store already arrives as an ordinary provider from the composition root.

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

**The two motion layers are decoration, and both are held to it by measurement rather than by
intent.** The orchestration field paints behind the interface; the boot choreography paints in
front of it, once per tab, for about a second and a half. With `?boot=off&motion=off`, all eight
reference states render **byte-identical** to the build before either existed — measured against
the pre-integration commit, served on its own port, not asserted. `frontend/docs/MOTION.md` and
`frontend/docs/BOOT.md` hold the detail; four things belong here.

**Readiness is a fact about the application, and it is emitted in exactly one place.**
`careloop:ready` fires from `useApplicationReady()` in `App`, two frames after the last effect of
the initial mount — the first committed frame of the routed application, painted. Measured at a
median of **89 ms**. It deliberately does **not** wait on the backend: `CareLoopProvider` starts
from `emptyDashboard()` and mints its session lazily, so a loader that waited for `GET /api/cases`
would sit over an unreachable backend for nine seconds and would hide `connecting` and the
connection banner — which are designed application states saying something true, not loading states
standing in for one. Emitted from an effect rather than after `createRoot().render()` because
`render()` returns before React commits, and the flag against a second announcement is raised **at
dispatch**, because `StrictMode`'s mount-teardown-remount would otherwise silence it entirely.

**Cosmetic startup code gets two independent removers, for the reason the policy layer has two
enforcement points.** The loader's own `finalize()` is idempotent, is reached from a `catch` and
from a deadline armed before the try block, and restores the header's mark by querying
`[data-boot-hidden]` out of the DOM. `index.html` holds a reaper that shares no reference with it —
not the controller, not a callback — arms only if `data-booting` is present, and undoes the same
three things twelve seconds later. The second is what covers a file that 404s, a build that throws
after the overlay is up, and a `finalize()` a future edit breaks. **The overlay becomes
`pointer-events: none` in the same statement that starts its fade**; upstream left it invisible and
still taking clicks for 740 ms, which looks exactly like a broken application and does not appear in
a screenshot. Every one of these has a mutant that turns the gate red, and the two that initially
survived are recorded in `BOOT.md` because each exposed a real weakness rather than a weak test.

**The loader says one thing and it is true.** Upstream cycles "Connecting context", "Preparing
trusted actions", "Verifying workspace". CareLoop performs none of those at startup — there is no
session to establish, nothing to verify, and the loader runs beside a React mount and nothing else.
Narrating work that is not occurring is the same small lie a decided card refuses to tell, so the
copy is "Preparing CareLoop", once, and the choreography carries the orchestration story. The
overlay is `aria-hidden` with no `role` and no `aria-live` for the matching reason: the application
underneath is *not* hidden from assistive technology, so announcing a picture five times would be
noise over a page the reader is already on.

**`SessionWorlds` is serialised, and it was not.** Found by running the e2e suite, not by reading:
two concurrent `POST /api/sessions` against a registry holding anything idle raised `KeyError` out
of `_forget_idle` and answered 500 — it snapshots the ids and then deletes them one at a time.
Starlette runs sync endpoints in a worker pool, so two visitors arriving together are two threads on
one `OrderedDict`, and `mint` also decided capacity from a length it did not hold. It now carries the
same `@serialised` decorator `SqliteCaseStore` does, with the same sweep test; `drain` is excluded by
name because holding a blocking lock across its `await` would stop the loop making the progress it
waits for, and it takes its snapshot under the lock instead. The reproduction needs
`sys.setswitchinterval(1e-7)` to open the window a served process opens constantly, which is stated
in the fixture rather than left as a mystery.

**A visitor's world has one owner, and it had two.** A session is a CareLoop store *and* a Demo World
clinic, and each registry decided on its own when that world ended: `SessionWorlds` expired by idle
time, refreshed on every API request; `Clinics` ordered by *clinic* access and dropped its oldest
unconditionally. So a judge polling the dashboard was alive on one side and the eviction victim on
the other — the appointment CareLoop had booked and verified disappeared from the external system,
and a later read-back that should have been `MATCHED` came back `ABSENT` against a world recreated
empty, with nothing raised anywhere. The guard in place was `DEFAULT_SESSION_LIMIT <=
DEFAULT_WORLD_LIMIT`, which is a number standing in for a lifecycle: **equal capacities do not
synchronise eviction order, and order was the whole disagreement.**

So **Demo World no longer removes a world on its own**. `Clinics.end` is the only remover and
CareLoop is its only caller — on expiry, on a demo reset, and on shutdown — reached over HTTP by
`ClinicClient.end_session`, the direction `POST /demo/reset` already went, because `demoworld` may
not import `careloop`. With nothing evicting there is no eviction order and nothing left for the two
registries to disagree about; there is deliberately no age, no clock and no last-used ordering left
in `Clinics` to tempt anyone into re-deriving one.

**At the cap Demo World refuses a new world rather than taking an existing one**, which is the trade
`SessionsAtCapacity` already documents one process over and is what makes the property structural
rather than dependent on a network call succeeding: a world CareLoop failed to end costs a *capacity
slot*, never a live visitor's appointment. It also forces a second change — **reads create nothing**.
A world is created by the act that puts something in it, which is booking; without that, an anonymous
`GET /clinic/slots?session=<anything>` in a loop would fill a registry that never evicts, and the
eviction that used to clean it up is gone. `Clinics.view` answers reads out of the held world or an
unregistered seeded one, identically either way.

**A failed end is queued, never swallowed**, and CareLoop lets the world go regardless — holding one
open here to repair a record over there is the wrong direction. The session is named on
`SessionWorlds.orphans()`, logged, and retried on the next mint; one `ClinicUnavailable`
short-circuits the sweep, because a service that did not answer this call will not answer the next
and a queue of twenty would otherwise cost twenty timeouts on one request. A `ClinicRefused` is the
clinic *answering*, which an idempotent delete has no reason to do, so it is logged as a bug rather
than retried forever. `mint` is therefore **not** `@serialised` — `_mint` is, and the HTTP happens
after it returns, because a lock held across a round trip stalls every other visitor for as long as
Demo World takes. `tests/test_session_worlds_are_serialised.py` now asserts both directions: what
must hold the lock, and what must not.

`tests/test_session_lifecycle.py` owns the property, driven against a real Demo World on a loopback
socket. The reproducer is the audit's own construction and it separates the two halves: removing the
coupling leaves Demo World holding a world for a visitor CareLoop forgot an hour ago and the next
arrival is refused a clinic, while the visitor still being served keeps everything — so without the
coupling the demo degrades, and without the refusal it degrades *onto the wrong person, quietly*.
`scripts/verify_deploy.py` reads `clinic_world_ended` off `POST /api/demo/reset` to assert the
**deployed pair** are coupled; that is the coupling half only, and the crossed-order half stays local
because a world has to age past a half-hour idle window to reach it.

**What is deferred, and what it now costs.** A CareLoop process that is *killed* rather than shut
down orphans every clinic world it held — the lifespan ends them on `SIGINT`, which covers an
ordinary redeploy, and covers nothing else. With Demo World no longer evicting, **those orphans are
permanent rather than self-healing**: this session changed the shape of that failure, and the next
person needs to know it. Closing it wants a bulk `DELETE /demo/sessions` swept at startup, which
hands any anonymous caller the ability to wipe every visitor's clinic and deserves its own decision.

**Settlement evidence is a one-way seam, and the evaluator that reads it imports nothing of
CareLoop's.** The domain emits typed claims through `careloop/graph/witness.py`, a protocol with no
OpenTelemetry anywhere behind it; `scripts/witness_span.py` is the only place a claim becomes a span
attribute, and `tests/test_witness_seam.py` sweeps the package to keep it that way. Telemetry has no
authority and must be structurally unable to acquire any: nothing under `careloop/` imports the
adapter, nothing branches on which witness a run holds, and a witness failure cannot reach a
business outcome. The attribute *names* are spelled in the adapter and nowhere else, which is why
`careloop/` contains no string literal beginning `careloop.effect`.

**Nine attributes, and there is no tenth.** `careloop.effect.{type,dispatch,fields,state_digest}`,
`careloop.observation.{mode,outcome,resource_id,state_digest}`, `careloop.settlement`.
`careloop.effect.action_key` was dropped during the design and must not return under that or any
other name: correlation is carried by `observation.resource_id` against the clinic read's **own
URL**, which is evidence CareLoop does not author. An identifier CareLoop minted and then checked
against itself would prove nothing that was not already assumed. The two optional attributes are
*omitted* rather than emitted empty — an absent `observation.state_digest` says no external state was
observed, and an empty string would be a malformed digest.

**`evaluators/` is a separate package on purpose, and the separation is the property.** Standard
library only — no `careloop`, no `evals`, no `boto3`, no OpenTelemetry, no network, no clock. It
declares its own `OBSERVABLE_EFFECT_TYPES` and `EXPECTED_FIELDS` rather than importing CareLoop's,
because an evaluator that imported them would be checking CareLoop against itself — the same
category error as verifying a booking by re-reading its own receipt. It is not in the wheel and the
Lambda is packaged from that directory alone. Its result vocabulary is closed and **never
interpolated**: every explanation is a fixed sentence selected by an enum member, for invariant 6's
reason applied to a surface whose consumer is a person rather than a model.

`FIELD_SET_UNEXPECTED` is ordered **before** the digest checks and that ordering is load-bearing: two
digests over an empty joint state are trivially equal, so digest equality proves nothing until the
field set is pinned to what the external system actually holds. 73 designated semantic mutants,
35/35 CRITICAL, 12/12 HIGH and 25/25 MEDIUM killed, one justified equivalent.

**It has been run in AWS against real production traces, and that is what the claim rests on.**
`CareLoopSettlementIntegrity` is a `TRACE`-level code-based AgentCore evaluator backed by
`careloop-settlement-evaluator` (Lambda, `python3.12`, 128 MB, packaged from the five
`evaluators/` modules and nothing else). Six real traces, six measured results: performed,
suppressed and caregiver-re-check settlements `PASS`; an externally diverged booking `FAIL`s with
`STATE_DIGESTS_DIFFER`; a vanished booking `FAIL`s with `OBSERVATION_NOT_SUCCESSFUL`; a trace with no
observable effect returns `NOT_APPLICABLE` **with no `value` key**. The nine attributes were measured
arriving intact through AgentCore delivery — exactly nine, nested under `attributes`, no tenth. Full
record in [`docs/agentcore-evaluations-proof.md`](docs/agentcore-evaluations-proof.md);
`scripts/evaluations_deploy.py` recreates the AWS side idempotently and
`scripts/evaluations_prove.py` reproduces a result.

**Three things about that proof must not be overstated, and the docs say so in these words.**
On-demand `Evaluate` requires `evaluationInput.sessionSpans`, so **the operator supplies the spans**
— AWS does not fetch the trace, and the payload SHA-256 is recorded for every row precisely because
of that. **AWS never queries the clinic**: the independent read is CareLoop's own, and what the
evaluator checks is whether the instrumentation-authored transport in the trace corroborates the
claim CareLoop wrote beside it. And this is not a proof that CareLoop cannot lie. Nothing here is
continuous — there is no online evaluation configuration, no AgentCore service role and no Lambda
resource-based policy, and `lambda:GetPolicy` answered `ResourceNotFoundException` both before
`CreateEvaluator` and after six successful `Evaluate` calls.

**The proof tool reads spans with its own accessors, never `evaluators.spans`.** A transport
cross-check computed with the evaluator's own reader would agree with it by construction, and a
defect in an accessor would move both sides together. It also never modifies a span — no repair, no
defaulting, no synthesised attribute, no normalised status spelling — and it refuses rather than
guesses when a literal matches more than one trace, because choosing between candidate traces is
deciding the thing the evidence exists to establish.

**An authorised action's identity is frozen before anything can leave the process, and a process
that dies after that converges rather than stopping.** `ExecuteNode` writes an `ACTION_PREPARED`
timeline entry — `{tool_name, idempotency_key, args_digest}` — in the **same commit** as
`READY_TO_EXECUTE → EXECUTING`, so "the case is `EXECUTING`" and "an attempt is durably prepared"
are one statement rather than two that can disagree. It says nothing about the provider: a
preparation is compatible with the request never being made, never arriving, arriving and not
committing, committing with the response lost, and the response arriving and not being recorded.
Which of those occurred is what reconciliation finds out.

**Before the saga this window was total and silent.** Measured on a case persisted `EXECUTING`:
`('dispatch',)`, `Status.COMPLETED`, version unchanged, `failure_reason: None`. No dispatch edge
covered the state, so the run reported success having committed nothing and the case sat there
permanently — rendered as "Working", unreachable by every API verb, while the clinic might already
hold the appointment. `WAITING_EXTERNAL` had no producer at all and four legal edges nobody had
taken, one of which was `→ PLANNING`: the session-Q double-effect defect pre-installed one state
over, waiting for somebody to give the state a producer. **Patch B removed it before Patch C
supplied one**, and that ordering is not cosmetic.

**Duplicate suppression is resolved before a preparation is written.** A logical action the case
already holds an `ActionRecord` for never acquires one, because `ACTION_PREPARED` means *about to be
attempted* and a stale intent on a completed action is exactly what a recovery would find and
dispatch. The suppression path therefore passes through `EXECUTING` and on to `VERIFYING` **in one
commit** rather than growing a `READY_TO_EXECUTE → VERIFYING` edge — there must be no instant at
which a case is durably `EXECUTING` with nothing prepared, since that is the shape reconciliation
fails closed on.

**`ActionRecord` was not touched, and that was the design decision.** Every consumer was swept
first: `ExecuteNode`'s duplicate branch, `VerifyNode`'s `rehydrate`, `recheck_target`, the API's
`action_count`/`actions`, and `scripts/resume_demo.py`. Three read existence as *the effect
completed*, and `rehydrate` subscripts a `result` that must parse into a typed result — so evolving
it into an intent record would have made "intent persisted" mean "effect happened" at three call
sites and required every one of them to grow a phase filter it could later forget. The preparation
is a **timeline entry** instead, read through `unsettled_preparation(case)`, which is the idiom
`Proposal`, `Handoff`, `Delivery` and `RecheckTarget` already use. *Unsettled* is computed —
`{prepared keys} − {recorded keys}` — so there is no phase field anywhere to go stale, and the
newest preparation is inspected and the scan then **stops**: answering `None` fails closed, where
searching backwards would hand a recovery an older episode and re-dispatch it.

**Persist *and* recompute *and* require agreement.** `ReconcileNode` reads the stored key and
independently re-derives `idempotency_key(case_id, latest_proposal.tool_name, latest_proposal.args)`
from the durable `PLAN_PROPOSED` entry, and **refuses if they differ** rather than preferring one —
`recheck_target`'s consistency clause applied to the problem it was written for. Storing alone loses
drift detection; recomputing alone loses crash-auditability. The logical action identity and the
provider idempotency key are **one value** by design: two derived identically would be two answers
to one question, and the preparation payload is where a divergence would be expressed if a provider
ever forced one. Run id, trace id and process id are none of these and may never be.

**Recovery asks no model anything.** No planner, no triage, no proposal, no new arguments, no new
identity — `tests/test_reconcile_writers.py` sweeps the recovery path for the five names that would
mean otherwise, and every recovery test runs against stubs that raise if touched. This also
neutralises the `reason` drift hazard **on the recovery path only**: recovery reads the arguments
that were authorised, so a punctuation change needs a fresh planner call that never happens. The
hazard is untouched on the replan path, which is the next roadmap item.

**Replay is licensed by the provider's write contract and never by an observation.**
`careloop/graph/recovery.py` declares a `RECOVERY_CLASS` per effect tool with an import-time
coverage check, the same arrangement as `VERIFIERS` and `RESULT_TYPES`, and `may_replay` reads that
declaration **before anything is read**. There is no `ABSENT → retry` rule anywhere and nowhere to
express one: `may_replay(action_type, observers=…)` has no parameter for an outcome, a status or a
diagnostic. `ABSENT` says the provider does not hold the effect *now, in the world this client
addressed* — which after a world reset or a lost session header is a false absence — and it is never
a statement that the write may be repeated. Only `book_appointment` is `IDEMPOTENT_REPLAYABLE`;
`send_email`, `upload_document` and `grant_portal_access` are `NOT_REPLAYABLE` and park.

**A replay also requires an independent observer bound to the run, and that is not a convenience.**
A replay nothing could read back would be recorded and then verified closed-loop, so the case would
reach `DONE` on CareLoop's own receipt for an effect it was not sure had happened — certifying by
*removing* evidence, which is the hazard `VerifyNode._nothing_was_observed` closes for a re-check.
The two conditions coincide because one collaborator turns on both the real write and the read that
observes it, and `ConflictingExecutors` makes anything else unconstructable.

**The replay happens while the case is `EXECUTING`, and that is what keeps `rules.yaml` untouched.**
Every effect rule carries `case_state_in: [PLANNING, READY_TO_EXECUTE, EXECUTING]` and
`deny-external-effect-outside-execution-states` refuses the rest, so a replay issued from
`WAITING_EXTERNAL` would be **denied**. Reconciliation re-claims the attempt through
`WAITING_EXTERNAL → EXECUTING` first — written by `ReconcileNode` alone, swept — and the replay then
re-passes the identical verdict with identical inputs. The state machine bends to the authority
layer; the authority layer does not bend to it. **No rule, argument class, trust level or approval
semantic changed.**

**`WAITING_EXTERNAL` now means one exact thing: a dispatch of a durably prepared action got no
answer at all.** Not a parking lot and not a synonym for `AWAITING_HUMAN` — it is the one condition
where waiting is *true*, because CareLoop can settle it itself as soon as something answers and a
person can do nothing about a timeout. Every other way of not knowing parks: a provider that
answered and declined, an action type that may never be re-attempted, a run with no channel, and a
policy denial all get `EXTERNAL_OUTCOME_NOT_ESTABLISHED` and **no `Approval`**. Both pre-effect
entries were removed, so every entry is post-effect.

**`EXTERNAL_OUTCOME_NOT_ESTABLISHED` is the tenth `ReasonCode` and the fourth that parks a case**,
so no rule carries it and the engine never emits it. It is deliberately not
`EXTERNAL_STATE_NOT_CONFIRMED`: that one means CareLoop holds a receipt an independent read could
not confirm, and implies *check whether the appointment is right*. This one means CareLoop holds no
receipt at all, and implies *check whether an appointment exists*. Different errands, and the older
code's own text says "an action already carried out" — which on this path is the open question.

**Two nodes can send an effect and one function decides what an unestablished outcome does.**
`ExecuteNode` reaches that fork when a first attempt's response is lost; `ReconcileNode` reaches it
after a crash. The situation is identical, so `_stop_with_the_outcome_unestablished` is the only
writer of `WAITING_EXTERNAL` and both nodes route through it. Transport failure is translated once,
in `careloop/graph/recovery.py::dispatch_prepared`, which is where HTTP stops on the dispatch path
exactly as `_clinic_readback` is where it stops on the observation path — nothing above either knows
a clinic speaks HTTP. A `PolicyDenied` and an `IdempotencyKeyCollision` pass through untranslated,
because the first is the authority layer refusing and the second is CareLoop being wrong about its
own identity.

**A survived ambiguous transport failure used to end the case `FAILED`, and that was the worst of
the three.** Reproduced against a real clinic on a loopback socket: the write landed, Demo World
held the appointment, and CareLoop marked the case `FAILED` — terminal, unrecoverable — with its own
`ACTION_PREPARED` entry naming an action nobody would ever look for again. `FAILED` said "this did
not happen" about an effect that had. It now waits.

**Concurrent recovery is safe, and the guarantees are attributed rather than claimed.** At most one
*durable record* is the **database** — the guarded `UPDATE … WHERE version = ?`. At most one
*appointment* is the **provider** — Demo World's own key map. That the two are about the same action
is the **application** — the frozen key both workers reuse. `RunAlreadyInFlight` is in-process
scheduling and is not durable. **No lease, no claim table, no distributed lock**, and none may be
added: a lease would duplicate the CAS and add a failure mode of its own.

Two concurrency defects were found and fixed, and both were reachable only once a sweep made two
workers on one case ordinary. `run_case` caught **every** exception and marked the case `FAILED`, so
a `VersionConflict` — a case that is *ahead*, not broken — had the loser terminate the winner's
work; the catch is narrowed and `_fail` carries its own guard, and both halves have separate
mutants. Separately, a worker whose dispatch edge selected a case mid-effect could arrive at
`ReconcileNode` after another worker had legitimately settled it, find the preparation settled,
and commit `VERIFYING → FAILED` — **successfully**, because reading fresh is exactly what stops the
CAS catching it. Found by two real threads rather than by review, and reproduced deterministically
before the guard was written.

**Crash points F and H are structurally impossible and G already recovered.** `_record_action` and
`→ VERIFYING` share one `save`, as do the verification outcome and `→ DONE`, and
`SqliteCaseStore.save` puts the case document and its timeline rows in one `BEGIN IMMEDIATE` …
`COMMIT`. `dispatch → verify` has routed a case persisted mid-verification since long before this
work. The saga's scope is exactly A–E.

**The startup sweep is one trigger, and it is the smallest one that works.** `resume_unsettled`
sweeps `EXECUTING` and `WAITING_EXTERNAL` and schedules a run for each. **Anything in one of those
states when a process starts is orphaned by definition**, because that process has scheduled nothing
yet — which is the whole safety argument. It schedules and decides nothing: no tool, no argument and
no key is read there. No EventBridge, no SQS, no worker; a real scheduler is a later *trigger*, not
a later design. The second trigger — a wake inside the living process — is below, and it is exactly
that: another *when*, over the same body.

**Reconciliation adds no cycle, so invariant 5 needs nothing.** `outgoing(reconcile)` is the single
edge to `verify`; its other three endings satisfy no condition and end the run. Retries happen
across runs, never as another lap, so nothing counts them.

**The nine telemetry attributes are still nine and `CareLoopSettlementIntegrity` is untouched.** A
recovery replay is a real `POST`, so it claims `PERFORMED` — never `SUPPRESSED`, which asserts no
HTTP write can be in the trace. A readback-only recovery claims `NOT_IN_THIS_TRACE`. That last one
exposed a **pre-existing** defect with nothing to do with the saga: `VerifyNode` tested
`entered_verifying_from(case) is AWAITING_HUMAN`, a durable fact standing in for a run-scoped one,
so a verification resumed after a crash emitted **five** attributes instead of nine and the frozen
evaluator judged a correct trace `FAIL`. Measured, then repaired in `careloop` rather than in
`evaluators`, because the evaluator was right. The condition is now a one-way, telemetry-only
`RunContext.effect_claimed` flag with four writers and exactly one reader, swept so no policy, tool,
lifecycle or API module can name it — a run-scoped flag is admissible **only** because nothing that
decides anything may read it.

`tests/test_saga_chaos.py` carries both proofs: a real child process killed with `os._exit` on the
line after the clinic confirms, and a real write whose response is discarded. Both converge on one
appointment, one logical action, one durable result, the original key, zero model calls, a `MATCHED`
independent read and `DONE`. A mutant that mints a fresh key on recovery turns four of them red —
and the failure it produces is `ClinicRefused 409`, which is Demo World's own slot-collision rule
standing where an authority decision should be, exactly as session Q measured.

**`book_appointment.reason` was unstable at the punctuation level, and Patch E measured the cost.**
Reproposals alternated between `"…for cardiology review"` and `"…for cardiology review."`, which
moved the argument digest, moved the idempotency key and defeated duplicate suppression at both
layers — reproduced in production, where each miss sent a second booking that only Demo World's slot
rule refused, ending the case `FAILED` on `ClinicRefused`. This is the session-Q hazard observed
live rather than inferred, and it is why the suppressed and diverged rows each needed retries. Both
halves are closed below: the wording can no longer vary, and a variation would no longer be a second
action if it did.

## A waiting case wakes itself

**`WAITING_EXTERNAL` was recoverable and not live, and that is now closed.** The saga froze the
identity, gave the state a producer and a meaning, and made `ReconcileNode` converge on any later
run. What it did not supply was a later run: `resume_unsettled` fires at process startup and nothing
else ever did, so a case that landed there at nine in the morning sat until somebody restarted
CareLoop — correct, durable, rendered as "Waiting for a reply", and stuck. Open decision 37.

**The wake decides exactly one thing: *when* another run happens.** Not what is attempted, not
whether it is authorised, not which arguments or which key, not whether the effect succeeded and not
whether the case may be certified. It schedules an ordinary graph run over durable state that
already exists, so every guarantee it leans on is the saga's — the same `PLAN_PROPOSED`, the same
`ACTION_PREPARED`, the same idempotency key, the same `args_digest`, the same policy verdict, the
same independent read-back. **No rule, state, transition, `ReasonCode`, timeline kind, telemetry
attribute, schema or persisted field changed**, and there is no durable retry field anywhere.

**Two triggers, one body.** `ApiDeps.register_recovery(case_id)` is the single recovery
registration; the startup sweep loops over it and a wake calls it for one case, so *"what a recovery
does"* has one definition and the triggers differ only in *when*. It reads no tool name, no
argument, no digest and no key — a case id and the `Delivery` that case is carried forward under —
and `ApiDeps.start_run` is the only way any run begins, which is what makes a run's consequences one
statement rather than five.

**The arming decision reads the store, never the exception.** `start_run` wraps the runner and, in a
`finally`, re-reads the case: `WAITING_EXTERNAL` arms a wake, anything else withdraws one and clears
the backoff. A run ends five ways — rest, park, fail, the SDK's bound, a raising node — and none of
them is the question; the durable state afterwards is. `finally` rather than `else` because a run
that lost a compare-and-set race *raised*, and the case the winner left behind may well be waiting.

**Delays are `5s, 15s, 45s, 120s, 300s`, the last repeating, and the counter is operational only.**
No jitter, no random source, no persisted attempt count, no retry timestamp, no maximum. The count
advances **only** on a registration that really started a run — a wake that was deferred, that found
another run in flight, or that found the case already settled attempted nothing and moves nothing.
A restart begins the schedule again, which is correct: a new process has new information, and the
sweep has just asked for itself.

**Nothing escalates because time passed.** A timeout is not evidence about the outside world, so
there is no attempt budget whose exhaustion parks the case and no path from *"this has taken a long
time"* to `AWAITING_HUMAN`. A provider that stays away costs a bounded number of attempts per hour
and a case that goes on saying, truthfully, that it is waiting. The visible cost is two
`STATE_TRANSITION` entries per attempt — roughly 576 a day at the ceiling — which is the honest
price of an append-only trail that records every attempt, and if it ever matters the fix is a
store-level timeline bound and **not** a fabricated verdict.

**A wake cannot exist for an effect that may not be re-sent**, and that needs no new guard.
`WAITING_EXTERNAL` is written only where `may_replay` says yes, which requires an
`IDEMPOTENT_REPLAYABLE` class *and* a bound independent observer — so `send_email`,
`upload_document` and `grant_portal_access` park with `EXTERNAL_OUTCOME_NOT_ESTABLISHED` and the
state a wake is armed for is unreachable for them. `RECOVERY_CLASS` did not change and must not.

**Stopping a wake is the hard part, and it is a linearization point rather than a check.** A wake
races three teardowns — a demo reset clearing the store, an idle eviction ending the visitor's
clinic, and shutdown — and one that registered a run *after* its world was destroyed would drive the
graph against an emptied store or a clinic session Demo World had been told to forget. So the gate
check and the registration happen inside one lock: **once `ReconcileWaker.stop()` returns, no wake
it holds can register another run**, and a wake already inside the critical section finishes first —
`stop()` cannot return until it has — which is why every lifecycle that stops a waker then *looks
again* for an active run instead of assuming there is none. `stop` cancels nothing, deliberately:
eviction runs in a Starlette worker thread, where cancelling a task is not safe, so a stopped wake
sleeps out its delay and finds the gate shut, leaving a bounded memory tail and **no capability**.

**Stopping is permanent; closing is not, and that distinction is where liveness lives.** An eviction
that finds a run in flight backs off and a refused reset changes nothing, so both close the gate
*reversibly* — and a wake that falls due inside such a window is **deferred, not spent**. It keeps
its intent, counts as no attempt, and is restored when the gate reopens; `reopen` hands the re-arming
back to the captured loop through `call_soon_threadsafe`, because arming creates a task and the
caller may be a worker thread. Consuming it instead would have a momentary lifecycle event silently
cost a still-waiting case the only opportunity it had scheduled. A **successful** reset calls
`discard_deferred` after clearing, because only the body of that scope can tell whether the cases
those intents named still exist.

`tests/test_reconcile_liveness.py` carries the property and
`tests/test_reconcile_wake.py::TestTheStopBoundaryIsLinearized` the boundary. The judge-facing proof
is `TestTheCaseWakesUpWithoutAnybodyAskingIt`: a real Demo World on a loopback socket behind a
transport that can be switched off, a real booking that nobody answers, **no restart and no human or
API action**, the clinic coming back, a wake CareLoop scheduled for itself, one appointment, one
`ActionRecord`, the original key, an independent `GET` returning `MATCHED`, and `DONE` — with the
planner and triage seams raising if either is so much as asked. It complements the hard-crash proof
in `tests/test_saga_chaos.py` rather than replacing it: that one earns *"a process can die mid-effect
and a later one converges"*, and this one earns *"nothing had to die, and nobody had to notice"*.
Eighteen mutants each turn their own tests red, including a wake that routes to the planner, one that
mints a fresh key, one that bypasses `ReconcileNode`, a due wake consumed by a temporary close, a
reopened gate that restores nothing, and a startup sweep removed altogether.

**What this does not do.** It creates no durable scheduler and claims none: the schedule is in
memory, dies with the process, and is not a business fact — invariant 8 is untouched. It does not
cover a *visitor's* world across a restart, because those stores are `:memory:` and never could.
There is no queue, no worker, no lease, no claim table and no distributed lock; two processes over
one durable case are still made safe by the store's compare-and-set and the provider's own
idempotency, exactly as before, and the per-case dedupe claim is scoped to one waker and says so.

## A judge can watch an ambiguous outcome recover

**The saga's strongest property was unreachable from a browser, and that is now closed.** Nothing an
HTTP client could press produced an ambiguous external outcome, so `WAITING_EXTERNAL` had never been
seen outside the test suite — `e2e/tests/unreachable-states.spec.ts` says so in its own words, and
mocks the post-effect states because a browser cannot reach them. `POST /api/demo/scenarios/recovery`
is the fifth demo scenario and the only one that arms anything: the clinic really creates the
appointment, the answer is discarded below CareLoop's HTTP client, and **the shipped saga, the
shipped five-second wake and the shipped read-back do everything after that**. No rule, state,
transition, `ReasonCode`, timeline kind, telemetry attribute, payload key, API schema or evaluator
changed, and no API allowlist widened — every field the presentation reads was already projected.

**The claim is "one appointment", never "one request", and the difference is the whole of it.**
Reconciliation issues a **second real `POST`** under the identity frozen before the first left the
process, which is precisely why Demo World answers out of `_by_key` with the appointment it already
made. Measured end to end in `tests/test_recovery_proof.py`: **2 provider `POST`s, 1 action identity,
1 `ActionRecord`, 1 `PLAN_PROPOSED`, 1 appointment, 1 independent `GET` returning `MATCHED`, `DONE`**.
The tidier sentence — *"CareLoop did not send the booking twice"* — is **false**, and it is exactly
what a well-meaning editor writes. `tests/test_recovery_copy.py` sweeps `frontend/src/` and
`docs/judges.md` for it and for eight near-synonyms, with both halves proven: the plausible false
sentences are caught, and ten true ones — *"no second appointment"*, *"CareLoop will not repeat the
action"*, *"tried twice to work out a valid next action"* — are left alone. It strips TypeScript
comments before matching, because this repository explains rules by *stating the false sentence*, and
a sweep that could not tell a claim from an explanation of the claim is a sweep that gets deleted.

**There is no model-call count on screen, and that is a deliberate refusal.** The durable trail
proves `PLAN_PROPOSED == 1`; it does **not** prove no model was invoked, because a model can be asked
and fail without ever producing a plan. So the evidence block says *"Plans made · 1 · no new plan
after the answer was lost"* and stops there. The stronger claim — zero planner, triage and model
calls during recovery — is proven where it can be: `PhasedAgents` hands the second and every later
context for that case an `ExplodingPlanner` and an `ExplodingTriage`, so a recovery *cannot* consult
one, and the seams are counted as well so it is measured rather than inferred. The two are asserted
separately and the docs say which is which.

**The fault is a transport, and it fires only after the provider says it committed.**
`careloop/api/faults.py::LostResponseTransport` wraps a real `httpx.BaseTransport`. The inner
request goes **first, always**; the loss is injected only when the answer is `201`, which is Demo
World *stating* it created the appointment — so an ambiguity can never be manufactured over a write
that did not land. A `409`, a `404`, a `200` replay, a read and every other route pass through
untouched, which is also what keeps the independent read-back that settles the case out of reach of
the seam. Deciding before the request instead turns 7 assertions red; firing on any status turns 4.
Nothing in the wrapper knows about cases, states, `ActionRecord`, policy, the planner, verification
or settlement, and `tests/test_faults.py` sweeps `graph/`, `policy/`, `state/` and `tools/` so none
of them can name it.

**The intercepted answer is released, not abandoned.** A transport-returned `httpx.Response` holds a
pooled connection until something consumes it, and nothing above `handle_request` will ever see this
one — the method raises instead of returning it. So it sets `response.request` (which the transport
layer does not, and which `read`/`close` both require), reads the body and closes it. Asserted
against a stream the test owns, in **both** directions: a discarded answer must be drained and
closed, and a passed-through answer must be left alone for `httpx.Client` to read. Removing only the
release turns exactly one assertion red.

**The fault belongs to one case, and the binding is which object a run holds rather than a check
somebody remembered.** A world with a clinic builds **two** `ClinicClient`s into the same Demo World
world; the context factory hands the losing one to runs for the case a recovery scenario armed and
the ordinary one to every other run. So a booking belonging to any other case in that same visitor's
world travels a transport containing no fault at all. This is not an argument from the scenario lock:
`RunRegistry.start` refuses only a second run of the *same* case, so an interleaved booking is
genuinely reachable, and `tests/test_recovery_proof.py::TestAnUnrelatedBookingCannotConsumeTheFault`
drives one for real — the unrelated case books **first**, while armed, and reaches `DONE` with the
loss still unspent. `case_id` is already a parameter of every `ContextFactory`, so no header, query
parameter, token, endpoint or run-scoped flag was invented to correlate any of this.

**One production arming site, swept.** `careloop/api/app.py::arm_the_lost_booking_response`, called
between the scenario's reset and its delivery, under `deps.scenarios`.
`DemoScenario.loses_the_booking_response` is data, so the route reads a scenario's own declaration
rather than comparing a key to a literal, and the sweep in `tests/test_demo_recovery.py` fails if a
second caller appears — the arrangement `CaseStore.clear` already has. **Default off**:
`ApiDeps.faults` is `None` on every CareLoop with no clinic, the routes do not exist without
`demo=True`, and a world with no clinic is refused with a truthful 409 rather than shown a recovery
that demonstrates nothing. `_reset_quiesced_world` disarms — the second of two disarms, since the
transport consumes its own — so a scenario that ran and one abandoned halfway leave the world
identical, and a *refused* reset changes nothing at all, arming included.

**`test_api_demo.py`'s distinctness invariant moved axis, deliberately.** `referral` and `recovery`
deliver the *same* inbound event, because the beat is what happens when the answer to a perfectly
ordinary booking never comes back and a different referral would make the scenario about the
document. So "event ids are distinct" — which was the property standing in for the real one — is now
"no two scenarios agree on everything a judge can perceive", with the pair that exercises it asserted
directly.

**Two `STATE_TRANSITION` rows reach the spine and no others.** `_stop_with_the_outcome_unestablished`
writes no timeline entry of its own on the waiting branch, so `EXECUTING → WAITING_EXTERNAL` **is**
the record that the answer never came, and `WAITING_EXTERNAL → EXECUTING` is the record that the
frozen attempt was re-claimed. Keyed on the closed `from`/`to` enum and **never** on the transition's
free-text `reason`, which a backend edit could reword. Every other transition is still the state
machine narrating itself and is still dropped; an ordinary booking's spine is unchanged, asserted
row for row. `WAITING_EXTERNAL`'s status copy is sharpened globally — *"NO ANSWER FROM THE CLINIC"* —
which is truthful because the state means exactly one thing, and would stop being truthful the moment
anything else writes it.

**The evidence block renders from a settled signature, never from a state.** `DONE`, plus a frozen
identity, plus a real waiting episode, plus a real re-claim, plus an `ACTION_EXECUTED` carrying *that*
identity, plus exactly one `ActionRecord`, plus an independent read. A case that is merely
`WAITING_EXTERNAL` fails it, which is the point: the outcome is genuinely unknown then, and a block
saying "one appointment" would assert something CareLoop does not yet know. Only a short key prefix
is shown. A mutant that renders it from the state alone turns 5 frontend tests red.

**Real process death is not reachable from the live demo, and the reason is not squeamishness.**
Visitor worlds are `SqliteCaseStore(":memory:")`, so killing the process there would destroy the very
durability the claim is about — the claim would be *false*, quite apart from taking every other
visitor's world with it. The split is stated in `docs/judges.md`: the live product demonstrates
provider-commit-plus-lost-answer recovering with no restart, and `tests/test_saga_chaos.py` (a real
child process, a real SQLite file, `os._exit` on the line after the clinic confirms) plus
`scripts/resume_demo.py` carry the hard-crash property. **No AgentCore claim is made from this
scenario**; the deterministic trace evaluation stays what it was, recorded and operator-driven.

## Four questions about one argument

**An argument answers four questions, and folding any two of them together is how each of the last
several defects happened.** They are declared in four places on purpose:

```
AUTHORITY      which values permit the action    ArgSpec.arg_class + ArgSpec.trust_key, read by rules.yaml
PAYLOAD        which values are transmitted      ToolSpec.arg_names, in full
IDENTITY       which values are the same action  ToolSpec.identifying_args
VERIFICATION   which values must match after     graph/verify.py COMPARED_ARGUMENTS / OBSERVABLE_STATE
```

For `book_appointment` after this session:

| Field | Authority | Payload | Identity | Verification |
| --- | --- | ---: | ---: | ---: |
| `provider_id` | `DESTINATION`, `TRUSTED` required by DENY and ALLOW | yes | **yes** | yes |
| `patient_id` | `VALUE`, `CONTROLLED` required by DENY and ALLOW | yes | **yes** | yes |
| `slot_start` | `VALUE`, `CONTROLLED` required by DENY and ALLOW | yes | **yes** | yes |
| `reason` | `VALUE`, no `trust_key`; `args_system_authored` on the ALLOW | yes | **no** | yes |

**Two new declarations, both on `ArgSpec`, both defaulting to today's behaviour.**
`author: ArgAuthor` says who composes the value — `MODEL` is the default, so a newly declared
argument behaves exactly as one declared last year. `identifies: bool` says whether the argument is
part of *which logical action this is* — `True` is the default, for the same reason. Opting out of
either is the deliberate act, and each is checked at import.

### `reason` is composed by CareLoop, not by a model

**`book_appointment.reason` is `SYSTEM`-authored and its value is one fixed literal**,
`AUTHORED_VALUES["book_appointment", "reason"] == "CareLoop appointment scheduling request."`, never
interpolated. It was the last free-text field on the one tool whose effect really leaves the process
*and* the only tool with an autonomous `ALLOW` path, so nobody saw it before it was sent. Raw
document pages are recorded verbatim as `excerpt:*` facts and rendered into the planning prompt, so
any span of an attacker-authored page could be copied into it, sent to the clinic over HTTP,
persisted there and echoed back on every read. Reproduced end to end against Demo World on a
loopback socket before the repair; `tests/test_outbound_content.py` is where the canaries are now
required to be absent from the **clinic's own record**.

The sentence makes **no clinical or purpose claim**, deliberately. Nothing in CareLoop produces a
`CONTROLLED` fact for why an appointment is needed — no `purpose` key, no validator, no promotion —
so anything richer would be inventing information, and *"see the referral on file"* would assert a
link nothing establishes. A richer vocabulary must begin with a promotable structured purpose fact;
it is not this patch.

**Two enforcement points, and they are independent.** *Structural, primary:* a `SYSTEM`-authored
argument is omitted from every model-facing proposal schema, so `propose_book_appointment` carries
exactly `rationale`, `provider_id`, `patient_id`, `slot_start` — a model that emits `reason` anyway
is refused by `extra="forbid"` rather than having its value dropped. `ProposalToolBase.canonical()`
is the single production seam that fills it, from the registry and from nothing else.
*Deterministic, at the authority layer:* `allow-booking-on-validated-slot` carries
`args_system_authored: [reason]`, so a hand-built `PlannerIntent`, a replayed corpus entry or a
future seam supplying its own text matches no `ALLOW` rule. Measured independent: a mutant reverting
authorship turns 21 red including the clinic-side canaries, while a mutant removing only the rule
clause plus its loader check turns 13 red and leaves every end-to-end canary green.

**Authorship is not trust, and no `Fact` is minted anywhere.** `reason` still declares no
`trust_key`, still resolves `UNTRUSTED` in every `Decision`, and a rule requiring its trust still
fails at load. `args_system_authored` asks *did a model write this string* and is answered before
any rule is matched — the same separation `args_approved` has from `arg_trust_at_least`.

**No new `ReasonCode`, and no `DENY` rule.** A refused wording gets `NO_RULE_MATCH` from
deny-by-default, which is what a malformed capability already gets. A distinct code would tell a
planner that the *content* of a descriptive field is what decided the refusal — precisely the probe
hint invariant 6 exists to withhold — and the field is not in the model's schema, so the refusal is
unreachable from it.

**The loader refuses a ruleset that leaves an authored argument unguarded.**
`load_rules_from_mapping` is four named steps now: `parse_rules`, `validate_rules_resolve`,
`validate_authority_coverage`, `validate_authored_coverage`. There is no parameter that turns the
fourth off, and `evals/replay.py::permissive_engine` still omits only the third — its violation is
the property that mutant exists to measure, and authorship is not what it measures.

**One deliberate behaviour change beyond the hole itself.** An *empty* `reason` used to reach
`ALLOW` and book autonomously; this file called that the open content question. It is closed — the
empty string is not the authored literal. Nothing about *completeness* moved: the call still
supplies the key, so it is refused for what its value says rather than as a malformed capability,
and `tests/test_policy_completeness.py` keeps the two distinguishable.

### `reason` does not identify the appointment

**`idempotency_key` covers exactly the arguments the registry declares identifying; `args_digest`
still covers every one of them.** For a booking that is `(case_id, action_type, provider_id,
patient_id, slot_start)`. The provider proves the semantics rather than a docstring asserting them:
Demo World's `Clinic._taken` is keyed on `(provider_id, slot_start)`, so one slot is one appointment
however it is described. CareLoop's identity is deliberately *finer* — it adds `patient_id` and the
case — because booking a different patient into one slot is a different action and CareLoop must not
depend on the clinic's slot rule to say so.

**The two digests are never folded together, and there is no identity-only digest exposed
anywhere.** `args_digest` is the human-consent capability identity
(`Case.approval_for(tool_name, args_digest)`) and the executor-received-what-policy-authorised check
in `verify_action`; narrowing it would trade one defect for a worse one. Every test that asserts key
invariance under rewording asserts digest divergence beside it, so a mutant that narrowed both would
still turn red.

**Nothing is normalised.** `canonical_value` still takes a `str` verbatim — no case-folding, no
trimming, no unicode normalisation — because normalisation is a laundering surface and the
exact-match trust rule that refuses `"PT-4417 "` depends on its absence. `"review"`, `"review."`,
`"Review"` and `" review "` key alike because the argument is *declared* non-identifying, not
because the strings were made equal.

**An argument may leave identity only where verification still compares it.**
`check_non_identifying_arguments_are_verified` is an import-time coupling check over
`COMPARED_ARGUMENTS`, so narrowing identity can never become a way of narrowing scrutiny. It would
refuse `send_email.body` — which no verifier compares — and a mutant removing `reason` from both the
comparison map and `OBSERVABLE_STATE` makes the package fail to import rather than turning a test
red. Two further import-time checks sit beside it: every `DESTINATION` and `AUTHORITY` argument
identifies, and no tool may identify nothing.

**Reconciliation now requires the frozen key *and* the frozen argument digest to agree.**
`Preparation.args_digest` has been recorded since the saga was written and nothing read it. With the
key narrowed, a proposal whose descriptive text had drifted would satisfy the key clause and then be
re-issued under the recorded key carrying different details — Demo World answers `409 KeyReused`,
which is an external service's consistency rule standing where an authority decision should be.
Measured: a mutant removing only that clause sends the write and parks on the clinic's refusal.
Unreachable under the shipped flow, since a second `PLAN_PROPOSED` needs `PLANNING` and no
post-effect state reaches it — which is exactly why it is written down.

**Recovery is otherwise untouched and still reads what was frozen.** No planner, no triage, no new
identity, no reconstruction from `AUTHORED_VALUES`: the arguments come from the durable
`PLAN_PROPOSED` entry that produced the preparation, and `tests/test_reconcile_writers.py`'s sweep
is unchanged. Every recovery test runs against model seams that raise if touched.

**Which layer actually acts on the reopen path, stated exactly.** `DONE` → `reopen_case` →
`GATHERING_EVIDENCE` → a fresh planning pass is the one surviving route to a second proposal over a
completed booking, and it was the reachable form of the punctuation hazard. On the shipped
configuration **authorship** is what neutralises it: a scripted planner that tries to repropose
`"review."` has the wording dropped at the seam, so `PLAN_PROPOSED` carries CareLoop's own sentence
and the reproposal is byte-identical. Identity narrowing is defence in depth *there* and
load-bearing on the reconciliation path and for any future descriptive argument that is not
`SYSTEM`-authored. `tests/test_action_identity.py` asserts the layer that acts rather than the one
the test is named after — one `DUPLICATE_SUPPRESSED`, one clinic write, one appointment, one
consumed slot, all read off Demo World.

### The rollout boundary

**Changing the identity definition changes the key for every historical booking, and there is no
migration.** Historical keys are probably reconstructible from timeline data — the arguments are on
`PLAN_PROPOSED` and the old derivation is in git — so the honest statement is not that migration is
impossible. It is **deliberately rejected**: deployed state is synthetic, and a clean reset is safer
than maintaining two booking-identity eras. There is no legacy-key fallback and there must never be
one; a second identity algorithm is a second answer to one question, which is the shape this
repository has closed three times.

**A clean demo-state reset is required immediately before the first deployment containing this
boundary**, and was deliberately *not* performed in the session that wrote it. Without it, three
paths break, and the third is the one that matters: a case persisted `EXECUTING` or
`WAITING_EXTERNAL` fails closed, a case persisted `VERIFYING` fails closed, and a `DONE` case booked
before the boundary and reopened after it **misses suppression and sends a real second `POST`**,
refused only by Demo World's slot rule.

**What did not change.** `CareLoopSettlementIntegrity` is untouched and the nine telemetry
attributes are still nine — `reason` stays in `Proposal.args`, in the provider payload, in
`args_digest`, in `_BOOKING_COMPARISON`, in `OBSERVABLE_STATE` and therefore in both settlement
state digests, so `careloop.effect.fields` still renders `patient_id,provider_id,reason,slot_start`
and the frozen evaluator's `EXPECTED_FIELDS` still matches. No AWS infrastructure, no evaluator
code, no observability adapter, no Demo World semantics, no prompt and no email content moved.

## Providers declare capabilities; the saga consumes two of them

**The saga does not know how a clinic or an object store works, and that is now machine-checked
rather than reviewed.** Two real providers with contracts that share nothing reach one deterministic
recovery engine, and the engine consumes exactly two answers about a provider — *may the frozen
identity be re-issued*, and *is an independent observer bound* — assembled by `may_replay` from
`RECOVERY_CLASS` and `RunContext.readbacks`.

**No capability framework was added, deliberately.** The audit's question was whether a third
provider would need another hidden `if tool == …`, and the answer is no: the two declarations plus
`Handler` and `ReadBack` already span every combination that exists. A `DuplicateSemantics` enum
would be read by the engine and then branched on identically, because *how* a provider delivers "no
second effect" is consumed entirely inside its adapter — the clinic replays its record, S3 refuses
with `412` and `put_once` fetches what is there. `RecoveryClass` is defined by the outcome it
guarantees rather than by the mechanism, and that is what lets one member cover both.

```
AUTHORITY        may this be re-issued          RECOVERY_CLASS, a write-contract fact
AVAILABILITY     can anything else see it       RunContext.readbacks, a fact about this run
PRODUCTION       what the write yields          Handler — including reconstruction, adapter-local
OBSERVATION      what the world holds now       ReadBack — a fresh read, never the write's answer
```

**The four combinations are all live and all correct.** A replayable type with no observer bound
parks; an observable type may be `NOT_REPLAYABLE` and still be independently verified, which is the
shape a transactional mailer with a delivery log would take. What cannot be *declared* is
replayable-with-nothing-able-to-observe-it: `check_replayable_types_are_observable` refuses it at
import. That is a coherence check and not a merge — `may_replay` still fails closed per run, and
availability is not capability.

**Reconstruction is not observation, and the object store is where the two are easiest to confuse.**
A `412` is answered by a `GetObject` on the *dispatch* path, which settles what CareLoop's own frozen
identity already created; the settlement read is a second `GetObject` in `VerifyNode`, keyed on the
recorded result. `tests/test_upload_recovery.py::TestTheReconstructionReadIsNotTheSettlementRead`
counts both and changes the object between them. Collapsing them would be cheaper, would look green
on every passing run, and would delete the only evidence verification has.

**Absence is never a licence to re-send, and there is nowhere to write that it is.** `may_replay`
has no parameter for an outcome and `recovery.py` imports nothing from `verify.py`, so the licence is
decided before any read is made.

**Evidence, and the four guards that keep it honest.**
`tests/test_demoworld.py::TestIdempotency` measures the clinic's contract, `docs/objectstore-contract.md`
and `tests/test_objectstore_contract.py` the store's. `tests/test_provider_seams.py` proves by AST
that no effect-tool name, provider identifier, provider import or raw status code reaches the
recovery engine — with declaration tables exempt, because `RECOVERY_CLASS` and `DEFAULT_HANDLERS`
*must* name every tool and a branch must not.
`tests/test_third_provider_seam.py` carries a synthetic provider with a third duplicate contract
(submit answers with a receipt and never a record) through strand → reconcile → record → verify →
`DONE`, importing no production provider client. `tests/test_evidence_citations.py` requires every
repository path any module cites to exist — written because `RecoveryClass`'s clinic evidence
citation named a file that never existed, twice.

**One inconsistency is consciously deferred.** Both providers can say *"your frozen identity is
already taken by something you did not write"*, and during **reconciliation** they land in different
lifecycles: the clinic's `409` becomes `DispatchRefused` and parks `AWAITING_HUMAN`, while the
store's `412`-plus-digest-mismatch raises `ObjectIdentityConflict`, escapes the node and ends the
case `FAILED`. Both fail closed, neither creates a second effect, and on a *first* attempt the two
agree — `ExecuteNode` catches no `DispatchRefused`. So this is a lifecycle-vocabulary difference and
not a safety hole, and choosing between them is a real decision rather than a tidy-up:
`ObjectIdentityConflict`'s docstring argues for the loud failure on purpose, and `FAILED` is terminal
where `AWAITING_HUMAN` is the state that means a person must look. **Do not align them as a side
effect of something else.** If it is taken, do it at the node — making `ObjectIdentityConflict` a
`ProviderRefused` would also change first-attempt behaviour and re-label a CareLoop-identity error as
a provider fact.


## Commands

```bash
.venv/Scripts/python -m pytest -q      # offline, $0
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
```
