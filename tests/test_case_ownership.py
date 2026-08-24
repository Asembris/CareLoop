"""Whose case this is, recorded durably and frozen when it is opened.

Patch 2. It writes ownership down and enforces nothing -- route enforcement is patch 3 and grants
are patch 6 -- so what has to be true here is that the record is *correct* and that adding it broke
nothing that was already persisted.

Three properties, and they fail differently.

**Ownership is settled once.** :func:`careloop.graph.runner.open_case` requires a caregiver, so a
case whose owner was decided by a default is unconstructable rather than merely discouraged. A
reopen takes no caregiver argument at all, so there is nowhere for a later event to change hands
through -- the same arrangement that keeps ``Case.event_id`` recording which event *opened* a case.

**A case persisted before ownership existed stays readable.** ``caregiver_id`` is optional, and
that is a compatibility decision rather than a soft boundary: a required field would make a legacy
case unreadable rather than merely unowned, which is the distinction ``Approval.is_actionable``
already draws. Reading stays total; the refusal lives where authority is decided.

**A workspace's owner is never something a caller sent.** :meth:`SessionWorlds.mint` takes it as an
argument the route supplies from a verified principal, and there is no path from a request header
to that value.
"""

from __future__ import annotations

import json

import pytest
from helpers import FIXED_NOW

from careloop.events.inbound import CLAIM_REJECTION_EVENT, REFERRAL_EVENT
from careloop.graph.runner import open_case, reopen_case
from careloop.graph.trusted import DEMO_CAREGIVER, DEMO_CAREGIVER_ID
from careloop.identity.directory import Caregiver
from careloop.state import CaseState, InMemoryCaseStore
from careloop.state.case import Case
from careloop.state.machine import transition
from careloop.state.sqlite_store import SqliteCaseStore

CASE_ID = "case-0001"
OTHER = Caregiver("cg-other", (("caregiver_email", "other@carer.example"),))


class TestOwnershipIsFrozenWhenTheCaseIsOpened:
    def test_a_new_case_carries_the_caregiver_it_was_opened_for(self) -> None:
        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, caregiver=OTHER, case_id=CASE_ID, now=FIXED_NOW)

        assert case.caregiver_id == "cg-other"
        assert store.get(CASE_ID).caregiver_id == "cg-other"

    def test_opening_a_case_without_naming_a_caregiver_is_not_possible(self) -> None:
        """Required rather than defaulted: an owner nobody chose is not an owner."""
        with pytest.raises(TypeError):
            open_case(InMemoryCaseStore(), REFERRAL_EVENT, case_id=CASE_ID)  # type: ignore[call-arg]

    def test_two_caregivers_opening_two_cases_own_one_each(self) -> None:
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id="case-a", now=FIXED_NOW)
        open_case(store, REFERRAL_EVENT, caregiver=OTHER, case_id="case-b", now=FIXED_NOW)

        assert store.get("case-a").caregiver_id == DEMO_CAREGIVER_ID
        assert store.get("case-b").caregiver_id == "cg-other"


class TestAReopenIsNotAChangeOfHands:
    def test_reopening_keeps_the_original_owner(self) -> None:
        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, caregiver=OTHER, case_id=CASE_ID, now=FIXED_NOW)
        base = case.version
        for step in (
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.DONE,
        ):
            case = transition(case, step, reason="test", actor="test")
        store.save(case, expected_version=base)

        reopened = reopen_case(store, CLAIM_REJECTION_EVENT, case_id=CASE_ID, now=FIXED_NOW)

        assert reopened.caregiver_id == "cg-other"
        assert reopened.state is CaseState.GATHERING_EVIDENCE

    def test_reopen_has_no_parameter_a_new_owner_could_arrive_through(self) -> None:
        """Structural rather than behavioural: there is nowhere to pass one."""
        import inspect

        assert "caregiver" not in inspect.signature(reopen_case).parameters


class TestCasesPersistedBeforeOwnershipStillRead:
    """The compatibility boundary, measured rather than asserted."""

    @pytest.mark.parametrize("state", list(CaseState), ids=lambda state: state.value)
    def test_a_legacy_document_deserializes_in_every_state(self, state: CaseState) -> None:
        legacy = {
            "case_id": CASE_ID,
            "event_id": "evt-0001-referral",
            "state": state.value,
            "version": 3,
            "summary": "a case from before ownership existed",
            "created_at": FIXED_NOW.isoformat(),
            "updated_at": FIXED_NOW.isoformat(),
            "facts": [],
            "approvals": [],
            "actions": [],
            "timeline": [],
            "failure_reason": None,
        }

        case = Case.model_validate_json(json.dumps(legacy))

        assert case.state is state
        assert case.caregiver_id is None

    def test_a_legacy_case_round_trips_through_the_deployed_store(self, tmp_path) -> None:
        """The store a deployment uses, not the in-memory one -- session C's finding."""
        store = SqliteCaseStore(str(tmp_path / "legacy.sqlite3"))
        store.create(Case(case_id=CASE_ID, event_id="evt-0001-referral", summary="legacy"))

        assert store.get(CASE_ID).caregiver_id is None

    def test_a_legacy_case_still_projects(self) -> None:
        """Reading stays total: an unowned case renders rather than raising."""
        from careloop.api.projections import case_detail

        detail = case_detail(Case(case_id=CASE_ID, event_id="evt-0001-referral", summary="legacy"))

        assert detail.case_id == CASE_ID


class TestOwnershipIsNotOnTheActionRecord:
    """Whose case it was is a property of the case, answerable in one place."""

    def test_the_action_record_has_no_identity_field(self) -> None:
        from careloop.state.case import ActionRecord

        assert set(ActionRecord.model_fields) == {
            "idempotency_key",
            "action_type",
            "args_digest",
            "result",
            "recorded_at",
        }


class TestAWorkspaceKnowsWhoItBelongsTo:
    def test_a_minted_workspace_records_its_owner(self) -> None:
        from careloop.api.sessions import SessionWorlds

        worlds = SessionWorlds(lambda _session: _world(), end_world=lambda *_: None)
        session = worlds.mint(owner="cg-other")

        assert worlds.owner_of(session) == "cg-other"

    def test_a_workspace_minted_with_no_owner_belongs_to_nobody(self) -> None:
        from careloop.api.sessions import SessionWorlds

        worlds = SessionWorlds(lambda _session: _world(), end_world=lambda *_: None)

        assert worlds.owner_of(worlds.mint()) is None

    def test_an_unknown_workspace_has_no_owner(self) -> None:
        from careloop.api.sessions import SessionWorlds

        worlds = SessionWorlds(lambda _session: _world(), end_world=lambda *_: None)

        assert worlds.owner_of("never-minted") is None

    def test_forgetting_a_workspace_forgets_its_owner(self) -> None:
        """A session id is reissued to nobody, but a stale owner is a stale authority answer."""
        from careloop.api.sessions import SessionWorlds

        clock = [0.0]
        worlds = SessionWorlds(
            lambda _session: _world(),
            idle_seconds=10.0,
            now=lambda: clock[0],
            end_world=lambda *_: None,
        )
        session = worlds.mint(owner="cg-other")
        clock[0] = 100.0
        worlds.mint(owner="cg-later")

        assert worlds.owner_of(session) is None

    def test_minting_takes_the_owner_as_an_argument_rather_than_reading_a_request(self) -> None:
        """Structural: nothing in the registry can see a header."""
        import inspect

        from careloop.api.sessions import SessionWorlds

        assert list(inspect.signature(SessionWorlds.mint).parameters) == ["self", "owner"]


def _world():
    from careloop.api.deps import ApiDeps

    return ApiDeps(store=InMemoryCaseStore())
