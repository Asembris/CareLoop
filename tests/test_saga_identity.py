"""The durable identity of an authorised action, frozen before anything can leave the process.

CareLoop's idempotency key has always been a pure function of ``(case_id, action_type, canonical
args)``, and the arguments have always been durable — the plan node writes them verbatim onto a
``PLAN_PROPOSED`` timeline entry. What was missing was any durable statement that a dispatch of that
exact action **was about to happen**. The key was derived into a local variable after the case had
already been committed as ``EXECUTING`` and lived nowhere else until the effect's result was
recorded, so a process that died in between left a case whose own trail could not say what it had
been doing.

``ACTION_PREPARED`` is that statement, and this file owns what it means:

    the exact previously-authorised logical action identity is durably frozen and eligible for
    dispatch

and, just as importantly, what it does not mean — that anything reached the outside world.

Two properties carry the weight.

:class:`TestTheIdentityIsReconstructibleWithoutTheModel` is why recovery can exist at all: the key a
later process needs is derivable from durable state alone, and equals the one that was stored. Both
halves are asserted, because persisting alone loses drift detection and recomputing alone loses
crash-auditability, and the design requires them to agree.

:class:`TestASettledActionNeverAcquiresAPreparation` is the ordering correction. Duplicate
suppression is resolved *before* a preparation is written, so a logical action the case already
holds a result for cannot leave a stale intent behind for a recovery to find and dispatch.
"""

from __future__ import annotations

from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    EMAIL_ARGS,
    CountingStore,
    booking_key,
    count,
    prepared_payloads,
    ready_to_execute,
)

from careloop.graph.context import Preparation, latest_proposal, unsettled_preparation
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.state.case import ActionRecord
from careloop.state.machine import append_event
from careloop.tools.idempotency import args_digest, idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import AppointmentBooked


def _prepare(case, tool_name: str, args: dict[str, str], *, key: str | None = None):
    """Append one ``ACTION_PREPARED`` entry, as ``ExecuteNode`` writes it."""
    return append_event(
        case,
        kind=TimelineKind.ACTION_PREPARED,
        actor="system",
        summary=f"{tool_name} prepared for dispatch",
        payload={
            "tool_name": tool_name,
            "idempotency_key": key or idempotency_key(CASE_ID, tool_name, args),
            "args_digest": args_digest(args),
        },
        now=FIXED_NOW,
    )


def _record(case, tool_name: str, key: str, args: dict[str, str], *, result: str = "{}"):
    """Attach an ``ActionRecord`` under ``key``, as ``_record_action`` writes it."""
    record = ActionRecord(
        idempotency_key=key,
        action_type=tool_name,
        args_digest=args_digest(args),
        result=result,
        recorded_at=FIXED_NOW,
    )
    return case.model_copy(update={"actions": (*case.actions, record), "version": case.version + 1})


def _already_performed(store):
    """A case in ``READY_TO_EXECUTE`` that already holds the booking's result. The duplicate."""
    case = ready_to_execute(store)
    base = case.version
    case = _record(
        case,
        "book_appointment",
        booking_key(),
        BOOKING_ARGS,
        result=AppointmentBooked(
            confirmation_id=f"apt-{booking_key()[:16]}", **BOOKING_ARGS
        ).model_dump_json(),
    )
    return store.save(case, expected_version=base)


class TestTheIdentityIsReconstructibleWithoutTheModel:
    """A later process can derive the same key from durable state, and it matches the stored one."""

    def test_the_key_derives_from_the_durable_proposal_alone(self) -> None:
        """No planner, no triage, no run context — the case and the registry are enough."""
        store = InMemoryCaseStore()
        case = ready_to_execute(store)

        proposal = latest_proposal(case)
        assert proposal is not None
        derived = idempotency_key(case.case_id, proposal.tool_name, proposal.args)

        assert derived == booking_key()

    def test_the_stored_key_and_the_rederived_key_agree(self) -> None:
        """Persist and recompute, then require agreement. Neither alone is the design."""
        store = InMemoryCaseStore()
        case = ready_to_execute(store)
        case = _prepare(case, "book_appointment", BOOKING_ARGS)

        prepared = unsettled_preparation(case)
        proposal = latest_proposal(case)
        assert prepared is not None and proposal is not None

        assert prepared.idempotency_key == idempotency_key(
            case.case_id, proposal.tool_name, proposal.args
        )
        assert prepared.args_digest == args_digest(proposal.args)
        assert prepared.tool_name == proposal.tool_name

    def test_the_identity_survives_a_serialisation_round_trip(self) -> None:
        """A recovery reads the case out of a store, so the entry has to survive JSON."""
        store = InMemoryCaseStore()
        case = ready_to_execute(store)
        base = case.version
        case = _prepare(case, "book_appointment", BOOKING_ARGS)
        store.save(case, expected_version=base)

        reloaded = store.get(CASE_ID)
        prepared = unsettled_preparation(reloaded)

        assert prepared == Preparation(
            tool_name="book_appointment",
            idempotency_key=booking_key(),
            args_digest=args_digest(BOOKING_ARGS),
        )

    def test_free_text_drift_would_change_the_identity_which_is_why_it_is_frozen(self) -> None:
        """The measured production hazard, and the reason recovery reads the stored proposal.

        ``reason`` is planner-authored free text and has been observed differing by one full stop
        between reproposals. That moves the digest, moves the key, and defeats duplicate suppression
        at both layers. Recovery never asks for a new proposal, so the drift cannot occur on that
        path — but the sensitivity is real and is asserted here rather than assumed.
        """
        drifted = {**BOOKING_ARGS, "reason": BOOKING_ARGS["reason"] + "."}

        assert idempotency_key(CASE_ID, "book_appointment", drifted) != booking_key()

    def test_every_effect_tool_keys_from_its_own_durable_proposal(self) -> None:
        """Not a spelling of one tool: the property is registry-wide."""
        for tool_name in sorted(REGISTRY.external_effect_tools):
            spec = REGISTRY.get(tool_name)
            assert spec is not None
            args = {name: f"value-for-{name}" for name in spec.arg_names}
            store = InMemoryCaseStore()
            case = ready_to_execute(
                store, tool_name=tool_name, args=args, case_id=f"case-{tool_name}"
            )
            proposal = latest_proposal(case)
            assert proposal is not None
            assert idempotency_key(case.case_id, proposal.tool_name, proposal.args) == (
                idempotency_key(f"case-{tool_name}", tool_name, args)
            )


class TestUnsettledIsComputedRatherThanStored:
    """A preparation is settled exactly when the case holds a result for its key."""

    def test_a_preparation_with_no_action_record_is_unsettled(self) -> None:
        store = InMemoryCaseStore()
        case = _prepare(ready_to_execute(store), "book_appointment", BOOKING_ARGS)

        assert unsettled_preparation(case) is not None

    def test_a_preparation_with_an_action_record_is_settled(self) -> None:
        store = InMemoryCaseStore()
        case = _prepare(ready_to_execute(store), "book_appointment", BOOKING_ARGS)
        case = _record(case, "book_appointment", booking_key(), BOOKING_ARGS)

        assert unsettled_preparation(case) is None

    def test_a_record_under_a_different_key_does_not_settle_it(self) -> None:
        """Settlement is by exact identity. Another action's result is not this action's."""
        store = InMemoryCaseStore()
        case = _prepare(ready_to_execute(store), "book_appointment", BOOKING_ARGS)
        case = _record(
            case, "send_email", idempotency_key(CASE_ID, "send_email", EMAIL_ARGS), EMAIL_ARGS
        )

        assert unsettled_preparation(case) is not None

    def test_a_case_with_no_preparations_at_all_reports_none(self) -> None:
        """Every case persisted before this patch. ``None`` is true, not a gap."""
        store = InMemoryCaseStore()

        assert unsettled_preparation(ready_to_execute(store)) is None

    def test_a_settled_newest_preparation_stops_the_scan(self) -> None:
        """Fail closed rather than resurrect an older episode.

        Answering ``None`` here is what makes ``ReconcileNode`` refuse an inconsistent case. Walking
        further back would hand a recovery an action that had already been left behind and dispatch
        it, which is strictly worse than stopping.
        """
        store = InMemoryCaseStore()
        stale = idempotency_key(CASE_ID, "send_email", EMAIL_ARGS)
        case = _prepare(ready_to_execute(store), "send_email", EMAIL_ARGS, key=stale)
        case = _prepare(case, "book_appointment", BOOKING_ARGS)
        case = _record(case, "book_appointment", booking_key(), BOOKING_ARGS)

        assert unsettled_preparation(case) is None


class TestASettledActionNeverAcquiresAPreparation:
    """Duplicate suppression is resolved before a preparation is written.

    The ordering correction, and it is not cosmetic. ``ACTION_PREPARED`` means *about to be
    attempted*; writing one for an effect the case already holds a result for would leave an
    unsettled-looking intent on an append-only trail — except that it would not even look unsettled,
    because the record is already there. What it would actually do is put a second preparation of a
    completed action on the timeline, so any future reader that walked backwards for the newest
    preparation would find one for work that was finished.
    """

    def test_a_suppressed_duplicate_writes_no_preparation(self) -> None:
        store = InMemoryCaseStore()
        _already_performed(store)

        result, _ = _dispatch(store)

        settled = store.get(CASE_ID)
        assert result.case.state is CaseState.DONE
        assert count(settled, TimelineKind.ACTION_PREPARED) == 0
        assert count(settled, TimelineKind.DUPLICATE_SUPPRESSED) == 1
        assert unsettled_preparation(settled) is None

    def test_a_suppressed_duplicate_performs_no_effect(self) -> None:
        store = InMemoryCaseStore()
        _already_performed(store)

        _, executor = _dispatch(store)

        assert executor.invocation_count == 0

    def test_a_suppressed_duplicate_still_passes_through_executing(self) -> None:
        """The state machine has no ``READY_TO_EXECUTE -> VERIFYING`` edge and must not grow one."""
        store = InMemoryCaseStore()
        _already_performed(store)

        _dispatch(store)

        moves = [
            (event.payload["from"], event.payload["to"])
            for event in store.get(CASE_ID).timeline
            if event.kind is TimelineKind.STATE_TRANSITION
        ]
        assert moves[-3:] == [
            (str(CaseState.READY_TO_EXECUTE), str(CaseState.EXECUTING)),
            (str(CaseState.EXECUTING), str(CaseState.VERIFYING)),
            (str(CaseState.VERIFYING), str(CaseState.DONE)),
        ]

    def test_a_fresh_action_does_acquire_exactly_one_preparation(self) -> None:
        """The control. Without it the two tests above would pass with the feature deleted."""
        store = InMemoryCaseStore()
        ready_to_execute(store)

        _dispatch(store)

        settled = store.get(CASE_ID)
        assert count(settled, TimelineKind.ACTION_PREPARED) == 1
        assert prepared_payloads(settled) == [
            {
                "tool_name": "book_appointment",
                "idempotency_key": booking_key(),
                "args_digest": args_digest(BOOKING_ARGS),
            }
        ]

    def test_the_preparation_carries_no_argument_values(self) -> None:
        """A digest and a key, never a patient id or a reason. The payload is a public surface."""
        store = InMemoryCaseStore()
        ready_to_execute(store)

        _dispatch(store)

        rendered = repr(prepared_payloads(store.get(CASE_ID)))
        for value in BOOKING_ARGS.values():
            assert value not in rendered


class TestThePreparationAndTheTransitionShareOneCommit:
    """Two writes, one ``save``. There is no instant at which they can disagree."""

    def test_the_first_save_carrying_a_preparation_is_already_executing(self) -> None:
        """The sharp assertion, and the one that kills a split commit.

        Counting saves is not enough: a mutant that commits the preparation on its own and *then*
        transitions still produces exactly one persisted ``EXECUTING`` case carrying a preparation.
        What it also produces, and a correct implementation cannot, is a durable
        ``READY_TO_EXECUTE`` case that already claims an action is prepared — a state whose two
        halves disagree, and one a crash could leave behind.
        """
        store = CountingStore()
        ready_to_execute(store)
        before = len(store.saves)

        _dispatch(store)

        with_a_preparation = [
            saved
            for saved in store.saves[before:]
            if count(saved, TimelineKind.ACTION_PREPARED) > 0
        ]
        assert with_a_preparation, "no save carried a preparation at all"
        assert with_a_preparation[0].state is CaseState.EXECUTING

    def test_no_persisted_case_is_prepared_without_being_executing(self) -> None:
        """The same property stated over every commit rather than over the first."""
        store = CountingStore()
        ready_to_execute(store)

        _dispatch(store)

        for saved in store.saves:
            if unsettled_preparation(saved) is not None:
                assert saved.state is CaseState.EXECUTING

    def test_no_persisted_state_is_executing_without_a_preparation(self) -> None:
        """The converse. The shape ``ReconcileNode`` fails closed on is unreachable here."""
        store = CountingStore()
        ready_to_execute(store)

        _dispatch(store)

        for saved in store.saves:
            if saved.state is CaseState.EXECUTING:
                assert unsettled_preparation(saved) is not None

    def test_a_suppressed_duplicate_also_costs_exactly_one_save(self) -> None:
        """Both transitions atomic, so suppression cannot be interrupted mid-way either.

        Counted as *saves that landed in* ``VERIFYING`` rather than as total saves, because the run
        goes on to verify and finish and those commits belong to another node.
        """
        store = CountingStore()
        _already_performed(store)
        before = len(store.saves)

        _dispatch(store)

        suppressing = [
            saved for saved in store.saves[before:] if saved.state is CaseState.VERIFYING
        ]
        assert len(suppressing) == 1
        assert count(suppressing[0], TimelineKind.DUPLICATE_SUPPRESSED) == 1
        assert count(suppressing[0], TimelineKind.ACTION_PREPARED) == 0


# -- the dispatch harness ----------------------------------------------------------------------


def _dispatch(store):
    """Run one graph pass over a case already in ``READY_TO_EXECUTE``, fully simulated.

    Returns ``(RunResult, executor)``. The planner and triage are the exploding stubs: this pass
    routes ``dispatch -> execute -> verify`` and must never reach either.
    """
    import asyncio

    from saga_helpers import SAGA_EVENT, ExplodingPlanner, ExplodingTriage

    from careloop.graph.runner import context_for, run_case
    from careloop.tools import MockExternalExecutor

    executor = MockExternalExecutor()
    context = context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        executor=executor,
        now=lambda: FIXED_NOW,
    )
    return asyncio.run(run_case(context, max_node_executions=12)), executor
