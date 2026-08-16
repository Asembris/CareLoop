"""What "verified" means, per action type.

``DONE`` is reachable only from ``VERIFYING``, which makes "the case did not finish via the unsafe
path" a structural property rather than an observation. That is worth nothing unless ``VERIFYING``
does something, and open decision 5 left what it does undefined. This module defines it.

**Verified means: the effect that was performed is the effect that was authorised — and, where
there is a real external system, that the system's own record of it agrees.**

The first half checks the gap that always exists in an authority system: between what the policy
engine approved and what the executor did. Three things, in order:

1. The idempotency key re-derived from the authorised arguments equals the key the action was
   recorded under. A mismatch means the call that happened is not the call that was decided.
2. The recorded argument digest matches. Same property, arrived at independently, and it holds
   even if the key derivation is what is broken.
3. A per-action-type predicate over the typed result: every field the result echoes back equals
   the argument that was authorised, and the synthetic identifier is present.

Step 3 is the one that would catch an executor returning somebody else's confirmation. On its own
it is still closed-loop: CareLoop's executor checked against CareLoop's approval, with CareLoop's
own receipt as the evidence.

**Step 4 closes that, for the one action type that has a real external system behind it.** When
the caller supplies ``observed`` — state fetched by a *separate* request to the external system,
never the response the effect itself returned — the same per-action-type predicate runs against it.
So a booking is verified against what the clinic holds now, not against what the clinic said at the
time. That is the difference between reading the world back and re-reading a receipt, and
``tests/test_verify_readback.py`` proves it by changing the clinic's record after the booking and
requiring verification to fail.

Where there is no external system — the three simulated effects — ``observed`` is ``None`` and
verification means exactly what it meant before, which is stated on the ``Verification`` itself so
the two cannot be confused in a timeline.

:data:`VERIFIERS` is checked against ``REGISTRY.external_effect_tools`` at import, the same way the
executor's handlers and the tool bodies are. An effectful tool with no verifier is a tool whose
case could reach ``DONE`` without anything having been checked, so it fails on import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.state.case import ActionRecord
from careloop.tools.idempotency import args_digest, canonical_args, idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import (
    AccessGranted,
    ActionResult,
    AppointmentBooked,
    DocumentUploaded,
    EmailSent,
)


class ObservationOutcome(StrEnum):
    """What the independent read of the external system established. A closed vocabulary.

    **Five answers to one question**, and the question is deliberately narrow: *what did the
    observation produce?* It is not *may this action be certified?* — that is
    :attr:`Verification.passed`, which also weighs the closed-loop checks against what policy
    authorised. Keeping them apart is what lets a timeline say "the clinic agrees with us and the
    effect we recorded is still not the one that was decided", which is a real state and was
    previously unsayable.

    Before this vocabulary existed the graph could distinguish exactly two of these, and only for
    the width of one ``except`` clause. ``ClinicClient._request`` raised one ``ClinicRefused`` for
    every status from 400 to 599, so a ``404`` on the exact confirmation id and a ``503`` were the
    same object; :meth:`careloop.graph.nodes.VerifyNode._verify` then folded whatever arrived into
    ``passed=False`` beside a genuine disagreement. Four different facts about the world reached the
    audit trail as one boolean and a paragraph of free text.

    **Nothing here is a lifecycle instruction.** Every failing member parks the case identically,
    exactly as before — see ``tests/test_post_effect_lifecycle.py``. What a member says is what
    CareLoop *knows*, and deciding what to do about each kind of knowledge is a separate question
    that this enum makes askable rather than answers.
    """

    MATCHED = "MATCHED"
    """An independent read succeeded and every compared field equals what CareLoop executed."""

    DIVERGED = "DIVERGED"
    """An independent read succeeded and one or more compared fields differ.

    **Positive evidence of disagreement.** The external system answered, the answer was a valid
    typed observation, and it does not match. Retrying the read would ask a deterministic service
    the same question twice.
    """

    ABSENT = "ABSENT"
    """The external system answered that it holds no record of the effect.

    Also positive evidence, and the member most easily confused with :attr:`UNAVAILABLE`. The
    clinic minted the confirmation id, stored it and returned it; a later read of *that exact id*
    answering ``404`` is the clinic contradicting the receipt CareLoop holds. Distinguished from a
    disagreement about field values because it is a different fact — there is nothing to compare,
    rather than something that compared badly.
    """

    UNAVAILABLE = "UNAVAILABLE"
    """CareLoop attempted an independent read and could not establish authoritative state.

    Absence of knowledge, not knowledge of absence. Nothing answered, or what answered could not
    serve the read. **This member says nothing about whether a later attempt would fare better** —
    that is a separate judgement about the *cause*, and Patch 2A does not make it. The underlying
    exception is preserved so it can be made later without re-deriving anything.
    """

    NOT_OBSERVED = "NOT_OBSERVED"
    """No independent observation took part in this verdict.

    A statement about CareLoop's own wiring rather than about the world: the three simulated
    effects have no external system to read, so verification means what it meant before the
    read-back existed. **Must never be confused with** :attr:`UNAVAILABLE`, which claims an
    external system was consulted and did not answer — a claim about the world made by a run that
    never touched one.
    """


class Verification(BaseModel):
    """The outcome of verifying one action."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    diagnostic: str
    """Timeline-only. Names fields and may quote values; never returned to a model.

    Values read back from an external system land here and nowhere else. That is deliberate: the
    diagnostic is withheld wholesale by the API and is never rendered into a prompt, so text
    CareLoop does not control cannot become an instruction. See
    ``tests/test_verify_readback.py::TestTheReadBackIsNotAnInjectionChannel``.

    **Nothing reads this to decide anything.** :attr:`outcome` is chosen by typed control flow, so
    a clinic whose failure message claims the observation matched cannot make it so —
    ``tests/test_observation_outcome.py::TestTheOutcomeIsNotReadOutOfText``.
    """

    externally_observed: bool
    """Whether an independent read of the external system took part in this verdict.

    Carried rather than inferred, because "verified" is a materially stronger claim when it is
    true and a timeline that cannot tell the two apart would let the weaker one be read as the
    stronger. It reaches the API as the ``read_back`` payload key.

    **Attempted, not succeeded**, and that meaning is unchanged by this session: a read that was
    made and failed still took part in the verdict. :attr:`outcome` is what says how it went, and
    redefining this field to mean something narrower would be a compatibility decision of its own
    rather than a clarification.
    """

    outcome: ObservationOutcome
    """What the independent read established, as a closed vocabulary.

    Required, with no default, for the reason every result field in
    :mod:`careloop.tools.results` is: a default would let a construction site decline to decide and
    have the model answer for it. There are four construction sites and each one names its outcome.
    """

    @property
    def failed(self) -> bool:
        """Whether verification refused the action."""
        return not self.passed


#: A verifier compares the canonical authorised arguments against the typed result.
Verifier = Callable[[Mapping[str, str], ActionResult], list[str]]
"""Returns the list of mismatches. Empty means verified."""

#: A read-back fetches the external system's *current* record of a performed effect.
ReadBack = Callable[[ActionResult], ActionResult]
"""Takes the recorded result — for its identifier — and returns what the system holds now."""


class ReadBackFailed(Exception):
    """Raised when the external system could not be read, or does not hold the effect.

    Both are verification failures rather than crashes, and both fail closed: an effect the
    external system cannot show CareLoop is an effect CareLoop cannot claim happened.

    **The two subclasses below are what a read-back must raise.** This base still resolves to an
    outcome so that handling stays total, and it resolves to the *weaker* claim on purpose:
    :attr:`ObservationOutcome.ABSENT` asserts the external system gave a definite answer, and a
    failure nobody classified must not be able to make that assertion by omission.
    """

    outcome: ObservationOutcome = ObservationOutcome.UNAVAILABLE
    """The evidence class this failure means. Read from the *type*, never from the message."""


class ObservationAbsent(ReadBackFailed):
    """The external system answered that it holds no record of the effect."""

    outcome = ObservationOutcome.ABSENT


class ObservationUnavailable(ReadBackFailed):
    """CareLoop could not obtain authoritative state from the external system."""

    outcome = ObservationOutcome.UNAVAILABLE


def _mismatches(
    result: ActionResult,
    args: Mapping[str, str],
    fields: Mapping[str, str],
    identifier: str,
) -> list[str]:
    """Compare each ``result`` field against the argument it echoes, plus the synthetic id."""
    found = [
        f"{field} was authorised as {args.get(arg_name)!r} but the result carries "
        f"{getattr(result, field)!r}"
        for field, arg_name in fields.items()
        if getattr(result, field) != args.get(arg_name)
    ]
    if not getattr(result, identifier, ""):
        found.append(f"{identifier} is empty; the executor recorded no identifier for the effect")
    return found


def _verify_email(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, EmailSent):
        return [f"expected EmailSent, the action recorded a {type(result).__name__}"]
    return _mismatches(result, args, {"to": "to", "subject": "subject"}, "message_id")


def _verify_booking(args: Mapping[str, str], result: ActionResult) -> list[str]:
    """Every declared booking argument, compared the same way.

    ``reason`` joined the other three this session. It carries no ``trust_key`` and no rule requires
    its trust, so what it is compared against is *the exact value CareLoop executed* rather than
    "the value policy authorised" — the whole call passed policy, and this argument has no trust
    predicate of its own. That distinction is why it belongs here and not in ``rules.yaml``:
    completion-time fidelity to an external record is a different question from authorisation-time
    provenance, and this map answers only the first.
    """
    if not isinstance(result, AppointmentBooked):
        return [f"expected AppointmentBooked, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result,
        args,
        {
            "provider_id": "provider_id",
            "patient_id": "patient_id",
            "slot_start": "slot_start",
            "reason": "reason",
        },
        "confirmation_id",
    )


def _verify_upload(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, DocumentUploaded):
        return [f"expected DocumentUploaded, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result, args, {"portal_id": "portal_id", "document_id": "document_id"}, "upload_id"
    )


def _verify_grant(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, AccessGranted):
        return [f"expected AccessGranted, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result,
        args,
        {"grantee": "grantee", "role": "role", "portal_id": "portal_id"},
        "grant_id",
    )


VERIFIERS: Final[Mapping[str, Verifier]] = {
    "send_email": _verify_email,
    "book_appointment": _verify_booking,
    "upload_document": _verify_upload,
    "grant_portal_access": _verify_grant,
}

#: The typed result each action type produces.
#:
#: :class:`~careloop.state.case.ActionRecord` stores its result as JSON text, because the store is
#: a key-value document store and a durable record has to survive as bytes. Rehydrating it needs
#: the type back, and reading the type name *out of* the stored document would let the document
#: choose which class to instantiate. This table is the trusted side of that decision: the action
#: type comes from the case's own record of what was authorised.
RESULT_TYPES: Final[Mapping[str, type[ActionResult]]] = {
    "send_email": EmailSent,
    "book_appointment": AppointmentBooked,
    "upload_document": DocumentUploaded,
    "grant_portal_access": AccessGranted,
}


#: Action types with a real external system that can be read back independently.
#:
#: **Capability, not availability.** This says an action type *can* be observed by something other
#: than CareLoop's own receipt; whether a particular run has that observer bound is
#: ``RunContext.readbacks``, and the two are different questions. A case may be structurally
#: re-checkable while a misconfigured run has no clinic bound at all — see
#: :class:`careloop.graph.nodes.VerifyNode`, which fails closed on exactly that.
#:
#: Deliberately a **subset** of ``REGISTRY.external_effect_tools`` rather than equal to it. Three of
#: the four effect tools are simulated on purpose — one action made genuinely external is the proof
#: and four is scope creep with a rationale — so requiring equality would either force three fake
#: observers into existence or make this declaration unmaintainable. What it must never do is name
#: a tool the registry does not have, which is what :func:`check_observable_tools_are_declared`
#: refuses.
#:
#: It lives here, beside :data:`VERIFIERS` and :data:`RESULT_TYPES`, because this module already
#: owns what verification means per action type. It deliberately does **not** live in
#: ``runner.py``, in the state machine or in an API route: a tool name in lifecycle code is the
#: thing ``check_verifiers_cover_the_registry`` exists to prevent one layer down.
INDEPENDENTLY_OBSERVABLE: Final[frozenset[str]] = frozenset({"book_appointment"})


class UnverifiableAction(Exception):
    """Raised at import when an external-effect tool has no verifier or no result type."""


def rehydrate(action_type: str, recorded: str) -> ActionResult:
    """Rebuild the typed result an :class:`~careloop.state.case.ActionRecord` holds as JSON.

    Raises:
        UnverifiableAction: if the action type has no declared result type.
    """
    result_type = RESULT_TYPES.get(action_type)
    if result_type is None:  # pragma: no cover - the import-time check keeps this unreachable
        raise UnverifiableAction(f"no result type for action type {action_type!r}")
    return result_type.model_validate_json(recorded)


def check_verifiers_cover_the_registry() -> None:
    """Fail at import if the verifiers and the registry's effectful tools disagree.

    An effectful tool with no verifier is a tool whose case could pass through ``VERIFYING``
    without anything being checked and reach ``DONE`` anyway, which would make invariant 4's
    structural claim hollow. A verifier for a tool the registry does not declare is dead code
    pretending to be a guard.
    """
    declared = REGISTRY.external_effect_tools
    verified = frozenset(VERIFIERS)
    if verified != declared:
        raise UnverifiableAction(
            f"verifiers disagree with the registry: "
            f"declared but unverifiable {sorted(declared - verified)}, "
            f"verifiable but undeclared {sorted(verified - declared)}"
        )

    typed = frozenset(RESULT_TYPES)
    if typed != declared:
        raise UnverifiableAction(
            f"result types disagree with the registry: "
            f"declared but untyped {sorted(declared - typed)}, "
            f"typed but undeclared {sorted(typed - declared)}"
        )


def check_observable_tools_are_declared() -> None:
    """Fail at import if :data:`INDEPENDENTLY_OBSERVABLE` names a tool the registry does not have.

    A **subset** check rather than an equality one, unlike the two above, and the asymmetry is the
    point: a verifier is required for every effectful tool because a tool that could reach ``DONE``
    unchecked would make invariant 4 hollow, whereas an independent observer exists only where
    there is a real external system to observe. Three tools deliberately have none.

    What a superset would mean is worse than untidy: a re-check accepted for an action type nothing
    can observe would run a verification episode that reads nothing, which is the false-``DONE``
    hazard this declaration exists to close.

    Raises:
        UnverifiableAction: naming every declared type the registry does not carry.
    """
    if unknown := sorted(INDEPENDENTLY_OBSERVABLE - REGISTRY.external_effect_tools):
        raise UnverifiableAction(
            f"independently observable action type(s) {unknown} are not effect tools the registry "
            f"declares; a capability naming nothing cannot be observed"
        )


check_verifiers_cover_the_registry()
check_observable_tools_are_declared()


def verify_action(
    *,
    case_id: str,
    action_type: str,
    args: Mapping[str, object],
    record: ActionRecord,
    result: ActionResult,
    observed: ActionResult | None = None,
) -> Verification:
    """Verify that ``record`` and ``result`` are the action ``args`` authorised.

    ``args`` are the arguments the policy engine allowed, not the arguments the executor was
    handed — comparing the executor's inputs to the executor's outputs would verify nothing about
    authority. They come from the approved proposal.

    ``observed`` is the external system's own record, fetched independently. When it is supplied
    the same per-action-type predicate runs against it as well, so every field the effect echoes
    is checked twice: once against what CareLoop recorded, and once against what the world holds.
    """
    problems: list[str] = []

    expected_key = idempotency_key(case_id, action_type, args)
    if expected_key != record.idempotency_key:
        problems.append(
            f"the authorised arguments key to {expected_key[:16]}... but the action was recorded "
            f"under {record.idempotency_key[:16]}...; the call that happened is not the call that "
            f"was decided"
        )

    expected_digest = args_digest(args)
    if expected_digest != record.args_digest:
        problems.append(
            f"argument digest {expected_digest[:16]}... does not match the recorded "
            f"{record.args_digest[:16]}..."
        )

    if record.action_type != action_type:
        problems.append(
            f"the authorised action was {action_type!r}; the record carries {record.action_type!r}"
        )

    verifier = VERIFIERS.get(action_type)
    outcome = ObservationOutcome.NOT_OBSERVED
    if verifier is None:
        # Unreachable while the import-time check holds. Fail closed rather than pass by omission,
        # and claim no observation: nothing was compared, whatever was read.
        problems.append(f"no verifier for action type {action_type!r}")
    else:
        canonical = canonical_args(args)
        problems.extend(verifier(canonical, result))
        if observed is not None:
            disagreements = verifier(canonical, observed)
            problems.extend(
                f"read back from the external system: {problem}" for problem in disagreements
            )
            # **The outcome describes the observation, not the verdict.** A closed-loop problem —
            # a key that does not re-derive, a digest that does not match — fails verification
            # without the external record having disagreed about anything, and reporting that as
            # DIVERGED would put a disagreement with the world into the trail that never happened.
            outcome = ObservationOutcome.DIVERGED if disagreements else ObservationOutcome.MATCHED

    if problems:
        return Verification(
            passed=False,
            diagnostic="; ".join(problems),
            externally_observed=observed is not None,
            outcome=outcome,
        )
    if observed is not None:
        return Verification(
            passed=True,
            externally_observed=True,
            outcome=outcome,
            diagnostic=(
                f"{action_type} verified against the external system: the recorded effect keys to "
                f"the authorised arguments, and a separate read of the external system's own "
                f"record matches what policy allowed"
            ),
        )
    return Verification(
        passed=True,
        externally_observed=False,
        outcome=ObservationOutcome.NOT_OBSERVED,
        diagnostic=(
            f"{action_type} verified: the recorded effect keys to the authorised arguments and "
            f"every field it echoes matches what policy allowed. No external system was read — "
            f"this closes the loop between decision and execution, not between execution and the "
            f"world"
        ),
    )


__all__ = [
    "INDEPENDENTLY_OBSERVABLE",
    "RESULT_TYPES",
    "VERIFIERS",
    "ObservationAbsent",
    "ObservationOutcome",
    "ObservationUnavailable",
    "ReadBack",
    "ReadBackFailed",
    "UnverifiableAction",
    "Verification",
    "Verifier",
    "check_observable_tools_are_declared",
    "check_verifiers_cover_the_registry",
    "rehydrate",
    "verify_action",
]
