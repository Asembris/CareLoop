/* Scroll reveal, and the four properties that keep it from ever hiding anything for good.
 *
 * This needs a real browser for a reason the development machine demonstrated the hard way: an
 * `IntersectionObserver` in a tab that is not compositing delivers no callbacks at all, so the
 * hook's watchdog fires and reveals every pending section. That is the failsafe behaving
 * correctly, and it also means a non-rendering environment cannot tell a working reveal from a
 * broken one. Playwright composites, so it can.
 */

import { expect, test } from "./base";
import { runScenario, sessionFromPage } from "./helpers";

/** Direct children of the reveal container, with the state the hook put on them. */
async function sections(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    const column = document.querySelector("main > div");
    if (!column) return [];
    return Array.from(column.children).map((el) => ({
      reveal: (el as HTMLElement).dataset.reveal ?? null,
      opacity: getComputedStyle(el).opacity,
      top: Math.round(el.getBoundingClientRect().top)
    }));
  });
}

test.describe("scroll reveal", () => {
  test("nothing on the first screen is hidden, and what is below it starts pending", async ({ page }) => {
    await page.goto("/trust");

    const initial = await sections(page);
    expect(initial.length, "the trust page renders a column of sections").toBeGreaterThan(3);

    const fold = page.viewportSize()!.height;

    // The half that matters for the reference screenshots and for a reader who never scrolls:
    // anything already on screen was never a candidate, so it carries no attribute at all.
    for (const section of initial.filter((s) => s.top < fold)) {
      expect(section.reveal, `a section at ${section.top}px is on the first screen`).toBeNull();
      expect(section.opacity).toBe("1");
    }

    // And the non-vacuity half. Without this the test above would pass just as happily against a
    // hook that did nothing whatsoever.
    const below = initial.filter((s) => s.top >= fold);
    expect(below.length, "the trust page has sections below the fold to reveal").toBeGreaterThan(0);
    expect(below.every((s) => s.reveal === "pending")).toBe(true);
    expect(below.every((s) => s.opacity === "0")).toBe(true);
  });

  test("scrolling reveals them, once, and they stay revealed", async ({ page }) => {
    await page.goto("/trust");
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));

    await expect
      .poll(async () => (await sections(page)).filter((s) => s.reveal === "pending").length)
      .toBe(0);

    // Polled rather than read once: the reveal is a 380ms transition, so reading immediately
    // after the attribute flips catches it mid-flight and asserts on an intermediate opacity.
    await expect
      .poll(async () => (await sections(page)).every((s) => s.opacity === "1"))
      .toBe(true);

    // Back to the top: a revealed section does not re-hide, so this never loops.
    await page.evaluate(() => window.scrollTo(0, 0));
    expect((await sections(page)).some((s) => s.reveal === "pending")).toBe(false);
  });

  /* On the case page rather than the trust page, because this guard needs a *focusable* control
     below the fold and only the case page has one — its evidence and document cards are buttons.
     Pointed at /trust this test skipped, which is no evidence at all for the property that stops
     a keyboard user landing on something transparent. */
  test("focus reveals a pending section before the caret can land on it", async ({ page, request }) => {
    // Seeded through the world the page itself minted — see `flow.spec.ts`. Seeding through the
    // `request` fixture instead would populate a different world and leave this asserting against
    // an empty case page.
    await page.goto("/");
    const session = await sessionFromPage(page);
    expect((await runScenario(request, session, "referral")).status()).toBe(202);

    await page.goto("/case/demo-referral");

    // The case arrives with the poller, and until it does this page renders its recovery state —
    // which has no sections at all. Reading before that is what made an earlier version of this
    // test report "no pending section" against a hook that was working perfectly.
    await expect(page.getByRole("heading", { name: "Cardiology referral" })).toBeVisible();
    await expect
      .poll(async () =>
        page.evaluate(
          () => document.querySelectorAll('main > div > [data-reveal="pending"]').length
        )
      )
      .toBeGreaterThan(0);

    const host = await page.evaluate(() => {
      const column = document.querySelector("main > div");
      if (!column) return null;
      const pending = Array.from(column.children).find(
        (el) =>
          (el as HTMLElement).dataset.reveal === "pending" && el.querySelector("button, a, [tabindex]")
      ) as HTMLElement | undefined;
      if (!pending) return null;
      (pending.querySelector("button, a, [tabindex]") as HTMLElement).focus();
      return { reveal: pending.dataset.reveal ?? null };
    });

    expect(host, "a pending section below the fold holds a focusable control").not.toBeNull();
    expect(host!.reveal, "focusing inside a pending section reveals it").toBe("shown");
  });

  test("reduced motion sets no reveal state at all", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/trust");

    // Not "revealed anyway" — the attribute is never applied, so there is no transition to zero
    // and nothing that could strand a section if the observer misbehaved.
    const all = await sections(page);
    expect(all.every((s) => s.reveal === null)).toBe(true);
    expect(all.every((s) => s.opacity === "1")).toBe(true);
  });
});
