"""CareLoop boots without the ADOT launcher, and the deployment says so rather than relying on it.

``scripts/demo_serve.py`` imports ``scripts.observability``. ``python scripts/demo_serve.py`` puts
the *script's* directory on ``sys.path`` and never the working directory, and the editable install
maps ``careloop`` alone — so the application root has to be supplied by something. Until this file
existed it was supplied by ``opentelemetry-instrument``, whose ``run()`` inserts ``getcwd()`` into
``PYTHONPATH`` before exec'ing the command it was given. The instrumented ``ExecStart`` in
``deploy/careloop-api.service`` therefore booted, and the uninstrumented one immediately below it —
the documented rollback, the whole of turning telemetry off — died on ``ModuleNotFoundError: No
module named 'scripts'`` and took systemd into a restart loop with it.

That is an optional observability layer holding up an availability property, which is the one
direction this repository's telemetry position does not permit. So the unit declares
``Environment=PYTHONPATH=/opt/careloop/app`` itself, and this file is what fails if it is ever
removed for tidiness.

**Two halves, and both are needed.** The unit is parsed, because the declaration is the fix and its
*value* is what makes the two modes agree — the launcher inserts the cwd only when it is not already
on ``PYTHONPATH``, so ``PYTHONPATH == WorkingDirectory`` is precisely what stops the instrumented
path acquiring a different application root. And both commands are then actually run against this
checkout, with the environment derived from what the unit declares rather than from a constant: a
unit that stopped declaring it produces a subprocess with no ``PYTHONPATH``, which is the failure
being guarded against.

**The launcher is real here, not modelled.** ``opentelemetry-instrument`` is the console script of
``opentelemetry-instrumentation``, which arrives through ``strands-agents`` →
``opentelemetry-instrumentation-threading``, so it is present under a bare ``.[dev]`` install and
this suite never skips. ``aws-opentelemetry-distro`` is the deployment extra and supplies a
*distro*; the ``PYTHONPATH`` transform under test lives in the launcher, which is the same object
either way — the same reasoning ``tests/test_observability.py`` uses to prove the redaction guard
without installing ADOT.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
UNIT: Final[Path] = REPO_ROOT / "deploy" / "careloop-api.service"

#: The entry point the demo is served through, named by both ``ExecStart`` forms.
ENTRY_POINT: Final[str] = "scripts/demo_serve.py"


def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def directive(name: str) -> str | None:
    """The value of a live (uncommented) ``name=value`` directive, or ``None`` if absent."""
    match = re.search(rf"^{name}=(.*)$", unit_text(), re.MULTILINE)
    return match.group(1).strip() if match else None


def declared_pythonpath() -> str | None:
    """What the unit says the application root is, or ``None`` if it has stopped saying."""
    match = re.search(r"^Environment=PYTHONPATH=(.*)$", unit_text(), re.MULTILINE)
    return match.group(1).strip() if match else None


def exec_starts() -> tuple[str, str]:
    """The active ``ExecStart`` and the commented rollback, each as one joined line.

    Line continuations are folded and the rollback's comment markers stripped, so both come back in
    the form systemd would see. Nothing else in the file begins a line with ``ExecStart=``.
    """
    active: list[str] = []
    rollback: list[str] = []
    target: list[str] | None = None
    for raw in unit_text().splitlines():
        line = raw.strip()
        stripped = line.lstrip("#").strip()
        if stripped.startswith("ExecStart="):
            target = rollback if line.startswith("#") else active
        if target is None:
            continue
        target.append(stripped)
        if not stripped.endswith("\\"):
            target = None
    return " ".join(active).replace("\\", " "), " ".join(rollback).replace("\\", " ")


def deployed_environment(*, declare: bool = True) -> dict[str, str]:
    """This process's environment as the unit would leave it, mapped onto this checkout.

    ``PYTHONPATH`` is cleared first and then set **only if the unit still declares it**, which is
    what keeps the two subprocess tests below from passing on a unit that has lost the line. It is
    set to the repository root rather than to the deployed path for the same reason the unit sets it
    to ``WorkingDirectory``: the value is "the application root", and here that is this tree.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if declare and declared_pythonpath() is not None:
        env["PYTHONPATH"] = str(REPO_ROOT)
    # The launcher configures an SDK that has no backend here; the transform under test happens
    # before that and is unaffected. Off, so this file cannot reach a network on any machine.
    env["OTEL_SDK_DISABLED"] = "true"
    return env


def boot(command: list[str], *, declare: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one of the unit's two startup forms far enough to prove its imports resolve.

    ``--help`` is argparse's, so it is reached only after ``scripts/demo_serve.py`` has been
    imported — which is where the failure was.
    """
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=deployed_environment(declare=declare),
        capture_output=True,
        text=True,
        timeout=120,
    )


def launcher() -> str:
    """``opentelemetry-instrument`` from the interpreter running this suite."""
    found = shutil.which("opentelemetry-instrument", path=str(Path(sys.executable).parent))
    assert found is not None, (
        "opentelemetry-instrument is not installed beside this interpreter. It arrives through "
        "strands-agents, so its absence is a broken environment rather than a missing extra — fix "
        "the install rather than skipping the instrumented half of this file."
    )
    return found


class TestTheUnitDeclaresTheApplicationImportPath:
    def test_it_declares_one_at_all(self) -> None:
        assert declared_pythonpath() is not None, (
            "deploy/careloop-api.service no longer sets Environment=PYTHONPATH. Without it the "
            "application root reaches sys.path only because opentelemetry-instrument inserts the "
            "cwd, so the documented uninstrumented rollback below it will not boot."
        )

    def test_it_is_the_working_directory(self) -> None:
        """The value, not merely the line — this is what makes both modes resolve the same root.

        ``opentelemetry-instrument`` inserts ``getcwd()`` into ``PYTHONPATH`` only when it is not
        already there. Declaring the working directory makes that insertion a no-op, so the
        instrumented path cannot acquire an application root the uninstrumented one does not have.
        """
        assert declared_pythonpath() == directive("WorkingDirectory")

    def test_both_startup_forms_are_present_and_run_the_same_entry_point(self) -> None:
        active, rollback = exec_starts()
        assert ENTRY_POINT in active
        assert ENTRY_POINT in rollback, (
            "the commented uninstrumented ExecStart is the whole of the observability rollback; "
            "deploy/README.md documents swapping the two."
        )

    def test_only_the_active_form_names_the_launcher(self) -> None:
        """Which is the property the rollback exists to exercise, stated so it cannot invert."""
        active, rollback = exec_starts()
        assert "opentelemetry-instrument" in active
        assert "opentelemetry-instrument" not in rollback


class TestBothStartupModesBootFromThatDeclaration:
    def test_the_uninstrumented_command_imports_the_application(self) -> None:
        """The rollback, and the thing that was broken."""
        result = boot([sys.executable, ENTRY_POINT, "--help"])
        assert result.returncode == 0, result.stderr

    def test_the_instrumented_command_imports_the_application(self) -> None:
        result = boot([launcher(), sys.executable, ENTRY_POINT, "--help"])
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("instrumented", [False, True], ids=["uninstrumented", "instrumented"])
    def test_both_reach_the_same_argument_parser(self, instrumented: bool) -> None:
        """Not merely "a process exited 0": the same application, with the same surface.

        ``--isolate-sessions`` is the flag the deployed unit passes and the one a rollback must keep
        honouring, so it stands in for having got all the way to CareLoop's own entry point.
        """
        command = [sys.executable, ENTRY_POINT, "--help"]
        if instrumented:
            command.insert(0, launcher())
        result = boot(command)
        assert result.returncode == 0, result.stderr
        assert "--isolate-sessions" in result.stdout


class TestTheDeclarationIsWhatSuppliesIt:
    """The vacuity proof. Without the unit's line the uninstrumented form must still fail.

    If this ever goes green, something else on this machine is putting the repository root on
    ``sys.path`` — a ``.pth``, a stray ``PYTHONPATH``, a non-editable install of ``scripts`` — and
    the tests above have stopped measuring the deployment contract.
    """

    def test_without_it_the_uninstrumented_command_still_cannot_import(self) -> None:
        result = boot([sys.executable, ENTRY_POINT, "--help"], declare=False)
        assert result.returncode != 0
        assert "ModuleNotFoundError" in result.stderr
        assert "scripts" in result.stderr

    def test_without_it_the_launcher_is_what_rescues_the_instrumented_command(self) -> None:
        """The defect itself, held still: ADOT alone makes the difference between the two."""
        result = boot([launcher(), sys.executable, ENTRY_POINT, "--help"], declare=False)
        assert result.returncode == 0, result.stderr
