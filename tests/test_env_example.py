"""`.env.example` is a template, and one documenting the wrong variables is worse than none.

A developer copies this file, fills it in, and believes what it says. So each of its claims is
checked against the thing that makes it true rather than against a constant written down beside it
— the treatment ``tests/test_deploy_docs.py`` already gives the deployment documentation.

**Two things were wrong when this was written**, and both were the same shape: prose about *who
reads this file* that had stopped being true.

The header said *"Only the benchmark harness under `bench/` loads these values."*
``scripts/demo_serve.py::load_env_file`` has read it into the **process environment** since the
demo entry point existed, which is not a footnote — it is the difference between a variable boto3
can see and one it cannot, and therefore the whole answer to whether ``AWS_PROFILE`` does anything
here.

And the file documented ``CARELOOP_CLINIC_URL`` while saying nothing about
``CARELOOP_UPLOAD_BUCKET``, its exact counterpart for the second effect that reaches a real
provider. A template that documents one half of a symmetric pair reads as though the other half
does not exist.

**Three boundaries this file must not blur**, asserted below:

``.env`` / this template
    local benchmark and developer provider configuration.

``deploy/careloop.env``
    the deployed application's configuration, on the instance.

your shell and ``~/.aws``
    deployment authentication. ``scripts/deploy.py`` reads no ``.env`` at all.

Whether this file carries anything credential-shaped is **not** asked here.
``tests/test_nothing_secret_is_tracked.py`` sweeps every tracked file for that, by shape and by the
exact live values; a second, weaker detector over one file would be the one somebody eventually
trusts.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Final

import pytest

from careloop.llm.providers import BEDROCK_MODEL_ID_VAR, ROLE_ENV_VAR

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
TEMPLATE: Final[Path] = ROOT / ".env.example"

#: The entry points that read this file. Named, so adding a third is a decision.
#:
#: They differ in a way the template has to state: ``bench/run.py`` builds a mapping and passes it
#: down, leaving the process environment untouched, while ``scripts/demo_serve.py`` writes into the
#: process environment. Anything resolved by boto3 rather than by CareLoop only works under the
#: second.
LOADERS: Final[tuple[Path, ...]] = (
    ROOT / "bench" / "run.py",
    ROOT / "scripts" / "demo_serve.py",
)

#: Modules whose ``os.environ`` reads are local-developer configuration this template should cover.
#:
#: ``scripts/e2e_serve.py`` is deliberately absent: it reads the same two variables and loads no
#: ``.env`` of its own, so it adds no obligation this list does not already carry.
CONFIGURED: Final[tuple[Path, ...]] = (
    ROOT / "careloop" / "llm" / "providers.py",
    ROOT / "scripts" / "demo_serve.py",
)

#: Variables the code reads that deliberately do **not** belong in this file, and why.
#:
#: ``CARELOOP_LIVE`` is the one that matters. ``bench/live.py`` requires it in the *process*
#: environment as well as credentials in ``.env``, precisely so that having keys configured is not
#: on its own enough to spend money; a blank for it in the template would undo that.
DELIBERATELY_NOT_IN_THE_TEMPLATE: Final[frozenset[str]] = frozenset(
    {
        "CARELOOP_LIVE",
        # Read first and documented as a precedence note rather than as a second blank: two
        # spellings of one setting in a template is a way to set them to different things.
        "AWS_REGION",
    }
)


def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def declared() -> frozenset[str]:
    """Every variable the template offers a value for, blank or not."""
    return frozenset(
        match.group(1)
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=", template(), flags=re.MULTILINE)
    )


def environment_reads(module: Path) -> frozenset[str]:
    """Every environment variable name ``module`` reads, from its own AST.

    ``os.environ["X"]``, ``os.environ.get("X")`` and ``os.getenv("X")``. Derived rather than
    listed, so a script that starts reading a new variable makes this file's coverage claim fail
    instead of quietly becoming incomplete.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ(node.value) and _literal(node.slice):
            names.add(node.slice.value)
        if (
            isinstance(node, ast.Call)
            and node.args
            and _literal(node.args[0])
            and _reads_the_environment(node.func)
        ):
            names.add(node.args[0].value)
    return frozenset(name for name in names if re.fullmatch(r"[A-Z][A-Z0-9_]*", name))


def _literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _reads_the_environment(function: ast.AST) -> bool:
    """``os.environ.get``, ``os.getenv`` or a bare ``getenv``."""
    if isinstance(function, ast.Attribute):
        return (function.attr == "get" and _is_environ(function.value)) or function.attr == "getenv"
    return isinstance(function, ast.Name) and function.id == "getenv"


def _is_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


class TestTheTemplateIsThereAndSaysSomething:
    """Vacuity. Every sweep below is satisfied by an empty file."""

    def test_it_exists(self) -> None:
        assert TEMPLATE.is_file()

    def test_it_declares_enough_to_be_worth_checking(self) -> None:
        assert len(declared()) >= 10


class TestEveryDocumentedVariableIsOneTheCodeReads:
    """No variable is offered that nothing consumes.

    The failure this prevents is specific and has happened in this repository before: a variable
    documented as configuration while the code read no environment for it at all, which
    ``tests/test_deploy_docs.py`` records for the deployment half.
    """

    def test_nothing_documented_is_unread(self) -> None:
        read = frozenset().union(*(environment_reads(m) for m in CONFIGURED))
        # The provider path resolves these through a mapping rather than a literal, so they are
        # taken from the declarations that define them rather than from an AST sweep.
        read |= frozenset(ROLE_ENV_VAR.values()) | {BEDROCK_MODEL_ID_VAR}
        # Credential and region names boto3 consumes. Read by `bedrock_session` out of the mapping.
        read |= {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
            "AWS_REGION",
            "AWS_PROFILE",
            "OPENAI_API_KEY",
            "NVIDIA_API_KEY",
            "NVIDIA_BASE_URL",
        }

        unread = sorted(declared() - read)
        assert unread == [], (
            f"{unread} are offered by .env.example and nothing reads them. Remove them, or point "
            "this test at whatever consumes them now."
        )

    def test_every_model_role_variable_is_offered(self) -> None:
        """Derived from ``ROLE_ENV_VAR``, so a fourth role cannot be silently undocumented."""
        assert frozenset(ROLE_ENV_VAR.values()) <= declared()

    def test_the_bedrock_override_is_offered_under_the_name_the_code_reads(self) -> None:
        assert BEDROCK_MODEL_ID_VAR in declared()


class TestEveryVariableTheLocalPathReadsIsDocumented:
    """The other direction, which is the one that had gone stale.

    ``CARELOOP_UPLOAD_BUCKET`` was read by ``scripts/demo_serve.py`` and named nowhere here, while
    its counterpart ``CARELOOP_CLINIC_URL`` was documented in full — so the template described a
    system with one real external effect.
    """

    def test_nothing_the_developer_entry_points_read_is_missing(self) -> None:
        read = frozenset().union(*(environment_reads(m) for m in CONFIGURED))
        missing = sorted(read - declared() - DELIBERATELY_NOT_IN_THE_TEMPLATE)

        assert missing == [], (
            f"{missing} are read by the local entry points and are not in .env.example. Document "
            "them, or add them to DELIBERATELY_NOT_IN_THE_TEMPLATE with the reason."
        )

    @pytest.mark.parametrize(
        "variable", ["CARELOOP_CLINIC_URL", "CARELOOP_UPLOAD_BUCKET", "CARELOOP_STATIC_DIR"]
    )
    def test_the_demo_entry_point_variables_are_offered(self, variable: str) -> None:
        assert variable in declared()

    def test_both_real_providers_are_represented(self) -> None:
        """One documented effect and one undocumented one is the defect this closes."""
        assert {"CARELOOP_CLINIC_URL", "CARELOOP_UPLOAD_BUCKET"} <= declared()

    def test_the_exclusions_are_real_variables_rather_than_a_way_out(self) -> None:
        """An exclusion list nobody reads is a place to hide an omission."""
        assert "CARELOOP_LIVE" in (ROOT / "bench" / "live.py").read_text(encoding="utf-8")
        assert "AWS_REGION" in (ROOT / "careloop" / "llm" / "providers.py").read_text(
            encoding="utf-8"
        )


class TestTheSweepWouldNotice:
    """Vacuity from the other end: the AST reader has to see what it claims to."""

    def test_it_reads_each_access_form(self, tmp_path: Path) -> None:
        module = tmp_path / "m.py"
        module.write_text(
            "import os\na = os.environ['ONE']\nb = os.environ.get('TWO')\nc = os.getenv('THREE')\n",
            encoding="utf-8",
        )

        assert environment_reads(module) == frozenset({"ONE", "TWO", "THREE"})

    def test_it_really_reads_the_shipped_scripts(self) -> None:
        assert "CARELOOP_UPLOAD_BUCKET" in environment_reads(ROOT / "scripts" / "demo_serve.py")

    def test_a_declared_name_is_recognised(self) -> None:
        assert "OPENAI_API_KEY" in declared()
        assert "NOT_A_VARIABLE_IN_THIS_FILE" not in declared()


class TestTheHeaderDescribesHowTheFileIsActuallyLoaded:
    """The claim that was wrong, pinned to the two loaders that make it true or false."""

    def test_both_loaders_are_named(self) -> None:
        text = template()

        for loader in LOADERS:
            assert loader.relative_to(ROOT).as_posix() in text, (
                f"{loader.name} loads this file and the file does not say so. That is how the "
                "header came to claim only bench/ reads it."
            )

    def test_it_no_longer_claims_bench_is_the_only_reader(self) -> None:
        assert "Only the benchmark harness" not in template()

    def test_the_two_loaders_really_do_differ_the_way_it_says(self) -> None:
        """Not prose about prose: the difference is asserted against the loaders themselves."""
        demo = (ROOT / "scripts" / "demo_serve.py").read_text(encoding="utf-8")
        bench = (ROOT / "bench" / "run.py").read_text(encoding="utf-8")

        assert "os.environ[key] = value" in demo, "demo_serve no longer writes the process env"
        assert "os.environ[" not in bench, "bench/run.py now writes the process env"

    def test_it_states_the_process_environment_distinction(self) -> None:
        text = template().lower()

        assert "process environment" in text
        assert "mapping" in text


class TestTheConfigurationBoundariesAreStated:
    """Three files, three jobs. Blurring them is how a credential ends up in the wrong one."""

    def test_the_deployed_configuration_is_named_as_a_different_file(self) -> None:
        assert "deploy/careloop.env" in template()

    def test_deployment_authentication_is_named_as_the_shell(self) -> None:
        text = template()

        assert "scripts/deploy.py" in text
        assert "reads no `.env`" in text or "reads nothing here" in text

    def test_the_deploy_driver_really_does_read_no_env_file(self) -> None:
        """The claim above, checked against the driver rather than believed."""
        source = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")

        assert "load_env_file" not in source
        assert "dotenv" not in source
        assert environment_reads(ROOT / "scripts" / "deploy.py") == frozenset()

    def test_the_two_buckets_are_not_conflated(self) -> None:
        """The upload bucket is local configuration; the artefact bucket is the deploy's own."""
        text = template()

        assert "CARELOOP_UPLOAD_BUCKET" in text
        assert "artefact bucket" in text
        assert "careloop-deploy-artifacts" not in text


class TestAwsProfileIsOfferedWithItsRealScope:
    """``AWS_PROFILE`` works here, and not everywhere, and the template has to say which.

    ``bedrock_session`` builds an explicit boto3 session **only** when the mapping carries an access
    key and a secret; with neither it returns ``None`` and boto3's own chain takes over — and that
    chain is what reads a profile. So a profile named in this file takes effect under
    ``scripts/demo_serve.py``, which loads into the process environment, and does nothing under
    ``bench/run.py``, which passes a mapping boto3 never sees.

    Both halves are asserted against ``bedrock_session`` itself rather than described, because the
    asymmetry is the entire reason the variable needed careful wording rather than a blank line.
    """

    def test_it_is_offered_blank(self) -> None:
        assert re.search(r"^AWS_PROFILE=\s*$", template(), flags=re.MULTILINE), (
            "AWS_PROFILE must be offered empty. A populated one in a committed template is a "
            "machine-specific setting presented as a default."
        )

    def test_a_profile_alone_defers_to_boto3s_own_chain(self) -> None:
        from careloop.llm.providers import bedrock_session

        assert bedrock_session({"AWS_PROFILE": "some-profile", "AWS_REGION": "us-east-1"}) is None

    def test_an_explicit_key_pair_still_wins(self) -> None:
        """The documented alternative. If this changed, the wording above would be wrong."""
        from careloop.llm.providers import bedrock_session

        session = bedrock_session(
            {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y", "AWS_REGION": "us-east-1"}
        )

        assert session is not None

    def test_it_is_documented_as_an_alternative_rather_than_an_addition(self) -> None:
        assert "alternative to the key pair" in template()

    def test_it_says_the_benchmark_path_does_not_honour_it(self) -> None:
        """The half a reader would otherwise assume, and be wrong about."""
        text = template()

        assert "bench/run.py" in text
        assert "does NOT" in text

    def test_it_refuses_the_deployer_as_a_benchmark_identity(self) -> None:
        text = template()

        assert "careloop-deployer" in text
        assert "is NOT the value to put here" in text

    def test_no_profile_name_is_offered_as_a_default(self) -> None:
        """Naming one would make a machine-specific setting look like configuration."""
        assert "AWS_PROFILE=careloop-deployer" not in template()
        assert not re.search(r"^AWS_PROFILE=\S", template(), flags=re.MULTILINE)


class TestNothingMachineSpecificIsOffered:
    """A template is copied verbatim, so anything host-shaped in it becomes somebody's setting."""

    @pytest.mark.parametrize(
        "variable",
        [
            "AWS_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE",
            "CURL_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        ],
    )
    def test_no_certificate_authority_setting_is_offered(self, variable: str) -> None:
        """TLS trust is a property of the machine, and a wrong one here disables verification."""
        assert variable not in declared()

    def test_no_account_identifier_is_offered(self) -> None:
        assert not re.search(r"\b\d{12}\b", template())

    def test_every_value_offered_is_blank_or_a_public_default(self) -> None:
        """The only non-blank values are a region and two public URLs."""
        populated = {
            match.group(1): match.group(2)
            for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=(.+)$", template(), flags=re.MULTILINE)
        }

        assert set(populated) == {"AWS_DEFAULT_REGION", "NVIDIA_BASE_URL"}
        assert populated["AWS_DEFAULT_REGION"] == "us-east-1"
        assert populated["NVIDIA_BASE_URL"].startswith("https://")


def test_the_ast_reader_runs_on_this_interpreter() -> None:
    """``sys.stdlib_module_names`` is not used here, but the AST forms are 3.12's."""
    assert sys.version_info >= (3, 12)
