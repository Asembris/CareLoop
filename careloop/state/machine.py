"""The case state machine.

This module is the only writer of ``Case.state``. The LLM emits intents; deterministic code decides
whether they become transitions.

The transition table is data, not control flow, so that "which moves are legal" is auditable by
reading one dict rather than tracing branches. In particular ``DONE`` is reachable only from
``VERIFYING``, which is what makes "the case did not reach DONE via the unsafe path" a structural
property rather than an observation about one test run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from careloop.state.case import Case, CaseState, TimelineEvent, TimelineKind

#: Legal transitions, keyed by source state.
TRANSITIONS: Final[dict[CaseState, frozenset[CaseState]]] = {
    CaseState.NEW: frozenset({CaseState.GATHERING_EVIDENCE, CaseState.FAILED}),
    # **Neither of these may reach WAITING_EXTERNAL, and both used to.** That state now means one
    # exact thing — an external effect was prepared and dispatched, and its outcome could not be
    # established — and every entry into it is therefore post-effect, exactly as every entry into
    # VERIFYING is. A pre-effect route into it would make the name a lie the first time one was
    # taken: a case that had not dispatched anything would be sitting in a state whose whole
    # meaning is that something was dispatched. Neither edge ever had a producer, so removing them
    # changes no behaviour; what it changes is what the state is allowed to mean.
    CaseState.GATHERING_EVIDENCE: frozenset({CaseState.PLANNING, CaseState.FAILED}),
    CaseState.PLANNING: frozenset(
        {
            CaseState.AWAITING_HUMAN,
            CaseState.READY_TO_EXECUTE,
            CaseState.FAILED,
        }
    ),
    # PLANNING is the replan target, and the sources are enumerated rather than assumed: the gate
    # leaves a denied proposal here, and a caregiver's rejection returns the case here. Both are
    # pre-effect — nothing has been carried out — which is what makes another attempt meaningful.
    #
    # A failed verification used to return here too and no longer may; see VERIFYING below.
    # VERIFYING joined this set for the caregiver's re-check, and it is the only entry into
    # VERIFYING that is not an execution path. It is legal here and reachable in production through
    # exactly one writer, careloop.graph.runner.request_recheck, which refuses unless the case's
    # current post-effect handoff is durably bound to an existing ActionRecord of an independently
    # observable action type whose identity the current proposal re-derives. The transition table
    # says the move is legal; a structural sweep in tests/test_recheck_writers.py says nothing the
    # agent drives can make it. Both halves are needed, exactly as they are for the reopen out of
    # DONE — and see VERIFYING below for why this does not weaken the post-effect property.
    CaseState.AWAITING_HUMAN: frozenset(
        {
            CaseState.READY_TO_EXECUTE,
            CaseState.PLANNING,
            CaseState.VERIFYING,
            CaseState.FAILED,
        }
    ),
    CaseState.READY_TO_EXECUTE: frozenset({CaseState.EXECUTING, CaseState.FAILED}),
    # EXECUTING means an attempt is durably prepared and its outcome is not established. Its four
    # exits are the four things that can become true about one:
    #
    #   VERIFYING       the provider's result is known and recorded
    #   WAITING_EXTERNAL nobody answered, and re-attempting the same key is safe
    #   AWAITING_HUMAN  the outcome cannot be established and must not be re-attempted
    #   FAILED          CareLoop is inconsistent about its own record
    #
    # AWAITING_HUMAN was added for the third. It is what a non-replayable action does after an
    # ambiguous outcome — an email that may or may not have been sent is not something an agent may
    # decide to send again — and it is deliberately not WAITING_EXTERNAL, because CareLoop will
    # never resolve it alone and a state implying otherwise would be a lie about who is waiting.
    CaseState.EXECUTING: frozenset(
        {
            CaseState.VERIFYING,
            CaseState.WAITING_EXTERNAL,
            CaseState.AWAITING_HUMAN,
            CaseState.FAILED,
        }
    ),
    # **VERIFYING cannot reach PLANNING, and that is the point rather than an omission.**
    #
    # Every entry into VERIFYING is post-effect, and that is still true now that there are three
    # sources rather than two. The execution path — EXECUTING — enters here after
    # careloop.graph.nodes.ExecuteNode has recorded an action or suppressed a duplicate of one, or
    # after careloop.graph.nodes.ReconcileNode has recorded the result of a prepared attempt it
    # reconciled; VerifyNode fails the case outright when no ActionRecord exists. The one
    # non-execution entry is the guarded human re-check from AWAITING_HUMAN, which
    # careloop.graph.context.recheck_target permits only when the case's current post-effect
    # handoff is bound to an existing ActionRecord; a case with nothing carried out cannot reach
    # it. So there is still no such thing as a verification failure before the effect, and "a
    # proposal that did not verify deserves another attempt" was never a description of a reachable
    # state.
    #
    # WAITING_EXTERNAL was a third source and is no longer one. Not a weakening: a case there holds
    # no ActionRecord by construction, so the edge could only ever have led to the no-recorded-
    # action failure. Reconciliation reaches verification the long way round — re-claim the attempt
    # through EXECUTING, record what the provider returns, and enter here with a result in hand.
    #
    # What it cost was measured rather than argued. With PLANNING legal, a failed verification sent
    # the case back to the planner; an identical reproposal was suppressed and re-verified into the
    # same failure until the SDK's node bound stopped the run, and a reproposal differing in one
    # free-text argument keyed differently, defeated duplicate suppression at both layers, reached
    # ALLOW and dispatched — two real bookings against a clinic that could not confirm the first.
    #
    # So the refusal lives here as well as in the graph, and the redundancy is deliberate. Deleting
    # the verify -> plan edge alone would leave a future node free to write transition(PLANNING) and
    # reopen the hazard with nothing raising; with PLANNING absent here, that line is an
    # IllegalTransition on the first run rather than a defect somebody measures later.
    #
    # AWAITING_HUMAN is the target instead: careloop.graph.nodes._park_with_nothing_certifiable
    # parks the case for a person, carrying no Approval and therefore no authority.
    CaseState.VERIFYING: frozenset({CaseState.DONE, CaseState.AWAITING_HUMAN, CaseState.FAILED}),
    # **WAITING_EXTERNAL could reach PLANNING, and that was the VERIFYING defect pre-installed one
    # state over.** The state is post-effect by definition, so routing it to the planner would do
    # precisely what a failed verification used to do: repropose on top of an action already
    # dispatched, and — since `book_appointment.reason` is planner-authored free text carrying no
    # trust key — repropose it with a different full stop, a different argument digest, a different
    # idempotency key, and duplicate suppression defeated at both layers. It had no producer, so
    # nothing had ever taken it. Giving the state a producer without removing this edge first would
    # have shipped the bug rather than the recovery.
    #
    # VERIFYING is gone for a narrower reason: nothing may enter verification without an
    # ActionRecord, and a case here has none by construction. Reconciliation reaches VERIFYING by
    # first re-claiming the attempt through EXECUTING and recording a result, which is the only
    # order in which that is true.
    #
    # EXECUTING is the re-claim, and it is written by careloop.graph.nodes.ReconcileNode alone —
    # the same arrangement as request_recheck's entry into VERIFYING, enforced structurally by
    # tests/test_reconcile_writers.py. It grants no authority and mints no identity: the tool, the
    # arguments and the key are the ones already frozen on the case's own ACTION_PREPARED entry.
    # It exists because the shipped rules permit an external effect from PLANNING,
    # READY_TO_EXECUTE and EXECUTING and from nowhere else, so a replay issued from here would be
    # refused by the policy engine. The state machine bends to the authority layer; the authority
    # layer does not bend to it.
    CaseState.WAITING_EXTERNAL: frozenset(
        {CaseState.EXECUTING, CaseState.AWAITING_HUMAN, CaseState.FAILED}
    ),
    # DONE has exactly one way out, and only the world can take it. A claim rejected weeks after
    # the appointment was arranged is the same piece of work, not a new one, so
    # careloop.graph.runner.reopen_case moves a finished case back to GATHERING_EVIDENCE to read
    # what has just arrived.
    #
    # **The agent can never leave DONE; the world can.** No graph edge exits DONE — see
    # careloop/graph/builder.py, where dispatch has no outgoing edge for it — and a structural
    # sweep in tests/test_reopen.py fails if any node, agent, tool or policy module so much as
    # names reopen_case. Both halves are needed: the missing edge is what makes it true today,
    # and the sweep is what keeps it true.
    #
    # Invariant 4 is untouched. DONE is still reachable only from VERIFYING, so "the case did not
    # finish via the unsafe path" remains structural.
    CaseState.DONE: frozenset({CaseState.GATHERING_EVIDENCE}),
    CaseState.FAILED: frozenset(),
}

#: States from which no transition is legal at all.
#:
#: FAILED alone, since commit 7. DONE left this set when the reopen edge was added, and the two
#: meanings were separated rather than conflated: see :data:`AT_REST_STATES`.
TERMINAL_STATES: Final[frozenset[CaseState]] = frozenset(
    state for state, allowed in TRANSITIONS.items() if not allowed
)

#: States the graph will not move a case out of.
#:
#: What ``TERMINAL_STATES`` meant before the reopen existed, and what every caller that asks "has
#: this case come to rest" actually wants. A case in one of these has no satisfied outgoing edge,
#: so a run ends with it sitting there; only a new inbound event can change that, and only for
#: ``DONE``.
AT_REST_STATES: Final[frozenset[CaseState]] = TERMINAL_STATES | {CaseState.DONE}

#: The one transition an inbound event may make on a finished case.
REOPEN_FROM: Final[CaseState] = CaseState.DONE
REOPEN_TO: Final[CaseState] = CaseState.GATHERING_EVIDENCE


class IllegalTransition(Exception):
    """Raised when a transition is not permitted by :data:`TRANSITIONS`."""

    def __init__(self, frm: CaseState, to: CaseState) -> None:
        self.frm = frm
        self.to = to
        if frm in TERMINAL_STATES:
            detail = f"{frm} is terminal"
        else:
            allowed = ", ".join(sorted(TRANSITIONS[frm])) or "nothing"
            detail = f"legal targets from {frm} are: {allowed}"
        super().__init__(f"illegal transition {frm} -> {to}: {detail}")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_legal(frm: CaseState, to: CaseState) -> bool:
    """Return whether ``frm -> to`` is a permitted transition."""
    return to in TRANSITIONS[frm]


def append_event(
    case: Case,
    *,
    kind: TimelineKind,
    actor: str,
    summary: str,
    diagnostic: str | None = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Case:
    """Return a copy of ``case`` with one timeline entry appended.

    Used for anything worth auditing that is not itself a state change — evidence recorded, a policy
    verdict, a suppressed duplicate. The version is bumped so the store's compare-and-set still sees
    the write.
    """
    at = now or _utcnow()
    event = TimelineEvent(
        seq=case.next_seq(),
        at=at,
        kind=kind,
        actor=actor,
        summary=summary,
        diagnostic=diagnostic,
        payload=payload or {},
    )
    return case.model_copy(
        update={
            "timeline": (*case.timeline, event),
            "version": case.version + 1,
            "updated_at": at,
        }
    )


def transition(
    case: Case,
    to: CaseState,
    *,
    reason: str,
    actor: str,
    diagnostic: str | None = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Case:
    """Move ``case`` to state ``to``, returning a new instance.

    Every transition appends a ``STATE_TRANSITION`` timeline entry recording where it came from,
    where it went, why, and who caused it. There is no way to change state without leaving that
    trace, because this is the only code path that writes ``Case.state``.

    Raises:
        IllegalTransition: if the move is not in :data:`TRANSITIONS`.
    """
    if not is_legal(case.state, to):
        raise IllegalTransition(case.state, to)

    at = now or _utcnow()
    event = TimelineEvent(
        seq=case.next_seq(),
        at=at,
        kind=TimelineKind.STATE_TRANSITION,
        actor=actor,
        summary=f"{case.state} -> {to}: {reason}",
        diagnostic=diagnostic,
        payload={"from": str(case.state), "to": str(to), "reason": reason, **(payload or {})},
    )

    update: dict[str, Any] = {
        "state": to,
        "timeline": (*case.timeline, event),
        "version": case.version + 1,
        "updated_at": at,
    }
    if to is CaseState.FAILED:
        update["failure_reason"] = reason

    return case.model_copy(update=update)
