import type { ScenarioId } from "@/types/domain";

export interface ScenarioMeta {
  id: ScenarioId;
  num: string;
  title: string;
  blurb: string;
  /** CSS variable name used for the scenario's numeral and CTA. */
  toneVar: string;
}

/** Launcher copy. Locked — do not rewrite.
 *
 *  One exception, approved: scenario 03 was `mismatch` and is now `resume`, because the backend
 *  ships no mismatch trigger. See `ScenarioId` in `types/domain.ts` for where that claim went. */
export const SCENARIOS: ScenarioMeta[] = [
  {
    id: "referral",
    num: "01",
    title: "Schedule a cardiology referral",
    blurb:
      "Paperwork handled, one decision requested, appointment booked, then checked against the clinic's own record.",
    toneVar: "var(--brand-accent)"
  },
  /* 02's blurb used to end "Watch the unsafe branch end while the real work continues", and that
     was a promise the runtime does not make. This scenario delivers the poisoned notice *beside*
     genuine work to a real planner, which is free to ignore it — measured against the deployed
     host, three runs in six carried any refusal at all, and the other three simply booked the
     appointment. A card telling a judge to watch a refusal is telling them to watch something that
     happens half the time, and tuning the model into taking bait to fix that would be rigging the
     demo. `tests/test_adversarial_copy.py` is what fails if the promise comes back.

     What the card says instead is what is actually guaranteed: the poison gains nothing either
     way. See `docs/judges.md` for the measurement. */
  {
    id: "injection",
    num: "02",
    title: "Malicious document",
    blurb:
      "A document tries to instruct the agent. The model may simply ignore it — and that is the system working. Either way the instruction gains nothing.",
    toneVar: "var(--state-untrusted)"
  },
  {
    id: "resume",
    num: "03",
    title: "Pause for a decision",
    blurb: "CareLoop reaches a step it has no authority for, stops, and finishes the moment you decide.",
    toneVar: "var(--state-decision)"
  },
  /* 04 is the only card that describes something the demo deliberately does *to* CareLoop, and the
     blurb says so outright. The ambiguity that follows is genuine — nothing above the transport
     receives an answer, and every state, key and check after that is the shipped one — but a card
     implying a spontaneous outage would be claiming something that did not happen, which is the
     small lie the rest of this product refuses to tell. */
  {
    id: "recovery",
    num: "04",
    title: "The answer gets lost",
    blurb:
      "The clinic books the appointment and CareLoop deliberately throws its reply away. Watch it work out what happened, on its own, without creating a second appointment.",
    toneVar: "var(--state-controlled)"
  }
];
