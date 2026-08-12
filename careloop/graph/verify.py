"""What "verified" means, per action type.

``DONE`` is reachable only from ``VERIFYING``, which makes "the case did not finish via the unsafe
path" a structural property rather than an observation. That is worth nothing unless ``VERIFYING``
does something, and open decision 5 left what it does undefined. This module defines it.

**Verified means: the effect that was performed is the effect that was authorised.**

Not "the outside world did the right thing". Against a mock executor that would be theatre — the
mock cannot know whether the clinic honoured the booking, and a check that pretended to would be a
green tick with nothing behind it. What this checks is the gap that actually exists in an authority
system: between what the policy engine approved and what the executor did. Three things, in order:

1. The idempotency key re-derived from the authorised arguments equals the key the action was
   recorded under. A mismatch means the call that happened is not the call that was decided.
2. The recorded argument digest matches. Same property, arrived at independently, and it holds
   even if the key derivation is what is broken.
3. A per-action-type predicate over the typed result: every field the result echoes back equals
   the argument that was authorised, and the synthetic identifier is present.

Step 3 is the one that would catch an executor returning somebody else's confirmation. It is
worth stating what it does **not** catch, and the README says so too: nothing here proves the
effect reached a real system, because in this build there is no real system. Verification closes
the loop between decision and execution, not between execution and the world.

:data:`VERIFIERS` is checked against ``REGISTRY.external_effect_tools`` at import, the same way the
executor's handlers and the tool bodies are. An effectful tool with no verifier is a tool whose
case could reach ``DONE`` without anything having been checked, so it fails on import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.state.case import ActionRecord
from careloop.tools.idempotency import args_digest, canonical_args, idempotency_key
from careloop.tools.registry import REGISTRY
from careloop.tools.results import (
    AccessGranted,
    ActionResult,
    AppointmentBooked,
    DocumentUploaded,
    EmailSent,
)


class Verification(BaseModel):
    """The outcome of verifying one action."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    diagnostic: str
    """Timeline-only. Names fields and may quote values; never returned to a model."""

    @property
    def failed(self) -> bool:
        """Whether verification refused the action."""
        return not self.passed


#: A verifier compares the canonical authorised arguments against the typed result.
Verifier = Callable[[Mapping[str, str], ActionResult], list[str]]
"""Returns the list of mismatches. Empty means verified."""


def _mismatches(
    result: ActionResult,
    args: Mapping[str, str],
    fields: Mapping[str, str],
    identifier: str,
) -> list[str]:
    """Compare each ``result`` field against the argument it echoes, plus the synthetic id."""
    found = [
        f"{field} was authorised as {args.get(arg_name)!r} but the result carries "
        f"{getattr(result, field)!r}"
        for field, arg_name in fields.items()
        if getattr(result, field) != args.get(arg_name)
    ]
    if not getattr(result, identifier, ""):
        found.append(f"{identifier} is empty; the executor recorded no identifier for the effect")
    return found


def _verify_email(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, EmailSent):
        return [f"expected EmailSent, the action recorded a {type(result).__name__}"]
    return _mismatches(result, args, {"to": "to", "subject": "subject"}, "message_id")


def _verify_booking(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, AppointmentBooked):
        return [f"expected AppointmentBooked, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result,
        args,
        {"provider_id": "provider_id", "patient_id": "patient_id", "slot_start": "slot_start"},
        "confirmation_id",
    )


def _verify_upload(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, DocumentUploaded):
        return [f"expected DocumentUploaded, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result, args, {"portal_id": "portal_id", "document_id": "document_id"}, "upload_id"
    )


def _verify_grant(args: Mapping[str, str], result: ActionResult) -> list[str]:
    if not isinstance(result, AccessGranted):
        return [f"expected AccessGranted, the action recorded a {type(result).__name__}"]
    return _mismatches(
        result,
        args,
        {"grantee": "grantee", "role": "role", "portal_id": "portal_id"},
        "grant_id",
    )


VERIFIERS: Final[Mapping[str, Verifier]] = {
    "send_email": _verify_email,
    "book_appointment": _verify_booking,
    "upload_document": _verify_upload,
    "grant_portal_access": _verify_grant,
}


class UnverifiableAction(Exception):
    """Raised at import when an external-effect tool has no verifier."""


def check_verifiers_cover_the_registry() -> None:
    """Fail at import if the verifiers and the registry's effectful tools disagree.

    An effectful tool with no verifier is a tool whose case could pass through ``VERIFYING``
    without anything being checked and reach ``DONE`` anyway, which would make invariant 4's
    structural claim hollow. A verifier for a tool the registry does not declare is dead code
    pretending to be a guard.
    """
    declared = REGISTRY.external_effect_tools
    verified = frozenset(VERIFIERS)
    if verified != declared:
        raise UnverifiableAction(
            f"verifiers disagree with the registry: "
            f"declared but unverifiable {sorted(declared - verified)}, "
            f"verifiable but undeclared {sorted(verified - declared)}"
        )


check_verifiers_cover_the_registry()


def verify_action(
    *,
    case_id: str,
    action_type: str,
    args: Mapping[str, object],
    record: ActionRecord,
    result: ActionResult,
) -> Verification:
    """Verify that ``record`` and ``result`` are the action ``args`` authorised.

    ``args`` are the arguments the policy engine allowed, not the arguments the executor was
    handed — comparing the executor's inputs to the executor's outputs would verify nothing about
    authority. They come from the approved proposal.
    """
    problems: list[str] = []

    expected_key = idempotency_key(case_id, action_type, args)
    if expected_key != record.idempotency_key:
        problems.append(
            f"the authorised arguments key to {expected_key[:16]}... but the action was recorded "
            f"under {record.idempotency_key[:16]}...; the call that happened is not the call that "
            f"was decided"
        )

    expected_digest = args_digest(args)
    if expected_digest != record.args_digest:
        problems.append(
            f"argument digest {expected_digest[:16]}... does not match the recorded "
            f"{record.args_digest[:16]}..."
        )

    if record.action_type != action_type:
        problems.append(
            f"the authorised action was {action_type!r}; the record carries {record.action_type!r}"
        )

    verifier = VERIFIERS.get(action_type)
    if verifier is None:
        # Unreachable while the import-time check holds. Fail closed rather than pass by omission.
        problems.append(f"no verifier for action type {action_type!r}")
    else:
        problems.extend(verifier(canonical_args(args), result))

    if problems:
        return Verification(passed=False, diagnostic="; ".join(problems))
    return Verification(
        passed=True,
        diagnostic=(
            f"{action_type} verified: the recorded effect keys to the authorised arguments and "
            f"every field it echoes matches what policy allowed"
        ),
    )


__all__ = [
    "VERIFIERS",
    "UnverifiableAction",
    "Verification",
    "Verifier",
    "check_verifiers_cover_the_registry",
    "verify_action",
]
