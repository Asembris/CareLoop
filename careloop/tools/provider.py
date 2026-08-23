"""What any external provider's client may raise, in vocabulary no provider owns.

**Two facts, and they are different facts about the world.** A provider that *answered and declined*
has told CareLoop something; a provider that *nobody could reach* has told CareLoop nothing. Every
layer above a client cares about that distinction and about nothing else it could learn from the
transport, which is why these two types exist and why there is not a third.

**They exist because the recovery path was written against one provider's spelling.**
:func:`careloop.graph.recovery.dispatch_prepared` translates a failed dispatch into the vocabulary
:class:`~careloop.graph.recovery.DispatchFailed` speaks, and it did so by catching
``ClinicUnavailable`` and ``ClinicRefused`` **by name**. Its own docstring claimed nothing above it
knew that a clinic speaks HTTP; that was true of its callers and false of the function itself. A
second provider raising its own exception types would have passed straight through untranslated —
so a lost upload response would have ended the case ``FAILED`` rather than ``WAITING_EXTERNAL``,
which is session Q's defect arriving through a different door.

**Provider-specific subclasses stay, and stay useful.** ``ClinicRefused`` still carries its
``status_code`` for the one caller that reads it, and ``AppointmentNotFound`` is still the narrower
thing it always was. What changes is that the *recovery* layer matches on the general type, so
adding a provider means adding a client, not editing a lifecycle.

Deliberately not a hierarchy of one. There is no shared ``ProviderError`` base, because the two
have no common handling anywhere: every site that catches one and not the other does so on purpose,
and a base class would offer a future author a way to stop distinguishing them.
"""

from __future__ import annotations


class ProviderUnavailable(Exception):
    """Nobody answered. Nothing was established about the request.

    Compatible with the provider never having seen it, having seen it and not committed, and having
    committed with the answer lost. What the caller may do about that depends on the action type's
    declared recovery class and never on this exception.
    """


class ProviderRefused(Exception):
    """The provider answered and declined.

    **A refusal is information, and what it establishes depends on when it arrives.** On a first
    attempt it usually means the write did not happen. During a reconciliation it may mean the
    opposite — a create-once store refuses a key it already holds, which is the provider saying the
    effect is *already there* — so nothing here reads a refusal as failure on the caller's behalf.
    """


__all__ = ["ProviderRefused", "ProviderUnavailable"]
