"""Binding a run to real external systems, one provider at a time.

**What a provider is, here.** Two mappings keyed by action type: the *handlers* that make an effect
really leave the process, and the *read-backs* that independently observe what it did. They arrive
together and they are declared together, because a real effect verified against a simulated
observation is the exact tautology the read-back exists to remove —
:class:`careloop.graph.context.ConflictingExecutors` has made that combination unconstructable since
the clinic was written, and this module is where the rule stops being about one clinic.

**Why it exists.** ``RunContext`` built its observers as
``{} if clinic is None else {"book_appointment": _clinic_readback(clinic)}`` — a tool name spelled
in lifecycle wiring, which is precisely what
:func:`careloop.graph.verify.check_verifiers_cover_the_registry` exists to prevent one layer down.
Adding a second real integration through that shape would have meant a second literal beside the
first, and a third would have meant three.

**Providers are merged, and a collision is an error rather than a winner.** Two providers claiming
one action type is a composition mistake with no safe resolution: whichever the merge happened to
prefer would silently decide where a real effect lands. Refusing is the only answer that cannot be
wrong.

**This is the layer where each provider's transport vocabulary becomes observation vocabulary**,
and each provider does its own translating. A clinic's ``404`` on a confirmation id and an object
store's ``NoSuchKey`` are the same *fact* — the provider answering that it holds no such thing —
expressed in two protocols, and the graph above must see the fact. Nothing above this module knows
either protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from careloop.graph.verify import (
    ObservationAbsent,
    ObservationUnavailable,
    ReadBack,
)
from careloop.tools.clinic import (
    AppointmentNotFound,
    ClinicClient,
    ClinicRefused,
    ClinicUnavailable,
)
from careloop.tools.executor import Handler
from careloop.tools.results import ActionResult


class ConflictingProviders(Exception):
    """Raised when two providers claim the same action type.

    Either could be the one a real effect reaches, and nothing in a merge can decide which was
    meant. A run built like this is a composition error and fails at construction rather than
    dispatching somewhere plausible.
    """


@dataclass(frozen=True, slots=True)
class ExternalProvider:
    """One real external system a run may be bound to.

    ``handlers`` overlay :data:`careloop.tools.executor.DEFAULT_HANDLERS`, so every action type this
    provider does not claim stays simulated. ``readbacks`` say which of its action types can be
    independently observed *in this run*; it is deliberately allowed to be narrower than
    ``handlers``, because whether a provider is reachable for reading is a separate question from
    whether it is reachable for writing — but a provider that writes without offering a read is
    exactly what :func:`careloop.graph.recovery.may_replay` refuses to replay.

    ``name`` is for diagnostics only. Nothing branches on it, and nothing may: which provider a run
    holds is a fact about wiring, and a lifecycle that could read it would be a lifecycle with a
    tool name in it again.
    """

    name: str
    handlers: Mapping[str, Handler]
    readbacks: Mapping[str, ReadBack]


def merged_handlers(providers: tuple[ExternalProvider, ...]) -> dict[str, Handler]:
    """Every provider's handlers, over the simulated defaults.

    Raises:
        ConflictingProviders: if two providers claim one action type.
    """
    from careloop.tools.executor import DEFAULT_HANDLERS

    merged: dict[str, Handler] = dict(DEFAULT_HANDLERS)
    claimed: dict[str, str] = {}
    for provider in providers:
        for action_type in provider.handlers:
            if action_type in claimed:
                raise ConflictingProviders(
                    f"{claimed[action_type]!r} and {provider.name!r} both claim {action_type!r}; "
                    f"a merge cannot decide which external system a real effect should reach"
                )
            claimed[action_type] = provider.name
        merged.update(provider.handlers)
    return merged


def merged_readbacks(providers: tuple[ExternalProvider, ...]) -> dict[str, ReadBack]:
    """Every provider's independent observers.

    Raises:
        ConflictingProviders: if two providers offer an observer for one action type.
    """
    merged: dict[str, ReadBack] = {}
    claimed: dict[str, str] = {}
    for provider in providers:
        for action_type in provider.readbacks:
            if action_type in claimed:
                raise ConflictingProviders(
                    f"{claimed[action_type]!r} and {provider.name!r} both observe {action_type!r}; "
                    f"a merge cannot decide which read is the independent one"
                )
            claimed[action_type] = provider.name
        merged.update(provider.readbacks)
    return merged


#: The action type the clinic is the external system for.
CLINIC_ACTION: Final[str] = "book_appointment"


def clinic_readback(client: ClinicClient) -> ReadBack:
    """Observe a booking by asking the clinic what it holds now.

    **A separate GET, keyed on the confirmation the effect returned.** It is not the booking's own
    response replayed, and it is not read from anything CareLoop stored — those would both make the
    check a re-read of CareLoop's receipt.

    **This is the boundary where HTTP stops.** Everything the client can raise is translated into
    the observation vocabulary of :mod:`careloop.graph.verify`, so a verifier and a graph node see
    what the read *established* rather than what status a service returned.

    Two answers, and they are different facts:

    ``AppointmentNotFound`` — the clinic answered about the exact confirmation it minted and said
    it holds no such appointment. A definite external answer, so
    :class:`~careloop.graph.verify.ObservationAbsent`.

    Anything else — nobody answered, or what answered could not serve the read. CareLoop
    established nothing, so :class:`~careloop.graph.verify.ObservationUnavailable`. The originating
    exception is kept as ``__cause__`` rather than discarded: distinguishing a transient cause from
    a permanent one is a judgement that is deliberately not made here, and re-deriving it later
    should not mean re-plumbing this.

    The ordering is load-bearing — ``AppointmentNotFound`` is a ``ClinicRefused``, so the narrower
    clause has to come first or the absence is swallowed by the general case.

    **The message is identical for both**, and that is deliberate too. What the failure *means*
    lives in its type; a reader who wants the clinic's own words has the diagnostic. Making the two
    texts differ would invite something downstream to tell them apart by reading it.
    """

    def observe(result: ActionResult) -> ActionResult:
        try:
            return client.appointment(result.confirmation_id)
        except AppointmentNotFound as absent:
            raise ObservationAbsent(
                f"the clinic could not confirm {result.confirmation_id}: {absent}"
            ) from absent
        except (ClinicRefused, ClinicUnavailable) as failure:
            raise ObservationUnavailable(
                f"the clinic could not confirm {result.confirmation_id}: {failure}"
            ) from failure

    return observe


def clinic_provider(client: ClinicClient) -> ExternalProvider:
    """The clinic, as one provider: a real booking and the read that observes it."""
    return ExternalProvider(
        name="clinic",
        handlers={CLINIC_ACTION: client.book},
        readbacks={CLINIC_ACTION: clinic_readback(client)},
    )


__all__ = [
    "CLINIC_ACTION",
    "ConflictingProviders",
    "ExternalProvider",
    "clinic_provider",
    "clinic_readback",
    "merged_handlers",
    "merged_readbacks",
]
