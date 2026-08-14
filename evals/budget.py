"""Two ceilings, because one of them cannot see what this run spends.

``bench/run.py`` guards a benchmark with a dollar ceiling, and that works there because every
model it buys is billed per token. It does not work here, and the reason is recorded in
``docs/spike-findings.md`` §7.7:

    ``build.nvidia.com`` is **credit-metered, not billed per token.** ``bench/rates.json`` prices
    it at ``0.0`` and says why. A dollar ceiling over a resource with no dollar price is not a
    generous guard — it is a guard that cannot see the thing it is guarding.

The simulator and the judge are on that endpoint. The *target* is not: its triage and planner
calls are Bedrock and genuinely priced. So the run carries two ceilings that bound different
resources:

``--max-spend``  USD, enforced against measured Bedrock spend. Guards the target.
``--max-calls``  every provider call the run makes, of any kind. Guards NVIDIA credits, and is
                 the one that matters here — deepteam retries three times with backoff, so a
                 failing endpoint burns calls fastest exactly when it is least useful.

The spend half is :class:`bench.cost.SpendLedger` unchanged, imported rather than reimplemented so
the two harnesses cannot drift on what a ceiling means.
"""

from __future__ import annotations

from typing import Final

from bench.cost import BudgetExceeded, RateTable, SpendLedger, load_rates

#: Ceiling on total provider calls. Approved default for session G.
DEFAULT_MAX_CALLS: Final[int] = 1500

#: Ceiling in USD over the priced (Bedrock) target calls.
DEFAULT_MAX_SPEND: Final[float] = 2.00


class CallCeilingExceeded(BudgetExceeded):
    """Raised when the run has made as many provider calls as it was permitted.

    A subclass of :class:`bench.cost.BudgetExceeded` so a caller that already stops on a budget
    failure stops on this one too. Two ceilings, one thing to catch.
    """


class CallLedger:
    """Counts every provider call the run makes, whoever makes it.

    Deliberately not per-role. The point is a bound on the *run*, and a per-role budget would let
    a judge that retries forever stay inside its own allowance while the endpoint refuses
    everything.
    """

    def __init__(self, ceiling: int = DEFAULT_MAX_CALLS) -> None:
        self.ceiling = ceiling
        self._made = 0
        self._by_role: dict[str, int] = {}

    @property
    def made(self) -> int:
        return self._made

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self._made)

    @property
    def by_role(self) -> dict[str, int]:
        return dict(sorted(self._by_role.items()))

    def charge(self, role: str, count: int = 1) -> None:
        """Record ``count`` calls made by ``role``.

        Raises:
            CallCeilingExceeded: once the ceiling is passed. Raised *after* recording, so the
                reported total is what was actually spent rather than what was permitted.
        """
        self._made += count
        self._by_role[role] = self._by_role.get(role, 0) + count
        if self._made > self.ceiling:
            raise CallCeilingExceeded(
                f"{self._made} provider call(s) made against a ceiling of {self.ceiling}; raise "
                f"--max-calls deliberately or reduce attacks-per-vulnerability-type"
            )

    def would_exceed(self, count: int = 1) -> bool:
        return self._made + count > self.ceiling


class Budget:
    """Both ceilings, held together so a run cannot be started under only one of them."""

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_spend_usd: float = DEFAULT_MAX_SPEND,
        rates: RateTable | None = None,
    ) -> None:
        self.calls = CallLedger(max_calls)
        self.spend = SpendLedger(max_spend_usd)
        self.rates = rates if rates is not None else load_rates()

    def charge_call(self, role: str, count: int = 1) -> None:
        self.calls.charge(role, count)

    def charge_priced(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        """Charge a *priced* call against both ceilings, and return what it cost.

        An unpriced model still charges the call ceiling. That is the whole point: the resource it
        consumes is real even where the dollar figure is zero.
        """
        cost = self.rates.cost_usd(model_id, input_tokens, output_tokens)
        self.spend.charge(cost)
        return cost

    def summary(self) -> str:
        return (
            f"{self.calls.made}/{self.calls.ceiling} provider calls {self.calls.by_role}, "
            f"${self.spend.spent_usd:.4f}/${self.spend.ceiling_usd:.2f} priced spend"
        )


def is_credit_metered(rates: RateTable, model_id: str) -> bool:
    """Whether a model's published price is zero because it is not billed per token.

    Used by the estimate, so a run can say plainly that its dollar figure does not describe the
    simulator and judge rather than printing ``~$0.0000`` and letting a reader assume it does.
    """
    try:
        rate = rates.for_model(model_id)
    except Exception:
        return False
    return rate.input == 0.0 and rate.output == 0.0


__all__ = [
    "DEFAULT_MAX_CALLS",
    "DEFAULT_MAX_SPEND",
    "Budget",
    "BudgetExceeded",
    "CallCeilingExceeded",
    "CallLedger",
    "is_credit_metered",
]
