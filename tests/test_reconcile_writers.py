"""Who may re-claim a prepared action, and who may not.

``WAITING_EXTERNAL → EXECUTING`` is a legal transition, and legality is the smaller half of what
makes it safe. ``EXECUTING`` is a state from which the shipped rules permit an external effect to be
dispatched, so anything that can write that transition can put a case back in front of the executor.
The larger half is **who can make the move**: exactly one node, on exactly one condition, for
exactly the action the case already froze.

This is the arrangement ``tests/test_reopen.py`` uses for the one edge out of ``DONE`` and
``tests/test_recheck_writers.py`` uses for the caregiver's re-entry into verification. It is here
for the same reason and against the same failure: the transition table says the move is permitted,
and a sweep says nothing else in the package can perform it.

**Three properties, deliberately separate**, because they fail differently.

:class:`TestOnlyReconciliationReClaimsAnAttempt` is the writer sweep — no module outside
``graph/nodes.py`` may transition a case to ``EXECUTING``, and within that module the writes are
counted so a third would be visible.

:class:`TestRecoveryNeverAsksAModelAnything` is the sweep that matters most. Recovery operating from
durable facts is the whole claim; a reconciliation that called the planner would be inventing an
action to explain one it had lost, which is the opposite. It is asserted structurally here and
behaviourally in ``tests/test_saga_recovery.py``.

:class:`TestTheReplayIdentityHasNoOtherSource` pins that nothing in the recovery path can mint an
identity: the key comes from the case's own preparation, and there is no parameter, constructor
argument or helper anywhere through which a caller could supply a different one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import careloop
from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass, may_replay
from careloop.state.case import CaseState
from careloop.state.machine import TRANSITIONS, is_legal

#: Names that mean a model was consulted. None may appear in the recovery path.
#:
#: ``propose`` and ``classify`` are the two seams themselves. ``PlannerAgent`` and ``TriageAgent``
#: are the objects that own them, listed because importing one is already deciding you might call
#: it. ``proposal_tool_call`` is the bounded-retry helper underneath ``propose``, listed so the
#: sweep is not defeated by reaching past the agent.
MODEL_NAMES = frozenset(
    {"propose", "classify", "PlannerAgent", "TriageAgent", "proposal_tool_call"}
)


def package_root() -> Path:
    return Path(careloop.__file__).parent


def names_used(source: str) -> set[str]:
    """Every bare name, attribute and imported name this module mentions."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
    return found


def _enclosing(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the class that contains it, or to the top-level function if there is none.

    Classes first, so a method reports its class rather than its own name; module-level functions
    then fill in for the writers that deliberately live outside a node — a shared decision one
    function makes for two callers is the strongest form of "one writer", and a matcher that could
    only see classes would report it as ``module`` and hide which one.
    """
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)
    return enclosing


def transitions_to(source: str, state: str) -> list[str]:
    """Every ``transition(..., CaseState.<state>, ...)`` call, by enclosing class name."""
    tree = ast.parse(source)
    enclosing = _enclosing(tree)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if callee != "transition":
            continue
        for argument in node.args[1:]:
            if isinstance(argument, ast.Attribute) and argument.attr == state:
                found.append(enclosing.get(node, "module"))
    return found


def reconcile_source() -> str:
    """``ReconcileNode`` and its park writer, isolated from the rest of ``nodes.py``.

    The sweep has to run over the recovery path rather than over the whole module, because
    ``nodes.py`` legitimately contains the plan node and therefore legitimately names the planner.
    Extracted by parsing rather than by slicing text, so a rename or a reorder cannot silently make
    the sweep read the wrong thing.
    """
    source = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    pieces = [
        ast.get_source_segment(source, node)
        for node in tree.body
        if (isinstance(node, ast.ClassDef) and node.name == "ReconcileNode")
        or (isinstance(node, ast.FunctionDef) and node.name == "_park_with_nothing_established")
    ]
    assert len(pieces) == 2, "the recovery path is not where this sweep thinks it is"
    return "\n".join(piece for piece in pieces if piece)


class TestOnlyReconciliationReClaimsAnAttempt:
    """The writer sweep for ``EXECUTING``, which is the state an effect may be dispatched from."""

    def test_nodes_is_the_only_module_that_writes_executing(self) -> None:
        writers = {
            str(path.relative_to(package_root())).replace("\\", "/")
            for path in package_root().rglob("*.py")
            if transitions_to(path.read_text(encoding="utf-8"), "EXECUTING")
        }

        assert writers == {"graph/nodes.py"}

    def test_exactly_two_nodes_write_it_and_they_are_the_expected_two(self) -> None:
        """``ExecuteNode`` prepares a fresh attempt; ``ReconcileNode`` re-claims a frozen one.

        ``ExecuteNode`` writes it twice — once for the ordinary dispatch and once inside the
        duplicate-suppression branch, which passes through ``EXECUTING`` in the same commit rather
        than growing a ``READY_TO_EXECUTE → VERIFYING`` edge. ``ReconcileNode`` writes it once.
        """
        nodes = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")

        assert transitions_to(nodes, "EXECUTING") == [
            "ExecuteNode",
            "ExecuteNode",
            "ReconcileNode",
        ]

    def test_one_function_writes_waiting_external_and_two_nodes_reach_it(self) -> None:
        """One producer, so the state means one thing.

        It had none at all before the saga, which is what let four legal edges out of it rot —
        including the one to ``PLANNING`` that would have replanned on top of a dispatched effect.

        The writer is a module-level function rather than a node, because two nodes reach one
        fork: ``ExecuteNode`` when a first attempt's response never comes back, and
        ``ReconcileNode`` when a crash left the outcome unknown. The situation is identical in
        both, so writing the decision twice would be two chances to answer it differently.
        """
        nodes = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")
        writers = {
            str(path.relative_to(package_root())).replace("\\", "/")
            for path in package_root().rglob("*.py")
            if transitions_to(path.read_text(encoding="utf-8"), "WAITING_EXTERNAL")
        }

        assert writers == {"graph/nodes.py"}
        assert transitions_to(nodes, "WAITING_EXTERNAL") == ["_stop_with_the_outcome_unestablished"]

    def test_both_nodes_route_an_unestablished_outcome_to_that_one_function(self) -> None:
        """The counterpart: the shared decision really is shared, not merely available."""
        nodes = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")
        tree = ast.parse(nodes)
        enclosing = _enclosing(tree)
        callers = {
            enclosing.get(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_stop_with_the_outcome_unestablished"
        }

        assert callers == {"ExecuteNode", "ReconcileNode"}

    def test_the_matcher_finds_what_it_claims_to(self) -> None:
        """Vacuity: a matcher that found nothing would make every assertion above pass."""
        source = (
            "class N:\n"
            "    def run(self):\n"
            "        case = transition(case, CaseState.EXECUTING, reason='r', actor='a')\n"
        )

        assert transitions_to(source, "EXECUTING") == ["N"]
        assert transitions_to(source, "WAITING_EXTERNAL") == []
        module_level = "def f():\n    transition(case, CaseState.WAITING_EXTERNAL)\n"
        assert transitions_to(module_level, "WAITING_EXTERNAL") == ["f"]


class TestRecoveryNeverAsksAModelAnything:
    """The structural half of the zero-model claim. The behavioural half runs the graph."""

    @pytest.mark.parametrize("name", sorted(MODEL_NAMES))
    def test_the_recovery_path_does_not_name_a_model_seam(self, name: str) -> None:
        assert name not in names_used(reconcile_source()), (
            f"the recovery path names {name}; a reconciliation that consulted a model would be "
            f"inventing an action to explain one it had lost"
        )

    def test_the_recovery_module_does_not_either(self) -> None:
        recovery = (package_root() / "graph" / "recovery.py").read_text(encoding="utf-8")

        assert not (MODEL_NAMES & names_used(recovery))

    def test_the_sweep_reads_the_recovery_path_and_not_something_else(self) -> None:
        """Vacuity: an empty or wrongly-targeted extraction passes every assertion above."""
        source = reconcile_source()

        assert "class ReconcileNode" in source
        assert "_park_with_nothing_established" in source
        assert "unsettled_preparation" in source
        assert len(source) > 2000

    def test_the_sweep_would_catch_a_call_however_it_is_spelled(self) -> None:
        assert "propose" in names_used("await planner.propose(case)")
        assert "propose" in names_used("from careloop.agents.planner import propose")
        assert "classify" in names_used("classify(text)")

    def test_the_plan_node_does_name_the_planner(self) -> None:
        """The counterpart. Without it the sweep would pass on a system with no planner at all."""
        nodes = (package_root() / "graph" / "nodes.py").read_text(encoding="utf-8")

        assert "propose" in names_used(nodes)


class TestTheReplayIdentityHasNoOtherSource:
    """Nothing in the recovery path can mint or be handed an identity."""

    def test_reconciliation_derives_the_key_from_the_case_alone(self) -> None:
        source = reconcile_source()

        assert "unsettled_preparation" in source
        assert "latest_proposal" in source
        assert "idempotency_key" in source

    def test_reconciliation_never_constructs_an_action_record_itself(self) -> None:
        """It calls ``_record_action``, the one writer, rather than building a record of its own."""
        source = reconcile_source()

        assert "_record_action" in source
        assert "ActionRecord(" not in source

    def test_reconciliation_raises_no_approval(self) -> None:
        """A recovery recovers knowledge. Nothing about it authorises anything."""
        source = reconcile_source()

        assert "Approval(" not in source
        assert "_request_approval" not in source

    def test_the_run_method_takes_no_arguments(self) -> None:
        """The client trust boundary, as a fact about the signature.

        There is nowhere for a tool, an argument map, a digest, a confirmation id or an action index
        to arrive. A caller cannot aim reconciliation at anything, because it accepts nothing.
        """
        import inspect

        from careloop.graph.nodes import ReconcileNode

        parameters = inspect.signature(ReconcileNode.run).parameters

        assert list(parameters) == ["self"]


class TestTheRecoveryClassesAreDeclaredAndCovered:
    """The provider capability table, and the honest shape of it."""

    def test_every_effect_tool_has_a_class(self) -> None:
        from careloop.tools.registry import REGISTRY

        assert frozenset(RECOVERY_CLASS) == REGISTRY.external_effect_tools

    def test_exactly_the_two_measured_providers_are_replayable(self) -> None:
        """An exact set, so widening it is a deliberate edit with a measurement behind it.

        Both entries were measured against their own provider before being written, and the two
        contracts have almost nothing in common: the clinic replays the original record under the
        same key, while the object store refuses a duplicate outright and CareLoop settles the
        outcome with a fresh read. That is why :class:`RecoveryClass` is defined by the outcome it
        guarantees rather than by a mechanism.
        """
        replayable = {
            name
            for name, klass in RECOVERY_CLASS.items()
            if klass is RecoveryClass.IDEMPOTENT_REPLAYABLE
        }

        assert replayable == {"book_appointment", "upload_document"}

    def test_the_simulated_effects_are_still_refused(self) -> None:
        """The other half. Nothing that reaches no real provider became replayable."""
        assert RECOVERY_CLASS["send_email"] is RecoveryClass.NOT_REPLAYABLE
        assert RECOVERY_CLASS["grant_portal_access"] is RecoveryClass.NOT_REPLAYABLE

    def test_a_replay_needs_both_a_class_and_an_observer(self) -> None:
        """Neither condition alone. A replay nothing can read back certifies on its own receipt."""
        assert may_replay("book_appointment", observers={"book_appointment"})
        assert not may_replay("book_appointment", observers=())
        assert not may_replay("send_email", observers={"send_email"})

    def test_an_unknown_action_type_may_not_be_replayed(self) -> None:
        """Fail closed. Unreachable while the import-time coverage check holds."""
        assert not may_replay("teleport_patient", observers={"teleport_patient"})


class TestTheTransitionIsLegalAndNarrow:
    """The table half, pinned so a further target is a deliberate edit."""

    def test_waiting_external_can_reclaim(self) -> None:
        assert is_legal(CaseState.WAITING_EXTERNAL, CaseState.EXECUTING)

    def test_and_nothing_else_was_opened_up(self) -> None:
        assert TRANSITIONS[CaseState.WAITING_EXTERNAL] == frozenset(
            {CaseState.EXECUTING, CaseState.AWAITING_HUMAN, CaseState.FAILED}
        )

    def test_done_is_still_reachable_only_from_verifying(self) -> None:
        """Invariant 4, untouched: a recovered case finishes through the same one door."""
        sources = [state for state, targets in TRANSITIONS.items() if CaseState.DONE in targets]

        assert sources == [CaseState.VERIFYING]
