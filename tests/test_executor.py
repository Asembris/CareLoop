"""The mock executor: one effect per key, and a replay that performs nothing.

T3 rests on these. The ledger is the only place an effect is recorded, so a second entry under one
key is a double effect, and a returned result that differs from the first is a replay that did not
replay.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from careloop.tools.executor import (
    DEFAULT_HANDLERS,
    ExecutionOutcome,
    IdempotencyKeyCollision,
    MockExternalExecutor,
    UnknownActionType,
    _check_handlers_cover_the_registry,
)
from careloop.tools.idempotency import UncanonicalizableArgument, idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import ActionResult, AppointmentBooked, EmailSent

CASE = "case-0001"

EMAIL_ARGS: dict[str, object] = {
    "to": "clinic@trusted.example",
    "subject": "Cardiology referral",
    "body": "Please find the referral attached.",
}


def email_key(args: dict[str, object] | None = None) -> str:
    return idempotency_key(CASE, "send_email", EMAIL_ARGS if args is None else args)


@pytest.fixture
def executor() -> MockExternalExecutor:
    return MockExternalExecutor()


class TestFirstInvocation:
    def test_an_effect_is_performed_and_recorded(self, executor: MockExternalExecutor) -> None:
        key = email_key()
        outcome = executor.invoke("send_email", EMAIL_ARGS, key)

        assert outcome.outcome is ExecutionOutcome.EXECUTED
        assert outcome.executed
        assert executor.invocation_count == 1
        assert executor.suppressed_count == 0
        assert executor.keys == {key}

    def test_the_result_is_typed_and_keeps_its_concrete_class(
        self, executor: MockExternalExecutor
    ) -> None:
        """Declaring the field as ``ActionResult`` must not flatten the subclass away."""
        result = executor.invoke("send_email", EMAIL_ARGS, email_key()).result
        assert isinstance(result, EmailSent)
        assert result.to == "clinic@trusted.example"

    def test_the_ledger_entry_records_the_call(self, executor: MockExternalExecutor) -> None:
        key = email_key()
        executor.invoke("send_email", EMAIL_ARGS, key)
        entry = executor.entry_for(key)

        assert entry is not None
        assert entry.action_type == "send_email"
        assert entry.args == {name: str(value) for name, value in EMAIL_ARGS.items()}
        assert entry.idempotency_key == key

    def test_a_new_executor_starts_empty(self) -> None:
        """Ledger state is per instance, not shared through a mutable class default."""
        first = MockExternalExecutor()
        first.invoke("send_email", EMAIL_ARGS, email_key())
        assert MockExternalExecutor().invocation_count == 0
        assert first.invocation_count == 1


class TestReplay:
    def test_the_same_key_twice_performs_one_effect(self, executor: MockExternalExecutor) -> None:
        key = email_key()
        first = executor.invoke("send_email", EMAIL_ARGS, key)
        second = executor.invoke("send_email", EMAIL_ARGS, key)

        assert executor.invocation_count == 1
        assert len(executor.ledger) == 1
        assert second.outcome is ExecutionOutcome.DUPLICATE_SUPPRESSED
        assert not second.executed
        assert second.result == first.result

    def test_the_replayed_result_is_the_recorded_object_itself(
        self, executor: MockExternalExecutor
    ) -> None:
        """Equal is not enough; a re-derived result would be equal and would prove nothing."""
        key = email_key()
        first = executor.invoke("send_email", EMAIL_ARGS, key)
        second = executor.invoke("send_email", EMAIL_ARGS, key)
        assert second.result is first.result

    def test_suppressions_are_counted_without_growing_the_ledger(
        self, executor: MockExternalExecutor
    ) -> None:
        key = email_key()
        for _ in range(5):
            executor.invoke("send_email", EMAIL_ARGS, key)

        assert executor.invocation_count == 1
        assert executor.suppressed_count == 4
        assert len(executor.ledger) == 1

    def test_distinct_keys_perform_distinct_effects(self, executor: MockExternalExecutor) -> None:
        other = dict(EMAIL_ARGS) | {"subject": "Second referral"}
        executor.invoke("send_email", EMAIL_ARGS, email_key())
        executor.invoke("send_email", other, email_key(other))

        assert executor.invocation_count == 2
        assert len(executor.keys) == 2

    def test_a_reordered_call_is_recognised_as_the_same_effect(
        self, executor: MockExternalExecutor
    ) -> None:
        """Argument order is not part of the call, so a reordered replay must still suppress."""
        reordered = dict(reversed(list(EMAIL_ARGS.items())))
        executor.invoke("send_email", EMAIL_ARGS, email_key())
        second = executor.invoke("send_email", reordered, email_key(reordered))

        assert second.outcome is ExecutionOutcome.DUPLICATE_SUPPRESSED
        assert executor.invocation_count == 1

    def test_results_are_byte_identical_across_a_replay(
        self, executor: MockExternalExecutor
    ) -> None:
        """No clock and no randomness, asserted rather than assumed.

        A timestamp or a random id in a result would make every replay differ from its original
        and reduce T3 to "roughly the same effect".
        """
        key = email_key()
        first = executor.invoke("send_email", EMAIL_ARGS, key)
        fresh = MockExternalExecutor().invoke("send_email", EMAIL_ARGS, key)
        assert fresh.result.model_dump_json() == first.result.model_dump_json()


class TestRefusals:
    def test_an_unknown_action_type_is_refused(self, executor: MockExternalExecutor) -> None:
        with pytest.raises(UnknownActionType, match="wire_transfer"):
            executor.invoke("wire_transfer", {"amount": 100}, "k")

    def test_a_refused_action_records_nothing(self, executor: MockExternalExecutor) -> None:
        """No half-effects. A refusal must leave the ledger exactly as it was."""
        with pytest.raises(UnknownActionType):
            executor.invoke("wire_transfer", {"amount": 100}, "k")
        assert executor.invocation_count == 0
        assert executor.ledger == ()

    def test_an_uncanonicalizable_argument_is_refused_before_any_effect(
        self, executor: MockExternalExecutor
    ) -> None:
        with pytest.raises(UncanonicalizableArgument):
            executor.invoke("send_email", dict(EMAIL_ARGS) | {"subject": 1.5}, "k")
        assert executor.invocation_count == 0

    def test_one_key_presented_for_two_different_calls_is_refused(
        self, executor: MockExternalExecutor
    ) -> None:
        """Reusing a key across different arguments would report one effect as another."""
        key = email_key()
        executor.invoke("send_email", EMAIL_ARGS, key)
        with pytest.raises(IdempotencyKeyCollision):
            executor.invoke("send_email", dict(EMAIL_ARGS) | {"to": "other@evil.example"}, key)

        assert executor.invocation_count == 1

    def test_one_key_presented_for_two_action_types_is_refused(
        self, executor: MockExternalExecutor
    ) -> None:
        key = email_key()
        executor.invoke("send_email", EMAIL_ARGS, key)
        with pytest.raises(IdempotencyKeyCollision, match="send_email"):
            executor.invoke("upload_document", {"portal_id": "p", "document_id": "d"}, key)

    def test_a_collision_is_not_counted_as_a_suppression(
        self, executor: MockExternalExecutor
    ) -> None:
        key = email_key()
        executor.invoke("send_email", EMAIL_ARGS, key)
        with pytest.raises(IdempotencyKeyCollision):
            executor.invoke("send_email", dict(EMAIL_ARGS) | {"to": "other@evil.example"}, key)
        assert executor.suppressed_count == 0


#: One well-formed call per declared external effect. Held to the registry by a test below, so a
#: sixth tool cannot be added without appearing here.
CALLS: dict[str, dict[str, object]] = {
    "send_email": EMAIL_ARGS,
    "book_appointment": {
        "provider_id": "PROV-77",
        "patient_id": "PT-4417",
        "slot_start": datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
        "reason": "cardiology review",
    },
    "upload_document": {"portal_id": "PORTAL-9", "document_id": "doc-0001-referral"},
    "grant_portal_access": {
        "grantee": "carer@trusted.example",
        "role": "viewer",
        "portal_id": "PORTAL-9",
    },
}


class TestEveryEffectfulAction:
    """Each declared external effect executes, records once, and returns its own type."""

    def test_the_call_table_covers_every_declared_external_effect(self) -> None:
        """Guards the loops below from passing by testing nothing."""
        assert set(CALLS) == REGISTRY.external_effect_tools
        assert CALLS

    @pytest.mark.parametrize("action_type", sorted(CALLS))
    def test_it_executes_once_and_replays_thereafter(
        self, action_type: str, executor: MockExternalExecutor
    ) -> None:
        args = CALLS[action_type]
        key = idempotency_key(CASE, action_type, args)

        first = executor.invoke(action_type, args, key)
        second = executor.invoke(action_type, args, key)

        assert first.outcome is ExecutionOutcome.EXECUTED
        assert second.outcome is ExecutionOutcome.DUPLICATE_SUPPRESSED
        assert executor.entries_of_type(action_type) == executor.ledger
        assert executor.invocation_count == 1

    @pytest.mark.parametrize("action_type", sorted(CALLS))
    def test_it_returns_an_action_result(
        self, action_type: str, executor: MockExternalExecutor
    ) -> None:
        args = CALLS[action_type]
        result = executor.invoke(action_type, args, idempotency_key(CASE, action_type, args)).result
        assert isinstance(result, ActionResult)
        assert not isinstance(result, str)

    def test_a_datetime_argument_is_recorded_in_canonical_text(
        self, executor: MockExternalExecutor
    ) -> None:
        args = CALLS["book_appointment"]
        result = executor.invoke(
            "book_appointment", args, idempotency_key(CASE, "book_appointment", args)
        ).result
        assert isinstance(result, AppointmentBooked)
        assert result.slot_start == "2026-09-03T10:00:00+00:00"


class TestHandlerCoverage:
    """The shipped handlers and the registry must not drift apart."""

    def test_the_shipped_handlers_match_the_registry(self) -> None:
        assert set(DEFAULT_HANDLERS) == REGISTRY.external_effect_tools

    def test_the_registry_actually_declares_external_effect_tools(self) -> None:
        """Otherwise the check above is satisfied by two empty sets."""
        assert REGISTRY.external_effect_tools

    def test_the_drift_check_fires_when_a_handler_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vacuity: the import-time check must be capable of failing."""
        thinned = {
            name: handler for name, handler in DEFAULT_HANDLERS.items() if name != "send_email"
        }
        monkeypatch.setattr("careloop.tools.executor.DEFAULT_HANDLERS", thinned)
        with pytest.raises(RuntimeError, match="send_email"):
            _check_handlers_cover_the_registry()

    def test_the_drift_check_fires_on_a_handler_for_an_undeclared_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dangerous direction: an effect the policy layer has never heard of."""
        widened = dict(DEFAULT_HANDLERS) | {"wire_transfer": DEFAULT_HANDLERS["send_email"]}
        monkeypatch.setattr("careloop.tools.executor.DEFAULT_HANDLERS", widened)
        with pytest.raises(RuntimeError, match="wire_transfer"):
            _check_handlers_cover_the_registry()

    def test_a_custom_handler_set_is_honoured(self) -> None:
        """Tests need narrow executors; the registry check binds the shipped set only."""
        executor = MockExternalExecutor({"send_email": DEFAULT_HANDLERS["send_email"]})
        assert executor.action_types == {"send_email"}
        with pytest.raises(UnknownActionType):
            executor.invoke("book_appointment", {}, "k")


class TestLedgerIsNotWritableFromOutside:
    def test_the_ledger_property_returns_a_tuple(self, executor: MockExternalExecutor) -> None:
        executor.invoke("send_email", EMAIL_ARGS, email_key())
        assert isinstance(executor.ledger, tuple)

    def test_ledger_entries_are_frozen(self, executor: MockExternalExecutor) -> None:
        from pydantic import ValidationError

        executor.invoke("send_email", EMAIL_ARGS, email_key())
        with pytest.raises(ValidationError):
            executor.ledger[0].action_type = "something_else"
