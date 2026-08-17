# Design Reference

**When implementation and written description appear to conflict, use the
supplied screenshots and current source implementation as the visual source of
truth.**

The screenshots in `screenshots/` are golden references for the approved UI.
Preserve them closely.

## Capture conditions

- **Viewport 909px wide**, desktop layout. This file said 1280px until session D1
  measured it: Home's content column is `max-width: 820px` centred, and its left
  edge sits at x=44 in every golden, which is exactly `(909 - 820) / 2`. Compare
  at 1280 and the demo launcher's card titles wrap differently — a divergence that
  is in the comparison, not in the app.
- Fonts: Instrument Sans, Newsreader, IBM Plex Mono, all loaded.
- Both themes captured for every state.

**Regenerated in session T against the deployed build** (2026-08-17, through the
CloudFront distribution, real scenario runs). The set predated every session-T
surface — citations, the documents strip, the observation panel, the trust
numbers, the stack section — and was a stale reference for anyone comparing.
Two changes to the medium, both deliberate:

- **They are true PNGs now.** The original set was JPEGs under a `.png`
  extension (session D1's finding, preserved below for history). Still 909x540,
  still cropped to the fold, still eyeball references rather than pixel goldens
  — the states carry live-run data (timestamps, run ids), so a pixel diff
  against them measures the data, not the design.
- **`case-detail-light.png` / `case-detail-dark.png` are new.** The case page is
  the centrepiece after session T and previously had no golden at all.

**Session U did not regenerate them, and that is a measurement rather than an
oversight.** Session U made the dashboard fill the first screen — at 1280x737 its
content went from ending at 412px (56% of the viewport) to 607px (82%). None of
that is visible here: the fill is `min-height` plus an `auto` top margin on the
closing row, so it only engages when the viewport is *taller* than the content,
and at the 540px reference height there is no slack to distribute. The two home
goldens re-captured against the new build came back pixel-identical to the
committed ones apart from the time-of-day greeting. Regenerating would have
committed a spurious diff and made the reference less stable, not more.

Two things follow for whoever compares next. These goldens do **not** show the
viewport composition, because 540px is below the height at which it changes —
compare at a real desktop height for that. And `e2e/tests/capture-goldens.spec.ts`
now holds the capture conditions as runnable code, skipped unless
`CARELOOP_CAPTURE=1`. It waits for the decision card to settle before shooting:
the card rises over 420ms, and the first run of that tool caught it
half-transparent, which is a worse reference than the one it would have replaced.

## Files

| File | State |
|---|---|
| `demo-light.png` / `demo-dark.png` | Scenario launcher, three cards |
| `home-idle-light.png` / `home-idle-dark.png` | "Nothing needs you right now." — fresh world |
| `home-needs-you-light.png` / `home-needs-you-dark.png` | Scenario 03 paused at the decision |
| `case-detail-light.png` / `case-detail-dark.png` | Completed referral: outcome, observation badge, spine with citation chips |
| `trust-light.png` / `trust-dark.png` | Authority model, guarantees, measured security evidence |
| `under-the-hood-light.png` / `under-the-hood-dark.png` | Drawer open over the dashboard, stack named |

`original-prototype.dc.html` is the approved design-environment prototype. It is
reference only — not part of the build, not to be shipped.

## Measurements that matter

- **Navbar:** 69px tall (18px padding, 23px mark). Logo lockup left, nav right.
  Active nav item is full `--foreground` with a 1px `--brand-forest` underline at
  0.75 opacity, inset 15% on each side, 9px below the text baseline box.
  Inactive items are `--muted-foreground`. One 1px × 20px separator sits between
  the nav links and the utilities (theme toggle, "Maya") — nowhere else.
- **Page column:** 820px max, centered, 40px gutters. Demo launcher: 880px.
- **Vertical rhythm:** 52px between top-level sections on Home and Trust; 44px on
  Case detail; 14–18px inside a section.
- **Hero:** greeting 14.5px muted, serif headline 46px/300 capped at 20ch,
  supporting line 16px muted. Three lines, no stat block.
- **Needs you:** copper section label and rule; card border at 38% tone, fill at
  5%. This is the only tinted surface on the dashboard.
- **Working / Completed:** rows, not cards. 1px hairline between them, 17px
  vertical padding, a 10px status mark on the left, timestamp right, tabular.
- **Case spine:** 1px vertical hairline at x=5px; 11px event dots, filled only
  when the event carries a tone; 44px monospace time column.
- **Seal:** 78px ring, drawn over 900ms, then the check.
- **Drawers:** 430px (hood) and 460px (evidence), full height, 1px left border,
  one soft shadow, sticky header.

## Responsive intent

- Desktop is the target — judge demo and 16:9 screen recording.
- The header wraps rather than compresses; brand text is not hidden prematurely.
- Content columns are fluid below 820px; page gutters stay at 40px.
- Drawers cap at 92–94vw so they remain usable on tablet.
- **Known limitation:** mobile below ~480px has not been designed in detail.
  Sections stack correctly and nothing overflows, but the header nav becomes
  tight. If mobile matters, design a mobile pattern — do not compress desktop.

## Light and dark

Light is canonical. Dark is deliberate: forest lightens to sage, copper warms,
nothing glows, no white halos, no raster fringe (the mark is vector). Muted text
is mixed at 76% in light and 62% in dark; both were measured for WCAG AA at 14px.

## Logo sizing

16px favicon · 20px compact · 23px navbar · 56px lockup · 128px+ brand surfaces.
The geometry is locked. Fix optical problems with geometry, never with shadows,
outlines or glow.
