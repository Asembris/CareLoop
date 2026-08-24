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
 * **All fourteen run. Nothing is withheld for contrast.** Three real `color-contrast` defects at
 * `serious` impact were found here and repaired at their source; the numbers below are what they
 * measured before and after, so a regression is recognisable rather than merely red.
 *
 *     1. --state-decision, light only          #ab673c 4.03:1  ->  #9e5b2f 4.79:1
 *        "NEEDS YOU", "DECISION NEEDED", "APPROVES", "Run scenario →". A token, darkened; the
 *        brand copper it used to alias is untouched, since its other uses are a logotype and a
 *        highlight background.
 *
 *     2. the drawer's stage names, both themes  2.08:1 light  ->  4.71:1   (opacity 0.35 -> 0.65)
 *                                               2.80:1 dark   ->  6.56:1
 *        Not a palette fault: `--foreground` at 35% opacity. See the component for why WCAG
 *        1.4.3's inactive-component exemption does not reach them.
 *
 *     3. the Trust page's evidence labels       3.56:1 light  ->  7.36:1   (opacity 0.7 removed)
 *                                               3.32:1 dark   ->  5.48:1
 *        Also not a palette fault: `--muted-foreground`, which passes on its own, multiplied by
 *        an opacity that took it below the line.
 *
 * Two of the three were an opacity layered over a token that already passed, which is worth
 * knowing: the palette was mostly right and the compositing was not.
 */

import { test, type APIRequestContext, type Page } from "./base";
import { scan, type Allowance } from "./axe";
import { runScenario, sessionFromPage, settled } from "./helpers";

type Theme = "light" | "dark";

const THEMES: Theme[] = ["light", "dark"];

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

/** My Boundaries bullets each refusal with an aria-hidden dash. */
const DECORATIVE_GLYPHS: Allowance[] = [
  {
    rule: "color-contrast",
    why:
      'Each refusal is prefixed with an aria-hidden "—" span, and axe reports "element content ' +
      'contains only non-text characters" — it declines to judge contrast for a glyph rather than ' +
      "getting it wrong. Correct to allow rather than to repair: the mark is decorative and hidden " +
      "from assistive technology, so it carries no information a contrast ratio would protect, and " +
      "the words beside it are real text and are scanned normally. It replaced the deleted " +
      "guarantee list's checkmark, which was the same shape of allowance for the same reason."
  }
];

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

function scanState(
  label: string,
  body: (page: Page, request: APIRequestContext) => Promise<void>
): void {
  test(label, async ({ page, request }) => {
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

  scanState(`my boundaries (${theme})`, async (page) => {
    await page.goto("/trust");
    await useTheme(page, theme);
    await page
      .getByRole("heading", { name: "What CareLoop may do, and what it will not." })
      .waitFor();
    // The bands are what the scan is for, and they arrive on the boundary response rather than on
    // first paint. Scanning before it lands would scan a skeleton and report nothing useful.
    await page.getByRole("heading", { name: "CARELOOP MAY, ON ITS OWN" }).waitFor();

    await scan(page, `my boundaries (${theme})`, DECORATIVE_GLYPHS);
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

  scanState(`document lens (${theme})`, async (page, request) => {
    // The document mode of the lens: full page text of the poisoned notice with the refusal
    // panel beneath. `injection_only` produces it for real and deterministically.
    await seed(page, request, "injection_only");
    await page.goto("/case/demo-injection-only");
    await useTheme(page, theme);

    await page.getByText("mediguard-continuity-notice.pdf").first().click();
    await settled(page.getByRole("dialog", { name: "Source document" }));

    await scan(page, `document lens (${theme})`, OVER_THE_PAGE);
  });

  scanState(`offline dashboard (${theme})`, async (page) => {
    // The one state reality cannot produce while the webServer keeps the API alive for the other
    // specs — see `truth-surfaces.spec.ts` for the justification of the abort. The theme is set
    // first, while the backend is still reachable, because the toggle itself needs no network.
    await page.goto("/");
    await useTheme(page, theme);
    await page.route("**/api/**", (route) => route.abort());
    await page.goto("/");

    await page.getByText("CareLoop can't be reached — retrying automatically.").waitFor();
    await scan(page, `offline dashboard (${theme})`, NONE);
  });
}
