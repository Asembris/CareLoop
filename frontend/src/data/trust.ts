export interface AuthorityRow {
  who: string;
  verb: string;
  toneVar: string;
  note: string;
}

/** The architecture principle, stated as four rows. Locked copy. */
export const AUTHORITY: AuthorityRow[] = [
  {
    who: "Model",
    verb: "PROPOSES",
    toneVar: "var(--state-untrusted)",
    note: "Reads documents, drafts a plan. Holds no authority."
  },
  {
    who: "Policy",
    verb: "AUTHORIZES",
    toneVar: "var(--state-controlled)",
    note: "Deterministic code decides what may run."
  },
  {
    who: "Human",
    verb: "APPROVES",
    toneVar: "var(--state-decision)",
    note: "Only where the decision is genuinely theirs."
  },
  {
    who: "Verifier",
    verb: "CONFIRMS",
    toneVar: "var(--state-verified)",
    note: "Independent read-back from the external system."
  }
];

export interface Guarantee {
  text: string;
  code: string;
}

/** Only claims the backend actually supports belong here. */
export const GUARANTEES: Guarantee[] = [
  { text: "Deterministic policy gate — the model cannot widen its own permissions", code: "GATE" },
  { text: "Approval is bound to the exact arguments shown to the person", code: "ARG_BIND" },
  { text: "Durable idempotency — a retry cannot book twice", code: "IDEMPOTENT" },
  { text: "Every extracted fact carries its source document and page", code: "PROVENANCE" },
  { text: "Completion requires an independent external read-back", code: "VERIFY" }
];

export const AGENT_STAGES = ["Triage", "Evidence", "Planner", "Gate", "Execute", "Verify", "Settled"] as const;
