/* `/trust#<tool>` has to actually put the card in front of the reader.
 *
 * **The bug this is written against, measured on the deployed app at 1280x800.** Clicking a
 * `/trust#send_email` link left the page at `scrollY: 0` with the card at `top: 1634`, while a
 * `scrollIntoView({block:"center"})` issued by hand at that same moment moved it to `1417`. The
 * card was in the DOM, the highlight was on the right card, and `toBeVisible` was true throughout —
 * the reader simply could not see it. Arriving at the same URL by typing it worked, and landed at
 * exactly the card's own `scroll-margin-top`, which is the *browser's* native fragment scroll
 * rather than the page's effect: a link click gets a fresh history entry whose restored scroll
 * position is `0`, and with `history.scrollRestoration` at `auto` that restore lands after the
 * effect and wins.
 *
 * **Why this file simulates the clobber rather than waiting for it.** The loopback stack does not
 * reproduce it: measured before the fix, every local route — direct load, click, and both again
 * with `/api/boundaries` delayed by 1200ms — scrolled correctly. A regression test that only
 * clicked would therefore have been green on the broken build, which is the one thing a regression
 * test may not be. So `resetsScrollOnce` reproduces the *mechanism* — one scroll to the top,
 * arriving a few frames after the page has settled, exactly as the restore does — and the fix is
 * required to survive it. The plain click test below is kept beside it as the product assertion.
 *
 * Both tests assert the card's **position**, never `toBeVisible`, which was true the whole time the
 * feature was broken.
 */

import { expect, test, type Page } from "./base";

const RUN_TIMEOUT = 60_000;

/** Where a card has to end up for a reader to be looking at it. Matches the page's own predicate. */
async function cardIsInView(page: Page, id: string): Promise<boolean> {
  return page.evaluate((elementId) => {
    const card = document.getElementById(elementId);
    if (!card) return false;
    const { top, bottom } = card.getBoundingClientRect();
    return top <= window.innerHeight * 0.75 && bottom >= 0;
  }, id);
}

/**
 * Put the page back to the top **once**, a few frames after it settles.
 *
 * This is the browser's own post-navigation scroll restore, reproduced deterministically. It is
 * deliberately one-shot and deliberately late: an immediate reset would be beaten by any
 * implementation at all, and a repeating one would be unwinnable by any implementation at all.
 * Neither would say anything about the defect.
 */
async function resetsScrollOnce(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.addEventListener("load", () => {
      let frames = 0;
      const clobber = () => {
        frames += 1;
        if (frames < 12) {
          requestAnimationFrame(clobber);
          return;
        }
        window.scrollTo(0, 0);
      };
      requestAnimationFrame(clobber);
    });
  });
}

/** Reach the adversarial case, which is where the newer deep link lives. */
async function openTheAdversarialCase(page: Page): Promise<void> {
  await page.goto("/demo");
  await page.getByText("Malicious document").click();
  await page.waitForURL("**/");
  await expect(page.getByText("No safe action is available")).toBeVisible({ timeout: RUN_TIMEOUT });
  await page.getByRole("button", { name: /Review situation/ }).click();
  await page.waitForURL("**/case/demo-adversarial");
}

/** Reach a settled referral, which is where the older `OutcomePanel` deep link lives. */
async function openTheReferralCase(page: Page): Promise<void> {
  await page.goto("/demo");
  await page.getByText("Schedule a cardiology referral").click();
  await page.waitForURL("**/");
  await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: RUN_TIMEOUT });
  await page.getByText("Cardiology referral", { exact: true }).click();
  await page.waitForURL("**/case/demo-referral");
}

test.describe("a My Boundaries deep link brings its card into view", () => {
  test.setTimeout(RUN_TIMEOUT * 2);

  test("survives a scroll reset arriving after the page has settled", async ({ page }) => {
    /* **The regression test.** Red before the fix, green after — the one-shot scroll to the top is
       what the page previously had no answer to. */
    await resetsScrollOnce(page);
    await openTheAdversarialCase(page);

    await page.getByRole("link", { name: /Why this boundary exists/ }).click();
    await page.waitForURL("**/trust#send_email");
    await expect(page.locator("#send_email")).toBeVisible({ timeout: RUN_TIMEOUT });

    await expect.poll(() => cardIsInView(page, "send_email"), { timeout: 10_000 }).toBe(true);
  });

  test("brings the card into view on an ordinary click", async ({ page }) => {
    await openTheAdversarialCase(page);

    await page.getByRole("link", { name: /Why this boundary exists/ }).click();
    await page.waitForURL("**/trust#send_email");

    await expect.poll(() => cardIsInView(page, "send_email"), { timeout: 10_000 }).toBe(true);
  });

  test("does the same for the older outcome-panel link", async ({ page }) => {
    /* `book_appointment` is the *first* capability card, so it is already in view at `scrollY: 0`
       and would pass a naive check on a completely broken page. The clobber is what gives this
       test something to prove: after a reset to the top the card is still in view, so the assertion
       has to be paired with the scroll position to mean anything — hence both are read. */
    await resetsScrollOnce(page);
    await openTheReferralCase(page);

    await page.getByRole("link", { name: /What this means/ }).click();
    await page.waitForURL("**/trust#book_appointment");
    await expect(page.locator("#book_appointment")).toBeVisible({ timeout: RUN_TIMEOUT });

    await expect.poll(() => cardIsInView(page, "book_appointment"), { timeout: 10_000 }).toBe(true);
    /* The page's own predicate centres the card, so a first card really does move the page rather
       than being left where a reset put it. Reading the offset is what tells the two apart. */
    await expect.poll(() => page.evaluate(() => Math.round(window.scrollY)), { timeout: 10_000 })
      .toBeGreaterThan(0);
  });

  test("still works when the URL is typed rather than clicked", async ({ page }) => {
    /* The direct route was never broken and must stay that way — the fix adds a watch, it does not
       replace the browser's native fragment scroll. */
    await page.goto("/trust#send_email");
    await expect(page.locator("#send_email")).toBeVisible({ timeout: RUN_TIMEOUT });

    await expect.poll(() => cardIsInView(page, "send_email"), { timeout: 10_000 }).toBe(true);
  });

  test("plays the locate highlight on the card the fragment named", async ({ page }) => {
    await openTheAdversarialCase(page);
    await page.getByRole("link", { name: /Why this boundary exists/ }).click();
    await page.waitForURL("**/trust#send_email");

    await expect(page.locator("#send_email")).toHaveAttribute("data-locate", "true");
    await expect(page.locator("#book_appointment")).not.toHaveAttribute("data-locate", "true");
  });

  test("an unknown fragment leaves the page alone and raises nothing", async ({ page }) => {
    const failures: string[] = [];
    page.on("pageerror", (error) => failures.push(String(error)));

    await page.goto("/trust#not_a_capability");
    await expect(page.locator("#send_email")).toBeVisible({ timeout: RUN_TIMEOUT });

    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(0);
    expect(await page.locator("[data-locate='true']").count()).toBe(0);
    expect(failures).toEqual([]);
  });

  test("a reader who scrolls away is not dragged back", async ({ page }) => {
    /* The watch yields the moment somebody takes over. Without this the fix would be worse than the
       bug: a page that fought the reader for a second after every deep link. */
    await openTheAdversarialCase(page);
    await page.getByRole("link", { name: /Why this boundary exists/ }).click();
    await page.waitForURL("**/trust#send_email");
    await expect.poll(() => cardIsInView(page, "send_email"), { timeout: 10_000 }).toBe(true);

    await page.mouse.wheel(0, -4000);
    await expect.poll(() => page.evaluate(() => Math.round(window.scrollY))).toBe(0);
    /* Given a moment, it must stay where the reader put it. */
    await page.waitForTimeout(500);
    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(0);
  });

  test("back and forward still land where the browser says", async ({ page }) => {
    /* `history.scrollRestoration` is deliberately untouched, so ordinary history navigation keeps
       the browser's own behaviour. */
    await page.goto("/trust");
    await expect(page.locator("#send_email")).toBeVisible({ timeout: RUN_TIMEOUT });
    await page.evaluate(() => window.scrollTo(0, 900));
    await expect.poll(() => page.evaluate(() => Math.round(window.scrollY))).toBe(900);

    await page.goto("/demo");
    await expect(page.getByText("See CareLoop work.")).toBeVisible({ timeout: RUN_TIMEOUT });

    await page.goBack();
    await page.waitForURL("**/trust");
    await expect(page.locator("#send_email")).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect.poll(() => page.evaluate(() => Math.round(window.scrollY)), { timeout: 10_000 })
      .toBeGreaterThan(0);
  });
});
