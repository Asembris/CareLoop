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
through this distribution is measured — `X-Caregiver` reaches the origin, and so does
`Authorization` under the `Managed-AllViewer` origin request policy — and cookie forwarding is not,
so a cookie would be an assumption that fails in production rather than in a test.

**In demo mode the id is an isolation namespace and not authentication**: nothing is verified,
exactly as nothing about `X-Caregiver` is. Possession is enough, which is why `?session=` in a URL
is acceptable there and nowhere else.

**In production mode possession is not enough.** The workspace records the verified caregiver who
minted it, and every later request must present the same principal; a request naming somebody
else's workspace is answered `410`, identically to one naming a workspace that never existed.
Minting requires an authenticated principal, so the anonymous capacity exhaustion the demo permits
is unreachable there.

Session ids no longer appear whole in log lines — see `careloop.api.sessions.named`, which logs the
first eight characters. A WARNING carrying a whole one used to hand anybody who could read the logs
everything that world could do.

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
| `../scripts/deploy.py` | runs **locally**: the one deploy command. Builds, packs, stages through S3, drives SSM, rolls back |
| `../scripts/deploy_artifact.py` | what goes in the artefact and what does not, with the exclusions in one place |
| `../scripts/verify_deploy.py` | asserts the deployed properties **through the distribution**, at the end of every deploy |
| `../scripts/observability.py` | the one span processor CareLoop adds: it removes an attribute and does nothing else |

`push.sh` is gone. It shipped the tree over `scp` and needed inbound port 22; SSM replaced it, and
**port 22 is closed and stays closed**.

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

Not performed by the deploy and not automated on purpose — each of these is an account-level change
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

From a checkout with AWS credentials for this account, and **no SSH**:

```bash
pip install -e ".[deploy]"
```

```bash
python scripts/deploy.py
```

The `deploy` extra is `boto3[crt]` and nothing else — `deploy_artifact.py` is standard library and
`verify_deploy.py` uses `urllib`. The `crt` half is not optional: an `aws login` profile carries a
`login_session`, and botocore's provider for it refuses without `botocore[crt]`. Credentials
themselves come from the shell — a profile, an instance role, or environment variables — and never
from this repository. The frontend build needs Node, which no Python extra can supply.

```bash
python scripts/deploy.py --dry-run     # build, check, print every remote script, touch no AWS
```

**Systems Manager is the deployment path and there is no other one.** The instance's own agent opens
an outbound session to AWS; the artefact travels through a private S3 bucket; authority is IAM.
Nothing inbound is opened, no key is transported, and **port 22 is closed** — a deploy that needs it
is a deploy that has gone wrong, not a security group to edit.

What the one command does, in order:

| Step | What it means |
|---|---|
| verify HEAD | refuses a working tree that does not match the commit; the artefact carries the commit's own blobs |
| build the frontend | locally, so Node never goes on the server and the bundle served is the bundle tested |
| pack the artefact | `scripts/deploy_artifact.py` — tracked tree at `HEAD`, minus the exclusions, plus `frontend/dist` |
| checksum and scan | a deterministic SHA-256, and a member scan that refuses anything secret |
| upload | to the private artefact bucket, SSE-S3, after asserting the bucket blocks public access |
| preflight | on the instance: disk, tools, download, **verify the digest**, scan the member list again, unpack nothing |
| rollback snapshot | `cp -a /opt/careloop/app /opt/careloop/app.prev` before anything moves |
| preserve `careloop.env` | the deployed file is copied aside, and the operator values it sets are carried into the new one |
| inject `CARELOOP_UPLOAD_BUCKET` | only when `--upload-bucket` is given; otherwise whatever the instance has is kept |
| demo-state reset | **only when the boundary marker says the store predates the current identity boundary** |
| atomic swap | two renames on one filesystem, after the new tree is fully extracted and checked |
| `bootstrap.sh` | the same idempotent script as ever, from the new tree |
| health | both units active, both origins answering, on the instance |
| `verify_deploy.py` | **through the distribution**, which is the only place a CDN misconfiguration is visible |
| cleanup | the transfer artefact is removed from the instance and the object deleted from S3 |

**Rollback is automatic, and only for what a rollback can undo.** A failing `bootstrap.sh` or a
failing health check restores `app.prev` and bootstraps again, and the deploy exits non-zero saying
so. A **demo-state reset is not undone** — deleting a synthetic store is not reversible, and the
tool says that rather than implying otherwise. A `verify_deploy.py` failure is reported and *not*
rolled back: the origin already answered, so a rollback would not address what is wrong.

Useful flags: `--upload-bucket`, `--force-state-reset`, `--no-build`, `--no-verify`,
`--instance-id`, `--artifact-bucket`, `--demo-url`.

### The demo-state boundary

`scripts/deploy.py` carries `DEMO_STATE_BOUNDARY` — `2026-08-24-booking-identity` — and the instance
carries `/opt/careloop/state/.boundary`. When they disagree, the synthetic store is cleared once and
the marker rewritten; when they agree, nothing is touched. That is what makes *CLAUDE.md*'s "a clean
demo-state reset is required immediately before the first deployment containing this boundary" a
comparison rather than something an operator has to remember. Changing the constant is what makes
the next reset happen — once.

Everything in the store is synthetic. There is deliberately no migration and no legacy-key
fallback: a `DONE` case booked before a booking-identity boundary and reopened after it misses
duplicate suppression and sends a real second `POST`.

### What has to exist first

Not created by the deploy, and each one an account-level decision:

| Resource | Requirement |
|---|---|
| the EC2 instance | SSM Agent running, and an instance profile carrying `AmazonSSMManagedInstanceCore` |
| the instance profile | plus `s3:GetObject` on the artefact bucket, the Bedrock statement, the telemetry statement, and — if uploads are real — the upload-bucket statement in [`../docs/deployment-objectstore.md`](../docs/deployment-objectstore.md) |
| the artefact bucket | **permanent and private**: `careloop-deploy-artifacts-<account>-us-east-1` by default. Block Public Access on, SSE-S3, and a short lifecycle rule on `deploy/`. The deploy *asserts* it blocks public access and refuses if it cannot establish that |
| the deploying principal | `s3:PutObject`, `s3:DeleteObject`, `s3:GetBucketPublicAccessBlock` on that bucket; `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:DescribeInstanceInformation`; `sts:GetCallerIdentity` |
| the CloudFront distribution | already exists; the deploy neither creates nor invalidates it |

The artefact object is deleted at the end of every deploy. The lifecycle rule is the backstop for a
run that was interrupted before it got there.

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


## Identity configuration

Three variables in `careloop.env`, none of them a secret, and a fourth that is optional:

| Variable | Meaning |
| --- | --- |
| `CARELOOP_AUTH_MODE` | `demo` (or unset) or `production`. Anything else **refuses to start**. |
| `CARELOOP_OIDC_ISSUER` | The issuer exactly as it appears in a token's `iss`. JWKS is derived from it. |
| `CARELOOP_OIDC_AUDIENCE` | The app client id, matched against `client_id` on an access token. |
| `CARELOOP_OIDC_LOGIN_DOMAIN` | The hosted-UI origin, for the browser login. Optional. |
| `CARELOOP_CAREGIVERS_FILE` | JSON: caregivers, their destinations, their grants, their subject mappings. |

With `CARELOOP_AUTH_MODE=production`, a missing issuer or audience stops the process at startup
rather than degrading it — the treatment an unguarded destination already gets from the policy
loader. There is **no client secret variable**: the browser client is a public PKCE client, and a
secret stored in a page anybody can read is not a secret.

The committed file ships `CARELOOP_AUTH_MODE=demo`, so a redeploy of this file never switches
production identity on by accident.


## Tearing it down

For whoever is keeping this running. It is here rather than in `docs/judges.md`, where it used to
sit: a judge's guide should not end with the commands that destroy the thing they were reading
about.

**After judging closes (2026-10-08), terminate the instance — do not stop it.** A stopped EC2
instance still bills its EBS volume every month, and the Elastic-IP-equivalent charge for a public
IPv4 address continues too. Stopping looks like turning it off and is not.

```
aws ec2 terminate-instances --instance-ids i-0aeb7e7150cd7524b
aws cloudfront delete-distribution --id E33IXQBGGBMTTV --if-match <etag>   # disable first
```

The CloudFront distribution must be **disabled and fully deployed** before it can be deleted; the
S3 bucket holding AgentCore deployment packages has a 30-day object expiry and needs nothing.
Everything in the store is synthetic, so nothing needs preserving before teardown.
