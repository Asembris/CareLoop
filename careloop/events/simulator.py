"""Delivering events, in the order a scenario says.

The simulator stands in for whatever would really watch a caregiver's inbox. It holds no state
about cases and makes no decisions: it answers "what arrived, and in what order", and the runner
does the rest.

**A scenario is an ordered tuple of event ids, and that order is the demo.** The resume scenario is
two events separated by weeks — a referral, then a claim rejection — and what makes it interesting
is not that both exist but that the second arrives at a case the first one finished. Naming the
sequences here means the demo script and T5 drive the same thing rather than two hand-built lists
that can drift apart.

**Nothing here decides which case an event belongs to.** That is
:func:`careloop.graph.runner.reopen_case`'s question, and it is answered from the ``CaseStore``.
An event that could nominate its own case would be untrusted input choosing what to reopen.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

from careloop.events.inbound import (
    CLAIM_REJECTION_EVENT_ID,
    EVENTS,
    POISONED_EVENT_ID,
    REFERRAL_EVENT_ID,
    InboundEvent,
)


class UnknownScenario(Exception):
    """Raised when a scenario name is not one of :data:`SCENARIOS`."""


#: Named sequences of events, in delivery order.
SCENARIOS: Final[Mapping[str, tuple[str, ...]]] = {
    "happy": (REFERRAL_EVENT_ID,),
    "injection": (POISONED_EVENT_ID,),
    "resume": (REFERRAL_EVENT_ID, CLAIM_REJECTION_EVENT_ID),
}


class EventSimulator:
    """Replays a scenario's events in order."""

    def __init__(self, events: Mapping[str, InboundEvent] | None = None) -> None:
        self._events = dict(events if events is not None else EVENTS)

    @property
    def event_ids(self) -> frozenset[str]:
        """Every event the simulator can deliver."""
        return frozenset(self._events)

    def get(self, event_id: str) -> InboundEvent:
        """One event by id.

        Raises:
            KeyError: if there is no such event.
        """
        return self._events[event_id]

    def scenario(self, name: str) -> tuple[InboundEvent, ...]:
        """Every event of ``name``, in delivery order.

        Raises:
            UnknownScenario: if the name is not declared, listing the ones that are. A typo
                returning an empty sequence would produce a demo that runs and does nothing.
        """
        if name not in SCENARIOS:
            raise UnknownScenario(f"no scenario {name!r}; known: {', '.join(sorted(SCENARIOS))}")
        return tuple(self._events[event_id] for event_id in SCENARIOS[name])

    def deliver(self, name: str) -> Iterator[InboundEvent]:
        """Iterate a scenario's events in order."""
        yield from self.scenario(name)


def check_scenarios_name_real_events(events: Mapping[str, InboundEvent] | None = None) -> None:
    """Fail at import if a scenario names an event that does not exist.

    Raises:
        UnknownScenario: naming every scenario and event involved.
    """
    known = events if events is not None else EVENTS
    missing = sorted(
        f"{name} -> {event_id}"
        for name, event_ids in SCENARIOS.items()
        for event_id in event_ids
        if event_id not in known
    )
    if missing:
        raise UnknownScenario("scenario(s) name unknown events: " + "; ".join(missing))


check_scenarios_name_real_events()


__all__ = [
    "SCENARIOS",
    "EventSimulator",
    "UnknownScenario",
    "check_scenarios_name_real_events",
]
