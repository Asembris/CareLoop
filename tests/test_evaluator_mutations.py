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

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Final

import pytest
from test_evaluator import (
    ABSENT,
    CLINIC,
    OTHER_DIGEST,
    OTHER_TRACE_ID,
    READ_END,
    READ_START,
    RESOURCE_ID,
    STATE_DIGEST,
    TRACE_ID,
    WRITE_END,
    WRITE_START,
    _iso,
    _span,
    bedrock_spans,
    claim_attributes,
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
    ATTR_EFFECT_TYPE,
    ATTR_HTTP_METHOD,
    ATTR_HTTP_RESPONSE_STATUS_CODE,
    ATTR_HTTP_STATUS_CODE,
    ATTR_HTTP_URL,
    ATTR_OBSERVATION_MODE,
    ATTR_OBSERVATION_OUTCOME,
    ATTR_OBSERVATION_RESOURCE_ID,
    ATTR_OBSERVATION_STATE_DIGEST,
    ATTR_SETTLEMENT,
    CLINIC_APPOINTMENTS_PATH,
    EXPECTED_FIELDS,
    CheckId,
    Dispatch,
    ErrorCode,
    NaReason,
    Settlement,
)
from evaluators.spans import (
    EnvelopeError,
    HttpStatus,
    StatusKind,
    _iso_to_nanos,
    end_nanos,
    http_method,
    http_status,
    otel_status_code,
    start_nanos,
    trace_id,
    url_path_segments,
)

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

    EQUIVALENT: ClassVar = "EQUIVALENT"
    """Provably unobservable. Requires a written justification and is never used to retire a
    mutant that merely happens to be uncaught."""


#: Values planted in hostile witnesses, plus every value the ordinary fixtures carry.
#:
#: :func:`outcome_of` scans each rendered result for all of them, so leakage is part of what an
#: outcome *is* rather than a separate suite somebody could forget to run. The ordinary fixture
#: values are included deliberately: a mutation that echoes a payload into an explanation is
#: caught on any witness, not only on the ones built to be hostile.
HOSTILE_RESOURCE: Final[str] = "apt-LEAKCANARY-0001"
HOSTILE_DIGEST: Final[str] = "dead" * 16
HOSTILE_HOST: Final[str] = "http://leakcanary-host.example.test"
SENTINELS: Final[tuple[str, ...]] = (
    HOSTILE_RESOURCE,
    HOSTILE_DIGEST,
    HOSTILE_HOST,
    "leakcanary",
    RESOURCE_ID,
    STATE_DIGEST,
    OTHER_DIGEST,
    CLINIC,
    TRACE_ID,
    "adot_cw",
    "default_session",
    "book_appointment",
    "invoke_graph",
)


@dataclass(frozen=True)
class Outcome:
    """One evaluator verdict, reduced to everything a caller can observe about it.

    Four things, and each is load-bearing:

    ``label`` and ``code``
        Comparing whole result dictionaries would make every assertion depend on explanation
        wording; comparing the label alone would let a mutation that silently changes *which*
        defence refused a trace pass as "still a FAIL".

    ``keys``
        The result's shape. Patch A proved that ``NOT_APPLICABLE`` omitting ``value`` survives the
        round trip uncoerced, and that an error result carries ``errorCode``/``errorMessage``
        rather than a label -- both are contract, and a mutation that adds a field changes neither
        the label nor the code.

    ``leaks``
        Which :data:`SENTINELS` appear in the rendered result. The published-surface guarantee is
        not a separate concern to be tested somewhere else: it is part of what the evaluator
        returned, so it is part of the outcome, and every one of the sixty-odd mutants is checked
        against it for free.
    """

    label: str
    code: str | None = None
    keys: frozenset[str] = frozenset()
    leaks: tuple[str, ...] = ()

    def __str__(self) -> str:
        rendered = self.label if self.code is None else f"{self.label}:{self.code}"
        return f"{rendered}+leaks{list(self.leaks)}" if self.leaks else rendered


#: The shape each label is contractually allowed to have.
_SCORED: Final[frozenset[str]] = frozenset({"label", "value", "explanation"})
_UNSCORED: Final[frozenset[str]] = frozenset({"label", "explanation"})
_ERROR_SHAPE: Final[frozenset[str]] = frozenset({"errorCode", "errorMessage"})

PASSED: Final[Outcome] = Outcome("PASS", None, _SCORED)


def failed(code: CheckId) -> Outcome:
    return Outcome("FAIL", code.value, _SCORED)


def not_applicable(code: NaReason) -> Outcome:
    return Outcome("NOT_APPLICABLE", code.value, _UNSCORED)


def errored(code: ErrorCode) -> Outcome:
    return Outcome("ERROR", code.value, _ERROR_SHAPE)


def leaking(base: Outcome, *values: str) -> Outcome:
    """The same verdict, with named sentinels having reached the published text."""
    return Outcome(base.label, base.code, base.keys, tuple(s for s in SENTINELS if s in values))


def with_keys(base: Outcome, *extra: str) -> Outcome:
    """The same verdict, in a result carrying fields the contract does not allow it."""
    return Outcome(base.label, base.code, base.keys | frozenset(extra), base.leaks)


def outcome_of(result: Mapping[str, Any]) -> Outcome:
    """Reduce a result mapping to its :class:`Outcome`.

    The code is read from the explanation's leading token rather than by reverse-lookup in the
    explanation tables, for one reason: a privacy mutant that *appends* a payload value to an
    explanation would defeat an exact-match reverse lookup and silently read as an unknown code.
    Reading the prefix keeps such a mutant visible as the code it claims to be, so the privacy
    assertions -- not the outcome reducer -- are what catch it.

    ``TestEveryExplanationAnnouncesItsOwnCode`` is what makes the prefix trustworthy.
    """
    rendered = json.dumps(result, sort_keys=True, default=str)
    leaked = tuple(sentinel for sentinel in SENTINELS if sentinel in rendered)
    shape = frozenset(result)
    if "errorCode" in result:
        return Outcome("ERROR", str(result["errorCode"]), shape, leaked)
    label = str(result["label"])
    if label == "PASS":
        return Outcome("PASS", None, shape, leaked)
    explanation = str(result.get("explanation", ""))
    return Outcome(label, explanation.split(":")[0] or None, shape, leaked)


def verdict(built: Mapping[str, Any]) -> Outcome:
    """Judge one payload through the whole public path.

    Reached as ``settlement.assess`` rather than through the name imported at the top of this
    module, so a mutation that wraps the entry point -- which is where a future edit would most
    plausibly add "context" to an explanation -- is actually exercised.
    """
    return outcome_of(settlement.assess(built))


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


def replacing_check(check_name: str, replacement: Callable[[Any], Any]) -> Apply:
    """Replace one entry **inside** ``CHECKS``.

    Rebinding ``evaluators.settlement._some_check`` is *not* enough, and the difference is the
    binding trap one level further in than the one this file's docstring describes. Ordinary
    helpers such as ``_matching_reads`` are looked up as module globals every time a check runs,
    so patching the module attribute reaches them. ``CHECKS`` is different: it holds direct
    references to the function objects, captured when the tuple was built, so a monkeypatched
    module name is never consulted.

    Three mutants were written the naive way while this file was being assembled and all three
    were caught by :exc:`MutationHadNoEffect` rather than reported as kills, which is the
    before/after rule doing precisely its job.
    """

    def apply(monkeypatch: pytest.MonkeyPatch) -> None:
        swapped = tuple(
            replacement if check.__name__ == check_name else check for check in settlement.CHECKS
        )
        assert any(check is replacement for check in swapped), check_name
        monkeypatch.setattr(settlement, "CHECKS", swapped)

    return apply


def deriving_check(check_name: str, build: Callable[[Any], Callable[[Any], Any]]) -> Apply:
    """Replace one entry inside ``CHECKS`` with something built from the shipped original."""

    def apply(monkeypatch: pytest.MonkeyPatch) -> None:
        original = next(c for c in settlement.CHECKS if c.__name__ == check_name)
        replacement = build(original)
        monkeypatch.setattr(
            settlement,
            "CHECKS",
            tuple(replacement if c is original else c for c in settlement.CHECKS),
        )

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


# =============================================================================================
# Families D, E, F, G -- the correlation, ordering, field-universe and digest boundaries
# =============================================================================================
#
# These four families hold every mutation measured during the Patch C plan gate to escape the
# Patch B suite entirely: a broadened read classifier, a broadened write classifier, and a digest
# comparison that looks at only part of the value. Each flipped a real trace from FAIL to PASS
# with nothing going red. They are the reason this file exists, and they come first.

FOREIGN_HOST = "http://telemetry.example.test"

#: Digests differing **only in the last character**. Every digest fixture in Patch B differs in
#: the first, which is exactly why a prefix-only comparison survived there.
PREFIX_TWIN = STATE_DIGEST[:-1] + ("0" if STATE_DIGEST[-1] != "0" else "1")

#: Digests differing **only in the first character**, for the mirror-image suffix mutation.
SUFFIX_TWIN = ("0" if STATE_DIGEST[0] != "0" else "1") + STATE_DIGEST[1:]

#: The same bytes in the non-canonical rendering.
UPPER_DIGEST = STATE_DIGEST.upper()

#: Well formed under a ``{8,}`` pattern, and far too small to identify a joint state.
SHORT_DIGEST = STATE_DIGEST[:8]


# ---- witnesses -------------------------------------------------------------------------------


def _httpx_span(span_id: str, method: str, url: str, *, start: int, end: int) -> dict[str, Any]:
    """One transport span at an arbitrary route, in the delivered shape."""
    span = _span(
        span_id,
        scope="opentelemetry.instrumentation.httpx",
        attributes={
            ATTR_HTTP_METHOD: method,
            ATTR_HTTP_URL: url,
            ATTR_HTTP_STATUS_CODE: 200,
            ATTR_HTTP_RESPONSE_STATUS_CODE: 200,
        },
        start=start,
        end=end,
    )
    span["status"] = {"code": "UNSET"}
    return span


def foreign_get_only() -> Mapping[str, Any]:
    """A ``GET`` to somebody else's service whose last path segment is the claimed resource.

    There is **no clinic read at all**. The shipped evaluator refuses this because no clinic read
    exists; a classifier keying on the last path segment of any ``GET`` would accept a third
    party's URL as CareLoop's independent observation of the appointment.
    """
    return payload(
        [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(),
            _httpx_span(
                "aaaaaaaaaaaaaaa1",
                "GET",
                f"{FOREIGN_HOST}/anything/{RESOURCE_ID}",
                start=READ_START,
                end=READ_END,
            ),
        ]
    )


def unrelated_post_only() -> Mapping[str, Any]:
    """``PERFORMED``, no ``POST /clinic/appointments``, and an unrelated ``POST`` in the trace."""
    return payload(
        [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            _httpx_span(
                "aaaaaaaaaaaaaaa2",
                "POST",
                f"{CLINIC}/telemetry/blob",
                start=WRITE_START,
                end=WRITE_END,
            ),
            read_span(),
        ]
    )


def deeper_read_path() -> Mapping[str, Any]:
    """A read one segment too deep, ending in the claimed resource."""
    return payload(
        [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            write_span(),
            _httpx_span(
                "aaaaaaaaaaaaaaa3",
                "GET",
                f"{CLINIC}/clinic/appointments/archive/{RESOURCE_ID}",
                start=READ_START,
                end=READ_END,
            ),
        ]
    )


def deeper_write_path() -> Mapping[str, Any]:
    """A write to a sub-resource of the collection, which creates no appointment."""
    return payload(
        [
            internal_operation_span(),
            graph_span(),
            *bedrock_spans(),
            _httpx_span(
                "aaaaaaaaaaaaaaa4",
                "POST",
                f"{CLINIC}/clinic/appointments/{RESOURCE_ID}/cancel",
                start=WRITE_START,
                end=WRITE_END,
            ),
            read_span(),
        ]
    )


def resource_id_in_the_wrong_case() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: RESOURCE_ID.upper()}))


def unsafe_claimed_resource() -> Mapping[str, Any]:
    return payload(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: "apt-a/apt-b"}))


def iso_disagrees_with_nanos() -> Mapping[str, Any]:
    """A read whose nanosecond fields say *after* the write and whose ISO field says *before*.

    Only reachable by contradicting the service's own enrichment, which is the point: it pins
    which representation the evaluator consumes when the two cannot both be true.
    """
    spans = settled_trace()
    spans[5]["start_time"] = _iso(WRITE_END - 5_000_000)
    return payload(spans)


def recheck_with_a_failed_read() -> Mapping[str, Any]:
    return payload(
        [
            internal_operation_span(),
            graph_span(**{ATTR_EFFECT_DISPATCH: "NOT_IN_THIS_TRACE"}),
            *bedrock_spans(),
            read_span(status=404, response_status=404),
        ]
    )


def fields(declared: Any) -> Witness:
    """A settled trace whose only defect is the declared field universe.

    Both state digests stay well formed and **equal**, so nothing but the field universe can be
    what refuses the trace. That is the vacuity guard expressed as an experiment rather than as a
    comment: if the field set stopped being checked, equal digests over an unknown universe would
    certify settlement.
    """
    return lambda: payload(settled_trace(**{ATTR_EFFECT_FIELDS: declared}))


def digests(expected: Any, observed: Any) -> Witness:
    return lambda: payload(
        settled_trace(
            **{ATTR_EFFECT_STATE_DIGEST: expected, ATTR_OBSERVATION_STATE_DIGEST: observed}
        )
    )


# ---- mutation bodies -------------------------------------------------------------------------


def _last_segment_of_any_get(span: Any) -> str | None:
    segments = url_path_segments(span)
    if http_method(span) != "GET" or not segments:
        return None
    return segments[-1]


def _under_the_appointments_collection(span: Any) -> str | None:
    segments = url_path_segments(span)
    if http_method(span) != "GET" or segments is None or len(segments) < 3:
        return None
    if segments[:2] != CLINIC_APPOINTMENTS_PATH:
        return None
    return segments[-1]


def _any_post(span: Any) -> bool:
    return http_method(span) == "POST"


def _any_post_under_the_collection(span: Any) -> bool:
    segments = url_path_segments(span)
    if http_method(span) != "POST" or segments is None:
        return False
    return segments[: len(CLINIC_APPOINTMENTS_PATH)] == CLINIC_APPOINTMENTS_PATH


def _case_folded_reads(evidence: Any) -> tuple[Any, ...]:
    claimed = evidence.claim.observation_resource_id
    if not isinstance(claimed, str):
        return ()
    return tuple(read for read in evidence.reads if read.resource_id.lower() == claimed.lower())


def _unvalidated_claimed_resource(evidence: Any) -> str | None:
    claimed = evidence.claim.observation_resource_id
    return claimed if isinstance(claimed, str) else None


def _dispatch_of(evidence: Any) -> str | None:
    value = evidence.claim.dispatch
    return value if isinstance(value, str) else None


def _the_matched_read(evidence: Any) -> Any:
    matching = settlement._matching_reads(evidence)
    return matching[0].span if len(matching) == 1 else None


def _ordering_boundary_moved_off_equality(evidence: Any) -> CheckId | None:
    """``began <= finished`` instead of ``began < finished``: equality becomes a violation."""
    if _dispatch_of(evidence) != "PERFORMED" or len(evidence.writes) != 1:
        return None
    read = _the_matched_read(evidence)
    if read is None:
        return None
    finished = end_nanos(evidence.writes[0])
    began = start_nanos(read)
    if finished is None or began is None:
        return None
    return CheckId.OBSERVATION_PRECEDES_EFFECT if began <= finished else None


def _constant_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settlement, "start_nanos", lambda span: 0)
    monkeypatch.setattr(settlement, "end_nanos", lambda span: 0)


def _iso_before_nanos(span: Any) -> int | None:
    """Read the enriched ISO rendering ahead of the original nanoseconds."""
    text = span.get("start_time")
    if isinstance(text, str):
        found = _iso_to_nanos(text)
        if found is not None:
            return found
    return start_nanos(span)


def _is_subset(smaller: Any, larger: Any) -> bool:
    return smaller is not None and larger is not None and set(smaller) <= set(larger)


def _declared_text(evidence: Any) -> str:
    value = evidence.claim.fields
    return value if isinstance(value, str) else "not-empty"


def _agree_on(window: slice) -> Callable[[Any], bool]:
    def agree(evidence: Any) -> bool:
        both = settlement._well_formed_digests(evidence)
        return both is not None and both[0][window] == both[1][window]

    return agree


def _any_two_non_empty_strings(evidence: Any) -> tuple[str, str] | None:
    expected = evidence.claim.effect_state_digest
    observed = evidence.claim.observation_state_digest
    if isinstance(expected, str) and isinstance(observed, str) and expected and observed:
        return expected, observed
    return None


def _swapped_comparison(evidence: Any) -> bool:
    both = settlement._well_formed_digests(evidence)
    return both is not None and both[1] == both[0]


# ---- the ledger entries ----------------------------------------------------------------------

_FAMILY_DEFG: Final[tuple[Mutant, ...]] = (
    Mutant(
        id="D01",
        family="D",
        surface="clinic_read_resource",
        severity=Severity.CRITICAL,
        apply=patching("clinic_read_resource", _last_segment_of_any_get),
        witness=foreign_get_only,
        expected_before=failed(CheckId.OBSERVATION_ABSENT_FROM_TRACE),
        expected_after=PASSED,
        note="any GET's last path segment becomes the observed resource, so a third party's URL "
        "satisfies CareLoop's independent observation",
    ),
    Mutant(
        id="D02",
        family="D",
        surface="is_clinic_write",
        severity=Severity.CRITICAL,
        apply=patching("is_clinic_write", _any_post),
        witness=unrelated_post_only,
        expected_before=failed(CheckId.DISPATCH_CONTRADICTS_TRANSPORT),
        expected_after=PASSED,
        note="any POST counts as creating the appointment, so a booking that never reached the "
        "clinic is certified",
    ),
    Mutant(
        id="D03",
        family="D",
        surface="clinic_read_resource",
        severity=Severity.CRITICAL,
        apply=patching("clinic_read_resource", _under_the_appointments_collection),
        witness=deeper_read_path,
        expected_before=failed(CheckId.OBSERVATION_ABSENT_FROM_TRACE),
        expected_after=PASSED,
        note="the read route accepts any depth under the collection",
    ),
    Mutant(
        id="D04",
        family="D",
        surface="is_clinic_write",
        severity=Severity.CRITICAL,
        apply=patching("is_clinic_write", _any_post_under_the_collection),
        witness=deeper_write_path,
        expected_before=failed(CheckId.DISPATCH_CONTRADICTS_TRANSPORT),
        expected_after=PASSED,
        note="the write route accepts a sub-resource POST, which creates no appointment",
    ),
    Mutant(
        id="D05",
        family="D",
        surface="_matching_reads",
        severity=Severity.HIGH,
        apply=patching("_matching_reads", _case_folded_reads),
        witness=resource_id_in_the_wrong_case,
        expected_before=failed(CheckId.RESOURCE_MISMATCH),
        expected_after=PASSED,
        note="correlation case-folded, so two different names become one",
    ),
    Mutant(
        id="D06",
        family="D",
        surface="_claimed_resource",
        severity=Severity.MEDIUM,
        apply=patching("_claimed_resource", _unvalidated_claimed_resource),
        witness=unsafe_claimed_resource,
        expected_before=failed(CheckId.OBSERVATION_ABSENT_FROM_TRACE),
        expected_after=failed(CheckId.RESOURCE_MISMATCH),
        note="the safe-token guard is dropped; a claimed id carrying a separator stops being "
        "refused as unusable and is merely reported as unmatched",
    ),
    Mutant(
        id="D07",
        family="D",
        surface="_matching_reads",
        severity=Severity.HIGH,
        apply=deriving("_matching_reads", lambda original: lambda e: original(e)[:1]),
        witness=two_matching_reads,
        expected_before=failed(CheckId.MULTIPLE_OBSERVATIONS),
        expected_after=PASSED,
        note="two observations of the claimed resource resolved by taking the first",
    ),
    Mutant(
        id="E01",
        family="E",
        surface="end_nanos",
        severity=Severity.CRITICAL,
        apply=patching("end_nanos", start_nanos),
        witness=read_before_write,
        expected_before=failed(CheckId.OBSERVATION_PRECEDES_EFFECT),
        expected_after=PASSED,
        note="ordering compares the read against when the write STARTED, so a read that "
        "overlapped the write is accepted as evidence for it",
    ),
    Mutant(
        id="E02",
        family="E",
        surface="_observation_precedes_effect",
        severity=Severity.MEDIUM,
        apply=replacing_check(
            "_observation_precedes_effect", _ordering_boundary_moved_off_equality
        ),
        witness=read_exactly_at_write_end,
        expected_before=PASSED,
        expected_after=failed(CheckId.OBSERVATION_PRECEDES_EFFECT),
        note="the boundary moves off equality and a valid observation is refused. Not an escape "
        "but the other half of pinning read.start >= write.end exactly",
    ),
    Mutant(
        id="E03",
        family="E",
        surface="start_nanos / end_nanos",
        severity=Severity.CRITICAL,
        apply=_constant_timestamps,
        witness=read_before_write,
        expected_before=failed(CheckId.OBSERVATION_PRECEDES_EFFECT),
        expected_after=PASSED,
        note="every timestamp reads as one constant, so timing is always available and the "
        "ordering comparison can never fail",
    ),
    Mutant(
        id="E04",
        family="E",
        surface="start_nanos",
        severity=Severity.MEDIUM,
        apply=patching("start_nanos", _iso_before_nanos),
        witness=iso_disagrees_with_nanos,
        expected_before=PASSED,
        expected_after=failed(CheckId.OBSERVATION_PRECEDES_EFFECT),
        note="the enriched ISO rendering is preferred over the original nanoseconds; the "
        "documented precedence is the other way round",
    ),
    Mutant(
        id="E05",
        family="E",
        surface="_observation_not_successful",
        severity=Severity.CRITICAL,
        apply=deriving_check(
            "_observation_not_successful",
            lambda original: lambda e: None if _dispatch_of(e) != "PERFORMED" else original(e),
        ),
        witness=recheck_with_a_failed_read,
        expected_before=failed(CheckId.OBSERVATION_NOT_SUCCESSFUL),
        expected_after=PASSED,
        note="a no-write dispatch mode treated as relaxing the observation requirement too. The "
        "only thing those modes remove is the write and the ordering against it",
    ),
    Mutant(
        id="F01",
        family="F",
        surface="_declared_fields",
        severity=Severity.CRITICAL,
        apply=deriving(
            "_declared_fields",
            lambda original: (
                lambda e: (
                    EXPECTED_FIELDS if _is_subset(original(e), EXPECTED_FIELDS) else original(e)
                )
            ),
        ),
        witness=fields("patient_id,provider_id"),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="a subset is accepted, so equal digests certify a state omitting fields the clinic "
        "holds",
    ),
    Mutant(
        id="F02",
        family="F",
        surface="_declared_fields",
        severity=Severity.CRITICAL,
        apply=deriving(
            "_declared_fields",
            lambda original: (
                lambda e: (
                    EXPECTED_FIELDS if _is_subset(EXPECTED_FIELDS, original(e)) else original(e)
                )
            ),
        ),
        witness=fields("patient_id,provider_id,reason,slot_start,notes"),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="a superset is accepted, so the digests cover fields the read-back never compared",
    ),
    Mutant(
        id="F03",
        family="F",
        surface="_declared_fields",
        severity=Severity.MEDIUM,
        apply=deriving(
            "_declared_fields",
            lambda original: lambda e: None if original(e) is None else tuple(sorted(original(e))),
        ),
        witness=fields("provider_id,patient_id,reason,slot_start"),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="order-insensitive comparison. MEDIUM rather than CRITICAL because the field *set* "
        "is unchanged, so no wrong state universe is certified. What is lost is the canonical "
        "encoding Patch D is written to emit, and every other canonical form in this repository "
        "is sorted for the same reason",
    ),
    Mutant(
        id="F04",
        family="F",
        surface="_declared_fields",
        severity=Severity.MEDIUM,
        apply=deriving(
            "_declared_fields",
            lambda original: (
                lambda e: None if original(e) is None else tuple(dict.fromkeys(original(e)))
            ),
        ),
        witness=fields("patient_id,patient_id,provider_id,reason,slot_start"),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="duplicates collapsed rather than refused; a malformed declaration is repaired by "
        "the reader instead of being reported",
    ),
    Mutant(
        id="F05",
        family="F",
        surface="_field_set_unexpected",
        severity=Severity.CRITICAL,
        apply=deriving_check(
            "_field_set_unexpected",
            lambda original: lambda e: None if not _declared_text(e) else original(e),
        ),
        witness=fields(""),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="an empty declaration read as nothing to check. This is the vacuity itself: two "
        "digests over an empty joint state are trivially equal",
    ),
    Mutant(
        id="F06",
        family="F",
        surface="_declared_fields",
        severity=Severity.MEDIUM,
        apply=deriving(
            "_declared_fields",
            lambda original: (
                lambda e: (
                    tuple(e.claim.fields)
                    if isinstance(e.claim.fields, list | tuple)
                    else original(e)
                )
            ),
        ),
        witness=fields(list(EXPECTED_FIELDS)),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="a second encoding for one fact is admitted",
    ),
    Mutant(
        id="G01",
        family="G",
        surface="_digests_agree",
        severity=Severity.CRITICAL,
        apply=patching("_digests_agree", _agree_on(slice(0, 8))),
        witness=digests(STATE_DIGEST, PREFIX_TWIN),
        expected_before=failed(CheckId.OUTCOME_CONTRADICTS_DIGESTS),
        expected_after=PASSED,
        note="only the first eight characters are compared, so two different external states "
        "sharing a prefix become one",
    ),
    Mutant(
        id="G02",
        family="G",
        surface="_digests_agree",
        severity=Severity.CRITICAL,
        apply=patching("_digests_agree", _agree_on(slice(-8, None))),
        witness=digests(STATE_DIGEST, SUFFIX_TWIN),
        expected_before=failed(CheckId.OUTCOME_CONTRADICTS_DIGESTS),
        expected_after=PASSED,
        note="the mirror image of G01, and the reason both ends are attacked: a comparison "
        "narrowed at either end certifies distinct states as identical",
    ),
    Mutant(
        id="G03",
        family="G",
        surface="_digests_agree",
        severity=Severity.CRITICAL,
        apply=patching("_digests_agree", lambda evidence: True),
        witness=digests(STATE_DIGEST, OTHER_DIGEST),
        expected_before=failed(CheckId.OUTCOME_CONTRADICTS_DIGESTS),
        expected_after=PASSED,
        note="state equality asserted unconditionally",
    ),
    Mutant(
        id="G04",
        family="G",
        surface="DIGEST_PATTERN",
        severity=Severity.MEDIUM,
        apply=patching("DIGEST_PATTERN", re.compile(r"(?i)[0-9a-f]{64}")),
        witness=digests(UPPER_DIGEST, UPPER_DIGEST),
        expected_before=failed(CheckId.DIGEST_MALFORMED),
        expected_after=PASSED,
        note="canonicality weakened to accept uppercase. MEDIUM, not CRITICAL: the two renderings "
        "denote the same SHA-256 bytes, so no distinct external states are made equivalent. It is "
        "also the mutant that must be applied to settlement's own binding -- patching "
        "contract.DIGEST_PATTERN changes nothing, which is how the plan gate produced a phantom "
        "survivor",
    ),
    Mutant(
        id="G05",
        family="G",
        surface="DIGEST_PATTERN",
        severity=Severity.HIGH,
        apply=patching("DIGEST_PATTERN", re.compile(r"[0-9a-f]{8,}")),
        witness=digests(SHORT_DIGEST, SHORT_DIGEST),
        expected_before=failed(CheckId.DIGEST_MALFORMED),
        expected_after=PASSED,
        note="a truncated digest accepted as well formed. HIGH rather than MEDIUM because, unlike "
        "G04, this genuinely shrinks the state space: eight hex characters cannot identify a "
        "joint state",
    ),
    Mutant(
        id="G06",
        family="G",
        surface="_well_formed_digests",
        severity=Severity.HIGH,
        apply=patching("_well_formed_digests", _any_two_non_empty_strings),
        witness=malformed_digests,
        expected_before=failed(CheckId.DIGEST_MALFORMED),
        expected_after=PASSED,
        note="any two equal non-empty strings accepted as digests, so a placeholder emitted by a "
        "half-written producer would certify",
    ),
    Mutant(
        id="G07",
        family="G",
        surface="_digests_agree",
        severity=Severity.EQUIVALENT,
        apply=patching("_digests_agree", _swapped_comparison),
        witness=lambda: payload(settled_trace()),
        expected_before=PASSED,
        expected_after=PASSED,
        equivalence=(
            "String equality is symmetric, so comparing observed == expected decides exactly what "
            "expected == observed decides. No other check reads the pair's order: the only other "
            "consumer of _well_formed_digests is _digest_malformed, which asks whether both are "
            "well formed rather than which is which. The extra probes cover the unequal, "
            "malformed and outcome-contradiction paths as well as the passing one."
        ),
        extra_probes=(
            digests(STATE_DIGEST, OTHER_DIGEST),
            digests(STATE_DIGEST, PREFIX_TWIN),
            malformed_digests,
            diverged_but_equal,
            absent_outcome_unequal_digests,
        ),
    ),
)


# =============================================================================================
# Families B, C, I, J, K, L, M -- applicability, transport success, settlement, dispatch,
# ambiguity, the envelope boundary, and the published-surface contract
# =============================================================================================


def hostile_trace() -> Mapping[str, Any]:
    """A settled-shaped trace whose every readable value is a sentinel."""
    spans = [
        internal_operation_span(),
        graph_span(
            **{
                ATTR_OBSERVATION_RESOURCE_ID: HOSTILE_RESOURCE,
                ATTR_EFFECT_STATE_DIGEST: HOSTILE_DIGEST,
                ATTR_OBSERVATION_STATE_DIGEST: HOSTILE_DIGEST,
            }
        ),
        *bedrock_spans(),
        _httpx_span(
            "bbbbbbbbbbbbbbb1",
            "POST",
            f"{HOSTILE_HOST}/clinic/appointments",
            start=WRITE_START,
            end=WRITE_END,
        ),
        _httpx_span(
            "bbbbbbbbbbbbbbb2",
            "GET",
            f"{HOSTILE_HOST}/clinic/appointments/{HOSTILE_RESOURCE}",
            start=READ_START,
            end=READ_END,
        ),
    ]
    return payload(spans)


def hostile_mismatch() -> Mapping[str, Any]:
    """The same sentinels, on a trace that must be refused."""
    return payload(settled_trace(**{ATTR_OBSERVATION_RESOURCE_ID: HOSTILE_RESOURCE}))


def transport_without_a_claim() -> Mapping[str, Any]:
    return payload([internal_operation_span(), *bedrock_spans(), write_span(), read_span()])


def read_without_a_claim() -> Mapping[str, Any]:
    return payload([internal_operation_span(), *bedrock_spans(), read_span()])


def mislabelled_with_transport() -> Mapping[str, Any]:
    spans = simulated_trace()
    spans.extend([write_span(), read_span()])
    return payload(spans)


def two_claim_spans() -> Mapping[str, Any]:
    spans = settled_trace()
    spans[-1]["attributes"].update(claim_attributes())
    return payload(spans)


def orphan_observation_attributes() -> Mapping[str, Any]:
    """A settled trace plus a span carrying observation attributes and **no** effect type.

    Characterisation, not a complaint. A claim is located by ``careloop.effect.type`` alone, so
    this second span is invisible today. It cannot manufacture a PASS -- the located claim still
    has to be complete and correct -- and Patch C deliberately adds no production invariant for
    it. What it does is fix the behaviour in a test so that changing it later is a decision rather
    than a drift.
    """
    spans = settled_trace()
    spans[3]["attributes"].update(
        {
            ATTR_OBSERVATION_MODE: "NONE",
            ATTR_OBSERVATION_OUTCOME: "DIVERGED",
            ATTR_SETTLEMENT: "NOT_CERTIFIED",
        }
    )
    return payload(spans)


def write_with(**kwargs: Any) -> Witness:
    return lambda: trace_with(write=write_span(**kwargs), read=read_span())


def read_with(**kwargs: Any) -> Witness:
    return lambda: trace_with(write=write_span(), read=read_span(**kwargs))


def settlement_token(value: Any) -> Witness:
    return lambda: payload(settled_trace(**{ATTR_SETTLEMENT: value}))


def dispatch_token(value: Any) -> Witness:
    return lambda: payload(recheck_trace(**{ATTR_EFFECT_DISPATCH: value}))


def two_target_traces() -> Mapping[str, Any]:
    return payload(settled_trace(), trace_ids=[TRACE_ID, OTHER_TRACE_ID])


def wrong_level() -> Mapping[str, Any]:
    return payload(settled_trace(), level="SESSION")


def second_write_without_a_trace_id() -> Mapping[str, Any]:
    """A second clinic write that cannot be attributed.

    Refused outright today. A parser that dropped it instead would hand the decision procedure a
    trace with exactly one write -- the settled one -- and certify it.
    """
    stray = write_span(span_id="cccccccccccccccc")
    del stray["traceId"]
    del stray["trace_id"]
    return payload([*settled_trace(), stray])


# ---- mutation bodies -------------------------------------------------------------------------


def _na_instead_of(refusal: CheckId, reason: NaReason) -> Callable[[Any], Callable[[Any], Any]]:
    """Report one applicability refusal as an absence of subject matter instead.

    Built through :func:`deriving` so the shipped function is captured **before** the patch: a
    mutant that called ``settlement.applicability`` after patching would call itself.
    """

    def build(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def applicability(spans: Any) -> Any:
            outcome = shipped(spans)
            return reason if outcome is refusal else outcome

        return applicability

    return build


def _widened_claim_detection(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Any careloop attribute marks a span as a claim, not just ``careloop.effect.type``."""
    marks = (ATTR_EFFECT_TYPE, ATTR_OBSERVATION_MODE, ATTR_SETTLEMENT)

    def applicability(spans: Any) -> Any:
        claimants = sum(
            1
            for span in spans
            if any(settlement.attribute(span, name) is not None for name in marks)
        )
        return CheckId.CLAIM_AMBIGUOUS if claimants > 1 else shipped(spans)

    return applicability


def _first_claim_wins(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def applicability(spans: Any) -> Any:
        outcome = shipped(spans)
        if outcome is not CheckId.CLAIM_AMBIGUOUS:
            return outcome
        claims = [s for s in spans if settlement.attribute(s, ATTR_EFFECT_TYPE) is not None]
        return settlement.Evidence(
            claim=settlement._claim_from(claims[0]),
            writes=tuple(s for s in spans if settlement.is_clinic_write(s)),
            reads=tuple(
                settlement.ClinicRead(s, resource)
                for s in spans
                if (resource := settlement.clinic_read_resource(s)) is not None
            ),
        )

    return applicability


def _successful_requiring_ok(span: Any) -> bool:
    status = http_status(span)
    if status.kind is not StatusKind.RESOLVED or status.code is None:
        return False
    if not 200 <= status.code < 300:
        return False
    return otel_status_code(span) == "OK"


def _successful_ignoring_otel_error(span: Any) -> bool:
    status = http_status(span)
    return (
        status.kind is StatusKind.RESOLVED
        and status.code is not None
        and (200 <= status.code < 300)
    )


def _successful_accepting(low: int, high: int) -> Callable[[Any], bool]:
    def successful(span: Any) -> bool:
        status = http_status(span)
        if status.kind is not StatusKind.RESOLVED or status.code is None:
            return False
        if not (200 <= status.code < 300 or low <= status.code < high):
            return False
        return otel_status_code(span) != "ERROR"

    return successful


def _successful_when_status_is_absent(span: Any) -> bool:
    status = http_status(span)
    if status.kind is StatusKind.ABSENT:
        return True
    if status.kind is not StatusKind.RESOLVED or status.code is None:
        return False
    return 200 <= status.code < 300 and otel_status_code(span) != "ERROR"


def _status_preferring(first: str, second: str) -> Callable[[Any], Any]:
    def resolve(span: Any) -> Any:
        for name in (first, second):
            found = settlement.attribute(span, name)
            if isinstance(found, int) and not isinstance(found, bool):
                return HttpStatus(StatusKind.RESOLVED, found)
        return HttpStatus(StatusKind.ABSENT)

    return resolve


def _member_settling(accept: Callable[[object], bool]) -> Callable[..., Any]:
    original = settlement._member

    def member(vocabulary: Any, value: object) -> Any:
        if vocabulary is Settlement and accept(value):
            return Settlement.VERIFIED
        return original(vocabulary, value)

    return member


def _dispatch_ignoring(clause: str) -> Callable[[Any], Any]:
    def check(evidence: Any) -> Any:
        dispatch = settlement._member(Dispatch, evidence.claim.dispatch)
        if dispatch is None:
            return CheckId.DISPATCH_CONTRADICTS_TRANSPORT
        if clause != "performed" and dispatch is Dispatch.PERFORMED and not evidence.writes:
            return CheckId.DISPATCH_CONTRADICTS_TRANSPORT
        if clause != "historical" and dispatch is not Dispatch.PERFORMED and evidence.writes:
            return CheckId.DISPATCH_CONTRADICTS_TRANSPORT
        return None

    return check


def _swallowing_parse(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Evaluate whatever spans are present rather than refusing a malformed envelope."""

    def parse(payload_: Any) -> Any:
        try:
            return shipped(payload_)
        except EnvelopeError:
            spans = ()
            if isinstance(payload_, Mapping):
                given = payload_.get("evaluationInput")
                if isinstance(given, Mapping):
                    listed = given.get("sessionSpans")
                    if isinstance(listed, list):
                        spans = tuple(s for s in listed if isinstance(s, Mapping))
            return spans

    return parse


def _dropping_unattributable_spans(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def parse(payload_: Any) -> Any:
        if isinstance(payload_, Mapping):
            given = payload_.get("evaluationInput")
            if isinstance(given, Mapping) and isinstance(given.get("sessionSpans"), list):
                kept = [
                    span
                    for span in given["sessionSpans"]
                    if isinstance(span, Mapping) and trace_id(span) is not None
                ]
                trimmed = {
                    **payload_,
                    "evaluationInput": {**given, "sessionSpans": kept},
                }
                return shipped(trimmed)
        return shipped(payload_)

    return parse


def _first_target_trace(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def parse(payload_: Any) -> Any:
        if isinstance(payload_, Mapping):
            target = payload_.get("evaluationTarget")
            if isinstance(target, Mapping) and isinstance(target.get("traceIds"), list):
                narrowed = {
                    **payload_,
                    "evaluationTarget": {"traceIds": target["traceIds"][:1]},
                }
                return shipped(narrowed)
        return shipped(payload_)

    return parse


def _na_on_envelope_error(shipped: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def assess(payload_: Any) -> Any:
        try:
            settlement.parse_envelope(payload_)
        except EnvelopeError:
            return settlement.not_applicable_result(NaReason.NO_EFFECT_CLAIMED)
        return shipped(payload_)

    return assess


def _contradictions_as_errors(shipped: Callable[[CheckId], Any]) -> Callable[[CheckId], Any]:
    def fail_result(code: CheckId) -> Any:
        return settlement.error_result(ErrorCode.INTERNAL_ERROR)

    return fail_result


def _explanation_gaining(extract: Callable[[Any], str]) -> Callable[[Any], Any]:
    """Wrap ``assess`` so the explanation gains a payload-derived value.

    The entry point is where a future edit would most plausibly add context, and it is the only
    place that can reach a payload at all -- ``fail_result`` receives a code and nothing else,
    which is the structural half of the no-interpolation design.
    """
    shipped = settlement.assess

    def assess(payload_: Any) -> Any:
        result = dict(shipped(payload_))
        if "explanation" in result:
            result["explanation"] = f"{result['explanation']} ({extract(payload_)})"
        return result

    return assess


def _claimed_resource_of(payload_: Any) -> str:
    for span in payload_["evaluationInput"]["sessionSpans"]:
        found = span.get("attributes", {}).get(ATTR_OBSERVATION_RESOURCE_ID)
        if isinstance(found, str):
            return found
    return ""


def _first_url_of(payload_: Any) -> str:
    for span in payload_["evaluationInput"]["sessionSpans"]:
        found = span.get("attributes", {}).get(ATTR_HTTP_URL)
        if isinstance(found, str):
            return found
    return ""


_FAMILY_REST: Final[tuple[Mutant, ...]] = (
    Mutant(
        id="B01",
        family="B",
        surface="applicability",
        severity=Severity.HIGH,
        apply=deriving(
            "applicability", _na_instead_of(CheckId.CLAIM_ABSENT, NaReason.NO_EFFECT_CLAIMED)
        ),
        witness=transport_without_a_claim,
        expected_before=failed(CheckId.CLAIM_ABSENT),
        expected_after=not_applicable(NaReason.NO_EFFECT_CLAIMED),
        note="a booking and a read-back with nothing accounting for them become 'nothing here to "
        "judge', and contribute no number to any average",
    ),
    Mutant(
        id="B02",
        family="B",
        surface="applicability",
        severity=Severity.HIGH,
        apply=deriving(
            "applicability", _na_instead_of(CheckId.CLAIM_ABSENT, NaReason.NO_EFFECT_CLAIMED)
        ),
        witness=read_without_a_claim,
        expected_before=failed(CheckId.CLAIM_ABSENT),
        expected_after=not_applicable(NaReason.NO_EFFECT_CLAIMED),
        note="the same escape with read-only transport",
    ),
    Mutant(
        id="B03",
        family="B",
        surface="applicability",
        severity=Severity.HIGH,
        apply=deriving(
            "applicability",
            _na_instead_of(
                CheckId.EFFECT_TYPE_MISLABELLED, NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE
            ),
        ),
        witness=mislabelled_with_transport,
        expected_before=failed(CheckId.EFFECT_TYPE_MISLABELLED),
        expected_after=not_applicable(NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE),
        note="clinic transport hidden behind a claim that names an unobservable effect type",
    ),
    Mutant(
        id="B04",
        family="B",
        surface="applicability",
        severity=Severity.HIGH,
        apply=deriving("applicability", _first_claim_wins),
        witness=two_claim_spans,
        expected_before=failed(CheckId.CLAIM_AMBIGUOUS),
        expected_after=PASSED,
        note="two claims resolved by taking the first, which is the evaluator deciding the thing "
        "it was asked to observe",
    ),
    Mutant(
        id="B05",
        family="B",
        surface="applicability",
        severity=Severity.MEDIUM,
        apply=deriving("applicability", _widened_claim_detection),
        witness=orphan_observation_attributes,
        expected_before=PASSED,
        expected_after=failed(CheckId.CLAIM_AMBIGUOUS),
        note="claim detection widened to any careloop attribute. Characterises the "
        "orphan-attribute "
        "behaviour rather than proposing it: a span carrying observation attributes with no effect "
        "type is invisible today, and this pins that as a decision",
    ),
    Mutant(
        id="C01",
        family="C",
        surface="successful",
        severity=Severity.MEDIUM,
        apply=patching("successful", _successful_requiring_ok),
        witness=lambda: payload(settled_trace()),
        expected_before=PASSED,
        expected_after=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        note="requires status.code == OK. Production spans carry UNSET on success, so this refuses "
        "every real CareLoop trace while accepting every fixture written from an OpenTelemetry "
        "reference. A false FAIL rather than an escape, which is why it is MEDIUM -- but it is the "
        "single mutation Patch A's A4 finding exists to prevent",
    ),
    Mutant(
        id="C02",
        family="C",
        surface="successful",
        severity=Severity.CRITICAL,
        apply=patching("successful", _successful_ignoring_otel_error),
        witness=write_with(otel="ERROR"),
        expected_before=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        expected_after=PASSED,
        note="an OpenTelemetry ERROR status on a 2xx write is ignored",
    ),
    Mutant(
        id="C03",
        family="C",
        surface="successful",
        severity=Severity.CRITICAL,
        apply=patching("successful", _successful_accepting(300, 400)),
        witness=write_with(status=302, response_status=302),
        expected_before=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        expected_after=PASSED,
        note="a redirect counts as having created the appointment",
    ),
    Mutant(
        id="C04",
        family="C",
        surface="successful",
        severity=Severity.CRITICAL,
        apply=patching("successful", _successful_accepting(400, 500)),
        witness=read_with(status=404, response_status=404),
        expected_before=failed(CheckId.OBSERVATION_NOT_SUCCESSFUL),
        expected_after=PASSED,
        note="the clinic answering that it holds no such appointment counts as an observation "
        "that it does",
    ),
    Mutant(
        id="C05",
        family="C",
        surface="successful",
        severity=Severity.CRITICAL,
        apply=patching("successful", _successful_accepting(500, 600)),
        witness=write_with(status=500, response_status=500),
        expected_before=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        expected_after=PASSED,
        note="a server error counts as a successful write",
    ),
    Mutant(
        id="C06",
        family="C",
        surface="successful",
        severity=Severity.CRITICAL,
        apply=patching("successful", _successful_when_status_is_absent),
        witness=write_with(status=ABSENT, response_status=ABSENT),
        expected_before=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        expected_after=PASSED,
        note="a span recording no status at all is treated as a success",
    ),
    Mutant(
        id="C07",
        family="C",
        surface="http_status",
        severity=Severity.MEDIUM,
        apply=patching(
            "http_status", _status_preferring(ATTR_HTTP_STATUS_CODE, ATTR_HTTP_RESPONSE_STATUS_CODE)
        ),
        witness=write_with(status=201, response_status=500),
        expected_before=failed(CheckId.TRANSPORT_STATUS_AMBIGUOUS),
        expected_after=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        note="a disagreement between the two measured spellings resolved by preferring the old "
        "one. MEDIUM rather than CRITICAL, and the reason is a real redundancy found while "
        "writing this: status is resolved twice by different code. The ambiguity checks read "
        "settlement's own http_status, while successful() resolves it again through spans' -- so "
        "preferring a spelling in one place loses the diagnostic and is still refused by the "
        "other. Two independent resolvers, the same shape as the policy layer's two enforcement "
        "points",
    ),
    Mutant(
        id="C08",
        family="C",
        surface="http_status",
        severity=Severity.MEDIUM,
        apply=patching(
            "http_status", _status_preferring(ATTR_HTTP_RESPONSE_STATUS_CODE, ATTR_HTTP_STATUS_CODE)
        ),
        witness=write_with(status=500, response_status=201),
        expected_before=failed(CheckId.TRANSPORT_STATUS_AMBIGUOUS),
        expected_after=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        note="the mirror image of C07, and the same backstop catches it",
    ),
    Mutant(
        id="I01",
        family="I",
        surface="_member (Settlement)",
        severity=Severity.CRITICAL,
        apply=patching("_member", _member_settling(lambda value: value is None)),
        witness=settlement_token(ABSENT),
        expected_before=failed(CheckId.SETTLEMENT_ABSENT),
        expected_after=PASSED,
        note="a missing settlement conclusion defaults to VERIFIED",
    ),
    Mutant(
        id="I02",
        family="I",
        surface="_member (Settlement)",
        severity=Severity.CRITICAL,
        apply=patching(
            "_member", _member_settling(lambda value: isinstance(value, str) and value == "MAYBE")
        ),
        witness=settlement_token("MAYBE"),
        expected_before=failed(CheckId.SETTLEMENT_ABSENT),
        expected_after=PASSED,
        note="a token outside the closed vocabulary is read as VERIFIED",
    ),
    Mutant(
        id="I03",
        family="I",
        surface="_member (Settlement)",
        severity=Severity.CRITICAL,
        apply=patching(
            "_member",
            _member_settling(lambda value: isinstance(value, str) and value == "NOT_CERTIFIED"),
        ),
        witness=settlement_token("NOT_CERTIFIED"),
        expected_before=failed(CheckId.SETTLEMENT_NOT_VERIFIED),
        expected_after=PASSED,
        note="CareLoop's own conclusion that it could not certify the effect is overridden",
    ),
    Mutant(
        id="J01",
        family="J",
        surface="_dispatch_contradicts_transport",
        severity=Severity.MEDIUM,
        apply=replacing_check("_dispatch_contradicts_transport", _dispatch_ignoring("performed")),
        witness=dispatch_token("PERFORMED"),
        expected_before=failed(CheckId.DISPATCH_CONTRADICTS_TRANSPORT),
        expected_after=failed(CheckId.WRITE_NOT_SUCCESSFUL),
        note="PERFORMED with no write in the trace stops being a contradiction. MEDIUM because "
        "_write_not_successful backstops it -- which is the designed redundancy, demonstrated",
    ),
    Mutant(
        id="J02",
        family="J",
        surface="_dispatch_contradicts_transport",
        severity=Severity.CRITICAL,
        apply=replacing_check("_dispatch_contradicts_transport", _dispatch_ignoring("historical")),
        witness=suppressed_but_wrote,
        expected_before=failed(CheckId.DISPATCH_CONTRADICTS_TRANSPORT),
        expected_after=PASSED,
        note="a claim that the effect was suppressed, beside a write that actually happened",
    ),
    Mutant(
        id="J03",
        family="J",
        surface="_member (Dispatch)",
        severity=Severity.CRITICAL,
        apply=deriving(
            "_member",
            lambda original: (
                lambda vocabulary, value: (
                    Dispatch.NOT_IN_THIS_TRACE
                    if vocabulary is Dispatch and isinstance(value, str) and value
                    else original(vocabulary, value)
                )
            ),
        ),
        witness=dispatch_token("HISTORICAL"),
        expected_before=failed(CheckId.DISPATCH_CONTRADICTS_TRANSPORT),
        expected_after=PASSED,
        note="an unrecognised dispatch token is read as the historical mode, which is the mode "
        "with the weakest transport expectation",
    ),
    Mutant(
        id="J04",
        family="J",
        surface="_field_set_unexpected",
        severity=Severity.CRITICAL,
        apply=deriving_check(
            "_field_set_unexpected",
            lambda original: lambda e: None if _dispatch_of(e) != "PERFORMED" else original(e),
        ),
        witness=lambda: payload(recheck_trace(**{ATTR_EFFECT_FIELDS: "patient_id"})),
        expected_before=failed(CheckId.FIELD_SET_UNEXPECTED),
        expected_after=PASSED,
        note="a no-write dispatch mode treated as relaxing the field universe. Together with E05 "
        "this is the proof that SUPPRESSED and NOT_IN_THIS_TRACE remove the write and its "
        "ordering, and nothing else",
    ),
    Mutant(
        id="K01",
        family="K",
        surface="parse_envelope",
        severity=Severity.HIGH,
        apply=deriving("parse_envelope", _first_target_trace),
        witness=two_target_traces,
        expected_before=errored(ErrorCode.AMBIGUOUS_TARGET),
        expected_after=PASSED,
        note="an envelope naming several traces evaluated against the first. Envelope ambiguity "
        "is an ERROR rather than a FAIL, and resolving it by selection produces a verdict about a "
        "trace nobody asked about",
    ),
    Mutant(
        id="L01",
        family="L",
        surface="parse_envelope",
        severity=Severity.CRITICAL,
        apply=deriving("parse_envelope", _swallowing_parse),
        witness=wrong_level,
        expected_before=errored(ErrorCode.UNSUPPORTED_EVALUATION_LEVEL),
        expected_after=PASSED,
        note="every envelope refusal swallowed and the spans evaluated anyway, so unevaluable "
        "input settles",
    ),
    Mutant(
        id="L02",
        family="L",
        surface="assess",
        severity=Severity.HIGH,
        apply=deriving("assess", _na_on_envelope_error),
        witness=wrong_level,
        expected_before=errored(ErrorCode.UNSUPPORTED_EVALUATION_LEVEL),
        expected_after=not_applicable(NaReason.NO_EFFECT_CLAIMED),
        note="unevaluable input reported as nothing to judge, which removes it from the record "
        "rather than flagging it",
    ),
    Mutant(
        id="L03",
        family="L",
        surface="fail_result",
        severity=Severity.MEDIUM,
        apply=deriving("fail_result", _contradictions_as_errors),
        witness=settlement_token("NOT_CERTIFIED"),
        expected_before=failed(CheckId.SETTLEMENT_NOT_VERIFIED),
        expected_after=errored(ErrorCode.INTERNAL_ERROR),
        note="a genuine contradiction in valid evidence reported as infrastructure trouble, which "
        "is how a finding stops being read as a finding",
    ),
    Mutant(
        id="L04",
        family="L",
        surface="parse_envelope",
        severity=Severity.CRITICAL,
        apply=deriving("parse_envelope", _dropping_unattributable_spans),
        witness=second_write_without_a_trace_id,
        expected_before=errored(ErrorCode.SPAN_MISSING_TRACE_ID),
        expected_after=PASSED,
        note="an unattributable span dropped rather than refused. The dropped span is a second "
        "clinic write, so discarding it turns an ambiguous trace into a settled one",
    ),
    Mutant(
        id="M01",
        family="M",
        surface="assess",
        severity=Severity.MEDIUM,
        apply=deriving("assess", lambda _shipped: _explanation_gaining(_claimed_resource_of)),
        witness=hostile_mismatch,
        expected_before=failed(CheckId.RESOURCE_MISMATCH),
        expected_after=leaking(failed(CheckId.RESOURCE_MISMATCH), HOSTILE_RESOURCE),
        note="the claimed resource id added to the explanation as context. The outcome is "
        "unchanged, which is exactly why the standing contract has to check the published text "
        "and not only the verdict",
    ),
    Mutant(
        id="M02",
        family="M",
        surface="assess",
        severity=Severity.MEDIUM,
        apply=deriving("assess", lambda _shipped: _explanation_gaining(_first_url_of)),
        witness=hostile_mismatch,
        expected_before=failed(CheckId.RESOURCE_MISMATCH),
        expected_after=leaking(failed(CheckId.RESOURCE_MISMATCH), CLINIC),
        note="an external URL added to the explanation",
    ),
    Mutant(
        id="M03",
        family="M",
        surface="not_applicable_result",
        severity=Severity.MEDIUM,
        apply=deriving(
            "not_applicable_result",
            lambda shipped: lambda reason: {**shipped(reason), "value": 0.0},
        ),
        witness=lambda: payload(simulated_trace()),
        expected_before=not_applicable(NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE),
        expected_after=with_keys(
            not_applicable(NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE), "value"
        ),
        note="NOT_APPLICABLE gains a numeric value, so an inapplicable trace starts contributing "
        "a zero to every average. Patch A proved the omission survives the round trip uncoerced; "
        "this is the mutation that would waste that",
    ),
    Mutant(
        id="M04",
        family="M",
        surface="error_result",
        severity=Severity.MEDIUM,
        apply=deriving(
            "error_result", lambda shipped: lambda code: {**shipped(code), "label": "ERROR"}
        ),
        witness=wrong_level,
        expected_before=errored(ErrorCode.UNSUPPORTED_EVALUATION_LEVEL),
        expected_after=with_keys(errored(ErrorCode.UNSUPPORTED_EVALUATION_LEVEL), "label"),
        note="an error result gains a label value Patch A never proved the service accepts",
    ),
)


MUTANTS: Final[tuple[Mutant, ...]] = _family_a() + _FAMILY_DEFG + _FAMILY_REST


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


class TestEquivalentMutantsAreProvablyUnobservable:
    """An equivalent mutant must move **nothing**, over more than its own witness.

    Equivalence is the one classification that retires a mutant without a kill, so it is the one
    that has to be hardest to claim. Each entry carries a written justification, and each is swept
    over its own extra probes as well as the whole standing corpus -- chosen to exercise the paths
    where a directional difference would show up if there were one.
    """

    EQUIVALENT: ClassVar = tuple(mutant for mutant in MUTANTS if mutant.is_equivalent)

    def test_there_is_something_to_check(self) -> None:
        assert self.EQUIVALENT

    @pytest.mark.parametrize(
        "mutant", EQUIVALENT, ids=[mutant.id for mutant in EQUIVALENT] or ["none"]
    )
    def test_no_extra_probe_moves_either(
        self, mutant: Mutant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert mutant.extra_probes, f"{mutant.id} claims equivalence with no extra probes"
        before = [verdict(probe()) for probe in mutant.extra_probes]
        mutant.apply(monkeypatch)
        after = [verdict(probe()) for probe in mutant.extra_probes]
        assert after == before, f"{mutant.id} is declared EQUIVALENT but moved a probe"

    @pytest.mark.parametrize(
        "mutant", EQUIVALENT, ids=[mutant.id for mutant in EQUIVALENT] or ["none"]
    )
    def test_the_probes_span_more_than_one_outcome(self, mutant: Mutant) -> None:
        """A sweep over probes that all answer the same thing would prove nothing."""
        reached = {verdict(probe()) for probe in mutant.extra_probes}
        assert len(reached) > 1, f"{mutant.id}'s probes do not discriminate"


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


# =============================================================================================
# Truth tables -- properties stated directly, beside the mutants that attack them
# =============================================================================================


def _matrix_trace(writes: int, reads: int, claims: str) -> Mapping[str, Any]:
    """A trace with a chosen amount of clinic transport and a chosen kind of claim."""
    spans: list[Any] = [internal_operation_span()]
    if claims == "observable":
        spans.append(graph_span())
    elif claims == "unobservable":
        spans.append(graph_span(**{ATTR_EFFECT_TYPE: "send_email"}))
    elif claims == "multiple":
        spans.append(graph_span())
        spans.append(graph_span(**{ATTR_EFFECT_TYPE: "book_appointment"}))
        spans[-1]["spanId"] = "dddddddddddddddd"
        spans[-1]["span_id"] = "dddddddddddddddd"
    spans.extend(bedrock_spans())
    for index in range(writes):
        spans.append(write_span(span_id=f"eeeeeeeeeeeeeee{index}"))
    for index in range(reads):
        spans.append(
            read_span(
                span_id=f"fffffffffffffff{index}",
                start=READ_START + index,
                end=READ_END + index,
            )
        )
    return payload(spans)


class TestApplicabilityNeverHidesClinicActivity:
    """The Phase-A invariant, over the whole matrix rather than at a few points.

    ``NOT_APPLICABLE`` is a statement that nothing external happened. The one thing it must never
    be is a way for a trace that booked or read an appointment to leave no number behind, whether
    because the claim is missing, because it names the wrong effect type, or because there are two
    of them.
    """

    MATRIX: ClassVar = [
        (writes, reads, claims)
        for writes in (0, 1, 2)
        for reads in (0, 1, 2)
        for claims in ("none", "observable", "unobservable", "multiple")
    ]

    @pytest.mark.parametrize(
        ("writes", "reads", "claims"),
        MATRIX,
        ids=[f"w{w}r{r}-{c}" for w, r, c in MATRIX],
    )
    def test_not_applicable_implies_no_clinic_transport(
        self, writes: int, reads: int, claims: str
    ) -> None:
        result = verdict(_matrix_trace(writes, reads, claims))
        if result.label == "NOT_APPLICABLE":
            assert writes == 0 and reads == 0, (
                f"a trace with {writes} clinic write(s) and {reads} clinic read(s) reported "
                f"NOT_APPLICABLE, so external activity left no number behind"
            )

    @pytest.mark.parametrize(
        ("writes", "reads", "claims"),
        MATRIX,
        ids=[f"w{w}r{r}-{c}" for w, r, c in MATRIX],
    )
    def test_the_matrix_never_settles_anything_it_should_not(
        self, writes: int, reads: int, claims: str
    ) -> None:
        """Only one cell in the matrix is a complete, unambiguous, settled trace."""
        result = verdict(_matrix_trace(writes, reads, claims))
        if result == PASSED:
            assert (writes, reads, claims) == (1, 1, "observable")

    def test_the_matrix_reaches_every_phase_a_answer(self) -> None:
        """A matrix that only ever produced one answer would prove nothing."""
        reached = {verdict(_matrix_trace(w, r, c)) for w, r, c in self.MATRIX}
        codes = {outcome.code for outcome in reached}
        assert {
            NaReason.NO_EFFECT_CLAIMED.value,
            NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE.value,
            CheckId.CLAIM_ABSENT.value,
            CheckId.EFFECT_TYPE_MISLABELLED.value,
            CheckId.CLAIM_AMBIGUOUS.value,
        } <= codes


class TestTheOutcomeAndDigestTruthTable:
    """Every combination of reported outcome against digest agreement, pinned to its exact code.

    Not "some FAIL": the three defences here overlap, and which one refuses a trace is the whole
    audit value. ``OUTCOME_CONTRADICTS_DIGESTS`` catches a claim disagreeing with itself,
    ``STATE_DIGESTS_DIFFER`` catches a plain disagreement under an outcome that claims none, and
    ``OUTCOME_NOT_MATCHED`` catches an outcome that never claimed agreement in the first place.
    Each is reachable, which is what stops any of them being decorative.
    """

    TABLE: ClassVar = [
        ("MATCHED", True, PASSED),
        ("MATCHED", False, failed(CheckId.OUTCOME_CONTRADICTS_DIGESTS)),
        ("DIVERGED", True, failed(CheckId.OUTCOME_CONTRADICTS_DIGESTS)),
        ("DIVERGED", False, failed(CheckId.STATE_DIGESTS_DIFFER)),
        ("ABSENT", True, failed(CheckId.OUTCOME_NOT_MATCHED)),
        ("ABSENT", False, failed(CheckId.STATE_DIGESTS_DIFFER)),
        ("UNAVAILABLE", True, failed(CheckId.OUTCOME_NOT_MATCHED)),
        ("UNAVAILABLE", False, failed(CheckId.STATE_DIGESTS_DIFFER)),
        ("NOT_OBSERVED", True, failed(CheckId.OUTCOME_NOT_MATCHED)),
        ("NOT_OBSERVED", False, failed(CheckId.STATE_DIGESTS_DIFFER)),
    ]

    @pytest.mark.parametrize(
        ("outcome", "agree", "expected"),
        TABLE,
        ids=[f"{outcome}-{'equal' if agree else 'unequal'}" for outcome, agree, _ in TABLE],
    )
    def test_each_combination_reaches_its_own_verdict(
        self, outcome: str, agree: bool, expected: Outcome
    ) -> None:
        observed = STATE_DIGEST if agree else OTHER_DIGEST
        built = payload(
            settled_trace(
                **{
                    ATTR_OBSERVATION_OUTCOME: outcome,
                    ATTR_OBSERVATION_STATE_DIGEST: observed,
                }
            )
        )
        assert verdict(built) == expected

    def test_only_matched_with_equal_digests_settles(self) -> None:
        settling = [row for row in self.TABLE if row[2] == PASSED]
        assert settling == [("MATCHED", True, PASSED)]

    def test_all_three_defences_are_independently_reachable(self) -> None:
        reached = {row[2].code for row in self.TABLE}
        assert CheckId.OUTCOME_CONTRADICTS_DIGESTS.value in reached
        assert CheckId.STATE_DIGESTS_DIFFER.value in reached
        assert CheckId.OUTCOME_NOT_MATCHED.value in reached


class TestAmbiguityIsRefusedAndNeverResolved:
    """Trace-content ambiguity is a finding; envelope-target ambiguity is an error.

    The split follows what the ambiguity is *about*. Two claims or two writes are facts about a
    trace this evaluator was asked to judge, so they are verdicts. Two target traces means the
    request itself does not name one thing to judge, so it is unevaluable input.
    """

    CONTENT: ClassVar = [
        ("two claims", two_claim_spans, CheckId.CLAIM_AMBIGUOUS),
        ("two writes", two_writes, CheckId.MULTIPLE_WRITES),
        ("two matching reads", two_matching_reads, CheckId.MULTIPLE_OBSERVATIONS),
        ("write status disagrees", ambiguous_write_status, CheckId.TRANSPORT_STATUS_AMBIGUOUS),
        ("read status disagrees", ambiguous_read_status, CheckId.TRANSPORT_STATUS_AMBIGUOUS),
    ]

    @pytest.mark.parametrize(("name", "witness", "code"), CONTENT, ids=[row[0] for row in CONTENT])
    def test_content_ambiguity_is_a_fail(self, name: str, witness: Witness, code: CheckId) -> None:
        assert verdict(witness()) == failed(code)

    def test_envelope_ambiguity_is_an_error(self) -> None:
        assert verdict(two_target_traces()) == errored(ErrorCode.AMBIGUOUS_TARGET)

    def test_no_ambiguous_trace_ever_settles(self) -> None:
        for _, witness, _ in self.CONTENT:
            assert verdict(witness()) != PASSED


class TestMalformedInputNeverSettlesAndNeverDisappears:
    """Every unevaluable envelope, and the two answers it must never give."""

    MALFORMED: ClassVar = [
        ("not a mapping", lambda: None, ErrorCode.ENVELOPE_MALFORMED),
        (
            "bad schema version",
            lambda: payload(settled_trace(), version="9.9"),
            ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        ),
        ("bad level", wrong_level, ErrorCode.UNSUPPORTED_EVALUATION_LEVEL),
        (
            "span-id target",
            lambda: payload(settled_trace(), target={"spanIds": ["x"]}),
            ErrorCode.UNSUPPORTED_TARGET_KIND,
        ),
        ("two target traces", two_target_traces, ErrorCode.AMBIGUOUS_TARGET),
        (
            "no target traces",
            lambda: payload(settled_trace(), trace_ids=[]),
            ErrorCode.AMBIGUOUS_TARGET,
        ),
        (
            "no evaluation input",
            lambda: {
                "evaluationLevel": "TRACE",
                "evaluationTarget": {"traceIds": [TRACE_ID]},
                "schemaVersion": "1.0",
            },
            ErrorCode.SPANS_MALFORMED,
        ),
        ("no spans", lambda: payload([]), ErrorCode.SPANS_MALFORMED),
        ("a span that is not a mapping", lambda: payload(["nope"]), ErrorCode.SPANS_MALFORMED),
        (
            "a span with no trace id",
            second_write_without_a_trace_id,
            ErrorCode.SPAN_MISSING_TRACE_ID,
        ),
        (
            "target trace absent",
            lambda: payload(settled_trace(), trace_ids=[OTHER_TRACE_ID]),
            ErrorCode.TARGET_TRACE_ABSENT,
        ),
    ]

    @pytest.mark.parametrize(
        ("name", "witness", "code"), MALFORMED, ids=[row[0] for row in MALFORMED]
    )
    def test_it_is_reported_as_the_error_it_is(
        self, name: str, witness: Witness, code: ErrorCode
    ) -> None:
        assert verdict(witness()) == errored(code)

    @pytest.mark.parametrize(
        ("name", "witness", "code"), MALFORMED, ids=[row[0] for row in MALFORMED]
    )
    def test_it_never_becomes_pass_or_not_applicable(
        self, name: str, witness: Witness, code: ErrorCode
    ) -> None:
        assert verdict(witness()).label == "ERROR"

    def test_a_contradiction_in_valid_evidence_stays_a_finding(self) -> None:
        """The reverse boundary: an evidence contradiction is never infrastructure trouble."""
        for witness in (
            settlement_token("NOT_CERTIFIED"),
            lambda: payload(settled_trace(**{ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST})),
            failed_read,
            read_before_write,
        ):
            assert verdict(witness()).label == "FAIL"


class TestThePublishedSurfaceCarriesNothingFromThePayload:
    """The result is a CloudWatch record and, at submission, a public one."""

    HOSTILE: ClassVar = [
        ("settled", hostile_trace),
        ("refused", hostile_mismatch),
        ("ordinary settled", lambda: payload(settled_trace())),
        ("not applicable", lambda: payload(simulated_trace())),
        ("unevaluable", wrong_level),
    ]

    @pytest.mark.parametrize("entry", HOSTILE, ids=[name for name, _ in HOSTILE])
    def test_no_sentinel_reaches_the_result(self, entry: tuple[str, Witness]) -> None:
        name, witness = entry
        assert verdict(witness()).leaks == (), name

    def test_the_hostile_trace_is_actually_hostile(self) -> None:
        """A sentinel that is not in the payload cannot prove anything about the result."""
        rendered = json.dumps(hostile_trace(), default=str)
        assert HOSTILE_RESOURCE in rendered
        assert HOSTILE_DIGEST in rendered
        assert HOSTILE_HOST in rendered

    def test_the_hostile_trace_still_settles(self) -> None:
        """Hostility is in the values, not in the evidence: this trace is valid and must pass."""
        assert verdict(hostile_trace()) == PASSED


class TestSettlementAbsenceIsNamedRatherThanMerelyRefused:
    """The diagnostic gap Patch B left, closed.

    Deleting ``_settlement_absent`` turned **zero** tests red before Patch C: absence still
    reached ``FAIL``, so no assertion noticed that the trail had stopped saying *why*. A missing
    settlement conclusion and a settlement conclusion of ``NOT_CERTIFIED`` are different facts
    about what CareLoop knows, and an append-only audit trail is the wrong place to collapse them.
    """

    def test_a_missing_settlement_is_reported_as_absent(self) -> None:
        assert verdict(settlement_token(ABSENT)()) == failed(CheckId.SETTLEMENT_ABSENT)

    def test_an_unknown_token_is_also_absent_rather_than_unverified(self) -> None:
        assert verdict(settlement_token("MAYBE")()) == failed(CheckId.SETTLEMENT_ABSENT)

    def test_a_recorded_refusal_is_reported_as_not_verified(self) -> None:
        assert verdict(settlement_token("NOT_CERTIFIED")()) == failed(
            CheckId.SETTLEMENT_NOT_VERIFIED
        )

    def test_only_verified_settles(self) -> None:
        for token in (ABSENT, "MAYBE", "NOT_CERTIFIED", "verified", "", 1, None):
            assert verdict(settlement_token(token)()) != PASSED, token


class TestNoWriteModesRemoveTheWriteAndNothingElse:
    """``SUPPRESSED`` and ``NOT_IN_THIS_TRACE`` drop the write and its ordering. That is all."""

    MODES: ClassVar = ["SUPPRESSED", "NOT_IN_THIS_TRACE"]

    @pytest.mark.parametrize("mode", MODES)
    def test_they_settle_with_no_write_at_all(self, mode: str) -> None:
        assert verdict(payload(recheck_trace(**{ATTR_EFFECT_DISPATCH: mode}))) == PASSED

    @pytest.mark.parametrize("mode", MODES)
    def test_but_every_other_requirement_still_applies(self, mode: str) -> None:
        for override, expected in (
            ({ATTR_OBSERVATION_MODE: "NONE"}, CheckId.NO_INDEPENDENT_OBSERVER),
            ({ATTR_OBSERVATION_RESOURCE_ID: "apt-other"}, CheckId.RESOURCE_MISMATCH),
            ({ATTR_EFFECT_FIELDS: "patient_id"}, CheckId.FIELD_SET_UNEXPECTED),
            ({ATTR_EFFECT_STATE_DIGEST: "nothex"}, CheckId.DIGEST_MALFORMED),
            ({ATTR_OBSERVATION_STATE_DIGEST: OTHER_DIGEST}, CheckId.OUTCOME_CONTRADICTS_DIGESTS),
            ({ATTR_OBSERVATION_OUTCOME: "ABSENT"}, CheckId.OUTCOME_NOT_MATCHED),
            ({ATTR_SETTLEMENT: "NOT_CERTIFIED"}, CheckId.SETTLEMENT_NOT_VERIFIED),
        ):
            built = payload(recheck_trace(**{ATTR_EFFECT_DISPATCH: mode, **override}))
            assert verdict(built) == failed(expected), (mode, override)

    @pytest.mark.parametrize("mode", MODES)
    def test_the_independent_read_is_never_historical(self, mode: str) -> None:
        """The write may belong to an earlier trace. The observation may not."""
        spans = [
            internal_operation_span(),
            graph_span(**{ATTR_EFFECT_DISPATCH: mode}),
            *bedrock_spans(),
        ]
        assert verdict(payload(spans)) == failed(CheckId.OBSERVATION_ABSENT_FROM_TRACE)

    @pytest.mark.parametrize("mode", MODES)
    def test_a_write_in_this_trace_contradicts_them(self, mode: str) -> None:
        spans = [*recheck_trace(**{ATTR_EFFECT_DISPATCH: mode}), write_span()]
        assert verdict(payload(spans)) == failed(CheckId.DISPATCH_CONTRADICTS_TRANSPORT)


class TestTheOrderingBoundaryIsExact:
    """``read.start >= write.end``, pinned from both sides at one nanosecond."""

    def test_a_read_beginning_exactly_when_the_write_finished_is_valid(self) -> None:
        assert verdict(read_exactly_at_write_end()) == PASSED

    def test_one_nanosecond_earlier_is_refused(self) -> None:
        assert verdict(read_before_write()) == failed(CheckId.OBSERVATION_PRECEDES_EFFECT)

    def test_unreadable_timing_is_a_failure_and_not_a_skipped_check(self) -> None:
        assert verdict(unreadable_timing()) == failed(CheckId.TIMING_UNAVAILABLE)


class TestCharacterisedButUnchangedBehaviour:
    """Contract behaviour Patch C records rather than proposes.

    Neither of these is a defect and neither is being repaired here. Writing them down is what
    turns a later change into a decision instead of a drift.
    """

    def test_an_orphan_observation_span_is_not_a_second_claim(self) -> None:
        """A claim is located by ``careloop.effect.type`` alone.

        A span carrying observation attributes without one is invisible, so contradictory values
        on it change nothing. This cannot manufacture a PASS -- the located claim still has to be
        complete and correct -- and mutant B05 is what would notice if the rule ever widened.
        """
        assert verdict(orphan_observation_attributes()) == PASSED

    def test_an_unhashable_effect_type_is_a_finding_and_not_a_crash(self) -> None:
        """``in`` against a frozenset raises on an unhashable value; the isinstance guard stops it.

        Without the guard these would leave ``assess`` as a ``TypeError`` and reach the handler's
        broad catch as ``INTERNAL_ERROR`` -- infrastructure trouble reported for a trace that is
        simply mislabelled.
        """
        for hostile in ({"a": 1}, ["book_appointment"], 7, 1.5, True):
            spans = settled_trace(**{ATTR_EFFECT_TYPE: hostile})
            assert verdict(payload(spans)) == failed(CheckId.EFFECT_TYPE_MISLABELLED), hostile

    def test_an_absent_effect_type_makes_the_span_no_claim_at_all(self) -> None:
        """A claim is located by the attribute's presence, so ``None`` is not a mislabelled claim.

        It is no claim, and the trace's clinic transport is then unaccounted for -- which is the
        stronger of the two findings and the correct one.
        """
        spans = settled_trace(**{ATTR_EFFECT_TYPE: None})
        assert verdict(payload(spans)) == failed(CheckId.CLAIM_ABSENT)
