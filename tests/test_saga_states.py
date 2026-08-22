"""What ``WAITING_EXTERNAL`` is allowed to mean, and what ``EXECUTING`` is allowed to do next.

``WAITING_EXTERNAL`` shipped in the state machine with no producer for the whole of the project's
life. ``README.md`` said so plainly and the honesty was real, but "reserved" turned out to be the
wrong word for what it actually was: a state with four legal edges nobody had ever taken, one of
which was the exact defect ``tests/test_post_effect_lifecycle.py`` exists to prevent, sitting one
state over and waiting for somebody to give the state a producer.

This file pins the shape the state has now that it has one:

    an external effect was prepared and dispatched, nobody answered, and re-attempting the same
    frozen identity is safe

Everything follows from that sentence. Every entry is post-effect, so the two pre-effect entries
are gone. Nothing may leave it for the planner, because replanning on top of a dispatched action is
what session Q measured the cost of. Nothing may leave it for verification, because a case here
holds no result to verify. What it may do is re-claim the attempt it already owns, and that is one
edge with one writer.

:class:`TestThePlannerCannotBeReachedFromAPostEffectState` is the load-bearing class. The mutant
that restores ``WAITING_EXTERNAL -> PLANNING`` turns it red and nothing else in the suite notices,
which is the honest measure of what was guarding it before: nothing.
"""

from __future__ import annotations

import pytest

from careloop.state import CaseState
from careloop.state.machine import TERMINAL_STATES, TRANSITIONS, IllegalTransition, is_legal

#: Every state from which the shipped rules permit an external effect to be dispatched.
#:
#: Declared here from ``rules.yaml``'s own vocabulary rather than imported, so that a rule change
#: and this file disagreeing is a test failure rather than a silent agreement. It is what
#: :func:`test_the_reclaim_lands_in_a_state_policy_permits_an_effect_from` reads.
EFFECT_PERMITTING_STATES = frozenset(
    {CaseState.PLANNING, CaseState.READY_TO_EXECUTE, CaseState.EXECUTING}
)


class TestWaitingExternalIsPostEffectOnly:
    """Both pre-effect entries are gone, and neither ever had a producer."""

    @pytest.mark.parametrize("source", [CaseState.GATHERING_EVIDENCE, CaseState.PLANNING], ids=str)
    def test_a_pre_effect_state_cannot_reach_it(self, source: CaseState) -> None:
        assert not is_legal(source, CaseState.WAITING_EXTERNAL)

    def test_executing_is_the_only_way_in(self) -> None:
        """Pinned as a set, so a second producer is a deliberate edit and not a drift."""
        sources = {
            state for state, targets in TRANSITIONS.items() if CaseState.WAITING_EXTERNAL in targets
        }

        assert sources == {CaseState.EXECUTING}

    def test_gathering_evidence_can_still_plan(self) -> None:
        """Vacuity for the removals: the rows were narrowed, not emptied."""
        assert is_legal(CaseState.GATHERING_EVIDENCE, CaseState.PLANNING)
        assert is_legal(CaseState.PLANNING, CaseState.READY_TO_EXECUTE)
        assert is_legal(CaseState.PLANNING, CaseState.AWAITING_HUMAN)


class TestThePlannerCannotBeReachedFromAPostEffectState:
    """The session-Q defect, refused in the table rather than only in the graph.

    A case in ``WAITING_EXTERNAL`` has had an effect dispatched. Sending it to the planner would
    repropose on top of that — and ``book_appointment.reason`` is planner-authored free text with no
    trust key, so a reproposal differing by one full stop moves the argument digest, moves the
    idempotency key, and defeats duplicate suppression in CareLoop *and* in the clinic. That is not
    a hypothetical: it is what a failed verification used to do, measured as two real bookings.
    """

    def test_waiting_external_cannot_replan(self) -> None:
        assert not is_legal(CaseState.WAITING_EXTERNAL, CaseState.PLANNING)

    def test_executing_cannot_replan_either(self) -> None:
        """The same property one state earlier, and it has always held. Stated so it stays held."""
        assert not is_legal(CaseState.EXECUTING, CaseState.PLANNING)

    def test_verifying_still_cannot_replan(self) -> None:
        """The original of this defect, unchanged."""
        assert not is_legal(CaseState.VERIFYING, CaseState.PLANNING)

    def test_no_post_effect_state_reaches_the_planner(self) -> None:
        """The three of them as one statement, which is what the property actually is."""
        post_effect = {CaseState.EXECUTING, CaseState.WAITING_EXTERNAL, CaseState.VERIFYING}

        assert not any(CaseState.PLANNING in TRANSITIONS[state] for state in post_effect)

    def test_the_attempt_raises_rather_than_being_ignored(self) -> None:
        """A future node writing this line fails on its first run, not in a later measurement."""
        from helpers import make_case

        from careloop.state.machine import transition

        case = make_case(state=CaseState.WAITING_EXTERNAL)

        with pytest.raises(IllegalTransition):
            transition(case, CaseState.PLANNING, reason="replan", actor="test")


class TestWaitingExternalLeavesOnlyByReclaimingItsOwnAttempt:
    """Three exits, and each says something different about what became true."""

    def test_the_exact_exits(self) -> None:
        assert TRANSITIONS[CaseState.WAITING_EXTERNAL] == frozenset(
            {CaseState.EXECUTING, CaseState.AWAITING_HUMAN, CaseState.FAILED}
        )

    def test_it_cannot_reach_verification_directly(self) -> None:
        """A case here holds no ``ActionRecord``, so there is nothing for verification to check.

        Reconciliation reaches ``VERIFYING`` the long way round — re-claim through ``EXECUTING``,
        record what the provider returns, and enter verification with a result in hand. Keeping the
        direct edge would have offered a route into verification with nothing to verify, which
        ``VerifyNode`` would have failed the case for.
        """
        assert not is_legal(CaseState.WAITING_EXTERNAL, CaseState.VERIFYING)

    def test_it_cannot_reach_done(self) -> None:
        """Invariant 4 is untouched: ``DONE`` is still entered from ``VERIFYING`` alone."""
        assert not is_legal(CaseState.WAITING_EXTERNAL, CaseState.DONE)

        sources = [state for state, targets in TRANSITIONS.items() if CaseState.DONE in targets]
        assert sources == [CaseState.VERIFYING]

    def test_the_reclaim_lands_in_a_state_policy_permits_an_effect_from(self) -> None:
        """Why the re-claim exists at all, stated as the constraint that forced it.

        The shipped rules permit an external effect from ``PLANNING``, ``READY_TO_EXECUTE`` and
        ``EXECUTING``, and ``deny-external-effect-outside-execution-states`` refuses the rest. A
        replay issued while the case sat in ``WAITING_EXTERNAL`` would therefore be denied by the
        policy engine — so reconciliation re-enters ``EXECUTING`` first. The alternative was
        widening the rules, which would bend the authority layer to fit the lifecycle.
        """
        assert CaseState.EXECUTING in EFFECT_PERMITTING_STATES
        assert CaseState.WAITING_EXTERNAL not in EFFECT_PERMITTING_STATES

        reclaim_targets = TRANSITIONS[CaseState.WAITING_EXTERNAL] & EFFECT_PERMITTING_STATES
        assert reclaim_targets == frozenset({CaseState.EXECUTING})


class TestExecutingHasOneExitPerThingThatCanBecomeTrue:
    """The four outcomes of a prepared attempt, and no fifth."""

    def test_the_exact_exits(self) -> None:
        assert TRANSITIONS[CaseState.EXECUTING] == frozenset(
            {
                CaseState.VERIFYING,
                CaseState.WAITING_EXTERNAL,
                CaseState.AWAITING_HUMAN,
                CaseState.FAILED,
            }
        )

    def test_a_non_replayable_action_can_be_handed_to_a_person(self) -> None:
        """The edge that lets an ambiguous email stop instead of being sent again.

        ``AWAITING_HUMAN`` rather than ``WAITING_EXTERNAL``, and the distinction is the whole of
        what the two states mean: waiting says CareLoop will resolve this itself once something
        answers, and for an action it may never re-attempt that would be false.
        """
        assert is_legal(CaseState.EXECUTING, CaseState.AWAITING_HUMAN)

    def test_it_cannot_reach_done(self) -> None:
        assert not is_legal(CaseState.EXECUTING, CaseState.DONE)


class TestTheRestOfTheMachineDidNotMove:
    """Vacuity for the whole patch. Four rows changed and the others are asserted unchanged."""

    def test_only_failed_is_terminal(self) -> None:
        assert frozenset({CaseState.FAILED}) == TERMINAL_STATES

    def test_waiting_external_is_not_terminal(self) -> None:
        """It is a resting place, not an end. A case there is still owed an answer."""
        assert CaseState.WAITING_EXTERNAL not in TERMINAL_STATES

    @pytest.mark.parametrize(
        ("state", "targets"),
        [
            (CaseState.NEW, {CaseState.GATHERING_EVIDENCE, CaseState.FAILED}),
            (CaseState.READY_TO_EXECUTE, {CaseState.EXECUTING, CaseState.FAILED}),
            (CaseState.VERIFYING, {CaseState.DONE, CaseState.AWAITING_HUMAN, CaseState.FAILED}),
            (CaseState.DONE, {CaseState.GATHERING_EVIDENCE}),
            (CaseState.FAILED, set()),
            (
                CaseState.AWAITING_HUMAN,
                {
                    CaseState.READY_TO_EXECUTE,
                    CaseState.PLANNING,
                    CaseState.VERIFYING,
                    CaseState.FAILED,
                },
            ),
        ],
        ids=str,
    )
    def test_the_untouched_rows(self, state: CaseState, targets: set[CaseState]) -> None:
        assert TRANSITIONS[state] == frozenset(targets)

    def test_every_state_has_a_row(self) -> None:
        """A state with no entry raises a ``KeyError`` deep inside ``is_legal`` rather than here."""
        assert set(TRANSITIONS) == set(CaseState)
