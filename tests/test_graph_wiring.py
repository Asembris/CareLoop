"""The node contract, the edge conditions, and the bounds.

Structure only — driving a case end to end is ``tests/test_case_run.py``. What is checked here is
the wiring the SDK will not check for us: that a node returns something the graph's input builder
accepts, that every edge branches on the persisted case rather than on node output, and that the
three bounds are actually set on the built graph.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import FIXED_NOW, make_case
from strands.agent import AgentResult
from strands.multiagent.base import MultiAgentResult, Status

from careloop.agents.schemas import TriageCategory, TriageResult, Urgency
from careloop.graph.builder import (
    EXECUTION_TIMEOUT,
    MAX_NODE_EXECUTIONS,
    NODE_IDS,
    NODE_TIMEOUT,
    build_graph,
)
from careloop.graph.conditions import when_case_is, when_case_is_not
from careloop.graph.context import (
    RunContext,
    latest_delivery,
    latest_proposal,
    prior_denials,
    proposal_for,
)
from careloop.graph.nodes import DeterministicNode, DispatchNode, TriageNode
from careloop.graph.trusted import trusted_facts
from careloop.llm.mock import mock_model
from careloop.policy.verdicts import ReasonCode
from careloop.state import Case, CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.machine import append_event
from careloop.tools.idempotency import args_digest

REFERRAL = "doc-0001-referral"


def opened_case(state: CaseState = CaseState.NEW) -> Case:
    return Case(
        case_id="case-0001",
        event_id="evt-0001",
        state=state,
        facts=trusted_facts(now=FIXED_NOW),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def context_for(case: Case, **kwargs) -> RunContext:
    store = InMemoryCaseStore()
    store.create(case)
    return RunContext(
        case_id=case.case_id,
        store=store,
        event_text="Referral received for Enid Marlowe.",
        document_ids=(REFERRAL,),
        now=lambda: FIXED_NOW,
        **kwargs,
    )


class TestTheNodeContract:
    def test_a_node_returns_a_multi_agent_result_the_graph_accepts(self) -> None:
        node = DispatchNode("dispatch", context_for(opened_case()))

        result = asyncio.run(node.invoke_async("ignored"))

        assert isinstance(result, MultiAgentResult)
        assert result.status is Status.COMPLETED

    def test_the_inner_result_is_an_agent_result_and_not_a_bare_string(self) -> None:
        """Spike finding 5: a bare ``str`` fails deep inside the graph, not at the call site."""
        node = DispatchNode("dispatch", context_for(opened_case()))

        result = asyncio.run(node.invoke_async("ignored"))

        assert isinstance(result.results["dispatch"].result, AgentResult)

    def test_the_wrapper_has_no_branches(self) -> None:
        """If the wrapper accumulates logic, "deterministic nodes" stops being a claim about it.

        The wrapper's whole job is to run the body and wrap the answer. Every decision belongs in
        a ``run``, where it is ordinary readable Python rather than something hidden inside an
        SDK-shaped adapter.
        """
        import inspect
        import textwrap

        body = textwrap.dedent(inspect.getsource(DeterministicNode.invoke_async))
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith(("#", '"""', "'''"))
        )
        statements = code.split('"""')[-1]

        for branch in ("if ", "elif ", "for ", "while ", "try:", "match "):
            assert branch not in statements, f"the node wrapper branches on {branch!r}"

    def test_the_base_node_refuses_to_run_without_a_body(self) -> None:
        node = DeterministicNode("bare", context_for(opened_case()))

        with pytest.raises(NotImplementedError):
            asyncio.run(node.run())

    def test_a_node_ignores_the_task_it_is_handed(self) -> None:
        """Business facts come from the store. A node steered by prose is a node an upstream
        model can steer."""
        context = context_for(opened_case())
        node = DispatchNode("dispatch", context)

        one = asyncio.run(node.invoke_async("do something else entirely"))
        two = asyncio.run(node.invoke_async(""))

        assert str(one.results["dispatch"].result) == str(two.results["dispatch"].result)


class TestNodesWriteThroughTheStore:
    def test_triage_commits_its_transition(self) -> None:
        context = context_for(
            opened_case(),
            triage=_triage_returning("Referral for cardiology follow-up."),
        )
        node = TriageNode("triage", context)

        asyncio.run(node.run())

        stored = context.current_case()
        assert stored.state is CaseState.GATHERING_EVIDENCE
        assert stored.summary == "Referral for cardiology follow-up."

    def test_triage_records_no_facts(self) -> None:
        """Model output never becomes a policy input; the node docstring has the two reasons."""
        case = opened_case()
        context = context_for(case, triage=_triage_returning("Referral."))

        asyncio.run(TriageNode("triage", context).run())

        assert context.current_case().facts == case.facts

    def test_dispatch_writes_nothing_at_all(self) -> None:
        context = context_for(opened_case())
        before = context.current_case()

        asyncio.run(DispatchNode("dispatch", context).run())

        assert context.current_case() == before


class TestEdgeConditionsReadThePersistedCase:
    def test_a_condition_holds_for_the_state_the_store_carries(self) -> None:
        context = context_for(opened_case(CaseState.PLANNING))

        assert when_case_is(context, CaseState.PLANNING)(None)
        assert not when_case_is(context, CaseState.DONE)(None)

    def test_it_follows_the_case_rather_than_being_fixed_at_build_time(self) -> None:
        context = context_for(opened_case(CaseState.NEW))
        condition = when_case_is(context, CaseState.GATHERING_EVIDENCE)
        assert not condition(None)

        moved = context.current_case().model_copy(
            update={"state": CaseState.GATHERING_EVIDENCE, "version": 1}
        )
        context.store.save(moved, expected_version=0)

        assert condition(None)

    def test_the_graph_state_argument_is_ignored(self) -> None:
        """Routing on graph execution state would be routing on what nodes returned."""
        context = context_for(opened_case(CaseState.PLANNING))
        condition = when_case_is(context, CaseState.PLANNING)

        assert condition(None) == condition("anything at all")

    def test_the_complement_condition_excludes_what_it_names(self) -> None:
        context = context_for(opened_case(CaseState.DONE))

        assert not when_case_is_not(context, CaseState.DONE)(None)
        assert when_case_is_not(context, CaseState.FAILED)(None)


class TestTheGraphIsBoundedBySdkPrimitives:
    def test_all_three_bounds_are_set(self) -> None:
        graph = build_graph(context_for(opened_case()))

        assert graph.max_node_executions == MAX_NODE_EXECUTIONS
        assert graph.execution_timeout == EXECUTION_TIMEOUT
        assert graph.node_timeout == NODE_TIMEOUT

    def test_the_bounds_are_overridable_for_a_test_that_needs_a_tight_one(self) -> None:
        graph = build_graph(
            context_for(opened_case()),
            max_node_executions=4,
            execution_timeout=1.0,
            node_timeout=0.5,
        )

        assert (graph.max_node_executions, graph.execution_timeout, graph.node_timeout) == (
            4,
            1.0,
            0.5,
        )

    def test_no_module_under_careloop_hand_rolls_an_execution_counter(self) -> None:
        """Invariant 5. The cycle is a real cycle and the SDK is what stops it."""
        from pathlib import Path

        package = Path(__file__).resolve().parent.parent / "careloop"
        for module in package.rglob("*.py"):
            source = module.read_text(encoding="utf-8")
            assert "max_iterations" not in source
            assert "loop_count" not in source


class TestTheGraphShape:
    def test_every_node_is_present(self) -> None:
        graph = build_graph(context_for(opened_case()))

        assert set(graph.nodes) == set(NODE_IDS)

    def test_dispatch_is_the_only_entry_point(self) -> None:
        graph = build_graph(context_for(opened_case()))

        assert [node.node_id for node in graph.entry_points] == ["dispatch"]

    def test_the_replan_cycle_exists_in_both_directions(self) -> None:
        graph = build_graph(context_for(opened_case()))
        edges = {(edge.from_node.node_id, edge.to_node.node_id) for edge in graph.edges}

        assert ("plan", "gate") in edges
        assert ("gate", "plan") in edges

    def test_verification_failure_cannot_route_back_to_planning(self) -> None:
        """**Half of the post-effect safety property, and the structural half.**

        There was a ``verify → plan`` edge here and this test asserted it. Every entry into
        ``VERIFYING`` is post-effect, so that edge meant replanning on top of an action already
        carried out: an identical reproposal was suppressed and re-verified into the same failure
        until the node bound stopped the run, and a reproposal differing in one free-text argument
        keyed differently, defeated duplicate suppression and executed a second real external
        effect.

        The other half is :data:`careloop.state.machine.TRANSITIONS` refusing ``VERIFYING →
        PLANNING`` — see ``tests/test_state_machine.py``. Both are needed and neither is decoration:
        without the edge a node could still write the state, and without the transition rule a
        future edge could still route on it.
        """
        graph = build_graph(context_for(opened_case()))
        edges = {(edge.from_node.node_id, edge.to_node.node_id) for edge in graph.edges}

        assert ("verify", "plan") not in edges

    def test_nothing_leaves_verify_at_all(self) -> None:
        """Both of verify's outcomes end the run by having no satisfied outgoing edge.

        ``DONE`` always did; ``AWAITING_HUMAN`` now does too, exactly as it already did at the gate.
        Asserted as the empty set rather than as "not plan", so a future edge out of verify has to
        be a deliberate change to this line.
        """
        graph = build_graph(context_for(opened_case()))
        out_of_verify = {
            edge.to_node.node_id for edge in graph.edges if edge.from_node.node_id == "verify"
        }

        assert out_of_verify == set()

    def test_the_gates_replan_is_the_only_cycle_left(self) -> None:
        """Vacuity for the two above: replanning was narrowed, not removed.

        A denied proposal still returns to the planner, because nothing has been carried out and
        another attempt is the point of the gate. Deleting that too would have been a much larger
        change wearing this one's clothes.
        """
        graph = build_graph(context_for(opened_case()))
        edges = {(edge.from_node.node_id, edge.to_node.node_id) for edge in graph.edges}

        assert ("gate", "plan") in edges
        assert {frm for frm, to in edges if to == "plan"} == {"dispatch", "evidence", "gate"}

    def test_the_gate_can_reach_execution_and_the_planner_and_nothing_else(self) -> None:
        graph = build_graph(context_for(opened_case()))
        out_of_gate = {
            edge.to_node.node_id for edge in graph.edges if edge.from_node.node_id == "gate"
        }

        assert out_of_gate == {"execute", "plan"}

    def test_dispatch_can_resume_into_every_non_terminal_stage(self) -> None:
        graph = build_graph(context_for(opened_case()))
        out_of_dispatch = {
            edge.to_node.node_id for edge in graph.edges if edge.from_node.node_id == "dispatch"
        }

        assert out_of_dispatch == {"triage", "evidence", "plan", "execute", "verify"}

    def test_every_proposal_is_gated_unconditionally(self) -> None:
        """The plan → gate edge carries no condition: the planner does not choose to be checked."""
        graph = build_graph(context_for(opened_case()))
        plan_to_gate = next(
            edge
            for edge in graph.edges
            if (edge.from_node.node_id, edge.to_node.node_id) == ("plan", "gate")
        )

        assert plan_to_gate.condition is None


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


class TestReadingTheCaseBack:
    def test_a_proposal_round_trips_through_the_timeline(self) -> None:
        case = append_event(
            make_case(),
            kind=TimelineKind.PLAN_PROPOSED,
            actor="planner",
            summary="proposed book_appointment",
            payload={
                "tool_name": "book_appointment",
                "args": {"provider_id": "P", "patient_id": "PT", "slot_start": "S", "reason": "r"},
                "rationale": "because",
            },
            now=FIXED_NOW,
        )

        proposal = latest_proposal(case)

        assert proposal.tool_name == "book_appointment"
        assert proposal.args["provider_id"] == "P"

    def test_the_most_recent_proposal_wins(self) -> None:
        case = make_case()
        for tool in ("send_email", "book_appointment"):
            case = append_event(
                case,
                kind=TimelineKind.PLAN_PROPOSED,
                actor="planner",
                summary=f"proposed {tool}",
                payload={"tool_name": tool, "args": {}, "rationale": ""},
                now=FIXED_NOW,
            )

        assert latest_proposal(case).tool_name == "book_appointment"

    def test_a_case_with_no_proposal_has_none(self) -> None:
        assert latest_proposal(make_case()) is None

    def test_a_proposal_is_findable_by_the_digest_an_approval_records(self) -> None:
        """What lets a decision card show the caregiver the call they are approving."""
        args = {"to": "carer@example", "subject": "s", "body": "b"}
        case = _proposed(make_case(), "send_email", args)

        found = proposal_for(case, "send_email", args_digest(args))

        assert found is not None
        assert found.args == args

    def test_a_digest_no_proposal_covers_finds_nothing(self) -> None:
        case = _proposed(make_case(), "send_email", {"to": "carer@example"})

        assert (
            proposal_for(case, "send_email", args_digest({"to": "attacker@evil.example"})) is None
        )

    def test_the_digest_picks_the_matching_proposal_not_the_latest(self) -> None:
        """An approval covers specific arguments; the newest proposal need not be them."""
        approved = {"to": "carer@example", "subject": "s", "body": "b"}
        case = _proposed(make_case(), "send_email", approved)
        case = _proposed(case, "send_email", {"to": "carer@example", "subject": "s2", "body": "b"})

        found = proposal_for(case, "send_email", args_digest(approved))

        assert found is not None
        assert found.args == approved
        assert latest_proposal(case).args != approved

    def test_the_latest_delivery_is_the_opening_event_before_any_reopen(self) -> None:
        case = append_event(
            make_case(),
            kind=TimelineKind.CASE_CREATED,
            actor="system",
            summary="case opened",
            payload={"event_id": "evt-0001-referral", "document_ids": ["doc-0001"]},
            now=FIXED_NOW,
        )

        delivery = latest_delivery(case)

        assert delivery.event_id == "evt-0001-referral"
        assert delivery.document_ids == ("doc-0001",)

    def test_a_reopen_becomes_the_latest_delivery_and_case_event_id_does_not_move(self) -> None:
        """The distinction the reader exists for.

        ``Case.event_id`` records which event *opened* the case and ``reopen_case`` deliberately
        does not rewrite it. Anything carrying a reopened case forward needs the later event's
        documents, so reading ``case.event_id`` would hand it the referral's.
        """
        case = append_event(
            make_case(),
            kind=TimelineKind.CASE_CREATED,
            actor="system",
            summary="case opened",
            payload={"event_id": "evt-0001-referral", "document_ids": ["doc-0001"]},
            now=FIXED_NOW,
        )
        case = append_event(
            case,
            kind=TimelineKind.CASE_REOPENED,
            actor="system",
            summary="reopened",
            payload={
                "event_id": "evt-0003-claim-rejection",
                "document_ids": ["doc-0004", "doc-0005"],
                "opened_by": "evt-0001-referral",
            },
            now=FIXED_NOW,
        )

        delivery = latest_delivery(case)

        assert delivery.event_id == "evt-0003-claim-rejection"
        assert delivery.document_ids == ("doc-0004", "doc-0005")
        assert case.event_id == "evt-0001"

    def test_a_case_with_no_delivery_entry_has_none(self) -> None:
        assert latest_delivery(make_case()) is None

    def test_denials_carry_the_tool_and_the_reason_code_and_nothing_else(self) -> None:
        case = append_event(
            make_case(),
            kind=TimelineKind.POLICY_DENY,
            actor="policy",
            summary="DENY for send_email",
            diagnostic="deny-email-to-untrusted-recipient matched on to=attacker@evil.example",
            payload={
                "tool_name": "send_email",
                "reason_code": "DESTINATION_NOT_TRUSTED",
                "matched_rule_id": "deny-email-to-untrusted-recipient",
            },
            now=FIXED_NOW,
        )

        assert prior_denials(case) == (("send_email", ReasonCode.DESTINATION_NOT_TRUSTED),)

    def test_repeated_denials_collapse(self) -> None:
        case = make_case()
        for _ in range(4):
            case = append_event(
                case,
                kind=TimelineKind.POLICY_DENY,
                actor="policy",
                summary="DENY",
                payload={"tool_name": "send_email", "reason_code": "DESTINATION_NOT_TRUSTED"},
                now=FIXED_NOW,
            )

        assert len(prior_denials(case)) == 1

    def test_an_allow_is_not_a_denial(self) -> None:
        case = append_event(
            make_case(),
            kind=TimelineKind.POLICY_ALLOW,
            actor="policy",
            summary="ALLOW",
            payload={"tool_name": "book_appointment", "verdict": "ALLOW", "reason_code": ""},
            now=FIXED_NOW,
        )

        assert prior_denials(case) == ()


def _triage_returning(summary: str):
    from careloop.agents.triage import TriageAgent

    return TriageAgent(
        mock_model(
            structured=[
                TriageResult(
                    category=TriageCategory.APPOINTMENT,
                    urgency=Urgency.SOON,
                    summary=summary,
                    patient_id="PT-4417",
                )
            ]
        )
    )
