"""The policy verdict vocabulary.

Two audiences, deliberately separated.

The **model** sees only :data:`REASON_TEXT` — a fixed mapping from a closed
:class:`ReasonCode` enum to a literal string. Denial text is echoed back to the planner as an
error tool result, so anything interpolated into it is content an attacker who controls a document
can cause the planner to read. Interpolation here is a self-inflicted injection channel, and the
defence is structural rather than careful: there is no f-string in this module, and the mapping is
literals only. A reason string can therefore never contain an argument value, a trust level, a
rule id, or the contents of any allowlist.

The **timeline** sees :class:`Decision`, which carries the full diagnostic picture — which rule
matched, what class each argument had, what trust each value resolved to, and the raw arguments.
None of that is ever returned to a model. :meth:`Decision.__str__` renders without arguments so
that a careless log line cannot leak one either.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from careloop.provenance.trust import ArgClass, TrustLevel


class Verdict(StrEnum):
    """What the policy engine decided about one proposed tool call."""

    ALLOW = "ALLOW"
    """Proceed. The call may execute."""

    DENY = "DENY"
    """Refuse. No human can be asked; the planner must propose something else."""

    REQUIRE_HUMAN = "REQUIRE_HUMAN"
    """Refuse for now. Surface the proposal to the caregiver and await a decision."""


class ReasonCode(StrEnum):
    """The closed vocabulary of model-visible refusal reasons.

    Closed on purpose. New codes are added here by deliberate decision and recorded in the session
    log; a rule may never carry free text to the model. Keeping the vocabulary coarse also limits
    how much of the policy surface the planner can map by probing it.
    """

    DESTINATION_NOT_TRUSTED = "DESTINATION_NOT_TRUSTED"
    """Where the effect would land did not come from approved configuration."""

    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"
    """The action is legitimate but needs a caregiver decision first."""

    WRONG_CASE_STATE = "WRONG_CASE_STATE"
    """The case has not reached a point where this action makes sense."""

    INSUFFICIENT_FACT_TRUST = "INSUFFICIENT_FACT_TRUST"
    """An argument's value is not confirmed well enough to be used."""

    NO_RULE_MATCH = "NO_RULE_MATCH"
    """Nothing permits this action. The deny-by-default outcome."""


#: The only strings that ever reach the model.
#:
#: Literals only. Adding an f-string, ``.format`` call or ``%`` interpolation to this mapping
#: reopens the injection channel that invariant 6 closes, and
#: ``tests/test_reason_purity.py`` fails the moment one appears.
REASON_TEXT: Final[Mapping[ReasonCode, str]] = {
    ReasonCode.DESTINATION_NOT_TRUSTED: (
        "Recipients must come from approved configuration. Propose a different action."
    ),
    ReasonCode.REQUIRES_HUMAN_APPROVAL: (
        "This action needs the caregiver's decision. Stop and await it."
    ),
    ReasonCode.WRONG_CASE_STATE: (
        "The required evidence has not been gathered yet. Gather it first."
    ),
    # No "confirm it and retry" directive, deliberately. This code also covers arguments that
    # confirmation can never make acceptable, and inviting a retry there buys a wasted replan.
    ReasonCode.INSUFFICIENT_FACT_TRUST: "This argument is not confirmed well enough to be used.",
    ReasonCode.NO_RULE_MATCH: "This action is not permitted. Propose a different action.",
}


def reason_text(code: ReasonCode) -> str:
    """Return the model-visible text for ``code``.

    A dictionary lookup and nothing else. Total over :class:`ReasonCode` by test, so the
    ``KeyError`` branch is unreachable rather than merely unlikely.
    """
    return REASON_TEXT[code]


class Decision(BaseModel):
    """One policy verdict, with everything the audit trail needs and nothing the model may see."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    tool_name: str

    reason_code: ReasonCode | None = None
    """Model-visible code. ``None`` exactly when the verdict is ``ALLOW``."""

    matched_rule_id: str | None = None
    """Which rule decided this. ``None`` when no rule matched and the default applied."""

    diagnostic: str = ""
    """Timeline-only free text. May name rule ids, argument classes and trust levels."""

    arg_classes: dict[str, ArgClass] = Field(default_factory=dict)
    arg_trust: dict[str, TrustLevel] = Field(default_factory=dict)

    args: dict[str, Any] = Field(default_factory=dict)
    """The proposed arguments, verbatim, for the timeline. Never rendered by ``__str__``."""

    @model_validator(mode="after")
    def _reason_code_iff_refused(self) -> Decision:
        if self.verdict is Verdict.ALLOW and self.reason_code is not None:
            raise ValueError(
                "an ALLOW decision carries no reason code; there is nothing to explain"
            )
        if self.verdict is not Verdict.ALLOW and self.reason_code is None:
            raise ValueError(f"a {self.verdict} decision must carry a reason code")
        return self

    @property
    def allowed(self) -> bool:
        """Whether the call may proceed."""
        return self.verdict is Verdict.ALLOW

    @property
    def model_visible_reason(self) -> str | None:
        """The text to return to the planner, or ``None`` when the call is allowed."""
        return None if self.reason_code is None else reason_text(self.reason_code)

    def __str__(self) -> str:
        """Render without argument values, so decisions are safe to log."""
        rule = self.matched_rule_id or "default"
        return f"Decision({self.verdict}, {self.tool_name}, rule={rule}, code={self.reason_code})"


__all__ = ["REASON_TEXT", "Decision", "ReasonCode", "Verdict", "reason_text"]
