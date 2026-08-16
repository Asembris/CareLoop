"""The property every HTTP response must satisfy, as pure functions over parsed JSON.

Used by ``tests/test_api_schema.py``, which wires these into Schemathesis as custom checks so they
run against every generated request to every declared operation. They live here, importing no
Schemathesis, for two reasons: the property is then covered by the default offline suite even in an
environment where the ``schema`` extra is not installed, and a predicate over a plain document is
testable against a hand-built counterexample rather than only through a fuzz run.

**The gap this actually closes, which is not the obvious one.**

It is tempting to describe this as "checking that ``diagnostic`` is withheld from responses". That
property is already closed, and closed better: :mod:`careloop.api.projections` runs
``check_every_stored_field_is_decided`` and ``check_every_response_field_has_a_source`` at *import*,
and every response model carries ``extra="forbid"``. A field is projected or explicitly withheld
before the package will load, and a model cannot carry a field nobody declared. Re-asserting that
against a sampled response would add a second, weaker source of truth for something already
structural — which is the thing this repository refuses everywhere else.

So the modelled responses are not where a withheld field can escape. **A modelled field is guarded.
What is not guarded is every surface no projection table governs**, and there are four:

* ``GET /api/health`` returns a bare ``dict[str, str]``.
* ``POST /api/sessions`` returns a bare ``dict[str, str]``.
* ``POST /api/demo/reset`` returns a bare ``dict[str, object]``.
* **Every error path returns ``{"detail": <string>}``**, assembled from ``str(exception)`` on a
  domain exception — roughly a dozen raise sites, and nothing constrains what the next one puts in
  the message.

None of those four passes through a response model, so no import-time check sees them, ``WITHHELD``
says nothing about them, and a new endpoint next week returning a bare dict inherits no protection
whatsoever. A withheld field does not leak through a modelled field. It leaks through an exception
message somebody widens six months later, or through a convenience endpoint added under deadline
that returns whatever it happened to have in scope.

That is what a check applied to *every response of every operation, declared or not* buys, and it is
why this is worth having on top of the import-time tables rather than instead of them.

**Depth is not a detail.** ``diagnostic`` can only ever appear nested — ``CaseDetail.timeline`` is
an array of event objects, ``CaseSections`` is three arrays of ``CaseCard``, each nesting a
``DecisionView``. There is no response shape in this API where a leak surfaces at the top level.
A check that inspected only top-level keys would therefore have been **vacuously green on every
endpoint that exists** while reading like a guard. Both sweeps here recurse through objects and
arrays for that reason.

**The disjointness precondition.** :func:`withheld_keys_in` matches on field *name*, which is only
sound while the withheld names and the projected response-field names are disjoint sets. They are
today — ``{diagnostic, extracted_at}`` against 49 projected names. If a future ``WITHHELD`` entry
ever names something that is also legitimately projected, a name-based sweep would stop being a leak
detector and quietly become a false-positive generator, failing every response that correctly
carries the projected field. :func:`check_withheld_names_are_distinct` asserts the precondition so
that day is a loud failure with a name in it rather than a suite that has to be argued with.

It covers the modelled vocabulary, which is where the collision risk lives, since both sides are
drawn from the same field names. The four unmodelled surfaces above declare their keys in no table;
theirs are fixed and disjoint by inspection (``status``, ``session``, ``cases_cleared``,
``clinic_reset``, ``scenarios``, ``detail``) and the negative controls in
``tests/test_schema_checks.py`` exercise them directly.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

from careloop.api.projections import DERIVED, SOURCES, TIMELINE_PAYLOAD_FIELDS, WITHHELD

#: The key a projected timeline event carries its allowlisted payload under.
PAYLOAD_KEY: Final[str] = "payload"


class WithheldNameIsAlsoProjected(Exception):
    """Raised when a withheld field name is also the name of a projected response field.

    Not a failure of the API — a failure of :func:`withheld_keys_in`'s own precondition. See the
    module docstring.
    """


def withheld_field_names() -> frozenset[str]:
    """Every stored field name the API has decided not to serialize.

    Read from :data:`careloop.api.projections.WITHHELD` rather than hardcoded, so a second withheld
    field is covered by these sweeps the day somebody declares it instead of the day somebody
    remembers to widen a test.
    """
    return frozenset(name for fields in WITHHELD.values() for name in fields)


def projected_field_names() -> frozenset[str]:
    """Every response field name the API does serialize, sourced or derived."""
    return frozenset(
        name for table in (SOURCES, DERIVED) for view_table in table.values() for name in view_table
    )


def check_withheld_names_are_distinct(
    withheld: frozenset[str] | None = None,
    projected: frozenset[str] | None = None,
) -> None:
    """Fail unless no withheld field name is also a projected response field name.

    Both vocabularies are parameters for the reason the checks in
    :mod:`careloop.api.projections` take theirs: a guard that can only ever be run against the
    shipped tables is a guard whose failure path is untested.

    Raises:
        WithheldNameIsAlsoProjected: naming every colliding field.
    """
    held = withheld_field_names() if withheld is None else withheld
    shown = projected_field_names() if projected is None else projected
    if collisions := sorted(held & shown):
        raise WithheldNameIsAlsoProjected(
            f"{', '.join(collisions)} is both withheld and projected under the same name, so a "
            f"name-based sweep of responses can no longer tell a leak from a legitimate field. "
            f"Match on something narrower than the bare name, or reconsider the projection."
        )


def _walk(document: object) -> Iterator[Mapping[str, object]]:
    """Every mapping in ``document``, at any depth, through objects and arrays alike."""
    if isinstance(document, Mapping):
        yield document
        for value in document.values():
            yield from _walk(value)
    elif isinstance(document, (list, tuple)):
        for item in document:
            yield from _walk(item)


def withheld_keys_in(document: object) -> set[str]:
    """Every withheld field name appearing as a key anywhere in ``document``.

    Empty is the passing answer. Recurses through objects and arrays — see the module docstring on
    why a top-level-only sweep would be vacuous here.
    """
    wanted = withheld_field_names()
    return {key for mapping in _walk(document) for key in mapping if key in wanted}


def payload_keys_outside_allowlist(document: object) -> set[str]:
    """Every timeline payload key in ``document`` that the allowlist does not name.

    Empty is the passing answer. Finds any ``payload`` object at any depth, which is deliberately
    shape-agnostic: a new endpoint projecting timeline events is covered without naming it here, and
    ``TimelineEventView.payload`` is ``dict[str, Any]`` so a nested payload is expressible.
    """
    return {
        key
        for mapping in _walk(document)
        if isinstance(nested := mapping.get(PAYLOAD_KEY), Mapping)
        for key in nested
        if key not in TIMELINE_PAYLOAD_FIELDS
    }


__all__ = [
    "PAYLOAD_KEY",
    "WithheldNameIsAlsoProjected",
    "check_withheld_names_are_distinct",
    "payload_keys_outside_allowlist",
    "projected_field_names",
    "withheld_field_names",
    "withheld_keys_in",
]
