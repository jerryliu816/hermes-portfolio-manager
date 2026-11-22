"""Phase 3 acceptance: extraction through to a verified, skip-aware result.

Runs the whole deterministic chain -- extract, normalize, skip, reconcile --
over the synthetic fixture, which reproduces every awkward shape of the real
corpus. This is the test that would catch a regression in how the pieces fit
together, as opposed to how each behaves alone.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from portfolio_manager.extraction.contracts import ExtractionContext, SourceDocument
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.ingestion.normalizer import (
    normalize_security_key,
    parse_money,
    parse_quantity,
)
from portfolio_manager.ingestion.reconciler import (
    ReconciliationReport,
    Tolerance,
    check_position_value,
)
from portfolio_manager.ingestion.validator import check_completeness, partition_accounts

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_synthetic.csv"
)
TOL = Tolerance(absolute=Decimal("1.00"), percentage=Decimal("0.001"), maximum=Decimal("25.00"))


def _run() -> tuple[object, object]:
    provider = MockExtractionProvider()
    document = SourceDocument.from_path(FIXTURE)
    overview = provider.understand_document(document, ExtractionContext()).overview
    extracted = provider.extract_positions(document, overview, ExtractionContext()).document
    return overview, extracted


def test_pipeline_reaches_a_complete_extraction() -> None:
    overview, extracted = _run()
    report = check_completeness(
        [a.external_account_key for a in overview.accounts],  # type: ignore[attr-defined]
        [a.external_account_key for a in extracted.accounts],  # type: ignore[attr-defined]
    )
    assert report.is_complete
    assert not report.requires_review


def test_zero_value_accounts_are_skipped_and_named() -> None:
    """One fixture account holds a position but reports no value."""
    _, extracted = _run()
    keep, skipped = partition_accounts(
        [
            (a.external_account_key, a.display_name, [p.market_value for p in a.positions])
            for a in extracted.accounts  # type: ignore[attr-defined]
        ]
    )
    assert len(keep) == 4
    assert [s.display_name for s in skipped] == ["Opaque Plan ABC-12345"]
    assert skipped[0].reason == "zero_reported_value"


def test_skipping_does_not_change_the_portfolio_total() -> None:
    """Skipped accounts contributed nothing, so the total must be unmoved."""
    _, extracted = _run()
    everything = Decimal("0")
    kept = Decimal("0")
    keep, _ = partition_accounts(
        [
            (a.external_account_key, a.display_name, [p.market_value for p in a.positions])
            for a in extracted.accounts  # type: ignore[attr-defined]
        ]
    )
    for account in extracted.accounts:  # type: ignore[attr-defined]
        for position in account.positions:
            value = parse_money(position.market_value)
            if value is None:
                continue
            everything += value
            if account.external_account_key in keep:
                kept += value
    assert everything == kept
    assert kept == Decimal("80200.00")


def test_every_position_reconciles_or_is_properly_skipped() -> None:
    """No spurious failures from closed rows or absent fields."""
    _, extracted = _run()
    report = ReconciliationReport()
    for account in extracted.accounts:  # type: ignore[attr-defined]
        for position in account.positions:
            quantity = parse_quantity(position.quantity)
            value = parse_money(position.market_value)
            closed = quantity == Decimal("0") and value is None
            report.checks.append(
                check_position_value(
                    f"{account.display_name[:16]}/{position.symbol or position.description[:14]}",
                    quantity,
                    parse_money(position.price),
                    value,
                    TOL,
                    is_closed=closed,
                )
            )
    assert report.passed, [c.describe() for c in report.failures]
    summary = report.summary()
    assert summary["failed"] == 0
    assert summary["skipped"] >= 3, "closed and unpriced rows must skip, not fail"


def test_holdings_without_a_ticker_still_get_a_stable_identity() -> None:
    _, extracted = _run()
    plan529 = next(a for a in extracted.accounts if "529" in a.display_name)  # type: ignore[attr-defined]
    keys = {normalize_security_key(p.symbol, p.description) for p in plan529.positions}
    assert "sample-500-index-portfolio" in keys
    assert all(k for k in keys)


def test_one_security_in_two_accounts_keeps_two_identities() -> None:
    """AAA is held natively and in an aggregated account at different prices."""
    _, extracted = _run()
    rows = [
        (a.display_name, parse_money(p.price), parse_money(p.market_value))
        for a in extracted.accounts  # type: ignore[attr-defined]
        for p in a.positions
        if p.symbol == "AAA"
    ]
    assert len(rows) == 2
    prices = {r[1] for r in rows}
    assert len(prices) == 2, "the same symbol legitimately holds two prices"
    # Aggregate by value, never by share count.
    assert sum(r[2] for r in rows if r[2]) == Decimal("40100.00")
