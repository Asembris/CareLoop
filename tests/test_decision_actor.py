"""Who the audit trail says pressed the button.

Patch 4. Before it, ``X-Caregiver`` was an unverified string that any HTTP caller could set to
anything, and it was written to ``Approval.decided_by``, to ``TimelineEvent.actor`` as
``human:<whatever>`` and into the entry's summary. **Authority never moved** -- ``rules.yaml`` reads
``args_approved``, a boolean over ``Case.approval_for(tool_name, args_digest)``, and has never read
``decided_by`` -- so this was audit integrity rather than an authorisation bypass, in the one
direction an append-only trail must not move.

Production now derives the actor from the verified principal, and **refuses the header rather than
ignoring it**. Ignoring would leave two spellings of caregiver identity in one request for the next
reader to disambiguate; refusing tells a client, once, that the field does not exist here. It is a
``400``, not a ``401``: the caller authenticated perfectly well and sent something that has no
meaning on this surface.

Demo mode keeps it, because the judge flow has no verified principal to draw a name from and a
synthetic trail should still record who pressed the button. It is a name a caller typed, exactly as
it always was.

**Nothing about the approval machinery changed** -- not ``approval_id``, not the
``(tool_name, args_digest)`` capability binding, not positional replacement, not a transition. The
tests at the bottom assert that rather than leaving it implied.
"""

from __future__ import annotations

import pytest
from api_helpers import EMAIL, factory_proposing, intent
from fastapi.testclient import TestClient
from identity_helpers import (
    CAREGIVER_A,
    EMAIL_TO_A,
    TOKEN_A,
    bearer,
    production_boundary,
)

from careloop.api.app import create_app
from careloop.state import CaseState, InMemoryCaseStore
from careloop.state.case import ApprovalStatus

CASE_ID = "case-0001"
EVENT = {"case_id": CASE_ID, "event_id": "evt-0001-referral"}


def parked(store, *, production: bool) -> tuple[TestClient, str]:
    """A case parked on a pending ``send_email`` decision, in the mode asked for."""
    # The recipient differs by mode, and that *is* patch 5: a demo case carries the demo
    # caregiver's trusted address and a production case carries Alma's, so an email to the other
    # one is correctly denied rather than being a broken fixture.
    from identity_helpers import two_caregiver_directory

    held = two_caregiver_directory()
    app = create_app(
        store,
        context_factory=factory_proposing(
            intent("send_email", **(EMAIL_TO_A if production else EMAIL)),
            directory=held if production else None,
        ),
        auth=production_boundary(held) if production else None,
    )
    client = TestClient(app)
    client.__enter__()
    client.post("/api/events?wait=5", headers=bearer(TOKEN_A) if production else {}, json=EVENT)
    approval = store.get(CASE_ID).pending_approval()
    assert approval is not None
    return client, approval.approval_id


# =================================================================================================
# P1 -- a spoofed caregiver header has zero authority in production
# =================================================================================================


class TestProductionRecordsTheVerifiedCaregiver:
    def test_the_decision_is_recorded_against_the_verified_caregiver(self) -> None:
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)
        with client:
            answered = client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        assert answered.status_code == 202
        decided = store.get(CASE_ID).approval_for("send_email", _digest(store))
        assert decided is not None
        assert decided.decided_by == CAREGIVER_A
        assert decided.status is ApprovalStatus.APPROVED

    def test_the_timeline_actor_is_the_verified_caregiver(self) -> None:
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)
        with client:
            client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        actors = {entry.actor for entry in store.get(CASE_ID).timeline}
        assert f"human:{CAREGIVER_A}" in actors

    @pytest.mark.parametrize("verb", ["approve", "reject"], ids=["approve", "reject"])
    def test_a_spoofed_header_is_refused_rather_than_ignored(self, verb: str) -> None:
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)
        before = store.get(CASE_ID)

        with client:
            answered = client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/{verb}",
                headers={**bearer(TOKEN_A), "X-Caregiver": "somebody-else"},
            )

        assert answered.status_code == 400
        assert store.get(CASE_ID).version == before.version

    def test_no_spoofed_name_ever_reaches_the_trail(self) -> None:
        """The property, stated over the whole durable record rather than over one field."""
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)

        with client:
            client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers={**bearer(TOKEN_A), "X-Caregiver": "somebody-else"},
            )
            client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        case = store.get(CASE_ID)
        recorded = "\n".join(
            [
                *(entry.summary for entry in case.timeline),
                *(str(entry.actor) for entry in case.timeline),
                *(str(entry.diagnostic or "") for entry in case.timeline),
                *(str(entry.payload) for entry in case.timeline),
                *(str(approval.decided_by) for approval in case.approvals),
            ]
        )
        assert "somebody-else" not in recorded

    def test_a_recheck_is_recorded_against_the_verified_caregiver(self) -> None:
        """The other human operation, and the same rule."""
        store = InMemoryCaseStore()
        client, _ = parked(store, production=True)
        with client:
            spoofed = client.post(
                f"/api/cases/{CASE_ID}/recheck",
                headers={**bearer(TOKEN_A), "X-Caregiver": "somebody-else"},
            )

        assert spoofed.status_code == 400


class TestTheDemoKeepsItsTypedName:
    def test_the_demo_records_the_header(self) -> None:
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=False)
        with client:
            answered = client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers={"X-Caregiver": "Maya"},
            )

        assert answered.status_code == 202
        assert store.get(CASE_ID).approvals[-1].decided_by == "Maya"

    def test_the_demo_still_refuses_an_anonymous_decision(self) -> None:
        """Unchanged, and still a 400: a required field is absent, and nothing was checked."""
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=False)
        with client:
            answered = client.post(f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve")

        assert answered.status_code == 400


class TestTheSchemaTellsTheTruthAboutTheHeader:
    def test_a_demo_app_declares_it_required(self) -> None:
        app = create_app(InMemoryCaseStore())
        assert _header_required(app.openapi(), "x-caregiver")

    def test_a_production_app_declares_it_optional(self) -> None:
        """Declared, and never required.

        Production reads the header in order to *refuse* it, so it is genuinely part of the
        contract -- as a field that must not be sent. Marking it required there would be the
        schema claiming the opposite of the behaviour, which is what
        :func:`careloop.api.app.declare_required_headers` exists to prevent.
        """
        spec = create_app(InMemoryCaseStore(), auth=production_boundary()).openapi()

        assert _header_declared(spec, "x-caregiver")
        assert not _header_required(spec, "x-caregiver")

    def test_a_production_app_declares_the_bearer_scheme(self) -> None:
        app = create_app(InMemoryCaseStore(), auth=production_boundary())
        spec = app.openapi()

        assert spec["security"] == [{"careloopBearer": []}]
        assert spec["components"]["securitySchemes"]["careloopBearer"]["scheme"] == "bearer"

    def test_a_demo_app_declares_no_security(self) -> None:
        """Truthful rather than tidy: nothing is verified there."""
        spec = create_app(InMemoryCaseStore()).openapi()

        assert "security" not in spec
        assert "careloopBearer" not in spec.get("components", {}).get("securitySchemes", {})

    def test_neither_app_declares_authorization_as_a_parameter(self) -> None:
        for app in (
            create_app(InMemoryCaseStore()),
            create_app(InMemoryCaseStore(), auth=production_boundary()),
        ):
            assert not _header_declared(app.openapi(), "authorization")


# =================================================================================================
# The approval machinery is untouched
# =================================================================================================


class TestNothingAboutApprovalIdentityMoved:
    def test_the_request_identity_is_still_positional(self) -> None:
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)
        client.close()

        assert approval_id.startswith("apr-")
        assert approval_id == f"apr-{_requested_seq(store)}"

    def test_the_capability_is_still_the_tool_and_the_digest(self) -> None:
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)
        with client:
            client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        case = store.get(CASE_ID)
        digest = _digest(store)
        assert case.approval_for("send_email", digest) is not None
        assert case.approval_for("upload_document", digest) is None

    def test_the_approval_model_grew_no_identity_field(self) -> None:
        from careloop.state.case import Approval

        assert set(Approval.model_fields) == {
            "approval_id",
            "tool_name",
            "args_digest",
            "reason_code",
            "status",
            "requested_at",
            "decided_at",
            "decided_by",
        }

    def test_the_case_still_reaches_done(self) -> None:
        """Vacuity: the actor change did not quietly break the lifecycle it is recorded in."""
        store = InMemoryCaseStore()
        client, approval_id = parked(store, production=True)
        with client:
            client.post(
                f"/api/cases/{CASE_ID}/decisions/{approval_id}/approve?wait=5",
                headers=bearer(TOKEN_A),
            )

        assert store.get(CASE_ID).state is CaseState.DONE


def _digest(store) -> str:
    return store.get(CASE_ID).approvals[-1].args_digest


def _requested_seq(store) -> int:
    for entry in store.get(CASE_ID).timeline:
        if str(entry.kind) == "APPROVAL_REQUESTED":
            return entry.seq
    raise AssertionError("no APPROVAL_REQUESTED entry")


def _header_declared(spec: dict, name: str) -> bool:
    return any(
        parameter.get("in") == "header" and parameter.get("name", "").lower() == name
        for operations in spec.get("paths", {}).values()
        for operation in operations.values()
        if isinstance(operation, dict)
        for parameter in operation.get("parameters", [])
    )


def _header_required(spec: dict, name: str) -> bool:
    return any(
        parameter.get("in") == "header"
        and parameter.get("name", "").lower() == name
        and parameter.get("required") is True
        for operations in spec.get("paths", {}).values()
        for operation in operations.values()
        if isinstance(operation, dict)
        for parameter in operation.get("parameters", [])
    )
