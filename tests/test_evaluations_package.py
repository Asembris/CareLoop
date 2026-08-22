"""What the Lambda actually receives, asserted on the artefact rather than on the source tree.

``tests/test_evaluator.py`` sweeps ``evaluators/`` for imports it must not carry. That is a claim
about the repository. This file is the same claim about the **zip**, which is a different object:
the deployment package is what runs in AWS, and a packaging step is exactly where a tree that
passes every source-level guard can still ship something else -- a stale ``.pyc``, a scratch
module, a directory walk that swept up ``careloop/`` because somebody moved a file.

Three properties, and they are not variations of one:

- **The manifest is exact, and it is checked in both directions.** Five modules, matching the
  directory. A sixth module added to ``evaluators/`` without being packaged would ship a Lambda
  that raises ``ModuleNotFoundError`` on its first invocation, which is an outage discovered by an
  auditor rather than by a test.
- **The artefact stands alone.** Extracted into an empty directory, with the repository nowhere on
  the path, it imports and answers. That is the independence claim made executable.
- **The bytes are stable.** Two builds of one tree are identical, which is what lets a proof
  document name the artefact that produced a result.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts.evaluations_deploy import (
    ARCHITECTURE,
    HANDLER,
    MEMORY_MB,
    PACKAGE,
    PACKAGE_MODULES,
    RUNTIME,
    TIMEOUT_SECONDS,
    ZIP_EPOCH,
    DeploymentRefused,
    build_package,
    evaluator_sources,
    package_digest,
    package_manifest,
)

ROOT = Path(__file__).resolve().parent.parent

#: Anything whose presence in a packaged module would mean the evaluator is no longer independent.
#: Checked against the packaged bytes, not against the files on disk.
FORBIDDEN_IMPORTS = ("import careloop", "from careloop", "import boto3", "import opentelemetry")


@pytest.fixture(scope="module")
def payload() -> bytes:
    return build_package(ROOT)


@pytest.fixture(scope="module")
def entries(payload: bytes) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        return archive.infolist()


class TestTheManifestIsExact:
    """Five modules, named, and the directory holds no sixth."""

    def test_the_manifest_is_the_five_evaluator_modules(self) -> None:
        assert package_manifest() == (
            "evaluators/__init__.py",
            "evaluators/contract.py",
            "evaluators/handler.py",
            "evaluators/settlement.py",
            "evaluators/spans.py",
        )

    def test_the_manifest_and_the_directory_agree(self) -> None:
        """Both directions. A module added to one and not the other is the defect this catches."""
        assert sorted(package_manifest()) == sorted(evaluator_sources(ROOT))

    def test_the_archive_contains_exactly_the_manifest(
        self, entries: list[zipfile.ZipInfo]
    ) -> None:
        assert [entry.filename for entry in entries] == list(package_manifest())

    def test_every_entry_lives_under_the_evaluators_package(
        self, entries: list[zipfile.ZipInfo]
    ) -> None:
        for entry in entries:
            assert entry.filename.startswith(f"{PACKAGE}/")
            assert ".." not in entry.filename

    def test_no_bytecode_and_no_directory_entries(self, entries: list[zipfile.ZipInfo]) -> None:
        for entry in entries:
            assert not entry.is_dir()
            assert not entry.filename.endswith(".pyc")
            assert "__pycache__" not in entry.filename

    def test_a_manifest_naming_something_absent_is_refused(self, tmp_path: Path) -> None:
        """The failure path of the build, so the two-way check above cannot pass vacuously."""
        (tmp_path / PACKAGE).mkdir()
        with pytest.raises(DeploymentRefused):
            build_package(tmp_path)


class TestTheArtefactCarriesNothingItMustNot:
    """The independence boundary, asserted on the packaged bytes."""

    def test_no_packaged_module_imports_careloop_or_a_provider_sdk(self, payload: bytes) -> None:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            for entry in archive.namelist():
                source = archive.read(entry).decode("utf-8")
                for forbidden in FORBIDDEN_IMPORTS:
                    assert forbidden not in source, f"{entry} carries {forbidden!r}"

    def test_the_package_is_small(self, payload: bytes) -> None:
        """A guard on the class of accident that ships a tree. Five modules are tens of KB."""
        assert len(payload) < 100_000


class TestTheBytesAreStable:
    """An unchanged tree produces an unchanged artefact."""

    def test_two_builds_are_identical(self, payload: bytes) -> None:
        assert build_package(ROOT) == payload

    def test_every_entry_carries_the_fixed_epoch(self, entries: list[zipfile.ZipInfo]) -> None:
        for entry in entries:
            assert entry.date_time == ZIP_EPOCH

    def test_the_digest_is_the_sha256_of_the_bytes(self, payload: bytes) -> None:
        import hashlib

        assert package_digest(payload) == hashlib.sha256(payload).hexdigest()


class TestTheHandlerResolvesFromTheArtefactAlone:
    """Extracted into an empty directory, with the repository off the path, it answers."""

    def test_the_handler_string_names_a_callable_in_the_package(self, tmp_path: Path) -> None:
        module_path, _, attribute = HANDLER.rpartition(".")
        assert module_path == "evaluators.handler"
        assert attribute == "lambda_handler"

    def test_the_extracted_package_answers_without_the_repository(
        self, payload: bytes, tmp_path: Path
    ) -> None:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            archive.extractall(tmp_path)
        # Run from the extracted directory so the repository root is not on sys.path. A malformed
        # envelope is the cheapest total input: it exercises import, dispatch and the error shape
        # without needing a fixture.
        finished = subprocess.run(
            [
                sys.executable,
                "-c",
                "from evaluators.handler import lambda_handler;"
                "print(lambda_handler(None)['errorCode'])",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert finished.returncode == 0, finished.stderr
        assert finished.stdout.strip() == "ENVELOPE_MALFORMED"


class TestTheDeployedShapeIsTheMeasuredOne:
    """The constants a change would have to move deliberately."""

    def test_the_runtime_and_size_are_what_patch_a_measured(self) -> None:
        assert RUNTIME == "python3.12"
        assert ARCHITECTURE == "x86_64"
        assert MEMORY_MB == 128
        assert TIMEOUT_SECONDS == 30

    def test_the_module_list_is_a_tuple_of_bare_module_names(self) -> None:
        for module in PACKAGE_MODULES:
            assert "/" not in module
            assert module.endswith(".py")
