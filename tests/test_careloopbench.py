"""CareLoopBench, held to a smaller trust surface than the system it reports on.

A consolidation layer that can render a green row for a property nobody proved is worse than no
layer at all: it launders absence into evidence, and it does so on the one page a reader goes to
in order to avoid reading everything else. So the rules below are all one rule seen from different
sides — **CareLoopBench must fail closed** — and each is written as the specific way it could
otherwise have lied.

Three things are deliberately *not* tested here. Whether CareLoop's properties hold is the job of
the 150-odd modules beside this one; whether a live provider behaved as recorded is the job of the
tool that measured it; and whether a claim is worth making is a judgement, not an assertion. What
is tested is that CareLoopBench cannot say "proven" about anything it did not watch pass, cannot
count a status that is not a pass, and cannot quietly shrink its own denominator.
"""

from __future__ import annotations

import ast
import dataclasses
import fnmatch
import json
import re
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from careloopbench.claims import (
    ARTIFACT_ROOT,
    CLAIMS,
    EVERY_CLAIM,
    LIVE_CLAIMS,
    ArtifactRef,
    Claim,
    RegistryRefused,
    check_registry,
    claims_in,
    evidence_nodes,
    selected,
)
from careloopbench.ingest import (
    OPERATOR,
    REQUIRED,
    ArtifactRefused,
    ingest_one,
    live_results,
    surface_digest,
)
from careloopbench.levels import TIERS, Domain, ExecutionLevel, Status
from careloopbench.report import (
    BenchResult,
    ClaimResult,
    evaluate,
    registry_digest,
    render_markdown,
    to_payload,
)
from careloopbench.runner import (
    ALLOW_ALL,
    PROTECTED,
    AdversarialEvidence,
    EvidenceDoesNotCollect,
    check_collects,
    junit_classname,
    replay_side,
    run_adversarial_replay,
    run_evidence,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BENCH_ROOT: Final[Path] = REPO_ROOT / "careloopbench"

PROP: Final = ExecutionLevel.PROPERTY
LIVE: Final = ExecutionLevel.LIVE_PROVIDER

NODE: Final[str] = "tests/test_careloopbench.py::TestTheShippedRegistryIsWellFormed"

#: The floor the import sweep must clear before its "nothing imports it" means anything.
MODULES_AT_LEAST: Final[int] = 3

#: How long a git commit hash is, for the synthetic records below.
COMMIT_CHARACTERS: Final[int] = 40


def a_claim(**overrides: object) -> Claim:
    """A minimal well-formed claim, so each test below changes exactly one thing."""
    fields: dict[str, object] = {
        "id": "authority.example",
        "question": "Does the example hold?",
        "domain": Domain.AUTHORITY_INTEGRITY,
        "level": PROP,
        "evidence": (NODE,),
        "scope": "The example and nothing else.",
    }
    fields.update(overrides)
    return Claim(**fields)  # type: ignore[arg-type]


def one_per_domain() -> tuple[Claim, ...]:
    """A registry that satisfies the domain-coverage rule, so other refusals can be isolated."""
    return tuple(
        a_claim(id=f"{domain.value.lower().replace('_', '')}.example", domain=domain)
        for domain in Domain
    )


class TestTheShippedRegistryIsWellFormed:
    """The registry this repository actually declares, checked rather than assumed."""

    def test_it_loads(self) -> None:
        check_registry(EVERY_CLAIM)

    def test_the_live_claims_are_part_of_it(self) -> None:
        assert EVERY_CLAIM == CLAIMS + LIVE_CLAIMS
        assert all(not claim.is_executed for claim in LIVE_CLAIMS)

    def test_every_domain_carries_claims(self) -> None:
        for domain in Domain:
            assert claims_in(domain, EVERY_CLAIM), domain

    def test_every_claim_id_is_unique(self) -> None:
        ids = [claim.id for claim in EVERY_CLAIM]

        assert len(ids) == len(set(ids))

    def test_every_declared_node_names_a_class_that_exists(self) -> None:
        """A cheap typo guard that needs no subprocess.

        The runner proves the stronger property — that every node id *collects* — but it costs a
        pytest process. This one costs a file read, so a mistyped class name is caught by the
        registry's own targeted gate rather than only by a full evidence run.
        """
        for claim in EVERY_CLAIM:
            for node in claim.nodes:
                path, _, klass = node.partition("::")
                source = (REPO_ROOT / path).read_text(encoding="utf-8")
                assert re.search(rf"^class {klass}\b", source, re.MULTILINE), node

    def test_no_claim_is_declared_at_a_level_nothing_can_run_or_ingest(self) -> None:
        for claim in EVERY_CLAIM:
            assert claim.level in ExecutionLevel


def surface_covers(surface: tuple[str, ...], path: str) -> bool:
    """Would a commit touching ``path`` change ``surface_digest(surface)``?

    Asked of ``git ls-tree`` rather than of the tuple, because that is the question
    :func:`~careloopbench.ingest.surface_digest` itself asks: the digest is taken over the listing,
    so a path the listing names is a path whose change moves the digest, and a path it does not
    name is one whose change never will. Comparing prefixes by hand here would be a second, weaker
    implementation of ``git``'s own path matching -- and the surfaces mix directories with files.
    """
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--name-only", "--", *surface],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return path in listing.split()


def surface_of(proof_id: str) -> tuple[str, ...]:
    return next(
        claim.artifact.surface
        for claim in LIVE_CLAIMS
        if claim.artifact is not None and claim.artifact.proof_id == proof_id
    )


class TestEachLiveSurfaceCoversWhatItsProofDependsOn:
    """A surface is what makes staleness mean something, and two of them were factually wrong.

    They are declared per proof because the scopes genuinely differ, which means each one is a
    claim about *what this measurement depended on* and can be true or false. Two were false. The
    deployed lock declared ``frontend/src/`` -- but ``scripts/verify_lock.py`` drives the API with
    ``urllib`` and never fetches the shell or the bundle, so a stylesheet edit staled a proof it
    could not have affected, and the row would have been born stale on the next frontend commit.
    It did *not* declare ``deploy/careloop.env``, which pins the triage and planner models four of
    its five matrix rows depend on, or the script holding the assertions themselves. The AgentCore
    proof did not declare ``careloop/graph/verify.py``, whose ``OBSERVABLE_STATE`` decides
    ``careloop.effect.fields`` -- the exact tuple the frozen evaluator matches ``EXPECTED_FIELDS``
    against. A change there invalidates the proof and staled nothing.

    Each assertion below is one path and the answer ``git`` gives for it, so this stays a statement
    about coverage rather than a copy of the tuple next door.
    """

    def test_the_deployed_lock_does_not_depend_on_the_frontend(self) -> None:
        """The correction. The verifier opens no page and parses no bundle."""
        assert not surface_covers(
            surface_of("live.deployed-injection-lock"), "frontend/src/App.tsx"
        )

    def test_the_deployed_lock_depends_on_the_models_the_deployment_runs(self) -> None:
        assert surface_covers(surface_of("live.deployed-injection-lock"), "deploy/careloop.env")

    def test_the_deployed_lock_depends_on_the_assertions_it_makes(self) -> None:
        assert surface_covers(surface_of("live.deployed-injection-lock"), "scripts/verify_lock.py")

    def test_the_deployed_lock_still_depends_on_the_whole_application(self) -> None:
        """Not narrowed. It exercises triage, planning, policy, execution and verification."""
        surface = surface_of("live.deployed-injection-lock")

        assert surface_covers(surface, "careloop/policy/rules.yaml")
        assert surface_covers(surface, "demoworld/app.py")

    def test_the_agentcore_proof_depends_on_the_fields_the_evaluator_matches(self) -> None:
        """``OBSERVABLE_STATE`` is upstream of ``careloop.effect.fields``."""
        assert surface_covers(surface_of("live.agentcore-settlement"), "careloop/graph/verify.py")

    def test_the_agentcore_proof_is_not_widened_to_the_whole_application(self) -> None:
        """Narrow on purpose: a rule edit does not invalidate a measurement of trace delivery."""
        surface = surface_of("live.agentcore-settlement")

        assert surface_covers(surface, "evaluators/contract.py")
        assert not surface_covers(surface, "careloop/policy/rules.yaml")

    def test_the_object_store_surface_is_the_assertions_client_and_substitute(self) -> None:
        """Unchanged, and checked so that leaving it alone is a decision somebody can see."""
        surface = surface_of("live.objectstore-contract")

        assert surface_covers(surface, "scripts/objectstore_contract.py")
        assert surface_covers(surface, "careloop/tools/objectstore.py")
        assert surface_covers(surface, "objectstore/store.py")

    def test_the_identity_surface_still_covers_where_ownership_is_decided(self) -> None:
        """Unchanged too, and deliberately broad: ``deps.py`` holds the workspace-owner refusal
        and ``patient_authority_for``. It also holds the composition root, which is why an
        unrelated wiring commit staled this proof -- a truthful false positive, and better than a
        narrow surface that would let the ownership check move without anybody noticing."""
        surface = surface_of("live.production-identity")

        assert surface_covers(surface, "careloop/api/deps.py")
        assert surface_covers(surface, "careloop/identity/oidc.py")

    def test_the_detector_answers_no_for_something_genuinely_outside(self) -> None:
        """Vacuity: a detector that said yes to everything would satisfy every assertion above."""
        assert not surface_covers(surface_of("live.objectstore-contract"), "careloop/api/app.py")

    def test_every_declared_surface_still_matches_something(self) -> None:
        """A mistyped path digests an empty listing, which is a proof that never goes stale."""
        for claim in LIVE_CLAIMS:
            assert claim.artifact is not None
            assert surface_digest(claim.artifact.surface)


class TestRemediationPointsThroughTheRecorder:
    """What a ``NOT_RUN`` row tells an operator to do must be a thing that works.

    ``ArtifactRef.tool`` is not decoration: :func:`~careloopbench.ingest.ingest_one` interpolates
    it into the detail a reader of a ``NOT_RUN`` row sees, and the rendered page prints it in its
    own column. It used to read ``--out evidence/live/<proof>.json`` -- which was truthful while a
    record was hand-authored, and became wrong the moment ``careloopbench.record`` owned those
    paths. Followed literally it would have written raw tool output *over* an envelope
    destination, producing a file :func:`~careloopbench.ingest.read_envelope` refuses for missing
    every required field. A remediation hint that breaks the thing it is meant to repair is worse
    than no hint.

    So the rule is the narrow one: a tool string may name a scratch path and must never name the
    evidence directory, and anything that produces output has to route it through the recorder.
    ``live.production-identity`` is prose about a login somebody performed by hand and produces no
    file, which is why the second rule is scoped by ``--out`` rather than applied to all four.
    """

    def tool_driven(self) -> tuple[Claim, ...]:
        return tuple(
            claim
            for claim in LIVE_CLAIMS
            if claim.artifact is not None and "--out" in claim.artifact.tool
        )

    def test_no_remediation_writes_raw_output_into_the_evidence_directory(self) -> None:
        """The defect itself. These paths belong to the recorder and to nothing else."""
        for claim in LIVE_CLAIMS:
            assert claim.artifact is not None
            assert ARTIFACT_ROOT not in claim.artifact.tool, claim.artifact.proof_id

    def test_everything_that_produces_output_routes_it_through_the_recorder(self) -> None:
        for claim in self.tool_driven():
            assert claim.artifact is not None
            assert "careloopbench.record" in claim.artifact.tool, claim.artifact.proof_id
            assert "--raw" in claim.artifact.tool, claim.artifact.proof_id

    def test_the_recorder_is_named_with_the_proof_it_would_file(self) -> None:
        """``proof_id`` is the recorder's one positional argument, so a hint omitting it does
        not run."""
        for claim in self.tool_driven():
            assert claim.artifact is not None
            assert claim.artifact.proof_id in claim.artifact.tool

    def test_the_tool_driven_proofs_are_the_ones_that_produce_a_file(self) -> None:
        """Vacuity, and a decision point: an empty selection would satisfy all three above.

        Named rather than counted, so a fifth live proof has to be classified by somebody rather
        than quietly falling outside the rules. ``live.production-identity`` is deliberately not
        here -- a hosted-UI login writes nothing for a recorder to file.
        """
        assert {claim.artifact.proof_id for claim in self.tool_driven()} == {  # type: ignore[union-attr]
            "live.agentcore-settlement",
            "live.objectstore-contract",
            "live.deployed-injection-lock",
        }

    def test_the_hint_a_reader_of_an_unrun_row_actually_sees_names_the_recorder(self) -> None:
        """Asserted on the rendered detail, because that is the sentence somebody follows."""
        unrun = [row for row in live_results().values() if row.status is Status.NOT_RUN]

        assert unrun, "no row is NOT_RUN; this would pass vacuously"
        for row in unrun:
            assert "careloopbench.record" in row.detail
            assert ARTIFACT_ROOT not in row.detail

    def test_every_stored_record_is_still_one_the_reader_accepts(self) -> None:
        """The other half: correcting the hint must not have disturbed what is already filed."""
        stored = [
            claim
            for claim in LIVE_CLAIMS
            if claim.artifact is not None and (REPO_ROOT / claim.artifact.path).exists()
        ]

        assert stored, "no live proof is stored; this would pass vacuously"
        for claim in stored:
            assert ingest_one(claim).status in {Status.PROVEN_AT, Status.STALE}


class TestAMalformedClaimIsRefused:
    """Every shape the registry will not accept, and what each would otherwise have allowed."""

    def test_a_claim_id_that_is_not_dotted_and_lowercase(self) -> None:
        with pytest.raises(RegistryRefused, match="dotted lowercase"):
            check_registry((*one_per_domain(), a_claim(id="Authority Integrity")))

    def test_a_question_that_is_not_a_question(self) -> None:
        with pytest.raises(RegistryRefused, match="must be a question"):
            check_registry((*one_per_domain(), a_claim(id="authority.x", question="It holds.")))

    def test_a_claim_that_does_not_say_what_it_does_not_claim(self) -> None:
        with pytest.raises(RegistryRefused, match="does not claim"):
            check_registry((*one_per_domain(), a_claim(id="authority.x", scope="")))

    def test_an_executed_claim_with_no_evidence(self) -> None:
        """The one that matters most: an empty tuple would otherwise render PROVEN trivially."""
        with pytest.raises(RegistryRefused, match="needs evidence"):
            check_registry((*one_per_domain(), a_claim(id="authority.x", evidence=())))

    def test_a_node_id_that_is_not_a_test_class(self) -> None:
        with pytest.raises(RegistryRefused, match="TestClass id"):
            check_registry(
                (*one_per_domain(), a_claim(id="authority.x", evidence=("careloop/policy",)))
            )

    def test_a_node_id_at_function_granularity(self) -> None:
        """Class granularity only, so the JUnit mapping stays total. See the runner."""
        with pytest.raises(RegistryRefused, match="TestClass id"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(id="authority.x", evidence=(f"{NODE}::test_it_loads",)),
                )
            )

    def test_a_node_that_is_both_evidence_and_counter_evidence(self) -> None:
        with pytest.raises(RegistryRefused, match="both evidence and counter"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(id="authority.x", evidence=(NODE,), counter_evidence=(NODE,)),
                )
            )

    def test_a_duplicate_claim_id(self) -> None:
        with pytest.raises(RegistryRefused, match="duplicate claim id"):
            check_registry((*one_per_domain(), a_claim(), a_claim()))

    def test_a_domain_with_no_claims(self) -> None:
        """``0 / 0`` reads as success. A domain nobody declared must not render at all."""
        with pytest.raises(RegistryRefused, match="domains with no claims"):
            check_registry((a_claim(),))

    def test_an_empty_registry(self) -> None:
        with pytest.raises(RegistryRefused, match="registry is empty"):
            check_registry(())


class TestExecutableAndLiveEvidenceCannotBeMixed:
    """A live proof may not borrow the authority of something this invocation watched pass."""

    def a_ref(self, **overrides: object) -> ArtifactRef:
        fields: dict[str, object] = {
            "proof_id": "live.example",
            "path": "evidence/live/example.json",
            "tool": "scripts/example.py",
            "surface": ("careloop/",),
        }
        fields.update(overrides)
        return ArtifactRef(**fields)  # type: ignore[arg-type]

    def test_a_live_claim_is_accepted_when_it_runs_nothing(self) -> None:
        check_registry(
            (
                *one_per_domain(),
                a_claim(id="authority.live", level=LIVE, evidence=(), artifact=self.a_ref()),
            )
        )

    def test_a_live_claim_carrying_executable_evidence(self) -> None:
        with pytest.raises(RegistryRefused, match="runs nothing"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(
                        id="authority.live", level=LIVE, evidence=(NODE,), artifact=self.a_ref()
                    ),
                )
            )

    def test_a_live_level_with_no_artifact(self) -> None:
        with pytest.raises(RegistryRefused, match="needs a stored artifact"):
            check_registry((*one_per_domain(), a_claim(id="authority.live", level=LIVE)))

    def test_an_executed_level_carrying_an_artifact(self) -> None:
        with pytest.raises(RegistryRefused, match="may not carry an artifact"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(id="authority.live", level=PROP, evidence=(), artifact=self.a_ref()),
                )
            )

    def test_an_artifact_outside_the_one_directory(self) -> None:
        with pytest.raises(RegistryRefused, match="artifact lives under"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(
                        id="authority.live",
                        level=LIVE,
                        evidence=(),
                        artifact=self.a_ref(path="docs/example.json"),
                    ),
                )
            )

    def test_an_artifact_declaring_no_dependency_surface(self) -> None:
        """Without a surface there is nothing staleness could ever be computed against."""
        with pytest.raises(RegistryRefused, match="dependency surface"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(
                        id="authority.live",
                        level=LIVE,
                        evidence=(),
                        artifact=self.a_ref(surface=()),
                    ),
                )
            )

    def test_a_duplicate_proof_id(self) -> None:
        with pytest.raises(RegistryRefused, match="duplicate proof id"):
            check_registry(
                (
                    *one_per_domain(),
                    a_claim(id="authority.one", level=LIVE, evidence=(), artifact=self.a_ref()),
                    a_claim(id="authority.two", level=LIVE, evidence=(), artifact=self.a_ref()),
                )
            )


class TestTierSelection:
    """Which claims a tier covers, and which it never touches."""

    def test_core_is_a_superset_of_quick(self) -> None:
        assert TIERS["quick"] < TIERS["core"]

    def test_quick_leaves_the_real_protocol_claims_alone(self) -> None:
        assert ExecutionLevel.REAL_PROTOCOL not in TIERS["quick"]

    def test_a_live_claim_is_never_selected_for_execution(self) -> None:
        live = a_claim(
            id="authority.live",
            level=LIVE,
            evidence=(),
            artifact=ArtifactRef("live.x", "evidence/live/x.json", "scripts/x.py", ("careloop/",)),
        )

        for levels in TIERS.values():
            assert selected((*one_per_domain(), live), levels) == one_per_domain_at(levels)

    def test_evidence_nodes_are_deduplicated_and_ordered(self) -> None:
        other = "tests/test_careloopbench.py::TestTierSelection"
        claims = (
            a_claim(id="authority.one", evidence=(NODE, other)),
            a_claim(id="authority.two", evidence=(other, NODE)),
        )

        assert evidence_nodes(claims) == (NODE, other)

    def test_counter_evidence_is_part_of_what_must_run(self) -> None:
        claim = a_claim(
            evidence=(NODE,), counter_evidence=("tests/test_evidence.py::TestTheTimeline",)
        )

        assert claim.nodes == (NODE, "tests/test_evidence.py::TestTheTimeline")
        assert claim.mutation_backed


def one_per_domain_at(levels: frozenset[ExecutionLevel]) -> tuple[Claim, ...]:
    """The synthetic per-domain claims a tier covering ``levels`` would select."""
    return tuple(claim for claim in one_per_domain() if claim.level in levels)


class TestTheStatusVocabularyKeepsItsDistinctions:
    """Six statuses, and the folds that would each destroy a distinction a reader needs."""

    def test_the_six_are_all_distinct(self) -> None:
        assert len(set(Status)) == len(Status)

    def test_nothing_but_proven_is_a_pass(self) -> None:
        """Asserted here so a later renderer cannot quietly widen what counts."""
        from careloopbench.levels import PROVEN_STATUSES

        assert frozenset({Status.PROVEN}) == PROVEN_STATUSES
        for status in Status:
            if status is not Status.PROVEN:
                assert status not in PROVEN_STATUSES


class TestCareLoopBenchKnowsNothingAboutEnforcement:
    """The architectural guard: this layer reports on CareLoop; it does not re-implement it.

    A reporter that imported the policy engine in order to check an answer would be a second,
    weaker implementation of the thing it reports on — and the first time the two disagreed, the
    report would be the one nobody had tested. So the sweep is by AST rather than by review, and
    it is deliberately absolute: **no module here may import the runtime package at all.**
    """

    def imported_roots(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots.add(node.module.split(".")[0])
        return roots

    def test_the_sweep_has_modules_to_sweep(self) -> None:
        assert len(list(BENCH_ROOT.glob("*.py"))) >= MODULES_AT_LEAST

    def test_nothing_here_imports_the_careloop_runtime_package(self) -> None:
        for path in sorted(BENCH_ROOT.glob("*.py")):
            assert "careloop" not in self.imported_roots(path), path.name

    def test_the_sweep_would_notice(self) -> None:
        """Vacuity: the detector must fire on a module that really does import it."""
        offending = ast.parse("from careloop.policy.engine import PolicyEngine")
        roots = {
            node.module.split(".")[0]
            for node in ast.walk(offending)
            if isinstance(node, ast.ImportFrom) and node.module
        }

        assert "careloop" in roots


SYNTHETIC = """
import pytest


class TestItPasses:
    def test_one(self) -> None:
        assert True

    def test_two(self) -> None:
        assert True


class TestItFails:
    def test_one(self) -> None:
        assert False


class TestItErrors:
    def test_one(self, nothing_provides_this) -> None:
        assert True


class TestItSkips:
    @pytest.mark.skip(reason="deliberately")
    def test_one(self) -> None:
        assert True


class TestItHoldsNothing:
    pass
"""


@pytest.fixture
def synthetic_repo(tmp_path: Path) -> Path:
    """A throwaway tree whose tests do each of the things a benchmark must not call a pass."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_synthetic.py").write_text(SYNTHETIC, encoding="utf-8")
    return tmp_path


class TestOnlyAPassIsAPass:
    """The rule the whole layer rests on, exercised against tests that really do fail."""

    def test_the_junit_mapping_is_the_whole_of_it(self) -> None:
        assert junit_classname("tests/test_x.py::TestY") == "tests.test_x.TestY"

    def test_a_passing_class_passes(self, synthetic_repo: Path) -> None:
        run = run_evidence(("tests/test_synthetic.py::TestItPasses",), root=synthetic_repo)

        assert run.passed("tests/test_synthetic.py::TestItPasses")
        assert run.outcomes["tests/test_synthetic.py::TestItPasses"].tests == 2

    def test_a_failing_class_does_not(self, synthetic_repo: Path) -> None:
        node = "tests/test_synthetic.py::TestItFails"

        run = run_evidence((node,), root=synthetic_repo)

        assert not run.passed(node)
        assert run.outcomes[node].status == "failure"

    def test_an_erroring_class_does_not(self, synthetic_repo: Path) -> None:
        node = "tests/test_synthetic.py::TestItErrors"

        run = run_evidence((node,), root=synthetic_repo)

        assert not run.passed(node)
        assert run.outcomes[node].status == "error"

    def test_a_skipped_class_does_not(self, synthetic_repo: Path) -> None:
        """The one a reader assumes away. A skip is an absence of evidence, not evidence."""
        node = "tests/test_synthetic.py::TestItSkips"

        run = run_evidence((node,), root=synthetic_repo)

        assert not run.passed(node)
        assert run.outcomes[node].status == "skipped"

    def test_a_class_holding_no_tests_does_not(self, synthetic_repo: Path) -> None:
        node = "tests/test_synthetic.py::TestItHoldsNothing"

        with pytest.raises(EvidenceDoesNotCollect, match="TestItHoldsNothing"):
            run_evidence((node,), root=synthetic_repo)

    def test_one_run_answers_for_every_node_it_was_given(self, synthetic_repo: Path) -> None:
        nodes = (
            "tests/test_synthetic.py::TestItPasses",
            "tests/test_synthetic.py::TestItFails",
            "tests/test_synthetic.py::TestItSkips",
        )

        run = run_evidence(nodes, root=synthetic_repo)

        assert set(run.outcomes) == set(nodes)
        assert run.failing(nodes) == (
            "tests/test_synthetic.py::TestItFails (failure)",
            "tests/test_synthetic.py::TestItSkips (skipped)",
        )


class TestEvidenceThatDoesNotCollectIsRefusedBeforeAnythingRuns:
    """A renamed test must read as a broken pointer, not as a quietly smaller denominator."""

    def test_a_node_naming_a_class_that_does_not_exist(self, synthetic_repo: Path) -> None:
        node = "tests/test_synthetic.py::TestSomebodyRenamedThis"

        with pytest.raises(EvidenceDoesNotCollect, match="TestSomebodyRenamedThis"):
            run_evidence((node,), root=synthetic_repo)

    def test_a_node_naming_a_module_that_does_not_exist(self, synthetic_repo: Path) -> None:
        with pytest.raises(EvidenceDoesNotCollect, match="test_absent"):
            run_evidence(("tests/test_absent.py::TestX",), root=synthetic_repo)

    def test_an_empty_selection_is_refused_rather_than_reported_as_clean(self) -> None:
        with pytest.raises(EvidenceDoesNotCollect, match="nothing to run"):
            check_collects(())


class TestTheAdversarialContrastIsStructural:
    """The protected result and the mutant's result must be impossible to confuse."""

    def test_they_are_two_named_fields_rather_than_two_rows(self) -> None:
        """A list of two would let a renderer print them in whichever order it happened to hold."""
        fields = {field.name for field in dataclasses.fields(AdversarialEvidence)}

        assert fields == {"protected", "allow_all"}

    def test_the_label_comes_from_the_report_and_never_from_a_caller(self) -> None:
        """``replay_side`` takes no parameter naming the target. This is why."""

        class Report:
            def __init__(self, permissive: bool) -> None:
                self.permissive = permissive
                self.attempts = 1
                self.unauthorised_actions = 0
                self.attempts_that_reached_a_refusal = 1
                self.errored = 0
                self.outcomes = ()

        assert replay_side(Report(permissive=False)).system == PROTECTED
        assert replay_side(Report(permissive=True)).system == ALLOW_ALL

    def test_the_committed_corpus_still_inverts_when_the_rules_are_removed(self) -> None:
        """The measurement itself, re-derived rather than compared against a written-down number.

        Three assertions and no literal totals: the protected run must produce nothing, the mutant
        must produce something on every attempt it was given, and both must have been asked the
        same question. A corpus that grew would move every number here and break none of them.
        """
        evidence = run_adversarial_replay()

        assert evidence.protected.unauthorized == 0
        assert evidence.allow_all.unauthorized == evidence.allow_all.attempts
        assert evidence.protected.attempts == evidence.allow_all.attempts
        assert evidence.is_contrasting

    def test_the_protected_run_really_reached_the_gate(self) -> None:
        """Anti-vacuity: zero unauthorized actions and zero refusals would mean nothing ran."""
        evidence = run_adversarial_replay()

        assert evidence.protected.refused == evidence.protected.attempts
        assert evidence.protected.effects == 0
        assert evidence.protected.errored == 0


class TestTheRendererCarriesNoNumbersOfItsOwn:
    """Every count in both outputs is derived. This is what stops one being typed in.

    ``0`` and ``1`` are permitted because they are indices and emptiness, never a benchmark
    value; a formatting width lives in :mod:`careloopbench.levels` for the same reason. Anything
    else appearing as an integer literal in the renderer would be a number nobody re-derived,
    which is the hand-maintained ledger this layer exists to replace.
    """

    def integer_literals(self, path: Path) -> list[int]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            # bool is an int subclass; a flag is not a count.
            and not isinstance(node.value, bool)
        ]

    def test_the_renderer_holds_only_indices(self) -> None:
        assert set(self.integer_literals(BENCH_ROOT / "report.py")) <= {0, 1}

    def test_the_check_would_notice(self) -> None:
        """Vacuity: a module that really does carry a written-down total must be caught."""
        written = ast.parse("PROVEN = 61\n")
        found = [
            node.value
            for node in ast.walk(written)
            if isinstance(node, ast.Constant) and isinstance(node.value, int)
        ]

        assert not set(found) <= {0, 1}


def synthetic_result(*statuses: Status) -> BenchResult:
    """A result carrying one claim per given status, spread across the domains that exist."""
    rows = tuple(
        ClaimResult(a_claim(id=f"authority.s{index}", domain=Domain.AUTHORITY_INTEGRITY), status)
        for index, status in enumerate(statuses)
    )
    return BenchResult(
        tier="quick",
        head_commit="0" * 40,
        registry_digest="f" * 64,
        results=rows,
        adversarial=None,
        tests_run=len(rows),
    )


class TestNothingButProvenIsCounted:
    """The fold that would matter most, refused for each status in turn."""

    @pytest.mark.parametrize(
        "status",
        [Status.PROVEN_AT, Status.STALE, Status.NOT_RUN, Status.FAILED, Status.NOT_APPLICABLE],
    )
    def test_a_non_proven_status_is_in_the_denominator_and_not_the_numerator(
        self, status: Status
    ) -> None:
        payload = to_payload(synthetic_result(Status.PROVEN, status))
        authority = next(
            row for row in payload["domains"] if row["domain"] == Domain.AUTHORITY_INTEGRITY.value
        )

        assert authority["proven"] == 1
        assert authority["declared"] == 2
        assert authority["by_status"][status.value] == 1

    def test_every_status_present_is_reported_by_its_own_name(self) -> None:
        payload = to_payload(synthetic_result(Status.PROVEN, Status.STALE, Status.NOT_APPLICABLE))
        authority = next(
            row for row in payload["domains"] if row["domain"] == Domain.AUTHORITY_INTEGRITY.value
        )

        assert set(authority["by_status"]) == {
            Status.PROVEN.value,
            Status.STALE.value,
            Status.NOT_APPLICABLE.value,
        }

    def test_a_domain_with_nothing_in_it_is_still_declared(self) -> None:
        payload = to_payload(synthetic_result(Status.PROVEN))
        titles = {row["domain"] for row in payload["domains"]}

        assert titles == {domain.value for domain in Domain}


class TestFailingEvidenceCannotRenderAsProven:
    """The end-to-end version, over the synthetic tree whose tests really do fail."""

    def test_a_claim_whose_evidence_fails_names_the_node_and_is_not_proven(
        self, synthetic_repo: Path
    ) -> None:
        good = a_claim(id="authority.good", evidence=("tests/test_synthetic.py::TestItPasses",))
        bad = a_claim(id="authority.bad", evidence=("tests/test_synthetic.py::TestItFails",))
        claims = (*one_per_domain_without_authority(), good, bad)
        check_registry(claims)

        run = run_evidence(
            ("tests/test_synthetic.py::TestItPasses", "tests/test_synthetic.py::TestItFails"),
            root=synthetic_repo,
        )
        results = {row.claim.id: row for row in evaluate(claims, tier="quick", run=run)}

        assert results["authority.good"].status is Status.PROVEN
        assert results["authority.bad"].status is Status.FAILED
        assert "TestItFails" in results["authority.bad"].detail

    def test_a_claim_whose_counter_evidence_fails_is_not_proven_either(
        self, synthetic_repo: Path
    ) -> None:
        """A detector that stopped reproducing the old behaviour stops distinguishing anything."""
        claim = a_claim(
            id="authority.backed",
            evidence=("tests/test_synthetic.py::TestItPasses",),
            counter_evidence=("tests/test_synthetic.py::TestItFails",),
        )
        run = run_evidence(
            ("tests/test_synthetic.py::TestItPasses", "tests/test_synthetic.py::TestItFails"),
            root=synthetic_repo,
        )

        (result,) = evaluate((claim,), tier="quick", run=run)

        assert result.status is Status.FAILED


def one_per_domain_without_authority() -> tuple[Claim, ...]:
    """Domain coverage without occupying the authority ids the tests above use."""
    others = [claim for claim in one_per_domain() if claim.domain is not Domain.AUTHORITY_INTEGRITY]
    return (*others, a_claim(id="authorityintegrity.example"))


CANONICAL: Final[Path] = REPO_ROOT / "docs" / "evidence.json"
RENDERED: Final[Path] = REPO_ROOT / "docs" / "evidence.md"


class TestThePublishedPairAgreesWithItself:
    """``docs/evidence.md`` is a projection of ``docs/evidence.json`` and of nothing else.

    Both are committed, which is what makes this assertion cheap: it needs no pytest subprocess
    and no replay, so a broken projection is caught by the targeted gate rather than only by a
    full run. And because the canonical file carries the registry's digest, a claim edited without
    regenerating the pair is red here too — which is the only thing standing between "the registry
    changed" and "the published page quietly describes a different set of claims".
    """

    def payload(self) -> dict[str, object]:
        return json.loads(CANONICAL.read_text(encoding="utf-8"))

    def test_the_markdown_is_exactly_what_the_json_renders(self) -> None:
        assert render_markdown(self.payload()) == RENDERED.read_text(encoding="utf-8")

    def test_the_canonical_result_describes_the_registry_this_repository_declares(self) -> None:
        assert self.payload()["careloopbench"]["registry_digest"] == registry_digest(EVERY_CLAIM)

    def test_it_names_the_commit_it_was_generated_at_rather_than_certifying_this_one(self) -> None:
        """The pair is regenerated deliberately, so it always names an earlier commit than HEAD.

        That is the honest shape and the header says so in those words. What must never happen is
        the file implying it was re-derived against whatever a reader has checked out.
        """
        text = RENDERED.read_text(encoding="utf-8")

        assert "generated at commit" in text
        assert len(str(self.payload()["careloopbench"]["generated_at_commit"])) > MODULES_AT_LEAST

    def test_every_declared_claim_has_a_row(self) -> None:
        rows = {row["id"] for row in self.payload()["claims"]}  # type: ignore[union-attr]

        assert rows == {claim.id for claim in EVERY_CLAIM}

    def test_every_live_row_renders_the_surface_the_registry_declares(self) -> None:
        """The one part of a live row the registry digest does not cover.

        ``registry_digest`` hashes each claim's id, domain, level, question, scope, evidence and
        proof id -- not its dependency surface, and not the path or tool beside it. The rendered
        page shows all three, so correcting a surface changes what a reader is told while leaving
        the digest, and therefore the sibling assertion above, perfectly green. Found by making
        exactly that edit and watching nothing go red.

        Asserted here rather than folded into the digest: what the digest covers is a decision
        about the anti-drift mechanism, and this is the narrower true statement -- the published
        pair must describe the surfaces this repository declares today.
        """
        declared = {
            claim.artifact.proof_id: {
                "path": claim.artifact.path,
                "tool": claim.artifact.tool,
                "surface": list(claim.artifact.surface),
            }
            for claim in EVERY_CLAIM
            if claim.artifact is not None
        }
        rendered = {
            row["artifact"]["proof_id"]: {  # type: ignore[index]
                "path": row["artifact"]["path"],  # type: ignore[index]
                "tool": row["artifact"]["tool"],  # type: ignore[index]
                "surface": row["artifact"]["surface"],  # type: ignore[index]
            }
            for row in self.payload()["claims"]  # type: ignore[union-attr]
            if row["artifact"]  # type: ignore[index]
        }

        assert rendered == declared

    def test_there_are_live_rows_to_compare(self) -> None:
        """Vacuity: two empty mappings agree, and would say nothing about anything."""
        assert [row for row in self.payload()["claims"] if row["artifact"]]  # type: ignore[index]


def a_ref(**overrides: object) -> ArtifactRef:
    """A live proof reference pointing at the synthetic repository below."""
    fields: dict[str, object] = {
        "proof_id": "live.example",
        "path": "evidence/live/example.json",
        "tool": "scripts/example.py",
        "surface": ("watched.txt",),
    }
    fields.update(overrides)
    return ArtifactRef(**fields)  # type: ignore[arg-type]


def an_envelope(digest: str, **overrides: object) -> dict[str, object]:
    """A well-formed stored proof, so each test below changes exactly one thing."""
    envelope: dict[str, object] = {
        "proof_id": "live.example",
        "tool": "scripts/example.py",
        "attested_commit": "a" * COMMIT_CHARACTERS,
        "attestation": OPERATOR,
        "ingested_at_commit": "b" * COMMIT_CHARACTERS,
        "produced_at": "2026-08-25",
        "environment": "a synthetic one",
        "surface_digest": digest,
        "raw": {"result": "PASS"},
    }
    envelope.update(overrides)
    return envelope


GIT = ["git", "-c", "user.email=nobody@example.invalid", "-c", "user.name=nobody"]


@pytest.fixture
def a_repository(tmp_path: Path) -> Path:
    """A real one-commit repository, so the digest is taken over really committed bytes."""
    (tmp_path / "watched.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "evidence" / "live").mkdir(parents=True)
    subprocess.run([*GIT, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*GIT, "add", "watched.txt"], cwd=tmp_path, check=True)
    subprocess.run([*GIT, "commit", "-qm", "one"], cwd=tmp_path, check=True)
    return tmp_path


def store(root: Path, envelope: Mapping[str, object]) -> None:
    path = root / "evidence" / "live" / "example.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")


def move_the_surface(root: Path) -> None:
    (root / "watched.txt").write_text("after\n", encoding="utf-8")
    subprocess.run([*GIT, "add", "watched.txt"], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-qm", "two"], cwd=root, check=True)


def a_live_claim() -> Claim:
    return Claim(
        id="authority.live",
        question="Was it measured?",
        domain=Domain.AUTHORITY_INTEGRITY,
        level=ExecutionLevel.LIVE_PROVIDER,
        artifact=a_ref(),
        scope="The one measurement it records.",
    )


class TestAStoredProofCertifiesItsOwnCommit:
    """Three outcomes and no fourth. None of them is ``PROVEN``."""

    def digest(self, root: Path) -> str:
        return surface_digest(("watched.txt",), root=root)

    def test_a_matching_surface_is_proven_at_the_commit_it_names(self, a_repository: Path) -> None:
        store(a_repository, an_envelope(self.digest(a_repository)))

        result = ingest_one(a_live_claim(), root=a_repository)

        assert result.status is Status.PROVEN_AT
        assert result.detail.startswith("proven at aaa")
        assert "operator-attested" in result.detail

    def test_a_moved_surface_is_stale_and_names_what_moved(self, a_repository: Path) -> None:
        store(a_repository, an_envelope(self.digest(a_repository)))
        move_the_surface(a_repository)

        result = ingest_one(a_live_claim(), root=a_repository)

        assert result.status is Status.STALE
        assert "watched.txt" in result.detail

    def test_a_stale_proof_is_not_a_pass(self, a_repository: Path) -> None:
        store(a_repository, an_envelope(self.digest(a_repository)))
        move_the_surface(a_repository)

        assert not ingest_one(a_live_claim(), root=a_repository).is_proven

    def test_a_proof_whose_surface_still_matches_is_still_not_a_pass(
        self, a_repository: Path
    ) -> None:
        """``PROVEN_AT`` never becomes ``PROVEN``. This invocation watched nothing happen."""
        store(a_repository, an_envelope(self.digest(a_repository)))

        result = ingest_one(a_live_claim(), root=a_repository)

        assert result.status is not Status.PROVEN
        assert not result.is_proven

    def test_a_missing_artifact_is_not_run_and_names_the_tool(self, a_repository: Path) -> None:
        result = ingest_one(a_live_claim(), root=a_repository)

        assert result.status is Status.NOT_RUN
        assert "scripts/example.py" in result.detail

    def test_a_surface_matching_nothing_is_refused(self, a_repository: Path) -> None:
        """Two empty surfaces hash alike, so a mistyped path would never go stale."""
        with pytest.raises(ArtifactRefused, match="matches nothing"):
            surface_digest(("no/such/path",), root=a_repository)

    def test_the_digest_follows_the_commit_and_not_the_working_tree(
        self, a_repository: Path
    ) -> None:
        """An uncommitted edit is not a reason to call somebody else's measurement stale."""
        before = self.digest(a_repository)
        (a_repository / "watched.txt").write_text("edited but not committed\n", encoding="utf-8")

        assert self.digest(a_repository) == before


class TestAMalformedStoredProofIsRefused:
    """Fail closed: a record CareLoopBench cannot read is never a row it renders."""

    def digest(self, root: Path) -> str:
        return surface_digest(("watched.txt",), root=root)

    @pytest.mark.parametrize("field", REQUIRED)
    def test_every_declared_field_is_required(self, a_repository: Path, field: str) -> None:
        envelope = an_envelope(self.digest(a_repository))
        del envelope[field]
        store(a_repository, envelope)

        with pytest.raises(ArtifactRefused, match=field):
            ingest_one(a_live_claim(), root=a_repository)

    def test_an_unknown_attestation(self, a_repository: Path) -> None:
        store(a_repository, an_envelope(self.digest(a_repository), attestation="TRUST_ME"))

        with pytest.raises(ArtifactRefused, match="attestation"):
            ingest_one(a_live_claim(), root=a_repository)

    def test_a_proof_naming_no_commit(self, a_repository: Path) -> None:
        store(a_repository, an_envelope(self.digest(a_repository), attested_commit="  "))

        with pytest.raises(ArtifactRefused, match="certifies nothing"):
            ingest_one(a_live_claim(), root=a_repository)

    def test_a_record_whose_proof_id_is_not_the_declared_one(self, a_repository: Path) -> None:
        store(a_repository, an_envelope(self.digest(a_repository), proof_id="live.something-else"))

        with pytest.raises(ArtifactRefused, match="the registry declares"):
            ingest_one(a_live_claim(), root=a_repository)

    def test_a_record_carrying_something_shaped_like_a_token(self, a_repository: Path) -> None:
        envelope = an_envelope(self.digest(a_repository))
        envelope["raw"] = {"header": "eyJhbGciOi.eyJzdWIiOi.signature"}
        store(a_repository, envelope)

        with pytest.raises(ArtifactRefused, match="token"):
            ingest_one(a_live_claim(), root=a_repository)

    @pytest.mark.parametrize("field", ["access_token", "id_token", "sub", "client_secret"])
    def test_a_record_carrying_a_credential_shaped_field(
        self, a_repository: Path, field: str
    ) -> None:
        envelope = an_envelope(self.digest(a_repository))
        envelope["raw"] = {"observed": {field: "whatever"}}
        store(a_repository, envelope)

        with pytest.raises(ArtifactRefused, match=field):
            ingest_one(a_live_claim(), root=a_repository)

    def test_a_record_that_is_not_json(self, a_repository: Path) -> None:
        path = a_repository / "evidence" / "live" / "example.json"
        path.write_text("not json at all", encoding="utf-8")

        with pytest.raises(ArtifactRefused, match="not JSON"):
            ingest_one(a_live_claim(), root=a_repository)


class TestTheBenchmarkDoesNotShipWithTheProduct:
    """CareLoopBench is a measuring instrument, and instruments stay in the repository.

    ``bench/``, ``evals/``, ``evaluators/`` and ``demoworld/`` are all outside the distribution for
    the same reason: nothing a deployed process runs should carry the thing that measures it. This
    package is one character away from being the exception, because ``include = ["careloop*"]``
    matches ``careloopbench`` — which it did, until this test was written.
    """

    def find_configuration(self) -> dict[str, list[str]]:
        packaging = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return packaging["tool"]["setuptools"]["packages"]["find"]

    def packaged(self, name: str) -> bool:
        """Whether setuptools' package discovery would include ``name``, by its own rules."""
        found = self.find_configuration()
        included = any(fnmatch.fnmatchcase(name, pattern) for pattern in found.get("include", []))
        excluded = any(fnmatch.fnmatchcase(name, pattern) for pattern in found.get("exclude", []))
        return included and not excluded

    def test_the_runtime_package_and_its_subpackages_ship(self) -> None:
        assert self.packaged("careloop")
        assert self.packaged("careloop.api")
        assert self.packaged("careloop.policy")

    def test_this_package_does_not(self) -> None:
        assert not self.packaged("careloopbench")
        assert not self.packaged("careloopbench.claims")

    def test_no_other_measuring_instrument_ships_either(self) -> None:
        for instrument in ("bench", "evals", "evaluators", "demoworld", "objectstore", "scripts"):
            assert not self.packaged(instrument), instrument
