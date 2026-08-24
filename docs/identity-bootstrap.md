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

The caregiver directory is composed at the composition root and `scripts/demo_serve.py` passes
`directory=None`, so **that entry point refuses to start in production mode today**. That is
deliberate and is the remaining code-side gap: a real deployment supplies a real directory holding
the caregivers, their trusted destinations, their grants and the `(issuer, subject) → caregiver_id`
mapping, and this server is the judge-facing demo.

## What the deployed proof must show, once this exists

Through the real CloudFront distribution, never the origin:

1. `GET /api/auth` reports `PRODUCTION` and publishes the login endpoints.
2. A protected route with **no** token answers `401`.
3. A malformed and an expired token both answer `401`.
4. A hosted-UI login yields a bearer token that reaches FastAPI, verifies, maps to a caregiver, and
   passes the patient grant.
5. That caregiver's booking reaches Demo World and the independent read-back returns `MATCHED`.
6. A second caregiver is refused that case with `404`, and the clinic holds nothing extra.
7. The S3 upload path still works for an authorised caregiver.

Until all seven are measured, the production identity claim is not earned.
