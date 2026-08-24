"""Deploy CareLoop to the instance, in one command, without opening a port.

    python scripts/deploy.py --dry-run      # build, check, and print every remote script
    python scripts/deploy.py                # the whole thing

**No SSH, and that is the point rather than a convenience.** ``deploy/push.sh`` needed an inbound
path to port 22 scoped to whoever happened to be deploying, so a deploy from a new address was a
security-group edit made under time pressure -- and a standing rule for a home IP is a standing
rule for whoever holds that IP next. Systems Manager inverts it: the instance's own agent opens an
outbound session to AWS, the artefact travels through a private S3 bucket, and authority is IAM
rather than a private key sitting in ``.keys/`` on somebody's laptop. **Port 22 stays closed**, and
nothing here will open it.

The order below is the deployment, and each line of it is a thing that can fail on its own::

    verify HEAD -> build the frontend -> pack the artefact -> checksum and scan
      -> upload to private S3 -> preflight on the instance -> snapshot for rollback
      -> preserve the deployed careloop.env -> inject CARELOOP_UPLOAD_BUCKET
      -> reset synthetic demo state when the boundary requires it -> swap the tree
      -> bootstrap.sh -> service health -> verify through the CDN -> remove the transfer artefact

**Rollback is automatic and is scoped to what a rollback can actually undo.** A failing
``bootstrap.sh`` or a failing health check restores the snapshot taken before the swap, restores the
``careloop.env`` that was deployed with it, and bootstraps again. It does **not** undo a demo-state
reset: deleting a synthetic store is not reversible, and pretending otherwise would be worse than
saying so. A failure in ``verify_deploy.py`` -- which runs through CloudFront, after the origin has
already answered -- is reported and not rolled back, because the origin being healthy is exactly the
evidence that a rollback would not address what is wrong.

**Nothing secret is sent and nothing secret is printed.** The remote scripts carry a bucket name, an
object key and a SHA-256, all of which are already public facts about this deployment. The deployed
``careloop.env`` is copied, merged and moved on the instance and is never read back into a command
result -- the merge reports how many values it carried forward and never which, or what they were.
The instance reaches Bedrock and S3 through its instance profile exactly as before; no key is
created, transported or stored by any part of this.

**No AWS infrastructure is created.** The instance, the distribution, the buckets, the roles and the
units all exist already and this deploy asserts their shape rather than reconciling it. A missing
bucket, an unmanaged instance or a bucket that is not private is a refusal with a sentence saying
which, not something to fix on the way past.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# `python scripts/deploy.py` puts *this directory* on sys.path and never the repository root, so
# `scripts.deploy_artifact` does not resolve without help. That is the same defect
# `careloop-api.service` documents for `scripts/demo_serve.py`, where the fix is a PYTHONPATH the
# unit declares; an operator running a deploy has no unit to declare one, so it is done here.
if __package__ in (None, ""):  # pragma: no cover - only when run as a file rather than imported
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.deploy_artifact import Artifact, ArtifactRefused, build

REGION: Final[str] = "us-east-1"

#: The demo instance. Already public -- ``docs/judges.md`` names it in the teardown command -- and
#: overridable, because the identifier of a host is not a secret and hiding it would only mean
#: somebody pasting it into a shell instead.
DEFAULT_INSTANCE_ID: Final[str] = "i-0aeb7e7150cd7524b"

#: Where the artefact is staged between here and the instance. Private, and asserted to be so
#: before anything is written to it.
ARTIFACT_BUCKET_TEMPLATE: Final[str] = "careloop-deploy-artifacts-{account}-{region}"
ARTIFACT_KEY_PREFIX: Final[str] = "deploy"

APP: Final[str] = "/opt/careloop/app"
STATE: Final[str] = "/opt/careloop/state"
TRANSFER: Final[str] = "/opt/careloop/transfer"
SNAPSHOT: Final[str] = "/opt/careloop/app.prev"
STAGING: Final[str] = "/opt/careloop/app.new"
SUPERSEDED: Final[str] = "/opt/careloop/app.old"
FAILED_TREE: Final[str] = "/opt/careloop/app.failed"
PRESERVED_ENV: Final[str] = "/opt/careloop/careloop.env.deployed"

#: Written the instant the tree is swapped and removed when the deploy is finished or rolled back.
#: The rollback reads *this* rather than the driver's guess about how far the release got, because
#: SSM truncates a command result at 24,000 characters and a marker on disk does not.
SWAP_MARKER: Final[str] = "/opt/careloop/.swap-complete"

#: The demo-state boundary the deployed store must belong to.
#:
#: ``CLAUDE.md``'s rollout boundary, made operational. Narrowing ``book_appointment``'s identity to
#: its identifying arguments changed the idempotency key of every historical booking, and there is
#: deliberately no migration: a ``DONE`` case booked before the boundary and reopened after it
#: misses duplicate suppression and sends a real second ``POST``. The marker file on the instance
#: records which boundary the state on disk belongs to, so "when necessary" is a comparison rather
#: than an operator remembering. Changing this constant is what makes the next boundary reset
#: happen, once, on the next deploy.
DEMO_STATE_BOUNDARY: Final[str] = "2026-08-24-booking-identity"
BOUNDARY_MARKER: Final[str] = f"{STATE}/.boundary"

#: The two origins, checked on the instance itself. The CDN half is ``scripts/verify_deploy.py``.
API_HEALTH: Final[str] = "http://127.0.0.1:8000/api/health"
CLINIC_HEALTH: Final[str] = "http://127.0.0.1:8100/clinic/slots"

#: Free space on ``/opt`` the preflight insists on. Two copies of an 3 MB tree plus a Python
#: interpreter and its wheels; the number is a floor rather than a measurement.
MINIMUM_FREE_KB: Final[int] = 524288

#: What the instance re-checks the artefact's member list against before unpacking it. A third
#: enforcement point after the exclusion rules and the local scan, and the only one that reads the
#: bytes that actually arrived.
REMOTE_FORBIDDEN: Final[str] = (
    r"(^|/)(\.env($|\.)|\.keys/|\.venv|venv/|node_modules/|.*\.pem$|.*\.(key|p12|pfx)$|.*\.sqlite3)"
)

#: Every value this module interpolates into a remote script. Bucket names, object keys and hex
#: digests only -- no spaces, no quotes, no shell metacharacters. A refusal rather than a quoting
#: scheme: there is no legitimate deployment in which one of these carries a semicolon.
SAFE_VALUE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._/@:-]{1,512}\Z")

#: How long the driver waits for each remote step.
PREFLIGHT_TIMEOUT: Final[int] = 300
RELEASE_TIMEOUT: Final[int] = 900
ROLLBACK_TIMEOUT: Final[int] = 900
CLEANUP_TIMEOUT: Final[int] = 120

#: How often the driver asks SSM whether a command has finished.
POLL_SECONDS: Final[float] = 3.0

#: The preamble every remote script opens with. ``set -euo pipefail`` so a step that fails stops
#: rather than carrying on into a swap, and a ``die`` that puts the reason on stderr -- where the
#: driver prints it under the step's name -- rather than leaving an operator to infer it from an
#: exit code SSM reports as ``Failed``.
DIE: Final[str] = 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }'

#: SSM's own terminal states. Anything else means the command is still going.
TERMINAL: Final[frozenset[str]] = frozenset(
    {"Success", "Cancelled", "Failed", "TimedOut", "Cancelling"}
)


class DeploymentRefused(Exception):
    """A precondition this script checks rather than assumes did not hold."""


@dataclass(frozen=True)
class RemoteStep:
    """One thing to run on the instance, named so a failure says which."""

    name: str
    script: str
    timeout: int


@dataclass(frozen=True)
class Invocation:
    """What one remote step produced."""

    name: str
    status: str
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.status == "Success"


@dataclass
class Report:
    """The concise account printed at the end, assembled as the deploy goes rather than after."""

    commit: str = ""
    sha256: str = ""
    size: int = 0
    members: int = 0
    instance: str = ""
    bucket: str = ""
    key: str = ""
    steps: list[Invocation] = field(default_factory=list)
    rolled_back: bool = False
    verified: str = "not run"
    notes: list[str] = field(default_factory=list)


def safe(value: str, *, what: str) -> str:
    """One interpolated value, refused rather than quoted if it is not the shape it must be."""
    if not SAFE_VALUE.match(value):
        raise DeploymentRefused(
            f"{what} is not a plain identifier: {value!r}. Nothing this deploy interpolates into a "
            "remote script may carry shell metacharacters."
        )
    return value


# --------------------------------------------------------------------------------------------
# The remote scripts. Pure functions of their arguments, so what runs on the instance can be read,
# diffed and asserted on from a machine holding no credentials.
# --------------------------------------------------------------------------------------------


def preflight_script(*, bucket: str, key: str, sha256: str, region: str = REGION) -> str:
    """Fetch the artefact onto the instance and refuse it if it is not the one that was built.

    The digest check is the whole reason the artefact is deterministic. The member scan beside it is
    the instance declining to unpack something carrying a credential, which is a claim the host gets
    to make about the bytes it received rather than one this laptop makes about the bytes it sent.
    """
    bucket, key, sha256 = (
        safe(bucket, what="the artefact bucket"),
        safe(key, what="the artefact key"),
        safe(sha256, what="the artefact digest"),
    )
    return f"""{DIE}
echo "careloop-deploy: preflight"

test -d /opt/careloop || die "no /opt/careloop: this host has never been bootstrapped"
command -v aws >/dev/null || die "no aws CLI: the artefact cannot be fetched"
command -v systemctl >/dev/null || die "no systemctl on the instance"
command -v sha256sum >/dev/null || die "no sha256sum on the instance"

free_kb=$(df -Pk /opt | awk 'NR==2 {{print $4}}')
if [ "$free_kb" -lt {MINIMUM_FREE_KB} ]; then
    die "only $free_kb KB free on /opt; a swap plus a snapshot needs {MINIMUM_FREE_KB} KB"
fi
echo "disk: $free_kb KB free on /opt"

mkdir -p {TRANSFER}
rm -f {TRANSFER}/careloop.tar.gz {TRANSFER}/manifest.txt
aws s3 cp "s3://{bucket}/{key}" {TRANSFER}/careloop.tar.gz --region {region} --only-show-errors

echo "{sha256}  {TRANSFER}/careloop.tar.gz" | sha256sum -c -

tar -tzf {TRANSFER}/careloop.tar.gz > {TRANSFER}/manifest.txt
for required in ./deploy/bootstrap.sh ./deploy/careloop.env ./frontend/dist/index.html; do
    grep -qxF "$required" {TRANSFER}/manifest.txt || die "the artefact has no $required"
done

if grep -Ei '{REMOTE_FORBIDDEN}' {TRANSFER}/manifest.txt; then
    die "refusing to unpack: the artefact contains something it must not"
fi

echo "artefact: $(wc -l < {TRANSFER}/manifest.txt) member(s), digest verified"
echo "careloop-deploy: preflight ok"
"""


def release_script(
    *,
    upload_bucket: str = "",
    boundary: str = DEMO_STATE_BOUNDARY,
    force_state_reset: bool = False,
) -> str:
    """Snapshot, stage, merge configuration, reset state if the boundary says so, swap, bootstrap.

    One command rather than seven, because the half of these that have run must never be a question
    the driver has to answer from a truncated command result. It ends with the health checks, so its
    exit code is exactly the condition the rollback is armed on.
    """
    if upload_bucket:
        upload_bucket = safe(upload_bucket, what="the upload bucket")
    boundary = safe(boundary, what="the demo-state boundary")
    forced = "1" if force_state_reset else "0"

    return f"""{DIE}
echo "careloop-deploy: release"

# --- rollback snapshot ----------------------------------------------------------------------
rm -rf {SNAPSHOT}
if [ -d {APP} ]; then
    cp -a {APP} {SNAPSHOT}
    echo "snapshot: taken"
else
    echo "snapshot: no previous tree to snapshot"
fi

# --- preserve the deployed configuration ------------------------------------------------------
if [ -f {APP}/deploy/careloop.env ]; then
    cp -a {APP}/deploy/careloop.env {PRESERVED_ENV}
    echo "careloop.env: preserved"
else
    echo "careloop.env: none deployed yet"
fi

# --- stage the new tree beside the old one ----------------------------------------------------
rm -rf {STAGING}
mkdir -p {STAGING}
tar -xzf {TRANSFER}/careloop.tar.gz -C {STAGING}
test -f {STAGING}/deploy/bootstrap.sh
test -f {STAGING}/deploy/careloop.env
test -f {STAGING}/frontend/dist/index.html
echo "staged: {STAGING}"

# --- carry forward the values the shipped file leaves for an operator to set ------------------
# The committed careloop.env is the source of truth for model ids, URLs and every telemetry
# setting, so a config change in the repository deploys. What it deliberately leaves blank is what
# an operator set on the box; those are carried across. Values are counted, never printed.
if [ -f {PRESERVED_ENV} ]; then
    before=$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=.+' {STAGING}/deploy/careloop.env || true)
    awk -F= '
        NR==FNR {{
            if ($0 ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {{
                k=$1; v=substr($0, index($0, "=") + 1); if (v != "") deployed[k]=v
            }}
            next
        }}
        {{
            if ($0 ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {{
                k=$1; v=substr($0, index($0, "=") + 1)
                if (v == "" && (k in deployed)) {{ print k "=" deployed[k]; next }}
            }}
            print
        }}
    ' {PRESERVED_ENV} {STAGING}/deploy/careloop.env > {TRANSFER}/careloop.env.merged
    mv {TRANSFER}/careloop.env.merged {STAGING}/deploy/careloop.env
    after=$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=.+' {STAGING}/deploy/careloop.env || true)
    echo "careloop.env: carried forward $((after - before)) operator value(s)"
fi

# --- inject CARELOOP_UPLOAD_BUCKET ------------------------------------------------------------
UPLOAD_BUCKET='{upload_bucket}'
if [ -n "$UPLOAD_BUCKET" ]; then
    if grep -q '^CARELOOP_UPLOAD_BUCKET=' {STAGING}/deploy/careloop.env; then
        awk -v b="$UPLOAD_BUCKET" '
            {{
                if ($0 ~ /^CARELOOP_UPLOAD_BUCKET=/) print "CARELOOP_UPLOAD_BUCKET=" b
                else print
            }}
        ' {STAGING}/deploy/careloop.env > {TRANSFER}/careloop.env.bucket
        mv {TRANSFER}/careloop.env.bucket {STAGING}/deploy/careloop.env
    else
        printf 'CARELOOP_UPLOAD_BUCKET=%s\\n' "$UPLOAD_BUCKET" >> {STAGING}/deploy/careloop.env
    fi
    echo "upload bucket: set from the deploy"
elif grep -q '^CARELOOP_UPLOAD_BUCKET=.\\+' {STAGING}/deploy/careloop.env; then
    echo "upload bucket: kept as deployed"
else
    echo "upload bucket: unset, so every upload stays simulated"
fi

# --- the synthetic demo-state boundary --------------------------------------------------------
# Narrowing a booking's identity changed the idempotency key of every historical booking and there
# is no migration. A store predating the boundary is cleared once, here, rather than being
# discovered later by a reopened case sending a real second POST.
mkdir -p {STATE}
at_boundary=0
if [ -f {BOUNDARY_MARKER} ] && [ "$(cat {BOUNDARY_MARKER})" = "{boundary}" ]; then at_boundary=1; fi
if [ "{forced}" = "1" ] || [ "$at_boundary" = "0" ]; then
    systemctl stop careloop-api >/dev/null 2>&1 || true
    rm -f {STATE}/*.sqlite3 {STATE}/*.sqlite3-wal {STATE}/*.sqlite3-shm
    printf '%s\\n' "{boundary}" > {BOUNDARY_MARKER}
    echo "demo state: reset to boundary {boundary}"
else
    echo "demo state: already at boundary {boundary}"
fi

# --- the swap -----------------------------------------------------------------------------------
# Two renames on one filesystem, after the new tree is fully extracted and checked. /opt/careloop/
# state is a sibling of the tree being replaced, so nothing durable is inside what moves.
rm -rf {SUPERSEDED}
if [ -d {APP} ]; then mv {APP} {SUPERSEDED}; fi
mv {STAGING} {APP}
touch {SWAP_MARKER}
rm -rf {SUPERSEDED}
echo "swap: {APP} is the new tree"

# --- bootstrap ------------------------------------------------------------------------------
bash {APP}/deploy/bootstrap.sh

# --- health, on the instance ------------------------------------------------------------------
systemctl is-active --quiet careloop-demoworld || die "careloop-demoworld is not active"
systemctl is-active --quiet careloop-api || die "careloop-api is not active"
curl -fsS {API_HEALTH} >/dev/null || die "{API_HEALTH} did not answer"
curl -fsS {CLINIC_HEALTH} >/dev/null || die "{CLINIC_HEALTH} did not answer"
echo "health: both units active and both origins answering"

echo "careloop-deploy: release ok"
"""


def rollback_script() -> str:
    """Put back the tree and the configuration that were there before the swap.

    Reads :data:`SWAP_MARKER` rather than being told how far the release got. A release that failed
    while staging left the running tree untouched, and re-bootstrapping it is the whole of what is
    owed; a release that failed after the swap gets the snapshot back.

    **The configuration comes back with the tree rather than beside it.** :data:`SNAPSHOT` is a full
    copy of what was deployed, ``deploy/careloop.env`` included, so restoring it restores both. A
    second ``cp`` from :data:`PRESERVED_ENV` was written here first and then removed: it could not
    fail independently of the tree restore, so it was a line no test could distinguish from its own
    absence. :data:`PRESERVED_ENV` is what the *forward* path merges from, and that is load-bearing.

    **A demo-state reset is not undone**, because it cannot be. The store is synthetic and this is
    said out loud rather than papered over.
    """
    return f"""{DIE}
echo "careloop-deploy: rollback"

if [ -f {SWAP_MARKER} ]; then
    test -d {SNAPSHOT} || die "the tree was swapped and there is no snapshot to restore"
    rm -rf {FAILED_TREE}
    if [ -d {APP} ]; then mv {APP} {FAILED_TREE}; fi
    cp -a {SNAPSHOT} {APP}
    rm -f {SWAP_MARKER}
    echo "rollback: the previous tree and its careloop.env are back in place"
else
    echo "rollback: the swap never happened, so the running tree was never replaced"
fi

rm -rf {STAGING}
test -d {APP} || die "there is no {APP} to bootstrap"
bash {APP}/deploy/bootstrap.sh

systemctl is-active --quiet careloop-demoworld || die "careloop-demoworld is not active"
systemctl is-active --quiet careloop-api || die "careloop-api is not active"
curl -fsS {API_HEALTH} >/dev/null || die "{API_HEALTH} did not answer after rollback"
curl -fsS {CLINIC_HEALTH} >/dev/null || die "{CLINIC_HEALTH} did not answer after rollback"

echo "careloop-deploy: rollback ok"
"""


def cleanup_script() -> str:
    """Remove the transfer artefact and every staging tree. Nothing durable is in reach of this."""
    return f"""{DIE}
rm -f {TRANSFER}/careloop.tar.gz {TRANSFER}/manifest.txt
rm -f {TRANSFER}/careloop.env.merged {TRANSFER}/careloop.env.bucket
rm -rf {STAGING} {SUPERSEDED} {FAILED_TREE}
rm -f {SWAP_MARKER}
echo "careloop-deploy: transfer artefact removed"
"""


def steps(
    *,
    bucket: str,
    key: str,
    sha256: str,
    region: str,
    upload_bucket: str,
    force_state_reset: bool,
) -> dict[str, RemoteStep]:
    """Every remote step this deploy can run, by name. What ``--dry-run`` prints."""
    return {
        "preflight": RemoteStep(
            "preflight",
            preflight_script(bucket=bucket, key=key, sha256=sha256, region=region),
            PREFLIGHT_TIMEOUT,
        ),
        "release": RemoteStep(
            "release",
            release_script(upload_bucket=upload_bucket, force_state_reset=force_state_reset),
            RELEASE_TIMEOUT,
        ),
        "rollback": RemoteStep("rollback", rollback_script(), ROLLBACK_TIMEOUT),
        "cleanup": RemoteStep("cleanup", cleanup_script(), CLEANUP_TIMEOUT),
    }


# --------------------------------------------------------------------------------------------
# The AWS half. Every client is built inside a function, so importing this module -- which is what
# the tests do -- needs no credentials and reaches nothing.
# --------------------------------------------------------------------------------------------


def artifact_key(artifact: Artifact) -> str:
    """Where the artefact is staged: named after the commit and digest, so it describes itself."""
    return f"{ARTIFACT_KEY_PREFIX}/{artifact.commit}-{artifact.sha256[:12]}.tar.gz"


def assert_bucket_is_private(s3: Any, bucket: str) -> None:
    """Refuse to stage a deployment through a bucket that is not closed to the public.

    Fails closed on a denial as well as on a negative answer: a principal that cannot read the
    public-access configuration has not established that the bucket is private, and "could not
    check" is not "checked".
    """
    try:
        configuration = s3.get_public_access_block(Bucket=bucket)
    except Exception as refused:  # botocore raises a generated per-service type
        raise DeploymentRefused(
            f"could not establish that s3://{bucket} blocks public access ({refused}). Grant the "
            "deploying principal s3:GetBucketPublicAccessBlock, or point --artifact-bucket at a "
            "bucket it can read that configuration for. A bucket whose exposure is unknown is not "
            "a bucket to stage a deployment through."
        ) from refused

    blocked = configuration.get("PublicAccessBlockConfiguration", {})
    missing = [
        setting
        for setting in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
        if not blocked.get(setting)
    ]
    if missing:
        raise DeploymentRefused(
            f"s3://{bucket} does not block public access ({', '.join(missing)} are off). The "
            "deploy artefact is the whole application; it does not go in a bucket anybody can read."
        )


def assert_instance_is_managed(ssm: Any, instance_id: str) -> None:
    """Refuse before uploading anything if the instance is not reachable over SSM."""
    described = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    found = described.get("InstanceInformationList", [])
    if not found:
        raise DeploymentRefused(
            f"{instance_id} is not a managed instance. SSM needs the agent running on the host and "
            "an instance profile carrying AmazonSSMManagedInstanceCore. This deploy creates "
            "neither; port 22 is not the fallback."
        )
    ping = found[0].get("PingStatus")
    if ping != "Online":
        raise DeploymentRefused(f"{instance_id} is registered with SSM but its agent is {ping}.")


def run_remote(ssm: Any, instance_id: str, step: RemoteStep, *, comment: str) -> Invocation:
    """Send one step and wait for it. Returns what happened; raises only when SSM itself fails."""
    sent = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment=comment[:100],
        TimeoutSeconds=step.timeout,
        Parameters={"commands": step.script.splitlines()},
    )
    command_id = sent["Command"]["CommandId"]

    deadline = time.monotonic() + step.timeout + 60
    while True:
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception:  # InvocationDoesNotExist, briefly, right after send
            result = {"Status": "Pending", "StandardOutputContent": "", "StandardErrorContent": ""}
        status = result.get("Status", "Pending")
        if status in TERMINAL:
            return Invocation(
                step.name,
                status,
                result.get("StandardOutputContent", ""),
                result.get("StandardErrorContent", ""),
            )
        if time.monotonic() > deadline:
            return Invocation(step.name, "TimedOut", "", "the driver stopped waiting")
        time.sleep(POLL_SECONDS)


def echo(invocation: Invocation) -> None:
    """Print a step's output under its name. SSM truncates at 24,000 characters."""
    for line in invocation.stdout.splitlines():
        print(f"   {line}")
    for line in invocation.stderr.splitlines():
        print(f"   ! {line}", file=sys.stderr)


def deploy(
    root: Path,
    *,
    instance_id: str,
    region: str,
    artifact_bucket: str,
    upload_bucket: str,
    force_state_reset: bool,
    allow_dirty: bool,
    skip_build: bool,
    skip_verify: bool,
    demo_url: str,
) -> Report:
    """The whole deployment. Raises rather than leaving the instance half-done."""
    import boto3

    report = Report(instance=instance_id)

    if not skip_build:
        print("== building the frontend")
        build_frontend(root)

    print("== packing the artefact")
    scratch = root / ".deploy"
    artifact = build(root, scratch / "careloop.tar.gz", allow_dirty=allow_dirty)
    report.commit, report.sha256 = artifact.commit, artifact.sha256
    report.size, report.members = artifact.size, len(artifact.members)
    print(f"   commit {artifact.commit}")
    print(f"   sha256 {artifact.sha256}")
    print(f"   {artifact.size / 1024:.0f} KB in {len(artifact.members)} member(s)")

    # **The local tarball is removed however this ends**, which the inner ``finally`` below did not
    # cover. Everything between packing and the release can refuse -- an artefact bucket that does
    # not exist, one whose exposure cannot be established, an unmanaged instance, a denied
    # ``PutObject`` -- and every one of those used to leave a HEAD-named archive sitting in
    # ``.deploy/``. Measured on the first real SSM deploy, 2026-08-24: the bucket did not exist,
    # the deploy refused before reaching AWS state exactly as designed, and 2.9 MB stayed behind.
    #
    # ``dry_run`` has always unlinked its own artefact, so this was an asymmetry rather than a
    # policy: a refused deploy left more on disk than a rehearsal did. The scratch file is
    # gitignored and each run overwrites it, so nothing was ever *wrong* -- what it costs is a stale
    # archive named after a commit, which is exactly the kind of object somebody later mistakes for
    # a current one.
    #
    # The S3 object stays in the inner ``finally`` and must: it exists only after ``put_object``,
    # and asking for its deletion before it was written would report a failure that never happened.
    try:
        session = boto3.session.Session(region_name=region)
        s3, ssm, sts = session.client("s3"), session.client("ssm"), session.client("sts")

        bucket = artifact_bucket or ARTIFACT_BUCKET_TEMPLATE.format(
            account=sts.get_caller_identity()["Account"], region=region
        )
        key = artifact_key(artifact)
        report.bucket, report.key = bucket, key

        print(f"== checking s3://{bucket}")
        assert_bucket_is_private(s3, bucket)

        print(f"== checking {instance_id} is managed by SSM")
        assert_instance_is_managed(ssm, instance_id)

        print(f"== uploading s3://{bucket}/{key}")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=artifact.path.read_bytes(),
            ServerSideEncryption="AES256",
            ContentType="application/gzip",
        )

        plan = steps(
            bucket=bucket,
            key=key,
            sha256=artifact.sha256,
            region=region,
            upload_bucket=upload_bucket,
            force_state_reset=force_state_reset,
        )

        try:
            release_over_ssm(ssm, instance_id, plan, report, tag=artifact.commit[:8])
        finally:
            remove_staged_artifact(s3, bucket, key, report)

        if skip_verify:
            report.verified = "skipped"
            report.notes.append("the deploy is unverified through the distribution")
        else:
            print("== verifying through the distribution")
            report.verified = verify_through_the_cdn(demo_url, report)
    finally:
        artifact.path.unlink(missing_ok=True)

    return report


def release_over_ssm(
    ssm: Any,
    instance_id: str,
    plan: dict[str, RemoteStep],
    report: Report,
    *,
    tag: str = "",
) -> None:
    """Preflight, release, roll back if the release failed, and always clean up.

    Separated from :func:`deploy` so the *decision* about when a rollback happens is one function
    with one caller and one test. A test that re-implemented this shape against a stubbed client
    would be asserting on its own copy of the logic, which is the thing this repository refuses
    everywhere else.
    """
    try:
        print("== preflight")
        preflight = run_remote(
            ssm, instance_id, plan["preflight"], comment=f"careloop {tag} preflight"
        )
        report.steps.append(preflight)
        echo(preflight)
        if not preflight.ok:
            raise DeploymentRefused(
                f"the preflight did not succeed ({preflight.status}). Nothing was swapped and "
                "nothing was restarted."
            )

        print("== release")
        release = run_remote(ssm, instance_id, plan["release"], comment=f"careloop {tag} release")
        report.steps.append(release)
        echo(release)
        if not release.ok:
            print("== rolling back")
            rollback = run_remote(ssm, instance_id, plan["rollback"], comment="careloop rollback")
            report.steps.append(rollback)
            report.rolled_back = True
            echo(rollback)
            if not rollback.ok:
                raise DeploymentRefused(
                    "the release failed AND the rollback failed. The instance needs a person: "
                    f"the previous tree is at {SNAPSHOT} and the failed one at {FAILED_TREE}."
                )
            raise DeploymentRefused(
                f"the release did not succeed ({release.status}); the previous version is back and "
                "answering. A demo-state reset, if one happened, is not undone."
            )
    finally:
        cleanup = run_remote(ssm, instance_id, plan["cleanup"], comment="careloop cleanup")
        report.steps.append(cleanup)


def build_frontend(root: Path) -> None:
    """Build the bundle here, so Node never goes on the instance.

    The artefact that gets served is then the bundle that was tested, and a host that cannot build
    one cannot accidentally serve a different one. That reasoning is ``deploy/push.sh``'s and it did
    not change when the transport did.
    """
    import subprocess

    finished = subprocess.run(
        ["npm", "run", "build"],
        cwd=root / "frontend",
        shell=sys.platform == "win32",
    )
    if finished.returncode != 0:
        raise DeploymentRefused("the frontend build failed; nothing was sent")
    if not (root / "frontend" / "dist" / "index.html").exists():
        raise DeploymentRefused("the frontend build produced no frontend/dist/index.html")


def remove_staged_artifact(s3: Any, bucket: str, key: str, report: Report) -> None:
    """Delete the transfer object. A failure here is noted rather than raised over a live deploy."""
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except Exception as refused:  # a lifecycle rule is the backstop for this
        report.notes.append(f"s3://{bucket}/{key} could not be removed ({refused})")


def verify_through_the_cdn(demo_url: str, report: Report) -> str:
    """``scripts/verify_deploy.py``, called rather than shelled out to.

    Through the distribution and never the origin: the release step already asked ``127.0.0.1``, and
    a check against the origin passes in exactly the situations this exists to catch.
    """
    from scripts.verify_deploy import DEFAULT_BASE, Failed, verify

    base = (demo_url or DEFAULT_BASE).rstrip("/")
    try:
        verify(base)
    except Failed as broken:
        report.notes.append(f"deploy verification failed: {broken}")
        return "failed"
    except OSError as unreachable:
        report.notes.append(f"{base} could not be reached: {unreachable}")
        return "unreachable"
    return "passed"


def print_report(report: Report) -> None:
    """The concise account. Names, digests and outcomes; no values out of any configuration file."""
    print("")
    print("== deployment report")
    print(f"   commit        {report.commit}")
    print(f"   artefact      {report.sha256}")
    print(f"   size          {report.size / 1024:.0f} KB in {report.members} member(s)")
    print(f"   instance      {report.instance}")
    print(f"   staged via    s3://{report.bucket}/{report.key}")
    for step in report.steps:
        print(f"   {step.name:<13} {step.status}")
    print(f"   rolled back   {'yes' if report.rolled_back else 'no'}")
    print(f"   cdn verify    {report.verified}")
    for note in report.notes:
        print(f"   note          {note}")


def exit_code(report: Report) -> int:
    """Non-zero unless the deployment was verified, or verification was deliberately skipped.

    A release that reached the instance and then failed through the distribution is a *failed*
    deploy: the origin answering is not the property, and exiting zero on it would make a CDN
    misconfiguration invisible to anything reading the exit status.
    """
    return 0 if report.verified in {"passed", "skipped"} else 1


def build_parser() -> argparse.ArgumentParser:
    """Every flag this deploy accepts.

    Separate from :func:`main` so ``deploy/README.md``'s flag list can be checked against the
    parser rather than against a second list of names -- ``tests/test_deploy_docs.py`` does that,
    and a README naming a flag that was renamed is exactly the kind of stale claim this session
    was about.
    """
    parser = argparse.ArgumentParser(
        description="Deploy CareLoop over SSM. No SSH, and port 22 stays closed."
    )
    parser.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
    parser.add_argument("--region", default=REGION)
    parser.add_argument(
        "--artifact-bucket",
        default="",
        help=f"private bucket the artefact is staged through; default {ARTIFACT_BUCKET_TEMPLATE}",
    )
    parser.add_argument(
        "--upload-bucket",
        default="",
        help=(
            "value for CARELOOP_UPLOAD_BUCKET. Omitted, whatever the instance already has is kept, "
            "which is why an ordinary deploy does not have to know it."
        ),
    )
    parser.add_argument(
        "--force-state-reset",
        action="store_true",
        help="clear the synthetic demo store even when it already belongs to the current boundary",
    )
    parser.add_argument("--allow-dirty", action="store_true", help="pack a tree that is not HEAD")
    parser.add_argument("--no-build", action="store_true", help="use frontend/dist as it stands")
    parser.add_argument("--no-verify", action="store_true", help="skip the check through the CDN")
    parser.add_argument("--demo-url", default="", help="the distribution to verify against")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and check locally, print every remote script, and reach no AWS service",
    )
    return parser


def accepted_flags() -> frozenset[str]:
    """The option strings :func:`build_parser` really accepts, for the README check."""
    return frozenset(
        option
        for action in build_parser()._get_optional_actions()
        for option in action.option_strings
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    root = Path(__file__).resolve().parents[1]

    try:
        if arguments.dry_run:
            return dry_run(root, arguments)
        report = deploy(
            root,
            instance_id=arguments.instance_id,
            region=arguments.region,
            artifact_bucket=arguments.artifact_bucket,
            upload_bucket=arguments.upload_bucket,
            force_state_reset=arguments.force_state_reset,
            allow_dirty=arguments.allow_dirty,
            skip_build=arguments.no_build,
            skip_verify=arguments.no_verify,
            demo_url=arguments.demo_url,
        )
    except (DeploymentRefused, ArtifactRefused) as refused:
        print(f"\ndeploy: {refused}\n", file=sys.stderr)
        return 1

    print_report(report)
    return exit_code(report)


def dry_run(root: Path, arguments: argparse.Namespace) -> int:
    """Everything that does not need a credential, and the exact text of everything that does."""
    artifact = build(root, root / ".deploy" / "careloop.tar.gz", allow_dirty=arguments.allow_dirty)
    print(f"commit  {artifact.commit}")
    print(f"sha256  {artifact.sha256}")
    print(f"size    {artifact.size / 1024:.0f} KB in {len(artifact.members)} member(s)")

    bucket = arguments.artifact_bucket or ARTIFACT_BUCKET_TEMPLATE.format(
        account="<account>", region=arguments.region
    )
    plan = steps(
        bucket=bucket if arguments.artifact_bucket else "careloop-deploy-artifacts",
        key=artifact_key(artifact),
        sha256=artifact.sha256,
        region=arguments.region,
        upload_bucket=arguments.upload_bucket,
        force_state_reset=arguments.force_state_reset,
    )
    for step in plan.values():
        print(f"\n----- {step.name} (timeout {step.timeout}s) -----")
        print(step.script)

    artifact.path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
