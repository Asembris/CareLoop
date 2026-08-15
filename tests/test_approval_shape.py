"""One writer of ``Approval``, and why that is a structural test rather than a convention.

An :class:`~careloop.state.case.Approval` is a **capability**. It records that a caregiver was
asked about one exact call, identified by the digest of its arguments, and
:func:`careloop.graph.runner._decide` releases the case when a decision arrives. Everything
downstream — ``pending_approval()``, the execute node's outstanding-decision guard, the ``Needs
You`` card, the ``args_approved`` rule clause — reads a pending one as *a decision waiting to be
made*, because that is the only thing it has ever meant.

Manual handoffs were written as approvals anyway, with ``tool_name`` and ``args_digest`` both
empty, and the consequence was not cosmetic: approving one moved the case to ``READY_TO_EXECUTE``,
where the execute node picked up the proposal policy had just **denied**. The intent was recorded
correctly in a docstring — *"approving it releases nothing on its own"* — and the code did the
opposite. A docstring is not an enforcement point.

So the guard is: **the only place in ``careloop/`` that constructs an ``Approval`` is the one that
has a proposal in hand.** A second construction site is how the next notification, receipt or
reminder gets encoded as an approval, and this test is what makes that a failing build rather than
a discovery six sessions later.

Two things this deliberately does not cover, both stated rather than left ambiguous:

* ``model_copy`` on an existing approval, which is how ``_decide`` records the outcome. That
  changes a decision's *status*; it cannot bring a new capability into being.
* Test and script code. A test that hand-builds a historical record to prove the guard refuses it —
  ``tests/test_no_safe_action.py`` does exactly that — is not a production writer, and a sweep that
  forbade it would forbid its own evidence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from careloop.state.case import Approval

#: The one production construction site: module path, then the function that owns it.
#:
#: ``_request_approval`` is reached only from the gate node's ``REQUIRE_HUMAN`` branch, with the
#: proposal it has just read, so both identifying fields are populated from that proposal and
#: neither can be empty for a registry tool.
THE_ONLY_WRITER = {("careloop/graph/nodes.py", "_request_approval")}

PACKAGE = Path(__file__).resolve().parent.parent / "careloop"


def construction_sites(source: str) -> set[str]:
    """Every function in ``source`` that calls ``Approval(...)``.

    Matched on the callee's name, so ``Approval(...)`` and ``case.Approval(...)`` both count and a
    variable that merely *holds* one does not. ``module`` is reported for a call at module scope,
    which is a site like any other and must not be able to hide by being outside a function.
    """
    tree = ast.parse(source)
    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)

    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = (
            callee.id
            if isinstance(callee, ast.Name)
            else callee.attr
            if isinstance(callee, ast.Attribute)
            else ""
        )
        if name == "Approval":
            found.add(owner.get(node, "module"))
    return found


def production_sites() -> set[tuple[str, str]]:
    """Every ``(module, function)`` under ``careloop/`` that constructs an ``Approval``."""
    return {
        (path.relative_to(PACKAGE.parent).as_posix(), function)
        for path in PACKAGE.rglob("*.py")
        for function in construction_sites(path.read_text(encoding="utf-8"))
    }


class TestOnlyOnePlaceCreatesAnApproval:
    def test_the_writer_is_the_one_that_has_a_proposal(self) -> None:
        assert production_sites() == THE_ONLY_WRITER

    def test_the_sweep_finds_something(self) -> None:
        """Vacuity: a matcher that found nothing would pass the test above by accident."""
        assert production_sites()

    def test_a_second_writer_would_be_caught(self) -> None:
        """Vacuity, the other way: the matcher must actually see a new site.

        Written against source text rather than by adding one to the package, so the proof does not
        depend on someone remembering to take the mutant back out.
        """
        smuggled = (
            "from careloop.state.case import Approval\n"
            "def notify(case):\n"
            "    return Approval(approval_id='x', tool_name='', args_digest='',\n"
            "                    reason_code='NO_SAFE_ACTION_AVAILABLE')\n"
        )

        assert construction_sites(smuggled) == {"notify"}

    def test_a_writer_hiding_at_module_scope_would_be_caught_too(self) -> None:
        assert construction_sites("from x import Approval\nSPARE = Approval()\n") == {"module"}


class TestWhatMakesAnApprovalApprovable:
    """``is_actionable``, which is what ``_decide`` reads before it moves anything."""

    @pytest.mark.parametrize(
        ("tool_name", "args_digest"),
        [("", ""), ("send_email", ""), ("", "d4f0")],
    )
    def test_a_record_missing_either_half_is_not_actionable(
        self, tool_name: str, args_digest: str
    ) -> None:
        """Both halves, and neither is redundant.

        The digest is what ``Case.is_approved`` matches a call against; the tool name is what the
        audit trail and the caregiver's card are written against. A record with one and not the
        other could not release a call *or* explain itself.
        """
        approval = Approval(
            approval_id="apr-x",
            tool_name=tool_name,
            args_digest=args_digest,
            reason_code="REQUIRES_HUMAN_APPROVAL",
        )

        assert not approval.is_actionable

    def test_a_real_approval_is_actionable(self) -> None:
        """Vacuity: the predicate must not simply always be false."""
        approval = Approval(
            approval_id="apr-x",
            tool_name="send_email",
            args_digest="d4f0",
            reason_code="REQUIRES_HUMAN_APPROVAL",
        )

        assert approval.is_actionable
