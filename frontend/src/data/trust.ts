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

/* `GUARANTEES` was here, and it is gone rather than moved.
 *
 * Five hand-written claims about CareLoop's authority — the deterministic gate, approval bound to
 * exact arguments, durable idempotency, provenance on every fact, and completion requiring an
 * independent read-back. Each was maintained by nobody and checkable by nothing, which is the
 * second-source-of-truth problem `My Boundaries` was built to remove: they sat on the page whatever
 * `rules.yaml` said underneath them.
 *
 * All five now render from `GET /api/boundaries` or from a case's own trail — and **one of them was
 * already false**. "Completion requires an independent external read-back" is true of
 * `book_appointment` and `upload_document` and untrue of the other two, which are simulated by
 * design; the derived copy says which is which per capability, because "verified" is a materially
 * stronger claim when something outside CareLoop agreed.
 *
 * `AUTHORITY` below is deliberately kept. It describes the *architecture* — who proposes, who
 * authorizes, who approves, who confirms — rather than what this caregiver may do, so it is
 * explanatory rather than a claim about limits, and it is read by the Under the hood drawer. */

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
 *  zero belongs to the policy engine.
 *
 *  **The denominator is a cross product, and it has to say so.** 320 is not 320 separate attacks:
 *  it is every payload in the corpus replayed down every delivery route and against every tool
 *  that can reach outside the process, which is what makes the result a ceiling rather than a
 *  sample. Read as a headcount it overstates the corpus eightfold, so the three factors are
 *  carried here — pinned to the same document by the same test — and rendered beside the numerals
 *  rather than left for a reader to assume. */
export const SECURITY_EVIDENCE = {
  attempts: 320,
  payloads: 40,
  deliveryRoutes: 2,
  effectTools: 4,
  shippedUnauthorized: 0,
  withoutAuthorityUnauthorized: 320,
  reproduceCommand: "python -m evals.replay",
  source: "docs/security-evaluation.md"
} as const;
