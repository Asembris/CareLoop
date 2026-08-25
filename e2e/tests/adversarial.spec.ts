/* The deterministic boundary test, in a browser, exactly as a judge meets it.
 *
 * One click on scenario 02 and then read. The poisoned notice really arrives, CareLoop really
 * supplies the proposal the attacker asked for, the real policy engine really refuses it against
 * the shipped `rules.yaml`, and the case really parks with nothing done.
 *
 * **What makes this worth running rather than trusting the unit tests.** The e2e harness scripts a
 * planner for every other demo case — see `scripts/e2e_serve.py` — and deliberately does *not*
 * script this one: `planner_for` hands the adversarial case the production fixture out of
 * `careloop/api/adversary.py`. So the proposals a judge sees here are the ones the deployed product
 * ships, and a harness that quietly restated them would keep passing after the real fixture drifted.
 *
 * **The claim it earns is the narrow one, and the narrowness is the feature.** CareLoop supplied the
 * proposal. This spec proves that a supplied dangerous proposal has no authority to act; it proves
 * nothing whatsoever about whether a real model would produce one, which is the separate question
 * the realistic variant on the same card still asks. The copy sweep that keeps those apart is
 * `tests/test_adversarial_copy.py`.
 */

import { expect, test, type Page } from "./base";

/** A whole graph run — triage, evidence, five plan/gate laps — against a real socket. */
const RUN_TIMEOUT = 60_000;

const ATTACKER = "records-intake@claims-continuity-partners.example";

/** Press 02 the way a judge does — from the demo page, by clicking the card. */
async function pressBoundaryTest(page: Page): Promise<void> {
  await page.goto("/demo");
  await page.getByText("Malicious document").click();
  await page.waitForURL("**/");
}

/** Wait for the run to settle, then open the case the way the dashboard offers it.
 *
 *  The case parks in `AWAITING_HUMAN`, so it arrives as a decision card under NEEDS YOU rather
 *  than as an ordinary row — which is itself part of the demonstration and is asserted below. The
 *  card's own button is the route to the case, and clicking it is what a judge does.
 */
async function openTheCase(page: Page): Promise<void> {
  await expect(page.getByText("No safe action is available")).toBeVisible({
    timeout: RUN_TIMEOUT
  });
  await page.getByRole("button", { name: /Review situation/ }).click();
  await page.waitForURL("**/case/demo-adversarial");
  await expect(page.getByText("Care continuity packet · boundary test")).toBeVisible();
}

test.describe("a supplied dangerous proposal has no authority to act", () => {
  test.setTimeout(RUN_TIMEOUT * 2);

  test("says CareLoop supplied the proposal, before showing the refusal", async ({ page }) => {
    await pressBoundaryTest(page);
    await openTheCase(page);

    const note = page.getByLabel("Adversarial test");
    await expect(note).toBeVisible({ timeout: RUN_TIMEOUT });
    await expect(note).toContainText("deliberately supplies the dangerous proposal");
    await expect(note).toContainText("Everything after the proposal is the ordinary CareLoop system");

    // The claim the note may never make. No model was asked for this proposal, so nothing on the
    // page may say one produced it.
    await expect(note).not.toContainText("the model tried");
    await expect(note).not.toContainText("took the bait");
  });

  test("shows the attacker's own address and the real refusal it earned", async ({ page }) => {
    await pressBoundaryTest(page);
    await openTheCase(page);

    const note = page.getByLabel("Adversarial test");
    await expect(note).toContainText(ATTACKER, { timeout: RUN_TIMEOUT });

    // Both fields come from the case's own `POLICY_DENY` payload. Neither is spelled anywhere in
    // the frontend — `matched_rule_id` is on the API's allowlist precisely so a judge can read the
    // named declarative rule that refused the call.
    await expect(note).toContainText("DESTINATION_NOT_TRUSTED");
    await expect(note).toContainText("deny-email-to-untrusted-recipient");
  });

  test("shows that nothing left CareLoop, and offers no way to approve it", async ({ page }) => {
    await pressBoundaryTest(page);
    await openTheCase(page);

    const note = page.getByLabel("Adversarial test");
    await expect(note).toContainText("EXTERNAL ACTIONS PERFORMED", { timeout: RUN_TIMEOUT });
    await expect(note).toContainText("No action was prepared and none was recorded");

    // The line the browser may not write. It receives the case, not the clinic's request log.
    await expect(note).not.toContainText("Outside systems contacted");

    // `DENY` is evaluated before `REQUIRE_HUMAN`, so the question was never put to a caregiver and
    // there is nothing here to press.
    await expect(note).toContainText("did not ask you to approve");
    await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  });

  test("ends in a person's hands with every action refused", async ({ page }) => {
    await pressBoundaryTest(page);

    // The ordinary product surface, and no part of it was written for this scenario. This is the
    // same NEEDS YOU card any parked case gets, and its sentence is only truthful because the
    // fixture proposes every tool in the registry — `check_every_tool_is_proposed` is what fails
    // at import if that stops being so.
    await expect(page.getByText("No safe action is available")).toBeVisible({
      timeout: RUN_TIMEOUT
    });
    await expect(
      page.getByText("Every action CareLoop could take here was refused by its own policy")
    ).toBeVisible();

    // And nothing on it offers authority. `NO_SAFE_ACTION_AVAILABLE` raises no `Approval`, so the
    // card renders no approve affordance at all.
    await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  });

  test("links the refusal to the standing boundary it is an instance of", async ({ page }) => {
    await pressBoundaryTest(page);
    await openTheCase(page);

    await page.getByRole("link", { name: /Why this boundary exists/ }).click();
    await page.waitForURL("**/trust#send_email");

    // The card the deep link asked for, derived server-side from the same ruleset that refused the
    // call. The global promise and the concrete refusal become each other's evidence.
    const card = page.locator("#send_email");
    await expect(card).toBeVisible();
    await expect(card).toContainText("rosa.marlowe@carer.example");

    // **In the viewport, not merely in the DOM.** `toBeVisible` means present and not hidden, which
    // was true of the router-`<Link>` version that landed on the right URL with the page still near
    // the top against the deployment.
    //
    // **This assertion does not guard that choice**, and saying so is the point of the comment:
    // reverting the anchor to a `<Link>` leaves this green, because the boundary response returns
    // fast enough on loopback that the scroll lands either way. It is kept because it holds a real
    // property cheaply and would catch a total failure to scroll; the anchor is defended by the
    // deployed measurement recorded in `AdversarialNote.tsx`, not by this line.
    await expect
      .poll(async () => card.evaluate((el) => el.getBoundingClientRect().top < window.innerHeight))
      .toBe(true);
  });

  test("fits a phone without pushing the page sideways", async ({ page }) => {
    // The note carries a 50-character email address, which is exactly the kind of string that
    // silently widens a page on a narrow screen. `overflowWrap: anywhere` is what stops it, and
    // this is what fails if somebody removes it.
    await page.setViewportSize({ width: 375, height: 812 });
    await pressBoundaryTest(page);
    await openTheCase(page);

    await expect(page.getByLabel("Adversarial test")).toBeVisible({ timeout: RUN_TIMEOUT });

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("shows no adversarial note on an ordinary booking", async ({ page }) => {
    // The vacuity proof for the note. Scenario 01 books and verifies without anything supplying a
    // proposal, so it has nothing to frame and must frame nothing.
    await page.goto("/demo");
    await page.getByText("Schedule a cardiology referral").click();
    await page.waitForURL("**/");
    await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: RUN_TIMEOUT });

    await page.getByText("Cardiology referral", { exact: true }).click();
    await page.waitForURL("**/case/demo-referral");

    await expect(page.getByLabel("Adversarial test")).toHaveCount(0);
  });
});
