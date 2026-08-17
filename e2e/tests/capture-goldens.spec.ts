/* A capture tool, not a test. Run it deliberately:
 *
 *   npx playwright test capture-goldens.spec.ts --grep-invert nothing
 *
 * It is skipped unless CARELOOP_CAPTURE=1, so a normal suite run never rewrites the reference
 * set. Conditions come from `frontend/reference/DESIGN_REFERENCE.md`: 909x540, cropped to the
 * fold, both themes, real backend data.
 */

import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { runScenario, sessionFromPage, settled } from "./helpers";

const OUT = resolve(import.meta.dirname, "../../frontend/reference/screenshots");
const THEMES = ["light", "dark"] as const;

test.describe("reference screenshots", () => {
  test.skip(process.env.CARELOOP_CAPTURE !== "1", "set CARELOOP_CAPTURE=1 to rewrite the goldens");
  test.use({ viewport: { width: 909, height: 540 } });

  for (const theme of THEMES) {
    test(`home-idle-${theme}`, async ({ page }) => {
      mkdirSync(OUT, { recursive: true });
      await page.addInitScript((t) => localStorage.setItem("careloop:theme", t), theme);
      await page.goto("/");

      // A fresh world: the state the dashboard spends most of its life in, and the one commit 2
      // was about — it used to end at 56% of the viewport.
      await expect(page.getByText("Nothing needs you right now.")).toBeVisible();
      await page.screenshot({ path: `${OUT}/home-idle-${theme}.png` });
    });

    test(`home-needs-you-${theme}`, async ({ page, request }) => {
      mkdirSync(OUT, { recursive: true });
      await page.addInitScript((t) => localStorage.setItem("careloop:theme", t), theme);
      await page.goto("/");

      const session = await sessionFromPage(page);
      expect((await runScenario(request, session, "resume")).status()).toBe(202);
      await page.reload();

      // `settled`, not just visible. The decision card rises over 420ms, and a screenshot taken
      // the moment it appears captures it half-transparent — which is how the first run of this
      // tool produced a worse reference than the one already committed.
      const card = page.getByRole("article");
      await settled(card);
      await page.screenshot({ path: `${OUT}/home-needs-you-${theme}.png` });
    });
  }
});
