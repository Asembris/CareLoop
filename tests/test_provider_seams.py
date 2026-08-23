"""A run is bound to providers, and the recovery path names none of them.

Two couplings kept the external-effect architecture booking-shaped, and neither would have failed a
test on the day a second provider arrived — they would have failed a *case*.

**The recovery path caught one client's exception types by name.**
:func:`careloop.graph.recovery.dispatch_prepared` is where transport becomes recovery vocabulary,
and its own docstring claimed nothing above it knew that a clinic speaks HTTP. That was true of its
callers and false of the function: it caught ``ClinicUnavailable`` and ``ClinicRefused``. A provider
raising anything else would have passed straight through untranslated, so a lost response would
have ended the case ``FAILED`` — the session-Q defect, arriving through a different door.

**The read-backs were a literal.** ``RunContext`` built them as
``{} if clinic is None else {"book_appointment": ...}`` — a tool name spelled in lifecycle wiring,
which is the thing ``check_verifiers_cover_the_registry`` exists to prevent one layer down.

Both are now provider-neutral, and **booking behaviour is unchanged**: a clinic-bound run has the
same executor, the same single observer and the same verdicts it had before. That is asserted here
and, more convincingly, by every booking suite continuing to pass untouched.
"""

from __future__ import annotations

import pytest
from clinic_helpers import clinic_server
from helpers import FIXED_NOW

from careloop.events.inbound import REFERRAL_EVENT
from careloop.graph.context import ConflictingExecutors, RunContext
from careloop.graph.providers import (
    ConflictingProviders,
    ExternalProvider,
    clinic_provider,
    merged_handlers,
    merged_readbacks,
)
from careloop.graph.recovery import (
    DispatchRefused,
    DispatchUnavailable,
    dispatch_prepared,
)
from careloop.graph.runner import context_for, open_case
from careloop.state import InMemoryCaseStore
from careloop.tools.clinic import ClinicClient, ClinicRefused, ClinicUnavailable
from careloop.tools.executor import DEFAULT_HANDLERS, MockExternalExecutor
from careloop.tools.provider import ProviderRefused, ProviderUnavailable
from careloop.tools.results import EmailSent

CASE_ID = "case-0001"


class ForeignUnavailable(ProviderUnavailable):
    """A provider that is not the clinic, being unreachable."""


class ForeignRefused(ProviderRefused):
    """A provider that is not the clinic, declining."""


def _raises(failure: Exception):
    def dispatch():
        raise failure

    return dispatch


class TestTheClinicSpeaksTheNeutralVocabulary:
    """Widening a base class adds callers and removes none, so nothing that caught these moved."""

    def test_unreachable_is_a_provider_unavailable(self) -> None:
        assert issubclass(ClinicUnavailable, ProviderUnavailable)

    def test_refused_is_a_provider_refused(self) -> None:
        assert issubclass(ClinicRefused, ProviderRefused)

    def test_the_two_are_still_distinct(self) -> None:
        """A refusal is the provider answering; unavailability is nobody answering.

        Collapsing them under one base would let a future ``except`` clause stop distinguishing
        them, which is the distinction the whole waiting lifecycle turns on.
        """
        assert not issubclass(ClinicUnavailable, ProviderRefused)
        assert not issubclass(ClinicRefused, ProviderUnavailable)

    def test_a_refusal_still_carries_the_status_its_one_reader_wants(self) -> None:
        refusal = ClinicRefused(operation="read_appointment", status_code=404)

        assert refusal.status_code == 404
        assert refusal.operation == "read_appointment"


class TestDispatchTranslatesAnyProvidersFailure:
    """The property the recovery path was missing, asserted with a provider that is not a clinic."""

    def test_an_unreachable_foreign_provider_leaves_the_outcome_unknown(self) -> None:
        with pytest.raises(DispatchUnavailable):
            dispatch_prepared(_raises(ForeignUnavailable("nobody answered")))

    def test_a_refusing_foreign_provider_is_a_refusal(self) -> None:
        with pytest.raises(DispatchRefused):
            dispatch_prepared(_raises(ForeignRefused("declined")))

    def test_the_clinic_is_translated_exactly_as_it_was(self) -> None:
        """Booking's behaviour, restated so a change to it would be visible here."""
        with pytest.raises(DispatchUnavailable):
            dispatch_prepared(_raises(ClinicUnavailable("unreachable")))
        with pytest.raises(DispatchRefused):
            dispatch_prepared(_raises(ClinicRefused(operation="book", status_code=409)))

    def test_the_originating_failure_is_kept_as_the_cause(self) -> None:
        """Deciding which *causes* are worth retrying is a later judgement, not a lost one."""
        original = ForeignUnavailable("nobody answered")

        with pytest.raises(DispatchUnavailable) as raised:
            dispatch_prepared(_raises(original))

        assert raised.value.__cause__ is original

    @pytest.mark.parametrize(
        "failure", [ValueError("a bug"), KeyError("a bug")], ids=["value", "key"]
    )
    def test_anything_that_is_not_transport_passes_through_untouched(
        self, failure: Exception
    ) -> None:
        """CareLoop being wrong about itself must fail loudly, not read as an unknown outcome."""
        with pytest.raises(type(failure)):
            dispatch_prepared(_raises(failure))

    def test_a_successful_dispatch_is_returned_unchanged(self) -> None:
        sentinel = object()

        assert dispatch_prepared(lambda: sentinel) is sentinel


class TestProvidersMerge:
    """Handlers and observers are assembled, and a collision is refused rather than resolved."""

    @staticmethod
    def _provider(name: str, action: str) -> ExternalProvider:
        return ExternalProvider(
            name=name,
            handlers={action: lambda args, key: EmailSent(message_id="m", to="a", subject="s")},
            readbacks={action: lambda result: result},
        )

    def test_a_providers_handlers_overlay_the_simulated_defaults(self) -> None:
        merged = merged_handlers((self._provider("first", "send_email"),))

        assert merged["send_email"] is not DEFAULT_HANDLERS["send_email"]
        assert merged["book_appointment"] is DEFAULT_HANDLERS["book_appointment"]

    def test_two_providers_claiming_one_action_type_is_refused(self) -> None:
        """Whichever a merge preferred would silently decide where a real effect lands."""
        with pytest.raises(ConflictingProviders, match="send_email"):
            merged_handlers(
                (self._provider("first", "send_email"), self._provider("second", "send_email"))
            )

    def test_two_providers_observing_one_action_type_is_refused(self) -> None:
        with pytest.raises(ConflictingProviders, match="which read is the independent one"):
            merged_readbacks(
                (self._provider("first", "send_email"), self._provider("second", "send_email"))
            )

    def test_distinct_action_types_merge_cleanly(self) -> None:
        merged = merged_readbacks(
            (self._provider("first", "send_email"), self._provider("second", "upload_document"))
        )

        assert set(merged) == {"send_email", "upload_document"}


class TestARunIsBoundToProviders:
    def test_a_clinic_bound_run_observes_exactly_the_clinics_action_type(self) -> None:
        """Booking's wiring, unchanged, and now assembled rather than spelled out."""
        with clinic_server() as (base_url, _world):
            context = self._context(clinic=ClinicClient(base_url))

            assert set(context.readbacks) == {"book_appointment"}
            assert context.executor is not None
            assert context.executor.action_types == frozenset(DEFAULT_HANDLERS)

    def test_an_unbound_run_observes_nothing(self) -> None:
        context = self._context()

        assert context.readbacks == {}
        assert context.bound == ()

    def test_a_run_bound_to_a_provider_observes_its_action_type(self) -> None:
        """The generalisation, with a provider the graph has never heard of."""
        provider = TestProvidersMerge._provider("foreign", "send_email")
        context = self._context(providers=(provider,))

        assert set(context.readbacks) == {"send_email"}
        assert context.bound == (provider,)

    def test_a_clinic_and_a_provider_are_both_bound(self) -> None:
        with clinic_server() as (base_url, _world):
            provider = TestProvidersMerge._provider("foreign", "send_email")
            context = self._context(clinic=ClinicClient(base_url), providers=(provider,))

            assert set(context.readbacks) == {"book_appointment", "send_email"}

    def test_a_provider_and_an_executor_together_are_unconstructable(self) -> None:
        """The rule that stops a real effect being verified against a simulated read."""
        provider = TestProvidersMerge._provider("foreign", "send_email")

        with pytest.raises(ConflictingExecutors):
            self._context(providers=(provider,), executor=MockExternalExecutor())

    def test_a_clinic_and_an_executor_together_are_still_unconstructable(self) -> None:
        with clinic_server() as (base_url, _world), pytest.raises(ConflictingExecutors):
            self._context(clinic=ClinicClient(base_url), executor=MockExternalExecutor())

    def test_the_clinic_provider_declares_one_write_and_one_read(self) -> None:
        with clinic_server() as (base_url, _world):
            provider = clinic_provider(ClinicClient(base_url))

            assert set(provider.handlers) == {"book_appointment"}
            assert set(provider.readbacks) == {"book_appointment"}

    @staticmethod
    def _context(**kwargs: object) -> RunContext:
        store = InMemoryCaseStore()
        open_case(store, REFERRAL_EVENT, case_id=CASE_ID, now=FIXED_NOW)
        return context_for(store, REFERRAL_EVENT, case_id=CASE_ID, **kwargs)


class TestNoLifecycleModuleSpellsAProviderAction:
    """The literal that used to sit in ``RunContext``, swept for."""

    def test_the_context_module_no_longer_names_a_tool_to_build_an_observer(self) -> None:
        from pathlib import Path

        import careloop.graph.context as context_module

        source = Path(context_module.__file__).read_text(encoding="utf-8")

        assert '"book_appointment"' not in source
        assert "'book_appointment'" not in source
