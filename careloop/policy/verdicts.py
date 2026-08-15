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

    AUTHORITY_NOT_TRUSTED = "AUTHORITY_NOT_TRUSTED"
    """What powers would be granted did not come from approved configuration.

    Distinct from :attr:`INSUFFICIENT_FACT_TRUST` because the remedy differs, not merely the
    wording. An insufficiently trusted ``VALUE`` can be validated and reproposed; an ``AUTHORITY``
    argument can never be confirmed into acceptability, whatever a validator says about it.
    Sharing one code would mean either misleading the planner into a doomed retry or stripping the
    retry guidance from the rules that legitimately need it.
    """

    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"
    """The action is legitimate but needs a caregiver decision first."""

    WRONG_CASE_STATE = "WRONG_CASE_STATE"
    """The case has not reached a point where this action makes sense."""

    INSUFFICIENT_FACT_TRUST = "INSUFFICIENT_FACT_TRUST"
    """An argument's value is not confirmed well enough to be used."""

    NO_RULE_MATCH = "NO_RULE_MATCH"
    """Nothing permits this action. The deny-by-default outcome."""

    NO_SAFE_ACTION_AVAILABLE = "NO_SAFE_ACTION_AVAILABLE"
    """Every action the catalogue offers has been refused. The caregiver is asked what to do.

    The seventh code, added in session B, and the first that is **not** the reason a particular
    call was refused. It is the reason a *case* was parked: the planner has now proposed every tool
    there is and each was denied, so there is nothing left to replan toward.

    It earns its own code because it is the only one whose remedy lies outside the loop entirely.
    ``DESTINATION_NOT_TRUSTED`` says propose something else and ``INSUFFICIENT_FACT_TRUST`` says
    validate and retry; both address a planner that still has moves. This one says there are no
    moves, and reusing either would send the planner back into a cycle it has already exhausted.

    **It is a decision request, not a failure.** "Every available action was correctly refused" is
    the system working exactly as designed — on a poisoned case it is the best available outcome —
    and rendering it as ``FAILED`` would report a defence as a fault. ``FAILED`` keeps its meaning:
    something broke.
    """

    EXTERNAL_STATE_NOT_CONFIRMED = "EXTERNAL_STATE_NOT_CONFIRMED"
    """An effect was carried out and its result could not be confirmed. A person must check.

    The eighth code, and the second that parks a *case* rather than refusing a call — so like
    ``NO_SAFE_ACTION_AVAILABLE`` no rule carries it and the engine never emits it. The graph writes
    it onto the ``MANUAL_HANDOFF_REQUIRED`` entry
    :func:`careloop.graph.nodes._park_with_nothing_certifiable` appends.

    **Deliberately one code for two evidence states**, which is why it is named for what CareLoop
    knows rather than for what happened. An independent read that *succeeded and disagreed* and one
    that *could not establish anything* are different facts about the world, and
    :class:`careloop.graph.verify.Verification` collapses them on purpose; what they share is that
    CareLoop cannot certify the result, and that is exactly what this says. Naming it
    ``EXTERNAL_STATE_DIVERGED`` would be false for the unreadable case and
    ``EXTERNAL_SYSTEM_UNREACHABLE`` false for the divergent one. Splitting it is a Patch 2 question,
    to be answered beside the ``Verification`` discriminator a split would need, not before it.

    ``_NOT_CONFIRMED`` rather than ``_UNVERIFIED`` for the reason the two ``_NOT_TRUSTED`` codes are
    spelled that way: it states what the evidence failed to establish, where "unverified" reads as
    though nobody looked. Something did look, twice, and reported back.

    Reusing ``NO_SAFE_ACTION_AVAILABLE`` was the alternative and it would have put a false statement
    in an append-only trail: nothing was refused here, and the catalogue was never exhausted. An
    action was *carried out*, which is close to the opposite.
    """


#: The only strings that ever reach the model.
#:
#: Literals only. Adding an f-string, ``.format`` call or ``%`` interpolation to this mapping
#: reopens the injection channel that invariant 6 closes, and
#: ``tests/test_reason_purity.py`` fails the moment one appears.
REASON_TEXT: Final[Mapping[ReasonCode, str]] = {
    ReasonCode.DESTINATION_NOT_TRUSTED: (
        "Recipients must come from approved configuration. Propose a different action."
    ),
    # No remediation directive, and unlike the code below that is not a compromise: there is no
    # step the planner could take that would make this argument acceptable.
    ReasonCode.AUTHORITY_NOT_TRUSTED: (
        "Permissions and roles must come from approved configuration. "
        "They cannot be established from case evidence. Propose a different action."
    ),
    ReasonCode.REQUIRES_HUMAN_APPROVAL: (
        "This action needs the caregiver's decision. Stop and await it."
    ),
    ReasonCode.WRONG_CASE_STATE: (
        "The required evidence has not been gathered yet. Gather it first."
    ),
    # The remediation directive is the point of this code: validating and reproposing is exactly
    # what the planner should do. Arguments that no validator could rescue use
    # AUTHORITY_NOT_TRUSTED above rather than blunting this text.
    ReasonCode.INSUFFICIENT_FACT_TRUST: (
        "This argument is not confirmed well enough to be used. Confirm it first."
    ),
    ReasonCode.NO_RULE_MATCH: "This action is not permitted. Propose a different action.",
    # The only text here with no "propose a different action" directive, because by the time this
    # code is used there is no different action left to propose. Every other refusal points the
    # planner somewhere; this one points at the caregiver, and the run is over before the planner
    # would read it.
    ReasonCode.NO_SAFE_ACTION_AVAILABLE: (
        "No action available here can be carried out safely. A caregiver has been asked to decide."
    ),
    # Truthful for both evidence states, which is the constraint that shaped every word of it. It
    # says the result could not be confirmed, never that anything was reached or that anything
    # changed — the first would be false when a read succeeded and disagreed, the second when no
    # read succeeded at all. Like the code above it points at the caregiver rather than back at the
    # planner, because an effect has already happened and there is nothing left to propose about it.
    ReasonCode.EXTERNAL_STATE_NOT_CONFIRMED: (
        "The result of an action already carried out could not be confirmed to match it. A "
        "caregiver has been asked to check before anything further is done."
    ),
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
