# The adaptation layer

Why there is a generated `index.html`, what it is generated from, exactly what changes, and what
is deliberately left alone.

## The shape

```
vendor/dossier.bundle.html      pristine Claude Design export — the fidelity surface
        +  site.toml            destinations and results
        +  src/dossier.dc.html  the mobile stylesheet, and only that
        ↓  adapt.py             a short list of textual adaptations
   index.html                   generated, untracked, what Pages serves
```

The bundle is a self-extracting document: a base64 **manifest** carrying every font, capture,
script and the runtime; a JSON **template** carrying the page; and a small runtime that mints blob
URLs and swaps the document. The manifest is why the page renders and animates the way it does.

`src/` is a second, deliberately narrow input: `adapt.py` reads exactly one CSS block out of it
(see §6) and nothing else.

**`adapt.py` never opens the manifest.** It hashes it before and after and refuses to write if a
single byte moved. That is the invariant the whole arrangement exists to hold, and
`tests/check_site.py` asserts it independently against the file on disk.

## Why the bundle was preserved rather than re-platformed

The obvious alternative was to rebuild the page from `src/` into loose assets — smaller first load,
a build the repository owns, no authoring-tool runtime. It was rejected, and the reason is that
every problem re-platforming would have solved turned out to be reachable without it: the values
that needed changing are plain literals in the template, and the metadata that needed adding goes
in a stub head the bundle never touches.

Re-platforming would have put the page's rendering — its type, its layout, its damped topology, its
pinned scrub — at risk to buy a smaller download. Measured on import, the transform touched **two
hunks** of the file — thirteen lines added to the stub head and one template line rewritten — and
every desktop state was pixel-identical to the untransformed bundle across twenty captures. A
rebuild could not have offered that evidence.

**Both numbers are historical, and this page said them in the present tense until session W.** The
transform has since grown the mobile stylesheet (session V) and twenty-seven copy corrections
(session W), so it now touches far more than two hunks and the page deliberately no longer renders
identically to the bundle. What the argument above rests on is unchanged: the *rendering* — type,
layout, damped topology, pinned scrub — is still the bundle's own, because nothing in the transform
opens the manifest and `adapt.py` refuses to write if a byte of it moves. Correcting a sentence is
not re-platforming. See `tests/fidelity.mjs`, which now names the three expected difference
classes rather than claiming there is one.

The cost is honest and unpaid: `index.html` is 1.83 MiB, of which about 878 KB is four product
captures inlined into the critical path, and their `loading="lazy"` cannot help in that form. For a
page a judge opens once, on a host that gzips, that was ranked below fidelity.

## Exactly what changes

Measured against the vendored bundle. Two hunks in the file: thirteen lines inserted into the stub
head, and one template line rewritten — the template is a single long line, so its 22 changed
lines all sit inside that one rewrite.

Only §6 is intended to change what anything looks like, and only below 721px.

### 1. Crawler-visible metadata (stub head)

Every `og:`, `twitter:`, description and favicon tag in this page lives in the template's
`<helmet>` and reaches the DOM **only after JavaScript swaps the document**. What the server sends
is a stub head carrying a charset and a title. A link-preview crawler that does not run scripts
therefore receives no description, no image and no card type.

So the stub head gains a real set of tags. The swap discards them and the hydrated page installs
its own; both come from the same `site.toml` values, so they cannot disagree.

`pages_base_url` is empty until the site is deployed. While it is, `canonical` and `og:url` are
omitted and the social image stays page-relative — correct for a browser, and honest, because it
claims no address the site does not yet have. Set it after the first deploy and the same tags
become absolute.

### 2. The social image

`og:image` and `twitter:image` carried `product/authority.png`, a path in neither the deployed tree
nor the bundle. Repointed at `assets/product/authority.png`.

### 3. The product lightbox — a real defect, repaired

`SHOTS` holds three CSS background URLs as relative paths the bundler did not recognise as
resources and therefore did not inline. Measured against the pristine bundle before any change:

```
GET /CareLoop/product/calm.png  ->  404
```

The lightbox opened as an empty paper-coloured panel, and the page logged a console error. This is
a defect in the handoff under *any* hosting path, not something the project subpath introduced.
Repointed at `assets/product/`, and `tests/dossier.spec.ts` asserts the background both resolves
under the project path and returns 200 — because a correct-looking URL that 404s looks identical
on screen.

**This is why exactly three captures are carried loose.** `calm`, `authority` and `verification`
are lightbox targets; `authority` is also the social image. The fourth, `execution.png`, is the
demo-film poster, which is an `<img>` the bundler *did* inline — it needs no loose copy and has
none.

### 4. Dead preconnects

The bundle inlines every font and fetches nothing, but `<link rel="preconnect">` hints to
`fonts.googleapis.com` and `fonts.gstatic.com` survived bundling. A page that claims to reach no
third party should not open connections to one. Removed.

### 5. Destinations and results

The page already carries a `PROJECT` object and an `EVIDENCE` array. `adapt.py` fills them from
`site.toml` — and leaves an empty value byte-for-byte alone, so the dossier's pending states are
preserved rather than rewritten to the same thing.

**A destination has to be written in two places, and the second is the one that decides.** The page
resolves each one as:

```js
const live = P.liveUrl ?? PROJECT.liveUrl;
```

Props first, `PROJECT` as fallback. The dc-runtime builds `P` from the `data-props` attribute, whose
declared default is the empty string — and `??` falls back only on `null` or `undefined`. An empty
string is neither, so the empty prop wins and `PROJECT` is never read.

Filling `PROJECT` alone is therefore inert, which was measured rather than reasoned about: with both
URLs sitting in `PROJECT`, all seven call-to-action anchors still rendered "Deployment pending" and
"Repository pending" and still pointed at in-page anchors. `adapt.py` now sets the prop default as
well and keeps `PROJECT` in step with it, and `tests/check_site.py` asserts both, so a URL that
reaches only one of them fails the gate instead of quietly rendering as pending.

Empty stays empty in both places, which is what keeps the absent video and Devpost destinations
correct: an empty `video_url` leaves the demo panel a same-page jump to the demo chapter and hides
"Play film", and `devpost_url` is read by no markup at all.

### 6. The mobile stylesheet, carried from `src/`

The handoff overflowed horizontally on a phone: **778px of document in a 375px viewport**, with 54
overflowing elements, measured identically on the pristine bundle. Four layouts sized their columns
with hard pixel minimums — the widest reserving 700px before its 56px gap — and the
`max-width: 720px` media query adjusted padding and section height without ever collapsing them.

The fix is one rule, made at source in `src/dossier.dc.html`:

```css
main [style*='grid-template-columns: minmax('] { grid-template-columns: 1fr !important; }
```

It matches on `minmax(` coming first, which is exactly what the seven other grids do not do:
`repeat(auto-fit, minmax(...))` and `repeat(3, minmax(0, 1fr))` already collapse on their own and
are left alone. The columns live in inline styles, so `!important` is required — the same thing the
three rules already in that block do.

**`adapt.py` carries the block from `src/` into the bundle**, because the bundle cannot be rebuilt
from source without Claude Design and a responsive fix that stayed in `src/` would never reach the
page anybody loads. The sync is scoped to that one `@media` block and matches braces by depth, so
an unrelated edit to `src/` cannot ride along and a nested rule cannot truncate the match. This is
the only path by which the editable source reaches the deployed page without a re-export.

Measured after the change: every width from **320 to 720px** has `scrollWidth == innerWidth`, and
all ten desktop states remain **pixel-identical** to the previous output.

**A narrower band at 721–799px still overflows**, at an unchanged 796px, exactly as it did before.
Those grids need roughly 756px plus padding and the breakpoint sits at 720, so the only cure is
moving the breakpoint — which would change the tablet layout this change was required to preserve
exactly. It is left alone deliberately, and it is the one piece of this defect still open.

Note that `docs/BUILD.md`'s pre-ship checklist claimed no horizontal overflow at mobile. That claim
was untrue when the handoff was written; it is true now below 721px.

### 7. Source receipts, prepared and switched off

Every architecture row and every proof-ledger row can name the repository file that backs it. The
mapping is written, and validated against the working tree on every run — but
`source_receipts.enabled` is `false`, so **`adapt.py` emits nothing**.

That is not caution for its own sake. The repository is private, so a receipt rendered today would
be a permalink a judge cannot open, and a 404 sitting under a claim is worse than the page's own
honest "Source pending". While the switch is off the generated page is byte-identical to one built
with the entire `[source_receipts]` section deleted, which `tests/check_site.py` asserts directly
rather than inferring.

Turning it on is two values — `enabled = true` and `sha` — and touches no markup.

**`sha` must be a pinned commit, and arming without one raises.** `blob/main` moves; a receipt that
drifts off the line it was citing is a false citation carrying a working link, which is worse than
either failure alone. Directories pin as `tree/<sha>/…` and files as `blob/<sha>/…`, because GitHub
does not serve a directory under `blob` and emitting one form for both would put a 404 behind a
receipt.

The two kinds of receipt render differently, because the design gives them different markup:

| Receipt | Renders as | Why |
|---|---|---|
| Proof-ledger rows | a clickable pinned permalink | `EVIDENCE.sourceUrl` is already drawn as a link |
| Architecture rows | a repository path, as text | the inspector's `SOURCE` row is drawn as text |

Making the architecture receipts clickable would mean changing that row's markup, which this layer
does not do. The paths resolve under the pinned commit either way.

Two ledger rows are deliberately left unmapped. `injection` and `e2e` carry labels that do not yet
describe what the repository measures, and a receipt under a wrong label lends the label
credibility it has not earned. They stay pending until the label is fixed.

The unauthorized-action evidence is split across two rows — `docs/security-evaluation.md` for the
method and its limits, `evals/replay.py` for the harness that produces it — because each ledger row
carries exactly one link, and the document and the harness are different artifacts.

## Re-exporting from Claude Design

1. Edit `src/dossier.dc.html` (or `careloop-story.js`).
2. Re-export the bundle from Claude Design.
3. Replace `vendor/dossier.bundle.html` with it, and update `PRISTINE_SHA256` in
   `tests/check_site.py` in the same commit — the hash is there to make an accidental edit fail,
   and a deliberate re-export is not an accident.
4. Run `python adapt.py`. Every substitution asserts its expected occurrence count, so a literal
   that moved stops the build instead of silently emitting a page that lost its metadata.
5. Re-run both gates, and the fidelity comparison against the new bundle.

## Verifying

```bash
python site/adapt.py                       # generate index.html
python site/tests/check_site.py            # static gate
cd site/tests && npx playwright test       # runtime gate, served at /CareLoop/

# visual fidelity, pristine vs adapted, 20 states across two viewports
python ../../site/tests/serve.py --port 8802 --root <pristine-extract> &
python ../../site/tests/serve.py --port 8803 &
node fidelity.mjs --out <dir> && python compare_shots.py <dir>
```

Recorded on import: geometry identical at 1440 and 375, twenty of twenty states pixel-identical
with the animated field hidden, and the adapted page free of the console error and 404 the pristine
bundle produces.

**Re-measured 2026-08-19, after the mobile fix and the copy corrections**, and it no longer comes
back clean — by design. Mobile `scrollWidth` is 375 against the bundle's 778, which is the overflow
fix working; desktop `docHeight` is 19275 against 19094, which is longer copy wrapping. The first
four section boxes are identical in both builds. A clean run is not the bar any more and
`fidelity.mjs` says so at the top of the file.
