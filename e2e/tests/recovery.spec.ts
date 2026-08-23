/* The judge-facing recovery, in a browser, with nobody touching anything.
 *
 * One click on scenario 04 and then hands off the keyboard. The clinic really creates the
 * appointment, CareLoop's transport really discards the answer, the case really reaches
 * `WAITING_EXTERNAL`, the shipped five-second wake really fires, and the frozen identity is really
 * re-issued — and the only thing this spec does between the click and `COMPLETED` is look.
 *
 * **What it deliberately never does**, because each one would make the proof mean less:
 *
 *   page.reload()          — the transition has to arrive on its own poll
 *   a recovery button      — there is none, and there must not be
 *   the re-check endpoint  — that is a *caregiver* asking for another look, a different property
 *   a second scenario press
 *   any API call at all between the click and the assertions
 *
 * **The claim it earns is the narrow one.** One appointment, one action identity, one plan — not
 * "the request was only sent once". Reconciliation issues a second `POST` under the identity frozen
 * before the first, which is exactly why the clinic returns the appointment it already made.
 * `tests/test_recovery_proof.py` measures both numbers and `tests/test_recovery_copy.py` sweeps
 * this file along with the rest of the product for any sentence that denies the second request.
 */

import { expect, test, type APIRequestContext, type Page } from "./base";
import { mintSession, runScenario, withSession } from "./helpers";

/** Generous, and it has to be: a real graph, a real socket, and a real five-second wake in the
 *  middle of it. Web-first assertions settle as soon as the column moves, so the budget costs
 *  nothing when everything is working. */
const RECOVERY_TIMEOUT = 90_000;

/** Press 04 the way a judge does — from the demo page, by clicking the card. */
async function pressRecovery(page: Page): Promise<void> {
  await page.goto("/demo");
  await page.getByText("The answer gets lost").click();
  await page.waitForURL("**/");
}

test.describe("the answer gets lost and CareLoop works it out", () => {
  test.setTimeout(RECOVERY_TIMEOUT);

  test("recovers on its own, with no reload and nothing clicked", async ({ page }) => {
    await pressRecovery(page);

    // The ambiguity, on screen. The case is not failed and not finished: a real request left the
    // process and nothing came back, so CareLoop does not know whether the booking happened.
    await expect(page.getByText("Sent, with no answer back")).toBeVisible({
      timeout: RECOVERY_TIMEOUT
    });

    // -- from here to the end of the test, nothing is clicked, typed, reloaded or requested. --

    await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: RECOVERY_TIMEOUT });
    await expect(page.getByText("Cardiology referral · lost reply")).toBeVisible();
  });

  test("shows the recovery on the case's own audit trail", async ({ page }) => {
    await pressRecovery(page);
    await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: RECOVERY_TIMEOUT });

    await page.getByText("Cardiology referral · lost reply").click();
    await page.waitForURL("**/case/demo-recovery");

    // The state the whole feature exists to make legible, said in the register of the page.
    await expect(page.getByText("NO ANSWER FROM THE CLINIC")).toBeHidden();
    await expect(
      page.getByText("The exact booking was written down before anything was sent")
    ).toBeVisible();
    await expect(
      page.getByText("No answer came back — CareLoop does not yet know whether it happened")
    ).toBeVisible();
    await expect(
      page.getByText("Retrying the same frozen booking identity — never a new booking")
    ).toBeVisible();
    await expect(page.getByText("Clinic record checked independently — match")).toBeVisible();
  });

  test("shows the evidence block, and it claims exactly what the trail supports", async ({
    page
  }) => {
    await pressRecovery(page);
    await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: RECOVERY_TIMEOUT });

    await page.getByText("Cardiology referral · lost reply").click();
    await page.waitForURL("**/case/demo-recovery");

    const proof = page.getByTestId("recovery-proof");
    await expect(proof).toBeVisible();
    await expect(proof).toContainText("ONE APPOINTMENT — SAME ACTION SAFELY REPLAYED");
    await expect(proof).toContainText("RECORDED ACTIONS");
    await expect(proof).toContainText("PLANS MADE");
    await expect(proof).toContainText("MATCHED");

    // The one number it may not claim. Zero recovery model calls is true and is proven in the
    // backend with seams that raise if touched; the durable trail cannot establish it, so the
    // screen does not say it.
    await expect(proof).not.toContainText("Model calls");
  });

  test("does not show the evidence block on an ordinary booking", async ({ page }) => {
    // The vacuity proof for the block. Scenario 01 books and verifies without ever losing an
    // answer, so it has no recovery to evidence and must show none.
    await page.goto("/demo");
    await page.getByText("Schedule a cardiology referral").click();
    await page.waitForURL("**/");
    await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: RECOVERY_TIMEOUT });

    await page.getByText("Cardiology referral", { exact: true }).click();
    await page.waitForURL("**/case/demo-referral");

    await expect(page.getByText("Clinic record checked independently — match")).toBeVisible();
    await expect(page.getByTestId("recovery-proof")).toBeHidden();
    await expect(
      page.getByText("No answer came back — CareLoop does not yet know whether it happened")
    ).toBeHidden();
  });
});

test.describe("one visitor's lost answer is nobody else's", () => {
  test.setTimeout(RECOVERY_TIMEOUT);

  /** Poll a case's state until it settles, without touching anything that could move it. */
  async function stateOf(
    request: APIRequestContext,
    session: string,
    caseId: string
  ): Promise<string> {
    const read = await request.get(`/api/cases/${caseId}`, { headers: withSession(session) });
    expect(read.status()).toBe(200);
    return (await read.json()).state;
  }

  test("a second visitor booking at the same moment is untouched", async ({ request }) => {
    // T8. Two worlds, two clinics, two stores. The fault belongs to one case in one of them, and
    // the binding is the world's context factory handing the losing client only to that case's
    // runs — so B's booking travels a transport containing no fault at all.
    const armed = await mintSession(request);
    const ordinary = await mintSession(request);

    const strandedPress = await runScenario(request, armed, "recovery");
    expect(strandedPress.status()).toBe(202);
    expect(await stateOf(request, armed, "demo-recovery")).toBe("WAITING_EXTERNAL");

    const normalPress = await runScenario(request, ordinary, "referral");
    expect(normalPress.status()).toBe(202);

    // B books, verifies and finishes while A is still waiting on an answer that never came.
    expect(await stateOf(request, ordinary, "demo-referral")).toBe("DONE");

    // And A settles itself afterwards, on the wake it scheduled for itself — no restart, no
    // request, and nothing B did.
    await expect
      .poll(() => stateOf(request, armed, "demo-recovery"), { timeout: RECOVERY_TIMEOUT })
      .toBe("DONE");
  });

  test("a later ordinary booking in the same world is not stranded", async ({ request }) => {
    // T2 over HTTP. The loss is consumed by the booking it was armed for; the next scenario in the
    // same world behaves exactly as it always did.
    const session = await mintSession(request);

    expect((await runScenario(request, session, "recovery")).status()).toBe(202);
    await expect
      .poll(() => stateOf(request, session, "demo-recovery"), { timeout: RECOVERY_TIMEOUT })
      .toBe("DONE");

    expect((await runScenario(request, session, "referral")).status()).toBe(202);
    expect(await stateOf(request, session, "demo-referral")).toBe("DONE");
  });
});
