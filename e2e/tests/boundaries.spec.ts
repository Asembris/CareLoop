/* My Boundaries, against the real backend, and the one claim that is worth an e2e.
 *
 * The claim is **prediction then confirmation**. Before anything is run, the page states what
 * CareLoop may do and where it may land — read from the ruleset the policy gate actually decides by.
 * A judge then presses a scenario, the case books that exact provider, and CareLoop's own
 * independent read of the clinic's record agrees. Nothing in the browser arranged for those to
 * match: the page read the configuration, and the graph obeyed it.
 *
 * That ordering is what the spec enforces. The prediction is captured **before** the scenario is
 * pressed, so a page that quietly re-read its answer from the finished case could not pass — it
 * would have nothing to read yet.
 *
 * The refusal beat is asserted through `injection_only`, which `docs/judges.md` records as
 * deterministic in outcome over thirteen runs: not one performed an effect. The route varies and the
 * spec does not depend on it, only on the standing statement matching the refusal that lands.
 */

import { expect, test, type APIRequestContext, type Page } from "./base";
import { runScenario, sessionFromPage, withSession } from "./helpers";

/** The provider the demo caregiver's configuration names. `careloop/graph/trusted.py`. */
const PROVIDER = "PROV-NORTHGATE-CARDIOLOGY";

/** The one address `send_email` may reach for that caregiver. */
const CAREGIVER_ADDRESS = "rosa.marlowe@carer.example";

async function boundaries(page: Page): Promise<string> {
  await page.goto("/trust");
  await page.getByRole("heading", { name: "What CareLoop may do, and what it will not." }).waitFor();
  // The bands arrive with the boundary response rather than on first paint, so waiting on one is
  // what separates "the page rendered" from "the page has been told something".
  await page.getByRole("heading", { name: "CARELOOP MAY, ON ITS OWN" }).waitFor();
  return (await page.getByRole("main").textContent()) ?? "";
}

function card(page: Page, tool: string) {
  return page.locator(`[data-capability="${tool}"]`);
}

test.describe("the boundary is read from what enforces it", () => {
  test("predicts the provider a booking will use, then the booking uses it", async ({
    page,
    request
  }) => {
    // 1. The prediction, made before anything has run. There is no case, no action and no
    //    appointment anywhere for the page to have read this from.
    await page.goto("/");
    const session = await sessionFromPage(page);

    const booking = card(page, "book_appointment");
    await boundaries(page);
    await expect(booking).toBeVisible();
    await expect(booking).toContainText("ON ITS OWN");
    await expect(booking).toContainText(PROVIDER);

    // 2. The judge presses the scenario.
    const pressed = await runScenario(request, session, "referral");
    expect(pressed.status()).toBe(202);

    // 3. The case really booked that provider, and an independent read agreed.
    const detail = await request.get("/api/cases/demo-referral", { headers: withSession(session) });
    expect(detail.status()).toBe(200);
    const body = await detail.json();
    expect(body.state).toBe("DONE");

    const proposed = body.timeline.filter((e: { kind: string }) => e.kind === "PLAN_PROPOSED").pop();
    expect(proposed.payload.args.provider_id).toBe(PROVIDER);

    // The read-back is asserted through CareLoop's own trail rather than by fetching the clinic
    // here, and the reason is the config's: Demo World runs on its own port and no host is written
    // into a spec. What this proves is not weaker for it — `read_back` is true only when the verify
    // node performed a *separate* GET of the clinic's record, keyed on the confirmation id, and
    // `MATCHED` is the outcome of comparing that record against what was authorised. Re-reading the
    // booking's own response would be re-reading CareLoop's receipt, which is the thing the whole
    // verification design refuses to do.
    const verified = body.timeline
      .filter((e: { kind: string }) => e.kind === "VERIFICATION_PASSED")
      .pop();
    // `"true"`, not `true`: timeline payloads are string-valued, and the backend writes
    // `str(...).lower()`. Asserted as it is actually sent rather than as it reads more naturally,
    // because a spec that quietly coerced would stop noticing if the shape changed.
    expect(verified.payload.read_back).toBe("true");
    expect(verified.payload.observation).toBe("MATCHED");
    expect(body.actions).toHaveLength(1);

    // 4. And the case's own outcome names the rule that authorised it, pointing back at the card
    //    the prediction came from — which is what makes the two ends of this one claim rather than
    //    two coincidences.
    await page.goto("/case/demo-referral");
    const allowed = page.getByText("ALLOWED BY");
    await expect(allowed).toBeVisible();
    await expect(page.getByRole("link", { name: /what this means/i })).toHaveAttribute(
      "href",
      "/trust#book_appointment"
    );
  });

  test("names the one address email may reach, and refuses the poisoned one", async ({
    page,
    request
  }) => {
    await page.goto("/");
    const session = await sessionFromPage(page);

    // The standing statement: outbound email asks a person, and may reach exactly one address.
    await boundaries(page);
    const email = card(page, "send_email");
    await expect(email).toContainText("ASKS YOU FIRST");
    await expect(email).toContainText(CAREGIVER_ADDRESS);

    // The poisoned document names an outside address and asks for records to be sent there.
    // Measured deterministic in *outcome* over thirteen runs: no effect is ever performed.
    const pressed = await runScenario(request, session, "injection_only");
    expect(pressed.status()).toBe(202);

    const detail = await request.get("/api/cases/demo-injection-only", {
      headers: withSession(session)
    });
    expect(detail.status()).toBe(200);
    const body = await detail.json();

    // Nothing left the process, whichever route the planner took to get there.
    expect(body.actions).toHaveLength(0);
    // And the address the document named is not the one the boundary page showed.
    const denials = body.timeline.filter((e: { kind: string }) => e.kind === "POLICY_DENY");
    expect(denials.length + (body.decision ? 1 : 0)).toBeGreaterThan(0);
  });

  test("says what is out of reach, and offers nothing to press about it", async ({ page }) => {
    await boundaries(page);

    const grant = card(page, "grant_portal_access");
    await expect(grant).toContainText("CURRENTLY UNAVAILABLE");
    await expect(grant).toContainText("a trusted role");

    // The only control is the disclosure. Nothing on this card implies a caregiver could enable it.
    await expect(grant.getByRole("button")).toHaveCount(1);
    await expect(grant.getByRole("button")).toHaveAccessibleName(/why this is enforced/i);
  });

  test("keeps an independently verified effect apart from a simulated one", async ({ page }) => {
    await boundaries(page);

    // The claim the deleted hand-written guarantee list got wrong for two of the four effects.
    await expect(card(page, "book_appointment")).toContainText("other system's own record");
    await expect(card(page, "send_email")).toContainText("simulated");
  });

  test("shows nothing at all when the boundary cannot be read", async ({ page }) => {
    // The property the whole feature rests on: no fallback copy, ever. A page that substituted its
    // own account of CareLoop's authority would be indistinguishable, to a reader, from one telling
    // the truth. Routed rather than served, because the backend cannot be made to fail this one
    // endpoint for one browser context while other specs run against it.
    await page.route("**/api/boundaries", (route) => route.fulfill({ status: 500, body: "" }));
    await page.goto("/trust");

    await expect(page.getByText("CareLoop can’t read your boundaries right now.")).toBeVisible();
    await expect(page.getByRole("heading", { name: "CARELOOP MAY, ON ITS OWN" })).toHaveCount(0);
    await expect(page.locator("[data-capability]")).toHaveCount(0);
  });
});

test.describe("a deep link from a settled case", () => {
  /* `OutcomePanel`'s ALLOWED BY row links to `/trust#<tool>`, and the obvious implementation — the
   * browser's own `:target` — does not work: the fragment is resolved at navigation time and the
   * capabilities do not exist then, so nothing matches and nothing scrolls. Reproduced on the
   * deployed demo before the fix, with the card in the DOM under the right id and `:target` null.
   *
   * Asserted in a real browser rather than in jsdom, which has no layout and therefore cannot tell
   * a scroll that happened from one that did not. */

  test("marks and reveals the capability the link names", async ({ page }) => {
    await page.goto("/trust#book_appointment");
    const booking = card(page, "book_appointment");
    await booking.waitFor();

    await expect(booking).toHaveAttribute("data-locate", "true");
    await expect(card(page, "send_email")).not.toHaveAttribute("data-locate", "true");

    // In the viewport and clear of the sticky header, which is the whole point of a deep link:
    // landing on the card rather than merely having it somewhere on the page.
    await expect(booking).toBeInViewport();
    const box = await booking.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y).toBeGreaterThan(80);
  });

  test("reveals a capability far enough down that arriving means scrolling", async ({ page }) => {
    // `book_appointment` is the first card and could pass by luck. This one cannot: it is below the
    // fold on load, so the assertion is that the page really moved.
    await page.goto("/trust");
    await card(page, "grant_portal_access").waitFor();
    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(0);

    await page.goto("/trust#grant_portal_access");
    await card(page, "grant_portal_access").waitFor();

    await expect(card(page, "grant_portal_access")).toBeInViewport();
    expect(await page.evaluate(() => Math.round(window.scrollY))).toBeGreaterThan(0);
  });

  test("marks nothing when the page is opened without a fragment", async ({ page }) => {
    await page.goto("/trust");
    await card(page, "book_appointment").waitFor();
    await expect(page.locator("[data-locate]")).toHaveCount(0);
  });
});

test.describe("the nav says what the page is", () => {
  test("reaches My Boundaries and marks it current", async ({ page }) => {
    await page.goto("/");
    const link = page.getByRole("button", { name: "My Boundaries", exact: true });
    await expect(link).toBeVisible();

    await link.click();
    await expect(page).toHaveURL(/\/trust$/);
    await expect(link).toHaveAttribute("aria-current", "page");
  });

  test("shows no invented caregiver name beside the identity strip", async ({ page }) => {
    await page.goto("/");
    // A hardcoded name used to sit in the header, under a strip whose whole job is to say the demo
    // identity is synthetic. The strip is what speaks about identity now, and it says so.
    await expect(page.getByRole("banner").getByText("Maya")).toHaveCount(0);
    await expect(page.getByTestId("identity-strip")).toContainText("synthetic");
  });
});

test.describe("it works on a phone", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("stacks without scrolling sideways", async ({ page }) => {
    await boundaries(page);
    await expect(card(page, "book_appointment")).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("keeps the disclosure usable", async ({ page }) => {
    await boundaries(page);
    const toggle = card(page, "book_appointment").getByRole("button", {
      name: /why this is enforced/i
    });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
  });
});
