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

export interface EvidenceExcerpt {
  fact: string;
  source: string;
  before: string;
  highlight: string;
  after: string;
  digest: string;
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
}

export interface Readback {
  authorized: string;
  observed: string;
  result: string;
  state: VerificationState;
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

export interface DashboardState {
  cases: CareCase[];
  run: AgentRun;
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
  runScenario(id: ScenarioId): Promise<ScenarioRun>;
  resetDemo(): Promise<void>;
  /** Mock stand-in for run polling. The real adapter should poll /api/runs/:id
   *  and re-emit the same DashboardState shape. */
  subscribe(listener: (state: DashboardState) => void): () => void;
}
