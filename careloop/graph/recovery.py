"""What may be done about an authorised action whose outcome is not known.

A process that dies between dispatching an external effect and recording its result leaves a case
holding a durably prepared action and no answer. There are only two safe things to do with one, and
which applies is a fact about the *provider*, not about the case, the tool's importance, or how
inconvenient stopping would be:

``IDEMPOTENT_REPLAYABLE``
    Re-issuing the frozen identity is a *lookup that also repairs*: it cannot create a second
    effect, and it answers the question the crash left open. **How a provider delivers that is its
    own business** — see the member's docstring for the two measured shapes, which have almost
    nothing in common mechanically.

``NOT_REPLAYABLE``
    It does not, or CareLoop has not measured that it does. An action of this class whose outcome is
    unknown stops, and a person decides. An email that may or may not have been sent is not
    something an agent may decide to send again.

**The declaration is data with an import-time coverage check**, the same arrangement
:data:`careloop.graph.verify.VERIFIERS` and :data:`careloop.graph.verify.RESULT_TYPES` use, and for
the same reason: an effectful tool with no declared recovery class is a tool whose crash behaviour
is decided by whichever branch happens to run, and a class declared for a tool the registry does not
have is dead code pretending to be a policy. Neither should survive to a test run.

**It lives here rather than in a node**, because "may this action be re-attempted" is a domain fact
about an integration, and a tool name in lifecycle code is exactly what
``check_verifiers_cover_the_registry`` exists to prevent one layer down.

**Replay is licensed by the write contract, never by an observation.** Nothing in this module reads
an :class:`careloop.graph.verify.ObservationOutcome`, and nothing anywhere may turn ``ABSENT``
into a retry: a negative read says the provider does not hold the effect *now*, in the world this
client addressed, which is not the same statement as "the write never landed and may be
repeated". The
licence is decided before any read is made, which is what keeps the two questions apart.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from enum import StrEnum
from typing import Final

from careloop.tools.executor import ExecutionResult
from careloop.tools.provider import ProviderRefused, ProviderUnavailable
from careloop.tools.registry import REGISTRY


class UnclassifiedActionType(Exception):
    """Raised at import when an external-effect tool has no declared recovery class."""


class RecoveryClass(StrEnum):
    """What may be done about one action type whose outcome a crash left unknown."""

    IDEMPOTENT_REPLAYABLE = "IDEMPOTENT_REPLAYABLE"
    """Re-issuing the frozen identity cannot create a second effect and settles what the crash left
    open.

    A claim about a *measured* contract and not an aspiration — and deliberately a claim about the
    **outcome** rather than about a mechanism, because the two members of this class deliver it in
    ways that share nothing.

    ``book_appointment`` is the shape this member used to be defined as: Demo World's
    ``Clinic._by_key`` keys writes by the idempotency header and **returns the original
    appointment** with ``200`` rather than ``201``, consumes no second slot, and refuses with
    ``409`` if the key names different arguments. ``tests/test_clinic_replay.py`` measures all three
    against a real service over a loopback socket.

    ``upload_document`` earns the same class by a different route, and the earlier wording would
    have been false about it. **Amazon S3 has no concept of an idempotency key and returns nothing
    on a duplicate**: a ``PutObject`` carrying ``IfNoneMatch="*"`` answers ``412
    PreconditionFailed`` and leaves the existing object exactly as it was. What makes the replay
    safe is *create-once* — measured, and measured together with the fact that dropping the
    precondition silently overwrites — and what settles the outcome is a fresh ``GetObject`` that
    :meth:`careloop.tools.objectstore.ObjectStoreClient.put_once` issues in answer to the ``412``,
    comparing the bytes the store returned against the bytes CareLoop was about to write. Equal is
    convergence; different is
    :class:`~careloop.tools.objectstore.ObjectIdentityConflict` and fails closed.
    ``docs/objectstore-contract.md`` records the measurement and
    ``tests/test_objectstore_contract.py`` re-runs it offline on every suite run.

    So: *the provider guarantees no second effect; CareLoop assembles the answer.* A member here
    asserts the first and takes responsibility for the second.
    """

    NOT_REPLAYABLE = "NOT_REPLAYABLE"
    """Nothing CareLoop has measured makes a second dispatch safe, so it will not make one.

    The two remaining simulated effects are here. Their handlers key off an in-process ledger that
    dies with the process, so there is no durable provider idempotency to rely on — and the real
    integrations they stand for are worse, not better. An email that may or may not have been sent
    is the clearest case: no transactional provider CareLoop has measured offers a way to ask, and
    a second send is a second message in somebody's inbox. An ambiguous outcome on one of these
    parks the case for a person with
    :attr:`~careloop.policy.verdicts.ReasonCode.EXTERNAL_OUTCOME_NOT_ESTABLISHED`.
    """


#: The recovery class of every external-effect tool.
#:
#: **The two effects with real providers are replayable and the two simulated ones are not**, which
#: is the honest state of the system. ``send_email`` and ``grant_portal_access`` reach no external
#: service: their handlers key off an in-process ledger that dies with the process, so classifying
#: them as replayable would be inferring a provider guarantee from the fact that a mock is
#: deterministic — and the real integrations they stand for offer less, not more.
#:
#: Adding a member here is a claim that a measured contract exists, and the two that are here were
#: measured separately against their own providers before the entry was written:
#: ``tests/test_clinic_replay.py`` for the clinic and ``docs/objectstore-contract.md`` plus
#: ``tests/test_objectstore_contract.py`` for the object store. Note how little the two contracts
#: have in common — one replays the original record, the other refuses and makes CareLoop look —
#: which is exactly why :class:`RecoveryClass` is defined by the outcome it guarantees rather than
#: by the mechanism that delivers it.
RECOVERY_CLASS: Final[Mapping[str, RecoveryClass]] = {
    "book_appointment": RecoveryClass.IDEMPOTENT_REPLAYABLE,
    "upload_document": RecoveryClass.IDEMPOTENT_REPLAYABLE,
    "send_email": RecoveryClass.NOT_REPLAYABLE,
    "grant_portal_access": RecoveryClass.NOT_REPLAYABLE,
}


def check_recovery_classes_cover_the_registry() -> None:
    """Fail at import if the declared classes and the registry's effectful tools disagree.

    An effectful tool with no class is a tool whose crash behaviour nobody decided; a class for a
    tool the registry does not declare is a policy about an action that cannot happen. The first is
    the dangerous one — reconciliation would have to guess — and guessing in the permissive
    direction is a second external effect.
    """
    declared = REGISTRY.external_effect_tools
    classified = frozenset(RECOVERY_CLASS)
    if classified != declared:
        raise UnclassifiedActionType(
            f"recovery classes disagree with the registry: "
            f"declared but unclassified {sorted(declared - classified)}, "
            f"classified but undeclared {sorted(classified - declared)}"
        )


check_recovery_classes_cover_the_registry()


def may_replay(action_type: str, *, observers: Collection[str]) -> bool:
    """Whether a prepared action of this type may be re-issued under its frozen identity.

    Two conditions, and both are necessary.

    The **declared class** must be :attr:`RecoveryClass.IDEMPOTENT_REPLAYABLE`. An unknown action
    type answers ``False``, which is the fail-closed direction and is unreachable while the
    import-time check holds.

    An **independent observer** must be bound for it in this run — ``observers`` is
    :attr:`careloop.graph.context.RunContext.readbacks`. This is not a convenience: a replay whose
    result nothing could read back would be recorded and then verified closed-loop, so the case
    would reach ``DONE`` on the strength of CareLoop's own receipt for an effect it was not sure had
    happened. That is certifying by *removing* evidence, which is the hazard
    :meth:`careloop.graph.nodes.VerifyNode._nothing_was_observed` closes for a re-check and this
    closes for a recovery. The two conditions coincide because an
    :class:`careloop.graph.providers.ExternalProvider` declares its handlers and its read-backs
    together — one collaborator turns on both the real write and the read that observes it — and
    :class:`careloop.graph.context.ConflictingExecutors` makes anything else unconstructable.
    """
    if RECOVERY_CLASS.get(action_type) is not RecoveryClass.IDEMPOTENT_REPLAYABLE:
        return False
    return action_type in observers


class DispatchFailed(Exception):
    """Base for a dispatch that did not produce a provider result.

    Neither member says the write did or did not land — that is precisely what was not established
    — and nothing downstream may read either as though it did.
    """


class DispatchUnavailable(DispatchFailed):
    """Nobody answered. The outcome is unknown and re-issuing the same key is still safe.

    **The ambiguous outcome, and the reason this is not a failure.** A request whose response never
    came back is compatible with the provider having committed and with it never having seen the
    request at all — measured against a real clinic, an appointment can already exist while CareLoop
    holds nothing. Treating it as failure would file a real external effect under "did not happen";
    treating it as success would certify a booking nobody has seen.

    The one condition under which waiting is the correct behaviour rather than stopping, and the
    only producer of ``WAITING_EXTERNAL``.
    """


class DispatchRefused(DispatchFailed):
    """The provider answered and declined.

    **What that establishes depends on when it happens**, which is why the two nodes handle it
    differently rather than sharing a branch. On a first attempt the clinic is saying it did not
    make this booking — an unknown slot, a slot somebody else holds — and the case fails, which is
    the behaviour that predates the saga and is unchanged by it. During a *reconciliation* the
    outcome was already unknown before the request was issued, and a refusal does not settle it:
    its sharpest instance is a key already recorded against different arguments, an identity
    contradiction no number of retries resolves and one a person has to look at.
    """


def dispatch_prepared(dispatch: Callable[[], ExecutionResult]) -> ExecutionResult:
    """Issue one prepared action, translating transport failure into recovery vocabulary.

    **Both nodes that can send an effect go through here**, the first attempt and the
    reconciliation alike, because the transport failures are the same failures either way and a
    second translation would be a second answer. What differs is what each node *does* with the
    result, and that difference lives in the nodes where it can be read.

    **This is where transport stops on the recovery path**, exactly as
    :func:`careloop.graph.providers.clinic_readback` is where it stops on the observation path, and
    for the same reason: a node deciding a lifecycle should be reading what a call *established*,
    not what status a service returned.

    It matches on :mod:`careloop.tools.provider`'s two neutral types rather than on any client's
    own, which is what makes the sentence above true of this function and not merely of its callers.
    It named ``ClinicUnavailable`` and ``ClinicRefused`` until a second provider existed, and a
    provider raising anything else would have passed straight through untranslated — so a lost
    response would have failed the case instead of leaving it waiting.

    ``dispatch`` is a thunk rather than a tool name and an argument map, so this function chooses
    nothing about *what* is re-issued — the caller has already proved which action that is, and
    handing the identity in here would be a second place it could be decided.

    Anything that is not transport passes through untouched. A :class:`careloop.policy.gate
    .PolicyDenied` is the authority layer refusing and is the caller's to handle; an
    :class:`careloop.tools.executor.IdempotencyKeyCollision` or an
    :class:`careloop.tools.idempotency.UncanonicalizableArgument` is CareLoop being wrong about its
    own identity, which should fail the case loudly rather than be reported as an unknown outcome.
    """
    try:
        return dispatch()
    except ProviderUnavailable as unreachable:
        raise DispatchUnavailable(
            "the external system could not be reached, so nothing was established about the "
            "prepared action"
        ) from unreachable
    except ProviderRefused as refused:
        raise DispatchRefused(
            "the external system answered and declined the replay, so nothing was established "
            "about the prepared action"
        ) from refused


__all__ = [
    "RECOVERY_CLASS",
    "DispatchFailed",
    "DispatchRefused",
    "DispatchUnavailable",
    "RecoveryClass",
    "UnclassifiedActionType",
    "check_recovery_classes_cover_the_registry",
    "dispatch_prepared",
    "may_replay",
]
