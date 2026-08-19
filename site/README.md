# CareLoop — the judge-facing dossier

A single scrolling page that argues one thing: an agent can be genuinely useful and still be
structurally prevented from exceeding its authority. One persistent topology carries fourteen
chapters — a proposal reaches a deterministic boundary and stops, a person decides, the action
runs, the outside world is read back independently, and only then does the loop close.

It is deployed separately from the product, as a GitHub project site at
`https://<owner>.github.io/CareLoop/`. **It shares nothing with the application**: no routing, no
bundle, no backend. A change here cannot regress the deployed product, and a deploy of the product
cannot change this.

## What to edit

| Want to change | Edit | Then |
|---|---|---|
| A destination URL, or a published result | `site.toml` | `python adapt.py` |
| The page's design, copy or topology | `src/` | re-export from Claude Design, replace `vendor/dossier.bundle.html`, update `PRISTINE_SHA256`, `python adapt.py` |
| What the transform does | `adapt.py` | `python adapt.py`, then both gates |

**Never edit `index.html`.** It is generated, and it is not tracked.

**Never edit `vendor/dossier.bundle.html`.** It is the pristine Claude Design export and the
fidelity surface: its embedded manifest holds every font, capture, script and the runtime, and
`tests/check_site.py` asserts its SHA-256. Never re-minify or re-bundle it.

`docs/ADAPTATION.md` explains why the bundle is preserved rather than rebuilt, exactly what the
transform changes, and what is deliberately left broken.

## Layout

```
vendor/dossier.bundle.html   pristine Claude Design export — locked
src/                         editable source: the page, its runtime, the topology
assets/                      the three lightbox captures, the social image, the mark
adapt.py                     vendor + site.toml -> index.html
site.toml                    the one edit surface for URLs and results
index.html                   GENERATED, untracked — what Pages serves
docs/                        content map, design system, topology, build, adaptation
tests/                       the static gate, the runtime gate, the fidelity comparison
HANDOFF.md                   read before changing anything about the design
DEPLOY.md                    GitHub Pages specifics
```

Only three captures are carried loose, and `docs/ADAPTATION.md` says why the fourth is not.

## Verifying

```bash
python site/adapt.py                       # generate index.html
python site/tests/check_site.py            # static gate
cd site/tests && npm ci && npx playwright test
```

The runtime gate serves the site at `/CareLoop/` and nothing at the origin root, on purpose: a
root-absolute asset path is invisible at the origin root and 404s under a project path, which is
the failure GitHub project pages actually produce.

## Nothing is published that the repository cannot defend

Every destination and every result in `site.toml` currently ships **empty**, and that is a designed
state rather than an oversight. An empty result renders `—` with "Verified result pending"; an
empty destination renders "Deployment pending" / "Repository pending" and falls back to an in-page
anchor, so nothing is ever a dead link.

A number goes in only when the repository can produce it on demand, under a label that describes
what was actually measured. The page types every claim `MEASURED`, `ASSERTED INVARIANT` or
`COUNTERFACTUAL`, and those distinctions are load-bearing: a test count is not a safety result, and
a design intention is not a measurement. `tests/check_site.py` fails if an unpublished claim
appears in the generated page.

## Provenance

Every capture in `assets/product/` was taken from the running CareLoop build at 3× device pixel
ratio. None are mockups, and none were retouched beyond flattening onto the page background. The
data shown is the application's own synthetic demo data.

Demo uses synthetic care data. Not a medical device.
