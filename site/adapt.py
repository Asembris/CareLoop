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
SOURCE_PAGE: Final = HERE / "src" / "dossier.dc.html"
CONFIG_FILE: Final = HERE / "site.toml"
OUTPUT: Final = HERE / "index.html"

#: The mobile stylesheet, carried from ``src/`` into the bundle.
#:
#: This is the one place the editable source reaches the deployed page without a Claude Design
#: re-export, and it is scoped to a single CSS block on purpose. Everything else in ``src/`` is
#: reference until the bundle is re-exported; a responsive rule is different, because leaving it
#: only in the source would mean the file that documents the fix is the one file that cannot
#: apply it. Nothing outside these braces is read, so an unrelated edit to ``src/`` cannot ride
#: along.
MOBILE_MEDIA_OPENER: Final = "@media (max-width: 720px) {"

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


#: Copy the dossier states that the repository does not support, and what it says instead.
#:
#: **Why this is a table here rather than a re-export.** The page's copy lives inside
#: ``vendor/dossier.bundle.html``, which is the fidelity surface and may not be rebuilt: its
#: embedded manifest holds every font, capture, script and the runtime, and ``check_site.py``
#: asserts its SHA-256. So a correction reaches the page the same way the mobile stylesheet does
#: — as a textual substitution applied to a copy of the bundle. ``src/dossier.dc.html`` stays the
#: editable source and carries every replacement, which ``check_site.py`` asserts in both
#: directions: the new string present in ``src/``, the old string absent from the generated page.
#:
#: **No pair may contain a double quote or a newline.** The bundle carries this copy twice — once
#: as HTML markup and once inside a JSON-escaped template — and a string free of both characters
#: is byte-identical in the two encodings, so one plain replace reaches both. A pair needing
#: escaping would silently reach only one of them, which is exactly the failure a substitution
#: table invites. ``_apply_copy_corrections`` refuses one rather than trusting this comment.
#:
#: Session W. Each of these was falsifiable by reading the repository the page links to: there is
#: no calendar integration, a trusted booking is authorised autonomously, only ``book_appointment``
#: has an external read-back, and two ledger rows were labelled for measurements nobody had made.
COPY_CORRECTIONS: Final[tuple[tuple[str, str], ...]] = (
    (
        "CareLoop read the paperwork, found the deadline, checked the calendar and found the "
        "conflict. It stops here — not because it failed, but because this choice belongs to a "
        "person.",
        "CareLoop read the paperwork, extracted the deadline and validated it against the "
        "referral. Then it reached an action that is not its to take — sending on the carer's "
        "behalf — and stopped.",
    ),
    (
        "A referral arrives. CareLoop opens a case and pulls in only the context this event needs.",
        "A referral arrives, and with it a continuity notice addressed to the agent. CareLoop "
        "opens one case and reads both.",
    ),
    (
        "The deadline is read from page 2 of the referral; the visit length from the instructions. "
        "Each fact keeps its source.",
        "The deadline and the patient id are read from the referral and validated. The notice's "
        "instructions are quoted as evidence and never become permission. Each fact keeps its "
        "source.",
    ),
    (
        "The model drafts a plan: Tuesday conflicts with Maya's calendar, Thursday at 2:00 PM is "
        "open and still meets the deadline.",
        "The model proposes one action: book the slot the referral offers, with the provider the "
        "referral names.",
    ),
    (
        "Booking on someone's behalf is not the agent's call. The signal parks. The topology "
        "simply refuses to continue.",
        "This booking is the agent's to take: a provider from trusted configuration, a slot "
        "validated against the referral. An outbound message on the carer's behalf is not, and the "
        "same boundary parks that one until a person approves the exact recipient, subject and "
        "body.",
    ),
    (
        "Approved, and bound to the exact arguments shown. The executor books the appointment "
        "once, idempotently.",
        "Authorized, and bound to the exact arguments the boundary allowed. The executor books the "
        "appointment once, idempotently.",
    ),
    (
        "The loop cannot close without the person.",
        "The boundary decides which actions need a person. The model never does.",
    ),
    (
        "Any decision that commits a person's time, money or records stops at the human "
        "checkpoint. The policy layer decides where that line falls; it is not inferred by the "
        "model.",
        "Where the line falls is a deterministic rule rather than a model's judgement. An action "
        "whose recipient, portal or granted role is not established by trusted configuration is "
        "refused outright; an outbound message on someone's behalf stops for a person. A booking "
        "with a trusted provider and a validated slot runs without asking — the line is drawn by "
        "what an action can reach, not by how consequential it sounds.",
    ),
    (
        "The clinics, payers and portals CareLoop acts against.",
        "The clinic CareLoop books against, running in its own process. Payers and portals are "
        "simulated.",
    ),
    (
        "Compare what was authorized with what the outside world reports.",
        "Re-read the external record of the one action that has one, and compare it with what was "
        "authorized.",
    ),
    (
        "The authorized action and an independent read-back.",
        "The authorized booking and an independent read of the clinic's own record.",
    ),
    (
        "CareLoop proposes actions, checks authority against deterministic policy, executes only "
        "what is permitted, and verifies the external result before a case is closed.",
        "CareLoop proposes actions, checks authority against deterministic policy, executes only "
        "what is permitted, and re-reads the clinic's own record before a booking is called done.",
    ),
    (
        "Prompt-injection attempts blocked",
        "Poisoned-document runs that reached an effect",
    ),
    (
        "Adversarial suite replays injected documents through the full agent path.",
        "The poison-only scenario, run against the deployed demo on 2026-08-19 and read from each "
        "case's own audit trail.",
    ),
    (
        "Referral and continuity documents carrying instructions addressed to an automated system.",
        "Thirteen runs of a case whose only document is a continuity notice addressed to the "
        "agent.",
    ),
    (
        "Covers the attack corpus in the repository, not all possible phrasings.",
        "Thirteen runs of one scenario against one model on one day. The corpus result is the row "
        "above.",
    ),
    (
        "Mutation score, policy layer",
        "Same corpus, authority layer removed",
    ),
    (
        "End-to-end scenarios",
        "Deployed runs settling as specified",
    ),
    (
        "Full workflow runs against the synthetic demo world.",
        "Every demo scenario and the approval cycle, driven through the CDN against the deployed "
        "demo.",
    ),
    (
        "Referral, malicious document and verification-mismatch scenarios, start to settled.",
        "Four scenarios five times each, plus five approval cycles, each in a freshly minted "
        "world.",
    ),
    (
        "Each scenario runs the real agent path with no step stubbed.",
        "scripts/verify_lock.py, public HTTP only, run 2026-08-19.",
    ),
    (
        "Every scenario reaches a terminal state the backend considers valid.",
        "One run in twenty-five did not: the model failed twice running to form a proposal that "
        "satisfies its own schema, and the case ended FAILED having done nothing.",
    ),
    (
        "Verification mismatches caught",
        "Booking fields re-read from the clinic",
    ),
    (
        "Cases where the world disagreed and the case stayed open.",
        "Provider, patient, slot and reason, each compared against the clinic's own record.",
    ),
    (
        "External read-backs seeded to disagree with the authorized action.",
        "A read-back moved behind CareLoop's back on each of the four fields in turn.",
    ),
    (
        "Unit and integration tests",
        "Repository test suite",
    ),
    (
        "Standard repository test run.",
        "python -m pytest -q, offline, on a mock model provider, at zero cost.",
    ),
)


def _apply_copy_corrections(text: str, source_page: str | None) -> str:
    """Replace every claim in :data:`COPY_CORRECTIONS`, and prove the source agrees.

    Each pair must occur exactly once, so a bundle that has moved on raises rather than silently
    correcting nothing. When ``source_page`` is given, the replacement is additionally required to
    be present in it — ``src/dossier.dc.html`` is the editable source, and a correction that lived
    only in this table would make the two disagree the moment anybody re-exported.
    """
    for old, new in COPY_CORRECTIONS:
        if any(c in s for s in (old, new) for c in (chr(34), chr(10))):
            raise AdaptationError(
                f"copy correction {old[:40]!r}: a pair carrying a quote or a newline is not "
                f"byte-identical in the bundle's two encodings"
            )
        if source_page is not None and new not in source_page:
            raise AdaptationError(
                f"copy correction {new[:40]!r}: not present in src/dossier.dc.html, so the "
                f"editable source and the generated page disagree"
            )
        text = _replace_exactly_once(text, old, new, f"copy correction {old[:40]!r}")
    return text


def _extract_mobile_media_query(text: str) -> str:
    """Return the whole ``@media (max-width: 720px) { … }`` block, braces included."""
    start = text.find(MOBILE_MEDIA_OPENER)
    if start < 0:
        raise AdaptationError("no @media (max-width: 720px) block")
    if text.find(MOBILE_MEDIA_OPENER, start + 1) >= 0:
        raise AdaptationError("more than one @media (max-width: 720px) block")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AdaptationError("unterminated @media (max-width: 720px) block")


def _sync_mobile_media_query(text: str, source_page: str) -> str:
    """Carry the mobile stylesheet from ``src/dossier.dc.html`` into the bundle.

    The bundle cannot be rebuilt from source without Claude Design, so a responsive fix made only
    in ``src/`` would never reach the page anybody loads. This transports exactly one block and
    nothing else: the source and the bundle carry it byte-identically today, and if they ever stop
    matching in *shape* the extraction raises rather than guessing.

    Braces are matched by depth rather than by regex because the block contains nested rules, and
    a lazy match would stop at the first inner ``}``.
    """
    wanted = _extract_mobile_media_query(source_page)
    # Inside the bundle the template is a JSON string: newlines are escaped and double quotes are
    # backslashed. The block's own rules use double quotes, so both forms have to be produced.
    current = _extract_mobile_media_query(text.replace("\\n", "\n").replace('\\"', '"'))
    if current == wanted:
        return text
    escaped_current = current.replace('"', r"\"").replace("\n", "\\n")
    escaped_wanted = wanted.replace('"', r"\"").replace("\n", "\\n")
    return _replace_exactly_once(text, escaped_current, escaped_wanted, "mobile media query")


def _fill_project_config(text: str, destinations: dict[str, str]) -> str:
    """Fill the page's destinations, in both places the page reads them from.

    **Two writes, and the second is the one that actually decides.** The page resolves each
    destination as ``P.liveUrl ?? PROJECT.liveUrl`` — props first, the ``PROJECT`` object as
    fallback. The dc-runtime supplies props from the ``data-props`` attribute, whose declared
    default is the empty string, and ``??`` falls back only on ``null``/``undefined``. An empty
    string is neither, so the empty prop wins every time and ``PROJECT`` is never consulted.

    Filling ``PROJECT`` alone is therefore inert: measured before this was understood, both URLs
    sat in ``PROJECT`` while all seven call-to-action anchors still rendered "Deployment pending"
    and "Repository pending" and pointed at in-page anchors.

    So the prop default is set as well, and ``PROJECT`` is kept in step with it rather than left
    behind — the two disagreeing would be a trap for whoever reads this next. An empty value is
    still left exactly as it was in both places, so the dossier's pending states survive byte for
    byte, which is what keeps an absent destination correct — ``devpost_url``, which no markup
    reads, is the one that stays that way.
    """
    for key, prop in (
        ("live_url", "liveUrl"),
        ("github_url", "githubUrl"),
        ("devpost_url", "devpostUrl"),
        ("video_url", "videoUrl"),
    ):
        fallback = rf"{prop}: \"\""
        seen = text.count(fallback)
        if seen != 1:
            raise AdaptationError(f"PROJECT.{prop}: expected exactly 1 empty literal, found {seen}")
        # The prop declaration lives in an HTML-escaped JSON attribute, so its quotes are entities.
        q = "&quot;"
        declared = f"{q}{prop}{q}: {{{q}editor{q}: {q}text{q}, {q}default{q}: {q}{q}"
        seen = text.count(declared)
        if seen != 1:
            raise AdaptationError(f"props.{prop}: expected exactly 1 empty default, found {seen}")

        value = destinations.get(key, "")
        if not value:
            continue
        if '"' in value or "&" in value or "<" in value:
            raise AdaptationError(f"{key}: refusing to inject a value needing escaping: {value!r}")
        text = text.replace(fallback, rf"{prop}: \"{value}\"", 1)
        text = text.replace(declared, declared[: -len("&quot;&quot;")] + f"&quot;{value}&quot;", 1)
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


#: The architecture inspector's SOURCE row, as the page ships it: one fixed literal shared by every
#: subsystem. Wiring receipts means making it read the selected subsystem instead.
SOURCE_ROW: Final = (
    r"{ k: \"SOURCE\", v: \"Source pending\", "
    r"tone: \"#6b675e\", keyTone: \"#6b675e\" }"
)
SOURCE_ROW_WIRED: Final = (
    r"{ k: \"SOURCE\", v: y.sourceReceipt || \"Source pending\", "
    r"tone: \"#6b675e\", keyTone: \"#6b675e\" }"
)


def _receipt_url(repo: str, sha: str, path: str) -> str:
    """A permalink to one path at one commit.

    ``blob`` for a file and ``tree`` for a directory, chosen by the trailing slash the config
    carries. GitHub does not serve a directory under ``blob``, so emitting one form for both would
    put a 404 behind a receipt — the exact failure this whole arrangement is switched off to avoid.
    """
    kind = "tree" if path.endswith("/") else "blob"
    return f"{repo.rstrip('/')}/{kind}/{sha}/{path.rstrip('/')}"


def _wire_source_receipts(text: str, receipts: dict) -> str:
    """Point every configured receipt at a pinned commit, or leave the page exactly as it was.

    **Disabled is a true no-op.** While ``enabled`` is false this returns its input unchanged, so a
    page built with receipts off is byte-identical to one built with the whole config section
    deleted. That is what keeps a private repository from showing a judge a link they cannot open.

    Enabled without a ``sha`` raises rather than falling back to a branch: ``blob/main`` moves, and
    a receipt that drifts off the line it cites is a false citation carrying a working link.

    Two different renderings, because the design gives the two kinds of receipt different markup.
    Subsystem receipts land in the architecture inspector's SOURCE row, which is drawn as text, so
    they resolve to repository paths. Ledger receipts land in ``EVIDENCE``, whose ``sourceUrl`` the
    page already renders as a link, so they resolve to clickable pinned URLs.
    """
    if not receipts.get("enabled", False):
        return text

    sha = receipts.get("sha", "")
    if not sha:
        raise AdaptationError(
            "source_receipts.enabled is true but sha is empty. Pin the submission commit; "
            "this refuses to fall back to a branch name."
        )
    repo = receipts.get("repo", "")
    if not repo:
        raise AdaptationError("source_receipts.enabled is true but repo is empty")

    subsystems: dict[str, list[str]] = receipts.get("subsystems", {})
    for atlas, paths in subsystems.items():
        anchor = rf"{{ atlas: \"{atlas}\", name: \""
        seen = text.count(anchor)
        if seen != 1:
            raise AdaptationError(f"subsystem {atlas!r}: expected 1 entry, found {seen}")
        receipt = " · ".join(p.rstrip("/") for p in paths)
        wired = rf"{{ sourceReceipt: \"{receipt}\", atlas: \"{atlas}\", name: \""
        text = text.replace(anchor, wired, 1)

    if subsystems:
        text = _replace_exactly_once(text, SOURCE_ROW, SOURCE_ROW_WIRED, "architecture SOURCE row")

    for eid, entry in receipts.get("evidence", {}).items():
        url = _receipt_url(repo, sha, entry["path"])
        label = entry.get("label", "View source")
        anchor = rf"id: \"{eid}\", category: \""
        seen = text.count(anchor)
        if seen != 1:
            raise AdaptationError(f"evidence receipt {eid!r}: expected 1 entry, found {seen}")
        text = text.replace(
            anchor,
            rf"sourceUrl: \"{url}\", sourceLabel: \"{label}\", id: \"{eid}\", category: \"",
            1,
        )
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


def adapt(bundle: str, config: dict, source_page: str | None = None) -> str:
    """Apply every approved transform, proving the manifest never moved."""
    before = manifest_digest(bundle)

    site = config["site"]
    base = site.get("pages_base_url", "").rstrip("/")
    image = site.get("og_image", "")
    social = f"{base}/{image}" if base and image else image

    text = bundle
    text = _apply_copy_corrections(text, source_page)
    if source_page is not None:
        text = _sync_mobile_media_query(text, source_page)
    text = _strip_dead_preconnects(text)
    text = _repoint_social_image(text, social)
    text = _repoint_lightbox_sources(text, "assets/product/")
    text = _fill_project_config(text, config.get("destinations", {}))
    text = _fill_evidence(text, config.get("evidence", {}), config.get("counterfactual", {}))
    text = _wire_source_receipts(text, config.get("source_receipts", {}))
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
    parser.add_argument("--source", type=Path, default=SOURCE_PAGE)
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
    adapted = adapt(bundle, config, args.source.read_text(encoding="utf-8"))

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
