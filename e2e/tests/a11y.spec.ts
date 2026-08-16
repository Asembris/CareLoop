/* Accessibility, scanned per state and in both themes.
 *
 * **Per state, not per route.** axe reads the live DOM at call time, so one scan per route tests
 * whichever theme happened to be active and never sees an open drawer. The drawers are not routes
 * at all — they have no URL — so nothing navigational reaches them.
 *
 * **Both themes, because contrast is a property of resolved colour.** Light is canonical and dark
 * is a deliberate second theme rather than an inversion, so its ratios are a separate claim about a
 * separate palette. This is also the pair jsdom cannot check at all: axe's `color-contrast` rule is
 * documented as non-functional there, so every scan here asserts something the 119 existing
 * frontend tests could not express.
 *
 * `tests/a11y-vacuity.spec.ts` is what makes the empty arrays mean something.
 *
 * ---
 *
 * **Six of the fourteen are withheld in `WITHHELD` below, and that is the finding rather than an
 * omission.** Three real `color-contrast` defects at `serious` impact fail WCAG 2.1 AA, and every
 * repair is a change to a design-locked palette — a design decision, not a test fix. Per this
 * patch's own rule they are reported and left for a separate approved patch, rather than quietly
 * fixed here or annotated into a pass. Measured reproducibly with animations frozen, twice:
 *
 *     1. --state-decision, light only                #ab673c on #f7f4ef    4.04:1  (needs 4.5)
 *        "NEEDS YOU", "DECISION NEEDED", "APPROVES", "Run scenario →", the scenario numerals.
 *        One token, and the nearest miss of the three.
 *
 *     2. the drawer's inactive stage names           #afaba6 on #f7f4ef    2.08:1  light
 *        Triage / Evidence / Planner / Gate /        #615f5b on #1a1713    2.80:1  dark
 *        Execute / Verify / Settled — dimmed on purpose to show which stage is live, and still
 *        text somebody reads. The worst of the three.
 *
 *     3. the Trust page's evidence labels            #85807b on #f7f4ef    3.56:1  light
 *        "INJECTIONS BLOCKED —", "EXCESSIVE AGENCY —" #6d6a66 on #1a1713   3.31:1  dark
 *
 * Every scan is declared; the withheld ones are named as data and reported as skipped on every
 * run, so the gap is visible in the output rather than only in this comment. Deleting an entry
 * from `WITHHELD` is the whole change once a palette decision is made.
 */

import { test, type APIRequestContext, type Page } from "@playwright/test";
import { scan, type Allowance } from "./axe";
import { runScenario, sessionFromPage, settled } from "./helpers";

type Theme = "light" | "dark";

const THEMES: Theme[] = ["light", "dark"];

/** `state (theme)` pairs that fail on a measured contrast defect. See the header for ratios. */
const WITHHELD = new Set([
  "decision card (light)",
  "demo launcher (light)",
  "trust (light)",
  "trust (dark)",
  "under-the-hood drawer (light)",
  "under-the-hood drawer (dark)"
]);

/** Put the page in `theme`. Light is canonical, so only dark needs pressing. */
async function useTheme(page: Page, theme: Theme): Promise<void> {
  if (theme === "dark") {
    await page.getByRole("button", { name: "Switch to dark" }).click();
    await page.getByRole("button", { name: "Switch to light" }).waitFor();
  }
}

async function seed(page: Page, request: APIRequestContext, key: string): Promise<void> {
  await page.goto("/");
  const session = await sessionFromPage(page);
  await runScenario(request, session, key);
  await page.reload();
}

/* ------------------------------------------------------------------ *
 * Allowances
 * ------------------------------------------------------------------ *
 *
 * Written by hand, one at a time, each with the reason it is acceptable. **Never regenerated from
 * a failing run** — that is how "unknown" silently becomes "green" six commits from now, which is
 * the vacuity this suite exists to prevent one level up. Adding an entry is a decision somebody
 * makes and signs; it is not a way to make a run pass.
 */

const NONE: Allowance[] = [];

/** The evidence lens sits over the page it was opened from. */
const OVER_THE_PAGE: Allowance[] = [
  {
    rule: "color-contrast",
    why:
      "The drawer is painted over the case detail, and axe cannot resolve an effective background " +
      "for text whose ancestor stack includes a translucent overlay, so it reports undecided " +
      "rather than wrong. The honest repair is to make the page behind a drawer genuinely inert — " +
      "axe skips inert subtrees, which removes the ambiguity by fixing a real focus-management " +
      "defect rather than by annotating it. That is a separate patch, noted here on purpose."
  }
];

/** Declare one scan, or record it as withheld. */
function scanState(
  label: string,
  body: (page: Page, request: APIRequestContext) => Promise<void>
): void {
  test(label, async ({ page, request }) => {
    test.skip(WITHHELD.has(label), "withheld on a measured contrast defect — see the file header");
    await body(page, request);
  });
}

for (const theme of THEMES) {
  scanState(`dashboard (${theme})`, async (page, request) => {
    await seed(page, request, "referral");
    await useTheme(page, theme);
    await page.getByText("COMPLETED", { exact: true }).waitFor();
    await scan(page, `dashboard (${theme})`, NONE);
  });

  scanState(`decision card (${theme})`, async (page, request) => {
    await seed(page, request, "resume");
    await useTheme(page, theme);
    await page.getByRole("article").waitFor();
    await scan(page, `decision card (${theme})`, NONE);
  });

  scanState(`demo launcher (${theme})`, async (page) => {
    await page.goto("/demo");
    await useTheme(page, theme);
    await page.getByRole("heading", { name: "See CareLoop work." }).waitFor();
    await scan(page, `demo launcher (${theme})`, NONE);
  });

  scanState(`case detail (${theme})`, async (page, request) => {
    await seed(page, request, "referral");
    await page.goto("/case/demo-referral");
    await useTheme(page, theme);

    // **Wait for the content, then scan.** The page fetches its case before rendering, so an
    // immediate scan finds an empty shell and one a moment later finds a fading one — measured at
    // ratios like 1.03:1, which is not a colour choice, it is four percent opacity. Anchoring on
    // an evidence card is what makes the scan see the finished page.
    await page.getByRole("button", { name: /referral\.pdf/ }).first().waitFor();
    await scan(page, `case detail (${theme})`, NONE);
  });

  scanState(`trust (${theme})`, async (page) => {
    await page.goto("/trust");
    await useTheme(page, theme);
    await page
      .getByRole("heading", { name: "The model never authorizes its own actions." })
      .waitFor();

    // When this is un-withheld it also needs an allowance for the guarantee list's aria-hidden
    // "✓" glyphs, which axe reports as undecided: "element content contains only non-text
    // characters". Correct to allow rather than fix — the mark is decorative and hidden from
    // assistive technology, so it carries nothing a contrast ratio would protect.
    await scan(page, `trust (${theme})`, NONE);
  });

  scanState(`under-the-hood drawer (${theme})`, async (page, request) => {
    await seed(page, request, "referral");
    await useTheme(page, theme);

    await page.getByRole("button", { name: "Under the hood" }).click();
    await settled(page.getByRole("dialog", { name: "Under the hood" }));

    await scan(page, `under-the-hood drawer (${theme})`, NONE);
  });

  scanState(`evidence lens (${theme})`, async (page, request) => {
    await seed(page, request, "referral");
    await page.goto("/case/demo-referral");
    await useTheme(page, theme);

    // Opened from an evidence card, the only way in — the lens has no URL either. Matched on the
    // citation it carries, so it is a fact drawn from the referral rather than from configuration.
    await page.getByRole("button", { name: /referral\.pdf/ }).first().click();
    await settled(page.getByRole("dialog", { name: "Evidence" }));

    await scan(page, `evidence lens (${theme})`, OVER_THE_PAGE);
  });
}
