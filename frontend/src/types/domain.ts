/* CareLoop frontend domain contract.
   These types are the seam between the approved UI and the real backend.
   Where the backend's own schema differs, adapt the ADAPTER (services/), not the components. */

import type { BoundariesView } from "@/services/backend";

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
export type ScenarioId = "referral" | "injection" | "adversarial" | "resume" | "recovery";

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
  /** The rule the gate recorded when it authorised what this case did, on a settled case.
   *
   *  Read from the case's own `POLICY_ALLOW` entry — both fields are already projected. It is what
   *  lets the outcome link back to the standing capability it is an instance of, so a judge can go
   *  from "this happened" to "this is why it was allowed to" without either page inventing the
   *  connection. Absent while a run is in flight, and absent on a case that recorded no allow. */
  authorisedBy?: { ruleId: string; toolName: string };
  /** Present only on a case whose own audit trail records a settled ambiguous-outcome recovery.
   *
   *  Absent everywhere else, including on a case that is *still* waiting — see
   *  `recoveryProofFor`. A block that appeared on a waiting case would be claiming a recovery had
   *  happened while the outcome was still unknown, which is the one thing this whole feature
   *  exists to stop the product doing. */
  recovery?: RecoveryProof | null;
  /** Present only on a case whose own audit trail shows CareLoop supplied the proposal itself.
   *
   *  Absent everywhere else, including on a realistic poisoned-document case where a real planner
   *  genuinely proposed the same action — which is the distinction the whole feature rests on, and
   *  the reason this is read from a durable rationale rather than from the scenario the browser
   *  remembers launching. See `adversarialProofFor`. */
  adversarial?: AdversarialProof | null;
}

/** What the deterministic boundary test leaves behind, read entirely from the durable timeline.
 *
 *  Nothing here is inferred and nothing is fetched: every field comes from a payload key the API
 *  already projects. The block does not render at all unless every clause holds, which means a
 *  case that somehow *did* record an action fails the signature and shows nothing — rather than
 *  rendering a zero-effect claim over a case that had one. */
export interface AdversarialProof {
  /** The tool the supplied proposal named, in the caregiver's words. */
  proposedAction: string;
  /** The destination it named, when the tool declares one. Empty when it does not. */
  destination: string;
  /** The refusal in the caregiver's words, built by `blockedFor` from the two allowlisted keys. */
  refusalText: string;
  /** The reason code the policy engine recorded for it. A closed backend vocabulary. */
  reasonCode: string;
  /** The rule that refused it. Empty when the refusal was deny-by-default. */
  ruleId: string;
  /** The registry tool the refusal was about, for the `/trust#<tool>` deep link. */
  toolName: string;
  /** Durable `ActionRecord`s on the case. Zero, or this block does not render. */
  recordedActions: number;
  /** How many proposals on this case were refused outright. */
  refusals: number;
}

/** What a settled recovery leaves behind, read entirely from the durable timeline.
 *
 *  Nothing here is inferred and nothing is fetched: every field comes from a payload key the API
 *  already projects. See `recoveryProofFor`.
 *
 *  **There is deliberately no model-call count.** The backend proves zero recovery model calls with
 *  booby-trapped seams in `tests/test_recovery_proof.py`; the durable trail cannot, because one
 *  persisted plan does not establish that no model was invoked and failed. `plans` says what the
 *  trail *can* support, and the copy says exactly that and no more. */
export interface RecoveryProof {
  /** A short, safe prefix of the frozen action identity. Never the whole key. */
  actionKey: string;
  /** Durable `ActionRecord`s on the case. One, or this is not a settled recovery. */
  recordedActions: number;
  /** `PLAN_PROPOSED` entries on the case. */
  plans: number;
  /** The observation word the independent read produced — one of the closed backend vocabulary. */
  observation: string;
}

/** Where one CareLoop effect stands under the current caregiver's configuration.
 *
 *  Backend truth, carried through unchanged. Nothing in the frontend computes it — see
 *  `services/backend.ts` and `careloop/policy/boundaries.py`. */
export type CapabilityStanding = "AUTONOMOUS" | "REQUIRES_HUMAN" | "UNAVAILABLE";

/** One argument of one capability, in the caregiver's words with the technical detail behind it. */
export interface BoundaryCondition {
  name: string;
  /** "Only with", "Only for", "Written by CareLoop" — the row's label. */
  label: string;
  /** What must be true of this argument, in plain language. */
  requirement: string;
  /** The caregiver's own configured destination, or the literal CareLoop composes. */
  value?: string;
  /** The tier word for the required trust. Absent when the rules require no trust of it. */
  trustLabel?: string;
  /** Raw backend fields, for the collapsed "Why?" detail. Never the primary rendering. */
  argClass: string;
  requiredTrust: string | null;
  trustKey: string | null;
}

/** One thing CareLoop may do, must ask about, or currently cannot do.
 *
 *  Every field is copy over a value the backend derived. The frontend reconstructs no standing, no
 *  requirement and no availability — doing so would rebuild the second source of truth that
 *  `My Boundaries` exists to have removed. */
export interface Capability {
  toolName: string;
  /** "Book an appointment" — the same phrase the decision card and the timeline already use. */
  title: string;
  standing: CapabilityStanding;
  /** One sentence saying what this standing means for this caregiver. */
  summary: string;
  /** Why it is out of reach, scoped to the current configuration. Absent unless unavailable. */
  unavailableReason?: string;
  conditions: BoundaryCondition[];
  /** What happens after the effect: an independent read-back, or CareLoop's own record only. */
  afterwards: string;
  /** What happens if the provider's answer never arrives. */
  ifTheAnswerIsLost: string;
}

/** The standing boundary, as `My Boundaries` renders it. */
export interface Boundaries {
  capabilities: Capability[];
  /** Plain-language refusals the active configuration actually implies. */
  refusals: string[];
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
 *  `live`, `unreachable` or `at_capacity`. The distinction the UI must respect: a dashboard that
 *  cannot be read is not a dashboard with nothing on it, so the calm success line may only render
 *  on `live` — an unreachable backend rendering "Nothing needs you right now." is a false statement
 *  on the one product whose claim is that the trail tells the truth.
 *
 *  `at_capacity` is its own state for the same reason, one step finer. The deployed demo gives
 *  every visitor their own world and holds sixty-four of them; when all sixty-four are in use,
 *  `POST /api/sessions` answers **503** and this page has no world to read. That is a *different
 *  fact* from a backend that cannot be reached — the service is up, it is answering, and the
 *  remedy is a few minutes rather than a refresh — and showing them as one would be this product
 *  telling a small lie about its own state on the screen that exists to say true things. */
export type ConnectionState = "connecting" | "live" | "unreachable" | "at_capacity";

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
  /** The standing boundary for whoever is asking, exactly as the backend derived it.
   *
   *  Returns the **wire** shape rather than a domain shape, deliberately. Everything in it is
   *  already derived server-side from `rules.yaml`, the tool registry and this caller's own
   *  caregiver record, and cross-checked there against the real policy engine; a second near
   *  identical domain object would only carry copy, which is `presentation.ts`'s job and is applied
   *  at render. Nothing between here and the screen may recompute a standing.
   *
   *  Read once when `My Boundaries` opens, deliberately **not** part of the dashboard poller: a
   *  boundary changes when the ruleset or the caregiver record changes, neither of which happens
   *  mid-session, so polling it would add load and imply a volatility that does not exist.
   *
   *  Resolves `null` when the backend could not be read. The page must then say so — there is no
   *  fallback copy, because hand-written authority claims are the thing this feature removed. */
  getBoundaries(): Promise<BoundariesView | null>;
  runScenario(id: ScenarioId): Promise<ScenarioRun>;
  resetDemo(): Promise<void>;
  /** Mock stand-in for run polling. The real adapter should poll /api/runs/:id
   *  and re-emit the same DashboardState shape. */
  subscribe(listener: (state: DashboardState) => void): () => void;
}
