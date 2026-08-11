"""Idempotency keys: stable under reordering, unstable under any value change.

The key is the whole of the duplicate-suppression property. A key that moves when nothing
meaningful changed causes a double effect; a key that stays put when something did changes one
action into a suppressed duplicate of another. Both directions are tested, and the canonical form
is held to the policy engine's, since the two must never disagree about what a value is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from careloop.policy import canonical_value
from careloop.tools.idempotency import (
    UncanonicalizableArgument,
    args_digest,
    canonical_args,
    idempotency_key,
)

CASE = "case-0001"
ACTION = "send_email"

BASE_ARGS: dict[str, object] = {
    "to": "clinic@trusted.example",
    "subject": "Cardiology referral",
    "body": "Please find the referral attached.",
}


class TestStability:
    def test_reordering_arguments_does_not_change_the_key(self) -> None:
        forwards = idempotency_key(CASE, ACTION, BASE_ARGS)
        backwards = idempotency_key(CASE, ACTION, dict(reversed(list(BASE_ARGS.items()))))
        assert forwards == backwards

    def test_the_same_call_keys_identically_every_time(self) -> None:
        assert idempotency_key(CASE, ACTION, BASE_ARGS) == idempotency_key(CASE, ACTION, BASE_ARGS)

    def test_the_key_is_a_sha256_hex_digest(self) -> None:
        key = idempotency_key(CASE, ACTION, BASE_ARGS)
        assert len(key) == 64
        assert set(key) <= set("0123456789abcdef")

    def test_a_call_with_no_arguments_still_keys(self) -> None:
        assert len(idempotency_key(CASE, "poll", {})) == 64


class TestInstability:
    @pytest.mark.parametrize("changed", sorted(BASE_ARGS))
    def test_changing_any_single_argument_changes_the_key(self, changed: str) -> None:
        mutated = dict(BASE_ARGS) | {changed: "something else entirely"}
        assert idempotency_key(CASE, ACTION, mutated) != idempotency_key(CASE, ACTION, BASE_ARGS)

    def test_changing_the_case_changes_the_key(self) -> None:
        assert idempotency_key("case-0002", ACTION, BASE_ARGS) != idempotency_key(
            CASE, ACTION, BASE_ARGS
        )

    def test_changing_the_action_type_changes_the_key(self) -> None:
        assert idempotency_key(CASE, "upload_document", BASE_ARGS) != idempotency_key(
            CASE, ACTION, BASE_ARGS
        )

    def test_renaming_an_argument_changes_the_key(self) -> None:
        renamed = {
            "recipient" if name == "to" else name: value for name, value in BASE_ARGS.items()
        }
        assert idempotency_key(CASE, ACTION, renamed) != idempotency_key(CASE, ACTION, BASE_ARGS)

    def test_dropping_an_argument_changes_the_key(self) -> None:
        fewer = {name: value for name, value in BASE_ARGS.items() if name != "subject"}
        assert idempotency_key(CASE, ACTION, fewer) != idempotency_key(CASE, ACTION, BASE_ARGS)

    def test_the_encoding_does_not_flatten_a_name_into_a_value(self) -> None:
        """``{"ab": "c"}`` and ``{"a": "bc"}`` must not collide.

        They would under any encoding that joins names and values with a delimiter. JSON quoting
        is what stops it, so the property is asserted rather than assumed.
        """
        assert idempotency_key(CASE, ACTION, {"ab": "c"}) != idempotency_key(
            CASE, ACTION, {"a": "bc"}
        )

    def test_a_delimiter_inside_a_value_does_not_forge_another_argument(self) -> None:
        smuggled = {"subject": 'x","body":"y'}
        honest = {"subject": "x", "body": "y"}
        assert idempotency_key(CASE, ACTION, smuggled) != idempotency_key(CASE, ACTION, honest)


class TestCanonicalForm:
    """The key canonicalizes exactly as the policy engine does, or the two would drift."""

    def test_both_consumers_share_one_implementation(self) -> None:
        """Not merely equivalent — the same function object.

        Equivalent implementations drift; a shared one cannot. This fails the moment someone
        gives the policy engine its own reduction, which is the failure that would let a value be
        trusted under one spelling and keyed under another.
        """
        from careloop.provenance.canonical import canonical_value as shared
        from careloop.tools import idempotency

        assert canonical_value is shared
        assert idempotency.canonical_value is shared

    @pytest.mark.parametrize(
        "value",
        [
            "clinic@trusted.example",
            42,
            True,
            False,
            datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
            date(2026, 9, 3),
        ],
    )
    def test_a_value_keys_as_its_canonical_text_keys(self, value: object) -> None:
        as_text = canonical_value(value)
        assert as_text is not None
        assert idempotency_key(CASE, ACTION, {"a": value}) == idempotency_key(
            CASE, ACTION, {"a": as_text}
        )

    def test_two_spellings_of_one_instant_key_differently(self) -> None:
        """No timezone conversion, for the same reason the engine does none.

        ``12:00+02:00`` and ``10:00+00:00`` are the same moment. Converting would let one action
        be keyed two ways, or — worse in the other direction — let a rescheduling to a different
        wall-clock time be suppressed as a duplicate.
        """
        berlin = datetime(2026, 9, 3, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        utc = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
        assert berlin == utc
        assert idempotency_key(CASE, "book_appointment", {"slot_start": berlin}) != idempotency_key(
            CASE, "book_appointment", {"slot_start": utc}
        )

    def test_a_naive_and_an_aware_datetime_key_differently(self) -> None:
        naive = datetime(2026, 9, 3, 10, 0)
        aware = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
        assert idempotency_key(CASE, ACTION, {"a": naive}) != idempotency_key(
            CASE, ACTION, {"a": aware}
        )

    def test_true_does_not_key_as_one(self) -> None:
        """``bool`` is checked before ``int`` in ``canonical_value``; it subclasses it."""
        assert canonical_args({"a": True}) == {"a": "true"}
        assert idempotency_key(CASE, ACTION, {"a": True}) != idempotency_key(CASE, ACTION, {"a": 1})

    def test_a_number_does_not_key_as_its_string(self) -> None:
        """``42`` and ``"42"`` share a canonical form, and therefore a key, by design.

        This is the engine's rule, not a separate decision: a fact records ``"42"`` as text and
        must match an argument that arrived as an ``int``.
        """
        assert idempotency_key(CASE, ACTION, {"a": 42}) == idempotency_key(
            CASE, ACTION, {"a": "42"}
        )


class TestUncanonicalizableValues:
    """A value with no canonical form must raise, not fold onto a sentinel.

    The policy engine maps ``None`` to ``UNTRUSTED``, which fails closed for *it*. Doing anything
    similar here fails **open**: two distinct calls would share a key and the second would be
    suppressed as a duplicate of the first. Refusing to key is the fail-closed direction.
    """

    @pytest.mark.parametrize(
        "value",
        [None, 1.5, [1, 2], {"nested": "mapping"}, object()],
        ids=["none", "float", "list", "dict", "object"],
    )
    def test_an_uncanonicalizable_argument_is_refused(self, value: object) -> None:
        with pytest.raises(UncanonicalizableArgument, match="no canonical form"):
            idempotency_key(CASE, ACTION, {"a": value})

    def test_two_distinct_uncanonicalizable_values_are_never_silently_merged(self) -> None:
        for value in (1.5, 2.5):
            with pytest.raises(UncanonicalizableArgument):
                idempotency_key(CASE, ACTION, {"a": value})

    def test_the_refusal_names_the_argument(self) -> None:
        with pytest.raises(UncanonicalizableArgument, match="'amount'"):
            idempotency_key(CASE, ACTION, {"amount": 12.5})

    def test_args_digest_refuses_the_same_values(self) -> None:
        with pytest.raises(UncanonicalizableArgument):
            args_digest({"a": None})


class TestArgsDigest:
    def test_the_digest_ignores_argument_order(self) -> None:
        assert args_digest(BASE_ARGS) == args_digest(dict(reversed(list(BASE_ARGS.items()))))

    def test_the_digest_moves_with_any_value(self) -> None:
        assert args_digest(BASE_ARGS | {"subject": "other"}) != args_digest(BASE_ARGS)

    def test_the_digest_is_independent_of_the_case_and_action(self) -> None:
        """Digest identifies arguments only. Scoping is the key's job, not the digest's."""
        assert args_digest(BASE_ARGS) != idempotency_key(CASE, ACTION, BASE_ARGS)
        assert len(args_digest(BASE_ARGS)) == 64
