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

/** What each stage actually is, in the stack's own names.
 *
 *  Every claim here is verified against the codebase rather than aspirational: two LLM agents and
 *  five deterministic `MultiAgentBase` nodes on a Strands `GraphBuilder` graph; the gate cancels
 *  tools through `BeforeToolCallEvent.cancel_tool` with the `@gated_tool` decorator as the second,
 *  independent enforcement point; the models are Amazon Nova on Bedrock and hold no authority. */
export const STAGE_PRIMITIVES: Record<(typeof AGENT_STAGES)[number], string> = {
  Triage: "LLM · Amazon Nova via Bedrock",
  Evidence: "deterministic node · MultiAgentBase",
  Planner: "LLM · Amazon Nova via Bedrock",
  Gate: "BeforeToolCallEvent → cancel_tool",
  Execute: "@gated_tool backstop · idempotency key",
  Verify: "independent GET of the clinic's record",
  Settled: "Strands graph settled"
};

export interface StackRow {
  k: string;
  v: string;
}

/** The Strands and AWS primitives the drawer names, for a judge who knows the platform. */
export const STACK: StackRow[] = [
  { k: "GRAPH", v: "Strands GraphBuilder — 2 LLM agents, 5 deterministic MultiAgentBase nodes" },
  { k: "ENFORCEMENT", v: "BeforeToolCall hook (cancel_tool) + @gated_tool decorator — two independent points" },
  { k: "RULES", v: "rules.yaml — declarative; the engine answers ALLOW / DENY / REQUIRE_HUMAN" },
  { k: "MODELS", v: "Amazon Nova via Bedrock. The models propose; they hold no authority" },
  { k: "BOUNDS", v: "set_max_node_executions + timeouts — loop bounds from the SDK, never a counter" }
];

/** The measured adversarial results, verbatim from `docs/security-evaluation.md`.
 *
 *  Constants rather than a fetch, because the measurement is a property of this repository at
 *  this commit — the corpus, the rules and the replay harness are all versioned beside this file.
 *  `src/data/trust.test.ts` parses the document and fails if these drift from it, which is what
 *  makes a hand-carried number honest: it cannot quietly outlive its source.
 *
 *  The *pair* is the evidence. A zero on its own could be a polite model; the same 320 attempts
 *  producing 320 unauthorized actions with only the authority layer removed is what shows the
 *  zero belongs to the policy engine. */
export const SECURITY_EVIDENCE = {
  attempts: 320,
  shippedUnauthorized: 0,
  withoutAuthorityUnauthorized: 320,
  reproduceCommand: "python -m evals.replay",
  source: "docs/security-evaluation.md"
} as const;
