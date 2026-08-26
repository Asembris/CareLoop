"""One writer of live evidence, one gate on it, and the registry owning where a record lands.

``careloopbench.record`` exists for one reason, and it is worth stating before any assertion below.
:func:`~careloopbench.ingest.ingest_one` has three outcomes and none of them is ``FAILED``, which
is correct — a stored record is something somebody else measured and the invocation reading it
watched nothing happen. The consequence is that a record of a *failed* run would render
``PROVEN_AT``, indistinguishable from one that held. All three proof tools make that reachable:
each writes its ``--out`` file and *then* returns a non-zero exit code.

So the gate lives on the writing side. Every raw output must carry its own tool's ``passed: true``,
and everything a reader later checks the record against — the destination, the tool name, the
dependency surface and therefore the digest — comes from the claim registry rather than from the
caller. Those are the two properties here, and each has a failing case beside it: a gate nobody has
watched refuse, and an ownership nobody has watched override, are decoration.

The synthetic repository is a real ``git`` repository with a real commit, exactly as
``tests/test_careloopbench.py`` builds one, because ``surface_digest`` reads ``git ls-tree`` and a
digest over a directory nobody committed would prove nothing about the thing being asserted.
"""

from __future__ import annotations

import ast
import json
import subprocess
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest

from careloopbench.claims import ARTIFACT_ROOT, EVERY_CLAIM, ArtifactRef, Claim
from careloopbench.ingest import MEASURED, OPERATOR, ArtifactRefused, ingest_one, surface_digest
from careloopbench.levels import Domain, ExecutionLevel, Status
from careloopbench.record import (
    VERDICT,
    RecordingRefused,
    artifact_for,
    build_envelope,
    claim_for,
    raw_that_passed,
    write_record,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BENCH_ROOT: Final[Path] = REPO_ROOT / "careloopbench"

GIT = ["git", "-c", "user.email=nobody@example.invalid", "-c", "user.name=nobody"]

#: Where the synthetic claim below says its record lives, and the surface it depends on.
STORED: Final[str] = f"{ARTIFACT_ROOT}example.json"
WATCHED: Final[str] = "watched.txt"


@pytest.fixture
def a_repository(tmp_path: Path) -> Path:
    """A real one-commit repository, so the digest is over bytes somebody really committed."""
    (tmp_path / WATCHED).write_text("before\n", encoding="utf-8")
    subprocess.run([*GIT, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*GIT, "add", WATCHED], cwd=tmp_path, check=True)
    subprocess.run([*GIT, "commit", "-qm", "one"], cwd=tmp_path, check=True)
    return tmp_path


def move_the_surface(root: Path) -> None:
    (root / WATCHED).write_text("after\n", encoding="utf-8")
    subprocess.run([*GIT, "add", WATCHED], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-qm", "two"], cwd=root, check=True)


def commit_something_else(root: Path) -> None:
    """A commit that moves ``HEAD`` and touches nothing the proof declared."""
    (root / "unwatched.txt").write_text("irrelevant\n", encoding="utf-8")
    subprocess.run([*GIT, "add", "unwatched.txt"], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-qm", "three"], cwd=root, check=True)


A_CLAIM: Final[Claim] = Claim(
    id="authority.live",
    question="Was it measured?",
    domain=Domain.AUTHORITY_INTEGRITY,
    level=ExecutionLevel.LIVE_PROVIDER,
    artifact=ArtifactRef(
        proof_id="live.example",
        path=STORED,
        tool="scripts/example.py",
        surface=(WATCHED,),
    ),
    scope="The one measurement it records.",
)
CLAIMS: Final[tuple[Claim, ...]] = (A_CLAIM,)


def a_raw(root: Path, name: str = "raw.json", **overrides: Any) -> Path:
    """A proof tool's output, passing unless told otherwise."""
    payload: dict[str, Any] = {VERDICT: True, "checks": [{"name": "c1", "satisfied": True}]}
    payload.update(overrides)
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def record(root: Path, *raws: Path, **overrides: Any) -> Path:
    fields: dict[str, Any] = {
        "attestation": MEASURED,
        "environment": "a synthetic one",
        "claims": CLAIMS,
        "root": root,
    }
    fields.update(overrides)
    return write_record("live.example", list(raws), **fields)


def stored(root: Path) -> Mapping[str, Any]:
    return json.loads((root / STORED).read_text(encoding="utf-8"))


class TestARecordIsOnlyWrittenForAProofThatHeld:
    """The gate. Everything below is a run whose own tool did not say the proof held."""

    def test_a_passing_tool_output_is_recorded(self, a_repository: Path) -> None:
        written = record(a_repository, a_raw(a_repository))

        assert written == a_repository / STORED
        assert stored(a_repository)["raw"][VERDICT] is True

    def test_a_failed_proof_is_refused(self, a_repository: Path) -> None:
        """Anti-vacuity: this is the whole reason the module exists."""
        with pytest.raises(RecordingRefused, match="did not hold"):
            record(a_repository, a_raw(a_repository, **{VERDICT: False}))

        assert not (a_repository / STORED).exists()

    def test_a_tool_output_with_no_verdict_is_refused(self, a_repository: Path) -> None:
        """Absence is not consent. An older tool's file says nothing about whether it held."""
        path = a_repository / "raw.json"
        path.write_text(json.dumps({"checks": []}), encoding="utf-8")

        with pytest.raises(RecordingRefused, match="carries no"):
            record(a_repository, path)

        assert not (a_repository / STORED).exists()

    @pytest.mark.parametrize("verdict", [1, "true", "passed", [1], {}, None])
    def test_only_a_true_verdict_is_a_verdict(self, a_repository: Path, verdict: Any) -> None:
        """By identity, not truthiness. ``1`` is not a tool saying its proof held."""
        with pytest.raises(RecordingRefused):
            record(a_repository, a_raw(a_repository, **{VERDICT: verdict}))

    def test_every_output_of_a_multi_run_proof_must_pass(self, a_repository: Path) -> None:
        """One met expectation among several does not make the set of them a proof."""
        held = a_raw(a_repository, "one.json")
        missed = a_raw(a_repository, "two.json", **{VERDICT: False})

        with pytest.raises(RecordingRefused, match="did not hold"):
            record(a_repository, held, missed)

        assert not (a_repository / STORED).exists()

    def test_a_refused_record_does_not_replace_the_one_already_there(
        self, a_repository: Path
    ) -> None:
        """A staged write that fails must not half-remove a record that was already good."""
        record(a_repository, a_raw(a_repository))
        before = (a_repository / STORED).read_text(encoding="utf-8")

        with pytest.raises(RecordingRefused):
            record(a_repository, a_raw(a_repository, "bad.json", **{VERDICT: False}))

        assert (a_repository / STORED).read_text(encoding="utf-8") == before
        assert not list((a_repository / ARTIFACT_ROOT).glob("*.staged"))

    def test_output_that_is_not_json_is_refused(self, a_repository: Path) -> None:
        path = a_repository / "raw.json"
        path.write_text("LOCK FAILED: unreachable\n", encoding="utf-8")

        with pytest.raises(RecordingRefused, match="not JSON"):
            record(a_repository, path)

    def test_a_record_needs_at_least_one_tool_output(self, a_repository: Path) -> None:
        with pytest.raises(RecordingRefused, match="at least one"):
            record(a_repository)


class TestTheRegistryOwnsEverythingAReaderChecks:
    """The caller names a proof and hands over what a tool wrote. It decides nothing else."""

    def test_the_destination_comes_from_the_registry(self, a_repository: Path) -> None:
        assert record(a_repository, a_raw(a_repository)) == a_repository / A_CLAIM.artifact.path

    def test_there_is_no_parameter_for_a_destination_surface_tool_or_digest(self) -> None:
        """Structural: a caller cannot pass what it must not choose, so nothing must remember to
        refuse it. Checked on the signature rather than by calling, because the property is that
        the parameter does not exist."""
        signature = ast.parse(BENCH_ROOT.joinpath("record.py").read_text(encoding="utf-8"))
        written = next(
            node
            for node in ast.walk(signature)
            if isinstance(node, ast.FunctionDef) and node.name == "write_record"
        )
        names = {argument.arg for argument in written.args.args + written.args.kwonlyargs}

        assert names.isdisjoint({"path", "out", "destination", "surface", "digest", "tool"})

    def test_the_recorded_tool_and_surface_digest_are_the_declared_ones(
        self, a_repository: Path
    ) -> None:
        record(a_repository, a_raw(a_repository))
        envelope = stored(a_repository)

        assert envelope["tool"] == A_CLAIM.artifact.tool
        assert envelope["surface_digest"] == surface_digest(
            A_CLAIM.artifact.surface, root=a_repository
        )

    def test_a_raw_output_claiming_another_identity_cannot_take_it(
        self, a_repository: Path
    ) -> None:
        """``raw`` is a tool's own bytes and is stored verbatim. It is not an instruction."""
        record(
            a_repository,
            a_raw(a_repository, proof_id="live.something-else", surface_digest="0" * 64),
        )
        envelope = stored(a_repository)

        assert envelope["proof_id"] == "live.example"
        assert envelope["surface_digest"] != "0" * 64
        assert envelope["raw"]["proof_id"] == "live.something-else"

    def test_a_proof_id_the_registry_does_not_declare_is_refused(self, a_repository: Path) -> None:
        with pytest.raises(RecordingRefused, match="no claim declares"):
            write_record(
                "live.invented",
                [a_raw(a_repository)],
                attestation=MEASURED,
                environment="a synthetic one",
                claims=CLAIMS,
                root=a_repository,
            )

    def test_every_shipped_proof_id_resolves(self) -> None:
        """Vacuity: the lookup above must find the real ones, or it refuses everything."""
        for claim in EVERY_CLAIM:
            if claim.artifact is not None:
                assert artifact_for(claim.artifact.proof_id) is claim.artifact
                assert claim_for(claim.artifact.proof_id) is claim


class TestTheRecordSaysHowItsCommitWasEstablished:
    """``MEASURED`` is a tool that could name its commit. ``OPERATOR`` is a person who could not."""

    def test_a_measured_record_names_the_commit_the_tool_ran_against(
        self, a_repository: Path
    ) -> None:
        record(a_repository, a_raw(a_repository))
        head = subprocess.run(
            [*GIT, "rev-parse", "HEAD"], cwd=a_repository, capture_output=True, text=True
        ).stdout.strip()

        assert stored(a_repository)["attested_commit"] == head
        assert stored(a_repository)["ingested_at_commit"] == head

    def test_a_measured_record_may_not_name_somebody_elses_commit(self, a_repository: Path) -> None:
        """``MEASURED`` means the tool observed it. A supplied commit is an operator's word."""
        with pytest.raises(RecordingRefused, match="OPERATOR"):
            record(a_repository, a_raw(a_repository), commit="a" * 40)

    def test_an_operator_record_may_name_a_build_this_repository_cannot_observe(
        self, a_repository: Path
    ) -> None:
        commit_something_else(a_repository)

        record(a_repository, a_raw(a_repository), attestation=OPERATOR, commit="a" * 40)
        envelope = stored(a_repository)

        assert envelope["attestation"] == OPERATOR
        assert envelope["attested_commit"] == "a" * 40
        assert envelope["ingested_at_commit"] != "a" * 40

    def test_an_unknown_attestation_is_refused(self, a_repository: Path) -> None:
        with pytest.raises(RecordingRefused, match="attestation"):
            record(a_repository, a_raw(a_repository), attestation="SIGNED")

    def test_a_record_with_no_environment_is_refused(self, a_repository: Path) -> None:
        with pytest.raises(RecordingRefused, match="environment"):
            record(a_repository, a_raw(a_repository), environment="   ")


class TestARecordedProofIsOneCareLoopBenchCanReport:
    """Written and then read back through the reader, at the reader's own granularity."""

    def test_it_is_proven_at_the_commit_it_names(self, a_repository: Path) -> None:
        record(a_repository, a_raw(a_repository))

        result = ingest_one(A_CLAIM, root=a_repository)

        assert result.status is Status.PROVEN_AT

    def test_a_commit_outside_its_surface_leaves_it_proven(self, a_repository: Path) -> None:
        """Staleness is scoped. A commit touching nothing it declared changes nothing."""
        record(a_repository, a_raw(a_repository))
        commit_something_else(a_repository)

        assert ingest_one(A_CLAIM, root=a_repository).status is Status.PROVEN_AT

    def test_a_commit_inside_its_surface_stales_it(self, a_repository: Path) -> None:
        record(a_repository, a_raw(a_repository))
        move_the_surface(a_repository)

        assert ingest_one(A_CLAIM, root=a_repository).status is Status.STALE


class TestNothingCredentialShapedCanBeFiled:
    """The reader's own refusal, reached before the record reaches its destination."""

    def test_a_token_shaped_value_in_a_tool_output_is_refused(self, a_repository: Path) -> None:
        planted = "eyJhbGciOi" + "abcdef.eyJzdWIiOi" + "abcdef.signature"

        with pytest.raises(ArtifactRefused, match="token"):
            record(a_repository, a_raw(a_repository, observed=planted))

        assert not (a_repository / STORED).exists()

    def test_a_credential_shaped_key_in_a_tool_output_is_refused(self, a_repository: Path) -> None:
        with pytest.raises(ArtifactRefused, match="session_token"):
            record(a_repository, a_raw(a_repository, session_token="anything at all"))

        assert not (a_repository / STORED).exists()

    def test_a_refused_record_leaves_nothing_staged(self, a_repository: Path) -> None:
        """A record that could not be reported must not be left half-written for git to find."""
        with pytest.raises(ArtifactRefused):
            record(a_repository, a_raw(a_repository, api_key="anything at all"))

        assert list((a_repository / ARTIFACT_ROOT).iterdir()) == []

    def test_the_refusal_is_the_readers_and_not_a_second_copy_of_it(self) -> None:
        """Vacuity: a private re-implementation here would drift from what the reader refuses."""
        source = BENCH_ROOT.joinpath("record.py").read_text(encoding="utf-8")

        assert "read_envelope" in source
        assert "FORBIDDEN_KEYS" not in source
        assert "eyJ" not in source


#: Attribute calls that put bytes on disk. Names rather than resolved objects, in the register
#: ``tests/test_api_demo.py``'s ``CaseStore.clear`` sweep already established.
WRITES: Final[frozenset[str]] = frozenset(
    {"write_text", "write_bytes", "writelines", "write", "replace", "dump", "rename"}
)

#: How a function says it is talking about where a live proof is stored.
DESTINATIONS: Final[tuple[str, ...]] = (ARTIFACT_ROOT, "artifact.path", "reference.path")


def live_evidence_writes(path: Path) -> list[int]:
    """Line numbers where ``path`` writes a file to a destination a live proof declared.

    Deliberately narrow, and narrow in both directions. A sweep for every write would fire on
    ``docs/evidence.json``; a sweep for every mention of the directory would fire on the registry
    that declares it and on the reader that resolves it. So a line counts only when a function
    both **resolves** a live-proof destination and **writes** one, which is the pair that files
    evidence.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = ast.unparse(node)
        if not any(name in body for name in DESTINATIONS):
            continue
        lines.extend(
            call.lineno
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in WRITES
        )
    return sorted(lines)


class TestTheRecorderIsTheOneProductionWriter:
    """A second hand-authored writer would be a second way past the gate above.

    Scoped to production code — ``careloopbench/`` and ``scripts/``. Tests construct envelopes on
    purpose and must go on being able to: what this refuses is a *shipped* path that files live
    evidence without asking whether the proof held.
    """

    def production_modules(self) -> list[Path]:
        return sorted(BENCH_ROOT.glob("*.py")) + sorted(REPO_ROOT.joinpath("scripts").glob("*.py"))

    def test_only_the_recorder_writes_a_live_proof(self) -> None:
        offenders = {
            path.name: lines
            for path in self.production_modules()
            if path.name != "record.py" and (lines := live_evidence_writes(path))
        }

        assert offenders == {}

    def test_the_recorder_itself_is_found(self) -> None:
        """Vacuity: a detector that found nothing anywhere would satisfy the assertion above."""
        assert live_evidence_writes(BENCH_ROOT / "record.py")

    def test_the_sweep_has_modules_to_sweep(self) -> None:
        """Vacuity: a glob matching nothing would satisfy it too."""
        names = {path.name for path in self.production_modules()}

        assert {"claims.py", "ingest.py", "record.py", "verify_lock.py"} <= names

    def test_the_reader_resolves_a_destination_and_is_not_a_writer(self) -> None:
        """``ingest.py`` names the same paths and only ever reads them. It must stay unflagged."""
        assert live_evidence_writes(BENCH_ROOT / "ingest.py") == []

    def test_the_detector_finds_a_planted_writer(self, tmp_path: Path) -> None:
        planted = tmp_path / "planted.py"
        planted.write_text(
            textwrap.dedent(
                """
                from pathlib import Path


                def file_it(reference, envelope):
                    Path(reference.path).write_text(envelope)
                """
            ),
            encoding="utf-8",
        )

        assert live_evidence_writes(planted) == [6]

    def test_the_detector_ignores_an_ordinary_write(self, tmp_path: Path) -> None:
        innocent = tmp_path / "innocent.py"
        innocent.write_text(
            textwrap.dedent(
                """
                from pathlib import Path


                def render(out: Path, text: str) -> None:
                    out.write_text(text)
                """
            ),
            encoding="utf-8",
        )

        assert live_evidence_writes(innocent) == []


class TestSeveralRunsAreSeveralRuns:
    """A proof made of several invocations keeps them apart. Nothing merges or summarises them."""

    def test_one_output_is_stored_as_itself(self, a_repository: Path) -> None:
        record(a_repository, a_raw(a_repository))

        assert "runs" not in stored(a_repository)["raw"]

    def test_several_outputs_are_stored_in_the_order_they_were_given(
        self, a_repository: Path
    ) -> None:
        first = a_raw(a_repository, "one.json", scenario="performed")
        second = a_raw(a_repository, "two.json", scenario="suppressed")

        record(a_repository, first, second)
        runs = stored(a_repository)["raw"]["runs"]

        assert [run["scenario"] for run in runs] == ["performed", "suppressed"]

    def test_nothing_summarises_what_the_tool_produced(self, a_repository: Path) -> None:
        """No headline, no summary, no verdict of the recorder's own composing."""
        envelope = build_envelope(
            A_CLAIM.artifact,
            [{VERDICT: True, "checks": []}],
            attestation=MEASURED,
            environment="a synthetic one",
            commit="a" * 40,
            produced_at="2026-08-26",
            root=a_repository,
        )

        assert set(envelope) == {
            "proof_id",
            "tool",
            "attested_commit",
            "attestation",
            "ingested_at_commit",
            "produced_at",
            "environment",
            "surface_digest",
            "raw",
        }

    def test_the_stored_bytes_are_the_tool_s_own(self, a_repository: Path) -> None:
        path = a_raw(a_repository, observed="ChecksumSHA256='abc='", odd={"nested": [1, 2]})
        original = json.loads(path.read_text(encoding="utf-8"))

        record(a_repository, path)

        assert stored(a_repository)["raw"] == original
        assert raw_that_passed(path) == original
