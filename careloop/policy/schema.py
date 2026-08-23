"""The shape of ``rules.yaml``.

Strictly declarative. Nothing here evaluates a condition, reads a case or touches the registry —
matching lives in :mod:`careloop.policy.engine` so that "what the engine does" is readable in one
file rather than spread across the data model.

Every model forbids unknown keys. A misspelt condition key is a rule that silently never fires,
which is the most dangerous kind of policy bug: the suite stays green and the guard is gone. Here
it fails at load instead.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from careloop.policy.verdicts import ReasonCode, Verdict
from careloop.provenance.trust import ArgClass, TrustLevel
from careloop.state.case import CaseState

_STRICT = ConfigDict(frozen=True, extra="forbid")


class ArgTrust(BaseModel):
    """A predicate about the trust resolved for one argument."""

    model_config = _STRICT

    arg: str
    level: TrustLevel

    arg_class: ArgClass | None = None
    """An assertion about the argument's class, not a definition of it.

    It has no effect at evaluation time. Its only job is to fail startup when a rule's belief
    about an argument contradicts the registry — so a rule can document what it is guarding
    without becoming a second declaration site for argument classes.
    """


class Condition(BaseModel):
    """When a rule fires. Every populated clause must hold; an empty condition always holds.

    Clause names read the way they fire. A ``DENY`` rule's condition describes the circumstances
    of the *denial*, so ``arg_trust_below`` on a ``DENY`` rule means "deny when the trust is below
    this", not "require this much trust".
    """

    model_config = _STRICT

    case_state_in: tuple[CaseState, ...] = ()
    """Holds when the case is in one of these states."""

    case_state_not_in: tuple[CaseState, ...] = ()
    """Holds when the case is in none of these states."""

    arg_trust_below: tuple[ArgTrust, ...] = ()
    """Holds when every named argument resolves *below* its level.

    An argument the call does not carry resolves ``UNTRUSTED``, so it is below anything higher.
    **That is no longer how a missing destination is refused**, and the docstring used to say it
    was: since session P, :meth:`careloop.policy.engine.PolicyEngine.evaluate` refuses an argument
    map that omits any argument the tool declares *before* any condition is evaluated, so
    ``send_email`` with no ``to`` is denied ``NO_RULE_MATCH`` as an incomplete call rather than
    ``DESTINATION_NOT_TRUSTED`` as an email to an untrusted recipient. A mapping with no recipient
    is not an email to the wrong person; it is not a complete call. The resolver's fail-closed
    default is unchanged and kept as defence in depth.
    """

    arg_trust_at_least: tuple[ArgTrust, ...] = ()
    """Holds when every named argument resolves at or above its level."""

    args_present: tuple[str, ...] = ()
    """Holds when the call supplies every named argument with a non-empty value."""

    args_system_authored: tuple[str, ...] = ()
    """Holds when every named argument carries exactly the value CareLoop composes for it.

    Deterministic, and deliberately not a statement about trust. It compares the supplied canonical
    value against :data:`careloop.tools.registry.AUTHORED_VALUES` — a fixed literal — and answers
    *did a model write this string*. Nothing is promoted, no
    :class:`~careloop.provenance.facts.Fact` is minted, and no trust level moves; an argument named
    here still resolves ``UNTRUSTED`` and would still fail a rule that required its trust.

    **The second of two enforcement points**, and the reason it exists is that the first is a
    schema. A ``SYSTEM``-authored argument is omitted from every model-facing proposal schema, so a
    model cannot express it — but a hand-built
    :class:`~careloop.agents.schemas.PlannerIntentBase`, a replayed corpus entry or a future seam
    can, and an ``ALLOW`` rule that did not read this would authorise whatever they put there.
    Naming it in the rule is what stops the positive path depending on the schema being the thing
    that catches it, exactly as ``allow-approved-portal-access-grant`` names ``portal_id`` beside a
    ``DENY`` that already refuses it.

    Naming an argument no tool declares ``SYSTEM``-authored fails at *load*, in the loader's
    registry-resolution step, rather than becoming a clause that can never hold.
    """

    args_approved: bool | None = None
    """Holds when a caregiver's approval covering exactly these arguments is (or is not) recorded.

    ``None`` — the default — means the clause is absent and approval is not consulted at all.
    ``False`` holds when no granted approval covers the call; ``True`` holds when one does.

    This clause exists because a ``REQUIRE_HUMAN`` rule without it can never be satisfied. Policy
    is evaluated twice — once to route the proposal while the case is ``PLANNING``, once at the
    tool boundary while it is ``EXECUTING`` — and an unconditional ``REQUIRE_HUMAN`` fires at both,
    so the caregiver's decision would park the case a second time and the action could never
    happen. Writing it as a *condition* keeps the fix in ``rules.yaml``: the alternative was a
    Python branch in the execute node that skipped the gate when an approval existed, which is a
    rule encoded in code and a bypass around the enforcement point in one move.

    The approval is matched on the canonical argument digest, so it authorises one specific call.
    It is also strictly weaker than the ``DENY`` rules, which are evaluated first: **an approval
    cannot launder an untrusted destination**, because the denial never becomes a question a human
    is asked.
    """

    @property
    def referenced_args(self) -> frozenset[str]:
        """Every argument name this condition mentions, whatever the clause."""
        return frozenset(
            [predicate.arg for predicate in (*self.arg_trust_below, *self.arg_trust_at_least)]
            + list(self.args_present)
            + list(self.args_system_authored)
        )

    @property
    def arg_class_assertions(self) -> tuple[ArgTrust, ...]:
        """Every predicate that asserts an argument class, for load-time cross-checking."""
        return tuple(
            predicate
            for predicate in (*self.arg_trust_below, *self.arg_trust_at_least)
            if predicate.arg_class is not None
        )


class Rule(BaseModel):
    """One policy rule."""

    model_config = _STRICT

    id: str
    effect: Verdict
    tools: tuple[str, ...] = Field(min_length=1)
    """Which tools this rule speaks about. Always a list, even for one tool: uniform to read and
    uniform for the coverage check to enumerate."""

    when: Condition = Field(default_factory=Condition)
    diagnostic: str
    """Timeline-only free text. May name anything; it never reaches a model."""

    reason_code: ReasonCode | None = None
    """Model-visible code. Required for a refusal, forbidden for an ``ALLOW``."""

    @model_validator(mode="after")
    def _reason_code_iff_refusal(self) -> Rule:
        if self.effect is Verdict.ALLOW and self.reason_code is not None:
            raise ValueError(
                f"rule {self.id!r} is an ALLOW and must not carry a reason_code; "
                f"an allowed call has nothing to explain to the model"
            )
        if self.effect is not Verdict.ALLOW and self.reason_code is None:
            raise ValueError(f"rule {self.id!r} is a {self.effect} and must carry a reason_code")
        return self


class RuleSet(BaseModel):
    """A whole rules file, in file order."""

    model_config = _STRICT

    version: int
    rules: tuple[Rule, ...]

    @model_validator(mode="after")
    def _reject_duplicate_ids(self) -> RuleSet:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"rule id {rule.id!r} is declared twice")
            seen.add(rule.id)
        return self

    def by_effect(self, effect: Verdict) -> tuple[Rule, ...]:
        """Every rule with this effect, in file order."""
        return tuple(rule for rule in self.rules if rule.effect is effect)

    @property
    def tools_named(self) -> frozenset[str]:
        """Every tool name mentioned by any rule."""
        return frozenset(tool for rule in self.rules for tool in rule.tools)

    @property
    def rule_ids(self) -> frozenset[str]:
        """Every rule id."""
        return frozenset(rule.id for rule in self.rules)


__all__ = ["ArgTrust", "Condition", "Rule", "RuleSet"]
