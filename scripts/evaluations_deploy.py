"""Put the settlement evaluator into AWS, reproducibly, in one command.

    python scripts/evaluations_deploy.py --dry-run     # build and describe the package only
    python scripts/evaluations_deploy.py               # create or update everything

**Why a committed script rather than a one-time spike.** The AgentCore evaluator and its Lambda are
meant to outlive the proof that created them, and a permanent resource nobody can recreate is a
liability rather than an asset. Everything here is idempotent: run it twice and the second run
updates nothing, because it compares before it writes.

**The package is a manifest, never a directory walk.** :data:`PACKAGE_MODULES` names the five
modules the Lambda gets, and ``tests/test_evaluations_package.py`` fails if the directory holds a
sixth. A walk would ship whatever happened to be lying in ``evaluators/`` -- a ``__pycache__``, a
scratch file, an experiment -- and the whole claim of this evaluator is that it contains nothing
but itself. That is the same reasoning that keeps ``careloop`` out of it: an evaluator importing
the application would be checking CareLoop against itself.

**The bytes are deterministic.** Every entry is written with a fixed timestamp and a fixed mode, so
an unchanged tree produces a byte-identical zip and a byte-identical SHA-256. That is what lets
this script decide "the deployed code is already this code" from a digest instead of from a guess,
and what lets a proof document name the artefact that ran.

**No boto3 at import time.** The packaging half is standard library only and is what the tests
exercise; the clients are built inside :func:`deploy`. So a test about what goes into the zip
cannot accidentally become a test that needs credentials.

**What this deliberately does not do.** It creates no online evaluation configuration, no AgentCore
service role and no Lambda resource-based policy -- the on-demand path measured in
``docs/spike-findings.md`` section 9, A3, needs none of the three, and it checks that the last of
them is still absent rather than assuming it. It also never creates the role or the log group:
both are IAM-shaped decisions made once, by hand, by whoever holds that authority.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Final

REGION: Final[str] = "us-east-1"

FUNCTION_NAME: Final[str] = "careloop-settlement-evaluator"
ROLE_NAME: Final[str] = "careloop-settlement-evaluator-role"
LOG_GROUP: Final[str] = "/aws/lambda/careloop-settlement-evaluator"

#: ``[a-zA-Z][a-zA-Z0-9_]{0,47}`` -- the service model's pattern. No hyphens, and the service
#: appends its own ten-character suffix to form the evaluator id and its ARN.
EVALUATOR_NAME: Final[str] = "CareLoopSettlementIntegrity"
EVALUATOR_DESCRIPTION: Final[str] = (
    "Deterministic settlement-integrity check over one CareLoop production trace"
)
EVALUATION_LEVEL: Final[str] = "TRACE"

HANDLER: Final[str] = "evaluators.handler.lambda_handler"
RUNTIME: Final[str] = "python3.12"
ARCHITECTURE: Final[str] = "x86_64"
MEMORY_MB: Final[int] = 128
TIMEOUT_SECONDS: Final[int] = 30
RETENTION_DAYS: Final[int] = 14

#: The package directory, and the prefix every entry in the zip carries.
PACKAGE: Final[str] = "evaluators"

#: Exactly what the Lambda gets. Five modules, no dependencies, nothing else.
PACKAGE_MODULES: Final[tuple[str, ...]] = (
    "__init__.py",
    "contract.py",
    "handler.py",
    "settlement.py",
    "spans.py",
)

#: The zip epoch, so the bytes depend on the sources and on nothing else.
#:
#: Stated rather than inherited, and the distinction is measured: ``ZipInfo(filename)`` already
#: *defaults* to this value, so naming it changes nothing today. What it guards against is the
#: implementation somebody reaches for instead -- ``ZipInfo.from_file``, which takes the file's
#: mtime and makes two checkouts of one commit produce different artefacts.
#: ``tests/test_evaluations_package.py`` fails on exactly that mutant and not on the no-op one.
ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)

#: ``0o644`` in the high half of ``external_attr``, plus the regular-file bit. Lambda needs to read
#: the modules; nothing needs to execute or write them.
ZIP_FILE_MODE: Final[int] = (0o100644 & 0xFFFF) << 16


class DeploymentRefused(Exception):
    """A precondition this script checks rather than assumes did not hold."""


def package_manifest() -> tuple[str, ...]:
    """Every path the zip will contain, in the order it will contain them."""
    return tuple(f"{PACKAGE}/{module}" for module in PACKAGE_MODULES)


def evaluator_sources(root: Path) -> tuple[str, ...]:
    """Every Python module actually present in ``evaluators/``, as manifest-shaped paths.

    The counterpart of :func:`package_manifest`, and what lets the packaging test check both
    directions: a module added to the package but not to the manifest would ship a Lambda missing
    an import, and a manifest entry with no file fails at build time. ``__pycache__`` is excluded
    by looking only at the directory's own ``*.py`` children.
    """
    directory = root / PACKAGE
    return tuple(sorted(f"{PACKAGE}/{found.name}" for found in directory.glob("*.py")))


def build_package(root: Path) -> bytes:
    """The Lambda deployment package, byte-identical for identical sources.

    Raises:
        DeploymentRefused: if a manifest entry names a file that is not there.
    """
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in package_manifest():
            source = root / entry
            if not source.is_file():
                raise DeploymentRefused(f"the package manifest names {entry}, which does not exist")
            info = zipfile.ZipInfo(filename=entry, date_time=ZIP_EPOCH)
            info.external_attr = ZIP_FILE_MODE
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes())
    return buffer.getvalue()


def package_digest(payload: bytes) -> str:
    """The SHA-256 of a deployment package, lowercase hex. What a proof document names."""
    return hashlib.sha256(payload).hexdigest()


def code_sha256(payload: bytes) -> str:
    """The digest Lambda itself reports for a package: base64 of the SHA-256, not hex."""
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _clients() -> tuple[Any, Any, Any, Any]:
    """The four clients, built here so importing this module needs no boto3 and no credentials."""
    import boto3

    session = boto3.session.Session()
    return (
        session.client("sts", region_name=REGION),
        session.client("lambda", region_name=REGION),
        session.client("logs", region_name=REGION),
        session.client("bedrock-agentcore-control", region_name=REGION),
    )


def ensure_function(lam: Any, role_arn: str, payload: bytes) -> dict[str, Any]:
    """Create the evaluator function, or update its code when the bytes differ.

    Returns the function's configuration as the service reports it afterwards.
    """
    from botocore.exceptions import ClientError

    try:
        existing = lam.get_function(FunctionName=FUNCTION_NAME)
    except ClientError as refusal:
        if refusal.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        print(f"   creating {FUNCTION_NAME}")
        lam.create_function(
            FunctionName=FUNCTION_NAME,
            Runtime=RUNTIME,
            Role=role_arn,
            Handler=HANDLER,
            Code={"ZipFile": payload},
            Description=EVALUATOR_DESCRIPTION,
            Timeout=TIMEOUT_SECONDS,
            MemorySize=MEMORY_MB,
            Architectures=[ARCHITECTURE],
            PackageType="Zip",
            Publish=False,
        )
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)
        return lam.get_function_configuration(FunctionName=FUNCTION_NAME)

    if existing["Configuration"].get("CodeSha256") == code_sha256(payload):
        print(f"   {FUNCTION_NAME} already carries these bytes")
    else:
        print(f"   updating {FUNCTION_NAME} code")
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=payload, Publish=False)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    return lam.get_function_configuration(FunctionName=FUNCTION_NAME)


def assert_configuration(configuration: dict[str, Any]) -> None:
    """Refuse a function whose shape is not the one this evaluator was measured with."""
    expected = {
        "Runtime": RUNTIME,
        "Handler": HANDLER,
        "MemorySize": MEMORY_MB,
        "Timeout": TIMEOUT_SECONDS,
    }
    wrong = {
        key: configuration.get(key)
        for key, want in expected.items()
        if configuration.get(key) != want
    }
    if wrong:
        raise DeploymentRefused(f"{FUNCTION_NAME} has unexpected configuration: {wrong}")
    if configuration.get("Architectures") != [ARCHITECTURE]:
        raise DeploymentRefused(f"{FUNCTION_NAME} is not {ARCHITECTURE}")
    if configuration.get("Environment"):
        raise DeploymentRefused(
            f"{FUNCTION_NAME} carries environment variables; assess() reads none, and a "
            f"deterministic function of the envelope is the whole property"
        )


def assert_log_retention(logs: Any) -> int:
    """Refuse to proceed while the function's log group would keep records forever."""
    found = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP).get("logGroups", [])
    exact = [group for group in found if group["logGroupName"] == LOG_GROUP]
    if not exact:
        raise DeploymentRefused(
            f"log group {LOG_GROUP} does not exist; it is created once, by hand, with retention"
        )
    retention = exact[0].get("retentionInDays")
    if retention != RETENTION_DAYS:
        raise DeploymentRefused(
            f"log group {LOG_GROUP} has retention {retention!r}, expected {RETENTION_DAYS}"
        )
    return int(retention)


def assert_no_resource_policy(lam: Any) -> None:
    """Record that the on-demand path still needs no resource-based policy on the function.

    Patch A measured this twice -- before ``CreateEvaluator`` and after two successful
    invocations -- and the negative is the part the design rests on. If one has appeared, that is
    a changed AWS contract and a reason to stop, not a reason to add ``lambda:AddPermission``.
    """
    from botocore.exceptions import ClientError

    try:
        lam.get_policy(FunctionName=FUNCTION_NAME)
    except ClientError as refusal:
        if refusal.response["Error"]["Code"] == "ResourceNotFoundException":
            print("   no resource-based policy (as measured in Patch A, A3)")
            return
        raise
    raise DeploymentRefused(
        f"{FUNCTION_NAME} now carries a resource-based policy. Patch A measured none at any "
        f"point; this is a changed contract and needs a decision, not a wider grant."
    )


def _all_evaluators(control: Any) -> list[dict[str, Any]]:
    """Every evaluator in the account, following the token."""
    found: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = control.list_evaluators(nextToken=token) if token else control.list_evaluators()
        found.extend(page.get("evaluatorSummaries") or page.get("evaluators") or [])
        token = page.get("nextToken")
        if not token:
            return found


def ensure_evaluator(control: Any, function_arn: str) -> dict[str, Any]:
    """Create the AgentCore evaluator, or return the one already carrying this name."""
    for evaluator in _all_evaluators(control):
        if evaluator.get("evaluatorName") != EVALUATOR_NAME:
            continue
        described = control.get_evaluator(evaluatorId=evaluator["evaluatorId"])
        print(f"   evaluator {described['evaluatorId']} already exists")
        return described
    print(f"   creating evaluator {EVALUATOR_NAME}")
    created = control.create_evaluator(
        evaluatorName=EVALUATOR_NAME,
        description=EVALUATOR_DESCRIPTION,
        level=EVALUATION_LEVEL,
        evaluatorConfig={
            "codeBased": {
                "lambdaConfig": {
                    "lambdaArn": function_arn,
                    "lambdaTimeoutInSeconds": TIMEOUT_SECONDS,
                }
            }
        },
    )
    return control.get_evaluator(evaluatorId=created["evaluatorId"])


def deploy(root: Path) -> dict[str, Any]:
    """Everything, in dependency order, with each precondition asked rather than assumed."""
    payload = build_package(root)
    print(f"package      {len(payload)} bytes, sha256 {package_digest(payload)}")

    sts, lam, logs, control = _clients()
    identity = sts.get_caller_identity()
    print(f"caller       {identity['Arn']}")

    role_arn = f"arn:aws:iam::{identity['Account']}:role/{ROLE_NAME}"
    configuration = ensure_function(lam, role_arn, payload)
    assert_configuration(configuration)
    retention = assert_log_retention(logs)
    assert_no_resource_policy(lam)

    function_arn = configuration["FunctionArn"]
    evaluator = ensure_evaluator(control, function_arn)

    print()
    print(f"function     {function_arn}")
    print(
        f"runtime      {configuration['Runtime']} {ARCHITECTURE} "
        f"{configuration['MemorySize']}MB {configuration['Timeout']}s"
    )
    print(f"handler      {configuration['Handler']}")
    print(f"log group    {LOG_GROUP} ({retention}-day retention)")
    print(f"evaluator    {evaluator['evaluatorId']}")
    print(f"level        {evaluator.get('level')}   status {evaluator.get('status')}")
    return {
        "package_sha256": package_digest(payload),
        "function_arn": function_arn,
        "evaluator_id": evaluator["evaluatorId"],
        "evaluator_arn": evaluator.get("evaluatorArn"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the CareLoop settlement evaluator.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the package and describe it; touch no AWS resource",
    )
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parent.parent

    if arguments.dry_run:
        payload = build_package(root)
        print(f"package      {len(payload)} bytes, sha256 {package_digest(payload)}")
        for entry in package_manifest():
            print(f"  {entry}")
        return 0

    try:
        deploy(root)
    except DeploymentRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
