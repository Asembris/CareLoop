# ADR 0003 — No runtime guardrail layer

**Status:** Rejected (adopting NeMo Guardrails, or any probabilistic runtime guardrail, in front of
or in place of the policy engine)
**Date:** 2026-08-14
**Pinned against:** `deepteam==1.0.9`, `deepeval==4.1.8`, `strands-agents==1.51.0`, Python 3.12
**Evidence:** [`docs/security-evaluation.md`](../security-evaluation.md),
[`evals/replay.py`](../../evals/replay.py),
[`tests/test_evals_replay.py`](../../tests/test_evals_replay.py) — `TestTheVacuityProof`

## Context

CareLoop's central claim is that a document telling the agent to email everything to an attacker
cannot make it happen, and that the audit trail proves it. The thing making that true is
`careloop/policy/` — a deterministic engine returning `ALLOW` / `DENY` / `REQUIRE_HUMAN` from
`rules.yaml`, enforced at two independent points, with argument classes owned by the tool registry.

Session G evaluated CareLoop with DeepTeam. Every mature LLM-security toolchain that came up during
that work offers, or sits beside, a **runtime guardrail layer**: NVIDIA NeMo Guardrails is the
prominent one, and DeepTeam ships `deepteam.Guardrails` of its own. The question this ADR settles
is whether CareLoop should adopt one.

It is a live question rather than a rhetorical one. Guardrails are the industry-standard answer,
a reviewer may reasonably expect to see one, and their absence could read as an omission rather
than as a decision.

## Decision

**Rejected.** CareLoop adopts no runtime guardrail layer. The deterministic policy engine remains
the only thing that decides whether an action may happen.

## Why — the argument that decides it

**NeMo Guardrails' execution rails sit exactly where CareLoop's policy gate already sits, and they
decide the same question with a weaker instrument.**

An execution rail intercepts a tool call and decides whether it may proceed. That is, precisely,
`careloop/policy/gate.py`. The difference is not placement but *epistemics*:

| | policy engine | execution rail |
|---|---|---|
| decides from | `rules.yaml`, argument classes, resolved trust levels | an LLM call, or a Colang pattern over text |
| same input, same output | always | not guaranteed |
| failure mode | fails closed on an unknown value, by construction | fails whichever way the model went |
| provable | mutation testing — remove it and named tests fail | can be measured, not proven |
| audit trail | matched rule id, argument classes, trust levels, on the timeline | a score and a rationale |

Replacing a mutation-proven deterministic authority layer with a probabilistic one would weaken
the exact property this project exists to demonstrate. Session G's own numbers make that concrete:
the offline replay drives a **fully compliant adversarial planner** — one that proposes precisely
what the attacker asked, every time — through every committed payload, and the unauthorised-action
count is **zero**, deterministically, on every commit, at $0. A guardrail cannot make that
statement, because "the classifier caught it every time we ran it" is a different kind of sentence
from "no rule permits it".

**And layering one in front would be worse than useless in a specific way.** It would produce a
second place where an action is refused, with a second vocabulary of reasons, feeding the same
planner. Invariant 6 exists because denial text re-enters the planner's prompt: a guardrail's
free-text rationale is exactly the interpolated, unbounded, model-visible reason string that
invariant forbids. Adopting one would mean either sanitising that text — the sanitiser this
project refuses everywhere else — or opening the injection channel invariant 6 closes.

**The honest counter-argument, stated rather than skipped.** A guardrail catches things a rule
cannot express: a plausible-sounding request with no policy-relevant argument, PII in a free-text
body, an unusual sequence. CareLoop's answer is structural rather than dismissive — the planner
emits a typed proposal and every external effect is a typed tool call with declared argument
classes, so there is no channel by which model prose becomes an *action*. **The surface a guardrail
defends is mostly one this architecture does not have.** Where it did have one — the denial text —
the answer was the closed `ReasonCode` enum, not a filter.

**"Mostly" is doing real work in that sentence, and it did not used to.** This ADR read *"nothing
the model writes reaches a person except through a gated action"*, and by session W that was no
longer true in two places. `TriageResult.summary` is one sentence a model writes from event text
that may be attacker-authored; the API projects it as `Case.summary`, and the dashboard renders it
as the case title for any case outside its three-entry fixed-title table. And a pending decision
card shows a caregiver the exact arguments of the call they are being asked to approve — for
`send_email` that is a model-authored subject and body, read by a person before they press approve.

**The decision does not change, and the reason is worth stating rather than assuming.** Neither
channel confers authority: the summary is re-framed as untrusted evidence when it reaches the
planner, and the decision card exists precisely so that a human sees the exact call before it is
released. What a guardrail would add here is *content* screening of prose a person reads — a
different job from deciding whether an action may happen, which is the job this ADR is about. The
argument below was that no such channel exists; the accurate argument is that these channels carry
no authority and that screening them is a separate question nobody has yet asked. If prose the
model writes ever reaches a person *without* a human decision attached — a generated letter sent
on, a notification — that is the trigger recorded below, and it is closer than this page implied.

## garak — not adopted

[garak](https://github.com/NVIDIA/garak) is a vulnerability scanner for LLMs: probes for
jailbreaks, prompt injection, data leakage, toxicity, run against a *model endpoint*.

Not adopted, for one reason that is not a criticism of the tool: **garak scans a model, and
CareLoop's claim is not about its model.** Pointing it at Nova 2 Lite would measure Nova 2 Lite,
which AWS has already measured more thoroughly than this project can. Pointing it at the gated
pipeline is awkward — it expects a text-in/text-out generator, and CareLoop's unit of work is a
case with typed evidence and a durable store, not a chat turn.

Session G's target 1 already covers what garak would have contributed and covers it better, because
it runs the *real* planner prompt with the *real* tool catalogue and executes the result, so an
exposure is reported as a performed action rather than as a suspicious string. Adding garak would
have produced a second, weaker measurement of the same thing.

## promptfoo — not adopted

[promptfoo](https://github.com/promptfoo/promptfoo) is a strong tool and the closest call here. Its
red-team suite has broad attack coverage, good reporting, and would have produced a handsome table.

Two reasons it is not adopted, the second being decisive.

**It is a Node toolchain, and it would be the third in this repository.** CareLoop is Python plus
one Vite frontend; CI already keeps those in separate jobs so the Python job's zero-secret contract
is not put behind an unrelated install. A Node-based evaluator would need a third job, a third
lockfile and a third dependency surface, to measure something DeepTeam measures in-process.

**Its assertions are configuration, and this project's evidence standard is a failing test.**
promptfoo expresses expected behaviour in YAML assertions evaluated by its own runner. That is
fine, and it is not what CareLoop's guards are held to: every defence here has a test that *fails
when the defence is removed*. `TestTheVacuityProof` replays the entire corpus through an allow-all
policy engine and requires the unauthorised-action count to become non-zero. A YAML assertion
cannot be mutation-tested against the system it describes, so adopting promptfoo would have added
coverage while lowering the standard of proof — the opposite trade to the one this project makes
everywhere else.

DeepTeam was chosen over it because it runs in-process in Python, accepts `tools_called` with
input parameters and outputs (so tool-level vulnerabilities are judged on what the agent *did*),
and — usefully — documents its own limitation: it tests at the model reasoning and response level,
and states that comprehensive agentic security needs additional runtime controls. That sentence is
the reason session G ran **two** targets rather than one, and the contrast between them is the
measurement.

## What was adopted instead

Nothing new at runtime. The evaluation lives entirely in `evals/`, changes no line of `careloop/`,
and produces:

- an offline replay of every committed attack through a fully compliant adversarial planner,
  deterministic, $0, run in CI on every commit;
- its vacuity proof, which requires the same replay to fail with the authority layer removed;
- a paid DeepTeam run against both targets, cached and committed as data.

## What would change the answer

- **CareLoop grows a free-text channel to a human that carries no human decision.** A generated
  letter that is sent, a notification, a chat surface. Two channels already exist and are named
  above — the triage summary rendered as a case title, and the arguments on a decision card — and
  both are attached to a person's own reading rather than to an autonomous send. The trigger to
  reopen is prose reaching somebody with nothing asked of them.
- **A rule turns out to be inexpressible.** If a genuine threat cannot be written in `rules.yaml`
  without encoding it in Python — which invariant 2 forbids — that is evidence the deterministic
  layer is insufficient rather than evidence the rule file needs an exception.
- **A guardrail becomes provable.** A deterministic, mutation-testable rail is not a contradiction
  in terms; it is roughly what `rules.yaml` is. If one ships with that property, the objection here
  disappears, because the objection is to probabilism and not to the word "guardrail".

## Consequence

`careloop/policy/` remains the only authority. No new runtime dependency, no second denial
vocabulary reaching the planner, and no probabilistic component between a proposal and its refusal.
The absence of a guardrail layer is now a recorded decision with an argument attached, which is
what this ADR is for: a stated rejection is stronger evidence of judgement than silence.
