# Live evidence

Proofs that were performed once, elsewhere, against a system this repository does not control.
CareLoopBench **ingests** them; it never re-runs them, and it never promotes one to the commit a
reader happens to have checked out. Each row is rendered `PROVEN_AT <commit>` with the commit the
record names, or `STALE` once the paths that proof depends on have moved, or `NOT_RUN` when there
is no record at all.

## What may go here

One JSON envelope per proof, named after its `proof_id`, carrying the fields
`careloopbench/ingest.py` requires:

```
proof_id  tool  attested_commit  attestation  ingested_at_commit
produced_at  environment  surface_digest  raw
```

`attestation` is `MEASURED` when the stored record was produced by a tool that could name the
commit it ran against, and `OPERATOR` when a person recorded what they observed and attests to the
commit themselves. CareLoop's deployment publishes no build identifier on the wire, so a proof
driven through a browser can only be the second — and saying so costs nothing and is true.

## What may not

**Nothing credential-shaped, ever.** No token, no JWT, no Cognito subject, no session id, no
access key, no `Authorization` header. `read_envelope` refuses a record carrying a field named like
one or a value shaped like one, and the repository's own sweep over tracked text files covers this
directory as well.

**Nothing transcribed.** Where a tool produces machine-readable output, that output is stored under
`raw` verbatim. Where no such output exists, the record says so in `raw.note` and its `attestation`
is `OPERATOR`; it is never dressed up as though a tool had emitted it. A proof nobody can produce a
record for is `NOT_RUN`, and CareLoopBench prints the command that would produce one.

## What is missing, and how to produce it

Three declared proofs have no stored record. None of them is faked in the meantime.

| proof | produce it with |
| --- | --- |
| `live.agentcore-settlement` | `python scripts/evaluations_prove.py --out evidence/live/agentcore-settlement.json` |
| `live.objectstore-contract` | `python scripts/objectstore_contract.py --bucket <scratch> --out evidence/live/objectstore-contract.json` |
| `live.deployed-injection-lock` | `python scripts/verify_lock.py --out evidence/live/deployed-lock.json` |

Each spends real AWS or real model calls, which is exactly why none of them runs from a test.
Wrap the tool's own output in the envelope above rather than editing it.
