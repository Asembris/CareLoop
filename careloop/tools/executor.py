"""The external executor, and the mock handlers it ships with.

Every external effect in CareLoop happens here and nowhere else. The four effectful tools are thin
wrappers that build an argument mapping, derive an idempotency key, and hand both to
:meth:`MockExternalExecutor.invoke`; none of them can reach the outside world by another route.
Concentrating the effect in one object is what makes "did this action happen?" a question with a
single, checkable answer — the ledger — rather than a question about four call sites.

**Two of those handlers are real now, and the class keeps its name anyway.** ``handlers`` has been a
constructor argument since this module was written, and
:func:`careloop.graph.providers.merged_handlers` uses it to overlay a real handler for every action
type a bound provider claims — a booking against the clinic, a filing against the object store —
leaving every action type no provider claims simulated. What this class contributes — the ledger,
replay suppression, the key-collision guard — is the same either way, and it is named for the
handlers it *defaults* to. Renaming it across every call site to describe a mapping the caller
chooses would move a lot of code to say less.

**Replay suppression.** ``invoke`` looks the key up first. On a hit it returns the recorded result
with outcome :attr:`ExecutionOutcome.DUPLICATE_SUPPRESSED` and does not execute. The ledger grows
by one entry per *effect*, never per attempt, so ``invocation_count == len(ledger)`` and
"the ledger has one entry" is a real assertion rather than an accounting convention. Suppressions
are counted separately, in :attr:`suppressed_count`.

**No state mutation.** The executor records what it did; it does not write to a case, append to a
timeline, or transition anything. The :attr:`ExecutionOutcome.DUPLICATE_SUPPRESSED` outcome exists
so a graph node can later render :attr:`careloop.state.case.TimelineKind.DUPLICATE_SUPPRESSED`,
but the writing belongs to the node, not here.

**No clock, no randomness.** Synthetic identifiers derive from the idempotency key and ledger
entries carry no timestamp. A replayed call must return a result byte-identical to the first, or
the idempotency property is approximate. Time is stamped by whoever writes the timeline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.tools.content import content_digest
from careloop.tools.idempotency import args_digest, canonical_args
from careloop.tools.registry import REGISTRY
from careloop.tools.results import (
    AccessGranted,
    ActionResult,
    AppointmentBooked,
    DocumentUploaded,
    EmailSent,
    ToolResult,
)


class UnknownActionType(Exception):
    """Raised when an action type has no handler. Deny by default extends to the executor."""


class IdempotencyKeyCollision(Exception):
    """Raised when one key is presented for two different calls.

    The key is derived from the arguments, so this cannot happen by accident — it means a caller
    built a key from something other than the call it then made, or the derivation is broken.
    Either way the alternative is worse than an exception: silently returning the *first* call's
    result for a genuinely different second call is an effect that did not happen being reported
    as one that did.
    """


class ExecutionOutcome(StrEnum):
    """What happened when a call reached the executor."""

    EXECUTED = "EXECUTED"
    """The effect was performed and recorded."""

    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    """The key was already in the ledger. Nothing was performed; the first result was returned."""


class LedgerEntry(BaseModel):
    """One performed effect.

    ``args`` holds the *canonical* argument text rather than the raw values, so an entry is
    byte-stable and comparable across a replay. The raw values live in the
    :class:`careloop.policy.verdicts.Decision` the policy layer records for the timeline.
    """

    model_config = ConfigDict(frozen=True)

    idempotency_key: str
    action_type: str
    args: dict[str, str]
    args_digest: str
    result: ActionResult


class ExecutionResult(ToolResult):
    """What :meth:`MockExternalExecutor.invoke` returns, and what an effectful tool returns.

    A :class:`ToolResult`, so "every tool returns a typed result" is a statement about types. It
    wraps the payload rather than being it, because the outcome is information the caller needs:
    a graph node writes :attr:`careloop.state.case.TimelineKind.DUPLICATE_SUPPRESSED` on the
    strength of it, and it cannot recover it from the payload alone — a suppressed replay returns
    a payload identical to the original by design.
    """

    outcome: ExecutionOutcome
    idempotency_key: str
    action_type: str
    result: ActionResult

    @property
    def executed(self) -> bool:
        """Whether this call performed the effect, as opposed to replaying a recorded one."""
        return self.outcome is ExecutionOutcome.EXECUTED


#: A handler turns canonical arguments and a key into the typed result of one effect.
Handler = Callable[[Mapping[str, str], str], ActionResult]


def _suffix(idempotency_key: str) -> str:
    """A short, deterministic tail of the key, used to build synthetic identifiers."""
    return idempotency_key[:16]


def _send_email(args: Mapping[str, str], key: str) -> EmailSent:
    return EmailSent(message_id=f"msg-{_suffix(key)}", to=args["to"], subject=args["subject"])


def _book_appointment(args: Mapping[str, str], key: str) -> AppointmentBooked:
    # Every declared booking argument is echoed, including ``reason``. Not an independence claim —
    # a mock echoing its own input says nothing about the world — but ``_verify_booking`` compares
    # the result's fields against the canonical arguments whether or not a clinic was involved, so
    # a simulated booking omitting one would fail every fully-mocked run against an empty string.
    return AppointmentBooked(
        confirmation_id=f"apt-{_suffix(key)}",
        provider_id=args["provider_id"],
        patient_id=args["patient_id"],
        slot_start=args["slot_start"],
        reason=args["reason"],
    )


#: Where a simulated upload pretends to have filed something.
#:
#: Deliberately not shaped like the real provider's namespace. A mock whose keys were
#: indistinguishable from a real store's would make "which of these ran" a question somebody has to
#: answer by reading configuration.
SIMULATED_OBJECT_PREFIX: Final[str] = "simulated"


def _upload_document(args: Mapping[str, str], key: str) -> DocumentUploaded:
    """Echo every field a real store would hold, including the content digest.

    **Not an independence claim** — a mock reducing bytes CareLoop already holds says nothing
    about the world, exactly as ``_book_appointment`` echoing ``reason`` says nothing about a
    clinic. It is here because ``_verify_upload`` compares the result against the derived
    expectation whether or not a provider was involved, so a simulated upload omitting the digest
    would fail every fully mocked run against an empty string.

    The bytes are read from the shipped document store, which is the same source the derived
    expectation reads. A document the store does not hold yields an empty digest rather than a
    guess, and verification then refuses it.
    """
    from careloop.tools.documents import default_document_store

    try:
        digest = content_digest(default_document_store().content(args["document_id"]))
    except Exception:  # a document nothing holds is a verification failure, not a crash here
        digest = ""
    return DocumentUploaded(
        object_key=f"{SIMULATED_OBJECT_PREFIX}/{key}",
        object_id=key,
        portal_id=args["portal_id"],
        document_id=args["document_id"],
        content_sha256=digest,
    )


def _grant_portal_access(args: Mapping[str, str], key: str) -> AccessGranted:
    return AccessGranted(
        grant_id=f"grt-{_suffix(key)}",
        grantee=args["grantee"],
        portal_id=args["portal_id"],
        role=args["role"],
    )


DEFAULT_HANDLERS: Final[Mapping[str, Handler]] = {
    "send_email": _send_email,
    "book_appointment": _book_appointment,
    "upload_document": _upload_document,
    "grant_portal_access": _grant_portal_access,
}


def _check_handlers_cover_the_registry() -> None:
    """Fail at import if the shipped handlers and the registry disagree.

    An external-effect tool with no handler is a tool that cannot execute; a handler for a tool the
    registry does not declare is an effect outside the policy layer's knowledge. The second is the
    dangerous one, and neither should survive to a test run.
    """
    declared = REGISTRY.external_effect_tools
    handled = frozenset(DEFAULT_HANDLERS)
    if handled != declared:
        raise RuntimeError(
            f"executor handlers disagree with the registry: "
            f"declared but unhandled {sorted(declared - handled)}, "
            f"handled but undeclared {sorted(handled - declared)}"
        )


_check_handlers_cover_the_registry()


class MockExternalExecutor:
    """Performs every external effect, and remembers each one exactly once.

    A plain class rather than a model: this is a service with mutable state, not a value. Every
    record it produces is a frozen model, so nothing it hands out can be edited behind its back.

    Simulates the effect only as far as its handlers do. With :data:`DEFAULT_HANDLERS` nothing
    leaves the process; with the mapping :func:`careloop.graph.providers.merged_handlers` builds, a
    provider-claimed action really reaches its external system. The ledger means the same thing in
    both cases, which is what lets one replay assertion cover both.
    """

    def __init__(self, handlers: Mapping[str, Handler] | None = None) -> None:
        self._handlers: Final[Mapping[str, Handler]] = (
            DEFAULT_HANDLERS if handlers is None else dict(handlers)
        )
        self._ledger: list[LedgerEntry] = []
        self._suppressed = 0

    def invoke(
        self,
        action_type: str,
        args: Mapping[str, object],
        idempotency_key: str,
    ) -> ExecutionResult:
        """Perform ``action_type``, or replay the effect already recorded under this key.

        Raises:
            UnknownActionType: if no handler exists. Nothing is recorded.
            IdempotencyKeyCollision: if the key is already recorded against a different call.
        """
        canonical = canonical_args(args)
        digest = args_digest(args)

        recorded = self.entry_for(idempotency_key)
        if recorded is not None:
            if recorded.action_type != action_type or recorded.args_digest != digest:
                raise IdempotencyKeyCollision(
                    f"key {idempotency_key} is already recorded against a different call "
                    f"({recorded.action_type}); refusing to report one effect as another"
                )
            self._suppressed += 1
            return ExecutionResult(
                outcome=ExecutionOutcome.DUPLICATE_SUPPRESSED,
                idempotency_key=idempotency_key,
                action_type=action_type,
                result=recorded.result,
            )

        handler = self._handlers.get(action_type)
        if handler is None:
            raise UnknownActionType(f"no handler for action type {action_type!r}")

        result = handler(canonical, idempotency_key)
        self._ledger.append(
            LedgerEntry(
                idempotency_key=idempotency_key,
                action_type=action_type,
                args=canonical,
                args_digest=digest,
                result=result,
            )
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.EXECUTED,
            idempotency_key=idempotency_key,
            action_type=action_type,
            result=result,
        )

    @property
    def ledger(self) -> tuple[LedgerEntry, ...]:
        """Every effect performed, in order. One entry per effect, never per attempt."""
        return tuple(self._ledger)

    @property
    def invocation_count(self) -> int:
        """How many effects were actually performed."""
        return len(self._ledger)

    @property
    def suppressed_count(self) -> int:
        """How many calls were replays of an effect already recorded."""
        return self._suppressed

    @property
    def action_types(self) -> frozenset[str]:
        """Every action type this executor can perform."""
        return frozenset(self._handlers)

    @property
    def keys(self) -> frozenset[str]:
        """Every idempotency key the ledger holds."""
        return frozenset(entry.idempotency_key for entry in self._ledger)

    def entry_for(self, idempotency_key: str) -> LedgerEntry | None:
        """The recorded effect for ``idempotency_key``, if it has already happened."""
        for entry in self._ledger:
            if entry.idempotency_key == idempotency_key:
                return entry
        return None

    def entries_of_type(self, action_type: str) -> tuple[LedgerEntry, ...]:
        """Every recorded effect of one action type."""
        return tuple(entry for entry in self._ledger if entry.action_type == action_type)


__all__ = [
    "DEFAULT_HANDLERS",
    "ExecutionOutcome",
    "ExecutionResult",
    "Handler",
    "IdempotencyKeyCollision",
    "LedgerEntry",
    "MockExternalExecutor",
    "UnknownActionType",
]
