"""The anonymized fixture must be a faithful stand-in for the real corpus.

A fixture that carries none of the content is only useful if it still carries
all of the *shape*. Every property asserted here was measured on the real file
first; if the anonymizer ever stops preserving one, the suite would keep passing
while quietly testing something easier than reality.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from portfolio_manager.config import Settings
from portfolio_manager.extraction.contracts import ExtractionContext, SourceDocument
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.ingestion.normalizer import parse_money
from portfolio_manager.models.entities import Account, AccountSnapshot, PositionSnapshot
from portfolio_manager.services.importer import import_document

from .test_no_private_data_in_repo import _private_csv

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)


@pytest.fixture
def extracted() -> object:
    provider = MockExtractionProvider()
    document = SourceDocument.from_path(FIXTURE)
    overview = provider.understand_document(document, ExtractionContext()).overview
    return provider.extract_positions(document, overview, ExtractionContext()).document


def test_byte_level_quirks_survive() -> None:
    raw = FIXTURE.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM must be preserved"
    assert b"\r\n" in raw, "CRLF line endings must be preserved"


def test_account_and_position_counts_match_the_real_corpus(extracted: object) -> None:
    assert len(extracted.accounts) == 14  # type: ignore[attr-defined]
    assert sum(len(a.positions) for a in extracted.accounts) == 86  # type: ignore[attr-defined]


def test_the_mix_of_native_and_aggregated_accounts_is_preserved(
    extracted: object,
) -> None:
    """4 native, 10 aggregated -- the distinction precedence depends on."""
    aggregated = [a for a in extracted.accounts if a.is_aggregated]  # type: ignore[attr-defined]
    native = [a for a in extracted.accounts if not a.is_aggregated]  # type: ignore[attr-defined]
    assert len(native) == 4
    assert len(aggregated) == 10


def test_null_density_matches_the_real_corpus(extracted: object) -> None:
    """12 symbol-less holdings, 10 without value, 25 without cost basis."""
    positions = [p for a in extracted.accounts for p in a.positions]  # type: ignore[attr-defined]
    assert sum(1 for p in positions if p.symbol is None) == 12
    assert sum(1 for p in positions if p.market_value is None) == 10
    assert sum(1 for p in positions if p.cost_basis is None) == 25


def test_zero_quantity_closed_rows_survive(extracted: object) -> None:
    positions = [p for a in extracted.accounts for p in a.positions]  # type: ignore[attr-defined]
    closed = [p for p in positions if p.quantity == "0" and p.market_value is None]
    assert len(closed) >= 6


def test_the_footer_date_is_still_findable(extracted: object) -> None:
    """The only date in the file sits below the disclaimers."""
    assert extracted.as_of_date == "Aug-26-2026"  # type: ignore[attr-defined]
    assert "Date downloaded" in (extracted.as_of_date_source or "")  # type: ignore[attr-defined]


def test_classification_still_finds_the_same_recognisable_accounts(
    extracted: object,
) -> None:
    """Account-type vocabulary is preserved even though names are not."""
    hints = {a.account_type_hint for a in extracted.accounts}  # type: ignore[attr-defined]
    assert {"Roth IRA", "Rollover IRA", "529", "Joint", "Corporate"} <= hints


def test_an_unclassifiable_account_is_still_unclassifiable(extracted: object) -> None:
    """The masked account must not accidentally become readable."""
    names = [a.display_name for a in extracted.accounts]  # type: ignore[attr-defined]
    assert any(n.startswith("**********") for n in names)


def test_split_price_divergence_survives(extracted: object) -> None:
    """One symbol at two prices across accounts, each internally consistent.

    This is the property that forces price onto the position rather than the
    security, so a fixture without it would let that regress unnoticed.
    """
    by_symbol: dict[str, set[str]] = {}
    for account in extracted.accounts:  # type: ignore[attr-defined]
        for position in account.positions:
            if position.symbol and position.price:
                by_symbol.setdefault(position.symbol, set()).add(position.price)
    divergent = {s: p for s, p in by_symbol.items() if len(p) > 1}
    assert len(divergent) >= 6, f"expected the real 6 divergent symbols, got {divergent}"


def test_amounts_are_scaled_away_from_the_real_values(extracted: object) -> None:
    """The shape is preserved; the magnitudes must not be.

    Checked against the private corpus itself rather than a hardcoded figure.
    Writing the real total into an assertion in order to prove it does not
    appear would put it in the repository, which is the thing being prevented —
    and it would go stale the moment the portfolio changes. When the private
    corpus is absent, as it is anywhere but the owner's machine, there is
    nothing to compare against and the check skips.
    """
    total = sum(
        (parse_money(p.market_value) or Decimal(0))
        for a in extracted.accounts  # type: ignore[attr-defined]
        for p in a.positions
    )
    assert total > Decimal("0")

    # Resolved by shape, not by one filename: the corpus is kept as dated
    # exports, and pinning a name is how the other guard silently stopped
    # guarding. One resolver, so there is one thing to keep right.
    private = _private_csv()
    if private is None:
        pytest.skip("the private corpus is not present; nothing to compare against")

    rows = [
        row
        for row in csv.reader(io.StringIO(private.read_text(encoding="utf-8-sig")))
        if len(row) >= 16 and row[0].strip() and row[0] != "Account number"
    ]
    real_total = sum((parse_money(row[7]) or Decimal(0) for row in rows), Decimal(0))
    assert total != real_total, "the real total must not survive anonymisation"


def test_the_whole_pipeline_runs_on_it(session: Session, tmp_path: Path) -> None:
    """End to end: the anonymized corpus behaves like the real one."""
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]
    result = import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)

    # 14 accounts, 4 of which report no value at all and are skipped.
    assert session.query(Account).count() == 10
    assert len(result.snapshot.snapshot.skipped_accounts_json) == 4
    assert session.query(PositionSnapshot).count() == 80

    # And it reconciles cleanly, exactly as the real file does.
    assert result.snapshot.reconciliation.passed, [
        c.describe() for c in result.snapshot.reconciliation.failures
    ]
    assert result.snapshot.validation.is_complete

    # Aggregated accounts carry an unknown as-of date.
    stale = session.query(AccountSnapshot).filter_by(is_stale=True).count()
    assert stale == 6


def test_the_anonymized_pair_exercises_precedence(session: Session, tmp_path: Path) -> None:
    """A synthetic direct source must reproduce the real pair's behaviour.

    The generated document deliberately reports fewer holdings than the
    aggregation and carries settlement cash outside its holdings table -- the
    two features that made the real Vanguard document impossible to treat as
    enrichment.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument
    from portfolio_manager.models.entities import AccountSnapshot
    from portfolio_manager.services.multi_source import add_document_to_snapshot

    pair_path = FIXTURE.parents[1] / "vanguard/direct_source_anonymized.json"
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    assert direct.accounts, "the pair fixture must cover at least one account"
    assert any(a.cash_balance for a in direct.accounts), "must carry settlement cash"

    settings = Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]
    imported = import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)
    snapshot = imported.snapshot.snapshot
    before = sum(a.total_value for a in session.query(AccountSnapshot).all())

    result = add_document_to_snapshot(session, snapshot, direct, settings=settings)

    # The direct source wins its accounts outright, and says why.
    assert result.precedence.replaced_accounts == len(direct.accounts)
    assert all(d.winner == "DirectVest" for d in result.precedence.decisions)
    assert all("directly" in d.reason for d in result.precedence.decisions)

    # The account set is unchanged; only its contents and value moved.
    assert session.query(Account).count() == 10
    after = sum(a.total_value for a in session.query(AccountSnapshot).all())
    assert after != before, "a winning direct source should change the valuation"

    # Settlement cash is carried rather than dropped.
    assert sum(a.cash_balance for a in session.query(AccountSnapshot).all()) > Decimal(0)

    # Those accounts are no longer stale, because a direct source now supplies them.
    supplied = [a for a in session.query(AccountSnapshot).all() if a.supplied_by == "DirectVest"]
    assert len(supplied) == len(direct.accounts)
    assert all(not a.is_stale for a in supplied)
