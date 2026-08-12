"""Facts and their provenance.

Every value CareLoop extracts from the outside world is wrapped in a :class:`Fact` that records
where it came from and how far it is trusted. Nothing in the system consumes a bare string that
originated in a document.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from careloop.provenance.trust import (
    ArgClass,
    PromotionForbidden,
    TrustLevel,
    check_promotable,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Provenance(BaseModel):
    """Where a fact came from."""

    model_config = ConfigDict(frozen=True)

    source_doc: str
    """Identifier of the document or message the value was lifted from."""

    page: int | None = None
    """Page number within the source, when the source is paginated."""

    extractor: str = "unknown"
    """Name of the extraction step that produced the value."""

    @property
    def citation(self) -> str:
        """Where this came from, as a caregiver reads it: ``referral.pdf p.2``.

        Carries no value and no trust level, so it is safe anywhere a summary is — which is the
        point: the timeline says *which page of which document* a fact came from, and that line is
        the visible form of invariant 3's provenance requirement. Documents without pagination
        cite the source alone rather than inventing a page number.
        """
        return f"{self.source_doc} p.{self.page}" if self.page is not None else self.source_doc


class Fact(BaseModel):
    """A single extracted value, its provenance, and its trust level.

    Immutable. :func:`promote` returns a new instance rather than raising the trust of an existing
    one, so a fact reference held elsewhere can never be silently upgraded underneath its holder.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    """Field name, e.g. ``appointment_date`` or ``referring_clinic_id``."""

    value: str
    """The extracted value, always as text. Interpretation is the validator's job."""

    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    provenance: Provenance
    extracted_at: datetime = Field(default_factory=_utcnow)

    validated_by: str | None = None
    """Name of the validator that justified the current trust level, if any."""

    @property
    def citation(self) -> str:
        """Where the value came from, as a caregiver reads it: ``referral.pdf p.2``."""
        return self.provenance.citation

    def __str__(self) -> str:
        """Render without the value, so facts are safe to log."""
        return f"Fact({self.key}, {self.trust_level}, from={self.provenance.source_doc})"


def promote(
    fact: Fact,
    *,
    to: TrustLevel,
    arg_class: ArgClass,
    validator: str,
    now: datetime | None = None,
) -> Fact:
    """Return a copy of ``fact`` at trust level ``to``.

    ``arg_class`` is the class of the tool argument the promoted fact is destined for. It is
    required rather than optional so that no caller can promote a value without stating what it
    will be used to control.

    Raises:
        PromotionForbidden: if ``arg_class`` may never be satisfied by promotion, or if ``to`` is
            not above the fact's current level. Promotion to ``TRUSTED`` is always refused:
            ``TRUSTED`` is reserved for values that never passed through untrusted content.
    """
    check_promotable(arg_class)

    if to is TrustLevel.TRUSTED:
        raise PromotionForbidden(
            "validation cannot produce TRUSTED; that level is reserved for trusted config "
            "and human decisions"
        )
    if to is not TrustLevel.CONTROLLED:
        raise PromotionForbidden(f"cannot promote to {to}")
    if fact.trust_level is not TrustLevel.UNTRUSTED:
        raise PromotionForbidden(
            f"fact {fact.key!r} is already {fact.trust_level}; promotion is not idempotent"
        )

    return fact.model_copy(
        update={
            "trust_level": TrustLevel.CONTROLLED,
            "validated_by": validator,
            "extracted_at": now or _utcnow(),
        }
    )


__all__ = ["Fact", "PromotionForbidden", "Provenance", "promote"]
