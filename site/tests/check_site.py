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
import html
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapt import COPY_CORRECTIONS, EM_DASH, _receipt_url, adapt, manifest_digest

SITE: Final = Path(__file__).resolve().parents[1]
VENDOR: Final = SITE / "vendor" / "dossier.bundle.html"
SOURCE_PAGE: Final = SITE / "src" / "dossier.dc.html"
GENERATED: Final = SITE / "index.html"
CONFIG: Final = SITE / "site.toml"

#: The bundle as it left Claude Design, recorded when it was imported from careloop-site.zip.
#: A mismatch means the vendored artifact was edited, which is the one thing it must never be.
PRISTINE_SHA256: Final = "91667270152fc76e6d46b6b19db23672b762545d4b32a76b976e1a7c717896ef"

#: The frozen executable release every source receipt pins to. Not the commit that publishes
#: this page: the cited implementation paths are identical in both, and the deployed build
#: reports this same value at /release.json, so it is the one a reader can corroborate.
PINNED_RELEASE: Final = "199f09a422c783c6d63f142f8e225ed9acdc9352"

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
    markup = head + template
    # A *fetched* reference, which is the thing that would make the page depend on a third party:
    # a script, a stylesheet or preload, an image, a CSS url() or an @import. A hyperlink is not
    # one of these, which is the distinction this check exists to make — the dossier deliberately
    # links out to the live application and the repository, and neither is a dependency.
    fetched = [
        m.group(1)
        for m in re.finditer(
            r'(?:<script[^>]+src|<link[^>]+href|<img[^>]+src)="(https?://[^"]+)"'
            r"|url\((https?://[^)]+)\)"
            r'|@import\s+"(https?://[^"]+)"',
            markup,
        )
        for m in [m]
        if (m.group(1) or m.group(2) or m.group(3))
    ]
    check("no off-origin resource is fetched", not fetched, ", ".join(fetched[:6]))
    # Everything else that merely *mentions* an origin must be a destination we chose on purpose.
    allowed = set(ALLOWED_TEXT_HOSTS) | {v for v in config["destinations"].values() if v}
    # `rstrip` takes a character set rather than a suffix, so the entity is split off explicitly:
    # stripping "&quot;," as characters would eat the trailing "t" of ".net".
    mentioned = {
        m.group(0).split("&quot;")[0].rstrip(",.")
        for m in re.finditer(r'https?://[^"\'\s)\\]+', markup)
    }
    unexpected = {u for u in mentioned if not any(u.startswith(a) for a in allowed)}
    check(
        "every off-origin mention is a configured destination",
        not unexpected,
        ", ".join(sorted(unexpected)[:6]),
    )
    check("the googleapis preconnect is gone", "fonts.googleapis.com" not in generated)
    check("the gstatic preconnect is gone", "fonts.gstatic.com" not in generated)

    print("\nnothing is root-absolute, so the project subpath resolves")
    absolute = re.findall(r'(?:src|href|content)="(/[^/"][^"]*)"', head + template)
    check("no root-absolute src/href/content", not absolute, ", ".join(absolute[:6]))

    print("\nonly what the repository can defend is published")
    # The two destinations that exist, asserted as exact strings rather than by substring: a
    # truncated or trailing-slash variant would still "contain" the host and would still be wrong.
    destinations = config["destinations"]
    for prop, key in (("liveUrl", "live_url"), ("githubUrl", "github_url")):
        expected = destinations[key]
        check(f"PROJECT.{prop} is exactly the configured URL", f'{prop}: "{expected}"' in template)
        # The prop default is the one the runtime actually reads: the page resolves
        # `P.liveUrl ?? PROJECT.liveUrl`, and `??` does not fall back on an empty string, so a
        # URL present only in PROJECT renders as "Deployment pending". Both must carry it.
        check(
            f"the {prop} prop default carries it too",
            f'"{prop}": {{"editor": "text", "default": "{expected}"' in html.unescape(template),
        )
    check(
        "the live URL is the one the repository records",
        destinations["live_url"] == "https://d2heuwvhvrdyig.cloudfront.net",
        destinations["live_url"],
    )
    check(
        "the repository URL matches the git remote",
        destinations["github_url"] == "https://github.com/Asembris/CareLoop",
        destinations["github_url"],
    )
    # The two that do not exist. No video and no Devpost entry are recorded anywhere in the
    # repository, so these stay empty and the page's own fallbacks stay in charge.
    for prop in ("devpostUrl", "videoUrl"):
        check(f"PROJECT.{prop} is still empty", f'{prop}: ""' in template)
    check("no invented video URL", "videoUrl" in template and 'videoUrl: ""' in template)

    print("\nthe page describes the product this repository actually contains")
    # Session W. Every pair in `COPY_CORRECTIONS` replaces a claim a judge could falsify by reading
    # the README two clicks away — there is no calendar integration, a trusted booking is authorised
    # autonomously, only `book_appointment` has an external read-back, and two ledger rows were
    # labelled for measurements nobody had made.
    #
    # Asserted in both directions, because each catches a different way of going wrong. The old
    # string absent from the page is the correction having *happened*; the new string present in
    # `src/` is the editable source and the generated page still agreeing, which is what stops a
    # future re-export from silently reinstating the old copy.
    for old, new in COPY_CORRECTIONS:
        check(f"the page no longer says {old[:44]!r}", old not in generated)
        check(f"src/ carries {new[:44]!r}", new in source_page)
        check(f"the page carries {new[:36]!r}", template.count(new) + head.count(new) >= 1)

    # Non-vacuity, three ways. Without these the loop above would pass against a bundle in which
    # the corrections were never applied and the old copy simply never existed.
    untransformed = _template(vendor) + _stub_head(vendor)
    check(
        "every old claim really is in the untransformed bundle",
        all(old in untransformed for old, _ in COPY_CORRECTIONS),
    )
    check(
        "and none of the replacements is",
        not any(new in untransformed for _, new in COPY_CORRECTIONS),
    )
    check("the corrections table is not empty", len(COPY_CORRECTIONS) >= 20)

    # The two encodings. The bundle carries this copy as HTML markup and again inside a
    # JSON-escaped template, so a pair needing escaping would reach only one of them. `adapt.py`
    # refuses such a pair; this asserts the table has none, so that refusal is never the thing a
    # reader discovers at build time.
    for old, new in COPY_CORRECTIONS:
        check(
            f"{old[:36]!r} needs no escaping in either encoding",
            not any(c in s for s in (old, new) for c in ('"', "\n")),
        )

    # The three specific claims the audit named, asserted by content rather than by table position
    # so that deleting a row from `COPY_CORRECTIONS` cannot quietly retire one of them.
    check("no calendar is claimed anywhere", "calendar" not in generated.lower())
    check(
        "booking is not described as always stopping for a human",
        "Booking on someone's behalf is not the agent's call" not in generated,
    )
    check(
        "the external world is scoped to the clinic",
        "The clinics, payers and portals CareLoop acts against" not in generated,
    )

    print("\nevery published result is what site.toml says, and nothing else is published")
    # **Inverted in session W, and the old shape is why.** This used to assert that all six results
    # were empty and that a fixed list of phrases had not "leaked in" — a leak detector over
    # literals frozen at the moment it was written. It kept passing after the suite grew from 2661
    # to 2696 and again to 2720, because the phrase it forbade had stopped being a phrase the
    # repository produces. A guard whose subject drifts out from under it guards nothing.
    #
    # So it is now two-directional and reads its expectations from the config: every configured
    # value appears in the page, and no *unconfigured* result-shaped string does. The second half is
    # what stops a number reaching a reader through the bundle rather than through `site.toml`.
    evidence = config["evidence"]
    counterfactual = config["counterfactual"]
    check("all six ledger results are configured", len(evidence) == 6 and all(evidence.values()))
    for eid, value in sorted(evidence.items()):
        check(f"evidence[{eid}] renders {value!r}", f'value: "{value}"' in template)
    for key, value in sorted(counterfactual.items()):
        check(f"counterfactual[{key}] renders {value!r}", f'value: "{value}"' in template)
    check("no ledger row is left pending", 'value: ""' not in template)
    check("no counterfactual row is left pending", f'value: "{EM_DASH}"' not in template)
    # Result-shaped strings the page must not carry unless `site.toml` put them there. These are
    # the ones this repository has actually published and then outgrown: three stale suite counts —
    # the last of them an exact collection figure a frontend commit falsified, which is why the row
    # is a floor now — and a lock result that is now 24 of 25.
    published = set(evidence.values()) | set(counterfactual.values())
    outgrown = ("2661 passed", "2696 passed", "103 passed", "6183 collected", "25 of 25", "0 of 25")
    for phrase in outgrown:
        if phrase not in published:
            check(f"no unconfigured result {phrase!r} is in the page", phrase not in generated)

    # Non-vacuity for the pair above: an unpublished result must be detectable by the same test.
    # Without this, "no unconfigured result is in the page" would pass just as happily against a
    # page the checker never actually searched.
    check("the search would find one if it were there", "0 of 320" in generated)

    # Receipts live inside the EVIDENCE array and nowhere else, enabled or not.
    before_evidence = template.split("EVIDENCE")[0]
    check("no source link appears before the EVIDENCE array", "sourceUrl" not in before_evidence)

    print("\nsource receipts are enabled and pinned to the submitted release")
    receipts = config.get("source_receipts", {})
    repo_root = SITE.parent
    enabled = receipts.get("enabled")
    check("the switch exists and is on", enabled is True, str(enabled))
    sha = receipts.get("sha", "")
    # Pinned to the frozen executable release, and deliberately not to the evidence commit that
    # publishes this page: the cited implementation paths are byte-identical between the two, and
    # naming the build a judge is actually running says more than naming the one that carries the
    # citation. The shape is asserted as well as the value, because a truncated or upper-case sha
    # resolves to nothing while still looking like a commit.
    check("a commit is pinned", sha == PINNED_RELEASE, sha)
    check("the pin is 40 lowercase hex", bool(re.fullmatch(r"[0-9a-f]{40}", sha)), sha)
    check(
        "the receipt repo is the real one",
        receipts.get("repo") == "https://github.com/Asembris/CareLoop",
    )

    # Every configured path must exist *and* be tracked: a file git does not carry cannot be at
    # the other end of a GitHub permalink, however present it is on this machine.
    configured: list[str] = []
    for paths in receipts.get("subsystems", {}).values():
        configured.extend(paths)
    for entry in receipts.get("evidence", {}).values():
        configured.append(entry["path"])
    for path in configured:
        target = repo_root / path
        check(f"{path} exists", target.exists())
        if target.exists():
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path],
                cwd=repo_root,
                capture_output=True,
            )
            check(f"{path} is tracked by git", tracked.returncode == 0)

    # Asserted on the page this build actually emits, rather than on an armed copy of the config.
    # Once the switch is on, the real generated page is the artifact a judge reads.
    check(
        "every ledger receipt is a link",
        template.count("sourceUrl:") == len(receipts["evidence"]),
    )
    check(
        "every subsystem receipt is a path",
        template.count("sourceReceipt:") == len(receipts["subsystems"]),
    )
    wired_row = 'v: y.sourceReceipt || "Source pending"'
    check("the SOURCE row reads the selected subsystem", wired_row in template)
    # Ledger receipts are the clickable ones, so those carry the pinned URL. Subsystem receipts
    # land in a row the design draws as text, so those resolve to repository paths -- asserted here
    # as the two different renderings they are, rather than pretending they are one.
    pinned = f"https://github.com/Asembris/CareLoop/blob/{sha}/docs/security-evaluation.md"
    check("a ledger receipt is a pinned permalink", f'sourceUrl: "{pinned}"' in template)
    rules_receipt = 'sourceReceipt: "careloop/policy/rules.yaml"'
    check("a subsystem receipt is a repository path", rules_receipt in template)
    check(
        "a paired subsystem receipt names both halves",
        'sourceReceipt: "careloop/graph/verify.py · tests/test_verify_readback.py"' in template,
    )
    # Every permalink in the page resolves against the pinned commit and nothing else. Asserted
    # over all of them rather than over the one sampled above, so a single mapping that slipped
    # back to a branch, or to some other commit, is caught here.
    permalinks = re.findall(r"github\.com/\S+?/(?:blob|tree)/([^/]+)/", template)
    check("the page carries permalinks", bool(permalinks), str(len(permalinks)))
    check(
        "every permalink names the pinned commit",
        set(permalinks) == {sha},
        str(sorted(set(permalinks))),
    )
    no_branch = "/blob/main/" not in template and "/tree/main/" not in template
    check("no receipt points at a moving branch", no_branch)
    check(
        "the receipts leave the manifest alone",
        manifest_digest(generated) == manifest_digest(vendor),
    )

    print("\nand the switch is the only thing producing them")
    # Anti-vacuity, and the property the switch exists for: with receipts off the page must be
    # byte-identical to one built with this whole section deleted, and must carry no receipt at
    # all. Asserted against a disabled copy of the live config, so the checks above are known to
    # be measuring the switch rather than something the vendored bundle always had.
    disabled = tomllib.loads(_read(CONFIG))
    disabled["source_receipts"]["enabled"] = False
    disabled["source_receipts"]["sha"] = ""
    dark = adapt(vendor, disabled, source_page)
    dark_template = _template(dark)
    check("with receipts off no receipt URL is in the page", "sourceUrl:" not in dark_template)
    check(
        "with receipts off no subsystem receipt is in the page",
        "sourceReceipt" not in dark_template,
    )
    check(
        "with receipts off no GitHub permalink is in the page",
        not re.search(r"github\.com/\S+/(?:blob|tree)/", dark_template),
    )
    check(
        "with receipts off the architecture SOURCE row reads as pending",
        '"Source pending"' in dark_template,
    )
    without = {k: v for k, v in disabled.items() if k != "source_receipts"}
    check(
        "with receipts off the page is byte-identical to having no receipt config",
        adapt(vendor, without, source_page) == dark,
    )
    check("and enabling them is what changes the page", dark != generated)

    # The blob/tree distinction is a property of the URL builder, and no evidence receipt is a
    # directory today, so it is exercised directly rather than through the page.
    check(
        "a file pins as a blob",
        _receipt_url("https://x/r", "abc", "careloop/policy/rules.yaml")
        == "https://x/r/blob/abc/careloop/policy/rules.yaml",
    )
    check(
        "a directory pins as a tree",
        _receipt_url("https://x/r", "abc", "careloop/agents/")
        == "https://x/r/tree/abc/careloop/agents",
    )
    # Arming without pinning must still fail loudly rather than emit a branch link.
    unpinned = tomllib.loads(_read(CONFIG))
    unpinned["source_receipts"]["enabled"] = True
    unpinned["source_receipts"]["sha"] = ""
    try:
        adapt(vendor, unpinned, source_page)
        check("enabling without a sha is refused", False, "it was accepted")
    except Exception as refusal:
        why = str(refusal)
        check("enabling without a sha is refused", "sha is empty" in why, why[:70])

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
