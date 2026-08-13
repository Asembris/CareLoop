# Design Reference

**When implementation and written description appear to conflict, use the
supplied screenshots and current source implementation as the visual source of
truth.**

The screenshots in `screenshots/` are golden references for the approved UI.
Preserve them closely.

## Capture conditions

- Viewport 1280px wide (the design environment's preview), desktop layout.
- Fonts: Instrument Sans, Newsreader, IBM Plex Mono, all loaded.
- Both themes captured for every state.

## Files

| File | State |
|---|---|
| `demo-light.png` / `demo-dark.png` | Scenario launcher, three cards |
| `home-idle-light.png` / `home-idle-dark.png` | "Nothing needs you right now." — Working + Completed only |
| `home-needs-you-light.png` / `home-needs-you-dark.png` | Scenario 01 paused at the decision |
| `trust-light.png` / `trust-dark.png` | Authority model, guarantees, empty security evidence |
| `under-the-hood-light.png` / `under-the-hood-dark.png` | Drawer open over the dashboard |

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
