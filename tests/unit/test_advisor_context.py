"""Philosophy and advisor context.

The rules under test: text is never guessed (a PDF with no text layer says so),
an advisor's paraphrase is not the user's word until confirmed, an expired view
is surfaced rather than dropped, and nothing is ever overwritten in the
philosophy's history.
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from portfolio_manager.config import Settings
from portfolio_manager.models.enums import ContextCreator, ContextKind, TextExtraction
from portfolio_manager.services.advisor_context import (
    ContextError,
    add_document,
    add_note,
    add_url,
    delete_context,
    extract_pdf_text,
    list_context,
    update_context,
)
from portfolio_manager.services.philosophy import (
    PhilosophyError,
    active_philosophy,
    philosophy_history,
    save_philosophy,
)


def _pdf(pages: list[str]) -> bytes:
    """A PDF whose pages carry the given text; an empty string is a blank page."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for text in pages:
        if text:
            pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


# --- PDF text ---------------------------------------------------------------


def test_pdf_text_is_extracted_per_page() -> None:
    result = extract_pdf_text(
        _pdf(["Rates are expected to fall next year.", "The second page has its own text too."])
    )
    assert result.extraction is TextExtraction.EXTRACTED
    assert "Rates are expected to fall" in result.text
    assert "[page 2]" in result.text
    assert result.empty_pages == []


def test_a_pdf_with_no_text_layer_is_reported_not_guessed() -> None:
    result = extract_pdf_text(_pdf(["", ""]))
    assert result.extraction is TextExtraction.NO_TEXT
    assert result.text == ""
    assert result.empty_pages == [1, 2]


def test_partly_scanned_pdf_names_the_missing_pages() -> None:
    result = extract_pdf_text(_pdf(["A page with plenty of real text on it.", ""]))
    assert result.extraction is TextExtraction.EXTRACTED
    assert result.empty_pages == [2]


def test_an_unreadable_pdf_raises_rather_than_returning_empty() -> None:
    with pytest.raises(ContextError):
        extract_pdf_text(b"%PDF-1.4 this is not really a pdf")


# --- documents --------------------------------------------------------------


def test_document_body_states_which_pages_were_unreadable(
    session: Session, settings: Settings
) -> None:
    record = add_document(
        session,
        filename="outlook.pdf",
        content=_pdf(["Outlook: equities look expensive versus bonds.", ""]),
        settings=settings,
        note="The bank's annual outlook",
    )
    assert record.extraction == TextExtraction.EXTRACTED.value
    assert record.body.startswith("The bank's annual outlook")
    assert "no text layer on page(s) 2" in record.body
    assert "equities look expensive" in record.body


def test_scanned_document_is_kept_and_marked(session: Session, settings: Settings) -> None:
    record = add_document(session, filename="scan.pdf", content=_pdf([""]), settings=settings)
    assert record.extraction == TextExtraction.NO_TEXT.value
    assert record.body == ""
    assert record.stored_path is not None


def test_document_bytes_are_preserved_owner_only(session: Session, settings: Settings) -> None:
    content = _pdf(["Some text long enough to count as a page."])
    record = add_document(session, filename="a.pdf", content=content, settings=settings)
    assert record.sha256 is not None
    path = settings.context_dir / record.sha256[:2] / record.sha256
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o600


def test_deleting_the_last_reference_removes_the_file(session: Session, settings: Settings) -> None:
    content = _pdf(["Some text long enough to count as a page."])
    first = add_document(session, filename="a.pdf", content=content, settings=settings)
    second = add_document(session, filename="b.pdf", content=content, settings=settings)
    assert first.sha256 is not None
    path = settings.context_dir / first.sha256[:2] / first.sha256

    delete_context(session, first, settings)
    assert path.exists(), "still referenced by the second upload"
    delete_context(session, second, settings)
    assert not path.exists()


def test_unsupported_and_empty_documents_are_refused(session: Session, settings: Settings) -> None:
    with pytest.raises(ContextError):
        add_document(session, filename="sheet.xlsx", content=b"PK\x03\x04", settings=settings)
    with pytest.raises(ContextError):
        add_document(session, filename="empty.pdf", content=b"", settings=settings)


def test_markdown_document_is_read_as_text(session: Session, settings: Settings) -> None:
    record = add_document(
        session, filename="notes.md", content=b"# Notes\nAvoid tobacco.", settings=settings
    )
    assert record.extraction == TextExtraction.EXTRACTED.value
    assert "Avoid tobacco" in record.body


# --- notes and urls ---------------------------------------------------------


def test_a_users_note_is_confirmed_and_an_advisors_is_not(session: Session) -> None:
    mine = add_note(session, kind=ContextKind.VIEW, title="Rates", body="Rates fall in 2027")
    theirs = add_note(
        session,
        kind=ContextKind.PREFERENCE,
        title="No banks",
        body="User said they avoid individual bank stocks",
        created_by=ContextCreator.HERMES,
    )
    assert mine.confirmed
    assert not theirs.confirmed


def test_editing_an_advisors_paraphrase_makes_it_the_users(session: Session) -> None:
    record = add_note(
        session,
        kind=ContextKind.VIEW,
        title="t",
        body="paraphrase",
        created_by=ContextCreator.HERMES,
    )
    update_context(session, record, body="what I actually think")
    assert record.confirmed


def test_a_note_must_say_something(session: Session) -> None:
    with pytest.raises(ContextError):
        add_note(session, kind=ContextKind.VIEW, title="t", body="   ")
    with pytest.raises(ContextError):
        add_note(session, kind=ContextKind.URL, title="t", body="b")


def test_url_without_captured_text_is_read_live(session: Session) -> None:
    record = add_url(session, url="https://example.com/outlook", note="worth reading")
    assert record.extraction == TextExtraction.NOT_CAPTURED.value
    assert record.title == "https://example.com/outlook"
    assert record.body == "worth reading"


@pytest.mark.parametrize("bad", ["ftp://example.com/x", "example.com", "javascript:alert(1)"])
def test_only_http_urls_are_accepted(session: Session, bad: str) -> None:
    with pytest.raises(ContextError):
        add_url(session, url=bad)


# --- listing ----------------------------------------------------------------


def test_an_expired_view_is_listed_separately_not_dropped(session: Session) -> None:
    today = dt.date(2026, 11, 22)
    lapsed = add_note(
        session,
        kind=ContextKind.VIEW,
        title="Fed",
        body="Cut at the next meeting",
        expires_on=dt.date(2026, 11, 1),
    )
    live = add_note(session, kind=ContextKind.PREFERENCE, title="ESG", body="Avoid tobacco")
    listing = list_context(session, today=today)
    assert lapsed in listing.expired
    assert live in listing.current
    assert lapsed not in listing.current


def test_archived_items_are_hidden_unless_asked_for(session: Session) -> None:
    record = add_note(session, kind=ContextKind.VIEW, title="t", body="b")
    update_context(session, record, is_active=False)
    assert record not in list_context(session).current
    assert record in list_context(session, include_archived=True).current


# --- philosophy -------------------------------------------------------------


def test_saving_a_philosophy_never_overwrites(session: Session) -> None:
    first = save_philosophy(session, "Index funds, long horizon.")
    second = save_philosophy(session, "Index funds, long horizon, small value tilt.")
    assert (first.version, second.version) == (1, 2)
    assert active_philosophy(session) is second
    assert not first.is_active
    assert [r.version for r in philosophy_history(session)] == [2, 1]


def test_an_empty_philosophy_is_refused(session: Session) -> None:
    with pytest.raises(PhilosophyError):
        save_philosophy(session, "  \n ")
