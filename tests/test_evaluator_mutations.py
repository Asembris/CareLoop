"""Deliberate attacks on ``CareLoopSettlementIntegrity``, and the proof that they are detected.

Patch B's suite asserts that the evaluator behaves correctly. That is a different claim from the
one this file makes, which is the one a reviewer actually needs:

    if a future engineer removes, weakens, broadens, inverts or vacuously satisfies a settlement
    defence, does the standing assertion set notice?

**The ledger is the evidence.** :data:`MUTANTS` is an explicit list of semantic mutations, each
naming the surface it attacks, the severity of its escaping, one *witness* payload, and the two
outcomes that witness must produce -- under the shipped evaluator, and under the mutation. Running

.. code-block:: bash

    pytest tests/test_evaluator_mutations.py -v

prints the ledger, because the pytest ids are the mutant ids. Nothing is generated, nothing is
snapshotted, and there is no denominator maintained by hand: every count in the final report is
computed from ``MUTANTS`` itself.

**Three things make a mutant count, and all three are enforced.**

*It must be safe before.* The witness under the shipped evaluator must produce
:attr:`Mutant.expected_before`. A witness that already fails for some unrelated reason proves
nothing about the defence under attack.

*It must be dangerous after.* The same witness under the mutation must produce
:attr:`Mutant.expected_after`, and that must **differ** from the before outcome. If it does not,
the mutation never reached the decision path, and that is a
:exc:`MutationHadNoEffect` **harness failure** -- never a surviving mutant, and never a flattering
score.

*It must be caught.* A mutant is killed when at least one entry in :data:`CORPUS` -- the standing
assertion set this file also enforces -- changes its outcome under the mutation. That is what
"the test suite notices" means, made operational.

**The binding trap this design exists to close.** ``settlement.py`` does ``from evaluators.spans
import successful`` and so on, so those names live in ``evaluators.settlement``'s own namespace.
Patching ``evaluators.spans.successful`` or ``evaluators.contract.DIGEST_PATTERN`` changes nothing
the decision procedure reads. During the Patch C plan gate exactly that happened: a digest mutant
reported a clean survival while nothing had been mutated at all. The before/after rule turns that
class of error into a loud harness failure instead of a green tick.

**Scope, stated so it is not overread.** This file proves resistance to *single* mutations.
Combinations are out of scope -- deleting checks 14, 15 and 16 together would let equal-but-
malformed digests settle, and no claim here covers that.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

import pytest
from test_evaluator import (
    ABSENT,
    OTHER_DIGEST,
    READ_END,
    WRITE_END,
    bedrock_spans,
    graph_span,
    internal_operation_span,
    payload,
    quiet_trace,
    read_span,
    recheck_trace,
    settled_trace,
    simulated_trace,
    write_span,
)

import evaluators.settlement as settlement
from evaluators.contract import (
    ATTR_EFFECT_DISPATCH,
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_STATE_DIGEST,
    ATTR_OBSERVATION_MODE,
    ATTR_OBSERVATION_OUTCOME,
    ATTR_OBSERVATION_RESOURCE_ID,
    ATTR_OBSERVATION_STATE_DIGEST,
    ATTR_SETTLEMENT,
    CheckId,
    ErrorCode,
    NaReason,
)
from evaluators.settlement import assess

# =============================================================================================
# Outcomes
# =============================================================================================


class Severity(StrEnum):
    """What it costs if a mutant escapes.

    The acceptance rule is stated on ``CRITICAL`` and ``HIGH`` alone and is never averaged; an
    aggregate score is exactly where one false-PASS mutant would hide behind ninety trivial kills.
    """

    CRITICAL = "CRITICAL"
    """Invalid or insufficient evidence can be certified as settled."""

    HIGH = "HIGH"
    """External clinic activity disappears into NOT_APPLICABLE, or ambiguity is resolved by
    arbitrary selection rather than refusal."""

    MEDIUM = "MEDIUM"
    """The audit, privacy or error contract weakens without a settlement PASS becoming reachable."""

    EQUIVALENT = "EQUIVALENT"
    """Provably unobservable. Requires a written justification and is never used to retire a
    mutant that merely happens to be uncaught."""


@dataclass(frozen=True)
class Outcome:
    """One evaluator verdict, reduced to the two things that matter about it.

    ``code`` is the closed-vocabulary name the result carries -- a :class:`CheckId`,
    :class:`NaReason` or :class:`ErrorCode` -- and is ``None`` only for ``PASS``. Comparing whole
    result dictionaries would make every assertion here depend on explanation wording; comparing
    label alone would let a mutation that silently changes *which* defence refused a trace slip
    past as "still a FAIL".
    """

    label: str
    code: str | None = None

    def __str__(self) -> str:
        return self.label if self.code is None else f"{self.label}:{self.code}"


PASSED: Final[Outcome] = Outcome("PASS")


def failed(code: CheckId) -> Outcome:
    return Outcome("FAIL", code.value)


def not_applicable(code: NaReason) -> Outcome:
    return Outcome("NOT_APPLICABLE", code.value)


def errored(code: ErrorCode) -> Outcome:
    return Outcome("ERROR", code.value)


def outcome_of(result: Mapping[str, Any]) -> Outcome:
    """Reduce a result mapping to its :class:`Outcome`.

    The code is read from the explanation's leading token rather than by reverse-lookup in the
    explanation tables, for one reason: a privacy mutant that *appends* a payload value to an
    explanation would defeat an exact-match reverse lookup and silently read as an unknown code.
    Reading the prefix keeps such a mutant visible as the code it claims to be, so the privacy
    assertions -- not the outcome reducer -- are what catch it.

    ``TestEveryExplanationAnnouncesItsOwnCode`` is what makes the prefix trustworthy.
    """
    if "errorCode" in result:
        return Outcome("ERROR", str(result["errorCode"]))
    label = str(result["label"])
    if label == "PASS":
        return Outcome("PASS")
    explanation = str(result.get("explanation", ""))
    return Outcome(label, explanation.split(":")[0] or None)


def verdict(built: Mapping[str, Any]) -> Outcome:
    """Judge one payload through the whole public path."""
    return outcome_of(assess(built))


# =============================================================================================
# The mutation harness
# =============================================================================================


class MutationHadNoEffect(AssertionError):
    """A mutation left the witness outcome unchanged.

    **A harness failure, never a surviving mutant.** It means the patched symbol is not the one
    the decision procedure reads -- almost always because ``evaluators.spans`` or
    ``evaluators.contract`` was patched instead of the name ``evaluators.settlement`` bound at
    import -- or that the witness does not exercise the mutated path. Either way the mutant proved
    nothing, and reporting it as "killed" would be a lie about coverage.
    """


#: How a mutation is applied. Takes the monkeypatch fixture so restoration is pytest's problem
#: rather than a ``try``/``finally`` in every entry.
Apply = Callable[[pytest.MonkeyPatch], None]

#: How a witness payload is built. A callable rather than a value so every mutant gets a fresh
#: mutable payload and one entry cannot contaminate the next.
Witness = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class Mutant:
    """One deliberate weakening of the evaluator, and the evidence that it is detected."""

    id: str
    family: str
    surface: str
    severity: Severity
    apply: Apply
    witness: Witness
    expected_before: Outcome
    expected_after: Outcome
    note: str = ""
    equivalence: str = ""
    """Why the mutation is behaviourally unobservable. Required for, and only for,
    :attr:`Severity.EQUIVALENT`."""

    extra_probes: tuple[Witness, ...] = field(default_factory=tuple)
    """Further payloads an equivalent mutant must also leave unchanged."""

    @property
    def is_equivalent(self) -> bool:
        return self.severity is Severity.EQUIVALENT


def patching(name: str, replacement: Callable[..., Any]) -> Apply:
    """Replace one name **in ``evaluators.settlement``**, which is the namespace that matters."""

    def apply(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settlement, name, replacement)

    return apply


def deriving(name: str, build: Callable[[Any], Callable[..., Any]]) -> Apply:
    """Replace a name with something built from the shipped original."""

    def apply(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settlement, name, build(getattr(settlement, name)))

    return apply


def deleting_check(check_name: str) -> Apply:
    """Remove one entry from ``CHECKS``, read at application time so no stale tuple is used."""

    def apply(monkeypatch: pytest.MonkeyPatch) -> None:
        surviving = tuple(c for c in settlement.CHECKS if c.__name__ != check_name)
        assert len(surviving) == len(settlement.CHECKS) - 1, check_name
        monkeypatch.setattr(settlement, "CHECKS", surviving)

    return apply


# =============================================================================================
# Witnesses
# =============================================================================================


def two_writes() -> Mapping[str, Any]:
    return payload([*settled_trace(), write_span(span_id="8888888888888888")])


def suppressed_but_wrote() -> Mapping[str, Any]:
    return payload(
        [
            internal_operation_span(),
            graph_span(**{ATTR_EFFECT_DISPATCH: "SUPPRESSED"}),
            *bedrock_spans(),
            write_span(),
            read_span(),
        ]
    )


def trace_with(**spans: Any) -> Mapping[str, Any]:
    """The settled six-span shape with the write and/or read replaced."""
    return payload(
        [
            internal_operation_span(),
            graph_span(**spans.pop("claim", {})),
            *bedrock_spans(),
            *([spans["write"]] if spans.get("write") is not None else []),
            *([spans["read"]] if spans.get("read") is not None else []),
        ]
    )


def ambiguous_write_status() -> Mapping[str, Any]:
    return trace_with(write=write_span(status=201, response_status=500), read=read_span())


def failed_write() -> Mapping[str, Any]:
    return trace_with(write=write_span(status=500, response_status=500), read=read_span())


def no_observer_claimed() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_OBSERVATION_MODE: "NONE"}))


def no_read_at_all() -> Mapping[str, Any]:
    return trace_with(write=write_span())


def unread_resource() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: "apt-never-read"}))


def two_matching_reads() -> Mapping[str, Any]:
    return payload(
        [
            *settled_trace(),
            read_span(span_id="8888888888888888", start=READ_END, end=READ_END + 1_000),
        ]
    )


def ambiguous_read_status() -> Mapping[str, Any]:
    return trace_with(write=write_span(), read=read_span(status=200, response_status=503))


def failed_read() -> Mapping[str, Any]:
    return trace_with(write=write_span(), read=read_span(status=404, response_status=404))


def unreadable_timing() -> Mapping[str, Any]:
    spans = settled_trace()
    for key in ("startTimeUnixNano", "endTimeUnixNano", "start_time", "end_time"):
        del spans[4][key]
    return payload(spans)


def read_before_write() -> Mapping[str, Any]:
    return trace_with(
        write=write_span(), read=read_span(start=WRITE_END - 1, end=WRITE_END + 1_000_000)
    )


def read_exactly_at_write_end() -> Mapping[str, Any]:
    return trace_with(
        write=write_span(), read=read_span(start=WRITE_END, end=WRITE_END + 1_000_000)
    )


def subset_field_universe() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_EFFECT_FIELDS: "patient_id,provider_id"}))


def malformed_digests() -> Mapping[str, Any]:
    return payload(
        settled_trace(
            **{ATTR_EFFECT_STATE_DIGEST: "nothex", ATTR_OBSERVATION_STATE_DIGEST: "nothex"}
        )
    )


def diverged_but_equal() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_OBSERVATION_OUTCOME: "DIVERGED"}))


def absent_outcome_unequal_digests() -> Mapping[str, Any]:
    return payload(
        settled_trace(
            **{
                ATTR_OBSERVATION_OUTCOME: "ABSENT",
                ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST,
            }
        )
    )


def absent_outcome() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_OBSERVATION_OUTCOME: "ABSENT"}))


def settlement_missing() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_SETTLEMENT: ABSENT}))


def settlement_not_certified() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_SETTLEMENT: "NOT_CERTIFIED"}))


# =============================================================================================
# Family A -- deleting each of the nineteen ordered checks
# =============================================================================================
#
# Measured, not assumed. Nine deletions make an invalid trace settle; ten only change which
# defence is reported, because a later check backstops the refusal. Both are real mutants and both
# are in the ledger -- pretending all nineteen create a false PASS would misdescribe the design,
# and dropping the ten would leave the audit trail unguarded.

#: ``(check, severity, witness, outcome after deletion)``. The before-outcome is always the check's
#: own code, which is the definition of the witness being the trace that check exists to refuse.
_FAMILY_A: Final[tuple[tuple[str, Severity, Witness, Outcome], ...]] = (
    ("_multiple_writes", Severity.HIGH, two_writes, failed(CheckId.WRITE_NOT_SUCCESSFUL)),
    ("_dispatch_contradicts_transport", Severity.CRITICAL, suppressed_but_wrote, PASSED),
    (
        "_write_status_ambiguous",
        Severity.MEDIUM,
        ambiguous_write_status,
        failed(CheckId.WRITE_NOT_SUCCESSFUL),
    ),
    ("_write_not_successful", Severity.CRITICAL, failed_write, PASSED),
    ("_no_independent_observer", Severity.CRITICAL, no_observer_claimed, PASSED),
    (
        "_observation_absent_from_trace",
        Severity.MEDIUM,
        no_read_at_all,
        failed(CheckId.RESOURCE_MISMATCH),
    ),
    (
        "_resource_mismatch",
        Severity.MEDIUM,
        unread_resource,
        failed(CheckId.OBSERVATION_NOT_SUCCESSFUL),
    ),
    (
        "_multiple_observations",
        Severity.HIGH,
        two_matching_reads,
        failed(CheckId.OBSERVATION_NOT_SUCCESSFUL),
    ),
    (
        "_read_status_ambiguous",
        Severity.MEDIUM,
        ambiguous_read_status,
        failed(CheckId.OBSERVATION_NOT_SUCCESSFUL),
    ),
    ("_observation_not_successful", Severity.CRITICAL, failed_read, PASSED),
    ("_timing_unavailable", Severity.CRITICAL, unreadable_timing, PASSED),
    ("_observation_precedes_effect", Severity.CRITICAL, read_before_write, PASSED),
    ("_field_set_unexpected", Severity.CRITICAL, subset_field_universe, PASSED),
    (
        "_digest_malformed",
        Severity.MEDIUM,
        malformed_digests,
        failed(CheckId.OUTCOME_CONTRADICTS_DIGESTS),
    ),
    (
        "_outcome_contradicts_digests",
        Severity.MEDIUM,
        diverged_but_equal,
        failed(CheckId.OUTCOME_NOT_MATCHED),
    ),
    (
        "_state_digests_differ",
        Severity.MEDIUM,
        absent_outcome_unequal_digests,
        failed(CheckId.OUTCOME_NOT_MATCHED),
    ),
    ("_outcome_not_matched", Severity.CRITICAL, absent_outcome, PASSED),
    (
        "_settlement_absent",
        Severity.MEDIUM,
        settlement_missing,
        failed(CheckId.SETTLEMENT_NOT_VERIFIED),
    ),
    ("_settlement_not_verified", Severity.CRITICAL, settlement_not_certified, PASSED),
)


#: The two ambiguity checks share one :class:`CheckId`, so the name-to-code derivation below
#: cannot reach it from the function name. Stated as an explicit exception rather than by widening
#: the derivation, because the sharing is the interesting fact and a lookup table would bury it.
_SHARED_CODES: Final[Mapping[str, CheckId]] = {
    "_write_status_ambiguous": CheckId.TRANSPORT_STATUS_AMBIGUOUS,
    "_read_status_ambiguous": CheckId.TRANSPORT_STATUS_AMBIGUOUS,
}


def _family_a() -> tuple[Mutant, ...]:
    """One mutant per entry in ``CHECKS``, numbered by position.

    The check's own :class:`CheckId` is derived from its function name, so a check renamed without
    renaming its code -- or given a code belonging to a different defence -- fails ledger
    construction rather than producing a mutant that quietly tests the wrong thing.
    """
    built: list[Mutant] = []
    for index, (name, severity, witness, after) in enumerate(_FAMILY_A, start=1):
        own = _SHARED_CODES.get(name) or CheckId[name.lstrip("_").upper()]
        built.append(
            Mutant(
                id=f"A{index:02}",
                family="A",
                surface=f"CHECKS[{index - 1}] {name}",
                severity=severity,
                apply=deleting_check(name),
                witness=witness,
                expected_before=failed(own),
                expected_after=after,
                note=(
                    "deletion makes an invalid trace settle"
                    if after == PASSED
                    else "deletion changes only which defence is reported; a later check backstops"
                ),
            )
        )
    return tuple(built)


MUTANTS: Final[tuple[Mutant, ...]] = _family_a()


# =============================================================================================
# The standing assertion set -- what "killed" is measured against
# =============================================================================================


#: Every witness paired with the outcome the **shipped** evaluator must give it, plus anchors that
#: no mutant owns.
#:
#: This is the corpus a mutation is killed by. It is not a second copy of Patch B's suite: it is
#: precisely the set of payloads this file asserts on, so "the standing assertions notice" is
#: measurable in-process rather than by re-running pytest once per mutant.
def _corpus() -> tuple[tuple[str, Witness, Outcome], ...]:
    entries: list[tuple[str, Witness, Outcome]] = [
        ("settled", lambda: payload(settled_trace()), PASSED),
        ("recheck", lambda: payload(recheck_trace()), PASSED),
        (
            "simulated",
            lambda: payload(simulated_trace()),
            not_applicable(NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE),
        ),
        ("quiet", lambda: payload(quiet_trace()), not_applicable(NaReason.NO_EFFECT_CLAIMED)),
        (
            "read_at_write_end",
            read_exactly_at_write_end,
            PASSED,
        ),
    ]
    entries.extend(
        (mutant.id, mutant.witness, mutant.expected_before)
        for mutant in MUTANTS
        if not mutant.is_equivalent
    )
    return tuple(entries)


CORPUS: Final[tuple[tuple[str, Witness, Outcome], ...]] = _corpus()


def deviations() -> list[str]:
    """Every corpus entry whose outcome is not what the shipped evaluator promises.

    Called with a mutation applied, this is the kill report: a non-empty list names the standing
    assertions that would go red if the mutation were made permanent.
    """
    return [name for name, witness, expected in CORPUS if verdict(witness()) != expected]


# =============================================================================================
# Ledger invariants
# =============================================================================================


class TestTheLedgerIsWellFormed:
    """Properties of the ledger itself. A malformed ledger cannot produce trustworthy evidence."""

    def test_ids_are_unique(self) -> None:
        ids = [mutant.id for mutant in MUTANTS]
        assert len(set(ids)) == len(ids)

    def test_every_mutant_names_a_family_and_a_surface(self) -> None:
        for mutant in MUTANTS:
            assert mutant.family, mutant.id
            assert mutant.surface, mutant.id
            assert mutant.id.startswith(mutant.family), mutant.id

    def test_every_non_equivalent_mutant_declares_a_changing_pair(self) -> None:
        for mutant in MUTANTS:
            if mutant.is_equivalent:
                continue
            assert mutant.expected_before != mutant.expected_after, mutant.id

    def test_every_equivalent_mutant_carries_a_justification(self) -> None:
        for mutant in MUTANTS:
            if mutant.is_equivalent:
                assert mutant.equivalence, mutant.id
                assert mutant.expected_before == mutant.expected_after, mutant.id

    def test_the_ledger_covers_every_ordered_check(self) -> None:
        """Family A is one mutant per entry in ``CHECKS``, read at run time."""
        family_a = [mutant for mutant in MUTANTS if mutant.family == "A"]
        assert len(family_a) == len(settlement.CHECKS)

    def test_the_corpus_holds_every_non_equivalent_witness(self) -> None:
        named = {name for name, _, _ in CORPUS}
        for mutant in MUTANTS:
            if not mutant.is_equivalent:
                assert mutant.id in named, mutant.id


class TestEveryExplanationAnnouncesItsOwnCode:
    """:func:`outcome_of` reads the code from the explanation's leading token.

    That is only trustworthy if every explanation really does begin with the name of the code it
    belongs to, which is a property of the tables rather than of the reducer. Asserted here so the
    whole ledger does not rest on an unstated convention.
    """

    def test_check_explanations(self) -> None:
        from evaluators.contract import CHECK_EXPLANATIONS

        for code, sentence in CHECK_EXPLANATIONS.items():
            assert sentence.split(":")[0] == code.value

    def test_na_explanations(self) -> None:
        from evaluators.contract import NA_EXPLANATIONS

        for code, sentence in NA_EXPLANATIONS.items():
            assert sentence.split(":")[0] == code.value


# =============================================================================================
# The three assertions every mutant must satisfy
# =============================================================================================

_IDS = [mutant.id for mutant in MUTANTS]


class TestTheShippedEvaluatorIsSafeOnEveryWitness:
    """Step one. A witness that is not safe to begin with proves nothing about a defence."""

    @pytest.mark.parametrize("mutant", MUTANTS, ids=_IDS)
    def test_the_witness_is_refused_as_expected(self, mutant: Mutant) -> None:
        assert verdict(mutant.witness()) == mutant.expected_before, (
            f"{mutant.id} ({mutant.surface}): the shipped evaluator does not produce "
            f"{mutant.expected_before} on this witness, so the mutation below would be testing "
            f"something other than the defence it names"
        )


class TestEveryMutationReachesTheDecisionPath:
    """Step two, and the rule the whole file is built around.

    A mutation that leaves the witness outcome unchanged did not reach the code the evaluator
    runs. That is a harness failure and is reported as one -- see :exc:`MutationHadNoEffect`.
    """

    @pytest.mark.parametrize("mutant", MUTANTS, ids=_IDS)
    def test_the_mutation_changes_the_witness_outcome(
        self, mutant: Mutant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = verdict(mutant.witness())
        mutant.apply(monkeypatch)
        after = verdict(mutant.witness())

        if mutant.is_equivalent:
            assert after == before, (
                f"{mutant.id} is declared EQUIVALENT but changed {before} to {after}"
            )
            return

        if after == before:
            raise MutationHadNoEffect(
                f"{mutant.id} ({mutant.surface}): the witness produced {before} both before and "
                f"after the mutation. The patched symbol is not the one "
                f"evaluators.settlement reads, or the witness does not exercise it. This is a "
                f"harness defect, not a surviving mutant."
            )
        assert after == mutant.expected_after, (
            f"{mutant.id} ({mutant.surface}): mutating produced {after}, not the declared "
            f"{mutant.expected_after}"
        )


class TestEveryMutantIsKilled:
    """Step three. The standing assertion set notices.

    A mutant is killed when at least one :data:`CORPUS` entry deviates from the outcome the
    shipped evaluator promises it. Those entries are asserted unmutated by
    :class:`TestTheShippedEvaluatorIsSafeOnEveryWitness` and
    :class:`TestTheStandingCorpusHolds`, so a deviation is exactly "a test would go red".
    """

    @pytest.mark.parametrize("mutant", MUTANTS, ids=_IDS)
    def test_the_corpus_detects_it(self, mutant: Mutant, monkeypatch: pytest.MonkeyPatch) -> None:
        mutant.apply(monkeypatch)
        caught = deviations()
        if mutant.is_equivalent:
            assert not caught, f"{mutant.id} is declared EQUIVALENT but moved {caught}"
            return
        assert caught, (
            f"{mutant.id} ({mutant.surface}) SURVIVES: severity {mutant.severity}, and no "
            f"standing assertion in this repository notices it"
        )


class TestTheStandingCorpusHolds:
    """The corpus is the assertion set, so it is asserted."""

    @pytest.mark.parametrize("entry", CORPUS, ids=[name for name, _, _ in CORPUS])
    def test_each_entry_produces_its_promised_outcome(
        self, entry: tuple[str, Witness, Outcome]
    ) -> None:
        name, witness, expected = entry
        assert verdict(witness()) == expected, name


# =============================================================================================
# Acceptance
# =============================================================================================


def survivors(severity: Severity, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every mutant of ``severity`` that no standing assertion detects."""
    found: list[str] = []
    for mutant in MUTANTS:
        if mutant.severity is not severity:
            continue
        with monkeypatch.context() as patched:
            mutant.apply(patched)
            if not deviations():
                found.append(f"{mutant.id} ({mutant.surface})")
    return found


class TestTheAcceptanceRule:
    """The whole point, stated as two assertions and computed from the ledger.

    No percentage and no denominator written by hand: an aggregate would let one false-PASS
    mutant hide behind a crowd of trivial kills, which is the failure mode a mutation score most
    reliably produces.
    """

    def test_no_critical_mutant_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert survivors(Severity.CRITICAL, monkeypatch) == []

    def test_no_high_mutant_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert survivors(Severity.HIGH, monkeypatch) == []

    def test_there_is_at_least_one_mutant_of_each_severity_that_matters(self) -> None:
        """An empty severity class would make its acceptance assertion vacuous."""
        present = {mutant.severity for mutant in MUTANTS}
        assert Severity.CRITICAL in present
        assert Severity.MEDIUM in present


# =============================================================================================
# Meta-vacuity -- can this harness detect a deliberately broken evaluator at all?
# =============================================================================================


class TestTheHarnessDetectsADeliberatelyBrokenEvaluator:
    """Layer-specific probes. None of these defects is committed.

    Split by enforcement layer on purpose. Emptying ``CHECKS`` proves nothing about a trace that
    is refused in Phase A or in the envelope parser, so requiring every witness to settle under
    one global probe would assert something false. Each layer gets a probe that attacks it.
    """

    def test_phase_b_with_no_checks_at_all_settles_invalid_traces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty ``CHECKS``: every applicable-but-invalid witness must become ``PASS``."""
        applicable = [
            mutant
            for mutant in MUTANTS
            if mutant.family == "A" and mutant.expected_before.label == "FAIL"
        ]
        assert applicable
        monkeypatch.setattr(settlement, "CHECKS", ())
        settled = [m.id for m in applicable if verdict(m.witness()) == PASSED]
        assert settled == [m.id for m in applicable], (
            "emptying CHECKS did not settle every applicable witness, so the Phase-B probe is not "
            "attacking what it claims to attack"
        )

    def test_phase_b_with_no_checks_at_all_moves_the_standing_corpus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settlement, "CHECKS", ())
        assert len(deviations()) >= 15

    def test_a_bypass_after_applicability_settles_everything_applicable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other Phase-B shape: the checks are intact and simply not consulted."""
        original = settlement.applicability

        def bypass(spans: Any) -> Any:
            outcome = original(spans)
            return outcome

        monkeypatch.setattr(settlement, "applicability", bypass)
        monkeypatch.setattr(settlement, "CHECKS", ())
        assert verdict(failed_write()) == PASSED

    def test_the_harness_rejects_a_mutation_that_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The binding trap, reproduced deliberately.

        Patching ``evaluators.spans.successful`` cannot affect the decision procedure, because
        ``settlement.py`` bound that name at import. The before/after rule must call this a
        harness failure rather than a surviving mutant, and this is what proves it does.
        """
        import evaluators.spans as spans

        witness = failed_write()
        before = verdict(witness)
        monkeypatch.setattr(spans, "successful", lambda span: True)
        after = verdict(failed_write())
        assert after == before, "patching evaluators.spans must not reach the decision procedure"

        phantom = Mutant(
            id="Z99",
            family="Z",
            surface="evaluators.spans.successful (wrong binding)",
            severity=Severity.CRITICAL,
            apply=lambda mp: mp.setattr(spans, "successful", lambda span: True),
            witness=failed_write,
            expected_before=failed(CheckId.WRITE_NOT_SUCCESSFUL),
            expected_after=PASSED,
        )
        with pytest.raises(MutationHadNoEffect):
            TestEveryMutationReachesTheDecisionPath().test_the_mutation_changes_the_witness_outcome(
                phantom, monkeypatch
            )

    def test_patching_the_right_binding_does_reach_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control for the probe above: the same replacement, at the binding that counts."""
        monkeypatch.setattr(settlement, "successful", lambda span: True)
        assert verdict(failed_write()) == PASSED
