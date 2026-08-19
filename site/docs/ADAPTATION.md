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
pinned scrub — at risk to buy a smaller download. Measured, the transform touches **two hunks** of
the file: thirteen lines added to the stub head, and one template line rewritten. Every desktop
state is pixel-identical to the untransformed bundle across twenty captures. A rebuild could not
have offered that evidence.

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
