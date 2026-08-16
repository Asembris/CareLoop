/* The stack is up, same-origin, and the app rendered.
 *
 * Deliberately the smallest thing that can fail for a real reason. If this is red, nothing else in
 * the suite is worth reading: either a server did not start, the bundle is missing, or the browser
 * could not reach the origin.
 */

import { expect, test } from "@playwright/test";

test.describe("the stack", () => {
  test("serves the API and the app from one origin", async ({ page, request }) => {
    const health = await request.get("/api/health");
    expect(health.status()).toBe(200);
    expect(await health.json()).toEqual({ status: "ok" });

    await page.goto("/");

    // Role and accessible name, never a class or a DOM position. This is the locator policy for
    // the whole suite: the frontend is going to be reworked visually before submission, and a
    // suite keyed to structure would have to be rewritten alongside it.
    //
    // `exact: true` on the nav items, because Playwright matches an accessible name as a
    // case-insensitive *substring* by default and the logo's "CareLoop — home" contains "Home".
    // Without it the locator resolves to two elements and fails strict mode — which is Playwright
    // refusing to guess, and the right answer is to say which one rather than to take the first.
    await expect(page.getByRole("button", { name: "CareLoop — home" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Home", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Trust", exact: true })).toBeVisible();
  });

  test("marks every API response uncacheable", async ({ request }) => {
    // The dashboard polls `GET /api/cases` to watch a run proceed, so a cached response reads
    // exactly like a hung backend. Asserted here because it is set by middleware at the origin
    // rather than by one deployment's CDN configuration.
    const health = await request.get("/api/health");
    expect(health.headers()["cache-control"]).toBe("no-store");
  });
});
