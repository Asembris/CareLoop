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

/**
 * The frozen executable release every source receipt pins to, and the value the deployed
 * build reports at `/release.json`. Not the commit that publishes this page: the cited
 * implementation paths are identical in both, and a receipt should name the build a reader
 * is actually running.
 */
const PINNED_RELEASE = "199f09a422c783c6d63f142f8e225ed9acdc9352";

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
    // Session W published the ledger, so the pending state must be *gone* — the inverse of what
    // this asserted through phase 1. Deliberately config-independent: `check_site.py` already ties
    // every rendered value to `site.toml`, and repeating those literals here would give the pair
    // two sources to drift between. What this adds is that the value reaches a screen at all.
    await expect(page.getByText("Verified result pending")).toHaveCount(0);
    const result = page.locator("text=RESULT").first();
    await expect(result).toBeVisible();
    await expect(page.getByText("0 of 320").first()).toBeVisible();
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

  const LIVE_URL = "https://d2heuwvhvrdyig.cloudfront.net";
  const REPO_URL = "https://github.com/Asembris/CareLoop";

  test("the live and repository CTAs resolve to the real destinations", async ({ page }) => {
    await open(page);

    // Both URLs are configured once and the page derives every CTA from them, so this asserts the
    // rendered anchors rather than the config — the config being right is check_site.py's job.
    const anchors = await page.evaluate(() =>
      [...document.querySelectorAll("a")].map((a) => ({
        href: a.getAttribute("href") ?? "",
        target: a.getAttribute("target") ?? "",
        rel: a.getAttribute("rel") ?? "",
        text: (a.textContent ?? "").trim(),
      })),
    );

    const live = anchors.filter((a) => a.href === LIVE_URL);
    const repo = anchors.filter((a) => a.href === REPO_URL);
    // Header, hero, the "three ways in" panel and the closing block for the live app; hero, panel
    // and closing block for the repository.
    expect(live.length).toBe(4);
    expect(repo.length).toBe(3);

    // A real destination opens in a new tab, and every one of them is safe to do so.
    for (const anchor of [...live, ...repo]) {
      expect(anchor.target).toBe("_blank");
      expect(anchor.rel).toContain("noopener");
    }
    // The pending copy is gone precisely where a destination now exists.
    expect(live.some((a) => a.text.includes("Open CareLoop"))).toBe(true);
    expect(repo.some((a) => a.text.includes("View source"))).toBe(true);
    await expect(page.getByText("Deployment pending")).toHaveCount(0);
    await expect(page.getByText("Repository pending")).toHaveCount(0);
  });

  test("the absent video and Devpost destinations stay pending, not broken", async ({ page }) => {
    await open(page);

    // No film exists, so the demo panel stays a same-page jump and "Play film" is never offered.
    await expect(page.getByRole("link", { name: /Play film/ })).toHaveCount(0);
    const demoPanel = await page.evaluate(() =>
      [...document.querySelectorAll("a")]
        .filter((a) => (a.textContent ?? "").includes("Watch demo"))
        .map((a) => ({ href: a.getAttribute("href") ?? "", target: a.getAttribute("target") ?? "" })),
    );
    expect(demoPanel.length).toBeGreaterThan(0);
    for (const panel of demoPanel) {
      // A same-page jump to the demo chapter. Some carry an explicit `_self` and some carry no
      // target at all, which are the same behaviour; what matters is that none opens a new tab.
      expect(panel.href).toBe("#demo");
      expect(panel.target).not.toBe("_blank");
    }
    // Devpost is read by no markup at all, so it must render nowhere rather than render empty.
    // Asserted on rendered text and on link destinations, not on innerHTML: the prop is declared
    // in the runtime's `data-props` attribute, so its *name* is in the DOM whether or not
    // anything uses it, and matching that would be testing the declaration rather than the page.
    const devpost = await page.evaluate(() => ({
      visible: /devpost/i.test(document.body.innerText),
      linked: [...document.querySelectorAll("a")].some((a) => /devpost/i.test(a.getAttribute("href") ?? "")),
    }));
    expect(devpost.visible).toBe(false);
    expect(devpost.linked).toBe(false);

    // Nothing anywhere is a dead link or an empty new tab.
    const hrefs = await page.evaluate(() =>
      [...document.querySelectorAll("a")].map((a) => a.getAttribute("href") ?? ""),
    );
    expect(hrefs.filter((h) => h === "" || h === "#")).toEqual([]);
    const unsafe = await page.evaluate(() =>
      [...document.querySelectorAll('a[target="_blank"]')]
        .filter((a) => !(a.getAttribute("rel") ?? "").includes("noopener"))
        .map((a) => a.getAttribute("href") ?? ""),
    );
    expect(unsafe).toEqual([]);
  });

  test("every source receipt is a permalink pinned to the released commit", async ({ page }) => {
    await open(page);

    // A receipt renders inside the panel that cites it, so the page has to be driven to where a
    // reader would actually meet one. This is the substance of the inversion: the switched-off
    // form of this test asserted an *absence* over a closed page, which it would have satisfied
    // just as happily had it never searched anything.
    const metric = page.locator("button.cl-metric").first();
    await metric.scrollIntoViewIfNeeded();
    await metric.click();
    await expect(metric).toHaveAttribute("aria-expanded", "true");

    // Asserted on rendered links rather than on config, so a receipt that failed to survive the
    // transform fails here too — and over every permalink rather than a sampled one, so a single
    // mapping that slipped back to a branch is caught.
    const permalinks = await page.evaluate(() =>
      [...document.querySelectorAll("a")]
        .map((a) => a.getAttribute("href") ?? "")
        .filter((href) => /github\.com\/[^/]+\/[^/]+\/(?:blob|tree)\//.test(href)),
    );
    expect(permalinks.length).toBeGreaterThan(0);

    const commits = [
      ...new Set(permalinks.map((href) => href.split(/\/(?:blob|tree)\//)[1].split("/")[0])),
    ];
    expect(commits).toEqual([PINNED_RELEASE]);

    // A moving branch reference is the exact failure pinning exists to prevent: it resolves today
    // and drifts off the line it was citing tomorrow.
    expect(permalinks.filter((href) => /\/(?:blob|tree)\/main\//.test(href))).toEqual([]);

    // The open panel says so in its own words where it used to read "Source pending".
    await expect(page.getByText("Source pending")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(metric).toHaveAttribute("aria-expanded", "false");

    // The architecture inspector's SOURCE row names a repository path now. Asserted as the
    // positive, because a missing inspector would satisfy the absence of the old copy just as
    // well as a working one.
    const chip = page.locator("button.cl-chip[data-atlas]").first();
    await chip.scrollIntoViewIfNeeded();
    await chip.click();
    await expect(chip).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByText(/careloop\/[a-z]/).first()).toBeVisible();
    await expect(page.getByText("Source pending")).toHaveCount(0);
    await page.keyboard.press("Escape");
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
   * The dossier used to overflow horizontally on a phone: 778px of document in a 375px viewport,
   * measured identically on the pristine handoff bundle, so it was a defect in the design rather
   * than anything the adaptation introduced. Four layouts sized their columns with hard pixel
   * minimums — the widest reserving 700px before its 56px gap — and the `max-width: 720px` media
   * query adjusted padding and section height without ever collapsing them.
   *
   * The fix is one rule in `src/dossier.dc.html`, carried into the bundle by `adapt.py`. This now
   * holds the real bar rather than a ratchet.
   *
   * Every width from 320 to 720 is clean. A separate band at **721–799px** still overflows, at an
   * unchanged 796px: those grids need ~756px plus padding, and the breakpoint sits at 720. Curing
   * that means moving the breakpoint, which would change the tablet layout this change is required
   * to preserve exactly, so it is left alone and recorded in docs/ADAPTATION.md.
   */
  for (const width of [320, 360, 375, 414, 600, 720]) {
    test(`no horizontal overflow at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 812 });
      await open(page);
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.innerWidth);
    });
  }

  test("the phone renders the hero, the workflow and a product capture cleanly", async ({ page }) => {
    const faults = watch(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await open(page);

    // Each collapsed grid must occupy one column, inside the viewport, with its capture intact.
    const collapsed = await page.evaluate(() => {
      const vw = document.documentElement.clientWidth;
      return [...document.querySelectorAll('[style*="grid-template-columns: minmax("]')].map((el) => {
        const box = el.getBoundingClientRect();
        return {
          columns: getComputedStyle(el).gridTemplateColumns.split(" ").length,
          withinViewport: box.left >= -1 && box.right <= vw + 1,
        };
      });
    });
    // Four declarations in the source, but the failure and architecture rows are rendered per
    // item, so the DOM holds more instances than the stylesheet holds rules. What matters is that
    // every one of them collapsed, not how many the list happened to produce.
    expect(collapsed.length).toBeGreaterThanOrEqual(4);
    for (const grid of collapsed) {
      expect(grid.columns).toBe(1);
      expect(grid.withinViewport).toBe(true);
    }

    // The pinned chapter still reserves its scroll, and the captures still render at full width.
    await expect(page.locator("[data-story-scrub]")).toHaveCount(1);
    const captures = await page.evaluate(() =>
      [...document.images].map((im) => ({
        width: Math.round(im.getBoundingClientRect().width),
        loaded: im.naturalWidth > 0,
      })),
    );
    expect(captures.length).toBe(4);
    for (const capture of captures) {
      expect(capture.loaded).toBe(true);
      expect(capture.width).toBeLessThanOrEqual(375);
    }

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
