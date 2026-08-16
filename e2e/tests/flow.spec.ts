/* The decision flow, end to end, against the real backend.
 *
 * **Seeding goes through the same session the browser minted.** The `request` fixture and the page
 * are different clients, and under `--isolate-sessions` a different client means a different world
 * — its own store, run registry and clinic. A spec that seeded through `request` and then asserted
 * through `page` would be asserting about somebody else's cases and would pass while showing an
 * empty dashboard. `sessionFromPage` is what keeps the two in one world.
 *
 * Seeding uses `?wait=`, which the shipped frontend never passes. That is deliberate on both sides:
 * the dashboard exists to show work happening, and a spec wants the settled truth before it starts
 * asserting. What is being tested here is the decision, not the animation.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { runScenario, sessionFromPage, withSession } from "./helpers";

/** Open the dashboard, seed a scenario into the world the page is using, and show it. */
async function seed(page: Page, request: APIRequestContext, key: string): Promise<string> {
  await page.goto("/");
  const session = await sessionFromPage(page);

  const pressed = await runScenario(request, session, key);
  expect(pressed.status()).toBe(202);

  await page.reload();
  return session;
}

test.describe("a case that needs a decision", () => {
  test("shows the exact call, and approving it carries the case to completion", async ({
    page,
    request
  }) => {
    await seed(page, request, "resume");

    // The card is an <article>; its title is the tool phrase the backend's proposal resolved to.
    const card = page.getByRole("article");
    await expect(card).toBeVisible();
    // `exact: true`, because text matching is a case-insensitive substring by default and the
    // card's own body says "…needs your authority before it acts".
    await expect(page.getByText("NEEDS YOU", { exact: true })).toBeVisible();
    await expect(card.getByRole("heading", { name: "Send an email" })).toBeVisible();

    // "This is exactly what it will do" is the product's claim, and the arguments rendered are
    // recovered by the backend from the PLAN_PROPOSED entry the approval's digest covers.
    await expect(card.getByText("rosa.marlowe@carer.example")).toBeVisible();

    await card.getByRole("button", { name: "Approve" }).click();

    // Generous, because the dashboard paces the display one state per poll and the run behind it
    // is real. Web-first assertion rather than a sleep: it settles as soon as the column moves.
    await expect(page.getByText("COMPLETED")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByRole("article")).toBeHidden();
  });

  test("declining it is recorded, and CareLoop looks for another way", async ({ page, request }) => {
    const session = await seed(page, request, "resume");

    const card = page.getByRole("article");
    await expect(card.getByRole("heading", { name: "Send an email" })).toBeVisible();

    // "Not now" rather than "Reject": the copy differs by reason code, and this is the
    // REQUIRES_HUMAN_APPROVAL card. Asserting the label the caregiver actually sees is the point.
    await card.getByRole("button", { name: "Not now" }).click();

    // The load-bearing assertion is the durable one. A rejected proposal is not a failed case —
    // the planner gets another attempt — so the card coming back looks identical to nothing having
    // happened. The audit trail is where the difference is, and it is what the product claims.
    await expect
      .poll(
        async () => {
          const detail = await request.get("/api/cases/demo-resume", {
            headers: withSession(session)
          });
          if (detail.status() !== 200) return [];
          return (await detail.json()).timeline.map((entry: { kind: string }) => entry.kind);
        },
        { timeout: 60_000 }
      )
      .toContain("APPROVAL_REJECTED");
  });
});

test.describe("a case that finished", () => {
  test("its detail page shows the independent read-back, not its own receipt", async ({
    page,
    request
  }) => {
    await seed(page, request, "referral");

    await page.goto("/case/demo-referral");
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

    // The whole claim of the project in one line of copy. `book_appointment` really was performed
    // over HTTP against Demo World, and verification then performed a *separate* GET keyed on the
    // confirmation id and compared every declared argument. The other wording — "Checked against
    // what was authorized" — is what a simulated effect produces, so this asserts the strong case.
    await expect(page.getByText("Clinic record checked independently — match")).toBeVisible({
      timeout: 30_000
    });
  });
});
