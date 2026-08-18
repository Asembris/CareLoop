/* The boot loader, and the one thing it must never cost: CareLoop.
 *
 * The loader is decoration in the strict sense — deleting it would leave the same application
 * underneath — and almost everything below is a statement about that, not about the animation.
 * The animation is the *subject* of two tests; it is the *hazard* in the rest.
 *
 * **This file opts in.** Every other spec imports `test` from `./base`, which seeds the loader's
 * own `careloop:boot` key so the overlay never appears. This one imports from `@playwright/test`
 * directly, drives unmodified URLs, and therefore sees exactly what a visitor sees.
 *
 * ## No sleeps, and no assertions about how long anything takes
 *
 * Everything here waits on a *boundary the production code already publishes* — the
 * `careloop:booted` event, the presence of `data-booting`, a computed `pointer-events`, an
 * attribute the loader writes. Nothing waits on a duration, and nothing asserts one: the two
 * places that care about ordering compare marks stamped inside the page against each other, never
 * against a constant. The suite runs on a two-core Linux runner and on this machine, and the only
 * thing both agree about is order.
 *
 * Listeners are installed with `addInitScript`, before anything on the page runs, because
 * `careloop:ready` fires about ninety milliseconds after navigation and a listener attached from
 * the test side would routinely miss it.
 *
 * ## One page per worker
 *
 * The same capacity fact `field.spec.ts` documents: the frontend mints a world per tab and the API
 * holds sixty-four of them. This file needs a *fresh* context per test — a tab that has already
 * booted will not boot again, which is the point of half of these — so it closes each one rather
 * than accumulating, and reaches the API for nothing.
 */

import { expect, test, type Browser, type Page } from "@playwright/test";

/** What the loader leaves in the DOM while it is running, and what it must leave behind. */
const OVERLAY = "#careloop-boot";
const NAVBAR_MARK = '[data-scene-zone="logo"] svg';

interface Marks {
  ready?: number;
  booted?: number;
  inert?: number;
  overlaySeen: boolean;
}

/**
 * A fresh tab that records the loader's own boundaries as they happen.
 *
 * The recorder is installed before the document's first script, so nothing it watches for can
 * happen first. `inert` is stamped by a frame loop rather than by an event because the production
 * code publishes no event for it — but the *value* it reads is production behaviour
 * (`pointer-events: none` on the overlay), not a probe added for this file.
 */
async function arrive(browser: Browser, url: string, options: Parameters<Browser["newContext"]>[0] = {}) {
  const context = await browser.newContext(options);
  const page = await context.newPage();

  await page.addInitScript(() => {
    const marks: Record<string, number | boolean> = { overlaySeen: false };
    (window as unknown as { __boot: typeof marks }).__boot = marks;
    const stamp = (name: string) => {
      if (marks[name] === undefined) marks[name] = performance.now();
    };
    window.addEventListener("careloop:ready", () => stamp("ready"));
    window.addEventListener("careloop:booted", () => stamp("booted"));

    const watch = () => {
      const overlay = document.getElementById("careloop-boot");
      if (overlay) {
        marks.overlaySeen = true;
        if (getComputedStyle(overlay).pointerEvents === "none") stamp("inert");
      }
      if (marks.booted === undefined) requestAnimationFrame(watch);
    };
    requestAnimationFrame(watch);
  });

  await page.goto(url);
  return { context, page };
}

/** Wait for the loader to have finished leaving. The event is its own, and it is the last thing
 *  `finalize()` does — so nothing that must be cleaned up can still be outstanding when it lands. */
async function booted(page: Page): Promise<void> {
  await page.waitForFunction(
    () => (window as unknown as { __boot?: Marks }).__boot?.booted !== undefined
  );
}

async function marks(page: Page): Promise<Marks> {
  return page.evaluate(() => (window as unknown as { __boot: Marks }).__boot);
}

/** Everything the loader is allowed to touch, read back after it claims to have finished. */
async function residue(page: Page) {
  return page.evaluate(
    ({ overlay, mark }) => {
      const target = document.querySelector(mark) as SVGElement | null;
      return {
        overlay: document.querySelectorAll(overlay).length,
        fliers: document.querySelectorAll("[data-boot-flier]").length,
        hiddenMarks: document.querySelectorAll("[data-boot-hidden]").length,
        booting: document.documentElement.hasAttribute("data-booting"),
        markPresent: !!target,
        markInlineOpacity: target ? target.style.opacity : "(absent)",
        markComputedOpacity: target ? getComputedStyle(target).opacity : "(absent)",
        // The Phase 1 contract, restated here because the loader sat on top of it.
        rootZIndex: getComputedStyle(document.getElementById("root") as HTMLElement).zIndex,
        rootPosition: getComputedStyle(document.getElementById("root") as HTMLElement).position,
        bodyOverflow: getComputedStyle(document.body).overflow,
        htmlOverflow: getComputedStyle(document.documentElement).overflow
      };
    },
    { overlay: OVERLAY, mark: NAVBAR_MARK }
  );
}

/** The header is back, whole, and nothing of the loader is left on the page. */
function expectNoResidue(seen: Awaited<ReturnType<typeof residue>>, label: string) {
  expect(seen.overlay, `${label}: the overlay is removed, not merely transparent`).toBe(0);
  expect(seen.fliers, `${label}: the flying mark is gone`).toBe(0);
  expect(seen.hiddenMarks, `${label}: nothing is left marked as hidden by the loader`).toBe(0);
  expect(seen.booting, `${label}: <html> no longer claims to be booting`).toBe(false);
  expect(seen.markPresent, `${label}: the real navbar mark is on the page`).toBe(true);
  expect(seen.markInlineOpacity, `${label}: the loader's inline opacity is removed, not set to 1`).toBe("");
  expect(seen.markComputedOpacity, `${label}: the brand is visible`).toBe("1");
  expect(seen.rootZIndex, `${label}: the Phase 1 stacking contract is intact`).toBe("1");
  expect(seen.rootPosition, `${label}: the application is still positioned`).toBe("relative");
  expect(seen.bodyOverflow, `${label}: scrolling was never taken away`).toBe("visible");
  expect(seen.htmlOverflow, `${label}: scrolling was never taken away`).toBe("visible");
}

/** The application answers a click, which is the only interesting definition of "usable". */
async function stillUsable(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Trust", exact: true }).click();
  await expect(page).toHaveURL(/\/trust$/);
  await expect(
    page.getByRole("heading", { name: "The model never authorizes its own actions." })
  ).toBeVisible();
  await page.getByRole("button", { name: "CareLoop — home" }).click();
  await expect(page).toHaveURL(/\/$/);
}

test.describe("the boot loader", () => {
  test("appears, runs its whole arc, and leaves nothing behind", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");
    await booted(page);

    const seen = await marks(page);
    expect(seen.overlaySeen, "the loader actually painted").toBe(true);
    expect(seen.ready, "the application announced itself").toBeDefined();
    // Ordering, not duration: readiness precedes the hand-off, and the overlay stops taking
    // clicks before it is removed. Both are compared against marks from the same clock.
    expect(seen.ready!, "readiness comes before the hand-off completes").toBeLessThan(seen.booted!);
    expect(seen.inert, "the overlay became transparent at some point").toBeDefined();
    expect(seen.inert!, "and it was inert before it was removed").toBeLessThanOrEqual(seen.booted!);

    expectNoResidue(await residue(page), "after a normal boot");
    await stillUsable(page);
    await context.close();
  });

  test("does not boot a second time in the same tab", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");
    await booted(page);

    // A reload is the ordinary way a judge sees the loader again, and the ordinary way they get
    // bored of it. The entrance is an entrance; the second arrival is not one.
    await page.reload();
    await expect(page.getByRole("button", { name: "CareLoop — home" })).toBeVisible();
    expect(await page.locator(OVERLAY).count(), "no second entrance").toBe(0);
    expectNoResidue(await residue(page), "on a reload");
    await context.close();
  });

  test("?boot=off skips it, and stays off for the whole tab", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/?boot=off");
    await expect(page.getByRole("button", { name: "CareLoop — home" })).toBeVisible();

    const seen = await marks(page);
    expect(seen.overlaySeen, "the loader never painted").toBe(false);
    expect(seen.booted, "and it never announced a departure it did not make").toBeUndefined();
    expectNoResidue(await residue(page), "with the loader off");

    /* The half a URL-only switch would miss. Client-side routing drops the query, so a reload
       after navigating is a bare address — and a kill switch a navigation silently undoes is not
       a kill switch. */
    await page.getByRole("button", { name: "Trust", exact: true }).click();
    await expect(page).toHaveURL(/\/trust$/);
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "The model never authorizes its own actions." })
    ).toBeVisible();
    expect(await page.locator(OVERLAY).count(), "still off after a bare reload").toBe(0);
    await context.close();
  });

  test("?boot=on replays it, and clears a sticky off", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/?boot=off");
    await expect(page.getByRole("button", { name: "CareLoop — home" })).toBeVisible();
    expect(await page.locator(OVERLAY).count()).toBe(0);

    await page.goto("/?boot=on");
    await booted(page);
    expect((await marks(page)).overlaySeen, "one flag is enough to get it back").toBe(true);
    expectNoResidue(await residue(page), "after an explicit replay");
    await context.close();
  });

  test("?boot=full holds for the complete choreography without moving readiness", async ({ browser }) => {
    const quick = await arrive(browser, "/?boot=on");
    await booted(quick.page);
    const condensed = await marks(quick.page);
    await quick.context.close();

    const long = await arrive(browser, "/?boot=full");
    await booted(long.page);
    const full = await marks(long.page);

    /* Two properties, and they are the ones that matter. Readiness is a fact about the
       application, so the flag must not move it — asserted as *both* paths announcing before
       either hands off, which is an ordering claim rather than a timing one. And the full path
       must genuinely be the longer one, compared against the condensed path measured on the same
       machine in the same run rather than against a constant. */
    expect(full.ready, "?boot=full still announces readiness").toBeDefined();
    expect(full.ready!, "and still announces it before handing off").toBeLessThan(full.booted!);
    expect(
      full.booted! - full.ready!,
      "the complete choreography is the longer one"
    ).toBeGreaterThan(condensed.booted! - condensed.ready!);

    expectNoResidue(await residue(long.page), "after the full choreography");
    await stillUsable(long.page);
    await long.context.close();
  });

  test("the overlay never swallows a click it cannot be seen to be taking", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    /* The failure this exists for: upstream drops the overlay's opacity 740ms before removing it
       and never sets `pointer-events`, so the application is fully visible and completely dead to
       the pointer for three quarters of a second — which looks exactly like a broken application
       and does not appear in a screenshot.
       Sampled every frame from inside the page, over the whole life of the overlay, so there is no
       window between two reads for it to hide in. */
    const verdict = await page.evaluate(
      () =>
        new Promise<{ frames: number; blockingWhileTransparent: number }>((resolve) => {
          let frames = 0;
          let blockingWhileTransparent = 0;
          const step = () => {
            const overlay = document.getElementById("careloop-boot");
            if (!overlay) return resolve({ frames, blockingWhileTransparent });
            frames += 1;
            const style = getComputedStyle(overlay);
            const transparent = Number(style.opacity) < 0.99;
            const takesClicks = style.pointerEvents !== "none";
            if (transparent && takesClicks) blockingWhileTransparent += 1;
            requestAnimationFrame(step);
          };
          requestAnimationFrame(step);
        })
    );

    expect(verdict.frames, "the overlay was observed while it was up").toBeGreaterThan(10);
    expect(
      verdict.blockingWhileTransparent,
      "no frame in which the overlay is see-through and still taking clicks"
    ).toBe(0);

    await booted(page);
    await stillUsable(page);
    await context.close();
  });

  test("hands the mark over without ever hiding both", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    /* Continuity, sampled rather than reasoned about: at no frame may the brand be absent from
       the screen. Either the loader's mark is up, or the flier is in transit, or the header's own
       mark is visible. A frame with none of the three is the ugly one this ordering exists to
       avoid, and it is the frame a screenshot would have to be lucky to catch. */
    const blankFrames = await page.evaluate(
      (mark) =>
        new Promise<number>((resolve) => {
          let blank = 0;
          const visible = (el: Element | null) => {
            if (!el) return false;
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return Number(style.opacity) > 0.05 && style.visibility !== "hidden" && rect.width > 0;
          };
          const step = () => {
            const overlay = document.getElementById("careloop-boot");
            const flier = document.querySelector("[data-boot-flier]");
            const header = document.querySelector(mark);
            const overlayMark = overlay?.querySelector("svg[viewBox='0 0 64 64']") ?? null;
            if (!visible(overlayMark) && !visible(flier) && !visible(header)) blank += 1;
            if (!overlay && !flier) return resolve(blank);
            requestAnimationFrame(step);
          };
          requestAnimationFrame(step);
        }),
      NAVBAR_MARK
    );

    expect(blankFrames, "the brand is on screen continuously through the hand-off").toBe(0);

    await booted(page);
    expectNoResidue(await residue(page), "after the hand-off");
    await context.close();
  });

  for (const theme of ["light", "dark"] as const) {
    test(`paints in the application's own ${theme} paper`, async ({ browser }) => {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.addInitScript((value: string) => localStorage.setItem("careloop:theme", value), theme);
      await page.goto("/");

      /* Both halves in one synchronous block, and both resolved by the compositor rather than
         read as text. `--background` is authored in `oklch()` and Chromium returns it verbatim
         from `getComputedStyle`; a 1x1 2D context resolves any CSS colour the way the page does.
         Read from one layout, so this cannot become the two-moments race the field spec fixed. */
      const paper = await page.evaluate(() => {
        const overlay = document.getElementById("careloop-boot");
        if (!overlay) return null;
        const probe = document.createElement("canvas").getContext("2d") as CanvasRenderingContext2D;
        probe.canvas.width = probe.canvas.height = 1;
        const rgb = (css: string) => {
          probe.clearRect(0, 0, 1, 1);
          probe.fillStyle = "#000";
          probe.fillStyle = css;
          probe.fillRect(0, 0, 1, 1);
          const data = probe.getImageData(0, 0, 1, 1).data;
          return [data[0], data[1], data[2]];
        };
        return {
          overlay: rgb(getComputedStyle(overlay).backgroundColor),
          application: rgb(getComputedStyle(document.body).backgroundColor),
          theme: document.documentElement.getAttribute("data-theme")
        };
      });

      expect(paper, "the loader was still up to be measured").not.toBeNull();
      expect(paper!.theme, "the application decided the theme").toBe(theme);
      // One level per channel is the rounding between an oklch triple and a hex literal. Twelve —
      // which is what the package shipped for dark — is a visible sheet over the page.
      for (let channel = 0; channel < 3; channel++) {
        expect(
          Math.abs(paper!.overlay[channel] - paper!.application[channel]),
          `${theme}: the loader's paper is the application's paper ` +
            `(loader ${paper!.overlay}, application ${paper!.application})`
        ).toBeLessThanOrEqual(1);
      }
      await context.close();
    });
  }

  test("a first-time visitor whose system is dark never sees a light frame", async ({ browser }) => {
    // No stored preference, and nothing has run yet to create one: the blocking script in
    // index.html has to resolve the system preference before the loader is even parsed.
    const { context, page } = await arrive(browser, "/", { colorScheme: "dark" });

    const first = await page.evaluate(() => {
      const overlay = document.getElementById("careloop-boot");
      const probe = document.createElement("canvas").getContext("2d") as CanvasRenderingContext2D;
      probe.canvas.width = probe.canvas.height = 1;
      probe.fillStyle = getComputedStyle(overlay as Element).backgroundColor;
      probe.fillRect(0, 0, 1, 1);
      const data = probe.getImageData(0, 0, 1, 1).data;
      return {
        stored: localStorage.getItem("careloop:theme"),
        theme: document.documentElement.getAttribute("data-theme"),
        luminance: (data[0] + data[1] + data[2]) / 3
      };
    });

    expect(first.stored, "nothing was persisted; the system preference is doing the work").toBeNull();
    expect(first.theme, "the application resolved dark before the loader existed").toBe("dark");
    expect(first.luminance, "and the loader painted dark, not ivory").toBeLessThan(80);
    await context.close();
  });

  test("reduced motion gets a static composition and a prompt exit", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/", { reducedMotion: "reduce" });

    // The composition is complete from the first frame rather than drawn: every path is at its
    // resting opacity immediately, which is what "static" means here.
    const drawn = await page.evaluate(() => {
      const overlay = document.getElementById("careloop-boot");
      if (!overlay) return null;
      /* The topology svg only. It is the overlay's first child; the brand mark is a second,
         nested svg further down, and its two strokes have no dash offset to be mid-draw. */
      const topology = overlay.querySelector("svg");
      const paths = Array.from(topology ? topology.querySelectorAll("path") : []);
      return {
        paths: paths.length,
        allPlaced: paths.every((p) => (p as SVGElement).style.strokeDashoffset === "0"),
        anyVisible: paths.some((p) => Number(p.getAttribute("opacity")) > 0)
      };
    });
    expect(drawn, "the loader ran at all").not.toBeNull();
    expect(drawn!.paths).toBeGreaterThan(0);
    expect(drawn!.allPlaced, "nothing is mid-draw under reduced motion").toBe(true);
    expect(drawn!.anyVisible, "the structure is shown rather than animated into being").toBe(true);

    await booted(page);
    const seen = await marks(page);
    /* Prompt, expressed as ordering against the same page's own readiness rather than as a
       millisecond budget: reduced motion must not sit through the arc it is not playing. It is
       compared against the condensed path in the `?boot=full` test; here the claim is only that it
       leaves without waiting for a choreography it skipped. */
    expect(seen.booted! - seen.ready!, "no animation is being waited out").toBeLessThan(1500);
    expectNoResidue(await residue(page), "under reduced motion");
    await stillUsable(page);
    await context.close();
  });

  test("a missing hand-off target fails open", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    // Take the target away before the loader reaches for it. This is the "navbar not mounted",
    // "route replaced the header", and "somebody renamed the zone" cases at once.
    await page.evaluate((mark) => {
      const target = document.querySelector(mark);
      if (target) target.remove();
    }, NAVBAR_MARK);

    await booted(page);
    const seen = await residue(page);
    expect(seen.overlay, "the overlay left anyway").toBe(0);
    expect(seen.fliers, "and took nothing with it").toBe(0);
    expect(seen.booting).toBe(false);
    await stillUsable(page);
    await context.close();
  });

  test("a hand-off that throws still gives the brand back", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    /* The worst case in the whole layer, and the reason `exit()` has a `finally`. Upstream hides
       the navbar mark at the top of the hand-off and restores it from a timer 890ms later; a
       throw anywhere between the two leaves the brand permanently invisible under a permanently
       mounted overlay at z-index 9000.
       Injected at the measurement, which is where a real one would come from — a detached node, a
       frozen element, a rect on something React has just unmounted. */
    await page.evaluate((mark) => {
      const target = document.querySelector(mark) as SVGElement;
      target.getBoundingClientRect = () => {
        throw new Error("induced hand-off failure");
      };
    }, NAVBAR_MARK);

    await booted(page);
    expectNoResidue(await residue(page), "after an induced hand-off failure");
    await stillUsable(page);
    await context.close();
  });

  test("a hand-off that throws after taking the brand still gives it back", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    /* The sharper half of the previous test, and the one that pins where restoration lives.
     *
     * A throw at the measurement happens before the header's mark has been touched, so a teardown
     * that restored nothing would still leave a correct page — which means that test alone cannot
     * tell whether `finalize()` restores anything. This one fails *after* the brand has been
     * taken and before the timer that would ordinarily give it back, which is the window upstream
     * leaves open: the mark is hidden at the top of the hand-off and un-hidden 890ms later by a
     * `setTimeout` nobody reaches when an exception has already unwound the method.
     *
     * `appendChild` is the injection point because it is the first thing the hand-off does after
     * taking the brand. What it stands in for is the class, not this call: any exception in the
     * flier setup, the wordmark dissolve or the topology fade lands in the same place. */
    await page.evaluate(() => {
      const real = document.body.appendChild.bind(document.body);
      document.body.appendChild = ((node: Node) => {
        if (node instanceof Element && node.hasAttribute("data-boot-flier")) {
          throw new Error("induced failure after the brand was taken");
        }
        return real(node);
      }) as typeof document.body.appendChild;
    });

    await booted(page);
    expectNoResidue(await residue(page), "after a throw mid-hand-off");
    await stillUsable(page);
    await context.close();
  });

  test("a degenerate hand-off geometry fails open rather than animating nonsense", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    // A zero-width target, which is what an unlaid-out or hidden header measures as. Upstream
    // divides by it, gets `scale = 0`, and flies the mark to nothing in front of the reader.
    await page.evaluate((mark) => {
      const target = document.querySelector(mark) as SVGElement;
      target.getBoundingClientRect = () =>
        ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    }, NAVBAR_MARK);

    /* The assertion is that it *declines*, not merely that it cleans up afterwards. A hand-off
       that runs at scale zero also cleans up afterwards, so "no residue" alone cannot tell the
       two apart. Sampled every frame across the overlay's whole life: the brand is never taken
       and no flier is ever created, because there was nothing worth flying to. */
    const observed = await page.evaluate(
      () =>
        new Promise<{ frames: number; fliers: number; hidden: number }>((resolve) => {
          let frames = 0;
          let fliers = 0;
          let hidden = 0;
          const step = () => {
            if (!document.getElementById("careloop-boot")) return resolve({ frames, fliers, hidden });
            frames += 1;
            fliers += document.querySelectorAll("[data-boot-flier]").length;
            hidden += document.querySelectorAll("[data-boot-hidden]").length;
            requestAnimationFrame(step);
          };
          requestAnimationFrame(step);
        })
    );

    expect(observed.frames, "the overlay was observed while it was up").toBeGreaterThan(10);
    expect(observed.fliers, "nothing was ever flown to a rect of no size").toBe(0);
    expect(observed.hidden, "and the header's own mark was never taken away").toBe(0);

    await booted(page);
    expectNoResidue(await residue(page), "after a degenerate measurement");
    await stillUsable(page);
    await context.close();
  });

  test("readiness that never arrives cannot trap anybody", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();

    // Silence the announcement before anything can make it. The loader's own nine-second timeout
    // and `index.html`'s twelve-second reaper are all that is left, which is the point.
    await page.addInitScript(() => {
      const dispatch = window.dispatchEvent.bind(window);
      window.dispatchEvent = ((event: Event) =>
        event.type === "careloop:ready" ? true : dispatch(event)) as typeof window.dispatchEvent;
    });
    await page.goto("/");
    expect(await page.locator(OVERLAY).count(), "the loader is up and nothing has told it to go").toBe(1);

    /* A bounded wait on a user-visible outcome, not a sleep: the overlay is gone and the header
       answers a click. The generous ceiling is the loader's own hard timeout plus the reaper
       behind it — this asserts that *some* bound exists, and deliberately not which one fired. */
    await expect(page.locator(OVERLAY)).toHaveCount(0, { timeout: 20_000 });
    expectNoResidue(await residue(page), "after the hard timeout");
    await stillUsable(page);
    await context.close();
  });

  test("the reaper removes an overlay the loader itself never cleans up", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();

    /* The second enforcement point, tested with the first one gone.
     *
     * `page.route`, and this is the third state in the suite to use it — justified here because
     * the property is *"the loader file is broken"*, and there is no honest way to reach that from
     * outside the file. Patching the real loader's teardown is not it: the reaper removes the
     * overlay with `Element.remove`, which is the same call the loader makes, so anything that
     * stops one stops both and the test would prove nothing. So the loader is replaced with a
     * defect: it mounts an overlay, hides the navbar mark and leaves a flier exactly as a real
     * hand-off does, and then never finishes. Nothing but `index.html` can end this page.
     *
     * It covers what the loader's own `finally` and hard timer structurally cannot: a build that
     * throws after the overlay is up, a `finalize()` a future edit breaks, an exit that dies
     * somewhere new. */
    await page.route("**/careloop-boot.js", (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `
          document.documentElement.setAttribute("data-booting", "");
          var overlay = document.createElement("div");
          overlay.id = "careloop-boot";
          overlay.setAttribute("aria-hidden", "true");
          overlay.style.cssText =
            "position:fixed;inset:0;z-index:9000;background:#f7f4ee";
          document.body.appendChild(overlay);
          var flier = document.createElement("div");
          flier.setAttribute("data-boot-flier", "");
          flier.style.cssText = "position:fixed;left:0;top:0;width:23px;height:23px;z-index:9100";
          document.body.appendChild(flier);
          // Take the brand the way a real hand-off does, then never give it back.
          setTimeout(function () {
            var mark = document.querySelector('[data-scene-zone="logo"] svg');
            if (mark) {
              mark.style.opacity = "0";
              mark.setAttribute("data-boot-hidden", "");
            }
          }, 400);
        `
      })
    );

    await page.goto("/");
    expect(await page.locator(OVERLAY).count(), "the broken loader mounted").toBe(1);
    await expect(page.locator("[data-boot-hidden]")).toHaveCount(1);

    /* A bounded wait on the user-visible outcome. Nothing here asserts when the reaper fires or
     * that it was the reaper rather than something else — only that a bound exists and that the
     * page comes back whole, which is the whole claim. */
    await expect(page.locator(OVERLAY)).toHaveCount(0, { timeout: 20_000 });
    expectNoResidue(await residue(page), "after a loader that never cleaned up");
    await stillUsable(page);
    await context.close();
  });

  test("the loader is nowhere in the accessibility tree and traps no focus", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");

    const semantics = await page.evaluate(() => {
      const overlay = document.getElementById("careloop-boot");
      if (!overlay) return null;
      const focusable = overlay.querySelectorAll(
        'a[href], button, input, select, textarea, [tabindex], [contenteditable]'
      );
      return {
        ariaHidden: overlay.getAttribute("aria-hidden"),
        role: overlay.getAttribute("role"),
        ariaLive: overlay.getAttribute("aria-live"),
        focusable: focusable.length,
        activeInside: overlay.contains(document.activeElement)
      };
    });

    expect(semantics, "the loader was up").not.toBeNull();
    expect(semantics!.ariaHidden, "decorative, and out of the tree").toBe("true");
    expect(semantics!.role, "not announced as a status").toBeNull();
    // The reason: the application underneath is *not* hidden from assistive technology, so a live
    // region here would narrate a picture over a page the reader is already on.
    expect(semantics!.ariaLive, "and not a live region narrating every beat").toBeNull();
    expect(semantics!.focusable, "nothing in it can be reached by keyboard").toBe(0);
    expect(semantics!.activeInside, "and focus was never moved into it").toBe(false);

    // Keyboard input during boot reaches the application, which is where it belongs.
    await page.keyboard.press("Tab");
    const focusedTag = await page.evaluate(() => document.activeElement?.closest("#careloop-boot") !== null);
    expect(focusedTag, "focus did not land inside the loader").toBe(false);

    await booted(page);
    await context.close();
  });

  test("its one line of copy is readable and claims no work that is not happening", async ({ browser }) => {
    for (const theme of ["light", "dark"] as const) {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.addInitScript((value: string) => localStorage.setItem("careloop:theme", value), theme);
      await page.goto("/");

      /* axe cannot judge this: the container is `aria-hidden`, so the contrast rule skips it
         entirely. That is the right call for the scan and the reason the measurement lives here
         instead — read the resolved ink, the overlay's own opacity and the paper from one layout
         and compute the ratio the reader actually gets. */
      const reading = await page.evaluate(() => {
        const overlay = document.getElementById("careloop-boot");
        if (!overlay) return null;
        /* The overlay holds exactly two spans, in this order: the wordmark and the status
           line. Taken by position because both are plain text with no role, no id and no class,
           and the wordmark's text is "CareLoop" — so any content-based filter for the status line
           would have to encode the answer this test exists to check. */
        const spans = Array.from(overlay.querySelectorAll("span"));
        const line = spans[spans.length - 1];
        if (!line || spans.length < 2) return null;

        const probe = document.createElement("canvas").getContext("2d") as CanvasRenderingContext2D;
        probe.canvas.width = probe.canvas.height = 1;
        const rgb = (css: string) => {
          probe.clearRect(0, 0, 1, 1);
          probe.fillStyle = "#000";
          probe.fillStyle = css;
          probe.fillRect(0, 0, 1, 1);
          const d = probe.getImageData(0, 0, 1, 1).data;
          return [d[0], d[1], d[2]];
        };
        const channel = (c: number) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
        const luminance = (c: number[]) =>
          0.2126 * channel(c[0] / 255) + 0.7152 * channel(c[1] / 255) + 0.0722 * channel(c[2] / 255);

        const paper = rgb(getComputedStyle(overlay).backgroundColor);
        const ink = rgb(getComputedStyle(line).color);
        // The line is faded in; the colour the eye gets is the blend, so that is what is measured.
        const alpha = Number(getComputedStyle(line).opacity) || 1;
        const blended = ink.map((c, i) => c * alpha + paper[i] * (1 - alpha));
        const [hi, lo] =
          luminance(blended) >= luminance(paper)
            ? [luminance(blended), luminance(paper)]
            : [luminance(paper), luminance(blended)];

        return { text: (line.textContent ?? "").trim(), ratio: (hi + 0.05) / (lo + 0.05) };
      });

      expect(reading, `${theme}: the status line was on screen`).not.toBeNull();
      /* One neutral line. CareLoop performs no work at boot — no session to establish, no context
         to connect, nothing to verify — so the copy says only that it is getting ready and the
         choreography carries the orchestration story. A regression here would be an invented
         operational claim, which is the one kind of copy this product does not ship. */
      expect(reading!.text, `${theme}: one restrained line, and no invented operation`).toBe(
        "Preparing CareLoop"
      );
      expect(
        reading!.ratio,
        `${theme}: the status line clears AA at ${reading!.ratio.toFixed(2)}:1`
      ).toBeGreaterThanOrEqual(4.5);
      await context.close();
    }
  });

  test("the orchestration field is healthy after the hand-off", async ({ browser }) => {
    const { context, page } = await arrive(browser, "/");
    await booted(page);

    await page.waitForFunction(() => {
      const scene = (
        window as unknown as { __careLoopScene?: { renderer: { info: { render: { frame: number } } } } }
      ).__careLoopScene;
      return !!scene && scene.renderer.info.render.frame > 2;
    });

    const field = await page.evaluate(() => {
      const scene = (
        window as unknown as {
          __careLoopScene: {
            themeName: string;
            renderer: { info: { memory: { geometries: number }; programs: unknown[] } };
          };
        }
      ).__careLoopScene;
      const canvas = document.getElementById("careloop-field") as HTMLCanvasElement;
      return {
        canvases: document.querySelectorAll("#careloop-field").length,
        theme: scene.themeName,
        attribute: document.documentElement.getAttribute("data-theme"),
        zIndex: getComputedStyle(canvas).zIndex,
        pointerEvents: getComputedStyle(canvas).pointerEvents,
        geometries: scene.renderer.info.memory.geometries,
        programs: scene.renderer.info.programs.length
      };
    });

    expect(field.canvases, "one field, and the loader never disturbed it").toBe(1);
    expect(field.theme, "the field and the application still agree").toBe(field.attribute);
    expect(field.zIndex, "paper → field → application, unchanged").toBe("0");
    expect(field.pointerEvents).toBe("none");
    expect(field.geometries, "the field was not re-initialised behind the loader").toBeGreaterThan(0);
    expect(field.programs).toBeGreaterThan(0);
    await context.close();
  });

  test.describe("the two flags are independent", () => {
    const COMBINATIONS = [
      { url: "/?boot=on", loader: true, field: true },
      { url: "/?boot=off", loader: false, field: true },
      { url: "/?motion=off&boot=on", loader: true, field: false },
      { url: "/?boot=off&motion=off", loader: false, field: false },
      { url: "/?boot=full&motion=off", loader: true, field: false }
    ] as const;

    for (const combination of COMBINATIONS) {
      test(combination.url, async ({ browser }) => {
        const { context, page } = await arrive(browser, combination.url);
        await expect(page.getByRole("button", { name: "CareLoop — home" })).toBeVisible();

        if (combination.loader) {
          await booted(page);
          expect((await marks(page)).overlaySeen, `${combination.url}: the loader ran`).toBe(true);
        } else {
          expect(await page.locator(OVERLAY).count(), `${combination.url}: no loader`).toBe(0);
        }

        const canvas = await page.locator("#careloop-field").count();
        expect(canvas, `${combination.url}: the field canvas`).toBe(combination.field ? 1 : 0);

        expectNoResidue(await residue(page), combination.url);
        await stillUsable(page);
        await context.close();
      });
    }
  });
});
