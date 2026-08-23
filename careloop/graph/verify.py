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
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.state.case import ActionRecord
from careloop.tools.idempotency import args_digest, canonical_args, idempotency_key
from careloop.tools.registry import REGISTRY, ToolRegistry
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


#: A verifier compares the **expectations** for one action against the typed result.
Verifier = Callable[[Mapping[str, str], ActionResult], list[str]]
"""Returns the list of mismatches. Empty means verified.

The mapping was the canonical authorised arguments and is now :func:`expectations` — those same
arguments plus any :class:`DerivedExpectation` the action type declares. For every action type
CareLoop ships the two are identical, because nothing declares one yet.
"""


@dataclass(frozen=True, slots=True)
class DerivedExpectation:
    """One value a verifier compares that **no argument supplies**.

    **Why this is a separate concept rather than another argument.** An argument is something a
    proposal carries, policy resolves trust for, an approval is keyed on, an idempotency key may
    cover, and a provider is sent. A derived expectation is none of those: it is computed from
    trusted or controlled state CareLoop already holds, at verification time, purely to be compared
    against what an external system turned out to hold. Making one an argument to reach it would
    put a value into authority, identity and the outbound payload in order to solve a question
    about scrutiny — which is the fold this repository has closed several times in the other
    direction.

    **It is emphatically not part of the authorised effect state.** The field list and the state
    digest a :class:`careloop.graph.witness.EffectClaim` carries are taken over
    :attr:`ObservableEffect.state_fields`, which
    :func:`check_observable_state_matches_verification` requires to be argument-sourced and free of
    every name declared here. So a richer verifier cannot silently redefine what a settlement
    digest is a digest *of*. A verifier may check more than the effect claim describes; the effect
    claim may never quietly grow because a verifier did.

    The attribute *names* those two become are spelled in the deployment adapter and nowhere in
    this package, which is why they are described here rather than quoted.

    ``derive`` is a pure function of the canonical authorised arguments. Returning ``None`` means
    *no expectation could be established*, which leaves the name absent from the mapping and makes
    every comparison against it mismatch — the fail-closed direction, and the same one
    :class:`careloop.tools.results.AppointmentBooked` refuses a default for.
    """

    name: str
    """What the comparison map refers to this expectation by. Never a declared argument name."""

    derive: Callable[[Mapping[str, str]], str | None]
    """Canonical authorised arguments in, one canonical expected value out, or ``None``."""


#: The derived expectations each action type declares, in comparison order.
#:
#: **Deliberately empty.** Nothing CareLoop ships today compares anything an argument does not
#: supply, so an entry here would be a guarantee that is not being made. The seam exists because
#: the alternative — reaching for it the day a provider needs one, under implementation pressure —
#: is how a derived value ends up as an argument, in the authority path and in the state digest.
#:
#: ``tests/test_derived_expectations.py`` drives it against synthetic action types, so the
#: mechanism is proven rather than merely declared.
DERIVED_EXPECTATIONS: Final[Mapping[str, tuple[DerivedExpectation, ...]]] = {}


def derived_names(
    action_type: str, declared: Mapping[str, tuple[DerivedExpectation, ...]] | None = None
) -> frozenset[str]:
    """Every expectation name for ``action_type`` that no argument supplies."""
    table = DERIVED_EXPECTATIONS if declared is None else declared
    return frozenset(expectation.name for expectation in table.get(action_type, ()))


def expectations(action_type: str, canonical: Mapping[str, str]) -> dict[str, str]:
    """What a verifier for ``action_type`` compares against.

    Every canonical authorised argument, plus every :class:`DerivedExpectation` the type declares.
    Arguments go in first and a derived expectation can never displace one:
    :func:`check_derived_expectations_do_not_shadow_arguments` refuses that at import, so a derived
    value cannot stand in for something policy actually allowed.

    A derivation answering ``None`` contributes no entry, which makes anything compared against it
    mismatch and fail the verification. That is the fail-closed reading — an expectation nobody
    could establish is not a licence to skip the comparison.
    """
    expected = dict(canonical)
    for expectation in DERIVED_EXPECTATIONS.get(action_type, ()):
        value = expectation.derive(canonical)
        if value is not None:
            expected[expectation.name] = value
    return expected


#: A read-back fetches the external system's *current* record of a performed effect.
ReadBack = Callable[[ActionResult], ActionResult]
"""Takes the recorded result — **for its identifier, and for nothing else** — and returns what the
system holds now.

Three requirements, and the first is the one the whole arrangement rests on.

**The returned values must come from a fresh request.** Not from the response the effect itself
produced, not from anything CareLoop stored, and not from the argument it was handed beyond reading
the resource identifier out of it. A read-back that echoes its input verifies that CareLoop agrees
with CareLoop. ``tests/test_verify_readback.py::TestTheIndependenceProof`` is what fails when the
clinic's does, and ``tests/test_content_integrity.py`` holds the generic form.

**An observed value may be *derived from the content the provider returned*, not only echoed from
metadata it reports.** For a record-shaped provider the two coincide: the clinic states a patient
id and the read-back carries it across. For a content-shaped provider they do not, and derivation
is the stronger evidence — bytes fetched back and reduced by
:func:`careloop.tools.content.content_digest` are a fact this process established about what the
provider is holding, where a digest the provider *reports* is a claim to be taken on trust. Where
both are available, prefer the one computed here.

**What it may not do is decide anything.** A read-back returns an observation or raises a
:class:`ReadBackFailed`; the verdict belongs to :func:`verify_action` and the lifecycle to
:class:`careloop.graph.nodes.VerifyNode`. In particular it never consults the write's status, its
receipt, or whether the effect "should" be there.
"""


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
    expected: Mapping[str, str],
    fields: Mapping[str, str],
    identifier: str,
    derived: frozenset[str] = frozenset(),
) -> list[str]:
    """Compare each ``result`` field against its expectation, plus the synthetic id.

    ``fields`` maps a result field to the *expectation name* it is compared against. For an
    argument-sourced expectation that name is the argument's; for a derived one it is
    :attr:`DerivedExpectation.name`, and ``derived`` is what tells the two apart.

    **The two report differently, and that is load-bearing rather than cosmetic.** "authorised as"
    is a claim that policy allowed this exact value, and it is untrue of a value no proposal
    carried. It is also how ``tests/test_evaluator_drift.py`` and ``tests/test_witness_seam.py``
    derive the field universe the settlement digests are taken over — by running a verifier against
    an empty expectation map and reading the phrase back — so a derived expectation borrowing the
    same wording would quietly enlarge a frozen telemetry contract from inside a diagnostic string.
    """
    found = [
        f"{field} was {'derived as' if name in derived else 'authorised as'} "
        f"{expected.get(name)!r} but the result carries {getattr(result, field)!r}"
        for field, name in fields.items()
        if getattr(result, field) != expected.get(name)
    ]
    if not getattr(result, identifier, ""):
        found.append(f"{identifier} is empty; the executor recorded no identifier for the effect")
    return found


#: Which email result field is compared against which authorised argument.
#:
#: ``body`` is deliberately absent: :class:`~careloop.tools.results.EmailSent` does not echo it, so
#: there is nothing to compare it against. That absence is now load-bearing rather than incidental —
#: :func:`check_non_identifying_arguments_are_verified` reads this table, so ``send_email.body``
#: cannot be declared non-identifying while nothing checks it.
_EMAIL_COMPARISON: Final[Mapping[str, str]] = {"to": "to", "subject": "subject"}

_EMAIL_IDENTIFIER: Final[str] = "message_id"


def _verify_email(expected: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, EmailSent):
        return [f"expected EmailSent, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result, expected, _EMAIL_COMPARISON, _EMAIL_IDENTIFIER, derived_names("send_email")
    )


#: Which booking result field is compared against which authorised argument.
#:
#: Extracted from the body of :func:`_verify_booking` rather than restated: it is now read by
#: :func:`check_observable_state_matches_verification` as well, and two spellings of "what the
#: clinic holds" is exactly the drift :data:`OBSERVABLE_STATE` exists to make impossible. The
#: **order is load-bearing** — :func:`_mismatches` iterates it, so it decides the order of the
#: diagnostics a timeline records — and is therefore left exactly as it was rather than sorted.
_BOOKING_COMPARISON: Final[Mapping[str, str]] = {
    "provider_id": "provider_id",
    "patient_id": "patient_id",
    "slot_start": "slot_start",
    "reason": "reason",
}

#: The clinic's own identifier for a booking, and the only thing a read-back can be keyed on.
_BOOKING_IDENTIFIER: Final[str] = "confirmation_id"


def _verify_booking(expected: Mapping[str, str], result: ActionResult) -> list[str]:
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
        expected,
        _BOOKING_COMPARISON,
        _BOOKING_IDENTIFIER,
        derived_names("book_appointment"),
    )


_UPLOAD_COMPARISON: Final[Mapping[str, str]] = {
    "portal_id": "portal_id",
    "document_id": "document_id",
}

_UPLOAD_IDENTIFIER: Final[str] = "upload_id"


def _verify_upload(expected: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, DocumentUploaded):
        return [f"expected DocumentUploaded, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result, expected, _UPLOAD_COMPARISON, _UPLOAD_IDENTIFIER, derived_names("upload_document")
    )


_GRANT_COMPARISON: Final[Mapping[str, str]] = {
    "grantee": "grantee",
    "role": "role",
    "portal_id": "portal_id",
}

_GRANT_IDENTIFIER: Final[str] = "grant_id"


def _verify_grant(expected: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, AccessGranted):
        return [f"expected AccessGranted, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result, expected, _GRANT_COMPARISON, _GRANT_IDENTIFIER, derived_names("grant_portal_access")
    )


#: Every argument each effect tool's verifier compares, keyed by tool name.
#:
#: The values of each map are argument names; the keys are the result fields they are compared
#: against. Assembled from the four comparison tables rather than restated, so a verifier that stops
#: comparing something cannot leave this table saying it still does.
#:
#: Its one reader is :func:`check_non_identifying_arguments_are_verified`. That is the coupling the
#: whole identity declaration rests on: an argument may leave the idempotency key only if something
#: independent still checks it afterwards, so narrowing identity can never be a way of narrowing
#: scrutiny.
def compared_arguments(
    action_type: str,
    comparison: Mapping[str, str],
    declared: Mapping[str, tuple[DerivedExpectation, ...]] | None = None,
) -> frozenset[str]:
    """The expectation names in ``comparison`` that an **argument** supplies.

    A comparison map refers to expectations, and since :class:`DerivedExpectation` exists those are
    two populations rather than one. Every reader below wants the argument half specifically —
    :func:`check_non_identifying_arguments_are_verified` asks whether an argument dropped from the
    idempotency key is still checked, and a derived name is not an answer to that; and
    :data:`OBSERVABLE_STATE` is indexed into the authorised arguments by
    :func:`careloop.graph.witness._authorised_state`, so a derived name there would look up an
    argument nothing supplies.

    Filtering rather than requiring a second declaration, so a comparison map stays the one place a
    tool says what it compares.
    """
    derived = derived_names(action_type, declared)
    return frozenset(name for name in comparison.values() if name not in derived)


COMPARED_ARGUMENTS: Final[Mapping[str, frozenset[str]]] = {
    "send_email": compared_arguments("send_email", _EMAIL_COMPARISON),
    "book_appointment": compared_arguments("book_appointment", _BOOKING_COMPARISON),
    "upload_document": compared_arguments("upload_document", _UPLOAD_COMPARISON),
    "grant_portal_access": compared_arguments("grant_portal_access", _GRANT_COMPARISON),
}

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


@dataclass(frozen=True, slots=True)
class ObservableEffect:
    """The externally held state of one action type, named so it can be digested and compared.

    :attr:`state_fields` is the **joint state universe**: the fields the external system holds and
    the read-back compares. It is deliberately not the registry's argument set — the registry says
    what CareLoop may *send*, and the two answer different questions even where they coincide, as
    ``tests/test_evaluator_drift.py`` says in as many words. Anything digesting "the state" has to
    follow this one, or an argument that is sent but not externally held would silently make two
    digests incomparable.

    Sorted, so that a field list rendered from it has one canonical spelling and no consumer has to
    normalise. Deduplicated, so a repeated field cannot be absorbed by a set conversion downstream.

    :attr:`resource_id_field` names the identifier the external system minted, which is what a
    later independent read is keyed on.
    """

    state_fields: tuple[str, ...]
    resource_id_field: str


#: What each independently observable action type compares against the external system.
#:
#: The bridge between :data:`INDEPENDENTLY_OBSERVABLE` and the per-type verifier, read only by
#: :func:`check_observable_state_matches_verification`. An entry is required for every observable
#: type, so adding one without saying what it compares fails at import rather than producing a
#: state declaration nothing corroborates.
_EXTERNAL_COMPARISONS: Final[Mapping[str, tuple[Mapping[str, str], str]]] = {
    "book_appointment": (_BOOKING_COMPARISON, _BOOKING_IDENTIFIER),
}

#: The externally held state of every independently observable action type.
#:
#: **Declared once and checked against verification at import**, because it is now read by two
#: consumers that must not disagree: the read-back already compares these fields, and
#: :mod:`careloop.graph.witness` digests them so that what CareLoop executed and what the world
#: holds can be compared by something outside this process. A digest universe that drifted from the
#: comparison universe would not raise anywhere — it would quietly make two digests that were never
#: about the same thing, and their inequality would be reported as a divergence that never
#: happened.
#:
#: It lives here for the reason :data:`INDEPENDENTLY_OBSERVABLE` does: this module already owns what
#: verification means per action type, and a field universe in a telemetry adapter would be a
#: domain fact stored where no domain test would look at it.
OBSERVABLE_STATE: Final[Mapping[str, ObservableEffect]] = {
    "book_appointment": ObservableEffect(
        state_fields=("patient_id", "provider_id", "reason", "slot_start"),
        resource_id_field=_BOOKING_IDENTIFIER,
    ),
}


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


def check_observable_state_matches_verification() -> None:
    """Fail at import if the declared external state disagrees with what verification compares.

    Four separate disagreements, each of which would be silent and none of which would raise on its
    own:

    A type in :data:`OBSERVABLE_STATE` that is not in :data:`INDEPENDENTLY_OBSERVABLE`, or the
    reverse — a state universe for something nothing observes, or an observable action whose state
    nobody declared. An **equality** rather than a subset, unlike
    :func:`check_observable_tools_are_declared`: both directions are defects here, because both
    consumers index this table by exactly the observable types.

    A type with no entry in :data:`_EXTERNAL_COMPARISONS` — a declaration nothing corroborates.

    A field set that is not exactly the **argument-sourced** half of what the verifier compares.
    This is the one that matters: the read-back's comparison universe is the definition of the
    externally held state, and a digest taken over a different set would produce an inequality that
    means nothing about the world.

    A field that is a :class:`DerivedExpectation` name. That is the same defect said in the
    direction this session opened: :func:`careloop.graph.witness._authorised_state` indexes these
    names into the arguments policy authorised, so a derived name would either resolve to nothing
    and silently suppress the whole effect claim, or — worse, if it ever did resolve — enlarge what
    the claim's state digest is a digest of. A verifier may check more than the effect claim
    describes; the effect claim may never grow because a verifier did.

    A resource identifier that is not the one the verifier requires, or a field tuple that is
    unsorted or carries a duplicate — both of which would make the rendered field list depend on
    how it happened to be written down.

    Raises:
        UnverifiableAction: naming the disagreement.
    """
    declared = frozenset(OBSERVABLE_STATE)
    if declared != INDEPENDENTLY_OBSERVABLE:
        raise UnverifiableAction(
            f"the declared external state and the observable action types disagree: "
            f"observable but undeclared {sorted(INDEPENDENTLY_OBSERVABLE - declared)}, "
            f"declared but unobservable {sorted(declared - INDEPENDENTLY_OBSERVABLE)}"
        )

    for action_type, observable in OBSERVABLE_STATE.items():
        comparison = _EXTERNAL_COMPARISONS.get(action_type)
        if comparison is None:
            raise UnverifiableAction(
                f"action type {action_type!r} declares an external state that no verifier "
                f"comparison corroborates; a state universe nothing compares is undefined"
            )
        compared, identifier = comparison
        if leaked := sorted(frozenset(observable.state_fields) & derived_names(action_type)):
            raise UnverifiableAction(
                f"the external state declared for {action_type!r} names derived expectation(s) "
                f"{leaked}; the authorised effect state is what CareLoop was allowed to send, and "
                f"a value no argument supplies cannot be part of it"
            )
        arguments = compared_arguments(action_type, compared)
        if frozenset(observable.state_fields) != arguments:
            raise UnverifiableAction(
                f"the external state declared for {action_type!r} is not the argument-sourced half "
                f"of what verification compares: declared {sorted(observable.state_fields)}, "
                f"compared {sorted(arguments)}"
            )
        if observable.state_fields != tuple(sorted(set(observable.state_fields))):
            raise UnverifiableAction(
                f"the external state declared for {action_type!r} must be sorted and free of "
                f"duplicates so its rendered field list has one spelling; it is "
                f"{list(observable.state_fields)}"
            )
        if observable.resource_id_field != identifier:
            raise UnverifiableAction(
                f"the resource identifier declared for {action_type!r} is "
                f"{observable.resource_id_field!r}; verification requires {identifier!r}"
            )


def check_derived_expectations_do_not_shadow_arguments(
    declared: Mapping[str, tuple[DerivedExpectation, ...]] | None = None,
    registry: ToolRegistry = REGISTRY,
) -> None:
    """Fail at import if a derived expectation could stand in for an authorised argument.

    :func:`expectations` puts the canonical arguments in first, so a derived expectation sharing a
    declared argument's name would overwrite it — and every comparison that reads that name would
    then be checking a value CareLoop computed against a value CareLoop computed, with the thing
    policy actually allowed no longer participating. A verification that cannot fail is worse than
    none, because the timeline says it passed.

    Also refuses a table entry for an action type the registry does not declare: a derived
    expectation for an action that cannot happen is a guarantee about nothing.

    The parameters exist so both refusals can be provoked against synthetic declarations rather
    than only asserted about the shipped ones, which today declare nothing at all.

    Raises:
        UnverifiableAction: naming the action type and the colliding names.
    """
    table = DERIVED_EXPECTATIONS if declared is None else declared
    for action_type, expectations_declared in table.items():
        tool = registry.get(action_type)
        if tool is None:
            raise UnverifiableAction(
                f"derived expectation(s) declared for {action_type!r}, which the registry does not "
                f"declare; an expectation about an action that cannot happen guarantees nothing"
            )
        names = [expectation.name for expectation in expectations_declared]
        if shadowed := sorted(frozenset(names) & tool.arg_names):
            raise UnverifiableAction(
                f"derived expectation(s) {shadowed} on {action_type!r} share a declared argument "
                f"name; a derived value would displace the one policy authorised and the "
                f"comparison would stop checking anything"
            )
        if len(frozenset(names)) != len(names):
            raise UnverifiableAction(
                f"derived expectation names for {action_type!r} are not distinct: {sorted(names)}"
            )


def check_non_identifying_arguments_are_verified(registry: ToolRegistry = REGISTRY) -> None:
    """Fail at import if an argument left out of the action's identity is checked by nothing.

    **The coupling that makes narrowing identity safe**, and the reason it is expressed as a check
    rather than as a paragraph. Excluding an argument from
    :func:`careloop.tools.idempotency.idempotency_key` says two calls differing only there are the
    same logical action; it says nothing whatever about the value being harmless, and it must never
    become a way to stop looking at one. So an argument may leave identity only where a verifier
    still compares it against what the action actually did — and, for an independently observable
    action type, against what the external system holds.

    Scoped to external-effect tools, because a tool with no effect has no verifier by design and
    nothing to compare against.

    Raises:
        UnverifiableAction: naming the tool and the arguments that are neither identifying nor
            compared.
    """
    for tool in registry:
        if not tool.external_effect:
            continue
        unchecked = sorted(
            tool.arg_names - tool.identifying_args - COMPARED_ARGUMENTS.get(tool.name, frozenset())
        )
        if unchecked:
            raise UnverifiableAction(
                f"{tool.name} declares argument(s) {unchecked} that neither identify the action "
                f"nor are compared by its verifier; an argument may leave the idempotency key only "
                f"where something still checks it afterwards"
            )


check_verifiers_cover_the_registry()
check_observable_tools_are_declared()
check_derived_expectations_do_not_shadow_arguments()
check_observable_state_matches_verification()
check_non_identifying_arguments_are_verified()


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
    authority. They come from the approved proposal. What the verifier is handed is
    :func:`expectations` over them, which for every action type CareLoop ships is the same mapping.

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
        expected = expectations(action_type, canonical_args(args))
        problems.extend(verifier(expected, result))
        if observed is not None:
            disagreements = verifier(expected, observed)
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
    "COMPARED_ARGUMENTS",
    "DERIVED_EXPECTATIONS",
    "INDEPENDENTLY_OBSERVABLE",
    "OBSERVABLE_STATE",
    "RESULT_TYPES",
    "VERIFIERS",
    "DerivedExpectation",
    "ObservableEffect",
    "ObservationAbsent",
    "ObservationOutcome",
    "ObservationUnavailable",
    "ReadBack",
    "ReadBackFailed",
    "UnverifiableAction",
    "Verification",
    "Verifier",
    "check_derived_expectations_do_not_shadow_arguments",
    "check_non_identifying_arguments_are_verified",
    "check_observable_state_matches_verification",
    "check_observable_tools_are_declared",
    "check_verifiers_cover_the_registry",
    "compared_arguments",
    "derived_names",
    "expectations",
    "rehydrate",
    "verify_action",
]
