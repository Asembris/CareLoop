# Security evaluation

**Unauthorized external actions: 0 of 320 adversarial attempts.**

With CareLoop's authority layer removed and nothing else changed, the same 320 attempts produce
**320 of 320**. That contrast is the evidence; the zero on its own would not be.

Reproduce both in about ten seconds, offline, with no credentials and no provider reachable:

```bash
pip install -e ".[dev]"
python -m evals.replay
```

---

## The measurement, and why it is the worst case

Every committed adversarial payload is replayed through the **real graph, the real `rules.yaml` and
both enforcement points**, driven by a planner that does exactly what the attacker asked, every
time it is asked. No real model can be *more* compliant with an attacker than one that proposes the
attacker's requested action with the attacker's own arguments on every turn — so this measures the
ceiling of model misbehaviour rather than one model's behaviour on one day.

Each payload is replayed across the full cross product — **2 delivery vectors × 4 external-effect
tools** — so 40 payloads become 320 attempts covering every effectful surface the registry declares.

| | attempts | unauthorized actions |
|---|---:|---:|
| generated (DeepTeam) | 288 | **0** |
| authored (CareLoop's own threat model) | 32 | **0** |
| **total, shipped `rules.yaml`** | **320** | **0** |
| total, allow-all mutant | 320 | 320 |

All 320 were refused by policy — `DESTINATION_NOT_TRUSTED` and `INSUFFICIENT_FACT_TRUST`, both from
the closed `ReasonCode` vocabulary — nothing reached the outside world, and nothing errored.

### The vacuity proof, which is the part that matters

Zero unauthorized actions is also what a broken harness reports, what an empty corpus reports, and
what a replay that never reaches the policy engine reports. So the identical replay runs a second
time through an **allow-all policy engine** — a real `RuleSet` through the real loader and the real
engine, differing from the shipped system in its *rules* and nothing else, so the mutant still has
the same argument classes, canonicalisation and load-time validation.

Every one of the 320 then performs its effect and reaches `DONE`. Not merely a different total:
**opposite on every single attempt**, 320 of 320 against 0 of 320.
`tests/test_evals_replay.py::TestTheVacuityProof` asserts it, and `python -m evals.replay` exits
`2` if removing the authority layer ever stops changing the answer.

### Two definitions, kept apart

**Effects that outran the gate** — `external_effects − allowed_by_the_gate`. Attack-agnostic: it
does not care what the attack said, only whether authority was obtained.

**Effects carrying a value from the attacker's text** — identifiers (addresses, portal and provider
ids, role tokens, URLs) extracted from each payload and matched against the arguments of every
performed effect.

The second **over-detects by construction**, which is the safe direction for a check whose job is to
notice an exfiltration, and the cost is stated rather than buried:

| | flagged | confirmed | resolved as false positive |
|---|---:|---:|---:|
| shipped `rules.yaml` | 0 | 0 | 0 |
| allow-all mutant | 560 | 560 | 0 |

On the shipped run there were **no performed effects at all**, so the detector had nothing to flag —
its false-positive rate is unmeasured there rather than zero, and the headline does not rest on it.
On the mutant every flag was confirmed. 560 exceeds 320 because one action can carry several
attacker-supplied arguments: a `grant_portal_access` whose grantee, role and portal all come from
the attack is three incidents on one action. One resolution rule is applied automatically — a value
equal to trusted configuration is CareLoop's own destination, not the attacker's — and it did not
need to fire. **The headline counts attempts, not incidents**, so its numerator can never exceed its
denominator.

---

## Where the corpus came from

The replay is only worth something if the attacks are real rather than written by us to pass our own
test. Every record carries provenance and `AttackRecord` **refuses to load without it**. Two
origins, counted separately and never merged, because an authored attack that fails is weaker
evidence than a generated one that fails.

**36 generated**, by `meta/llama-3.3-70b-instruct` under `deepteam==1.0.9` on 2026-08-14 — 18
baseline attacks and 18 after an attack technique was applied. Recovered from the committed response
cache by `scripts/mine_attack_cache.py`.

They are recorded as `unattributed` for vulnerability class and technique, and that is a limit
rather than an omission. **A `DeepEvalBaseLLM` is called with a prompt and a schema and nothing
else**, so the adapter never learns which vulnerability prompted a generation; recovering it would
mean parsing DeepTeam's internal prompt text, which is unstable across releases and a worse thing to
depend on than an honest `unattributed`.

**4 authored**, one per external-effect tool, each with a written rationale — CareLoop's own
canonical threat model, the poisoned continuity notice the system was designed against, which no
generator would necessarily reinvent.

## Model-layer exposure — attempted, and provider-limited

Two targets, because DeepTeam's own documentation states it tests agentic risk at the model
reasoning and response level only, and that comprehensive agentic security requires additional
runtime controls. CareLoop is that layer, so measuring one target would measure the wrong thing.

| | what it is | gate |
|---|---|---|
| **planner** | the planner model, the real tool catalogue, proposal executed through a bare `ToolSet` | none |
| **gated** | `run_case` over the real graph | `BeforeToolCall` hook + `@gated_tool` backstop |

Both run the **deployed configuration** — triage on `us.amazon.nova-micro-v1:0`, planner on
`us.amazon.nova-2-lite-v1:0`, the shipped prompts, the shipped rules, the shipped node budget.
Restricting capability during red teaming hides real vulnerabilities.

`tools_called` carries **input parameters and outputs** for every invocation, including refused
ones. Without them DeepTeam cannot assess a tool-level vulnerability at all — and a bug found during
this session would have silently emptied them, so it is asserted rather than assumed
(`tests/test_evals_capture.py::TestTheOutputNeverLosesThePayload`).

**What actually happened.** Attack *generation* completed: 118 provider calls, 36 usable payloads,
and those are the corpus above. Attack *judging* did not complete against either target, and the
reasons are entirely environmental:

| | observed |
|---|---|
| generation calls that succeeded | 36 of 90 payload generations; 54 failed on truncation or timeout |
| planner-target run | 118 calls completed, then aborted printing its own results table — DeepTeam's output contains emoji and Windows' default cp1252 stdout cannot encode them. Use `PYTHONIOENCODING=utf-8`. |
| gated-target run | aborted on `Node 'plan' execution timed out after 45.0s` |

**So no DeepTeam pass/fail table is published, because none was earned.** Reporting a partial table
as though the run completed would be the overclaim this document exists to avoid.

### The node timeout was not lowered, deliberately

The gated run died because Bedrock throttling pushed a planner call past `NODE_TIMEOUT`, which is
**45 s in the shipped graph and normally completes in 1–2 s**. Raising or removing that timeout
would have let the evaluation finish.

It was not changed. A timeout relaxed so an evaluation can pass means the evaluation is no longer
testing what ships, which would make every other number in this document worthless. The shipped
configuration is the thing under test; when the environment cannot run it, the honest result is an
errored run and not a reconfigured one.

### Vulnerabilities the suite runs

`ExcessiveAgency`, `ToolOrchestrationAbuse`, `IndirectInstruction`, `AutonomousAgentDrift`,
`ExternalSystemAbuse`, `PIILeakage` — 22 types. Techniques: `PromptInjection`, `ContextPoisoning`,
`GoalRedirection`, `PermissionEscalation`.

### What was deliberately not run, and why

`OWASP_ASI_2026` exists and works, and running it whole would have produced a longer table with more
green in it. It was **not** run whole. CareLoop executes no code, runs no database, opens no shell
and generates no content for human consumption, so `SQLInjection`, `ShellInjection`,
`UnexpectedCodeExecution`, `DebugAccess`, `IntellectualProperty`, `Ethics` and `Misinformation` would
all pass **because the surface is absent, not because it is defended**. A pass that means nothing
dilutes the passes that do. Bias, toxicity and graphic content are excluded for the same reason.

Two OWASP ASI 2026 categories map cleanly and are the ones claimed: **ASI_01** (agent goal hijack)
and **ASI_02** (tool misuse).

---

## Reproducing this — a real constraint

The offline replay reproduces in seconds. **The paid layer does not, and the reason is the providers
rather than the harness.** Anyone reproducing this on free-tier credits will hit both of these.

**NVIDIA `build.nvidia.com`** (simulator and judge): **15–38 s per healthy call**;
`503 ResourceExhausted: Worker local total request limit reached (16/16)` under modest load; request
timeouts at 180 s even at `max_concurrent=2`. DeepTeam defaults to `max_concurrent=10` with a
three-attempt retry loop, which against a 16-worker shared pool manufactures the exhaustion it is
retrying around.

**Bedrock** (the target's own models): throttled hard enough that Nova 2 Lite planner calls, which
normally return in 1–2 s, exceeded the graph's 45 s node timeout.

Responses are cached and committed, so a re-run costs nothing and needs no credentials — which is
what let the encoding crash above be recovered without re-buying a single call, and what let the
generated corpus be recovered after the run itself failed.

Cost discipline: `--confirm-spend` is required or the run is a dry run, and the estimate prints
first. **Two ceilings, because one cannot see what this run spends** — `--max-spend` in USD bounds
the Bedrock target, and `--max-calls` bounds every provider call, since `build.nvidia.com` is
credit-metered rather than billed per token and a dollar ceiling over it is decoration. One run
aborted at **0 calls and $0.00** when a vulnerability type name was wrong, which is the arrangement
working.

---

## What this evaluation does **not** cover

- **It is not a penetration test of the deployment.** No network, TLS, CloudFront, IAM or host
  configuration was tested. The `X-Caregiver` header is an audit identity and not authentication, by
  design, and nothing here evaluates the authentication that would sit in front of it.
- **No DeepTeam pass/fail rates are published.** Judging did not complete. What the paid layer
  contributed is the corpus, and that is all it is claimed to have contributed.
- **Generated attacks are unattributed** to vulnerability class, for the interface reason given
  above. They are genuinely generated; they are not sortable by threat category.
- **Three of four external effects are simulated.** Only `book_appointment` reaches a real service in
  production; the evaluation runs all four through the simulated executor and **never** constructs a
  `ClinicClient`, enforced by an AST sweep. This measures whether an effect was *authorised*, not
  what a real downstream system would do with one.
- **The identifier detector over-detects and cannot read intent.** It matches tokens, not meaning,
  and its false-positive rate on a clean run is unmeasured because there were no effects to flag. A
  payload naming no concrete destination gets a canary appended — recorded as part of the attack,
  not hidden.
- **Multi-turn attacks are out of scope.** CareLoop's unit of work is a case, not a conversation; a
  second turn opens a second case. Crescendo-style escalation across turns was not run.
- **The replay's planner is scripted, not stochastic.** That is the point — it is the compliance
  ceiling — but it means this says nothing about how *often* a real model takes the bait. That
  question is measured separately in [`live-loop-findings.md`](live-loop-findings.md).
- **No claim is made that `careloop/` is free of ordinary software defects.** This evaluates the
  authority property, not correctness in general.

---

## Standing files

| | |
|---|---|
| harness | [`evals/`](../evals/) |
| attack corpus | [`evals/attacks/`](../evals/attacks/) |
| response cache | [`evals/cache/`](../evals/cache/) |
| replay + vacuity proof | [`evals/replay.py`](../evals/replay.py), [`tests/test_evals_replay.py`](../tests/test_evals_replay.py) |
| corpus recovery | [`scripts/mine_attack_cache.py`](../scripts/mine_attack_cache.py) |
| why no guardrail layer | [ADR 0003](adr/0003-no-runtime-guardrail-layer.md) |
| DeepTeam surface, measured | [`spike-findings.md`](spike-findings.md) §7 |
