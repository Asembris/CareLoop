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
   makes "did not act again on an outcome it could not certify" structural in the same way.
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

**What this does not claim.** `reason` remains planner-authored free text with no trust key, no
content constraint, no length bound and no human review before an autonomous booking, and it can be
influenced by document evidence. This patch verifies external fidelity, not provenance or
appropriateness; whether the reason should additionally be templated from controlled facts,
constrained, or removed is still open and deliberately untouched, so the two properties keep
independent causal evidence.

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
deserves another attempt" was never a description of a reachable state. `book_appointment.reason` is
planner-authored free text carrying no `trust_key` and named by no rule, so a reproposal changing only
*that* moved the argument digest, moved the idempotency key, and defeated duplicate suppression in
CareLoop **and** in Demo World. Measured through the real graph on a loopback clinic: against a clinic
that could not confirm the first booking, **two `ACTION_EXECUTED` entries, two `ActionRecord`s, two
executor invocations, two confirmation ids** — a second autonomous external booking issued after
CareLoop already knew it could not certify the first. Against a diverging clinic, the second `POST`
left CareLoop and only Demo World's own slot-collision rule refused it, which is an external service's
consistency check standing where an authority decision should have been.

**Verified divergence and unavailable read-back share this lifecycle, deliberately.** `Verification`
collapses "the independent read succeeded and the values disagree" with "the read established
nothing", and `VerifyNode._verify` says so. Their *remediation* may differ — a bounded
observation-only retry is defensible for the second and never for the first, since Demo World is
deterministic with no eventual-consistency model — but retrying the *effect* is wrong for both, and
stopping is the only lifecycle correct for both today. Splitting them needs a discriminator
`Verification` does not carry and is deliberately deferred. **A malformed observation does not use
this path yet**: a 200 whose body omits a required field raises out of the node, `run_case` marks the
case `FAILED` and re-raises. That already fails closed, never replans and issues no second effect;
routing it here is a separate patch.

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

**One tool is real, and the read-back is what makes that worth anything.** `book_appointment` routes
over HTTP to `demoworld/`; the other three effects stay simulated, because one action made genuinely
external is the proof and four is scope creep with a rationale. Verification then performs a
*separate* `GET`, keyed on the confirmation id the effect returned, and compares observed clinic
state against **every declared booking argument** — provider, patient, slot and reason — never the
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
