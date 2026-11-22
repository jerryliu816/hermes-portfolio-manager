"""Document detection and deterministic CSV pre-cleaning."""

from __future__ import annotations

from pathlib import Path

from portfolio_manager.extraction.contracts import DocumentKind
from portfolio_manager.ingestion.detector import clean_csv, detect_kind

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_synthetic.csv"
)


def test_pdf_detected_by_magic_number_not_extension() -> None:
    """A mislabelled file must not be fed to the CSV path."""
    assert detect_kind("statement.txt", b"%PDF-1.4\n...") is DocumentKind.PDF
    assert detect_kind("holdings.pdf", b"%PDF-1.4") is DocumentKind.PDF


def test_csv_detected_by_extension_and_by_header() -> None:
    assert detect_kind("export.csv", b"a,b\n1,2") is DocumentKind.CSV
    headerish = b"Account number,Account name,Symbol,Quantity,Current value\nZ1,x,AAA,1,$2"
    assert detect_kind("export.dat", headerish) is DocumentKind.CSV


def test_unrecognised_content_is_text() -> None:
    assert detect_kind("notes.dat", b"just some prose") is DocumentKind.TEXT


def test_bom_is_stripped_from_the_header() -> None:
    text = FIXTURE.read_bytes().decode("utf-8-sig")
    cleaned = clean_csv(text)
    assert cleaned.header[0] == "Account number"


def test_trailing_empty_field_is_dropped_without_truncating_data() -> None:
    """Every real data row has 17 fields against a 16-field header."""
    text = FIXTURE.read_bytes().decode("utf-8-sig")
    cleaned = clean_csv(text)
    assert cleaned.width == 16
    assert all(len(row) == 16 for row in cleaned.rows)
    assert any("trailing empty field" in n for n in cleaned.notes)


def test_quoted_commas_inside_account_names_survive() -> None:
    text = FIXTURE.read_bytes().decode("utf-8-sig")
    cleaned = clean_csv(text)
    names = {row[1] for row in cleaned.rows}
    assert any(name.startswith("Pat Q Sample, Alex Sample") for name in names)


def test_disclaimers_and_the_date_footer_are_separated_from_data() -> None:
    """The only date in the file sits below the disclaimers."""
    text = FIXTURE.read_bytes().decode("utf-8-sig")
    cleaned = clean_csv(text)
    assert any("Date downloaded" in line for line in cleaned.trailing_lines)
    assert not any("Date downloaded" in row[0] for row in cleaned.rows)
    assert any("synthetic file" in line for line in cleaned.trailing_lines)


def test_empty_document_is_reported_not_crashed() -> None:
    cleaned = clean_csv("")
    assert cleaned.rows == []
    assert "empty" in cleaned.notes[0]
