# deploy/

How CareLoop is put on a host a judge can reach. A peer of `demoworld/`, `bench/` and `frontend/`:
nothing under `careloop/` imports anything here, and nothing here decides anything the application
would otherwise decide.

## The shape, and why

One EC2 instance runs two processes. **One CloudFront distribution, two backend origins** — the
instance on `:8000` and the instance on `:8100` are separate origin definitions with their own
behaviours, which is the only way one hostname can route to two ports.

```
https://<distribution>.cloudfront.net
  /            →  instance:8000   the built frontend, served by FastAPI itself
  /api/*       →  instance:8000   the dashboard API
  /clinic/*    →  instance:8100   Demo World, so a judge can check the booking themselves
```

**One *browser* origin is the point, and the two senses of the word are worth keeping apart.** A
CloudFront origin is a backend the distribution forwards to, and there are two. A browser origin is
scheme-host-port as the page sees it, and there is one: everything above is served from the same
`https://<distribution>.cloudfront.net`. That second one is what matters to the same-origin policy.
`careloop/` mounts no `CORSMiddleware` anywhere and this arrangement is what keeps that true off a
developer's machine: the frontend keeps making relative `/api` calls exactly as it does behind the
Vite dev proxy, and no cross-origin request is ever made. Serving the built app from a different
host would need CORS, and weakening that is not a deployment's decision to take.

**Two processes, not one.** Demo World is a system CareLoop does not own, and `demoworld/` may
never import `careloop`. Routing both through one CDN does not couple them — they are separate
units, separate ports and separate failure domains, and `systemctl stop careloop-demoworld` is a
demonstration rather than an outage.

**No secrets on the box.** Bedrock is reached through an EC2 instance profile, the same way the
AgentCore runtime reaches it — `credential_method: iam-role`, no access keys anywhere in the
environment. `careloop.env` therefore holds only model ids and URLs and is committed; there is
nothing in it worth hiding.

## One world per visitor

The API unit runs with `--isolate-sessions`, and that is what makes the URL safe to hand to more
than one person at a time. Every visitor is minted an opaque id on their first request and gets
their **own** case store, run registry and clinic; one visitor's reset cannot reach another's
cases, and two visitors booking the same seeded slot both succeed, because they are booking in
different clinics.

The id travels in `X-CareLoop-Session`, and Demo World reads the same value out of
`X-Demo-Session` or `?session=`. A **header rather than a cookie** because header forwarding
through this distribution is measured — `X-Caregiver` reaches the origin — and cookie forwarding
is not, so a cookie would be an assumption that fails in production rather than in a test. It is
an isolation namespace and **not authentication**: nothing is verified, exactly as nothing about
`X-Caregiver` is.

**What this costs, stated plainly.** A per-visitor world lives in memory and does not survive a
restart of the API process; `CARELOOP_DB` is unused while the flag is on. A visitor whose world
has been forgotten — a redeploy, or half an hour idle — gets a `410`, and the page mints a new one
and carries on with an empty dashboard. So **the deployed URL no longer demonstrates durable
ownership of a case across processes**. That claim belongs to `scripts/resume_demo.py` and
`tests/test_resume.py`, which drive one case across three processes over a SQLite file, and it is
the single-session path — this same script without the flag — that they exercise.

Bounded at 64 worlds, measured at about 300 KB each with all four demo scenarios run, and never
more than Demo World's own cap. **That inequality is defence in depth and is no longer the
property**, which is a session-V correction this page had not caught up with: equal capacities do
not synchronise eviction *order*, and order was the whole disagreement. Demo World now removes a
world only when told to, and this registry tells it — so there is no eviction order left for the
two sides to disagree about. `careloop/api/sessions.py` and `demoworld/clinic.py` carry the
argument; `tests/test_session_lifecycle.py` carries the property.

## Files

| File | What it is |
|---|---|
| `careloop.env` | non-secret configuration, read by both units via `EnvironmentFile` |
| `careloop-api.service` | uvicorn: the API and the built frontend, port 8000 |
| `careloop-demoworld.service` | the clinic, port 8100 |
| `bootstrap.sh` | runs **on** the instance: installs Python, the venv, the units |
| `push.sh` | runs **locally**: builds the frontend, ships the tree, restarts the services |
| `../scripts/observability.py` | the one span processor CareLoop adds: it removes an attribute and does nothing else |

## Observability

The API unit runs under `opentelemetry-instrument` with the AWS distro, which sends **traces only**
to CloudWatch and surfaces them in Bedrock AgentCore GenAI Observability. The whole decision is
[ADR 0004](../docs/adr/0004-agentcore-observability-out-of-band.md); what matters here is how to
operate it.

```
EC2 CareLoop ──ADOT SDK──▶ SigV4-signed OTLP ──▶ CloudWatch Transaction Search / X-Ray
                                                      ──▶ AgentCore GenAI Observability
```

No collector process: AWS states the ADOT Collector is not supported for agent observability outside
AgentCore Runtime, so the application signs and posts its own OTLP. Nothing new runs on the box.

**Turning it off is one line.** `careloop-api.service` carries the instrumented `ExecStart` and,
immediately below it, the uninstrumented one commented out. Swap the two, `systemctl daemon-reload
&& systemctl restart careloop-api`, and CareLoop runs exactly as it did before — the global tracer
provider becomes a no-op, nothing is recorded, and the startup banner says `telemetry (not
instrumented)`. No reinstall, no redeploy, no code change. Telemetry is not part of CareLoop's
availability contract, and that line is where it is true.

**What is and is not exported.** Model id, token counts, latency, HTTP status, `rpc.method`, region,
and the shape of the run. Not prompts, not model responses, not system prompts, not evidence text,
not patient identifiers, not caregiver names, not tool arguments, not tool results, not session
tokens, not credentials, and not external response bodies. Two settings in `careloop.env` pin that
rather than inheriting it, because both libraries default the unsafe way in one direction each —
read the comments there before changing either.

**The access key id is redacted in-process.** ADOT attaches the calling credential's access key id
to every AWS span and offers no switch. `scripts/observability.py` overwrites it with `[redacted]`
before it can be exported. It is a temporary `ASIA…` id from the instance profile and discloses only
the account id, so this is tidiness rather than a fix — but the traces are meant to be published, so
it is removed. `tests/test_observability.py` proves it, including the case where the processor is
absent.

### One-time AWS setup, per account

Not performed by `push.sh` and not automated on purpose — each of these is an account-level change
that should be made deliberately.

```bash
# 1. Transaction Search: let X-Ray write spans into CloudWatch Logs.
aws logs put-resource-policy --policy-name TransactionSearchXRayAccess --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"TransactionSearchXRayAccess","Effect":"Allow","Principal":{"Service":"xray.amazonaws.com"},"Action":"logs:PutLogEvents","Resource":["arn:aws:logs:us-east-1:<ACCOUNT>:log-group:aws/spans:*","arn:aws:logs:us-east-1:<ACCOUNT>:log-group:/aws/application-signals/data:*"],"Condition":{"ArnLike":{"aws:SourceArn":"arn:aws:xray:us-east-1:<ACCOUNT>:*"},"StringEquals":{"aws:SourceAccount":"<ACCOUNT>"}}}]}'
aws xray update-trace-segment-destination --destination CloudWatchLogs --region us-east-1

# 2. The agent log group, and its retention. NEVER EXPIRE is the default and is not acceptable.
aws logs create-log-group --log-group-name /aws/bedrock-agentcore/runtimes/careloop --region us-east-1
aws logs put-retention-policy --log-group-name /aws/bedrock-agentcore/runtimes/careloop --retention-in-days 14 --region us-east-1

# 3. Attach the telemetry statement to the EC2 instance profile role (see ADR 0004 for the table).
```

Verify in the console: **CloudWatch → Application Signals (APM) → Transaction search** shows
enabled, and **CloudWatch → GenAI Observability → Bedrock AgentCore** lists `careloop` after the
first scenario is run.

### Deploying it, and checking the boot

`opentelemetry-instrument` is part of process startup, so the instrumented unit is the one place
telemetry can affect availability. Check the boot rather than assuming it:

```bash
sudo systemctl restart careloop-api
systemctl is-active careloop-api                       # active
journalctl -u careloop-api -n 20 --no-pager | grep telemetry   # ADOT, access key id redacted
curl -fsS https://<distribution>.cloudfront.net/api/health      # {"status":"ok"}
```

If any of those fails, swap to the commented `ExecStart` and restart; the demo is unaffected.

### Evaluations

A deterministic code evaluator runs in AWS Lambda under Bedrock AgentCore Evaluations and judges
whether CareLoop's settlement claim agrees with the transport evidence in the same production
trace. It is **on demand only** — nothing evaluates continuously, and no online evaluation
configuration exists.

| Resource | Note |
|---|---|
| `careloop-settlement-evaluator` | Lambda, `python3.12`, 128 MB, 30 s. Packaged from `evaluators/` alone: no dependencies, no env vars, no VPC |
| `careloop-settlement-evaluator-role` | trusts Lambda only; may write its own log stream and nothing else |
| `/aws/lambda/careloop-settlement-evaluator` | 14-day retention |
| `CareLoopSettlementIntegrity-…` | the AgentCore evaluator, `level=TRACE` |

```bash
python scripts/evaluations_deploy.py        # idempotent: creates or updates, then asserts the shape
```

It refuses to proceed if the log group is missing or would keep records forever, if the function
has grown environment variables, or if a Lambda resource-based policy has appeared — the on-demand
path needs none, and one turning up is a changed AWS contract rather than something to work around.

The running CareLoop application has no part in this: it never calls `Evaluate`, never invokes the
function, and gains no permission from it. The measured results are in
[`../docs/agentcore-evaluations-proof.md`](../docs/agentcore-evaluations-proof.md).

## Deploying

From a checkout, with SSH access to the instance:

```bash
deploy/push.sh <ssh-target>        # e.g. deploy/push.sh ec2-user@ec2-1-2-3-4.compute-1.amazonaws.com
```

`push.sh` builds `frontend/dist` **locally** and ships it. Node is never installed on the server:
the artefact is the built bundle, and a host that cannot build it cannot accidentally serve a
different one from the one that was tested.

The first run also executes `bootstrap.sh`; later runs just replace the tree and restart, which
takes a few seconds.

## Checking it

```bash
curl -fsS https://<distribution>.cloudfront.net/api/health          # {"status":"ok"}
curl -fsSI https://<distribution>.cloudfront.net/api/health | grep -i cache-control   # no-store
curl -fsS https://<distribution>.cloudfront.net/clinic/slots        # the seeded world

# Isolation, end to end: a world of your own, and a case list that is yours.
S=$(curl -fsS -X POST https://<distribution>.cloudfront.net/api/sessions | jq -r .session)
curl -fsS -H "X-CareLoop-Session: $S" https://<distribution>.cloudfront.net/api/cases
curl -fsS https://<distribution>.cloudfront.net/api/cases           # 400: names no world
```

The second one matters more than it looks. The dashboard polls `GET /api/cases` to watch a run
proceed, so a cached response reads exactly like a hung backend — see
`tests/test_api_static.py::TestNoCacheOnTheApi`, and check it **through the distribution** rather
than against the origin, because that is the only place a CDN misconfiguration can be seen.
