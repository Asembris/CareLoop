"""The one-command deploy: what it sends, in what order, and what it does when a step fails.

Two halves, and they answer different questions.

**What the remote scripts say.** Pure functions of their arguments, so the text that would run on
the instance is readable from a machine holding no credentials. The order is asserted by position
rather than by presence: a release that preserved ``careloop.env`` *after* swapping the tree would
contain every line this file looks for and would still have thrown the operator's configuration
away. The transport is asserted too -- no ``ssh``, no ``scp``, no identity file, no port 22 --
because "SSM is canonical" is a claim about what is absent.

**What the release script does.** The scripts are run for real, in a sandboxed ``/opt/careloop``
with stub units, over a real artefact tarball. That is the half that catches an ``awk`` program
which parses and merges the wrong way round, a boundary comparison that resets every time, and a
rollback that restores a tree but not the configuration that belonged to it. Skipped where there is
no POSIX shell; never faked.

Nothing here reaches AWS. :func:`~scripts.deploy.deploy` builds its clients inside the function, so
importing this module costs nothing and asks for no credential.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import deploy
from scripts.deploy import (
    DEMO_STATE_BOUNDARY,
    DeploymentRefused,
    assert_bucket_is_private,
    assert_instance_is_managed,
    cleanup_script,
    preflight_script,
    release_over_ssm,
    release_script,
    rollback_script,
    safe,
    steps,
)

DIGEST = "a" * 64

#: The one invocation of bootstrap.sh that runs it, as opposed to checking it is there.
BOOTSTRAP = "bash /opt/careloop/app/deploy/bootstrap.sh"

BASH = shutil.which("bash")
needs_a_shell = pytest.mark.skipif(BASH is None, reason="no POSIX shell to run the remote scripts")


def plan() -> dict[str, deploy.RemoteStep]:
    return steps(
        bucket="careloop-deploy-artifacts-000000000000-us-east-1",
        key="deploy/abc1234-0123456789ab.tar.gz",
        sha256=DIGEST,
        region="us-east-1",
        upload_bucket="",
        force_state_reset=False,
    )


def at(script: str, needle: str) -> int:
    """Where a line appears, so ordering can be asserted rather than only membership."""
    found = script.find(needle)
    assert found >= 0, f"the script never does {needle!r}"
    return found


class TestEveryRemoteScriptFailsClosed:
    """A step that carries on past a failure is a step that swaps a tree it should not have."""

    @pytest.mark.parametrize("name", ["preflight", "release", "rollback", "cleanup"])
    def test_it_stops_on_the_first_error(self, name: str) -> None:
        assert plan()[name].script.startswith("set -euo pipefail")

    @needs_a_shell
    @pytest.mark.parametrize("name", ["preflight", "release", "rollback", "cleanup"])
    def test_it_is_valid_shell(self, name: str, tmp_path: Path) -> None:
        written = tmp_path / f"{name}.sh"
        written.write_text(plan()[name].script, encoding="utf-8", newline="\n")
        subprocess.run([str(BASH), "-n", str(written)], check=True, capture_output=True)


class TestTheTransportIsSsmAndNothingElse:
    """Port 22 stays closed, which is a property of what these scripts do not contain."""

    @pytest.mark.parametrize(
        "forbidden",
        ["ssh ", "scp ", "ssh-keyscan", "StrictHostKeyChecking", "IdentityFile", "-p 22", ":22"],
    )
    def test_no_script_reaches_for_ssh(self, forbidden: str) -> None:
        for name, step in plan().items():
            assert forbidden not in step.script, f"the {name} script names {forbidden!r}"

    def test_the_driver_itself_names_no_ssh_transport(self) -> None:
        source = Path(deploy.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess.run(['ssh'", '"scp"', "paramiko", "fabric"):
            assert forbidden not in source


class TestThePreflightRefusesRatherThanUnpacks:
    """The instance checks the bytes that arrived. Nothing above it can make that check for it."""

    def test_the_digest_is_checked_against_the_artefact_that_was_built(self) -> None:
        script = preflight_script(bucket="b", key="deploy/k.tar.gz", sha256=DIGEST)
        assert f"{DIGEST}  /opt/careloop/transfer/careloop.tar.gz" in script
        assert "sha256sum -c -" in script

    def test_the_member_list_is_scanned_on_the_instance(self) -> None:
        script = preflight_script(bucket="b", key="deploy/k.tar.gz", sha256=DIGEST)
        assert at(script, "sha256sum -c -") < at(script, "tar -tzf")
        assert "refusing to unpack" in script
        for pattern in (r"\.env($|\.)", r"\.keys/", r"\.venv", r".*\.pem$", r".*\.sqlite3"):
            assert pattern in script

    def test_it_nothing_is_extracted(self) -> None:
        """The preflight lists the archive and never unpacks it; the release does that."""
        script = preflight_script(bucket="b", key="deploy/k.tar.gz", sha256=DIGEST)
        assert "tar -tzf" in script
        assert "tar -xzf" not in script

    @pytest.mark.parametrize(
        "hostile",
        ["b; rm -rf /", "b$(whoami)", "b`id`", "b'\nrm -rf /\n'", "b bucket", 'b"'],
    )
    def test_a_hostile_bucket_name_is_refused_rather_than_quoted(self, hostile: str) -> None:
        with pytest.raises(DeploymentRefused, match="plain identifier"):
            preflight_script(bucket=hostile, key="deploy/k.tar.gz", sha256=DIGEST)

    def test_an_ordinary_bucket_name_is_fine(self) -> None:
        assert safe("careloop-deploy-artifacts-000000000000-us-east-1", what="x")


class TestTheReleaseHappensInTheOrderTheFlowStates:
    """Presence is not the property. A step in the wrong place is a step that destroys something."""

    def test_the_snapshot_is_taken_before_anything_moves(self) -> None:
        script = release_script()
        assert at(script, "/opt/careloop/app.prev") < at(script, "tar -xzf")
        assert at(script, "/opt/careloop/app.prev") < at(script, "mv /opt/careloop/app.new")

    def test_the_deployed_env_is_preserved_before_the_swap(self) -> None:
        script = release_script()
        assert at(script, "/opt/careloop/careloop.env.deployed") < at(
            script, "mv /opt/careloop/app.new"
        )

    def test_configuration_is_settled_before_the_tree_is_swapped(self) -> None:
        script = release_script(upload_bucket="careloop-demo-uploads-1-us-east-1")
        assert at(script, "CARELOOP_UPLOAD_BUCKET=") < at(script, "mv /opt/careloop/app.new")

    def test_the_state_reset_happens_before_bootstrap_restarts_anything(self) -> None:
        script = release_script()
        assert at(script, "demo state:") < at(script, BOOTSTRAP)

    def test_bootstrap_runs_after_the_swap_and_health_after_bootstrap(self) -> None:
        script = release_script()
        assert at(script, "mv /opt/careloop/app.new") < at(script, BOOTSTRAP)
        assert at(script, BOOTSTRAP) < at(script, "systemctl is-active")
        assert at(script, "systemctl is-active") < at(script, "curl -fsS")

    def test_both_origins_are_checked(self) -> None:
        script = release_script()
        assert "http://127.0.0.1:8000/api/health" in script
        assert "http://127.0.0.1:8100/clinic/slots" in script


class TestNothingSecretIsSentOrPrinted:
    """The deployed configuration is moved on the instance and never read back into a result."""

    def test_the_env_file_is_never_printed(self) -> None:
        for name, step in plan().items():
            for leak in (
                "cat /opt/careloop/app/deploy/careloop.env",
                "cat " + deploy.PRESERVED_ENV,
            ):
                assert leak not in step.script, f"the {name} script would print the configuration"

    def test_the_merge_reports_a_count_and_not_a_value(self) -> None:
        script = release_script()
        assert "carried forward $((after - before)) operator value(s)" in script

    def test_no_credential_shaped_argument_is_interpolated(self) -> None:
        for step in plan().values():
            for leak in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"):
                assert leak not in step.script


class TestTheRollbackKnowsWhetherItHasAnythingToUndo:
    """It reads the marker on disk, never the driver's guess about how far the release got."""

    def test_it_branches_on_the_swap_marker(self) -> None:
        script = rollback_script()
        assert f"[ -f {deploy.SWAP_MARKER} ]" in script

    def test_it_restores_the_snapshot_which_carries_the_configuration_with_it(self) -> None:
        """One restore, not two. The behavioural half is asserted below, against a real tree."""
        script = rollback_script()
        assert f"cp -a {deploy.SNAPSHOT} {deploy.APP}" in script
        assert f"cp -a {deploy.PRESERVED_ENV}" not in script

    def test_it_bootstraps_and_checks_health_afterwards(self) -> None:
        script = rollback_script()
        assert at(script, BOOTSTRAP) < at(script, "systemctl is-active")
        assert "did not answer after rollback" in script

    def test_it_does_not_pretend_to_undo_a_state_reset(self) -> None:
        assert "sqlite3" not in rollback_script()
        assert deploy.BOUNDARY_MARKER not in rollback_script()


class TestTheCleanupRemovesTheTransferArtefactAndNothingDurable:
    def test_it_removes_the_artefact(self) -> None:
        assert "rm -f /opt/careloop/transfer/careloop.tar.gz" in cleanup_script()

    def test_it_does_not_touch_the_app_or_the_state(self) -> None:
        script = cleanup_script()
        assert f"rm -rf {deploy.APP}\n" not in script
        assert deploy.STATE not in script
        assert deploy.SNAPSHOT not in script


class TestThePreconditionsAreCheckedRatherThanAssumed:
    """Each refusal happens before anything is uploaded, and each says which one it was."""

    def test_a_public_bucket_is_refused(self) -> None:
        stub = _S3(
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": False,
                    "RestrictPublicBuckets": False,
                }
            }
        )
        with pytest.raises(DeploymentRefused, match="does not block public access"):
            assert_bucket_is_private(stub, "b")

    def test_an_unreadable_configuration_is_refused_too(self) -> None:
        with pytest.raises(DeploymentRefused, match="could not establish"):
            assert_bucket_is_private(_S3(RuntimeError("AccessDenied")), "b")

    def test_a_private_bucket_passes(self) -> None:
        assert_bucket_is_private(
            _S3(
                {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    }
                }
            ),
            "b",
        )

    def test_an_unmanaged_instance_is_refused(self) -> None:
        with pytest.raises(DeploymentRefused, match="not a managed instance"):
            assert_instance_is_managed(_Ssm([]), "i-0")

    def test_an_offline_agent_is_refused(self) -> None:
        with pytest.raises(DeploymentRefused, match="ConnectionLost"):
            assert_instance_is_managed(_Ssm([{"PingStatus": "ConnectionLost"}]), "i-0")

    def test_an_online_instance_passes(self) -> None:
        assert_instance_is_managed(_Ssm([{"PingStatus": "Online"}]), "i-0")


class _S3:
    def __init__(self, answer: object) -> None:
        self._answer = answer

    def get_public_access_block(self, **_: object) -> object:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class _Ssm:
    def __init__(self, information: list[dict[str, str]]) -> None:
        self._information = information

    def describe_instance_information(self, **_: object) -> dict[str, object]:
        return {"InstanceInformationList": self._information}


# ---------------------------------------------------------------------------------------------
# The release script, run for real against a sandboxed /opt/careloop.
# ---------------------------------------------------------------------------------------------


def posix(path: Path) -> str:
    """A Windows path as the POSIX shell sees it. ``C:\\x`` becomes ``/c/x``."""
    text = path.resolve().as_posix()
    if len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


class Sandbox:
    """A ``/opt/careloop`` of our own, plus the two commands the scripts call and we do not have."""

    def __init__(self, tmp_path: Path, *, bootstrap_fails: bool = False) -> None:
        self.root = tmp_path / "opt" / "careloop"
        self.app = self.root / "app"
        self.state = self.root / "state"
        self.transfer = self.root / "transfer"
        for directory in (self.app / "deploy", self.state, self.transfer):
            directory.mkdir(parents=True, exist_ok=True)

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for command in ("systemctl", "curl"):
            stub = self.bin / command
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
            stub.chmod(0o755)

        self.bootstrap_fails = bootstrap_fails

    def deployed_env(self, body: str) -> None:
        """The tree already on the instance: its configuration, its bootstrap and a marker."""
        (self.app / "deploy" / "careloop.env").write_text(body, encoding="utf-8", newline="\n")
        (self.app / "deploy" / "bootstrap.sh").write_text(
            "#!/bin/sh\necho previous bootstrap\nexit 0\n", encoding="utf-8", newline="\n"
        )
        (self.app / "MARKER").write_text("the previous tree", encoding="utf-8", newline="\n")

    def artefact(self, *, env_body: str) -> None:
        """A tarball shaped like the real one: units, configuration and a built bundle."""
        staging = self.root.parent / "staging"
        (staging / "deploy").mkdir(parents=True, exist_ok=True)
        (staging / "frontend" / "dist").mkdir(parents=True, exist_ok=True)
        failing = "exit 1\n" if self.bootstrap_fails else "exit 0\n"
        (staging / "deploy" / "bootstrap.sh").write_text(
            f"#!/bin/sh\necho stub bootstrap\n{failing}", encoding="utf-8", newline="\n"
        )
        (staging / "deploy" / "careloop.env").write_text(env_body, encoding="utf-8", newline="\n")
        (staging / "frontend" / "dist" / "index.html").write_text(
            "<!doctype html>", encoding="utf-8"
        )
        (staging / "MARKER").write_text("the new tree", encoding="utf-8", newline="\n")

        with tarfile.open(self.transfer / "careloop.tar.gz", "w:gz") as archive:
            for found in sorted(staging.rglob("*")):
                if found.is_file():
                    archive.add(found, arcname=f"./{found.relative_to(staging).as_posix()}")

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        localised = script.replace("/opt/careloop", posix(self.root))
        written = self.root.parent / "step.sh"
        written.write_text(localised, encoding="utf-8", newline="\n")
        import os

        environment = dict(os.environ)
        environment["PATH"] = f"{posix(self.bin)}:{environment.get('PATH', '')}"
        assert BASH is not None
        return subprocess.run([BASH, str(written)], capture_output=True, text=True, env=environment)


@needs_a_shell
class TestTheReleaseScriptDoesWhatItSays:
    """Run for real. A script that parses is not a script that preserves anybody's configuration."""

    def test_the_tree_is_swapped_and_a_snapshot_is_kept(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("CARELOOP_UPLOAD_BUCKET=\n")
        sandbox.artefact(env_body="CARELOOP_UPLOAD_BUCKET=\n")

        done = sandbox.run(release_script())

        assert done.returncode == 0, done.stdout + done.stderr
        assert (sandbox.app / "MARKER").read_text().strip() == "the new tree"
        assert (sandbox.root / "app.prev" / "MARKER").read_text().strip() == "the previous tree"
        assert (sandbox.root / ".swap-complete").exists()

    def test_an_operator_value_the_repository_leaves_blank_is_carried_forward(
        self, tmp_path: Path
    ) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("CARELOOP_UPLOAD_BUCKET=set-by-an-operator\nAWS_REGION=us-east-1\n")
        sandbox.artefact(env_body="CARELOOP_UPLOAD_BUCKET=\nAWS_REGION=us-east-1\n")

        done = sandbox.run(release_script())

        assert done.returncode == 0, done.stdout + done.stderr
        deployed = (sandbox.app / "deploy" / "careloop.env").read_text()
        assert "CARELOOP_UPLOAD_BUCKET=set-by-an-operator" in deployed
        assert "carried forward 1 operator value(s)" in done.stdout

    def test_a_committed_change_still_deploys_over_the_deployed_one(self, tmp_path: Path) -> None:
        """The repository is the source of truth for everything it fills in."""
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("CARELOOP_MODEL_PLANNER=bedrock:old\n")
        sandbox.artefact(env_body="CARELOOP_MODEL_PLANNER=bedrock:new\n")

        done = sandbox.run(release_script())

        assert done.returncode == 0, done.stdout + done.stderr
        assert "bedrock:new" in (sandbox.app / "deploy" / "careloop.env").read_text()

    def test_an_explicit_upload_bucket_wins(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("CARELOOP_UPLOAD_BUCKET=set-by-an-operator\n")
        sandbox.artefact(env_body="CARELOOP_UPLOAD_BUCKET=\n")

        done = sandbox.run(release_script(upload_bucket="careloop-demo-uploads-1-us-east-1"))

        assert done.returncode == 0, done.stdout + done.stderr
        body = (sandbox.app / "deploy" / "careloop.env").read_text()
        assert "CARELOOP_UPLOAD_BUCKET=careloop-demo-uploads-1-us-east-1" in body
        assert "set-by-an-operator" not in body

    def test_an_absent_key_is_appended_rather_than_dropped(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("AWS_REGION=us-east-1\n")
        sandbox.artefact(env_body="AWS_REGION=us-east-1\n")

        done = sandbox.run(release_script(upload_bucket="careloop-demo-uploads-1-us-east-1"))

        assert done.returncode == 0, done.stdout + done.stderr
        body = (sandbox.app / "deploy" / "careloop.env").read_text()
        assert "CARELOOP_UPLOAD_BUCKET=careloop-demo-uploads-1-us-east-1" in body


@needs_a_shell
class TestTheDemoStateBoundaryResetsExactlyOnce:
    """ "When necessary" is a comparison against a marker, never an operator remembering."""

    def test_a_store_predating_the_boundary_is_cleared(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("X=1\n")
        sandbox.artefact(env_body="X=1\n")
        (sandbox.state / "demo.sqlite3").write_text("old", encoding="utf-8")
        (sandbox.state / "demo.sqlite3-wal").write_text("old", encoding="utf-8")

        done = sandbox.run(release_script())

        assert done.returncode == 0, done.stdout + done.stderr
        assert not (sandbox.state / "demo.sqlite3").exists()
        assert not (sandbox.state / "demo.sqlite3-wal").exists()
        assert (sandbox.state / ".boundary").read_text().strip() == DEMO_STATE_BOUNDARY
        assert f"demo state: reset to boundary {DEMO_STATE_BOUNDARY}" in done.stdout

    def test_a_store_already_at_the_boundary_is_left_alone(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("X=1\n")
        sandbox.artefact(env_body="X=1\n")
        (sandbox.state / ".boundary").write_text(f"{DEMO_STATE_BOUNDARY}\n", encoding="utf-8")
        (sandbox.state / "demo.sqlite3").write_text("live", encoding="utf-8")

        done = sandbox.run(release_script())

        assert done.returncode == 0, done.stdout + done.stderr
        assert (sandbox.state / "demo.sqlite3").read_text() == "live"
        assert "demo state: already at boundary" in done.stdout

    def test_a_forced_reset_clears_it_anyway(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("X=1\n")
        sandbox.artefact(env_body="X=1\n")
        (sandbox.state / ".boundary").write_text(f"{DEMO_STATE_BOUNDARY}\n", encoding="utf-8")
        (sandbox.state / "demo.sqlite3").write_text("live", encoding="utf-8")

        done = sandbox.run(release_script(force_state_reset=True))

        assert done.returncode == 0, done.stdout + done.stderr
        assert not (sandbox.state / "demo.sqlite3").exists()


@needs_a_shell
class TestAFailedBootstrapPutsTheOldVersionBack:
    """The property the whole snapshot exists for, exercised rather than described."""

    def test_the_release_fails_and_the_rollback_restores_both_halves(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path, bootstrap_fails=True)
        sandbox.deployed_env("CARELOOP_UPLOAD_BUCKET=set-by-an-operator\n")
        sandbox.artefact(env_body="CARELOOP_UPLOAD_BUCKET=\n")

        release = sandbox.run(release_script())
        assert release.returncode != 0
        assert (sandbox.app / "MARKER").read_text().strip() == "the new tree"

        rollback = sandbox.run(rollback_script())

        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert (sandbox.app / "MARKER").read_text().strip() == "the previous tree"
        body = (sandbox.app / "deploy" / "careloop.env").read_text()
        assert "CARELOOP_UPLOAD_BUCKET=set-by-an-operator" in body
        assert not (sandbox.root / ".swap-complete").exists()

    def test_a_rollback_with_no_swap_leaves_the_running_tree_alone(self, tmp_path: Path) -> None:
        sandbox = Sandbox(tmp_path)
        sandbox.deployed_env("X=1\n")
        sandbox.artefact(env_body="X=1\n")

        rollback = sandbox.run(rollback_script())

        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert "the swap never happened" in rollback.stdout
        assert (sandbox.app / "MARKER").read_text().strip() == "the previous tree"


class TestTheDriverRollsBackOnlyWhenTheReleaseFails:
    """Which steps run, in which order, for each outcome. No AWS: the clients are stubbed."""

    def test_a_clean_deploy_never_rolls_back(self) -> None:
        ssm = _RecordingSsm({"preflight": "Success", "release": "Success", "cleanup": "Success"})
        assert _drive(ssm) == ["preflight", "release", "cleanup"]

    def test_a_failed_release_rolls_back_and_still_cleans_up(self) -> None:
        ssm = _RecordingSsm(
            {
                "preflight": "Success",
                "release": "Failed",
                "rollback": "Success",
                "cleanup": "Success",
            }
        )
        with pytest.raises(DeploymentRefused, match="the previous version is back"):
            _drive(ssm)
        assert ssm.ran == ["preflight", "release", "rollback", "cleanup"]

    def test_a_failed_preflight_swaps_nothing_and_rolls_back_nothing(self) -> None:
        ssm = _RecordingSsm({"preflight": "Failed", "cleanup": "Success"})
        with pytest.raises(DeploymentRefused, match="Nothing was swapped"):
            _drive(ssm)
        assert ssm.ran == ["preflight", "cleanup"]

    def test_a_failed_rollback_says_a_person_is_needed(self) -> None:
        ssm = _RecordingSsm(
            {
                "preflight": "Success",
                "release": "Failed",
                "rollback": "Failed",
                "cleanup": "Success",
            }
        )
        with pytest.raises(DeploymentRefused, match="needs a person"):
            _drive(ssm)


class _RecordingSsm:
    """Enough of the SSM client to drive the orchestration, and nothing more."""

    def __init__(self, outcomes: dict[str, str]) -> None:
        self.outcomes = outcomes
        self.ran: list[str] = []

    def send_command(self, **request: object) -> dict[str, object]:
        comment = str(request["Comment"])
        name = comment.rsplit(" ", 1)[-1]
        self.ran.append(name)
        return {"Command": {"CommandId": name}}

    def get_command_invocation(self, **request: object) -> dict[str, str]:
        name = str(request["CommandId"])
        return {
            "Status": self.outcomes.get(name, "Failed"),
            "StandardOutputContent": f"{name} output",
            "StandardErrorContent": "",
        }


def _drive(ssm: _RecordingSsm) -> list[str]:
    """The real orchestration, over a stub client. Never a second copy of the same decisions."""
    report = deploy.Report(instance="i-0")
    release_over_ssm(ssm, "i-0", plan(), report)
    return [step.name for step in report.steps]


class TestTheDeployIsOnlyDoneWhenTheDistributionAgrees:
    """``scripts/verify_deploy.py`` is the last step, and its verdict reaches the exit status.

    The release step already asked ``127.0.0.1``. That is liveness, and liveness is not what breaks:
    a stripped session header, a cache policy serving one visitor's cases to another, or a shell
    naming a bundle that was never shipped all leave both origins answering. So the CDN check is
    what the exit code is built from, and a failure there is reported rather than rolled back --
    the origin being healthy is the evidence a rollback would not address it.
    """

    def test_a_passing_verification_exits_zero(self) -> None:
        assert deploy.exit_code(deploy.Report(verified="passed")) == 0

    def test_a_deliberately_skipped_one_exits_zero_too(self) -> None:
        assert deploy.exit_code(deploy.Report(verified="skipped")) == 0

    @pytest.mark.parametrize("outcome", ["failed", "unreachable", "not run"])
    def test_anything_else_exits_non_zero(self, outcome: str) -> None:
        assert deploy.exit_code(deploy.Report(verified=outcome)) == 1

    def test_a_failed_property_is_reported_as_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.verify_deploy as verifier

        def refuse(_: str, expected_commit: str | None = None) -> None:
            raise verifier.Failed("the session header did not reach the origin")

        monkeypatch.setattr(verifier, "verify", refuse)
        report = deploy.Report()
        assert deploy.verify_through_the_cdn("https://example.invalid", report) == "failed"
        assert any("session header" in note for note in report.notes)

    def test_an_unreachable_distribution_is_reported_as_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scripts.verify_deploy as verifier

        def unreachable(_: str, expected_commit: str | None = None) -> None:
            raise OSError("connection refused")

        monkeypatch.setattr(verifier, "verify", unreachable)
        report = deploy.Report()
        assert deploy.verify_through_the_cdn("https://example.invalid", report) == "unreachable"
        assert any("could not be reached" in note for note in report.notes)

    def test_the_distribution_is_the_default_and_the_origin_is_never_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scripts.verify_deploy as verifier

        asked: list[str] = []
        monkeypatch.setattr(
            verifier,
            "verify",
            lambda base, expected_commit=None: asked.append(base),
        )
        assert deploy.verify_through_the_cdn("", deploy.Report()) == "passed"

        assert asked == [verifier.DEFAULT_BASE]
        assert asked[0].startswith("https://")
        assert "127.0.0.1" not in asked[0]

    def test_the_packed_commit_is_what_the_marker_is_checked_against(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The driver hands over the commit it packed, not a guess and not nothing.

        Without this handover, `/release.json` would only be required to be *well-formed*, and a
        deploy that somehow served a previous release would verify clean -- the exact confusion
        the marker exists to make impossible.
        """
        import scripts.verify_deploy as verifier

        seen: dict[str, str | None] = {}

        def record(base: str, expected_commit: str | None = None) -> None:
            seen["expected"] = expected_commit

        monkeypatch.setattr(verifier, "verify", record)
        report = deploy.Report(commit="c" * 40)

        assert deploy.verify_through_the_cdn("", report) == "passed"
        assert seen["expected"] == "c" * 40


# ---------------------------------------------------------------------------------------------
# What the first real SSM deploy left behind.
# ---------------------------------------------------------------------------------------------


class _PrivateS3:
    """A bucket that blocks public access, and a record of what was written and deleted."""

    def __init__(self, upload: Exception | None = None) -> None:
        self._upload = upload
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    def get_public_access_block(self, **_: object) -> dict[str, object]:
        return {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

    def put_object(self, **request: object) -> None:
        if self._upload is not None:
            raise self._upload
        self.uploaded.append(str(request["Key"]))

    def delete_object(self, **request: object) -> None:
        self.deleted.append(str(request["Key"]))


class _Sts:
    def __init__(self, account: str) -> None:
        self._account = account

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self._account}


class _StubSession:
    """Enough of a boto3 session to hand back three stubs."""

    def __init__(self, *, s3: object, ssm: object) -> None:
        self._clients = {"s3": s3, "ssm": ssm, "sts": _Sts("0" * 12)}

    def client(self, name: str) -> object:
        return self._clients[name]


class _ManagedSsm(_RecordingSsm):
    """The orchestration stub, plus the precondition check the driver makes before it."""

    def describe_instance_information(self, **_: object) -> dict[str, object]:
        return {"InstanceInformationList": [{"PingStatus": "Online"}]}


class TestTheLocalArtefactIsRemovedHoweverTheDeployEnds:
    """A refused deploy leaves nothing on disk, and that had to be measured to be noticed.

    **Found on the first real SSM deploy, 2026-08-24.** The artefact bucket did not exist, so
    :func:`~scripts.deploy.assert_bucket_is_private` refused -- correctly, before any upload and
    before any ``SendCommand``, with a sentence naming the bucket. What it also did was leave
    ``.deploy/careloop.tar.gz``, 2.9 MB named after ``HEAD``, sitting in the checkout.

    The cleanup lived in the ``finally`` guarding the SSM release, which is the wrong scope: every
    refusal between packing and that ``try`` -- an absent bucket, one whose exposure cannot be
    established, an unmanaged instance, an offline agent, a denied ``PutObject`` -- skipped it
    entirely. ``dry_run`` has always unlinked its own artefact, so this was an asymmetry rather
    than a decision: a rehearsal tidied up after itself and a real refusal did not.

    Nothing was ever *wrong* -- ``.deploy/`` is gitignored and each run overwrites it. What it costs
    is a stale archive named after a commit, which is the sort of object somebody later mistakes for
    a current one.

    **The S3 object is a different question and stays where it was.** It exists only after
    ``put_object``, so asking for its deletion on a path that never uploaded would report a failure
    that never happened. The cases below walk each refusal point precisely to pin that boundary:
    the local file goes in all of them, and ``delete_object`` is called in none.
    """

    @pytest.fixture
    def artefact(self, tmp_path: Path) -> Path:
        staged = tmp_path / ".deploy" / "careloop.tar.gz"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"a packed artefact")
        return staged

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, staged: Path, s3: object, ssm: object) -> None:
        """The real :func:`~scripts.deploy.deploy`, with the packing and the clients stubbed."""
        import boto3

        from scripts.deploy_artifact import Artifact

        monkeypatch.setattr(
            deploy,
            "build",
            lambda *_a, **_k: Artifact(
                path=staged, commit="c" * 40, sha256=DIGEST, size=17, members=("./x",)
            ),
        )
        monkeypatch.setattr(boto3.session, "Session", lambda **_k: _StubSession(s3=s3, ssm=ssm))
        deploy.deploy(
            staged.parent.parent,
            instance_id="i-0",
            region="us-east-1",
            artifact_bucket="",
            upload_bucket="",
            force_state_reset=False,
            allow_dirty=False,
            skip_build=True,
            skip_verify=True,
            demo_url="",
        )

    def test_it_exists_before_any_of_this(self, artefact: Path) -> None:
        """Vacuity. Every assertion below is about a file that was really there."""
        assert artefact.is_file()

    def test_the_bucket_that_did_not_exist(
        self, monkeypatch: pytest.MonkeyPatch, artefact: Path
    ) -> None:
        """The measured case, reproduced: ``GetPublicAccessBlock`` answering ``NoSuchBucket``."""
        s3 = _S3(RuntimeError("NoSuchBucket"))

        with pytest.raises(DeploymentRefused, match="could not establish"):
            self._run(monkeypatch, artefact, s3, _Ssm([{"PingStatus": "Online"}]))

        assert not artefact.exists(), "a refused deploy left its artefact in the checkout"

    def test_an_unmanaged_instance(self, monkeypatch: pytest.MonkeyPatch, artefact: Path) -> None:
        s3 = _PrivateS3()

        with pytest.raises(DeploymentRefused, match="not a managed instance"):
            self._run(monkeypatch, artefact, s3, _Ssm([]))

        assert not artefact.exists()
        assert s3.deleted == [], "an object that was never uploaded was asked to be deleted"

    def test_a_denied_upload(self, monkeypatch: pytest.MonkeyPatch, artefact: Path) -> None:
        """Not a ``DeploymentRefused`` -- the cleanup may not depend on the exception type."""
        s3 = _PrivateS3(upload=RuntimeError("AccessDenied"))

        with pytest.raises(RuntimeError, match="AccessDenied"):
            self._run(monkeypatch, artefact, s3, _Ssm([{"PingStatus": "Online"}]))

        assert not artefact.exists()
        assert s3.deleted == []

    def test_a_clean_deploy_removes_it_too(
        self, monkeypatch: pytest.MonkeyPatch, artefact: Path
    ) -> None:
        """The happy path is unchanged -- the cleanup moved scope rather than moving out."""
        s3 = _PrivateS3()
        ssm = _ManagedSsm({"preflight": "Success", "release": "Success", "cleanup": "Success"})

        self._run(monkeypatch, artefact, s3, ssm)

        assert not artefact.exists()
        assert len(s3.uploaded) == 1
        assert s3.deleted == s3.uploaded, "the transfer object was not removed after a real upload"
        assert ssm.ran == ["preflight", "release", "cleanup"]
