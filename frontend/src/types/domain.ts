/* CareLoop frontend domain contract.
   These types are the seam between the approved UI and the real backend.
   Where the backend's own schema differs, adapt the ADAPTER (services/), not the components. */

export type CaseSection = "needs" | "working" | "done";

/** Public status vocabulary. The UI groups by CaseSection; this is the exported equivalent. */
export type CaseStatus = "NEEDS_YOU" | "WORKING" | "COMPLETED";

export type RunStage =
  | "IDLE"
  | "TRIAGE"
  | "EVIDENCE"
  | "PLANNER"
  | "GATE"
  | "EXECUTE"
  | "VERIFY"
  | "SETTLED";

export type Authority = "MODEL" | "POLICY" | "HUMAN" | "VERIFIER";

export type PolicyVerdict = "PENDING" | "ALLOW" | "DENY" | "REQUIRE_HUMAN";

/** Mirrors the backend distinction the UI must respect:
 *  only REQUIRES_HUMAN_APPROVAL may render Approve / Reject. */
export type DecisionKind = "REQUIRES_HUMAN_APPROVAL" | "NO_SAFE_ACTION_AVAILABLE";

export type VerificationState = "PENDING" | "MATCH" | "MISMATCH";

export type EventTone = "neutral" | "decision" | "verified" | "denied" | "untrusted";

/** The three keys `careloop/api/demo.py` actually ships.
 *
 *  The third was `mismatch` in the design package and has no backend trigger: producing a real
 *  verification mismatch needs the appointment moved from outside CareLoop, which no demo
 *  endpoint can do without becoming a demo that does something the real flow does not. The claim
 *  is made instead by `tests/test_verify_readback.py::TestTheIndependenceProof`, which moves the
 *  appointment behind CareLoop's back and requires verification to fail. A test that proves it
 *  beats a button whose backend state contradicts its own screen. */
export type ScenarioId = "referral" | "injection" | "resume";

/** One step of a fact's provenance journey, rendered as a dot and a sentence in the lens. */
export interface ProvenanceStep {
  tone: "untrusted" | "controlled" | "trusted" | "muted";
  text: string;
}

export interface EvidenceExcerpt {
  fact: string;
  source: string;
  before: string;
  highlight: string;
  after: string;
  digest: string;
  /** This fact's actual journey. Optional because the mock predates it; the lens falls back to
   *  the generic tier legend when it is absent. */
  journey?: ProvenanceStep[];
}

export interface EvidenceItem {
  fact: string;
  source: string;
  /** Caregiver-facing trust word. Raw trust tiers stay out of this view. */
  trust: string;
  excerpt: EvidenceExcerpt;
}

export interface BlockedAction {
  text: string;
  rule?: string;
}

/** One page of a source document, exactly as extraction recorded it, with the values that were
 *  lifted into facts so the lens can show where each came from. */
export interface DocumentPage {
  page: number;
  text: string;
  highlights: string[];
}

/** A source document reassembled from its page facts. The text is quoted, untrusted data — the
 *  entire point of rendering it is that a reader can see the attack and see that it stayed inert. */
export interface CaseDocument {
  name: string;
  pages: DocumentPage[];
}

/** One action the policy engine refused on this case, in caregiver words plus the audit fields. */
export interface PolicyRefusal {
  text: string;
  reasonCode: string;
  ruleId: string;
}

export interface TimelineEvent {
  id: string;
  time: string;
  text: string;
  tone: EventTone;
  source?: EvidenceExcerpt;
  blocked?: BlockedAction[];
}

export interface DecisionFact {
  label: string;
  value: string;
}

export interface DecisionView {
  id: string;
  kind: DecisionKind;
  title: string;
  body: string;
  facts: DecisionFact[];
  /** What CareLoop will do once the person decides. */
  after: string;
  approveLabel: string;
  rejectLabel: string;
  /** Whether this card may offer "check the external system again".
   *
   *  Independent of `kind`, deliberately. `kind` says whether the card may offer *authority* —
   *  only `REQUIRES_HUMAN_APPROVAL` ever may — and this says whether it may offer *another look*,
   *  which is not authority at all. A card can have neither, and the manual-handoff card that
   *  gains a re-check button still renders no Approve and no Reject. */
  recheckable: boolean;
  /** The label for that affordance. Observation, never action — see `presentation.ts`. */
  recheckLabel: string;
}

export interface Readback {
  authorized: string;
  observed: string;
  result: string;
  state: VerificationState;
  /** A member of the backend's closed observation vocabulary — MATCHED / DIVERGED / ABSENT /
   *  UNAVAILABLE / NOT_OBSERVED — or absent on a case recorded before it existed. */
  observation?: string;
  /** The one-sentence reading of `observation`, from `presentation.OBSERVATION_COPY`. */
  observationText?: string;
}

export interface CareCase {
  id: string;
  title: string;
  section: CaseSection;
  /** Human-facing state line, e.g. "GATHERING DOCUMENTS". Never a raw enum. */
  statusLabel: string;
  statusText: string;
  headline: string;
  updatedLabel: string;
  verified?: boolean;
  mismatch?: boolean;
  runId?: string;
  outcome?: { title: string; body: string };
  readback?: Readback;
  decision?: DecisionView | null;
  events: TimelineEvent[];
  evidence: EvidenceItem[];
  /** Optional because the mock predates them; the real adapter always fills both. */
  documents?: CaseDocument[];
  refusals?: PolicyRefusal[];
}

export interface AgentRun {
  id: string;
  scenario: ScenarioId | null;
  stage: RunStage;
  /** Free-text stage line shown in the drawer, e.g. "run_01J8QK · gate → REQUIRE_HUMAN". */
  stageLabel: string;
  actionIntent: string;
  idempotencyKey: string;
  policyVerdict: PolicyVerdict;
  policyDetail: string;
  evidenceDigest: string;
  verification: VerificationState;
  verificationDetail: string;
}

/** What the adapter currently knows about its own connection to the backend.
 *
 *  `connecting` is only ever the state before the first read has answered; after that the page is
 *  `live` or `unreachable` forever. The distinction the UI must respect: a dashboard that cannot
 *  be read is not a dashboard with nothing on it, so the calm success line may only render on
 *  `live` — an unreachable backend rendering "Nothing needs you right now." is a false statement
 *  on the one product whose claim is that the trail tells the truth. */
export type ConnectionState = "connecting" | "live" | "unreachable";

export interface DashboardState {
  cases: CareCase[];
  run: AgentRun;
  connection: ConnectionState;
}

export interface ApprovalInput {
  caseId: string;
  decisionId: string;
  approved: boolean;
}

export interface ApprovalResult {
  caseId: string;
  runId: string;
  accepted: boolean;
}

export interface ScenarioRun {
  scenarioId: ScenarioId;
  runId: string;
  caseId: string;
}

export interface CareLoopApi {
  getDashboard(): Promise<DashboardState>;
  getCase(caseId: string): Promise<CareCase | undefined>;
  getRun(runId: string): Promise<AgentRun | undefined>;
  approveAction(input: ApprovalInput): Promise<ApprovalResult>;
  /** Ask CareLoop to observe an already-recorded effect again.
   *
   *  Takes the case and nothing else. There is deliberately no parameter for a tool, arguments, a
   *  confirmation id or an action: the backend resolves what to read entirely from durable case
   *  state, and a client that could name it would be choosing what CareLoop looks at. */
  recheckCase(caseId: string): Promise<ApprovalResult>;
  runScenario(id: ScenarioId): Promise<ScenarioRun>;
  resetDemo(): Promise<void>;
  /** Mock stand-in for run polling. The real adapter should poll /api/runs/:id
   *  and re-emit the same DashboardState shape. */
  subscribe(listener: (state: DashboardState) => void): () => void;
}
