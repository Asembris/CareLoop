# Building

## Working on it

Serve `src/` over HTTP and open the page. It cannot run from `file://` — the
component runtime loads as a module.

```bash
cd src && python3 -m http.server 8000
# http://localhost:8000/dossier.dc.html
```

In this form, fonts come from Google Fonts and React from unpkg at runtime, so
you need network access and the page reaches two third parties. That is why this
form is for editing only and is never published. Edits are live on reload.

## Producing the deployable bundle

This is what Claude Design does on export, recorded so the artifact is
explicable. **The repository does not perform this step** — it vendors the
result as `vendor/dossier.bundle.html` and adapts it with `adapt.py`. See
`docs/ADAPTATION.md`.

The bundle was produced by inlining every external reference — scripts,
fonts, images, the runtime — into one file. Nothing is fetched at load time.

If you have no bundler to hand, the transformation is mechanical:

1. Inline `support.js` and `careloop-story.js` as `<script>` blocks, in the
   order the source loads them.
2. Fetch each Google Font `woff2`, base64 it into an `@font-face` `src:`.
3. Inline the React and ReactDOM UMD builds the runtime expects.
4. Convert every image and the SVG mark to data URIs.
5. Keep every path relative — the built file must work from a subdirectory.

Order matters for one thing: the boot script must run before the scene module,
because the loader has to paint before the scene mounts.

## Query flags

| Flag | Effect |
|---|---|
| `?boot=full` | Plays the complete boot choreography rather than the condensed one |
| `?boot=off` | Skips the loader |

Useful when recording: `?boot=full` gives the full sequence every time.

## Checks before shipping a rebuild

```
no console errors
all four captures load and are >=3x their displayed size
the lightbox opens a capture that loads, rather than an empty panel
topology renders and morphs on scroll
section 03 pins and scrubs
inspection pins release on scroll
both themes
reduced motion
no horizontal overflow at 1440, 1536, 1920, tablet, mobile
```
