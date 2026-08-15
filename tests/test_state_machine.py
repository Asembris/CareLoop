"""The transition table is the spine's load-bearing invariant, so it is tested exhaustively."""

from __future__ import annotations

import pytest
from helpers import make_case

from careloop.state import (
    AT_REST_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    Case,
    CaseState,
    IllegalTransition,
    TimelineKind,
    append_event,
    is_legal,
    transition,
)
from careloop.state.machine import REOPEN_FROM, REOPEN_TO

ALL_STATES = list(CaseState)


def test_every_state_appears_in_the_table() -> None:
    assert set(TRANSITIONS) == set(ALL_STATES)


def test_every_target_is_a_real_state() -> None:
    for targets in TRANSITIONS.values():
        assert targets <= set(ALL_STATES)


def test_no_state_transitions_to_itself() -> None:
    for state, targets in TRANSITIONS.items():
        assert state not in targets, f"{state} lists itself as a target"


@pytest.mark.parametrize("state", ALL_STATES)
def test_failed_is_reachable_from_every_state_still_in_flight(state: CaseState) -> None:
    """A case must always have somewhere to go when it breaks.

    Skips states **at rest** rather than states that are strictly terminal. That distinction
    appeared in commit 7 with the reopen: ``DONE`` gained an outgoing transition and so left
    ``TERMINAL_STATES``, but it is still a state the graph will not move a case out of, and a
    finished case has no need of a route to ``FAILED``. The property this test is about is
    in-flight cases having somewhere to go, and ``AT_REST_STATES`` is that set.
    """
    if state in AT_REST_STATES:
        pytest.skip(f"{state} is at rest")
    assert CaseState.FAILED in TRANSITIONS[state]


def test_verification_can_never_return_a_case_to_planning() -> None:
    """**The state-machine half of the post-effect safety property.**

    Every entry into ``VERIFYING`` is post-effect: it is reachable only from ``EXECUTING`` and
    ``WAITING_EXTERNAL``, the execute node moves a case here after recording an action or
    suppressing a duplicate of one, and the verify node fails the case outright when no
    ``ActionRecord`` exists. So a verification failure is never "that did not work, try again" — an
    action has been carried out — and returning the case to the planner meant planning on top of an
    outcome nobody had established.

    Measured before the repair: an identical reproposal was suppressed and re-verified into the same
    failure until the SDK's node bound stopped the run, and a reproposal differing in one free-text
    argument keyed differently, defeated duplicate suppression at both layers, reached ``ALLOW`` and
    executed a second real external effect.

    The graph's ``verify → plan`` edge is gone too — ``tests/test_graph_wiring.py`` — and this is
    deliberately not the same assertion. The edge stops the graph routing there; this stops a node
    writing the state at all, so restoring the hazard takes two edits in two files rather than one.
    """
    assert CaseState.PLANNING not in TRANSITIONS[CaseState.VERIFYING]
    assert not is_legal(CaseState.VERIFYING, CaseState.PLANNING)


def test_verification_failure_can_park_the_case_for_a_person() -> None:
    """The target it goes to instead, asserted so the refusal above is not merely a dead end.

    ``AWAITING_HUMAN`` was reachable only from ``PLANNING`` until this session, which was the gate
    parking a proposal it will not take responsibility for. It is now also reachable from
    ``VERIFYING``, which is the verify node parking an *effect* it cannot certify. Both carry no
    authority; the second carries none for a further reason, since the effect already happened and
    no decision could change it.
    """
    assert CaseState.AWAITING_HUMAN in TRANSITIONS[CaseState.VERIFYING]
    assert is_legal(CaseState.VERIFYING, CaseState.AWAITING_HUMAN)


def test_verifying_can_reach_exactly_three_states() -> None:
    """The whole of this session's blast radius on the transition table, in one line.

    ``DONE`` on success, ``AWAITING_HUMAN`` when the effect cannot be certified, and ``FAILED`` for
    the structural faults the node still raises — no proposal, no recorded action.
    """
    assert TRANSITIONS[CaseState.VERIFYING] == frozenset(
        {CaseState.DONE, CaseState.AWAITING_HUMAN, CaseState.FAILED}
    )


def test_replanning_is_still_legal_from_where_it_was_always_meant_to_be() -> None:
    """Vacuity for the three above: replanning was narrowed, not removed.

    Both remaining sources are pre-effect. The gate leaves a denied proposal in ``PLANNING`` and a
    caregiver's rejection returns a parked case there, and in neither has anything been carried out
    — which is the distinction the whole repair turns on.
    """
    assert CaseState.PLANNING in TRANSITIONS[CaseState.AWAITING_HUMAN]
    assert CaseState.PLANNING in TRANSITIONS[CaseState.GATHERING_EVIDENCE]
    assert {state for state, targets in TRANSITIONS.items() if CaseState.PLANNING in targets} == {
        CaseState.GATHERING_EVIDENCE,
        CaseState.AWAITING_HUMAN,
        CaseState.WAITING_EXTERNAL,
    }


def test_done_is_reachable_only_from_verifying() -> None:
    """This is what makes 'did not reach DONE via the unsafe path' structural, not observational.

    Unchanged by the reopen, and deliberately so: invariant 4 is about how a case *enters* ``DONE``,
    and the reopen is an edge out of it.
    """
    sources = [state for state, targets in TRANSITIONS.items() if CaseState.DONE in targets]
    assert sources == [CaseState.VERIFYING]


def test_only_failed_is_strictly_terminal() -> None:
    """``DONE`` left this set in commit 7. Recorded rather than quietly adjusted.

    Before the reopen, ``TERMINAL_STATES`` carried two meanings at once — "nothing may leave" and
    "the graph is finished with this" — and they were the same set. They are not the same idea, and
    the reopen separated them. ``FAILED`` is the only state nothing may leave.
    """
    assert sorted(TERMINAL_STATES) == [CaseState.FAILED]


def test_at_rest_is_done_and_failed() -> None:
    """What ``TERMINAL_STATES`` meant before, under the name that says what it means."""
    assert sorted(AT_REST_STATES) == [CaseState.DONE, CaseState.FAILED]


def test_done_has_exactly_one_way_out_and_it_is_the_reopen() -> None:
    """The whole of the reopen's blast radius on the transition table.

    ``GATHERING_EVIDENCE`` and nothing else, so a finished case cannot be pushed back to planning,
    execution or failure. Which code may make this move is a separate question, answered
    structurally in ``tests/test_reopen.py``: no node, agent, tool or policy module may name
    ``reopen_case``, so the agent can never leave ``DONE`` and only the world can.
    """
    assert TRANSITIONS[CaseState.DONE] == frozenset({CaseState.GATHERING_EVIDENCE})
    assert (REOPEN_FROM, REOPEN_TO) == (CaseState.DONE, CaseState.GATHERING_EVIDENCE)


@pytest.mark.parametrize("to", ALL_STATES)
def test_done_refuses_every_outbound_move_except_the_reopen(to: CaseState) -> None:
    """``DONE`` is no longer swept by the terminal-state test, so it is swept here.

    Without this, removing ``DONE`` from ``TERMINAL_STATES`` would have quietly stopped anything
    checking that a finished case cannot be moved wherever a caller likes.
    """
    case = make_case(state=CaseState.DONE)

    if to is CaseState.GATHERING_EVIDENCE:
        assert transition(case, to, reason="reopen", actor="system").state is to
    else:
        with pytest.raises(IllegalTransition):
            transition(case, to, reason="escape attempt", actor="test")


@pytest.mark.parametrize("frm", ALL_STATES)
@pytest.mark.parametrize("to", ALL_STATES)
def test_all_state_pairs_match_the_table(frm: CaseState, to: CaseState) -> None:
    """Sweep the full N x N matrix: legal pairs succeed, every other pair raises."""
    case = make_case(state=frm)
    if is_legal(frm, to):
        moved = transition(case, to, reason="sweep", actor="test")
        assert moved.state is to
    else:
        with pytest.raises(IllegalTransition):
            transition(case, to, reason="sweep", actor="test")


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("to", ALL_STATES)
def test_terminal_states_refuse_every_outbound_move(state: CaseState, to: CaseState) -> None:
    with pytest.raises(IllegalTransition):
        transition(make_case(state=state), to, reason="escape attempt", actor="test")


def test_transition_returns_a_new_instance_and_leaves_the_original_alone(new_case: Case) -> None:
    moved = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system")
    assert moved is not new_case
    assert new_case.state is CaseState.NEW
    assert moved.state is CaseState.GATHERING_EVIDENCE


def test_case_is_frozen(new_case: Case) -> None:
    """Correction 4: illegal mutation raises at runtime for every caller, present and future."""
    with pytest.raises(Exception, match=r"frozen|immutable"):
        new_case.state = CaseState.DONE  # type: ignore[misc]


def test_case_collections_are_immutable(new_case: Case) -> None:
    """A shallow freeze would still let callers mutate shared collections."""
    assert isinstance(new_case.timeline, tuple)
    assert isinstance(new_case.facts, tuple)
    with pytest.raises(AttributeError):
        new_case.timeline.append("nope")  # type: ignore[attr-defined]


def test_transition_appends_exactly_one_timeline_entry(new_case: Case) -> None:
    moved = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system")
    assert len(moved.timeline) == len(new_case.timeline) + 1
    event = moved.timeline[-1]
    assert event.kind is TimelineKind.STATE_TRANSITION
    assert event.actor == "system"
    assert event.payload["from"] == CaseState.NEW
    assert event.payload["to"] == CaseState.GATHERING_EVIDENCE
    assert event.payload["reason"] == "ingested"


def test_transition_bumps_version(new_case: Case) -> None:
    moved = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="ingested", actor="system")
    assert moved.version == new_case.version + 1


def test_failing_records_the_reason(new_case: Case) -> None:
    failed = transition(
        new_case, CaseState.FAILED, reason="bounded replan exhausted", actor="graph"
    )
    assert failed.failure_reason == "bounded replan exhausted"


def test_timeline_sequence_numbers_are_dense_and_ordered(new_case: Case) -> None:
    case = transition(new_case, CaseState.GATHERING_EVIDENCE, reason="a", actor="t")
    case = append_event(case, kind=TimelineKind.EVIDENCE_RECORDED, actor="t", summary="b")
    case = transition(case, CaseState.PLANNING, reason="c", actor="t")
    assert [event.seq for event in case.timeline] == [0, 1, 2]


def test_append_event_does_not_change_state(new_case: Case) -> None:
    after = append_event(
        new_case, kind=TimelineKind.EVIDENCE_RECORDED, actor="t", summary="recorded"
    )
    assert after.state is new_case.state
    assert after.version == new_case.version + 1


def test_illegal_transition_message_names_the_legal_targets() -> None:
    with pytest.raises(IllegalTransition) as caught:
        transition(make_case(state=CaseState.NEW), CaseState.DONE, reason="x", actor="t")
    assert "GATHERING_EVIDENCE" in str(caught.value)
