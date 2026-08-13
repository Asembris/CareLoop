# CareLoop Design System

Exact values from the approved build. Do not approximate them.

## Principle

Roughly 90% of any screen is paper, ink and hairline rules. Color appears only
when it carries state, so a single copper dot in a quiet field is unmissable
without being alarming. State is never encoded by color alone — every state also
carries a word and a distinct mark shape (rotating arc = working, closed ring =
verified, dashed offshoot = blocked).

## Color

Defined in `src/styles/tokens.css`. Values are oklch; keep them as written.

### Light (`:root`, canonical)

| Token | Value | Use |
|---|---|---|
| `--background` | `oklch(0.968 0.007 85)` | Page and header |
| `--foreground` | `oklch(0.26 0.012 65)` | Primary text, solid buttons |
| `--muted-foreground` | `color-mix(in oklab, var(--foreground) 76%, var(--background))` | Secondary text, inactive nav |
| `--border` | `color-mix(in oklab, var(--foreground) 12%, transparent)` | Hairlines |
| `--brand-forest` | `#315e52` | Mark (caregiver), active nav underline |
| `--brand-sage` | `#a6b09d` | Mark (cared-for) |
| `--brand-accent` | `oklch(0.4 0.045 162)` | Working state, past run stages |
| `--brand-copper` / `--state-decision` | `oklch(0.58 0.105 52)` | Needs you, evidence highlight |
| `--state-verified` | `oklch(0.5 0.08 155)` | Completed, seal, guarantees |
| `--state-denied` | `oklch(0.52 0.12 30)` | Blocked actions, mismatch |
| `--state-untrusted` | `oklch(0.5 0.06 320)` | Document content, model proposals |
| `--state-controlled` | `oklch(0.48 0.055 250)` | Validated facts, policy |

### Dark (`.dark`, deliberate — not an inversion)

| Token | Value |
|---|---|
| `--background` | `oklch(0.205 0.008 70)` |
| `--foreground` | `oklch(0.92 0.006 85)` |
| `--muted-foreground` | mix at **62%** (not 76%) |
| `--brand-forest` | `#91b4a7` |
| `--brand-sage` | `#c7cfbe` |
| `--brand-accent` | `oklch(0.74 0.055 162)` |
| `--brand-copper` | `oklch(0.78 0.09 56)` |
| `--state-verified` | `oklch(0.78 0.08 158)` |
| `--state-denied` | `oklch(0.7 0.12 30)` |
| `--state-untrusted` | `oklch(0.74 0.06 320)` |
| `--state-controlled` | `oklch(0.74 0.05 250)` |

Trust tiers (untrusted / controlled / verified) share one chroma and lightness
and differ only in hue, so they read as one family rather than a traffic light.

## Typography

Three voices, loaded from Google Fonts in `index.html`. If you self-host, keep
the same families — do not substitute Times New Roman for the serif.

| Voice | Family | Where |
|---|---|---|
| Editorial | Newsreader 300 | One headline per screen, nothing else |
| Interface | Instrument Sans 400/500/600 | All UI, including the wordmark |
| Technical | IBM Plex Mono 400/500 | Section labels, timestamps, IDs, rule codes |

Scale as built:

| Element | Size / weight | Tracking |
|---|---|---|
| Demo headline | 52 / 300 serif, lh 1.08 | -0.02em |
| Home headline | 46 / 300 serif, lh 1.1, max 20ch | -0.02em |
| Trust headline | 44 / 300 serif, lh 1.12 | -0.02em |
| Case title | 40 / 300 serif | -0.02em |
| Decision title | 21 / 500 sans | -0.012em |
| Outcome title | 20 / 500 sans | -0.012em |
| Scenario title | 17 / 500 sans | -0.01em |
| Case row title | 16.5 / 500 sans | — |
| Body | 15–17 / 400, lh 1.5–1.65 | — |
| Wordmark | 15 / 600 sans | -0.025em |
| Nav link | 14 / 400 sans | — |
| Section label | 11 mono, uppercase | 0.12em |
| Field label | 10.5 mono, uppercase | 0.08em |

Timestamps, durations and read-back values use `font-variant-numeric: tabular-nums`.
Mono never appears in caregiver-facing sentences — only labels, IDs and codes.

## Spacing and layout

- Page column: `--page-max: 820px` (Demo launcher: 880px), centered.
- Page gutter: 40px. Header padding: 18px 40px → 69px tall.
- Page top padding: Home/Trust 68px, Case 52px, Demo 96px. Bottom 120–130px.
- Section gap on Home/Trust: 52px. Within a section: 14–18px.
- Card padding: decision 28px, outcome 30/32px, scenario 24/22px.
- Row padding: 17px 8px 17px 4px, separated by a 1px hairline (no card chrome).
- Radius: 5px cards, 3px controls, 4px small panels. Nothing larger.
- Drawers: Under the hood 430px, Evidence 460px, both capped at ~92vw.

## Motion

Four durations, one easing family (`cubic-bezier(0.22,0.61,0.36,1)`):

| Duration | Meaning | Where |
|---|---|---|
| 120–160ms | Response | Hover, focus, nav color |
| 220–380ms | Change | Rows entering (`clRise`), page fade |
| 300–420ms | Consequence | Drawers (`clSlide`), decision card entering |
| 900ms | Closure | `clDraw` + `clCheck` — the verification seal, once per case |

Working indicator: 2.4s linear rotation, low contrast. Transform and opacity
only. `prefers-reduced-motion` collapses everything to ~0ms in one global rule.

## Components

Inline styles over tokens. The only class-based CSS is
`src/styles/components.css`, which holds hover states (`:hover` cannot be
expressed inline). Do not introduce a CSS framework or a component library —
the restraint is the design.
