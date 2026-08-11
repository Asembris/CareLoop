"""The never-promote invariant is the whole defence against document-sourced destinations."""

from __future__ import annotations

import pytest

from careloop.provenance import (
    PROMOTABLE_ARG_CLASSES,
    ArgClass,
    Fact,
    PromotionForbidden,
    Provenance,
    TrustLevel,
    at_least,
    promote,
)


def test_extracted_facts_start_untrusted(untrusted_fact: Fact) -> None:
    assert untrusted_fact.trust_level is TrustLevel.UNTRUSTED


def test_value_arguments_can_be_promoted_after_validation(untrusted_fact: Fact) -> None:
    promoted = promote(
        untrusted_fact, to=TrustLevel.CONTROLLED, arg_class=ArgClass.VALUE, validator="iso_date"
    )
    assert promoted.trust_level is TrustLevel.CONTROLLED
    assert promoted.validated_by == "iso_date"
    assert promoted.value == untrusted_fact.value


@pytest.mark.parametrize("arg_class", [ArgClass.DESTINATION, ArgClass.AUTHORITY])
def test_destination_and_authority_can_never_be_promoted(
    untrusted_fact: Fact, arg_class: ArgClass
) -> None:
    """Even with a validator that passes. This is the injection defence in one assertion."""
    with pytest.raises(PromotionForbidden):
        promote(
            untrusted_fact,
            to=TrustLevel.CONTROLLED,
            arg_class=arg_class,
            validator="rfc5322_email_validator_that_passes",
        )


def test_promotable_set_contains_only_value() -> None:
    """Guards against a future widening that would quietly defeat the trust model."""
    assert set(PROMOTABLE_ARG_CLASSES) == {ArgClass.VALUE}


def test_validation_cannot_manufacture_trusted(untrusted_fact: Fact) -> None:
    with pytest.raises(PromotionForbidden, match="reserved"):
        promote(
            untrusted_fact, to=TrustLevel.TRUSTED, arg_class=ArgClass.VALUE, validator="anything"
        )


def test_promotion_is_not_idempotent(untrusted_fact: Fact) -> None:
    """Re-promoting would let a caller launder trust by validating twice."""
    once = promote(
        untrusted_fact, to=TrustLevel.CONTROLLED, arg_class=ArgClass.VALUE, validator="v1"
    )
    with pytest.raises(PromotionForbidden, match="already"):
        promote(once, to=TrustLevel.CONTROLLED, arg_class=ArgClass.VALUE, validator="v2")


def test_promotion_returns_a_new_fact(untrusted_fact: Fact) -> None:
    promoted = promote(
        untrusted_fact, to=TrustLevel.CONTROLLED, arg_class=ArgClass.VALUE, validator="v"
    )
    assert promoted is not untrusted_fact
    assert untrusted_fact.trust_level is TrustLevel.UNTRUSTED


def test_fact_is_frozen(untrusted_fact: Fact) -> None:
    with pytest.raises(Exception, match=r"frozen|immutable"):
        untrusted_fact.trust_level = TrustLevel.TRUSTED  # type: ignore[misc]


def test_provenance_is_carried_through_promotion(untrusted_fact: Fact) -> None:
    promoted = promote(
        untrusted_fact, to=TrustLevel.CONTROLLED, arg_class=ArgClass.VALUE, validator="v"
    )
    assert promoted.provenance.source_doc == "referral.pdf"
    assert promoted.provenance.page == 2


def test_every_fact_requires_provenance() -> None:
    with pytest.raises(Exception, match="provenance"):
        Fact(key="k", value="v")  # type: ignore[call-arg]


def test_fact_repr_does_not_leak_the_value() -> None:
    """Facts get logged; document-sourced values should not ride along into logs by default."""
    fact = Fact(
        key="recipient",
        value="attacker@evil.example",
        provenance=Provenance(source_doc="referral.pdf"),
    )
    assert "attacker@evil.example" not in str(fact)


@pytest.mark.parametrize(
    ("level", "minimum", "expected"),
    [
        (TrustLevel.UNTRUSTED, TrustLevel.UNTRUSTED, True),
        (TrustLevel.UNTRUSTED, TrustLevel.CONTROLLED, False),
        (TrustLevel.CONTROLLED, TrustLevel.UNTRUSTED, True),
        (TrustLevel.CONTROLLED, TrustLevel.TRUSTED, False),
        (TrustLevel.TRUSTED, TrustLevel.CONTROLLED, True),
    ],
)
def test_trust_ordering(level: TrustLevel, minimum: TrustLevel, expected: bool) -> None:
    assert at_least(level, minimum) is expected
