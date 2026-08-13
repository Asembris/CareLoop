import { describe, expect, it } from "vitest";
import {
  actionIntentFor,
  argValue,
  blockedFor,
  decisionCopy,
  decisionKindFor,
  factSummary,
  isExcerptFact,
  isRenderedOnTheSpine,
  observedLabel,
  outcomeCopy,
  policyDetailFor,
  relativeTime,
  sectionFor,
  slotLabel,
  stageFor,
  stageLabelFor,
  statusCopy,
  toneFor,
  trustWord,
  verificationDetailFor
} from "./presentation";

const NOW = new Date("2026-08-13T12:00:00");

function status(overrides: Partial<Parameters<typeof statusCopy>[0]> = {}) {
  return statusCopy({
    state: "PLANNING",
    failureReason: null,
    mismatch: false,
    verified: false,
    outcomeBody: null,
    ...overrides
  });
}

describe("sections", () => {
  it("renames the backend's three section names", () => {
    expect(sectionFor("needs_you")).toBe("needs");
    expect(sectionFor("working")).toBe("working");
    expect(sectionFor("completed")).toBe("done");
  });

  it("puts an unrecognised section in Working rather than losing the case", () => {
    // The backend defines `working` as "the rest" for exactly this reason: a case visible in the
    // wrong column beats a case that has silently vanished from the dashboard.
    expect(sectionFor("something_new")).toBe("working");
  });
});

describe("status copy", () => {
  it("never renders a raw case state", () => {
    const states = [
      "NEW",
      "GATHERING_EVIDENCE",
      "PLANNING",
      "AWAITING_HUMAN",
      "READY_TO_EXECUTE",
      "EXECUTING",
      "VERIFYING",
      "WAITING_EXTERNAL",
      "DONE",
      "FAILED"
    ];
    for (const state of states) {
      const copy = status({ state });
      expect(copy.statusLabel).not.toContain("_");
      expect(copy.statusText).not.toBe(state);
      expect(copy.headline).not.toBe(state);
    }
  });

  it("names the failure reason on a FAILED case rather than soothing it", () => {
    const copy = status({ state: "FAILED", failureReason: "book_appointment refused at the tool boundary" });
    expect(copy.statusLabel).toBe("DID NOT COMPLETE");
    expect(copy.statusText).toContain("book_appointment refused at the tool boundary");
  });

  it("does not let a FAILED case read as a success", () => {
    const copy = status({ state: "FAILED", failureReason: null });
    expect(copy.statusText.toLowerCase()).not.toContain("verified");
    expect(copy.statusText.toLowerCase()).not.toContain("completed");
  });

  it("separates a DONE case that was read back from one that was not", () => {
    expect(status({ state: "DONE", verified: true }).statusText).toBe("Done · independently verified");
    expect(status({ state: "DONE", verified: false }).statusText).toBe(
      "Done · checked against what was authorized"
    );
  });

  it("lets a mismatch outrank the case state, so it can never read as complete", () => {
    // HANDOFF.md §13: a MISMATCH may not be presented as a completed case. A read-back mismatch
    // routes the case back to PLANNING and reaches FAILED the long way, so every state below is
    // one a mismatched case can genuinely be in while the timeline already knows better.
    for (const state of ["PLANNING", "EXECUTING", "VERIFYING", "DONE", "FAILED"]) {
      const copy = status({ state, mismatch: true, verified: true, outcomeBody: "Booked" });
      expect(copy.statusLabel).toBe("DID NOT MATCH");
      expect(copy.statusText).toBe("The clinic's record disagrees");
      expect(copy.statusText.toLowerCase()).not.toContain("verified");
    }
  });
});

describe("relative time", () => {
  it("reads the way the dashboard's rows do", () => {
    expect(relativeTime("2026-08-13T11:59:40", NOW)).toBe("Just now");
    expect(relativeTime("2026-08-13T11:46:00", NOW)).toBe("14 min ago");
    expect(relativeTime("2026-08-13T09:00:00", NOW)).toBe("3 hr ago");
    expect(relativeTime("2026-08-12T16:02:00", NOW)).toBe("Yesterday");
    expect(relativeTime("2026-08-11T16:02:00", NOW)).toBe("Aug 11");
  });

  it("says nothing rather than NaN when the timestamp is unreadable", () => {
    expect(relativeTime("not a date", NOW)).toBe("");
  });
});

describe("slots", () => {
  it("renders a clinic slot the way a person reads it, without converting the timezone", () => {
    // No timezone conversion, for the reason the backend's canonical form does none: two
    // spellings of one instant must not compare equal, and the screen must agree with the rule.
    expect(slotLabel("2026-09-03T09:30:00")).toBe("Thu Sep 3 · 9:30 AM");
    expect(slotLabel("2026-09-03T14:00:00")).toBe("Thu Sep 3 · 2:00 PM");
    expect(slotLabel("2026-09-03T00:15:00")).toBe("Thu Sep 3 · 12:15 AM");
  });

  it("passes an unparseable value through rather than inventing a date", () => {
    expect(slotLabel("soon")).toBe("soon");
  });

  it("formats a slot wherever an argument carries one", () => {
    expect(argValue("slot_start", "2026-09-03T09:30:00")).toBe("Thu Sep 3 · 9:30 AM");
    expect(argValue("provider_id", "NORTHGATE-CARD")).toBe("NORTHGATE-CARD");
  });
});

describe("trust", () => {
  it("gives a caregiver a word rather than a provenance tier", () => {
    expect(trustWord("UNTRUSTED")).toBe("From a document");
    expect(trustWord("CONTROLLED")).toBe("Validated");
    expect(trustWord("TRUSTED")).toBe("Confirmed");
  });

  it("never renders the raw tier, including one it has not heard of", () => {
    for (const tier of ["UNTRUSTED", "CONTROLLED", "TRUSTED", "SOMETHING_NEW"]) {
      expect(trustWord(tier)).not.toBe(tier);
    }
  });
});

describe("evidence", () => {
  it("recognises the whole-page facts extraction records", () => {
    // Extraction records one fact per page whose value is the entire page. They reach the API
    // like any other fact, and an evidence grid that showed them would be a wall of page dumps.
    expect(isExcerptFact("excerpt:doc-0001-referral:p2")).toBe(true);
    expect(isExcerptFact("slot_start")).toBe(false);
  });

  it("labels a fact key and formats its value", () => {
    expect(factSummary("slot_start", "2026-09-03T09:30:00")).toBe("Appointment slot · Thu Sep 3 · 9:30 AM");
    expect(factSummary("patient_id", "PT-4471")).toBe("Patient · PT-4471");
    expect(factSummary("some_new_key", "x")).toBe("Some new key · x");
  });
});

describe("the timeline", () => {
  it("maps all eighteen timeline kinds onto the five tones", () => {
    expect(toneFor("VERIFICATION_PASSED")).toBe("verified");
    expect(toneFor("VERIFICATION_FAILED")).toBe("denied");
    expect(toneFor("POLICY_DENY")).toBe("denied");
    expect(toneFor("APPROVAL_REQUESTED")).toBe("decision");
    expect(toneFor("VALIDATION_REFUSED")).toBe("untrusted");
    expect(toneFor("EVIDENCE_RECORDED")).toBe("neutral");
    expect(toneFor("SOMETHING_NEW")).toBe("neutral");
  });

  it("keeps the state machine's narration off the spine and folds denials instead", () => {
    expect(isRenderedOnTheSpine("STATE_TRANSITION")).toBe(false);
    expect(isRenderedOnTheSpine("POLICY_DENY")).toBe(false);
    expect(isRenderedOnTheSpine("ACTION_EXECUTED")).toBe(true);
  });

  it("says what was blocked and why, using the real reason code", () => {
    // AUTHORITY_NOT_TRUSTED, not the mock adapter's invented AUTHORITY_ESCALATION.
    const blocked = blockedFor("grant_portal_access", "AUTHORITY_NOT_TRUSTED");
    expect(blocked.text).toBe("Grant portal access with powers nothing approved");
    expect(blocked.rule).toBe("AUTHORITY_NOT_TRUSTED");

    expect(blockedFor("send_email", "DESTINATION_NOT_TRUSTED").text).toBe(
      "Send an email to a destination nothing approved"
    );
  });
});

describe("decisions", () => {
  it("only lets REQUIRES_HUMAN_APPROVAL be approvable", () => {
    expect(decisionKindFor("REQUIRES_HUMAN_APPROVAL")).toBe("REQUIRES_HUMAN_APPROVAL");
    for (const code of [
      "NO_SAFE_ACTION_AVAILABLE",
      "DESTINATION_NOT_TRUSTED",
      "AUTHORITY_NOT_TRUSTED",
      "WRONG_CASE_STATE",
      "INSUFFICIENT_FACT_TRUST",
      "NO_RULE_MATCH",
      "A_CODE_THIS_BUILD_HAS_NEVER_HEARD_OF",
      ""
    ]) {
      expect(decisionKindFor(code)).toBe("NO_SAFE_ACTION_AVAILABLE");
    }
  });

  it("names exactly the call that will be made", () => {
    const copy = decisionCopy("REQUIRES_HUMAN_APPROVAL", "book_appointment", {
      provider_id: "NORTHGATE-CARD",
      patient_id: "PT-4471",
      slot_start: "2026-09-03T09:30:00",
      reason: ""
    });
    expect(copy.title).toBe("Book the appointment");
    expect(copy.facts).toEqual([
      { label: "WHERE", value: "NORTHGATE-CARD" },
      { label: "PATIENT", value: "PT-4471" },
      { label: "WHEN", value: "Thu Sep 3 · 9:30 AM" }
    ]);
  });

  it("does not put the planner's own reason text in front of a caregiver", () => {
    const plannerText = "This action needs the caregiver's decision. Stop and await it.";
    const copy = decisionCopy("REQUIRES_HUMAN_APPROVAL", "send_email", { to: "a@b.example" });
    expect(copy.body).not.toContain(plannerText);
    expect(copy.body).not.toContain("planner");
  });

  it("offers a person to take over rather than an approval, when nothing is safe", () => {
    const copy = decisionCopy("NO_SAFE_ACTION_AVAILABLE", "", {});
    expect(copy.title).toBe("No safe action is available");
    expect(copy.after).toContain("Nothing was changed");
  });
});

describe("the outcome panel", () => {
  it("says what happened, and says 'confirmed' only when something was read back", () => {
    const args = { provider_id: "NORTHGATE-CARD", slot_start: "2026-09-03T09:30:00" };
    expect(outcomeCopy("book_appointment", args, true).title).toBe("Appointment booked and confirmed");
    expect(outcomeCopy("book_appointment", args, false).title).toBe("Appointment booked");
    expect(outcomeCopy("book_appointment", args, true).body).toBe("NORTHGATE-CARD · Thu Sep 3 · 9:30 AM");
  });

  it("says whether the world was read, never what it said", () => {
    // The observed values live only in Verification.diagnostic, which the API withholds wholesale.
    expect(observedLabel(true)).toBe("Confirmed by an independent read");
    expect(observedLabel(false)).toBe("Not read back");
  });
});

describe("the run record", () => {
  it("maps graph nodes onto the drawer's stages and skips dispatch", () => {
    expect(stageFor(["dispatch"], "RUNNING", "NEW")).toBe("IDLE");
    expect(stageFor(["dispatch", "triage"], "RUNNING", "GATHERING_EVIDENCE")).toBe("TRIAGE");
    expect(stageFor(["dispatch", "triage", "evidence", "plan"], "RUNNING", "PLANNING")).toBe("PLANNER");
    expect(stageFor(["dispatch", "triage", "evidence", "plan", "gate"], "RUNNING", "AWAITING_HUMAN")).toBe(
      "GATE"
    );
  });

  it("follows the replan cycle backwards, because the agent really did go back", () => {
    expect(stageFor(["dispatch", "plan", "gate", "plan"], "RUNNING", "PLANNING")).toBe("PLANNER");
  });

  it("does not call a parked case settled", () => {
    // A run that settles with the case in AWAITING_HUMAN is the normal approval beat, not an end:
    // the graph has no satisfied outgoing edge and stops. The case is at the gate, so is the UI.
    expect(stageFor(["dispatch", "plan", "gate"], "SETTLED", "AWAITING_HUMAN")).toBe("GATE");
    expect(stageFor(["dispatch", "verify"], "SETTLED", "DONE")).toBe("SETTLED");
    expect(stageFor(["dispatch", "execute"], "SETTLED", "FAILED")).toBe("SETTLED");
    expect(stageFor(["dispatch"], "ERRORED", "FAILED")).toBe("SETTLED");
  });

  it("labels the run line the way the drawer expects", () => {
    const base = { runId: "run-demo-referral-1", runState: "RUNNING", error: null };
    expect(
      stageLabelFor({ ...base, executedNodes: ["dispatch", "plan", "gate"], caseState: "AWAITING_HUMAN", verdict: "REQUIRE_HUMAN" })
    ).toBe("run-demo-referral-1 · gate → REQUIRE_HUMAN");
    expect(
      stageLabelFor({ ...base, executedNodes: ["dispatch", "plan", "gate", "plan"], caseState: "PLANNING", verdict: "DENY" })
    ).toBe("run-demo-referral-1 · plan (replan)");
    expect(
      stageLabelFor({ ...base, executedNodes: ["dispatch", "verify"], caseState: "DONE", verdict: "ALLOW" })
    ).toBe("run-demo-referral-1 · settled");
    expect(
      stageLabelFor({ ...base, runState: "ERRORED", error: "ClinicRefused", executedNodes: [], caseState: "FAILED", verdict: null })
    ).toBe("run-demo-referral-1 · errored (ClinicRefused)");
  });

  it("renders the proposed call, dropping the arguments the planner left empty", () => {
    expect(
      actionIntentFor("book_appointment", {
        provider_id: "NORTHGATE-CARD",
        slot_start: "2026-09-03T09:30:00",
        reason: ""
      })
    ).toBe("book_appointment(provider_id=NORTHGATE-CARD, slot_start=2026-09-03T09:30:00)");
    expect(actionIntentFor("", {})).toBe("none proposed");
  });

  it("counts the denials and names the rule that matched", () => {
    expect(policyDetailFor([])).toBe("pending");
    expect(
      policyDetailFor([
        { verdict: "DENY", reasonCode: "DESTINATION_NOT_TRUSTED", matchedRuleId: "deny-upload-to-untrusted-portal" }
      ])
    ).toBe("DENY · rule deny-upload-to-untrusted-portal");
    expect(
      policyDetailFor([
        { verdict: "DENY", reasonCode: "DESTINATION_NOT_TRUSTED", matchedRuleId: "r1" },
        { verdict: "DENY", reasonCode: "DESTINATION_NOT_TRUSTED", matchedRuleId: "r2" },
        { verdict: "DENY", reasonCode: "DESTINATION_NOT_TRUSTED", matchedRuleId: "r3" },
        { verdict: "ALLOW", reasonCode: "", matchedRuleId: "allow-booking-on-validated-slot" }
      ])
    ).toBe("DENY ×3 → ALLOW · rule allow-booking-on-validated-slot");
  });

  it("keeps an external read-back apart from a closed-loop check", () => {
    // The whole reason `read_back` is on the payload allowlist: a panel that could not tell them
    // apart would let the weaker claim be read as the stronger one.
    expect(verificationDetailFor(true, true)).toBe("MATCH — external read-back");
    expect(verificationDetailFor(true, false)).toBe("MATCH — recorded effect checked");
    expect(verificationDetailFor(false, true)).toBe("MISMATCH — authorized ≠ observed");
    expect(verificationDetailFor(null, false)).toBe("pending");
  });
});
