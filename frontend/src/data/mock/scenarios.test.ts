import { describe, expect, it } from "vitest";
import { SCENARIOS } from "./scenarios";

/* The launcher is where a judge decides what they are about to watch, so its copy is the one place
   a promise the runtime does not keep does the most damage.

   Two claims are guarded here, and they pull in opposite directions. The deterministic card must
   say that CareLoop supplies the proposal — because it does, and a reader who assumed a model wrote
   it would take away a stronger belief about Bedrock than this run measures. The realistic
   alternate must *not* promise a refusal — because the planner is free to ignore the poison, and
   three of six measured deployed runs did exactly that.

   The phrase-level sweep over every judge-facing surface lives in `tests/test_adversarial_copy.py`.
   This file is the positive half: not "nothing false is said" but "the necessary things are". */

const card = (id: string) => SCENARIOS.find((s) => s.id === id);

describe("the launcher keeps four primary scenarios", () => {
  it("offers exactly four cards, numbered in order", () => {
    expect(SCENARIOS).toHaveLength(4);
    expect(SCENARIOS.map((s) => s.num)).toEqual(["01", "02", "03", "04"]);
  });

  it("makes the deterministic boundary test the primary second beat", () => {
    expect(SCENARIOS[1].id).toBe("adversarial");
  });

  it("keeps the realistic poisoned document reachable without a fifth card", () => {
    expect(card("adversarial")?.alternate?.id).toBe("injection");
  });

  it("gives only that card an alternate", () => {
    expect(SCENARIOS.filter((s) => s.alternate)).toHaveLength(1);
  });
});

describe("the launcher is a guided sequence rather than a catalog", () => {
  it("marks exactly one card as the place to start, and it is the referral", () => {
    const starters = SCENARIOS.filter((s) => s.startHere);
    expect(starters).toHaveLength(1);
    expect(starters[0]!.id).toBe("referral");
  });

  it("gives every card its own action line", () => {
    /* Four identical "Run scenario →" labels were the catalog. Distinctness is the property —
       exact wording is free to move, subject to the honesty sweeps. */
    expect(new Set(SCENARIOS.map((s) => s.cta)).size).toBe(SCENARIOS.length);
    for (const s of SCENARIOS) expect(s.cta.trim().length).toBeGreaterThan(0);
  });
});

describe("the referral card promises only what the referral run does", () => {
  it("no longer claims a decision is requested", () => {
    /* The run books and verifies with nobody asked — the recovery e2e drives the same inbound
       event to COMPLETED while asserting nothing is clicked. A card that promised a decision was
       promising an interaction the judge would then wait for and never get. */
    expect(card("referral")!.blurb).not.toMatch(/decision/i);
    expect(card("referral")!.blurb).not.toMatch(/approv/i);
    expect(card("referral")!.blurb).not.toMatch(/\bask/i);
  });

  it("still claims the external check, which the run does make", () => {
    expect(card("referral")!.blurb).toMatch(/clinic's own record/i);
  });
});

describe("the deterministic card says who wrote the proposal", () => {
  const blurb = card("adversarial")!.blurb;

  it("says CareLoop supplies it, deliberately", () => {
    expect(blurb).toMatch(/CareLoop deliberately proposes/i);
  });

  it("never attributes the proposal to a model", () => {
    for (const claim of [/the model/i, /the ai\b/i, /bedrock/i, /nova/i, /the planner/i]) {
      expect(blurb).not.toMatch(claim);
    }
  });

  it("promises a question rather than an outcome", () => {
    /* "see whether that proposal has any authority to act" is a thing the judge can check. "watch
       CareLoop block it" would be a promise about a verdict, which is the shape the old copy had
       on the card that could not keep it. */
    expect(blurb).toMatch(/see whether/i);
    expect(blurb).not.toMatch(/watch\b/i);
  });
});

describe("the realistic alternate promises nothing about the model's behaviour", () => {
  const alternate = card("adversarial")!.alternate!;

  it("says the model may ignore the attack", () => {
    expect(alternate.note).toMatch(/may simply ignore/i);
  });

  it("calls that the system working rather than a failed demo", () => {
    expect(alternate.note).toMatch(/the system working/i);
  });

  it("does not promise a refusal", () => {
    for (const claim of [/watch/i, /blocks?\b/i, /refuses?\b/i, /will stop/i]) {
      expect(alternate.note).not.toMatch(claim);
    }
  });
});
