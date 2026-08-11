"""The tool bodies, and the claim that an effect can only happen through the executor.

The vacuity test is the one that matters. "Every external-effect tool routes through the executor"
is worth nothing unless the check that says so is capable of failing, so it is run twice: over the
shipped toolset, which must pass, and over a synthetic toolset whose body forges its own result,
which must fail. A check asserted in only one direction is satisfied by an empty registry and by a
check that never looks.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from careloop.tools import callables as callables_module
from careloop.tools.callables import (
    ToolBindingError,
    ToolSet,
    UnknownTool,
    bound_tools,
    implements,
)
from careloop.tools.documents import POISONED_DOCUMENT_ID, DocumentNotFound
from careloop.tools.executor import (
    ExecutionOutcome,
    ExecutionResult,
    MockExternalExecutor,
    UnknownActionType,
)
from careloop.tools.idempotency import idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import (
    AccessGranted,
    ActionResult,
    AppointmentBooked,
    Document,
    DocumentUploaded,
    EmailSent,
    ToolResult,
)

CASE = "case-0001"

#: One well-formed call per declared external effect. Held to the registry by a test below.
EFFECT_CALLS: dict[str, dict[str, object]] = {
    "send_email": {
        "to": "clinic@trusted.example",
        "subject": "Cardiology referral",
        "body": "Please find the referral attached.",
    },
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

RESULT_TYPES: dict[str, type[ActionResult]] = {
    "send_email": EmailSent,
    "book_appointment": AppointmentBooked,
    "upload_document": DocumentUploaded,
    "grant_portal_access": AccessGranted,
}


def make_tools(executor: MockExternalExecutor) -> ToolSet:
    return ToolSet(CASE, executor=executor)


@pytest.fixture
def executor() -> MockExternalExecutor:
    return MockExternalExecutor()


@pytest.fixture
def tools(executor: MockExternalExecutor) -> ToolSet:
    return ToolSet(CASE, executor=executor)


class TestTheCallTableIsHonest:
    def test_it_covers_every_declared_external_effect(self) -> None:
        """Every loop below is driven by this table; an incomplete one tests less than it says."""
        assert set(EFFECT_CALLS) == REGISTRY.external_effect_tools
        assert set(RESULT_TYPES) == REGISTRY.external_effect_tools
        assert REGISTRY.external_effect_tools

    def test_the_arguments_match_the_registry(self) -> None:
        for tool_name, args in EFFECT_CALLS.items():
            assert set(args) == REGISTRY.get(tool_name).arg_names


class TestExternalEffects:
    @pytest.mark.parametrize("tool_name", sorted(EFFECT_CALLS))
    def test_one_call_produces_exactly_one_ledger_entry(
        self, tool_name: str, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        tools.call(tool_name, EFFECT_CALLS[tool_name])

        assert executor.invocation_count == 1
        assert len(executor.ledger) == 1
        assert executor.ledger[0].action_type == tool_name

    @pytest.mark.parametrize("tool_name", sorted(EFFECT_CALLS))
    def test_calling_twice_produces_one_effect_and_one_suppression(
        self, tool_name: str, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        first = tools.call(tool_name, EFFECT_CALLS[tool_name])
        second = tools.call(tool_name, EFFECT_CALLS[tool_name])

        assert isinstance(first, ExecutionResult)
        assert isinstance(second, ExecutionResult)
        assert first.outcome is ExecutionOutcome.EXECUTED
        assert second.outcome is ExecutionOutcome.DUPLICATE_SUPPRESSED
        assert executor.invocation_count == 1
        assert second.result == first.result

    @pytest.mark.parametrize("tool_name", sorted(EFFECT_CALLS))
    def test_the_key_is_the_one_the_derivation_produces(
        self, tool_name: str, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        """The body must key the call it actually makes, not something adjacent to it."""
        args = EFFECT_CALLS[tool_name]
        tools.call(tool_name, args)
        assert executor.keys == {idempotency_key(CASE, tool_name, args)}

    @pytest.mark.parametrize("tool_name", sorted(EFFECT_CALLS))
    def test_the_result_is_typed(self, tool_name: str, tools: ToolSet) -> None:
        outcome = tools.call(tool_name, EFFECT_CALLS[tool_name])
        assert isinstance(outcome, ToolResult)
        assert isinstance(outcome.result, RESULT_TYPES[tool_name])

    def test_two_cases_do_not_share_an_effect(self, executor: MockExternalExecutor) -> None:
        """``case_id`` scopes the key, so the same action in two cases is two effects."""
        args = EFFECT_CALLS["send_email"]
        ToolSet("case-0001", executor=executor).call("send_email", args)
        ToolSet("case-0002", executor=executor).call("send_email", args)
        assert executor.invocation_count == 2

    def test_direct_method_calls_route_the_same_way(
        self, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        """``call`` is a convenience, not the gate. Bypassing it must change nothing."""
        tools.send_email(**EFFECT_CALLS["send_email"])
        assert executor.invocation_count == 1


class TestFetchDocument:
    def test_it_returns_a_document_from_the_store(self, tools: ToolSet) -> None:
        document = tools.call("fetch_document", {"document_id": "doc-0001-referral"})
        assert isinstance(document, Document)
        assert document.document_id == "doc-0001-referral"

    def test_it_produces_no_ledger_entry(
        self, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        """Reading is not an effect. Recording it as one would weaken what the ledger means."""
        tools.call("fetch_document", {"document_id": "doc-0001-referral"})
        tools.call("fetch_document", {"document_id": POISONED_DOCUMENT_ID})
        assert executor.invocation_count == 0
        assert executor.ledger == ()

    def test_reading_twice_is_not_suppressed(
        self, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        first = tools.call("fetch_document", {"document_id": "doc-0001-referral"})
        second = tools.call("fetch_document", {"document_id": "doc-0001-referral"})
        assert first == second
        assert executor.suppressed_count == 0

    def test_an_unknown_document_raises(self, tools: ToolSet) -> None:
        with pytest.raises(DocumentNotFound):
            tools.call("fetch_document", {"document_id": "doc-9999"})

    def test_it_returns_the_poisoned_document_unmodified(self, tools: ToolSet) -> None:
        """The tool does not sanitise. Content is evidence; authority is the policy layer's job.

        Stripping the payload here would be a filter that looks like a defence and is not one —
        and it would make T2 pass for the wrong reason.
        """
        document = tools.call("fetch_document", {"document_id": POISONED_DOCUMENT_ID})
        assert "records-intake@claims-continuity-partners.example" in document.text


class TestDispatch:
    def test_an_unknown_tool_is_refused(self, tools: ToolSet) -> None:
        with pytest.raises(UnknownTool, match="wire_transfer"):
            tools.call("wire_transfer", {"amount": 100})

    def test_an_undeclared_argument_is_refused(self, tools: ToolSet) -> None:
        with pytest.raises(TypeError):
            tools.call("send_email", dict(EFFECT_CALLS["send_email"]) | {"bcc": "x@y.example"})

    def test_a_missing_argument_is_refused(self, tools: ToolSet) -> None:
        with pytest.raises(TypeError):
            tools.call("send_email", {"to": "clinic@trusted.example"})

    def test_a_refused_call_produces_no_effect(
        self, tools: ToolSet, executor: MockExternalExecutor
    ) -> None:
        for args in ({"amount": 1}, {}):
            with pytest.raises((UnknownTool, TypeError)):
                tools.call("wire_transfer", args)
        assert executor.invocation_count == 0


class TestBindings:
    def test_every_registered_tool_has_a_body(self) -> None:
        assert bound_tools() == REGISTRY.names

    def test_the_registry_is_not_empty(self) -> None:
        """Otherwise the equality above holds between two empty sets."""
        assert len(REGISTRY) == 5

    @pytest.mark.parametrize("tool_name", sorted(REGISTRY.names))
    def test_each_body_takes_exactly_the_declared_arguments(self, tool_name: str) -> None:
        from inspect import signature

        method = getattr(ToolSet, callables_module._BINDINGS[tool_name])
        parameters = {name for name in signature(method).parameters if name != "self"}
        assert parameters == REGISTRY.get(tool_name).arg_names

    def test_case_id_is_not_a_tool_argument(self) -> None:
        """The model must not be able to supply the value that scopes idempotency."""
        for tool_name in REGISTRY.names:
            assert "case_id" not in REGISTRY.get(tool_name).arg_names

    def test_binding_an_unregistered_tool_fails(self) -> None:
        with pytest.raises(ToolBindingError, match="wire_transfer"):

            @implements("wire_transfer")
            def wire_transfer(self, amount: str) -> ToolResult:  # pragma: no cover
                raise AssertionError

    def test_binding_a_body_with_the_wrong_arguments_fails(self) -> None:
        """The import-time check that makes the registry the contract."""
        with pytest.raises(ToolBindingError, match="recipient"):

            @implements("send_email")
            def send_email(self, recipient: str, subject: str, body: str) -> ToolResult:
                raise AssertionError  # pragma: no cover

    def test_binding_a_body_that_drops_an_argument_fails(self) -> None:
        """A dropped argument is a classified value the body silently ignores."""
        with pytest.raises(ToolBindingError, match="send_email"):

            @implements("send_email")
            def send_email(self, to: str, subject: str) -> ToolResult:
                raise AssertionError  # pragma: no cover

    def test_binding_a_body_that_swallows_arguments_fails(self) -> None:
        """``**kwargs`` would accept arguments the registry never classified.

        Caught by the name check rather than the kind check — ``kwargs`` is not a declared
        argument name. The kind check earns its place on the case below.
        """
        with pytest.raises(ToolBindingError):

            @implements("send_email")
            def send_email(self, **kwargs: str) -> ToolResult:
                raise AssertionError  # pragma: no cover

    def test_binding_a_positional_only_body_fails(self) -> None:
        """The one thing the parameter-kind check catches that the name check does not.

        These parameter names match the registry exactly, so the name check passes. But
        ``call`` dispatches by keyword, and a positional-only body cannot be called that way —
        without the kind check this binds cleanly at import and fails at the first invocation.
        """
        with pytest.raises(ToolBindingError, match="POSITIONAL_ONLY"):

            @implements("send_email")
            def send_email(self, to: str, subject: str, body: str, /) -> ToolResult:
                raise AssertionError  # pragma: no cover

    def test_binding_the_same_tool_twice_fails(self) -> None:
        with pytest.raises(ToolBindingError, match="already implemented"):

            @implements("send_email")
            def send_email(self, to: str, subject: str, body: str) -> ToolResult:
                raise AssertionError  # pragma: no cover

    def test_the_coverage_check_fires_when_a_body_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vacuity: the import-time coverage check must be capable of failing."""
        thinned = {
            name: bound
            for name, bound in callables_module._BINDINGS.items()
            if name != "grant_portal_access"
        }
        monkeypatch.setattr(callables_module, "_BINDINGS", thinned)
        with pytest.raises(ToolBindingError, match="grant_portal_access"):
            callables_module._check_bindings_cover_the_registry()


def check_routes_through_executor(
    build: Callable[[MockExternalExecutor], ToolSet], tool_name: str
) -> None:
    """Assert that invoking ``tool_name`` on the built toolset reaches the executor exactly once.

    The whole vacuity argument rests on this being one function used twice — once where it must
    pass and once where it must fail. Two similar-looking checks would not prove anything about
    either.
    """
    executor = MockExternalExecutor()
    build(executor).call(tool_name, EFFECT_CALLS[tool_name])
    assert executor.invocation_count == 1, f"{tool_name} produced no effect through the executor"


class ForgingToolSet(ToolSet):
    """A toolset whose ``send_email`` produces its result without going near the executor.

    This is exactly the mistake the check exists to catch: a body that looks like it works, returns
    a well-formed typed result, and leaves no trace in the ledger.
    """

    def send_email(self, to: str, subject: str, body: str) -> ExecutionResult:
        return ExecutionResult(
            outcome=ExecutionOutcome.EXECUTED,
            idempotency_key="forged",
            action_type="send_email",
            result=EmailSent(message_id="msg-forged", to=to, subject=subject),
        )


class TestNoEffectWithoutTheExecutor:
    """The vacuity test, asserted in both directions."""

    @pytest.mark.parametrize("tool_name", sorted(EFFECT_CALLS))
    def test_every_external_effect_tool_routes_through_the_executor(self, tool_name: str) -> None:
        check_routes_through_executor(make_tools, tool_name)

    def test_the_check_fails_for_a_tool_that_bypasses_the_executor(self) -> None:
        """Same function, same tool, a body that forges its result — must fail."""
        with pytest.raises(AssertionError, match="send_email"):
            check_routes_through_executor(
                lambda executor: ForgingToolSet(CASE, executor=executor), "send_email"
            )

    def test_the_forging_toolset_really_does_produce_a_plausible_result(self) -> None:
        """Guards the test above from passing because the forged body simply crashed."""
        forged = ForgingToolSet(CASE, executor=MockExternalExecutor()).call(
            "send_email", EFFECT_CALLS["send_email"]
        )
        assert isinstance(forged, ExecutionResult)
        assert isinstance(forged.result, EmailSent)

    @pytest.mark.parametrize("tool_name", sorted(EFFECT_CALLS))
    def test_a_body_cannot_produce_its_result_without_a_handler(self, tool_name: str) -> None:
        """Delegation, not decoration.

        A body that built its own result and merely *also* told the executor would still pass the
        ledger count. Removing the handler proves the executor is the source of the result rather
        than a bystander to it.
        """
        blind = ToolSet(CASE, executor=MockExternalExecutor(handlers={}))
        with pytest.raises(UnknownActionType):
            blind.call(tool_name, EFFECT_CALLS[tool_name])


def imported_modules(path: Path) -> set[str]:
    """Every module name ``path`` imports, read from its syntax tree."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def imports_matching(path: Path, banned: tuple[str, ...]) -> set[str]:
    """The imports of ``path`` that are, or sit under, one of ``banned``."""
    return {
        imported
        for imported in imported_modules(path)
        for prefix in banned
        if imported == prefix or imported.startswith(f"{prefix}.")
    }


def tool_modules() -> list[Path]:
    return sorted(Path(callables_module.__file__).parent.glob("*.py"))


class TestToolsStayOutOfTheRestOfTheSystem:
    """Tools do not mutate case state, evaluate policy, or touch a model.

    Enforced on the import graph rather than by convention: a tool that cannot import the state
    layer cannot transition a case, and a tool that cannot import the policy layer cannot decide
    its own authority.
    """

    FORBIDDEN = ("careloop.state", "careloop.policy", "strands")

    def test_the_package_has_the_modules_this_test_expects(self) -> None:
        """Otherwise the sweep below runs over nothing."""
        assert {path.name for path in tool_modules()} >= {
            "callables.py",
            "documents.py",
            "executor.py",
            "idempotency.py",
            "registry.py",
            "results.py",
        }

    def test_no_tool_module_imports_state_policy_or_a_model(self) -> None:
        offenders = {
            path.name: sorted(found)
            for path in tool_modules()
            if (found := imports_matching(path, self.FORBIDDEN))
        }
        assert offenders == {}

    def test_the_detector_finds_a_forbidden_import_where_one_exists(self) -> None:
        """Vacuity: ``policy/engine.py`` imports the state layer, so the sweep must flag it."""
        engine = Path(callables_module.__file__).parent.parent / "policy" / "engine.py"
        assert "careloop.state.case" in imports_matching(engine, self.FORBIDDEN)

    def test_the_detector_does_not_flag_an_unrelated_import(self) -> None:
        """A prefix match on ``careloop.state`` must not catch, say, ``careloop.statistics``."""
        assert imports_matching(Path(callables_module.__file__), self.FORBIDDEN) == set()
        assert "careloop.provenance.canonical" in imported_modules(
            Path(callables_module.__file__).parent / "idempotency.py"
        )


class TestNoClockAndNoRandomness:
    """Nothing in ``careloop/tools/`` may read the time or a random source.

    Byte-identical replay is what makes T3 an assertion rather than an approximation, and it is
    exactly the property that dies quietly the first time somebody adds a ``sent_at`` for
    convenience. Asserting it by comparing two results is not enough — on a platform whose clock
    ticks every 15ms, two results built microseconds apart compare equal and the test passes while
    the property is gone. So it is enforced on the source instead.
    """

    FORBIDDEN = ("datetime", "time", "random", "secrets", "uuid")

    def test_no_tool_module_imports_a_clock_or_a_random_source(self) -> None:
        offenders = {
            path.name: sorted(found)
            for path in tool_modules()
            if (found := imports_matching(path, self.FORBIDDEN))
        }
        assert offenders == {}

    def test_the_detector_finds_a_clock_where_one_exists(self) -> None:
        """Vacuity: ``state/case.py`` stamps times, so the sweep must flag it."""
        case = Path(callables_module.__file__).parent.parent / "state" / "case.py"
        assert imports_matching(case, self.FORBIDDEN) == {"datetime"}

    def test_no_result_field_is_filled_in_from_ambient_state(self) -> None:
        """A default or a factory is where a timestamp or a random id would enter.

        Every field of every result is required, so a result can only say what its caller told
        it — there is nowhere for the current time to arrive from.
        """
        subclasses = _all_subclasses(ToolResult)
        assert len(subclasses) >= 6

        offenders = {
            f"{model.__name__}.{name}"
            for model in subclasses
            for name, field in model.model_fields.items()
            if field.default_factory is not None or not field.is_required()
        }
        assert offenders == set()

    def test_the_field_check_would_notice_a_default_factory(self) -> None:
        """Vacuity: a synthetic result with a clock must be caught by the same rule."""
        from datetime import UTC, datetime

        from pydantic import Field

        class Stamped(ToolResult):
            at: datetime = Field(default_factory=lambda: datetime.now(UTC))

        offenders = {
            name
            for name, field in Stamped.model_fields.items()
            if field.default_factory is not None or not field.is_required()
        }
        assert offenders == {"at"}


def _all_subclasses(root: type) -> set[type]:
    found = set(root.__subclasses__())
    return found | {nested for child in found for nested in _all_subclasses(child)}
