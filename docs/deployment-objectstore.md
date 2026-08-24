# Deploying the second external effect

What has to exist in AWS before `upload_document` is real, and what has to be measured once it is.

Nothing here was performed by the session that wrote it. The provider *contract* was measured and is
recorded in [`objectstore-contract.md`](objectstore-contract.md); the deployment below is described,
not done.

## What CareLoop needs

One private S3 bucket, one environment variable, and a policy on the instance profile the EC2 host
already carries. No access key, no secret, no session token, and nothing a judge supplies — the same
posture Bedrock has had since the beginning.

```
CARELOOP_UPLOAD_BUCKET=careloop-demo-uploads-<account>-us-east-1
```

Absent, every upload is simulated, exactly as every booking is without `CARELOOP_CLINIC_URL`. That
is the default and it is why nothing has to remember to switch this off.

**`CARELOOP_UPLOAD_BUCKET` is the only environment variable this feature has**, and it is read in
one place: `scripts/demo_serve.py`'s `--upload-bucket` default. `deploy/careloop.env` ships it
**empty**, so a deploy does not decide it; `scripts/deploy.py` carries whatever the instance already
has forward across the swap, and `--upload-bucket` sets it when an operator means to change it.

**There is no `CARELOOP_UPLOAD_PREFIX`, and there never was.** The key prefix is
`careloop.tools.objectstore.DEFAULT_PREFIX`, a module constant whose value is `careloop`, and
`ObjectStoreClient` takes it as a keyword argument with that default. Nothing reads an environment
variable for it and nothing in the deployment can change it — every key CareLoop writes is
`careloop/<session>/<idempotency-key>`. That matters here rather than being trivia: the lifecycle
rule and both IAM statements below name `careloop/*` literally, so they are correct because the
constant says so, and a rename of that constant is an IAM change in the same commit. If the prefix
ever does become configurable, this table stops being true the moment somebody sets it.

## The bucket

| Setting | Value | Why |
| --- | --- | --- |
| Block Public Access | **on**, bucket *and* account | Nothing filed here is ever served to anybody. |
| Default encryption | SSE-S3 (`AES256`) | Measured compatible with the create-once flow. |
| Versioning | **off** | What keeps a read of a deleted key an unambiguous absence. Turning it on changes what `ABSENT` means and is not a setting to flip casually. |
| ACLs | disabled (bucket owner enforced) | There is no cross-account anything here. |
| Lifecycle | expire `careloop/*` after 1 day | The backstop for objects a killed process never cleared. |
| Lifetime | **permanent** | Not created or deleted by a deploy. `scripts/deploy.py` neither creates nor reconciles it, and a run that cannot find it refuses rather than making one. |

**Two private buckets, and they are not the same bucket.** This one holds uploaded documents and is
named by `CARELOOP_UPLOAD_BUCKET`. The *deploy artefact* bucket —
`careloop-deploy-artifacts-<account>-us-east-1` by default, prefix `deploy/` — is where
`scripts/deploy.py` stages the tarball between a laptop and the instance, and is described in
[`../deploy/README.md`](../deploy/README.md). Both are permanent, both Block Public Access, and
neither is created by a deploy; keeping documents and deployment artefacts apart means the instance
role's `s3:GetObject` on one grants nothing over the other.

**The lifecycle rule fixes something the clinic still lacks.** A CareLoop process that is *killed*
rather than shut down orphans the clinic worlds it held, permanently, because Demo World no longer
evicts. Here the same failure is self-healing within a day. The window is far longer than any case's
life, so a live demo never meets it.

## The instance role

Least privilege, and scoped to the prefix — which is the literal `careloop` of `DEFAULT_PREFIX`, not
a value anybody configures.

The instance profile carries three other things this feature does not own and cannot work without:
the Bedrock statement, the telemetry statement from [ADR 0004](adr/0004-agentcore-observability-out-
of-band.md), and — since the deploy became SSM-driven — `AmazonSSMManagedInstanceCore` plus
`s3:GetObject` on the deploy artefact bucket. **No access key is involved in any of them**, here or
there; that is the whole posture and the reason `deploy/careloop.env` is committed.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::careloop-demo-uploads-<account>-us-east-1/careloop/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::careloop-demo-uploads-<account>-us-east-1",
      "Condition": { "StringLike": { "s3:prefix": ["careloop/*"] } }
    }
  ]
}
```

**`s3:ListBucket` is there for a semantic reason, not a convenience one.** Without it S3 answers
`403 AccessDenied` for a key the caller cannot see, rather than `404 NoSuchKey` — and CareLoop
classifies those differently on purpose:

```
NoSuchKey    → ObservationAbsent       a definite answer about an object CareLoop wrote
AccessDenied → ObservationUnavailable  a statement about the caller, never about the object
```

Both fail closed, so a deployment that got this wrong would still be *correct* — it would report
"could not establish" where it could have reported "the object is gone". The grant is what buys the
sharper answer. `tests/test_upload_mutations.py::M10` is what fails if anybody ever decides to read a
`403` as absence.

## What must be measured after deploying, and has not been

`docs/objectstore-contract.md` records the contract as measured under the **CloudShell principal**,
which held `s3:ListBucket`. The deployed instance role is a different principal and the `404`-versus-
`403` behaviour has to be observed again under it. `scripts/verify_deploy.py` is where that belongs,
beside the `clinic_world_ended` check it already makes.

Until that measurement exists, the honest statement is: *absence is classified correctly by
construction, and whether the deployed role can distinguish absence from denial is unmeasured.*

## Cleanup, and what a reset means

`POST /api/demo/reset` deletes every object under the presented session's prefix and reports the
count as `objects_removed` — `null` on a CareLoop with no bucket, which is a different fact from `0`.

**This is load-bearing rather than housekeeping.** The frozen idempotency key is derived from the
case id, the action type and the identifying arguments, all of which a demo scenario fixes — so a
second press of the `records` scenario addresses *the same object*. Without the clear, the
create-once precondition refuses that write and the visitor watches a reconciliation settle an old
object instead of watching a document being filed.
`tests/test_demo_upload.py::TestAResetClearsThisWorldsObjects` holds both halves, including the
vacuity one.

The session id is the namespace and nothing more: not a credential, verified by nothing, and exactly
what `X-Demo-Session` is to the clinic. What it buys is that two visitors pressing the same button
file two objects rather than colliding on one.

## What is deliberately not here

**No AgentCore evaluation.** `CareLoopSettlementIntegrity` judges booking and answers an upload
trace `NOT_APPLICABLE`. That is a decision, recorded as `NOT_EVALUATED_IN_AWS` in
`tests/test_evaluator_drift.py` and proven behaviourally in `tests/test_evaluator_upload_scope.py`.
Extending it means a second Lambda deployment, a second mutation campaign and a second set of
recorded results, and it is its own roadmap item.

**No new telemetry.** The nine attributes are still nine. An upload emits them with
`careloop.effect.type = upload_document` and `careloop.effect.fields = document_id,portal_id`.

**No credentials in the repository, in the frontend, or in a browser.** `deploy/careloop.env` names
a bucket, which is not a secret. `tests/test_nothing_secret_is_tracked.py` sweeps for the rest.
