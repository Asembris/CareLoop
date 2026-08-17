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
  CaseDocument,
  ConnectionState,
  DashboardState,
  DecisionView as UiDecisionView,
  DocumentPage,
  EvidenceExcerpt,
  EvidenceItem,
  PolicyRefusal,
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
  pacedState,
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
  type PacedState,
  type PolicyLine
} from "./presentation";

/** Empty by default: every request is a relative `/api` path, which the vite dev proxy forwards
 *  and a production deployment serves same-origin. Nothing in the app knows where CareLoop lives,
 *  so backend CORS is never involved. */
const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";

/** The audit identity every decision is recorded against. See the header: not authentication. */
export const CAREGIVER = "Maya";

/* ------------------------------------------------------------------ *
 * The session — one world per visitor
 * ------------------------------------------------------------------ *
 *
 * The deployed demo gives every visitor their own case store and their own clinic, so two judges
 * can open the URL at the same time without either noticing the other. This is the client half:
 * one `POST /api/sessions` at the start, then that id on every request.
 *
 * **Nothing is asked of the visitor.** No sign-in, no button: the first request the page makes
 * mints a world. The id is an isolation namespace and *not* authentication — the backend verifies
 * nothing — which is the same thing `X-Caregiver` says about audit identity.
 *
 * **A backend without sessions answers 404 here, and that is a supported answer.** Local
 * development and the offline suite serve one world to everybody; the adapter carries no header at
 * all in that case and behaves exactly as it did before any of this existed.
 *
 * **`sessionStorage`, not `localStorage`, and not nothing.** Not nothing, because a page reload
 * would otherwise abandon a world with the visitor's cases in it. Not `localStorage`, because that
 * is shared by every tab: a judge with two tabs open would have them writing to one world and
 * resetting it under each other, which is a smaller copy of the bug this session exists to fix.
 */

const SESSION_HEADER = "X-CareLoop-Session";
const SESSION_STORAGE_KEY = "careloop.session";

/** The world this page is using, `null` once we know the backend serves only one. */
let session: string | null = null;

/** Whether the question above has been answered yet. Distinguishes "no session" from "not asked". */
let sessionResolved = false;

/** The mint in flight, so concurrent first requests produce one world rather than several. */
let minting: Promise<string | null> | null = null;

function storedSession(): string | null {
  try {
    return window.sessionStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    // Private-mode browsers throw on storage access. A world that does not survive a reload is a
    // great deal better than a page that does not load.
    return null;
  }
}

function rememberSession(value: string | null): void {
  session = value;
  sessionResolved = true;
  try {
    if (value === null) window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
    else window.sessionStorage.setItem(SESSION_STORAGE_KEY, value);
  } catch {
    /* see storedSession */
  }
}

/** Forget the current world, so the next request mints another. Used on a 410, and by tests. */
export function forgetSession(): void {
  session = null;
  sessionResolved = false;
  minting = null;
  try {
    window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    /* see storedSession */
  }
}

async function mint(): Promise<string | null> {
  const held = storedSession();
  if (held) {
    rememberSession(held);
    return held;
  }

  try {
    const response = await fetch(`${BASE_URL}/api/sessions`, {
      method: "POST",
      headers: { Accept: "application/json" }
    });
    if (response.status === 404) {
      // The one status that answers the question: this backend serves one world to everybody, so
      // stop asking and carry no session. Local development and the offline suite live here.
      rememberSession(null);
      return null;
    }
    if (!response.ok) {
      // A backend that could not answer is not a backend without sessions, and the difference is
      // not academic: measured against the deployed stack, this route answers **504 for about
      // seven seconds** while systemd restarts the API. Remembering that as "no sessions" is
      // sticky and permanent — the page would carry no session, every later request would be
      // refused 400, and the dashboard would sit empty looking like a hung backend until somebody
      // reloaded. A judge opening the URL during a redeploy is precisely who that happens to.
      return null;
    }
    const body = (await response.json()) as { session?: string };
    rememberSession(body.session ?? null);
    return session;
  } catch {
    // Offline. Not remembered, for the same reason: this is not an answer about the backend's
    // shape, and a blip must not leave the page permanently session-less against a backend that
    // requires one.
    return null;
  }
}

/** The world to name on requests, minting one the first time it is needed. */
export async function ensureSession(): Promise<string | null> {
  if (sessionResolved) return session;
  if (!minting) minting = mint().finally(() => (minting = null));
  return minting;
}

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
 *  last good state through a blip is better than a screen that goes blank.
 *
 *  **Two statuses are retried, once each, and both mean "you have no usable world".** A **410** is
 *  the world this page was using being gone — the backend restarted, or it went idle and was
 *  forgotten. A **400 on a request that carried no session** is the other side of the same coin:
 *  the mint failed a moment ago (a restarting backend answers 504, not 404), so this page is
 *  asking anonymously of a backend that hands out worlds. Both remedies are mechanical — mint and
 *  ask again — and both recover on the next poll rather than needing a reload.
 *
 *  A 400 *with* a session held is left alone: that is a different fault and swallowing it would
 *  hide it. And 410 stays distinct from the 409 a busy case returns, so neither has to be told
 *  apart by reading prose. */
export async function request<T>(
  path: string,
  init: RequestInit = {},
  remint = true
): Promise<HttpResult<T>> {
  const world = await ensureSession();
  try {
    const response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(world ? { [SESSION_HEADER]: world } : {}),
        ...(init.headers ?? {})
      }
    });
    const worldless = response.status === 400 && !world;
    if ((response.status === 410 || worldless) && remint) {
      forgetSession();
      return request<T>(path, init, false);
    }
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

/** Every state the case has been in, in order, off its own `STATE_TRANSITION` entries.
 *
 *  Both payload keys are on the backend's allowlist. This is what lets the display be paced
 *  without inventing anything: the sequence is the audit trail's, not the adapter's. */
export function recordedStates(timeline: readonly TimelineEventView[]): string[] {
  return timeline
    .filter((event) => event.kind === "STATE_TRANSITION")
    .map((event) => payloadString(event, "to"))
    .filter(Boolean);
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

/** The case's source documents, reassembled from the page facts the grid filters out.
 *
 *  This is the deliberate un-hiding of the poisoned document. The page facts carry the entire
 *  text of everything CareLoop read — the attack included — and until this existed there was no
 *  way, anywhere in the UI, to read what the flagship scenario is about. Grouped by provenance
 *  (`source_doc` + `page`), never by parsing the excerpt key, for the same reason `excerptFor`
 *  matches that way. The highlights are the values extraction actually lifted from that page, so
 *  the lens can show what became a fact and what stayed inert text. */
export function documentsFor(facts: readonly FactView[]): CaseDocument[] {
  const byDoc = new Map<string, DocumentPage[]>();
  for (const fact of facts) {
    if (!isExcerptFact(fact.key) || fact.page === null) continue;
    const pages = byDoc.get(fact.source_doc) ?? [];
    pages.push({
      page: fact.page,
      text: fact.value,
      highlights: facts
        .filter(
          (other) =>
            !isExcerptFact(other.key) &&
            other.source_doc === fact.source_doc &&
            other.page === fact.page &&
            other.value.length > 0
        )
        .map((other) => other.value)
    });
    byDoc.set(fact.source_doc, pages);
  }
  return Array.from(byDoc.entries()).map(([name, pages]) => ({
    name,
    pages: pages.sort((a, b) => a.page - b.page)
  }));
}

/** Every action the policy refused on this case, for the document lens's answer panel.
 *
 *  Distinct from the spine's fold (`timelineFor` hangs each denial off the event it interrupted):
 *  this is the flat list that sits beside the document, so a reader can hold the attack text and
 *  the refusals in one view. `matched_rule_id` is exposed deliberately — invariant 6 withholds it
 *  from the *model* because denial text re-enters the planner's prompt; a judge reading this panel
 *  is who the named declarative rule was written for. */
export function refusalsFor(timeline: readonly TimelineEventView[]): PolicyRefusal[] {
  return timeline
    .filter((event) => event.kind === "POLICY_DENY")
    .map((event) => ({
      text: blockedFor(payloadString(event, "tool_name"), payloadString(event, "reason_code")).text,
      reasonCode: payloadString(event, "reason_code"),
      ruleId: payloadString(event, "matched_rule_id")
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
    // Written out rather than left to the `summary` fallback. This is the end of the strongest
    // story the product tells — every action refused, nothing done — and the spine should say so
    // in its own words instead of relying on a default that a backend rewording could change.
    case "MANUAL_HANDOFF_REQUIRED":
      return "CareLoop stopped — manual follow-up needed";
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

/** The citation chip for a spine entry that recorded or validated a fact.
 *
 *  The payload names the fact by `key`; the fact itself carries the citation and the page text
 *  reaches the lens through `excerptFor`. Nothing without provenance gets a chip — triage's
 *  classification writes this kind with no key, and a config fact has no source document. */
function spineSource(entry: TimelineEventView, facts: readonly FactView[]): EvidenceExcerpt | undefined {
  if (entry.kind !== "EVIDENCE_RECORDED" && entry.kind !== "FACT_PROMOTED") return undefined;
  const key = payloadString(entry, "key");
  if (!key || isExcerptFact(key)) return undefined;
  const fact = facts.find((one) => one.key === key);
  if (!fact || !fact.source_doc) return undefined;
  return excerptFor(fact, facts);
}

/** The spine.
 *
 *  Two shapes are imposed here rather than by the backend. `STATE_TRANSITION` entries are dropped
 *  — the state machine narrating itself, which the status line already says. And `POLICY_DENY`
 *  entries are **folded** into the event they interrupted, because the backend records them as
 *  separate top-level entries and the UI hangs them as a dashed branch off the proposal they
 *  refused. A denial with nothing before it is emitted standalone rather than lost. */
export function timelineFor(
  timeline: readonly TimelineEventView[],
  facts: readonly FactView[] = []
): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  // A whole run finishes inside one minute, so every entry carries the same HH:MM and the rail
  // becomes a column of one repeated number that reads as a rendering fault. The time is printed
  // when it changes and left blank when it does not, which is what a log rail does.
  let printed = "";

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

    const time = clockTime(entry.at);
    events.push({
      id: String(entry.seq),
      time: time === printed ? "" : time,
      text: timelineText(entry),
      tone: toneFor(entry.kind),
      source: spineSource(entry, facts)
    });
    printed = time;
  }

  return events;
}

/* ------------------------------------------------------------------ *
 * Decisions
 * ------------------------------------------------------------------ */

/** The backend's pending decision as the card renders it.
 *
 *  `args` is what makes the card mean anything: an `Approval` stores `(tool_name, args_digest)`
 *  rather than the arguments, so the backend walks back to the `PLAN_PROPOSED` entry that *pair*
 *  came from — both halves, so another tool's identically-hashed proposal cannot supply them — and
 *  the card therefore names exactly the call that will be made. `args` is `{}` when no such entry
 *  exists, and the card is still shown. Nothing is reconstructed here: `approval_id` is opaque on
 *  both sides, and no client computes which proposal a decision covers. */
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
    rejectLabel: copy.rejectLabel,
    // Straight from the backend, never derived from `reason_code` here. The server evaluates the
    // handoff's recorded action identity and the action type's observer capability; a client that
    // guessed from the code alone would draw a button on cases the endpoint refuses with a 409.
    recheckable: decision.recheckable,
    recheckLabel: copy.recheckLabel
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
  now: Date,
  /** The state the display is currently walking through — see `pacedState`. Defaults to the
   *  case's real state, which is what every test that is not about pacing wants. */
  shownState: string = card.state
): CareCase {
  const timeline = detail?.timeline ?? [];
  const proposal = latestProposal(timeline);
  const verification = latestVerification(timeline);
  const decision = detail?.decision ?? card.decision;

  // Never paced. A mismatch is a safety claim, not a beat: the moment the timeline says the
  // clinic disagrees, the screen says so, whatever state the display has reached.
  const mismatch = verification !== null && !verification.passed;
  const verified =
    shownState === "DONE" && verification !== null && verification.passed && verification.readBack;
  const outcome =
    shownState === "DONE" && proposal ? outcomeCopy(proposal.toolName, proposal.args, verified) : undefined;

  const status = statusCopy({
    state: shownState,
    failureReason: card.failure_reason,
    mismatch,
    verified,
    outcomeBody: outcome?.body || null
  });

  return {
    id: card.case_id,
    title: titleFor(card.case_id, card.summary),
    // The section follows the shown state, not the stored one. Splitting them would put a row
    // reading "Making the arrangement…" under COMPLETED, which is worse than either alone.
    section: sectionFor(sectionNameFor(shownState, card.section)),
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
    events: timelineFor(timeline, detail?.facts ?? []),
    evidence: evidenceFor(detail?.facts ?? []),
    documents: documentsFor(detail?.facts ?? []),
    refusals: refusalsFor(timeline)
  };
}

/** The backend's own section rule, applied to the state the display is showing.
 *
 *  A copy of `careloop.api.projections.SECTIONS`, and the only place the frontend restates a
 *  backend decision — unavoidable, because the section for a state the case has already left is
 *  not something the backend was asked. It is the *rule* rather than a second list: `working` is
 *  "the rest", so a new case state joins it here exactly as it does there. When the display has
 *  caught up, the backend's own answer is used instead of this one. */
function sectionNameFor(shownState: string, storedSection: string): string {
  if (shownState === "AWAITING_HUMAN") return "needs_you";
  if (shownState === "DONE" || shownState === "FAILED") return "completed";
  if (shownState === "") return storedSection;
  return "working";
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

type Listener = (state: DashboardState) => void;

/** How often the dashboard re-reads while anything is moving.
 *
 *  A second is fast enough that the Working column visibly progresses and slow enough that three
 *  demo cases do not flood a local backend. It is not a pacing device: the beats are real, and
 *  every node commits its transition before it finishes. */
export const POLL_INTERVAL_MS = 1000;

/** How long after a command the poller keeps going even if nothing looks busy yet.
 *
 *  The verbs return 202 before the graph has done anything, so the first poll after pressing a
 *  button can legitimately still show the old state. Without this the poller would conclude the
 *  world is at rest and stop one tick before the run it just started became visible. */
export const SETTLE_GRACE_MS = 4000;

/** How often to re-try the dashboard read while the backend is unreachable.
 *
 *  Slower than `POLL_INTERVAL_MS` because nothing on screen is moving — the only thing a probe can
 *  discover is that the backend came back — and a page left open against a dead host should not
 *  hammer it once a second forever. It never stops while a listener exists: recovery is the one
 *  transition an unreachable page cannot be told about any other way. */
export const OFFLINE_RETRY_MS = 3000;

/** Gap between attempts when a scenario or a reset is refused because a run is in flight. */
export const RESET_RETRY_MS = 400;

/** How many times to re-attempt a refused scenario or reset.
 *
 *  `RESET_ATTEMPTS * RESET_RETRY_MS` has to exceed the longest a run can take, and the original
 *  3 x 300ms did not come close. Session E measured 6.7s at the slow end and widened this to 16s;
 *  a press that outlives the budget is dropped in silence, which is the failure this guards.
 *
 *  **Session S re-measured it and it had moved.** 32s, because the slowest run now observed is
 *  14.6s — see `SLOWEST_OBSERVED_RUN_MS`. It only ever elapses while this visitor's *own* run is
 *  in flight, which is exactly when pressing another scenario should wait; under isolation another
 *  visitor's run can no longer refuse this one at all. */
export const RESET_ATTEMPTS = 80;

/** The slowest run observed end to end against the deployed backend.
 *
 *  Here so the retry budget can be asserted against the thing it has to outlast, rather than
 *  against a number someone picked. If runs get slower, this moves and the test says so.
 *
 *  **14.6s, session S, measured through CloudFront.** Session E's 6.7s was a single visitor on a
 *  warm process. What actually varies, measured in the same sitting on the same host:
 *
 *    solo, warm, `referral`          1.9 – 2.9s
 *    three visitors at once          2.1 / 2.2 / 2.4s
 *    `injection_only`, warm          6.8s   (every tool proposed and refused, so many planner calls)
 *    two at once, minutes after a redeploy   10.7s and **14.6s**
 *
 *  So concurrency is not what costs — three simultaneous runs are as quick as one — and the two
 *  things that do are the scenario's own shape and a process that has just started. A number
 *  measured once under the friendliest conditions is the kind of constant this project keeps
 *  going stale. */
export const SLOWEST_OBSERVED_RUN_MS = 14600;

export class RealCareLoopApi implements CareLoopApi {
  /** Case detail by id, keyed on the version it was read at.
   *
   *  `CaseCard.version` increments on every committed transition, so a poll that sees an
   *  unchanged version can reuse the detail it already has. Three demo cases at one poll a second
   *  would otherwise be four requests a second for data that has not moved. */
  private details = new Map<string, { version: number; detail: CaseDetail }>();

  /** Which state each case's row is currently showing. See `pacedState` for the trade. */
  private paced = new Map<string, PacedState>();

  protected run: AgentRun = emptyRun();
  protected scenario: ScenarioId | null = null;

  /** What the last read established about the backend. `connecting` only before the first answer. */
  private connection: ConnectionState = "connecting";

  /** The last dashboard that was actually read, kept so an outage shows stale truth labelled as
   *  such rather than a blank page — or worse, a calm empty one. */
  private lastCases: CareCase[] = [];

  /** The last `RunView` seen, kept so the drawer survives the registry losing the run. */
  private lastRunView: RunView | null = null;

  private listeners = new Set<Listener>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private polling = false;
  private graceUntil = 0;
  /** Bumped by every stop. A tick that was already in flight when the poller was stopped
   *  finishes its request and then finds its generation stale, so it cannot schedule the next
   *  one — otherwise unsubscribing during a request would leave the poller running forever. */
  private generation = 0;

  /* -------------------- reads -------------------- */

  /** Every case, in the section its state puts it in.
   *
   *  `GET /api/cases` returns three arrays rather than a flat list, and the flattening keeps the
   *  section each case came in under. `completed` is the backend's `AT_REST_STATES`, so a FAILED
   *  case arrives there with the DONE ones and is told apart by `state` — never by the column. */
  async getDashboard(): Promise<DashboardState> {
    const cases = this.absorb(await this.readCases());
    return { cases, run: this.run, connection: this.connection };
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

  /* -------------------- writes -------------------- */

  /** Approve or reject a pending decision.
   *
   *  `X-Caregiver` is required and a missing one is **400 by design, never 401** — nothing is
   *  verified, so 401 would claim a check that is not happening. It is not a login and the 400 is
   *  not "upgraded".
   *
   *  Both refusals are answers rather than errors, and both are reported honestly rather than
   *  thrown: **409** means a run is already in flight for this case, or the approval has already
   *  been decided — pressing again once it settles is the whole remedy. **404** means the case or
   *  the approval is gone. `useCareLoop` calls this as `void api.approveAction(...)` and has
   *  nowhere to show an exception, so the result is a value and the next poll tells the truth. */
  async approveAction(input: ApprovalInput): Promise<ApprovalResult> {
    const verb = input.approved ? "approve" : "reject";
    const path =
      `/api/cases/${encodeURIComponent(input.caseId)}` +
      `/decisions/${encodeURIComponent(input.decisionId)}/${verb}`;

    const result = await request<RunView>(path, {
      method: "POST",
      headers: { "X-Caregiver": CAREGIVER }
    });

    if (result.data) this.adopt(result.data);
    this.kick();
    return {
      caseId: input.caseId,
      runId: result.data?.run_id ?? this.run.id,
      accepted: result.status === 202
    };
  }

  /** Ask CareLoop to observe an already-recorded effect again.
   *
   *  **The case id is the entire request.** No body is sent, and there is nothing in this
   *  signature that could carry a tool, an argument, a confirmation id or an action — the backend
   *  resolves what to read from the handoff it already stored. That is the security property of
   *  the route, and it is worth keeping visible on the client side of it too.
   *
   *  Refusals are answers rather than errors, exactly as they are for `approveAction`: **409**
   *  means a run is in flight or the case is not one where looking again means anything, and
   *  **404** means the case is gone. Nothing is thrown; the next poll tells the truth. */
  async recheckCase(caseId: string): Promise<ApprovalResult> {
    const result = await request<RunView>(
      `/api/cases/${encodeURIComponent(caseId)}/recheck`,
      { method: "POST", headers: { "X-Caregiver": CAREGIVER } }
    );

    if (result.data) this.adopt(result.data);
    this.kick();
    return {
      caseId,
      runId: result.data?.run_id ?? this.run.id,
      accepted: result.status === 202
    };
  }

  /** Press one of the four scenarios.
   *
   *  **One request, and the reset is the server's half of it.** `referral` and `injection` carry
   *  the same referral document and therefore ask for the same slot, so a scenario pressed on top
   *  of another gets a correct 409 from a clinic that already holds it — which reads, in front of
   *  a judge, like the injection defence failing at something unrelated to injection. This used to
   *  be `resetDemo()` and then the scenario, which is two requests with a window between them: the
   *  reset lands, the start is refused, and the screen is left empty with nothing running. A client
   *  cannot close that window, so the backend does the pair under this world's own lock.
   *
   *  A 409 here means this visitor's *own* previous run has not settled, which is exactly when
   *  pressing again should wait — so the retry budget stays, now guarding one call rather than
   *  bridging two. */
  async runScenario(id: ScenarioId): Promise<ScenarioRun> {
    this.forget();
    this.scenario = id;

    let result: HttpResult<RunView> = { status: 0 };
    for (let attempt = 0; attempt < RESET_ATTEMPTS; attempt += 1) {
      result = await request<RunView>(`/api/demo/scenarios/${encodeURIComponent(id)}`, {
        method: "POST"
      });
      if (result.status !== 409) break;
      await new Promise((resolve) => setTimeout(resolve, RESET_RETRY_MS));
    }

    if (result.data) this.adopt(result.data);
    this.kick();
    return {
      scenarioId: id,
      runId: result.data?.run_id ?? "",
      caseId: result.data?.case_id ?? ""
    };
  }

  /** Clear every case and put the clinic back.
   *
   *  Refused with 409 while any run is in flight — clearing the store underneath a live graph is
   *  a version conflict wearing a costume. Waiting and pressing again is the remedy the backend's
   *  own error text names, so that is what this does rather than reporting it at a caregiver. */
  async resetDemo(): Promise<void> {
    for (let attempt = 0; attempt < RESET_ATTEMPTS; attempt += 1) {
      const result = await request<unknown>("/api/demo/reset", { method: "POST" });
      if (result.status !== 409) break;
      await new Promise((resolve) => setTimeout(resolve, RESET_RETRY_MS));
    }
    this.forget();
    this.run = emptyRun();
    this.lastRunView = null;
    this.scenario = null;
    this.kick();
  }

  /* -------------------- the subscription -------------------- */

  /** Poll `GET /api/cases` and re-emit the same `DashboardState` the mock pushed.
   *
   *  `GET /api/cases` rather than `GET /api/runs/{id}`, because the *case* carries the state
   *  transitions the dashboard exists to show; the run carries only the node trace. And never
   *  `?wait=`, which blocks until the run settles and would make the Working column unreachable.
   *
   *  Polling **stops** when nothing is moving: no case in a working state and no command issued
   *  in the last few seconds. It is not throttled to a slower beat — a stopped poller is a
   *  checkable claim, and every way the world can change from this screen goes through a verb
   *  that restarts it. */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    this.kick();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stop();
    };
  }

  /* -------------------- internals -------------------- */

  /** `null` means the dashboard could not be read at all — a failed read and an empty dashboard
   *  are different facts, and folding them together is how an outage renders as "nothing to do". */
  private async readCases(): Promise<CareCase[] | null> {
    const sections = await request<CaseSections>("/api/cases");
    if (!sections.data) return null;

    const cards = [...sections.data.needs_you, ...sections.data.working, ...sections.data.completed];
    const now = new Date();
    return Promise.all(
      cards.map(async (card) => {
        const detail = await this.detailFor(card);
        return buildCase(card, detail, this.runIdFor(card), now, this.pace(card, detail, now));
      })
    );
  }

  /** Advance this case's display one step, at most, towards the state it is actually in. */
  private pace(card: CaseCard, detail: CaseDetail | null, now: Date): string {
    const next = pacedState(
      this.paced.get(card.case_id),
      recordedStates(detail?.timeline ?? []),
      card.state,
      now.getTime()
    );
    this.paced.set(card.case_id, next);
    return next.shown;
  }

  /** Note the run a verb just started, and give the poller something to follow. */
  private adopt(view: RunView): void {
    this.lastRunView = view;
    this.run = buildRun(view, this.details.get(view.case_id)?.detail ?? null, this.scenario);
    this.graceUntil = Date.now() + SETTLE_GRACE_MS;
  }

  /** Re-read the run record, surviving a registry that has never heard of it.
   *
   *  `GET /api/runs/{id}` is an in-memory, per-process registry (open decision 17): a backend
   *  restart 404s a run id that was live a second ago. Everything the drawer actually shows —
   *  the proposed call, the gate verdicts, the idempotency key, the verification — comes from the
   *  case, so the last node trace is reused and the panel keeps telling the truth about the case
   *  even when the run record behind it is gone. */
  private async refreshRun(): Promise<void> {
    if (!this.run.id) return;

    const fetched = await request<RunView>(`/api/runs/${encodeURIComponent(this.run.id)}`);
    const view = fetched.data ?? this.lastRunView;
    if (!view) return;
    if (fetched.data) this.lastRunView = fetched.data;

    this.run = buildRun(view, this.details.get(view.case_id)?.detail ?? null, this.scenario);
  }

  private emit(state: DashboardState): void {
    for (const listener of this.listeners) listener(state);
  }

  /** Fold one read's answer into what the adapter knows, and return what the screen should show. */
  private absorb(read: CareCase[] | null): CareCase[] {
    if (read === null) {
      this.connection = "unreachable";
      return this.lastCases;
    }
    this.connection = "live";
    this.lastCases = read;
    return read;
  }

  /** One read, then decide whether there is any reason to do another. */
  private async tick(generation: number): Promise<void> {
    const read = await this.readCases();
    await this.refreshRun();
    if (generation !== this.generation) return;
    const cases = this.absorb(read);
    this.emit({ cases, run: this.run, connection: this.connection });

    if (this.listeners.size === 0) {
      this.polling = false;
      this.timer = null;
      return;
    }

    // Unreachable never stops the poller: the page cannot learn the backend came back any other
    // way. It slows to OFFLINE_RETRY_MS instead, because nothing on a dead connection is "moving".
    if (read === null) {
      this.timer = setTimeout(() => void this.tick(generation), OFFLINE_RETRY_MS);
      return;
    }

    // `working` is every case state except AWAITING_HUMAN, DONE and FAILED — which is exactly
    // "the graph will move this again". A case parked for a decision is at rest until a person
    // acts, and a person acting goes through a verb that restarts the poller.
    const moving = cases.some((one) => one.section === "working");
    if (moving || Date.now() < this.graceUntil) {
      this.timer = setTimeout(() => void this.tick(generation), POLL_INTERVAL_MS);
    } else {
      this.polling = false;
      this.timer = null;
    }
  }

  /** Read now, and keep reading while anything moves. Safe to call when already polling. */
  private kick(): void {
    this.graceUntil = Date.now() + SETTLE_GRACE_MS;
    if (this.polling || this.listeners.size === 0) return;
    this.polling = true;
    void this.tick(this.generation);
  }

  private stop(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.polling = false;
    this.generation += 1;
  }

  /** Whether a poll is currently scheduled. Exists so "polling stops on settle" is checkable. */
  get isPolling(): boolean {
    return this.polling;
  }


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
    this.paced.clear();
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
