/* The first-minute judge spine, walked end to end the way a cold judge walks it.
 *
 * Two beats, in order, all through the UI: the referral doing useful work and being verified
 * against the clinic's own record, then the deterministic boundary test refusing a deliberately
 * supplied dangerous proposal with nothing performed. Every stop asserts the thing the judge is
 * supposed to learn there — the thesis on the cold landing, START HERE on exactly one card, the
 * independent read on the settled case, the quiet next-step link, the supplied-proposal honesty
 * note and the zero.
 *
 * Recovery is deliberately not part of this walk. The spine's last assertion is that it is
 * *offered* — the adversarial case links onward — because minute two is a choice, not a stop.
 */

import { expect, test, type Page } from "./base";

/** A whole graph run against a real socket — the same budget `adversarial.spec.ts` uses. */
const RUN_TIMEOUT = 60_000;

async function landCold(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByTestId("judge-intro")).toBeVisible();
}

test.describe("a cold judge walks the two-beat spine", () => {
  test.setTimeout(RUN_TIMEOUT * 3);

  test("from an unexplained empty inbox to a refused attack, with every claim on the way", async ({
    page
  }) => {
    /* 1 — the cold landing states the thesis, human purpose first. */
    await landCold(page);
    const intro = page.getByTestId("judge-intro");
    await expect(intro).toContainText("family caregivers");
    await expect(intro).toContainText("MODEL PROPOSES");
    await expect(intro).toContainText("POLICY AUTHORIZES");
    await expect(intro).toContainText("EXTERNAL SYSTEM VERIFIES");

    /* 2 — its one call to action lands on the launcher, where exactly one card says START HERE
       and it is the referral. */
    await page.getByRole("button", { name: /See CareLoop work/ }).click();
    await expect(page.getByRole("heading", { name: "See CareLoop work." })).toBeVisible();
    await expect(page.getByText("START HERE")).toHaveCount(1);
    await expect(
      page.getByRole("button", { name: /Schedule a cardiology referral/ })
    ).toContainText("START HERE");

    /* 3 — beat one. The referral runs to a completion that names its verification. */
    await page.getByText("Schedule a cardiology referral").click();
    await page.waitForURL("**/");
    await expect(page.getByText("Done · independently verified")).toBeVisible({
      timeout: RUN_TIMEOUT
    });

    /* 4 — the settled case carries the independent read: the plain sentence, the contrast with
       CareLoop's own report, and the exact recorded word — bound to the projected observation,
       which is what `behaviours.test.tsx` proves by rendering none of this without one. */
    await page.getByText("Cardiology referral", { exact: true }).click();
    await page.waitForURL("**/case/demo-referral");
    await expect(page.getByText("Confirmed by an independent read")).toBeVisible();
    await expect(page.getByText("MATCHED", { exact: true })).toBeVisible();
    await expect(
      page.getByText("This is based on the clinic's record, not CareLoop's own report.")
    ).toBeVisible();
    await expect(page.getByText("Clinic record checked independently — match")).toBeVisible();

    /* 5 — the quiet next step exists, and it goes to the launcher rather than running anything:
       the boundary card's own framing must be read before it is pressed. */
    await page
      .getByRole("button", { name: "Next: test what happens when the proposal itself is unsafe →" })
      .click();
    await page.waitForURL("**/demo");
    await expect(page.getByRole("heading", { name: "See CareLoop work." })).toBeVisible();
    await expect(page.getByText("Test the boundary →")).toBeVisible();

    /* 6 — beat two. The boundary test parks with nothing safe to do, and the dashboard offers
       the case the way it offers every decision. */
    await page.getByText("Malicious document").click();
    await page.waitForURL("**/");
    await expect(page.getByText("No safe action is available")).toBeVisible({
      timeout: RUN_TIMEOUT
    });
    await page.getByRole("button", { name: /Review situation/ }).click();
    await page.waitForURL("**/case/demo-adversarial");

    /* 7 — the honesty note frames the refusal before anything else can be misread, and the zero
       is the claim: nothing was performed, and nobody was asked to approve anything. */
    const note = page.getByLabel("Adversarial test");
    await expect(note).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(note).toContainText("deliberately supplies the dangerous proposal");
    await expect(note).toContainText("EXTERNAL ACTIONS PERFORMED");
    await expect(note).toContainText("No action was prepared and none was recorded");
    await expect(note).toContainText("did not ask you to approve");
    await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);

    /* 8 — minute two is offered, not forced: the recovery link onward, and the boundary
       explanation into My Boundaries. */
    await expect(
      page.getByRole("button", { name: "Next: see how CareLoop handles a lost external reply →" })
    ).toBeVisible();
    await page.getByRole("link", { name: /Why this boundary exists/ }).click();
    await page.waitForURL("**/trust#send_email");
    await expect(
      page.getByRole("heading", { name: "What CareLoop may do, and what it will not." })
    ).toBeVisible();
  });
});

test.describe("the spine's new surfaces hold on a phone", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("the framed cold landing and the guided launcher do not overflow", async ({ page }) => {
    await landCold(page);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      )
    ).toBeLessThanOrEqual(0);

    await page.goto("/demo");
    await expect(page.getByText("START HERE")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      )
    ).toBeLessThanOrEqual(0);
  });
});
