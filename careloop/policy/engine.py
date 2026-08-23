"""The deterministic policy engine.

:meth:`PolicyEngine.evaluate` is a pure function of ``(rules, registry, tool, args, case)``. It
performs no I/O, calls no model, and mutates nothing — not the case, not the arguments, not
itself. Everything it needs to decide is passed in, which is what makes a verdict reproducible
from the timeline months later.

Two ideas carry the weight.

**Trust resolution.** An argument arrives as a bare value; its trust comes from looking that value
up in the case's recorded facts **under the fact key the argument declares**. The lowest trust
level among facts matching *both* wins, and a value the case has never recorded under that key
resolves ``UNTRUSTED``. Deny-by-default therefore extends from rules to provenance: an argument
that appeared from nowhere is untrusted, not unclassified.

**The key half of that pair is load-bearing and was once absent.** Until it was added, resolution
matched on ``fact.value`` alone, so the question actually being asked was *"has this string ever
been seen at this trust level, under any key at all?"* — and a value that earned trust for one
purpose satisfied a requirement for another. A ``patient_id`` validated as ``PT-0000`` satisfied
``upload_document.document_id``; a ``document_id`` satisfied ``book_appointment.slot_start`` and
reached the clinic over HTTP; and the configured ``portal_id``, named as a *role*, resolved
``TRUSTED`` and walked past ``deny-authority-grant-below-trusted`` — the system's only
``AUTHORITY`` guard — with no promotion involved and nothing but trusted configuration on the case.
``tests/test_typed_trust.py`` refuses all four, and proves it would still notice if the key were
ignored again.

There is deliberately **no fallback to matching by value** when an argument declares no key. Such an
argument resolves ``UNTRUSTED`` outright, and a rule that tries to require trust of one fails in
:mod:`careloop.policy.loader` at startup rather than becoming a rule that can never match.

The text an argument is reduced to before that lookup comes from
:func:`careloop.provenance.canonical.canonical_value`, re-exported here for the callers that have
always read it from this module. It lives in ``provenance`` because tool idempotency keys derive
from the same reduction, and one value must not have two spellings.

**Completeness comes before all of it.** A verdict is only meaningful for a *complete* tool call, so
an argument map whose keys are not exactly the ones the registry declares is refused before trust is
resolved, before an approval is consulted and before any rule is matched. Undeclared arguments were
always refused that way; arguments the tool declares and the call omits were not, and session P
found what the missing half cost. Ten of the thirteen declared arguments were denied when omitted,
but only because a rule happened to name each of them and read the absence as ``UNTRUSTED``; the
three that no rule names — ``send_email.subject``, ``send_email.body`` and
``book_appointment.reason``, which carry no ``trust_key`` and by design cannot — went the other way.
A booking without its reason reached ``ALLOW`` autonomously on the one tool whose effect is
genuinely external, and an email without its subject or body reached ``ALLOW`` once a caregiver's
approval covered the incomplete map, so the policy layer considered a malformed capability approved.
Nothing incomplete could execute — ``ToolSet.<tool>(**args)`` raises ``TypeError`` before the
executor — and nothing incomplete could be proposed, because
:meth:`careloop.agents.schemas.PlannerIntentBase._bind_arguments_to_the_chosen_tool` rebuilds a
proposal's arguments as exactly ``spec.arg_names``. The defect was that the authority layer issued
verdicts about objects that are not tool calls, with an argument-binding accident in another layer
as the only thing between one of those verdicts and an effect.

**Key presence, and nothing else.** ``{"body": ""}`` is a complete ``send_email`` call: the model
named the argument and put nothing in it, which is a statement about the *value*, and the trust
rules and ``args_present`` already answer those. Reading truthiness here would conflate syntactic
completeness with semantic validity and take work away from the rules that own it.

**Match ordering**, fixed here and not configurable from YAML: explicit ``DENY``, then
``REQUIRE_HUMAN``, then ``ALLOW``, then a default ``DENY``. A rules file that could reorder its own
precedence could be talked into promoting an ``ALLOW`` above a ``DENY``.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache

from careloop.policy.loader import default_rules
from careloop.policy.schema import Condition, RuleSet
from careloop.policy.verdicts import Decision, ReasonCode, Verdict
from careloop.provenance.canonical import canonical_value
from careloop.provenance.trust import TrustLevel, at_least
from careloop.state.case import Case
from careloop.tools.idempotency import UncanonicalizableArgument, args_digest
from careloop.tools.registry import AUTHORED_VALUES, REGISTRY, ToolRegistry


def resolve_trust(fact_key: str | None, value: object, case: Case) -> TrustLevel:
    """Return the trust level ``value`` carries within ``case`` **as a** ``fact_key``.

    ``fact_key`` is the :attr:`careloop.provenance.facts.Fact.key` the consuming argument declares
    in :data:`careloop.tools.registry.REGISTRY`, and it is required and positional so that no
    caller can perform a value-only lookup by omitting it. ``None`` means the argument declares no
    key, and resolves ``UNTRUSTED`` without consulting the case at all — there is no fallback to
    matching by value, because a fallback would restore exactly the defect this parameter exists to
    close.

    The **lowest** level among facts matching that key *and* that canonical value. Lowest rather
    than highest so that a poisoned duplicate cannot be laundered by a legitimate twin: if the same
    string was recorded under one key both from trusted config and from an attacker-controlled PDF,
    the PDF wins. Scoped to one key rather than applied across all of them, because a fact under an
    unrelated key is not a duplicate of this one — it is a different statement that happens to be
    spelled the same, and letting it participate was both the vulnerability above and a standing
    false-denial hazard in the other direction.
    """
    if fact_key is None:
        return TrustLevel.UNTRUSTED

    canonical = canonical_value(value)
    if canonical is None:
        return TrustLevel.UNTRUSTED

    lowest: TrustLevel | None = None
    for fact in case.facts:
        if fact.key != fact_key or fact.value != canonical:
            continue
        if lowest is None or not at_least(fact.trust_level, lowest):
            lowest = fact.trust_level

    return TrustLevel.UNTRUSTED if lowest is None else lowest


def _condition_holds(
    condition: Condition,
    tool_name: str,
    args: Mapping[str, object],
    case: Case,
    trust: Mapping[str, TrustLevel],
) -> bool:
    """Whether every populated clause of ``condition`` holds. An empty condition always holds."""
    if condition.case_state_in and case.state not in condition.case_state_in:
        return False
    if condition.case_state_not_in and case.state in condition.case_state_not_in:
        return False

    # An argument the map does not carry resolves UNTRUSTED rather than skipping the clause. That
    # is defence in depth rather than the live path: since session P, `evaluate` refuses an argument
    # map missing a declared key before any condition is read, so this default is unreachable
    # through the engine for a *declared* argument. It is kept because it is the fail-closed
    # direction and because a rule may name an argument the tool does not declare — which the loader
    # refuses at startup, and which this would refuse again if one ever got past it.
    for predicate in condition.arg_trust_below:
        if at_least(trust.get(predicate.arg, TrustLevel.UNTRUSTED), predicate.level):
            return False
    for predicate in condition.arg_trust_at_least:
        if not at_least(trust.get(predicate.arg, TrustLevel.UNTRUSTED), predicate.level):
            return False

    for arg_name in condition.args_present:
        if canonical_value(args.get(arg_name)) in (None, ""):
            return False

    # **Authorship, which is not trust and not approval.** It asks whether the supplied value is
    # byte-identical to the fixed literal CareLoop composes for this argument, so a model-authored
    # string fails it whatever the string says. Compared through ``canonical_value`` because that is
    # the one reduction the engine and the idempotency key share; a value with no canonical form
    # answers ``None``, which cannot equal a declared literal, so it fails closed.
    for arg_name in condition.args_system_authored:
        declared = AUTHORED_VALUES.get((tool_name, arg_name))
        if declared is None or canonical_value(args.get(arg_name)) != declared:
            return False

    if condition.args_approved is None:
        return True
    return approved(tool_name, args, case) is condition.args_approved


def approved(tool_name: str, args: Mapping[str, object], case: Case) -> bool:
    """Whether ``case`` carries a granted approval for exactly this call.

    ``tool_name`` is the tool the engine is currently evaluating, and it is required and positional
    for the reason ``fact_key`` is on :func:`resolve_trust`: an authority lookup that can be made
    tool-blind by omitting an argument is an authority lookup that will be. It is threaded from
    :meth:`PolicyEngine.evaluate` rather than declared in ``rules.yaml`` — the engine already knows
    which tool it is deciding, and naming it in the rule file would make the tool a second source of
    truth that a rule could contradict.

    So ``args_approved: true`` reads *"a caregiver approved this exact tool with these exact
    arguments"*. It used to read *"a caregiver approved these exact arguments"*, and the missing
    half is what let an approval for one tool satisfy the clause for another whose argument map
    canonicalised the same way. See :meth:`careloop.state.case.Case.is_approved`.

    Fails closed twice over. Arguments with no canonical form cannot be digested, so they cannot
    match an approval and are reported unapproved rather than raising — a call that cannot be
    identified has not been authorised. And the digest covers *every* argument, so an approval
    granted for one body does not authorise the same email with a different one.
    """
    try:
        digest = args_digest(args)
    except UncanonicalizableArgument:
        return False
    return case.is_approved(tool_name, digest)


#: The order in which effects are considered. Fixed here, never read from YAML.
_MATCH_ORDER: tuple[Verdict, ...] = (Verdict.DENY, Verdict.REQUIRE_HUMAN, Verdict.ALLOW)


class PolicyEngine:
    """Evaluates proposed tool calls against a rule set and a tool registry."""

    def __init__(self, ruleset: RuleSet, registry: ToolRegistry = REGISTRY) -> None:
        self._ruleset = ruleset
        self._registry = registry

    @property
    def ruleset(self) -> RuleSet:
        """The rules this engine decides by."""
        return self._ruleset

    @property
    def registry(self) -> ToolRegistry:
        """The registry this engine resolves argument classes against."""
        return self._registry

    def evaluate(self, tool_name: str, args: Mapping[str, object], case: Case) -> Decision:
        """Decide one proposed tool call.

        Pure: ``case`` and ``args`` are read and never written, and the same inputs always produce
        an equal :class:`~careloop.policy.verdicts.Decision`.
        """
        supplied = dict(args)

        spec = self._registry.get(tool_name)
        if spec is None:
            return self._default_deny(
                tool_name,
                supplied,
                diagnostic=(
                    f"tool {tool_name!r} is not in the registry; an unregistered tool has no "
                    f"argument classes and therefore no policy"
                ),
            )

        undeclared = sorted(set(supplied) - spec.arg_names)
        if undeclared:
            # Deny rather than ignoring the extra arguments. An argument the registry does not know
            # has no class, so nothing establishes that it is not a destination.
            return self._default_deny(
                tool_name,
                supplied,
                diagnostic=(
                    f"tool {tool_name!r} was called with undeclared argument(s) "
                    f"{', '.join(undeclared)}; undeclared arguments have no argument class"
                ),
            )

        missing = sorted(spec.arg_names - set(supplied))
        if missing:
            # The symmetric half, and it was absent until session P. Ten of the thirteen declared
            # arguments were denied when omitted, but by accident: each happens to be named by a
            # rule, so its absence surfaced as UNTRUSTED through the ``trust.get(arg, UNTRUSTED)``
            # default and the rule refused it. The three named by no rule — ``send_email.subject``,
            # ``send_email.body`` and ``book_appointment.reason``, which carry no ``trust_key`` and
            # by design cannot — went the other way: a booking without its reason reached ALLOW
            # autonomously, and an email without its subject or body reached ALLOW once a
            # caregiver's approval covered the incomplete map. The approval subsystem was right and
            # unchanged; what was wrong is that the engine ever reached it.
            #
            # Key presence and nothing else. ``{"body": ""}`` is a complete call — the model named
            # the argument and put nothing in it, which is a statement about the *value*, and the
            # trust rules and ``args_present`` already answer those. Reading truthiness here would
            # conflate syntactic completeness with semantic validity and quietly take work away
            # from the rules that own it.
            return self._default_deny(
                tool_name,
                supplied,
                diagnostic=(
                    f"tool {tool_name!r} was called without declared argument(s) "
                    f"{', '.join(missing)}; a policy verdict is only meaningful for a complete "
                    f"tool call"
                ),
            )

        arg_classes = {name: spec.arg_class(name) for name in supplied}
        arg_trust = {
            name: resolve_trust(spec.trust_key(name), value, case)
            for name, value in supplied.items()
        }

        for effect in _MATCH_ORDER:
            for rule in self._ruleset.by_effect(effect):
                if tool_name not in rule.tools:
                    continue
                if not _condition_holds(rule.when, tool_name, supplied, case, arg_trust):
                    continue
                return Decision(
                    verdict=rule.effect,
                    tool_name=tool_name,
                    reason_code=rule.reason_code,
                    matched_rule_id=rule.id,
                    diagnostic=rule.diagnostic,
                    arg_classes=arg_classes,
                    arg_trust=arg_trust,
                    args=supplied,
                )

        return self._default_deny(
            tool_name,
            supplied,
            diagnostic=f"no rule matched a call to {tool_name!r}; denying by default",
            arg_classes=arg_classes,
            arg_trust=arg_trust,
        )

    @staticmethod
    def _default_deny(
        tool_name: str,
        args: dict[str, object],
        *,
        diagnostic: str,
        arg_classes: dict[str, object] | None = None,
        arg_trust: dict[str, TrustLevel] | None = None,
    ) -> Decision:
        return Decision(
            verdict=Verdict.DENY,
            tool_name=tool_name,
            reason_code=ReasonCode.NO_RULE_MATCH,
            matched_rule_id=None,
            diagnostic=diagnostic,
            arg_classes=arg_classes or {},
            arg_trust=arg_trust or {},
            args=args,
        )


@lru_cache(maxsize=1)
def default_engine() -> PolicyEngine:
    """The engine built from the shipped rules and the shipped registry."""
    return PolicyEngine(default_rules(), REGISTRY)


__all__ = ["PolicyEngine", "approved", "canonical_value", "default_engine", "resolve_trust"]
