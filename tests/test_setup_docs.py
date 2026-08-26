"""The setup contract, asserted against the thing that makes it true.

A new engineer learns three things from this repository before anything runs: which Python to
install, which Node to install, and which commands turn a clean checkout into a working CareLoop.
Until this file existed, all three were prose. ``node-version: "24"`` lived inside
``.github/workflows/ci.yml`` and nowhere a reader would look; ``requires-python = ">=3.12"`` was a
resolver floor being read as a statement about what had been *run*; and the documented install
commands were the CI commands only by the accident of one person having written both.

So each claim is checked against its source rather than against a constant written down beside it --
the treatment ``tests/test_env_example.py`` gives the local template, and
``tests/test_deploy_docs.py`` gives the deployment documentation.

**Semantics, never punctuation.** ``README.md`` legitimately writes ``.venv/Scripts/pip install -e
".[dev]"`` where CI writes ``pip install -e ".[dev]"``, carries Windows and Linux variants of one
line, and ends several of them in a trailing ``#`` comment. A guard demanding that every fenced
command appear verbatim in a workflow would fail on a correct README and would be deleted within a
week. What is compared here is the *invocation* -- the arguments after the interpreter and the tool
-- so a venv path, a ``python -m`` prefix, an ``npm --prefix`` and a trailing comment are all noise,
while a change from ``npm ci`` to ``npm install`` is not.

**No npm semver implementation.** No such parser is in this project's dependency tree, and writing
one is how a guard acquires bugs of its own that look like the thing it guards. Instead the accepted
``engines`` grammar is deliberately closed: exactly ``>=N <N+1``, one tested major, parsed by a
strict regular expression that *refuses* anything else rather than interpreting it. A range this
file cannot parse fails here, which is the same choice ``recheck_target`` makes when two identities
disagree -- the disagreement is the finding, not something to resolve to one side. Python is a
different matter: ``packaging`` is already in the tree, so PEP 440 is parsed by the library that
owns it.

**Comments are stripped before a claim is read out of source.** ``e2e/playwright.config.ts`` names
``demo_serve.py`` in prose, explaining why it is *not* used -- so a naive substring check would read
the explanation of the rule as a violation of it. ``tests/test_recovery_copy.py`` strips TypeScript
comments for the same reason and in the same words: a sweep that cannot tell a claim from an
explanation of the claim is a sweep that gets deleted.

**Anti-vacuity is structural.** Every invariant below is a pure predicate over parsed inputs, and
``TestTheReadersWouldNotice`` calls those same predicates against mutated *copies*. No repository
file is edited by a test, and no constant feeds both the expected and the actual side of a
comparison: every value is read out of the file that owns it.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
WORKFLOW: Final[Path] = ROOT / ".github" / "workflows" / "ci.yml"
NVMRC: Final[Path] = ROOT / ".nvmrc"
README: Final[Path] = ROOT / "README.md"
FRONTEND_README: Final[Path] = ROOT / "frontend" / "README.md"
PYPROJECT: Final[Path] = ROOT / "pyproject.toml"
PLAYWRIGHT_CONFIG: Final[Path] = ROOT / "e2e" / "playwright.config.ts"

#: The Node package trees this repository builds. Both are tested by one toolchain.
NODE_TREES: Final[tuple[str, ...]] = ("frontend", "e2e")

#: The environment a job must set to be unable to reach AWS. The values matter as much as the keys:
#: an empty ``AWS_CONFIG_FILE`` is not a denial of anything.
CREDENTIAL_DENIAL: Final[dict[str, str]] = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_CONFIG_FILE": "/nonexistent",
    "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
}

#: The only ``engines.node`` shape this file will interpret: one tested major, closed above.
ENGINE_GRAMMAR: Final[re.Pattern[str]] = re.compile(r"^>=(?P<low>\d+)\s+<(?P<high>\d+)$")

#: A pip invocation, however it was reached: ``python -m pip``, ``.venv/Scripts/pip``, bare ``pip``.
PIP_CALL: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)(?:[\w./\\-]*python(?:\.exe)?\s+-m\s+pip|[\w./\\-]*pip(?:\.exe)?)\s+(?P<args>.+?)\s*$"
)

#: An npm invocation, with any ``--prefix <dir>`` dropped: it says *where*, never *what*.
#:
#: The class excludes a newline as well as the shell separators, and that is not cosmetic. Without
#: it, ``npm run build`` at the end of a fenced block ran on into every following line of the
#: document -- so one README block yielded a single 40-token "command" and the comparison below
#: silently stopped being about npm at all. Caught by an assertion rather than by review.
NPM_CALL: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)npm\s+(?:--prefix\s+\S+\s+)?(?P<args>[^&|;\n]+)"
)

#: TypeScript comments, both forms. Applied before any claim is read out of a config.
TS_COMMENTS: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


# --- readers --------------------------------------------------------------------------------------


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def package_json(tree: str) -> dict[str, Any]:
    return json.loads((ROOT / tree / "package.json").read_text(encoding="utf-8"))


def declared_python() -> str:
    """The floor ``pyproject.toml`` declares -- a resolver constraint, not a tested runtime."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["requires-python"]


def setup_version(job: dict[str, Any], key: str) -> str:
    """The single ``key`` a job's setup actions pin, refusing a job that pins two different ones."""
    found = {str(step["with"][key]) for step in job["steps"] if key in (step.get("with") or {})}
    assert len(found) == 1, f"expected exactly one {key} in this job, found {sorted(found)}"
    return found.pop()


def jobs_pinning(ci: dict[str, Any], key: str) -> set[str]:
    """Every version of ``key`` any job pins, so two jobs testing two runtimes is visible."""
    return {
        setup_version(job, key)
        for job in ci["jobs"].values()
        if any(key in (step.get("with") or {}) for step in job["steps"])
    }


def run_lines(job: dict[str, Any], *, within: str | None = None) -> list[str]:
    """Every shell line a job runs, optionally only those whose working directory is ``within``.

    The job-level ``defaults.run.working-directory`` is honoured, because the ``frontend`` job sets
    it once rather than on each step -- and a reader that missed it would conclude that job installs
    nothing at all and then pass vacuously.
    """
    fallback = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
    lines: list[str] = []
    for step in job["steps"]:
        if "run" not in step:
            continue
        if within is not None and (step.get("working-directory") or fallback) != within:
            continue
        lines.extend(step["run"].splitlines())
    return lines


def pip_arguments(lines: list[str]) -> list[list[str]]:
    """What each pip call asks for, with every interpreter prefix and trailing comment dropped."""
    return [
        shlex.split(match["args"], comments=True)
        for line in lines
        if (match := PIP_CALL.search(line))
    ]


def npm_arguments(text: str) -> list[list[str]]:
    """What each npm call asks for, with any ``--prefix`` and trailing comment dropped."""
    return [shlex.split(match["args"], comments=True) for match in NPM_CALL.finditer(text)]


def fenced_commands(document: Path) -> str:
    """Every fenced code block in ``document``, joined. Prose is not a command."""
    text = document.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"```[a-z]*\n(.*?)```", text, re.S))


def toolchain_paragraph(text: str) -> str:
    """The README paragraph stating the tested runtimes, or ``""`` if it has gone."""
    for paragraph in text.replace("\r\n", "\n").split("\n\n"):
        if "tested toolchain" in paragraph:
            return paragraph
    return ""


def without_comments(source: str) -> str:
    return TS_COMMENTS.sub(" ", source)


# --- the invariants, as predicates the real checks and the mutants both call ---------------------


def engine_majors(engine: str) -> tuple[int, int]:
    """``(low, high)`` for an accepted range. Raises on anything this file will not interpret."""
    match = ENGINE_GRAMMAR.match(engine.strip())
    if match is None:
        raise ValueError(
            f"engines.node {engine!r} is not the closed grammar this repository accepts; "
            f"the one accepted range is `>=N <N+1`, a single tested major. Widen it "
            f"deliberately rather than leaving the range to be guessed at."
        )
    low, high = int(match["low"]), int(match["high"])
    if high != low + 1:
        raise ValueError(f"engines.node {engine!r} spans more than one major, which is untested")
    return low, high


def engine_admits(engine: str, major: int) -> bool:
    low, high = engine_majors(engine)
    return low <= major < high


def python_is_within_declaration(ci_version: str, requires_python: str) -> bool:
    return Version(ci_version) in SpecifierSet(requires_python)


def documents_agree_on_node(nvmrc: str, ci_node: str, engines: dict[str, str], readme: str) -> bool:
    """Every place naming a tested Node names the same one, and admits no untested successor."""
    major = int(ci_node.split(".")[0])
    if int(nvmrc.strip().lstrip("v").split(".")[0]) != major:
        return False
    for engine in engines.values():
        if not engine_admits(engine, major) or engine_admits(engine, major + 1):
            return False
    return str(major) in toolchain_paragraph(readme)


def install_paths_match(documented: list[list[str]], performed: list[list[str]]) -> bool:
    """The documented pip install is one CI performs, compared as arguments rather than as text."""
    installs = [args for args in documented if args and args[0] == "install"]
    return bool(installs) and all(args in performed for args in installs)


def npm_mode_is_reproducible(commands: list[list[str]]) -> bool:
    """No npm call here resolves a tree the committed lockfile did not decide."""
    return bool(commands) and not any(args and args[0] == "install" for args in commands)


def job_denies_credentials(job: dict[str, Any]) -> bool:
    env = job.get("env") or {}
    if any(env.get(key) != value for key, value in CREDENTIAL_DENIAL.items()):
        return False
    return not any(key.startswith("CARELOOP_MODEL_") for key in env)


def assembly_is_the_scripted_harness(config: str) -> bool:
    """The app CI assembles is the scripted harness, and no model-resolving entry point."""
    commands = without_comments(config)
    return "e2e_serve.py" in commands and "demo_serve.py" not in commands


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return workflow()


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def engines() -> dict[str, str]:
    return {tree: package_json(tree)["engines"]["node"] for tree in NODE_TREES}


# --- Python --------------------------------------------------------------------------------------


class TestThePythonRuntimeContract:
    """The package floor and the tested runtime are different claims, and are stated as two."""

    def test_ci_python_satisfies_the_package_declaration(self, ci) -> None:
        pinned = setup_version(ci["jobs"]["check"], "python-version")
        assert python_is_within_declaration(pinned, declared_python())

    def test_the_readme_names_the_python_ci_exercises(self, ci, readme) -> None:
        pinned = setup_version(ci["jobs"]["check"], "python-version")
        assert pinned in toolchain_paragraph(readme)

    def test_the_readme_quotes_the_declaration_beside_the_tested_runtime(self, readme) -> None:
        """Both halves, so a resolver floor cannot be read as the only Python anybody has run."""
        assert declared_python() in toolchain_paragraph(readme)

    def test_every_python_job_pins_one_runtime(self, ci) -> None:
        pinned = jobs_pinning(ci, "python-version")
        assert len(pinned) == 1, f"CI exercises more than one Python: {sorted(pinned)}"


# --- Node ----------------------------------------------------------------------------------------


class TestTheNodeRuntimeContract:
    """One tested major, named identically everywhere, admitting no untested successor."""

    def test_every_declaration_agrees(self, ci, readme, engines) -> None:
        assert documents_agree_on_node(
            NVMRC.read_text(encoding="utf-8"),
            setup_version(ci["jobs"]["frontend"], "node-version"),
            engines,
            readme,
        )

    @pytest.mark.parametrize("tree", NODE_TREES)
    def test_ci_node_satisfies_the_declared_engine(self, ci, engines, tree) -> None:
        major = int(setup_version(ci["jobs"]["frontend"], "node-version").split(".")[0])
        assert engine_admits(engines[tree], major)

    @pytest.mark.parametrize("tree", NODE_TREES)
    def test_the_engine_advertises_no_untested_major(self, ci, engines, tree) -> None:
        """``>=24`` would claim Node 25 and 26 work here. Nobody has run them."""
        major = int(setup_version(ci["jobs"]["frontend"], "node-version").split(".")[0])
        assert not engine_admits(engines[tree], major + 1)

    def test_both_trees_tell_one_story(self, engines) -> None:
        assert len(set(engines.values())) == 1, f"two supported-Node stories: {engines}"

    def test_every_node_job_pins_one_runtime(self, ci) -> None:
        pinned = jobs_pinning(ci, "node-version")
        assert len(pinned) == 1, f"CI exercises more than one Node: {sorted(pinned)}"

    def test_the_readme_points_at_the_version_file(self, readme) -> None:
        assert ".nvmrc" in toolchain_paragraph(readme)


# --- the documented setup path -------------------------------------------------------------------


class TestTheDocumentedInstallPathIsTheOneCiTakes:
    """Compared as invocations. A venv prefix is not a difference; an extra is."""

    def test_the_readme_install_is_a_ci_install(self, ci) -> None:
        assert install_paths_match(
            pip_arguments(fenced_commands(README).splitlines()),
            pip_arguments(run_lines(ci["jobs"]["check"])),
        )

    def test_the_normaliser_drops_the_interpreter_and_keeps_the_extra(self) -> None:
        """The reader's own boundary, stated: prefixes and comments are noise, arguments are not."""
        wanted = [["install", "-e", ".[dev]"]]
        assert pip_arguments(['.venv/Scripts/pip install -e ".[dev]"   # Linux: bin/pip']) == wanted
        assert pip_arguments(['python -m pip install -e ".[dev]"']) == wanted
        assert pip_arguments(['pip install -e ".[bench]"']) != wanted


class TestTheDocumentedFrontendPathIsTheOneCiTakes:
    """A clean install resolves the committed lockfile, in the documentation and in CI alike."""

    @pytest.mark.parametrize("document", [README, FRONTEND_README], ids=["readme", "frontend"])
    def test_the_documented_install_is_lockfile_exact(self, document) -> None:
        assert npm_mode_is_reproducible(npm_arguments(fenced_commands(document)))

    def test_ci_installs_the_same_way(self, ci) -> None:
        assert npm_mode_is_reproducible(npm_arguments("\n".join(run_lines(ci["jobs"]["frontend"]))))

    def test_the_documented_build_is_a_real_package_script_ci_runs(self, ci) -> None:
        scripts = package_json("frontend")["scripts"]
        called = npm_arguments(fenced_commands(README))
        documented = [args[1] for args in called if args[:1] == ["run"]]
        assert documented, "the README documents no frontend script at all"
        assert all(name in scripts for name in documented)
        performed = npm_arguments("\n".join(run_lines(ci["jobs"]["frontend"])))
        assert all(["run", name] in performed for name in documented)


# --- the credential-free claim -------------------------------------------------------------------


class TestTheCredentialFreeAssemblyClaimIsTrue:
    """The README says CI assembles CareLoop without credentials. This is what makes that true."""

    def test_the_readme_makes_the_claim(self, readme) -> None:
        assert "credential-free" in readme

    @pytest.mark.parametrize("job", ["check", "e2e"])
    def test_the_job_can_reach_neither_aws_nor_a_model(self, ci, job) -> None:
        assert job_denies_credentials(ci["jobs"][job])

    def test_the_assembled_app_is_the_scripted_harness(self) -> None:
        assert assembly_is_the_scripted_harness(PLAYWRIGHT_CONFIG.read_text(encoding="utf-8"))

    def test_the_e2e_job_starts_no_model_resolving_server(self, ci) -> None:
        assert "demo_serve.py" not in "\n".join(run_lines(ci["jobs"]["e2e"]))

    def test_the_harness_is_not_offered_as_a_way_to_launch_careloop(self, readme) -> None:
        """``scripts/e2e_serve.py`` is test-only and the README may not promote it.

        What the README is allowed to say is what CI *proves*. The moment that file appears there as
        a command, a test harness has become a third application mode -- and its own docstring says
        it is structurally not one.
        """
        assert "e2e_serve" not in readme


# --- anti-vacuity --------------------------------------------------------------------------------


class TestTheReadersWouldNotice:
    """Every predicate above, against a mutated copy. Nothing here edits a repository file."""

    def test_a_ci_node_bump_that_leaves_nvmrc_behind_is_caught(self, readme, engines) -> None:
        assert not documents_agree_on_node("24\n", "25", engines, readme)

    def test_an_engine_raised_above_ci_is_caught(self, readme) -> None:
        assert not documents_agree_on_node("24\n", "24", {"frontend": ">=25 <26"}, readme)

    def test_an_engine_opened_to_every_future_major_is_refused(self) -> None:
        with pytest.raises(ValueError, match="closed"):
            engine_majors(">=24")

    def test_a_multi_major_engine_is_refused(self) -> None:
        with pytest.raises(ValueError, match="more than one major"):
            engine_majors(">=24 <27")

    def test_a_readme_that_lost_its_node_declaration_is_caught(self, engines) -> None:
        assert not documents_agree_on_node("24\n", "24", engines, "no toolchain paragraph here")

    def test_a_python_outside_the_declaration_is_caught(self) -> None:
        assert not python_is_within_declaration("3.11", ">=3.12")

    def test_an_npm_call_stops_at_the_end_of_its_line(self) -> None:
        """The defect this reader really had: a call that ran on into the rest of the document."""
        assert npm_arguments("npm ci\nnpm run build\n") == [["ci"], ["run", "build"]]

    def test_npm_install_instead_of_npm_ci_is_caught(self) -> None:
        assert not npm_mode_is_reproducible(npm_arguments("npm --prefix frontend install"))
        assert npm_mode_is_reproducible(npm_arguments("npm --prefix frontend ci"))

    def test_an_install_ci_never_performs_is_caught(self) -> None:
        documented = [["install", "-e", ".[dev]"]]
        assert not install_paths_match(documented, [["install", "-e", ".[bench]"]])

    def test_documenting_nothing_at_all_does_not_satisfy_these(self) -> None:
        """An empty documented set must not satisfy an assertion quantified over it."""
        assert not install_paths_match([], [["install", "-e", ".[dev]"]])
        assert not npm_mode_is_reproducible([])

    def test_removing_the_credential_denial_is_caught(self, ci) -> None:
        mutant = copy.deepcopy(ci["jobs"]["e2e"])
        del mutant["env"]["AWS_CONFIG_FILE"]
        assert not job_denies_credentials(mutant)

    def test_emptying_the_credential_denial_is_caught(self, ci) -> None:
        mutant = copy.deepcopy(ci["jobs"]["e2e"])
        mutant["env"]["AWS_CONFIG_FILE"] = ""
        assert not job_denies_credentials(mutant)

    def test_a_job_handed_a_real_model_is_caught(self, ci) -> None:
        mutant = copy.deepcopy(ci["jobs"]["e2e"])
        mutant["env"]["CARELOOP_MODEL_PLANNER"] = "bedrock:us.amazon.nova-2-lite-v1:0"
        assert not job_denies_credentials(mutant)

    def test_switching_the_assembly_to_the_model_backed_server_is_caught(self) -> None:
        real = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
        assert not assembly_is_the_scripted_harness(real.replace("e2e_serve.py", "demo_serve.py"))

    def test_an_explanation_of_the_rule_is_not_read_as_a_breach_of_it(self) -> None:
        """The comment-stripping half, isolated: prose naming the forbidden file must not fail."""
        assert assembly_is_the_scripted_harness(
            '/* demo_serve.py cannot be used here. */\ncommand: "scripts/e2e_serve.py"'
        )
        assert not assembly_is_the_scripted_harness('command: "scripts/demo_serve.py"')

    def test_the_readers_really_read_the_shipped_files(self, ci, engines) -> None:
        """The parsers are pointed at the repository, not at fixtures that flatter them."""
        assert set(ci["jobs"]) >= {"check", "frontend", "e2e"}
        assert engines and all(ENGINE_GRAMMAR.match(value) for value in engines.values())
        assert pip_arguments(run_lines(ci["jobs"]["check"]))
        assert npm_arguments("\n".join(run_lines(ci["jobs"]["frontend"])))
        assert toolchain_paragraph(README.read_text(encoding="utf-8"))
