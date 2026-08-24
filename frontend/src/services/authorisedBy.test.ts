import { describe, expect, it } from "vitest";
import { authorisedBy } from "./realApi";
import type { TimelineEventView } from "./backend";

/* The bridge from a settled case back to the boundary it was an instance of.
 *
 * It reads two payload keys the API already projects and derives nothing. What is worth asserting is
 * *which* verdict it reads and when it declines to read one: a case can record several, and naming
 * an earlier one would attribute an outcome to a rule that authorised something else. */

let seq = 0;

function entry(kind: string, payload: Record<string, unknown> = {}): TimelineEventView {
  seq += 1;
  return { seq, at: "2026-08-13T11:00:00+00:00", kind, actor: "policy", summary: kind, payload };
}

describe("the rule that authorised what a case did", () => {
  it("reads the matched rule and the tool from the allow entry", () => {
    expect(
      authorisedBy([
        entry("PLAN_PROPOSED", { tool_name: "book_appointment" }),
        entry("POLICY_ALLOW", {
          tool_name: "book_appointment",
          matched_rule_id: "allow-booking-on-validated-slot"
        })
      ])
    ).toEqual({ ruleId: "allow-booking-on-validated-slot", toolName: "book_appointment" });
  });

  it("takes the latest allow when a case recorded several", () => {
    // A refused proposal, a replan, then the allow that actually authorised the effect. The first
    // one describes a call that never happened.
    const found = authorisedBy([
      entry("POLICY_ALLOW", { tool_name: "fetch_document", matched_rule_id: "allow-document-fetch" }),
      entry("POLICY_DENY", { tool_name: "send_email", reason_code: "DESTINATION_NOT_TRUSTED" }),
      entry("POLICY_ALLOW", {
        tool_name: "book_appointment",
        matched_rule_id: "allow-booking-on-validated-slot"
      })
    ]);
    expect(found?.toolName).toBe("book_appointment");
  });

  it("is absent when nothing was allowed", () => {
    expect(
      authorisedBy([
        entry("POLICY_DENY", { tool_name: "send_email", reason_code: "DESTINATION_NOT_TRUSTED" }),
        entry("MANUAL_HANDOFF_REQUIRED", { reason_code: "NO_SAFE_ACTION_AVAILABLE" })
      ])
    ).toBeNull();
  });

  it("is absent on an allow that names no rule", () => {
    // Deny-by-default records no `matched_rule_id`, and a link to `/trust#` naming no capability
    // would scroll nowhere. Nothing is better than a dead anchor.
    expect(authorisedBy([entry("POLICY_ALLOW", { tool_name: "book_appointment" })])).toBeNull();
  });

  it("is absent on an allow that names no tool", () => {
    expect(authorisedBy([entry("POLICY_ALLOW", { matched_rule_id: "allow-something" })])).toBeNull();
  });

  it("is absent on an empty timeline", () => {
    expect(authorisedBy([])).toBeNull();
  });

  it("reads the payload and never the summary", () => {
    // The summary is free text a backend edit could reword; the two payload keys are allowlisted
    // and stable. A reader that fell back to prose would drift the first time somebody rephrased.
    const found = authorisedBy([
      {
        seq: 99,
        at: "2026-08-13T11:00:00+00:00",
        kind: "POLICY_ALLOW",
        actor: "policy",
        summary: "allow-a-rule-that-is-only-in-the-summary permitted upload_document",
        payload: { tool_name: "book_appointment", matched_rule_id: "allow-booking-on-validated-slot" }
      }
    ]);
    expect(found).toEqual({
      ruleId: "allow-booking-on-validated-slot",
      toolName: "book_appointment"
    });
  });
});
