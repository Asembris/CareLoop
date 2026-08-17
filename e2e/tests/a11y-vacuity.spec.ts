/* Proof that the accessibility scans can fail.
 *
 * Every scan in `a11y.spec.ts` asserts that two arrays are empty. Two empty arrays are also what a
 * misconfigured scanner returns — wrong tags, a `runOnly` clobbered by `.options()`, an `analyze()`
 * whose injection silently failed — so without this file the whole a11y layer could be green and
 * measuring nothing.
 *
 * **Contrast is the reason this has to be a real browser.** axe's `color-contrast` rule is
 * documented as non-functional under jsdom: it needs layout and resolved colours, which jsdom does
 * not produce. So the single most common accessibility defect in a design-led product is
 * unreachable from the existing 119 Vitest tests by construction, not by omission.
 *
 * **`page.setContent`, never a file in `frontend/public/`.** A fixture with deliberately broken
 * contrast placed there would be built into the bundle and served from CloudFront — shipping a
 * knowingly inaccessible page to prove the tests can spot one.
 */

import { expect, test } from "@playwright/test";
import { freezeAnimations, scanner } from "./axe";

/** Deliberately broken, in two independent ways. */
const BROKEN = `
  <main style="background: #808080; padding: 24px">
    <h1 style="color: #8a8a8a">A heading nobody can read</h1>
    <p style="color: #888888">Grey on grey, at roughly 1.1:1.</p>
    <form>
      <input type="text" />
      <button type="submit" style="color: #8a8a8a; background: #808080">Go</button>
    </form>
  </main>
`;

test.describe("the scanner", () => {
  test("catches a deliberate contrast failure", async ({ page }) => {
    await page.setContent(BROKEN);

    const results = await scanner(page).analyze();
    const rules = results.violations.map((violation) => violation.id);

    expect(rules).toContain("color-contrast");
  });

  test("still catches it with the orchestration field behind the page", async ({ page }) => {
    /* `freezeAnimations` hides `#careloop-field` for the duration of a scan, because axe returns
     * every element above a `<canvas>` as undecided rather than judging it. That rule earns its
     * place only if a real defect painted over the same canvas is still caught — otherwise it is
     * an off switch with a comment attached.
     *
     * The canvas here is the shipped one, by id and by the styling the app gives it. Nothing is
     * drawn into it: what is being proven is that hiding it does not take the page with it. */
    await page.setContent(`
      <canvas id="careloop-field" aria-hidden="true"
        style="position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none"></canvas>
      <div style="position:relative;z-index:1">${BROKEN}</div>
    `);
    await freezeAnimations(page);

    const results = await scanner(page).analyze();

    expect(results.violations.map((violation) => violation.id)).toContain("color-contrast");
    // And the undecided arm is what the rule is for: without hiding the canvas this is where the
    // failure would have gone instead, reported as "axe could not tell" rather than as a defect.
    expect(results.incomplete.map((result) => result.id)).not.toContain("color-contrast");
  });

  test("catches a deliberate missing label", async ({ page }) => {
    await page.setContent(BROKEN);

    const results = await scanner(page).analyze();
    const rules = results.violations.map((violation) => violation.id);

    // An input a screen reader cannot name. Distinct from contrast on purpose: one is a computed
    // rendering property and the other is pure structure, so between them they prove the scan is
    // reading a laid-out page *and* its semantics.
    expect(rules).toContain("label");
  });

  test("passes a page with neither fault", async ({ page }) => {
    // The other half of the vacuity proof: a scanner that reported everything would satisfy both
    // assertions above and be equally useless.
    //
    // A *complete* document, because `setContent` otherwise produces one with no `<title>` and no
    // `lang`, and axe correctly reports `document-title` and `html-has-lang` for it. Measured while
    // writing this: those two are what a bare fragment fails on, and leaving them in would have
    // made "a clean page scans clean" quietly untrue.
    await page.setContent(`
      <!doctype html>
      <html lang="en">
        <head><title>A page with nothing wrong with it</title></head>
        <body>
          <main style="background: #ffffff; padding: 24px">
            <h1 style="color: #111111">A heading with real contrast</h1>
            <form>
              <label for="who">Who</label>
              <input id="who" type="text" />
            </form>
          </main>
        </body>
      </html>
    `);

    const results = await scanner(page).analyze();

    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
