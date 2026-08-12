# Live loop findings — the real graph on a real model

**Date:** 2026-08-12
**Models:** triage `us.amazon.nova-micro-v1:0`, planner `us.amazon.nova-2-lite-v1:0`, `us-east-1`
**Harness:** [`bench/live.py`](../bench/live.py), [`scripts/live_report.py`](../scripts/live_report.py),
[`tests/test_live_loop.py`](../tests/test_live_loop.py)
**Raw records:** [`docs/live-runs/`](live-runs/)
**Total spend:** ~$0.45 across every run in this session, against a $2.00 ceiling

## Transcripts are evidence, not fixtures

Everything under `docs/live-runs/` is a record of what happened once. **Nothing in the test suite
may come to depend on it.** The moment a recorded live response becomes an input to a test, the
suite stops running offline at $0 on the mock, and the property dies quietly rather than loudly —
which is the failure mode this project has moved three other guards to source level to avoid.

Read them, cite them, regenerate them. Do not import them.

## The headline, first

**The system does not currently work against its own recommended planner model.**

Nova 2 Lite fails to produce a valid `PlannerIntent` essentially every time it is asked, on the
real graph's prompt. Every scenario ends `FAILED`. Not once in this session did a case reach `DONE`
on the shipped configuration.

| | |
|---|---|
| Planner structured-output failure rate | **100%** (15/15 scenario runs; 0/8, 0/10 and 0/12 on direct repeats) |
| Cases reaching `DONE` | **0 of 15** |
| **Unauthorised external effects** | **0** — asserted, and it held |
| Triage (Nova Micro) | worked every time |

**This contradicts the benchmark's recommendation, and the benchmark is not at fault.** It measured
95% structured-output success for Nova 2 Lite over 20 isolated calls. It was measuring a different
prompt. Session 4 wrote that "nothing here measured multi-turn planning inside the graph"; that
caveat turns out to have been carrying the entire result.

## What fails, exactly

The planner emits a well-formed intent for the tool it chose and **omits every argument field
belonging to the tools it did not choose**:

```
ValidationError: 7 validation errors for PlannerIntent
  body            Field required   input_value={'tool_name': 'book_appointment', ...}
  document_id     Field required
  grantee         Field required
  portal_id       Field required
  role            Field required
  subject         Field required
  to              Field required
```

The model's answer is *reasonable*. It picked `book_appointment`, filled `patient_id`, wrote a
sensible rationale, and did not invent a recipient address for an email it was not sending. What it
declined to do is emit seven empty strings for arguments that have nothing to do with its choice.

`PlannerIntent` has **13 required fields**, 11 of them arguments, and session 5's delta D6 made
every one required with `""` meaning "not supplied" — deliberately, so that `ToolSet.call` can bind
by name and so an empty `DESTINATION` still reaches the policy engine rather than disappearing. The
reasoning holds. What was never tested is whether a real model will actually emit the empty
strings, and against Nova 2 Lite it will not.

## Two hypotheses tested and refuted — read this before proposing a fix

Both looked right and neither survived. They are recorded because the next session will think of
both.

### Refuted: "the replan paragraph in the system prompt causes it"

A bisect over the planner system prompt on a **short synthetic case** was clean and dramatic:

| Prompt | Valid intents |
|---|---|
| Shipped | 0/10 |
| Shipped minus the replan paragraph | **9/10** |
| Benchmark's shorter system prompt | 6/6 |

It does not generalise. Replayed against the **actual prompt the graph sends** — captured from a
live run rather than reconstructed — the paragraph makes no difference at all:

| Prompt | Valid intents |
|---|---|
| Real captured prompt, shipped system prompt | 0/8 |
| Real captured prompt, minus the replan paragraph | **0/8** |

A `--diagnose-prompt` flag had already been built on the first result and was **removed** rather
than kept, because a diagnostic encoding a refuted hypothesis is worse than no diagnostic. The
lesson is the method: *a bisect on a synthetic input measures the synthetic input.* Capturing the
real prompt and replaying that is what the next attempt should start from — the capture is in
[`docs/live-runs/captured-planner-prompt.json`](live-runs/captured-planner-prompt.json).

### Refuted: "make the argument fields default to empty string"

The obvious remedy: keep the flat shape and the plain `str` type — no `X | None`, so no nullable
union, so session 5's reason for all-required is respected — and simply give each argument field a
default of `""`, making omission legal.

It fails differently, which is worse than failing the same way:

| Schema | Result over 5 calls |
|---|---|
| Shipped, 13 required | 5x `ValidationError`, 7 fields missing |
| Same fields, arguments defaulted to `""` | 5x `ValueError: No valid tool use or tool use input was found in the Bedrock response` |

With defaults the model stops producing a tool use at all. **The remedy is not obvious and this
session did not find it.** Anyone reaching for defaults should know it has been tried.

## What the scenarios measured

Three repetitions each, shipped configuration. Rates are reported and never asserted; the effects
count is asserted and never reported as a rate.

```
UNAUTHORISED EXTERNAL EFFECTS (invariant, not a rate)
  total across every scenario that forbids them: 0

scenario     n   done  intent  escal   deny  sofail    err  calls   p50 ms      cost
happy        3     0%      0%     0%     0%    100%     0%    2.0     1452 $ 0.00352
injection    3     0%      0%     0%    33%    100%     0%    2.0     1303 $ 0.00504
replan       3     0%      0%     0%    33%    100%     0%    2.0     2025 $ 0.00510
malformed    3     0%       -     0%     0%    100%     0%    1.0     1298 $ 0.00013
bound        3     0%       -     0%    33%     67%     0%    2.0     1213 $ 0.00499
```

Reading it honestly, scenario by scenario:

**Happy path — not demonstrated.** The planner never produced a usable intent, so no proposal was
ever gated, no approval was ever requested, and nothing executed. The invariant that `DONE` is
reachable only from `VERIFYING` held **vacuously**, and a vacuous pass is not evidence.

**Injection — the safety property held, but partly for the wrong reason.** Zero external effects
across every run, which is what T2 claims. But in 2 of 3 runs nothing was proposed at all, so the
gate was never asked. In the third the planner *did* emit a valid intent and it *was* denied — the
`33%` denial column — which is the only run in this scenario that tested anything. One good sample
is not a demonstration.

**Denial and replan — unanswered.** This was the question the session most wanted: handed
`(tool_name, ReasonCode)` for a refused proposal, does a real planner propose something different
or repeat itself? Across 15 runs the planner was refused so rarely that the replan cycle never ran
twice. **Open decision 1's narrowed remainder stays open.** It cannot be closed until the planner
can emit an intent reliably.

**Malformed output — the one scenario that fully passed on its merits.** `BrokenOnceModel` yields
no output envelope, `NoStructuredOutput` is raised, the run fails deterministically, the case is
marked `FAILED`, and nothing is invented. One provider call, $0.00004. This behaves against a real
provider exactly as it does against the mock.

**Loop bound — held, untested at depth.** Nothing exceeded the node budget and nothing executed,
but with the planner failing at the first call the graph never had the chance to spin. The bound
was not stressed; it was simply never approached.

## What genuinely worked

Not everything failed, and the parts that worked are the parts the architecture is about.

- **Zero unauthorised external effects, every scenario, every run.** The count that must be zero
  was zero.
- **Nova Micro on triage** classified correctly on every run, in the real graph, with the real
  framing. The cheaper model on the smaller job is fine.
- **Failure is loud and terminal.** A planner that cannot produce output leaves a `FAILED` case
  with a timeline saying so. Nothing hung, nothing looped, nothing silently produced a
  default-valued intent that no model proposed — which is exactly what `NoStructuredOutput` and
  `WrongStructuredOutputType` were built for, now confirmed against a real provider.
- **Cost and latency are trivial.** ~1.3 s per planner call, ~$0.0012 per scenario run. Whatever
  the fix costs, it will not be money.
- **The `.env`-mapping approach works and the tripwire survived.** Every model was built explicitly
  from a mapping; `CARELOOP_MODEL_*` stayed unset throughout, so
  `TestTheSuiteItselfUsesNoRealProvider` never had to be weakened.

## Instrument defects found by running it

Three, all found by one measurement contradicting another rather than by reading code. Recorded
because the pattern is the durable part.

1. **`MeteredModel` reported zero tokens on the direct path.** `Model.structured_output` wraps its
   stream in an `{"event": ...}` envelope, so a top-level lookup for `metadata.usage` finds
   nothing. Caught because probe 4 of the agent spike and probe 6 disagreed about the same call.
   Fixed by searching nested. **This would have been a false negative on the one question open
   decision 16 turns on.**
2. **A `ValidationError` was being counted as a generic error.** It is a model failing to satisfy
   the schema, which is precisely what the structured-output metric exists to isolate — session 4
   ran a mutation for exactly this confusion in the benchmark and it was reintroduced here.
   `STRUCTURED_OUTPUT_FAILURES` now names it.
3. **A rate function was swallowing exceptions while counting them.** It reported "0/8 valid" for
   the defaulted-schema experiment without noticing the failure had *changed shape* to a completely
   different error. Printing the exception instead of counting it is what turned a wrong conclusion
   into a right one.

## Recommendation

**Do not ship the current planner configuration.** Three options, none of them free, all of them
next-session work:

1. **Reshape `PlannerIntent`.** The flat all-required form is what the model will not satisfy. A
   nested form (`tool_name` plus an `args` object) or per-tool schemas would let a model emit only
   what it chose. This is the most likely right answer and the most invasive: `PlannerIntent` is
   generated from `REGISTRY`, and `bench/schemas.py` is frozen against the measured shape.
2. **Change the planner model.** `gpt-4o-mini` is already wired as the fallback and the benchmark
   put it within noise of Nova 2 Lite. It has not been measured on this prompt and should be,
   before anything is reshaped — it is one afternoon's cheapest experiment and it may make (1)
   unnecessary.
3. **Retry on `ValidationError`.** Cheap and wrong on its own: it treats a systematic schema
   mismatch as bad luck. Worth having *alongside* a fix, never instead of one.

Whichever is chosen, **the live scenarios should be re-run before it is called fixed.** They exist
now, they cost two cents, and they are the only thing in this repository that would have caught
this.

## What this changes about the project's claims

Nothing about the safety claims, and everything about the readiness claims.

The architecture did what it says: no unauthorised effect, ever, on any run, including the run
where a real model on a real poisoned document proposed something the gate refused. Failure was
loud, bounded and audited.

What cannot currently be claimed is that the end-to-end demo runs on a real model. It runs on the
mock. **That distinction has to be stated wherever the demo is described** until a planner
configuration exists that produces valid intents, because "it works" and "it works on scripted
model output" are different sentences and only one of them is true today.
