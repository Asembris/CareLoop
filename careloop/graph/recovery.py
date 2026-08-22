"""What may be done about an authorised action whose outcome is not known.

A process that dies between dispatching an external effect and recording its result leaves a case
holding a durably prepared action and no answer. There are only two safe things to do with one, and
which applies is a fact about the *provider*, not about the case, the tool's importance, or how
inconvenient stopping would be:

``IDEMPOTENT_REPLAYABLE``
    The provider keys writes by an idempotency key, durably, and returns the original resource when
    the same key arrives again. Re-issuing the frozen identity is therefore a *lookup that also
    repairs*: it cannot create a second effect, and it answers the question the crash left open.

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

from careloop.tools.clinic import ClinicRefused, ClinicUnavailable
from careloop.tools.executor import ExecutionResult
from careloop.tools.registry import REGISTRY


class UnclassifiedActionType(Exception):
    """Raised at import when an external-effect tool has no declared recovery class."""


class RecoveryClass(StrEnum):
    """What may be done about one action type whose outcome a crash left unknown."""

    IDEMPOTENT_REPLAYABLE = "IDEMPOTENT_REPLAYABLE"
    """The provider's own idempotency makes re-issuing the frozen identity safe.

    A claim about a *measured* contract and not an aspiration. For ``book_appointment`` it is Demo
    World's ``Clinic._by_key``: the same key returns the recorded appointment with ``200`` rather
    than ``201``, consumes no second slot, and refuses with ``409`` if the key names different
    arguments. ``tests/test_clinic_replay.py`` measures all three against a real service over a
    loopback socket, so this member is backed by transport rather than by a docstring.
    """

    NOT_REPLAYABLE = "NOT_REPLAYABLE"
    """Nothing CareLoop has measured makes a second dispatch safe, so it will not make one.

    The three simulated effects are here. Their handlers key off an in-process ledger that dies with
    the process, so there is no durable provider idempotency to rely on — and the real integrations
    they stand for are worse, not better. An ambiguous outcome on one of these parks the case for a
    person with :attr:`~careloop.policy.verdicts.ReasonCode.EXTERNAL_OUTCOME_NOT_ESTABLISHED`.
    """


#: The recovery class of every external-effect tool.
#:
#: **Only ``book_appointment`` is replayable, and that is the honest state of the system rather than
#: a staging post.** It is the one effect that reaches a real external service, the one whose
#: provider keys writes durably, and the one CareLoop can independently read back. The other three
#: are simulated on purpose — one action made genuinely external is the proof — so classifying them
#: as replayable would be inferring a provider guarantee from the fact that a mock is deterministic.
#:
#: Adding a member here is a claim that a measured contract exists. The test that backs it belongs
#: beside the one for booking, driven against the real service, before the entry is written.
RECOVERY_CLASS: Final[Mapping[str, RecoveryClass]] = {
    "book_appointment": RecoveryClass.IDEMPOTENT_REPLAYABLE,
    "send_email": RecoveryClass.NOT_REPLAYABLE,
    "upload_document": RecoveryClass.NOT_REPLAYABLE,
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
    closes for a recovery. The two conditions coincide today because one collaborator — the
    clinic — turns on both the real write and the read that observes it, and
    :class:`careloop.graph.context.ConflictingExecutors` makes anything else unconstructable.
    """
    if RECOVERY_CLASS.get(action_type) is not RecoveryClass.IDEMPOTENT_REPLAYABLE:
        return False
    return action_type in observers


class ReplayFailed(Exception):
    """Base for a replay that did not produce a provider result.

    Neither member says the original write did or did not land — that is precisely what a
    reconciliation failed to establish — and nothing downstream may read either as though it did.
    """


class ReplayUnavailable(ReplayFailed):
    """Nobody answered. The outcome is unknown and re-issuing the same key is still safe.

    The one condition under which waiting is the correct behaviour rather than stopping, and the
    only producer of ``WAITING_EXTERNAL``.
    """


class ReplayRefused(ReplayFailed):
    """The provider answered and declined. A definite answer that establishes nothing.

    Its sharpest instance is a key already recorded against different arguments — an identity
    contradiction no number of retries resolves, and one a person has to look at.
    """


def replay(dispatch: Callable[[], ExecutionResult]) -> ExecutionResult:
    """Re-issue one prepared action, translating transport failure into recovery vocabulary.

    **This is where HTTP stops on the recovery path**, exactly as
    :func:`careloop.graph.context._clinic_readback` is where it stops on the observation path, and
    for the same reason: a node deciding a lifecycle should be reading what a call *established*,
    not what status a service returned. Nothing above this function knows that a clinic speaks HTTP,
    which is what lets a second integration answer the same question in its own terms.

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
    except ClinicUnavailable as unreachable:
        raise ReplayUnavailable(
            "the external system could not be reached, so nothing was established about the "
            "prepared action"
        ) from unreachable
    except ClinicRefused as refused:
        raise ReplayRefused(
            "the external system answered and declined the replay, so nothing was established "
            "about the prepared action"
        ) from refused


__all__ = [
    "RECOVERY_CLASS",
    "RecoveryClass",
    "ReplayFailed",
    "ReplayRefused",
    "ReplayUnavailable",
    "UnclassifiedActionType",
    "check_recovery_classes_cover_the_registry",
    "may_replay",
    "replay",
]
