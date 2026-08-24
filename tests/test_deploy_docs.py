"""The deployment documentation, held to the deployment.

A stale operational document is worse than an absent one: it is confidently wrong at the moment
somebody is under pressure. Three of these were live in this repository at once -- a README telling
a maintainer to run a script that had been deleted, an IAM policy naming a key prefix nothing
compared against the constant that produces it, and a variable that was documented as configuration
while the code read no environment for it at all.

So the claims are compared against the thing that makes them true, not against a constant written
down beside them:

- the deploy command is asserted to *resolve*, against the module that implements it
- the key prefix in the IAM policy and the lifecycle rule is compared against
  :data:`careloop.tools.objectstore.DEFAULT_PREFIX`
- the demo-state boundary in the prose is compared against
  :data:`scripts.deploy.DEMO_STATE_BOUNDARY`
- "SSM is canonical, port 22 is closed" is asserted as an *absence*, over a named set of operational
  documents, with the set itself checked so the sweep cannot pass by having nothing left to read

Whether any of these documents carries something credential-shaped is **not** asked here.
``tests/test_nothing_secret_is_tracked.py`` sweeps every tracked text file for that, by shape and by
the exact live values, at zero false positives; a second, weaker detector over a subset would be the
one somebody eventually trusts.

Historical records are deliberately out of scope. ``docs/session-log.md`` and
``docs/agentcore-evaluations-proof.md`` record what was done and when -- including a temporary SSH
ingress rule that was opened and removed -- and rewriting a log to match today would be the opposite
of what a log is for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from careloop.tools.objectstore import DEFAULT_PREFIX
from scripts.deploy import DEMO_STATE_BOUNDARY

ROOT = Path(__file__).resolve().parents[1]

DEPLOY_README = ROOT / "deploy" / "README.md"
DEPLOY_ENV = ROOT / "deploy" / "careloop.env"
OBJECTSTORE = ROOT / "docs" / "deployment-objectstore.md"

#: What a reader could reasonably take as instructions for operating this deployment today.
#: A named list rather than a glob, so adding an operational document is a decision.
OPERATIONAL: tuple[Path, ...] = (
    DEPLOY_README,
    DEPLOY_ENV,
    OBJECTSTORE,
    ROOT / "deploy" / "bootstrap.sh",
    ROOT / "deploy" / "careloop-api.service",
    ROOT / "deploy" / "careloop-demoworld.service",
    ROOT / "docs" / "judges.md",
    ROOT / "README.md",
    ROOT / ".github" / "workflows" / "demo-health.yml",
)

#: Reaching the instance over SSH, in every spelling an operational document would use. ``ssh`` on
#: its own is not one of them: the point is a *transport being prescribed*, and a sentence saying
#: SSH is no longer needed has to stay sayable.
SSH_TRANSPORT: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bssh\s+ec2-user@"),
    re.compile(r"\bscp\s"),
    re.compile(r"StrictHostKeyChecking"),
    re.compile(r"\.keys/[\w.-]*\.pem"),
    re.compile(r"push\.sh\s+<ssh-target>"),
    re.compile(r"open\w*\s+port\s+22", re.IGNORECASE),
)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestEveryOperationalDocumentExists:
    """Vacuity. Every sweep below is satisfied by an empty set of files."""

    @pytest.mark.parametrize("path", OPERATIONAL, ids=lambda p: p.name)
    def test_it_is_there(self, path: Path) -> None:
        assert path.exists(), (
            f"{path} is named as an operational document and is not there. Point this list at "
            "whatever replaced it, or remove it in the same commit; do not leave a sweep that "
            "passes because there is nothing left to read."
        )

    def test_there_are_enough_of_them_to_be_worth_sweeping(self) -> None:
        assert len(OPERATIONAL) >= 8


class TestSsmIsTheDeployPathAndSshIsNotMentionedAsOne:
    """ "Port 22 stays closed" is a claim about what no operational document tells anybody to do."""

    @pytest.mark.parametrize("path", OPERATIONAL, ids=lambda p: p.name)
    def test_no_ssh_transport_is_prescribed(self, path: Path) -> None:
        body = text(path)
        for rule in SSH_TRANSPORT:
            found = rule.search(body)
            assert found is None, (
                f"{path.name} still prescribes an SSH deploy: {found.group(0)!r}. SSM is the "
                "deployment path and port 22 is closed."
            )

    def test_the_sweep_would_catch_one(self) -> None:
        """The other half: these patterns really do match the shapes they are written for."""
        planted = (
            "run ssh ec2-user@ec2-1-2-3-4.compute-1.amazonaws.com",
            "scp careloop.tar.gz host:/tmp",
            "SSH_OPTS=(-o StrictHostKeyChecking=accept-new)",
            "deploy/push.sh <ssh-target>",
            "you must open port 22 to your address",
            "-i .keys/careloop-deploy.pem",
        )
        for sentence in planted:
            assert any(rule.search(sentence) for rule in SSH_TRANSPORT), sentence

    def test_the_readme_says_ssm_is_how_it_is_done(self) -> None:
        body = text(DEPLOY_README)
        assert "python scripts/deploy.py" in body
        assert "Systems Manager" in body
        assert "port 22 is closed" in body

    def test_the_readme_does_not_still_point_at_the_deleted_script(self) -> None:
        assert not (ROOT / "deploy" / "push.sh").exists()
        assert "deploy/push.sh <" not in text(DEPLOY_README)


class TestTheDocumentedCommandResolves:
    """A command in a README is a promise. This is the promise being kept."""

    def test_the_deploy_module_is_where_the_readme_says(self) -> None:
        assert (ROOT / "scripts" / "deploy.py").exists()
        assert (ROOT / "scripts" / "deploy_artifact.py").exists()

    @pytest.mark.parametrize(
        "named",
        [
            "careloop.env",
            "careloop-api.service",
            "careloop-demoworld.service",
            "bootstrap.sh",
            "../scripts/deploy.py",
            "../scripts/deploy_artifact.py",
            "../scripts/verify_deploy.py",
            "../scripts/observability.py",
        ],
    )
    def test_every_file_the_readme_tabulates_is_there(self, named: str) -> None:
        assert f"| `{named}` |" in text(DEPLOY_README), f"the Files table no longer lists {named}"
        assert (DEPLOY_README.parent / named).exists(), f"{named} is tabulated and is not there"

    def test_every_documented_flag_is_a_real_flag(self) -> None:
        """Against the parser, never against a second list of names.

        The obvious version of this -- calling ``main([flag, "--help"])`` and expecting a
        ``SystemExit`` -- passes for a *made-up* flag too, because argparse exits either way. It
        was written that way first and proved nothing.
        """
        from scripts.deploy import accepted_flags

        # Scoped to the line that lists the deploy's own flags. The page also mentions
        # `--isolate-sessions`, which belongs to the unit's ExecStart and not to this tool.
        listed = re.search(r"Useful flags:(.+?)\n\n", text(DEPLOY_README), re.DOTALL)
        assert listed, "deploy/README.md no longer lists the deploy's flags"
        documented = set(re.findall(r"`(--[a-z-]+)`", listed.group(1)))
        assert documented, "the README no longer lists any flags, so this check reads nothing"
        assert documented <= accepted_flags(), (
            f"deploy/README.md names {sorted(documented - accepted_flags())}, which "
            "scripts/deploy.py does not accept."
        )


class TestTheUploadConfigurationSaysWhatTheCodeDoes:
    """The reconciliation. A prefix is a constant here and an IAM resource there."""

    def test_the_bucket_variable_is_documented_where_it_is_set(self) -> None:
        body = text(DEPLOY_ENV)
        assert "CARELOOP_UPLOAD_BUCKET=" in body
        assert "scripts/deploy.py" in body, (
            "careloop.env leaves CARELOOP_UPLOAD_BUCKET empty and the deploy is what carries the "
            "deployed value forward. A reader who does not know that reads the empty line as "
            "'uploads are off' and would be wrong about a deployed host."
        )

    def test_the_bucket_variable_is_documented_in_the_deployment_note(self) -> None:
        assert "CARELOOP_UPLOAD_BUCKET" in text(OBJECTSTORE)

    def test_no_document_invents_an_upload_prefix_variable(self) -> None:
        """There is no ``CARELOOP_UPLOAD_PREFIX``; the docs may only say so, never rely on one."""
        for path in OPERATIONAL:
            body = text(path)
            for line in body.splitlines():
                if "CARELOOP_UPLOAD_PREFIX" in line:
                    assert "no CARELOOP_UPLOAD_PREFIX" in line or "There is no" in line, (
                        f"{path.name} names CARELOOP_UPLOAD_PREFIX as if it were configuration: "
                        f"{line.strip()!r}. Nothing reads that variable."
                    )

    def test_nothing_in_the_package_reads_such_a_variable(self) -> None:
        """The claim above is only worth making because the code really does not read one."""
        for source in (ROOT / "careloop").rglob("*.py"):
            assert "CARELOOP_UPLOAD_PREFIX" not in source.read_text(encoding="utf-8")
        assert "CARELOOP_UPLOAD_PREFIX" not in (ROOT / "scripts" / "demo_serve.py").read_text(
            encoding="utf-8"
        )

    def test_the_iam_policy_names_the_prefix_the_code_produces(self) -> None:
        body = text(OBJECTSTORE)
        resources = re.findall(r'"Resource": "arn:aws:s3:::[^"]+/([^/"]+)/\*"', body)
        assert resources, "the deployment note no longer carries an S3 resource ARN to check"
        for prefix in resources:
            assert prefix == DEFAULT_PREFIX, (
                f"the IAM policy scopes uploads to {prefix!r} and the code writes under "
                f"{DEFAULT_PREFIX!r}. One of the two is wrong and the deployment would fail closed "
                "on every upload."
            )

    def test_the_list_condition_names_it_too(self) -> None:
        body = text(OBJECTSTORE)
        conditions = re.findall(r'"s3:prefix": \["([^/"]+)/\*"\]', body)
        assert conditions
        assert set(conditions) == {DEFAULT_PREFIX}

    def test_the_lifecycle_rule_names_it_too(self) -> None:
        assert f"expire `{DEFAULT_PREFIX}/*`" in text(OBJECTSTORE)

    def test_the_prose_states_the_actual_default(self) -> None:
        assert f"`{DEFAULT_PREFIX}`" in text(OBJECTSTORE)
        assert "DEFAULT_PREFIX" in text(OBJECTSTORE)


class TestTheRolloutBoundaryIsDocumentedAsItBehaves:
    """The state reset, and the honest limit of what a rollback puts back."""

    def test_the_boundary_the_deploy_carries_is_the_one_the_readme_names(self) -> None:
        assert DEMO_STATE_BOUNDARY in text(DEPLOY_README)

    def test_the_marker_is_named_so_an_operator_can_look_at_it(self) -> None:
        assert "/opt/careloop/state/.boundary" in text(DEPLOY_README)

    def test_the_readme_says_a_reset_is_not_rolled_back(self) -> None:
        body = text(DEPLOY_README)
        assert "not undone" in body
        assert "no migration" in body

    def test_the_readme_says_what_a_rollback_does_restore(self) -> None:
        assert "app.prev" in text(DEPLOY_README)


class TestTheRequiredAwsResourcesAreStated:
    """What has to exist before the one command works, and what the command will not create."""

    @pytest.mark.parametrize(
        "requirement",
        [
            "AmazonSSMManagedInstanceCore",
            "careloop-deploy-artifacts-<account>-us-east-1",
            "Block Public Access",
            "s3:GetBucketPublicAccessBlock",
            "ssm:SendCommand",
        ],
    )
    def test_the_readme_names_it(self, requirement: str) -> None:
        assert requirement in text(DEPLOY_README)

    def test_the_artefact_bucket_default_matches_the_code(self) -> None:
        from scripts.deploy import ARTIFACT_BUCKET_TEMPLATE

        assert ARTIFACT_BUCKET_TEMPLATE.format(account="<account>", region="us-east-1") in text(
            DEPLOY_README
        )

    def test_the_two_buckets_are_distinguished(self) -> None:
        """One holds documents, one holds deploy artefacts. Conflating them is an IAM mistake."""
        assert "not the same bucket" in text(OBJECTSTORE)
