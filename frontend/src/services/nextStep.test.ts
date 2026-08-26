import { describe, expect, it } from "vitest";
import { nextStepFor } from "./presentation";
import type { AdversarialProof } from "@/types/domain";

/* The guided route's one affordance. The gates must read durable identity and settled state and
   nothing else — a link that rendered from the scenario the browser remembers launching, or on a
   case that is still working, would be guiding a judge away from an outcome that does not exist
   yet. */

const proof: AdversarialProof = {
  proposedAction: "Send an email",
  destination: "records-intake@claims-continuity-partners.example",
  refusalText: "Send an email to a destination nothing approved",
  reasonCode: "DESTINATION_NOT_TRUSTED",
  ruleId: "deny-email-to-untrusted-recipient",
  toolName: "send_email",
  recordedActions: 0,
  refusals: 5
};

describe("the settled demo fixtures offer the next beat", () => {
  it("a completed referral points at the boundary test", () => {
    const next = nextStepFor({ id: "demo-referral", section: "done" });
    expect(next?.to).toBe("/demo");
    expect(next?.text).toMatch(/unsafe/i);
  });

  it("a settled boundary test points at the lost-reply recovery", () => {
    const next = nextStepFor({ id: "demo-adversarial", section: "needs", adversarial: proof });
    expect(next?.to).toBe("/demo");
    expect(next?.text).toMatch(/lost external reply/i);
  });

  it("both point at the launcher, never at a scenario endpoint", () => {
    /* The judge must read the next card's own framing before pressing it — the boundary card says
       CareLoop supplies the proposal, and a link that skipped that disclosure would spend it. */
    for (const c of [
      { id: "demo-referral", section: "done" as const },
      { id: "demo-adversarial", section: "needs" as const, adversarial: proof }
    ]) {
      expect(nextStepFor(c)?.to).toBe("/demo");
    }
  });
});

describe("every other case gets nothing", () => {
  it("a referral still working offers no next step", () => {
    expect(nextStepFor({ id: "demo-referral", section: "working" })).toBeNull();
    expect(nextStepFor({ id: "demo-referral", section: "needs" })).toBeNull();
  });

  it("a boundary test still proposing offers no next step", () => {
    // Mid-run the proof signature can already hold while the fixture is still working through the
    // registry — the section gate is what keeps the link off an unsettled case.
    expect(nextStepFor({ id: "demo-adversarial", section: "working", adversarial: proof })).toBeNull();
  });

  it("a boundary-test id without the audit-trail proof offers no next step", () => {
    expect(nextStepFor({ id: "demo-adversarial", section: "needs", adversarial: null })).toBeNull();
    expect(nextStepFor({ id: "demo-adversarial", section: "needs" })).toBeNull();
  });

  it("the recovery fixture is the spine's last stop and offers none", () => {
    expect(nextStepFor({ id: "demo-recovery", section: "done" })).toBeNull();
  });

  it("an ordinary case — any non-fixture id — offers none, whatever its state", () => {
    for (const section of ["needs", "working", "done"] as const) {
      expect(nextStepFor({ id: "case-0042", section })).toBeNull();
      expect(nextStepFor({ id: "demo-resume", section })).toBeNull();
    }
  });
});
