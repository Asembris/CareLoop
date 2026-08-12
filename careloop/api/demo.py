"""The three scenarios the demo control API can trigger, and nothing else.

Data, not behaviour. The routes live in :mod:`careloop.api.app` beside the ones they reuse, because
the whole claim of the demo API is that a button runs **the normal application path** — the same
``deliver`` a browser's ``POST /api/events`` reaches, the same graph, the same gate, the same store.
A demo endpoint that did something the real flow does not is a demo that lies, and a judge poking at
it would find out.

**Not :data:`careloop.events.simulator.SCENARIOS`, and the difference is what each is for.** That
table names *delivery sequences* — ``resume`` there is a referral followed weeks later by a claim
rejection, which is what ``scripts/resume_demo.py`` and T5 drive across three processes. These are
*starting points*: one press, one inbound event, one run, and then the judge does the human part.
The resume beat a judge can actually press is a case that parks for a decision so the Approve
button has something to approve — a second event would need the first case finished, which is
exactly what has not happened yet when somebody is looking at the screen.

What is here is the mapping from a scenario name to the inbound event that produces it, plus the
load-time check that each named event exists. A scenario naming a fixture that has been renamed
should fail when the module is imported, not in front of an audience.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from careloop.events.simulator import EventSimulator


class DemoScenario(BaseModel):
    """One thing a judge can press."""

    model_config = ConfigDict(frozen=True)

    key: str
    case_id: str
    event_id: str
    title: str
    """What the scenario demonstrates. Shown by the control surface; never sent to a model."""


#: Fixed case ids, so a scenario pressed twice reopens the case it made rather than accumulating
#: cases nobody will look at — and so a reset has a knowable set of cases to have removed.
DEMO_SCENARIOS: Final[Mapping[str, DemoScenario]] = MappingProxyType(
    {
        scenario.key: scenario
        for scenario in (
            DemoScenario(
                key="referral",
                case_id="demo-referral",
                event_id="evt-0001-referral",
                title="A referral arrives; the appointment is booked and verified externally",
            ),
            DemoScenario(
                key="injection",
                case_id="demo-injection",
                event_id="evt-0002-continuity-notice",
                title="A poisoned continuity notice arrives beside real work and gains nothing",
            ),
            DemoScenario(
                key="resume",
                case_id="demo-resume",
                event_id="evt-0004-appointment-instructions",
                title="A case parks for a caregiver decision and resumes when it is made",
            ),
        )
    }
)


class UnknownDemoScenario(Exception):
    """Raised when a scenario key is not one of :data:`DEMO_SCENARIOS`."""


class UnknownDemoEvent(Exception):
    """Raised at import when a scenario names an inbound event that does not exist."""


def check_demo_scenarios_name_real_events(events: EventSimulator | None = None) -> None:
    """Fail loudly if a scenario points at a fixture that is not there.

    Takes its simulator as a parameter for the reason the projection checks take their tables: a
    guard that can only run against the shipped fixtures is a guard whose failure path is untested.
    """
    simulator = events if events is not None else EventSimulator()
    missing = {
        scenario.key: scenario.event_id
        for scenario in DEMO_SCENARIOS.values()
        if scenario.event_id not in simulator.event_ids
    }
    if missing:
        raise UnknownDemoEvent(f"demo scenarios name events that do not exist: {missing}")


check_demo_scenarios_name_real_events()


__all__ = [
    "DEMO_SCENARIOS",
    "DemoScenario",
    "UnknownDemoEvent",
    "UnknownDemoScenario",
    "check_demo_scenarios_name_real_events",
]
