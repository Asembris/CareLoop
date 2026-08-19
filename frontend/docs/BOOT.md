# The boot choreography

An SVG loader that paints the orchestration story assembling itself — structure converging, a
reasoning branch, one route verified, the loop closing — and then hands its central mark to the
header's real one. It is the origin state of the field described in [MOTION.md](MOTION.md): same
palette, same hairline grammar, same signal and ripple vocabulary.

**It is decoration, and nothing in CareLoop depends on it.** Turning it off leaves the same
application underneath, proven rather than asserted: with `?boot=off&motion=off`, all eight
reference states (four routes × two themes, 909×540, cropped to the fold) render **byte-identical**
to the pre-integration build taken through `?motion=off`. Same measurement `docs/MOTION.md` records
for the field, one layer out.

## Files

| File | What |
|---|---|
| `public/careloop-boot.js` | The loader. Vendored from the `careloop-motion` package, classic script, no build step |
| `index.html` | The script tag, and the independent reaper |
| `src/boot/ready.ts` | The one place that says CareLoop is usable |
| `src/app/App.tsx` | One call to `useApplicationReady()` |
| `e2e/tests/boot.spec.ts` | What the loader must never cost |
| `e2e/tests/base.ts` | Why every other spec never sees it |

The mark asset is untouched. `public/favicon.svg` was already byte-identical to the package's
`careloop-mark.svg`, and `CareLoopMark.tsx` is unchanged — the loader draws the same geometry from
the same numbers, which is what makes the hand-off read as one mark rather than two.

## What "ready" means

`careloop:ready` fires at **the first committed frame of the routed application, after it has
painted**. Measured against the real stack, seven cold loads of `/`: a **median of 89 ms** (min 82,
max 185).

The boundary was chosen by tracing the startup path rather than by picking a plausible-sounding
event. There is no asynchronous bootstrap: no `React.lazy`, no `Suspense`, no dynamic import, no
service worker, and `BrowserRouter` initialises synchronously. The theme is resolved *before* any of
this — the blocking script in `index.html` writes `class="dark"` and `data-theme` before the first
stylesheet, so the loader paints in the right theme on its first frame with nothing to wait for.

**The backend is excluded on purpose.** `CareLoopProvider` starts from `emptyDashboard()` and mints
its session lazily on the first request. A loader that held on until `GET /api/cases` answered would
sit over an unreachable backend for the full nine seconds — and it would hide `connecting` and the
connection banner, which are designed application states saying something true rather than loading
states standing in for one.

**Emitted from an effect in `App`, not from `main.tsx`.** A parent's effect runs after every child's,
so it is the last effect of the initial mount: after the theme is written, after the provider has
subscribed, after the matched route has mounted. `createRoot().render()` returns *before* React has
committed — the initial render is scheduled on a task — so a `requestAnimationFrame` in `main.tsx`
can and does run first. The double frame after the effect turns "committed" into "painted".

The flag that stops a second announcement is raised **at dispatch**, not when the frames are
scheduled. `StrictMode` mounts, tears down and remounts in development; a flag raised on scheduling
would be true for the second mount while the first mount's frames had already been cancelled by its
own cleanup, and nothing would ever be announced. `src/boot/ready.test.ts` has that case by name.

## Timing

| | measured |
|---|---|
| `careloop:ready` | median **89 ms** |
| application perceptible (overlay begins fading and stops taking clicks) | median **1605 ms** |
| mark landed in the header | ≈ **2280 ms** |
| overlay removed, `careloop:booted` | median **2332 ms** |

Every real load is a fast load here, because there is nothing to wait for — so these are what a
visitor experiences on every visit, not an edge case. Upstream's constants put the application on
screen at ~2.0 s and landed the mark at ~2.7 s; `CONDENSED = 1400` with a condensed arc compressed
to `CONDENSE_ARC = 1160` reaches the numbers above.

**No beat was removed to get there.** Origin, the reasoning branch with its muted arm, traversal
earning sage, the two out-of-phase verification rings, the synchronisation wave, the missing
connection drawing, the loop closing — all of them, in order, with the same causal gaps, at about
three quarters of the duration. `CONDENSE_ARC` is where the last beat lands and it is the floor on
leaving, so a slower start delays the departure rather than clipping the closure.

Slow loads settle into ambient drift instead of replaying, `?boot=full` holds for the complete
~3.9 s choreography, and reduced motion shows the finished composition and leaves.

## Run policy

| | |
|---|---|
| default | once per tab |
| `?boot=off` | skipped, and **sticky for the tab** |
| `?boot=on` | replay, and clears a sticky off |
| `?boot=full` | the complete choreography, and clears a sticky off |

Sticky because CareLoop routes on the client: `navigate("/")` drops the query, so a URL-only switch
would be undone by the next reload. A kill switch a navigation silently undoes is not a kill switch.
Once per tab because it is an entrance, and a judge reloading mid-demo has already arrived.

`sessionStorage`, not `localStorage`: a second tab is a second arrival and deserves the entrance.
Every access is guarded, and a browser that refuses storage falls back to the URL flag alone —
failing towards showing the loader rather than towards suppressing it.

### With `?motion=off`

The two flags are read by two independent blocks and share no state. All five meaningful
combinations are asserted in `boot.spec.ts`; `?boot=off&motion=off` is the visual baseline above.

## Failing open

The loader is cosmetic startup code, which is the exact category that must never become a single
point of failure. There are **two independent enforcement points**, the same shape the policy layer
uses for a related reason.

**One — the loader's own teardown.** `exit()` arms a `setTimeout` deadline *before* the try block,
so it is already running before there is anything to fail, and calls `flip()` inside it. `finalize()`
is idempotent, restores the header's mark by querying `[data-boot-hidden]` out of the DOM, removes
any `[data-boot-flier]`, removes the overlay, drops `data-booting`, clears every timer, unbinds the
pointer listener and dispatches `careloop:booted`. Restoration lives *there* rather than in a timer
inside the hand-off, which is where upstream put it — any path reaching teardown early or by way of
an exception left the brand permanently invisible under a permanently mounted overlay.

The hand-off marks and hides the header's mark **before** it touches anything else, and sets
`data-boot-hidden` before the opacity: `restoreTarget` finds what to undo by querying that
attribute, so the reverse order leaves a window in which the brand is invisible and unfindable.
Nothing visual depends on it — the whole method is one synchronous task, so no frame is painted
part-way through.

The geometry is guarded before it is trusted. Upstream divides two widths; a target that has not
been laid out gives `scale = 0` and flies the mark to nothing, and a boot svg that has not gives
`Infinity`, where a `NaN` in a transform makes the browser drop the whole declaration without
saying so. Both degrade to the same clean fade as a missing target.

**Transparent and inert in the same statement.** Upstream drops the overlay's opacity 740 ms before
removing it and never sets `pointer-events`. For those three quarters of a second the application
is fully visible and every click lands on an invisible div at z-index 9000 — which looks exactly
like a broken application and does not appear in a screenshot. The two writes are adjacent so that
no future edit can move one without the other.

**Two — the reaper in `index.html`.** Twelve lines, inline, holding **no reference to the loader**:
not the controller, not the class, not a callback. It reads the DOM the loader leaves behind and
undoes exactly that. It arms only when the loader actually mounted, which it asks by looking for
`data-booting`, and it disarms on `careloop:booted`. It covers what the loader's own teardown
structurally cannot: a file that 404s or is blocked, a build that throws after the overlay is up, a
`finalize()` a future edit breaks, an exit that dies somewhere new.

| Failure | Contained by |
|---|---|
| exception in the constructor | the bootstrap `catch`, then the reaper |
| missing hand-off target | guarded branch → clean fade; the mark is never taken |
| degenerate FLIP geometry (0, `NaN`, `Infinity`) | the same branch |
| a throw mid-hand-off, after the brand is taken | `catch` → `finalize()`, plus the deadline |
| `careloop:ready` never fires | the loader's 9 s timeout, then the reaper at 12 s |
| page hidden or backgrounded | every bound is a `setTimeout`; `requestAnimationFrame` does not run in a hidden tab |
| a teardown that cannot run at all | the reaper |

**Nothing is permanently altered.** No scroll lock — `data-booting` carries no CSS rule and stays a
pure marker. No `inert` on `#root`, deliberately: it is both the more accessible choice and one less
thing that has to be undone. No focus is moved. `boot.spec.ts` compares the post-boot DOM against
the `?boot=off` DOM — overlay count, flier count, hidden marks, `data-booting`, the header's inline
opacity, `#root`'s position and z-index, and both overflows.

## Theme

The application decides; the loader reads its decision. In practice the first branch always answers,
because `index.html`'s blocking script writes `data-theme` on `<html>` before the first stylesheet
and before this file is parsed — so a first-time visitor whose OS is dark gets a dark loader on the
very first painted frame with no branch involved. The rest exists so the loader is never the thing
that disagrees:

```
<html data-theme>  →  .dark on <html>  →  localStorage["careloop:theme"]
                   →  prefers-color-scheme  →  light
```

**The dark paper is CareLoop's, not the package's.** Upstream ships `#26241f` and its own token
table claims that is `oklch(0.205 0.008 70)`. It is not: that colour resolves to `#1a1713`, which is
what `--background` paints. Twelve levels per channel is not a rounding difference — the overlay
reads as a lighter sheet over the page, announcing itself at exactly the moment the hand-off is
meant to be seamless. Light was already within one level and is untouched.

It stays a hex literal rather than reading `var(--background)`: in development the bundle CSS is
injected by JavaScript and has not landed when the loader builds its DOM, so the property would be
unresolved and the overlay would paint transparent — a white flash, in dark mode, in the one place
this is trying to prevent one. `boot.spec.ts` resolves both colours in the browser from one layout
and requires them within a level per channel, so the literal and the token cannot drift.

## Accessibility

The overlay is `aria-hidden="true"` and carries no `role` and no `aria-live`. Upstream makes it a
live region and then changes its text five times — five polite announcements for an animation, over
an application that is *not* hidden from assistive technology. Nothing here is `inert`, nothing is
`aria-hidden` on `#root`, and no focus is moved, so a screen-reader user is already on the real,
usable CareLoop from the first committed frame; the honest thing to say about a picture they cannot
see is nothing. The same treatment `#careloop-field` gets.

Nothing in the overlay is focusable, so it can trap nothing — asserted rather than assumed, along
with focus never being inside it and `Tab` during boot reaching the application.

`prefers-reduced-motion` shows the finished composition immediately — no draw-in, no pointer
parallax, no ambient loop — and leaves promptly rather than sitting out an arc it is not playing.

**One line of copy, and it claims nothing.** Upstream cycles "Connecting context", "Preparing
trusted actions", "Verifying workspace". Not one of those describes anything happening during this
application's startup: there is no session to establish, no context to connect, nothing to verify —
the loader runs beside a React mount and nothing else. Narrating work that is not occurring is the
same small lie a decided card refuses to tell elsewhere in this product. So the copy is
**"Preparing CareLoop"**, once, and the choreography carries the orchestration story — which is what
it was drawn to do. The state machine is untouched: every state still exists, still advances, and
still reads back through `CareLoopBootController.state()`.

axe cannot judge that line — the container is `aria-hidden`, so the contrast rule skips it — which is
the right call for the scan and the reason `boot.spec.ts` measures it directly instead: **6.45:1**
light and **8.19:1** dark, both against the blend the reader actually gets.

**And it is those numbers on every frame it exists for, because it no longer fades in.** Upstream
brings the line up from zero over 620 ms and rests it at 0.88, which measured 4.86:1 light and
6.61:1 dark *at rest* — fine — while every frame of the fade was a real, visible, sub-AA
intermediate. Against the deployed build `boot.spec.ts` caught two of them, at **2.953:1** and
**3.138:1** in light. Text that is on screen and being read has to clear 4.5:1 while it is being
read, not once it settles, so the line now renders at opacity 1 with no opacity transition at all.

Nothing was done to the assertion or to how long the test waits: an accessibility test adjusted
until it passes is worth nothing, and the test was catching something real. The line still *leaves*
on a fade, in `exit()`, where it is going away rather than being read. The wordmark beside it still
fades in from zero and is untouched — it is `--foreground` at 27px and clears AA at every point of
that fade.

## Performance

No network request for the loader beyond its own file, no second WebGL renderer, no Three.js. It is
a classic script that builds its DOM synchronously, so it paints on the first frame. Seven paths,
seven node groups, two ripples and at most a handful of transient signal circles. Exactly two
`getBoundingClientRect()` calls, adjacent, before any write. After the hand-off, every node,
listener and timer is gone.

The field initialises underneath the whole time — its module is deferred and its Three.js fetch runs
concurrently with the choreography, so the ~1.5 s is warm-up it would otherwise have paid in front
of the reader. The scene never references `careloop:ready`, `careloop:booted` or `data-booting`, and
the loader never touches `#careloop-field`; `boot.spec.ts` checks that one canvas, one context and
one agreed theme survive the hand-off.

## The vendored loader, and its local deltas

`public/careloop-boot.js` is the package's `src/careloop-boot.js` with each departure marked
`LOCAL DELTA` and numbered in a block at the top of the file, so the next upgrade is a diff rather
than a merge — the same discipline `MOTION.md` records for the scene.

| # | What |
|---|---|
| 1 | Run policy: once per tab, sticky `?boot=off`, `?boot=on\|full` replay |
| 2 | Dark paper `#26241f` → `#1a1713` |
| 3 | Theme chain extended through `.dark` and `prefers-color-scheme` |
| 4 | One neutral line of copy |
| 4b | That line at full opacity, never faded in — a fade is sub-AA while it is readable |
| 5 | The overlay leaves the accessibility tree |
| 6 | Fail open: idempotent `finalize()`, guarded FLIP, hard exit timer, inert-while-fading, DOM markers |
| 7 | Condensed path tuned to this application's measured readiness |

## Testing

`e2e/tests/boot.spec.ts` — 25 tests, opting in by importing `test` from `@playwright/test` directly
so it drives unmodified URLs and sees what a visitor sees. Everything waits on a boundary the
production code already publishes; nothing waits on or asserts a duration, and the two tests that
care about ordering compare marks stamped inside the page against each other rather than against a
constant.

Every other spec imports `test` from `e2e/tests/base.ts`, which seeds the loader's own
`careloop:boot` key so the overlay never appears. Seeding storage rather than appending `?boot=off`
is deliberate: `presentation.spec.ts` asserts the address bar exactly, and changing what an existing
test asserts in order to accommodate a decoration is the thing not to do.

**Every defence has a mutant that turns it red.** Measured, against a 25-green baseline:

| Mutant | Red |
|---|---|
| `pointer-events: none` on fade removed | 2 |
| `finalize()` no longer restores the mark | 1 |
| `try`/`catch` and the hard exit deadline removed | 2 |
| FLIP geometry guard reverted to upstream | 1 |
| dark paper reverted to `#26241f` | 1 |
| `role="status"` / `aria-live` restored | 1 |
| copy reverted to an invented operational line | 1 |
| `?boot=off` no longer sticky | 1 |
| once-per-tab removed | 1 |
| the reaper's timer removed | 1 |

The two that initially survived are worth recording, because each exposed a real weakness rather
than a weak test. The restore mutant survived because the only induced failure threw *before* the
brand was taken — fixed by adding a failure that lands after it, and by taking the brand earlier so
the window is as small as it can be. The geometry mutant survived because "no residue afterwards" is
true of a hand-off that runs at scale zero too — fixed by asserting that it *declines*: no flier is
ever created and the header's mark is never taken.
