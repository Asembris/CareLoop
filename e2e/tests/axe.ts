/* One accessibility scanner, with the three `AxeBuilder` footguns handled once.
 *
 * **`.withTags()` only.** `.withRules()` and `.withTags()` are mutually exclusive — the second call
 * replaces the first's `runOnly` — and `.options()` overwrites `runOnly` wholesale, so a single
 * `.options({ rules: … })` anywhere would silently widen the scan to every rule axe ships. Neither
 * is called here or anywhere in the suite. `.disableRules()` resets its list on every call, so it
 * takes one array rather than being called per rule.
 *
 * **WCAG 2.1 AA, and no 2.2 claim.** axe's only 2.2 rule, `target-size`, ships disabled, so a suite
 * that added `wcag22aa` would advertise a level it does not check. The tags are stated in one place
 * because a scan that quietly used a different set than its neighbour would make the whole sweep
 * incomparable.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";
import type { Result } from "axe-core";

/** What every scan in this suite runs. Nothing narrows or widens it per call site. */
export const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] as const;

/** One `incomplete` item this state is allowed to produce, and why it is allowed.
 *
 *  **Hand-written, entry by entry, and never generated from a failing run.** Auto-populating this
 *  is how "unknown" silently becomes "green" six commits from now — which is the exact vacuity this
 *  repository forbids everywhere else. A new entry is a decision somebody makes and signs, not a
 *  fix somebody applies.
 */
export interface Allowance {
  rule: string;
  why: string;
}

export function scanner(page: Page) {
  return new AxeBuilder({ page }).withTags([...WCAG_TAGS]);
}

/** Readable failure output. The rule id and where it landed, never the whole axe result object. */
function describe(results: Result[]): string[] {
  return results.map(
    (result) => `${result.id} — ${result.nodes.map((node) => node.target.join(" ")).join(", ")}`
  );
}

/**
 * Scan whatever the page is currently showing.
 *
 * **`incomplete` is asserted, not only `violations`.** axe returns a rule as incomplete when it
 * could not decide — a colour it could not resolve, an element it believes is obscured. Ignoring
 * that array converts every "unknown" into a pass, and the slide-over drawers generate exactly
 * those items. So an incomplete item fails the scan unless this state names it and says why.
 *
 * `label` is the state being scanned rather than the route, because axe reads the live DOM at call
 * time: one scan per route would test one theme and never see an open drawer.
 */
export async function scan(page: Page, label: string, allowed: Allowance[] = []): Promise<void> {
  const results = await scanner(page).analyze();

  expect(describe(results.violations), `${label}: accessibility violations`).toEqual([]);

  const permitted = new Set(allowed.map((allowance) => allowance.rule));
  const undecided = results.incomplete.filter((result) => !permitted.has(result.id));

  expect(
    describe(undecided),
    `${label}: axe could not decide these, and this state does not account for them. Add an ` +
      `Allowance with a reason, or fix the page — never widen the list to make a run green.`
  ).toEqual([]);
}
