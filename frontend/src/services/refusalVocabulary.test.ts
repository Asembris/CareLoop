import { describe, expect, it } from "vitest";
import { blockedFor, decisionCopy } from "./presentation";

/* Two caregiver-facing statements that were not true, found while deriving My Boundaries.
 *
 * Neither was an authority defect — nothing was mis-authorised and no effect changed. Both were the
 * product describing what happened *wrongly*, on the one surface whose claim is that the trail tells
 * the truth, and the second was the one that mattered.
 *
 * `REFUSAL_PHRASES` carried nine of the backend's eleven reason codes while its own comment said it
 * stayed "total over the backend's vocabulary". The two missing ones both reach a caregiver through
 * a `MANUAL_HANDOFF_REQUIRED` entry.
 *
 * `PATIENT_AUTHORITY_DENIED` then fell through `decisionCopy` to the no-safe-action card, which
 * says **"Every action CareLoop could take here was refused by its own policy."** That is false:
 * `graph/nodes.py` asks the patient boundary before any intent is durably written, so nothing was
 * proposed and policy refused nothing. A caregiver reading it would go looking for a rule that never
 * matched, and would not be told the one thing that is actually true — this case is about somebody
 * their access does not cover. */

/** Every member of `careloop.policy.verdicts.ReasonCode`, spelled out.
 *
 *  A runtime list rather than the `ReasonCode` type, because a TypeScript union cannot be iterated
 *  and the gap this file exists for was precisely a map that looked complete. */
const EVERY_REASON_CODE = [
  "DESTINATION_NOT_TRUSTED",
  "AUTHORITY_NOT_TRUSTED",
  "REQUIRES_HUMAN_APPROVAL",
  "WRONG_CASE_STATE",
  "INSUFFICIENT_FACT_TRUST",
  "NO_RULE_MATCH",
  "NO_SAFE_ACTION_AVAILABLE",
  "EXTERNAL_STATE_NOT_CONFIRMED",
  "NO_VALID_PROPOSAL_PRODUCED",
  "EXTERNAL_OUTCOME_NOT_ESTABLISHED",
  "PATIENT_AUTHORITY_DENIED"
] as const;

describe("the refusal vocabulary covers everything the backend can say", () => {
  it("gives every reason code a sentence of its own", () => {
    for (const code of EVERY_REASON_CODE) {
      const blocked = blockedFor("send_email", code);
      // Without a phrase, `blockedFor` falls back to the bare tool phrase — "Send an email" with no
      // "why" at all. That is what the two missing codes produced.
      expect(blocked.text, code).not.toBe("Send an email");
      expect(blocked.rule).toBe(code);
    }
  });

  it("says something different for each one", () => {
    const sentences = EVERY_REASON_CODE.map((code) => blockedFor("send_email", code).text);
    expect(new Set(sentences).size).toBe(EVERY_REASON_CODE.length);
  });

  it("still falls back gracefully for a code this build has never heard of", () => {
    expect(blockedFor("send_email", "A_CODE_FROM_THE_FUTURE").text).toBe("Send an email");
  });
});

describe("a case outside the caregiver's access", () => {
  const copy = decisionCopy("PATIENT_AUTHORITY_DENIED", "", {});

  it("does not claim policy refused anything", () => {
    // The false sentence, verbatim. Nothing was proposed, so nothing was refused.
    expect(copy.body).not.toContain("refused by its own policy");
    expect(copy.title).not.toBe("No safe action is available");
  });

  it("says what actually happened instead", () => {
    expect(copy.title).toContain("outside your access");
    expect(copy.body).toContain("who this case is about");
    expect(copy.body).toContain("Nothing was sent, booked or changed");
  });

  it("says access is not something to grant here", () => {
    expect(copy.after).toContain("nothing here to approve");
    expect(copy.after).toContain("outside CareLoop");
  });
});

describe("an effect whose outcome was never established", () => {
  const copy = decisionCopy("EXTERNAL_OUTCOME_NOT_ESTABLISHED", "book_appointment", {});
  const confirmed = decisionCopy("EXTERNAL_STATE_NOT_CONFIRMED", "book_appointment", {});

  it("is not the same card as an effect that could not be confirmed", () => {
    // Two different errands. "We hold a receipt the clinic disagrees with" asks *is it right*;
    // "we hold no receipt at all" asks *does it exist*. Rendering them alike would send a caregiver
    // to check the wrong thing.
    expect(copy.title).not.toBe(confirmed.title);
    expect(copy.body).not.toBe(confirmed.body);
  });

  it("says the answer never came back rather than that it disagreed", () => {
    expect(copy.body).toContain("no answer ever came back");
    expect(copy.body).not.toContain("do not agree");
  });

  it("asks the caregiver to look for a record rather than to correct one", () => {
    expect(copy.after).toContain("check the other system for a record");
  });

  it("promises no further automatic attempt", () => {
    // True: this parks in AWAITING_HUMAN with no approval, and the reconcile wake arms only for
    // WAITING_EXTERNAL. Nothing schedules another run for this case.
    expect(copy.after).toContain("Nothing further happens automatically");
  });
});
