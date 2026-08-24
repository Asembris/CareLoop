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

import ast
from pathlib import Path

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
from careloop.graph.trusted import DEMO_CAREGIVER
from careloop.state import InMemoryCaseStore
from careloop.tools.clinic import ClinicClient, ClinicRefused, ClinicUnavailable
from careloop.tools.executor import DEFAULT_HANDLERS, MockExternalExecutor
from careloop.tools.provider import ProviderRefused, ProviderUnavailable
from careloop.tools.results import EmailSent

CASE_ID = "case-0001"

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        open_case(store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=CASE_ID, now=FIXED_NOW)
        return context_for(store, REFERRAL_EVENT, case_id=CASE_ID, **kwargs)


class TestNoLifecycleModuleSpellsAProviderAction:
    """The saga engine may not learn how any provider works, and CI proves it rather than review.

    **What the engine is allowed to know**, and this is the whole of it: a prepared action identity,
    a provider result, "nobody answered", "the provider declined", whether a replay is safe, whether
    an independent observer is bound, and what a read established. Everything below that — a status
    code, a confirmation id, an object key, a bucket, a precondition — belongs to an adapter.

    **The rules are AST-shaped on purpose.** This repository explains its architecture by *naming*
    the things it forbids, at length, in docstrings; a raw-text sweep over these modules would flag
    the explanation of the rule as a violation of it, and would be deleted within a week. So
    docstrings are excluded structurally, and what is checked is the code.

    **A declaration table is not a branch, and the distinction is the load-bearing one.**
    :data:`~careloop.graph.recovery.RECOVERY_CLASS` and
    :data:`~careloop.tools.executor.DEFAULT_HANDLERS` must name every effect tool — that is what
    makes them coverage-checkable against the registry. What must never appear is the same string
    anywhere a decision is made. So a tool name is permitted as a constant inside a *module-level
    assignment to an upper-case name* and nowhere else, which is exactly the difference between::

        RECOVERY_CLASS = {"upload_document": RecoveryClass.IDEMPOTENT_REPLAYABLE, ...}   # fine
        if prepared.tool_name == "upload_document": ...                                  # not

    **Three roles, three rule sets**, because the modules do genuinely different jobs:

    ``SAGA_MODULES``
        The recovery and lifecycle engine. Strictest: no provider token in code, in an identifier,
        or in an import.

    ``careloop/tools/executor.py``
        The dispatch seam, which *owns* the simulated default handlers a provider overlays. Its
        ``_book_appointment`` and friends are adapters, so the identifier rule would be wrong there;
        the string and import rules still apply, and its tool names are dict keys.

    ``careloop/graph/context.py``
        The one tolerated legacy exception, below, pinned to an exact allowance.
    """

    #: The generic recovery and lifecycle engine.
    SAGA_MODULES = (
        "careloop/graph/nodes.py",
        "careloop/graph/runner.py",
        "careloop/graph/recovery.py",
        "careloop/graph/builder.py",
        "careloop/graph/conditions.py",
        "careloop/graph/witness.py",
    )

    #: The dispatch seam. String and import rules only — see the class docstring.
    EXECUTOR_MODULE = "careloop/tools/executor.py"

    #: What a provider knows and the saga must not. Matched case-insensitively as substrings, so
    #: ``ClinicRefused``, ``OBJECT_STORE_ACTION`` and ``PreconditionFailed`` are all reached.
    PROVIDER_TOKENS = (
        "book_appointment",
        "upload_document",
        "send_email",
        "grant_portal_access",
        "clinic",
        "objectstore",
        "object_store",
        "bucket",
        "nosuchkey",
        "preconditionfailed",
        "ifnonematch",
        "confirmation_id",
        "object_key",
        "appointment",
    )

    #: Modules whose import into the engine would be provider mechanics arriving wholesale.
    PROVIDER_MODULES = ("clinic", "objectstore", "boto3", "httpx", "demoworld")

    @staticmethod
    def _tree(module: str) -> ast.Module:
        return ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))

    @classmethod
    def _explanatory_strings(cls, tree: ast.Module) -> set[int]:
        """Every string the module uses to *describe* itself rather than to compute with.

        Docstrings and attribute docstrings — a bare string expression in a module, class or
        function body — plus every constant inside a module-level assignment to an upper-case name,
        which is this repository's spelling of a declaration table.
        """
        explanatory: set[int] = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list):
                for statement in body:
                    if isinstance(statement, ast.Expr) and isinstance(
                        statement.value, ast.Constant
                    ):
                        explanatory.add(id(statement.value))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if not all(isinstance(t, ast.Name) and t.id.isupper() for t in targets):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant):
                    explanatory.add(id(sub))
        return explanatory

    @classmethod
    def _leaked_strings(cls, module: str) -> list[tuple[int, str]]:
        tree = cls._tree(module)
        explanatory = cls._explanatory_strings(tree)
        leaks: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in explanatory:
                continue
            if isinstance(node.value, str):
                lowered = node.value.lower()
                if any(token in lowered for token in cls.PROVIDER_TOKENS):
                    leaks.append((node.lineno, node.value[:60]))
            elif isinstance(node.value, int) and 400 <= node.value <= 599:
                # A provider's status code, arriving where only "answered" or "did not" belongs.
                leaks.append((node.lineno, str(node.value)))
        return leaks

    @classmethod
    def _leaked_identifiers(cls, module: str) -> list[tuple[int, str]]:
        leaks: list[tuple[int, str]] = []
        for node in ast.walk(cls._tree(module)):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            else:
                continue
            if any(token in name.lower() for token in cls.PROVIDER_TOKENS):
                leaks.append((node.lineno, name))
        return leaks

    @classmethod
    def _provider_imports(cls, module: str) -> list[str]:
        imported: list[str] = []
        for node in ast.walk(cls._tree(module)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        return [
            name for name in imported if any(provider in name for provider in cls.PROVIDER_MODULES)
        ]

    @pytest.mark.parametrize("module", SAGA_MODULES)
    def test_the_saga_computes_with_no_provider_string(self, module: str) -> None:
        assert self._leaked_strings(module) == []

    @pytest.mark.parametrize("module", SAGA_MODULES)
    def test_the_saga_names_no_provider_identifier(self, module: str) -> None:
        assert self._leaked_identifiers(module) == []

    @pytest.mark.parametrize("module", SAGA_MODULES)
    def test_the_saga_imports_no_provider(self, module: str) -> None:
        assert self._provider_imports(module) == []

    def test_the_dispatch_seam_computes_with_no_provider_string_either(self) -> None:
        """Its tool names are keys of ``DEFAULT_HANDLERS``; none is a branch."""
        assert self._leaked_strings(self.EXECUTOR_MODULE) == []
        assert self._provider_imports(self.EXECUTOR_MODULE) == []

    def test_the_sweep_would_notice_a_branch(self) -> None:
        """Vacuity, and it is the whole point of the class.

        The two mutants the audit named, checked against the real rule rather than against a
        source file — a test that had to edit ``nodes.py`` to prove itself would be a test nobody
        could run twice.
        """
        for tool in ("upload_document", "book_appointment"):
            branch = ast.parse(f'if prepared.tool_name == "{tool}":\n    pass\n')
            leaked = [
                node.value
                for node in ast.walk(branch)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(token in node.value.lower() for token in self.PROVIDER_TOKENS)
                and id(node) not in self._explanatory_strings(branch)
            ]

            assert leaked == [tool]

    def test_the_sweep_would_notice_a_status_code(self) -> None:
        """``412`` and ``409`` are adapter vocabulary. The saga speaks refused-or-unavailable."""
        raw = ast.parse("if response.status_code == 412:\n    pass\n")
        leaked = [
            node.value
            for node in ast.walk(raw)
            if isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value >= 400
        ]

        assert leaked == [412]

    def test_a_declaration_table_is_not_a_leak(self) -> None:
        """The other half of vacuity: the rule must not forbid what the architecture requires."""
        table = ast.parse('RECOVERY_CLASS = {"book_appointment": 1, "upload_document": 2}\n')
        explanatory = self._explanatory_strings(table)
        leaked = [
            node.value
            for node in ast.walk(table)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in explanatory
        ]

        assert leaked == []


class TestTheOneToleratedProviderCoupling:
    """``RunContext.clinic`` is legacy, inert, and pinned so it cannot spread.

    **Why it is still here.** ``RunContext`` carries a ``clinic: ClinicClient | None`` field that
    predates :class:`~careloop.graph.providers.ExternalProvider`. ``__post_init__`` converts it with
    :func:`~careloop.graph.providers.clinic_provider` on the way in and **nothing below that line
    reads it** — every handler, every observer and every recovery decision comes from ``bound``,
    which is assembled the same way for the clinic and for any other provider. Removing the field
    would touch sixty-odd call sites across production and tests for no behavioural change, which
    is a refactor for tidiness rather than for correctness.

    **What is not tolerated is a second one.** The allowance below is an *exact* set of names, so
    adding an ``objects: ObjectStoreClient | None`` field beside it — the obvious way to wire the
    next provider, and the way this one was wired — fails here. That is the whole value of writing
    the exception down: the composition root
    (:func:`careloop.api.deps.world_context_factory`) already passes the object store as an
    ordinary provider, and this keeps that the only way.
    """

    MODULE = "careloop/graph/context.py"

    #: Every provider-shaped name the run context may still spell. Exact, not a prefix rule.
    ALLOWED_NAMES = frozenset({"clinic", "ClinicClient", "clinic_provider"})

    #: The one provider module it may import.
    ALLOWED_IMPORT = "careloop.tools.clinic"

    def test_the_only_provider_names_left_are_the_clinic_field_and_its_conversion(self) -> None:
        leaked = {
            name
            for _line, name in TestNoLifecycleModuleSpellsAProviderAction._leaked_identifiers(
                self.MODULE
            )
        }

        assert leaked == self.ALLOWED_NAMES

    def test_it_imports_exactly_one_provider_module(self) -> None:
        assert TestNoLifecycleModuleSpellsAProviderAction._provider_imports(self.MODULE) == [
            self.ALLOWED_IMPORT
        ]

    def test_it_still_spells_no_action_type(self) -> None:
        """The literal that used to build the read-backs. The reason this class exists."""
        assert TestNoLifecycleModuleSpellsAProviderAction._leaked_strings(self.MODULE) == []

    def test_the_field_decides_nothing_the_provider_path_does_not(self) -> None:
        """Behavioural half: a clinic-bound run and a provider-bound run assemble identically."""
        with clinic_server() as (base_url, _world):
            client = ClinicClient(base_url)
            store = InMemoryCaseStore()
            open_case(
                store, REFERRAL_EVENT, caregiver=DEMO_CAREGIVER, case_id=CASE_ID, now=FIXED_NOW
            )
            by_field = context_for(store, REFERRAL_EVENT, case_id=CASE_ID, clinic=client)
            by_provider = context_for(
                store, REFERRAL_EVENT, case_id=CASE_ID, providers=(clinic_provider(client),)
            )

            assert set(by_field.readbacks) == set(by_provider.readbacks)
            assert by_field.executor is not None
            assert by_provider.executor is not None
            assert by_field.executor.action_types == by_provider.executor.action_types
