/* Direct-load routing, on every route and on paths that match none.
 *
 * **Direct load is the whole point.** The 119 Vitest tests run under jsdom, which has no real
 * navigation and no origin server, so no existing test can express a routing defect at all: they
 * mount a component tree at a path the test chose. Here the browser asks the origin for the path,
 * the origin answers `index.html`, and React Router decides what that means — which is the sequence
 * that is actually broken in production.
 *
 * Every route asserts a `main` landmark. That is the precise shape of the defect: the header, the
 * two closed drawers and nothing else, because `<Routes>` matched nothing and rendered null. A
 * title check would pass on a blank page, and a text check would tie routing to copy.
 *
 * **Measured red before the repair, on the commit that added this file** — six failures, and only
 * the six. The four `direct load` tests passed unchanged, which is what makes the other six a
 * finding rather than a broken fixture:
 *
 *     ok  1 direct load › the dashboard renders
 *     ok  2 direct load › the scenario launcher renders
 *     ok  3 direct load › the trust page renders
 *     ok  4 direct load › an unknown case id reaches the recovery state, not the not-found page
 *     x   5 /under-the-hood — a drawer, mistaken for a page — renders the not-found page
 *     x   6 /under-the-hood offers a way back to the dashboard
 *     x   7 /nonsense — an address that never existed — renders the not-found page
 *     x   8 /nonsense offers a way back to the dashboard
 *     x   9 /case/ — the case route with its id missing — renders the not-found page
 *     x  10 /case/ offers a way back to the dashboard
 *
 *     Error: expect(locator).toBeVisible() failed
 *     Locator: getByRole('main')
 *     Error: element(s) not found
 *
 * All three unmatched paths answered **200** and rendered **no `main` at all**. The status is the
 * origin doing the right thing and the missing landmark is the router doing nothing, which is
 * exactly the division of responsibility the defect report described.
 */

import { expect, test } from "@playwright/test";

/** Paths that match no route. All three must reach the same recovery surface. */
const UNMATCHED = [
  // The reported defect. Reachable by typed URL, bookmark or shared link — never by clicking,
  // because "Under the hood" is a drawer toggle in the header rather than a nav link.
  { path: "/under-the-hood", why: "a drawer, mistaken for a page" },
  { path: "/nonsense", why: "an address that never existed" },
  // No id at all, so there is no case for the detail route to be about. This is deliberately not
  // the unknown-case recovery state — see the assertion below.
  { path: "/case/", why: "the case route with its id missing" }
];

test.describe("direct load", () => {
  test("the dashboard renders", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  });

  test("the scenario launcher renders", async ({ page }) => {
    await page.goto("/demo");
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByRole("heading", { name: "See CareLoop work." })).toBeVisible();
  });

  test("the trust page renders", async ({ page }) => {
    await page.goto("/trust");
    await expect(page.getByRole("main")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "The model never authorizes its own actions." })
    ).toBeVisible();
  });

  test("an unknown case id reaches the recovery state, not the not-found page", async ({ page }) => {
    // `/case/:caseId` matched — there simply is no such case. That is a different statement from
    // "this address is not part of CareLoop", and the product says so in different words. Asserted
    // separately from the unmatched paths so a future catch-all cannot quietly swallow it.
    await page.goto("/case/case-that-never-existed");
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByRole("heading", { name: "That case is no longer here." })).toBeVisible();
  });
});

test.describe("a path that matches no route", () => {
  for (const { path, why } of UNMATCHED) {
    test(`${path} — ${why} — renders the not-found page`, async ({ page }) => {
      const response = await page.goto(path);

      // The origin serves the SPA shell for any unmatched path, by design: `careloop/api/static.py`
      // returns `index.html` so client-side routing can take it. So this is a 200 and the question
      // is entirely what React Router does with it.
      expect(response?.status()).toBe(200);

      await expect(page.getByRole("main")).toBeVisible();
      await expect(page.getByRole("heading", { name: "That page isn't here." })).toBeVisible();
    });

    test(`${path} offers a way back to the dashboard`, async ({ page }) => {
      await page.goto(path);

      // **"← Home", not "Home".** The header carries a "Home" nav button on every page including a
      // blank one, so asserting on that name would pass against the defect this file exists to
      // catch. The recovery control is the page's own, and it shares its wording with the
      // unknown-case state in `CasePage` — the same situation deserves the same words.
      await page.getByRole("button", { name: "← Home" }).click();

      await expect(page).toHaveURL(/\/$/);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    });
  }
});
