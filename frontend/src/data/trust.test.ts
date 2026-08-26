import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SECURITY_EVIDENCE } from "./trust";

/* The Trust page's numbers are hand-carried constants, and this is what keeps that honest: they
   are parsed back out of `docs/security-evaluation.md` — the committed record of the measurement —
   and any drift between what the page says and what was measured is a red test, not a stale page.

   If the eval is re-run and the document changes, this fails until the constants are updated,
   which is the point: updating the number on the page is then a deliberate act with a diff. */

const DOCUMENT = join(__dirname, "..", "..", "..", "docs", "security-evaluation.md");

describe("the Trust page numbers are pinned to the measured record", () => {
  it("finds the committed record where it claims to be", () => {
    expect(existsSync(DOCUMENT)).toBe(true);
    expect(SECURITY_EVIDENCE.source.replace(/\//g, "")).toBe("docssecurity-evaluation.md");
  });

  it("matches the shipped-configuration headline", () => {
    const text = readFileSync(DOCUMENT, "utf-8");
    const headline = text.match(/Unauthorized external actions: (\d+) of (\d+) adversarial attempts/);
    expect(headline).not.toBeNull();
    expect(Number(headline![1])).toBe(SECURITY_EVIDENCE.shippedUnauthorized);
    expect(Number(headline![2])).toBe(SECURITY_EVIDENCE.attempts);
  });

  it("matches the authority-removed contrast", () => {
    const text = readFileSync(DOCUMENT, "utf-8");
    const contrast = text.match(/authority layer removed[\s\S]{0,200}?\*\*(\d+) of (\d+)\*\*/);
    expect(contrast).not.toBeNull();
    expect(Number(contrast![1])).toBe(SECURITY_EVIDENCE.withoutAuthorityUnauthorized);
    expect(Number(contrast![2])).toBe(SECURITY_EVIDENCE.attempts);
  });

  /* The denominator's composition, pinned the same way the headline is.
     `320` is a cross product, and the panel now says so on screen. That sentence is only worth
     having if its three factors cannot drift from the harness: the document states them in one
     place, so they are parsed back out of it and the multiplication is checked as well — a set of
     factors that no longer multiplies to the attempts is a wrong sentence even if each number was
     transcribed correctly. */
  it("matches the cross product the document decomposes", () => {
    const text = readFileSync(DOCUMENT, "utf-8");
    const spread = text.match(
      /\*\*(\d+) delivery vectors × (\d+) external-effect\s+tools\*\* — so (\d+) payloads become (\d+) attempts/
    );
    expect(spread).not.toBeNull();
    expect(Number(spread![1])).toBe(SECURITY_EVIDENCE.deliveryRoutes);
    expect(Number(spread![2])).toBe(SECURITY_EVIDENCE.effectTools);
    expect(Number(spread![3])).toBe(SECURITY_EVIDENCE.payloads);
    expect(Number(spread![4])).toBe(SECURITY_EVIDENCE.attempts);
  });

  it("carries factors that multiply to the attempts it publishes", () => {
    const { payloads, deliveryRoutes, effectTools, attempts } = SECURITY_EVIDENCE;
    expect(payloads * deliveryRoutes * effectTools).toBe(attempts);
    expect(payloads).toBeLessThan(attempts);
  });

  it("names the reproduce command the document actually gives", () => {
    const text = readFileSync(DOCUMENT, "utf-8");
    expect(text).toContain(SECURITY_EVIDENCE.reproduceCommand);
  });
});
