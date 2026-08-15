/* Everything the UI says that the backend does not say.
 *
 * `careloop/api/` decides nothing and invents nothing: two import-time checks in
 * `projections.py` require every response field to name the stored field it comes from or be
 * declared derived, and response fields carry no defaults. So a `statusLabel` could not be added
 * to `CaseCard` even if somebody wanted to — there is no stored field for it to name.
 *
 * That is why all of this lives here. Every function below is a pure function of what the store
 * holds, with no clock of its own except the one passed in and no network at all, which is what
 * makes the whole file testable without a backend.
 *
 * Three rules it keeps:
 *
 *   1. **Never claim more than the backend said.** "Verified" means an external system was read
 *      back, not merely that a case finished. A FAILED case never renders in a success tone.
 *   2. **Never render a raw enum.** Trust levels, case states and reason codes all pass through a
 *      table on their way to a caregiver.
 *   3. **Default closed.** An unrecognised reason code is not approvable; an unrecognised anything
 *      falls back to the backend's own wording rather than to a guess.
 */

import type {
  BlockedAction,
  CaseSection,
  DecisionFact,
  DecisionKind,
  EventTone,
  RunStage
} from "@/types/domain";
import type { CaseState, RunState } from "./backend";

/* ------------------------------------------------------------------ *
 * Sections
 * ------------------------------------------------------------------ */

const SECTION_NAMES: Record<string, CaseSection> = {
  needs_you: "needs",
  working: "working",
  completed: "done"
};

/** The backend's section name as the UI's.
 *
 *  A `FAILED` case arrives under `completed` — the backend's `completed` is `AT_REST_STATES`,
 *  which means "the graph will move this no further" rather than "this went well". It therefore
 *  lands in `done` and is told apart by `state`, which is what `statusCopy` does. Anything
 *  unrecognised goes to `working`, matching the backend's own rule that a new state joins the
 *  middle column rather than vanishing from the dashboard. */
export function sectionFor(backendSection: string): CaseSection {
  return SECTION_NAMES[backendSection] ?? "working";
}

/* ------------------------------------------------------------------ *
 * State → copy
 * ------------------------------------------------------------------ */

export interface StatusCopy {
  statusLabel: string;
  statusText: string;
  headline: string;
}

/* Typed as `Record<CaseState, …>` on purpose: an eleventh CaseState stops the frontend
   compiling until somebody writes its copy, rather than rendering a raw enum at a caregiver. */
const STATUS: Record<CaseState, StatusCopy> = {
  NEW: {
    statusLabel: "JUST ARRIVED",
    statusText: "Just arrived",
    headline: "Reading what just came in"
  },
  GATHERING_EVIDENCE: {
    statusLabel: "GATHERING DOCUMENTS",
    statusText: "Gathering the documents",
    headline: "Gathering the documents"
  },
  PLANNING: {
    statusLabel: "WORKING OUT THE NEXT STEP",
    statusText: "Working out the next step",
    headline: "Working out the next step"
  },
  AWAITING_HUMAN: {
    statusLabel: "NEEDS YOUR DECISION",
    statusText: "Needs your decision",
    headline: "Needs your decision"
  },
  READY_TO_EXECUTE: {
    statusLabel: "READY TO ACT",
    statusText: "Ready to act on your decision",
    headline: "Ready to act on your decision"
  },
  EXECUTING: {
    statusLabel: "ACTING",
    statusText: "Making the arrangement…",
    headline: "Making the arrangement…"
  },
  VERIFYING: {
    statusLabel: "CONFIRMING",
    statusText: "Confirming with the clinic…",
    headline: "Confirming with the clinic…"
  },
  WAITING_EXTERNAL: {
    statusLabel: "WAITING FOR A REPLY",
    statusText: "Waiting for the other side",
    headline: "Waiting for the other side"
  },
  DONE: {
    statusLabel: "COMPLETED",
    statusText: "Completed",
    headline: "Completed"
  },
  FAILED: {
    statusLabel: "DID NOT COMPLETE",
    statusText: "Did not complete",
    headline: "CareLoop stopped rather than guess"
  }
};

export interface StatusInput {
  state: string;
  /** `Case.failure_reason`, which the state machine sets from the transition's own reason. */
  failureReason: string | null;
  /** The last verification on the timeline failed. */
  mismatch: boolean;
  /** A DONE case whose verification read an external system back. */
  verified: boolean;
  /** The outcome line, when there is one — a completed case says what happened, not "Completed". */
  outcomeBody: string | null;
}

/** The three status strings for one case.
 *
 *  A mismatch outranks the state, and it still needs to. Verification failing used to route the
 *  case back to `PLANNING`, so it could be mid-replan while the honest thing to say was that the
 *  clinic's record disagrees; it now parks in `AWAITING_HUMAN`, whose own copy is about a decision
 *  being needed rather than about what went wrong. Either way the state alone does not say it.
 *  `HANDOFF.md` §13 is explicit that a MISMATCH may not be presented as a completed case, and this
 *  is where that is enforced. */
export function statusCopy(input: StatusInput): StatusCopy {
  if (input.mismatch) {
    return {
      statusLabel: "DID NOT MATCH",
      statusText: "The clinic's record disagrees",
      headline: "The clinic's record disagrees with what was authorized"
    };
  }

  const base = STATUS[input.state as CaseState] ?? STATUS.NEW;

  if (input.state === "FAILED") {
    return {
      statusLabel: base.statusLabel,
      // `failure_reason` is the transition's own reason — engineer-facing, and the only honest
      // account of why this stopped. Naming it beats a soothing line that says nothing.
      statusText: input.failureReason ? `Did not complete — ${input.failureReason}` : base.statusText,
      headline: base.headline
    };
  }

  if (input.state === "DONE") {
    return {
      statusLabel: base.statusLabel,
      statusText: input.verified ? "Done · independently verified" : "Done · checked against what was authorized",
      headline: input.outcomeBody ?? base.headline
    };
  }

  return base;
}

/* ------------------------------------------------------------------ *
 * Time
 * ------------------------------------------------------------------ */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  );
}

/** "Just now" / "14 min ago" / "3 hr ago" / "Yesterday" / "Aug 11".
 *
 *  `now` is a parameter rather than a call to `new Date()` so the tests are not a race. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";

  const seconds = Math.max(0, (now.getTime() - then.getTime()) / 1000);
  if (seconds < 45) return "Just now";

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${Math.max(1, minutes)} min ago`;

  if (sameDay(then, now)) return `${Math.floor(minutes / 60)} hr ago`;

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(then, yesterday)) return "Yesterday";

  return `${MONTHS[then.getMonth()]} ${then.getDate()}`;
}

/** "16:02" — the timeline spine's left rail. */
export function clockTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

/** "Thu Sep 3 · 9:30 AM" — an appointment slot as a person reads it.
 *
 *  A clinic slot arrives as a naive ISO string (`2026-09-03T09:30:00`) and is deliberately not
 *  timezone-converted: the backend's canonical form does not convert either, because two
 *  spellings of one instant must not compare equal. Rendering it verbatim keeps the screen and
 *  the trust rule talking about the same value. */
export function slotLabel(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;

  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const hours = at.getHours();
  const suffix = hours < 12 ? "AM" : "PM";
  const twelve = hours % 12 === 0 ? 12 : hours % 12;
  const minutes = String(at.getMinutes()).padStart(2, "0");
  return `${days[at.getDay()]} ${MONTHS[at.getMonth()]} ${at.getDate()} · ${twelve}:${minutes} ${suffix}`;
}

/* ------------------------------------------------------------------ *
 * Trust
 * ------------------------------------------------------------------ */

const TRUST_WORDS: Record<string, string> = {
  UNTRUSTED: "From a document",
  CONTROLLED: "Validated",
  TRUSTED: "Confirmed"
};

/** The caregiver-facing word for a trust tier.
 *
 *  `types/domain.ts` says raw tiers stay out of this view, and it is right to: `CONTROLLED` is a
 *  provenance term, and what a caregiver needs to know is that the value was checked. */
export function trustWord(level: string): string {
  return TRUST_WORDS[level] ?? "Unchecked";
}

/* ------------------------------------------------------------------ *
 * Tools and their arguments
 * ------------------------------------------------------------------ */

/** What a tool does, as a sentence opener. Keys are `careloop.tools.registry.REGISTRY` names. */
const TOOL_PHRASES: Record<string, string> = {
  send_email: "Send an email",
  book_appointment: "Book the appointment",
  upload_document: "Upload the document",
  grant_portal_access: "Grant portal access",
  fetch_document: "Fetch the document"
};

export function toolPhrase(toolName: string): string {
  return TOOL_PHRASES[toolName] ?? (toolName ? `Run ${toolName}` : "Take an action");
}

/** The same tool as a thing that has already happened, for the timeline. */
const TOOL_PAST: Record<string, string> = {
  send_email: "Email sent",
  book_appointment: "Appointment booked",
  upload_document: "Document uploaded",
  grant_portal_access: "Access granted",
  fetch_document: "Document fetched"
};

export function toolPast(toolName: string): string {
  return TOOL_PAST[toolName] ?? (toolName ? `${toolName} performed` : "Action performed");
}

/** Mono labels for a tool's arguments, as the decision card's fact row shows them. */
const ARG_LABELS: Record<string, Record<string, string>> = {
  send_email: { to: "TO", subject: "SUBJECT", body: "MESSAGE" },
  book_appointment: {
    provider_id: "WHERE",
    patient_id: "PATIENT",
    slot_start: "WHEN",
    reason: "REASON"
  },
  upload_document: { portal_id: "PORTAL", document_id: "DOCUMENT" },
  grant_portal_access: { grantee: "WHO", role: "ROLE", portal_id: "PORTAL" },
  fetch_document: { document_id: "DOCUMENT" }
};

export function argLabel(toolName: string, argName: string): string {
  return ARG_LABELS[toolName]?.[argName] ?? argName.replace(/_/g, " ").toUpperCase();
}

/** A tool's arguments in the order the registry declares them, not the order JSON happened to
 *  serialize them in.
 *
 *  It matters most on `send_email`, where the declared order puts the recipient first: the
 *  destination is the safety-relevant field and a caregiver approving an email should read who it
 *  is going to before they read what it says. Anything the table does not know keeps its
 *  incoming order, appended after what it does. */
export function orderedArgs(toolName: string, args: Record<string, string>): [string, string][] {
  const declared = Object.keys(ARG_LABELS[toolName] ?? {});
  const known = declared.filter((name) => name in args).map((name): [string, string] => [name, args[name]]);
  const rest = Object.entries(args).filter(([name]) => !declared.includes(name));
  return [...known, ...rest];
}

/** An argument value as a person reads it. Only `slot_start` needs help. */
export function argValue(argName: string, value: string): string {
  if (!value) return "—";
  return argName === "slot_start" ? slotLabel(value) : value;
}

/* ------------------------------------------------------------------ *
 * Evidence
 * ------------------------------------------------------------------ */

/** `careloop.graph.extract.EXCERPT_PREFIX`.
 *
 *  Extraction records one fact per document page whose *value is the whole page*, so the planner
 *  can read the document without any of it being lifted into a key an action could be built from.
 *  Those facts reach the API like any other, and putting them in the evidence grid would fill it
 *  with page dumps. They are filtered out of the grid and used as the source for the lens. */
export const EXCERPT_PREFIX = "excerpt:";

export function isExcerptFact(key: string): boolean {
  return key.startsWith(EXCERPT_PREFIX);
}

const FACT_LABELS: Record<string, string> = {
  patient_id: "Patient",
  slot_start: "Appointment slot",
  document_id: "Document",
  provider_id: "Provider",
  claim_number: "Claim number",
  rejection_code: "Rejection code",
  receipt_number: "Receipt number",
  amount_billed: "Amount billed",
  amount_allowed: "Amount allowed",
  plan_paid: "Plan paid",
  patient_responsibility: "Patient responsibility",
  referring_clinician: "Referring clinician",
  amount_paid: "Amount paid"
};

/** A fact key as a caregiver reads it. Falls back to the key with its underscores opened out. */
export function factLabel(key: string): string {
  if (FACT_LABELS[key]) return FACT_LABELS[key];
  const opened = key.replace(/_/g, " ");
  return opened.charAt(0).toUpperCase() + opened.slice(1);
}

/** "Appointment slot · Thu Sep 3 · 9:30 AM" — the evidence card's headline. */
export function factSummary(key: string, value: string): string {
  return `${factLabel(key)} · ${argValue(key, value)}`;
}

/* ------------------------------------------------------------------ *
 * Timeline
 * ------------------------------------------------------------------ */

const TONES: Record<string, EventTone> = {
  CASE_CREATED: "neutral",
  CASE_REOPENED: "neutral",
  STATE_TRANSITION: "neutral",
  EVIDENCE_RECORDED: "neutral",
  EVIDENCE_DUPLICATE_SKIPPED: "neutral",
  VALIDATION_REFUSED: "untrusted",
  FACT_PROMOTED: "neutral",
  PLAN_PROPOSED: "neutral",
  POLICY_ALLOW: "neutral",
  POLICY_DENY: "denied",
  POLICY_REQUIRE_HUMAN: "decision",
  APPROVAL_REQUESTED: "decision",
  APPROVAL_GRANTED: "decision",
  APPROVAL_REJECTED: "decision",
  // "decision" rather than "denied": the refusals are already rendered as denied on their own
  // entries, and this row is the one that says a person is needed — the same tone the Needs You
  // card it produces is drawn in.
  MANUAL_HANDOFF_REQUIRED: "decision",
  ACTION_EXECUTED: "neutral",
  DUPLICATE_SUPPRESSED: "neutral",
  VERIFICATION_PASSED: "verified",
  VERIFICATION_FAILED: "denied"
};

export function toneFor(kind: string): EventTone {
  return TONES[kind] ?? "neutral";
}

/** Timeline kinds the spine does not render.
 *
 *  `STATE_TRANSITION` is the state machine narrating itself — every one of them is already said
 *  by the status line, and a dozen "PLANNING -> EXECUTING" rows would bury the four things that
 *  actually happened. `POLICY_DENY` is not dropped: it is *folded* into the event it interrupted,
 *  which is where the UI hangs its dashed blocked branch. */
export function isRenderedOnTheSpine(kind: string): boolean {
  return kind !== "STATE_TRANSITION" && kind !== "POLICY_DENY";
}

/** Why an action was refused, as one sentence.
 *
 *  Built from the two allowlisted payload keys a POLICY_DENY carries. `AUTHORITY_NOT_TRUSTED` is
 *  the real code for an authority grant — the mock adapter invented `AUTHORITY_ESCALATION`, and
 *  the distinction the backend keeps is that an insufficiently trusted VALUE can be validated and
 *  reproposed while an AUTHORITY argument never can. */
const REFUSAL_PHRASES: Record<string, string> = {
  DESTINATION_NOT_TRUSTED: "to a destination nothing approved",
  AUTHORITY_NOT_TRUSTED: "with powers nothing approved",
  INSUFFICIENT_FACT_TRUST: "on a value that has not been confirmed",
  WRONG_CASE_STATE: "before this case was ready for it",
  REQUIRES_HUMAN_APPROVAL: "without a person's decision",
  NO_RULE_MATCH: "with no rule permitting it",
  NO_SAFE_ACTION_AVAILABLE: "safely",
  // Never reached in practice: this code is carried by a MANUAL_HANDOFF_REQUIRED entry and never by
  // a POLICY_DENY, because nothing was refused — an action was carried out and could not be
  // confirmed. Listed so the map stays total over the backend's vocabulary rather than falling
  // through to a bare tool phrase if a future writer ever does deny with it.
  EXTERNAL_STATE_NOT_CONFIRMED: "and could not be confirmed afterwards"
};

export function blockedFor(toolName: string, reasonCode: string): BlockedAction {
  const why = REFUSAL_PHRASES[reasonCode];
  return {
    text: why ? `${toolPhrase(toolName)} ${why}` : toolPhrase(toolName),
    rule: reasonCode || undefined
  };
}

/* ------------------------------------------------------------------ *
 * Decisions
 * ------------------------------------------------------------------ */

/** Which decision kind a reason code is, defaulting closed.
 *
 *  Only `REQUIRES_HUMAN_APPROVAL` may render Approve and Reject. Everything else — including a
 *  code this build has never heard of — maps to `NO_SAFE_ACTION_AVAILABLE`, which `DecisionCard`
 *  renders with no approval affordance at all. Defaulting closed is both correct and free.
 *
 *  `EXTERNAL_STATE_NOT_CONFIRMED` is the second backend reason that parks a case without raising
 *  an approval, and it needed nothing here: the default already refuses it an approve button, and
 *  a *kind* is about what the card may offer rather than about why it appeared. What it did need
 *  is truthful copy, which is `decisionCopy`'s job — the two are deliberately separate, so a new
 *  reason to stop can never accidentally arrive with authority attached. */
export function decisionKindFor(reasonCode: string): DecisionKind {
  return reasonCode === "REQUIRES_HUMAN_APPROVAL" ? "REQUIRES_HUMAN_APPROVAL" : "NO_SAFE_ACTION_AVAILABLE";
}

export interface DecisionCopy {
  title: string;
  body: string;
  facts: DecisionFact[];
  after: string;
  approveLabel: string;
  rejectLabel: string;
}

/** The caregiver-facing decision card.
 *
 *  `DecisionView.reason_text` is not used, and that is the point: it is a literal from
 *  `REASON_TEXT` written for the *planner* — "This action needs the caregiver's decision. Stop and
 *  await it." — which is the wrong register and the wrong reader.
 *
 *  `args` is the load-bearing input. An `Approval` stores `(tool_name, args_digest)`, so the
 *  backend walks back to the `PLAN_PROPOSED` entry that pair came from — matching both halves, so
 *  another tool's proposal cannot supply them — and the card therefore names exactly the call that
 *  will be made. Losing that would leave a caregiver approving a tool name. */
export function decisionCopy(
  reasonCode: string,
  toolName: string,
  args: Record<string, string>
): DecisionCopy {
  const facts = orderedArgs(toolName, args)
    .filter(([, value]) => value !== "")
    .map(([name, value]) => ({ label: argLabel(toolName, name), value: argValue(name, value) }));

  // Two reasons a case stops with nothing to approve, and they are close to opposites. The copy
  // below says nothing was done; this one says something *was*, and using the wrong one would be
  // the worse mistake in the safer direction — telling a caregiver no action was taken when a
  // booking is sitting in a clinic nobody has checked. Both render the same non-approval card:
  // `decisionKindFor` defaults closed, so neither offers an affordance it has no authority for.
  if (reasonCode === "EXTERNAL_STATE_NOT_CONFIRMED") {
    return {
      title: "The result could not be confirmed",
      body:
        "CareLoop carried this out and then checked the other system's own record of it. The " +
        "two do not agree, or the record could not be read at all — so CareLoop has stopped " +
        "rather than assume, and will not act again on this case by itself.",
      facts,
      after: "Check the other system before taking another action. CareLoop will not retry.",
      approveLabel: "Approve",
      rejectLabel: "Reject"
    };
  }

  if (decisionKindFor(reasonCode) === "NO_SAFE_ACTION_AVAILABLE") {
    return {
      title: "No safe action is available",
      body:
        "Every action CareLoop could take here was refused by its own policy. Nothing has been " +
        "done and nothing will be done automatically — this one needs a person.",
      facts,
      after: "Nothing was changed. CareLoop will not retry on its own.",
      approveLabel: "Approve",
      rejectLabel: "Reject"
    };
  }

  return {
    title: toolPhrase(toolName),
    body:
      "CareLoop has worked out the next step and needs your authority before it acts. This is " +
      "exactly what it will do — nothing more.",
    facts,
    after: "After you approve: CareLoop acts, then checks the result independently.",
    approveLabel: "Approve",
    rejectLabel: "Not now"
  };
}

/* ------------------------------------------------------------------ *
 * Outcome and read-back
 * ------------------------------------------------------------------ */

export interface OutcomeCopy {
  title: string;
  body: string;
}

/** What happened, from the tool that did it and the arguments it was authorised with. */
export function outcomeCopy(
  toolName: string,
  args: Record<string, string>,
  verified: boolean
): OutcomeCopy {
  const confirmed = verified ? " and confirmed" : "";
  switch (toolName) {
    case "book_appointment":
      return {
        title: `Appointment booked${confirmed}`,
        body: [args.provider_id, args.slot_start ? slotLabel(args.slot_start) : ""]
          .filter(Boolean)
          .join(" · ")
      };
    case "send_email":
      return {
        title: `Email sent${confirmed}`,
        body: [args.to, args.subject].filter(Boolean).join(" · ")
      };
    case "upload_document":
      return {
        title: `Document uploaded${confirmed}`,
        body: [args.document_id, args.portal_id].filter(Boolean).join(" → ")
      };
    case "grant_portal_access":
      return {
        title: `Access granted${confirmed}`,
        body: [args.grantee, args.role, args.portal_id].filter(Boolean).join(" · ")
      };
    default:
      return { title: `Completed${confirmed}`, body: toolName };
  }
}

/** The compact form of what was authorised, for the outcome panel's AUTHORIZED column. */
export function authorizedSummary(toolName: string, args: Record<string, string>): string {
  switch (toolName) {
    case "book_appointment":
      return args.slot_start ? slotLabel(args.slot_start) : "—";
    case "send_email":
      return args.to || "—";
    case "upload_document":
      return [args.document_id, args.portal_id].filter(Boolean).join(" → ") || "—";
    case "grant_portal_access":
      return [args.grantee, args.role].filter(Boolean).join(" · ") || "—";
    default:
      return Object.values(args).filter(Boolean).join(" · ") || "—";
  }
}

/** The OBSERVED AT CLINIC column, which has no value behind it and must not pretend to.
 *
 *  The observed values exist only inside `Verification.diagnostic`, which the API withholds
 *  wholesale and deliberately: they are text from a system CareLoop does not control, and
 *  `tests/test_verify_readback.py::TestTheReadBackIsNotAnInjectionChannel` exists to keep them out
 *  of anything a prompt or a page could render. What reaches the API is `read_back: true|false` —
 *  enough to say *whether* the world was read, never *what it said*. This column says exactly
 *  that much. */
export function observedLabel(readBack: boolean): string {
  return readBack ? "Confirmed by an independent read" : "Not read back";
}

/** The RESULT column. */
export function readbackResult(passed: boolean | null): string {
  if (passed === null) return "Pending";
  return passed ? "Match — loop closed" : "No match";
}

/* ------------------------------------------------------------------ *
 * The run record, as the Under the Hood drawer reads it
 * ------------------------------------------------------------------ */

const NODE_STAGES: Record<string, RunStage> = {
  // `dispatch` has no UI stage: it is the router that reads the case's state and picks an entry
  // point, so painting it would add a step to the agent path that is not one.
  triage: "TRIAGE",
  evidence: "EVIDENCE",
  plan: "PLANNER",
  gate: "GATE",
  execute: "EXECUTE",
  verify: "VERIFY"
};

/** Which stage the drawer's agent path should be showing.
 *
 *  `SETTLED` is reserved for a run that is genuinely over: the graph errored, or the case reached
 *  a resting state. A run that *settles* with the case parked in `AWAITING_HUMAN` is the normal
 *  approval beat and not an end — the graph simply has no satisfied outgoing edge — so the drawer
 *  keeps showing `GATE`, which is where the case actually is.
 *
 *  The sequence can go backwards: a refused proposal sends the graph `gate → plan`, and the last
 *  mapped node is what the drawer paints. That is honest — the agent really did go back a step. */
export function stageFor(
  executedNodes: readonly string[],
  runState: string,
  caseState: string
): RunStage {
  if (runState === "ERRORED") return "SETTLED";
  if (caseState === "DONE" || caseState === "FAILED") return "SETTLED";

  for (let i = executedNodes.length - 1; i >= 0; i -= 1) {
    const stage = NODE_STAGES[executedNodes[i]];
    if (stage) return stage;
  }
  return "IDLE";
}

export interface StageLabelInput {
  runId: string;
  executedNodes: readonly string[];
  runState: RunState | string;
  caseState: string;
  /** The most recent policy verdict on the case, if there is one. */
  verdict: string | null;
  /** `RunView.error` — the raised type's name, never its message. */
  error: string | null;
}

/** The drawer's RUN line: `run-demo-referral-1 · gate → REQUIRE_HUMAN`. */
export function stageLabelFor(input: StageLabelInput): string {
  const { runId, executedNodes } = input;
  if (input.runState === "ERRORED") return `${runId} · errored${input.error ? ` (${input.error})` : ""}`;
  if (input.caseState === "DONE") return `${runId} · settled`;
  if (input.caseState === "FAILED") return `${runId} · halted`;

  const last = [...executedNodes].reverse().find((node) => NODE_STAGES[node]);
  if (!last) return `${runId} · idle`;
  if (last === "gate" && input.verdict) return `${runId} · gate → ${input.verdict}`;
  // A `plan` reached after a `gate` is the bounded replan cycle, not a first attempt.
  if (last === "plan" && executedNodes.includes("gate")) return `${runId} · plan (replan)`;
  return `${runId} · ${last}`;
}

/** `book_appointment(provider_id=NORTHGATE-CARD, slot_start=2026-09-03T09:30:00)`.
 *
 *  Values are truncated rather than wrapped: an email body is a paragraph, and the drawer row is
 *  a mono line. Empty arguments are dropped — the planner emits every argument a tool declares,
 *  empty ones included, so that the policy engine gets to deny an empty DESTINATION rather than
 *  never see it. That matters to the gate; it is noise here. */
export function actionIntentFor(toolName: string, args: Record<string, string>): string {
  if (!toolName) return "none proposed";
  const rendered = orderedArgs(toolName, args)
    .filter(([, value]) => value !== "")
    .map(([name, value]) => `${name}=${value.length > 44 ? `${value.slice(0, 43)}…` : value}`)
    .join(", ");
  return `${toolName}(${rendered})`;
}

export interface PolicyLine {
  verdict: string;
  reasonCode: string;
  matchedRuleId: string;
}

/** The drawer's POLICY VERDICT row: `DENY ×3 · rule deny-email-to-untrusted-recipient`.
 *
 *  `matched_rule_id` is on the backend's payload allowlist deliberately and for this panel.
 *  Invariant 6 keeps it from the planner because it maps the defence for the component being
 *  tempted; a judge reading the drawer is who the named declarative rule was written for. */
export function policyDetailFor(lines: readonly PolicyLine[]): string {
  if (lines.length === 0) return "pending";

  const latest = lines[lines.length - 1];
  const denials = lines.filter((line) => line.verdict === "DENY").length;
  const rule = latest.matchedRuleId || latest.reasonCode || "default";

  if (latest.verdict === "DENY") {
    return denials > 1 ? `DENY ×${denials} · rule ${rule}` : `DENY · rule ${rule}`;
  }
  return denials > 0 ? `DENY ×${denials} → ${latest.verdict} · rule ${rule}` : `${latest.verdict} · rule ${rule}`;
}

/** The drawer's VERIFICATION row.
 *
 *  The distinction it carries is the whole reason `read_back` is on the payload allowlist:
 *  "verified" is a materially stronger claim when an external system was read than when only
 *  CareLoop's own record was checked, and a panel that could not tell them apart would let the
 *  weaker one be read as the stronger. */
export function verificationDetailFor(passed: boolean | null, readBack: boolean): string {
  if (passed === null) return "pending";
  if (passed) return readBack ? "MATCH — external read-back" : "MATCH — recorded effect checked";
  return readBack ? "MISMATCH — authorized ≠ observed" : "MISMATCH — authorized ≠ recorded";
}

/* ------------------------------------------------------------------ *
 * Pacing
 * ------------------------------------------------------------------ */

/** How long each state the case passed through stays on screen before the next one replaces it. */
export const STATE_DWELL_MS = 550;

/** The most states the display will ever be behind. Past this it skips ahead.
 *
 *  Without it a case that ran while the tab was in the background would replay its whole history
 *  at walking pace on the next poll, which is a stale dashboard rather than a paced one. */
const MAX_LAG = 3;

export interface PacedState {
  shown: string;
  at: number;
}

/** Which state the screen should be showing, given which one the case is actually in.
 *
 *  **The problem this solves, measured.** A real run reaches `DONE` about four seconds after the
 *  scenario is pressed, and approving a parked decision completes in **440ms**. At a one-second
 *  poll the dashboard therefore never observes `EXECUTING` or `VERIFYING` at all: the case goes
 *  from a decision card straight to a completed row, and the two beats the design is built around
 *  — "Making the arrangement…", "Confirming with the clinic…" — do not happen on screen. The
 *  backend is not slow enough to watch.
 *
 *  **What this does, and what it refuses to do.** It walks the display forward one state at a
 *  time, and the states it walks through come from the case's own `STATE_TRANSITION` entries —
 *  every one is a state the case genuinely was in, in the order it was in them. Nothing is
 *  invented and nothing is reordered. What it costs is freshness: the dashboard can be up to
 *  {@link MAX_LAG} × {@link STATE_DWELL_MS} behind the store, which for the approval beat is
 *  about a second. That is a deliberate trade — the alternative is a screen that is exactly
 *  correct and shows nothing.
 *
 *  A case seen for the first time is shown as it is, with no replay: opening the dashboard on a
 *  case that finished yesterday should not animate it. */
export function pacedState(
  previous: PacedState | undefined,
  recorded: readonly string[],
  current: string,
  now: number
): PacedState {
  if (!previous) return { shown: current, at: now };
  if (previous.shown === current) return previous;

  const at = recorded.lastIndexOf(previous.shown);
  // The shown state is not in the recorded run at all — nothing to walk through, so do not.
  if (at < 0) return { shown: current, at: now };
  if (now - previous.at < STATE_DWELL_MS) return previous;

  const remaining = recorded.length - 1 - at;
  const next =
    remaining > MAX_LAG ? recorded[recorded.length - MAX_LAG] : (recorded[at + 1] ?? current);
  return { shown: next, at: now };
}

/* ------------------------------------------------------------------ *
 * Case titles
 * ------------------------------------------------------------------ */

/** The three demo cases have fixed ids (`careloop/api/demo.py`), so they can have real titles.
 *
 *  `Case.summary` is triage's one-line classification, which is a summary and not a name — it
 *  reads like a log line at the top of a page. Anything not in this table falls back to it. */
const TITLES: Record<string, string> = {
  "demo-referral": "Cardiology referral",
  "demo-injection": "Care continuity packet",
  "demo-resume": "Appointment instructions"
};

export function titleFor(caseId: string, summary: string): string {
  return TITLES[caseId] ?? (summary || caseId);
}
