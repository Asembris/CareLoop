/* The wire shapes CareLoop's FastAPI read surface actually returns.
 *
 * Hand-written rather than generated, and deliberately so: these are the *response* models in
 * `careloop/api/models.py`, which are projections with no defaults and no invented fields, and
 * writing them out is what makes the adapter's mapping reviewable against that module. Nothing
 * here is the frontend's domain — `types/domain.ts` is — and nothing here should grow a helper.
 *
 * Two shapes differ from what a reader might guess, and both are load-bearing:
 *
 *   - `GET /api/cases` returns THREE ARRAYS, not a flat list. The partition is total and disjoint
 *     over `CaseState`, and `completed` is `AT_REST_STATES` — so **a FAILED case arrives under
 *     `completed`**, on purpose. It is told apart by `state`, never by which array it came in.
 *   - `RunView` is not the drawer's `AgentRun`. It carries five fields; the rest of the drawer is
 *     composed from the case's own timeline.
 *
 * `TimelineEventView.payload` is already allowlist-filtered by the API (33 keys), and
 * `TimelineEvent.diagnostic` is withheld wholesale. Neither is negotiable from here: a payload key
 * that is not on the backend's allowlist does not arrive, whatever this file declares.
 */

/** The ten members of `careloop.state.case.CaseState`. */
export type CaseState =
  | "NEW"
  | "GATHERING_EVIDENCE"
  | "PLANNING"
  | "AWAITING_HUMAN"
  | "READY_TO_EXECUTE"
  | "EXECUTING"
  | "VERIFYING"
  | "WAITING_EXTERNAL"
  | "DONE"
  | "FAILED";

/** The backend's section names. Renamed to the UI's `needs | working | done` in the adapter. */
export type BackendSection = "needs_you" | "working" | "completed";

/** The eighteen members of `careloop.state.case.TimelineKind`. */
export type TimelineKind =
  | "CASE_CREATED"
  | "CASE_REOPENED"
  | "STATE_TRANSITION"
  | "EVIDENCE_RECORDED"
  | "EVIDENCE_DUPLICATE_SKIPPED"
  | "VALIDATION_REFUSED"
  | "FACT_PROMOTED"
  | "PLAN_PROPOSED"
  | "POLICY_ALLOW"
  | "POLICY_DENY"
  | "POLICY_REQUIRE_HUMAN"
  | "APPROVAL_REQUESTED"
  | "APPROVAL_GRANTED"
  | "APPROVAL_REJECTED"
  | "ACTION_EXECUTED"
  | "DUPLICATE_SUPPRESSED"
  | "VERIFICATION_PASSED"
  | "VERIFICATION_FAILED";

/** The seven members of `careloop.policy.verdicts.ReasonCode`.
 *
 *  Note `AUTHORITY_NOT_TRUSTED`, which the mock adapter invented as `AUTHORITY_ESCALATION`. The
 *  two the backend keeps apart are `INSUFFICIENT_FACT_TRUST` (remediable — validate and repropose)
 *  and `AUTHORITY_NOT_TRUSTED` (irremediable — no validator can raise an AUTHORITY argument). */
export type ReasonCode =
  | "DESTINATION_NOT_TRUSTED"
  | "AUTHORITY_NOT_TRUSTED"
  | "REQUIRES_HUMAN_APPROVAL"
  | "WRONG_CASE_STATE"
  | "INSUFFICIENT_FACT_TRUST"
  | "NO_RULE_MATCH"
  | "NO_SAFE_ACTION_AVAILABLE";

/** `careloop.api.runs.RunState`. `SETTLED` means the graph stopped — read the case for what it
 *  stopped *at*: parked for a decision, done, or failed. */
export type RunState = "RUNNING" | "SETTLED" | "ERRORED";

/** `careloop.provenance.trust.TrustLevel`. Never rendered raw — see `trustWord`. */
export type TrustLevel = "UNTRUSTED" | "CONTROLLED" | "TRUSTED";

export interface FactView {
  key: string;
  value: string;
  trust_level: TrustLevel | string;
  /** Already formatted by the backend, e.g. `referral.pdf p.2`. */
  citation: string;
  source_doc: string;
  page: number | null;
  extractor: string;
  validated_by: string | null;
}

export interface DecisionView {
  approval_id: string;
  tool_name: string;
  reason_code: ReasonCode | string;
  /** Written for the *planner*, not the caregiver. The adapter substitutes its own copy. */
  reason_text: string;
  /** The arguments the approval's digest covers, recovered from the PLAN_PROPOSED entry. */
  args: Record<string, string>;
  requested_at: string;
}

export interface ApprovalView {
  approval_id: string;
  tool_name: string;
  args_digest: string;
  reason_code: ReasonCode | string;
  reason_text: string;
  status: "PENDING" | "APPROVED" | "REJECTED" | string;
  requested_at: string;
  decided_at: string | null;
  decided_by: string | null;
}

export interface ActionView {
  idempotency_key: string;
  action_type: string;
  args_digest: string;
  /** The typed tool result, as JSON text. */
  result: string;
  recorded_at: string;
}

export interface TimelineEventView {
  seq: number;
  at: string;
  kind: TimelineKind | string;
  actor: string;
  summary: string;
  payload: Record<string, unknown>;
}

export interface CaseCard {
  case_id: string;
  state: CaseState | string;
  section: BackendSection | string;
  summary: string;
  version: number;
  created_at: string;
  updated_at: string;
  failure_reason: string | null;
  fact_count: number;
  action_count: number;
  decision: DecisionView | null;
}

export interface CaseSections {
  needs_you: CaseCard[];
  working: CaseCard[];
  completed: CaseCard[];
}

export interface CaseDetail {
  case_id: string;
  state: CaseState | string;
  section: BackendSection | string;
  version: number;
  event_id: string;
  summary: string;
  created_at: string;
  updated_at: string;
  failure_reason: string | null;
  decision: DecisionView | null;
  facts: FactView[];
  approvals: ApprovalView[];
  actions: ActionView[];
  timeline: TimelineEventView[];
}

export interface RunView {
  run_id: string;
  case_id: string;
  state: RunState | string;
  /** Graph node ids in execution order: dispatch, triage, evidence, plan, gate, execute, verify.
   *  A replan genuinely emits `… gate, plan, gate …` — the sequence can go backwards. */
  executed_nodes: string[];
  /** The raised type's name, never its message. */
  error: string | null;
}
