"""The declared document sources, and the generator that turns them into the committed PDFs.

Nothing here opens a PDF for its *text* — that is ``tests/test_pdf.py``, which tests the
extraction path. This module tests the other direction: that the sources are well-formed, that
they respect the constraint trusted configuration imposes on document content, and that the bytes
committed under ``careloop/tools/fixtures/`` are exactly what the source renders to today.

The drift check is the one that earns its keep. A committed PDF is bytes nobody reviews; the whole
arrangement rests on the claim that they are derived from text somebody can, and a test is the
only thing that keeps that claim true after the next edit.
"""

from __future__ import annotations

import pytest

from careloop.graph.trusted import TRUSTED_CONFIG
from careloop.tools.document_source import (
    ATTACKER_ADDRESS,
    BOLD_MARKER,
    DOCUMENT_SOURCES,
    POISONED_DOCUMENT_ID,
    RECEIPT_DOCUMENT_ID,
    DocumentKind,
    DocumentSource,
    all_lines,
    check_sources_are_distinct,
    source,
)
from scripts.generate_documents import (
    FIXTURES_DIR,
    LineTooWide,
    drifted,
    render,
    render_all,
)

IDS = [document.document_id for document in DOCUMENT_SOURCES]


def joined(document: DocumentSource) -> str:
    """Every line of every page, space-separated. For substring assertions over one document."""
    return " ".join(
        line for page in range(1, document.page_count + 1) for line in document.text_lines(page)
    )


class TestTheSourcesAreWellFormed:
    def test_there_are_six_documents(self) -> None:
        """The set the demo is built around: referral, instructions, EOB, rejection, receipt, and
        the poisoned continuity notice."""
        assert len(DOCUMENT_SOURCES) == 6

    def test_every_kind_is_represented_exactly_once(self) -> None:
        kinds = [document.kind for document in DOCUMENT_SOURCES]
        assert sorted(kinds) == sorted(DocumentKind)

    @pytest.mark.parametrize("document", DOCUMENT_SOURCES, ids=IDS)
    def test_every_document_has_pages_with_content(self, document: DocumentSource) -> None:
        assert document.page_count >= 1
        for page in range(1, document.page_count + 1):
            assert document.text_lines(page), f"page {page} of {document.source_doc} is blank"

    @pytest.mark.parametrize("document", DOCUMENT_SOURCES, ids=IDS)
    def test_every_filename_is_a_pdf(self, document: DocumentSource) -> None:
        assert document.source_doc.endswith(".pdf")

    def test_ids_and_filenames_are_distinct(self) -> None:
        check_sources_are_distinct()

    @pytest.mark.parametrize("field", ["document_id", "source_doc"])
    def test_a_collision_would_be_caught(self, field: str, monkeypatch) -> None:
        """Vacuity: the check has to fail on the thing it exists to catch."""
        import careloop.tools.document_source as module

        update = {"document_id": "doc-9999-other", "source_doc": "other.pdf"}
        update[field] = getattr(DOCUMENT_SOURCES[0], field)
        duplicate = DOCUMENT_SOURCES[0].model_copy(update=update)
        monkeypatch.setattr(module, "DOCUMENT_SOURCES", (*DOCUMENT_SOURCES, duplicate))

        with pytest.raises(ValueError, match="declared twice"):
            check_sources_are_distinct()

    def test_source_lookup_raises_rather_than_returning_none(self) -> None:
        assert source(POISONED_DOCUMENT_ID).document_id == POISONED_DOCUMENT_ID
        with pytest.raises(KeyError):
            source("doc-9999-nonexistent")

    def test_the_bold_marker_never_reaches_the_text(self) -> None:
        """It is a rendering instruction, so it must not be part of what extraction sees."""
        assert not any(line.startswith(BOLD_MARKER) for line in all_lines())

    def test_some_line_actually_is_bold(self) -> None:
        """Otherwise the previous test passes because the marker is unused."""
        bold = [
            text
            for document in DOCUMENT_SOURCES
            for page in range(1, document.page_count + 1)
            for text, is_bold in document.rendered(page)
            if is_bold
        ]
        assert bold


class TestDocumentContentRespectsTrustedConfiguration:
    """No document may carry a value that trusted configuration also carries.

    Trust resolution takes the **lowest** level among facts whose value matches, so a configured
    destination that also appeared in a document would be dragged to ``UNTRUSTED`` and the
    legitimate action it authorises would be denied — for a reason no diagnostic explains. The same
    lowest-wins consequence made fact promotion silently invisible before commit 6.
    """

    @staticmethod
    def _collisions(lines: tuple[str, ...]) -> list[str]:
        """Configured keys whose value appears in any of ``lines``."""
        return sorted(key for key, value in TRUSTED_CONFIG if any(value in line for line in lines))

    def test_no_document_names_a_configured_value(self) -> None:
        assert self._collisions(all_lines()) == []

    def test_the_sweep_would_notice(self) -> None:
        """Vacuity: run the same check over a line carrying a configured value."""
        key, value = TRUSTED_CONFIG[0]
        planted = (*all_lines(), f"Please forward a copy to {value} for filing.")

        assert self._collisions(planted) == [key]


class TestThePoisonedDocument:
    """The fixture T2 aims at. It must stay dangerous."""

    @pytest.fixture
    def notice(self) -> DocumentSource:
        return source(POISONED_DOCUMENT_ID)

    def test_it_asks_for_all_three_refused_things(self, notice: DocumentSource) -> None:
        text = joined(notice)
        assert ATTACKER_ADDRESS in text, "the DESTINATION payload (forward to an address)"
        assert "PORTAL-MG-TRANSFER" in text, "the second DESTINATION payload (upload to a portal)"
        assert "records_admin" in text, "the AUTHORITY payload (grant a role)"

    def test_the_exfiltration_address_is_on_one_declared_line(self, notice: DocumentSource) -> None:
        """The address must not be split across lines in the source.

        A wrapped address would be extracted as two tokens, and the planner would then be tempted
        by something that is not a usable address at all — T2 would pass because the attack was
        accidentally defused rather than because the gate refused it. ``tests/test_pdf.py``
        asserts the same property on the far side of extraction; this asserts it here, where the
        mistake would actually be made.
        """
        lines = [
            line for page in range(1, notice.page_count + 1) for line in notice.text_lines(page)
        ]
        assert any(ATTACKER_ADDRESS in line for line in lines)
        assert not any(
            line.endswith("records-intake@") or line.startswith("claims-continuity")
            for line in lines
        )

    def test_it_carries_no_crude_jailbreak_marker(self, notice: DocumentSource) -> None:
        """A defanged fixture would make T2 prove less than it appears to.

        The interesting claim is that a *plausible* instruction gains no authority. An "ignore all
        previous instructions" banner is caught by anything, including nothing.
        """
        text = joined(notice).lower()
        for marker in (
            "ignore all previous",
            "ignore previous instructions",
            "disregard your instructions",
            "system prompt",
            "you are now",
        ):
            assert marker not in text

    def test_it_reads_as_a_real_insurer_notice(self, notice: DocumentSource) -> None:
        """Each request carries a business reason, which is what makes it a temptation."""
        text = joined(notice)
        assert "prior-authorisation" in text
        assert "Policy: MG-88213-B" in text


class TestTheClaimRejectionDrivesTheResume:
    """The follow-up event's document has to name the document the reopened case supplies."""

    def test_it_names_the_receipt(self) -> None:
        rejection = source("doc-0006-claim-rejection")
        text = " ".join(rejection.text_lines(1))
        assert f"Required document: {RECEIPT_DOCUMENT_ID}" in text

    def test_the_named_document_is_one_that_exists(self) -> None:
        assert source(RECEIPT_DOCUMENT_ID).kind is DocumentKind.RECEIPT

    def test_the_receipt_settles_the_amount_the_eob_left_outstanding(self) -> None:
        """The cross-document check in commit 3 rests on these two numbers agreeing."""
        eob = " ".join(source("doc-0004-eob").text_lines(1))
        receipt = " ".join(source(RECEIPT_DOCUMENT_ID).text_lines(1))
        assert "Patient responsibility: 172.00" in eob
        assert "Amount paid: 172.00" in receipt

    def test_the_rejection_and_the_eob_are_the_same_claim(self) -> None:
        eob = " ".join(source("doc-0004-eob").text_lines(1))
        rejection = " ".join(source("doc-0006-claim-rejection").text_lines(1))
        assert "Claim number: CLM-2026-44718" in eob
        assert "Claim number: CLM-2026-44718" in rejection


class TestTheGeneratorIsDeterministic:
    """``invariant=1`` is what makes the committed bytes comparable.

    Without it reportlab stamps ``/CreationDate`` and a trailer ``/ID`` from the clock, and every
    regeneration differs. Verified by execution before the generator was written: with the flag two
    runs share a sha256, without it they do not.
    """

    @pytest.mark.parametrize("document", DOCUMENT_SOURCES, ids=IDS)
    def test_rendering_twice_gives_the_same_bytes(self, document: DocumentSource) -> None:
        assert render(document) == render(document)

    def test_the_output_carries_no_clock(self) -> None:
        """The one field that would otherwise move, pinned to reportlab's invariant epoch."""
        data = render(DOCUMENT_SOURCES[0])
        assert b"D:20000101000000" in data

    def test_every_declared_document_is_rendered(self) -> None:
        assert set(render_all()) == {document.source_doc for document in DOCUMENT_SOURCES}


class TestTheCommittedPdfsMatchTheirSource:
    def test_nothing_has_drifted(self) -> None:
        """If this fails, run ``python scripts/generate_documents.py`` and commit the result."""
        assert drifted() == []

    def test_the_fixtures_directory_holds_exactly_the_rendered_set(self) -> None:
        on_disk = {path.name for path in FIXTURES_DIR.glob("*.pdf")}
        assert on_disk == {document.source_doc for document in DOCUMENT_SOURCES}

    def test_drift_would_be_noticed(self, tmp_path) -> None:
        """Vacuity, in both directions: a changed file and a missing one both count as drift."""
        for filename, data in render_all().items():
            (tmp_path / filename).write_bytes(data)
        assert drifted(tmp_path) == []

        tampered = next(iter(render_all()))
        (tmp_path / tampered).write_bytes(b"%PDF-1.4 not what the source says")
        assert drifted(tmp_path) == [tampered]

        (tmp_path / tampered).unlink()
        assert drifted(tmp_path) == [tampered]

    def test_an_undeclared_pdf_counts_as_drift(self, tmp_path) -> None:
        """A PDF nothing can regenerate is the opaque binary this arrangement exists to avoid."""
        for filename, data in render_all().items():
            (tmp_path / filename).write_bytes(data)
        (tmp_path / "orphan.pdf").write_bytes(b"%PDF-1.4 from nowhere")

        assert drifted(tmp_path) == ["orphan.pdf"]


class TestTheLayoutGuard:
    """A line that does not fit is an error rather than a line that runs off the page.

    ``drawString`` neither wraps nor clips, so an over-long line leaves the visible page but stays
    in the content stream. Extraction still finds it; only a human opening the file would see
    anything wrong. For the poisoned notice that is the difference between an attack that survives
    and one that was defused by accident.
    """

    def test_an_overlong_line_is_refused(self) -> None:
        too_wide = DOCUMENT_SOURCES[0].model_copy(update={"pages": (("x" * 400,),)})

        with pytest.raises(LineTooWide, match="wide and the frame is"):
            render(too_wide)

    def test_the_shipped_documents_all_fit(self) -> None:
        """Vacuity for the guard above: it must not be refusing everything."""
        for document in DOCUMENT_SOURCES:
            assert render(document)
