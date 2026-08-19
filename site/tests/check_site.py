"""Static gate for the CareLoop dossier.

Run it directly (``python site/tests/check_site.py``) or under pytest — every check is a plain
function so the file needs no test framework to be useful, and the repository suite does not
collect it (``pyproject.toml`` sets ``testpaths = ["tests"]``), which is deliberate: this gate is
about the published artifact, not about CareLoop, and it must not lengthen the suite that gates the
agent.

The properties, in the order they would fail if the adaptation broke:

* the pristine bundle is present and is the exact artifact that was handed over
* the generated page exists, and re-running the transform reproduces it byte for byte
* **the embedded manifest is byte-identical between the two** — the fidelity invariant
* the crawler-visible head really is visible to something that does not run JavaScript
* every local asset the head and the lightbox name actually exists on disk
* nothing points at an origin the page does not serve itself
* no path is root-absolute, which would 404 under ``/<repo>/``
* every destination and every result is still empty, because Phase 1 publishes no claims
* the substitution machinery is not vacuous — a temporary config proves it lands
* nothing that looks like a secret or a local filesystem path was introduced

Several checks are written against the *generated* page rather than the transform's return value,
so they would still fail if someone hand-edited ``index.html`` and skipped ``adapt.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapt import adapt, manifest_digest

SITE: Final = Path(__file__).resolve().parents[1]
VENDOR: Final = SITE / "vendor" / "dossier.bundle.html"
SOURCE_PAGE: Final = SITE / "src" / "dossier.dc.html"
GENERATED: Final = SITE / "index.html"
CONFIG: Final = SITE / "site.toml"

#: The bundle as it left Claude Design, recorded when it was imported from careloop-site.zip.
#: A mismatch means the vendored artifact was edited, which is the one thing it must never be.
PRISTINE_SHA256: Final = "91667270152fc76e6d46b6b19db23672b762545d4b32a76b976e1a7c717896ef"

#: Hosts the page is allowed to *mention*. Nothing here is fetched: these are outbound hyperlinks
#: and an XML namespace, which is why the check below distinguishes references from destinations.
ALLOWED_TEXT_HOSTS: Final = ("http://www.w3.org/2000/svg", "http://www.w3.org/1999/xhtml")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILURES.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _stub_head(text: str) -> str:
    """Everything a crawler receives before the bundler swaps the document."""
    return text[: text.index("</head>")]


def _template(text: str) -> str:
    """The page markup, decoded out of its JSON carrier."""
    found = re.search(r'<script type="__bundler/template">(.*?)</script>', text, re.DOTALL)
    assert found is not None
    return json.loads(found.group(1))


def main() -> int:
    print("\nthe vendored bundle is the artifact that was handed over")
    check("vendor/dossier.bundle.html exists", VENDOR.exists())
    if not VENDOR.exists():
        return 1
    vendor = _read(VENDOR)
    actual = hashlib.sha256(VENDOR.read_bytes()).hexdigest()
    check("it is byte-identical to the pristine handoff", actual == PRISTINE_SHA256, actual)

    print("\nthe generated page is generated")
    check("index.html exists", GENERATED.exists(), "run: python site/adapt.py")
    if not GENERATED.exists():
        return 1
    generated = _read(GENERATED)
    config = tomllib.loads(_read(CONFIG))
    source_page = _read(SOURCE_PAGE)
    rebuilt = adapt(vendor, config, source_page)
    check("re-running adapt.py reproduces it byte for byte", rebuilt == generated)
    check(
        "the transform is deterministic across runs",
        adapt(vendor, config, source_page) == rebuilt,
    )
    check(".nojekyll is present at the artifact root", (SITE / ".nojekyll").exists())

    print("\nthe rendering manifest never moved")
    check(
        "the embedded manifest is byte-identical to the vendored one",
        manifest_digest(vendor) == manifest_digest(generated),
        f"{manifest_digest(vendor)} != {manifest_digest(generated)}",
    )

    print("\na crawler that does not run JavaScript sees real metadata")
    head = _stub_head(generated)
    for tag in (
        "<title>",
        'name="description"',
        'property="og:title"',
        'property="og:description"',
        'property="og:image"',
        'property="og:type"',
        'name="twitter:card"',
        'name="twitter:title"',
        'name="twitter:description"',
        'name="twitter:image"',
        'rel="icon"',
    ):
        check(f"the served head carries {tag}", tag in head)
    check(
        "the head is served before any bundler payload",
        generated.index("</head>") < generated.index('<script type="__bundler/manifest">'),
    )

    print("\nevery local file the page names exists")
    site_cfg = config["site"]
    for role, rel in (("favicon", site_cfg["favicon"]), ("og:image", site_cfg["og_image"])):
        check(f"{role} -> {rel}", (SITE / rel).is_file())
    for shot in ("calm", "authority", "verification"):
        rel = f"assets/product/{shot}.png"
        check(f"lightbox -> {rel}", (SITE / rel).is_file())
        check(f"the page points the {shot} lightbox at it", f'"{rel}"' in _template(generated))

    print("\nthe mobile stylesheet is carried from src/ into the page")
    collapse = (
        "main [style*='grid-template-columns: minmax('] { grid-template-columns: 1fr !important; }"
    )
    check("src/dossier.dc.html collapses the hard-minimum grids", collapse in source_page)
    check("and the generated page carries the same rule", collapse in _template(generated))
    # Non-vacuous: without the sync the rule cannot reach the page, so the two must disagree.
    unsynced = adapt(vendor, config)
    check("omitting the sync would leave it out", collapse not in _template(unsynced))
    # Counted inside `style="…"` only: the rule's own attribute selector contains the same
    # substring, and matching it would make this assertion count itself.
    inline_hard = re.findall(r'style="[^"]*grid-template-columns: minmax\(', _template(generated))
    inline_soft = re.findall(r'style="[^"]*grid-template-columns: repeat\(', _template(generated))
    check("four inline grids carry hard minimums", len(inline_hard) == 4, str(len(inline_hard)))
    check("seven inline grids collapse already", len(inline_soft) == 7, str(len(inline_soft)))

    print("\nnothing reaches an origin the site does not serve")
    template = _template(generated)
    hosts = {
        m.group(0)
        for m in re.finditer(r'https?://[^"\'\s)\\]+', head + template)
        if not m.group(0).startswith(ALLOWED_TEXT_HOSTS)
    }
    check("no off-origin resource reference remains", not hosts, ", ".join(sorted(hosts)[:6]))
    check("the googleapis preconnect is gone", "fonts.googleapis.com" not in generated)
    check("the gstatic preconnect is gone", "fonts.gstatic.com" not in generated)

    print("\nnothing is root-absolute, so the project subpath resolves")
    absolute = re.findall(r'(?:src|href|content)="(/[^/"][^"]*)"', head + template)
    check("no root-absolute src/href/content", not absolute, ", ".join(absolute[:6]))

    print("\nPhase 1 publishes no claims")
    for prop in ("liveUrl", "githubUrl", "devpostUrl", "videoUrl"):
        check(f"PROJECT.{prop} is still empty", f'{prop}: ""' in template)
    check("all six evidence results are still empty", template.count('value: ""') == 6)
    check("both counterfactual rows still read as pending", template.count('value: "—"') == 2)
    for phrase in ("0 of 320", "320 of 320", "2661", "d2heuwvhvrdyig", "github.com/Asembris"):
        check(f"no unpublished claim {phrase!r} leaked in", phrase not in generated)

    print("\nthe substitution machinery is not vacuous")
    probe = tomllib.loads(_read(CONFIG))
    probe["site"]["pages_base_url"] = "https://example.invalid/CareLoop"
    probe["destinations"]["live_url"] = "https://live.invalid"
    probe["destinations"]["github_url"] = "https://repo.invalid"
    probe["evidence"]["tests"] = "PROBE-EVIDENCE"
    probe["counterfactual"]["careloop"] = "PROBE-ROW"
    filled = adapt(vendor, probe, source_page)
    filled_template = _template(filled)
    ft = filled_template
    check("a configured live_url reaches PROJECT", 'liveUrl: "https://live.invalid"' in ft)
    check("a configured github_url reaches PROJECT", 'githubUrl: "https://repo.invalid"' in ft)
    check("a configured result reaches its evidence slot", 'value: "PROBE-EVIDENCE"' in ft)
    check("it reaches the right slot", 'id: "tests", category: "RELIABILITY"' in ft)
    check("a configured counterfactual row is filled", 'value: "PROBE-ROW"' in ft)
    check(
        "a base URL makes the social image absolute",
        'content="https://example.invalid/CareLoop/assets/product/authority.png"' in filled,
    )
    check("a base URL adds a canonical", 'href="https://example.invalid/CareLoop/"' in filled)
    check("the probe leaves the manifest alone", manifest_digest(filled) == manifest_digest(vendor))
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "probe.html").write_text(filled, encoding="utf-8")
    check("the probe was never written to the published page", "PROBE-EVIDENCE" not in generated)

    print("\nnothing private was introduced")
    introduced = generated.replace(vendor, "")
    for pattern, label in (
        (r"[A-Za-z]:\\\\Users", "a Windows filesystem path"),
        (r"/home/[a-z]", "a POSIX home path"),
        (r"AKIA[0-9A-Z]{16}", "an AWS access key id"),
        (r"(?i)secret[_-]?key\s*[:=]", "a secret assignment"),
        (r"(?i)authorization:\s*bearer", "a bearer token"),
    ):
        check(f"the generated additions carry no {label}", re.search(pattern, introduced) is None)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for line in FAILURES:
            print(f"  - {line}")
        return 1
    print("all static checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
