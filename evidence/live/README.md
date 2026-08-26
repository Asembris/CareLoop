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

## How a record gets here

`careloopbench/record.py` writes it, and nothing else does. An operator runs the proof to a scratch
path and then names it:

```
python scripts/objectstore_contract.py --bucket <bucket> --prefix <unique> --out scratch/os.json
python -m careloopbench.record live.objectstore-contract --raw scratch/os.json \
    --attestation MEASURED --environment "real Amazon S3, us-east-1, ..."
```

**The gate is the reason it exists.** Every proof tool writes its `--out` file *and then* returns a
non-zero exit code, so a failed run leaves behind a file shaped exactly like a successful one — and
`ingest_one` has three outcomes, none of them `FAILED`, so a stored record of a failed run would
render `PROVEN_AT`. So each tool serialises its own verdict as `passed`, and the recorder refuses
anything that is not `passed: true`. A proof that did not hold keeps its scratch file for diagnosis
and never becomes evidence.

**The registry owns everything a reader checks.** There is no `--out`, no `--surface` and no
`--digest`: the destination, the tool name and the dependency surface come from the
`ArtifactRef` in `careloopbench/claims.py`, so the writer and the reader cannot disagree about what
a record is. Repeat `--raw` for a proof made of several runs; they are stored under `runs`, in
order, unmerged. `--commit` names a build this repository cannot observe and is refused on a
`MEASURED` record.

## What is missing, and how to produce it

Two declared proofs have no stored record. Neither is faked in the meantime.

| proof | produce it with |
| --- | --- |
| `live.agentcore-settlement` | `scripts/evaluations_prove.py`, once per scenario, then `--raw` each |
| `live.deployed-injection-lock` | `scripts/verify_lock.py`, against the distribution |

Both are measured against the *deployed* build, which publishes no identifier on the wire, so both
are `OPERATOR`-attested and both wait for a deployment that is happening anyway. Each spends real
AWS or real model calls, which is exactly why neither runs from a test.

## When to run one again

The surface digest already answers this and no schedule is needed. **Re-run a live proof when
CareLoopBench reports it `STALE` and the row needs to be current, plus once at the final
pre-submission refresh.** A proof whose surface has not moved is still about this code, however
long ago it was taken; a proof whose surface has moved says so on every invocation.
