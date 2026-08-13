/* The real CareLoop adapter.
 *
 * Everything the approved UI needs, composed from the nine routes `careloop/api/app.py` ships.
 * Not one component changes: `CareLoopApi` is the whole seam, and every divergence between what
 * the backend stores and what the design asks for is resolved here.
 *
 * Four things about this file are load-bearing rather than incidental.
 *
 * **It never passes `?wait=`.** Every verb accepts it and it blocks until the run settles, which
 * would make the Working column unreachable — everything would appear at once, which is precisely
 * what `careloop/api/runs.py` explains the 202 exists to avoid. The dashboard exists to show work
 * happening; a synchronous call deletes the thing being demonstrated.
 *
 * **It polls `GET /api/cases`, not `GET /api/runs/{id}`.** The case carries the state transitions
 * — every node commits its transition to the store before it finishes, so a poller genuinely
 * observes GATHERING_EVIDENCE → PLANNING → EXECUTING → VERIFYING. The run carries only the node
 * trace, and it lives in an in-process registry that a backend restart empties.
 *
 * **Approve and reject carry `X-Caregiver`.** It is an audit identity, not authentication:
 * nothing is verified and anyone reaching the port can claim any name, which is exactly why a
 * missing one is 400 and never 401. It is not surfaced as a login and the 400 is not "upgraded".
 *
 * **It reads the timeline through named readers.** The functions below mirror
 * `careloop.graph.context`'s `latest_delivery` / `proposal_for` in intent: one place that knows
 * what a `PLAN_PROPOSED` payload holds, so the next mapping does not go rummaging.
 */

import type {
  AgentRun,
  ApprovalInput,
  ApprovalResult,
  CareCase,
  CareLoopApi,
  DashboardState,
  DecisionView as UiDecisionView,
  EvidenceExcerpt,
  EvidenceItem,
  PolicyVerdict,
  Readback,
  ScenarioId,
  ScenarioRun,
  TimelineEvent,
  VerificationState
} from "@/types/domain";
import type {
  CaseCard,
  CaseDetail,
  CaseSections,
  DecisionView,
  FactView,
  RunView,
  TimelineEventView
} from "./backend";
import {
  actionIntentFor,
  authorizedSummary,
  blockedFor,
  clockTime,
  decisionCopy,
  decisionKindFor,
  factLabel,
  factSummary,
  isExcerptFact,
  isRenderedOnTheSpine,
  observedLabel,
  outcomeCopy,
  policyDetailFor,
  readbackResult,
  relativeTime,
  sectionFor,
  stageFor,
  stageLabelFor,
  statusCopy,
  titleFor,
  toneFor,
  toolPast,
  toolPhrase,
  trustWord,
  verificationDetailFor,
  type PolicyLine
} from "./presentation";

/** Empty by default: every request is a relative `/api` path, which the vite dev proxy forwards
 *  and a production deployment serves same-origin. Nothing in the app knows where CareLoop lives,
 *  so backend CORS is never involved. */
const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";

/** The audit identity every decision is recorded against. See the header: not authentication. */
export const CAREGIVER = "Maya";

/* ------------------------------------------------------------------ *
 * HTTP
 * ------------------------------------------------------------------ */

export interface HttpResult<T> {
  status: number;
  /** Absent when the response was not a 2xx, or carried no body. */
  data?: T;
}

/** One request. Never throws for an HTTP status — a 404 and a 409 are both answers.
 *
 *  A thrown network error is caught and reported as status 0, because the UI has nowhere to show
 *  an exception: `useCareLoop` calls every verb as `void api.x()`. A screen that keeps showing the
 *  last good state through a blip is better than a screen that goes blank. */
export async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<HttpResult<T>> {
  try {
    const response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init.headers ?? {}) }
    });
    if (!response.ok) return { status: response.status };
    const text = await response.text();
    return { status: response.status, data: text ? (JSON.parse(text) as T) : undefined };
  } catch {
    return { status: 0 };
  }
}

/* ------------------------------------------------------------------ *
 * Named readers over the case timeline
 * ------------------------------------------------------------------ */

function payloadString(event: TimelineEventView, key: string): string {
  const value = event.payload[key];
  return typeof value === "string" ? value : "";
}

function lastOfKind(timeline: readonly TimelineEventView[], kind: string): TimelineEventView | null {
  for (let i = timeline.length - 1; i >= 0; i -= 1) {
    if (timeline[i].kind === kind) return timeline[i];
  }
  return null;
}

export interface Proposal {
  toolName: string;
  args: Record<string, string>;
}

/** The planner's most recent proposal. Both payload keys are on the backend's allowlist. */
export function latestProposal(timeline: readonly TimelineEventView[]): Proposal | null {
  const event = lastOfKind(timeline, "PLAN_PROPOSED");
  if (!event) return null;
  const args = event.payload.args;
  return {
    toolName: payloadString(event, "tool_name"),
    args: args && typeof args === "object" ? (args as Record<string, string>) : {}
  };
}

export interface VerificationOutcome {
  passed: boolean;
  /** Whether an *external* system was read back, rather than only CareLoop's own record. */
  readBack: boolean;
}

/** The last verification, whichever way it went.
 *
 *  Read as the *last* rather than as "is there a PASSED anywhere", because a read-back mismatch
 *  routes the case back to PLANNING and re-verifies: a case can carry a PASSED that a later
 *  FAILED has superseded, and taking the optimistic one would let a mismatch read as complete. */
export function latestVerification(
  timeline: readonly TimelineEventView[]
): VerificationOutcome | null {
  for (let i = timeline.length - 1; i >= 0; i -= 1) {
    const event = timeline[i];
    if (event.kind === "VERIFICATION_PASSED" || event.kind === "VERIFICATION_FAILED") {
      return {
        passed: event.kind === "VERIFICATION_PASSED",
        readBack: payloadString(event, "read_back") === "true"
      };
    }
  }
  return null;
}

/** Every gate verdict the case recorded, in order, for the under-the-hood panel. */
export function policyLines(timeline: readonly TimelineEventView[]): PolicyLine[] {
  return timeline
    .filter(
      (event) =>
        event.kind === "POLICY_ALLOW" ||
        event.kind === "POLICY_DENY" ||
        event.kind === "POLICY_REQUIRE_HUMAN"
    )
    .map((event) => ({
      verdict: payloadString(event, "verdict") || event.kind.replace("POLICY_", ""),
      reasonCode: payloadString(event, "reason_code"),
      matchedRuleId: payloadString(event, "matched_rule_id")
    }));
}

/* ------------------------------------------------------------------ *
 * Evidence
 * ------------------------------------------------------------------ */

const EXCERPT_WINDOW_BEFORE = 180;
const EXCERPT_WINDOW_AFTER = 240;

/** The page a fact came from, with the extracted span located inside it.
 *
 *  Extraction records one fact per document page whose *value is the whole page*, so the planner
 *  can read a document without any of it being lifted into a key an action could be built from.
 *  Those page facts reach the API like any other, which is what makes the lens's excerpt
 *  reachable — the page is matched by provenance (`source_doc` + `page`) rather than by parsing
 *  the excerpt key, so the two tables never have to agree on a format.
 *
 *  It degrades rather than lies: a fact whose page is not on the case, or whose value does not
 *  appear in it verbatim, gets the value alone and no surrounding text. `digest` stays empty —
 *  there is no digest of evidence anywhere in the backend, and inventing one here would be the
 *  relabelled row the drawer's EVIDENCE DIGEST was dropped to avoid. */
export function excerptFor(fact: FactView, facts: readonly FactView[]): EvidenceExcerpt {
  const bare: EvidenceExcerpt = {
    fact: factSummary(fact.key, fact.value),
    source: fact.citation,
    before: "",
    highlight: fact.value,
    after: "",
    digest: ""
  };

  const page = facts.find(
    (candidate) =>
      isExcerptFact(candidate.key) &&
      candidate.source_doc === fact.source_doc &&
      candidate.page === fact.page
  );
  if (!page) return bare;

  const at = page.value.indexOf(fact.value);
  if (at < 0) return bare;

  const start = Math.max(0, at - EXCERPT_WINDOW_BEFORE);
  const end = Math.min(page.value.length, at + fact.value.length + EXCERPT_WINDOW_AFTER);
  return {
    ...bare,
    before: (start > 0 ? "… " : "") + page.value.slice(start, at),
    after: page.value.slice(at + fact.value.length, end) + (end < page.value.length ? " …" : "")
  };
}

/** The evidence grid: extracted facts only.
 *
 *  Page facts are filtered out. They are not evidence a caregiver reads — each one is an entire
 *  page of a PDF — and a grid of them would bury the three or four values an action was actually
 *  built from. They are still used, as the source for the lens above. */
export function evidenceFor(facts: readonly FactView[]): EvidenceItem[] {
  return facts
    .filter((fact) => !isExcerptFact(fact.key))
    .map((fact) => ({
      fact: factSummary(fact.key, fact.value),
      source: fact.citation,
      trust: trustWord(fact.trust_level),
      excerpt: excerptFor(fact, facts)
    }));
}

/* ------------------------------------------------------------------ *
 * The timeline
 * ------------------------------------------------------------------ */

/** One audit entry as a caregiver reads it.
 *
 *  The backend's `summary` is accurate and written for an engineer — "DENY for send_email",
 *  "recorded slot_start=2026-09-03T09:30:00 — source: referral.pdf p.2". These say the same thing
 *  in the register the rest of the screen is in, and fall back to `summary` for any kind that has
 *  no line here, so a new `TimelineKind` renders as itself rather than disappearing. */
export function timelineText(event: TimelineEventView): string {
  const tool = payloadString(event, "tool_name");
  const key = payloadString(event, "key");
  const who = payloadString(event, "decided_by");

  switch (event.kind) {
    case "CASE_CREATED":
      return "Case opened";
    case "CASE_REOPENED":
      return "New information arrived — case reopened";
    case "EVIDENCE_RECORDED":
      // Triage writes its classification under this kind too, with a different payload.
      return payloadString(event, "category")
        ? `Read as ${payloadString(event, "category").toLowerCase()} · ${payloadString(event, "urgency").toLowerCase()}`
        : key
          ? `${factLabel(key)} found`
          : event.summary;
    case "EVIDENCE_DUPLICATE_SKIPPED":
      return key ? `${factLabel(key)} was already recorded` : event.summary;
    case "VALIDATION_REFUSED":
      return key ? `${factLabel(key)} could not be validated` : event.summary;
    case "FACT_PROMOTED":
      return key ? `${factLabel(key)} validated` : event.summary;
    case "PLAN_PROPOSED":
      return `Next step: ${toolPhrase(tool).toLowerCase()}`;
    case "POLICY_ALLOW":
      return "Checked against policy — allowed";
    case "POLICY_REQUIRE_HUMAN":
      return "Policy requires your decision";
    case "APPROVAL_REQUESTED":
      return "Decision requested";
    case "APPROVAL_GRANTED":
      return `${who || "The caregiver"} approved`;
    case "APPROVAL_REJECTED":
      return `${who || "The caregiver"} declined — looking for another way`;
    case "ACTION_EXECUTED":
      return toolPast(tool);
    case "DUPLICATE_SUPPRESSED":
      return "Already done for this case — not repeated";
    case "VERIFICATION_PASSED":
      return payloadString(event, "read_back") === "true"
        ? "Clinic record checked independently — match"
        : "Checked against what was authorized — match";
    case "VERIFICATION_FAILED":
      return payloadString(event, "read_back") === "true"
        ? "The clinic's record disagrees — not marked complete"
        : "Did not match what was authorized";
    default:
      return event.summary;
  }
}

/** The spine.
 *
 *  Two shapes are imposed here rather than by the backend. `STATE_TRANSITION` entries are dropped
 *  — the state machine narrating itself, which the status line already says. And `POLICY_DENY`
 *  entries are **folded** into the event they interrupted, because the backend records them as
 *  separate top-level entries and the UI hangs them as a dashed branch off the proposal they
 *  refused. A denial with nothing before it is emitted standalone rather than lost. */
export function timelineFor(timeline: readonly TimelineEventView[]): TimelineEvent[] {
  const events: TimelineEvent[] = [];

  for (const entry of timeline) {
    if (entry.kind === "POLICY_DENY") {
      const blocked = blockedFor(
        payloadString(entry, "tool_name"),
        payloadString(entry, "reason_code")
      );
      const previous = events[events.length - 1];
      if (previous) {
        previous.blocked = [...(previous.blocked ?? []), blocked];
      } else {
        events.push({
          id: String(entry.seq),
          time: clockTime(entry.at),
          text: blocked.text,
          tone: "denied"
        });
      }
      continue;
    }

    if (!isRenderedOnTheSpine(entry.kind)) continue;
    // One fact per page, each holding an entire page of a PDF. Real evidence, wrong grain.
    if (entry.kind === "EVIDENCE_RECORDED" && isExcerptFact(payloadString(entry, "key"))) continue;

    events.push({
      id: String(entry.seq),
      time: clockTime(entry.at),
      text: timelineText(entry),
      tone: toneFor(entry.kind)
    });
  }

  return events;
}

/* ------------------------------------------------------------------ *
 * Decisions
 * ------------------------------------------------------------------ */

/** The backend's pending decision as the card renders it.
 *
 *  `args` is what makes the card mean anything: an `Approval` stores only a digest, so the
 *  backend walks back to the `PLAN_PROPOSED` entry that digest came from and the card therefore
 *  names exactly the call that will be made. `approval_id` is opaque on both sides — pass it
 *  through, never parse it. */
export function decisionFor(decision: DecisionView | null): UiDecisionView | null {
  if (!decision) return null;
  const copy = decisionCopy(decision.reason_code, decision.tool_name, decision.args);
  return {
    id: decision.approval_id,
    kind: decisionKindFor(decision.reason_code),
    title: copy.title,
    body: copy.body,
    facts: copy.facts,
    after: copy.after,
    approveLabel: copy.approveLabel,
    rejectLabel: copy.rejectLabel
  };
}

/* ------------------------------------------------------------------ *
 * A whole case
 * ------------------------------------------------------------------ */

function verificationState(outcome: VerificationOutcome | null): VerificationState {
  if (!outcome) return "PENDING";
  return outcome.passed ? "MATCH" : "MISMATCH";
}

function readbackFor(
  proposal: Proposal | null,
  outcome: VerificationOutcome | null
): Readback | undefined {
  if (!proposal || !outcome) return undefined;
  return {
    authorized: authorizedSummary(proposal.toolName, proposal.args),
    observed: observedLabel(outcome.readBack),
    result: readbackResult(outcome.passed),
    state: verificationState(outcome)
  };
}

/** One case, from its dashboard row and its detail.
 *
 *  `verified` is deliberately narrow: it means a DONE case whose verification *read an external
 *  system back*, which is a materially stronger claim than "finished". The `VerificationSeal`
 *  plays on it, and a seal that played on every completion would mean nothing. A `send_email`
 *  that completed correctly is therefore DONE and unsealed, which is the honest reading — the
 *  other three effects are still simulated and their verification is closed-loop. */
export function buildCase(
  card: CaseCard,
  detail: CaseDetail | null,
  runId: string | undefined,
  now: Date
): CareCase {
  const timeline = detail?.timeline ?? [];
  const proposal = latestProposal(timeline);
  const verification = latestVerification(timeline);
  const decision = detail?.decision ?? card.decision;

  const mismatch = verification !== null && !verification.passed;
  const verified = card.state === "DONE" && verification !== null && verification.passed && verification.readBack;
  const outcome =
    card.state === "DONE" && proposal ? outcomeCopy(proposal.toolName, proposal.args, verified) : undefined;

  const status = statusCopy({
    state: card.state,
    failureReason: card.failure_reason,
    mismatch,
    verified,
    outcomeBody: outcome?.body || null
  });

  return {
    id: card.case_id,
    title: titleFor(card.case_id, card.summary),
    section: sectionFor(card.section),
    statusLabel: status.statusLabel,
    statusText: status.statusText,
    headline: status.headline,
    updatedLabel: relativeTime(card.updated_at, now),
    verified,
    mismatch,
    runId,
    outcome,
    readback: readbackFor(proposal, verification),
    decision: decisionFor(decision),
    events: timelineFor(timeline),
    evidence: evidenceFor(detail?.facts ?? [])
  };
}

/* ------------------------------------------------------------------ *
 * A run
 * ------------------------------------------------------------------ */

const VERDICTS: Record<string, PolicyVerdict> = {
  ALLOW: "ALLOW",
  DENY: "DENY",
  REQUIRE_HUMAN: "REQUIRE_HUMAN"
};

/** The drawer's run record, before any run has been started. */
export function emptyRun(): AgentRun {
  return {
    id: "",
    scenario: null,
    stage: "IDLE",
    stageLabel: "idle",
    actionIntent: "none proposed",
    idempotencyKey: "none yet",
    policyVerdict: "PENDING",
    policyDetail: "pending",
    // Absent from the backend rather than withheld: there is no digest of evidence anywhere in
    // it. The drawer's EVIDENCE DIGEST row was dropped rather than filled with something else.
    evidenceDigest: "",
    verification: "PENDING",
    verificationDetail: "pending"
  };
}

/** `RunView` plus the case's own timeline.
 *
 *  `AgentRun` is not `RunView`: the run carries five fields, and everything interesting in the
 *  drawer — the proposed call, the idempotency key, the gate verdicts, the verification — is on
 *  the case. `scenario` is not on the API at all and is supplied by whoever pressed the button. */
export function buildRun(
  run: RunView,
  detail: CaseDetail | null,
  scenario: ScenarioId | null
): AgentRun {
  const timeline = detail?.timeline ?? [];
  const proposal = latestProposal(timeline);
  const verification = latestVerification(timeline);
  const lines = policyLines(timeline);
  const latest = lines[lines.length - 1];
  const caseState = detail?.state ?? "";
  const actions = detail?.actions ?? [];
  const action = actions[actions.length - 1];

  return {
    id: run.run_id,
    scenario,
    stage: stageFor(run.executed_nodes, run.state, caseState),
    stageLabel: stageLabelFor({
      runId: run.run_id,
      executedNodes: run.executed_nodes,
      runState: run.state,
      caseState,
      verdict: latest?.verdict ?? null,
      error: run.error
    }),
    actionIntent: proposal ? actionIntentFor(proposal.toolName, proposal.args) : "none proposed",
    idempotencyKey: action?.idempotency_key ?? "none yet",
    policyVerdict: (latest && VERDICTS[latest.verdict]) ?? "PENDING",
    policyDetail: policyDetailFor(lines),
    evidenceDigest: "",
    verification: verificationState(verification),
    verificationDetail: verificationDetailFor(
      verification ? verification.passed : null,
      verification?.readBack ?? false
    )
  };
}

/* ------------------------------------------------------------------ *
 * The adapter
 * ------------------------------------------------------------------ */

export class RealCareLoopApi implements CareLoopApi {
  /** Case detail by id, keyed on the version it was read at.
   *
   *  `CaseCard.version` increments on every committed transition, so a poll that sees an
   *  unchanged version can reuse the detail it already has. Three demo cases at one poll a second
   *  would otherwise be four requests a second for data that has not moved. */
  private details = new Map<string, { version: number; detail: CaseDetail }>();

  protected run: AgentRun = emptyRun();
  protected scenario: ScenarioId | null = null;

  /* -------------------- reads -------------------- */

  /** Every case, in the section its state puts it in.
   *
   *  `GET /api/cases` returns three arrays rather than a flat list, and the flattening keeps the
   *  section each case came in under. `completed` is the backend's `AT_REST_STATES`, so a FAILED
   *  case arrives there with the DONE ones and is told apart by `state` — never by the column. */
  async getDashboard(): Promise<DashboardState> {
    const sections = await request<CaseSections>("/api/cases");
    if (!sections.data) return { cases: [], run: this.run };

    const cards = [...sections.data.needs_you, ...sections.data.working, ...sections.data.completed];
    const now = new Date();
    const cases = await Promise.all(
      cards.map(async (card) => buildCase(card, await this.detailFor(card), this.runIdFor(card), now))
    );
    return { cases, run: this.run };
  }

  async getCase(caseId: string): Promise<CareCase | undefined> {
    const detail = await request<CaseDetail>(`/api/cases/${encodeURIComponent(caseId)}`);
    if (!detail.data) return undefined;

    const card = cardFromDetail(detail.data);
    this.details.set(caseId, { version: detail.data.version, detail: detail.data });
    return buildCase(card, detail.data, this.runIdFor(card), new Date());
  }

  /** One run. `undefined` when the registry has never heard of it.
   *
   *  The registry is in memory and per-process, so a backend restart 404s a run id that was live
   *  a second ago — see open decision 17. That is a real thing to survive rather than a bug to
   *  report at a caregiver, and `subscribe` degrades to the case's own state when it happens. */
  async getRun(runId: string): Promise<AgentRun | undefined> {
    const run = await request<RunView>(`/api/runs/${encodeURIComponent(runId)}`);
    if (!run.data) return undefined;

    const detail = await request<CaseDetail>(`/api/cases/${encodeURIComponent(run.data.case_id)}`);
    return buildRun(run.data, detail.data ?? null, this.scenario);
  }

  /* -------------------- writes, wired in the next commit -------------------- */

  async approveAction(input: ApprovalInput): Promise<ApprovalResult> {
    return { caseId: input.caseId, runId: this.run.id, accepted: false };
  }

  async runScenario(id: ScenarioId): Promise<ScenarioRun> {
    return { scenarioId: id, runId: "", caseId: "" };
  }

  async resetDemo(): Promise<void> {}

  subscribe(): () => void {
    return () => {};
  }

  /* -------------------- internals -------------------- */

  /** The case's detail, from cache when the version has not moved. */
  protected async detailFor(card: CaseCard): Promise<CaseDetail | null> {
    const held = this.details.get(card.case_id);
    if (held && held.version === card.version) return held.detail;

    const fetched = await request<CaseDetail>(`/api/cases/${encodeURIComponent(card.case_id)}`);
    if (!fetched.data) return held?.detail ?? null;

    this.details.set(card.case_id, { version: fetched.data.version, detail: fetched.data });
    return fetched.data;
  }

  /** `runId` is not a case field: `RunView.run_id` is minted per run by an in-memory registry,
   *  and the UI treats it as a stable case property. The adapter retains the one it started. */
  protected runIdFor(card: CaseCard): string | undefined {
    return this.run.id && cardMatchesRun(card, this.run) ? this.run.id : undefined;
  }

  protected forget(): void {
    this.details.clear();
  }
}

/** A run id looks like `run-{case_id}-{n}`, which is the only link back to a case the dashboard
 *  has without a second request. Matched by prefix rather than parsed. */
function cardMatchesRun(card: CaseCard, run: AgentRun): boolean {
  return run.id.startsWith(`run-${card.case_id}-`);
}

/** The dashboard row a detail response implies, so `getCase` and `getDashboard` build a case the
 *  same way. `CaseDetail` carries every field `CaseCard` does except the two counts. */
function cardFromDetail(detail: CaseDetail): CaseCard {
  return {
    case_id: detail.case_id,
    state: detail.state,
    section: detail.section,
    summary: detail.summary,
    version: detail.version,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
    failure_reason: detail.failure_reason,
    fact_count: detail.facts.length,
    action_count: detail.actions.length,
    decision: detail.decision
  };
}

export const realApi: CareLoopApi = new RealCareLoopApi();
