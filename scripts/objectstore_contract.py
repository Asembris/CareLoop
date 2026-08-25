"""The object store's write contract, as executable assertions.

**One module, two endpoints, and that is the whole point of it.** The offline suite runs these
assertions against the loopback substitute in ``objectstore/`` on every CI run; an operator runs
the identical assertions against real Amazon S3 and records the result in
``docs/objectstore-contract.md``. A substitute that drifts from the provider it stands in for turns
the operator run red, which is the only thing that keeps "we measured this" true after the week it
was measured.

**It imports no CareLoop.** What it establishes is a fact about a provider, and a contract test that
reached into the application would be checking the application's beliefs about the provider rather
than the provider. The same reasoning ``evaluators/`` is a separate package for.

**Ordering is load-bearing and the sequence is destructive on purpose.** ``overwrite-without-the-
precondition`` really does overwrite, and ``absence-after-delete`` really does delete, so they run
last and in that order. Anything reading the created object has to run before them. A shuffled
version of this file would still pass and would establish something weaker.

Every check reports what it *observed*, not merely whether it liked it. A run that fails says which
status arrived and which error code, because the interesting outcome of a contract test is usually
not "false" but "false, and here is the provider's actual behaviour".

Usage::

    python scripts/objectstore_contract.py --bucket my-scratch-bucket
    python scripts/objectstore_contract.py --bucket b --endpoint-url http://127.0.0.1:9000

The real-S3 form creates and deletes objects under ``--prefix`` and nothing else. It never creates,
configures or deletes a bucket: pointing it at a bucket is the operator's decision and cleaning one
up is the operator's job.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

#: What the conditional create asks for. **Load-bearing**: without it the same key accepts a
#: different body and silently overwrites, measured, which is the failure mode the whole create-once
#: design exists to remove.
IF_NONE_MATCH_ANY = "*"

#: What a refused conditional create answers with.
PRECONDITION_FAILED = "PreconditionFailed"

#: What a read of a key the store does not hold answers with. Distinct from ``AccessDenied``, which
#: is a statement about the caller rather than about the object and must never be read as absence.
NO_SUCH_KEY = "NoSuchKey"

#: The checksum algorithm asked of the provider on write, and computed locally for comparison.
CHECKSUM_ALGORITHM = "SHA256"

#: Metadata keys the contract carries. Lower-cased by the protocol; asserted as round-tripped.
DOCUMENT_METADATA = "document-id"
PORTAL_METADATA = "portal-id"

ORIGINAL = b"%PDF-1.4 receipt RCT-2026-00841 172.00"
DIVERGENT = b"%PDF-1.4 receipt RCT-2026-00841 999.00"

METADATA = {DOCUMENT_METADATA: "doc-0005-receipt", PORTAL_METADATA: "PORTAL-NORTHGATE-RECORDS"}


def sha256_hex(data: bytes) -> str:
    """Lowercase hexadecimal SHA-256. The form CareLoop compares in."""
    return hashlib.sha256(data).hexdigest()


def sha256_base64(data: bytes) -> str:
    """The same digest in the encoding S3 reports it in.

    Converted at this boundary and nowhere above it: an encoding is a fact about the protocol.
    """
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


@dataclass(frozen=True, slots=True)
class Check:
    """One contract assertion and what running it observed."""

    name: str
    satisfied: bool
    observed: str

    def __str__(self) -> str:
        return f"{'PASS' if self.satisfied else 'FAIL'}  {self.name:38}  {self.observed}"


@dataclass
class Report:
    """Every check, in the order it ran."""

    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, satisfied: bool, observed: str) -> None:
        self.checks.append(Check(name=name, satisfied=satisfied, observed=observed))

    @property
    def satisfied(self) -> bool:
        return all(check.satisfied for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.satisfied]


def _error_code(failure: Exception) -> str:
    """The provider's own error code, or the exception type when there is not one.

    Read from the structured response rather than from ``str(failure)``. A contract that told two
    refusals apart by their prose would be a contract the provider could reword out from under.
    """
    response = getattr(failure, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str):
            return code
    return type(failure).__name__


def _status(response: dict[str, Any]) -> int:
    return int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _refusal(call: Callable[[], Any]) -> tuple[str, int]:
    """Run ``call``, expecting it to be refused. Returns ``(error code, status)``.

    ``("", 0)`` when it was *not* refused, which every caller reports as a failed check rather than
    raising: a provider that accepted something it should have refused is a finding, not a crash.
    """
    try:
        call()
    except Exception as failure:  # any refusal at all is the observation being made
        response = getattr(failure, "response", None)
        status = _status(response) if isinstance(response, dict) else 0
        return _error_code(failure), status
    return "", 0


def run_contract(client: Any, bucket: str, prefix: str) -> Report:
    """Execute every contract assertion against one endpoint. Returns what it observed.

    ``client`` is a boto3 S3 client. Nothing here knows whether it points at Amazon or at the
    loopback substitute, which is the property that makes one run comparable to the other.
    """
    report = Report()
    key = f"{prefix.rstrip('/')}/contract-object"

    # -- C1: conditional create on an absent key ----------------------------------------------
    created = client.put_object(
        Bucket=bucket,
        Key=key,
        Body=ORIGINAL,
        IfNoneMatch=IF_NONE_MATCH_ANY,
        ChecksumAlgorithm=CHECKSUM_ALGORITHM,
        Metadata=dict(METADATA),
        ContentType="application/pdf",
    )
    report.record(
        "create-once-on-absent-key",
        _status(created) == 200,
        f"HTTP {_status(created)}",
    )
    report.record(
        "create-returns-a-checksum",
        created.get("ChecksumSHA256") == sha256_base64(ORIGINAL),
        f"ChecksumSHA256={created.get('ChecksumSHA256')!r}",
    )

    # -- C2: the same key with the same bytes --------------------------------------------------
    code, status = _refusal(
        lambda: client.put_object(
            Bucket=bucket,
            Key=key,
            Body=ORIGINAL,
            IfNoneMatch=IF_NONE_MATCH_ANY,
            ChecksumAlgorithm=CHECKSUM_ALGORITHM,
            Metadata=dict(METADATA),
            ContentType="application/pdf",
        )
    )
    report.record(
        "same-key-same-bytes-is-refused",
        code == PRECONDITION_FAILED and status == 412,
        f"{code or '(accepted)'} HTTP {status}",
    )

    # -- C3: the same key with different bytes -------------------------------------------------
    code, status = _refusal(
        lambda: client.put_object(
            Bucket=bucket,
            Key=key,
            Body=DIVERGENT,
            IfNoneMatch=IF_NONE_MATCH_ANY,
            ChecksumAlgorithm=CHECKSUM_ALGORITHM,
            Metadata=dict(METADATA),
            ContentType="application/pdf",
        )
    )
    report.record(
        "same-key-divergent-bytes-is-refused",
        code == PRECONDITION_FAILED and status == 412,
        f"{code or '(accepted)'} HTTP {status}",
    )

    # The refusal on its own does not say the object survived it. This is what does.
    fetched = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    report.record(
        "the-refused-write-changed-nothing",
        fetched == ORIGINAL,
        f"held bytes match the original: {fetched == ORIGINAL}",
    )

    # -- the primary settlement mechanism ------------------------------------------------------
    read = client.get_object(Bucket=bucket, Key=key)
    body = read["Body"].read()
    report.record(
        "fresh-read-returns-the-stored-bytes",
        sha256_hex(body) == sha256_hex(ORIGINAL),
        f"sha256={sha256_hex(body)[:16]}...",
    )
    report.record(
        "metadata-round-trips",
        {name: read.get("Metadata", {}).get(name) for name in METADATA} == METADATA,
        f"Metadata={read.get('Metadata')!r}",
    )

    # -- supporting evidence only. Never the primary proof; see the module docstring. ----------
    head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    report.record(
        "reported-checksum-corroborates",
        head.get("ChecksumSHA256") == sha256_base64(ORIGINAL),
        f"ChecksumSHA256={head.get('ChecksumSHA256')!r}",
    )

    listed = client.list_objects_v2(Bucket=bucket, Prefix=key)
    report.record(
        "exactly-one-object-under-the-key",
        listed.get("KeyCount") == 1,
        f"KeyCount={listed.get('KeyCount')!r}",
    )

    # -- C4: what the precondition is holding back. Destructive, so it runs late. ---------------
    overwritten = client.put_object(
        Bucket=bucket, Key=key, Body=DIVERGENT, ContentType="application/pdf"
    )
    now_held = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    report.record(
        "without-the-precondition-it-overwrites",
        _status(overwritten) == 200 and now_held == DIVERGENT,
        f"HTTP {_status(overwritten)}, now holds the divergent bytes: {now_held == DIVERGENT}",
    )

    # -- absence. Last, because there is nothing left afterwards. -------------------------------
    deleted = client.delete_object(Bucket=bucket, Key=key)
    report.record(
        "delete-is-accepted",
        _status(deleted) in {200, 204},
        f"HTTP {_status(deleted)}",
    )
    code, status = _refusal(lambda: client.get_object(Bucket=bucket, Key=key))
    report.record(
        "absent-key-answers-no-such-key",
        code == NO_SUCH_KEY and status == 404,
        f"{code or '(accepted)'} HTTP {status}",
    )

    return report


def checks(client: Any, bucket: str, prefix: str) -> Iterator[Check]:
    """The report's checks, for a caller that wants to assert on each one separately."""
    yield from run_contract(client, bucket, prefix).checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True, help="an existing bucket to write into")
    parser.add_argument("--prefix", default="careloop-contract")
    parser.add_argument("--endpoint-url", default=None, help="point at a substitute instead of AWS")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--out",
        default=None,
        help="also write the observations as JSON, for CareLoopBench to ingest verbatim",
    )
    arguments = parser.parse_args(argv)

    import boto3

    client = boto3.session.Session().client(
        "s3", region_name=arguments.region, endpoint_url=arguments.endpoint_url
    )
    report = run_contract(client, arguments.bucket, arguments.prefix)
    for check in report.checks:
        print(check)
    print(f"\n{len(report.checks) - len(report.failures)}/{len(report.checks)} satisfied")
    if arguments.out:
        # Observations only: what each check is called, whether it held, and what the
        # provider actually answered. No bucket, no key, no prefix and no endpoint --
        # a record of a measurement is not a place to publish where it was taken.
        pathlib.Path(arguments.out).write_text(
            json.dumps(
                {
                    "checks": [
                        {"name": c.name, "satisfied": c.satisfied, "observed": c.observed}
                        for c in report.checks
                    ],
                    "satisfied": report.satisfied,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {arguments.out}")
    return 0 if report.satisfied else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
