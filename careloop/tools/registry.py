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

**Two further declarations live here, and both are about an argument rather than about a tool.**

:attr:`ArgSpec.author` says *who composes the value*. ``MODEL`` — the default, and today's whole
registry — means a planner supplies it. ``SYSTEM`` means CareLoop composes it from
:data:`AUTHORED_VALUES` and the model is never asked for it at all. Authorship is **not** trust:
a ``SYSTEM``-authored value mints no :class:`~careloop.provenance.facts.Fact`, carries no
``trust_key``, and moves no trust level, exactly as a caregiver's approval does not. It answers a
different question — *did a model write this string* — and it is answered before the policy engine
is reached.

:attr:`ArgSpec.identifies` says whether the argument is part of *which logical action this is*.
``True`` is the default and is today's whole registry, so the derivation is unchanged until an
argument opts out. Excluding one is a claim that two calls differing only in that argument are the
same external action, which is a statement about the provider's semantics and belongs beside the
argument rather than in the function that hashes it.

The four questions this module and its readers keep apart:

``AUTHORITY``
    Which values permit the action? — :attr:`ArgSpec.arg_class`, :attr:`ArgSpec.trust_key`, and the
    rules that read them.
``PAYLOAD``
    Which values are transmitted externally? — :attr:`ToolSpec.arg_names`, in full.
``IDENTITY``
    Which values define the same logical action? — :attr:`ToolSpec.identifying_args`.
``VERIFICATION``
    Which values must independently match afterwards? — ``careloop.graph.verify``.

They are deliberately four sets and not one. A field may legitimately be transmitted while being
the wrong thing to key an action on, and dropping a field from identity must never also drop it
from scrutiny — which is why ``careloop.graph.verify`` refuses at import if a non-identifying
argument is one no verifier compares.

This module declares metadata only. Tool bodies and the executor arrive separately; nothing here
imports or invokes a callable, so the policy layer can be evaluated without any tool being
implementable yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from careloop.provenance.trust import ArgClass


class DuplicateToolName(Exception):
    """Raised when a registry is built with the same tool name twice."""


class UndeclaredAuthorship(Exception):
    """Raised at import when authorship metadata and :data:`AUTHORED_VALUES` disagree.

    Three disagreements, each silent on its own: a ``SYSTEM``-authored argument with no declared
    value, a declared value for an argument no tool declares ``SYSTEM``, and a ``SYSTEM``-authored
    ``DESTINATION`` or ``AUTHORITY``. The third is the dangerous one — CareLoop composing where an
    effect lands, or what powers it confers, would route around
    :data:`careloop.graph.trusted.TRUSTED_CONFIG` entirely.
    """


class UnidentifiableAction(Exception):
    """Raised at import when a tool's declared identity is not usable as one.

    An empty identifying set would key every call of that tool alike, collapsing genuinely distinct
    actions into one suppressed duplicate. A non-identifying ``DESTINATION`` or ``AUTHORITY`` is the
    same defect stated about the argument that matters most: where an effect lands is definitionally
    part of *which action it is*, and two calls differing only in their destination are never the
    same logical action.
    """


class ArgAuthor(StrEnum):
    """Who composes an argument's value.

    **Not a trust level and not a permission.** It answers *did a model write this string*, which
    is a question about provenance of authorship rather than about authority, and it is settled
    before the policy engine is reached. A ``SYSTEM``-authored value mints no
    :class:`~careloop.provenance.facts.Fact` and moves no trust level, exactly as an
    :class:`~careloop.state.case.Approval` does not.
    """

    MODEL = "MODEL"
    """A planner supplies the value. The default, and the only thing that existed before this."""

    SYSTEM = "SYSTEM"
    """CareLoop composes the value from :data:`AUTHORED_VALUES`; no model is asked for it.

    The argument is omitted from every model-facing proposal schema, so the value is not merely
    ignored when a model supplies one — it cannot be expressed. That is the narrowest place to
    enforce it, and it is the same principle
    :mod:`careloop.agents.schemas` already states about the intent as a whole: a field that cannot
    be expressed cannot be decided.
    """


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

    author: ArgAuthor = ArgAuthor.MODEL
    """Who composes this argument's value.

    ``MODEL`` — the default — is what every argument was before this field existed, so a newly
    declared argument behaves exactly as one declared last year. Declaring ``SYSTEM`` is the
    deliberate act, and it requires an entry in :data:`AUTHORED_VALUES` or the module fails to
    import.
    """

    identifies: bool = True
    """Whether this argument is part of *which logical action* a call is.

    ``True`` — the default — is what every argument was before this field existed, so
    :func:`careloop.tools.idempotency.idempotency_key` derives exactly what it always did until an
    argument opts out. Opting out asserts that two calls differing only here are the **same**
    external action, which is a claim about the provider's semantics.

    It is emphatically not a claim that the argument matters less. A non-identifying argument is
    still transmitted, still covered by
    :func:`careloop.tools.idempotency.args_digest`, and — enforced at import by
    ``careloop.graph.verify`` — still compared against the external system afterwards. Dropping a
    field from identity may never also drop it from scrutiny.
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

    @property
    def identifying_args(self) -> frozenset[str]:
        """Every argument of this tool that is part of which logical action a call is.

        **The IDENTITY set**, and the one
        :func:`careloop.tools.idempotency.idempotency_key` derives from. Deliberately a different
        question from :attr:`arg_names`, which is the PAYLOAD set — what CareLoop transmits — and
        from what :func:`careloop.tools.idempotency.args_digest` covers, which is all of them.
        """
        return frozenset(arg.name for arg in self.args if arg.identifies)

    @property
    def system_authored_args(self) -> frozenset[str]:
        """Every argument of this tool whose value CareLoop composes rather than a model."""
        return frozenset(arg.name for arg in self.args if arg.author is ArgAuthor.SYSTEM)

    @property
    def model_authored_args(self) -> frozenset[str]:
        """Every argument of this tool a model is asked for.

        What a model-facing proposal schema must expose, exactly. Read by
        :func:`careloop.agents.schemas.build_proposal_tool`.
        """
        return frozenset(arg.name for arg in self.args if arg.author is ArgAuthor.MODEL)

    def author(self, name: str) -> ArgAuthor | None:
        """Who composes ``name``, or ``None`` if the tool does not declare it."""
        for arg in self.args:
            if arg.name == name:
                return arg.author
        return None


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

    def identifying_args(self, tool_name: str) -> frozenset[str]:
        """Every identifying argument of ``tool_name``, or the empty set if it is unknown.

        An unknown tool answers empty, which is unreachable through
        :func:`careloop.tools.idempotency.idempotency_key` — that function refuses an unregistered
        action type outright rather than keying it on nothing.
        """
        tool = self.get(tool_name)
        return frozenset() if tool is None else tool.identifying_args

    def author(self, tool_name: str, arg_name: str) -> ArgAuthor | None:
        """Who composes ``arg_name`` on ``tool_name``, or ``None`` if either is unknown."""
        tool = self.get(tool_name)
        return None if tool is None else tool.author(arg_name)

    @property
    def system_authored(self) -> frozenset[tuple[str, str]]:
        """Every ``(tool name, argument name)`` whose value CareLoop composes."""
        return frozenset((tool.name, arg) for tool in self for arg in tool.system_authored_args)


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


#: The exact value CareLoop composes for each ``SYSTEM``-authored argument.
#:
#: **Fixed literals, never interpolated**, which is invariant 6's rule applied in the outbound
#: direction. A value assembled from case content would be a value a document could steer, and the
#: whole point of declaring an argument ``SYSTEM``-authored is that nothing outside this file
#: chooses what it says.
#:
#: Keyed by ``(tool name, argument name)`` rather than by argument name alone: two tools may
#: legitimately declare an argument of the same name, and a table keyed on the name would make one
#: tool's outbound text the other's.
#:
#: Empty in the commit that introduced it, on purpose. The metadata arrives with no shipped
#: argument using it, so nothing about the running system moves; declaring the first
#: ``SYSTEM``-authored argument is a separate, deliberate change.
AUTHORED_VALUES: Final[Mapping[tuple[str, str], str]] = {}


def check_authorship_is_declared(
    registry: ToolRegistry = REGISTRY,
    authored: Mapping[tuple[str, str], str] = AUTHORED_VALUES,
) -> None:
    """Fail at import if authorship metadata and the authored values disagree.

    Three disagreements, and the parameters exist so each can be provoked against a synthetic
    registry rather than only asserted about the shipped one.

    A ``SYSTEM``-authored argument with **no** declared value would leave the filling seam with
    nothing to supply, and whatever it substituted would be a value nobody wrote down.

    A declared value for an argument that is **not** ``SYSTEM``-authored is dead text pretending to
    be a guarantee — worse than untidy, because a reader would take it for one.

    A ``SYSTEM``-authored ``DESTINATION`` or ``AUTHORITY`` is the dangerous one. Where an effect
    lands and what powers it confers must come from
    :data:`careloop.graph.trusted.TRUSTED_CONFIG` and be required at ``TRUSTED`` by every rule that
    can authorise the tool; a value composed here would satisfy neither and would route around the
    entire destination model. ``VALUE`` is the only class that may be ``SYSTEM``-authored.

    Raises:
        UndeclaredAuthorship: naming the disagreement.
    """
    declared = registry.system_authored
    supplied = frozenset(authored)

    if missing := sorted(declared - supplied):
        raise UndeclaredAuthorship(
            f"SYSTEM-authored argument(s) {missing} have no declared value; an authored argument "
            f"whose text nobody wrote down would be filled with something nobody chose"
        )
    if orphaned := sorted(supplied - declared):
        raise UndeclaredAuthorship(
            f"authored value(s) declared for {orphaned}, which no tool declares SYSTEM-authored; "
            f"a value nothing composes is a guarantee that is not being made"
        )

    for tool in registry:
        for arg in tool.args:
            if arg.author is ArgAuthor.SYSTEM and arg.arg_class is not ArgClass.VALUE:
                raise UndeclaredAuthorship(
                    f"{tool.name}.{arg.name} is {arg.arg_class} and SYSTEM-authored; where an "
                    f"effect lands and what powers it confers must come from trusted config and be "
                    f"required at TRUSTED by rule, not composed by CareLoop"
                )


def check_identity_is_usable(registry: ToolRegistry = REGISTRY) -> None:
    """Fail at import if a tool's declared identity could not identify one of its calls.

    An **empty** identifying set would make every call of that tool key alike, so the second would
    be suppressed as a duplicate of the first — distinct actions collapsed into one, which is the
    fail-*open* direction :mod:`careloop.tools.idempotency` refuses an uncanonicalisable value for.

    A non-identifying ``DESTINATION`` or ``AUTHORITY`` is the same defect said about the arguments
    that matter most. Two calls differing only in where they land, or in what they grant, are never
    the same logical action; only ``VALUE`` may opt out, and only deliberately.

    Raises:
        UnidentifiableAction: naming the tool and, where relevant, the argument.
    """
    for tool in registry:
        if not tool.identifying_args:
            raise UnidentifiableAction(
                f"tool {tool.name!r} declares no identifying argument, so every call of it would "
                f"key alike and the second would be suppressed as a duplicate of the first"
            )
        for arg in tool.args:
            if not arg.identifies and arg.arg_class is not ArgClass.VALUE:
                raise UnidentifiableAction(
                    f"{tool.name}.{arg.name} is {arg.arg_class} and does not identify the action; "
                    f"two calls differing only in where an effect lands, or in what it grants, are "
                    f"not the same logical action"
                )


check_authorship_is_declared()
check_identity_is_usable()


__all__ = [
    "AUTHORED_VALUES",
    "REGISTRY",
    "ArgAuthor",
    "ArgSpec",
    "DuplicateToolName",
    "ToolRegistry",
    "ToolSpec",
    "UndeclaredAuthorship",
    "UnidentifiableAction",
    "check_authorship_is_declared",
    "check_identity_is_usable",
]
