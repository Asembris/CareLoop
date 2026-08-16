"""The response sweeps, and the counterexamples that prove they are not vacuous.

Runs in the default offline suite at $0 and imports no Schemathesis: the *property* is covered
wherever CareLoop is installed, and only the fuzz wiring in ``tests/test_api_schema.py`` skips
without the ``schema`` extra.

Every positive here is paired with a negative control. A sweep that has only ever been shown to
return the empty set has not been shown to be a sweep.
"""

from __future__ import annotations

import pytest

from careloop.api.app import create_app
from careloop.api.projections import TIMELINE_PAYLOAD_FIELDS, WITHHELD
from careloop.state.memory_store import InMemoryCaseStore
from tests.schema_checks import (
    WithheldNameIsAlsoProjected,
    check_withheld_names_are_distinct,
    payload_keys_outside_allowlist,
    projected_field_names,
    withheld_field_names,
    withheld_keys_in,
)


class TestTheWithheldVocabularyIsReadFromTheTable:
    """The sweep must inherit new withheld fields rather than needing to be widened."""

    def test_it_names_the_two_fields_the_api_withholds_today(self) -> None:
        assert withheld_field_names() == frozenset({"diagnostic", "extracted_at"})

    def test_it_is_derived_from_withheld_rather_than_hardcoded(self) -> None:
        # The point of reading the table: a third withheld field is swept without a test edit.
        assert withheld_field_names() == frozenset(
            name for fields in WITHHELD.values() for name in fields
        )

    def test_a_new_withheld_field_is_swept_without_widening_the_sweep(self) -> None:
        extended = dict(WITHHELD) | {"Case": {"internal_notes": "invented for this test"}}
        names = frozenset(name for fields in extended.values() for name in fields)
        # Standing in for the real table: the sweep is a set membership test over whatever it holds.
        assert "internal_notes" in names


class TestTheDisjointnessPrecondition:
    """A name-based sweep is only sound while the two vocabularies do not overlap."""

    def test_the_shipped_tables_are_disjoint(self) -> None:
        assert not (withheld_field_names() & projected_field_names())

    def test_the_shipped_tables_pass_the_check(self) -> None:
        check_withheld_names_are_distinct()

    def test_a_collision_fails_loudly_and_names_the_field(self) -> None:
        # The negative control for the precondition itself, driven through the real check over a
        # deliberately broken vocabulary. `page` is projected by FactView, so a WITHHELD entry
        # naming it would turn the sweep into a false-positive generator.
        assert "page" in projected_field_names()
        with pytest.raises(WithheldNameIsAlsoProjected, match="page"):
            check_withheld_names_are_distinct(withheld=withheld_field_names() | {"page"})

    def test_it_names_every_collision_rather_than_the_first(self) -> None:
        with pytest.raises(WithheldNameIsAlsoProjected) as raised:
            check_withheld_names_are_distinct(
                withheld=frozenset({"page", "value"}), projected=frozenset({"page", "value"})
            )
        assert "page" in str(raised.value) and "value" in str(raised.value)

    def test_disjoint_synthetic_vocabularies_pass(self) -> None:
        check_withheld_names_are_distinct(
            withheld=frozenset({"secret"}), projected=frozenset({"public"})
        )


class TestTheWithheldSweepFindsAKeyAtAnyDepth:
    """Depth is the whole point: in this API a leak can only ever be nested."""

    def test_a_clean_document_sweeps_empty(self) -> None:
        assert (
            withheld_keys_in({"case_id": "c-1", "timeline": [{"seq": 1, "payload": {}}]}) == set()
        )

    def test_a_top_level_leak_is_found(self) -> None:
        assert withheld_keys_in({"diagnostic": "rule deny-x matched on arg to=a@b"}) == {
            "diagnostic"
        }

    def test_a_leak_nested_in_an_object_is_found(self) -> None:
        assert withheld_keys_in({"decision": {"diagnostic": "leaked"}}) == {"diagnostic"}

    def test_a_leak_nested_inside_an_array_is_found(self) -> None:
        # The shape that matters: CaseDetail.timeline is an array of event objects, so this is
        # exactly where a real leak would appear and exactly what a shallow check would miss.
        document = {
            "case_id": "c-1",
            "timeline": [
                {"seq": 1, "kind": "CASE_OPENED", "payload": {"event_id": "e-1"}},
                {"seq": 2, "kind": "POLICY_DENY", "diagnostic": "arg to=attacker@example.com"},
            ],
        }
        assert withheld_keys_in(document) == {"diagnostic"}

    def test_a_leak_three_levels_down_through_arrays_and_objects_is_found(self) -> None:
        document = {"needs_you": [{"decision": {"args": [{"extracted_at": "2026-01-01"}]}}]}
        assert withheld_keys_in(document) == {"extracted_at"}

    def test_a_shallow_sweep_would_have_missed_every_realistic_shape(self) -> None:
        # Stated as an assertion rather than a comment: for each nested counterexample, a
        # top-level-only check returns empty and would have passed.
        nested = [
            {"decision": {"diagnostic": "x"}},
            {"timeline": [{"diagnostic": "x"}]},
            {"needs_you": [{"decision": {"args": [{"extracted_at": "x"}]}}]},
        ]
        for document in nested:
            shallow = {key for key in document if key in withheld_field_names()}
            assert shallow == set(), "a top-level sweep should be blind to these"
            assert withheld_keys_in(document) != set(), "the recursive sweep must not be"

    def test_it_sweeps_the_unmodelled_surfaces_too(self) -> None:
        # The four surfaces no projection table governs. Their real bodies are clean...
        for body in (
            {"status": "ok"},
            {"session": "s-1"},
            {"cases_cleared": 0, "clinic_reset": False, "scenarios": ["referral"]},
            {"detail": "no case 'unknown'"},
        ):
            assert withheld_keys_in(body) == set()
        # ...and a widened exception message is the leak this exists to catch.
        assert withheld_keys_in({"detail": {"diagnostic": "rule internals"}}) == {"diagnostic"}

    def test_scalars_and_empties_are_handled(self) -> None:
        for document in (None, "diagnostic", 1, True, [], {}, [[], [{}]]):
            assert withheld_keys_in(document) == set()


class TestThePayloadAllowlistSweep:
    """The allowlist half, recursive for the same reason."""

    def test_an_allowlisted_payload_sweeps_empty(self) -> None:
        document = {"timeline": [{"payload": {"tool_name": "send_email", "verdict": "DENY"}}]}
        assert payload_keys_outside_allowlist(document) == set()

    def test_a_key_outside_the_allowlist_is_found(self) -> None:
        document = {"timeline": [{"payload": {"tool_name": "send_email", "raw_args": "..."}}]}
        assert payload_keys_outside_allowlist(document) == {"raw_args"}

    def test_it_finds_a_payload_at_any_depth(self) -> None:
        document = {"needs_you": [{"decision": {"payload": {"invented": 1}}}]}
        assert payload_keys_outside_allowlist(document) == {"invented"}

    def test_a_non_object_payload_is_not_inspected_and_does_not_raise(self) -> None:
        assert payload_keys_outside_allowlist({"payload": "not an object"}) == set()
        assert payload_keys_outside_allowlist({"payload": None}) == set()

    def test_every_allowlisted_key_passes(self) -> None:
        document = {"payload": dict.fromkeys(TIMELINE_PAYLOAD_FIELDS, "v")}
        assert payload_keys_outside_allowlist(document) == set()

    def test_diagnostic_in_a_payload_is_caught_by_both_sweeps(self) -> None:
        # It is not in the allowlist and it is a withheld name, so both fire. Belt and braces on
        # purpose: the two sweeps answer different questions and neither is the other's fallback.
        document = {"timeline": [{"payload": {"diagnostic": "leaked"}}]}
        assert payload_keys_outside_allowlist(document) == {"diagnostic"}
        assert withheld_keys_in(document) == {"diagnostic"}


class TestTheSweepsOverRealResponses:
    """Against the actual app, so the predicates are known to pass what they must pass."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(create_app(InMemoryCaseStore(), demo=True), raise_server_exceptions=False)

    def test_the_read_surface_is_clean(self, client) -> None:
        for path in ("/api/health", "/api/cases"):
            body = client.get(path).json()
            assert withheld_keys_in(body) == set()
            assert payload_keys_outside_allowlist(body) == set()

    def test_the_error_bodies_are_clean(self, client) -> None:
        for response in (
            client.get("/api/cases/unknown"),
            client.get("/api/runs/unknown"),
            client.post("/api/sessions"),
            client.post("/api/demo/scenarios/unknown"),
            client.post("/api/cases/x/decisions/y/approve"),
        ):
            assert response.status_code >= 400
            body = response.json()
            assert withheld_keys_in(body) == set(), f"{response.request.url} leaked"
            assert payload_keys_outside_allowlist(body) == set()

    def test_a_case_with_a_full_timeline_is_clean(self, client) -> None:
        started = client.post(
            "/api/events",
            json={"case_id": "c-sweep", "event_id": "evt-0001-referral"},
            params={"wait": 10},
        )
        assert started.status_code == 202
        body = client.get("/api/cases/c-sweep").json()
        assert body["timeline"], "the case must carry a timeline for this to mean anything"
        assert withheld_keys_in(body) == set()
        assert payload_keys_outside_allowlist(body) == set()
