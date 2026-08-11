"""The transition table is the spine's load-bearing invariant, so it is tested exhaustively."""

from __future__ import annotations

import pytest

from careloop.state import (
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
from tests.conftest import make_case

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
def test_failed_is_reachable_from_every_non_terminal_state(state: CaseState) -> None:
    """A case must always have somewhere to go when it breaks."""
    if state in TERMINAL_STATES:
        pytest.skip(f"{state} is terminal")
    assert CaseState.FAILED in TRANSITIONS[state]


def test_done_is_reachable_only_from_verifying() -> None:
    """This is what makes 'did not reach DONE via the unsafe path' structural, not observational."""
    sources = [state for state, targets in TRANSITIONS.items() if CaseState.DONE in targets]
    assert sources == [CaseState.VERIFYING]


def test_terminal_states_are_exactly_done_and_failed() -> None:
    assert sorted(TERMINAL_STATES) == [CaseState.DONE, CaseState.FAILED]


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
