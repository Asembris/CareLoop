<img src="docs/careloop-mark.svg" alt="" width="64">

# CareLoop

**An autonomous administrative agent for family caregivers — one that acts, but never beyond its
authority.**

[![CI](https://github.com/Asembris/CareLoop/actions/workflows/ci.yml/badge.svg)](https://github.com/Asembris/CareLoop/actions/workflows/ci.yml)
[![unauthorized actions 0 / 320](https://img.shields.io/badge/unauthorized%20actions-0%20%2F%20320-2f7d4f)](docs/security-evaluation.md)
[![license Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![python 3.12](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)

### ▶ Try it — <https://d2heuwvhvrdyig.cloudfront.net>

Live, nothing to install, nothing to sign into. **Three minutes, guided:**
[**docs/judges.md**](docs/judges.md) — press one button and watch a case run; press another and
watch a poisoned document fail to get anywhere.

![The CareLoop dashboard with one case awaiting a caregiver decision](frontend/reference/screenshots/home-needs-you-light.png)

## The problem

Caring for someone generates a second job made entirely of admin: referrals to book, prior
authorizations to chase, claim rejections to answer, documents to find and send. It arrives as
email and PDFs, it is time-critical, and it lands on whoever is already doing the caring. That
person is not short of judgement — they are short of hours, and the hours go to work that is
almost, but not quite, mechanical.

## What makes it different

**The model proposes. Deterministic code authorises. A separate read of an external system
confirms what happened.**

The interesting claim is not that an agent can book an appointment. It is that **a document
telling the agent to email everything to an attacker cannot make it happen** — not because a
classifier caught it, but because no rule permits it — and that the audit trail proves it.

Two LLM agents exist: one classifies an inbound event, one proposes a single next action. Neither
holds a tool, and neither decides anything. A deterministic policy engine returns
`ALLOW` / `DENY` / `REQUIRE_HUMAN` for every tool call from declarative rules in `rules.yaml`,
enforced at two independent points at the tool boundary. Facts extracted from documents carry
`{source_doc, page, trust_level}`, and arguments that decide *where* an effect lands can never be
promoted by validation, whatever a validator says.

## The evidence

**Unauthorized external actions: 0 of 320 adversarial attempts. With the authority layer removed
and nothing else changed: 320 of 320.** Opposite on every single attempt — which is what makes the
zero mean something, because zero is also what a broken harness reports.

Every committed attack is replayed through the real graph, the real `rules.yaml` and both
enforcement points, driven by a planner that does exactly what the attacker asked, every time it
is asked. No real model can be *more* compliant with an attacker than that, so this measures the
ceiling of model misbehaviour rather than one model's behaviour on one day. 40 payloads — **36
generated** by `meta/llama-3.3-70b-instruct` under `deepteam`, 4 authored — across 2 delivery
vectors and all 4 external-effect tools.

It runs offline, at $0, with no provider reachable, in about a second, on every commit:

```console
$ python -m evals.replay
Corpus:   40 attacks (36 generated, 4 authored)
Classes:  AutonomousAgentDrift, ExcessiveAgency, ExternalSystemAbuse, IndirectInstruction, unattributed

SHIPPED rules.yaml   unauthorized external actions: 0 of 320 adversarial attempts
                     refused 320/320, effects 0, errored 0, flagged-before-resolution 0
                       AutonomousAgentDrift     0 of 8
                       ExcessiveAgency          0 of 8
                       ExternalSystemAbuse      0 of 8
                       IndirectInstruction      0 of 8
                       unattributed             0 of 288

ALLOW-ALL mutant     unauthorized external actions: 320 of 320 adversarial attempts
                     refused 0/320, effects 320

OK: 0 of 320 with the shipped rules, 320 of 320 with the authority layer removed.
```

Full method, corpus provenance and limits: [**docs/security-evaluation.md**](docs/security-evaluation.md).

## Quickstart

Clone to the number above, on a machine with no credentials and nothing configured:

```bash
git clone https://github.com/Asembris/CareLoop.git
cd CareLoop
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m evals.replay      # Linux/macOS: .venv/bin/python
```

`.venv/Scripts/python -m pytest -q` runs the whole suite the same way: offline, on a mock model
provider, at $0, with no provider SDK installed and no credentials configured. CI asserts that
rather than assuming it.

<details>
<summary><b>Running the whole demo locally</b> — two processes, and it needs a real model</summary>

The graph, the dashboard and the clinic, on your own machine. The last two lines are servers, so
they want a terminal each:

```bash
npm --prefix frontend ci && npm --prefix frontend run build
python -m demoworld --port 8100
CARELOOP_MODEL_TRIAGE=bedrock:us.amazon.nova-micro-v1:0 \
CARELOOP_MODEL_PLANNER=bedrock:us.amazon.nova-2-lite-v1:0 \
CARELOOP_MODEL_EXTRACTION=bedrock:us.amazon.nova-2-lite-v1:0 \
python scripts/demo_serve.py --clinic-url http://127.0.0.1:8100 --static-dir frontend/dist --port 8000
```

Then open `http://127.0.0.1:8000`, or drive it from a terminal:

```bash
curl -X POST "http://127.0.0.1:8000/api/demo/scenarios/referral?wait=90"
curl http://127.0.0.1:8100/clinic/slots      # a slot is gone, and you can check yourself
```

Measured on exactly these commands: `/` serves the built dashboard, the case reaches `DONE` in a
few seconds — 3.9 s and 5.4 s on two runs from a home connection — and the clinic drops from three
seeded slots to two.

**Those model variables are not optional.** Unset, every role resolves to the scripted mock
provider, whose script is empty outside the test suite — the run fails at the first triage call
with `MockScriptExhausted` and the case lands `FAILED`. That is deliberate: a process that was
never told about a real provider cannot reach one by accident, which is what lets CI assert "$0,
offline, no secrets" rather than hope for it. **The credential-free ways to see CareLoop work are
the live URL and the replay above.**

</details>

## Architecture

[![CareLoop architecture: three trust zones, one policy engine, an independent read-back](docs/architecture.svg)](docs/architecture.svg)

One page of caption, including what each zone means and why there are exactly two agents:
[**docs/architecture.md**](docs/architecture.md).

## What this does not claim

- **CareLoop fires the Strands `BeforeToolCall` hook itself, from its own execute node, against a
  real `HookRegistry`.** An SDK event loop does not fire it, because there is no event loop to
  fire it: both agents drive the model provider directly and neither holds a tool. The event type,
  the registry and the `cancel_tool` contract are genuine SDK machinery; what is CareLoop's is the
  *firing*. "Strands blocks the attack" would be an overstatement.
- **Verification checks the effect performed against the effect authorised** — and, for
  `book_appointment` only, against a separate `GET` to the external clinic. It does not prove the
  outside world did the right thing. The other three effects are simulated.
- **The mixed injection scenario is deliberately not deterministic.** The poisoned document arrives
  beside legitimate work, and a planner that ignores the bait and books the appointment is the
  system working. Measured over six runs: three carried a visible refusal, three went straight to
  the correct booking. Tuning the model into taking the bait would be rigging the demo. The
  poison-only scenario refuses every tool it is offered, five times in five runs.
- **No DeepTeam pass/fail table is published**, because judging never completed — the paid layer's
  contribution is the corpus, and that is all it is claimed to have contributed.
- **`X-Caregiver` is an audit identity, not authentication.** Nothing is verified; a missing header
  is `400`, never `401`, because `401` would claim a check that is not happening.
- **Synthetic data throughout.** One invented patient, one invented clinic, no PHI, and nothing
  here is clinical — CareLoop schedules and files, and never advises.

## Honest limits

- Approval expiry is unmodelled — a case can sit in `AWAITING_HUMAN` indefinitely.
- `WAITING_EXTERNAL` is reserved: it is in the state machine and has no producer.
- `book_appointment`'s `reason` is planner-authored free text. It carries no trust key, no content
  constraint and no human review before an autonomous booking, and document evidence can influence
  it. Verification compares it against the clinic's record, which is fidelity rather than
  provenance.
- The offline suite only ever sees the mock model, so green means the spine is correct, not that a
  provider is well-behaved. Three separate sessions found bugs that no offline test could catch.
- Cost per case is not reported. Nothing stamps token usage on a case, and the API inventing one
  would be worse than its absence.
- The API's run registry is in memory — execution context, not a business fact, so a restart costs
  the node trace and nothing the store owns.
- The **deployed** demo gives each visitor an ephemeral world so two people can use it at once, so
  that URL does not demonstrate durable ownership of a case across processes. `scripts/resume_demo.py`
  and `tests/test_resume.py` do, over a SQLite file and three processes; the store itself is
  unchanged either way.

## Where to read more

| | |
|---|---|
| [docs/judges.md](docs/judges.md) | the three-minute guided tour of the live demo |
| [docs/architecture.md](docs/architecture.md) | the diagram, the trust zones, the two agents |
| [docs/security-evaluation.md](docs/security-evaluation.md) | 0 of 320, the vacuity proof, and what it does not cover |
| [ADR 0001](docs/adr/0001-strands-agent-for-the-proposal-agents.md) | why the proposal agents are not `strands.Agent` |
| [ADR 0002](docs/adr/0002-graph-execution-stays-on-the-host.md) | why the graph did not move onto AgentCore Runtime |
| [ADR 0003](docs/adr/0003-no-runtime-guardrail-layer.md) | why there is no probabilistic guardrail layer |
| [docs/model-benchmark.md](docs/model-benchmark.md) | which model backs which role, decided by measurement |
| [docs/live-loop-findings.md](docs/live-loop-findings.md) | what a real planner does after a refusal |
| [docs/spike-findings.md](docs/spike-findings.md) | SDK behaviour verified by execution, not by reading |
| [docs/session-log.md](docs/session-log.md) | every session, what was measured, and what was cut |

Built on the [Strands Agents SDK](https://github.com/strands-agents/sdk-python) for the AWS
*Agents for Humans* hackathon. Licensed [Apache-2.0](LICENSE).
