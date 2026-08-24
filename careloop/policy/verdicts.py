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

    **Deliberately one code for several evidence states**, which is why it is named for what
    CareLoop knows rather than for what happened. An independent read that *succeeded and
    disagreed*, one the external system answered by saying it holds *no such record*, and one that
    *could not establish anything* are three different facts about the world; what they share is
    that CareLoop cannot certify the result, and that is exactly what this says. Naming it
    ``EXTERNAL_STATE_DIVERGED`` would be false for the unreadable case and
    ``EXTERNAL_SYSTEM_UNREACHABLE`` false for the divergent one.

    **The three are now told apart, and this code still does not split**, which is a decision rather
    than an omission. :class:`careloop.graph.verify.ObservationOutcome` records which evidence state
    occurred, on the ``VERIFICATION_FAILED`` entry, in a closed vocabulary. The two answer different
    questions: an outcome says *what the read produced*, and a reason code says *why CareLoop
    stopped and asked a person*. The answer to the second is identical in all three cases, so a
    second code here would be a distinction the lifecycle does not make, offered to a caregiver as
    though it did.

    ``_NOT_CONFIRMED`` rather than ``_UNVERIFIED`` for the reason the two ``_NOT_TRUSTED`` codes are
    spelled that way: it states what the evidence failed to establish, where "unverified" reads as
    though nobody looked. Something did look, twice, and reported back.

    Reusing ``NO_SAFE_ACTION_AVAILABLE`` was the alternative and it would have put a false statement
    in an append-only trail: nothing was refused here, and the catalogue was never exhausted. An
    action was *carried out*, which is close to the opposite.
    """

    NO_VALID_PROPOSAL_PRODUCED = "NO_VALID_PROPOSAL_PRODUCED"
    """No well-formed next action could be produced. The case needs manual handling.

    The ninth code, and the third that parks a *case* rather than refusing a call — so like the two
    above no rule carries it and the engine never emits it. The graph writes it onto the
    ``MANUAL_HANDOFF_REQUIRED`` entry
    :func:`careloop.graph.nodes._park_with_nothing_proposable` appends, at the one point where a
    proposal was asked for and the bounded retry in
    :func:`careloop.agents.invoke.proposal_tool_call` came back with nothing usable.

    **It is not a refusal, and that is why it is not one of the codes above.** Nothing was
    evaluated: no rule matched, no verdict was reached, no argument was inspected, and the policy
    engine was never called, because there was no call to judge. Reusing
    ``NO_SAFE_ACTION_AVAILABLE`` would state that every action in the catalogue was proposed and
    denied — on a path where *nothing* was proposed and *nothing* was denied — and would put a
    false statement in an append-only trail. That is the same argument
    ``EXTERNAL_STATE_NOT_CONFIRMED`` makes one code up, and it holds here for the opposite reason:
    that one covers an action that happened, this one covers an action that was never formed.

    **Deliberately one code for all four generation failures.** An answer carrying no tool call,
    one carrying several, one naming a tool that was not offered and one whose arguments do not
    satisfy the schema are four different facts about a provider — they are
    :data:`careloop.agents.invoke.RECOVERABLE_PROPOSAL_FAILURES`, and they are told apart in the
    logs and in the timeline diagnostic where the distinction is actionable. What they share is
    that CareLoop has no valid next action, which is the only part a caregiver or a lifecycle can
    do anything with. Splitting the code would offer a distinction the lifecycle does not make.

    **Named for what CareLoop knows rather than for what the provider did**, like the two codes
    above. ``MALFORMED_PROPOSAL`` would name one of the four and would put an implementation-level
    exception name into the product vocabulary.
    """

    EXTERNAL_OUTCOME_NOT_ESTABLISHED = "EXTERNAL_OUTCOME_NOT_ESTABLISHED"
    """Whether an authorised effect happened at all is not known, and cannot be settled here.

    The tenth code, and the fourth that parks a *case* rather than refusing a call — so like the
    three above no rule carries it and the engine never emits it. The graph writes it onto the
    ``MANUAL_HANDOFF_REQUIRED`` entry :func:`careloop.graph.nodes._park_with_nothing_established`
    appends, when reconciliation of a durably prepared action cannot determine what became of it and
    the action may not be re-attempted.

    **This is not ``EXTERNAL_STATE_NOT_CONFIRMED`` and the difference is what a caregiver does
    next.** That code means CareLoop holds a receipt — an effect was carried out, its result was
    recorded, and an independent read could not confirm that the world matches it. The instruction
    it implies is *check whether the appointment is right*. This one means CareLoop holds no receipt
    at all: an action was frozen, dispatched, and the process died or the provider went silent, so
    the honest statement is that nobody knows whether anything happened. The instruction it implies
    is *check whether an appointment exists*. Those are different errands, and a caregiver handed
    the wrong one wastes the trip.

    Reusing the other code would also put a false claim in an append-only trail, which is the
    argument every park code here has had to make. ``EXTERNAL_STATE_NOT_CONFIRMED``'s own text says
    "an action already carried out", and on this path whether it was carried out is precisely the
    open question.

    **One code for every way of not knowing**, for the reason ``EXTERNAL_STATE_NOT_CONFIRMED`` is
    one code for three evidence states. A provider that answered and refused, an action type that
    may never be autonomously re-attempted, a run with no channel to the provider, and a replay the
    policy engine declined are four different facts; what they share is that CareLoop cannot
    establish the outcome and will not guess, and that is the whole of what a caregiver or a
    lifecycle can act on.

    ``_NOT_ESTABLISHED`` rather than ``_UNKNOWN`` because it says what failed — an attempt to find
    out — where "unknown" reads as though nobody tried. Something did try, and reported nothing.
    """

    PATIENT_AUTHORITY_DENIED = "PATIENT_AUTHORITY_DENIED"
    """The people this case is about may not currently be acted for.

    The eleventh code, and the fifth that parks a *case* rather than refusing a call -- so like the
    four above no rule carries it and the engine never emits it. The graph writes it onto the
    ``MANUAL_HANDOFF_REQUIRED`` entry :func:`careloop.graph.nodes._park_with_no_patient_authority`
    appends, when a run is about to cause its **first** external effect and
    :meth:`careloop.graph.authority.PatientAuthority.allows` says no.

    **Why none of the other ten would do.** ``NO_SAFE_ACTION_AVAILABLE`` asserts that every tool in
    the catalogue was proposed and refused; here exactly one action was proposed and policy allowed
    it. ``EXTERNAL_STATE_NOT_CONFIRMED`` and ``EXTERNAL_OUTCOME_NOT_ESTABLISHED`` both assert that
    something was carried out; here nothing was, and nothing was even prepared. Every refusal code
    -- ``DESTINATION_NOT_TRUSTED`` and the rest -- is a verdict the policy engine reached about
    arguments, and this is not one: the arguments are fine and the *case* is not one this run may
    act on. Reusing any of them would put a false statement in an append-only trail, which is the
    argument each park code here has had to make.

    **It names the boundary and not the caller.** Not ``CAREGIVER_NOT_AUTHORISED``, not
    ``GRANT_REVOKED``, not anything mentioning a principal or a token: this vocabulary is read by a
    model, and invariant 6's rule is that a refusal reason must not describe the mechanism that
    produced it. It is also read by the graph, which is not allowed to know that caregivers exist --
    :mod:`careloop.graph.authority` deliberately speaks only of patients.

    **It is a *live* answer, not a stale one.** The question is asked at the execute node, after
    evidence has established who the case is about; a set of authorised patients computed when the
    run began would be a revocation that had not happened yet, and would also have been computed
    before there was a patient to compute it about. That was the defect this code exists for.
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
    # Says what happened and stops. It claims no refusal, because nothing was refused; it promises
    # no decision, because there is none to make; and unlike every other text here it gives the
    # planner no directive at all, since the run is over before this could be read and there is no
    # step it could take that would form a proposal it has already failed twice to form.
    ReasonCode.NO_VALID_PROPOSAL_PRODUCED: (
        "No valid next action could be formed here. CareLoop has stopped without doing anything "
        "and the case needs manual handling."
    ),
    # Says that the outcome is unknown and stops. It deliberately does not say the action happened
    # — that is the open question — and it deliberately does not say it did not, which is the error
    # that would invite a second attempt. Like the two codes above it points at the caregiver, and
    # like the one above it gives the planner no directive: an effect may already exist, so there is
    # nothing safe for a model to propose about it.
    ReasonCode.EXTERNAL_OUTCOME_NOT_ESTABLISHED: (
        "Whether an action CareLoop was authorised to carry out actually happened could not be "
        "established. A caregiver has been asked to check before anything further is done."
    ),
    # Says that this case may not be acted on and stops. It names no caregiver, no grant and no
    # mechanism -- a model reading this must not learn what decided it, and the graph that writes
    # it does not know either. Like the three codes above it points at a person and gives the
    # planner no directive: no argument it could change would make this case actionable.
    ReasonCode.PATIENT_AUTHORITY_DENIED: (
        "This case is not one CareLoop may currently act on. Nothing has been done and a person "
        "has been asked to take it from here."
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
