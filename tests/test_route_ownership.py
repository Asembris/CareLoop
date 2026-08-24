"""Knowing an identifier is not permission to use it.

Patch 3. Before it, the predicate stopping caller A reaching caller B's case was *"the caller
supplied the correct session header"* and nothing else, and a single-world app had no predicate at
all. After it, every read and every caller-driven mutation goes through
:meth:`careloop.api.access.CaseAccess.case`, and a workspace resolves only for the principal it was
minted for.

**Two boundaries, and they fail differently.** A workspace is refused in
:func:`careloop.api.deps.deps`, before any store is reached -- so a request naming somebody else's
world never touches their cases at all. A case is refused in ``CaseAccess``, which is what covers a
single durable store holding several caregivers' work, where there is no world boundary to lean on.

**Refusals disclose nothing and record nothing.** A case belonging to somebody else answers
byte-identically to a case that does not exist, and a refused mutation leaves the case's version,
timeline, approvals and actions exactly as they were -- asserted on the store rather than on the
status code, because a 404 with a side effect is the failure that matters.
"""

from __future__ import annotations

import pytest
from api_helpers import intent
from identity_helpers import (
    CAREGIVER_A,
    CAREGIVER_B,
    EMAIL_TO_A,
    TOKEN_A,
    TOKEN_B,
    bearer,
    isolated_production_client,
    mint,
    production_client,
    workspace,
)

from careloop.graph.runner import open_case
from careloop.identity.directory import Caregiver
from careloop.state import CaseState, InMemoryCaseStore
from careloop.state.machine import transition

ALMA = Caregiver(CAREGIVER_A, (("caregiver_email", "alma@carer.example"),))
BO = Caregiver(CAREGIVER_B, (("caregiver_email", "bo@carer.example"),))

BOOKING_EVENT = {"case_id": "case-0001", "event_id": "evt-0001-referral"}


def store_with(*owners: Caregiver) -> InMemoryCaseStore:
    """A store holding one case per caregiver, named ``case-<caregiver_id>``."""
    from helpers import FIXED_NOW

    from careloop.events.inbound import REFERRAL_EVENT

    store = InMemoryCaseStore()
    for owner in owners:
        open_case(
            store,
            REFERRAL_EVENT,
            caregiver=owner,
            case_id=f"case-{owner.caregiver_id}",
            now=FIXED_NOW,
        )
    return store


def snapshot(store, case_id: str) -> tuple:
    case = store.get(case_id)
    return (case.version, case.state, len(case.timeline), len(case.approvals), len(case.actions))


# =================================================================================================
# P2 -- cross-workspace isolation
# =================================================================================================


class TestAWorkspaceResolvesOnlyForItsOwner:
    def test_its_owner_may_use_it(self) -> None:
        client, _ = isolated_production_client()
        with client:
            session = mint(client, TOKEN_A)
            assert client.get("/api/cases", headers=workspace(session, TOKEN_A)).status_code == 200

    def test_another_caregiver_may_not(self) -> None:
        client, _ = isolated_production_client()
        with client:
            session = mint(client, TOKEN_A)
            answered = client.get("/api/cases", headers=workspace(session, TOKEN_B))

        assert answered.status_code == 410

    def test_the_refusal_is_indistinguishable_from_a_world_that_never_existed(self) -> None:
        """A 403 would say "that world exists and is not yours", which is an oracle."""
        client, _ = isolated_production_client()
        with client:
            session = mint(client, TOKEN_A)
            stolen = client.get("/api/cases", headers=workspace(session, TOKEN_B))
            invented = client.get("/api/cases", headers=workspace("never-minted", TOKEN_B))

        assert stolen.status_code == invented.status_code
        assert stolen.json() == invented.json()

    def test_each_caregiver_gets_their_own(self) -> None:
        """Vacuity: the refusals above are about ownership, not about minting being broken."""
        client, worlds = isolated_production_client()
        with client:
            first = mint(client, TOKEN_A)
            second = mint(client, TOKEN_B)

            assert first != second
            assert worlds.owner_of(first) == CAREGIVER_A
            assert worlds.owner_of(second) == CAREGIVER_B

    def test_a_workspace_cannot_be_minted_without_a_verified_principal(self) -> None:
        """The anonymous capacity exhaustion the audit found is unreachable in production."""
        client, _ = isolated_production_client()
        with client:
            assert client.post("/api/sessions").status_code == 401
            assert client.post("/api/sessions", headers=bearer("forged")).status_code == 401

    def test_a_stolen_workspace_never_reaches_the_store(self) -> None:
        """Refused in ``deps`` before a store is resolved, not filtered afterwards."""
        client, worlds = isolated_production_client()
        with client:
            session = mint(client, TOKEN_A)
            world = worlds.world(session)
            reads: list[str] = []
            world.store.list_by_state = _recording(world.store.list_by_state, reads)  # type: ignore[method-assign]

            client.get("/api/cases", headers=workspace(session, TOKEN_B))

        assert reads == []


def _recording(method, sink):
    def wrapped(*args, **kwargs):
        sink.append("read")
        return method(*args, **kwargs)

    return wrapped


# =================================================================================================
# P3 -- cross-case isolation
# =================================================================================================


class TestKnowingACaseIdIsNotEnough:
    def test_the_owner_may_read_their_case(self) -> None:
        with production_client(store_with(ALMA, BO)) as client:
            answered = client.get(f"/api/cases/case-{CAREGIVER_A}", headers=bearer(TOKEN_A))

        assert answered.status_code == 200

    def test_another_caregiver_may_not(self) -> None:
        with production_client(store_with(ALMA, BO)) as client:
            answered = client.get(f"/api/cases/case-{CAREGIVER_A}", headers=bearer(TOKEN_B))

        assert answered.status_code == 404

    def test_the_refusal_is_indistinguishable_from_a_case_that_does_not_exist(self) -> None:
        with production_client(store_with(ALMA, BO)) as client:
            theirs = client.get(f"/api/cases/case-{CAREGIVER_A}", headers=bearer(TOKEN_B))
            invented = client.get("/api/cases/case-{CAREGIVER_A}", headers=bearer(TOKEN_B))

        assert theirs.status_code == invented.status_code == 404

    def test_the_dashboard_shows_only_the_callers_own_cases(self) -> None:
        with production_client(store_with(ALMA, BO)) as client:
            listed = client.get("/api/cases", headers=bearer(TOKEN_A)).json()

        every = [card["case_id"] for section in listed.values() for card in section]
        assert every == [f"case-{CAREGIVER_A}"]

    def test_the_other_caregiver_sees_the_other_case(self) -> None:
        """Vacuity: the filter is by owner, not a filter that hides everything."""
        with production_client(store_with(ALMA, BO)) as client:
            listed = client.get("/api/cases", headers=bearer(TOKEN_B)).json()

        every = [card["case_id"] for section in listed.values() for card in section]
        assert every == [f"case-{CAREGIVER_B}"]

    def test_a_case_persisted_before_ownership_is_reachable_by_nobody(self) -> None:
        """Readable, projectable, and correctly unable to satisfy an owner nobody recorded."""
        from careloop.state.case import Case

        store = InMemoryCaseStore()
        store.create(Case(case_id="case-legacy", event_id="evt-0001-referral", summary="legacy"))

        with production_client(store) as client:
            for token in (TOKEN_A, TOKEN_B):
                assert (
                    client.get("/api/cases/case-legacy", headers=bearer(token)).status_code == 404
                )


def parked_case(store) -> tuple:
    """Drive a real case to ``AWAITING_HUMAN`` as Alma, and return its id and pending approval.

    A real parked case rather than an invented identifier, because a decision on an approval that
    does not exist answers 404 whether ownership is checked or not -- so a test built on one would
    pass with the predicate deleted.
    """
    from api_helpers import factory_proposing
    from fastapi.testclient import TestClient
    from identity_helpers import stub_verifier, two_caregiver_directory

    held = two_caregiver_directory()

    from careloop.api.app import create_app
    from careloop.api.auth import production_auth

    app = create_app(
        store,
        context_factory=factory_proposing(intent("send_email", **EMAIL_TO_A), directory=held),
        auth=production_auth(verifier=stub_verifier(), directory=held),
    )
    with TestClient(app) as client:
        client.post("/api/events?wait=5", headers=bearer(TOKEN_A), json=BOOKING_EVENT)

    case = store.get(BOOKING_EVENT["case_id"])
    assert case.state is CaseState.AWAITING_HUMAN, case.state
    pending = case.pending_approval()
    assert pending is not None
    return case.case_id, pending.approval_id


class TestEveryCallerDrivenRouteChecksOwnership:
    """Driven against a case that really exists and really has something to decide."""

    @pytest.mark.parametrize("verb", ["approve", "reject"], ids=["approve", "reject"])
    def test_a_stranger_cannot_decide_a_real_pending_approval(self, verb: str) -> None:
        store = InMemoryCaseStore()
        case_id, approval_id = parked_case(store)
        before = snapshot(store, case_id)

        with production_client(store) as client:
            answered = client.post(
                f"/api/cases/{case_id}/decisions/{approval_id}/{verb}",
                headers=bearer(TOKEN_B),
            )

        assert answered.status_code == 404
        assert snapshot(store, case_id) == before

    def test_the_owner_can_decide_the_same_approval(self) -> None:
        """Vacuity: the refusals are about who asked, not about an undecidable approval."""
        store = InMemoryCaseStore()
        case_id, approval_id = parked_case(store)

        with production_client(store, intent("send_email", **EMAIL_TO_A)) as client:
            answered = client.post(
                f"/api/cases/{case_id}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        assert answered.status_code == 202
        assert store.get(case_id).state is not CaseState.AWAITING_HUMAN

    def test_a_stranger_cannot_read_a_real_parked_case(self) -> None:
        store = InMemoryCaseStore()
        case_id, _ = parked_case(store)

        with production_client(store) as client:
            assert client.get(f"/api/cases/{case_id}", headers=bearer(TOKEN_B)).status_code == 404
            assert client.get(f"/api/cases/{case_id}", headers=bearer(TOKEN_A)).status_code == 200

    def test_a_stranger_cannot_ask_for_a_recheck_on_a_real_case(self) -> None:
        store = InMemoryCaseStore()
        case_id, _ = parked_case(store)
        before = snapshot(store, case_id)

        with production_client(store) as client:
            answered = client.post(
                f"/api/cases/{case_id}/recheck",
                headers=bearer(TOKEN_B),
            )

        assert answered.status_code == 404
        assert snapshot(store, case_id) == before

    def test_the_owners_recheck_is_refused_for_a_domain_reason_instead(self) -> None:
        """Vacuity: the owner reaches the handler and is turned back by the case's own shape."""
        store = InMemoryCaseStore()
        case_id, _ = parked_case(store)

        with production_client(store) as client:
            answered = client.post(
                f"/api/cases/{case_id}/recheck",
                headers=bearer(TOKEN_A),
            )

        assert answered.status_code == 409

    def test_delivering_an_event_onto_somebody_elses_case_is_refused(self) -> None:
        store = store_with(ALMA, BO)
        with production_client(store) as client:
            answered = client.post(
                "/api/events",
                headers=bearer(TOKEN_B),
                json={"case_id": f"case-{CAREGIVER_A}", "event_id": "evt-0001-referral"},
            )

        assert answered.status_code == 404

    def test_reading_somebody_elses_run_is_refused(self) -> None:
        store = InMemoryCaseStore()
        with production_client(store, intent("send_email", **EMAIL_TO_A)) as client:
            started = client.post(
                "/api/events?wait=5", headers=bearer(TOKEN_A), json=BOOKING_EVENT
            ).json()

            mine = client.get(f"/api/runs/{started['run_id']}", headers=bearer(TOKEN_A))
            theirs = client.get(f"/api/runs/{started['run_id']}", headers=bearer(TOKEN_B))

        assert mine.status_code == 200
        assert theirs.status_code == 404


class TestARefusedMutationRecordsNothing:
    """The assertion that matters: a 404 with a side effect would be worse than a 200."""

    def test_a_refused_delivery_neither_reopens_nor_runs(self) -> None:
        from helpers import FIXED_NOW

        store = store_with(ALMA)
        case = store.get(f"case-{CAREGIVER_A}")
        base = case.version
        for step in (
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.DONE,
        ):
            case = transition(case, step, reason="t", actor="t", now=FIXED_NOW)
        store.save(case, expected_version=base)
        before = snapshot(store, f"case-{CAREGIVER_A}")

        with production_client(store) as client:
            answered = client.post(
                "/api/events",
                headers=bearer(TOKEN_B),
                json={"case_id": f"case-{CAREGIVER_A}", "event_id": "evt-0003-claim-rejection"},
            )

        assert answered.status_code == 404
        assert snapshot(store, f"case-{CAREGIVER_A}") == before

    def test_the_same_delivery_by_the_owner_really_does_reopen(self) -> None:
        """Vacuity: the refusal above is ownership, not the event being unacceptable."""
        from helpers import FIXED_NOW

        store = store_with(ALMA)
        case = store.get(f"case-{CAREGIVER_A}")
        base = case.version
        for step in (
            CaseState.GATHERING_EVIDENCE,
            CaseState.PLANNING,
            CaseState.READY_TO_EXECUTE,
            CaseState.EXECUTING,
            CaseState.VERIFYING,
            CaseState.DONE,
        ):
            case = transition(case, step, reason="t", actor="t", now=FIXED_NOW)
        store.save(case, expected_version=base)

        with production_client(store, intent("send_email", **EMAIL_TO_A)) as client:
            answered = client.post(
                "/api/events",
                headers=bearer(TOKEN_A),
                json={"case_id": f"case-{CAREGIVER_A}", "event_id": "evt-0003-claim-rejection"},
            )

        assert answered.status_code == 202
        assert store.get(f"case-{CAREGIVER_A}").state is not CaseState.DONE


class TestTheDemoIsNotSubjectToAnyOfThis:
    """A synthetic world is isolated rather than authorised, and the judge flow is unchanged."""

    def test_the_demo_needs_no_token(self) -> None:
        from fastapi.testclient import TestClient

        from careloop.api.app import create_app

        with TestClient(create_app(store_with(ALMA, BO))) as client:
            assert client.get("/api/cases").status_code == 200
            assert client.get(f"/api/cases/case-{CAREGIVER_A}").status_code == 200

    def test_the_demo_dashboard_hides_nothing(self) -> None:
        from fastapi.testclient import TestClient

        from careloop.api.app import create_app

        with TestClient(create_app(store_with(ALMA, BO))) as client:
            listed = client.get("/api/cases").json()

        every = {card["case_id"] for section in listed.values() for card in section}
        assert every == {f"case-{CAREGIVER_A}", f"case-{CAREGIVER_B}"}
