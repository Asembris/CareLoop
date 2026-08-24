"""What the active configuration permits, stated once, derived from what enforces it.

**This module decides nothing.** It reads the loaded :class:`~careloop.policy.schema.RuleSet`, the
:class:`~careloop.tools.registry.ToolRegistry`, and one caregiver's trusted destinations, and
reports what those three already say. There is no rule here, no trust resolution, no approval
lookup and no verdict: :class:`~careloop.policy.engine.PolicyEngine` remains the only thing that
answers *may this call happen*, and ``tests/test_boundaries.py`` cross-checks every claim below
against it rather than against this file's own arithmetic.

It exists because the product had a second source of truth. ``frontend/src/data/trust.ts`` held the
standing statement of CareLoop's authority as hand-written literals, maintained by nobody, checkable
by nothing. A caregiver-facing page that asserts *"email may only reach an approved address"* while
``rules.yaml`` is edited underneath it is the failure mode invariant 2 exists to prevent, one layer
out. So the statement is computed from the enforcement, and a page that cannot read it says so
rather than falling back to prose.

**Scope is the external-effect tools and nothing else.** ``fetch_document`` reads a document
CareLoop already holds, has no effect outside the process, and is not a boundary a caregiver has an
interest in. Including it would put a row on the page that answers a question nobody asked.

Three derivations carry the weight, and each is deliberately conservative in a *stated* direction.

**Standing.** ``AUTONOMOUS`` requires an ``ALLOW`` rule that does not depend on an approval **and**
the absence of any ``REQUIRE_HUMAN`` rule that fires without one. The second half is what makes it
correct rather than optimistic: the engine's match order puts ``REQUIRE_HUMAN`` ahead of ``ALLOW``,
so a tool with both is human-gated however permissive its ``ALLOW`` rule reads.

**Required trust.** The level an argument must reach on *every* viable path, which is the strongest
statement that cannot overstate. It is the stronger of two floors, and both are needed:

``DENY`` floor
    A ``DENY`` rule naming exactly one argument and carrying no other clause fires whatever else is
    true, so its threshold is universal. A rule with a state clause, an approval clause, or more
    than one ``arg_trust_below`` predicate is **not** counted -- a multi-predicate
    ``arg_trust_below`` denies only when *every* named argument is below its level, which is a
    weaker statement about any one of them, and reading it as a per-argument floor would overstate.

``ALLOW`` floor
    The **weakest** requirement across the ``ALLOW`` rules that could authorise the tool, because
    any one of them matching is enough. If a single ``ALLOW`` rule does not name the argument at
    all, there is a viable path that does not constrain it and the floor is ``None`` -- CareLoop
    then says nothing about that argument rather than inventing a requirement. Session J is the
    reason this direction matters: a rule that names an argument without requiring its trust is not
    guarding it, and a page that claimed otherwise would be asserting a guard that is not there.

**Availability.** ``TRUSTED`` is reachable only through
:func:`careloop.graph.trusted.trusted_facts`, whose input is the caregiver's own ``destinations``
-- so an argument required at ``TRUSTED`` whose fact key that caregiver does not configure can never
be satisfied, by any document, any validator or any approval. That is what makes
``grant_portal_access`` report ``UNAVAILABLE`` here, and it reproduces from data a fact
``tests/test_typed_trust.py::TestTheGrantPathHasNoProductionAuthority`` proves independently.

**It is a statement about the current configuration, not about the product.** ``UNAVAILABLE`` says
*nothing this caregiver holds can authorise this*, and a different caregiver record or a different
trusted configuration would answer differently. The caregiver-facing copy is required to keep that
scope; see ``frontend/src/services/presentation.ts``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from careloop.graph.recovery import RECOVERY_CLASS, RecoveryClass
from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE
from careloop.policy.schema import Condition, RuleSet
from careloop.policy.verdicts import Verdict
from careloop.provenance.trust import PROMOTABLE_ARG_CLASSES, ArgClass, TrustLevel, at_least
from careloop.tools.registry import AUTHORED_VALUES, REGISTRY, ArgSpec, ToolRegistry


class CapabilityStanding(StrEnum):
    """Where one external-effect tool stands under the configuration being read."""

    AUTONOMOUS = "AUTONOMOUS"
    """CareLoop may do this without asking, once its conditions are established."""

    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    """A caregiver's decision is required for each call, and releases only that call."""

    UNAVAILABLE = "UNAVAILABLE"
    """Nothing this configuration holds can authorise it. See :class:`Unavailability`."""


class Unavailability(StrEnum):
    """Why a capability cannot currently be reached.

    Both members are statements about *this* configuration. Neither says the action is impossible in
    principle, and the caregiver-facing copy must not read as though it did.
    """

    NO_TRUSTED_SOURCE = "NO_TRUSTED_SOURCE"
    """An argument must be ``TRUSTED`` and this caregiver configures no fact under its key.

    Unreachable by any other route: ``TRUSTED`` is not promotable, an approval mints no fact, and
    document content can never supply it. See :mod:`careloop.graph.trusted`.
    """

    NO_AUTHORISING_RULE = "NO_AUTHORISING_RULE"
    """No ``ALLOW`` rule names the tool at all, so deny-by-default refuses every call."""

    NO_ROUTE_TO_A_DECISION = "NO_ROUTE_TO_A_DECISION"
    """Authorising it needs an approval, and nothing in the rules ever asks for one.

    An :class:`~careloop.state.case.Approval` exists only because a ``REQUIRE_HUMAN`` verdict raised
    one, so an ``ALLOW`` rule carrying ``args_approved: true`` with no ``REQUIRE_HUMAN`` rule in
    front of it authorises nothing -- every call falls through to deny-by-default. Unreachable in
    the shipped rules and found by the engine cross-check rather than by review: dropping
    ``require-human-for-outbound-email`` does not make email autonomous, it makes email impossible,
    and a page reporting *"CareLoop must ask you"* about it would be describing a conversation that
    never happens.
    """


class RefusalPrinciple(StrEnum):
    """A standing refusal the configuration actually implies.

    Each member is emitted only when the derivation finds it, so a ruleset that stopped enforcing
    one would stop claiming it. Deliberately coarse: this is the caregiver's summary, and the
    per-case timeline already carries the exact reason code and matched rule for anything that was
    really refused.
    """

    UNTRUSTED_DESTINATION = "UNTRUSTED_DESTINATION"
    """Where an effect lands must come from configuration, and validation cannot substitute."""

    UNEARNED_VALUE_TRUST = "UNEARNED_VALUE_TRUST"
    """A value a document asserted is not usable until a deterministic check has passed it."""

    DOCUMENT_AUTHORITY = "DOCUMENT_AUTHORITY"
    """What powers an action confers must come from configuration, never from content."""

    SECOND_RECORDED_EFFECT = "SECOND_RECORDED_EFFECT"
    """Every external effect declares an identity, so one action cannot be recorded twice."""


@dataclass(frozen=True)
class ArgumentBoundary:
    """One argument of one capability, as the boundary statement describes it."""

    name: str
    arg_class: ArgClass

    required_trust: TrustLevel | None
    """The level every viable path requires, or ``None`` when some path requires nothing.

    ``None`` is a real answer and not a gap. ``send_email.subject`` declares no ``trust_key``, no
    rule names it, and claiming a requirement for it would be the overstatement this whole module is
    written to avoid.
    """

    trust_key: str | None
    """The fact key this argument draws trust from -- :attr:`ArgSpec.trust_key`, verbatim."""

    configured_value: str | None
    """This caregiver's configured value for :attr:`trust_key`, when they have one.

    The caregiver's own destination, from CareLoop's own directory. Never a claim, never another
    caregiver's, and never a value a document supplied.
    """

    authored_value: str | None
    """The fixed literal CareLoop composes for this argument, when it composes one.

    Present exactly when :attr:`careloop.tools.registry.ArgSpec.author` is ``SYSTEM``, which is what
    lets the page say a field is not the model's to write.
    """

    identifies: bool
    """Whether this argument is part of which logical action a call is."""


@dataclass(frozen=True)
class Capability:
    """One external-effect tool, as the active configuration permits it."""

    tool_name: str
    standing: CapabilityStanding
    unavailable_because: Unavailability | None

    unmet_trust_keys: tuple[str, ...]
    """Fact keys an argument needs at ``TRUSTED`` that this caregiver does not configure."""

    independently_observed: bool
    """Whether completion re-reads the external system's own record.

    :data:`careloop.graph.verify.INDEPENDENTLY_OBSERVABLE`, read and never restated. Two of the four
    effects are simulated, so "verified" is a materially different claim between them and a page
    that could not tell them apart would let the weaker one read as the stronger.
    """

    recovery_class: RecoveryClass
    """What may be done when a dispatch's answer is lost.

    :data:`careloop.graph.recovery.RECOVERY_CLASS`, read and never restated.
    """

    arguments: tuple[ArgumentBoundary, ...]


@dataclass(frozen=True)
class Boundaries:
    """Everything the boundary statement says, for one caregiver, under one ruleset."""

    capabilities: tuple[Capability, ...]
    refusals: tuple[RefusalPrinciple, ...]


#: The trust an argument of each class must reach before it is considered authority-bearing.
#:
#: The same threshold :func:`careloop.policy.loader.uncovered_authority_arguments` enforces, read
#: from the class rather than restated per tool.
_AUTHORITY_TRUST: Final[Mapping[ArgClass, TrustLevel]] = {
    ArgClass.DESTINATION: TrustLevel.TRUSTED,
    ArgClass.AUTHORITY: TrustLevel.TRUSTED,
}


def _stronger(one: TrustLevel | None, other: TrustLevel | None) -> TrustLevel | None:
    """The higher of two levels, treating ``None`` as "says nothing"."""
    if one is None:
        return other
    if other is None:
        return one
    return one if at_least(one, other) else other


def _is_unconditional_about(condition: Condition, arg_name: str) -> bool:
    """Whether ``condition`` is a bare floor under exactly ``arg_name`` and nothing else.

    Every other clause must be empty. A ``DENY`` rule with a state clause fires only in some states;
    one with several ``arg_trust_below`` predicates fires only when *all* of them are below, which
    says less about any single argument than a reader expects. Neither is a universal floor, and
    counting either would let this module claim a guarantee the engine does not give.
    """
    if condition.case_state_in or condition.case_state_not_in:
        return False
    if condition.arg_trust_at_least or condition.args_present or condition.args_system_authored:
        return False
    if condition.args_approved is not None:
        return False
    return len(condition.arg_trust_below) == 1 and condition.arg_trust_below[0].arg == arg_name


def _deny_floor(ruleset: RuleSet, tool_name: str, arg_name: str) -> TrustLevel | None:
    """The level a ``DENY`` rule demands of ``arg_name`` whatever else is true, if one does."""
    floor: TrustLevel | None = None
    for rule in ruleset.by_effect(Verdict.DENY):
        if tool_name not in rule.tools or not _is_unconditional_about(rule.when, arg_name):
            continue
        floor = _stronger(floor, rule.when.arg_trust_below[0].level)
    return floor


def _allow_floor(ruleset: RuleSet, tool_name: str, arg_name: str) -> TrustLevel | None:
    """The **weakest** level any authorising rule demands of ``arg_name``.

    ``None`` when some ``ALLOW`` rule for the tool does not name it, because that rule is a viable
    path with no requirement -- and also when no ``ALLOW`` rule names the tool at all, where the
    capability is unreachable for a reason a per-argument floor should not be used to express.
    """
    authorising = [rule for rule in ruleset.by_effect(Verdict.ALLOW) if tool_name in rule.tools]
    if not authorising:
        return None

    weakest: TrustLevel | None = None
    for rule in authorising:
        demanded: TrustLevel | None = None
        for predicate in rule.when.arg_trust_at_least:
            if predicate.arg == arg_name:
                demanded = _stronger(demanded, predicate.level)
        if demanded is None:
            return None
        if weakest is None or at_least(weakest, demanded):
            weakest = demanded
    return weakest


def required_trust(ruleset: RuleSet, tool_name: str, arg_name: str) -> TrustLevel | None:
    """The level ``arg_name`` must reach on every path a call to ``tool_name`` could take.

    The stronger of the two floors, because a call must clear the ``DENY`` rules *and* satisfy
    whichever ``ALLOW`` rule matched it. ``None`` means the configuration requires nothing of this
    argument, which is the honest answer for free text no rule names.
    """
    return _stronger(
        _deny_floor(ruleset, tool_name, arg_name), _allow_floor(ruleset, tool_name, arg_name)
    )


def _fires(ruleset: RuleSet, effect: Verdict, tool_name: str, *, approval_held: bool) -> bool:
    """Whether some rule of ``effect`` for ``tool_name`` can fire in a world with (or without) an
    approval.

    Only the ``args_approved`` clause is read, because it is the one clause whose truth an approval
    changes. Every other clause is about facts and states, and treating an unmet one as "cannot
    fire" would let this module claim a capability is unreachable when it is merely not established
    yet -- the difference between a boundary and a to-do list.
    """
    return any(
        tool_name in rule.tools and rule.when.args_approved is not (not approval_held)
        for rule in ruleset.by_effect(effect)
    )


def _reachable(ruleset: RuleSet, tool_name: str, *, approval_held: bool) -> bool:
    """Whether a call could be authorised in that world, in the engine's own match order.

    ``DENY`` is deliberately not consulted: a ``DENY`` rule refuses *some* calls, and whether this
    caregiver can present one that clears it is the question :func:`required_trust` and the unmet
    keys answer. What is asked here is only whether ``REQUIRE_HUMAN`` sits in front of every
    ``ALLOW``, which is a fact about the rules rather than about any particular call.
    """
    if _fires(ruleset, Verdict.REQUIRE_HUMAN, tool_name, approval_held=approval_held):
        return False
    return _fires(ruleset, Verdict.ALLOW, tool_name, approval_held=approval_held)


def _argument_boundary(
    ruleset: RuleSet, tool_name: str, arg: ArgSpec, configured: Mapping[str, str]
) -> ArgumentBoundary:
    return ArgumentBoundary(
        name=arg.name,
        arg_class=arg.arg_class,
        required_trust=required_trust(ruleset, tool_name, arg.name),
        trust_key=arg.trust_key,
        configured_value=None if arg.trust_key is None else configured.get(arg.trust_key),
        authored_value=AUTHORED_VALUES.get((tool_name, arg.name)),
        identifies=arg.identifies,
    )


def _capability(
    ruleset: RuleSet,
    registry: ToolRegistry,
    tool_name: str,
    configured: Mapping[str, str],
    observable: frozenset[str],
    recovery: Mapping[str, RecoveryClass],
) -> Capability:
    spec = registry.get(tool_name)
    if spec is None:  # pragma: no cover - callers iterate the registry's own names
        raise UnclassifiedCapability(f"{tool_name!r} is not in the registry")

    arguments = tuple(_argument_boundary(ruleset, tool_name, arg, configured) for arg in spec.args)

    # An argument that must be TRUSTED draws it from trusted configuration or from nowhere, so a key
    # this caregiver does not configure is a requirement nothing can meet. A required-TRUSTED
    # argument declaring no key at all is the same answer arrived at one step earlier.
    unmet = tuple(
        sorted(
            {
                argument.trust_key or argument.name
                for argument in arguments
                if argument.required_trust is TrustLevel.TRUSTED
                and (argument.trust_key is None or argument.trust_key not in configured)
            }
        )
    )

    authorising = any(tool_name in rule.tools for rule in ruleset.by_effect(Verdict.ALLOW))
    asked = _fires(ruleset, Verdict.REQUIRE_HUMAN, tool_name, approval_held=False)

    if not authorising:
        standing, because = CapabilityStanding.UNAVAILABLE, Unavailability.NO_AUTHORISING_RULE
    elif unmet:
        standing, because = CapabilityStanding.UNAVAILABLE, Unavailability.NO_TRUSTED_SOURCE
    elif _reachable(ruleset, tool_name, approval_held=False):
        standing, because = CapabilityStanding.AUTONOMOUS, None
    elif asked and _reachable(ruleset, tool_name, approval_held=True):
        # A person is really asked, and their answer really releases it. Both halves are needed:
        # an approval exists only because a REQUIRE_HUMAN verdict raised one, so a rule set whose
        # only ALLOW demands an approval that nothing requests authorises nothing at all.
        standing, because = CapabilityStanding.REQUIRES_HUMAN, None
    else:
        standing, because = CapabilityStanding.UNAVAILABLE, Unavailability.NO_ROUTE_TO_A_DECISION

    return Capability(
        tool_name=tool_name,
        standing=standing,
        unavailable_because=because,
        unmet_trust_keys=unmet,
        independently_observed=tool_name in observable,
        recovery_class=recovery[tool_name],
        arguments=arguments,
    )


def _refusals(capabilities: tuple[Capability, ...]) -> tuple[RefusalPrinciple, ...]:
    """The standing refusals this configuration actually implies.

    Each is derived rather than asserted, so a ruleset that stopped enforcing one would stop
    claiming it -- which is the whole difference between this page and the hand-written copy it
    replaces.
    """
    arguments = [argument for capability in capabilities for argument in capability.arguments]

    def guarded(arg_class: ArgClass, level: TrustLevel) -> bool:
        return any(
            argument.arg_class is arg_class
            and argument.required_trust is not None
            and at_least(argument.required_trust, level)
            for argument in arguments
        )

    found: list[RefusalPrinciple] = []
    if ArgClass.DESTINATION not in PROMOTABLE_ARG_CLASSES and guarded(
        ArgClass.DESTINATION, _AUTHORITY_TRUST[ArgClass.DESTINATION]
    ):
        found.append(RefusalPrinciple.UNTRUSTED_DESTINATION)
    if guarded(ArgClass.VALUE, TrustLevel.CONTROLLED):
        found.append(RefusalPrinciple.UNEARNED_VALUE_TRUST)
    if ArgClass.AUTHORITY not in PROMOTABLE_ARG_CLASSES and guarded(
        ArgClass.AUTHORITY, _AUTHORITY_TRUST[ArgClass.AUTHORITY]
    ):
        found.append(RefusalPrinciple.DOCUMENT_AUTHORITY)
    if capabilities and all(
        any(argument.identifies for argument in capability.arguments) for capability in capabilities
    ):
        found.append(RefusalPrinciple.SECOND_RECORDED_EFFECT)
    return tuple(found)


def boundaries(
    ruleset: RuleSet,
    registry: ToolRegistry,
    destinations: tuple[tuple[str, str], ...],
    *,
    observable: frozenset[str] = INDEPENDENTLY_OBSERVABLE,
    recovery: Mapping[str, RecoveryClass] = RECOVERY_CLASS,
) -> Boundaries:
    """What ``destinations`` may reach under ``ruleset``, over ``registry``'s external effects.

    Pure. Nothing is read from a store, a provider, a clock or a principal, and nothing is written
    anywhere -- so the same three inputs always describe the same boundary, which is what lets the
    cross-check in ``tests/test_boundaries.py`` compare this against the engine's own answers.

    Args:
        ruleset: the rules an engine would decide by -- the loaded object, never a re-parse.
        registry: the tool registry those rules resolve against.
        destinations: the owning caregiver's ``(fact key, value)`` pairs, in exactly the shape
            :func:`careloop.graph.trusted.trusted_facts` seeds. It is CareLoop's own directory
            record and never a token claim; see ``tests/test_identity_boundary.py``.
        observable: which action types something other than CareLoop can read back. Defaults to
            the shipped declaration and is a parameter for the reason the projection checks take
            their tables: a derivation that can only run against four known tool names is a
            derivation nobody can prove generic. Production passes the default; nothing else may.
        recovery: what may be done when a dispatch's answer is lost, per action type. Same
            defaulting, same reason.
    """
    configured = dict(destinations)
    capabilities = tuple(
        _capability(ruleset, registry, tool_name, configured, observable, recovery)
        for tool_name in sorted(registry.external_effect_tools)
    )
    return Boundaries(capabilities=capabilities, refusals=_refusals(capabilities))


class UnclassifiedCapability(Exception):
    """Raised when an external-effect tool cannot be described at all."""


def check_every_external_effect_is_describable(
    registry: ToolRegistry = REGISTRY, recovery: Mapping[str, RecoveryClass] = RECOVERY_CLASS
) -> None:
    """Fail at import unless every external-effect tool has a recovery classification.

    :data:`careloop.graph.recovery.RECOVERY_CLASS` is subscripted rather than defaulted in
    :func:`_capability`, deliberately -- a tool with no classification is a tool nobody decided
    about, and defaulting one would have this module invent a recovery contract. That module's own
    coverage check already refuses the mismatch; this fails in the same breath rather than as a
    ``KeyError`` in front of a caregiver.

    Raises:
        UnclassifiedCapability: naming every external-effect tool with no recovery class.
    """
    missing = sorted(registry.external_effect_tools - frozenset(recovery))
    if missing:
        raise UnclassifiedCapability(
            f"external-effect tool(s) {missing} carry no RECOVERY_CLASS, so their boundary "
            f"cannot state what happens when a dispatch's answer is lost"
        )


check_every_external_effect_is_describable()


__all__ = [
    "ArgumentBoundary",
    "Boundaries",
    "Capability",
    "CapabilityStanding",
    "RefusalPrinciple",
    "Unavailability",
    "UnclassifiedCapability",
    "boundaries",
    "check_every_external_effect_is_describable",
    "required_trust",
]
