# ADR 0002 — Graph execution stays on the host

**Status:** Rejected (moving execution to AgentCore Runtime)
**Date:** 2026-08-14
**Pinned against:** `strands-agents==1.51.0`, Python 3.12, `botocore==1.43.68`, `us-east-1`
**Evidence:** measured against the deployed runtime `careloop_hello-Ge04ZVAik3` and the live
distribution `https://d2heuwvhvrdyig.cloudfront.net`
**Seam:** [`careloop/api/deps.py`](../../careloop/api/deps.py) — `GraphRunner`,
[`tests/test_api_events.py`](../../tests/test_api_events.py) — `TestTheGraphRunnerSeam`

## What is not in question

**AgentCore Runtime is deployed, invoked and measured in this project.** `careloop_hello-Ge04ZVAik3`
is `READY` in `us-east-1`, calls Nova Micro through an instance role, and answered every probe
below. All three once-per-account IAM gates in front of `CreateAgentRuntime` are closed and
recorded in `CLAUDE.md`.

This ADR is about **where CareLoop's production graph runs**, not about whether AgentCore works. It
works. The question was whether moving the graph onto it makes the submission better, and the
answer is no — for one reason that has nothing to do with AgentCore's quality.

## Context

Stage 1 — the deployed demo — is live: one `t4g.small` behind CloudFront serving `/`, `/api/*` and
`/clinic/*` from one origin, real model, real Demo World, real read-back.

Stage 2 would have moved graph execution into an AgentCore runtime. `/invocations` receives the
case and the inbound event, the runtime builds a `RunContext`, seeds an in-memory store, runs the
graph, and returns the case; the EC2 host stays the durable source of truth and commits the result
with `save(case, expected_version=N)`. Nothing about the authority layer, the trust model, the
state machine or `rules.yaml` would have changed. It is `tests/test_resume.py` pointed at a
different process.

The motivation was rubric-shaped: making AgentCore load-bearing rather than a checkbox.

## Decision

**Rejected.** The graph runs in the API process, as it does today.

The `GraphRunner` seam is built anyway, so the rejection is falsifiable rather than rhetorical —
see *The seam* below.

## The measured basis

Every figure below was produced by execution against the deployed runtime, not by reading
documentation. Three invokes on one `runtimeSessionId`, each including one Nova Micro call:

| | measured |
|---|---|
| cold session | **6.47 s** |
| warm | **0.72 s**, **0.93 s** |
| **cold-start penalty** | **≈ 5.5 s** |

Against Stage 1's deployed numbers — `referral` end to end in **1.96 s**, approve-to-`DONE` in
**0.16 s** — a warm remote run lands somewhere near 2.5–3 s and a cold one near 8 s.

The execution environment, from the hello-world's own `{"probe": true}` branch:

```
arch: aarch64          python: 3.12.12        cpu_count: 2
cwd: /var/task (read-only)                    /tmp: writable
credential_method: iam-role
egress_bedrock: OK 6ms   egress_public_internet: OK 5ms   egress_arbitrary_host: OK 5ms
```

**Packaging is viable and was not the obstacle.** Stage 1 does not use a container: the runtime is
`codeConfiguration` — a Python 3.12 zip staged in S3 with entrypoint `main.py`. The hello package
is 28 MB compressed / 60 MB uncompressed and already vendors `strands-agents`, `fastapi`, `httpx`,
`pydantic`, `cryptography` and `yaml` as `cp312-manylinux2014_aarch64` wheels. The only tree Stage 2
adds is pdfplumber's, which resolves cleanly for that platform — `pdfplumber`, `pdfminer.six`,
`cryptography`, `cffi`, `Pillow`, `pypdfium2`, 21 MB of wheels — landing the package around 85 MB
uncompressed. No Docker, no ECR, no ARM64 image build, no cross-compilation problem.

Two repo-side checks also passed: `Case` round-trips `model_dump_json` → `model_validate_json` to
equality, and `SqliteCaseStore.save` writes `case.version` verbatim while appending timeline
entries past `stored_seq`, so committing a case that advanced N→N+k inside a runtime under
`expected_version=N` works exactly as the interface promises.

**So the cut is not "it was too hard".** It was buildable, and the pieces were measured before
anything was written.

## The criterion that decides it

**Per-node commits to the host store are what make the dashboard responsive, and a single terminal
commit removes that.**

Today every graph node commits its transition before it finishes, so a browser polling
`GET /api/cases` at 1 Hz genuinely watches the case move through `GATHERING_EVIDENCE`, `PLANNING`,
`EXECUTING`, `VERIFYING`. `careloop/api/runs.py` says so in its own module docstring: nothing had to
be built to make in-flight work observable, only to stop it being hidden.

Under Stage 2 the store the nodes commit to is *inside the runtime*. The host sees one commit, at
the end. From the dashboard's side the entire run becomes atomic.

**The animation survives; the responsiveness does not.** This distinction is the whole decision, so
it is worth being precise about. `pacedState` in `frontend/src/services/presentation.ts` walks the
display forward one state at a time through the states the case's **own `STATE_TRANSITION` entries**
record. It replays from the timeline rather than from what the poller happened to catch — which is
why it exists at all, since even locally the backend commits faster than a 1 Hz poll can sample.
So the beats still play after a remote run: "Making the arrangement…", "Confirming with the
clinic…", all of it, in the order the case genuinely passed through them.

What is lost is the interval *before* the walk begins. The card appears in Working at its opening
state and sits there, motionless, for the length of the remote run. Warm that is about a second
worse than today. Cold it is roughly six seconds of a card doing nothing while a judge looks at it.

And the cold path is the likely one. `idleRuntimeSessionTimeout` is **300 s**: a judge who reads the
page, opens the under-the-hood drawer, or watches the intro before pressing a scenario gets a cold
start. The demo's own guide asks them to look around first.

**We would be trading a visibly working demo for a rubric mention we already substantially have** —
a deployed, invoked, measured AgentCore runtime, plus this ADR and ADR 0001.

## What was rejected along the way

**Warming the runtime.** Available, cheap, and it was the plan's own mitigation: warm at app
startup and on `POST /api/demo/reset`, which `realApi.runScenario` already calls before every
scenario, so no frontend change would have been needed. It **reduces** the cold path without
**removing** it — the 300 s idle timeout outlives any warm-up the host can schedule without
inventing a heartbeat, and a heartbeat to keep a demo's latency acceptable is a smell rather than a
solution. Warming would have made the common case fine and left the worst case exactly as bad.

**Streaming or checkpointing from the runtime.** The honest fix: have the runtime commit each node's
transition back to the host as it goes, restoring incremental visibility. That is a different
architecture — a durable store reachable from both sides, or a callback channel — and it reopens
invariant 8, which is the last thing to reopen in a timeboxed session. Noted below as what would
change the answer.

**Accepting the regression and shipping it anyway.** Rejected on the project's own standard. This is
a demo whose thesis is that you can watch authority being enforced. A dashboard that goes quiet for
six seconds does not fail safe or unsafe; it just stops showing the thing the submission is about.

## Two unresolved risks, and why they make the cut cheap rather than urgent

Neither of these blocked the decision. Both are recorded because they are what a later session
would have to resolve first, and because each is a real cost that the cut simply does not incur.

**1. The runtime role's Bedrock scope is unknown.** `CareLoopAgentCoreRuntimeRole` demonstrably
invokes `us.amazon.nova-micro-v1:0` — the hello-world does it. CareLoop also needs
`us.amazon.nova-2-lite-v1:0` for the planner. If the role was scoped to the one model the
hello-world used, the planner fails on the first deployed run. `careloop-bench` is denied IAM read,
so this could not be settled from here. Small, but it is a deploy-cycle-to-diagnose rather than a
read-the-policy.

**2. The read-back would route through a caching CDN, and that is the sharp one.** The runtime
cannot reach the host's `http://127.0.0.1:8100`, so `CARELOOP_CLINIC_URL` would have to become the
public `/clinic` path — meaning both the booking **and the verification's separate `GET`** traverse
CloudFront.

If that behaviour caches, **the read-back reads the CDN's memory instead of the clinic, and still
shows green.** `Verification.externally_observed` would keep reporting `read_back: true` while
observing nothing external at all. That is the most valuable claim in the project —
`tests/test_verify_readback.py::TestTheIndependenceProof` exists precisely to stop the read-back
becoming a re-read of CareLoop's own receipt — and Stage 2 routes it through a new failure surface
that fails *silently and green*, which is the worst available shape.

Probing it from here was suggestive but not conclusive: two consecutive `GET /clinic/slots` both
returned `X-Cache: Miss from cloudfront`, and the origin sets no `Cache-Control` on that path, so
whether it caches depends on a distribution cache policy rather than on anything in this repo.
"Probably not cached" is not a standard this project applies to the read-back anywhere else.

Keeping the clinic on loopback keeps that whole surface out of existence. `CLAUDE.md` already says
it: anything that makes the read-back cheaper deletes the only thing it buys — and here the
"cheaper" would have been a CDN doing it for free.

## The wire-format finding, recorded because it outlives the cut

Session E's Stage 2 sketch had `/invocations` receive `{case, event, clinic_url}`. **That was
wrong, and the reason generalises past this ADR:** a clinic URL is a `DESTINATION`-shaped value, and
putting it in a request body makes the destination of every real booking something the request can
set.

Nothing could forge that request — `InvokeAgentRuntime` is IAM-gated — but "nothing can reach it" is
exactly the argument this project refuses everywhere else. Invariant 3 does not say destinations are
safe when the caller is trustworthy; it says `DESTINATION` never promotes, whatever the validator
says. The same reasoning that makes `case_id` a `ToolSet` constructor argument rather than a tool
parameter (invariant 7) makes a clinic URL configuration rather than a payload field: **if a value
decides where an effect lands, it does not travel on the same channel as the request to perform
it.**

So the wire would have been `{case, event}`, with the clinic URL an environment variable on the
runtime exactly as `CARELOOP_CLINIC_URL` already is on the host. Written down here because it is the
trust model applied to a wire format, and the next thing that grows a remote surface should inherit
it rather than rediscover it.

## The seam

`ApiDeps.graph_runner` and the `GraphRunner` type exist as of this ADR, defaulting to
`local_graph_runner`, which is the composition of the context factory and `run_case` and nothing
more. `schedule` drives it. The whole suite passes unchanged — **1714 passed, 37 skipped both
before and after**, confirmed by running the suite at the previous commit.

The seam is what makes this ADR falsifiable rather than a story. Moving execution elsewhere is one
implementation of one type, not a rewrite of the API layer, and `TestTheGraphRunnerSeam` holds that
open: an injected runner is what `schedule` drives, and it *replaces* local execution rather than
layering on it — the vacuity proof being a runner that does nothing, after which the case is still
`NEW` and holds no actions.

One live hazard was fixed in passing and has its own test. `create_app` assigned `context_factory`
onto `ApiDeps` **after** constructing it; a local runner built in `__post_init__` would then have
closed over the *default* factory — real agents against whatever the environment resolves — while
every route went on reporting the injected one. The factory is now resolved before construction.
`test_the_default_runner_is_built_over_the_factory_this_app_was_given` fails against the old
ordering. The failure mode it prevents is a test suite that silently stops using its own mock, which
is the kind that stays green.

Three mutations, each applied to a clean tree, suite re-run, reverted:

| Mutation | Result |
|---|---|
| `schedule` ignores `graph_runner` and runs locally | 2 failed |
| the local runner is built over `default_context_factory` | 8 failed |
| `create_app` restored to assign-after-construction | 8 failed |

## What would change the answer

The rejection is falsifiable. It would be worth revisiting if:

- **The runtime can commit per node rather than per run** — a store both sides reach, or a callback
  channel back to the host. That dissolves the deciding criterion rather than working around it,
  and it is the only change that does.
- **The judge-facing demo stops depending on incremental commits for its responsiveness** — a
  redesign where the Working card is driven by something other than observed state transitions.
- **Cold start stops being the common case** — a provisioned-concurrency equivalent, or an idle
  timeout long enough that a judge reading the page first does not pay it.

None of these is about AgentCore being unsuitable. A runtime that boots in 6.5 s and answers in
0.7 s is entirely reasonable infrastructure; it is a poor fit for a specific interaction whose whole
value is that you can watch it happen.

## Consequence

Stage 1 is the demo, unchanged and unweakened. `CARELOOP_GRAPH_RUNTIME_ARN` and the deployment tree
it would have needed were **not** built — the seam is the deliverable, and an unused deployment
script is a thing that rots. Nothing was deployed for this ADR beyond session E's hello-world, no
IAM was changed, and the marginal AWS cost of the measurements above is four Nova Micro invocations.
