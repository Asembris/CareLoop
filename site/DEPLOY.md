# Deploying to GitHub Pages

The dossier deploys from this repository as a **GitHub project site**, at
`https://<owner>.github.io/CareLoop/`. What is published is `site/`, and what is
served is `site/index.html`, which `adapt.py` generates from the vendored bundle.

Nothing here is hand-assembled, so the deployment procedure is: run `adapt.py`,
pass the gates, publish the directory.

## The project path is the only path

This is a project site, never a user site, so every reference must resolve under
`/CareLoop/`. The bundle uses no root-absolute paths and `adapt.py` introduces
none; `tests/check_site.py` asserts that, and `tests/dossier.spec.ts` runs the
whole gate against a server that publishes the site at `/CareLoop/` and nothing
at the origin root — so a root-absolute path fails the suite rather than being
discovered after a deploy.

If you add an asset later, keep the reference relative.

## Publishing it

**No Pages workflow exists yet, and publishing is a manual act.**
`.github/workflows/` carries CI and the demo health check and nothing that
builds or publishes this directory — so today the procedure is by hand: run
`adapt.py`, pass the two gates, and upload `site/` as the Pages artifact
(Settings → Pages → Build and deployment: source `GitHub Actions`). When the
workflow is written it should do exactly that from `main` only, with pull
requests validating without publishing — a failed gate uploads nothing, so the
previously deployed version stays exactly as it was.

Two things must be true before the first deploy, and neither is about this
directory: the repository has to be public (or on a plan that serves Pages from
a private repository), and `pages_base_url` in `site.toml` should be set once the
address is known, so the social tags become absolute.

## Serving the source instead of the bundle

You can publish `src/` directly rather than the bundle. Point Pages at a
`/docs` folder or a `gh-pages` branch containing the contents of `src/`, and
rename `dossier.dc.html` to `index.html`.

**Development only.** The source form loads fonts from Google Fonts and React
from unpkg at runtime, so it reaches two third parties, needs network access and
is slower on first paint. The bundle has everything inlined and reaches none.
Never publish the source form — see `docs/ADAPTATION.md`.

## `.nojekyll`

Included at the root. Without it, GitHub runs Jekyll, which ignores files and
folders beginning with an underscore. Nothing here currently starts with one,
but the file costs nothing and prevents a class of silent 404s.

## Custom domain

```bash
echo "careloop.example" > CNAME
git add CNAME && git commit -m "Custom domain" && git push
```

Then add the domain under Settings → Pages and create the DNS records GitHub
shows you. Enable **Enforce HTTPS** once the certificate is issued.

## Social preview

`og:image` and `twitter:image` point at the human-checkpoint capture. Two things
about that were wrong in this document and are worth stating plainly, because
both were measured rather than assumed.

It is **not** an inlined data URI. In the bundle both tags carry the relative
path `product/authority.png`, which exists in neither the deployed tree nor the
bundle — it returns 404 under any hosting path.

More importantly, **every one of these tags is injected by JavaScript**. They
live in the template's `<helmet>` and reach the DOM only after the bundle swaps
the document, so a crawler that does not run scripts receives a head carrying a
charset and a title and nothing else.

`adapt.py` fixes both: it repoints the tags at `assets/product/authority.png`,
which is carried loose for exactly this reason, and it writes a real set of tags
into the served stub head. Set `pages_base_url` in `site.toml` once the site is
deployed and the same tags become absolute, which is what a link preview needs.

## Verify after deploying

- [ ] Page paints immediately; the boot sequence completes and hands off.
- [ ] The topology renders and morphs as you scroll.
- [ ] All four product captures load and are sharp on a retina display.
- [ ] Section 03 pins and scrubs through its eight steps.
- [ ] Architecture chips and failure rows respond, and release when you scroll on.
- [ ] Both themes.
- [ ] Reduced motion: content is complete and readable, animation is stilled.
- [ ] The lightbox opens a capture that actually loads, not an empty panel.
- [ ] No console errors.
