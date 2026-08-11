"""Token costing, and the spend guard.

Two numbers with different standing, kept apart on purpose:

**The estimate**, computed before any call, from a character-count heuristic. It exists to be shown
to a human before money is spent, and it is an over- rather than under-estimate by construction.

**The actual**, computed after, from provider-reported token usage. Only this one goes in the
results table.

Conflating them would let a run report a cost it never measured.
"""

from __future__ import annotations

import json
import pathlib
from typing import Final

from pydantic import BaseModel, ConfigDict

RATES_PATH: Final[pathlib.Path] = pathlib.Path(__file__).parent / "rates.json"

#: Characters per token, for the pre-run estimate only. English prose runs nearer 4; 3 is used so
#: the estimate errs high. An estimate that flatters the budget is worse than no estimate.
CHARS_PER_TOKEN_ESTIMATE: Final[float] = 3.0

#: Assumed output tokens per call when estimating. Structured answers here are short; this is
#: generous.
ESTIMATED_OUTPUT_TOKENS: Final[int] = 300


class UnknownRate(Exception):
    """Raised when a model has no published rate.

    Fails loudly rather than costing a run at zero. A model silently priced at nothing would look
    like the cheapest candidate in the table.
    """


class Rate(BaseModel):
    """Published price for one model, USD per 1M tokens."""

    model_config = ConfigDict(frozen=True)

    input: float
    output: float
    source: str = ""
    confidence: str = ""


class RateTable(BaseModel):
    """The parsed contents of ``rates.json``."""

    model_config = ConfigDict(frozen=True)

    currency: str
    unit: str
    as_of: str
    region: str
    rates: dict[str, Rate]

    def for_model(self, model_id: str) -> Rate:
        rate = self.rates.get(model_id)
        if rate is None:
            raise UnknownRate(
                f"no published rate for {model_id!r} in rates.json; add one rather than "
                f"letting the run report a cost of zero"
            )
        return rate

    def cost_usd(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        rate = self.for_model(model_id)
        return (input_tokens * rate.input + output_tokens * rate.output) / 1_000_000


def load_rates(path: pathlib.Path = RATES_PATH) -> RateTable:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("_comment", None)
    return RateTable.model_validate(payload)


def estimate_tokens(text: str) -> int:
    """A deliberately high character-based token estimate."""
    return max(1, int(len(text) / CHARS_PER_TOKEN_ESTIMATE) + 1)


class SpendEstimate(BaseModel):
    """What a run is expected to cost, before it runs."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    model_id: str
    calls: int
    cached_calls: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_usd: float

    @property
    def billable_calls(self) -> int:
        return self.calls - self.cached_calls


class BudgetExceeded(Exception):
    """Raised mid-run when actual spend passes the ceiling.

    A pre-run estimate is a guess. The ceiling is the thing that actually protects a
    non-replaceable key, so it is enforced against measured spend while the run is in progress,
    not checked once at the start.
    """


class SpendLedger:
    """Running total of actual spend, with a hard ceiling."""

    def __init__(self, ceiling_usd: float) -> None:
        self.ceiling_usd = ceiling_usd
        self._spent = 0.0

    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.ceiling_usd - self._spent)

    def charge(self, amount_usd: float) -> None:
        self._spent += amount_usd
        if self._spent > self.ceiling_usd:
            raise BudgetExceeded(
                f"spend ${self._spent:.4f} exceeded the ceiling ${self.ceiling_usd:.4f}; "
                f"raise --max-spend deliberately or reduce the task set"
            )

    def would_exceed(self, amount_usd: float) -> bool:
        return self._spent + amount_usd > self.ceiling_usd


__all__ = [
    "CHARS_PER_TOKEN_ESTIMATE",
    "ESTIMATED_OUTPUT_TOKENS",
    "RATES_PATH",
    "BudgetExceeded",
    "Rate",
    "RateTable",
    "SpendEstimate",
    "SpendLedger",
    "UnknownRate",
    "estimate_tokens",
    "load_rates",
]
