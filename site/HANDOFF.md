# CareLoop dossier — handoff

Read this before changing anything.

## Purpose

This site exists to make one argument legible in about four minutes: CareLoop
acts autonomously, and is structurally prevented from exceeding its authority.
Every design decision serves that. If a change makes the page prettier and the
argument weaker, it is the wrong change.

## The build

Two artifacts, and neither is edited by hand.

`vendor/dossier.bundle.html` is the deployable bundle exactly as Claude Design
produced it. It is the fidelity surface: its embedded manifest holds every font,
capture, script and the runtime, and a check asserts its SHA-256 is unchanged.
**Never edit it, and never re-minify or re-bundle it.**

`index.html` is generated from that bundle by `adapt.py`, which applies a short
list of textual adaptations — crawler-visible metadata, project-path asset paths,
and the values in `site.toml` — and refuses to write if the manifest moved by a
byte. It is not tracked. **Never edit it**; run `python adapt.py` instead.

`src/` is the editable Claude Design source. Changing the page's **design**
means editing it there, re-exporting from Claude Design, and replacing the
vendored bundle — see `docs/ADAPTATION.md` and `docs/BUILD.md`.

Changing its **copy** no longer requires that, and session W is why: a sentence
that is untrue has to be fixable without putting the page's rendering at risk.
Edit `src/`, then add the pair to `COPY_CORRECTIONS` in `adapt.py`, which applies
it to a copy of the bundle. `tests/check_site.py` asserts both halves — the new
string present in `src/`, the old one absent from the page — so the two inputs
cannot drift, and a re-export folds the corrections in and empties the table.

## Source layout

```
src/
  dossier.dc.html            the page: markup, copy, and its logic class
  careloop-story.js          the persistent topology (hand-written SVG)
  support.js                 the component runtime the page is authored against
  careloop-mark.svg          brand mark, used as favicon
  product/                   the four product captures
```

The page is a single component file. That is deliberate: the whole argument sits
in one place, and one persistent scene serves every chapter. Splitting it into
per-section components would break the continuity the design depends on.

## The topology is the point

`careloop-story.js` owns one graph — ten nodes, twelve edges — that never
changes identity. Chapters change activation, tone and signal behaviour; they do
not swap in different scenes. A visitor spends the whole page inside one system.

Three moments are literal, not decorative:

| Moment | What happens | Why it matters |
|---|---|---|
| **Authority** | A violet signal walks the reasoning edge and stops at 96.5% of it, then pulses while parked. | The gap is the argument: a proposal is not permission. |
| **Verification** | Two ultra-fine rings, 135ms out of phase; connected topology then synchronises. | The check genuinely happened. |
| **Closure** | The closure edge resolves from zero length — but only where the world agreed. In mismatch it draws to 34% and stops. | The loop does not close on the agent's own report. |

If you touch the topology, preserve those three. Everything else in the scene is
supporting structure.

## Systems you should not casually change

Each of these was built to solve a specific problem and is easy to regress:

- **Text protection.** Node and edge alpha is attenuated by measured screen-space
  distance from real text runs (`protectFade`). Only text *leaves* are measured,
  not their containers, so topology can pass through empty card regions but never
  across type. Focused elements deliberately punch through this.
- **Camera composition.** The camera side is derived from the area-weighted
  centroid of visible text, not hardcoded, so it self-corrects per section.
- **Pinned scrubbing.** Section 03 pins for several viewport heights and scrubs
  eight weighted steps. The narrative column and the field read the same
  `data-story-weights`, so they stay locked together.
- **Inspection release.** Architecture chips and failure rows pin the field.
  Scrolling the owning section out of view releases every pin, flag and focus —
  and forces an immediate scroll re-read so state resumes in the same gesture.
- **Focus ownership.** Subsystem emphasis goes through one delegated
  `data-atlas` handler. Calling `focus()` from a control that also carries
  `data-atlas` will fight it and toggle straight off.

## Copy rules

- **No invented numbers.** Unmeasured results render `—` with "Verified result
  pending". This is load-bearing for the page's credibility; an estimate here
  undoes the whole argument. Session W filled all six ledger rows and both
  counterfactual rows from measurements the repository can reproduce, and
  `tests/check_site.py` now asserts both directions — every configured value
  reaches the page, and no unconfigured result does.
- **Claims are typed.** Every proof-ledger entry is labelled `MEASURED`,
  `ASSERTED INVARIANT` or `COUNTERFACTUAL`, so a design intention never reads
  as a measurement.
- **Scope is stated, not hedged.** The limits section says plainly that the demo
  world is synthetic and external systems are simulated.
- **One CTA vocabulary:** `Open CareLoop ↗` / `Watch demo` / `View source ↗`,
  with `Deployment pending` / `Repository pending` fallbacks.

## Known placeholders

| What | Where | Replace with |
|---|---|---|
| Deployment URL | `liveUrl` prop | The running app's URL |
| Repository URL | `githubUrl` prop | The public repo |
| ~~Demo film URL~~ | `videoUrl` prop | **Filled, the submission video** |
| Devpost URL | `devpostUrl` prop | Nothing — no markup reads it; the entry is linked from README.md |
| ~~Six metric results~~ | `[evidence]` in `site.toml` | **Filled, session W** |
| Source links | Architecture detail, `SOURCE` rows | Repository file paths |

Empty URLs are handled: the CTA falls back to a relevant in-page anchor and
states that it is pending. Nothing renders as a dead link.

## Accessibility

Verified and worth keeping:

- All small text meets WCAG AA against the ivory background (≥4.5:1); the
  smallest mono labels sit at 5.13:1.
- Architecture chips carry `aria-pressed`, failure rows `aria-expanded`.
- Escape clears metric, failure and subsystem inspection together.
- `prefers-reduced-motion` stills the field and drops the pinned section to a
  normal length — meaning survives without the animation.
- The field is inline SVG, so it needs no WebGL context and has no fallback to
  lose. If it were ever removed the page would remain entirely readable; the
  "presentation can fail without product behaviour failing" row makes that
  argument in copy regardless.

## Do not

- Edit `index.html` or `vendor/dossier.bundle.html` directly.
- Add a chat interface. The absence of one is the argument.
- Add gradients, glassmorphism, drop shadows on cards, or a second colour family.
- Replace the serif, or set body copy in the display face.
- Reset the field between sections, or add a second scene.
- Turn the failure section back into a table.
- Fill any metric with a number nobody measured, or round one that was.
- Describe a capability the repository does not have. Session W removed a calendar
  integration, a claim that every booking stops for a human, and a claim that every
  effect is externally verified. A judge can falsify a sentence like those by reading
  the README two clicks away, and one that survives costs more than it buys.
