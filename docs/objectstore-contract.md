# The object store's write contract, measured

CareLoop's second genuinely external effect — `upload_document` — files a document into Amazon S3.
This document records what was **measured** about that provider, what was not, and which parts of
CareLoop's design each measurement earns.

It exists for the reason [`docs/agentcore-evaluations-proof.md`](agentcore-evaluations-proof.md)
does: a claim about a third-party system is worth exactly as much as the run that established it,
and a run nobody wrote down is a claim nobody can check. Everything below marked **MEASURED** was
observed against real S3. Everything else says so.

The assertions are executable — [`scripts/objectstore_contract.py`](../scripts/objectstore_contract.py)
— and the offline suite runs the *same module* against the loopback substitute in `objectstore/` on
every CI run. A substitute that drifts from the provider turns the operator's run red. That is the
only mechanism keeping this page true after the day it was written.

## Environment

Measured in AWS CloudShell, `us-east-1`.

| | |
| --- | --- |
| Python | 3.13.14 |
| boto3 | 1.43.38 |
| botocore | 1.43.57 |
| AWS CLI | 2.36.25 |
| Encryption | SSE-S3 (`AES256`) |
| Bucket versioning | never enabled |
| Bucket | temporary, deleted after the measurement |

No measurement resource remains. The temporary object was deleted, the bucket confirmed at
`KeyCount: 0`, and the bucket deleted.

## What was measured

### The write is create-once, and the precondition is what makes it so

| # | Call | Observed | Status |
| --- | --- | --- | --- |
| C1 | `PutObject(IfNoneMatch="*", ChecksumAlgorithm="SHA256")` on an absent key | HTTP `200`, object created, `ChecksumSHA256` returned, `VersionId: None` | **MEASURED** |
| C2 | the same key, the **same** bytes, `IfNoneMatch="*"` | HTTP `412`, `Error.Code == "PreconditionFailed"`, no second create | **MEASURED** |
| C3 | the same key, **different** bytes, `IfNoneMatch="*"` | HTTP `412`, `PreconditionFailed`; a fresh `GetObject` afterwards returned the *original* bytes (`MatchesA: True`, `MatchesB: False`) | **MEASURED** |
| C4 | the same key, different bytes, **without** the precondition | HTTP `200`; a fresh `GetObject` returned the new bytes (`MatchesA: False`, `MatchesB: True`) | **MEASURED** |

**C4 is the reason `IfNoneMatch="*"` is described as load-bearing rather than defensive.** Remove it
and the provider silently overwrites. It is not a retry optimisation and it is not politeness to the
service; it is the entire difference between a store CareLoop can converge on and a store in which
a replay destroys evidence.

**C3 is the sharper of the two refusals.** A `412` on its own says only that the write did not
happen. That the *existing* object was untouched is a separate fact and was separately measured, and
it is what lets a reconciliation treat a `412` as an invitation to look rather than as damage.

### The observation is a fresh read of the bytes

| Call | Observed | Status |
| --- | --- | --- |
| `GetObject` | returned the actual stored bytes; CareLoop-side SHA-256 over them matched the digest of the authorised held document exactly | **MEASURED** |
| `GetObject` immediately after `PutObject` | HTTP `200`/`200`, `ImmediateReadMatches: True`, with no delay and no retry | **MEASURED** |
| `Metadata` round-trip (`document-id`, `portal-id`) | returned unchanged | **MEASURED** |
| `DeleteObject` then `GetObject` | delete HTTP `204`; read HTTP `404`, `Error.Code == "NoSuchKey"` | **MEASURED**, under the CloudShell principal |
| `ListObjectsV2` on the prefix | `KeyCount: 1` | **MEASURED** |
| a fresh Python process reading the object an earlier process created | HTTP `200`, 34 bytes, same SHA-256, metadata preserved, `ServerSideEncryption: AES256` | **MEASURED** |
| `GetBucketVersioning` | no enabled status | **MEASURED** |
| the whole create-once/read-back flow under `ServerSideEncryption = AES256` | works | **MEASURED** |

**The primary settlement mechanism is `GetObject` followed by hashing the bytes the provider
returned**, compared against a digest computed freshly from the document CareLoop holds. Both sides
are values this process computed, over two byte strings it has actually seen.

### Supporting evidence, and what it is not

`HeadObject(ChecksumMode="ENABLED")` returned a `ChecksumSHA256` that matched the locally computed
SHA-256 in base64. **MEASURED.**

This is corroboration and is used as corroboration: it is compared against the write path's own
expectation, where it adds a check the closed loop would otherwise make vacuously. It is **not** the
settlement proof, and this measurement does not license the sentence *"AWS independently computed
the digest"* — what was observed is that a value the service reported agreed with a value CareLoop
computed. Settlement rests on the returned bytes.

## What was not measured

| | |
| --- | --- |
| `IfNoneMatch` interaction with a bucket lifecycle rule | **NOT MEASURED.** Deployment-phase concern; the demo's lifecycle window is far longer than any case's life. |
| `IfNoneMatch` with bucket versioning **on** | **NOT MEASURED**, and deliberately out of scope: versioning stays off, which is what keeps an absent read unambiguous. |
| Per-request cost and throttling | **NOT MEASURED.** Irrelevant at fixture scale — the documents are ~2 KB and S3 permits 3,500 `PUT`/s per prefix. |
| `404` versus `403` **under the deployed EC2 instance role** | **NOT MEASURED.** See below. |

### The one open measurement, and why it is safe to defer

S3 answers `403 AccessDenied` rather than `404 NoSuchKey` for a missing key when the caller lacks
`s3:ListBucket` on the prefix. The measurement above was taken under the CloudShell principal, which
had it. **The deployed instance role must be measured again**, and
[`scripts/verify_deploy.py`](../scripts/verify_deploy.py) is where that belongs.

Deferring it is safe because the classification rule fails closed either way:

```
NoSuchKey    → ObservationAbsent       a definite answer about an object CareLoop wrote
AccessDenied → ObservationUnavailable  a statement about the caller, never about the object
```

An absence CareLoop cannot distinguish from a permissions failure is reported as *unavailable*, which
parks the case. The `s3:ListBucket` grant exists in the deployment policy for the sole purpose of
making `404` mean `404`; without it the system is still correct and merely less informative.

## What these measurements earn

| Design decision | Earned by |
| --- | --- |
| resource key derived from the durable action identity, never from the bytes | C1–C4: the key is chosen by the caller and the store has no opinion about contents |
| `PutObject(..., IfNoneMatch="*")` on every write **and every replay** | C4 |
| a `412` is not a failure and not a success | C2, C3 |
| a `412` is reconciled by a fresh `GetObject` and an exact content and metadata match | C3 plus the read-back rows |
| an absent key is a definite external answer, and never a licence to write again | the delete/`NoSuchKey` row plus the `AccessDenied` rule above |
| provider state outlives the CareLoop process | the fresh-process row |

## What is deliberately still not claimed

`RECOVERY_CLASS["upload_document"] = IDEMPOTENT_REPLAYABLE` is a claim about **CareLoop**, not about
S3. This page establishes that re-issuing a frozen identity cannot create a second object and cannot
mutate the first. That the implementation actually re-issues the frozen identity, always keeps the
precondition, reconciles a `412` through a fresh read, fails closed on divergence and asks no model
anything is proven in `tests/`, not here.

Note one honest difference from the clinic, which the `RecoveryClass` docstring records: S3 does
**not** return the original resource when a duplicate key arrives. It refuses, and CareLoop composes
the result from a second call. The safety property is the same; the mechanism is not.
