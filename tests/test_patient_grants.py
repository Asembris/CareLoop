"""May this caregiver currently act for this patient -- and what changes when the answer changes.

Patch 6. Two properties live here and they pull in opposite directions, which is why they are in one
file: separating them would let somebody satisfy either by breaking the other.

**Authorisation governs new authority.** A caller who no longer holds a grant cannot deliver an
event, approve a proposal, ask for a re-check or start a scenario. Nothing is dispatched, no
``ActionRecord`` appears, and no provider is touched -- asserted on the provider and on the case,
never on a status code.

**Provider reconciliation governs already-issued uncertainty.** A booking that may already have
committed is *not* un-issued by a revocation, and refusing to look at it would leave a real
appointment in a real clinic permanently unknowable. So the recovery path -- the startup sweep,
:meth:`careloop.api.deps.ApiDeps.register_recovery`, the wake, ``ReconcileNode`` -- consults no
directory, holds no principal and asks no grant question. It converges on one appointment and
certifies it with an independent read, exactly as it did before identity existed.

**The mutant that matters is the second one**, because the broken version looks *more* secure:
blocking reconciliation after revocation reads as caution and is in fact CareLoop refusing to find
out what it did in somebody's name. ``TestReconciliationIsNotAnAuthorityDecision`` is the
class that turns red.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import FIXED_NOW
from identity_helpers import (
    CAREGIVER_A,
    EMAIL_TO_A,
    PATIENT_A,
    TOKEN_A,
    bearer,
    production_boundary,
    two_caregiver_directory,
)

from careloop.api.access import NOT_AUTHORISED_FOR_THIS_PATIENT
from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.context import ESTABLISHED_PATIENT_TRUST, established_patients
from careloop.graph.runner import open_case
from careloop.provenance import Fact, Provenance, TrustLevel
from careloop.state import CaseState, InMemoryCaseStore

CASE_ID = "case-0001"
EVENT = {"case_id": CASE_ID, "event_id": "evt-0001-referral"}


def fact(key: str, value: str, level: TrustLevel) -> Fact:
    return Fact(
        key=key,
        value=value,
        trust_level=level,
        provenance=Provenance(source_doc="referral.pdf", extractor="labelled"),
        extracted_at=FIXED_NOW,
    )


def client_for(directory, *proposals):
    from api_helpers import factory_proposing
    from fastapi.testclient import TestClient

    from careloop.api.app import create_app

    return TestClient(
        create_app(
            InMemoryCaseStore() if not proposals else proposals[0],
            context_factory=factory_proposing(*proposals[1:]) if len(proposals) > 1 else None,
            auth=production_boundary(directory),
        )
    )


# =================================================================================================
# Which patients a case is about
# =================================================================================================


class TestOnlyEstablishedPatientsCount:
    def test_a_case_with_no_documents_read_names_nobody(self) -> None:
        store = InMemoryCaseStore()
        opened = open_case(
            store,
            REFERRAL_EVENT,
            caregiver=two_caregiver_directory().caregiver(CAREGIVER_A),
            case_id=CASE_ID,
            now=FIXED_NOW,
        )

        assert established_patients(opened) == frozenset()

    def test_a_controlled_patient_counts(self) -> None:
        case = _case_with(fact("patient_id", PATIENT_A, TrustLevel.CONTROLLED))

        assert established_patients(case) == frozenset({PATIENT_A})

    def test_an_untrusted_patient_does_not(self) -> None:
        """The stricter reading looks safer and is not.

        An attacker-authored page naming ``PT-9999`` would otherwise decide which caregiver may
        work on the case -- denial of service by document. Such a value cannot reach an effect
        anyway: ``deny-booking-for-unverified-patient`` refuses it one layer down.
        """
        case = _case_with(fact("patient_id", "PT-9999", TrustLevel.UNTRUSTED))

        assert established_patients(case) == frozenset()

    def test_the_threshold_is_the_one_the_rules_require(self) -> None:
        """Two halves of one question, read from one constant."""
        assert ESTABLISHED_PATIENT_TRUST is TrustLevel.CONTROLLED

    def test_a_non_patient_fact_is_not_a_patient(self) -> None:
        case = _case_with(fact("document_id", PATIENT_A, TrustLevel.CONTROLLED))

        assert established_patients(case) == frozenset()


# =================================================================================================
# P4 -- a caller may not act outside their grants
# =================================================================================================


class TestNewAuthorityNeedsACurrentGrant:
    def test_a_caregiver_with_no_grants_cannot_open_a_case(self) -> None:
        directory = two_caregiver_directory()
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)

        with client_for(directory) as client:
            answered = client.post("/api/events", headers=bearer(TOKEN_A), json=EVENT)

        assert answered.status_code == 403
        assert answered.json()["detail"] == NOT_AUTHORISED_FOR_THIS_PATIENT

    def test_a_deactivated_caregiver_cannot_open_a_case(self) -> None:
        directory = two_caregiver_directory()
        directory.deactivate(CAREGIVER_A)

        with client_for(directory) as client:
            assert (
                client.post("/api/events", headers=bearer(TOKEN_A), json=EVENT).status_code == 403
            )

    def test_a_granted_caregiver_can(self) -> None:
        """Vacuity: the refusals above are the grant, not the route being broken."""
        with client_for(two_caregiver_directory()) as client:
            answered = client.post("/api/events", headers=bearer(TOKEN_A), json=EVENT)

        assert answered.status_code == 202

    def test_a_refused_delivery_creates_no_case_at_all(self) -> None:
        directory = two_caregiver_directory()
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)
        store = InMemoryCaseStore()

        with client_for(directory, store) as client:
            client.post("/api/events", headers=bearer(TOKEN_A), json=EVENT)

        from careloop.state.store import CaseNotFound

        with pytest.raises(CaseNotFound):
            store.get(CASE_ID)


class TestRevocationStopsAnApprovalDeadInItsTracks:
    """A never-issued effect. Nothing was dispatched, so nothing may be."""

    def _parked(self, directory):
        from api_helpers import factory_proposing
        from fastapi.testclient import TestClient

        from careloop.api.app import create_app

        store = InMemoryCaseStore()
        app = create_app(
            store,
            context_factory=factory_proposing(intent_email()),
            auth=production_boundary(directory),
        )
        with TestClient(app) as client:
            client.post("/api/events?wait=5", headers=bearer(TOKEN_A), json=EVENT)
        parked = store.get(CASE_ID)
        assert parked.state is CaseState.AWAITING_HUMAN
        approval = parked.pending_approval()
        assert approval is not None
        return store, approval.approval_id

    def test_an_approval_after_revocation_is_refused(self) -> None:
        directory = two_caregiver_directory()
        store, approval_id = self._parked(directory)
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)
        before = store.get(CASE_ID)

        with client_for(directory, store) as client:
            answered = client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        assert answered.status_code == 403
        after = store.get(CASE_ID)
        assert after.version == before.version
        assert after.state is CaseState.AWAITING_HUMAN

    def test_zero_external_effects_follow(self) -> None:
        """P10, at the level that matters: the action record, not the status code."""
        directory = two_caregiver_directory()
        store, approval_id = self._parked(directory)
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)

        with client_for(directory, store) as client:
            client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        after = store.get(CASE_ID)
        assert after.actions == ()
        assert not any(str(entry.kind) == "ACTION_PREPARED" for entry in after.timeline)
        assert not any(str(entry.kind) == "ACTION_EXECUTED" for entry in after.timeline)

    def test_the_same_approval_succeeds_while_the_grant_holds(self) -> None:
        """Vacuity: the refusal is the revocation, not an undecidable approval."""
        directory = two_caregiver_directory()
        store, approval_id = self._parked(directory)

        with client_for(directory, store) as client:
            answered = client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        assert answered.status_code == 202
        assert store.get(CASE_ID).state is CaseState.DONE

    def test_a_recheck_after_revocation_is_refused(self) -> None:
        directory = two_caregiver_directory()
        store, _ = self._parked(directory)
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)

        with client_for(directory, store) as client:
            answered = client.post(f"/api/cases/{CASE_ID}/recheck", headers=bearer(TOKEN_A))

        assert answered.status_code == 403

    def test_reading_the_case_is_still_allowed_after_revocation(self) -> None:
        """Deliberate. A caregiver whose access was withdrawn may still see what was done."""
        directory = two_caregiver_directory()
        store, _ = self._parked(directory)
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)

        with client_for(directory, store) as client:
            assert client.get(f"/api/cases/{CASE_ID}", headers=bearer(TOKEN_A)).status_code == 200
            assert client.get("/api/cases", headers=bearer(TOKEN_A)).status_code == 200


# =================================================================================================
# P11 -- reconciliation is not an authority decision
# =================================================================================================


class TestReconciliationIsNotAnAuthorityDecision:
    """The half a well-meaning edit breaks, because breaking it looks like caution."""

    def test_the_recovery_dependencies_hold_no_directory(self) -> None:
        """Structural, and the reason the property is not a check somebody remembers."""
        from careloop.api.deps import ApiDeps

        assert "directory" not in ApiDeps.__dataclass_fields__
        assert "boundary" not in ApiDeps.__dataclass_fields__
        assert "principal" not in ApiDeps.__dataclass_fields__

    def test_the_recovery_entry_points_name_no_grant_question(self) -> None:
        import inspect

        from careloop.api.deps import ApiDeps

        for method in (ApiDeps.register_recovery, ApiDeps.resume_unsettled, ApiDeps.start_run):
            source = inspect.getsource(method)
            for forbidden in ("may_act_for", "directory", "Principal", "caregiver_id"):
                assert forbidden not in source, f"{method.__name__} names {forbidden}"

    def test_the_reconcile_node_names_no_grant_question(self) -> None:
        import inspect

        from careloop.graph.nodes import ReconcileNode

        source = inspect.getsource(ReconcileNode)
        for forbidden in ("may_act_for", "directory", "Principal", "caregiver"):
            assert forbidden.lower() not in source.lower(), f"ReconcileNode names {forbidden}"

    def test_the_recovery_engine_names_no_grant_question(self) -> None:
        import inspect

        from careloop.graph import recovery

        source = inspect.getsource(recovery)
        for forbidden in ("may_act_for", "Principal", "AuthBoundary", "caregiver"):
            assert forbidden.lower() not in source.lower()

    def test_a_revoked_caregivers_prepared_action_is_still_reconciled(self) -> None:
        """The behavioural half, on a case durably mid-effect with the grant already withdrawn."""
        from saga_helpers import (
            BOOKING_ARGS,
            SAGA_EVENT,
            booking_key,
            executing_with_a_prepared_action,
        )

        from careloop.api.deps import ApiDeps
        from careloop.events.simulator import EventSimulator
        from careloop.graph.context import unsettled_preparation

        directory = two_caregiver_directory()
        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        directory.revoke_grant(CAREGIVER_A, PATIENT_A)
        directory.deactivate(CAREGIVER_A)

        case_id = store.list_by_state(CaseState.EXECUTING)[0].case_id
        deps = ApiDeps(store=store, events=EventSimulator({SAGA_EVENT.event_id: SAGA_EVENT}))

        async def sweep() -> tuple[str, ...]:
            found = deps.resume_unsettled()
            await deps.runs.drain()
            return found

        resumed = asyncio.run(sweep())

        # The sweep found it and scheduled it, having asked nobody anything about authorisation --
        # with the caregiver's grant revoked *and* the caregiver deactivated.
        assert list(resumed) == [case_id]
        prepared = unsettled_preparation(store.get(case_id))
        assert prepared is not None
        assert prepared.idempotency_key == booking_key(case_id, BOOKING_ARGS)

    def test_the_sweep_is_reachable_without_any_identity_at_all(self) -> None:
        """It runs at process start, where there is no request and no principal to consult."""
        import inspect

        from careloop.api.deps import ApiDeps

        assert list(inspect.signature(ApiDeps.resume_unsettled).parameters) == ["self"]


def intent_email():
    from api_helpers import intent

    return intent("send_email", **EMAIL_TO_A)


def _case_with(*facts: Fact):
    store = InMemoryCaseStore()
    opened = open_case(
        store,
        REFERRAL_EVENT,
        caregiver=two_caregiver_directory().caregiver(CAREGIVER_A),
        case_id=CASE_ID,
        now=FIXED_NOW,
    )
    return opened.model_copy(update={"facts": (*opened.facts, *facts)})
