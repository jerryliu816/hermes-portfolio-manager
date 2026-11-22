"""Analytics, tested against hand-built views.

Several of these guard rules that would produce a plausible-looking wrong
number if they regressed, which is the dangerous kind of bug: nothing crashes,
the dashboard just lies.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from portfolio_manager.analytics import (
    by_asset_class,
    by_institution,
    by_tax_treatment,
    cash_equivalents,
    compare_snapshots,
    concentration,
    divergent_price_securities,
    embedded_gains,
    find_exposure,
    gains_by_tax_treatment,
    summarize,
)
from portfolio_manager.analytics.view import AccountRow, HoldingRow, PortfolioView

ZERO = Decimal("0")


def _holding(**kw: object) -> HoldingRow:
    base: dict[str, object] = {
        "account_id": "a1",
        "account_name": "Taxable",
        "institution": "Fidelity",
        "account_type": "brokerage",
        "tax_treatment": "taxable",
        "is_aggregated": False,
        "account_is_stale": False,
        "supplied_by": "Fidelity",
        "security_id": "s1",
        "symbol": "AAA",
        "security_name": "Alpha ETF",
        "security_type": "etf",
        "asset_class": "us_equity",
        "quantity": Decimal("10"),
        "price": Decimal("100"),
        "market_value": Decimal("1000"),
        "cost_basis": Decimal("600"),
        "cost_basis_source": "reported",
        "unrealized_gain_loss": Decimal("400"),
        "is_closed": False,
        "value_source": "primary",
    }
    base.update(kw)
    return HoldingRow(**base)  # type: ignore[arg-type]


def _account(**kw: object) -> AccountRow:
    base: dict[str, object] = {
        "account_id": "a1",
        "display_name": "Taxable",
        "institution": "Fidelity",
        "account_type": "brokerage",
        "tax_treatment": "taxable",
        "is_aggregated": False,
        "is_stale": False,
        "staleness_reason": None,
        "supplied_by": "Fidelity",
        "as_of_date": dt.date(2026, 8, 26),
        "as_of_confidence": "stated",
        "holdings_value": Decimal("1000"),
        "cash_balance": ZERO,
        "position_count": 1,
    }
    base.update(kw)
    return AccountRow(**base)  # type: ignore[arg-type]


def _view(holdings: list[HoldingRow], accounts: list[AccountRow], **kw: object) -> PortfolioView:
    base: dict[str, object] = {
        "snapshot_id": "snap",
        "as_of_date": dt.date(2026, 8, 26),
        "completeness": "complete",
        "status": "imported",
        "holdings": tuple(holdings),
        "accounts": tuple(accounts),
    }
    base.update(kw)
    return PortfolioView(**base)  # type: ignore[arg-type]


# --- the rule that shares are never summed -------------------------------


def test_share_counts_are_never_summed_across_accounts() -> None:
    """The same ETF pre-split in one source and post-split in another.

    1348.207 + 141.637 shares is a number that has never existed. Values add;
    quantities stay per account.
    """
    view = _view(
        [
            _holding(
                account_id="a1",
                quantity=Decimal("1348.207"),
                price=Decimal("117.51"),
                market_value=Decimal("158427.80"),
            ),
            _holding(
                account_id="a2",
                account_name="IRA",
                quantity=Decimal("141.637"),
                price=Decimal("755.28"),
                market_value=Decimal("106975.82"),
            ),
        ],
        [
            _account(holdings_value=Decimal("158427.80")),
            _account(account_id="a2", display_name="IRA", holdings_value=Decimal("106975.82")),
        ],
    )
    exposure = find_exposure(view, "AAA")
    assert exposure is not None
    assert not hasattr(exposure, "total_quantity"), "shares must not be aggregated"
    assert exposure.total_value == Decimal("265403.62")
    assert {h.quantity for h in exposure.holdings} == {
        Decimal("1348.207"),
        Decimal("141.637"),
    }


def test_divergent_prices_are_surfaced_not_averaged() -> None:
    view = _view(
        [
            _holding(account_id="a1", price=Decimal("117.51")),
            _holding(account_id="a2", price=Decimal("755.28")),
        ],
        [_account(), _account(account_id="a2")],
    )
    exposure = find_exposure(view, "AAA")
    assert exposure is not None and exposure.has_divergent_prices
    assert exposure.price_range == (Decimal("117.51"), Decimal("755.28"))
    assert [e.symbol for e in divergent_price_securities(view)] == ["AAA"]


# --- closed and valueless holdings ---------------------------------------


def test_closed_positions_do_not_dilute_allocation() -> None:
    """The real 529 accounts carry six quantity-zero rows."""
    view = _view(
        [
            _holding(market_value=Decimal("1000")),
            _holding(
                security_id="s2",
                symbol="BBB",
                asset_class="fixed_income",
                quantity=ZERO,
                market_value=None,
                is_closed=True,
            ),
        ],
        [_account()],
    )
    breakdown = by_asset_class(view)
    assert breakdown.total == Decimal("1000")
    assert [s.key for s in breakdown.slices] == ["us_equity"]
    assert breakdown.slices[0].fraction == Decimal("1")


def test_valueless_holdings_are_excluded_from_denominators() -> None:
    view = _view(
        [_holding(market_value=Decimal("1000")), _holding(security_id="s2", market_value=None)],
        [_account()],
    )
    assert by_asset_class(view).total == Decimal("1000")


# --- unknown tax treatment ------------------------------------------------


def test_unknown_tax_treatment_is_excluded_and_reported() -> None:
    """Folding unknown into taxable would look complete and be wrong."""
    view = _view(
        [
            _holding(market_value=Decimal("700")),
            _holding(account_id="a2", tax_treatment="unknown", market_value=Decimal("300")),
        ],
        [
            _account(holdings_value=Decimal("700")),
            _account(
                account_id="a2",
                display_name="Opaque",
                tax_treatment="unknown",
                holdings_value=Decimal("300"),
            ),
        ],
    )
    breakdown = by_tax_treatment(view)
    assert [s.key for s in breakdown.slices] == ["taxable"]
    assert breakdown.total == Decimal("700")
    assert breakdown.excluded_value == Decimal("300")
    assert breakdown.excluded_fraction == Decimal("0.3")
    assert "no confirmed tax treatment" in breakdown.excluded_label


def test_cash_inherits_its_account_tax_treatment() -> None:
    view = _view(
        [_holding(market_value=Decimal("1000"), tax_treatment="tax_deferred")],
        [_account(tax_treatment="tax_deferred", cash_balance=Decimal("500"))],
    )
    breakdown = by_tax_treatment(view)
    assert breakdown.get("tax_deferred").value == Decimal("1500")  # type: ignore[union-attr]


# --- gains coverage -------------------------------------------------------


def test_gains_report_the_coverage_they_are_based_on() -> None:
    """29% of real holdings have no basis; a bare total would mislead."""
    view = _view(
        [
            _holding(market_value=Decimal("1000"), cost_basis=Decimal("600")),
            _holding(
                security_id="s2",
                market_value=Decimal("1000"),
                cost_basis=None,
                cost_basis_source="unknown",
            ),
        ],
        [_account(holdings_value=Decimal("2000"))],
    )
    gains = embedded_gains(view)
    assert gains.unrealized_gain == Decimal("400")
    assert gains.coverage.covered_fraction == Decimal("0.5")
    assert "50.0%" in (gains.coverage.caveat() or "")


def test_full_coverage_produces_no_caveat() -> None:
    view = _view([_holding()], [_account()])
    assert embedded_gains(view).coverage.caveat() is None


def test_gains_are_split_by_tax_treatment() -> None:
    """A gain in a taxable account is a different fact from one in a Roth."""
    view = _view(
        [
            _holding(market_value=Decimal("1000"), cost_basis=Decimal("400")),
            _holding(
                account_id="a2",
                tax_treatment="tax_free",
                market_value=Decimal("1000"),
                cost_basis=Decimal("900"),
            ),
        ],
        [_account(), _account(account_id="a2", tax_treatment="tax_free")],
    )
    result = gains_by_tax_treatment(view)
    assert result.by_treatment["taxable"].unrealized_gain == Decimal("600")
    assert result.by_treatment["tax_free"].unrealized_gain == Decimal("100")


def test_return_fraction_is_not_called_a_return() -> None:
    """It has no cash flows or time weighting behind it."""
    view = _view([_holding()], [_account()])
    gains = embedded_gains(view)
    assert gains.return_fraction is not None
    assert not hasattr(gains, "annualized_return")


# --- summary and data quality --------------------------------------------


def test_summary_reports_the_stale_fraction() -> None:
    """On the real corpus this starts at 61% of value."""
    view = _view(
        [_holding(), _holding(account_id="a2", account_is_stale=True)],
        [
            _account(holdings_value=Decimal("400")),
            _account(
                account_id="a2",
                display_name="Aggregated",
                is_aggregated=True,
                is_stale=True,
                holdings_value=Decimal("600"),
            ),
        ],
    )
    summary = summarize(view)
    assert summary.total_value == Decimal("1000")
    assert summary.quality.stale_fraction == Decimal("0.6")
    assert "Aggregated" in summary.quality.stale_accounts
    assert any("as-of date is unknown" in c for c in summary.quality.caveats())
    assert not summary.quality.is_fully_trusted


def test_summary_counts_securities_separately_from_positions() -> None:
    view = _view(
        [_holding(account_id="a1"), _holding(account_id="a2")],
        [_account(), _account(account_id="a2")],
    )
    summary = summarize(view)
    assert summary.position_count == 2
    assert summary.security_count == 1


def test_summary_includes_cash_in_the_total() -> None:
    view = _view([_holding()], [_account(cash_balance=Decimal("500"))])
    summary = summarize(view)
    assert summary.holdings_value == Decimal("1000")
    assert summary.cash_value == Decimal("500")
    assert summary.total_value == Decimal("1500")


def test_summary_warns_about_incompleteness_and_skips() -> None:
    view = _view(
        [_holding()],
        [_account()],
        completeness="partial",
        skipped_accounts=({"display_name": "IIA 6QQ-80000", "reason": "zero_reported_value"},),
    )
    summary = summarize(view)
    assert any("incomplete" in w for w in summary.warnings)
    assert any("IIA 6QQ-80000" in w for w in summary.warnings)


def test_a_clean_portfolio_reports_full_trust() -> None:
    view = _view([_holding()], [_account()])
    assert summarize(view).quality.is_fully_trusted
    assert summarize(view).quality.caveats() == []


# --- concentration --------------------------------------------------------


def test_concentration_flags_oversized_single_holdings() -> None:
    view = _view(
        [
            _holding(security_id="s1", symbol="BIG", market_value=Decimal("600")),
            _holding(security_id="s2", symbol="SM1", market_value=Decimal("200")),
            _holding(security_id="s3", symbol="SM2", market_value=Decimal("200")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    result = concentration(view, threshold=Decimal("0.25"))
    assert result.top_1_fraction == Decimal("0.6")
    assert [e.symbol for e in result.single_holdings_over_threshold] == ["BIG"]
    assert result.hhi == Decimal("0.44")


def test_institution_split_includes_cash() -> None:
    view = _view(
        [_holding()],
        [_account(holdings_value=Decimal("1000"), cash_balance=Decimal("200"))],
    )
    assert by_institution(view).get("Fidelity").value == Decimal("1200")  # type: ignore[union-attr]


# --- comparison -----------------------------------------------------------


def test_comparison_never_calls_the_change_a_return() -> None:
    before = _view([_holding(market_value=Decimal("1000"))], [_account()])
    after = _view(
        [_holding(market_value=Decimal("1200"))],
        [_account(holdings_value=Decimal("1200"))],
    )
    result = compare_snapshots(before, after)
    assert result.value_change == Decimal("200")
    assert "value" in result.headline()
    assert "return" not in result.headline().lower()


def test_comparison_identifies_new_and_removed_positions() -> None:
    before = _view(
        [_holding(security_id="s1", symbol="OLD")],
        [_account()],
    )
    after = _view(
        [_holding(security_id="s2", symbol="NEW")],
        [_account()],
    )
    result = compare_snapshots(before, after)
    assert [c.symbol for c in result.new_positions] == ["NEW"]
    assert [c.symbol for c in result.removed_positions] == ["OLD"]


def test_a_source_change_is_reported_as_a_confounder() -> None:
    """Part of the difference is better data, not market movement."""
    before = _view([_holding()], [_account(supplied_by="Fidelity")])
    after = _view(
        [_holding(market_value=Decimal("1500"))],
        [_account(supplied_by="Vanguard", holdings_value=Decimal("1500"))],
    )
    result = compare_snapshots(before, after)
    assert any("source supplying" in c for c in result.confounders)
    assert any("better data rather than market movement" in c for c in result.confounders)


def test_stale_accounts_are_reported_as_a_confounder() -> None:
    before = _view([_holding()], [_account(is_stale=True)])
    after = _view([_holding()], [_account(is_stale=True)])
    result = compare_snapshots(before, after)
    assert any("unknown as-of date" in c for c in result.confounders)


def test_allocation_drift_is_expressed_in_fractions() -> None:
    before = _view(
        [
            _holding(security_id="s1", asset_class="us_equity", market_value=Decimal("500")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("500")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    after = _view(
        [
            _holding(security_id="s1", asset_class="us_equity", market_value=Decimal("800")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("200")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    drift = {d.key: d.drift for d in compare_snapshots(before, after).drift}
    assert drift["us_equity"] == Decimal("0.3")
    assert drift["fixed_income"] == Decimal("-0.3")


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-100")])
def test_analytics_survive_degenerate_portfolios(value: Decimal) -> None:
    view = _view([], [_account(holdings_value=value)])
    summary = summarize(view)
    assert summary.position_count == 0
    assert by_asset_class(view).slices == ()
    assert embedded_gains(view).unrealized_gain == ZERO
    assert concentration(view).hhi == ZERO


def test_a_zero_value_bucket_is_not_an_allocation_slice() -> None:
    """It renders as an invisible slice with a legend entry saying 0%.

    Caught by looking at the dashboard, where an empty "Unknown" slice sat in
    the legend at $0.00 after every holding had been classified.
    """
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("1000")),
            _holding(security_id="s2", asset_class="unknown", market_value=Decimal("0")),
        ],
        [_account()],
    )
    assert [s.key for s in by_asset_class(view).slices] == ["us_equity"]


# --- cash and equivalents, split by tax side -----------------------------


def test_cash_counts_both_shapes_it_arrives_in() -> None:
    """Vanguard reports a settlement balance; Fidelity lists a money-market row.

    Counting only one of them is the bug this exists to prevent: the summary's
    `cash_value` sees settlement balances alone, so it understated real cash by
    the whole money-market position.
    """
    view = _view(
        [
            _holding(
                account_id="a1",
                asset_class="cash_equivalent",
                security_id="mm",
                symbol="FZDXX",
                market_value=Decimal("2000"),
            )
        ],
        [_account(account_id="a1", cash_balance=Decimal("500"))],
    )
    cash = cash_equivalents(view)
    assert cash.settlement_value == Decimal("500")
    assert cash.holdings_value == Decimal("2000")
    assert cash.total_value == Decimal("2500")


def test_cash_separates_taxable_from_sheltered() -> None:
    view = _view(
        [
            _holding(
                account_id="a1",
                tax_treatment="taxable",
                asset_class="cash_equivalent",
                security_id="mm1",
                market_value=Decimal("3000"),
            ),
            _holding(
                account_id="a2",
                tax_treatment="tax_deferred",
                asset_class="cash_equivalent",
                security_id="mm2",
                market_value=Decimal("1000"),
            ),
            _holding(
                account_id="a3",
                tax_treatment="tax_free",
                asset_class="cash_equivalent",
                security_id="mm3",
                market_value=Decimal("500"),
            ),
            # Not cash. It must not reach either bucket.
            _holding(
                account_id="a1",
                tax_treatment="taxable",
                asset_class="us_equity",
                security_id="eq",
                market_value=Decimal("9000"),
            ),
        ],
        [
            _account(account_id="a1", tax_treatment="taxable", cash_balance=Decimal("200")),
            _account(account_id="a2", tax_treatment="tax_deferred"),
            _account(account_id="a3", tax_treatment="tax_free"),
        ],
    )
    cash = cash_equivalents(view)
    taxable = cash.get("taxable")
    sheltered = cash.get("tax_sheltered")
    assert taxable is not None and sheltered is not None
    assert taxable.total_value == Decimal("3200")
    assert sheltered.total_value == Decimal("1500")
    # tax_deferred and tax_free are one side of the line but stay nameable.
    assert sheltered.treatments == ("tax_deferred", "tax_free")
    assert cash.total_value == Decimal("4700")


def test_cash_in_an_unclassified_account_is_its_own_bucket() -> None:
    """Folding it into either side would answer a question we cannot answer."""
    view = _view(
        [
            _holding(
                account_id="a9",
                tax_treatment="unknown",
                asset_class="cash_equivalent",
                security_id="tbill",
                symbol=None,
                market_value=Decimal("750"),
            )
        ],
        [_account(account_id="a9", tax_treatment="unknown", holdings_value=Decimal("750"))],
    )
    cash = cash_equivalents(view)
    assert [b.key for b in cash.buckets] == ["unknown"]
    assert cash.unconfirmed_value == Decimal("750")
    assert cash.taxable_value == ZERO


def test_a_closed_cash_position_contributes_nothing() -> None:
    view = _view(
        [
            _holding(
                asset_class="cash_equivalent",
                security_id="mm",
                market_value=Decimal("100"),
                is_closed=True,
            )
        ],
        [_account(holdings_value=ZERO)],
    )
    assert cash_equivalents(view).buckets == ()
    assert cash_equivalents(view).total_value == ZERO


def test_cash_fractions_are_of_cash_and_of_the_portfolio() -> None:
    view = _view(
        [
            _holding(
                account_id="a1",
                asset_class="cash_equivalent",
                security_id="mm",
                market_value=Decimal("250"),
            ),
            _holding(account_id="a1", security_id="eq", market_value=Decimal("750")),
        ],
        [_account(account_id="a1", holdings_value=Decimal("1000"))],
    )
    cash = cash_equivalents(view)
    assert cash.buckets[0].fraction == Decimal("1")
    assert cash.fraction_of_portfolio == Decimal("0.25")
