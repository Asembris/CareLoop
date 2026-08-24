"""A provider the saga has never heard of, driven through the whole recovery lifecycle.

**The architectural claim, and the only honest way to earn it.** CareLoop's saga is supposed to be
provider-neutral: adapters own protocol, status translation, duplicate semantics and result
reconstruction, and the recovery engine consumes an action identity, a provider result, two
transport facts, and the answers to *may this be re-issued* and *is an independent observer bound*.
``tests/test_provider_seams.py`` proves the *structural* half — no provider token reaches the engine
— and structure alone is not proof. A module can be free of provider names and still have been
shaped around the two providers that happen to exist.

So this drives a **third** one. It is synthetic and deliberately stays synthetic: nothing here is a
new external effect, a new tool, a new registry entry or a new AWS resource. It is a stand-in whose
only job is to have a duplicate contract that **neither shipped provider has**, and to be carried by
the shipped saga anyway.

::

    clinic          duplicate write -> 200, the original record comes back in the response
    object store    duplicate write -> 412, refused; the adapter fetches what is there
    this            duplicate write -> accepted and ignored; the write NEVER returns a record,
                    so the adapter fetches on every write, first attempt and replay alike

That third shape is not a contrivance for its own sake. A submit-then-fetch API — the write answers
with a receipt and the record is a separate resource — is an ordinary way for a service to be built,
and it is the shape that would break an engine which had quietly assumed a write returns its own
result.

**Nothing production-provider-specific is imported.** No ``ClinicClient``, no ``ObjectStoreClient``,
no ``demoworld``, no ``objectstore``, no ``boto3`` and no HTTP. What is imported is the seam:
:class:`~careloop.graph.providers.ExternalProvider`, the two neutral failure types, and the
observation vocabulary. If integrating a provider needed anything else, that would show up here as
an import.

**What it does not claim.** It does not measure a real contract, so it earns no ``RecoveryClass``
entry for anything — the shipped declarations are unchanged and this uses an existing action type.
And it does not re-prove the properties that already have non-vacuous homes: that a
``NOT_REPLAYABLE`` type parks is
``tests/test_reconcile_liveness.py::TestANonReplayableEffectCannotBeWokenAtAll``, and that a replay
with no observer parks is ``tests/test_saga_recovery.py``'s
``TestAReplayWithNoObserverIsNotAllowedEither``. The declaration-level statement that an unsafe
provider needs no node edit is asserted at the end, against those.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping

import pytest
from helpers import FIXED_NOW
from saga_helpers import (
    BOOKING_ARGS,
    CASE_ID,
    SAGA_EVENT,
    ExplodingPlanner,
    ExplodingTriage,
    booking_key,
    executing_with_a_prepared_action,
)

from careloop.graph.providers import ExternalProvider
from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass, may_replay
from careloop.graph.runner import context_for, run_case
from careloop.graph.verify import (
    ObservationAbsent,
    ObservationOutcome,
    ObservationUnavailable,
    rehydrate,
)
from careloop.state import CaseState, InMemoryCaseStore, TimelineKind
from careloop.tools.provider import ProviderRefused, ProviderUnavailable
from careloop.tools.results import ActionResult, AppointmentBooked

#: The action type the synthetic provider claims. An existing one, because inventing a tool would
#: mean a registry entry, a verifier, a result type and a recovery class — a new external effect,
#: which this session is not adding. What is new is the *provider*, which is the thing under test.
ACTION = "book_appointment"


class LedgerDown(ProviderUnavailable):
    """Nobody answered. The synthetic provider's spelling of the one fact that matters."""


class LedgerDeclined(ProviderRefused):
    """It answered and declined."""


class NoSuchLedgerRecord(LedgerDeclined):
    """It answered that it holds no record under that receipt.

    Narrower than a refusal for :class:`~careloop.tools.clinic.AppointmentNotFound`'s reason: a
    definite answer about a resource is evidence, where a failure to obtain an answer is not.
    """


class LedgerService:
    """A synthetic external system: submit returns a receipt, the record is fetched separately.

    **Create-once by submission key, and the write hands back nothing about the record.** That is
    the contract, and it is the third shape — the clinic replays its record and the object store
    refuses a duplicate outright, and this does neither. A second submission under a key it already
    holds is accepted, changes nothing, and returns the same receipt, so the adapter must go and
    look either way.

    Counters, so a test can say how many times the world was written to and read from. They are
    test instrumentation and nothing in CareLoop knows they exist.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, str]] = {}
        self._receipts: dict[str, str] = {}
        self.submissions = 0
        self.reads = 0
        self.reachable = True

    def submit(self, submission_key: str, fields: Mapping[str, str]) -> str:
        """Accept a submission and answer with a receipt. Never with the record."""
        self.submissions += 1
        if not self.reachable:
            raise LedgerDown("the ledger did not answer the submission")
        if submission_key not in self._receipts:
            receipt = f"LR-{submission_key[:12]}"
            self._records[receipt] = dict(fields)
            self._receipts[submission_key] = receipt
        return self._receipts[submission_key]

    def record(self, receipt: str) -> dict[str, str]:
        """What the ledger holds under ``receipt``, now."""
        self.reads += 1
        if not self.reachable:
            raise LedgerDown("the ledger did not answer the read")
        if receipt not in self._records:
            raise NoSuchLedgerRecord(f"the ledger holds no record under {receipt}")
        return dict(self._records[receipt])

    def rewrite(self, receipt: str, **fields: str) -> None:
        """Change what is held, behind CareLoop's back. The independence lever."""
        self._records[receipt].update(fields)

    def __len__(self) -> int:
        return len(self._records)


def ledger_provider(service: LedgerService) -> ExternalProvider:
    """The synthetic service as one provider: a real write and the read that observes it.

    **The adapter does all the provider-shaped work**, which is the point. It knows that a
    submission answers with a receipt and nothing else, that the record has to be fetched, and how
    this service says *I hold no such thing* as against *I could not answer*. Everything above it
    sees an :class:`~careloop.tools.results.ActionResult` or one of two neutral failures.
    """

    def submit_and_read_back(args: Mapping[str, str], idempotency_key: str) -> ActionResult:
        receipt = service.submit(idempotency_key, args)
        return _as_booking(receipt, service.record(receipt))

    def observe(result: ActionResult) -> ActionResult:
        try:
            held = service.record(result.confirmation_id)
        except NoSuchLedgerRecord as absent:
            raise ObservationAbsent(
                f"the ledger could not confirm {result.confirmation_id}: {absent}"
            ) from absent
        except (LedgerDeclined, LedgerDown) as failure:
            raise ObservationUnavailable(
                f"the ledger could not confirm {result.confirmation_id}: {failure}"
            ) from failure
        return _as_booking(result.confirmation_id, held)

    return ExternalProvider(
        name="ledger", handlers={ACTION: submit_and_read_back}, readbacks={ACTION: observe}
    )


def _as_booking(receipt: str, held: Mapping[str, str]) -> AppointmentBooked:
    """The service's own record, in the shape the verifier compares.

    Valued entirely from what was fetched. A field the service has lost yields an empty string
    rather than anything carried across from CareLoop's side, which is the fail-closed direction.
    """
    return AppointmentBooked(
        confirmation_id=receipt,
        provider_id=held.get("provider_id", ""),
        patient_id=held.get("patient_id", ""),
        slot_start=held.get("slot_start", ""),
        reason=held.get("reason", ""),
    )


@pytest.fixture
def service() -> Iterator[LedgerService]:
    yield LedgerService()


def stranded(store: InMemoryCaseStore) -> None:
    """A case a process abandoned mid-effect: prepared, dispatched, nothing recorded."""
    executing_with_a_prepared_action(store, tool_name=ACTION, args=BOOKING_ARGS, case_id=CASE_ID)


def run(store: InMemoryCaseStore, service: LedgerService):
    """One graph run, with both model seams primed to raise if a recovery consults one."""
    context = context_for(
        store,
        SAGA_EVENT,
        case_id=CASE_ID,
        triage=ExplodingTriage(),
        planner=ExplodingPlanner(),
        now=lambda: FIXED_NOW,
        providers=(ledger_provider(service),),
    )
    asyncio.run(run_case(context))
    return store.get(CASE_ID)


class TestTheSagaCarriesAProviderItHasNeverHeardOf:
    """A stranded action, reconciled and certified against a provider written in this file."""

    def test_it_reaches_done(self, service: LedgerService) -> None:
        store = InMemoryCaseStore()
        stranded(store)

        case = run(store, service)

        assert case.state is CaseState.DONE

    def test_under_the_identity_the_case_froze(self, service: LedgerService) -> None:
        """No new key, no new arguments, nothing re-derived from anything a model said."""
        store = InMemoryCaseStore()
        stranded(store)

        case = run(store, service)

        assert len(case.actions) == 1
        assert case.actions[0].idempotency_key == booking_key()
        assert case.actions[0].action_type == ACTION

    def test_the_provider_holds_exactly_one_record(self, service: LedgerService) -> None:
        store = InMemoryCaseStore()
        stranded(store)

        run(store, service)

        assert len(service) == 1

    def test_the_result_came_from_the_provider_rather_than_from_the_case(
        self, service: LedgerService
    ) -> None:
        """The submission returned a receipt; every field was fetched."""
        store = InMemoryCaseStore()
        stranded(store)

        case = run(store, service)
        result = rehydrate(ACTION, case.actions[0].result)

        assert result.confirmation_id.startswith("LR-")
        assert result.patient_id == BOOKING_ARGS["patient_id"]
        assert result.slot_start == BOOKING_ARGS["slot_start"]

    def test_an_independent_read_is_what_certified_it(self, service: LedgerService) -> None:
        store = InMemoryCaseStore()
        stranded(store)

        case = run(store, service)

        (passed,) = [
            entry for entry in case.timeline if entry.kind is TimelineKind.VERIFICATION_PASSED
        ]
        assert passed.payload["observation"] == ObservationOutcome.MATCHED
        assert passed.payload["read_back"] == "true"

    def test_the_reconstruction_and_the_observation_are_separate_reads(
        self, service: LedgerService
    ) -> None:
        """One submission, two reads — the adapter's own, and ``VerifyNode``'s.

        The same separation ``tests/test_upload_recovery.py`` proves for the object store, reached
        by a different route: here the *first* attempt already has to fetch, because this provider's
        write never returns a record at all.
        """
        store = InMemoryCaseStore()
        stranded(store)

        run(store, service)

        assert service.submissions == 1
        assert service.reads == 2

    def test_no_model_was_consulted(self, service: LedgerService) -> None:
        """Both seams raise if touched, so reaching ``DONE`` is itself the assertion."""
        store = InMemoryCaseStore()
        stranded(store)

        case = run(store, service)

        assert case.state is CaseState.DONE
        assert (
            len([entry for entry in case.timeline if entry.kind is TimelineKind.PLAN_PROPOSED]) == 1
        )


class TestTheSyntheticContractIsTheThirdShape:
    """The fixture's own properties, so it cannot quietly stop being what it claims to be.

    Everything above rests on this service being create-once by submission key and never returning
    a record from a write. A fixture that lost either would still pass most of the lifecycle tests
    and would stop proving anything — the divergence test is the one that noticed when an earlier
    draft of this file got the create-once check wrong.
    """

    def test_a_duplicate_submission_creates_nothing_and_returns_the_same_receipt(
        self, service: LedgerService
    ) -> None:
        first = service.submit("key-0001", BOOKING_ARGS)
        second = service.submit("key-0001", {**BOOKING_ARGS, "patient_id": "PT-OTHER"})

        assert first == second
        assert len(service) == 1
        assert service.submissions == 2
        assert service.record(first)["patient_id"] == BOOKING_ARGS["patient_id"]

    def test_a_submission_answers_with_a_receipt_and_not_with_the_record(
        self, service: LedgerService
    ) -> None:
        """Neither shipped provider behaves this way, which is the whole reason it is here."""
        receipt = service.submit("key-0002", BOOKING_ARGS)

        assert isinstance(receipt, str)
        assert service.reads == 0, "the write handed back a record and nothing had to be fetched"

    def test_a_receipt_it_does_not_hold_is_a_definite_answer(self, service: LedgerService) -> None:
        with pytest.raises(NoSuchLedgerRecord):
            service.record("LR-nothing")

    def test_and_being_unreachable_is_a_different_one(self, service: LedgerService) -> None:
        service.reachable = False

        with pytest.raises(LedgerDown):
            service.record("LR-nothing")


class TestItsFailuresAreTheOnesTheSagaAlreadySpeaks:
    """The adapter translates; the saga reasons about what a call established, and nothing more."""

    def test_a_provider_that_does_not_answer_leaves_the_case_waiting(
        self, service: LedgerService
    ) -> None:
        """``WAITING_EXTERNAL``: the outcome is unknown and re-issuing the frozen key is safe."""
        store = InMemoryCaseStore()
        stranded(store)
        service.reachable = False

        case = run(store, service)

        assert case.state is CaseState.WAITING_EXTERNAL
        assert case.actions == ()

    def test_and_a_later_run_settles_it_once_the_provider_comes_back(
        self, service: LedgerService
    ) -> None:
        """No restart, no human, no new identity — the shipped convergence over a new provider."""
        store = InMemoryCaseStore()
        stranded(store)
        service.reachable = False
        run(store, service)

        service.reachable = True
        case = run(store, service)

        assert case.state is CaseState.DONE
        assert len(case.actions) == 1
        assert case.actions[0].idempotency_key == booking_key()
        assert len(service) == 1

    def test_a_provider_that_diverges_is_not_certified(self, service: LedgerService) -> None:
        """The independence proof, for a provider written in this file.

        The submission and the reconstruction both succeed, the record is written, and the world
        then changes underneath. Verification has to notice, or the read-back is decorative.
        """
        store = InMemoryCaseStore()
        stranded(store)
        service.reachable = False
        run(store, service)

        service.reachable = True
        service.submit(booking_key(), BOOKING_ARGS)
        receipt = _only_receipt(service)
        service.rewrite(receipt, patient_id="PT-SOMEBODY-ELSE")
        case = run(store, service)

        assert case.state is CaseState.AWAITING_HUMAN
        (failed,) = [
            entry for entry in case.timeline if entry.kind is TimelineKind.VERIFICATION_FAILED
        ]
        assert failed.payload["observation"] == ObservationOutcome.DIVERGED


class TestAnUnsafeProviderNeedsNoNodeEdit:
    """The other corner, stated against the declarations rather than re-driven.

    A provider offering no safe duplicate-send semantics integrates by being declared
    ``NOT_REPLAYABLE``, and one offering no way to be read integrates by declaring no read-back.
    Both answers are already computed by :func:`~careloop.graph.recovery.may_replay`, which
    ``ReconcileNode`` consults before anything is issued — so an unsafe provider changes a
    declaration and a client, and no node.

    The *behavioural* halves have non-vacuous homes already and are deliberately not repeated here:
    ``tests/test_reconcile_liveness.py::TestANonReplayableEffectCannotBeWokenAtAll`` drives a
    stranded unreplayable action to a park with an observer bound, so the recovery class is provably
    what refused it, and
    ``tests/test_saga_recovery.py::TestAReplayWithNoObserverIsNotAllowedEither`` drives the other
    condition.
    """

    def test_no_safe_duplicate_send_means_no_automatic_replay(self) -> None:
        unsafe = {"transactional_mail": RecoveryClass.NOT_REPLAYABLE}

        assert unsafe["transactional_mail"] is RecoveryClass.NOT_REPLAYABLE
        assert not may_replay("transactional_mail", observers={"transactional_mail"})

    def test_no_observer_means_no_automatic_replay_either(self) -> None:
        """Even for a type whose write contract *is* measured safe."""
        assert RECOVERY_CLASS[ACTION] is RecoveryClass.IDEMPOTENT_REPLAYABLE
        assert not may_replay(ACTION, observers=frozenset())

    def test_a_provider_may_offer_a_write_without_offering_a_read(
        self, service: LedgerService
    ) -> None:
        """``ExternalProvider`` allows it, and that is what makes the corner representable."""
        write_only = ExternalProvider(
            name="ledger-write-only",
            handlers=ledger_provider(service).handlers,
            readbacks={},
        )

        assert set(write_only.handlers) == {ACTION}
        assert write_only.readbacks == {}
        assert not may_replay(ACTION, observers=write_only.readbacks)


def _only_receipt(service: LedgerService) -> str:
    """The one receipt the service holds. Fails loudly if the test set up more than one."""
    (receipt,) = service._records
    return receipt
