"""The first-run patient boundary, and the two things it must not break.

**The defect, reproduced before this file existed.** Caregiver A is granted `PT-9999` and nothing
else. A delivers the fixture referral, whose documents establish `PT-4417`. At the moment the
delivery is accepted no document has been read, so the only grant question the route can ask is
*does this caregiver hold **some** grant* -- and A does. The run then extracted `PT-4417`, promoted
it to `CONTROLLED`, planned a booking for it and dispatched: measured, **202, DONE, one
ActionRecord, one appointment at the clinic**, for a patient A may not act for. The check was in
the right place for the second run and there was no check at all on the first.

**What closes it.** :class:`careloop.graph.authority.PatientAuthority` is asked *live*, inside the
run, after evidence has established who the case is about. Twice, independently: the gate node
parks properly, and the execute node fails closed for the path that never reaches the gate. Neither
knows what a caregiver is -- the protocol takes patient identifiers and answers yes or no.

**And the two things that must survive.** An authorised first run still books and verifies exactly
as before; and a *revoked* caregiver's ambiguous, already-dispatched effect is still reconciled,
because an appointment that may exist does not stop existing when a grant is withdrawn.
"""

from __future__ import annotations

import ast
from pathlib import Path

from api_helpers import intent, planner_proposing, triage_agent
from clinic_helpers import clinic_server
from fastapi.testclient import TestClient
from helpers import BOOKING_REASON, FIXED_NOW
from identity_helpers import (
    CAREGIVER_A,
    DESTINATIONS_A,
    ISSUER,
    TOKEN_A,
    bearer,
    production_boundary,
    stub_verifier,
)

import careloop
from careloop.api.app import create_app
from careloop.api.auth import production_auth
from careloop.api.deps import patient_authority_for
from careloop.graph.authority import AUTHORISES_NOBODY
from careloop.graph.runner import context_for
from careloop.identity.directory import (
    Caregiver,
    Grant,
    InMemoryCaregiverDirectory,
    SubjectMapping,
)
from careloop.identity.patients import CaregiverPatientAuthority
from careloop.policy.verdicts import ReasonCode
from careloop.state import CaseState, InMemoryCaseStore

CASE_ID = "case-0001"
EVENT = {"case_id": CASE_ID, "event_id": "evt-0001-referral"}

#: What the fixture referral actually establishes.
PATIENT_IN_THE_DOCUMENTS = "PT-4417"

#: The only patient caregiver A is granted in this file. Deliberately not the one above.
PATIENT_A_MAY_ACT_FOR = "PT-9999"

BOOKING = {
    "provider_id": dict(DESTINATIONS_A)["provider_id"],
    "patient_id": PATIENT_IN_THE_DOCUMENTS,
    "slot_start": "2026-09-03T09:30:00",
    "reason": BOOKING_REASON,
}


def directory_granting(*patients: str) -> InMemoryCaregiverDirectory:
    return InMemoryCaregiverDirectory(
        caregivers={CAREGIVER_A: Caregiver(CAREGIVER_A, DESTINATIONS_A)},
        grants=tuple(Grant(CAREGIVER_A, patient) for patient in patients),
        subjects=(SubjectMapping(ISSUER, "sub-alma", CAREGIVER_A),),
    )


def booking_app(store, clinic, directory):
    """A production app that would book, if the boundary let it."""

    def build(store_, inbound, case_id):
        return context_for(
            store_,
            inbound,
            case_id=case_id,
            triage=triage_agent(),
            planner=planner_proposing(intent("book_appointment", **BOOKING)),
            now=lambda: FIXED_NOW,
            clinic=clinic,
            **patient_authority_for(directory, store_, case_id),
        )

    return create_app(
        store,
        context_factory=build,
        clinic=clinic,
        auth=production_auth(verifier=stub_verifier(), directory=directory),
    )


def patients_on(case) -> set[str]:
    return {fact.value for fact in case.facts if fact.key == "patient_id"}


def reason_codes(case) -> list[str]:
    return [
        entry.payload["reason_code"] for entry in case.timeline if "reason_code" in entry.payload
    ]


# =================================================================================================
# The reproduction, now closed
# =================================================================================================


class TestAFirstRunCannotActForAPatientTheCaregiverIsNotGranted:
    """The exact scenario, driven through the real routes against a real clinic."""

    def test_no_appointment_is_made(self) -> None:
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, world):
            from careloop.tools.clinic import ClinicClient

            app = booking_app(
                store, ClinicClient(base_url), directory_granting(PATIENT_A_MAY_ACT_FOR)
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

            assert world.appointments() == ()

    def test_the_case_discovered_the_patient_and_still_did_nothing(self) -> None:
        """Non-vacuity for the whole file: the run really did get as far as establishing PT-4417."""
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, _world):
            from careloop.tools.clinic import ClinicClient

            app = booking_app(
                store, ClinicClient(base_url), directory_granting(PATIENT_A_MAY_ACT_FOR)
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

        case = store.get(CASE_ID)
        assert PATIENT_IN_THE_DOCUMENTS in patients_on(case)
        assert case.actions == ()
        assert case.state is not CaseState.DONE

    def test_nothing_was_even_prepared(self) -> None:
        """Load-bearing: an ``ACTION_PREPARED`` entry is what a recovery finds and dispatches.

        Refusing after writing one would leave an unsettled intent for the startup sweep to act on,
        which is this check being routed around by the recovery path.
        """
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, _world):
            from careloop.tools.clinic import ClinicClient

            app = booking_app(
                store, ClinicClient(base_url), directory_granting(PATIENT_A_MAY_ACT_FOR)
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

        kinds = {str(entry.kind) for entry in store.get(CASE_ID).timeline}
        assert "ACTION_PREPARED" not in kinds
        assert "ACTION_EXECUTED" not in kinds

    def test_the_case_parks_for_a_person_and_says_why(self) -> None:
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, _world):
            from careloop.tools.clinic import ClinicClient

            app = booking_app(
                store, ClinicClient(base_url), directory_granting(PATIENT_A_MAY_ACT_FOR)
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

        case = store.get(CASE_ID)
        assert case.state is CaseState.AWAITING_HUMAN
        assert str(ReasonCode.PATIENT_AUTHORITY_DENIED) in reason_codes(case)

    def test_no_approval_is_raised(self) -> None:
        """An approval authorises one exact call, and the call is not what was refused."""
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, _world):
            from careloop.tools.clinic import ClinicClient

            app = booking_app(
                store, ClinicClient(base_url), directory_granting(PATIENT_A_MAY_ACT_FOR)
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

        assert store.get(CASE_ID).pending_approval() is None

    def test_the_same_run_books_when_the_grant_covers_the_patient(self) -> None:
        """Vacuity, and P9: the refusal is the grant, not the run being broken."""
        store = InMemoryCaseStore()
        with clinic_server() as (base_url, world):
            from careloop.tools.clinic import ClinicClient

            app = booking_app(
                store, ClinicClient(base_url), directory_granting(PATIENT_IN_THE_DOCUMENTS)
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

            assert len(world.appointments()) == 1

        case = store.get(CASE_ID)
        assert case.state is CaseState.DONE
        assert len(case.actions) == 1


class TestRevocationBetweenEvidenceAndDispatchIsCaught:
    """The answer is live, so a grant withdrawn mid-run takes effect."""

    def test_a_grant_withdrawn_before_the_execute_node_stops_the_effect(self) -> None:
        """The directory is mutated *while the run holds it*, which a snapshot would not see."""
        directory = directory_granting(PATIENT_IN_THE_DOCUMENTS)
        store = InMemoryCaseStore()

        class RevokesOnFirstQuestion:
            """Answers the gate honestly, then withdraws before the executor asks again.

            Stands in for a caregiver whose access is removed in the seconds a run takes. What it
            proves is that the second question is really asked -- a snapshot taken at run start
            would answer yes both times.
            """

            def __init__(self) -> None:
                self.asked = 0
                self.real = CaregiverPatientAuthority(directory, CAREGIVER_A)

            def allows(self, patient_ids: frozenset[str]) -> bool:
                self.asked += 1
                answer = self.real.allows(patient_ids)
                directory.revoke_grant(CAREGIVER_A, PATIENT_IN_THE_DOCUMENTS)
                return answer

        authority = RevokesOnFirstQuestion()

        def build(store_, inbound, case_id):
            return context_for(
                store_,
                inbound,
                case_id=case_id,
                triage=triage_agent(),
                planner=planner_proposing(intent("book_appointment", **BOOKING)),
                now=lambda: FIXED_NOW,
                clinic=clinic,
                patient_authority=authority,
            )

        with clinic_server() as (base_url, world):
            from careloop.tools.clinic import ClinicClient

            clinic = ClinicClient(base_url)
            app = create_app(
                store,
                context_factory=build,
                clinic=clinic,
                auth=production_boundary(directory),
            )
            with TestClient(app) as client:
                client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

            assert world.appointments() == ()

        # Asked twice: once by the gate, which allowed, and once by the executor, which did not.
        assert authority.asked == 2
        assert store.get(CASE_ID).actions == ()

    def test_the_executor_is_the_one_that_refused(self) -> None:
        """So the second enforcement point is proven live rather than merely present."""
        directory = directory_granting(PATIENT_IN_THE_DOCUMENTS)
        store = InMemoryCaseStore()

        class RevokesAfterTheGate:
            def __init__(self) -> None:
                self.asked = 0

            def allows(self, patient_ids: frozenset[str]) -> bool:
                self.asked += 1
                return self.asked == 1

        authority = RevokesAfterTheGate()

        def build(store_, inbound, case_id):
            return context_for(
                store_,
                inbound,
                case_id=case_id,
                triage=triage_agent(),
                planner=planner_proposing(intent("book_appointment", **BOOKING)),
                now=lambda: FIXED_NOW,
                patient_authority=authority,
            )

        with TestClient(
            create_app(store, context_factory=build, auth=production_boundary(directory))
        ) as client:
            client.post("/api/events?wait=10", headers=bearer(TOKEN_A), json=EVENT)

        case = store.get(CASE_ID)
        assert case.actions == ()
        assert case.state is CaseState.FAILED
        assert "ACTION_PREPARED" not in {str(entry.kind) for entry in case.timeline}


# =================================================================================================
# What must not change
# =================================================================================================


class TestReconciliationStillDoesNotAsk:
    """An already-issued effect is settled whatever the current grant says."""

    def test_a_revoked_caregivers_prepared_action_still_reconciles(self) -> None:
        """The behavioural half, driven through the real reconcile node with a denying authority."""
        import asyncio

        from saga_helpers import (
            BOOKING_ARGS,
            SAGA_EVENT,
            booking_key,
            executing_with_a_prepared_action,
        )

        from careloop.graph.runner import run_case
        from careloop.tools.clinic import ClinicClient

        store = InMemoryCaseStore()
        executing_with_a_prepared_action(store)
        case_id = store.list_by_state(CaseState.EXECUTING)[0].case_id

        with clinic_server() as (base_url, world):
            context = context_for(
                store,
                SAGA_EVENT,
                case_id=case_id,
                triage=triage_agent(),
                planner=planner_proposing(),
                now=lambda: FIXED_NOW,
                clinic=ClinicClient(base_url),
                # **Denies everything.** If reconciliation asked, nothing would settle.
                patient_authority=AUTHORISES_NOBODY,
            )
            result = asyncio.run(run_case(context))

            assert len(world.appointments()) == 1

        assert "reconcile" in result.executed_nodes
        settled = store.get(case_id)
        assert len(settled.actions) == 1
        assert settled.actions[0].idempotency_key == booking_key(case_id, BOOKING_ARGS)
        assert settled.state is CaseState.DONE

    def test_the_reconcile_node_names_no_authority(self) -> None:
        import inspect

        from careloop.graph.nodes import ReconcileNode

        source = inspect.getsource(ReconcileNode)
        for forbidden in ("patient_authority", "PatientAuthority", "allows"):
            assert forbidden not in source, f"ReconcileNode names {forbidden}"

    def test_the_recovery_engine_names_no_authority(self) -> None:
        import inspect

        from careloop.graph import recovery

        source = inspect.getsource(recovery)
        for forbidden in ("patient_authority", "PatientAuthority"):
            assert forbidden not in source

    def test_the_recovery_dependencies_still_hold_no_directory(self) -> None:
        from careloop.api.deps import ApiDeps

        assert "directory" not in ApiDeps.__dataclass_fields__


class TestThereIsNoWayToAllowEverything:
    """Fail-closed by construction, swept so a permissive sibling cannot appear."""

    def test_the_shipped_default_refuses_everything(self) -> None:
        assert not AUTHORISES_NOBODY.allows(frozenset())
        assert not AUTHORISES_NOBODY.allows(frozenset({PATIENT_IN_THE_DOCUMENTS}))

    def test_a_bare_run_context_holds_the_refusing_authority(self) -> None:
        from careloop.graph.context import RunContext

        assert RunContext(case_id="c", store=InMemoryCaseStore()).patient_authority is (
            AUTHORISES_NOBODY
        )

    def test_no_shipped_implementation_always_returns_true(self) -> None:
        """A sweep: "there is no allow-all" is a property of the package, not of a file."""
        offenders = [
            path.relative_to(_root()).as_posix()
            for path in sorted(_root().rglob("*.py"))
            if _always_allows(path.read_text(encoding="utf-8"))
        ]

        assert offenders == [], (
            f"{offenders} define an allows() that can only return True. A patient authority that "
            f"permits everything is a default somebody will reach for"
        )

    def test_the_sweep_would_catch_one(self) -> None:
        """Vacuity: the matcher recognises the shape it is looking for."""
        assert _always_allows("class X:\n    def allows(self, p):\n        return True\n")
        assert not _always_allows("class X:\n    def allows(self, p):\n        return False\n")
        assert not _always_allows("class X:\n    def allows(self, p):\n        return bool(p)\n")

    def test_an_unowned_case_is_refused(self) -> None:
        bound = CaregiverPatientAuthority(directory_granting(PATIENT_IN_THE_DOCUMENTS), None)

        assert not bound.allows(frozenset({PATIENT_IN_THE_DOCUMENTS}))
        assert not bound.allows(frozenset())

    def test_every_established_patient_must_be_granted_not_merely_one(self) -> None:
        bound = CaregiverPatientAuthority(directory_granting(PATIENT_IN_THE_DOCUMENTS), CAREGIVER_A)

        assert bound.allows(frozenset({PATIENT_IN_THE_DOCUMENTS}))
        assert not bound.allows(frozenset({PATIENT_IN_THE_DOCUMENTS, PATIENT_A_MAY_ACT_FOR}))

    def test_a_caseless_patient_set_asks_whether_the_caregiver_may_act_at_all(self) -> None:
        """An ``upload_document`` case establishes no patient; it is not therefore unauthorised."""
        granting = CaregiverPatientAuthority(
            directory_granting(PATIENT_IN_THE_DOCUMENTS), CAREGIVER_A
        )
        ungranted = CaregiverPatientAuthority(directory_granting(), CAREGIVER_A)

        assert granting.allows(frozenset())
        assert not ungranted.allows(frozenset())


class TestTheGraphStillKnowsNothingAboutIdentity:
    def test_the_authority_protocol_names_no_identity(self) -> None:
        source = (_root() / "graph" / "authority.py").read_text(encoding="utf-8")
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Name | ast.Attribute)
        }
        assert not (
            names & {"Principal", "Caregiver", "CaregiverDirectory", "may_act_for", "Grant"}
        )

    def test_the_execute_and_gate_nodes_name_no_identity(self) -> None:
        source = (_root() / "graph" / "nodes.py").read_text(encoding="utf-8")
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Name | ast.Attribute)
        }
        assert not (names & {"Principal", "Caregiver", "caregiver_id", "may_act_for", "directory"})


def _root() -> Path:
    return Path(careloop.__file__).parent


def _always_allows(source: str) -> bool:
    """Whether this module defines an ``allows`` whose body is nothing but ``return True``."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or node.name != "allows":
            continue
        body = [step for step in node.body if not isinstance(step, ast.Expr)]
        if (
            len(body) == 1
            and isinstance(body[0], ast.Return)
            and isinstance(body[0].value, ast.Constant)
            and body[0].value.value is True
        ):
            return True
    return False
