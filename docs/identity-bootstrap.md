# AWS identity bootstrap

Everything the production identity mode needs from AWS, and nothing else.

**Why this is a separate document and a separate hand.** The deployment identity,
`arn:aws:iam::265243686715:user/careloop-deployer`, is intentionally least-privilege for
*deploying* and cannot create identity infrastructure. That is the correct arrangement — a
credential that can deploy code and also mint user pools is a credential whose compromise creates
users — so these resources are provisioned once, by an administrator, from CloudShell, and their
identifiers are handed back as three environment variables. Nothing in this repository creates them
and nothing in this repository can.

**Nothing here is a secret.** An issuer URL, a public app-client id and a hosted-UI domain appear in
every token this deployment will ever see, and in the query string of every login redirect. There is
**no client secret**, deliberately: a browser client that had one would be storing it in a page
anybody can read, and PKCE exists precisely so that it does not need one.

---

## What to create

| Resource | Setting | Value |
| --- | --- | --- |
| User pool | Name | `careloop` |
| | Sign-in | Email |
| | MFA | Optional (a demo-adjacent deployment; not load-bearing for any claim CareLoop makes) |
| App client | Name | `careloop-web` |
| | Type | **Public** — no client secret |
| | Auth flows | Authorization code grant only |
| | PKCE | Required |
| | OAuth scopes | `openid` **only** |
| | Callback URL | `https://d2heuwvhvrdyig.cloudfront.net/` |
| | Sign-out URL | `https://d2heuwvhvrdyig.cloudfront.net/` |
| Hosted UI domain | Type | Cognito prefix domain — **no custom domain, no ACM certificate** |
| | Prefix | e.g. `careloop-<suffix>` (must be globally unique) |

**`openid` and nothing else.** CareLoop reads exactly one claim, `sub`, and resolves it against its
own directory. Asking for `email` or `profile` would collect personal data with no consumer and put
it in a token that travels through a CDN.

**One callback URL, and it is an origin with a trailing slash.** The frontend computes
`window.location.origin + "/"` and sends that as `redirect_uri`; Cognito matches callback URLs
exactly, so a mismatch is a login that fails at the redirect with a Cognito error page rather than
anywhere CareLoop can explain it. Add a second entry only if a second origin genuinely serves the
app.

## Commands, for an administrator to run — **not run by this session**

```bash
# 1. The user pool.
aws cognito-idp create-user-pool \
  --pool-name careloop \
  --auto-verified-attributes email \
  --username-attributes email \
  --region eu-west-1

# 2. The public app client. Note --no-generate-secret: a public PKCE client has none.
aws cognito-idp create-user-pool-client \
  --user-pool-id <POOL-ID> \
  --client-name careloop-web \
  --no-generate-secret \
  --allowed-o-auth-flows code \
  --allowed-o-auth-scopes openid \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers COGNITO \
  --callback-urls https://d2heuwvhvrdyig.cloudfront.net/ \
  --logout-urls https://d2heuwvhvrdyig.cloudfront.net/ \
  --region eu-west-1

# 3. The hosted-UI domain. The prefix must be globally unique.
aws cognito-idp create-user-pool-domain \
  --user-pool-id <POOL-ID> \
  --domain careloop-<SUFFIX> \
  --region eu-west-1

# 4. One caregiver to sign in as.
aws cognito-idp admin-create-user \
  --user-pool-id <POOL-ID> \
  --username <EMAIL> \
  --region eu-west-1
```

## What to hand back

```
CARELOOP_OIDC_ISSUER=https://cognito-idp.<region>.amazonaws.com/<POOL-ID>
CARELOOP_OIDC_AUDIENCE=<APP-CLIENT-ID>
CARELOOP_OIDC_LOGIN_DOMAIN=https://careloop-<SUFFIX>.auth.<region>.amazoncognito.com
```

…plus the `sub` of the created user, which is what
`careloop/identity/directory.py::SubjectMapping` resolves to a `caregiver_id`. Read it with:

```bash
aws cognito-idp admin-get-user --user-pool-id <POOL-ID> --username <EMAIL> --region eu-west-1
```

The JWKS endpoint is **derived** from the issuer (`<issuer>/.well-known/jwks.json`), so there is no
fourth identifier to keep in step.

## What is explicitly **not** required

Stated so nobody adds one out of habit:

- **No client secret.** Public PKCE client.
- **No custom domain and no ACM certificate.** The Cognito prefix domain is enough.
- **No ALB and no Lambda@Edge.** The topology is unchanged: CloudFront → EC2 → uvicorn.
- **No CloudFront change.** The origin request policy is `Managed-AllViewer`
  (`216adef6-5c7f-47e4-b989-5492eafa07d3`), measured as `HeaderBehavior: allViewer`, so viewer
  `Authorization` already reaches FastAPI.
- **No security-group change and no new port.** The origin stays CloudFront-only behind
  `pl-3b927c52`, and SSH stays closed.
- **No Secrets Manager entry and no SSM parameter.** None of the three values is a secret.
- **No IAM expansion of `careloop-deployer`.** It deploys code; it does not create users.
- **No new deployment step.** `deploy/bootstrap.sh` already runs
  `uv pip install -e "$APP[observability]"` on every deploy, so `PyJWT[crypto]` — added to
  `pyproject.toml`'s runtime dependencies — installs itself on the next
  `python scripts/deploy.py`.

## Turning it on

`careloop.env` ships `CARELOOP_AUTH_MODE=demo`, so a redeploy never switches production identity on
by accident. Switching it on is editing that one value and redeploying; switching it back is the
same edit in reverse. A production mode with a missing issuer or audience **refuses to start**
rather than serving anonymous traffic, and a value that is neither `demo` nor `production` is
refused too — a typo must not be the difference between authenticating and not.

The caregiver directory is read from `CARELOOP_CAREGIVERS_FILE`, a JSON document holding the
caregivers, their trusted destinations, their grants and the `(issuer, subject) → caregiver_id`
mapping. So switching production on is **configuration and not code**: three identifiers from the
bootstrap above, one file, and a redeploy. A file that is missing, unparseable, or names a caregiver
it does not declare stops the process at startup rather than answering every request `401` for a
reason nothing on the surface explains.

A minimal one, once the bootstrap returns a `sub`:

```json
{
  "caregivers": [
    {
      "caregiver_id": "cg-rosa",
      "destinations": {
        "caregiver_email": "rosa.marlowe@carer.example",
        "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
        "portal_id": "PORTAL-NORTHGATE-RECORDS"
      }
    }
  ],
  "grants": [{ "caregiver_id": "cg-rosa", "patient_id": "PT-4417" }],
  "subjects": [
    {
      "issuer": "https://cognito-idp.<region>.amazonaws.com/<POOL-ID>",
      "subject": "<SUB-FROM-ADMIN-GET-USER>",
      "caregiver_id": "cg-rosa"
    }
  ]
}
```

Nothing in it is a credential — no token, no password, no key. An address is *configuration*, the
address CareLoop may write to; a subject is an opaque identifier the issuer already puts in every
token.

## The deployed proof, as measured

**This was performed.** The resources above were provisioned, `CARELOOP_AUTH_MODE=production` was
deployed, and the seven checks below were carried out against the real CloudFront distribution —
never the origin — on **2026-08-24**, at commit **`49630e5`**. That is the commit the claim
certifies, and CareLoopBench renders it as exactly that:
[`evidence/live/production-identity.json`](../evidence/live/production-identity.json), read by
`careloopbench/ingest.py` and shown in [`evidence.md`](evidence.md) as
`PROVEN_AT 49630e5da939 · operator-attested`.

| # | Check | Result |
| --- | --- | --- |
| 1 | `GET /api/auth` reports `PRODUCTION` and publishes the login endpoints | measured |
| 2 | A protected route with **no** token answers `401` | measured |
| 3 | A malformed token and a tampered token both answer `401` | measured |
| 4 | A hosted-UI login yields a bearer token that reaches FastAPI, verifies, maps to a caregiver, and passes the patient grant | measured |
| 5 | That caregiver's booking reaches Demo World and the independent read-back returns `MATCHED` | measured — the case reached `DONE` |
| 6 | A workspace belonging to another principal does not resolve, and the clinic holds nothing extra | measured |
| 7 | The S3 upload path still works for an authorised caregiver | measured — a real `PutObject`, settled by a fresh read, case `DONE` |

Four things beyond the list were measured in the same pass and are recorded with it: the
authorization-code exchange used PKCE and the returned `state` matched exactly; the **ID token from
that same login was refused** where the access token was accepted; an `X-Caregiver` header supplied
by the caller carried no production authority; and an unauthenticated caller produced **zero**
external effects at the provider, asserted against the clinic's own record rather than against a
status code.

**The attestation is `OPERATOR`, and that word is doing real work.** No machine-readable artifact
exists for this run — it was a browser login and a sequence of requests through a CDN, and
CareLoop's deployment publishes no build identifier on the wire, so nothing in the transcript could
name the commit it ran against. The record therefore says a person observed this and attests to the
commit themselves. It is deliberately not dressed up as tool output. Making it `MEASURED` would
mean publishing a build id from `GET /api/health` or `GET /api/build`, which is a change to the
product's own surface and is a separate decision.

**What the record does not carry, on purpose:** no access token, no ID token, no `Authorization`
header, no Cognito subject and no session id. `careloopbench/ingest.py` refuses a stored proof
carrying any of them, by field name and by shape, and the repository's sweep over tracked text
files covers that directory as well. The caregiver (`cg-proof-01`) and the patient (`PT-4417`) are
synthetic fixtures and appear in the committed directory already.

**The distribution serves `demo` again.** `deploy/careloop.env` says so and
`tests/test_deployed_identity_configuration.py` asserts it, which is why the proof's declared
dependency surface is the identity boundary itself — `careloop/identity/`, `careloop/api/auth.py`,
`careloop/api/access.py`, `careloop/api/deps.py`, `deploy/caregivers.json` and the browser's login
service — and not the line that decides which mode is served. Flipping that one word back is a
one-value deploy; none of the code this proof is about has changed since it was taken, which is
what keeps the row `PROVEN_AT` rather than `STALE`.
