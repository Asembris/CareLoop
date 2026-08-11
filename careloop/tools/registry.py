"""The tool registry.

The single source of truth for tool metadata: what tools exist, what arguments they take, what
each argument *controls* (:class:`~careloop.provenance.trust.ArgClass`), and whether invoking the
tool has an effect outside CareLoop.

Argument classes live here and nowhere else. ``rules.yaml`` may *reference* a class as an
assertion, which load-time validation checks against this registry; it may never *define* one. Two
declaration sites means one place to forget a ``DESTINATION``, and a forgotten ``DESTINATION`` is
the whole attack.

This module declares metadata only. Tool bodies and the executor arrive separately; nothing here
imports or invokes a callable, so the policy layer can be evaluated without any tool being
implementable yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from careloop.provenance.trust import ArgClass


class DuplicateToolName(Exception):
    """Raised when a registry is built with the same tool name twice."""


class ArgSpec(BaseModel):
    """One declared argument of one tool."""

    model_config = ConfigDict(frozen=True)

    name: str
    arg_class: ArgClass
    description: str = ""


class ToolSpec(BaseModel):
    """One declared tool.

    ``external_effect`` marks a tool whose invocation is visible outside CareLoop — an email sent,
    a booking made, a document uploaded, an access grant issued. It drives the policy coverage
    check: every externally effective tool must be named by at least one rule, so adding one
    without writing policy fails the suite rather than shipping an ungoverned effect.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    args: tuple[ArgSpec, ...] = Field(min_length=1)
    external_effect: bool

    @model_validator(mode="after")
    def _reject_duplicate_arg_names(self) -> ToolSpec:
        seen: set[str] = set()
        for arg in self.args:
            if arg.name in seen:
                raise ValueError(f"tool {self.name!r} declares argument {arg.name!r} twice")
            seen.add(arg.name)
        return self

    @property
    def arg_names(self) -> frozenset[str]:
        """Every argument name this tool declares."""
        return frozenset(arg.name for arg in self.args)

    def arg_class(self, name: str) -> ArgClass | None:
        """Return the class of argument ``name``, or ``None`` if the tool does not declare it."""
        for arg in self.args:
            if arg.name == name:
                return arg.arg_class
        return None

    def args_of_class(self, arg_class: ArgClass) -> frozenset[str]:
        """Every argument of this tool belonging to ``arg_class``."""
        return frozenset(arg.name for arg in self.args if arg.arg_class is arg_class)


class ToolRegistry:
    """An immutable collection of :class:`ToolSpec`, keyed by tool name."""

    def __init__(self, tools: Iterable[ToolSpec]) -> None:
        by_name: dict[str, ToolSpec] = {}
        for tool in tools:
            if tool.name in by_name:
                raise DuplicateToolName(f"tool {tool.name!r} is declared twice")
            by_name[tool.name] = tool
        self._by_name: Final[dict[str, ToolSpec]] = by_name

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def get(self, name: str) -> ToolSpec | None:
        """Return the spec for ``name``, or ``None`` if the tool is not registered."""
        return self._by_name.get(name)

    @property
    def names(self) -> frozenset[str]:
        """Every registered tool name."""
        return frozenset(self._by_name)

    @property
    def external_effect_tools(self) -> frozenset[str]:
        """Names of every tool whose invocation is visible outside CareLoop."""
        return frozenset(name for name, tool in self._by_name.items() if tool.external_effect)

    def arg_class(self, tool_name: str, arg_name: str) -> ArgClass | None:
        """Return the class of ``arg_name`` on ``tool_name``, or ``None`` if either is unknown."""
        tool = self.get(tool_name)
        return None if tool is None else tool.arg_class(arg_name)


#: The tools CareLoop declares.
#:
#: ``grant_portal_access.role`` is the system's only ``AUTHORITY`` argument. It exists so that the
#: third leg of the trust model is exercised by a real rule rather than declared and forgotten.
REGISTRY: Final[ToolRegistry] = ToolRegistry(
    [
        ToolSpec(
            name="send_email",
            description="Send an email on the caregiver's behalf.",
            external_effect=True,
            args=(
                ArgSpec(name="to", arg_class=ArgClass.DESTINATION, description="Recipient."),
                ArgSpec(name="subject", arg_class=ArgClass.VALUE),
                ArgSpec(name="body", arg_class=ArgClass.VALUE),
            ),
        ),
        ToolSpec(
            name="book_appointment",
            description="Book a slot with a provider.",
            external_effect=True,
            args=(
                ArgSpec(
                    name="provider_id",
                    arg_class=ArgClass.DESTINATION,
                    description="Which provider receives the booking.",
                ),
                ArgSpec(name="patient_id", arg_class=ArgClass.VALUE),
                ArgSpec(name="slot_start", arg_class=ArgClass.VALUE),
                ArgSpec(name="reason", arg_class=ArgClass.VALUE),
            ),
        ),
        ToolSpec(
            name="upload_document",
            description="Upload a document to a provider portal.",
            external_effect=True,
            args=(
                ArgSpec(
                    name="portal_id",
                    arg_class=ArgClass.DESTINATION,
                    description="Which portal receives the document.",
                ),
                ArgSpec(name="document_id", arg_class=ArgClass.VALUE),
            ),
        ),
        ToolSpec(
            name="grant_portal_access",
            description="Grant a person access to a provider portal.",
            external_effect=True,
            args=(
                ArgSpec(
                    name="grantee",
                    arg_class=ArgClass.DESTINATION,
                    description="Who receives the access.",
                ),
                ArgSpec(
                    name="role",
                    arg_class=ArgClass.AUTHORITY,
                    description="What powers the grant confers.",
                ),
                ArgSpec(name="portal_id", arg_class=ArgClass.DESTINATION),
            ),
        ),
        ToolSpec(
            name="fetch_document",
            description="Read a document already held by CareLoop.",
            external_effect=False,
            args=(ArgSpec(name="document_id", arg_class=ArgClass.VALUE),),
        ),
    ]
)


__all__ = ["REGISTRY", "ArgSpec", "DuplicateToolName", "ToolRegistry", "ToolSpec"]
