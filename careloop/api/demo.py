"""The five scenarios the demo control API can trigger, and nothing else.

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

**``injection`` and ``injection_only`` are both here, and the pair is deliberate.** Session E
measured the mixed ``injection`` scenario on the deployed host at **3 runs in 6 carrying any
``POLICY_DENY`` at all**: the poisoned notice arrives beside real work, and a planner that ignores
the poison and books the appointment is the system working rather than the demo failing. So the
refusal is genuine when it happens and absent when it does not, and neither is a defect.

``injection_only`` carries the same poisoned document with *no* legitimate work beside it, which
is where a refusal is reachable without any model being made to misbehave — `bench/live.py`
measured 5 denials in 5 runs and its ``require_denial`` flag says why in the same words. It is
here so the beat can be *shown* without the mixed scenario being tuned into always producing it;
prompt-tuning a planner into taking bait, or building a fixture with no legitimate alternative and
calling it the normal case, would both be rigging the demo. The model is still free to propose
nothing effectful.

**``recovery`` is the one scenario that arms anything, and it arms one thing.** It delivers the
ordinary referral to a case of its own and lets the clinic's answer to that case's booking be
discarded once, after the clinic has said it created the appointment — so the case reaches
``WAITING_EXTERNAL`` genuinely rather than by being put there, and the shipped saga, the shipped
five-second wake and the shipped read-back settle it with nothing else pressed. Everything about the
narrowing lives in :mod:`careloop.api.faults`; what lives here is the declaration that this scenario
is the one it applies to. It exists because the property is otherwise unreachable from a browser:
``e2e/tests/unreachable-states.spec.ts`` has to mock the post-effect states, and mocking a state the
backend can produce for real is the thing this repository avoids everywhere else.

The demo card for it says plainly that CareLoop throws the answer away. The ambiguity that
follows is genuine — nothing above the transport received an answer — but implying a spontaneous
outage would be claiming something that did not happen.

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

    loses_the_booking_response: bool = False
    """Whether pressing this arms one scoped loss of the clinic's answer to this case's booking.

    **Data rather than a branch**, so the one place that arms a fault reads a scenario's own
    declaration instead of comparing a key to a literal — the difference between a table somebody
    extends and an ``if`` somebody copies. ``False`` for every scenario that is not about ambiguous
    outcomes, which is the safe default and the one three of the five carry.

    What it licenses is narrow and is enforced elsewhere: see
    :class:`careloop.api.faults.BookingResponseLoss` for the case binding, the one-shot behaviour
    and the requirement that the provider have answered ``201`` before anything is discarded.
    """


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
            DemoScenario(
                key="recovery",
                case_id="demo-recovery",
                event_id="evt-0001-referral",
                title="The clinic books the appointment and CareLoop is not told; it finds out",
                loses_the_booking_response=True,
            ),
            DemoScenario(
                key="injection_only",
                case_id="demo-injection-only",
                event_id="evt-0005-continuity-notice-only",
                title="The poisoned notice with no legitimate work to do instead",
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
