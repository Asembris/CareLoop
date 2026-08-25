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
  Boundaries,
  CapabilityStanding,
  CaseSection,
  DecisionFact,
  DecisionKind,
  EventTone,
  ProvenanceStep,
  RecoveryProof,
  RunStage
} from "@/types/domain";
import type { BoundariesView, CaseState, FactView, RunState } from "./backend";

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
  /* `WAITING_EXTERNAL` means one exact thing — a request that was really sent and never answered
     — so the copy can say it, and the old line did not. "Waiting for the other side" reads like a
     polite queue; what has actually happened is that CareLoop asked an outside system to do
     something, got no answer at all, and does not know whether it happened.

     It deliberately does not promise that nothing will be sent again, because something will be:
     reconciliation re-issues the *same frozen booking identity*, which is a lookup that also
     repairs. What it promises is the true and stronger thing — the identity does not change, and
     nothing is concluded until the other system's own record has been read. See
     `tests/test_recovery_copy.py` in the backend, which fails if any of this copy starts claiming
     CareLoop will not send the request again. */
  WAITING_EXTERNAL: {
    statusLabel: "NO ANSWER FROM THE CLINIC",
    statusText: "Sent, with no answer back",
    headline: "The booking was sent and no answer came back"
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

/** The fact's actual provenance journey, for the lens.
 *
 *  This replaced a static three-line legend that was identical for every fact — and therefore
 *  wrong for most of them: a trusted-config fact captioned "Document content — untrusted" is the
 *  one surface that exists to explain provenance explaining it incorrectly. Each step states what
 *  is true of *this* fact; the closing line is the same for every fact because it is the one
 *  statement that is: nothing a fact says is ever a source of authority. */
export function provenanceJourney(
  fact: Pick<FactView, "trust_level" | "citation" | "page" | "validated_by">
): ProvenanceStep[] {
  const steps: ProvenanceStep[] = [];

  // `page === null` is what says "not from a document". Not an empty `source_doc`: configured
  // facts carry the sentinel `"trusted-config"` there — measured against the live API, where
  // keying on the doc name mislabelled every config fact as document content, which is precisely
  // the defect this function exists to remove. Extraction is per-page, so a document fact always
  // has one.
  if (fact.page === null) {
    steps.push(
      fact.trust_level === "TRUSTED"
        ? { tone: "trusted", text: "Configured by you — trusted" }
        : { tone: "controlled", text: `Recorded by CareLoop — ${trustWord(fact.trust_level).toLowerCase()}` }
    );
  } else {
    steps.push({ tone: "untrusted", text: `Found in ${fact.citation} — document content, untrusted` });
    if (fact.trust_level === "CONTROLLED") {
      steps.push({
        tone: "controlled",
        text: fact.validated_by
          ? `Validated (${fact.validated_by}) — usable as fact`
          : "Validated — usable as fact"
      });
    } else {
      steps.push({ tone: "muted", text: "Not validated — read as context, never used in an action" });
    }
  }

  steps.push({ tone: "muted", text: "Never a source of authority" });
  return steps;
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
  // Neutral on purpose. A frozen action identity is a precaution, not an alarm, and colouring it
  // would make an ordinary booking look eventful on every case that never needs recovering.
  ACTION_PREPARED: "neutral",
  EXTERNAL_RECHECK_REQUESTED: "decision",
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
 *  which is where the UI hangs its dashed blocked branch.
 *
 *  **Two transitions are the exception, and `recoveryTransitionText` is where they are named.**
 *  They are not the machine narrating itself: the backend writes no other timeline entry for
 *  either of them, so those two rows *are* the record of what happened. This function still says
 *  no to every `STATE_TRANSITION`, because it only sees a kind; `timelineFor` asks the other
 *  question first. */
export function isRenderedOnTheSpine(kind: string): boolean {
  return kind !== "STATE_TRANSITION" && kind !== "POLICY_DENY";
}

/** The two state changes the spine renders, keyed on the closed enum and never on free text.
 *
 *  `EXECUTING -> WAITING_EXTERNAL` is the only durable record that a request was sent and no answer
 *  came back: `_stop_with_the_outcome_unestablished` writes no timeline entry of its own on that
 *  branch. `WAITING_EXTERNAL -> EXECUTING` is the only record that the frozen attempt was
 *  re-claimed — written by `ReconcileNode` alone, which the backend sweeps for.
 *
 *  The transition payload also carries a `reason`, and nothing here reads it. It is free text a
 *  backend edit could reword at any time; `from` and `to` are members of a closed enum.
 *
 *  **The second line says "retrying the same booking", not "not booking again".** Reconciliation
 *  really does issue another request — under the identity frozen before the first one, which is
 *  what makes the provider return the appointment it already made instead of creating a second.
 *  Copy claiming CareLoop does not send again would be false; the backend's
 *  `tests/test_recovery_copy.py` is what fails if it appears. */
const RECOVERY_TRANSITIONS: Record<string, string> = {
  "EXECUTING>WAITING_EXTERNAL": "No answer came back — CareLoop does not yet know whether it happened",
  "WAITING_EXTERNAL>EXECUTING": "Retrying the same frozen booking identity — never a new booking"
};

export function recoveryTransitionText(from: string, to: string): string | null {
  return RECOVERY_TRANSITIONS[`${from}>${to}`] ?? null;
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
  // Never reached in practice: the last four are carried by a MANUAL_HANDOFF_REQUIRED entry and
  // never by a POLICY_DENY, because nothing was refused — an action was carried out and could not
  // be confirmed, its outcome was never established, no action could be formed at all, or the case
  // was about somebody this caregiver may not act for. Listed so the map stays total over the
  // backend's vocabulary rather than falling through to a bare tool phrase if a future writer ever
  // does deny with one.
  //
  // It was **not** total until My Boundaries went looking: the last two were missing while the
  // comment above them claimed completeness. See `decisionCopy` for the one that mattered.
  EXTERNAL_STATE_NOT_CONFIRMED: "and could not be confirmed afterwards",
  EXTERNAL_OUTCOME_NOT_ESTABLISHED: "and CareLoop never learned whether it happened",
  NO_VALID_PROPOSAL_PRODUCED: "before it could be written down",
  PATIENT_AUTHORITY_DENIED: "for somebody your access does not currently cover"
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
  /** The label for the non-authorising re-observation affordance.
   *
   *  Present on every copy variant so the type stays total, and rendered only when the backend
   *  says the case is actually re-checkable — see `DecisionView.recheckable`. */
  recheckLabel: string;
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
      // The old line said "CareLoop will not retry", which stopped being true the moment a
      // caregiver could ask for another observation — and the distinction it now has to carry is
      // the whole patch. CareLoop will not repeat the *action*; it can be asked to repeat the
      // *look*. Getting that backwards in either direction is the dangerous copy: promising no
      // further contact while a re-check exists, or implying the booking might be made again.
      after:
        "CareLoop will not repeat the action. If you have checked or corrected the other " +
        "system, you can ask it to look again.",
      approveLabel: "Approve",
      rejectLabel: "Reject",
      recheckLabel: "Check the clinic again"
    };
  }

  // The third reason a case stops with nothing to approve, and it is not the one below either.
  // That one says every action was refused *by policy*; this one says no action was ever formed,
  // so there was nothing for policy to refuse. Using the wrong one would tell a caregiver a
  // defence fired when what actually happened is that CareLoop could not write down a next step —
  // and would send them looking for a rule that never matched.
  //
  // **Nothing here offers a way forward inside CareLoop, because there is not one.** There is no
  // approval to grant, no external record to look at again, and no button that asks CareLoop to
  // try again. Copy implying any of those would be the small lie a decided card refuses to tell.
  if (reasonCode === "NO_VALID_PROPOSAL_PRODUCED") {
    return {
      title: "CareLoop could not work out a safe next step",
      body:
        "CareLoop tried twice to work out a valid next action here and could not. It stopped " +
        "rather than guess. Nothing was sent, booked or changed, and nothing will be done " +
        "automatically — this one needs a person.",
      facts,
      after:
        "Nothing was changed. CareLoop will not try again on its own, and there is nothing here " +
        "to approve — this case has to be picked up by hand.",
      approveLabel: "Approve",
      rejectLabel: "Reject",
      recheckLabel: "Check the clinic again"
    };
  }

  // The fourth reason a case stops with nothing to approve, and the one that was being described
  // wrongly. It has nothing to do with policy refusing a call: `graph/nodes.py` asks the patient
  // boundary *before* an intent is durably written, so nothing was proposed, nothing was refused,
  // nothing was prepared and no external system was reached. Falling through to the card below told
  // a caregiver "every action CareLoop could take here was refused by its own policy", which would
  // send them looking for a rule that never matched — and would hide the only fact that matters,
  // which is that this case is about somebody they may not currently act for.
  if (reasonCode === "PATIENT_AUTHORITY_DENIED") {
    return {
      title: "This case is outside your access",
      body:
        "CareLoop checked who this case is about before doing anything, and your access does not " +
        "currently cover them. Nothing was sent, booked or changed, and no outside system was " +
        "contacted.",
      facts,
      after:
        "Nothing was changed. CareLoop will not work on this case while that is true, and there " +
        "is nothing here to approve — access is granted outside CareLoop.",
      approveLabel: "Approve",
      rejectLabel: "Reject",
      recheckLabel: "Check the clinic again"
    };
  }

  // The fifth, and the counterpart to EXTERNAL_STATE_NOT_CONFIRMED above rather than a synonym for
  // it. That one means CareLoop holds a receipt an independent read could not confirm, and asks
  // *is the appointment right*. This one means CareLoop holds no receipt at all, and asks *does an
  // appointment exist*. Different errands, so different words.
  if (reasonCode === "EXTERNAL_OUTCOME_NOT_ESTABLISHED") {
    return {
      title: "CareLoop doesn’t know whether this happened",
      body:
        "CareLoop sent this and no answer ever came back, so it cannot tell whether it was " +
        "carried out. Rather than assume either way — and rather than do it a second time — it " +
        "has stopped and left the question to you.",
      facts,
      after:
        "Nothing further happens automatically. Please check the other system for a record of " +
        "this before anything else is tried.",
      approveLabel: "Approve",
      rejectLabel: "Reject",
      recheckLabel: "Check the clinic again"
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
      rejectLabel: "Reject",
      recheckLabel: "Check the clinic again"
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
    rejectLabel: "Not now",
    recheckLabel: "Check the clinic again"
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
export function observedLabel(readBack: boolean, observation = ""): string {
  switch (observation) {
    case "MATCHED":
      return "Confirmed by an independent read";
    case "DIVERGED":
      return "The clinic's record disagrees";
    case "ABSENT":
      return "The clinic holds no such record";
    case "UNAVAILABLE":
      return "The clinic could not be read";
    case "NOT_OBSERVED":
      return "Not read back";
  }
  // A case recorded before the vocabulary existed carries no `observation`; the two-way label
  // is still true and is all that can honestly be said about it.
  return readBack ? "Confirmed by an independent read" : "Not read back";
}

/** The RESULT column. */
export function readbackResult(passed: boolean | null): string {
  if (passed === null) return "Pending";
  return passed ? "Match — loop closed" : "No match";
}

/** One honest sentence per member of `careloop.graph.verify.ObservationOutcome`.
 *
 *  The five words are CareLoop's own closed vocabulary — chosen names, never values an external
 *  system supplied, which is why the payload key is allowlisted where the diagnostic beside it is
 *  withheld. The distinctions matter enough to spell out: `ABSENT` is the clinic *contradicting*
 *  the receipt CareLoop holds, where `UNAVAILABLE` established nothing; `NOT_OBSERVED` is a fact
 *  about CareLoop's wiring, not a claim about the world, and a simulated run reports it plainly
 *  rather than pretending a read happened. */
export const OBSERVATION_COPY: Record<string, string> = {
  MATCHED: "An independent read of the clinic's own record agrees with every value CareLoop executed.",
  DIVERGED: "An independent read succeeded — and the clinic's record disagrees with what was executed.",
  ABSENT: "The clinic answered that it holds no record of this action.",
  UNAVAILABLE: "The clinic was asked and could not be read. Nothing was established either way.",
  NOT_OBSERVED: "No independent observer took part — CareLoop checked its own recorded effect, and says so."
};

export function observationCopy(observation: string): string {
  return OBSERVATION_COPY[observation] ?? "";
}

/** Why the observed *values* never render, stated where a reader will wonder.
 *
 *  The backend withholds read-back text wholesale
 *  (`tests/test_verify_readback.py::TestTheReadBackIsNotAnInjectionChannel`), and the reason
 *  belongs on screen: the withholding is the trust model applying to CareLoop's own UI. */
export const OBSERVED_VALUES_WITHHELD =
  "What the clinic actually said is never shown, deliberately: text from an external system is " +
  "untrusted everywhere in CareLoop, and this screen is not an exception.";

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
  // A reconciliation is the executor doing the same job on a later run: it re-issues the action the
  // case already froze. Mapping it to `EXECUTE` keeps the drawer's agent path honest — without it a
  // live recovery run showed `IDLE`, which is the one thing it certainly was not.
  reconcile: "EXECUTE",
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
 * The recovery evidence block
 * ------------------------------------------------------------------ */

/** The heading over a settled recovery.
 *
 *  **Both halves are load-bearing and the second is where the honesty is.** "One appointment" is
 *  the outcome; "same action safely replayed" is *how*, and it says plainly that the action was
 *  replayed. The tidier heading — "recovered without sending the booking twice" — is false: the
 *  measured shape is two HTTP requests, one action identity, one appointment. The backend's
 *  `tests/test_recovery_proof.py` measures it and `tests/test_recovery_copy.py` sweeps this file
 *  for any sentence that denies it. */
export const RECOVERY_PROOF_TITLE = "ONE APPOINTMENT — SAME ACTION SAFELY REPLAYED";

export const RECOVERY_PROOF_BODY =
  "The clinic accepted the booking and CareLoop never got the answer. It reused the exact action " +
  "identity it had frozen beforehand, so the clinic returned the appointment it already had " +
  "instead of making a second one — then CareLoop checked that record itself before finishing.";

export interface RecoveryProofRow {
  label: string;
  value: string;
}

/** The four rows of the block, each one a durable fact the API already projects.
 *
 *  **"Plans made" is deliberately not "model calls".** One persisted plan is what the trail can
 *  support; it does not establish that no model was invoked and failed without producing one. The
 *  stronger claim — zero planner, triage and model calls during recovery — is proven in the backend
 *  with seams that raise if touched, and it belongs in the tests and the documentation rather than
 *  on a screen that would be asserting something its own evidence cannot reach. */
export function recoveryProofRows(proof: RecoveryProof): RecoveryProofRow[] {
  return [
    { label: "ACTION IDENTITY", value: `${proof.actionKey} · frozen before the first attempt` },
    { label: "RECORDED ACTIONS", value: String(proof.recordedActions) },
    { label: "PLANS MADE", value: `${proof.plans} · no new plan after the answer was lost` },
    {
      label: "CONFIRMED BY",
      value: `an independent read of the clinic's own record · ${proof.observation}`
    }
  ];
}

/* ------------------------------------------------------------------ *
 * The adversarial boundary test
 * ------------------------------------------------------------------ */

/** The rationale CareLoop writes on every proposal it supplies for the adversarial test.
 *
 *  **Byte-identical to `ADVERSARIAL_RATIONALE` in `careloop/api/adversary.py`**, and
 *  `tests/test_adversarial_demo.py` reads this file to say so — a cross-language constant with a
 *  test rather than two strings that drift.
 *
 *  This is the *only* thing the framing note below is allowed to key on, and the reason is the
 *  whole honesty of the feature. A note saying "CareLoop supplied this proposal" must appear over
 *  a case where CareLoop really did, and must not appear over one where a real planner produced
 *  the same action — which is a thing the realistic scenario can genuinely do. The scenario id the
 *  browser remembers cannot tell those apart; a durable rationale CareLoop wrote can. */
export const SUPPLIED_PROPOSAL_RATIONALE =
  "Supplied by CareLoop's adversarial test: this is the action the notice asks the agent to take.";

export const ADVERSARIAL_NOTE_LABEL = "ADVERSARIAL TEST";

export const ADVERSARIAL_NOTE_BODY =
  "The attached notice contains an instruction addressed to the agent. For this test, CareLoop " +
  "deliberately supplies the dangerous proposal the attacker wants, instead of waiting to see " +
  "whether a planner would. Everything after the proposal is the ordinary CareLoop system: the " +
  "same policy engine, the same rules, the same audit trail.";

/** Why the refusal happening *before* the approval step is the interesting part.
 *
 *  Every outbound email is a caregiver decision under `require-human-for-outbound-email`. `DENY` is
 *  evaluated first and unconditionally, so an attacker-supplied recipient never reaches that rule
 *  and no person is ever shown the choice. Stated because a reader who does not know the ordering
 *  would reasonably expect to be asked. */
export const ADVERSARIAL_NOT_ASKED =
  "CareLoop did not ask you to approve this. The destination was never eligible for your approval, " +
  "so the question was never put to you.";

export const ZERO_EFFECT_LABEL = "EXTERNAL ACTIONS PERFORMED";

/** Every clause here is a durable fact the API already projects, and no clause claims more.
 *
 *  **There is deliberately no provider-call count.** The browser receives the case, not the
 *  clinic's request log, so a line reading "outside systems contacted: 0" would be asserting
 *  something this page cannot see. The stronger internal statement — zero handler invocations,
 *  an empty executor ledger — is proven in `tests/test_adversarial_demo.py`, which can observe it.
 *
 *  "Nothing was prepared" is `ACTION_PREPARED`'s absence, which is the sharper of the two: it says
 *  CareLoop never reached the point of writing down an attempt, not merely that no attempt
 *  finished. "Execution never began" is the absence of any transition into `EXECUTING`. */
export const ZERO_EFFECT_BODY =
  "No action was prepared and none was recorded. The proposal was refused before execution could " +
  "begin, so nothing left CareLoop.";

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
  // The same poisoned notice as 02's realistic variant, and the title has to say which row is
  // which for the same reason `demo-recovery` does — a judge may well have both on screen.
  "demo-adversarial": "Care continuity packet · boundary test",
  "demo-resume": "Appointment instructions",
  // The same referral as 01, which is the point of the scenario — so the title has to say which
  // row is which, or a judge with both on screen cannot tell them apart.
  "demo-recovery": "Cardiology referral · lost reply"
};

export function titleFor(caseId: string, summary: string): string {
  return TITLES[caseId] ?? (summary || caseId);
}

/* ------------------------------------------------------------------ *
 * My Boundaries
 * ------------------------------------------------------------------ */

/* Copy over values the backend derived, and nothing else.
 *
 * Every standing, requirement, availability and refusal below is a *lookup* keyed on a closed
 * backend enum. Nothing here reads `rules.yaml`, resolves a trust level, compares an argument or
 * decides whether something is reachable — `careloop/policy/boundaries.py` does all of that and
 * `tests/test_boundaries.py` proves it against the real policy engine. A function here that
 * computed a standing would be the second source of truth this feature was built to delete, and it
 * would be *invisible*, because it would look right on the shipped configuration.
 *
 * The one rule the copy itself must keep: **"currently", not "never".** `UNAVAILABLE` is a fact
 * about this caregiver's configuration, not about the product, and a sentence implying a permanent
 * limitation would claim more than the backend derived. */

/** What each standing means for the caregiver reading it. */
const STANDING_SUMMARY: Record<string, string> = {
  AUTONOMOUS: "CareLoop does this on its own, once everything below is true.",
  REQUIRES_HUMAN:
    "CareLoop never does this by itself. It shows you the exact action and waits; approving covers " +
    "that one action and nothing else.",
  UNAVAILABLE: "CareLoop cannot do this under your current boundaries."
};

/** The short banner word for each standing. Never the raw enum. */
const STANDING_LABEL: Record<string, string> = {
  AUTONOMOUS: "ON ITS OWN",
  REQUIRES_HUMAN: "ASKS YOU FIRST",
  UNAVAILABLE: "CURRENTLY UNAVAILABLE"
};

export function standingLabel(standing: string): string {
  return STANDING_LABEL[standing] ?? "UNKNOWN";
}

export function standingSummary(standing: string): string {
  return STANDING_SUMMARY[standing] ?? "CareLoop cannot describe this action right now.";
}

/** A trust key as a caregiver reads it, for the sentence explaining an unmet requirement. */
const TRUSTED_THING: Record<string, string> = {
  role: "a trusted role",
  caregiver_email: "a trusted address",
  provider_id: "a trusted provider",
  portal_id: "a trusted portal",
  patient_id: "a trusted patient record",
  document_id: "a trusted document",
  slot_start: "a trusted appointment time"
};

function trustedThing(key: string): string {
  return TRUSTED_THING[key] ?? `a trusted ${key.replace(/_/g, " ")}`;
}

/** A readable list: "a", "a and b", "a, b and c". */
function listed(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** Why a capability is out of reach, scoped to the current configuration.
 *
 *  **The scope is the whole point.** `NO_TRUSTED_SOURCE` says nothing this caregiver holds
 *  configures the fact an argument needs; a different configuration answers differently, and the
 *  backend derives it per caller. Copy reading "CareLoop can never do this" would state a product
 *  limitation the backend did not derive — and would be wrong the day somebody configures one. */
export function unavailableReason(because: string | null, unmetKeys: string[]): string {
  if (because === "NO_TRUSTED_SOURCE") {
    const missing = listed(unmetKeys.map(trustedThing)) || "a trusted value it needs";
    return (
      `Your current configuration establishes no ${missing}. Nothing else can supply it — not a ` +
      `document, not an approval — so CareLoop refuses this action rather than acting on a value ` +
      `it cannot trace.`
    );
  }
  if (because === "NO_ROUTE_TO_A_DECISION") {
    return (
      "Authorising this would need your approval, and nothing in the active policy ever asks for " +
      "one. With no way to obtain it, every attempt is refused."
    );
  }
  if (because === "NO_AUTHORISING_RULE") {
    return "No rule in the active policy permits this, so it is refused by default.";
  }
  return "CareLoop cannot currently authorise this action.";
}

/** The row label for one argument. Semantic, so the card reads as a sentence rather than a form. */
const CONDITION_LABELS: Record<string, Record<string, string>> = {
  book_appointment: {
    provider_id: "Only with",
    patient_id: "Only for",
    slot_start: "Only at",
    reason: "Reason"
  },
  send_email: { to: "Only to", subject: "Subject", body: "Message" },
  upload_document: { portal_id: "Only to", document_id: "Only the" },
  grant_portal_access: { grantee: "Only to", role: "Only with", portal_id: "Only on" }
};

const CLASS_LABELS: Record<string, string> = {
  DESTINATION: "Only to",
  AUTHORITY: "Only with",
  VALUE: "Only when"
};

export function conditionLabel(toolName: string, argName: string, argClass: string): string {
  return (
    CONDITION_LABELS[toolName]?.[argName] ??
    CLASS_LABELS[argClass] ??
    argName.replace(/_/g, " ")
  );
}

/** What must be true of one argument, in plain language.
 *
 *  Keyed on the backend's own answers — the argument class, the trust it requires, and whether
 *  CareLoop composes the value itself. An argument the rules require nothing of says so; inventing
 *  a requirement for it would assert a guard that does not exist, which is exactly the defect
 *  `required_trust: null` was made able to express. */
export function conditionRequirement(
  argClass: string,
  requiredTrust: string | null,
  authored: boolean,
  standing: string
): string {
  if (authored) return "written by CareLoop, not by the model — this exact wording, every time";
  if (requiredTrust === "TRUSTED") {
    return argClass === "AUTHORITY"
      ? "powers that came from your configuration — never any a document claimed"
      : "a destination from your configuration — never one a document named";
  }
  if (requiredTrust === "CONTROLLED") {
    return "a value your own documents established and CareLoop checked before using it";
  }
  if (requiredTrust === "UNTRUSTED") return "no particular provenance";
  return standing === "REQUIRES_HUMAN"
    ? "free text — you read the exact wording before you approve it"
    : "nothing in the active policy constrains this field";
}

/** What confirms the effect afterwards. Two materially different claims, kept apart. */
export function afterwardsCopy(independentlyObserved: boolean): string {
  return independentlyObserved
    ? "Afterwards CareLoop reads the other system's own record and compares it, field by field, " +
        "against what it was authorised to do. The case cannot finish unless they agree."
    : "Afterwards CareLoop checks what it did against what was authorised. This effect is " +
        "simulated, so nothing outside CareLoop confirms it — and the case says so.";
}

/** What happens when a provider's answer never arrives.
 *
 *  **The replayable sentence is written carefully and must stay that way.** Reconciliation really
 *  does issue another request; what it cannot do is create a second appointment, because the
 *  identity was frozen before the first one left. Copy denying the second request would be false
 *  about the one part of this system whose value is that it does not lie about what happened.
 *  `tests/test_recovery_copy.py` sweeps this file for exactly that family of sentence. */
export function recoveryCopy(recoveryClass: string): string {
  return recoveryClass === "IDEMPOTENT_REPLAYABLE"
    ? "If the answer is lost, CareLoop asks again under the action identity it froze beforehand, " +
        "so the provider hands back the record it already made. No second appointment is created."
    : "If the answer is lost, CareLoop stops and hands the case to you. It will not try this " +
        "effect a second time on its own.";
}

/** The standing refusals, in the caregiver's words. Emitted only when the backend derived them. */
const REFUSAL_COPY: Record<string, string> = {
  UNTRUSTED_DESTINATION:
    "Send anything to an address or system that appears only inside a document.",
  UNEARNED_VALUE_TRUST: "Act on a detail a document asserted that nothing has checked.",
  DOCUMENT_AUTHORITY: "Treat an instruction found in a document as permission to act.",
  SECOND_RECORDED_EFFECT: "Record a second effect for an action it has already carried out."
};

export function refusalCopy(principle: string): string {
  return REFUSAL_COPY[principle] ?? "";
}

/** The backend's boundary statement, dressed for a caregiver.
 *
 *  A pure mapping: every capability, in the order the backend sent them, with copy applied and
 *  **no field recomputed**. A capability the build has never heard the standing of still renders —
 *  it keeps its backend standing string and gets the default summary — because a boundary a browser
 *  cannot describe is still a boundary and dropping it would silently shorten the list.
 *
 *  A refusal principle this build has no copy for is dropped instead, and that asymmetry is
 *  deliberate: a capability has a tool name and conditions worth showing without its summary, while
 *  a refusal *is* its sentence and an empty one would render as a bullet with nothing beside it. */
export function boundariesFor(view: BoundariesView): Boundaries {
  return {
    capabilities: view.capabilities.map((capability) => ({
      toolName: capability.tool_name,
      title: toolPhrase(capability.tool_name),
      standing: capability.standing as CapabilityStanding,
      summary: standingSummary(capability.standing),
      ...(capability.standing === "UNAVAILABLE"
        ? {
            unavailableReason: unavailableReason(
              capability.unavailable_because,
              capability.unmet_trust_keys
            )
          }
        : {}),
      conditions: capability.arguments.map((argument) => ({
        name: argument.name,
        label: conditionLabel(capability.tool_name, argument.name, argument.arg_class),
        requirement: conditionRequirement(
          argument.arg_class,
          argument.required_trust,
          argument.authored_value !== null,
          capability.standing
        ),
        ...(argument.configured_value ?? argument.authored_value
          ? { value: (argument.configured_value ?? argument.authored_value) as string }
          : {}),
        ...(argument.required_trust ? { trustLabel: trustWord(argument.required_trust) } : {}),
        argClass: argument.arg_class,
        requiredTrust: argument.required_trust,
        trustKey: argument.trust_key
      })),
      afterwards: afterwardsCopy(capability.independently_observed),
      ifTheAnswerIsLost: recoveryCopy(capability.recovery_class)
    })),
    refusals: view.refusals.map(refusalCopy).filter(Boolean)
  };
}
