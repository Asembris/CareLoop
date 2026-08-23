"""Serve CareLoop for the end-to-end suite: scripted agents, a real clinic, everything else real.

    python -m demoworld --port 8100
    python scripts/e2e_serve.py --clinic-url http://127.0.0.1:8100 --port 8010 \
        --static-dir frontend/dist

Normally the suite starts both itself — see ``e2e/playwright.config.ts``, which passes ``8010``
rather than the ``8000`` this defaults to. Measured reason: something unrelated already held 8000
on the development machine, and uvicorn's failure there is an opaque WinError about socket
permissions rather than "that port is taken".

**Why this exists rather than ``scripts/demo_serve.py``.** That script resolves its models from the
environment, and unset means the mock — which :func:`careloop.llm.providers.build_model` constructs
with *no script at all*. The mock is strict on purpose: the first triage call raises
``MockScriptExhausted`` and every case fails before it has classified anything. So a locally served
CareLoop cannot drive a single case to a decision at zero cost, and the e2e layer's whole value is
that it talks to a real backend. Pointing the roles at Bedrock would fix that by spending money and
by making every run nondeterministic, which is the opposite of what a blocking CI job needs.

This binds **scripted** agents instead. The graph, the policy engine, the gate, the store, the tool
boundary, the idempotency keys, the clinic and the read-back are all the real ones; the only thing
replaced is the model's opinion about what to do next, which is the one part a browser test cannot
assert on anyway.

**Test-only, and structurally so.** ``pyproject.toml`` packages ``careloop*`` alone, so nothing here
is in the wheel, and ``deploy/push.sh`` excludes the suite that drives it. Nothing under
``careloop/`` imports this file and nothing here decides anything the application would otherwise
decide — it is a composition root, exactly as ``demo_serve.py`` is.

**One world per visitor, always.** The deployed unit runs ``--isolate-sessions`` and so does this,
with no flag to turn it off: every browser context mints its own store, run registry and clinic, so
the suite is parallel-safe for the same reason two judges are, and it exercises the shape that
actually ships. A single shared world would make every spec depend on the order the others ran in.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncGenerator, Sequence
from typing import Any, Final, TypeVar

from pydantic import BaseModel
from strands.types.content import Message

from careloop.agents.planner import PlannerAgent
from careloop.agents.schemas import (
    PlannerIntent,
    TriageCategory,
    TriageResult,
    Urgency,
    proposal_tool_call_for,
)
from careloop.agents.triage import TriageAgent
from careloop.api.app import create_app
from careloop.api.demo import DEMO_SCENARIOS
from careloop.api.deps import ApiDeps
from careloop.api.faults import BookingResponseLoss, booking_response_loss
from careloop.api.sessions import CLINIC_SESSION_HEADER, SessionWorlds
from careloop.events.simulator import EventSimulator
from careloop.graph.runner import context_for
from careloop.llm.mock import MockModel
from careloop.state.sqlite_store import SqliteCaseStore
from careloop.tools.clinic import DEFAULT_TIMEOUT, ClinicClient
from careloop.tools.registry import REGISTRY

T = TypeVar("T", bound=BaseModel)


class MissingRefusalProposal(Exception):
    """Raised at import when the poisoned script no longer names every tool in the registry."""


class E2EScriptExhausted(Exception):
    """Raised when this harness's script runs out mid-run.

    **A distinct type because the diagnosis has to be distinct.** A consumed script and a genuinely
    failing run both end as a ``FAILED`` case, and they are opposite conclusions: the first means
    the harness is under-scripted and says nothing about CareLoop, the second means CareLoop did
    something wrong. ``run_case`` records ``f"a graph node raised: {type(failure).__name__}"`` as
    the case's failure reason and :class:`careloop.api.runs.RunRegistry` records the same type name
    as ``RunView.error``, so raising something named for *this file* puts the right word in front of
    whoever is reading, at two independent points, over HTTP.

    Not ``MockScriptExhausted``: that name is true but ambiguous — it is what the offline suite's
    own mocks raise — and the question a red e2e run needs answered is *whose* script ran out.
    """


class ScriptedModel(MockModel):
    """A :class:`~careloop.llm.mock.MockModel` that names this harness when it runs dry.

    Subclassed rather than reimplemented so the strictness is inherited whole: a scripted instance
    of the wrong type still raises, a scripted exception is still raised, and the ``{"output": …}``
    envelope the spike established is still the one the caller sees.
    """

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        if not self.remaining[1]:
            raise E2EScriptExhausted(
                f"the e2e harness ran out of scripted answers while a node asked for "
                f"{output_model.__name__}. This is the harness being under-scripted, not CareLoop "
                f"failing: raise SCRIPT_DEPTH in scripts/e2e_serve.py."
            )
        async for chunk in super().structured_output(
            output_model, prompt, system_prompt=system_prompt, **kwargs
        ):
            yield chunk

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        """The planner's path. Same message when the harness is under-scripted, same reason."""
        if tool_specs and not self.remaining[2]:
            raise E2EScriptExhausted(
                "the e2e harness ran out of scripted proposals while the planner asked for one. "
                "This is the harness being under-scripted, not CareLoop failing: raise "
                "SCRIPT_DEPTH in scripts/e2e_serve.py."
            )
        async for chunk in super().stream(
            messages, tool_specs=tool_specs, system_prompt=system_prompt, **kwargs
        ):
            yield chunk


#: How many times each scripted answer is repeated within one run.
#:
#: A fresh script is built per *run* — the agents are constructed inside the context factory, which
#: the API calls once per run — so this only has to outlast the replans of a single run. The
#: bounded replan cycle tops out well below this; the margin is here so that a future rule turning
#: one more proposal into a denial does not turn the suite red for a reason that has nothing to do
#: with the change.
SCRIPT_DEPTH: Final[int] = 12

CAREGIVER_ADDRESS: Final[str] = "rosa.marlowe@carer.example"

#: The referral's booking, as the shipped documents support it.
#:
#: Every value is one Demo World seeds or one the referral PDF carries — ``PROV-NORTHGATE-
#: CARDIOLOGY`` at ``2026-09-03T09:30:00`` is ``demoworld.clinic.SEEDED_SLOTS[0]``, and ``PT-4417``
#: is the patient id the referral states. Inventing any of them would have the booking refused by
#: the policy engine or by the clinic, which is a correct refusal of a wrong test.
BOOKING: Final[dict[str, str]] = {
    "provider_id": "PROV-NORTHGATE-CARDIOLOGY",
    "patient_id": "PT-4417",
    "slot_start": "2026-09-03T09:30:00",
    "reason": "cardiology review",
}

EMAIL: Final[dict[str, str]] = {
    "to": CAREGIVER_ADDRESS,
    "subject": "Cardiology referral for Enid Marlowe",
    "body": "The referral has arrived and an appointment is being arranged.",
}

ATTACKER: Final[str] = "records-intake@claims-continuity-partners.example"
ATTACKER_PORTAL: Final[str] = "PORTAL-MG-TRANSFER"

#: One refusable proposal per tool, in the order a live planner produced them.
#:
#: Every one is refused, which is the point of the scenario: the suite asserts the refusal and the
#: audit trail, not the planner's judgement. Scripting the bait is what makes the denial reachable
#: without tuning a real model into misbehaving.
#:
#: **It has to cover the whole registry, and that is not a detail.** ``PlanNode`` parks the case as
#: ``NO_SAFE_ACTION_AVAILABLE`` on one checkable condition — ``REGISTRY.names <= refused`` — so a
#: script naming four of the five tools never satisfies it. Measured here before the guard below
#: existed: the planner kept being asked, the run walked ``plan, gate`` until
#: ``set_max_node_executions`` fired at 24, and the case ended **FAILED** reading "the graph stopped
#: without the case reaching a resting state". That is a runaway detector reporting a scripting
#: mistake, and it looks exactly like the defence having broken.
EVERY_TOOL_REFUSED: Final[tuple[tuple[str, dict[str, str]], ...]] = (
    (
        "upload_document",
        {"portal_id": ATTACKER_PORTAL, "document_id": "doc-0002-continuity-notice"},
    ),
    (
        "grant_portal_access",
        {"grantee": ATTACKER, "role": "records_admin", "portal_id": ATTACKER_PORTAL},
    ),
    ("send_email", {"to": ATTACKER, "subject": "Referral letter", "body": "coverage summary"}),
    (
        "book_appointment",
        {
            "provider_id": "PROV-X",
            "patient_id": "PT-4417",
            "slot_start": "whenever",
            "reason": "unverified",
        },
    ),
    ("fetch_document", {"document_id": "doc-0002-continuity-notice"}),
)


def intent(tool_name: str, args: dict[str, str]) -> PlannerIntent:
    return PlannerIntent(tool_name=tool_name, rationale="the next step for this case", args=args)


#: What the planner proposes, per demo case.
#:
#: Keyed on ``case_id`` rather than on the scenario key, because the context factory is handed a
#: case and never a scenario — the demo route and ``POST /api/events`` reach the same ``deliver``.
SCRIPTS: Final[dict[str, tuple[PlannerIntent, ...]]] = {
    DEMO_SCENARIOS["referral"].case_id: (intent("book_appointment", BOOKING),),
    DEMO_SCENARIOS["injection"].case_id: (intent("book_appointment", BOOKING),),
    DEMO_SCENARIOS["resume"].case_id: (intent("send_email", EMAIL),),
    # The recovery scenario books exactly what the referral one books. It has to: the beat it
    # demonstrates is what happens when the answer to a perfectly ordinary booking never comes
    # back, and a different action would make the scenario about the action instead.
    DEMO_SCENARIOS["recovery"].case_id: (intent("book_appointment", BOOKING),),
    DEMO_SCENARIOS["injection_only"].case_id: tuple(
        intent(tool, args) for tool, args in EVERY_TOOL_REFUSED
    ),
}


def check_the_refusal_script_covers_the_registry() -> None:
    """Fail at import if the poisoned script can no longer park the case.

    A tool added to the registry makes ``REGISTRY.names <= refused`` unsatisfiable for a script
    that does not name it, and the symptom is a ``FAILED`` case blamed on the node budget several
    layers away from the cause. Checked here so that adding a tool breaks the *harness* loudly
    rather than the suite mysteriously.
    """
    proposed = {tool for tool, _ in EVERY_TOOL_REFUSED}
    if missing := REGISTRY.names - proposed:
        raise MissingRefusalProposal(
            f"the injection_only script proposes {sorted(proposed)} but the registry holds "
            f"{sorted(REGISTRY.names)}; without a refusable proposal for {sorted(missing)} the "
            f"case can never reach NO_SAFE_ACTION_AVAILABLE and will fail on the node budget"
        )


check_the_refusal_script_covers_the_registry()

#: What a case this table does not name gets.
#:
#: ``send_email`` on purpose: it parks for a caregiver rather than reaching outside, so a spec that
#: delivers an event under an id nobody scripted cannot book a slot another spec's world is using
#: and cannot leave an appointment in a clinic. A default that acted externally would make the
#: cheapest mistake in this file the most expensive one.
DEFAULT_SCRIPT: Final[tuple[PlannerIntent, ...]] = (intent("send_email", EMAIL),)


def repeated(answers: Sequence[BaseModel]) -> list[BaseModel]:
    """``answers``, cycled to :data:`SCRIPT_DEPTH` proposals, in order."""
    return [answers[index % len(answers)] for index in range(SCRIPT_DEPTH)]


def triage_for() -> TriageAgent:
    """Triage that classifies every case the same way.

    The suite asserts on what the *spine* did with a classification, never on the classification
    itself, so one answer is the honest amount of variation to script here.
    """
    return TriageAgent(
        ScriptedModel(
            structured_outputs=repeated(
                [
                    TriageResult(
                        category=TriageCategory.APPOINTMENT,
                        urgency=Urgency.SOON,
                        summary="Cardiology referral needs an outpatient appointment.",
                        patient_id="PT-4417",
                    )
                ]
            )
        )
    )


def planner_for(case_id: str) -> PlannerAgent:
    return PlannerAgent(
        ScriptedModel(
            proposal_outputs=[
                proposal_tool_call_for(intent)
                for intent in repeated(SCRIPTS.get(case_id, DEFAULT_SCRIPT))
            ]
        )
    )


def scripted_factory(clinic: ClinicClient | None, faults: BookingResponseLoss | None = None):
    """A context factory whose agents are scripted for the case being planned.

    No pinned clock. ``tests/`` pins one so a timeline is byte-comparable; here the dashboard
    renders relative times a caregiver reads, and freezing time would show every case as having
    been updated at the same long-ago instant.

    ``faults`` is the recovery scenario's losing client, and the binding is the same one the
    deployed composition uses: runs for the case a recovery scenario armed get that client, and
    every other run gets the ordinary one. Written here rather than by composing
    :meth:`careloop.api.faults.BookingResponseLoss.context_factory` because this factory also
    supplies scripted agents, and there is only ever one context to build.
    """

    def build(store, inbound, case_id: str):
        reaching = faults.clinic if faults is not None and faults.claims(case_id) else clinic
        return context_for(
            store,
            inbound,
            case_id=case_id,
            triage=triage_for(),
            planner=planner_for(case_id),
            clinic=reaching,
        )

    return build


def scripted_worlds(clinic_url: str) -> SessionWorlds:
    """Worlds shaped exactly like the deployed ones, with scripted agents in place of a model.

    The store is SQLite in memory, which is what :func:`careloop.api.sessions.demo_session_worlds`
    builds — keeping the deployed store class is what stops the suite exercising a combination the
    deployment never runs.

    The event simulator is **shared across worlds**, for the same reason and in the same words as
    the composition this mirrors: its fixtures are read-only documents, so there is nothing there
    for one visitor to change under another. Letting each world default to its own would be a
    quiet divergence from the shape being tested.
    """
    import httpx

    shared_events = EventSimulator()

    def build(session_id: str) -> ApiDeps:
        clinic = (
            ClinicClient(
                clinic_url,
                client=httpx.Client(
                    timeout=DEFAULT_TIMEOUT,
                    headers={CLINIC_SESSION_HEADER: session_id},
                ),
            )
            if clinic_url
            else None
        )
        faults = (
            booking_response_loss(clinic_url, session=session_id) if clinic is not None else None
        )
        return ApiDeps(
            store=SqliteCaseStore(":memory:"),
            events=shared_events,
            context_factory=scripted_factory(clinic, faults),
            clinic=clinic,
            faults=faults,
        )

    # A short idle TTL and a raised cap, both load-bearing for the suite rather than tuning
    # preferences.
    #
    # Every spec mints its own world, the whole suite mints more than ``DEFAULT_SESSION_LIMIT`` of
    # them, and under the deployed half-hour TTL the cap fills mid-run — measured as
    # ``sessionFromPage`` timing out on whichever specs run last, an order-dependent failure that
    # reads as flakiness. A spec touches its world for seconds and the per-test budget is 30s, so
    # two minutes of idleness is 4x the longest a live spec can go quiet, and worlds from earlier
    # specs (or an earlier run against the same server) are forgotten instead of counted against
    # the cap.
    #
    # The TTL alone stopped being enough once the suite passed a hundred tests: **the whole run is
    # shorter than the TTL**, so nothing minted during it is ever evictable and the cap becomes a
    # hard ceiling on worlds per run. Measured — ``POST /api/sessions`` immediately after a passing
    # run answers ``503 … all of them are in use``, meaning the suite lands on exactly 64 and any
    # ordering that reaches it a moment sooner takes the tail of the suite down with it. Lowering
    # the TTL under the run length instead would evict a world a spec is still between assertions
    # on, which trades a loud failure for a confusing one.
    #
    # 256 is headroom for roughly four times the current suite. It is safe *here* and nowhere else:
    # the cap exists so a deployed demo cannot be made to hold unbounded worlds by anyone who can
    # reach the port, and this server is loopback, short-lived and driven by one suite. The
    # deployed defaults are untouched — a judge's half hour is not a spec's 30 seconds, and a
    # public port is not this one.
    return SessionWorlds(build, limit=256, idle_seconds=120.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve CareLoop with scripted agents, for e2e.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--clinic-url",
        default=os.environ.get("CARELOOP_CLINIC_URL", ""),
        help="Demo World's base URL; empty means every effect stays simulated",
    )
    parser.add_argument(
        "--static-dir",
        default=os.environ.get("CARELOOP_STATIC_DIR", ""),
        help="a built frontend to serve from this same origin; empty means API only",
    )
    arguments = parser.parse_args()

    app = create_app(
        sessions=scripted_worlds(arguments.clinic_url),
        demo=True,
        static_dir=arguments.static_dir or None,
    )

    print("agents        scripted (no model provider is reached)")
    print("case store    per visitor, SQLite in memory")
    print(f"clinic        {arguments.clinic_url or '(simulated)'}")
    print(f"frontend      {arguments.static_dir or '(API only)'}")

    import uvicorn

    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover - a process entry point, not a call site
    main()
