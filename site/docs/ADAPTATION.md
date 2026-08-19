# The adaptation layer

Why there is a generated `index.html`, what it is generated from, exactly what changes, and what
is deliberately left broken.

## The shape

```
vendor/dossier.bundle.html      pristine Claude Design export — the fidelity surface
        +  site.toml            the one edit surface
        ↓  adapt.py             a short list of textual adaptations
   index.html                   generated, untracked, what Pages serves
```

The bundle is a self-extracting document: a base64 **manifest** carrying every font, capture,
script and the runtime; a JSON **template** carrying the page; and a small runtime that mints blob
URLs and swaps the document. The manifest is why the page renders and animates the way it does.

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
pinned scrub — at risk to buy a smaller download. Measured, the transform changes **six lines** of
the template and adds thirteen to the head, and the rendered result is pixel-identical across
twenty captured states. A rebuild could not have offered that evidence.

The cost is honest and unpaid: `index.html` is 1.83 MiB, of which about 878 KB is four product
captures inlined into the critical path, and their `loading="lazy"` cannot help in that form. For a
page a judge opens once, on a host that gzips, that was ranked below fidelity.

## Exactly what changes

Measured against the vendored bundle. Two hunks in the file: thirteen lines inserted into the stub
head, and one template line rewritten.

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

## What is deliberately still broken

**The page overflows horizontally at 375px.** Measured identically on the pristine bundle and the
adapted page: a document 778px wide in a 375px viewport, with 54 overflowing elements.

The cause is in the design. Two chapters set their capture beside their prose with hard grid
floors — `minmax(300px, 0.78fr) minmax(400px, 1fr)` and `minmax(260px, 0.56fr) minmax(420px, 1fr)`,
about 756px before the gap — and the `max-width: 720px` media query adjusts padding and section
height without ever collapsing them to one column.

It is **not** repaired here. Fixing it means changing the layout, which is outside the adaptations
this layer is allowed to make, and doing it by injecting CSS around a locked artifact is precisely
the "compensate by editing the runtime" move this approach exists to refuse. It belongs at source,
in `src/dossier.dc.html`, followed by a Claude Design re-export and a replaced vendored bundle.

Note that `docs/BUILD.md`'s pre-ship checklist claims no horizontal overflow at mobile. That claim
was untrue when the handoff was written.

Until it is fixed, `tests/dossier.spec.ts` holds a ratchet rather than the goal: the artifact may
not get worse than the figure measured on the pristine bundle. When the design is fixed, tighten it
to the same bar the desktop test already holds.

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
