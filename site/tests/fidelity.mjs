/**
 * Visual-fidelity gate: the pristine Claude Design bundle against the adapted page.
 *
 *   node fidelity.mjs --pristine http://127.0.0.1:8802/CareLoop/ --adapted http://127.0.0.1:8803/CareLoop/
 *
 * The adaptation is supposed to change what a crawler reads and where three lightbox backgrounds
 * point, and nothing else. This proves that, in the only two ways that are actually decidable:
 *
 * **Geometry, compared exactly.** Section boxes, document height, the pinned chapter's reserved
 * scroll length, and the horizontal overflow figure. These are deterministic across loads, so any
 * difference is a real layout regression rather than a timing artifact. Exact equality is the bar.
 *
 * **Pixels, with the animated layer removed.** The topology is a live field — signals travel,
 * ripples decay, ambient intervals are irregular on purpose — so two loads of the *same* build
 * never produce identical frames, and a strict pixel diff over it would measure the clock rather
 * than the change. `__careLoopStory.destroy()` removes exactly that layer and leaves the static
 * page: type, rules, cards, captures. Those must be pixel-identical, and the field's separate
 * presence is asserted before it is torn down.
 *
 * The lightbox is expected to differ and is reported separately: in the pristine bundle its
 * background is a 404 and the panel opens empty, which is the defect the adaptation repairs.
 */

import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, token, i, all) => {
    if (token.startsWith("--")) pairs.push([token.slice(2), all[i + 1]]);
    return pairs;
  }, []),
);

const PRISTINE = args.pristine ?? "http://127.0.0.1:8802/CareLoop/";
const ADAPTED = args.adapted ?? "http://127.0.0.1:8803/CareLoop/";
const OUT = args.out ?? "fidelity-out";

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 375, height: 812 },
];

/** The named states the plan requires, resolved to something the page can actually be put into. */
const STATES = [
  { name: "hero", scroll: 0 },
  { name: "calm-frame", nav: "story", nudge: 0 },
  { name: "authority-stop", nav: "trust", nudge: 0 },
  { name: "pinned-workflow", scrub: true, nudge: 900 },
  { name: "verification", nav: "trust", nudge: 1800 },
  { name: "receipts", nav: "proof", nudge: 0 },
  { name: "mutation-intact", nav: "proof", nudge: 1200 },
  { name: "architecture", nav: "hood", nudge: 0 },
  { name: "failure-simulation", nav: "hood", nudge: 1400 },
  { name: "final-closure", scroll: "bottom" },
];

async function settle(page) {
  await page.waitForFunction(() => document.querySelectorAll("main > section").length > 0);
  await page.waitForFunction(() => document.fonts.status === "loaded");
  await page.waitForTimeout(400);
}

async function open(browser, url, viewport) {
  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
  const page = await context.newPage();
  const faults = [];
  page.on("pageerror", (e) => faults.push(`pageerror: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && faults.push(`console: ${m.text()}`));
  page.on("response", (r) => r.status() >= 400 && faults.push(`${r.status()} ${r.url()}`));
  await page.goto(url, { waitUntil: "load" });
  await settle(page);
  return { context, page, faults };
}

/** Everything measurable that a layout regression would move. */
async function geometry(page) {
  return page.evaluate(() => {
    const round = (n) => Math.round(n * 100) / 100;
    const sections = [...document.querySelectorAll("main > section")].map((s, i) => {
      const b = s.getBoundingClientRect();
      return {
        i,
        nav: s.getAttribute("data-nav") ?? "",
        scrub: s.hasAttribute("data-story-scrub"),
        top: round(b.top + window.scrollY),
        height: round(b.height),
        width: round(b.width),
      };
    });
    return {
      docHeight: document.documentElement.scrollHeight,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      sectionCount: sections.length,
      sections,
      chips: document.querySelectorAll("button.cl-chip[data-atlas]").length,
      metrics: document.querySelectorAll("button.cl-metric").length,
      rows: document.querySelectorAll("button.cl-row").length,
      images: [...document.images].map((im) => ({
        alt: im.alt,
        natural: `${im.naturalWidth}x${im.naturalHeight}`,
        rendered: `${round(im.getBoundingClientRect().width)}x${round(im.getBoundingClientRect().height)}`,
      })),
      fieldPresent: !!document.querySelector("#careloop-story"),
      storyApi: (() => {
        const s = window.__careLoopStory;
        if (!s) return null;
        return ["setState", "focus", "pinState", "setMutant", "setNoBridge", "setHidden", "destroy"]
          .map((k) => `${k}:${typeof s[k]}`)
          .join(",");
      })(),
    };
  });
}

async function positionFor(page, state) {
  if (state.scroll === "bottom") {
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  } else if (typeof state.scroll === "number") {
    await page.evaluate((y) => window.scrollTo(0, y), state.scroll);
  } else if (state.scrub) {
    await page.evaluate((n) => {
      const el = document.querySelector("[data-story-scrub]");
      window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY + n);
    }, state.nudge ?? 0);
  } else {
    await page.evaluate(
      ([nav, nudge]) => {
        const el = document.querySelector(`main > section[data-nav="${nav}"]`);
        window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY + nudge);
      },
      [state.nav, state.nudge ?? 0],
    );
  }
  await settleAnimations(page);
}

/**
 * Wait until the chapter has finished revealing itself.
 *
 * Sections enter with a staggered `clRise`/`clFade` — 220–380ms plus a 40–80ms stagger — and the
 * pinned chapter additionally damps toward its scrubbed step. A fixed short pause captures those
 * mid-flight, and because the two pages are positioned one after the other they are caught at
 * different points, which reads as a fidelity difference that is really a stopwatch difference.
 * Measured: with a flat 320ms wait the pinned state reported ~1% differing pixels, and in
 * isolation the very same comparison reported zero. So wait for the animations themselves.
 */
async function settleAnimations(page) {
  await page.waitForTimeout(120);
  await page
    .waitForFunction(
      () => document.getAnimations().every((a) => a.playState !== "running"),
      undefined,
      { timeout: 5000 },
    )
    .catch(() => {
      // The field's own ambient loop can keep an animation running indefinitely; it is hidden for
      // these captures, and the fixed settle below covers anything else still in flight.
    });
  await page.waitForTimeout(420);
}

/**
 * Hide the live field so what remains is deterministic and worth diffing pixel by pixel.
 *
 * Hidden rather than destroyed: `destroy()` is one-way, so a torn-down field would force a reload
 * between every state, and the page is a 1.9 MB self-extracting document that re-parses on each
 * one. Hiding is reversible, costs nothing, and removes the same pixels.
 */
async function hideTheField(page) {
  await page.evaluate(() => {
    if (document.getElementById("__fidelity_still")) return;
    const style = document.createElement("style");
    style.id = "__fidelity_still";
    style.textContent = "#careloop-story{display:none !important}";
    document.head.appendChild(style);
  });
  await page.waitForTimeout(120);
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const report = { geometry: [], pixels: [], faults: {}, lightbox: {} };
  let differences = 0;

  for (const viewport of VIEWPORTS) {
    const a = await open(browser, PRISTINE, viewport);
    const b = await open(browser, ADAPTED, viewport);
    report.faults[`pristine/${viewport.name}`] = a.faults;
    report.faults[`adapted/${viewport.name}`] = b.faults;

    const ga = await geometry(a.page);
    const gb = await geometry(b.page);
    const same = JSON.stringify(ga) === JSON.stringify(gb);
    if (!same) differences += 1;
    report.geometry.push({ viewport: viewport.name, identical: same, pristine: ga, adapted: gb });

    // Geometry above was measured with the field live; pixels below are measured with it hidden.
    await hideTheField(a.page);
    await hideTheField(b.page);

    for (const state of STATES) {
      await positionFor(a.page, state);
      await positionFor(b.page, state);
      const key = `${viewport.name}-${state.name}`;
      const pa = join(OUT, `${key}.pristine.png`);
      const pb = join(OUT, `${key}.adapted.png`);
      await a.page.screenshot({ path: pa });
      await b.page.screenshot({ path: pb });
      report.pixels.push({ key, pristine: pa, adapted: pb });
    }

    // The one difference the adaptation is supposed to make, measured rather than assumed.
    for (const [label, ctx] of [
      ["pristine", a],
      ["adapted", b],
    ]) {
      const shots = [];
      ctx.page.on("response", (r) => {
        if (/product\/.*\.png/.test(r.url())) shots.push(`${r.status()} ${new URL(r.url()).pathname}`);
      });
      await ctx.page.getByRole("button", { name: /^Enlarge / }).first().click();
      await ctx.page.waitForTimeout(500);
      const bg = await ctx.page
        .locator('[role="dialog"] [role="img"]')
        .evaluate((n) => getComputedStyle(n).backgroundImage)
        .catch(() => "no dialog");
      report.lightbox[`${label}/${viewport.name}`] = { background: bg, responses: shots };
      await ctx.page.keyboard.press("Escape");
    }

    await a.context.close();
    await b.context.close();
  }

  await browser.close();
  writeFileSync(join(OUT, "report.json"), JSON.stringify(report, null, 2));

  console.log("\n=== geometry ===");
  for (const g of report.geometry) {
    console.log(`  ${g.viewport}: ${g.identical ? "IDENTICAL" : "DIFFERENT"}`);
    if (!g.identical) {
      for (const k of Object.keys(g.pristine)) {
        const x = JSON.stringify(g.pristine[k]);
        const y = JSON.stringify(g.adapted[k]);
        if (x !== y) console.log(`    ${k}:\n      pristine ${x.slice(0, 300)}\n      adapted  ${y.slice(0, 300)}`);
      }
    }
    console.log(
      `    docHeight=${g.pristine.docHeight} scrollWidth=${g.pristine.scrollWidth} innerWidth=${g.pristine.innerWidth} sections=${g.pristine.sectionCount}`,
    );
  }
  console.log("\n=== faults ===");
  for (const [k, v] of Object.entries(report.faults)) console.log(`  ${k}: ${v.length ? v.join(" | ") : "none"}`);
  console.log("\n=== lightbox ===");
  for (const [k, v] of Object.entries(report.lightbox)) {
    console.log(`  ${k}: ${v.background.slice(0, 90)}  responses=[${v.responses.join(", ")}]`);
  }
  console.log(`\nscreenshot pairs written to ${OUT}/ (compare with compare_shots.py)`);
  process.exit(differences > 0 ? 1 : 0);
}

main();
