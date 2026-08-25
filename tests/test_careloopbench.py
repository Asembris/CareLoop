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
import re
from pathlib import Path
from typing import Final

import pytest

from careloopbench.claims import (
    CLAIMS,
    ArtifactRef,
    Claim,
    RegistryRefused,
    check_registry,
    claims_in,
    evidence_nodes,
    selected,
)
from careloopbench.levels import TIERS, Domain, ExecutionLevel, Status
from careloopbench.runner import (
    EvidenceDoesNotCollect,
    check_collects,
    junit_classname,
    run_evidence,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BENCH_ROOT: Final[Path] = REPO_ROOT / "careloopbench"

PROP: Final = ExecutionLevel.PROPERTY
LIVE: Final = ExecutionLevel.LIVE_PROVIDER

NODE: Final[str] = "tests/test_careloopbench.py::TestTheShippedRegistryIsWellFormed"

#: The floor the import sweep must clear before its "nothing imports it" means anything.
MODULES_AT_LEAST: Final[int] = 3


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
        check_registry(CLAIMS)

    def test_every_domain_carries_claims(self) -> None:
        for domain in Domain:
            assert claims_in(domain, CLAIMS), domain

    def test_every_claim_id_is_unique(self) -> None:
        ids = [claim.id for claim in CLAIMS]

        assert len(ids) == len(set(ids))

    def test_every_declared_node_names_a_class_that_exists(self) -> None:
        """A cheap typo guard that needs no subprocess.

        The runner proves the stronger property — that every node id *collects* — but it costs a
        pytest process. This one costs a file read, so a mistyped class name is caught by the
        registry's own targeted gate rather than only by a full evidence run.
        """
        for claim in CLAIMS:
            for node in claim.nodes:
                path, _, klass = node.partition("::")
                source = (REPO_ROOT / path).read_text(encoding="utf-8")
                assert re.search(rf"^class {klass}\b", source, re.MULTILINE), node

    def test_no_claim_is_declared_at_a_level_nothing_can_run_or_ingest(self) -> None:
        for claim in CLAIMS:
            assert claim.level in ExecutionLevel


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
