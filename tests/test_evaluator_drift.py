"""The evaluator owns its expectations. This is what stops them drifting from CareLoop's.

``evaluators/`` imports nothing from ``careloop`` and nothing from ``demoworld``, and that
independence is the whole reason its verdicts are worth anything: an evaluator that read CareLoop's
constants would be checking CareLoop against itself. The cost of independence is that two copies of
the same truth can drift apart silently, and the copy in ``evaluators/`` would go on being confident
while judging the wrong thing.

**Tests may import the application. The evaluator may not.** That asymmetry is the arrangement --
the same one ``demoworld/`` already uses -- and ``tests/test_evaluator.py`` keeps proving the
runtime half with its import sweep.

**Every anchor here is derived, not transcribed.** Reading a value out of the application at test
time is what makes this a drift check; re-typing the value into an assertion would produce a third
copy to drift.

Three of the four relations are equalities. The fourth is deliberately weaker, and §*Demo World's
routes* says why.
"""

from __future__ import annotations

import inspect

import pytest

from careloop.graph.verify import INDEPENDENTLY_OBSERVABLE, OBSERVABLE_STATE, _verify_booking
from careloop.graph.verify import ObservationOutcome as CareLoopOutcome
from careloop.graph.witness import FIELD_SEPARATOR as CARELOOP_FIELD_SEPARATOR
from careloop.graph.witness import EffectDispatch as CareLoopDispatch
from careloop.graph.witness import ObservationMode as CareLoopMode
from careloop.graph.witness import Settlement as CareLoopSettlement
from careloop.tools.registry import REGISTRY
from careloop.tools.results import AppointmentBooked
from demoworld import CLINIC_PREFIX, create_clinic_app
from evaluators.contract import (
    CLINIC_APPOINTMENTS_PATH,
    CLINIC_READ_METHOD,
    CLINIC_WRITE_METHOD,
    EXPECTED_FIELDS,
    FIELD_SEPARATOR,
    OBSERVABLE_EFFECT_TYPES,
    Dispatch,
    ObservationMode,
    ObservationOutcome,
    Settlement,
)


class TestWhatCanBeIndependentlyObserved:
    """``OBSERVABLE_EFFECT_TYPES`` against ``INDEPENDENTLY_OBSERVABLE``.

    An **equality**, not a subset relation in either direction. An effect type the evaluator
    thinks is observable but CareLoop does not would make the evaluator demand a read-back that
    can never happen; one CareLoop can observe but the evaluator does not know about would be
    silently answered ``NOT_APPLICABLE`` -- a real external booking leaving no number behind,
    which is the HIGH escape class the mutation ledger exists to prevent.
    """

    def test_the_two_declarations_agree(self) -> None:
        assert OBSERVABLE_EFFECT_TYPES == INDEPENDENTLY_OBSERVABLE

    def test_the_declaration_is_not_empty(self) -> None:
        """An empty set on either side would make the equality above vacuously true."""
        assert OBSERVABLE_EFFECT_TYPES

    def test_every_observable_type_is_an_effect_tool_the_registry_declares(self) -> None:
        assert REGISTRY.external_effect_tools >= OBSERVABLE_EFFECT_TYPES


def _fields_verification_compares() -> frozenset[str]:
    """The booking fields CareLoop's read-back actually compares, derived by running it.

    Derived behaviourally rather than by reading the source: ``_verify_booking`` is handed a
    result whose every field is populated and an empty argument map, so every comparison
    mismatches and the returned diagnostics name exactly the fields it looked at. A source parse
    would go stale the first time the map moved; this cannot.
    """
    populated = AppointmentBooked(
        provider_id="p",
        patient_id="q",
        slot_start="s",
        reason="r",
        confirmation_id="c",
    )
    return frozenset(
        problem.split(" ")[0]
        for problem in _verify_booking({}, populated)
        if " was authorised as " in problem
    )


class TestTheExternallyHeldBookingFields:
    """``EXPECTED_FIELDS`` against what CareLoop's read-back compares. The primary anchor.

    **``_verify_booking`` and not the registry**, and the distinction is the point of this class.
    The registry says *what CareLoop may send*; ``_verify_booking`` says *what an external system
    holds and CareLoop compares on the way out*. ``EXPECTED_FIELDS`` is the field universe the two
    joint state digests are taken over, which is the second of those.

    They agree today. The secondary assertion below records that agreement rather than relying on
    it, so that the day an argument is sent but not externally compared -- or compared but not
    sent -- somebody has to decide which meaning ``EXPECTED_FIELDS`` follows instead of the answer
    being chosen by whichever constant happened to be imported.
    """

    def test_the_evaluator_expects_exactly_what_verification_compares(self) -> None:
        assert frozenset(EXPECTED_FIELDS) == _fields_verification_compares()

    def test_the_derivation_is_not_empty(self) -> None:
        assert _fields_verification_compares()

    def test_the_expected_fields_are_in_canonical_sorted_order(self) -> None:
        """Patch D emits the canonical form, and mutant F03 is what defends it."""
        assert tuple(sorted(EXPECTED_FIELDS)) == EXPECTED_FIELDS
        assert len(set(EXPECTED_FIELDS)) == len(EXPECTED_FIELDS)

    def test_the_registry_currently_agrees_and_the_two_questions_stay_distinct(self) -> None:
        """A secondary consistency assertion, deliberately not the binding one.

        If this ever fails while the primary assertion above passes, the correct response is to
        decide what ``EXPECTED_FIELDS`` means -- not to change whichever side is easier.
        """
        booking = REGISTRY.get("book_appointment")
        assert booking is not None
        assert frozenset(EXPECTED_FIELDS) == booking.arg_names, (
            "the registry's booking arguments and the fields verification compares have "
            "diverged. EXPECTED_FIELDS follows what an external system holds and CareLoop "
            "compares, which is _verify_booking. Decide deliberately rather than picking one."
        )


def _clinic_routes() -> set[tuple[str, str]]:
    """Every ``(method, path)`` Demo World serves under the clinic prefix, from the live app."""
    app = create_clinic_app()
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if path.startswith(CLINIC_PREFIX) and methods:
            found.update((method, path) for method in methods)
    return found


class TestDemoWorldsRoutes:
    """The evaluator's route constants against the application that serves them.

    **Containment, not equality, and that asymmetry is deliberate.** The evaluator must recognise
    the two routes that carry settlement evidence; it must *not* track every route Demo World
    happens to serve. ``/clinic/slots`` is a read that settles nothing, and a future route would
    be the same -- binding the evaluator to the full route set would make an unrelated clinic
    feature a reason to change what settlement means.

    The evaluator still imports nothing from ``demoworld``. This test does the importing, which is
    exactly the arrangement the module docstring describes.
    """

    def test_the_write_route_is_served_as_the_evaluator_expects(self) -> None:
        expected = "/" + "/".join(CLINIC_APPOINTMENTS_PATH)
        assert (CLINIC_WRITE_METHOD, expected) in _clinic_routes()

    def test_the_read_route_is_served_as_a_single_identifier_segment(self) -> None:
        collection = "/" + "/".join(CLINIC_APPOINTMENTS_PATH)
        reads = [
            path
            for method, path in _clinic_routes()
            if method == CLINIC_READ_METHOD and path.startswith(collection + "/")
        ]
        assert len(reads) == 1, reads
        remainder = reads[0][len(collection) + 1 :]
        assert remainder.startswith("{") and remainder.endswith("}")
        assert "/" not in remainder, (
            "the clinic's read-back route is no longer one identifier segment deep, which is the "
            "shape clinic_read_resource parses"
        )

    def test_the_evaluator_does_not_track_the_clinics_other_routes(self) -> None:
        """``/clinic/slots`` exists and must remain none of the evaluator's business."""
        others = {
            path
            for _, path in _clinic_routes()
            if not path.startswith("/" + "/".join(CLINIC_APPOINTMENTS_PATH))
        }
        assert others, "the containment claim is vacuous if the clinic serves nothing else"

    def test_the_prefix_the_evaluator_parses_is_the_prefix_the_clinic_serves(self) -> None:
        assert CLINIC_APPOINTMENTS_PATH[0] == CLINIC_PREFIX.strip("/")


class TestTheSharedObservationVocabulary:
    """``ObservationOutcome`` is a genuinely shared vocabulary, unlike everything else here.

    The evaluator restates CareLoop's five members rather than importing them, because importing
    would break the independence the whole package rests on. But these two are describing the same
    fact about the same read, and a member added on one side and not the other would mean the
    evaluator reading a value it has no opinion about -- which resolves to *not MATCHED* and
    refuses a trace for a reason nobody wrote down.
    """

    def test_the_members_agree_exactly(self) -> None:
        assert {member.value for member in ObservationOutcome} == {
            member.value for member in CareLoopOutcome
        }

    def test_matched_means_the_same_thing_on_both_sides(self) -> None:
        assert ObservationOutcome.MATCHED.value == CareLoopOutcome.MATCHED.value

    def test_the_vocabulary_is_not_empty(self) -> None:
        assert len(list(ObservationOutcome)) == 5


class TestTheClaimVocabulariesTheApplicationNowEmits:
    """The three vocabularies Patch D gave CareLoop, against the evaluator's own copies.

    ``ObservationOutcome`` above is a vocabulary CareLoop already had and the evaluator restated.
    These three are the reverse: the evaluator declared them first, and the application now owns a
    copy because it has to *emit* them. The drift risk is the same in both directions and worse
    here -- an application member the evaluator does not recognise resolves to ``None`` in
    ``_member`` and is refused as *absent*, so a rename would be reported as a missing claim rather
    than as an unknown one.

    **Equalities, not containments.** A value the evaluator accepts and CareLoop never emits is
    dead vocabulary, and a value CareLoop emits and the evaluator does not accept is a trace that
    fails for a reason nobody chose.
    """

    def test_the_dispatch_vocabulary_agrees(self) -> None:
        assert {member.value for member in Dispatch} == {
            member.value for member in CareLoopDispatch
        }

    def test_the_observation_mode_vocabulary_agrees(self) -> None:
        assert {member.value for member in ObservationMode} == {
            member.value for member in CareLoopMode
        }

    def test_the_settlement_vocabulary_agrees(self) -> None:
        assert {member.value for member in Settlement} == {
            member.value for member in CareLoopSettlement
        }

    def test_none_of_them_is_empty(self) -> None:
        assert len(list(CareLoopDispatch)) == 3
        assert len(list(CareLoopMode)) == 2
        assert len(list(CareLoopSettlement)) == 2

    def test_the_field_separator_is_one_character_agreed_by_both(self) -> None:
        assert CARELOOP_FIELD_SEPARATOR == FIELD_SEPARATOR

    def test_the_field_string_the_application_renders_is_the_one_the_evaluator_expects(
        self,
    ) -> None:
        """The end-to-end anchor: what production joins must equal what the evaluator splits."""
        rendered = CARELOOP_FIELD_SEPARATOR.join(OBSERVABLE_STATE["book_appointment"].state_fields)
        assert rendered == FIELD_SEPARATOR.join(EXPECTED_FIELDS)
        assert tuple(rendered.split(FIELD_SEPARATOR)) == EXPECTED_FIELDS

    def test_the_declared_external_state_is_the_evaluators_expected_field_set(self) -> None:
        assert OBSERVABLE_STATE["book_appointment"].state_fields == EXPECTED_FIELDS

    def test_the_declared_state_covers_exactly_what_the_evaluator_thinks_is_observable(
        self,
    ) -> None:
        assert frozenset(OBSERVABLE_STATE) == OBSERVABLE_EFFECT_TYPES


class TestTheEvaluatorStillImportsNothingFromTheApplication:
    """This file imports CareLoop. The package it tests must not, and here that is re-proven.

    ``tests/test_evaluator.py`` holds the source sweep. This is the complement: a runtime check
    that the modules resolved in *this* process -- one that has already imported ``careloop`` and
    ``demoworld`` -- still reach neither from the evaluator's own source.
    """

    @pytest.mark.parametrize(
        "module",
        ["evaluators.contract", "evaluators.spans", "evaluators.settlement", "evaluators.handler"],
    )
    def test_no_evaluator_module_mentions_the_application(self, module: str) -> None:
        import importlib

        source = inspect.getsource(importlib.import_module(module))
        for banned in ("import careloop", "from careloop", "import demoworld", "from demoworld"):
            assert banned not in source, f"{module} reaches the application via {banned!r}"
