"""The tool bodies.

Five callables, one per :class:`~careloop.tools.registry.ToolSpec`. Four produce an external effect
and reach it only through :class:`~careloop.tools.executor.MockExternalExecutor`; the fifth,
``fetch_document``, reads from the document store and produces no effect at all.

**Bodies bind to the registry; they do not live in it.** ``ToolSpec`` is a frozen, serializable
declaration, and hanging a callable off it would make it neither — and would give argument classes
a second declaration site, which is the one thing ``registry.py`` exists to prevent. Instead each
method carries an :func:`implements` decorator that checks its signature against the registry **at
import time**. A body whose parameters disagree with its declaration fails on import, not in a
test, and not in front of a judge.

**What a tool is not.** A tool does not mutate case state, evaluate policy, append to the timeline,
or touch a model. It is an effect-producer invoked by a graph node; every state change belongs to
:func:`careloop.state.machine.transition`, and every authority decision to the policy engine. The
separation is checked structurally: nothing in ``careloop/tools/`` imports ``careloop.state``,
``careloop.policy``, or ``strands``.

**``case_id`` is a constructor argument, never a tool parameter.** It scopes the idempotency key,
so a model that could supply it could scope two identical actions into two distinct effects — or
collapse two cases' actions into one. It is bound when the toolset is built, out of the model's
reach.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from inspect import Parameter, signature
from typing import Final, TypeVar

from careloop.tools.documents import DocumentStore, default_document_store
from careloop.tools.executor import ExecutionResult, MockExternalExecutor
from careloop.tools.idempotency import idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import Document, ToolResult


class ToolBindingError(Exception):
    """Raised at import time when a tool body and its registry declaration disagree."""


class UnknownTool(Exception):
    """Raised when a tool name has no bound body."""


#: Tool name to the name of the :class:`ToolSet` method implementing it. Populated by
#: :func:`implements` as the class body executes.
_BINDINGS: Final[dict[str, str]] = {}

_F = TypeVar("_F", bound=Callable[..., ToolResult])


def implements(tool_name: str) -> Callable[[_F], _F]:
    """Bind a method to a registered tool, checking its signature against the declaration.

    Raises:
        ToolBindingError: if the tool is not registered, is already bound, or if the method's
            parameters are not exactly the arguments the registry declares. Parameter *order* is
            not checked — arguments arrive by name — but the set must match exactly, so an
            argument the registry classifies cannot be quietly dropped from the body, and a body
            cannot accept one the registry has never classified.
    """

    def bind(method: _F) -> _F:
        spec = REGISTRY.get(tool_name)
        if spec is None:
            raise ToolBindingError(f"{method.__name__} implements unregistered tool {tool_name!r}")
        parameters = [
            name for name, parameter in signature(method).parameters.items() if name != "self"
        ]
        if frozenset(parameters) != spec.arg_names:
            raise ToolBindingError(
                f"{method.__name__} takes {sorted(parameters)} but the registry declares "
                f"{sorted(spec.arg_names)} for tool {tool_name!r}"
            )
        for name, parameter in signature(method).parameters.items():
            if name != "self" and parameter.kind is not Parameter.POSITIONAL_OR_KEYWORD:
                raise ToolBindingError(
                    f"{method.__name__} declares {name!r} as {parameter.kind}; tool arguments "
                    f"must be plain named parameters so a call can be made by name"
                )

        # Last, so that a body which is wrong in more than one way reports the more useful of the
        # two: a signature that contradicts the registry is the failure worth naming.
        if tool_name in _BINDINGS:
            raise ToolBindingError(f"tool {tool_name!r} is already implemented")

        _BINDINGS[tool_name] = method.__name__
        return method

    return bind


class ToolSet:
    """The five tools, bound to one case, one executor and one document store.

    Constructed per case. Holds exactly two collaborators, and neither of them can reach a case, a
    timeline or a model — which is what makes the separation above a property of the object graph
    rather than a promise about the code.
    """

    def __init__(
        self,
        case_id: str,
        executor: MockExternalExecutor | None = None,
        documents: DocumentStore | None = None,
    ) -> None:
        self._case_id = case_id
        self._executor = executor if executor is not None else MockExternalExecutor()
        self._documents = documents if documents is not None else default_document_store()

    @property
    def case_id(self) -> str:
        """The case every effect from this toolset is keyed against."""
        return self._case_id

    @property
    def executor(self) -> MockExternalExecutor:
        """The one route to an external effect."""
        return self._executor

    @property
    def documents(self) -> DocumentStore:
        """The read-only document store behind ``fetch_document``."""
        return self._documents

    # -- external effects ---------------------------------------------------------------------

    @implements("send_email")
    def send_email(self, to: str, subject: str, body: str) -> ExecutionResult:
        """Send an email on the caregiver's behalf."""
        return self._effect("send_email", {"to": to, "subject": subject, "body": body})

    @implements("book_appointment")
    def book_appointment(
        self, provider_id: str, patient_id: str, slot_start: str, reason: str
    ) -> ExecutionResult:
        """Book a slot with a provider."""
        return self._effect(
            "book_appointment",
            {
                "provider_id": provider_id,
                "patient_id": patient_id,
                "slot_start": slot_start,
                "reason": reason,
            },
        )

    @implements("upload_document")
    def upload_document(self, portal_id: str, document_id: str) -> ExecutionResult:
        """Upload a document to a provider portal."""
        return self._effect("upload_document", {"portal_id": portal_id, "document_id": document_id})

    @implements("grant_portal_access")
    def grant_portal_access(self, grantee: str, role: str, portal_id: str) -> ExecutionResult:
        """Grant a person access to a provider portal."""
        return self._effect(
            "grant_portal_access", {"grantee": grantee, "role": role, "portal_id": portal_id}
        )

    # -- reads --------------------------------------------------------------------------------

    @implements("fetch_document")
    def fetch_document(self, document_id: str) -> Document:
        """Read a document CareLoop already holds.

        No executor, no idempotency key, no ledger entry. Reading twice is not a duplicated
        effect, and recording it as one would make the ledger mean something weaker than
        "these things happened in the world".
        """
        return self._documents.get(document_id)

    # -- dispatch -----------------------------------------------------------------------------

    def call(self, tool_name: str, args: Mapping[str, object]) -> ToolResult:
        """Invoke a tool by name.

        The route a graph node takes, and the route that makes "every external-effect tool goes
        through the executor" checkable by enumeration rather than by reading four call sites.

        Raises:
            UnknownTool: if no body is bound to ``tool_name``.
            TypeError: if ``args`` does not match the tool's declared arguments.
        """
        method_name = _BINDINGS.get(tool_name)
        if method_name is None:
            raise UnknownTool(f"no tool named {tool_name!r}")
        return getattr(self, method_name)(**args)

    def _effect(self, action_type: str, args: Mapping[str, object]) -> ExecutionResult:
        """Derive the key and hand the call to the executor. The only path to an effect."""
        return self._executor.invoke(
            action_type, args, idempotency_key(self._case_id, action_type, args)
        )


def bound_tools() -> frozenset[str]:
    """Every tool name with a bound body."""
    return frozenset(_BINDINGS)


def _check_bindings_cover_the_registry() -> None:
    """Fail at import if a declared tool has no body, or a body no declaration."""
    declared = REGISTRY.names
    bound = frozenset(_BINDINGS)
    if bound != declared:
        raise ToolBindingError(
            f"tool bodies disagree with the registry: "
            f"declared but unimplemented {sorted(declared - bound)}, "
            f"implemented but undeclared {sorted(bound - declared)}"
        )


_check_bindings_cover_the_registry()


__all__ = [
    "ToolBindingError",
    "ToolSet",
    "UnknownTool",
    "bound_tools",
    "implements",
]
