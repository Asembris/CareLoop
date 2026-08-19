/* The session-T surfaces: connection truth, citations, the readable poisoned document, the
 * observation vocabulary and the measured trust numbers — each driven through the real backend
 * except where reality cannot produce the state, and that one exception is named.
 *
 * **One `page.route` use, justified here the way `unreachable-states.spec.ts` justifies its two.**
 * An unreachable backend cannot be produced by this suite's own stack: Playwright's `webServer`
 * keeps the API alive for every other spec running in parallel, and killing it would fail them
 * for the wrong reason. `route.abort()` on `/api/**` is one browser context experiencing the
 * outage the health probe exists to catch, while the process serves everyone else undisturbed.
 *
 * The second use, in the same test.describe and for the same reason one step over: a demo with no
 * worlds left needs all sixty-four of them held, and minting sixty-four here would fill the shared
 * backend every other spec is using. Fulfilling the mint with a **503** — the exact status
 * `SessionsAtCapacity` produces — is one browser context arriving at a full demo.
 */

import { expect, test, type APIRequestContext, type Page } from "./base";
import { runScenario, sessionFromPage, settled } from "./helpers";

async function seed(page: Page, request: APIRequestContext, key: string): Promise<string> {
  await page.goto("/");
  const session = await sessionFromPage(page);
  const pressed = await runScenario(request, session, key);
  expect(pressed.status()).toBe(202);
  return session;
}

test.describe("connection truth", () => {
  test("an unreachable backend never renders as a calm empty dashboard", async ({ page }) => {
    await page.route("**/api/**", (route) => route.abort());
    await page.goto("/");

    // The banner and the hero both say what is true. The locked success line is the one thing
    // that must NOT appear: "Nothing needs you right now." over data that could not be read is
    // an outage rendered as the product's proudest claim.
    await expect(page.getByText("CareLoop can't be reached — retrying automatically.")).toBeVisible();
    await expect(page.getByText("Nothing needs you right now.")).toHaveCount(0);
  });

  test("a full demo says so, and does not say the backend is down", async ({ page }) => {
    // Every world in use is a *different fact* from nobody answering: the service is up, it is
    // answering, and the remedy is a few minutes rather than a refresh. Rendering it as the outage
    // state would be this product making a false statement about its own health on the screen that
    // exists to say true things — and it would send whoever read it to reload, which cannot help.
    await page.route("**/api/sessions", (route) =>
      route.fulfill({ status: 503, contentType: "application/json", body: "{}" })
    );
    await page.goto("/");

    await expect(
      page.getByText("The demo is full right now — every world is in use. Trying again shortly.")
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "The demo is full right now." })
    ).toBeVisible();
    await expect(page.getByText("CareLoop can't be reached — retrying automatically.")).toHaveCount(0);
    await expect(page.getByText("Nothing needs you right now.")).toHaveCount(0);
  });
});

test.describe("citations on the spine", () => {
  test("a recorded fact carries its citation, and the chip opens the page it came from", async ({
    page,
    request
  }) => {
    await seed(page, request, "referral");
    await page.goto("/case/demo-referral");

    // The chip is the fact's real citation, rendered on the entry that recorded it.
    const chip = page.locator(".cl-source").first();
    await expect(chip).toBeVisible();
    await expect(chip).toContainText(/referral\.pdf p\.\d/);

    await chip.click();
    const lens = page.getByRole("dialog", { name: "Evidence" });
    await settled(lens);

    // The lens shows this fact's own journey — not a generic legend. A validated document fact
    // reads found → validated → never-authority, with the validator named.
    await expect(lens.getByText(/Found in referral\.pdf p\.\d — document content, untrusted/)).toBeVisible();
    await expect(lens.getByText(/Validated \(.+\) — usable as fact/)).toBeVisible();
    await expect(lens.getByText("Never a source of authority")).toBeVisible();
  });
});

test.describe("the poisoned document is readable beside the policy's answer", () => {
  test("the attack text renders as quoted data with every refusal and its rule id", async ({
    page,
    request
  }) => {
    await seed(page, request, "injection_only");
    await page.goto("/case/demo-injection-only");

    // The strip exists, counts the refusals, and names the document.
    await expect(page.getByText("SOURCE DOCUMENTS")).toBeVisible();
    await expect(page.getByText(/\d actions refused by policy/)).toBeVisible();

    await page.getByText("mediguard-continuity-notice.pdf").first().click();
    const lens = page.getByRole("dialog", { name: "Source document" });
    await settled(lens);

    // The attack itself, on screen, framed as untrusted data. This text is the malicious
    // document's own instruction — the thing that until this view existed was invisible.
    await expect(lens.getByText(/never for instructions/)).toBeVisible();
    await expect(lens.getByText(/records-intake@claims-continuity-partners\.example/).first()).toBeVisible();

    // And the policy's answer directly beside it, with the named declarative rule. The rule id
    // is exposed to this panel on purpose — invariant 6 withholds it from the model, not from a
    // person reading the audit trail.
    await expect(lens.getByText("THE POLICY'S ANSWER")).toBeVisible();
    await expect(lens.getByText("deny-upload-to-untrusted-portal")).toBeVisible();
    await expect(lens.getByText(/DESTINATION_NOT_TRUSTED/).first()).toBeVisible();
  });
});

test.describe("the observation is first-class", () => {
  test("a verified booking names MATCHED and says why observed values are withheld", async ({
    page,
    request
  }) => {
    await seed(page, request, "referral");
    await page.goto("/case/demo-referral");

    await expect(page.getByText("OBSERVATION", { exact: true })).toBeVisible();
    await expect(page.getByText("MATCHED", { exact: true })).toBeVisible();
    await expect(page.getByText(/agrees with every value CareLoop executed/)).toBeVisible();
    await expect(page.getByText(/never shown, deliberately/)).toBeVisible();
  });
});

test.describe("the trust page shows the measured pair", () => {
  test("0 of 320 faces 320 of 320, with the reproduce command", async ({ page }) => {
    await page.goto("/trust");

    // `exact`, because "320 of 320" contains "0 of 320" as a substring and the default
    // substring match resolves to both numerals at once.
    await expect(page.getByText("0 of 320", { exact: true })).toBeVisible();
    await expect(page.getByText("320 of 320", { exact: true })).toBeVisible();
    await expect(page.getByText("python -m evals.replay")).toBeVisible();

    // The recovery loop has its visual home, vocabulary spelled out.
    await expect(page.getByText("WHEN THE WORLD DISAGREES")).toBeVisible();
    await expect(page.getByText("NOT_OBSERVED", { exact: true })).toBeVisible();
  });
});

test.describe("under the hood names the stack", () => {
  test("the drawer names the Strands and AWS primitives", async ({ page, request }) => {
    await seed(page, request, "referral");
    await page.getByRole("button", { name: "Under the hood" }).click();
    const drawer = page.getByRole("dialog", { name: "Under the hood" });
    await settled(drawer);

    await expect(drawer.getByText("BeforeToolCallEvent → cancel_tool")).toBeVisible();
    await expect(drawer.getByText(/Strands GraphBuilder/)).toBeVisible();
    await expect(drawer.getByText(/Amazon Nova via Bedrock/).first()).toBeVisible();
    await expect(drawer.getByText(/rules\.yaml/)).toBeVisible();
  });
});
