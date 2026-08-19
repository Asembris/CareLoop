import { expect, test, type ConsoleMessage, type Page, type Request, type Response } from "@playwright/test";

/**
 * Runtime gate for the CareLoop dossier, served the way GitHub Pages will serve it.
 *
 * The config points `baseURL` at `/CareLoop/` and `serve.py` publishes nothing at the origin root,
 * so every assertion here is made against the real project-path deployment shape. That is the
 * whole reason this file exists rather than a `curl` of the HTML: the defects that project-path
 * hosting produces — a root-absolute asset, a relative background that resolves against the wrong
 * directory — are invisible to a static read and obvious to a browser.
 *
 * It is deliberately one file. The dossier is a single static page; a suite with fixtures,
 * page objects and helpers would be more machinery than the artifact it guards.
 */

const ORIGIN = "http://127.0.0.1";

/** Everything that went wrong while the page was open, collected per test. */
type Faults = {
  pageErrors: string[];
  consoleErrors: string[];
  failedResponses: string[];
  offOrigin: string[];
};

function watch(page: Page): Faults {
  const faults: Faults = { pageErrors: [], consoleErrors: [], failedResponses: [], offOrigin: [] };

  page.on("pageerror", (error: Error) => faults.pageErrors.push(error.message));
  page.on("console", (message: ConsoleMessage) => {
    if (message.type() === "error") faults.consoleErrors.push(message.text());
  });
  page.on("response", (response: Response) => {
    if (response.status() >= 400) faults.failedResponses.push(`${response.status()} ${response.url()}`);
  });
  page.on("request", (request: Request) => {
    const url = request.url();
    // blob: and data: never leave the document. Anything else must be same-origin: the bundle
    // inlines React, every font and every capture precisely so that no third party is involved.
    if (url.startsWith("blob:") || url.startsWith("data:")) return;
    if (!url.startsWith(ORIGIN)) faults.offOrigin.push(url);
  });

  return faults;
}

function expectClean(faults: Faults): void {
  expect(faults.pageErrors, "uncaught page errors").toEqual([]);
  expect(faults.consoleErrors, "console errors").toEqual([]);
  expect(faults.failedResponses, "responses >= 400").toEqual([]);
  expect(faults.offOrigin, "off-origin requests").toEqual([]);
}

/** The page hydrates by swapping the document, so wait for the swapped body to exist. */
async function open(page: Page): Promise<void> {
  await page.goto("./", { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll("main > section").length > 0);
  await page.waitForFunction(() => document.fonts.status === "loaded");
}

test.describe("the dossier under the GitHub project path", () => {
  test("loads cleanly, with nothing missing and nothing off-origin", async ({ page }) => {
    const faults = watch(page);
    await open(page);

    expect(page.url()).toContain("/CareLoop/");
    await expect(page).toHaveTitle(/CareLoop/);
    // Sixteen chapters plus the hero, per docs/CONTENT_MAP.md.
    expect(await page.locator("main > section").count()).toBeGreaterThanOrEqual(16);

    // Scroll the whole page so lazy images, the pinned scrub and every observer actually run;
    // a fault that only appears at section 09 must not be missed by testing the hero alone.
    await page.evaluate(async () => {
      const step = window.innerHeight;
      for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
        window.scrollTo(0, y);
        await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 24)));
      }
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(300);

    expectClean(faults);
  });

  test("serves crawler-visible metadata before any JavaScript runs", async ({ request }) => {
    // Fetched, not rendered: this is what a link-preview crawler receives.
    const response = await request.get("./");
    expect(response.status()).toBe(200);
    const html = await response.text();
    const head = html.slice(0, html.indexOf("</head>"));

    expect(head).toContain("<title>");
    expect(head).toContain('name="description"');
    expect(head).toContain('property="og:title"');
    expect(head).toContain('property="og:image"');
    expect(head).toContain('name="twitter:card"');
    expect(head).toContain('rel="icon"');
    // Phase 1 ships no absolute base, so the social image stays page-relative and must resolve.
    expect(head).toContain('content="assets/product/authority.png"');
  });

  test("every asset the head names is actually fetchable at the project path", async ({ request }) => {
    for (const asset of [
      "assets/product/authority.png",
      "assets/product/calm.png",
      "assets/product/verification.png",
      "assets/careloop-mark.svg",
    ]) {
      const response = await request.get(`./${asset}`);
      expect(response.status(), asset).toBe(200);
    }
  });

  test("the topology initialises and keeps its documented API", async ({ page }) => {
    await open(page);
    const api = await page.evaluate(() => {
      const story = (window as unknown as { __careLoopStory?: Record<string, unknown> }).__careLoopStory;
      if (!story) return null;
      return ["setState", "focus", "pinState", "setMutant", "setNoBridge", "setHidden", "destroy"].map(
        (name) => `${name}:${typeof story[name]}`,
      );
    });
    expect(api).toEqual([
      "setState:function",
      "focus:function",
      "pinState:function",
      "setMutant:function",
      "setNoBridge:function",
      "setHidden:function",
      "destroy:function",
    ]);
    // The module owns its own container — docs/TOPOLOGY.md explains why it appends it itself.
    await expect(page.locator("#careloop-story")).toHaveCount(1);
  });

  test("every in-page anchor resolves to a section that exists", async ({ page }) => {
    await open(page);
    const dangling = await page.evaluate(() =>
      [...document.querySelectorAll('a[href^="#"]')]
        .map((a) => a.getAttribute("href") ?? "")
        .filter((href) => href.length > 1 && !document.getElementById(href.slice(1))),
    );
    expect(dangling).toEqual([]);
  });

  test("the pinned workflow is present and scrubs", async ({ page }) => {
    await open(page);
    const scrub = page.locator("[data-story-scrub]");
    await expect(scrub).toHaveCount(1);
    // The narrative column and the field read the same weights, which is what keeps them locked.
    const weights = await scrub.getAttribute("data-story-weights");
    expect(weights?.split(",").length).toBe(8);

    const box = await scrub.boundingBox();
    expect(box, "the pinned section reserves scroll length").not.toBeNull();
    expect(box!.height).toBeGreaterThan(1000);

    await scrub.scrollIntoViewIfNeeded();
    await page.mouse.wheel(0, 1200);
    await page.waitForTimeout(240);
    const state = await page.evaluate(
      () => (window as unknown as { __careLoopStory: { state: string } }).__careLoopStory.state,
    );
    expect(typeof state).toBe("string");
    expect(state.length).toBeGreaterThan(0);
  });

  test("the evidence inspector opens a metric", async ({ page }) => {
    const faults = watch(page);
    await open(page);
    const metric = page.locator("button.cl-metric").first();
    await metric.scrollIntoViewIfNeeded();
    await expect(metric).toHaveAttribute("aria-expanded", "false");
    await metric.click();
    await expect(metric).toHaveAttribute("aria-expanded", "true");
    // Phase 1 publishes no results, so the pending state is what must be on screen.
    await expect(page.getByText("Verified result pending").first()).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(metric).toHaveAttribute("aria-expanded", "false");
    expectClean(faults);
  });

  test("the mutation switch flips the counterfactual", async ({ page }) => {
    const faults = watch(page);
    await open(page);
    const flip = page.locator('button[role="switch"]').first();
    await flip.scrollIntoViewIfNeeded();
    await expect(flip).toHaveAttribute("aria-checked", "false");
    await flip.click();
    await expect(flip).toHaveAttribute("aria-checked", "true");
    await flip.click();
    await expect(flip).toHaveAttribute("aria-checked", "false");
    expectClean(faults);
  });

  test("architecture chips and failure rows respond and release", async ({ page }) => {
    const faults = watch(page);
    await open(page);

    const chip = page.locator("button.cl-chip[data-atlas]").first();
    await chip.scrollIntoViewIfNeeded();
    await expect(chip).toHaveAttribute("aria-pressed", "false");
    await chip.click();
    await expect(chip).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.press("Escape");
    await expect(chip).toHaveAttribute("aria-pressed", "false");

    const row = page.locator("button.cl-row").first();
    await row.scrollIntoViewIfNeeded();
    await expect(row).toHaveAttribute("aria-expanded", "false");
    await row.click();
    await expect(row).toHaveAttribute("aria-expanded", "true");
    await page.keyboard.press("Escape");
    await expect(row).toHaveAttribute("aria-expanded", "false");

    expectClean(faults);
  });

  test("the product lightbox opens with a capture that actually loads", async ({ page }) => {
    const faults = watch(page);
    await open(page);

    const capture = page.getByRole("button", { name: /^Enlarge / }).first();
    await capture.scrollIntoViewIfNeeded();
    await capture.click();

    const dialog = page.locator('[role="dialog"][aria-modal="true"]');
    await expect(dialog).toBeVisible();

    // The regression this exists for: the background is a relative CSS url(), and in the pristine
    // handoff it pointed at product/*.png — a path that exists nowhere, so the panel opened empty
    // and issued a 404. Assert the URL resolves under the project path AND that it was fetched
    // successfully, because a correct-looking URL that 404s looks identical on screen.
    const background = await dialog.locator('[role="img"]').evaluate((node) => getComputedStyle(node).backgroundImage);
    expect(background).toContain("/CareLoop/assets/product/");
    const source = background.match(/url\("?([^")]+)"?\)/)?.[1] ?? "";
    const probe = await page.request.get(source);
    expect(probe.status(), source).toBe(200);

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expectClean(faults);
  });

  test("empty destinations stay pending rather than becoming dead links", async ({ page }) => {
    await open(page);
    // Phase 1 configures no URLs, so the dossier's own fallback copy is what must appear.
    await expect(page.getByText("Deployment pending").first()).toBeVisible();
    await expect(page.getByText("Repository pending").first()).toBeVisible();
    // A pending CTA falls back to an in-page anchor; nothing may open a new tab to nowhere.
    const hrefs = await page.evaluate(() =>
      [...document.querySelectorAll("a")].map((a) => a.getAttribute("href") ?? ""),
    );
    expect(hrefs.filter((h) => h === "" || h === "#")).toEqual([]);
    // Every external link that does exist is safe.
    const unsafe = await page.evaluate(() =>
      [...document.querySelectorAll('a[target="_blank"]')]
        .filter((a) => !(a.getAttribute("rel") ?? "").includes("noopener"))
        .map((a) => a.getAttribute("href") ?? ""),
    );
    expect(unsafe).toEqual([]);
  });

  test("no horizontal overflow on desktop", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await open(page);
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.innerWidth);
  });

  /**
   * The dossier overflows horizontally at 375px, and it did so before this integration existed.
   *
   * Measured identically against the pristine handoff bundle and the adapted page: both report a
   * document 778px wide in a 375px viewport. The cause is in the design, not the adaptation — two
   * chapters lay their capture beside their prose with hard grid floors,
   * `minmax(300px, 0.78fr) minmax(400px, 1fr)` and `minmax(260px, 0.56fr) minmax(420px, 1fr)`,
   * which reserve ~756px before the gap, and the `max-width: 720px` media query adjusts padding
   * and section height without ever collapsing those grids to one column.
   *
   * It is therefore **not** repaired here. Fixing it means changing the layout, which is outside
   * the transformations this integration is allowed to make, and doing it by injecting CSS around
   * a locked artifact would be exactly the "compensate by editing the runtime" move Path A exists
   * to refuse. It belongs at source, in a Claude Design re-export, and it is recorded as a Phase-2
   * blocker in docs/ADAPTATION.md.
   *
   * So this asserts a ratchet rather than the goal: the artifact may not get *worse* than the
   * figure that was measured on the pristine bundle. When the design is fixed, tighten this to
   * `toBeLessThanOrEqual(innerWidth)` — the same bar the desktop test already holds.
   */
  const KNOWN_MOBILE_OVERFLOW = 778;

  test("mobile overflow is no worse than the pristine handoff's", async ({ page }) => {
    const faults = watch(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await open(page);
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(overflow.scrollWidth).toBeLessThanOrEqual(KNOWN_MOBILE_OVERFLOW);
    expectClean(faults);
  });

  test("reduced motion shortens the pinned chapter and keeps the content", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    const page = await context.newPage();
    const faults = watch(page);
    await open(page);

    // Meaning survives without the animation: the chapter is still there, just not long.
    const scrub = page.locator("[data-story-scrub]");
    await expect(scrub).toHaveCount(1);
    const box = await scrub.boundingBox();
    expect(box!.height).toBeLessThan(4000);
    expect(await page.locator("main > section").count()).toBeGreaterThanOrEqual(16);

    expectClean(faults);
    await context.close();
  });

  test("keyboard reaches the interactive controls", async ({ page }) => {
    await open(page);
    const reached: string[] = [];
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      reached.push(
        await page.evaluate(() => {
          const el = document.activeElement;
          return el ? `${el.tagName.toLowerCase()}${el.className ? "." + String(el.className).split(" ")[0] : ""}` : "";
        }),
      );
    }
    expect(reached.some((name) => name.startsWith("a") || name.startsWith("button"))).toBe(true);
  });
});
