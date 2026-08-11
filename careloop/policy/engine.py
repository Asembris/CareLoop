"""The deterministic policy engine.

:meth:`PolicyEngine.evaluate` is a pure function of ``(rules, registry, tool, args, case)``. It
performs no I/O, calls no model, and mutates nothing — not the case, not the arguments, not
itself. Everything it needs to decide is passed in, which is what makes a verdict reproducible
from the timeline months later.

Two ideas carry the weight.

**Trust resolution.** An argument arrives as a bare value; its trust comes from looking that value
up in the case's recorded facts. The lowest trust level among matching facts wins, and a value the
case has never recorded resolves ``UNTRUSTED``. Deny-by-default therefore extends from rules to
provenance: an argument that appeared from nowhere is untrusted, not unclassified.

**Match ordering**, fixed here and not configurable from YAML: explicit ``DENY``, then
``REQUIRE_HUMAN``, then ``ALLOW``, then a default ``DENY``. A rules file that could reorder its own
precedence could be talked into promoting an ``ALLOW`` above a ``DENY``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache

from careloop.policy.loader import default_rules
from careloop.policy.schema import Condition, RuleSet
from careloop.policy.verdicts import Decision, ReasonCode, Verdict
from careloop.provenance.trust import TrustLevel, at_least
from careloop.state.case import Case
from careloop.tools.registry import REGISTRY, ToolRegistry


def canonical_value(value: object) -> str | None:
    """Return the comparison form of an argument value, or ``None`` if it has none.

    Facts store values as text, so an argument must be reduced to text before it can be matched
    against one. The reduction is closed and deliberately unforgiving:

    ``str``
        Used verbatim. No case-folding, no whitespace trimming, no unicode normalisation.
        Normalisation is a laundering surface — homoglyphs, casing, a trailing dot — and a
        fail-closed false denial costs a replan, while a false trust costs the whole property.
    ``bool``
        ``"true"`` / ``"false"``. Checked before ``int``, which it subclasses.
    ``int``
        Decimal, no separators.
    ``datetime`` / ``date``
        ISO 8601, **without timezone conversion**. Two spellings of one instant do not match, for
        the same reason two spellings of one address do not.
    anything else
        ``None``. The value has no comparison form, so it matches no fact and resolves
        ``UNTRUSTED``. ``float`` is excluded on purpose: its repr is unstable enough to be both a
        laundering hazard and a false-denial hazard, and no argument needs one.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return None


def resolve_trust(value: object, case: Case) -> TrustLevel:
    """Return the trust level ``value`` carries within ``case``.

    The **lowest** level among facts whose recorded value matches exactly. Lowest rather than
    highest so that a poisoned duplicate cannot be laundered by a legitimate twin: if the same
    string was seen both in trusted config and in an attacker-controlled PDF, the PDF wins.
    """
    canonical = canonical_value(value)
    if canonical is None:
        return TrustLevel.UNTRUSTED

    lowest: TrustLevel | None = None
    for fact in case.facts:
        if fact.value != canonical:
            continue
        if lowest is None or not at_least(fact.trust_level, lowest):
            lowest = fact.trust_level

    return TrustLevel.UNTRUSTED if lowest is None else lowest


def _condition_holds(
    condition: Condition,
    args: Mapping[str, object],
    case: Case,
    trust: Mapping[str, TrustLevel],
) -> bool:
    """Whether every populated clause of ``condition`` holds. An empty condition always holds."""
    if condition.case_state_in and case.state not in condition.case_state_in:
        return False
    if condition.case_state_not_in and case.state in condition.case_state_not_in:
        return False

    # An omitted argument resolves UNTRUSTED rather than skipping the clause, so a missing
    # destination is denied instead of slipping past the guard that names it.
    for predicate in condition.arg_trust_below:
        if at_least(trust.get(predicate.arg, TrustLevel.UNTRUSTED), predicate.level):
            return False
    for predicate in condition.arg_trust_at_least:
        if not at_least(trust.get(predicate.arg, TrustLevel.UNTRUSTED), predicate.level):
            return False

    for arg_name in condition.args_present:
        if canonical_value(args.get(arg_name)) in (None, ""):
            return False

    return True


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

        arg_classes = {name: spec.arg_class(name) for name in supplied}
        arg_trust = {name: resolve_trust(value, case) for name, value in supplied.items()}

        for effect in _MATCH_ORDER:
            for rule in self._ruleset.by_effect(effect):
                if tool_name not in rule.tools:
                    continue
                if not _condition_holds(rule.when, supplied, case, arg_trust):
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


__all__ = ["PolicyEngine", "canonical_value", "default_engine", "resolve_trust"]
