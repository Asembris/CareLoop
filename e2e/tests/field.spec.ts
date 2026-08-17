/* The orchestration field, and the four things it must never cost.
 *
 * The field is decoration. Everything asserted here is a property of the *application* holding
 * while a WebGL canvas paints behind it: nothing it draws reaches the pointer, the tab order, the
 * accessibility tree, or the contrast of anything a caregiver reads. It is also the only place
 * that measures what `a11y.spec.ts` cannot — axe declines to judge text over a canvas at all, so a
 * suite without this file would have no statement about readability over the field, only silence
 * about it.
 *
 * **A real browser, and a real GPU path.** Everything here reads either the compositor's hit
 * testing or the framebuffer itself; jsdom has neither.
 */

import { expect, test as base, type Page } from "@playwright/test";

/* One page per worker, not one per test — and that is a capacity fact rather than a style choice.
 *
 * The frontend mints a world on first load and keeps it in `sessionStorage`, so a world is spent
 * per *tab*, and the API holds 64 of them for half an hour after their last request
 * (`DEFAULT_SESSION_LIMIT`). Written the ordinary way, this file would spend nine of those on
 * decoration and push the rest of the suite into `503 … all of them are in use` — measured, and it
 * took six unrelated specs down with it.
 *
 * Nothing here needs a world of its own: every test is about the canvas, the DOM it reads and the
 * pixels it paints. What that costs is isolation, so `visit` below restores theme and viewport on
 * every navigation rather than trusting the order tests happen to run in.
 */
const test = base.extend<Record<string, never>, { field: Page }>({
  field: [
    async ({ browser }, use) => {
      const context = await browser.newContext();
      const page = await context.newPage();
      await use(page);
      await context.close();
    },
    { scope: "worker" }
  ]
});

/** The four routes, and the reading surfaces each one puts in front of the field. */
const ROUTES = ["/", "/demo", "/trust", "/nope"] as const;

const DESKTOP = { width: 1440, height: 900 };

/** Viewports the field retunes itself for. The tiers are 92 / 64 / 38 nodes at 1080 / 620. */
const VIEWPORTS = [
  { name: "1920x1080", width: 1920, height: 1080, nodes: 92 },
  { name: "1536x864", width: 1536, height: 864, nodes: 92 },
  { name: "1440x900", width: 1440, height: 900, nodes: 92 },
  { name: "tablet 820x1180", width: 820, height: 1180, nodes: 64 },
  { name: "mobile 390x844", width: 390, height: 844, nodes: 38 }
] as const;

/** Wait until the field has booted and drawn. Never a fixed sleep standing in for a state. */
async function fieldReady(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const scene = (
      window as unknown as { __careLoopScene?: { renderer: { info: { render: { frame: number } } } } }
    ).__careLoopScene;
    return !!scene && scene.renderer.info.render.frame > 2;
  });
}

/** Go to `route` in a known theme and viewport, whatever the previous test left behind. */
async function visit(
  page: Page,
  route: string,
  options: { theme?: "light" | "dark"; viewport?: { width: number; height: number }; motion?: boolean } = {}
): Promise<void> {
  const { theme = "light", viewport = DESKTOP, motion = true } = options;
  await page.setViewportSize(viewport);
  await page.goto(route);
  const stored = await page.evaluate(() => localStorage.getItem("careloop:theme"));
  if (stored !== theme) {
    await page.evaluate((value) => localStorage.setItem("careloop:theme", value), theme);
    await page.reload();
  }
  if (motion) await fieldReady(page);
}

test.describe("the orchestration field", () => {
  test("paints behind every route and never intercepts a pointer", async ({ field: page }) => {
    for (const route of ROUTES) {
      await visit(page, route);

      const reading = await page.evaluate(() => {
        const canvas = document.getElementById("careloop-field") as HTMLCanvasElement;
        const style = getComputedStyle(canvas);
        // Hit test a grid across the whole viewport: the canvas may not be on top anywhere.
        const hits: string[] = [];
        for (let x = 0.05; x < 1; x += 0.1) {
          for (let y = 0.05; y < 1; y += 0.1) {
            const el = document.elementFromPoint(x * innerWidth, y * innerHeight);
            if (el && el.id === "careloop-field") hits.push(`${x},${y}`);
          }
        }
        return {
          position: style.position,
          zIndex: style.zIndex,
          pointerEvents: style.pointerEvents,
          ariaHidden: canvas.getAttribute("aria-hidden"),
          tabIndex: canvas.tabIndex,
          rootZIndex: getComputedStyle(document.getElementById("root") as HTMLElement).zIndex,
          hits
        };
      });

      expect(reading.position, `${route}: the field is fixed to the viewport`).toBe("fixed");
      expect(reading.zIndex, `${route}: paper → field → application`).toBe("0");
      expect(reading.rootZIndex, `${route}: the application sits above the field`).toBe("1");
      expect(reading.pointerEvents, `${route}: the field can never take a click`).toBe("none");
      expect(reading.ariaHidden, `${route}: decorative, and out of the accessibility tree`).toBe("true");
      // Not focusable. A canvas is not focusable by default, so this guards against somebody adding
      // a tabindex to make it "interactive" later rather than restating the platform.
      expect(reading.tabIndex, `${route}: the field is not in the tab order`).toBeLessThan(0);
      expect(reading.hits, `${route}: the field is on top of nothing`).toEqual([]);
    }
  });

  test("reads the real layout: every reading surface becomes an exclusion zone", async ({ field: page }) => {
    for (const route of ROUTES) {
      await visit(page, route);

      const zones = await page.evaluate(() => {
        const scene = (
          window as unknown as { __careLoopScene: { layout: { zones: number[][] }; refreshZones(): void } }
        ).__careLoopScene;

        /* Both halves in one synchronous block, after asking the field to re-measure.
         *
         * The RAF loop refreshes zones every twelfth frame, so `layout.zones` is a value computed
         * some frames ago; comparing it against a DOM snapshot taken now is a race, and CI lost it
         * on /trust — four zones against three visible surfaces, because a late layout shift moved
         * one of them out of the band between the two reads. Different font metrics on a Linux
         * runner are enough to change which surfaces sit inside it.
         *
         * Waiting for the page to settle would only narrow the window. Measuring both from the
         * same layout closes it, and `refreshZones` is the same method the loop calls, so this
         * still exercises the real DOM-to-world bridge rather than a test-only path. */
        scene.refreshZones();
        const marked = Array.from(document.querySelectorAll("[data-scene-exclude]"));
        const onScreen = marked.filter((el) => {
          const rect = el.getBoundingClientRect();
          return rect.width >= 4 && rect.height >= 4 && rect.bottom >= -80 && rect.top <= innerHeight + 80;
        });
        return { marked: marked.length, onScreen: onScreen.length, measured: scene.layout.zones.length };
      });

      // At least one, or the page has no reading surface the field knows about.
      expect(zones.marked, `${route}: marks its reading surfaces`).toBeGreaterThan(0);
      // The field keeps its six largest boxes; everything visible below that is carried.
      expect(zones.measured, `${route}: the visible surfaces reached the field`).toBe(
        Math.min(zones.onScreen, 6)
      );
    }
  });

  test("retunes to the viewport rather than to one screenshot size", async ({ field: page }) => {
    for (const viewport of VIEWPORTS) {
      await visit(page, "/demo", { viewport });

      const reading = await page.evaluate(() => {
        const scene = (
          window as unknown as {
            __careLoopScene: { layout: { nodes: unknown[]; zones: number[][] }; dpr: number };
          }
        ).__careLoopScene;
        const canvas = document.getElementById("careloop-field") as HTMLCanvasElement;
        return {
          nodes: scene.layout.nodes.length,
          dpr: scene.dpr,
          backing: [canvas.width, canvas.height],
          expected: [Math.round(innerWidth * scene.dpr), Math.round(innerHeight * scene.dpr)],
          zones: scene.layout.zones.length,
          documentWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth
        };
      });

      expect(reading.nodes, `${viewport.name}: density tier`).toBe(viewport.nodes);
      expect(reading.backing, `${viewport.name}: the canvas covers the viewport`).toEqual(reading.expected);
      expect(reading.dpr, `${viewport.name}: device pixel ratio stays capped`).toBeLessThanOrEqual(1.75);
      expect(reading.zones, `${viewport.name}: exclusion still measured`).toBeGreaterThan(0);
      expect(
        reading.documentWidth,
        `${viewport.name}: the field introduces no horizontal overflow`
      ).toBeLessThanOrEqual(reading.innerWidth);
    }
  });

  /* The measurement axe declines to make.
   *
   * `color-contrast` returns `incomplete` for any text whose background stack contains a canvas —
   * a fact about the element type, proven in `a11y-vacuity.spec.ts` against a canvas that was never
   * drawn to. So the readability of text over the field is not something the accessibility scan can
   * answer, and this is where it gets answered instead: read the framebuffer inside each text rect,
   * blend what is there over the paper, and take the worst contrast that blend can produce against
   * that element's own resolved ink.
   *
   * Deliberately a *bound* rather than a rendering: the worst pixel anywhere inside a text's box
   * counts, whether or not a glyph is drawn on it, and twelve frames are sampled because the field
   * moves. Measured when this was written, the worst case across both themes was 4.93:1 on the demo
   * cards' 14.5px blurb — passing, with less headroom than the 7.36:1 that text has on bare paper.
   * That number is why this is a gate and not a note: with `ZONE_FLOOR` raised from 0.12 to 1, so
   * that reading surfaces stop suppressing the field, this drops to 4.36:1 under "Run scenario →"
   * and the light case turns red.
   */
  for (const theme of ["light", "dark"] as const) {
    test(`keeps every reading surface above WCAG AA (${theme})`, async ({ field: page }) => {
      for (const route of ["/", "/demo", "/trust"]) {
        await visit(page, route, { theme });

        const worst = await page.evaluate(async () => {
          const canvas = document.getElementById("careloop-field") as HTMLCanvasElement;
          const gl = (canvas.getContext("webgl2") ?? canvas.getContext("webgl")) as WebGLRenderingContext;

          const channel = (c: number) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
          const luminance = ([r, g, b]: number[]) =>
            0.2126 * channel(r / 255) + 0.7152 * channel(g / 255) + 0.0722 * channel(b / 255);
          const ratio = (a: number[], b: number[]) => {
            const [hi, lo] =
              luminance(a) >= luminance(b) ? [luminance(a), luminance(b)] : [luminance(b), luminance(a)];
            return (hi + 0.05) / (lo + 0.05);
          };

          /* The palette is authored in `oklch()`, and Chromium returns it verbatim from
             `getComputedStyle` — reading the digits would give the oklch triple rather than sRGB.
             A 1x1 2D context resolves any CSS colour the way the compositor does. */
          const probe = document.createElement("canvas").getContext("2d", {
            willReadFrequently: true
          }) as CanvasRenderingContext2D;
          probe.canvas.width = probe.canvas.height = 1;
          const rgb = (css: string) => {
            probe.clearRect(0, 0, 1, 1);
            probe.fillStyle = "#000";
            probe.fillStyle = css;
            probe.fillRect(0, 0, 1, 1);
            const data = probe.getImageData(0, 0, 1, 1).data;
            return [data[0], data[1], data[2]];
          };

          /* Text standing on its own opaque surface is not a case the field can reach — the solid
             button paints over it. Including those would compare paper-coloured ink against the
             paper and report a meaningless 1:1. */
          const standsOnItsOwnSurface = (el: Element) => {
            for (let node: Element | null = el; node && node !== document.body; node = node.parentElement) {
              const background = getComputedStyle(node).backgroundColor;
              if (
                background &&
                !/rgba\(0, 0, 0, 0\)|transparent/.test(background) &&
                !/, 0(\.\d+)?\)$/.test(background)
              ) {
                return true;
              }
            }
            return false;
          };

          const texts: { el: Element; rect: DOMRect }[] = [];
          for (const block of document.querySelectorAll("[data-scene-exclude]")) {
            for (const el of block.querySelectorAll("h1, h2, p, span, button, a, li")) {
              if (!el.textContent?.trim() || standsOnItsOwnSurface(el)) continue;
              const rect = el.getBoundingClientRect();
              if (rect.width < 8 || rect.height < 8 || rect.bottom < 0 || rect.top > innerHeight) continue;
              texts.push({ el, rect });
            }
          }

          const paper = rgb(getComputedStyle(document.body).backgroundColor);
          const dpr = window.devicePixelRatio || 1;
          let lowest: { ratio: number; onPaper: number; text: string; alpha: number } | null = null;

          for (let frame = 0; frame < 12; frame++) {
            await new Promise((resolve) => requestAnimationFrame(resolve));
            for (const { el, rect } of texts) {
              const x = Math.max(0, Math.floor(rect.left * dpr));
              // readPixels counts from the bottom left; the DOM counts from the top left.
              const y = Math.max(0, Math.floor((innerHeight - rect.bottom) * dpr));
              const width = Math.min(Math.ceil(rect.width * dpr), canvas.width - x);
              const height = Math.min(Math.ceil(rect.height * dpr), canvas.height - y);
              if (width <= 0 || height <= 0) continue;

              const pixels = new Uint8Array(width * height * 4);
              gl.readPixels(x, y, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
              const ink = rgb(getComputedStyle(el).color);

              for (let i = 0; i < pixels.length; i += 4) {
                const alpha = pixels[i + 3] / 255;
                if (alpha < 0.004) continue;
                const blended = [pixels[i], pixels[i + 1], pixels[i + 2]].map(
                  (c, index) => c * alpha + paper[index] * (1 - alpha)
                );
                const contrast = ratio(ink, blended);
                if (!lowest || contrast < lowest.ratio) {
                  lowest = {
                    ratio: Number(contrast.toFixed(2)),
                    onPaper: Number(ratio(ink, paper).toFixed(2)),
                    alpha: Number(alpha.toFixed(3)),
                    text: el.textContent.trim().slice(0, 40)
                  };
                }
              }
            }
          }
          return { samples: texts.length, lowest };
        });

        expect(worst.samples, `${route} (${theme}): there is text to measure`).toBeGreaterThan(0);
        expect(
          worst.lowest?.ratio ?? 21,
          `${route} (${theme}): worst blend under "${worst.lowest?.text}" ` +
            `(${worst.lowest?.onPaper}:1 on bare paper, field alpha ${worst.lowest?.alpha})`
        ).toBeGreaterThanOrEqual(4.5);
      }
    });
  }

  test("?motion=off leaves the application and nothing else", async ({ field: page }) => {
    await visit(page, "/demo?motion=off", { motion: false });
    await page.getByRole("heading", { name: "See CareLoop work." }).waitFor();

    const reading = await page.evaluate(() => ({
      canvas: !!document.getElementById("careloop-field"),
      scene: "__careLoopScene" in window,
      // Nothing was fetched for it either: the module is what pulls three.js in.
      requested: performance
        .getEntriesByType("resource")
        .some((entry) => entry.name.includes("careloop-scene") || entry.name.includes("three")),
      excluded: document.querySelectorAll("[data-scene-exclude]").length,
      zones: document.querySelectorAll("[data-scene-zone]").length
    }));

    expect(reading.canvas, "the canvas is removed rather than left blank").toBe(false);
    expect(reading.scene, "no scene is constructed").toBe(false);
    expect(reading.requested, "and nothing is downloaded for it").toBe(false);

    // The application is untouched, including the attributes the field would have read. They are
    // inert markup without it, which is the point: nothing on the page depends on the motion layer.
    expect(reading.excluded).toBeGreaterThan(0);
    expect(reading.zones).toBeGreaterThan(0);
    await expect(page.getByRole("button", { name: "Skip to the dashboard" })).toBeVisible();
  });

  test("the scenarios claim only what they can honour", async ({ field: page }) => {
    await visit(page, "/demo");

    const declared = await page.evaluate(() =>
      Array.from(document.querySelectorAll("[data-scene-zone]")).map((el) => el.getAttribute("data-scene-zone"))
    );
    const mode = () =>
      page.evaluate(() => (window as unknown as { __careLoopScene: { mode: string } }).__careLoopScene.mode);

    await page.getByText("Schedule a cardiology referral").hover();
    await expect.poll(mode, { message: "scenario 01 is a referral that completes" }).toBe("referral");

    await page.getByText("Malicious document").hover();
    await expect.poll(mode, { message: "scenario 02 is an injection whose branch ends" }).toBe("injection");

    /* Scenario 03 is `resume`: CareLoop stops for a decision it has no authority to take. The field
     * ships a `mismatch` sequence — a verification that disagrees and a loop that refuses to close —
     * and pointing 03 at it would animate a failure this scenario does not have. So the card carries
     * no zone and the field stays ambient. Asserted directly, because a wrong mapping would still
     * hover, still animate, and still look deliberate. */
    await page.getByText("Pause for a decision").hover();
    await expect.poll(mode, { message: "scenario 03 claims nothing" }).toBe("ambient");
    expect(declared, "nothing in this application is a verification mismatch").not.toContain("mismatch");

    // And the card is still a button that starts its run.
    await page.getByText("Pause for a decision").click();
    await page.waitForURL("**/");
  });

  test("the theme is one decision the application owns and the field follows", async ({ field: page }) => {
    await visit(page, "/");

    const themed = () =>
      page.evaluate(() => ({
        attribute: document.documentElement.getAttribute("data-theme"),
        klass: document.documentElement.classList.contains("dark"),
        scene: (window as unknown as { __careLoopScene: { themeName: string } }).__careLoopScene.themeName
      }));

    expect(await themed()).toEqual({ attribute: "light", klass: false, scene: "light" });

    await page.getByRole("button", { name: "Switch to dark" }).click();
    await expect
      .poll(themed, { message: "the field retones without a reload" })
      .toEqual({ attribute: "dark", klass: true, scene: "dark" });

    // And the choice survives a reload with the first frame already dark — the blocking script in
    // index.html writes both forms before any stylesheet, so there is no flash to catch.
    await page.reload();
    await fieldReady(page);
    expect(await themed()).toEqual({ attribute: "dark", klass: true, scene: "dark" });
  });

  test("a route change never restarts it and never leaks a context", async ({ field: page }) => {
    await visit(page, "/");

    const resources = () =>
      page.evaluate(() => {
        const scene = (
          window as unknown as {
            __careLoopScene: { renderer: { info: { memory: { geometries: number }; programs: unknown[] } } };
          }
        ).__careLoopScene;
        return {
          geometries: scene.renderer.info.memory.geometries,
          programs: scene.renderer.info.programs.length,
          canvases: document.querySelectorAll("#careloop-field").length
        };
      });

    const before = await resources();
    for (const route of ["/trust", "/demo", "/", "/trust", "/"]) {
      await visit(page, route);
    }

    /* The module re-boots itself if its canvas ever leaves the document, which is why the canvas is
     * static markup outside the React tree rather than an element React owns. Equal counts are how
     * that shows up: a second boot would leave the first scene's geometries and programs behind. */
    expect(await resources(), "one field, one context, across five route changes").toEqual(before);
  });
});
