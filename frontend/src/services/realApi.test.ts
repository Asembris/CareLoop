import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CaseCard, CaseDetail, CaseSections, FactView, RunView, TimelineEventView } from "./backend";
import {
  RealCareLoopApi,
  buildCase,
  buildRun,
  evidenceFor,
  latestProposal,
  latestVerification,
  policyLines,
  timelineFor
} from "./realApi";

/* ------------------------------------------------------------------ *
 * Fixtures shaped like what `careloop/api/` actually returns
 * ------------------------------------------------------------------ */

let seq = 0;

function entry(kind: string, payload: Record<string, unknown> = {}, summary = ""): TimelineEventView {
  seq += 1;
  return {
    seq,
    at: "2026-08-13T11:00:00+00:00",
    kind,
    actor: "system",
    summary: summary || kind.toLowerCase(),
    payload
  };
}

function fact(over: Partial<FactView> = {}): FactView {
  return {
    key: "slot_start",
    value: "2026-09-03T09:30:00",
    trust_level: "CONTROLLED",
    citation: "referral.pdf p.2",
    source_doc: "referral.pdf",
    page: 2,
    extractor: "pdfplumber",
    validated_by: "iso_datetime",
    ...over
  };
}

function card(over: Partial<CaseCard> = {}): CaseCard {
  return {
    case_id: "demo-referral",
    state: "PLANNING",
    section: "working",
    summary: "cardiology referral, routine",
    version: 4,
    created_at: "2026-08-13T10:00:00+00:00",
    updated_at: "2026-08-13T11:00:00+00:00",
    failure_reason: null,
    fact_count: 2,
    action_count: 0,
    decision: null,
    ...over
  };
}

function detail(over: Partial<CaseDetail> = {}): CaseDetail {
  const base = card(over as Partial<CaseCard>);
  return {
    case_id: base.case_id,
    state: base.state,
    section: base.section,
    version: base.version,
    event_id: "evt-0001-referral",
    summary: base.summary,
    created_at: base.created_at,
    updated_at: base.updated_at,
    failure_reason: base.failure_reason,
    decision: base.decision,
    facts: [],
    approvals: [],
    actions: [],
    timeline: [],
    ...over
  };
}

const NOW = new Date("2026-08-13T11:00:30Z");

const BOOKING = {
  provider_id: "NORTHGATE-CARD",
  patient_id: "PT-4471",
  slot_start: "2026-09-03T09:30:00",
  reason: "cardiology referral"
};

beforeEach(() => {
  seq = 0;
});

/* ------------------------------------------------------------------ *
 * Named readers
 * ------------------------------------------------------------------ */

describe("reading the timeline", () => {
  it("finds the planner's most recent proposal", () => {
    const timeline = [
      entry("PLAN_PROPOSED", { tool_name: "send_email", args: { to: "a@b.example" } }),
      entry("POLICY_DENY", { tool_name: "send_email", reason_code: "DESTINATION_NOT_TRUSTED" }),
      entry("PLAN_PROPOSED", { tool_name: "book_appointment", args: BOOKING })
    ];
    expect(latestProposal(timeline)).toEqual({ toolName: "book_appointment", args: BOOKING });
    expect(latestProposal([])).toBeNull();
  });

  it("takes the LAST verification, so a superseded pass cannot read as complete", () => {
    // A read-back mismatch routes the case back to PLANNING and re-verifies, so a case can carry
    // a PASSED that a later FAILED has superseded. Taking the optimistic one would be the bug.
    const timeline = [
      entry("VERIFICATION_PASSED", { read_back: "true" }),
      entry("VERIFICATION_FAILED", { read_back: "true" })
    ];
    expect(latestVerification(timeline)).toEqual({ passed: false, readBack: true });
  });

  it("separates an external read-back from a closed-loop check", () => {
    expect(latestVerification([entry("VERIFICATION_PASSED", { read_back: "true" })])).toEqual({
      passed: true,
      readBack: true
    });
    expect(latestVerification([entry("VERIFICATION_PASSED", { read_back: "false" })])).toEqual({
      passed: true,
      readBack: false
    });
  });

  it("collects every gate verdict in order", () => {
    const timeline = [
      entry("POLICY_DENY", { verdict: "DENY", reason_code: "DESTINATION_NOT_TRUSTED", matched_rule_id: "r1" }),
      entry("EVIDENCE_RECORDED", { key: "slot_start" }),
      entry("POLICY_ALLOW", { verdict: "ALLOW", reason_code: "", matched_rule_id: "allow-booking" })
    ];
    expect(policyLines(timeline)).toEqual([
      { verdict: "DENY", reasonCode: "DESTINATION_NOT_TRUSTED", matchedRuleId: "r1" },
      { verdict: "ALLOW", reasonCode: "", matchedRuleId: "allow-booking" }
    ]);
  });
});

/* ------------------------------------------------------------------ *
 * The spine
 * ------------------------------------------------------------------ */

describe("the timeline the spine renders", () => {
  it("folds POLICY_DENY entries into the event they interrupted", () => {
    // The backend records denials as separate top-level entries; the UI hangs them as a dashed
    // branch off the proposal they refused.
    const events = timelineFor([
      entry("PLAN_PROPOSED", { tool_name: "send_email", args: { to: "attacker@evil.example" } }),
      entry("POLICY_DENY", { tool_name: "send_email", reason_code: "DESTINATION_NOT_TRUSTED" }),
      entry("POLICY_DENY", { tool_name: "upload_document", reason_code: "DESTINATION_NOT_TRUSTED" })
    ]);

    expect(events).toHaveLength(1);
    expect(events[0].text).toBe("Next step: send an email");
    expect(events[0].blocked).toEqual([
      { text: "Send an email to a destination nothing approved", rule: "DESTINATION_NOT_TRUSTED" },
      { text: "Upload the document to a destination nothing approved", rule: "DESTINATION_NOT_TRUSTED" }
    ]);
  });

  it("keeps the state machine's narration off the spine", () => {
    const events = timelineFor([
      entry("CASE_CREATED", { event_id: "evt-0001-referral" }),
      entry("STATE_TRANSITION", { from: "NEW", to: "GATHERING_EVIDENCE" }),
      entry("STATE_TRANSITION", { from: "GATHERING_EVIDENCE", to: "PLANNING" })
    ]);
    expect(events.map((e) => e.text)).toEqual(["Case opened"]);
  });

  it("keeps whole-page evidence entries off the spine and real facts on it", () => {
    const events = timelineFor([
      entry("EVIDENCE_RECORDED", { key: "excerpt:doc-0001-referral:p1" }),
      entry("EVIDENCE_RECORDED", { key: "excerpt:doc-0001-referral:p2" }),
      entry("EVIDENCE_RECORDED", { key: "slot_start", trust_level: "UNTRUSTED" }),
      entry("FACT_PROMOTED", { key: "slot_start", to_trust_level: "CONTROLLED" })
    ]);
    expect(events.map((e) => e.text)).toEqual(["Appointment slot found", "Appointment slot validated"]);
  });

  it("says what happened in the register the rest of the screen is in", () => {
    const events = timelineFor([
      entry("APPROVAL_GRANTED", { decided_by: "Maya", tool_name: "book_appointment" }),
      entry("ACTION_EXECUTED", { tool_name: "book_appointment", idempotency_key: "abc" }),
      entry("VERIFICATION_PASSED", { tool_name: "book_appointment", read_back: "true" })
    ]);
    expect(events.map((e) => e.text)).toEqual([
      "Maya approved",
      "Appointment booked",
      "Clinic record checked independently — match"
    ]);
    expect(events.map((e) => e.tone)).toEqual(["decision", "neutral", "verified"]);
  });

  it("falls back to the backend's own summary for a kind it has no line for", () => {
    const events = timelineFor([entry("SOME_FUTURE_KIND", {}, "something new happened")]);
    expect(events.map((e) => e.text)).toEqual(["something new happened"]);
  });
});

/* ------------------------------------------------------------------ *
 * Evidence
 * ------------------------------------------------------------------ */

describe("evidence", () => {
  const page = fact({
    key: "excerpt:doc-0001-referral:p2",
    value:
      "NORTHGATE CARDIOLOGY\nThe patient should be seen by cardiology at 2026-09-03T09:30:00, " +
      "per the referring physician's instruction. Bring insurance details to the first visit."
  });

  it("keeps whole-page facts out of the grid", () => {
    const items = evidenceFor([page, fact()]);
    expect(items).toHaveLength(1);
    expect(items[0].fact).toBe("Appointment slot · Thu Sep 3 · 9:30 AM");
    expect(items[0].trust).toBe("Validated");
    expect(items[0].source).toBe("referral.pdf p.2");
  });

  it("locates the extracted span inside the page it came from", () => {
    const [item] = evidenceFor([page, fact()]);
    expect(item.excerpt.highlight).toBe("2026-09-03T09:30:00");
    expect(item.excerpt.before).toContain("should be seen by cardiology at");
    expect(item.excerpt.after).toContain("referring physician");
  });

  it("degrades to the value alone rather than inventing surrounding text", () => {
    const [item] = evidenceFor([fact()]);
    expect(item.excerpt.highlight).toBe("2026-09-03T09:30:00");
    expect(item.excerpt.before).toBe("");
    expect(item.excerpt.after).toBe("");
  });

  it("never invents a digest, because the backend has none", () => {
    for (const item of evidenceFor([page, fact()])) {
      expect(item.excerpt.digest).toBe("");
    }
  });
});

/* ------------------------------------------------------------------ *
 * A whole case
 * ------------------------------------------------------------------ */

describe("building a case", () => {
  it("seals only a DONE case whose verification read an external system back", () => {
    const done = (readBack: string) =>
      buildCase(
        card({ state: "DONE", section: "completed" }),
        detail({
          state: "DONE",
          section: "completed",
          timeline: [
            entry("PLAN_PROPOSED", { tool_name: "book_appointment", args: BOOKING }),
            entry("VERIFICATION_PASSED", { tool_name: "book_appointment", read_back: readBack })
          ]
        }),
        undefined,
        NOW
      );

    expect(done("true").verified).toBe(true);
    // Correct, and deliberately so: the seal means "an external system agreed", not "finished".
    // A send_email that completed properly is DONE and unsealed.
    expect(done("false").verified).toBe(false);
    expect(done("false").statusText).toBe("Done · checked against what was authorized");
  });

  it("marks a failed verification as a mismatch whatever state the case is in", () => {
    const built = buildCase(
      card({ state: "PLANNING" }),
      detail({
        timeline: [
          entry("PLAN_PROPOSED", { tool_name: "book_appointment", args: BOOKING }),
          entry("VERIFICATION_FAILED", { tool_name: "book_appointment", read_back: "true" })
        ]
      }),
      undefined,
      NOW
    );
    expect(built.mismatch).toBe(true);
    expect(built.verified).toBe(false);
    expect(built.statusLabel).toBe("DID NOT MATCH");
  });

  it("names the failure reason on a FAILED case", () => {
    const built = buildCase(
      card({
        state: "FAILED",
        section: "completed",
        failure_reason: "book_appointment refused at the tool boundary"
      }),
      detail({ state: "FAILED", section: "completed" }),
      undefined,
      NOW
    );
    expect(built.section).toBe("done");
    expect(built.statusText).toContain("book_appointment refused at the tool boundary");
    expect(built.verified).toBe(false);
    expect(built.outcome).toBeUndefined();
  });

  it("says whether the world was read, never what it said", () => {
    const built = buildCase(
      card({ state: "DONE", section: "completed" }),
      detail({
        state: "DONE",
        timeline: [
          entry("PLAN_PROPOSED", { tool_name: "book_appointment", args: BOOKING }),
          entry("VERIFICATION_PASSED", { read_back: "true" })
        ]
      }),
      undefined,
      NOW
    );
    expect(built.readback).toEqual({
      authorized: "Thu Sep 3 · 9:30 AM",
      observed: "Confirmed by an independent read",
      result: "Match — loop closed",
      state: "MATCH"
    });
  });

  it("carries the decision's arguments through, so the card names the call that will be made", () => {
    const built = buildCase(
      card({
        state: "AWAITING_HUMAN",
        section: "needs_you",
        decision: {
          approval_id: "apr-0123456789abcdef",
          tool_name: "book_appointment",
          reason_code: "REQUIRES_HUMAN_APPROVAL",
          reason_text: "This action needs the caregiver's decision. Stop and await it.",
          args: BOOKING,
          requested_at: "2026-08-13T11:00:00+00:00"
        }
      }),
      detail({ state: "AWAITING_HUMAN", section: "needs_you" }),
      undefined,
      NOW
    );

    expect(built.section).toBe("needs");
    expect(built.decision?.id).toBe("apr-0123456789abcdef");
    expect(built.decision?.kind).toBe("REQUIRES_HUMAN_APPROVAL");
    expect(built.decision?.facts).toContainEqual({ label: "WHEN", value: "Thu Sep 3 · 9:30 AM" });
    expect(built.decision?.body).not.toContain("Stop and await it");
  });
});

/* ------------------------------------------------------------------ *
 * A run
 * ------------------------------------------------------------------ */

describe("building a run", () => {
  const run = (over: Partial<RunView> = {}): RunView => ({
    run_id: "run-demo-injection-1",
    case_id: "demo-injection",
    state: "RUNNING",
    executed_nodes: ["dispatch", "triage", "evidence", "plan", "gate"],
    error: null,
    ...over
  });

  it("composes the drawer from RunView plus the case's own timeline", () => {
    const built = buildRun(
      run(),
      detail({
        case_id: "demo-injection",
        state: "PLANNING",
        actions: [
          {
            idempotency_key: "8f1c2d",
            action_type: "book_appointment",
            args_digest: "d",
            result: "{}",
            recorded_at: "2026-08-13T11:00:00+00:00"
          }
        ],
        timeline: [
          entry("PLAN_PROPOSED", { tool_name: "send_email", args: { to: "attacker@evil.example", subject: "x", body: "" } }),
          entry("POLICY_DENY", {
            verdict: "DENY",
            tool_name: "send_email",
            reason_code: "DESTINATION_NOT_TRUSTED",
            matched_rule_id: "deny-email-to-untrusted-recipient"
          })
        ]
      }),
      "injection"
    );

    expect(built.id).toBe("run-demo-injection-1");
    expect(built.scenario).toBe("injection");
    expect(built.stage).toBe("GATE");
    expect(built.policyVerdict).toBe("DENY");
    expect(built.policyDetail).toBe("DENY · rule deny-email-to-untrusted-recipient");
    expect(built.actionIntent).toBe("send_email(to=attacker@evil.example, subject=x)");
    expect(built.idempotencyKey).toBe("8f1c2d");
    expect(built.verification).toBe("PENDING");
  });

  it("leaves the evidence digest empty rather than substituting a citation", () => {
    // Absent from the backend rather than withheld: there is no digest of evidence anywhere in
    // it, and a slot labelled EVIDENCE DIGEST holding something else would imply a value that
    // does not exist. The drawer's row is dropped; this field stays unread.
    expect(buildRun(run(), null, null).evidenceDigest).toBe("");
  });

  it("does not call a parked case settled", () => {
    const built = buildRun(
      run({ state: "SETTLED" }),
      detail({ state: "AWAITING_HUMAN", section: "needs_you" }),
      "referral"
    );
    expect(built.stage).toBe("GATE");
    expect(built.stageLabel).toContain("gate");
  });
});

/* ------------------------------------------------------------------ *
 * The dashboard, over a stubbed backend
 * ------------------------------------------------------------------ */

describe("getDashboard", () => {
  function stubBackend(sections: CaseSections, details: Record<string, CaseDetail>) {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(url);
        if (url.endsWith("/api/cases")) {
          return new Response(JSON.stringify(sections), { status: 200 });
        }
        const id = url.split("/").pop() as string;
        const found = details[decodeURIComponent(id)];
        return found
          ? new Response(JSON.stringify(found), { status: 200 })
          : new Response("", { status: 404 });
      })
    );
    return calls;
  }

  it("flattens three arrays into one list and keeps the section each came in under", async () => {
    const failed = card({ case_id: "demo-injection", state: "FAILED", section: "completed", failure_reason: "clinic refused" });
    const working = card({ case_id: "demo-referral", state: "PLANNING", section: "working" });
    const needs = card({ case_id: "demo-resume", state: "AWAITING_HUMAN", section: "needs_you" });

    stubBackend({ needs_you: [needs], working: [working], completed: [failed] }, {
      "demo-referral": detail({ case_id: "demo-referral" }),
      "demo-injection": detail({ case_id: "demo-injection", state: "FAILED", section: "completed" }),
      "demo-resume": detail({ case_id: "demo-resume", state: "AWAITING_HUMAN", section: "needs_you" })
    });

    const state = await new RealCareLoopApi().getDashboard();
    expect(state.cases.map((c) => [c.id, c.section])).toEqual([
      ["demo-resume", "needs"],
      ["demo-referral", "working"],
      // A FAILED case is filed under `completed` by the backend on purpose — `completed` means
      // "the graph will move this no further", not "this went well" — and is told apart by state.
      ["demo-injection", "done"]
    ]);
    expect(state.cases[2].statusText).toContain("clinic refused");
  });

  it("re-reads a case only when its version has moved", async () => {
    const first = card({ version: 4 });
    const calls = stubBackend({ needs_you: [], working: [first], completed: [] }, {
      "demo-referral": detail({ version: 4 })
    });

    const api = new RealCareLoopApi();
    await api.getDashboard();
    await api.getDashboard();

    // Two dashboard reads, one detail read: the case did not move, so nothing was re-fetched.
    expect(calls.filter((url) => url.endsWith("/api/cases"))).toHaveLength(2);
    expect(calls.filter((url) => url.includes("/api/cases/demo-referral"))).toHaveLength(1);
  });

  it("keeps the screen up when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));
    const state = await new RealCareLoopApi().getDashboard();
    expect(state.cases).toEqual([]);
  });
});
