import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentRun } from "@/types/domain";
import type { CaseCard, CaseDetail, CaseSections, FactView, RunView, TimelineEventView } from "./backend";
import { clockTime } from "./presentation";
import {
  RESET_ATTEMPTS,
  RESET_RETRY_MS,
  RealCareLoopApi,
  SLOWEST_OBSERVED_RUN_MS,
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

  it("says CareLoop stopped, in its own words rather than the backend's", () => {
    // The end of the strongest story the product tells: every action refused, nothing done, a
    // person needed. It is its own timeline kind precisely so it does not read as "Decision
    // requested" — nothing here was requested, and there is no approval behind it to grant.
    const events = timelineFor([
      entry("PLAN_PROPOSED", { tool_name: "send_email", args: { to: "attacker@evil.example" } }),
      entry("POLICY_DENY", { tool_name: "send_email", reason_code: "DESTINATION_NOT_TRUSTED" }),
      entry("MANUAL_HANDOFF_REQUIRED", { reason_code: "NO_SAFE_ACTION_AVAILABLE" })
    ]);

    expect(events.map((e) => e.text)).toEqual([
      "Next step: send an email",
      "CareLoop stopped — manual follow-up needed"
    ]);
    expect(events.map((e) => e.text)).not.toContain("Decision requested");
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

  it("prints the time only when it changes", () => {
    // A whole run finishes inside one minute, so every entry carries the same HH:MM and the rail
    // becomes a column of one repeated number that reads as a rendering fault.
    const one = { ...entry("CASE_CREATED"), at: "2026-08-13T11:00:01+00:00" };
    const two = { ...entry("ACTION_EXECUTED", { tool_name: "send_email" }), at: "2026-08-13T11:00:04+00:00" };
    const three = { ...entry("VERIFICATION_PASSED", { read_back: "false" }), at: "2026-08-13T11:02:00+00:00" };

    expect(timelineFor([one, two, three]).map((e) => e.time)).toEqual([
      clockTime(one.at),
      "",
      clockTime(three.at)
    ]);
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
          requested_at: "2026-08-13T11:00:00+00:00",
          recheckable: false
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

/* ------------------------------------------------------------------ *
 * Polling
 * ------------------------------------------------------------------ */

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
}

/** A backend that answers from whatever `sections`/`details`/`runs` currently hold, so a test can
 *  move the world between polls the way a real run does. */
function fakeBackend() {
  const calls: Call[] = [];
  const world = {
    sections: { needs_you: [], working: [], completed: [] } as CaseSections,
    details: {} as Record<string, CaseDetail>,
    runs: {} as Record<string, RunView>,
    status: {} as Record<string, number>,
    calls
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        url,
        method: init?.method ?? "GET",
        headers: (init?.headers ?? {}) as Record<string, string>
      });

      const forced = world.status[url];
      if (forced && forced !== 202) return new Response("", { status: forced });

      if (url.endsWith("/api/cases")) return new Response(JSON.stringify(world.sections), { status: 200 });
      if (url.includes("/decisions/")) {
        const view: RunView = {
          run_id: "run-demo-referral-1",
          case_id: "demo-referral",
          state: "RUNNING",
          executed_nodes: ["dispatch"],
          error: null
        };
        return new Response(JSON.stringify(view), { status: 202 });
      }
      if (url.startsWith("/api/cases/")) {
        const id = decodeURIComponent(url.split("/")[3].split("?")[0]);
        const found = world.details[id];
        return found ? new Response(JSON.stringify(found), { status: 200 }) : new Response("", { status: 404 });
      }
      if (url.startsWith("/api/runs/")) {
        const id = decodeURIComponent(url.split("/")[3]);
        const found = world.runs[id];
        return found ? new Response(JSON.stringify(found), { status: 200 }) : new Response("", { status: 404 });
      }
      if (url.startsWith("/api/demo/")) {
        const view: RunView = {
          run_id: "run-demo-referral-1",
          case_id: "demo-referral",
          state: "RUNNING",
          executed_nodes: ["dispatch"],
          error: null
        };
        return new Response(JSON.stringify(url.includes("reset") ? {} : view), { status: 202 });
      }
      return new Response("", { status: 404 });
    })
  );

  return world;
}

function working(): CaseCard {
  return card({ state: "EXECUTING", section: "working" });
}

function settled(): CaseCard {
  return card({ state: "DONE", section: "completed" });
}

describe("subscribe", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  // `restoreMocks` does not put the clock back, and a fake clock leaking into the next describe
  // makes an `await sleep(...)` hang until the suite times out.
  afterEach(() => {
    vi.useRealTimers();
  });

  it("emits immediately rather than making the first screen wait a second", async () => {
    const world = fakeBackend();
    world.sections = { needs_you: [], working: [working()], completed: [] };
    world.details["demo-referral"] = detail({ state: "EXECUTING" });

    const seen: string[] = [];
    const api = new RealCareLoopApi();
    api.subscribe((state) => seen.push(state.cases[0]?.statusLabel ?? "none"));

    await vi.advanceTimersByTimeAsync(0);
    expect(seen[0]).toBe("ACTING");
  });

  it("keeps polling while a case is still moving", async () => {
    const world = fakeBackend();
    world.sections = { needs_you: [], working: [working()], completed: [] };
    world.details["demo-referral"] = detail({ state: "EXECUTING" });

    const api = new RealCareLoopApi();
    api.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(6000);

    expect(api.isPolling).toBe(true);
    const reads = world.calls.filter((c) => c.url === "/api/cases").length;
    expect(reads).toBeGreaterThan(4);
  });

  it("stops polling once nothing is moving", async () => {
    const world = fakeBackend();
    world.sections = { needs_you: [], working: [working()], completed: [] };
    world.details["demo-referral"] = detail({ state: "EXECUTING" });

    const api = new RealCareLoopApi();
    api.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.isPolling).toBe(true);

    // The run finishes: the case moves out of the working section.
    world.sections = { needs_you: [], working: [], completed: [settled()] };
    world.details["demo-referral"] = detail({ state: "DONE", section: "completed" });

    await vi.advanceTimersByTimeAsync(6000);
    expect(api.isPolling).toBe(false);

    const before = world.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(world.calls.length).toBe(before);
  });

  it("stops when a case parks for a decision, because a person is the next mover", async () => {
    const world = fakeBackend();
    world.sections = { needs_you: [card({ state: "AWAITING_HUMAN", section: "needs_you" })], working: [], completed: [] };
    world.details["demo-referral"] = detail({ state: "AWAITING_HUMAN", section: "needs_you" });

    const api = new RealCareLoopApi();
    api.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(8000);
    expect(api.isPolling).toBe(false);
  });

  it("never passes ?wait=, which would block until settle and empty the Working column", async () => {
    const world = fakeBackend();
    world.sections = { needs_you: [], working: [working()], completed: [] };
    world.details["demo-referral"] = detail({ state: "EXECUTING" });

    const api = new RealCareLoopApi();
    api.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(4000);

    expect(world.calls.every((c) => !c.url.includes("wait="))).toBe(true);
  });

  it("stops reading when the last listener goes away", async () => {
    const world = fakeBackend();
    world.sections = { needs_you: [], working: [working()], completed: [] };
    world.details["demo-referral"] = detail({ state: "EXECUTING" });

    const api = new RealCareLoopApi();
    const unsubscribe = api.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(2000);
    unsubscribe();

    const before = world.calls.length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(world.calls.length).toBe(before);
  });

  it("keeps the drawer truthful when the run registry has forgotten the run", async () => {
    // `GET /api/runs/{id}` is an in-memory, per-process registry: a restart 404s a live run id.
    // Everything the drawer shows comes from the case, so the panel keeps working.
    const world = fakeBackend();
    world.sections = { needs_you: [], working: [working()], completed: [] };
    world.details["demo-referral"] = detail({
      state: "EXECUTING",
      timeline: [entry("PLAN_PROPOSED", { tool_name: "book_appointment", args: BOOKING })]
    });
    world.runs["run-demo-referral-1"] = {
      run_id: "run-demo-referral-1",
      case_id: "demo-referral",
      state: "RUNNING",
      executed_nodes: ["dispatch", "plan", "gate", "execute"],
      error: null
    };

    let latest: AgentRun | null = null;
    const api = new RealCareLoopApi();
    await api.runScenario("referral");
    api.subscribe((state) => {
      latest = state.run;
    });
    await vi.advanceTimersByTimeAsync(1500);
    expect(latest!.stage).toBe("EXECUTE");

    delete world.runs["run-demo-referral-1"];
    await vi.advanceTimersByTimeAsync(1500);

    expect(latest!.stage).toBe("EXECUTE");
    expect(latest!.actionIntent).toContain("book_appointment");
  });
});

/* ------------------------------------------------------------------ *
 * Approve, reject, and the demo control surface
 * ------------------------------------------------------------------ */

describe("approve and reject", () => {
  it("posts to approve, with the caregiver header", async () => {
    const world = fakeBackend();
    world.runs["run-demo-referral-1"] = {
      run_id: "run-demo-referral-1",
      case_id: "demo-referral",
      state: "RUNNING",
      executed_nodes: [],
      error: null
    };
    world.status["/api/cases/demo-referral/decisions/apr-abc/approve"] = 202;

    const api = new RealCareLoopApi();
    const result = await api.approveAction({ caseId: "demo-referral", decisionId: "apr-abc", approved: true });

    const call = world.calls.find((c) => c.url.endsWith("/approve"));
    expect(call).toBeDefined();
    expect(call!.url).toBe("/api/cases/demo-referral/decisions/apr-abc/approve");
    expect(call!.method).toBe("POST");
    // An audit identity, not authentication. A missing one is 400 by design, never 401.
    expect(call!.headers["X-Caregiver"]).toBe("Maya");
    expect(result.accepted).toBe(true);
  });

  it("posts to reject on the other button", async () => {
    const world = fakeBackend();
    world.status["/api/cases/demo-referral/decisions/apr-abc/reject"] = 202;

    const api = new RealCareLoopApi();
    await api.approveAction({ caseId: "demo-referral", decisionId: "apr-abc", approved: false });

    expect(world.calls.some((c) => c.url.endsWith("/reject"))).toBe(true);
    expect(world.calls.some((c) => c.url.endsWith("/approve"))).toBe(false);
  });

  it("reports a refusal honestly rather than throwing at a UI with nowhere to show it", async () => {
    const world = fakeBackend();
    // 409: a run is already in flight for this case, or the approval is already decided.
    world.status["/api/cases/demo-referral/decisions/apr-abc/approve"] = 409;

    const api = new RealCareLoopApi();
    const conflicted = await api.approveAction({
      caseId: "demo-referral",
      decisionId: "apr-abc",
      approved: true
    });
    expect(conflicted.accepted).toBe(false);

    world.status["/api/cases/demo-referral/decisions/apr-gone/approve"] = 404;
    const missing = await api.approveAction({
      caseId: "demo-referral",
      decisionId: "apr-gone",
      approved: true
    });
    expect(missing.accepted).toBe(false);
  });

  it("does not send an approval id it has parsed or rebuilt", async () => {
    // `apr-<args_digest[:16]>` is opaque and stays opaque: a decision made about one action must
    // not be able to authorise another, and the digest is what stops that.
    const world = fakeBackend();
    const opaque = "apr-0123456789abcdef";
    world.status[`/api/cases/demo-referral/decisions/${opaque}/approve`] = 202;

    await new RealCareLoopApi().approveAction({
      caseId: "demo-referral",
      decisionId: opaque,
      approved: true
    });
    expect(world.calls.some((c) => c.url.includes(opaque))).toBe(true);
  });
});

describe("one world per visitor", () => {
  /** A backend that mints worlds and refuses anything that does not name one it knows. */
  function isolatedBackend(minted: string[] = ["world-one", "world-two"]) {
    const calls: Call[] = [];
    const live = new Set<string>();
    let next = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        calls.push({ url, method: init?.method ?? "GET", headers });

        if (url.endsWith("/api/sessions")) {
          const id = minted[Math.min(next, minted.length - 1)];
          next += 1;
          live.add(id);
          return new Response(JSON.stringify({ session: id }), { status: 201 });
        }
        const named = headers["X-CareLoop-Session"];
        if (!named) return new Response("", { status: 400 });
        if (!live.has(named)) return new Response("", { status: 410 });
        return new Response(
          JSON.stringify({ needs_you: [], working: [], completed: [] }),
          { status: 200 }
        );
      })
    );

    return { calls, live };
  }

  it("mints a world before the first read and names it on every request", async () => {
    const world = isolatedBackend();
    const api = new RealCareLoopApi();

    await api.getDashboard();
    await api.getDashboard();

    expect(world.calls.filter((c) => c.url.endsWith("/api/sessions"))).toHaveLength(1);
    const named = world.calls.filter((c) => c.url.endsWith("/api/cases"));
    expect(named).toHaveLength(2);
    expect(named.every((c) => c.headers["X-CareLoop-Session"] === "world-one")).toBe(true);
  });

  it("mints once even when several reads start at the same moment", async () => {
    // The dashboard fires the case list and each detail together, so the first paint is where a
    // careless implementation mints a world per in-flight request and then keeps the last one.
    const world = isolatedBackend();
    const api = new RealCareLoopApi();

    await Promise.all([api.getDashboard(), api.getDashboard(), api.getDashboard()]);

    expect(world.calls.filter((c) => c.url.endsWith("/api/sessions"))).toHaveLength(1);
  });

  it("re-mints and retries when its world has been forgotten", async () => {
    // 410 is its own status precisely so this is mechanical: the backend restarted or the world
    // went idle, and the remedy is a new one. A judge sees a fresh dashboard rather than an error.
    const world = isolatedBackend();
    const api = new RealCareLoopApi();
    await api.getDashboard();

    world.live.delete("world-one");
    const after = await api.getDashboard();

    expect(after.cases).toEqual([]);
    expect(world.calls.filter((c) => c.url.endsWith("/api/sessions"))).toHaveLength(2);
    const last = world.calls[world.calls.length - 1];
    expect(last.headers["X-CareLoop-Session"]).toBe("world-two");
  });

  it("re-mints at most once per request rather than looping", async () => {
    const world = isolatedBackend();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        world.calls.push({
          url,
          method: init?.method ?? "GET",
          headers: (init?.headers ?? {}) as Record<string, string>
        });
        if (url.endsWith("/api/sessions")) {
          return new Response(JSON.stringify({ session: "always-stale" }), { status: 201 });
        }
        return new Response("", { status: 410 });
      })
    );

    await new RealCareLoopApi().getDashboard();

    expect(world.calls.filter((c) => c.url.endsWith("/api/cases"))).toHaveLength(2);
  });

  it("carries no session against a backend that serves one world to everybody", async () => {
    // Local development and the offline suite. 404 on the mint is a supported answer, not a fault.
    const world = fakeBackend();
    await new RealCareLoopApi().getDashboard();

    const read = world.calls.filter((c) => c.url.endsWith("/api/cases"));
    expect(read).toHaveLength(1);
    expect(read[0].headers["X-CareLoop-Session"]).toBeUndefined();
  });

  it("keeps its world across a reload", async () => {
    // Not `localStorage`: two tabs sharing one world would reset it under each other, which is a
    // smaller copy of the bug per-visitor isolation exists to fix. And not nothing, because a
    // reload would otherwise abandon a world with the visitor's cases still in it.
    const world = isolatedBackend();
    await new RealCareLoopApi().getDashboard();

    // A reload is fresh module state over the same tab storage, so the module is genuinely
    // re-executed rather than a private reset being exposed to fake one.
    vi.resetModules();
    const reloaded = await import("./realApi");
    await new reloaded.RealCareLoopApi().getDashboard();

    expect(world.calls.filter((c) => c.url.endsWith("/api/sessions"))).toHaveLength(1);
    expect(window.sessionStorage.getItem("careloop.session")).toBe("world-one");
  });
});

describe("the demo control surface", () => {
  it("presses a scenario in one request and lets the server reset", async () => {
    // referral and injection both carry the same referral document and propose the same slot;
    // pressing the second without a reset gets a correct 409 from a clinic that already holds it.
    // This used to be a client-side reset followed by the scenario — two requests with a window
    // between them, where the reset can land and the start be refused, leaving an empty dashboard
    // and nothing running. No client can make that pair atomic, so the backend owns it now.
    const world = fakeBackend();
    const api = new RealCareLoopApi();
    await api.runScenario("injection");

    const order = world.calls.filter((c) => c.url.startsWith("/api/demo/")).map((c) => c.url);
    expect(order).toEqual(["/api/demo/scenarios/injection"]);
  });

  it("waits out a scenario refused because its own run is in flight", async () => {
    // The retry budget did not go away with the client-side reset — it moved onto the one call.
    // A 409 here is this visitor's previous run, which is exactly when pressing again should wait.
    vi.useFakeTimers();
    try {
      const world = fakeBackend();
      world.status["/api/demo/scenarios/referral"] = 409;

      const api = new RealCareLoopApi();
      const settled = api.runScenario("referral");
      await vi.advanceTimersByTimeAsync(RESET_ATTEMPTS * RESET_RETRY_MS + 1000);
      await settled;

      expect(
        world.calls.filter((c) => c.url === "/api/demo/scenarios/referral")
      ).toHaveLength(RESET_ATTEMPTS);
    } finally {
      vi.useRealTimers();
    }
  });

  it("waits out a reset refused because a run is in flight", async () => {
    vi.useFakeTimers();
    try {
      const world = fakeBackend();
      world.status["/api/demo/reset"] = 409;

      const api = new RealCareLoopApi();
      const settled = api.resetDemo();
      await vi.advanceTimersByTimeAsync(RESET_ATTEMPTS * RESET_RETRY_MS + 1000);
      await settled;

      // Refused every time, so it spent the whole budget and gave up rather than looping forever.
      expect(world.calls.filter((c) => c.url === "/api/demo/reset")).toHaveLength(RESET_ATTEMPTS);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives up retrying the reset only after the slowest run could have finished", () => {
    // The bug this pins, reproduced against the deployed stack before it was fixed: a judge
    // presses scenario 02 while 01 is still running, the reset is refused for the whole retry
    // budget, and 02 then books against a clinic that already holds the one seeded slot. The
    // case ends FAILED with ClinicRefused — which reads exactly like the injection defence
    // collapsing, on the one scenario where that misreading is most costly.
    //
    // 3 x 300ms could not outlast a 2s run, let alone a 6.7s one.
    expect(RESET_ATTEMPTS * RESET_RETRY_MS).toBeGreaterThan(SLOWEST_OBSERVED_RUN_MS * 2);
  });

  it("keeps the run id the 202 handed back", async () => {
    fakeBackend();
    const api = new RealCareLoopApi();
    const started = await api.runScenario("referral");
    expect(started).toEqual({
      scenarioId: "referral",
      runId: "run-demo-referral-1",
      caseId: "demo-referral"
    });
  });
});
