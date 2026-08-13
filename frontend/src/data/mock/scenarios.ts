import type { ScenarioId } from "@/types/domain";

export interface ScenarioMeta {
  id: ScenarioId;
  num: string;
  title: string;
  blurb: string;
  /** CSS variable name used for the scenario's numeral and CTA. */
  toneVar: string;
}

/** Launcher copy. Locked — do not rewrite. */
export const SCENARIOS: ScenarioMeta[] = [
  {
    id: "referral",
    num: "01",
    title: "Schedule a cardiology referral",
    blurb:
      "Paperwork handled, one decision requested, appointment booked, then checked against the clinic's own record.",
    toneVar: "var(--brand-accent)"
  },
  {
    id: "injection",
    num: "02",
    title: "Malicious document",
    blurb: "A document tries to instruct the agent. Watch the unsafe branch end while the real work continues.",
    toneVar: "var(--state-untrusted)"
  },
  {
    id: "mismatch",
    num: "03",
    title: "Verification mismatch",
    blurb: "The clinic's record disagrees with what CareLoop authorized. The case does not close.",
    toneVar: "var(--state-denied)"
  }
];
