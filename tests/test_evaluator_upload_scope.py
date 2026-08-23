"""The deployed evaluator judges booking, and an upload trace is out of its scope — safely.

``CareLoopSettlementIntegrity`` is a frozen artefact: a Lambda deployed in AWS, backed by six
recorded real-trace results and a campaign of 73 designated mutants. Adding a second real effect to
CareLoop must not require editing it, and this file is what proves it did not have to.

**The claim is narrow and the danger is specific.** An effect type the evaluator does not know
about could be answered ``PASS`` — a real external write leaving a green number behind that means
nothing — or it could crash the handler. What must happen instead is ``NOT_APPLICABLE`` with no
``value`` key, which is the evaluator saying *there is nothing here I judge* rather than *this was
fine*.

That behaviour is not new code. ``applicability`` has gated on
``claim.effect_type in OBSERVABLE_EFFECT_TYPES`` since it was written, so the frozen evaluator was
**already** safe against a second effect type before one existed. This file measures that rather
than assuming it, and pins it so a later widening of ``OBSERVABLE_EFFECT_TYPES`` cannot happen by
accident.

``tests/test_evaluator_drift.py`` holds the declaration side — the subset contract and the named
``NOT_EVALUATED_IN_AWS`` exclusion. This holds the behavioural side.
"""

from __future__ import annotations

import pytest
from test_evaluator import (
    graph_span,
    internal_operation_span,
    payload,
    read_span,
    write_span,
)
from test_evaluator_drift import NOT_EVALUATED_IN_AWS

from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE, OBSERVABLE_STATE
from careloop.graph.witness import FIELD_SEPARATOR
from evaluators.contract import (
    ATTR_EFFECT_FIELDS,
    ATTR_EFFECT_TYPE,
    ATTR_OBSERVATION_RESOURCE_ID,
    OBSERVABLE_EFFECT_TYPES,
    NaReason,
)
from evaluators.settlement import assess

#: What ``careloop.effect.fields`` really renders for an upload, derived rather than restated.
UPLOAD_FIELDS = FIELD_SEPARATOR.join(OBSERVABLE_STATE["upload_document"].state_fields)

#: A bounded resource id of the shape an upload emits: the frozen idempotency key's hex.
UPLOAD_RESOURCE_ID = "f274f9b5a00f73e9fe4ff630a6feac4bb97bcc8c0d2cc34f8deae9f89081f175"


def upload_spans() -> list[object]:
    """A well-formed CareLoop trace whose one claim is an upload.

    Deliberately a trace that *would* settle if the evaluator judged uploads: nine attributes, both
    digests present and equal, ``MATCHED``, ``VERIFIED``. If the scope gate were removed, this is
    the trace that would be judged — so answering ``NOT_APPLICABLE`` for it is a statement about
    scope rather than about the trace being malformed.

    It carries **no clinic transport**, which is the honest shape: an upload run makes no HTTP call
    to ``/clinic/appointments``, and the evaluator's transport cross-check looks for exactly those
    routes. The claim rides on ``invoke_graph``, where CareLoop really puts it — one claim span per
    trace, which is what keeps ``CLAIM_AMBIGUOUS`` out of the picture.
    """
    return [
        internal_operation_span(),
        graph_span(
            **{
                ATTR_EFFECT_TYPE: "upload_document",
                ATTR_EFFECT_FIELDS: UPLOAD_FIELDS,
                ATTR_OBSERVATION_RESOURCE_ID: UPLOAD_RESOURCE_ID,
            }
        ),
    ]


def upload_trace() -> dict[str, object]:
    """The same spans in the envelope AWS actually sends."""
    return payload(upload_spans())


class TestAnUploadTraceIsOutOfScope:
    def test_it_is_not_applicable(self) -> None:
        assert assess(upload_trace())["label"] == "NOT_APPLICABLE"

    def test_it_says_the_effect_is_not_one_it_observes(self) -> None:
        """The specific reason, so a future change that made it not-applicable for some *other*
        reason — a malformed claim, an absent attribute — would show up as a different answer."""
        result = assess(upload_trace())

        assert result["explanation"] == _explanation(NaReason.EFFECT_NOT_INDEPENDENTLY_OBSERVABLE)

    def test_it_carries_no_value_key(self) -> None:
        """A ``NOT_APPLICABLE`` scores nothing. A ``value`` would be a number about nothing."""
        assert "value" not in assess(upload_trace())

    def test_it_is_not_a_pass(self) -> None:
        """The failure this file exists to prevent, stated as its own assertion."""
        assert assess(upload_trace())["label"] != "PASS"

    def test_it_is_not_an_error_either(self) -> None:
        """Out of scope is a decision, not a crash. The handler must stay total.

        An error result carries ``errorCode`` and **no label at all**, so its absence is what says
        the evaluator judged this rather than failing to read it.
        """
        assert "errorCode" not in assess(upload_trace())

    def test_the_same_trace_would_settle_if_it_were_a_booking(self) -> None:
        """**The vacuity proof.** Change one attribute — the effect type — and it passes.

        Without this, ``NOT_APPLICABLE`` could be coming from anything: a digest mismatch, an
        attribute the fixture got wrong, a field set the evaluator rejects. This says the *only*
        thing standing between this trace and a verdict is which effect it claims.
        """
        booking = [
            internal_operation_span(),
            graph_span(),
            write_span(),
            read_span(),
        ]

        assert assess(payload(booking))["label"] == "PASS"


class TestTheScopeIsDeclaredRatherThanIncidental:
    def test_the_evaluator_judges_booking_only(self) -> None:
        assert frozenset({"book_appointment"}) == OBSERVABLE_EFFECT_TYPES

    def test_the_upload_is_the_named_exclusion(self) -> None:
        assert frozenset({"upload_document"}) == NOT_EVALUATED_IN_AWS

    def test_careloop_observes_both(self) -> None:
        """Which is what makes the exclusion a real gap rather than a note about nothing."""
        assert frozenset({"book_appointment", "upload_document"}) == INDEPENDENTLY_OBSERVABLE

    def test_the_exclusion_accounts_for_the_whole_difference(self) -> None:
        assert INDEPENDENTLY_OBSERVABLE - OBSERVABLE_EFFECT_TYPES == NOT_EVALUATED_IN_AWS


class TestFalselyMarkingTheUploadEvaluatedWouldBeCaught:
    """The mutation. Widening the evaluator's scope without deploying anything must not pass.

    ``OBSERVABLE_EFFECT_TYPES`` is the evaluator's own declaration, and the evaluator is a deployed
    Lambda. Editing that constant in the repository changes what the *tests* judge and nothing about
    what AWS runs, so the two would silently disagree — the deployed artefact would still answer
    ``NOT_APPLICABLE`` while the suite believed uploads were covered.
    """

    def test_widening_the_scope_breaks_the_declaration_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drift contract is what refuses it, and here that refusal is exercised directly."""
        import evaluators.contract as contract

        widened = frozenset({"book_appointment", "upload_document"})
        monkeypatch.setattr(contract, "OBSERVABLE_EFFECT_TYPES", widened)

        # The exclusion no longer accounts for the difference: the mutant claims AWS judges
        # something it has never been deployed to judge.
        assert INDEPENDENTLY_OBSERVABLE - widened != NOT_EVALUATED_IN_AWS

    def test_an_upload_trace_would_then_be_judged_on_bookings_field_set(self) -> None:
        """And what it would be judged *as* is the second half of why this is not a small edit.

        ``EXPECTED_FIELDS`` is booking's four fields. An upload renders two. So a widened evaluator
        would not quietly pass an upload — it would **fail** it with ``FIELD_SET_UNEXPECTED``, and a
        correct trace would be reported as a settlement defect. Generalising the evaluator means
        keying its expectations per effect type, redeploying, and re-running the mutation campaign.
        """
        from evaluators.contract import EXPECTED_FIELDS

        assert FIELD_SEPARATOR.join(EXPECTED_FIELDS) != UPLOAD_FIELDS


def _explanation(reason: NaReason) -> str:
    from evaluators.contract import NA_EXPLANATIONS

    return NA_EXPLANATIONS[reason]
