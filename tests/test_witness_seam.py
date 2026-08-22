"""The witness seam: what it may say, what it must refuse, and what it may never reach.

Four properties, and they are different claims rather than four spellings of one.

*The projections are pure and total.* They are handed domain values and return either a claim
whose every field is safe or ``None``. There is no third answer and no default, because a defaulted
field would have CareLoop compare what it did against a string it invented.

*The two digests cannot share a source.* :func:`~careloop.graph.witness._observation_claim` has no
parameter an effect digest could arrive through, which is asserted on the signature and not only on
values -- a value test proves they are equal today, and the signature test proves they could not be
made equal by editing one line.

*Nothing safe-looking leaks.* Hostile patient ids, a reason long enough to be a document and a
confirmation id shaped like a URL are pushed through the projections, and none of them appears in
anything a witness receives.

*The domain cannot be steered by telemetry.* Static, over the real package: no module under
``careloop/`` imports OpenTelemetry, nothing branches on a witness, and the two broad exception
handlers that exist for telemetry are both in the seam and both discard everything but the type.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any

import pytest

from careloop.graph.verify import (
    INDEPENDENTLY_OBSERVABLE,
    OBSERVABLE_STATE,
    ObservationOutcome,
    Verification,
    _verify_booking,
)
from careloop.graph.witness import (
    FIELD_SEPARATOR,
    NO_WITNESS,
    EffectClaim,
    EffectDispatch,
    NullWitness,
    ObservationClaim,
    ObservationMode,
    Settlement,
    _effect_claim,
    _observation_claim,
    witness_effect,
    witness_observation,
)
from careloop.tools.idempotency import args_digest, digest_mapping
from careloop.tools.results import AppointmentBooked

BOOKING = "book_appointment"

AUTHORISED: dict[str, str] = {
    "provider_id": "PR-CARDIO-1",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-01T09:00:00",
    "reason": "cardiology referral follow-up",
}

CONFIRMATION = "apt-c98a9981caefc548"


def booked(**changes: str) -> AppointmentBooked:
    """The clinic's record of the authorised booking, with any field overridden."""
    fields: dict[str, str] = {**AUTHORISED, "confirmation_id": CONFIRMATION}
    fields.update(changes)
    return AppointmentBooked(**fields)


def verification(
    *,
    passed: bool = True,
    externally_observed: bool = True,
    outcome: ObservationOutcome = ObservationOutcome.MATCHED,
) -> Verification:
    return Verification(
        passed=passed,
        diagnostic="a diagnostic that must never reach a witness",
        externally_observed=externally_observed,
        outcome=outcome,
    )


class RecordingWitness:
    """Keeps every claim it is handed, and does nothing else."""

    def __init__(self) -> None:
        self.effects: list[EffectClaim] = []
        self.observations: list[ObservationClaim] = []

    def record_effect(self, claim: EffectClaim) -> None:
        self.effects.append(claim)

    def record_observation(self, claim: ObservationClaim) -> None:
        self.observations.append(claim)


class ThrowingWitness:
    """Fails every way a witness can."""

    def record_effect(self, claim: EffectClaim) -> None:
        raise RuntimeError("the witness is broken and says so at length: PT-4417")

    def record_observation(self, claim: ObservationClaim) -> None:
        raise RuntimeError("the witness is broken and says so at length: PT-4417")


# =================================================================================================
# NullWitness
# =================================================================================================


class TestTheDefaultWitnessDoesNothing:
    def test_both_methods_return_none(self) -> None:
        witness = NullWitness()
        claim = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        assert claim is not None
        assert witness.record_effect(claim) is None
        observation = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )
        assert observation is not None
        assert witness.record_observation(observation) is None

    def test_it_carries_no_state_at_all(self) -> None:
        """Slotted and field-less, so there is nowhere for a witness to accumulate anything."""
        assert NullWitness.__dataclass_fields__ == {}
        assert not hasattr(NullWitness(), "__dict__")

    def test_it_is_frozen_and_two_are_interchangeable(self) -> None:
        assert NullWitness.__dataclass_params__.frozen is True
        assert NullWitness.__slots__ == ()
        assert NullWitness() == NullWitness()

    def test_nothing_can_be_attached_to_one(self) -> None:
        """Asserted on the behaviour rather than on an exception class.

        A frozen *slotted* dataclass raises ``TypeError`` out of its generated ``__setattr__``
        rather than ``FrozenInstanceError``, which is a CPython implementation detail and not a
        property worth pinning. What matters is that the assignment does not take.
        """
        witness = NullWitness()
        with pytest.raises((TypeError, AttributeError)):
            witness.anything = 1  # type: ignore[attr-defined]
        assert not hasattr(witness, "anything")

    def test_the_module_constant_is_one_of_them(self) -> None:
        assert isinstance(NO_WITNESS, NullWitness)

    def test_a_run_context_defaults_to_it(self) -> None:
        from careloop.graph.context import RunContext
        from careloop.state import InMemoryCaseStore

        assert RunContext(case_id="c-1", store=InMemoryCaseStore()).witness is NO_WITNESS


# =================================================================================================
# The effect projection
# =================================================================================================


class TestTheEffectProjection:
    def test_it_claims_the_action_type_the_dispatch_and_the_canonical_field_list(self) -> None:
        claim = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        assert claim is not None
        assert claim.effect_type == BOOKING
        assert claim.dispatch is EffectDispatch.PERFORMED
        assert claim.fields == "patient_id,provider_id,reason,slot_start"

    def test_the_field_list_is_rendered_from_the_declaration(self) -> None:
        """Derived, so a change to the declared state universe cannot leave this behind."""
        claim = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        assert claim is not None
        assert claim.fields == FIELD_SEPARATOR.join(OBSERVABLE_STATE[BOOKING].state_fields)

    def test_the_digest_is_lowercase_sixty_four_hex(self) -> None:
        claim = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        assert claim is not None
        assert len(claim.state_digest) == 64
        assert set(claim.state_digest) <= set("0123456789abcdef")

    def test_it_is_deterministic(self) -> None:
        first = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=dict(AUTHORISED)
        )
        second = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=dict(AUTHORISED)
        )
        assert first == second

    @pytest.mark.parametrize("dispatch", list(EffectDispatch))
    def test_the_digest_ignores_the_dispatch_mode(self, dispatch: EffectDispatch) -> None:
        """The joint state is what was authorised; what happened to it in this trace is separate."""
        claim = _effect_claim(action_type=BOOKING, dispatch=dispatch, args=AUTHORISED)
        reference = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        assert claim is not None and reference is not None
        assert claim.state_digest == reference.state_digest

    @pytest.mark.parametrize(
        "action_type", ["send_email", "upload_document", "grant_portal_access", "unknown_tool"]
    )
    def test_an_action_type_with_no_externally_held_state_claims_nothing(
        self, action_type: str
    ) -> None:
        """Three effects are simulated by design, so a claim about one could never be checked."""
        assert (
            _effect_claim(
                action_type=action_type,
                dispatch=EffectDispatch.PERFORMED,
                args={"to": "someone@example.test", "subject": "s", "body": "b"},
            )
            is None
        )

    @pytest.mark.parametrize("missing", sorted(AUTHORISED))
    def test_a_missing_field_refuses_rather_than_defaults(self, missing: str) -> None:
        args = {name: value for name, value in AUTHORISED.items() if name != missing}
        assert (
            _effect_claim(action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=args) is None
        )

    def test_a_value_with_no_canonical_form_refuses(self) -> None:
        args: dict[str, Any] = {**AUTHORISED, "reason": 3.5}
        assert (
            _effect_claim(action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=args) is None
        )

    def test_an_empty_value_is_a_value_and_is_digested(self) -> None:
        """``NOT_SUPPLIED`` is the empty string, and an empty reason is reachable by design."""
        empty = _effect_claim(
            action_type=BOOKING,
            dispatch=EffectDispatch.PERFORMED,
            args={**AUTHORISED, "reason": ""},
        )
        full = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        assert empty is not None and full is not None
        assert empty.state_digest != full.state_digest


# =================================================================================================
# The digest itself
# =================================================================================================


class TestTheJointStateDigest:
    def test_mapping_order_does_not_change_it(self) -> None:
        forwards = dict(AUTHORISED)
        backwards = {name: AUTHORISED[name] for name in reversed(list(AUTHORISED))}
        assert list(forwards) != list(backwards)
        assert digest_mapping(forwards) == digest_mapping(backwards)

    @pytest.mark.parametrize("field", sorted(AUTHORISED))
    def test_changing_exactly_one_field_changes_it(self, field: str) -> None:
        changed = {**AUTHORISED, field: AUTHORISED[field] + "-X"}
        assert digest_mapping(changed) != digest_mapping(AUTHORISED)

    def test_it_is_the_one_encoding_the_repository_already_uses(self) -> None:
        """One canonicalisation rule, three callers -- not a second answer to the same question."""
        assert digest_mapping(AUTHORISED) == args_digest(AUTHORISED)

    def test_the_encoding_is_injective_across_key_and_value_boundaries(self) -> None:
        assert digest_mapping({"ab": "c"}) != digest_mapping({"a": "bc"})

    def test_it_carries_no_case_no_action_type_and_no_confirmation(self) -> None:
        """Only the four externally held values, so two systems can compare it without agreeing
        about anything else."""
        from careloop.tools.idempotency import idempotency_key

        assert digest_mapping(AUTHORISED) != idempotency_key("case-1", BOOKING, AUTHORISED)
        assert digest_mapping(AUTHORISED) != digest_mapping(
            {**AUTHORISED, "confirmation_id": CONFIRMATION}
        )


# =================================================================================================
# The observation projection
# =================================================================================================


class TestTheObservationProjection:
    def test_it_reports_the_verification_that_was_already_reached(self) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )
        assert claim is not None
        assert claim.mode is ObservationMode.INDEPENDENT_READ
        assert claim.outcome is ObservationOutcome.MATCHED
        assert claim.settlement is Settlement.VERIFIED
        assert claim.resource_id == CONFIRMATION
        assert claim.state_digest == digest_mapping(AUTHORISED)

    @pytest.mark.parametrize("outcome", list(ObservationOutcome))
    def test_every_verification_outcome_is_carried_through_uncollapsed(
        self, outcome: ObservationOutcome
    ) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(passed=False, outcome=outcome),
            result=booked(),
            observed=None,
        )
        assert claim is not None
        assert claim.outcome is outcome

    def test_the_mode_follows_whether_a_read_took_part_not_whether_one_was_configured(self) -> None:
        """A read that was attempted and failed still took part, and saying ``NONE`` would hide
        it."""
        attempted = _observation_claim(
            action_type=BOOKING,
            verification=verification(
                passed=False, externally_observed=True, outcome=ObservationOutcome.UNAVAILABLE
            ),
            result=booked(),
            observed=None,
        )
        none_at_all = _observation_claim(
            action_type=BOOKING,
            verification=verification(
                passed=False, externally_observed=False, outcome=ObservationOutcome.NOT_OBSERVED
            ),
            result=booked(),
            observed=None,
        )
        assert attempted is not None and none_at_all is not None
        assert attempted.mode is ObservationMode.INDEPENDENT_READ
        assert none_at_all.mode is ObservationMode.NONE

    @pytest.mark.parametrize("passed", [True, False])
    def test_settlement_is_read_from_the_verdict_and_never_decided_here(self, passed: bool) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(passed=passed),
            result=booked(),
            observed=booked(),
        )
        assert claim is not None
        assert claim.settlement is (Settlement.VERIFIED if passed else Settlement.NOT_CERTIFIED)

    def test_no_observation_means_no_observed_digest(self) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(
                passed=False, externally_observed=False, outcome=ObservationOutcome.NOT_OBSERVED
            ),
            result=booked(),
            observed=None,
        )
        assert claim is not None
        assert claim.state_digest is None

    def test_the_resource_is_still_named_when_the_read_failed(self) -> None:
        """A 404 on a confirmation id is evidence, and naming it is what makes the 404 legible."""
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(passed=False, outcome=ObservationOutcome.ABSENT),
            result=booked(),
            observed=None,
        )
        assert claim is not None
        assert claim.resource_id == CONFIRMATION

    @pytest.mark.parametrize(
        "action_type", ["send_email", "upload_document", "grant_portal_access", "unknown_tool"]
    )
    def test_an_action_type_with_no_externally_held_state_claims_nothing(
        self, action_type: str
    ) -> None:
        assert (
            _observation_claim(
                action_type=action_type,
                verification=verification(),
                result=booked(),
                observed=booked(),
            )
            is None
        )


# =================================================================================================
# Digest independence -- the property the whole patch rests on
# =================================================================================================


class TestTheTwoDigestsCannotShareASource:
    """Equality between them is evidence, so it must be observed and never manufactured."""

    def test_the_observation_projection_has_no_parameter_a_digest_could_arrive_through(
        self,
    ) -> None:
        """Asserted on the signature, not on values.

        A value assertion proves the two agree today; this proves that making them agree would
        need a new parameter rather than a changed line, which is the difference between a
        property and a coincidence.
        """
        parameters = set(inspect.signature(_observation_claim).parameters)
        assert parameters == {"action_type", "verification", "result", "observed"}
        assert not any("digest" in name for name in parameters)

    def test_the_effect_projection_is_never_given_the_external_record(self) -> None:
        parameters = set(inspect.signature(_effect_claim).parameters)
        assert parameters == {"action_type", "dispatch", "args"}
        assert "observed" not in parameters and "result" not in parameters

    def test_one_field_of_divergence_moves_only_the_observed_digest(self) -> None:
        effect = _effect_claim(
            action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        agreeing = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )
        diverging = _observation_claim(
            action_type=BOOKING,
            verification=verification(passed=False, outcome=ObservationOutcome.DIVERGED),
            result=booked(),
            observed=booked(reason="ATTACKER SUPPLIED REASON"),
        )
        assert effect is not None and agreeing is not None and diverging is not None

        assert effect.state_digest == agreeing.state_digest
        assert effect.state_digest != diverging.state_digest
        assert agreeing.state_digest != diverging.state_digest

    def test_the_effect_digest_is_unmoved_by_anything_the_clinic_returns(self) -> None:
        """The same authorised arguments always key the same intended state, whatever came back."""
        digests = {
            _effect_claim(action_type=BOOKING, dispatch=dispatch, args=AUTHORISED).state_digest  # type: ignore[union-attr]
            for dispatch in EffectDispatch
        }
        assert digests == {digest_mapping(AUTHORISED)}

    def test_a_confirmation_id_never_enters_either_digest(self) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(confirmation_id="apt-something-else-entirely"),
        )
        assert claim is not None
        assert claim.state_digest == digest_mapping(AUTHORISED)


# =================================================================================================
# Privacy
# =================================================================================================

HOSTILE: dict[str, str] = {
    "provider_id": "PR-<script>alert(1)</script>",
    "patient_id": "PT-SECRET-909090",
    "slot_start": "2026-09-01T09:00:00+02:00",
    "reason": "Margaret Okonjo, 78, margaret.okonjo@example.test, +44 7700 900123, " + "x" * 4000,
}


def _claim_strings(claim: object) -> list[str]:
    """Every string a witness would actually receive, enum values included."""
    found: list[str] = []
    for name in claim.__dataclass_fields__:  # type: ignore[attr-defined]
        value = getattr(claim, name)
        if value is not None:
            found.append(str(value))
    return found


class TestNothingSensitiveCrossesTheBoundary:
    def test_no_hostile_value_appears_in_an_effect_claim(self) -> None:
        claim = _effect_claim(action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=HOSTILE)
        assert claim is not None
        rendered = " ".join(_claim_strings(claim))
        for label, value in HOSTILE.items():
            assert value not in rendered, label
        for marker in ("Margaret", "okonjo", "900123", "SECRET", "<script>", "2026-09-01"):
            assert marker not in rendered, marker

    def test_no_hostile_value_appears_in_an_observation_claim(self) -> None:
        observed = AppointmentBooked(**HOSTILE, confirmation_id=CONFIRMATION)
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=observed,
            observed=observed,
        )
        assert claim is not None
        rendered = " ".join(_claim_strings(claim))
        for label, value in HOSTILE.items():
            assert value not in rendered, label

    def test_the_verification_diagnostic_never_travels(self) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=Verification(
                passed=False,
                diagnostic="the clinic said: PT-SECRET-909090 is not the patient",
                externally_observed=True,
                outcome=ObservationOutcome.DIVERGED,
            ),
            result=booked(),
            observed=booked(patient_id="PT-SOMEBODY-ELSE"),
        )
        assert claim is not None
        assert "SECRET" not in " ".join(_claim_strings(claim))
        assert not any("diagnostic" in name for name in ObservationClaim.__dataclass_fields__)

    @pytest.mark.parametrize(
        ("label", "hostile"),
        [
            ("a path separator", "apt-1/../../etc/passwd"),
            ("a query string", "apt-1?session=leak"),
            ("a whole URL", "http://10.0.0.5:8100/clinic/appointments/apt-1"),
            ("a newline", "apt-1\nX-Injected: yes"),
            ("a space", "apt 1"),
            ("an empty string", ""),
            ("far too long", "apt-" + "a" * 200),
            ("a percent escape", "apt-1%2F%2E%2E"),
        ],
    )
    def test_a_resource_id_the_clinic_chose_is_bounded_before_it_can_be_repeated(
        self, label: str, hostile: str
    ) -> None:
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(confirmation_id=hostile),
            observed=booked(),
        )
        assert claim is not None
        assert claim.resource_id is None, label

    def test_a_well_formed_identifier_still_travels(self) -> None:
        """The bound must not be vacuous: the real shape Demo World mints has to pass."""
        claim = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )
        assert claim is not None
        assert claim.resource_id == CONFIRMATION

    def test_every_claim_field_is_short_enough_to_be_a_span_attribute(self) -> None:
        """The 120-character ceiling ``tests/test_observability.py`` already sweeps for."""
        effect = _effect_claim(action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=HOSTILE)
        observation = _observation_claim(
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )
        assert effect is not None and observation is not None
        for value in _claim_strings(effect) + _claim_strings(observation):
            assert len(value) <= 120, value[:80]


# =================================================================================================
# The failure boundary
# =================================================================================================


class TestTheSeamContainsEveryTelemetryFailure:
    def test_a_throwing_witness_is_absorbed_by_the_effect_seam(self) -> None:
        witness_effect(
            ThrowingWitness(),
            action_type=BOOKING,
            dispatch=EffectDispatch.PERFORMED,
            args=AUTHORISED,
        )

    def test_a_throwing_witness_is_absorbed_by_the_observation_seam(self) -> None:
        witness_observation(
            ThrowingWitness(),
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )

    def test_a_witness_that_is_not_one_at_all_is_absorbed(self) -> None:
        """A projection bug and a broken implementation must end the same way."""
        witness_effect(
            object(),  # type: ignore[arg-type]
            action_type=BOOKING,
            dispatch=EffectDispatch.PERFORMED,
            args=AUTHORISED,
        )

    def test_a_recording_witness_receives_exactly_one_claim_per_call(self) -> None:
        witness = RecordingWitness()
        witness_effect(
            witness, action_type=BOOKING, dispatch=EffectDispatch.PERFORMED, args=AUTHORISED
        )
        witness_observation(
            witness,
            action_type=BOOKING,
            verification=verification(),
            result=booked(),
            observed=booked(),
        )
        assert len(witness.effects) == 1
        assert len(witness.observations) == 1

    def test_an_unclaimable_action_type_reaches_the_witness_at_all(self) -> None:
        witness = RecordingWitness()
        witness_effect(
            witness,
            action_type="send_email",
            dispatch=EffectDispatch.PERFORMED,
            args={"to": "a@b.test"},
        )
        assert witness.effects == []

    def test_the_seam_never_returns_anything_a_caller_could_branch_on(self) -> None:
        assert (
            witness_effect(
                RecordingWitness(),
                action_type=BOOKING,
                dispatch=EffectDispatch.PERFORMED,
                args=AUTHORISED,
            )
            is None
        )
        assert (
            witness_observation(
                RecordingWitness(),
                action_type=BOOKING,
                verification=verification(),
                result=booked(),
                observed=booked(),
            )
            is None
        )

    def test_a_broken_witness_logs_the_exception_type_and_nothing_it_said(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG", logger="careloop.graph.witness"):
            witness_effect(
                ThrowingWitness(),
                action_type=BOOKING,
                dispatch=EffectDispatch.PERFORMED,
                args=AUTHORISED,
            )
        assert "RuntimeError" in caplog.text
        assert "PT-4417" not in caplog.text
        assert "at length" not in caplog.text


# =================================================================================================
# Structural: the domain cannot be steered by telemetry
# =================================================================================================

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "careloop"
MODULES = sorted(PACKAGE.rglob("*.py"))


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _condition_expressions(tree: ast.Module) -> list[ast.expr]:
    """Every expression whose value chooses a branch."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If | ast.While | ast.IfExp | ast.Assert):
            found.append(node.test)
        elif isinstance(node, ast.BoolOp):
            found.extend(node.values)
        elif isinstance(node, ast.comprehension):
            found.extend(node.ifs)
    return found


class TestTheApplicationCannotReachTelemetry:
    @pytest.mark.parametrize("module", MODULES, ids=lambda path: str(path.name))
    def test_no_module_imports_opentelemetry(self, module: pathlib.Path) -> None:
        """A core that imported OpenTelemetry would make telemetry an availability dependency."""
        assert "opentelemetry" not in _imported_roots(_tree(module))

    @pytest.mark.parametrize("module", MODULES, ids=lambda path: str(path.name))
    def test_no_module_imports_the_deployment_layer_or_the_evaluator(
        self, module: pathlib.Path
    ) -> None:
        roots = _imported_roots(_tree(module))
        assert "scripts" not in roots
        assert "evaluators" not in roots

    def test_the_sweep_is_not_vacuous(self) -> None:
        assert len(MODULES) > 30


class TestNothingBranchesOnAWitness:
    """A branch on the witness is where telemetry would start deciding things."""

    @pytest.mark.parametrize("module", MODULES, ids=lambda path: str(path.name))
    def test_no_condition_anywhere_reads_a_witness(self, module: pathlib.Path) -> None:
        for test in _condition_expressions(_tree(module)):
            for node in ast.walk(test):
                if isinstance(node, ast.Name):
                    assert node.id != "witness", ast.dump(test)
                if isinstance(node, ast.Attribute):
                    assert node.attr != "witness", ast.dump(test)

    def test_the_condition_sweep_actually_finds_conditions(self) -> None:
        found = _condition_expressions(_tree(PACKAGE / "graph" / "nodes.py"))
        assert len(found) > 20

    def test_the_condition_sweep_would_catch_a_witness_branch(self) -> None:
        """Vacuity proof: the same walk over a module that does branch on one must fail."""
        planted = ast.parse("if self.context.witness:\n    pass\n")
        names = {
            node.attr
            for test in _condition_expressions(planted)
            for node in ast.walk(test)
            if isinstance(node, ast.Attribute)
        }
        assert "witness" in names


class TestTheOnlyTelemetryFailureBoundaryIsTheSeam:
    def _broad_handlers(self, path: pathlib.Path) -> list[ast.ExceptHandler]:
        return [
            node
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.ExceptHandler)
            and (
                node.type is None
                or (
                    isinstance(node.type, ast.Name)
                    and node.type.id in {"Exception", "BaseException"}
                )
            )
        ]

    def test_the_graph_nodes_catch_nothing_broadly(self) -> None:
        """The seam is the boundary, so no try/except was scattered into the emission sites."""
        assert self._broad_handlers(PACKAGE / "graph" / "nodes.py") == []

    def test_the_seam_has_exactly_two_and_they_are_the_two_seam_functions(self) -> None:
        handlers = self._broad_handlers(PACKAGE / "graph" / "witness.py")
        assert len(handlers) == 2

    def test_neither_handler_reads_anything_but_the_exception_type(self) -> None:
        """``str(exc)`` would carry a clinic's own words into a log; ``type(exc)`` cannot."""
        for handler in self._broad_handlers(PACKAGE / "graph" / "witness.py"):
            bound = handler.name
            assert bound is not None
            wrapped = {
                id(argument)
                for node in ast.walk(ast.Module(body=handler.body, type_ignores=[]))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "type"
                for argument in node.args
            }
            for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                if isinstance(node, ast.Name) and node.id == bound:
                    assert id(node) in wrapped, "the exception itself escaped into the log line"


# =================================================================================================
# The declared external state, against what verification actually compares
# =================================================================================================


def _fields_verification_compares() -> frozenset[str]:
    """Derived by running the verifier, exactly as ``tests/test_evaluator_drift.py`` derives it."""
    populated = AppointmentBooked(
        provider_id="p", patient_id="q", slot_start="s", reason="r", confirmation_id="c"
    )
    return frozenset(
        problem.split(" ")[0]
        for problem in _verify_booking({}, populated)
        if " was authorised as " in problem
    )


class TestTheDeclaredExternalState:
    def test_it_is_exactly_what_the_read_back_compares(self) -> None:
        assert frozenset(OBSERVABLE_STATE[BOOKING].state_fields) == _fields_verification_compares()

    def test_the_derivation_is_not_empty(self) -> None:
        assert _fields_verification_compares()

    def test_it_covers_exactly_the_independently_observable_types(self) -> None:
        assert frozenset(OBSERVABLE_STATE) == INDEPENDENTLY_OBSERVABLE

    def test_the_field_tuple_is_sorted_and_free_of_duplicates(self) -> None:
        fields = OBSERVABLE_STATE[BOOKING].state_fields
        assert fields == tuple(sorted(set(fields)))

    def test_the_resource_identifier_is_the_one_the_read_back_is_keyed_on(self) -> None:
        assert OBSERVABLE_STATE[BOOKING].resource_id_field == "confirmation_id"
        assert hasattr(booked(), OBSERVABLE_STATE[BOOKING].resource_id_field)

    def test_the_import_time_check_refuses_a_state_universe_that_drifted(self) -> None:
        """Vacuity proof for the guard: it has to fail on the thing it exists to catch."""
        import careloop.graph.verify as verify_module

        original = verify_module.OBSERVABLE_STATE
        verify_module.OBSERVABLE_STATE = {
            BOOKING: verify_module.ObservableEffect(
                state_fields=("patient_id", "provider_id", "slot_start"),
                resource_id_field="confirmation_id",
            )
        }
        try:
            with pytest.raises(verify_module.UnverifiableAction):
                verify_module.check_observable_state_matches_verification()
        finally:
            verify_module.OBSERVABLE_STATE = original
        verify_module.check_observable_state_matches_verification()
