"""Whose destinations a case may reach.

Patch 5, and the one identity-driven correction to a closed subsystem.

:data:`careloop.graph.trusted.TRUSTED_CONFIG` was a **process-global** tuple seeded onto every case
by ``open_case``. That was correct while there was one synthetic caregiver and became an authority
contradiction the moment there were two: ``send_email.to`` is ``DESTINATION``-class with
``trust_key: caregiver_email``, and ``deny-email-to-untrusted-recipient`` requires it ``TRUSTED``.
So with a global tuple, caregiver B's case would have been permitted to email **caregiver A's
address** -- the only trusted address in the process -- and refused B their own. Not a subtle
degradation: the wrong person receives correspondence about somebody else's patient.

``trusted_facts`` now takes the owning caregiver's record. **Invariant 3 is unchanged**: it is still
the only function that constructs a ``Fact`` at ``TRUSTED``, and ``tests/test_identity_boundary.py``
holds that structurally. What moved is where its *inputs* come from -- CareLoop's own directory,
never a token claim, which is the boundary that stops an identity provider becoming a second
producer.

The mutant is at the bottom: restoring the process-global tuple turns this file red while every
existing trust, policy and destination test stays green.
"""

from __future__ import annotations

import pytest
from helpers import FIXED_NOW
from identity_helpers import (
    CAREGIVER_A,
    CAREGIVER_B,
    DESTINATIONS_A,
    DESTINATIONS_B,
    TOKEN_A,
    TOKEN_B,
    bearer,
    production_client,
    two_caregiver_directory,
)

from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.trusted import CONFIG_SOURCE, DEMO_CAREGIVER, TRUSTED_CONFIG, trusted_facts
from careloop.identity.directory import Caregiver
from careloop.policy import PolicyEngine
from careloop.policy.loader import load_rules
from careloop.policy.verdicts import Verdict
from careloop.provenance.trust import TrustLevel
from careloop.state import InMemoryCaseStore
from careloop.state.case import CaseState

ALMA = two_caregiver_directory().caregiver(CAREGIVER_A)
BO = two_caregiver_directory().caregiver(CAREGIVER_B)


def destinations_on(case) -> set[tuple[str, str]]:
    return {
        (fact.key, fact.value)
        for fact in case.facts
        if fact.trust_level is TrustLevel.TRUSTED and fact.provenance.source_doc == CONFIG_SOURCE
    }


class TestACaseCarriesItsOwnersDestinations:
    def test_almas_case_carries_almas_destinations(self) -> None:
        from careloop.graph.runner import open_case

        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, caregiver=ALMA, case_id="c", now=FIXED_NOW)

        assert destinations_on(case) == set(DESTINATIONS_A)

    def test_bos_case_carries_bos_destinations(self) -> None:
        from careloop.graph.runner import open_case

        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, caregiver=BO, case_id="c", now=FIXED_NOW)

        assert destinations_on(case) == set(DESTINATIONS_B)

    def test_neither_case_carries_the_others(self) -> None:
        """The property, stated as the disjointness that used not to hold at all."""
        from careloop.graph.runner import open_case

        store = InMemoryCaseStore()
        alma = open_case(store, REFERRAL_EVENT, caregiver=ALMA, case_id="a", now=FIXED_NOW)
        bo = open_case(store, REFERRAL_EVENT, caregiver=BO, case_id="b", now=FIXED_NOW)

        assert destinations_on(alma) & destinations_on(bo) == set()

    def test_the_producer_still_requires_a_caregiver(self) -> None:
        with pytest.raises(TypeError):
            trusted_facts()  # type: ignore[call-arg]

    def test_a_caregiver_with_no_destinations_seeds_none(self) -> None:
        """Fail closed: nothing configured means nothing trusted, not the last global default."""
        assert trusted_facts(Caregiver("cg-empty"), now=FIXED_NOW) == ()

    def test_the_demo_caregivers_facts_are_byte_identical_to_before(self) -> None:
        """The judge demo must not move. Its caregiver carries exactly the shipped tuple."""
        seeded = trusted_facts(DEMO_CAREGIVER, now=FIXED_NOW)

        assert tuple((fact.key, fact.value) for fact in seeded) == TRUSTED_CONFIG
        assert {fact.trust_level for fact in seeded} == {TrustLevel.TRUSTED}
        assert {fact.provenance.source_doc for fact in seeded} == {CONFIG_SOURCE}


class TestOneCaregiverCannotReachAnothersDestination:
    """The contradiction itself, through the real policy engine on the shipped rules."""

    @pytest.fixture
    def engine(self) -> PolicyEngine:
        return PolicyEngine(load_rules())

    def _case(self, owner: Caregiver):
        from careloop.graph.runner import open_case
        from careloop.state.machine import transition

        store = InMemoryCaseStore()
        case = open_case(store, REFERRAL_EVENT, caregiver=owner, case_id="c", now=FIXED_NOW)
        for step in (CaseState.GATHERING_EVIDENCE, CaseState.PLANNING):
            case = transition(case, step, reason="t", actor="t", now=FIXED_NOW)
        return case

    def test_bo_cannot_email_almas_address(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            "send_email",
            {"to": dict(DESTINATIONS_A)["caregiver_email"], "subject": "s", "body": "b"},
            self._case(BO),
        )

        assert decision.verdict is Verdict.DENY
        assert decision.arg_trust["to"] is TrustLevel.UNTRUSTED

    def test_bo_can_reach_bos_own_address(self, engine: PolicyEngine) -> None:
        """Vacuity: the denial above is about whose address it is, not about email being refused."""
        decision = engine.evaluate(
            "send_email",
            {"to": dict(DESTINATIONS_B)["caregiver_email"], "subject": "s", "body": "b"},
            self._case(BO),
        )

        assert decision.verdict is Verdict.REQUIRE_HUMAN
        assert decision.arg_trust["to"] is TrustLevel.TRUSTED

    def test_alma_cannot_reach_bos_address_either(self, engine: PolicyEngine) -> None:
        """Symmetric, so the property is not an accident of which caregiver was seeded first."""
        decision = engine.evaluate(
            "send_email",
            {"to": dict(DESTINATIONS_B)["caregiver_email"], "subject": "s", "body": "b"},
            self._case(ALMA),
        )

        assert decision.verdict is Verdict.DENY

    def test_bo_cannot_book_with_almas_provider(self, engine: PolicyEngine) -> None:
        """``provider_id`` is the other ``DESTINATION``, and it moves for the same reason."""
        decision = engine.evaluate(
            "book_appointment",
            {
                "provider_id": dict(DESTINATIONS_A)["provider_id"],
                "patient_id": "PT-8823",
                "slot_start": "2026-09-03T09:30:00",
                "reason": "CareLoop appointment scheduling request.",
            },
            self._case(BO),
        )

        assert decision.verdict is Verdict.DENY
        assert decision.arg_trust["provider_id"] is TrustLevel.UNTRUSTED


class TestNothingAboutProvenanceMoved:
    def test_trusted_facts_is_still_the_only_producer(self) -> None:
        """Restated beside the boundary sweep, because this is the patch that risked it."""
        from pathlib import Path

        import careloop

        root = Path(careloop.__file__).parent
        producing = [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("*.py"))
            if _builds_trusted(path.read_text(encoding="utf-8"))
        ]

        assert producing == ["graph/trusted.py"]

    def test_a_caregiver_record_cannot_be_built_from_a_token_claim(self) -> None:
        """Structural: the directory is the only source, and no credential reaches it."""
        import inspect

        from careloop.identity.directory import InMemoryCaregiverDirectory

        source = inspect.getsource(InMemoryCaregiverDirectory)
        for forbidden in ("jwt", "claim", "token"):
            assert forbidden not in source.lower()

    def test_promotion_still_cannot_manufacture_trusted(self) -> None:
        from careloop.provenance.facts import Fact, Provenance, promote

        fact = Fact(
            key="patient_id",
            value="PT-4417",
            trust_level=TrustLevel.UNTRUSTED,
            provenance=Provenance(source_doc="referral.pdf", extractor="labelled"),
            extracted_at=FIXED_NOW,
        )
        from careloop.provenance.facts import ArgClass, PromotionForbidden

        with pytest.raises(PromotionForbidden):
            promote(
                fact,
                to=TrustLevel.TRUSTED,
                arg_class=ArgClass.VALUE,
                validator="patient_id_format",
            )


class TestTheDemoIsUnaffectedEndToEnd:
    def test_a_demo_case_reaches_the_same_destinations_it_always_did(self) -> None:
        from fastapi.testclient import TestClient

        from careloop.api.app import create_app

        store = InMemoryCaseStore()
        with TestClient(create_app(store)) as client:
            client.post(
                "/api/events?wait=5", json={"case_id": "case-0001", "event_id": "evt-0001-referral"}
            )

        assert destinations_on(store.get("case-0001")) == set(TRUSTED_CONFIG)


class TestOwnershipAndDestinationsTravelTogether:
    def test_a_production_case_is_seeded_from_its_own_owners_record(self) -> None:
        """End to end over HTTP: who opened the case decides what the case may reach."""
        store = InMemoryCaseStore()
        with production_client(store) as client:
            client.post(
                "/api/events",
                headers=bearer(TOKEN_B),
                json={"case_id": "case-bo", "event_id": "evt-0001-referral"},
            )
            client.post(
                "/api/events",
                headers=bearer(TOKEN_A),
                json={"case_id": "case-alma", "event_id": "evt-0001-referral"},
            )

        assert destinations_on(store.get("case-bo")) == set(DESTINATIONS_B)
        assert destinations_on(store.get("case-alma")) == set(DESTINATIONS_A)


def _builds_trusted(source: str) -> bool:
    import ast

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        callee = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if callee != "Fact":
            continue
        for keyword in node.keywords:
            if keyword.arg == "trust_level" and getattr(keyword.value, "attr", "") == "TRUSTED":
                return True
    return False
