# deploy/

How CareLoop is put on a host a judge can reach. A peer of `demoworld/`, `bench/` and `frontend/`:
nothing under `careloop/` imports anything here, and nothing here decides anything the application
would otherwise decide.

## The shape, and why

One EC2 instance runs two processes; CloudFront puts HTTPS and one origin in front of them.

```
https://<distribution>.cloudfront.net
  /            →  instance:8000   the built frontend, served by FastAPI itself
  /api/*       →  instance:8000   the dashboard API
  /clinic/*    →  instance:8100   Demo World, so a judge can check the booking themselves
```

**One origin is the point.** `careloop/` mounts no `CORSMiddleware` anywhere and this arrangement
is what keeps that true off a developer's machine: the frontend keeps making relative `/api` calls
exactly as it does behind the Vite dev proxy, and no cross-origin request is ever made. Serving the
built app from a different host would need CORS, and weakening that is not a deployment's decision
to take.

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
more than Demo World's own cap: if this side held more, a live visitor's clinic could be evicted
underneath them and a read-back would report an appointment missing for a reason that has nothing
to do with the outside world.

## Files

| File | What it is |
|---|---|
| `careloop.env` | non-secret configuration, read by both units via `EnvironmentFile` |
| `careloop-api.service` | uvicorn: the API and the built frontend, port 8000 |
| `careloop-demoworld.service` | the clinic, port 8100 |
| `bootstrap.sh` | runs **on** the instance: installs Python, the venv, the units |
| `push.sh` | runs **locally**: builds the frontend, ships the tree, restarts the services |

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
