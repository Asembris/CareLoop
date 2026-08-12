"""What the API is allowed to say about a case, enforced rather than intended.

Four properties, each with a mutation that turns it red:

1. The three sections partition every ``CaseState``. A state in no section is a case that has
   silently vanished from the one screen a caregiver looks at.
2. ``TimelineEvent.diagnostic`` never reaches a browser, by any route.
3. Every stored field is projected or explicitly withheld — nothing disappears by omission.
4. Every response field names where it comes from — nothing is invented.

The cases these run against come out of the real graph on the mock model. A projection test that
assembled its own timeline would agree with itself about what a timeline looks like.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from api_helpers import CASE_ID, EMAIL, finished_case, parked_store, poisoned_case
from helpers import FIXED_NOW, make_case

import careloop
from careloop.api import projections
from careloop.api.models import CaseCard, CaseDetail, FactView, ResponseModel
from careloop.api.projections import (
    COMPLETED,
    NEEDS_YOU,
    SECTION_ORDER,
    SECTIONS,
    SOURCE_MODELS,
    SOURCES,
    TIMELINE_PAYLOAD_FIELDS,
    WITHHELD,
    WORKING,
    SectionsAreNotAPartition,
    UndecidedField,
    case_card,
    case_detail,
    check_every_response_field_has_a_source,
    check_every_stored_field_is_decided,
    check_sections_partition_every_state,
    decision_view,
    payload_keys_in,
    section_for,
    timeline_event_view,
)
from careloop.state.case import (
    Approval,
    ApprovalStatus,
    Case,
    CaseState,
    TimelineEvent,
    TimelineKind,
)
from careloop.state.machine import append_event
from careloop.tools.idempotency import args_digest

# =================================================================================================
# 1 — the three sections
# =================================================================================================


class TestTheSectionsPartitionEveryState:
    def test_the_shipped_partition_is_total_and_disjoint(self) -> None:
        check_sections_partition_every_state()

    @pytest.mark.parametrize("state", list(CaseState), ids=str)
    def test_every_state_resolves_to_exactly_one_section(self, state: CaseState) -> None:
        section = section_for(state)

        assert section in SECTION_ORDER
        assert [name for name in SECTION_ORDER if state in SECTIONS[name]] == [section]

    def test_the_three_sections_are_the_ones_the_dashboard_has(self) -> None:
        assert set(SECTIONS) == {NEEDS_YOU, WORKING, COMPLETED}

    def test_awaiting_human_is_the_only_thing_that_needs_you(self) -> None:
        assert SECTIONS[NEEDS_YOU] == {CaseState.AWAITING_HUMAN}

    def test_a_failed_case_is_completed_rather_than_still_working(self) -> None:
        """It is finished. Leaving it in Working would show work that will never move."""
        assert section_for(CaseState.FAILED) == COMPLETED
        assert section_for(CaseState.DONE) == COMPLETED

    def test_a_state_in_no_section_is_caught(self) -> None:
        """The mutation that matters: an eleventh CaseState nobody put on the dashboard."""
        holed = {
            NEEDS_YOU: frozenset({CaseState.AWAITING_HUMAN}),
            WORKING: frozenset({CaseState.PLANNING}),
            COMPLETED: frozenset({CaseState.DONE}),
        }

        with pytest.raises(SectionsAreNotAPartition, match="in no section"):
            check_sections_partition_every_state(holed)

    def test_a_state_in_two_sections_is_caught(self) -> None:
        overlapping = {
            NEEDS_YOU: frozenset(CaseState),
            WORKING: frozenset(CaseState),
            COMPLETED: frozenset(CaseState),
        }

        with pytest.raises(SectionsAreNotAPartition, match="in more than one section"):
            check_sections_partition_every_state(overlapping)

    def test_the_check_accepts_a_partition_that_really_is_one(self) -> None:
        """Otherwise it rejects everything and the two mutations above prove nothing."""
        check_sections_partition_every_state(
            {
                NEEDS_YOU: frozenset({CaseState.AWAITING_HUMAN}),
                COMPLETED: frozenset({CaseState.DONE, CaseState.FAILED}),
                WORKING: frozenset(CaseState)
                - {CaseState.AWAITING_HUMAN, CaseState.DONE, CaseState.FAILED},
            }
        )


# =================================================================================================
# 2 — the diagnostic never reaches a browser
# =================================================================================================

MARKER = "DIAGNOSTIC-LEAK-CANARY-8f21"


def _proposed(case: Case, tool_name: str, args: dict[str, str]) -> Case:
    """Append the ``PLAN_PROPOSED`` entry the plan node writes."""
    return append_event(
        case,
        kind=TimelineKind.PLAN_PROPOSED,
        actor="planner",
        summary=f"proposed {tool_name}",
        payload={"tool_name": tool_name, "args": dict(args), "rationale": ""},
        now=FIXED_NOW,
    )


def marked(case: Case) -> Case:
    """Rewrite every diagnostic on ``case`` to carry a token nothing else could produce.

    Rewritten rather than searched for, because the real diagnostics legitimately share their text
    with summaries and payloads — a sweep for "deny-email-to-untrusted-recipient" would fire on the
    ``matched_rule_id`` the API exposes on purpose. A canary distinguishes the field from its
    contents, which is the property being tested.
    """
    return case.model_copy(
        update={
            "timeline": tuple(
                event.model_copy(update={"diagnostic": f"{MARKER} {event.diagnostic or ''}"})
                for event in case.timeline
            )
        }
    )


def serialized(case: Case) -> str:
    """Every projection of ``case``, as the bytes a browser would receive."""
    return json.dumps(
        {
            "detail": case_detail(case).model_dump(mode="json"),
            "card": case_card(case).model_dump(mode="json"),
            "decision": (view.model_dump(mode="json") if (view := decision_view(case)) else None),
            "timeline": [
                timeline_event_view(event).model_dump(mode="json") for event in case.timeline
            ],
        }
    )


class TestTheDiagnosticNeverReachesABrowser:
    @pytest.mark.parametrize("build", [finished_case, poisoned_case], ids=["finished", "poisoned"])
    def test_no_projection_of_a_real_case_carries_it(self, build) -> None:
        assert MARKER not in serialized(marked(build()))

    @pytest.mark.parametrize("build", [finished_case, poisoned_case], ids=["finished", "poisoned"])
    def test_the_markers_really_are_on_the_case(self, build) -> None:
        """Vacuity. Without this the sweep passes on a case that never had a diagnostic."""
        case = marked(build())

        assert len(case.timeline) > 10
        assert all(MARKER in event.diagnostic for event in case.timeline)

    def test_the_real_diagnostics_are_not_empty_either(self) -> None:
        """The withholding is a separation, not an absence: the audit trail keeps the detail."""
        case = poisoned_case()

        denials = [event for event in case.timeline if event.kind is TimelineKind.POLICY_DENY]

        assert denials
        assert all(event.diagnostic for event in denials)
        assert any("deny-" in event.diagnostic for event in denials)

    def test_no_response_model_declares_a_diagnostic_field(self) -> None:
        offenders = sorted(
            f"{view.__name__}.diagnostic" for view in SOURCES if "diagnostic" in view.model_fields
        )

        assert offenders == []

    def test_the_sweep_would_catch_a_projection_that_leaked_it(self) -> None:
        """Vacuity for the sweep itself: a view that does carry the diagnostic is detected."""

        class Leaky(ResponseModel):
            seq: int
            diagnostic: str

        event = marked(finished_case()).timeline[0]
        leaked = json.dumps(Leaky(seq=event.seq, diagnostic=event.diagnostic).model_dump())

        assert MARKER in leaked


# =================================================================================================
# 2b — the payload allowlist
# =================================================================================================


def payload_keys_written_under(root: Path) -> set[str]:
    """Every string key any ``payload={...}`` literal in the package writes.

    Read from the syntax tree rather than by running a scenario: a writer only one rare path
    reaches would otherwise never appear, and the allowlist is meant to cover what the code *can*
    emit rather than what one run happened to.
    """
    keys: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "payload" and isinstance(keyword.value, ast.Dict):
                    keys.update(
                        key.value
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
    return keys


class TestThePayloadAllowlistIsHonest:
    def test_the_scan_finds_the_writers_it_claims_to(self) -> None:
        written = payload_keys_written_under(Path(careloop.__file__).parent)

        assert len(written) > 20
        assert {"tool_name", "reason_code", "matched_rule_id", "event_id"} <= written

    def test_every_key_a_shipped_writer_emits_is_decided(self) -> None:
        """An allowlist drops silently, so a key nobody listed must fail loudly instead."""
        written = payload_keys_written_under(Path(careloop.__file__).parent)

        undecided = sorted(written - TIMELINE_PAYLOAD_FIELDS)

        assert undecided == [], (
            f"{undecided} are written to timeline payloads but not in TIMELINE_PAYLOAD_FIELDS, so "
            f"the API silently drops them. Add them, or decide not to and say why."
        )

    def test_the_allowlist_names_nothing_no_writer_emits(self) -> None:
        """Drift the other way: an entry for a key that no longer exists is stale."""
        written = payload_keys_written_under(Path(careloop.__file__).parent)

        assert sorted(TIMELINE_PAYLOAD_FIELDS - written) == []

    def test_a_key_nobody_decided_about_is_dropped(self) -> None:
        event = TimelineEvent(
            seq=0,
            at=FIXED_NOW,
            kind=TimelineKind.POLICY_DENY,
            actor="policy",
            summary="DENY for send_email",
            payload={"tool_name": "send_email", "internal_engine_state": "whatever"},
        )

        view = timeline_event_view(event)

        assert view.payload == {"tool_name": "send_email"}

    def test_real_cases_carry_only_decided_keys(self) -> None:
        for case in (finished_case(), poisoned_case()):
            assert payload_keys_in(case) <= TIMELINE_PAYLOAD_FIELDS


# =================================================================================================
# 3 and 4 — nothing vanishes, nothing is invented
# =================================================================================================


class TestNothingVanishesSilently:
    def test_the_shipped_tables_decide_every_stored_field(self) -> None:
        check_every_stored_field_is_decided()

    def test_the_only_thing_withheld_from_the_timeline_is_the_diagnostic(self) -> None:
        assert set(WITHHELD["TimelineEvent"]) == {"diagnostic"}

    def test_every_withholding_states_a_reason(self) -> None:
        for model, fields in WITHHELD.items():
            for field, reason in fields.items():
                assert len(reason) > 40, f"{model}.{field} is withheld without saying why"

    def test_a_new_stored_field_nobody_decided_about_fails(self) -> None:
        """The mutation: adding Case.assigned_to and forgetting the API."""

        class CaseWithANewField(Case):
            assigned_to: str = ""

        with pytest.raises(UndecidedField, match="assigned_to"):
            check_every_stored_field_is_decided(models={**SOURCE_MODELS, "Case": CaseWithANewField})

    def test_the_check_passes_once_the_new_field_is_decided(self) -> None:
        """Vacuity: the failure above is about the decision, not about the subclass."""

        class CaseWithANewField(Case):
            assigned_to: str = ""

        check_every_stored_field_is_decided(
            models={**SOURCE_MODELS, "Case": CaseWithANewField},
            withheld={**WITHHELD, "Case": {"assigned_to": "decided: " + "x" * 40}},
        )

    def test_withholding_a_field_the_model_does_not_have_is_caught(self) -> None:
        """Stale entries rot the same way missing ones do, in the other direction."""
        with pytest.raises(UndecidedField, match="is not a field of Case"):
            check_every_stored_field_is_decided(
                withheld={**WITHHELD, "Case": {"removed_years_ago": "x" * 41}}
            )


class TestNothingIsInvented:
    def test_the_shipped_tables_source_every_response_field(self) -> None:
        check_every_response_field_has_a_source()

    def test_every_source_path_names_a_real_stored_field(self) -> None:
        for view, table in SOURCES.items():
            for field, path in table.items():
                model_name, *segments = path.split(".")

                assert model_name in SOURCE_MODELS, f"{view.__name__}.{field} -> {path}"
                assert segments

    def test_a_response_field_with_no_declared_origin_fails(self) -> None:
        """The mutation: a view field the store has no answer for."""

        class Embellished(CaseCard):
            risk_score: int

        with pytest.raises(UndecidedField, match="risk_score"):
            check_every_response_field_has_a_source(
                sources={Embellished: dict(SOURCES[CaseCard])},
                derived={Embellished: dict(projections.DERIVED[CaseCard])},
            )

    def test_the_same_field_declared_derived_is_accepted(self) -> None:
        """Vacuity: the failure above is about the missing declaration, not about the subclass."""

        class Embellished(CaseCard):
            risk_score: int

        check_every_response_field_has_a_source(
            sources={Embellished: dict(SOURCES[CaseCard])},
            derived={Embellished: {**projections.DERIVED[CaseCard], "risk_score": "computed"}},
        )

    def test_a_source_path_naming_a_field_that_does_not_exist_fails(self) -> None:
        with pytest.raises(UndecidedField, match="no_such_field"):
            check_every_response_field_has_a_source(
                sources={FactView: {**SOURCES[FactView], "key": "Fact.no_such_field"}},
            )

    def test_a_nested_path_resolves_through_the_nested_model(self) -> None:
        """Vacuity for the resolver: it really walks the chain rather than checking the head."""
        with pytest.raises(UndecidedField, match="Provenance has no field 'nope'"):
            check_every_response_field_has_a_source(
                sources={FactView: {**SOURCES[FactView], "key": "Fact.provenance.nope"}},
            )


# =================================================================================================
# the timeline is read in one place
# =================================================================================================


def api_modules() -> list[Path]:
    return sorted((Path(careloop.__file__).parent / "api").rglob("*.py"))


def functions_reading_timeline(path: Path) -> set[str]:
    """Names of the functions in ``path`` that touch ``.timeline``, ``"<module>"`` for top level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(
            isinstance(inner, ast.Attribute) and inner.attr == "timeline"
            for inner in ast.walk(node)
        ):
            found.add(node.name)
    return found


class TestTheApiReadsTheTimelineInOnePlace:
    """Keeps the withholding decision enforced rather than conventional.

    ``latest_delivery`` and ``proposal_for`` live in the graph layer precisely so that a route
    handler wanting something out of the timeline calls a named reader instead of walking the
    entries itself — and a handler that walked them itself would be projecting audit data with no
    allowlist anywhere near it.
    """

    def test_the_sweep_runs_over_the_modules_it_claims_to(self) -> None:
        swept = api_modules()

        assert len(swept) >= 3
        assert {path.name for path in swept} >= {"models.py", "projections.py", "__init__.py"}

    def test_only_projections_reads_the_timeline(self) -> None:
        offenders = sorted(
            path.name
            for path in api_modules()
            if path.name != "projections.py" and functions_reading_timeline(path)
        )

        assert offenders == [], (
            f"{offenders} read case.timeline directly. Anything needing something out of the "
            f"timeline calls a named reader in careloop.graph.context; projections.py is the only "
            f"place the entries themselves are handled, because that is where the allowlist is."
        )

    def test_and_only_to_project_it(self) -> None:
        reading = functions_reading_timeline(
            Path(careloop.__file__).parent / "api" / "projections.py"
        )

        assert reading == {"case_detail", "payload_keys_in"}

    def test_the_detector_finds_a_read_where_one_exists(self, tmp_path: Path) -> None:
        offending = tmp_path / "handler.py"
        offending.write_text(
            "def last_denial(case):\n"
            "    return [e for e in case.timeline if e.kind == 'POLICY_DENY'][-1]\n",
            encoding="utf-8",
        )

        assert functions_reading_timeline(offending) == {"last_denial"}

    def test_the_detector_does_not_fire_on_a_docstring(self, tmp_path: Path) -> None:
        innocent = tmp_path / "handler.py"
        innocent.write_text(
            "def handler(case):\n    '''Never reads case.timeline itself.'''\n    return 1\n",
            encoding="utf-8",
        )

        assert functions_reading_timeline(innocent) == set()


# =================================================================================================
# the projections themselves
# =================================================================================================


class TestTheProjectionsSayWhatTheCaseSays:
    def test_a_fact_keeps_its_trust_level_and_its_citation(self) -> None:
        detail = case_detail(finished_case())

        slot = next(view for view in detail.facts if view.key == "slot_start")

        assert slot.trust_level == "CONTROLLED"
        assert slot.citation.endswith(f"p.{slot.page}")
        assert slot.source_doc in slot.citation

    def test_the_detail_carries_every_fact_action_and_entry_the_case_holds(self) -> None:
        case = finished_case()

        detail = case_detail(case)

        assert len(detail.facts) == len(case.facts)
        assert len(detail.actions) == len(case.actions)
        assert len(detail.timeline) == len(case.timeline)
        assert len(detail.approvals) == len(case.approvals)

    def test_a_gate_verdict_keeps_its_rule_id_for_the_under_the_hood_panel(self) -> None:
        """Withheld from the planner by invariant 6; shown to the human it was written for."""
        case = poisoned_case()

        denials = [
            timeline_event_view(event)
            for event in case.timeline
            if event.kind is TimelineKind.POLICY_DENY
        ]

        assert denials
        assert all(view.payload["matched_rule_id"].startswith("deny-") for view in denials)

    def test_a_parked_case_carries_the_decision_the_caregiver_is_approving(self) -> None:
        decision = decision_view(parked_store().get(CASE_ID))

        assert decision is not None
        assert decision.tool_name == "send_email"
        assert decision.reason_code == "REQUIRES_HUMAN_APPROVAL"
        assert decision.reason_text == (
            "This action needs the caregiver's decision. Stop and await it."
        )
        assert decision.args["to"] == EMAIL["to"]
        assert decision.args["subject"] == EMAIL["subject"]

    def test_the_decision_names_the_arguments_the_approval_actually_covers(self) -> None:
        """Not merely the latest proposal: an approval is about one specific call."""
        case = parked_store().get(CASE_ID)

        decision = decision_view(case)

        assert args_digest(decision.args) == case.pending_approval().args_digest
        assert args_digest(decision.args) == args_digest(EMAIL)

    def test_the_card_shows_the_approved_proposal_even_when_a_later_one_exists(self) -> None:
        """Why the card reads ``proposal_for`` and not ``latest_proposal``.

        The approval records a digest, so the card must go back to the proposal *that digest
        covers*. Reaching for the most recent proposal instead would show a caregiver one set of
        arguments while the button executed another — the exact confusion the digest exists to
        prevent. Unreachable in a real run, because nothing plans while a case is parked; asserted
        anyway, because the two readers are indistinguishable until it is.
        """
        approved = {"to": "carer@example", "subject": "the one being approved", "body": "b"}
        later = {"to": "carer@example", "subject": "proposed afterwards", "body": "b"}

        case = _proposed(make_case(state=CaseState.AWAITING_HUMAN), "send_email", approved)
        case = _proposed(case, "send_email", later)
        case = case.model_copy(
            update={
                "approvals": (
                    Approval(
                        approval_id="apr-1",
                        tool_name="send_email",
                        args_digest=args_digest(approved),
                        reason_code="REQUIRES_HUMAN_APPROVAL",
                        status=ApprovalStatus.PENDING,
                        requested_at=FIXED_NOW,
                    ),
                )
            }
        )

        decision = decision_view(case)

        assert decision.args == approved
        assert decision.args != later

    def test_a_case_with_nothing_pending_has_no_decision(self) -> None:
        assert decision_view(finished_case()) is None

    def test_an_approval_whose_proposal_is_missing_renders_without_arguments(self) -> None:
        """Unreachable in a real run, and visible rather than hidden if it ever happens."""
        case = make_case(state=CaseState.AWAITING_HUMAN).model_copy(
            update={
                "approvals": (
                    Approval(
                        approval_id="apr-orphan",
                        tool_name="send_email",
                        args_digest="digest-nothing-proposed",
                        reason_code="REQUIRES_HUMAN_APPROVAL",
                        status=ApprovalStatus.PENDING,
                        requested_at=FIXED_NOW,
                    ),
                )
            }
        )

        decision = decision_view(case)

        assert decision is not None
        assert decision.args == {}

    def test_an_unrecognised_reason_code_renders_as_itself_rather_than_raising(self) -> None:
        case = make_case(state=CaseState.AWAITING_HUMAN).model_copy(
            update={
                "approvals": (
                    Approval(
                        approval_id="apr-x",
                        tool_name="send_email",
                        args_digest="d",
                        reason_code="A_CODE_FROM_THE_FUTURE",
                        status=ApprovalStatus.PENDING,
                        requested_at=FIXED_NOW,
                    ),
                )
            }
        )

        assert decision_view(case).reason_text == "A_CODE_FROM_THE_FUTURE"

    def test_a_card_counts_what_the_case_holds(self) -> None:
        case = finished_case()

        card = case_card(case)

        assert card.fact_count == len(case.facts)
        assert card.action_count == len(case.actions)
        assert card.section == COMPLETED
        assert card.state == "DONE"

    def test_a_reopened_case_still_reports_the_event_that_opened_it(self) -> None:
        case = append_event(
            make_case(),
            kind=TimelineKind.CASE_REOPENED,
            actor="system",
            summary="reopened",
            payload={"event_id": "evt-0003-claim-rejection", "document_ids": ["doc-0004"]},
            now=FIXED_NOW,
        )

        assert case_detail(case).event_id == "evt-0001"


class TestTheDetailIsACompleteProjection:
    def test_every_case_field_is_reachable_from_the_detail_or_withheld(self) -> None:
        """The dashboard-facing form of "nothing vanishes"."""
        projected = set(SOURCES[CaseDetail].values()) | set(SOURCES[CaseCard].values())
        reached = {path.split(".")[1] for path in projected}
        withheld = set(WITHHELD.get("Case", {}))

        assert set(Case.model_fields) == reached | withheld
