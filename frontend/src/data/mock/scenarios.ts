import type { ScenarioId } from "@/types/domain";

export interface ScenarioMeta {
  id: ScenarioId;
  num: string;
  title: string;
  blurb: string;
  /** The card's action line. One per card, and they must differ: four identical "Run scenario →"
   *  labels made the launcher read as a feature catalog, when the ordering is a guided story —
   *  work, then the boundary, then the human, then the uncertainty. The wording states what the
   *  judge is choosing to do, never what verdict they will watch — the same rule the blurbs keep. */
  cta: string;
  /** Marks the recommended first card. At most one card carries it, asserted in the test beside
   *  this file — two "start here" labels are a contradiction, and zero is the catalog again. */
  startHere?: boolean;
  /** CSS variable name used for the scenario's numeral and CTA. */
  toneVar: string;
  /** A second, quieter scenario the same card can start.
   *
   *  One card, two questions — used by 02 and by nothing else. The deterministic boundary test and
   *  the realistic poisoned document are different experiments over the same document, and the
   *  honest thing is to make the difference readable where a judge is already looking rather than
   *  to spend a fifth card on it or to leave the realistic one reachable only by `curl`.
   *
   *  Optional, and absent is the ordinary case: a card without one renders exactly as before. */
  alternate?: {
    id: ScenarioId;
    /** The link text. An action, so it reads as a second button and not as a footnote. */
    label: string;
    /** One line saying what the other run asks instead. Never a promise about its outcome. */
    note: string;
  };
}

/** Launcher copy. Locked — do not rewrite.
 *
 *  Three exceptions, each approved: scenario 03 was `mismatch` and is now `resume`, because the
 *  backend ships no mismatch trigger (see `ScenarioId` in `types/domain.ts` for where that claim
 *  went); scenario 01's blurb said "one decision requested", which the referral run does not do —
 *  it books and verifies with nobody asked, proven by the recovery e2e driving the same inbound
 *  event to COMPLETED with nothing clicked — so the promise came out; and the per-card `cta` and
 *  the single `startHere` mark were added so the launcher reads as a guided sequence. */
export const SCENARIOS: ScenarioMeta[] = [
  {
    id: "referral",
    num: "01",
    title: "Schedule a cardiology referral",
    blurb:
      "A referral arrives. Paperwork read, appointment booked, then checked against the clinic's own record.",
    cta: "See it work →",
    startHere: true,
    toneVar: "var(--brand-accent)"
  },
  /* 02 asks the question the launcher can actually answer, and offers the other one beside it.

     Its blurb used to end "Watch the unsafe branch end while the real work continues", which was a
     promise the runtime does not make. The realistic scenario hands a poisoned notice to a real
     planner that is free to ignore it — measured against the deployed host, three runs in six
     carried any refusal at all, and the other three simply booked the appointment. Telling a judge
     to watch a refusal there is telling them to watch something that happens half the time, and
     tuning the model into taking bait to fix that would be rigging the demo.

     So the primary run is the deterministic one, and its blurb says outright that CareLoop supplies
     the proposal. That is not a weaker claim than the old one — it is a different and stronger
     question. The realistic scenario does not vanish; it is the alternate below, described by what
     it actually asks. `tests/test_adversarial_copy.py` fails if either promise comes back. */
  {
    id: "adversarial",
    num: "02",
    title: "Malicious document",
    blurb:
      "A document tries to instruct the agent. For this test CareLoop deliberately proposes the action the attacker asked for — so you can see whether that proposal has any authority to act.",
    cta: "Test the boundary →",
    toneVar: "var(--state-untrusted)",
    alternate: {
      id: "injection",
      label: "Run the realistic version instead →",
      note: "The model may simply ignore the attack — and that is the system working."
    }
  },
  {
    id: "resume",
    num: "03",
    title: "Pause for a decision",
    blurb: "CareLoop reaches a step it has no authority for, stops, and finishes the moment you decide.",
    cta: "Review a decision →",
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
    cta: "Watch it recover →",
    toneVar: "var(--state-controlled)"
  }
];
