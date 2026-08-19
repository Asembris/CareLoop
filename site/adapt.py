"""Adapt the pristine Claude Design dossier bundle for GitHub Pages project-path hosting.

Reads ``vendor/dossier.bundle.html`` and ``site.toml``; writes ``index.html``.

**This is a text transform around a proven artifact, not a build.** The bundle is a self-extracting
document: a base64 manifest holding every font, script and image, a JSON template holding the page,
and a runtime that mints blob URLs and swaps the document. The manifest is why the page renders and
animates the way it does, and this script never opens it — it hashes it before and after and
refuses to write if a single byte moved.

Five problems are solved here, every one of them outside the manifest:

1. **A crawler sees no metadata.** Every ``og:`` and ``twitter:`` tag, the description and the
   favicon live in the template's ``<helmet>`` and reach the DOM only after JavaScript swaps the
   document. What the server actually sends is a stub head carrying a charset and a title. So the
   stub head gains real tags, which a crawler reads and the swap then discards — the hydrated page
   keeps its own, and both are populated from the same ``site.toml`` values so they cannot disagree.

2. **The social image 404s.** ``og:image`` and ``twitter:image`` point at ``product/authority.png``,
   a path that exists in neither the deployed tree nor the bundle.

3. **The product lightbox 404s, and this one is visible.** ``SHOTS`` holds three CSS background
   URLs — ``product/calm.png``, ``product/authority.png``, ``product/verification.png`` — as
   relative paths the bundler did not recognise as resources and therefore did not inline. Measured
   against the pristine bundle before any change here: clicking a capture issues
   ``GET /CareLoop/product/calm.png -> 404`` and the lightbox opens as an empty panel. That is a
   defect in the handoff under *any* hosting path, not something the project subpath introduced.

   This is why exactly three captures are carried loose under ``assets/product/``. The fourth,
   ``execution.png``, is the demo-film poster, which is an ``<img>`` the bundler *did* inline — it
   needs no loose copy and gets none.

4. **Two dead preconnects.** The bundle inlines its fonts and fetches nothing, but the hints to
   ``fonts.googleapis.com`` and ``fonts.gstatic.com`` survived bundling. A page that claims to reach
   no third party should not open connections to one.

5. **Destinations and results are empty literals.** The page already carries a ``PROJECT`` object
   and an ``EVIDENCE`` array; this fills them from one file rather than from four places.

Every replacement asserts its expected occurrence count and raises ``AdaptationError`` otherwise.
That is deliberate: if the dossier is re-exported from Claude Design and a literal moves, this must
stop rather than silently emit a page that quietly lost its metadata or its lightbox.

Substitution happens on the raw text, against the backslash-escaped forms the literals take inside
the template's JSON string. Decoding the JSON and re-encoding it is the obvious alternative and is
rejected on purpose: re-encoding rewrites escaping the bundler chose, and the template carries
markup whose escaping is what stops it closing its own ``<script>`` element.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
import tomllib
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).resolve().parent
VENDOR_BUNDLE: Final = HERE / "vendor" / "dossier.bundle.html"
CONFIG_FILE: Final = HERE / "site.toml"
OUTPUT: Final = HERE / "index.html"

#: The manifest is the fidelity surface: every font, every capture, React, the runtime and the
#: topology module. Matched non-greedily so a later ``</script>`` cannot extend the span.
MANIFEST_RE: Final = re.compile(r'<script type="__bundler/manifest">(.*?)</script>', re.DOTALL)

#: Font-CDN hints the bundle made unnecessary by inlining every face. The trailing ``\n`` is the
#: template's own escaped newline, removed with the tag so no blank line is left behind.
PRECONNECT_LINKS: Final = (
    r"<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">" + "\\n",
    r"<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin=\"\">" + "\\n",
)

#: The two social-image tags, replaced as whole tags. The bare path is *not* used as the needle:
#: it occurs three times, and the third is the lightbox, which needs a different destination.
SOCIAL_IMAGE_TAGS: Final = (
    r"<meta property=\"og:image\" content=\"product/authority.png\">",
    r"<meta name=\"twitter:image\" content=\"product/authority.png\">",
)

#: The lightbox's three CSS background sources.
LIGHTBOX_SHOTS: Final = ("calm", "authority", "verification")

#: Evidence entries, by the id the page's EVIDENCE array gives them.
EVIDENCE_IDS: Final = ("unauthorized", "injection", "mutation", "readback", "e2e", "tests")

#: The two counterfactual rows, keyed by the name rendered beside them. Their pristine value is an
#: em dash, which is the page's own pending state and is what an empty config leaves in place.
COUNTERFACTUAL_ROWS: Final = {"careloop": "CARELOOP", "boundary_removed": "BOUNDARY REMOVED"}
EM_DASH: Final = "—"


class AdaptationError(RuntimeError):
    """A transform did not find what it expected, or the manifest moved."""


def manifest_digest(text: str) -> str:
    """SHA-256 of the embedded manifest payload — the locked fidelity surface."""
    found = MANIFEST_RE.search(text)
    if found is None:
        raise AdaptationError("no __bundler/manifest block: this is not the dossier bundle")
    return hashlib.sha256(found.group(1).encode("utf-8")).hexdigest()


def _replace_exactly_once(text: str, old: str, new: str, what: str) -> str:
    seen = text.count(old)
    if seen != 1:
        raise AdaptationError(f"{what}: expected exactly 1 occurrence, found {seen}")
    return text.replace(old, new, 1)


def _strip_dead_preconnects(text: str) -> str:
    """Drop the two font-CDN hints the bundle inlined its fonts to make unnecessary."""
    for link in PRECONNECT_LINKS:
        text = _replace_exactly_once(text, link, "", "preconnect hint")
    return text


def _repoint_social_image(text: str, image_url: str) -> str:
    """Point og:image and twitter:image at a file that exists."""
    for tag in SOCIAL_IMAGE_TAGS:
        text = _replace_exactly_once(
            text,
            tag,
            tag.replace(r"product/authority.png", image_url),
            "social image tag",
        )
    return text


def _repoint_lightbox_sources(text: str, prefix: str) -> str:
    """Point the three lightbox backgrounds at the loose captures.

    Kept page-relative rather than absolute: this is a runtime fetch by the visitor's own browser,
    where a relative path is both correct and cacheable, and an absolute one would send a reader of
    a local preview to the deployed host.
    """
    for shot in LIGHTBOX_SHOTS:
        text = _replace_exactly_once(
            text,
            rf"src: \"product/{shot}.png\"",
            rf"src: \"{prefix}{shot}.png\"",
            f"lightbox source {shot}",
        )
    return text


def _fill_project_config(text: str, destinations: dict[str, str]) -> str:
    """Fill the page's own PROJECT object.

    An empty value is left exactly as it was rather than rewritten to the same thing, so the
    dossier's pending states survive byte for byte.
    """
    for key, prop in (
        ("live_url", "liveUrl"),
        ("github_url", "githubUrl"),
        ("devpost_url", "devpostUrl"),
        ("video_url", "videoUrl"),
    ):
        literal = rf"{prop}: \"\""
        seen = text.count(literal)
        if seen != 1:
            raise AdaptationError(f"PROJECT.{prop}: expected exactly 1 empty literal, found {seen}")
        value = destinations.get(key, "")
        if value:
            text = text.replace(literal, rf"{prop}: \"{value}\"", 1)
    return text


def _fill_evidence_value(text: str, eid: str, value: str) -> str:
    """Replace the empty ``value:`` belonging to one evidence entry.

    Anchored on the entry's id *and its category*, and bounded by the next entry's id, so a slot
    can never be filled with another entry's result even if the array is reordered or reformatted.

    The category is part of the anchor because ``id`` alone is not unique across the page: the
    ``FAILURES`` array reuses ``injection`` for the malicious-document failure row, which carries
    ``when``/``then`` rather than a result. Only evidence entries declare a ``category``.
    """
    anchor = rf"id: \"{eid}\", category: \""
    empty = r"value: \"\""
    start = text.find(anchor)
    if start < 0:
        raise AdaptationError(f"evidence[{eid}]: anchor not found")
    if text.find(anchor, start + 1) >= 0:
        raise AdaptationError(f"evidence[{eid}]: anchor is not unique")
    at = text.find(empty, start)
    if at < 0:
        raise AdaptationError(f"evidence[{eid}]: no empty value after its anchor")
    following = text.find(r"id: \"", start + len(anchor))
    if 0 <= following < at:
        raise AdaptationError(f"evidence[{eid}]: the next entry starts before this one's value")
    if not value:
        return text
    return text[:at] + rf"value: \"{value}\"" + text[at + len(empty) :]


def _fill_evidence(text: str, evidence: dict[str, str], counterfactual: dict[str, str]) -> str:
    """Fill the six proof-ledger results and the two counterfactual rows."""
    for eid in EVIDENCE_IDS:
        text = _fill_evidence_value(text, eid, evidence.get(eid, ""))
    for key, name in COUNTERFACTUAL_ROWS.items():
        value = counterfactual.get(key, "")
        literal = rf"name: \"{name}\", value: \"{EM_DASH}\""
        seen = text.count(literal)
        if seen != 1:
            raise AdaptationError(f"counterfactual[{key}]: expected 1 pending row, found {seen}")
        if value:
            text = text.replace(literal, rf"name: \"{name}\", value: \"{value}\"", 1)
    return text


def _crawler_head(site: dict[str, str]) -> str:
    """The tags a crawler that does not run JavaScript will actually receive.

    Absolute URLs are emitted only once ``pages_base_url`` is set. Until the site is deployed there
    is no address to canonicalise to, and inventing one would put a claim in the head that the
    deployment has not yet earned. Relative meanwhile is correct for a browser and for any crawler
    that resolves relative URLs.
    """
    base = site.get("pages_base_url", "").rstrip("/")
    image = site.get("og_image", "")
    absolute_image = f"{base}/{image}" if base and image else image

    def meta(markup: str) -> str:
        return f"  {markup}\n"

    def attr(value: str) -> str:
        return html.escape(value, quote=True)

    out = ["\n  <!-- Generated by site/adapt.py from site.toml. Do not edit index.html. -->\n"]
    out.append(meta(f'<meta name="description" content="{attr(site["description"])}">'))
    out.append(meta('<meta name="theme-color" content="#f7f4ee">'))
    if site.get("favicon"):
        out.append(meta(f'<link rel="icon" type="image/svg+xml" href="{site["favicon"]}">'))
    if base:
        out.append(meta(f'<link rel="canonical" href="{base}/">'))
        out.append(meta(f'<meta property="og:url" content="{base}/">'))
    out.append(meta('<meta property="og:type" content="website">'))
    out.append(meta(f'<meta property="og:title" content="{attr(site["og_title"])}">'))
    out.append(meta(f'<meta property="og:description" content="{attr(site["og_description"])}">'))
    if absolute_image:
        out.append(meta(f'<meta property="og:image" content="{absolute_image}">'))
    out.append(meta('<meta name="twitter:card" content="summary_large_image">'))
    out.append(meta(f'<meta name="twitter:title" content="{attr(site["og_title"])}">'))
    out.append(meta(f'<meta name="twitter:description" content="{attr(site["og_description"])}">'))
    if absolute_image:
        out.append(meta(f'<meta name="twitter:image" content="{absolute_image}">'))
    return "".join(out)


def _inject_crawler_head(text: str, site: dict[str, str]) -> str:
    """Insert the crawler-visible tags into the stub head the server actually sends."""
    close = text.find("</head>")
    if close < 0:
        raise AdaptationError("no </head> in the bundle")
    manifest_at = text.find('<script type="__bundler/manifest">')
    if manifest_at < 0 or close > manifest_at:
        raise AdaptationError("the first </head> is not the stub head; refusing to inject")
    return text[:close] + _crawler_head(site) + text[close:]


def adapt(bundle: str, config: dict) -> str:
    """Apply every approved transform, proving the manifest never moved."""
    before = manifest_digest(bundle)

    site = config["site"]
    base = site.get("pages_base_url", "").rstrip("/")
    image = site.get("og_image", "")
    social = f"{base}/{image}" if base and image else image

    text = _strip_dead_preconnects(bundle)
    text = _repoint_social_image(text, social)
    text = _repoint_lightbox_sources(text, "assets/product/")
    text = _fill_project_config(text, config.get("destinations", {}))
    text = _fill_evidence(text, config.get("evidence", {}), config.get("counterfactual", {}))
    text = _inject_crawler_head(text, site)

    after = manifest_digest(text)
    if before != after:
        raise AdaptationError(
            f"the embedded manifest changed: {before} -> {after}. "
            "Nothing in this transform may touch it; refusing to write."
        )
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adapt the dossier bundle for GitHub Pages.")
    parser.add_argument("--bundle", type=Path, default=VENDOR_BUNDLE)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the existing output matches what this run would write, and write nothing",
    )
    args = parser.parse_args(argv)

    bundle = args.bundle.read_text(encoding="utf-8")
    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    adapted = adapt(bundle, config)

    if args.check:
        if not args.out.exists():
            print(f"{args.out} does not exist; run adapt.py", file=sys.stderr)
            return 1
        if args.out.read_text(encoding="utf-8") != adapted:
            print(f"{args.out} is stale; re-run adapt.py", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0

    args.out.write_text(adapted, encoding="utf-8", newline="\n")
    print(f"wrote {args.out} ({len(adapted):,} chars)")
    print(f"manifest sha256 {manifest_digest(adapted)} (unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
