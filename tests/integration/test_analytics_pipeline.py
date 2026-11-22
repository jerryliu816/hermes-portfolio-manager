"""Analytics over a real import, end to end.

Runs against the anonymized corpus, which preserves every awkward property of
the real files -- so these assertions are about how the analytics behave on
messy data rather than on a tidy fixture.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from portfolio_manager.analytics import (
    by_account,
    by_institution,
    by_tax_treatment,
    concentration,
    divergent_price_securities,
    embedded_gains,
    gains_by_tax_treatment,
    load_portfolio,
    security_exposures,
    summarize,
)
from portfolio_manager.config import Settings
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.services.importer import import_document

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)


@pytest.fixture
def view(session: Session, tmp_path: Path) -> object:
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]
    result = import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)
    return load_portfolio(session, result.snapshot.snapshot)


def test_the_summary_totals_match_the_import(view: object) -> None:
    summary = summarize(view)  # type: ignore[arg-type]
    assert summary.account_count == 10
    assert summary.position_count == 80
    assert summary.security_count < summary.position_count, (
        "securities held in several accounts must not be double-counted"
    )
    assert summary.total_value > Decimal("0")
    assert summary.skipped_account_count == 4


def test_most_of_the_value_is_reported_as_stale(view: object) -> None:
    """The headline finding: 6 of 10 accounts have an unknown as-of date."""
    summary = summarize(view)  # type: ignore[arg-type]
    assert summary.quality.stale_fraction > Decimal("0.5")
    assert len(summary.quality.stale_accounts) == 6
    assert any("as-of date is unknown" in c for c in summary.quality.caveats())
    assert not summary.quality.is_fully_trusted


def test_unclassified_accounts_are_held_out_of_tax_analysis(view: object) -> None:
    breakdown = by_tax_treatment(view)  # type: ignore[arg-type]
    assert breakdown.excluded_value > Decimal("0")
    assert "unknown" not in {s.key for s in breakdown.slices}
    # Percentages must still sum to one across what IS known.
    assert sum(s.fraction for s in breakdown.slices) == Decimal("1")


def test_allocation_fractions_sum_to_one(view: object) -> None:
    for breakdown in (by_institution(view), by_account(view)):  # type: ignore[arg-type]
        assert sum(s.fraction for s in breakdown.slices) == Decimal("1")


def test_closed_holdings_are_excluded_from_every_denominator(view: object) -> None:
    summary = summarize(view)  # type: ignore[arg-type]
    assert summary.closed_position_count >= 6
    counted = sum(s.position_count for s in by_account(view).slices)  # type: ignore[arg-type]
    assert counted == summary.position_count


def test_divergent_prices_are_found_in_the_real_shape(view: object) -> None:
    """Six symbols are priced two ways across accounts in the corpus."""
    divergent = divergent_price_securities(view)  # type: ignore[arg-type]
    assert len(divergent) >= 6
    for exposure in divergent:
        low, high = exposure.price_range  # type: ignore[misc]
        assert low != high


def test_exposure_aggregates_value_but_not_shares(view: object) -> None:
    exposures = security_exposures(view)  # type: ignore[arg-type]
    multi = [e for e in exposures if e.account_count > 1]
    assert multi, "the corpus holds several securities in more than one account"
    for exposure in multi:
        assert exposure.total_value == sum(h.value for h in exposure.holdings)
        assert not hasattr(exposure, "total_quantity")


def test_gains_disclose_their_partial_coverage(view: object) -> None:
    """25 of 86 holdings report no cost basis."""
    gains = embedded_gains(view)  # type: ignore[arg-type]
    assert gains.coverage.uncovered_value > Decimal("0")
    assert gains.coverage.covered_fraction < Decimal("1")
    assert gains.coverage.caveat() is not None


def test_gains_by_tax_treatment_exclude_the_unclassified(view: object) -> None:
    result = gains_by_tax_treatment(view)  # type: ignore[arg-type]
    assert "unknown" not in result.by_treatment
    assert result.unclassified_value > Decimal("0")


def test_concentration_is_computable_on_the_real_shape(view: object) -> None:
    result = concentration(view)  # type: ignore[arg-type]
    assert Decimal("0") < result.top_1_fraction <= Decimal("1")
    assert result.top_1_fraction <= result.top_5_fraction <= result.top_10_fraction
    assert Decimal("0") < result.hhi <= Decimal("1")


def test_every_analytic_returns_decimals_never_floats(view: object) -> None:
    """A float anywhere in a money path is a correctness bug."""
    summary = summarize(view)  # type: ignore[arg-type]
    for value in (
        summary.total_value,
        summary.holdings_value,
        summary.cash_value,
        summary.quality.stale_fraction,
        concentration(view).hhi,  # type: ignore[arg-type]
        embedded_gains(view).unrealized_gain,  # type: ignore[arg-type]
    ):
        assert isinstance(value, Decimal)
    for slice_ in by_account(view).slices:  # type: ignore[arg-type]
        assert isinstance(slice_.value, Decimal)
        assert isinstance(slice_.fraction, Decimal)
