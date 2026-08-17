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

/**
 * Wait until nothing on the page is still animating.
 *
 * **Measured, and it changes the answer.** Every page in this product enters on a `clRise` or
 * `clFade` opacity animation. Scanned mid-flight, axe resolves the *blended* colour and reports
 * results like `1.19:1` for the demo launcher's 52px heading — text that is merely part-way
 * through fading in, not text anybody could fail to read. A suite that reported those as contrast
 * defects would be reporting its own timing, and the first person to investigate would rightly
 * stop trusting the whole layer.
 *
 * **Awaiting once is not enough, and that also had to be measured.** Pages fetch their case before
 * rendering it, so the entrance animation begins *after* a single check has already found the
 * document quiet — scanning then produced ratios like `1.03:1`, which is not a colour choice,
 * it is an element at four percent opacity. So this waits, re-checks, and only stops when a fresh
 * look finds nothing running.
 *
 * Infinite animations are skipped rather than waited on, since `finished` never resolves for one.
 */
/**
 * Put every animation on the page at its end state, and keep it there.
 *
 * **Waiting for a settled moment does not work here, and that was measured rather than assumed.**
 * Three identical runs of these scans failed 3, then 2, then 6 of ten — including states that had
 * passed twice — because axe was photographing a moving page. Every surface enters on a `clRise` or
 * `clFade` opacity animation, and scanned mid-flight axe resolves the *blended* colour: ratios like
 * `1.03:1` for a 52px heading, which is not a colour choice, it is an element at four percent
 * opacity.
 *
 * Two waits were tried before this. Awaiting `getAnimations()` once is not enough, because the
 * pages fetch their case before rendering it, so the animation starts after a single check has
 * already found the document quiet. Waiting for the network to go idle first — the dashboard polls
 * `GET /api/cases` about once a second and every response re-renders — made nine of ten scans time
 * out at 30s without ever reaching a quiet window.
 *
 * So the page is made deterministic instead of waited for. `finish()` jumps every running animation
 * to its end, and the injected stylesheet stops any later re-render from starting another. Nothing
 * about the palette changes: this removes the transient blend, which is precisely the thing a
 * contrast scan must not measure. What it deliberately does *not* cover is contrast **during** a
 * transition — a real but separate question, and one no fixed-duration wait would answer reliably
 * either.
 */
export async function freezeAnimations(page: Page): Promise<void> {
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
    }

    /* Scroll-reveal sections are transparent until they are scrolled to, and a scan reads the
       whole document rather than whatever is currently on screen. Forcing them visible makes
       *more* of the page scannable, which is why it belongs beside the frozen animations rather
       than being an exemption from the scan: without it, everything below the fold on the trust
       and case pages would be measured at zero opacity and reported as undecided. */
    [data-reveal] {
      opacity: 1 !important;
      transform: none !important;
    }`
  });

  await page.evaluate(() => {
    for (const animation of document.getAnimations()) animation.finish();
  });
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
  await freezeAnimations(page);
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
