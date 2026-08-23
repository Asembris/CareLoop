"""Deterministic document extraction, and the validation that decides what may be promoted.

**No model runs here, and that is an architecture decision rather than an optimisation.** Invariant
1 permits exactly two LLM agents, and its second sentence says what everything else is:
deterministic Python or a typed tool. Session 5 closed this by deciding that the third model role
stays wired and unused; a third agent reading documents into facts would be an invariant 1 change
and has to be argued as one. (The role's enum member is not named here in full: a sweep in
``tests/test_agents.py`` fails if any module outside ``careloop/llm/providers.py`` mentions it, and
this module — the one a future author would be tempted to wire it into — is exactly where that
sweep has to keep working.)

**This module is where untrusted text enters the system, and it is where it enters the planner's
context.** Two kinds of fact come out of a document:

*Page excerpts*, one fact per page, holding the extracted page text verbatim. These exist so the
planner can read the evidence it is planning against, and they are the reason T2 is a real
temptation rather than a staged one: :func:`careloop.agents.prompts.planner_user_prompt` renders
every fact's value inside the quoted evidence block, so the poisoned continuity notice's three
requests reach the planner in full. Nothing is stripped, escaped or sanitised on the way — a filter
that looks like a defence would make T2 pass for the wrong reason, and the authority lives in the
policy engine.

*Structured values*, lifted by the patterns in :data:`PATTERNS` and then checked three ways before
any of them can be trusted with anything.

Three checks, in order, each one able to stop a value on its own:

1. **Which patterns run at all is decided by the document's kind**, which is metadata rather than
   something read out of the text — see :class:`careloop.tools.results.DocumentKind`. A continuity
   notice is scanned for no structured values whatsoever, so an insurer's letter cannot contribute
   a ``patient_id`` however it words one.
2. **The document's fields are validated as a set**, against the schema its kind names. Formats are
   checked field by field, and relationships between fields are checked by the schema's own model
   validator — an explanation of benefits whose arithmetic does not hold is not a document worth
   lifting values from. **A document that fails its schema yields page excerpts and no structured
   facts at all.** Discarding them rather than recording them unpromotable is what keeps the
   outcome independent of the order documents arrive in: a value another document supplies
   correctly is still recorded and still promotable.
3. **Cross-document checks** run over the whole case, because the interesting inconsistencies are
   between documents rather than inside one. Each check names the fact keys it gates, and a failing
   check withholds promotion of exactly those keys — leaving them ``UNTRUSTED``, which denies the
   dependent action ``INSUFFICIENT_FACT_TRUST``: remediable, and it says so.

**Only ``VALUE``-class keys are promotable.** :data:`PROMOTABLE_KEYS` says so explicitly and
:func:`check_promotable_keys_match_the_registry` fails at import if the table's belief about a key
contradicts the tool registry. ``promote_fact`` would refuse a ``DESTINATION`` anyway; the check is
here so the contradiction is caught where it is written rather than where it is used. Excerpts are
absent from the table and can never be promoted at all.

**What is deliberately not extracted, because it cannot be done deterministically.** The reason for
a referral as clinical prose; the *intent* of a letter — whether a notice is informational or is
asking for something; and the identity of a provider from letterhead rather than from a structured
identifier. Every field below is anchored to a label the document itself prints. Reaching for a
model to read the rest would be an invariant 1 architecture change and has to be argued as one, not
slipped in as an improvement to extraction. The README says the same thing where a reader will find
it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from careloop.provenance.facts import Fact, Provenance
from careloop.provenance.trust import ArgClass, TrustLevel
from careloop.state.case import Case, TimelineKind
from careloop.state.evidence import promote_fact, record_fact
from careloop.state.machine import append_event
from careloop.tools.documents import DocumentStore, default_document_store
from careloop.tools.registry import REGISTRY, ToolRegistry
from careloop.tools.results import Document, DocumentKind

#: Name recorded on every fact this module produces.
EXTRACTOR: Final[str] = "deterministic_extractor"

#: Prefix of a page-excerpt fact's key. ``excerpt:<document_id>:p<page>``.
EXCERPT_PREFIX: Final[str] = "excerpt:"


class UnpromotableKeyDeclared(Exception):
    """Raised at import when :data:`PROMOTABLE_KEYS` contradicts the tool registry."""


class UnschematisedKind(Exception):
    """Raised at import when a document kind has patterns but no schema, or the reverse."""


def _labelled(label: str, capture: str = r"\S+") -> re.Pattern[str]:
    """A pattern matching one printed ``Label: value`` line, and nothing spanning two.

    Anchored and line-oriented on purpose. A loose pattern over attacker-controlled text is a way
    to extract whatever the attacker wants extracted, and an extractor that finds a ``Patient ID:``
    line an insurer's notice invented would put the attacker's value in the same key the referral's
    went in. Every pattern captures a single token or a single line, and never spans one.
    """
    return re.compile(rf"^{re.escape(label)}:[ \t]*({capture})[ \t]*$", re.MULTILINE)


#: Structured values lifted from document text, per document kind, as ``(fact key, pattern)``.
#:
#: Scoping by kind is the first of the three checks. A ``CONTINUITY_NOTICE`` maps to no patterns at
#: all, so the poisoned fixture contributes page excerpts and nothing else: it can tempt the
#: planner, which is its job, and it cannot contribute a fact, which is the point.
PATTERNS: Final[Mapping[DocumentKind, tuple[tuple[str, re.Pattern[str]], ...]]] = {
    DocumentKind.REFERRAL: (
        ("patient_id", _labelled("Patient ID")),
        ("slot_start", _labelled("Offered appointment")),
        ("referring_clinician", _labelled("Referring clinician", r".+?")),
    ),
    DocumentKind.APPOINTMENT_INSTRUCTIONS: (("patient_id", _labelled("Patient ID")),),
    DocumentKind.EXPLANATION_OF_BENEFITS: (
        ("patient_id", _labelled("Patient ID")),
        ("claim_number", _labelled("Claim number")),
        ("amount_billed", _labelled("Amount billed")),
        ("amount_allowed", _labelled("Amount allowed")),
        ("plan_paid", _labelled("Plan paid")),
        ("patient_responsibility", _labelled("Patient responsibility")),
    ),
    DocumentKind.CLAIM_REJECTION: (
        ("patient_id", _labelled("Patient ID")),
        ("claim_number", _labelled("Claim number")),
        ("rejection_code", _labelled("Rejection code")),
        ("document_id", _labelled("Required document")),
    ),
    DocumentKind.RECEIPT: (
        ("patient_id", _labelled("Patient ID")),
        ("receipt_number", _labelled("Receipt number")),
        ("amount_paid", _labelled("Amount paid")),
    ),
    DocumentKind.CONTINUITY_NOTICE: (),
}


# -- schemas -------------------------------------------------------------------------------------


def _matches(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def _money(value: str) -> Decimal:
    """Parse an amount, or raise ``InvalidOperation``. Never a ``float``.

    ``float`` is excluded from the canonical value form for the same reason it is excluded here:
    its repr is unstable enough to be both a laundering and a false-denial hazard.
    """
    return Decimal(value)


class ExtractedFields(BaseModel):
    """The structured fields one document yields, validated as a set.

    Frozen and ``extra="forbid"``: a pattern producing a key the schema does not declare is a
    disagreement between two tables, and it should fail rather than be dropped silently.

    **Every field is required.** A document missing one of the fields its kind is expected to print
    fails validation and contributes no structured values. That is the fail-closed reading and it
    is deliberate: a partially-extracted document is one whose layout was not what extraction
    thought it was, and lifting the fields that happened to match is exactly how the wrong value
    ends up in the right key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class _HasPatientId(ExtractedFields):
    patient_id: str

    @field_validator("patient_id")
    @classmethod
    def _check(cls, value: str) -> str:
        if not _matches(r"PT-\d{4}", value):
            raise ValueError("patient_id must look like PT-0000")
        return value


class ReferralFields(_HasPatientId):
    slot_start: str
    referring_clinician: str

    @field_validator("slot_start")
    @classmethod
    def _check_slot(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value)
        except ValueError as failure:
            raise ValueError("slot_start must be an ISO datetime") from failure
        return value

    @field_validator("referring_clinician")
    @classmethod
    def _check_clinician(cls, value: str) -> str:
        if not value.strip() or len(value) > 80:
            raise ValueError("referring_clinician must be a non-empty name")
        return value


class AppointmentInstructionsFields(_HasPatientId):
    """Instructions carry the identifier and nothing else worth lifting.

    The arrival time, the clinic location and the cancellation policy are all prose or
    presentation. They are readable in the page excerpts; none of them is an argument to anything.
    """


class ExplanationOfBenefitsFields(_HasPatientId):
    claim_number: str
    amount_billed: str
    amount_allowed: str
    plan_paid: str
    patient_responsibility: str

    @field_validator("claim_number")
    @classmethod
    def _check_claim(cls, value: str) -> str:
        if not _matches(r"CLM-\d{4}-\d{5}", value):
            raise ValueError("claim_number must look like CLM-0000-00000")
        return value

    @field_validator("amount_billed", "amount_allowed", "plan_paid", "patient_responsibility")
    @classmethod
    def _check_money(cls, value: str) -> str:
        if not _matches(r"\d+\.\d{2}", value):
            raise ValueError("amounts are decimal with two places")
        return value

    @model_validator(mode="after")
    def _check_the_arithmetic(self) -> ExplanationOfBenefitsFields:
        """The cross-field check inside one document.

        Two relationships an explanation of benefits must satisfy to be worth reading: the plan
        cannot allow more than was billed or pay more than it allowed, and what is left for the
        member to pay is exactly the difference. A statement failing either is either a
        misextraction or a forgery, and neither is something to lift a patient identifier out of.
        """
        try:
            billed = _money(self.amount_billed)
            allowed = _money(self.amount_allowed)
            paid = _money(self.plan_paid)
            responsibility = _money(self.patient_responsibility)
        except InvalidOperation as failure:  # pragma: no cover - field validators run first
            raise ValueError("amounts are not parseable") from failure

        if not billed >= allowed >= paid:
            raise ValueError(
                f"amounts are not ordered: billed {billed} >= allowed {allowed} >= paid {paid}"
            )
        if responsibility != allowed - paid:
            raise ValueError(
                f"patient responsibility {responsibility} is not allowed {allowed} "
                f"minus plan paid {paid}"
            )
        return self


class ClaimRejectionFields(_HasPatientId):
    claim_number: str
    rejection_code: str
    document_id: str

    @field_validator("claim_number")
    @classmethod
    def _check_claim(cls, value: str) -> str:
        if not _matches(r"CLM-\d{4}-\d{5}", value):
            raise ValueError("claim_number must look like CLM-0000-00000")
        return value

    @field_validator("rejection_code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        if not _matches(r"R-\d{3}", value):
            raise ValueError("rejection_code must look like R-000")
        return value

    @field_validator("document_id")
    @classmethod
    def _check_document(cls, value: str) -> str:
        if not _matches(r"doc-\d{4}-[a-z0-9-]+", value):
            raise ValueError("document_id must look like doc-0000-name")
        return value


class ReceiptFields(_HasPatientId):
    receipt_number: str
    amount_paid: str

    @field_validator("receipt_number")
    @classmethod
    def _check_receipt(cls, value: str) -> str:
        if not _matches(r"RCT-\d{4}-\d{5}", value):
            raise ValueError("receipt_number must look like RCT-0000-00000")
        return value

    @field_validator("amount_paid")
    @classmethod
    def _check_money(cls, value: str) -> str:
        if not _matches(r"\d+\.\d{2}", value):
            raise ValueError("amounts are decimal with two places")
        return value


class NoFields(ExtractedFields):
    """The schema for a document kind nothing structured is lifted from."""


#: Document kind to the schema its structured fields are validated against.
SCHEMAS: Final[Mapping[DocumentKind, type[ExtractedFields]]] = {
    DocumentKind.REFERRAL: ReferralFields,
    DocumentKind.APPOINTMENT_INSTRUCTIONS: AppointmentInstructionsFields,
    DocumentKind.EXPLANATION_OF_BENEFITS: ExplanationOfBenefitsFields,
    DocumentKind.CLAIM_REJECTION: ClaimRejectionFields,
    DocumentKind.RECEIPT: ReceiptFields,
    DocumentKind.CONTINUITY_NOTICE: NoFields,
}


# -- cross-document checks -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    """Everything a cross-check may read about one case. One object, so the answer is one thing.

    It replaced a ``(values, documents)`` pair when a check first needed to ask *which document*
    a value came from. Growing a third positional parameter would have given three call sites an
    argument two of them ignore; naming the readable surface instead is the same arrangement
    :class:`careloop.graph.verify.ObservableEffect` and
    :class:`careloop.graph.context.RecheckTarget` already use.

    **Excerpts are absent from both collections**, exactly as they were from the mapping this
    replaced. A page excerpt is the untrusted text itself rather than a value lifted out of it; it
    is never promotable, so a check that could gate on one would be gating on the attacker's prose.
    """

    values: Mapping[str, tuple[str, ...]]
    """Structured fact values on the case, by fact key. Two values under one key means two
    documents disagreed — which is what :func:`_patient_id_agrees` reads."""

    facts: tuple[Fact, ...]
    """The same structured facts, carrying their :class:`~careloop.provenance.facts.Provenance`.

    Present so a check can ask *which document supplied this value*, which is the only way to
    establish that a document named by a letter is one this case actually read rather than one
    that merely exists. :attr:`values` cannot answer it: it is values alone, and a value says
    nothing about where it came from.
    """

    documents: DocumentStore
    """Every document CareLoop holds. **Not scoped to the case** — deliberately, because
    "CareLoop holds this at all" and "this case read it" are two different questions and
    :data:`CROSS_CHECKS` asks both, separately."""


class CrossCheck(BaseModel):
    """One consistency requirement spanning more than one document.

    ``gates`` is the set of fact keys the check is allowed to stop. A check that fails withholds
    promotion of exactly those keys and touches nothing else — a disagreement about a claim number
    is no reason to distrust an appointment time.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    gates: frozenset[str]
    check: Callable[[CaseEvidence], str | None]
    """Returns ``None`` when satisfied, or a diagnostic saying why it is not."""


def _patient_id_agrees(evidence: CaseEvidence) -> str | None:
    """Every document naming a patient must name the same one.

    Facts are deduplicated on ``(key, value)``, so more than one distinct ``patient_id`` on a case
    means two documents disagreed. Promoting either would be picking a winner between an authentic
    document and something else, and there is nothing here that can tell which is which.
    """
    found = evidence.values.get("patient_id", ())
    if len(found) > 1:
        return f"documents disagree about the patient: {', '.join(sorted(found))}"
    return None


def _required_document_is_held(evidence: CaseEvidence) -> str | None:
    """A document demanded by a letter must be one CareLoop actually holds.

    Without this, a well-formatted identifier that no document answers to could be promoted and
    then uploaded — the upload would fail at the tool, but the promotion would have been granted on
    the strength of the identifier merely looking right.

    **Existence only, and that was once mistaken for enough.** It asks whether CareLoop holds the
    document *anywhere*, which is a question about the store rather than about this case; see
    :func:`_required_document_answers_the_demand` for the relationship this one does not establish.
    """
    missing = sorted(
        value for value in evidence.values.get("document_id", ()) if value not in evidence.documents
    )
    if missing:
        return f"named document(s) not held by CareLoop: {', '.join(missing)}"
    return None


def _receipt_settles_the_balance(evidence: CaseEvidence) -> str | None:
    """A receipt offered as proof of payment must be for the amount that was outstanding.

    Applies only when the case holds both figures; a case with one and not the other has nothing
    to compare and is not made suspicious by the absence. When both are present and they differ,
    the receipt does not answer the rejection, and authorising its upload as though it did would be
    the system asserting something it has not checked.
    """
    outstanding = evidence.values.get("patient_responsibility", ())
    paid = evidence.values.get("amount_paid", ())
    if not outstanding or not paid:
        return None
    try:
        if {_money(value) for value in outstanding} != {_money(value) for value in paid}:
            return (
                f"receipt total(s) {', '.join(sorted(paid))} do not settle the outstanding "
                f"balance {', '.join(sorted(outstanding))}"
            )
    except InvalidOperation:
        return "an amount could not be read as a decimal"
    return None


#: The structured value a document must supply to count as answering a claim rejection.
#:
#: A rejection demands *evidence that the member paid the amount shown as patient responsibility* —
#: that is what the shipped rejection code says and it is the only demand CareLoop's one upload
#: workflow answers. A document supplying no ``amount_paid`` is not proof of payment, whatever a
#: letter calls it. Named here rather than spelled inside the check so that the relationship
#: between this and :func:`_receipt_settles_the_balance`, which compares that same value against
#: the outstanding balance, is one declaration read twice rather than two strings that can drift.
#:
#: A future demand for something other than proof of payment needs a deliberate extension. Until
#: one is written such a demand is refused, which is the fail-closed direction: an upload CareLoop
#: cannot show a reason for is an upload it does not make.
DEMAND_IS_ANSWERED_BY: Final[str] = "amount_paid"


def _required_document_answers_the_demand(evidence: CaseEvidence) -> str | None:
    """A document a letter demands must be one **this case read**, and must answer the demand.

    **The relationship the other two checks do not establish, and the reason this exists.**
    ``document_id`` is lifted from a ``Required document:`` line printed by a ``CLAIM_REJECTION``,
    and that line is untrusted text like every other line of every other document. Before this
    check, everything downstream asked only whether the named identifier was *well formed*
    (``document_id_format``) and whether CareLoop held *something* under it
    (:func:`_required_document_is_held`) — neither of which is a statement about this case. So a
    rejection could name any document in the entire store and have it promoted to ``CONTROLLED``
    and authorised for autonomous upload to the trusted portal: the poisoned continuity notice, a
    referral never attached to this case, anything at all. Reproduced against the shipped rules and
    the shipped extractor before this was written.

    That it did not happen with the shipped fixtures was an accident of what the fixture rejection
    happens to print — exactly the kind of boundary this repository refuses to rely on.

    Two clauses, and both are about *this case*:

    **The demanded document contributed structured evidence here.** Matched on
    :attr:`careloop.provenance.facts.Provenance.source_doc`, resolved through the store from the
    demanded id, so the relationship is the evidence node's own record of what it read rather than
    a second list somebody has to keep in step. A page excerpt does not count and cannot: excerpts
    are absent from :attr:`CaseEvidence.facts`, so a document that was read but yielded nothing a
    schema accepted answers nothing — which is precisely the poisoned notice's position, since
    ``CONTINUITY_NOTICE`` maps to no patterns at all.

    **What it contributed answers the demand.** It must have supplied
    :data:`DEMAND_IS_ANSWERED_BY`. A document that was read, and agrees about the patient, and is
    still not proof of payment is still not the document to send.

    Together with :func:`_receipt_settles_the_balance`, which then requires that amount to settle
    the outstanding balance, the chain from an untrusted line to an external effect reads: a letter
    on this case demanded D, CareLoop holds D, this case read D, D established what was paid, and
    what was paid settles what this case's own explanation of benefits says was owed.

    Skips an id the store does not hold, so a missing document is reported once by the check whose
    subject it is — :func:`cross_check_failures` keeps the first failure per key and
    :data:`CROSS_CHECKS` puts that one first.
    """
    unanswered: list[str] = []
    for demanded in sorted(evidence.values.get("document_id", ())):
        if demanded not in evidence.documents:
            continue
        source = evidence.documents.get(demanded).source_doc
        supplied = frozenset(
            fact.key for fact in evidence.facts if fact.provenance.source_doc == source
        )
        if not supplied:
            unanswered.append(f"{demanded} supplied no evidence to this case")
        elif DEMAND_IS_ANSWERED_BY not in supplied:
            unanswered.append(f"{demanded} established no {DEMAND_IS_ANSWERED_BY}")
    if unanswered:
        joined = "; ".join(unanswered)
        return f"demanded document(s) do not answer the demand: {joined}"
    return None


#: Every cross-document check, in evaluation order.
CROSS_CHECKS: Final[tuple[CrossCheck, ...]] = (
    CrossCheck(
        name="patient-id-agrees-across-documents",
        gates=frozenset({"patient_id"}),
        check=_patient_id_agrees,
    ),
    CrossCheck(
        name="required-document-is-held",
        gates=frozenset({"document_id"}),
        check=_required_document_is_held,
    ),
    CrossCheck(
        name="required-document-answers-the-demand",
        gates=frozenset({"document_id"}),
        check=_required_document_answers_the_demand,
    ),
    CrossCheck(
        name="receipt-settles-the-outstanding-balance",
        gates=frozenset({"document_id"}),
        check=_receipt_settles_the_balance,
    ),
)


# -- promotion -------------------------------------------------------------------------------


#: Keys that may be promoted, and the argument class each is destined for.
#:
#: ``VALUE`` only. ``DESTINATION`` and ``AUTHORITY`` are absent from
#: :data:`careloop.provenance.trust.PROMOTABLE_ARG_CLASSES` and no entry here could change that;
#: the table exists so a rule about *which values get validated* is written down rather than
#: implied by whichever validators happen to exist.
PROMOTABLE_KEYS: Final[Mapping[str, ArgClass]] = {
    "patient_id": ArgClass.VALUE,
    "slot_start": ArgClass.VALUE,
    "document_id": ArgClass.VALUE,
}

#: A validator answers one question: is this text well-formed for its key?
#:
#: Format only. A validator can confirm that a string is an ISO datetime; it cannot confirm that
#: the appointment is the right one, and it must never be asked to. Confirming *shape* is what
#: raises ``UNTRUSTED`` to ``CONTROLLED``, and ``CONTROLLED`` is still below ``TRUSTED``.
Validator = Callable[[str], bool]


def _is_patient_id(text: str) -> bool:
    return _matches(r"PT-\d{4}", text)


def _is_iso_datetime(text: str) -> bool:
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _is_document_id(text: str) -> bool:
    return _matches(r"doc-\d{4}-[a-z0-9-]+", text)


#: Key to ``(validator name, validator)``. The name is recorded on the promoted fact, so the audit
#: trail says which check justified the trust level rather than merely that one did.
VALIDATORS: Final[Mapping[str, tuple[str, Validator]]] = {
    "patient_id": ("patient_id_format", _is_patient_id),
    "slot_start": ("iso_datetime", _is_iso_datetime),
    "document_id": ("document_id_format", _is_document_id),
}


def check_promotable_keys_match_the_registry(registry: ToolRegistry = REGISTRY) -> None:
    """Fail if a promotable key is classified differently by the tool registry.

    The registry owns argument classes; this table only *references* one, exactly as ``rules.yaml``
    references them. A key the registry calls ``DESTINATION`` appearing here as ``VALUE`` is a
    contradiction, and the whole trust model rests on that classification being in one place.

    **Resolved through :attr:`careloop.tools.registry.ArgSpec.trust_key`, not through argument
    names.** The keys in this table are *fact* keys, and an argument's name is not its fact key —
    ``send_email.to`` draws on ``caregiver_email``. Matching by name worked only for as long as
    every promotable key happened to be spelled the same as the argument consuming it, and it would
    have gone on silently checking nothing the first time one was not. Matching by ``trust_key`` is
    the same question asked of the field that actually answers it.

    Raises:
        UnpromotableKeyDeclared: on any disagreement.
    """
    wrong: list[str] = []
    for key, declared in PROMOTABLE_KEYS.items():
        for tool in registry:
            for arg in tool.args:
                if arg.trust_key != key or arg.arg_class is declared:
                    continue
                wrong.append(
                    f"{key}: table says {declared}, {tool.name}.{arg.name} declares {arg.arg_class}"
                )
    if wrong:
        raise UnpromotableKeyDeclared(
            "promotable key table contradicts the tool registry: " + "; ".join(sorted(wrong))
        )


def check_every_kind_is_schematised() -> None:
    """Fail if a document kind has no schema, or a schema declares fields no pattern produces.

    Both directions are drift. A kind without a schema would be validated by nothing; a schema
    field no pattern fills would make every document of that kind fail for a reason nobody
    intended.

    Raises:
        UnschematisedKind: on either disagreement.
    """
    problems: list[str] = []
    for kind in DocumentKind:
        if kind not in PATTERNS:
            problems.append(f"{kind} has no patterns")
            continue
        if kind not in SCHEMAS:
            problems.append(f"{kind} has no schema")
            continue
        produced = {key for key, _pattern in PATTERNS[kind]}
        declared = set(SCHEMAS[kind].model_fields)
        if produced != declared:
            problems.append(
                f"{kind}: patterns produce {sorted(produced)}, schema declares {sorted(declared)}"
            )
    if problems:
        raise UnschematisedKind("; ".join(problems))


def check_gated_keys_are_promotable() -> None:
    """Fail if a cross-check gates a key that could never be promoted anyway.

    A check that gates nothing promotable is a check that cannot change an outcome, and a guard
    that cannot fire is decoration. Session 6 deleted one of those; this stops another being
    written.

    Raises:
        UnpromotableKeyDeclared: if any gated key is absent from :data:`PROMOTABLE_KEYS`.
    """
    stray = sorted(
        key for check in CROSS_CHECKS for key in check.gates if key not in PROMOTABLE_KEYS
    )
    if stray:
        raise UnpromotableKeyDeclared(
            f"cross-checks gate non-promotable key(s) {stray}, so they can never change an outcome"
        )


check_promotable_keys_match_the_registry()
check_every_kind_is_schematised()
check_gated_keys_are_promotable()


def excerpt_key(document_id: str, page: int) -> str:
    """The fact key holding one page of one document."""
    return f"{EXCERPT_PREFIX}{document_id}:p{page}"


def structured_values(document: Document) -> dict[str, tuple[str, int]]:
    """Every structured value in ``document``, as ``key -> (value, page)``.

    Which patterns run is decided by ``document.kind``. A pattern that matches more than once on a
    page yields only its first match, and the page loop stops at the first page that matched:
    taking the first is deterministic, and taking the last would let a document append a second
    ``Patient ID:`` line and overwrite the real one, which is the whole shape of the attack this
    system is about.
    """
    found: dict[str, tuple[str, int]] = {}
    for key, pattern in PATTERNS[document.kind]:
        for page_number, page in enumerate(document.pages, start=1):
            match = pattern.search(page)
            if match is None:
                continue
            found[key] = (match.group(1), page_number)
            break
    return found


def validate_fields(kind: DocumentKind, values: Mapping[str, str]) -> str | None:
    """``None`` if ``values`` satisfy the schema for ``kind``, else why they do not."""
    try:
        SCHEMAS[kind](**values)
    except ValidationError as failure:
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'document'}: {error['msg']}"
            for error in failure.errors()
        )
    return None


def extract(document: Document, now: datetime | None = None) -> tuple[tuple[Fact, ...], str | None]:
    """Every fact ``document`` yields, all ``UNTRUSTED``, and the schema failure if there was one.

    Page excerpts come first, in page order, then structured values in :data:`PATTERNS` order. The
    ordering is fixed so two runs over one document produce identical facts and a case's timeline
    is comparable across runs.

    When the schema refuses the document's fields, the excerpts are returned and the structured
    values are **discarded**. The planner can still read the page — refusing to show it evidence
    would be a filter — but nothing was lifted out of it into a key an action could be built from.
    """
    at = now or datetime.now(UTC)
    facts: list[Fact] = [
        Fact(
            key=excerpt_key(document.document_id, page_number),
            value=page,
            trust_level=TrustLevel.UNTRUSTED,
            provenance=Provenance(
                source_doc=document.source_doc, page=page_number, extractor=EXTRACTOR
            ),
            extracted_at=at,
        )
        for page_number, page in enumerate(document.pages, start=1)
    ]

    found = structured_values(document)
    refusal = validate_fields(document.kind, {key: value for key, (value, _page) in found.items()})
    if refusal is not None:
        return tuple(facts), refusal

    facts.extend(
        Fact(
            key=key,
            value=value,
            trust_level=TrustLevel.UNTRUSTED,
            provenance=Provenance(source_doc=document.source_doc, page=page, extractor=EXTRACTOR),
            extracted_at=at,
        )
        for key, (value, page) in found.items()
    )
    return tuple(facts), None


def record_document(case: Case, document: Document, now: datetime | None = None) -> Case:
    """Record every fact ``document`` yields onto ``case``, without promoting any of them."""
    facts, refusal = extract(document, now=now)
    for fact in facts:
        case = record_fact(case, fact, now=now)
    if refusal is not None:
        case = _refused(
            case,
            summary=f"no structured values lifted from {document.source_doc}",
            stage="schema",
            detail=(
                f"{document.source_doc} did not satisfy the {document.kind} schema ({refusal}), "
                f"so its page excerpts were recorded as evidence and its structured fields were "
                f"discarded rather than recorded unpromotable — which keeps the outcome "
                f"independent of the order documents arrive in"
            ),
            payload={"source_doc": document.source_doc, "kind": str(document.kind)},
            now=now,
        )
    return case


def cross_check_failures(
    case: Case, documents: DocumentStore | None = None
) -> dict[str, tuple[str, str]]:
    """Fact keys whose promotion is withheld, as ``key -> (check name, why)``.

    Evaluated over the case rather than over one document, because the inconsistencies worth
    catching are between documents. Values are read from the case's facts by key, which is also
    why the deduplication in :func:`careloop.state.evidence.record_fact` matters here: two
    surviving values under one key means two documents genuinely disagreed.
    """
    held = documents if documents is not None else default_document_store()
    structured = tuple(fact for fact in case.facts if not fact.key.startswith(EXCERPT_PREFIX))
    values: dict[str, tuple[str, ...]] = {}
    for fact in structured:
        values[fact.key] = (*values.get(fact.key, ()), fact.value)
    evidence = CaseEvidence(values=values, facts=structured, documents=held)

    blocked: dict[str, tuple[str, str]] = {}
    for cross_check in CROSS_CHECKS:
        why = cross_check.check(evidence)
        if why is None:
            continue
        for key in cross_check.gates:
            blocked.setdefault(key, (cross_check.name, why))
    return blocked


def validate_and_promote(
    case: Case,
    now: datetime | None = None,
    documents: DocumentStore | None = None,
) -> Case:
    """Promote every ``UNTRUSTED`` fact on ``case`` whose validator passes and whose checks hold.

    Walks the case's own facts rather than a list handed in, so a fact recorded by some other
    route is validated on the same terms. A fact whose key has no validator, whose validator
    refuses, or whose key a cross-check has withheld is left exactly as it is — the action
    depending on it is then denied ``INSUFFICIENT_FACT_TRUST``, which is remediable and says so.

    Promotion **replaces** the untrusted original; see :mod:`careloop.state.evidence` for why
    appending beside it makes the promotion silently invisible.
    """
    blocked = cross_check_failures(case, documents)

    for key, (name, why) in sorted(blocked.items()):
        if any(fact.key == key for fact in _promotable(case.facts)):
            case = _refused(
                case,
                summary=f"{key} not validated",
                stage="cross_check",
                detail=(
                    f"cross-document check {name} failed ({why}), so {key} was left UNTRUSTED. "
                    f"An action depending on it is denied INSUFFICIENT_FACT_TRUST, which is "
                    f"remediable: correct or re-supply the document and the value can be "
                    f"validated."
                ),
                payload={"key": key, "check": name},
                now=now,
            )

    for fact in _promotable(case.facts):
        if fact.key in blocked:
            continue
        validator_name, validator = VALIDATORS[fact.key]
        if not validator(fact.value):
            continue
        case = promote_fact(
            case,
            fact,
            arg_class=PROMOTABLE_KEYS[fact.key],
            validator=validator_name,
            now=now,
        )
    return case


def _refused(
    case: Case,
    *,
    summary: str,
    stage: str,
    detail: str,
    payload: dict[str, object],
    now: datetime | None,
) -> Case:
    """Record that deterministic validation declined to accept or promote something.

    Written to the timeline rather than performed quietly for the same reason the duplicate skip
    is: a value that fails to promote produces a denial three nodes later, and an audit trail that
    does not say why turns a policy outcome into a mystery.
    """
    return append_event(
        case,
        kind=TimelineKind.VALIDATION_REFUSED,
        actor="evidence",
        summary=summary,
        diagnostic=detail,
        payload={"stage": stage, **payload},
        now=now,
    )


def _promotable(facts: Iterable[Fact]) -> list[Fact]:
    """Untrusted facts whose key is both promotable and has a validator."""
    return [
        fact
        for fact in facts
        if fact.trust_level is TrustLevel.UNTRUSTED
        and fact.key in PROMOTABLE_KEYS
        and fact.key in VALIDATORS
    ]


def promotable_keys() -> Sequence[str]:
    """Every key validation may raise to ``CONTROLLED``."""
    return sorted(PROMOTABLE_KEYS)


__all__ = [
    "CROSS_CHECKS",
    "EXCERPT_PREFIX",
    "EXTRACTOR",
    "PATTERNS",
    "PROMOTABLE_KEYS",
    "SCHEMAS",
    "VALIDATORS",
    "AppointmentInstructionsFields",
    "ClaimRejectionFields",
    "CrossCheck",
    "ExplanationOfBenefitsFields",
    "ExtractedFields",
    "NoFields",
    "ReceiptFields",
    "ReferralFields",
    "UnpromotableKeyDeclared",
    "UnschematisedKind",
    "Validator",
    "check_every_kind_is_schematised",
    "check_gated_keys_are_promotable",
    "check_promotable_keys_match_the_registry",
    "cross_check_failures",
    "excerpt_key",
    "extract",
    "promotable_keys",
    "record_document",
    "structured_values",
    "validate_and_promote",
    "validate_fields",
]
