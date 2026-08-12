"""The backstop — a policy check bound to the tool body itself.

Invariant 2 asks for enforcement at two independent points: the ``BeforeToolCall`` hook, and a
``@gated_tool`` decorator on every external-effect tool that holds **even if the hook is never
registered**. This module is the second one.

**Why it is not in** ``careloop/tools/``. Commit 4 established, and ``tests/test_tool_callables.py``
enforces by source sweep, that no module under ``careloop/tools/`` may import ``careloop.state``,
``careloop.policy`` or ``strands``. That sweep is load-bearing: a tool that cannot import the
policy layer cannot decide its own authority. A decorator living beside the tool bodies would have
to break it. So the decorator lives here, and :class:`GatedToolSet` applies it by **enumerating the
registry** — which is better than hand-decorating four methods anyway, because a fifth effectful
tool is wrapped without anyone remembering to, and ``tests/test_policy_gated.py`` fails if the
enumeration ever misses one.

**Every tool is wrapped, not only the effectful ones.** Invariant 2 sets the floor at
external-effect tools; going wider costs nothing and buys a claim that survives being read
carefully. Deny-by-default already applies to every tool call — ``allow-document-fetch`` exists in
``rules.yaml`` precisely because the read path would otherwise be denied — so "the hook and the
backstop enforce over the same set" is true rather than nearly true, and "removing the hook changes
nothing about what is enforced" needs no asterisk.

**One decision, not two.** Both layers ask the same :class:`~careloop.policy.gate.PolicyGate`, and
the gate caches on a key that identifies the case version, so layering costs one evaluation of the
engine rather than two. Where the cache would be unsafe it declines to cache; it never declines to
decide.

**Refusal is an exception, not a return value.** :exc:`~careloop.policy.gate.PolicyDenied` carries
the whole :class:`~careloop.policy.verdicts.Decision`, which names rule ids and holds raw argument
values, and it is for the timeline. What a planner is told is
``decision.model_visible_reason`` — a literal from the closed reason-code vocabulary — and the
graph node that catches this is the only thing that decides to say even that.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any, Final

from careloop.policy.gate import PolicyGate
from careloop.tools.callables import ToolSet, UnknownTool, bound_method_name
from careloop.tools.registry import REGISTRY, ToolRegistry
from careloop.tools.results import ToolResult

#: Attribute stamped onto a wrapped body, naming the tool it gates.
#:
#: Present so coverage is checkable by inspecting the object rather than by trusting the
#: constructor: a test can ask every callable on a gated toolset which tool it guards, and an
#: unwrapped body has no answer.
GATED_MARKER: Final[str] = "__careloop_gated_tool__"


class ReservedToolName(Exception):
    """Raised when a tool's method name would shadow a :class:`GatedToolSet` member."""


def reserved_method_names() -> frozenset[str]:
    """Names on :class:`GatedToolSet` that a gated body must not overwrite.

    Read off the class rather than listed, so a member added later is covered without anyone
    remembering to add it here.
    """
    return frozenset(name for name in vars(GatedToolSet) if not name.startswith("_"))


def check_no_reserved_collision(registry: ToolRegistry = REGISTRY) -> None:
    """Fail if a tool's bound method name collides with a :class:`GatedToolSet` member.

    A real hazard rather than a theoretical one. Bodies are attached by ``setattr`` under the name
    the registry gives them, so a tool called ``call`` would replace the dispatch method itself and
    a tool called ``executor`` would replace the accessor the tests count effects through. Either
    would break quietly — a shadowed ``call`` still *looks* callable.

    The earlier version of this guard compared the gated set against ``registry.names``, which is
    the very set the enumeration iterates. It could not fail, and deleting it left the suite green:
    a guard that cannot fire is decoration. This one takes a registry and inspects names only, so
    a synthetic registry can turn it red.

    Raises:
        ReservedToolName: if any declared tool would shadow a member.
    """
    reserved = reserved_method_names()
    colliding = sorted(name for name in registry.names if name in reserved)
    if colliding:
        raise ReservedToolName(
            f"tool name(s) {', '.join(colliding)} would shadow GatedToolSet member(s) of the same "
            f"name; a shadowed dispatch method still looks callable and would fail silently"
        )


def gated_tool(
    body: Callable[..., ToolResult],
    *,
    gate: PolicyGate,
    tool_name: str,
) -> Callable[..., ToolResult]:
    """Wrap ``body`` so the policy engine decides before it runs.

    Keyword arguments only, matching :meth:`careloop.tools.callables.ToolSet.call`, which binds by
    name against the registry's exact argument set.

    Raises:
        PolicyDenied: from :meth:`~careloop.policy.gate.PolicyGate.check`, before ``body`` is
            reached. The refusal happens *before* the call, not after: a tool that has already run
            cannot be un-run, and the ledger would carry an effect the policy engine refused.
    """

    @functools.wraps(body)
    def call(**args: Any) -> ToolResult:
        gate.check(tool_name, args)
        return body(**args)

    setattr(call, GATED_MARKER, tool_name)
    return call


class GatedToolSet:
    """A :class:`~careloop.tools.callables.ToolSet` with every body behind the policy gate.

    A drop-in for ``ToolSet``: the same method names, the same ``call`` dispatch, the same typed
    results. What differs is that nothing gets through without a verdict.
    """

    def __init__(
        self,
        toolset: ToolSet,
        gate: PolicyGate,
        registry: ToolRegistry = REGISTRY,
    ) -> None:
        check_no_reserved_collision(registry)

        self._toolset = toolset
        self._gate = gate
        self._registry = registry
        self._gated: dict[str, Callable[..., ToolResult]] = {}

        # Enumeration over the registry, not a hand-written list. Coverage is then asserted from
        # the outside — ``gated_tools`` against ``REGISTRY.names``, and the marker on every bound
        # body — because a check comparing the result against the set it was built from could
        # never fail.
        for tool_name in sorted(registry.names):
            body = getattr(toolset, bound_method_name(tool_name))
            wrapped = gated_tool(body, gate=gate, tool_name=tool_name)
            self._gated[tool_name] = wrapped
            setattr(self, bound_method_name(tool_name), wrapped)

    @property
    def case_id(self) -> str:
        """The case every effect from the wrapped toolset is keyed against."""
        return self._toolset.case_id

    @property
    def executor(self) -> Any:
        """The wrapped toolset's one route to an external effect."""
        return self._toolset.executor

    @property
    def documents(self) -> Any:
        """The wrapped toolset's read-only document store."""
        return self._toolset.documents

    @property
    def gate(self) -> PolicyGate:
        """The gate every wrapped body consults. Shared with the hook."""
        return self._gate

    @property
    def gated_tools(self) -> frozenset[str]:
        """Every tool name this toolset gates."""
        return frozenset(self._gated)

    def call(self, tool_name: str, args: Mapping[str, object]) -> ToolResult:
        """Invoke a tool by name, through its gate.

        Raises:
            UnknownTool: if no body is bound to ``tool_name``. Deny-by-default reaches here too:
                an unknown name is not silently ignored.
            PolicyDenied: if the engine refuses the call.
            TypeError: if ``args`` does not match the tool's declared arguments.
        """
        gated = self._gated.get(tool_name)
        if gated is None:
            raise UnknownTool(f"no gated tool named {tool_name!r}")
        return gated(**args)


#: Checked at import over the shipped registry, the same way ``careloop/tools/executor.py`` and
#: ``careloop/tools/callables.py`` check their own drift. A tool added under a name that shadows a
#: dispatcher member fails here rather than at the first call that quietly does nothing.
#:
#: Honest note on what is and is not proven: this call and the one in ``__init__`` are unreachable
#: while no such tool exists, so deleting either leaves the suite green. What the tests do prove is
#: the function itself, against synthetic registries — see ``TestNoToolMayShadowTheDispatcher``.
check_no_reserved_collision(REGISTRY)


def gates_of(toolset: GatedToolSet) -> dict[str, str | None]:
    """Which tool each of ``toolset``'s bodies declares it guards.

    Reads :data:`GATED_MARKER` off every bound body, so an unwrapped one reports ``None``. This is
    the both-directions vacuity check: run it over the shipped gated toolset and every entry is
    populated; run it over a plain ``ToolSet`` and every entry is ``None``.
    """
    found: dict[str, str | None] = {}
    for tool_name in sorted(REGISTRY.names):
        try:
            body = getattr(toolset, bound_method_name(tool_name), None)
        except UnknownTool:  # pragma: no cover - the registry and the bindings agree at import
            body = None
        found[tool_name] = getattr(body, GATED_MARKER, None) if body is not None else None
    return found


__all__ = [
    "GATED_MARKER",
    "GatedToolSet",
    "ReservedToolName",
    "check_no_reserved_collision",
    "gated_tool",
    "gates_of",
    "reserved_method_names",
]
