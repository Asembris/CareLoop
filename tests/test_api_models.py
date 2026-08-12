"""Response models can only say what the projector gave them.

The half of "serialization is not a second source of truth" that lives on the models themselves: no
field default, no ``default_factory``, nothing extra, nothing mutable after construction. The other
half — every field naming where it comes from — is in ``test_api_projections.py``.
"""

from __future__ import annotations

import pytest
from pydantic import Field, ValidationError

from careloop.api import models
from careloop.api.models import (
    RESPONSE_MODELS,
    CaseSections,
    ResponseFieldHasDefault,
    ResponseModel,
    check_no_response_defaults,
)


class TestNoResponseFieldCanInventAValue:
    """Invariant 9's no-defaults rule for tool results, applied to responses for the same reason."""

    def test_the_shipped_models_carry_no_defaults(self) -> None:
        check_no_response_defaults()

    @pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda model: model.__name__)
    def test_every_field_of_every_shipped_model_is_required(self, model) -> None:
        optional = sorted(
            name for name, field in model.model_fields.items() if not field.is_required()
        )

        assert optional == [], (
            f"{model.__name__}.{optional} would serialize without the projector supplying them, "
            f"so the response could report a value the store never held"
        )

    def test_a_plain_default_is_caught(self) -> None:
        class Invented(ResponseModel):
            case_id: str
            state: str = "DONE"

        with pytest.raises(ResponseFieldHasDefault, match=r"Invented\.state"):
            check_no_response_defaults((Invented,))

    def test_a_default_factory_is_caught(self) -> None:
        """The shape invariant 9 found the hard way: a clock hiding behind a factory."""

        class Stamped(ResponseModel):
            case_id: str
            rendered_at: str = Field(default_factory=lambda: "2026-08-12T00:00:00Z")

        with pytest.raises(ResponseFieldHasDefault, match=r"Stamped\.rendered_at"):
            check_no_response_defaults((Stamped,))

    def test_the_check_passes_a_model_that_is_actually_clean(self) -> None:
        """Otherwise it could be rejecting everything and the two tests above prove nothing."""

        class Clean(ResponseModel):
            case_id: str
            state: str

        check_no_response_defaults((Clean,))


class TestTheModelListIsTheWholeModelList:
    def test_response_models_names_every_shape_defined_in_the_module(self) -> None:
        """A model defined and left out of RESPONSE_MODELS escapes the no-defaults check."""
        defined = {
            value
            for value in vars(models).values()
            if isinstance(value, type)
            and issubclass(value, ResponseModel)
            and value is not ResponseModel
        }

        assert defined == set(RESPONSE_MODELS)


class TestResponsesAreFrozenAndClosed:
    def test_a_response_cannot_be_edited_after_projection(self) -> None:
        """A handler that could edit a projection is a second place the store's answer changes."""
        sections = CaseSections(needs_you=[], working=[], completed=[])

        with pytest.raises(ValidationError):
            sections.needs_you = ["anything"]

    def test_an_unexpected_keyword_fails_rather_than_being_dropped(self) -> None:
        with pytest.raises(ValidationError):
            CaseSections(needs_you=[], working=[], completed=[], archived=[])
