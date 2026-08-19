# Model benchmark addendum — the post-B2 proposal seam

**Run 2026-08-19**, `us-east-1`, `us.amazon.nova-2-lite-v1:0`, 12 tasks × 3 repeats = **36
attempts, 38 provider calls, $0.0388**. Harness [`bench/propose.py`](../bench/propose.py), tasks
[`bench/propose_tasks.json`](../bench/propose_tasks.json), full record
[`bench/results/propose.json`](../bench/results/propose.json).

```bash
.venv/Scripts/python -m bench.propose                                       # $0, dry run
CARELOOP_LIVE=1 .venv/Scripts/python -m bench.propose --confirm-spend --max-spend 0.25
```

## Why this exists

[`model-benchmark.md`](model-benchmark.md) chose the model behind the planner, and it measured a
planner interface that no longer exists. Every planner number there went through
`Model.structured_output` against a single flat `PlannerIntent` wrapper; since B2 the model is
offered one closed `propose_*` schema per registered action and calls exactly one of them. Leaving
that unmeasured would mean the shipped model choice rested on a measurement of something else.

**This does not re-run the six-candidate comparison**, so the *choice* of model still rests on the
original. It asks whether the seam that ships works, and how it fails when it does.

## Results

| | |
|---|---|
| Structurally valid proposals | **36 of 36** |
| Structural failures escaping the seam | **0** |
| Retries fired | **2** |
| Retries that recovered | **2 of 2** |
| Failure type behind every retry | **`MalformedProposal`** — the other three never occurred |
| Provider calls for 36 proposals | 38 |
| Unambiguous tasks naming the expected tool | **21 of 21** |
| Proposals carrying an attacker-supplied literal | **0 of 36** |
| Latency, p50 / worst | **1709 ms / 3629 ms** (1708 / 2387 excluding the two retries) |
| Cost | **$0.0388** for the whole set |
| Tokens | 95,358 in, 4,075 out |

## What it says

**The seam works, and the flat wrapper's failure does not reproduce on it.** B2 was prompted by a
measurement where 25 of 25 responses invented a tool named after an entry in the prompt catalogue
when the only tool offered was `PlannerIntent`. Nothing of that kind occurred here: every one of
the 36 attempts called an offered `propose_*` tool, so `NoProposalToolCall`,
`AmbiguousProposalToolCall` and `UnknownProposalTool` were each observed **zero** times.

**One failure mode exists, and it is `MalformedProposal`** — the provider calls the right tool with
arguments that do not satisfy its schema. Two of 36 attempts, and **both on the same task**:
`propose-06-replan-after-destination-refusal`, the case carrying a prior `DESTINATION_NOT_TRUSTED`
refusal in its prompt. Repeats 1 and 3 of that task retried; repeat 2 did not, so this is a rate on
one task rather than a property of it.

**The bounded retry earns its place.** Both were recovered by the second attempt, and both then
named the expected tool. Without it, this run would have reported 34 of 36 rather than 36 of 36 —
so the retry is not defence-in-depth decoration here, it is what makes the number what it is.

**The replan path is where the model struggles, and it is the path no earlier benchmark reached.**
`model-benchmark.md` §5 lists behaviour after a policy denial as *"the single most important
behaviour in the system and it is entirely unmeasured"* — a single call from a clean context cannot
produce it. Three of these twelve tasks carry `prior_denials`, and both failures landed on one of
them. That is 2 of 9 replan attempts against 0 of 27 clean ones, which is suggestive at this sample
size and nothing stronger.

**Independent corroboration, from a different instrument.** On the deployed demo the same day, 2 of
28 runs of scenario 03 ended `FAILED` with `MalformedProposal`, and in both the exception escaped
on **the replan call after a refusal** — the same path, the same failure type, from the real graph
rather than this harness. What the deployed runs add is the tail this one does not reach: when the
retry *also* fails, the exception leaves the graph and the case is marked `FAILED` rather than
handed to a person. That is recorded in the README's honest limits and was not fixed in session W.

**Cost and latency are unremarkable and that is worth saying.** A proposal costs about a tenth of a
cent and takes about 1.7 s at p50. The tool specifications are the larger half of the request —
input runs 2,422 to 4,974 tokens per attempt against 4,075 output tokens across all 36 — which is
the shape of the seam rather than a problem with it.

## What it does not say

1. **Sample size.** 36 attempts, 12 tasks, one model, one region, one afternoon. The retry rate is
   two observations. Treat `MalformedProposal` as *it happens*, not as 5.6%.
2. **This is a structural benchmark, not a correctness one.** `expects` is filled only where one
   tool is the unambiguous next action; the injection probes, the sparse case and two of the
   replans are null and their proposals are scored as *formed*, not as right. The 21 of 21 is a
   real number about the 21 rows it covers and says nothing about the other 15.
3. **No policy engine took part.** A proposal here is never evaluated, allowed or denied. "0 of 36
   carried an attacker literal" is an observation about one model on one day and is **not** a
   safety result — the safety result is `evals/replay.py`'s 0 of 320, which drives a planner
   deliberately more compliant with an attacker than any real model can be.
4. **No cache, so no free re-run.** `bench.run` caches because its value is a reproducible table;
   this measures a *rate* across repeats, and a cache would turn the second repeat into the first
   one's answer. Re-running costs about four cents.
5. **Latency is wall-clock from one Windows machine on one network**, the same caveat the original
   carries, and the two retried attempts include a whole second provider call.

## Does the model choice still hold?

**Yes, on the evidence available, and the honest form of that sentence is narrower than it looks.**
The seam is reliable on the shipped model, its one failure mode is bounded and usually recovered,
and the cost is negligible. What has *not* been re-tested is whether some other candidate would be
better on this seam — `gpt-4o-mini` reaches structured output through a different mechanism
entirely, and comparing the two across providers would confound the seam with the provider, which
is the trap [`model-benchmark.md`](model-benchmark.md) records under "hold the provider constant
and vary only the shape". Re-opening the choice means re-running the comparison, not extrapolating
from this page.
