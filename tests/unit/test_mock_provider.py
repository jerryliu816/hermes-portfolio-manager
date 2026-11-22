"""The mock provider, exercised against a structurally faithful synthetic file.

The fixture reproduces every awkward shape found in the real corpus. If the
mock handled only clean data, the whole test suite would be testing a fiction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument
from portfolio_manager.extraction.contracts import (
    DocumentExtractionProvider,
    DocumentKind,
    ExtractionContext,
    ExtractionError,
    ExtractionErrorKind,
    SourceDocument,
)
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_synthetic.csv"
)


@pytest.fixture
def document() -> SourceDocument:
    return SourceDocument.from_path(FIXTURE)


@pytest.fixture
def provider() -> MockExtractionProvider:
    return MockExtractionProvider()


def _extract(
    provider: MockExtractionProvider, document: SourceDocument
) -> ExtractedPortfolioDocument:
    overview = provider.understand_document(document, ExtractionContext()).overview
    return provider.extract_positions(document, overview, ExtractionContext()).document


def test_provider_satisfies_the_protocol(provider: MockExtractionProvider) -> None:
    assert isinstance(provider, DocumentExtractionProvider)


def test_utf8_bom_and_crlf_do_not_corrupt_the_header(document: SourceDocument) -> None:
    assert document.content.startswith(b"\xef\xbb\xbf"), "fixture must retain its BOM"
    assert b"\r\n" in document.content, "fixture must retain CRLF"
    assert document.text.startswith("Account number"), "BOM must be stripped on decode"


def test_finds_every_account_including_single_position_ones(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    doc = _extract(provider, document)
    assert len(doc.accounts) == 5
    counts = {a.display_name[:20]: len(a.positions) for a in doc.accounts}
    assert counts["Joint WROS - TOD"] == 2
    assert counts["ROTH IRA"] == 1


def test_trailing_comma_does_not_shift_columns(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """Every data row has 17 fields against a 16-field header."""
    doc = _extract(provider, document)
    joint = next(a for a in doc.accounts if a.display_name == "Joint WROS - TOD")
    alpha = next(p for p in joint.positions if p.symbol == "AAA")
    assert alpha.price == "$200.00"
    assert alpha.market_value == "$20100.00"
    assert alpha.cost_basis == "$15000.00"


def test_null_sentinel_becomes_none_not_zero(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    doc = _extract(provider, document)
    joint = next(a for a in doc.accounts if a.display_name == "Joint WROS - TOD")
    mmkt = next(p for p in joint.positions if p.symbol == "MMKT")
    assert mmkt.cost_basis is None
    assert mmkt.unrealized_gain_loss is None
    assert mmkt.market_value == "$20100.00"


def test_positions_without_a_ticker_keep_a_null_symbol(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    doc = _extract(provider, document)
    plan529 = next(a for a in doc.accounts if "529" in a.display_name)
    assert all(p.symbol is None for p in plan529.positions)
    assert plan529.positions[0].description == "Sample 500 Index Portfolio"


def test_zero_quantity_rows_are_kept(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """A closed position is information; dropping it loses history."""
    doc = _extract(provider, document)
    plan529 = next(a for a in doc.accounts if "529" in a.display_name)
    closed = [p for p in plan529.positions if p.quantity == "0"]
    assert len(closed) == 2
    assert all(p.market_value is None for p in closed)


def test_aggregated_accounts_are_flagged_and_attributed(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """A UUID key means the account is reported second-hand."""
    doc = _extract(provider, document)
    native = [a for a in doc.accounts if not a.is_aggregated]
    aggregated = [a for a in doc.accounts if a.is_aggregated]
    assert {a.display_name for a in native} == {"Joint WROS - TOD", "ROTH IRA"}
    assert len(aggregated) == 3
    assert all(a.institution != "Fidelity" for a in aggregated)


def test_account_type_is_hinted_only_when_the_name_says_so(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """An opaque account name must yield null, never a guess."""
    doc = _extract(provider, document)
    hints = {a.display_name[:20]: a.account_type_hint for a in doc.accounts}
    assert hints["ROTH IRA"] == "Roth IRA"
    assert hints["Joint WROS - TOD"] == "Joint"
    assert hints["Opaque Plan ABC-1234"] is None


def test_as_of_date_is_read_from_the_footer(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """The only date in the file sits below the disclaimers, not in a header."""
    doc = _extract(provider, document)
    assert doc.as_of_date == "Aug-26-2026"
    assert doc.as_of_date_source is not None
    assert "Date downloaded" in doc.as_of_date_source


def test_disclaimer_rows_are_not_parsed_as_holdings(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    doc = _extract(provider, document)
    descriptions = [p.description for a in doc.accounts for p in a.positions]
    assert not any("synthetic file" in d for d in descriptions)


def test_stage1_and_stage2_agree_on_the_account_set(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """The completeness invariant depends on these two agreeing."""
    overview = provider.understand_document(document, ExtractionContext()).overview
    doc = provider.extract_positions(document, overview, ExtractionContext()).document
    assert {a.external_account_key for a in overview.accounts} == {
        a.external_account_key for a in doc.accounts
    }
    for descriptor in overview.accounts:
        account = next(
            a for a in doc.accounts if a.external_account_key == descriptor.external_account_key
        )
        assert descriptor.expected_position_count == len(account.positions)


def test_context_can_scope_extraction_to_named_accounts(
    provider: MockExtractionProvider, document: SourceDocument
) -> None:
    """Chunked extraction depends on account scoping working."""
    overview = provider.understand_document(document, ExtractionContext()).overview
    result = provider.extract_positions(
        document, overview, ExtractionContext(account_keys=("222222222",))
    )
    assert [a.external_account_key for a in result.document.accounts] == ["222222222"]


def test_pdf_without_a_fixture_fails_loudly(provider: MockExtractionProvider) -> None:
    """Never fabricate holdings for a document the mock cannot read."""
    pdf = SourceDocument(
        filename="statement.pdf", kind=DocumentKind.PDF, content=b"%PDF-1.4", sha256="deadbeef"
    )
    with pytest.raises(ExtractionError) as excinfo:
        provider.understand_document(pdf, ExtractionContext())
    assert excinfo.value.kind is ExtractionErrorKind.UNSUPPORTED_DOCUMENT


def test_unparseable_csv_fails_loudly(provider: MockExtractionProvider) -> None:
    junk = SourceDocument(
        filename="junk.csv",
        kind=DocumentKind.CSV,
        content=b"alpha,beta\n1,2\n",
        sha256="cafe",
    )
    with pytest.raises(ExtractionError) as excinfo:
        provider.understand_document(junk, ExtractionContext())
    assert excinfo.value.kind is ExtractionErrorKind.UNSUPPORTED_DOCUMENT
