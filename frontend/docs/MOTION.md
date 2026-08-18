# The orchestration field

A WebGL field painted behind the whole interface: sparse anchor nodes, hairline spline paths,
travelling signals and verification ripples, drawn as ink and graphite embedded into paper rather
than light on black. At rest it is nearly invisible; the margins carry structure and the reading
column does not.

**It is decoration, and nothing in CareLoop depends on it.** If WebGL is unavailable, if three.js
fails to load, or if a visitor adds `?motion=off`, the application is exactly what it was before
this layer existed — proven rather than asserted: with the field off, all eight reference states
(four routes × two themes, 909×540, cropped to the fold) render byte-identical to the build before
the integration.

## Files

| File | What |
|---|---|
| `public/careloop-scene.js` | The field. Vendored from the `careloop-motion` package, ES module, no build step |
| `public/assets/three-0.160.0.module.min.js` | three.js r160, self-hosted. See its `.LICENSE.md` |
| `index.html` | The canvas, the theme bridge, the loader and the kill switch |
| `src/styles/global.css` | The paper, and `#root`'s stacking position |
| `e2e/tests/field.spec.ts` | What the field must never cost |

The boot choreography (`careloop-boot.js`) from the same package is integrated too, and has its own
record: **[BOOT.md](BOOT.md)**. It is the field's origin state seen close up, it initialises the
field underneath itself rather than delaying it, and it is decorative on the same terms — with
`?boot=off&motion=off` the eight reference states are byte-identical to the build before either
layer existed.

### The vendored scene, and its one local delta

`public/careloop-scene.js` is byte-identical to the package's `src/careloop-scene.js` except for a
single line, marked `LOCAL DELTA` in the file: the three.js URL. Upstream it is an unpkg address;
here it is `/assets/three-0.160.0.module.min.js`. The package documents that constant as the way to
self-host. Nothing else is modified, so the next upgrade is a copy plus one line rather than a
merge.

Self-hosted for the reason already written into `index.html` about the fonts: the demo is filmed
once, and a page whose visual field depends on a CDN being reachable at that moment can lose it. It
also keeps the deployment single-origin, which is what lets `careloop/` mount no CORS middleware
anywhere.

The version is in the filename because `careloop/api/static.py` serves everything under `assets/`
as `immutable`, and that is only honest for bytes that cannot change under a name. **Upgrading
three.js means a new filename and a new `CDN` constant — never an edit in place.** No backend
change was needed for this, and none should be.

## The three contracts the application provides

### 1. The canvas, and the stacking order

```
html/body            the paper (--background)
  #careloop-field    the field, position: fixed, z-index: 0, pointer-events: none
    #root            the application, position: relative, z-index: 1
```

The canvas is **static markup in `index.html`, outside the React tree**. The module re-boots itself
whenever its canvas leaves the document, and a canvas React owned would hand that guard every
StrictMode double-mount and every re-render to interpret. Outside the tree it is also invisible to
the jsdom test suite, which never loads `index.html`.

Its positioning is an inline `style` attribute rather than a rule in `global.css`, because the
stylesheet arrives with the bundle: an unstyled canvas is a 300×150 inline block, and it would be
one at the top of the page for as long as the CSS takes to land.

`App.tsx`'s root paints `background: transparent`. The paper moved to `html, body` — same token,
same 400 ms theme cross-fade — because an opaque sheet there would hide the field completely, which
looks exactly like WebGL having failed.

### 2. `data-theme`, beside the `dark` class

The application owns the theme; the field follows it. `<html>` carries both forms — `class="dark"`
for the tokens, `data-theme="dark|light"` for the field, which watches the first `[data-theme]`
element and retones on mutation without a reload. Both are written by the blocking script in
`index.html` (so the first painted frame is right, with no flash) and kept in sync by `useTheme`.

`light` is written explicitly rather than left implied: an observer reads an attribute *value*, and
an absent attribute is not one.

### 3. `data-scene-exclude` and `data-scene-zone`

Both are bare attributes on elements that already existed. No component was restructured, no style
changed, and nothing in the application reads either of them.

**`data-scene-exclude`** marks a reading surface. The field reads the element's rect and drops its
resting density to a floor of 0.12 inside it, so the field frames the interface instead of
wallpapering it. Marked on *blocks* rather than rows: the field keeps only its six largest visible
boxes, so one `<section>` covers its cards and the gaps between them for one slot instead of many.

This is load-bearing for readability, not taste. `field.spec.ts` measures the worst contrast the
field's own pixels can produce under any text, and with the floor removed the light theme drops to
4.36:1 under "Run scenario →" and the gate turns red.

The drawers are deliberately **not** marked: they paint an opaque `--background` over the field
already, and being large they would evict real reading surfaces from the six-slot sort.

**`data-scene-zone`** names a sequence the field runs on hover and, more strongly, on click. The
field listens on `window` and never takes the event, so every button keeps its own handler, label
and focus behaviour.

| Element | Zone |
|---|---|
| Header brand button | `logo` |
| Nav "Home" / "Trust" / "Under the hood" | `home` / `trust` / `hood` |
| Scenario 01, "Schedule a cardiology referral" | `referral` |
| Scenario 02, "Malicious document" | `injection` |
| Scenario 03, "Pause for a decision" | **none** |

**Scenario 03 carries no zone, and that is the decision rather than an omission.** It is `resume`:
CareLoop stops for a decision it has no authority to take and finishes when the caregiver decides.
The package ships a `mismatch` sequence — a verification that disagrees and a loop that refuses to
close — and pointing 03 at it would animate a failure this scenario does not have. An unmapped card
gets the ambient field, which asserts nothing. A truthful `resume` sequence is a thing to author,
not to borrow. `field.spec.ts` asserts the absence directly, because a wrong mapping would still
hover, still animate and still look deliberate.

## `?motion=off`

Removes the canvas and never fetches three.js. The application renders normally; the attributes
above stay in the markup as inert data. It is the debugging lever and the fail-open switch, and it
is what the visual-regression comparison above is taken through.

Implemented in `index.html` rather than in the vendored file, so the scene keeps its single local
delta. The module is injected by that same inline script rather than written as a `<script
type="module" src="/careloop-scene.js">` tag, because Vite resolves script srcs in `index.html` as
build inputs and this one is a `public/` file that must be copied verbatim.

The scene's own fallbacks are separate and still apply: no WebGL context, or a failed fetch, hides
the canvas and logs a warning.

## Accessibility

The canvas is `aria-hidden`, not focusable, and `pointer-events: none`. `prefers-reduced-motion`
renders a static composition — no camera travel, no pointer deformation, no ambient signals.

**axe cannot judge text over a canvas, and that changed the a11y harness.** `element-has-image`
treats a canvas as an image node, so `color-contrast` returns `incomplete` for every element above
one. Measured against a canvas that was never given a drawing context: the same result, so it is a
fact about the element type rather than about anything CareLoop paints. `e2e/tests/axe.ts` therefore
hides `#careloop-field` for the duration of a scan, which keeps the contrast rule fully asserted on
the DOM; the alternative — an `Allowance` in each of twelve states — would have switched the rule's
undecided arm off across almost the whole suite. `a11y-vacuity.spec.ts` proves a real contrast
defect painted over the same canvas is still caught, and `field.spec.ts` measures the field's actual
contribution in pixels, which is the thing axe declines to judge.

## Performance

One draw call each for paths, nodes and signals; one instanced mesh for ripples. Density tiers by
viewport (92 / 64 / 38 nodes) and DPR capped at 1.75 / 1.5 / 1.25. The RAF loop stops when the tab
is hidden. Zones are re-measured every twelfth frame, which is why the count is kept small.

`window.__careLoopScene` is the live scene, for debugging.
