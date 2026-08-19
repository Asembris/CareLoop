# Design system

## Principle

Most of any frame is paper, ink and hairline rules. Colour appears only when it
carries state, so one copper or violet event in a quiet field is unmissable
without being alarming. State is never encoded by colour alone — every state
also carries a word and a distinct mark shape.

## Palette

All values are literal hex. Every one below meets WCAG AA against `#f7f4ee` at
the sizes used.

| Token | Value | Ratio | Meaning |
|---|---|---|---|
| Paper | `#f7f4ee` | — | Background |
| Ink | `#2e2b26` | 13.9:1 | Primary text |
| Secondary | `#5c584f` | 7.1:1 | Body copy |
| Muted | `#6b675e` | 5.13:1 | Mono labels, captions, pending states |
| Forest | `#315e52` | 6.9:1 | Brand mark |
| Sage | `#3f6b5b` | 5.52:1 | Trusted, verified, closure |
| Violet | `#695879` | 5.85:1 | Reasoning, untrusted content |
| Copper | `#9c5c2e` | 4.80:1 | Human decision, parked states |
| Terracotta | `#a15a45` | 4.6:1 | Mismatch, blocked |

Restraint is not low contrast. If you introduce a tone, measure it.

## Type

Three voices, and they do not trade places.

| Voice | Family | Where |
|---|---|---|
| Editorial | Newsreader 300 | One headline per chapter. Never body copy. |
| Interface | Instrument Sans | All UI, all body, the wordmark. |
| Technical | IBM Plex Mono | Eyebrows, IDs, rule codes, metric labels. |

Headlines are `clamp(32px, 3.4vw, 46px)`, line-height 1.1, tracking -0.022em.
Eyebrows are 11px mono at 0.14em. Body sits between 14.5 and 17px.

## Motion

One easing family: `cubic-bezier(0.22, 0.61, 0.36, 1)` for anything settling,
`cubic-bezier(0.33, 0, 0.2, 1)` for opacity. No bounce, no overshoot.

| Duration | Meaning |
|---|---|
| 120–160ms | Response — hover, focus |
| 220–380ms | Change — entering, fading |
| 300–620ms | Consequence — drawers, path draws |
| 900–1050ms | Closure — the verification ring, once |

Reaction chains are staggered by 40–80ms so topology feels physical rather than
simultaneous. Ambient intervals are irregular so no loop is identifiable.

## Layout

- Page column caps at 1060–1180px depending on chapter.
- Cinematic chapters hold viewport height; editorial and evidence sections size
  to their content. That distinction is why the page reads at 24.7vh rather
  than 28.5vh with no words cut.
- Radius 5–6px. Borders 1px at 11–22% ink. No shadows on cards.
