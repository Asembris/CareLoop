"""The tool registry.

The single source of truth for tool metadata: what tools exist, what arguments they take, what
each argument *controls* (:class:`~careloop.provenance.trust.ArgClass`), **which fact key each
argument may draw trust from** (:attr:`ArgSpec.trust_key`), and whether invoking the tool has an
effect outside CareLoop.

Argument classes live here and nowhere else. ``rules.yaml`` may *reference* a class as an
assertion, which load-time validation checks against this registry; it may never *define* one. Two
declaration sites means one place to forget a ``DESTINATION``, and a forgotten ``DESTINATION`` is
the whole attack.

:attr:`ArgSpec.trust_key` lives here for the same reason and is the same kind of statement about
the same argument. It names the :attr:`careloop.provenance.facts.Fact.key` an argument's trust may
come from, and it is what binds trust to *meaning* rather than to a bare literal. Without it,
:func:`careloop.policy.engine.resolve_trust` answered "has this string ever been seen at this trust
level, under any key at all?" — so a validated ``patient_id`` satisfied a ``document_id``
requirement, and, worse, the configured ``portal_id`` satisfied the system's only ``AUTHORITY``
argument and defeated ``deny-authority-grant-below-trusted`` outright. Both were reproduced against
the shipped rules before this field existed; ``tests/test_typed_trust.py`` is where they are now
refused.

``None`` means **this argument draws no trust from evidence at all** and always resolves
``UNTRUSTED``. It is not "unclassified" and there is no fallback to matching by value: a rule that
asserts a trust predicate on such an argument fails at *load*, in
:func:`careloop.policy.loader._validate_against_registry`, rather than becoming a rule that can
never match.

Note that an argument's name and its trust key are deliberately allowed to differ, and two
arguments may legitimately share one key. ``send_email.to`` and ``grant_portal_access.grantee``
both draw on ``caregiver_email``, because the caregiver's own address is the one destination
trusted configuration supplies for either.

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

    trust_key: str | None = None
    """The :attr:`careloop.provenance.facts.Fact.key` this argument may draw its trust from.

    ``None`` — the default — means no fact can lend this argument trust, so it always resolves
    ``UNTRUSTED``. Defaulting to ``None`` is the fail-closed direction: a newly declared argument
    is untrusted until somebody says which fact key gives it meaning, and a rule that tries to
    require trust of it fails at load rather than silently never matching.
    """


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

    def trust_key(self, name: str) -> str | None:
        """The fact key argument ``name`` draws trust from, or ``None``.

        ``None`` for an argument that declares no key *and* for an argument this tool does not
        declare at all. The two collapse deliberately: both mean "nothing establishes trust for
        this", which is the answer that fails closed.
        """
        for arg in self.args:
            if arg.name == name:
                return arg.trust_key
        return None

    @property
    def trust_keys(self) -> frozenset[str]:
        """Every fact key any argument of this tool draws trust from."""
        return frozenset(arg.trust_key for arg in self.args if arg.trust_key is not None)

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

    def trust_key(self, tool_name: str, arg_name: str) -> str | None:
        """Return the fact key ``arg_name`` on ``tool_name`` draws trust from, or ``None``."""
        tool = self.get(tool_name)
        return None if tool is None else tool.trust_key(arg_name)

    @property
    def trust_keys(self) -> frozenset[str]:
        """Every fact key any argument of any registered tool draws trust from."""
        return frozenset(key for tool in self for key in tool.trust_keys)


#: The tools CareLoop declares.
#:
#: ``grant_portal_access.role`` is the system's only ``AUTHORITY`` argument. It exists so that the
#: third leg of the trust model is exercised by a real rule rather than declared and forgotten. Its
#: ``trust_key`` is ``"role"``, and **no producer anywhere in CareLoop emits a fact under that
#: key** — not :data:`careloop.graph.trusted.TRUSTED_CONFIG`, not extraction, not promotion. That
#: is the honest state of the system rather than an oversight: there is no legitimate trusted role,
#: so ``grant_portal_access`` can never reach ``ALLOW``, and the correct response to that is to
#: leave it denied rather than to invent a trusted role to restore reachability.
#: ``tests/test_typed_trust.py::TestTheGrantPathHasNoProductionAuthority`` pins it in both
#: directions.
#:
#: The ``VALUE``-class arguments split three ways, and the split is the point of the field. Two of
#: them (``slot_start``, ``document_id``) name promotable fact keys and are what the ``ALLOW`` rules
#: condition on. ``patient_id`` names its own key so it cannot be satisfied by an unrelated
#: literal. The rest — ``subject``, ``body``, ``reason`` — are free text that no fact describes,
#: and they declare no key at all: they resolve ``UNTRUSTED`` always, and no rule may require trust
#: of them.
REGISTRY: Final[ToolRegistry] = ToolRegistry(
    [
        ToolSpec(
            name="send_email",
            description="Send an email on the caregiver's behalf.",
            external_effect=True,
            args=(
                ArgSpec(
                    name="to",
                    arg_class=ArgClass.DESTINATION,
                    description="Recipient.",
                    trust_key="caregiver_email",
                ),
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
                    trust_key="provider_id",
                ),
                ArgSpec(name="patient_id", arg_class=ArgClass.VALUE, trust_key="patient_id"),
                ArgSpec(name="slot_start", arg_class=ArgClass.VALUE, trust_key="slot_start"),
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
                    trust_key="portal_id",
                ),
                ArgSpec(name="document_id", arg_class=ArgClass.VALUE, trust_key="document_id"),
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
                    trust_key="caregiver_email",
                ),
                ArgSpec(
                    name="role",
                    arg_class=ArgClass.AUTHORITY,
                    description="What powers the grant confers.",
                    trust_key="role",
                ),
                ArgSpec(name="portal_id", arg_class=ArgClass.DESTINATION, trust_key="portal_id"),
            ),
        ),
        ToolSpec(
            name="fetch_document",
            description="Read a document already held by CareLoop.",
            external_effect=False,
            args=(ArgSpec(name="document_id", arg_class=ArgClass.VALUE, trust_key="document_id"),),
        ),
    ]
)


__all__ = ["REGISTRY", "ArgSpec", "DuplicateToolName", "ToolRegistry", "ToolSpec"]
