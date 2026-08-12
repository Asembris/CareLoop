"""One decision, consulted twice.

Enforcement is layered: the ``BeforeToolCall`` hook is the primary gate and the gated tool wrapper
is the backstop that holds even if the hook is never registered. Both consult the same
:class:`~careloop.policy.engine.PolicyEngine` over the same ``rules.yaml``, so the layering is
about *where* the check happens rather than about what it decides. Caching the verdict is what
keeps two enforcement points from costing two evaluations.

**The cache key is the whole of this module's difficulty.** Keying on
``(case_id, tool_name, args)`` — which is what session 2 planned — does not identify the *facts
the decision was computed against*. A decision computed before the case changed would then be
replayed after it changed, which is a time-of-check-to-time-of-use window sitting precisely
between the two places the architecture claims are independent. Two enforcement points reading one
stale answer are one enforcement point wearing a disguise.

So the key also carries ``case.version``, ``case.state`` and a fingerprint over the case's facts.
Version alone would very nearly do, since every persisted change bumps it — but the version is a
counter and the fingerprint is the honest identifier of what was actually decided against, and
``case.state`` is in the key because rules condition on it directly. Over-invalidating costs one
pure-function call; under-invalidating costs the property.

**Arguments that cannot be canonicalised are never cached.** ``args_digest`` refuses to key a
value with no canonical form, because folding distinct values onto a sentinel would let two
different calls share one entry — the same fail-open hazard that makes
:mod:`careloop.tools.idempotency` raise. Here the answer is milder: such a call is evaluated
fresh every time. The engine still decides it, and it still fails closed, since a value with no
canonical form resolves ``UNTRUSTED``.

**Nothing in this module is model-visible.** :exc:`PolicyDenied` carries the whole
:class:`~careloop.policy.verdicts.Decision` — matched rule id, argument classes, resolved trust
levels and the raw arguments — because the timeline needs all of it. A caller that wants
something to show a planner asks the decision for
:attr:`~careloop.policy.verdicts.Decision.model_visible_reason` and gets a literal from the closed
:class:`~careloop.policy.verdicts.ReasonCode` vocabulary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping

from careloop.policy.engine import PolicyEngine, default_engine
from careloop.policy.verdicts import Decision
from careloop.state.case import Case
from careloop.tools.idempotency import UncanonicalizableArgument, args_digest


class PolicyDenied(Exception):
    """Raised when a gated tool call is refused.

    Carries the full :class:`~careloop.policy.verdicts.Decision`, which names rule ids and holds
    raw argument values. It is an audit object, not a message: nothing in CareLoop returns this
    exception, its text, or the decision inside it to a model. The planner is told
    ``decision.model_visible_reason``, which is a literal from the closed reason-code vocabulary,
    and nothing else.
    """

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        # Decision.__str__ renders without argument values, so this message is safe to log.
        super().__init__(f"policy refused {decision}")


def facts_fingerprint(case: Case) -> str:
    """A digest identifying the trust picture a decision would be computed against.

    Covers the key, value and trust level of every fact, in order. Order is included because it is
    free and conservative: it cannot change a verdict — trust resolution takes the lowest matching
    level regardless of order — but including it means the fingerprint can only ever
    over-invalidate the cache, never under-invalidate it.
    """
    payload = [[fact.key, fact.value, str(fact.trust_level)] for fact in case.facts]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decision_cache_key(case: Case, tool_name: str, args: Mapping[str, object]) -> str | None:
    """The cache key for one proposed call, or ``None`` if the call must not be cached.

    ``None`` is returned when the arguments have no canonical form, since a key that cannot
    distinguish two calls must not be allowed to answer for either of them.
    """
    try:
        digest = args_digest(args)
    except UncanonicalizableArgument:
        return None

    payload = {
        "case_id": case.case_id,
        "version": case.version,
        "state": str(case.state),
        "facts": facts_fingerprint(case),
        "tool": tool_name,
        "args": digest,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PolicyGate:
    """The single decision point both enforcement layers consult.

    Holds a *supplier* of the current case rather than a case, because the gate outlives any one
    version of it: the hook and the backstop fire within one tool dispatch, but the gate is built
    once per run and the case moves underneath it. Taking a snapshot at construction would make
    the staleness this module exists to prevent structurally invisible instead of structurally
    impossible.
    """

    def __init__(
        self,
        case_for: Callable[[], Case],
        engine: PolicyEngine | None = None,
    ) -> None:
        self._case_for = case_for
        self._engine = engine if engine is not None else default_engine()
        self._cache: dict[str, Decision] = {}
        self._evaluations = 0

    @property
    def engine(self) -> PolicyEngine:
        """The engine every decision comes from."""
        return self._engine

    @property
    def evaluations(self) -> int:
        """How many times the engine was actually consulted.

        Exposed so that "layering the two enforcement points costs one evaluation" is a test
        rather than a claim, and so that a cache that fails to invalidate is visible as a count
        that did not move.
        """
        return self._evaluations

    def case(self) -> Case:
        """The case as it stands now."""
        return self._case_for()

    def decide(self, tool_name: str, args: Mapping[str, object]) -> Decision:
        """Return the verdict for one proposed call against the current case."""
        case = self.case()
        key = decision_cache_key(case, tool_name, args)

        if key is not None and key in self._cache:
            return self._cache[key]

        self._evaluations += 1
        decision = self._engine.evaluate(tool_name, args, case)

        if key is not None:
            self._cache[key] = decision
        return decision

    def check(self, tool_name: str, args: Mapping[str, object]) -> Decision:
        """Return the verdict, or raise if it is not ``ALLOW``.

        Raises:
            PolicyDenied: on ``DENY`` and on ``REQUIRE_HUMAN`` alike. Both mean *not now*, and a
                tool boundary is not the place that can tell them apart — by the time a call is
                being dispatched the case is in ``EXECUTING``, from which the state machine has no
                edge to ``AWAITING_HUMAN``. Routing on ``REQUIRE_HUMAN`` happens earlier, while
                the case is still ``PLANNING``.
        """
        decision = self.decide(tool_name, args)
        if not decision.allowed:
            raise PolicyDenied(decision)
        return decision


__all__ = ["PolicyDenied", "PolicyGate", "decision_cache_key", "facts_fingerprint"]
