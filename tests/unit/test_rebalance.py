"""Rebalancing arithmetic, tested against hand-built views.

Every figure here is one an advisor would otherwise be tempted to compute
itself. The tests that matter most guard the cases where a wrong answer would
look confident: a missing basis reported as a zero gain, an unconfirmed account
treated as taxable, a share count summed across a split.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_manager.analytics.rebalance import (
    TaxStatus,
    Trade,
    TradeAction,
    ViolationKind,
    simulate,
    suggest_rebalance,
    target_gaps,
)
from portfolio_manager.analytics.view import AccountRow, HoldingRow, PortfolioView
from portfolio_manager.models.enums import AssetClass
from portfolio_manager.schemas.policy import PolicyDocument

ZERO = Decimal("0")
D = Decimal


def _holding(**kw: object) -> HoldingRow:
    base: dict[str, object] = {
        "account_id": "tax",
        "account_name": "Brokerage",
        "institution": "Inst A",
        "account_type": "brokerage",
        "tax_treatment": "taxable",
        "is_aggregated": False,
        "account_is_stale": False,
        "supplied_by": "Inst A",
        "security_id": "s-eq",
        "symbol": "EQX",
        "security_name": "Equity Index Fund",
        "security_type": "etf",
        "asset_class": "us_equity",
        "quantity": D("10"),
        "price": D("100"),
        "market_value": D("1000"),
        "cost_basis": D("600"),
        "cost_basis_source": "reported",
        "unrealized_gain_loss": D("400"),
        "is_closed": False,
        "value_source": "primary",
    }
    base.update(kw)
    return HoldingRow(**base)  # type: ignore[arg-type]


def _account(**kw: object) -> AccountRow:
    base: dict[str, object] = {
        "account_id": "tax",
        "display_name": "Brokerage",
        "institution": "Inst A",
        "account_type": "brokerage",
        "tax_treatment": "taxable",
        "is_aggregated": False,
        "is_stale": False,
        "staleness_reason": None,
        "supplied_by": "Inst A",
        "as_of_date": dt.date(2026, 8, 26),
        "as_of_confidence": "stated",
        "holdings_value": D("1000"),
        "cash_balance": ZERO,
        "position_count": 1,
    }
    base.update(kw)
    return AccountRow(**base)  # type: ignore[arg-type]


def _view(holdings: list[HoldingRow], accounts: list[AccountRow]) -> PortfolioView:
    return PortfolioView(
        snapshot_id="snap",
        as_of_date=dt.date(2026, 8, 26),
        completeness="complete",
        status="imported",
        holdings=tuple(holdings),
        accounts=tuple(accounts),
    )


def _policy(**kw: object) -> PolicyDocument:
    data: dict[str, object] = {
        "targets": {"us_equity": "0.6", "fixed_income": "0.4"},
        "rebalancing": {"min_trade_value": "100"},
    }
    data.update(kw)
    return PolicyDocument.model_validate(data)


def _two_account_view(ira_cash: Decimal = ZERO, tax_cash: Decimal = ZERO) -> PortfolioView:
    """Taxable: 6000 equity. IRA: 2000 equity + 2000 bonds. 80/20 against 60/40."""
    return _view(
        [
            _holding(market_value=D("6000"), cost_basis=D("4000")),
            _holding(
                account_id="ira",
                account_name="IRA",
                tax_treatment="tax_deferred",
                market_value=D("2000"),
                cost_basis=D("1500"),
            ),
            _holding(
                account_id="ira",
                account_name="IRA",
                tax_treatment="tax_deferred",
                security_id="s-bd",
                symbol="BNDX",
                security_name="Bond Index Fund",
                asset_class="fixed_income",
                market_value=D("2000"),
                cost_basis=D("2000"),
            ),
        ],
        [
            _account(holdings_value=D("6000"), cash_balance=tax_cash),
            _account(
                account_id="ira",
                display_name="IRA",
                tax_treatment="tax_deferred",
                holdings_value=D("4000"),
                cash_balance=ira_cash,
            ),
        ],
    )


def _kinds(result: object) -> set[ViolationKind]:
    return {v.kind for v in result.violations}  # type: ignore[attr-defined]


# --- target gaps -----------------------------------------------------------


def test_gaps_are_dollars_against_classified_value() -> None:
    gaps = target_gaps(_two_account_view(), _policy())
    equity = gaps.get("us_equity")
    bonds = gaps.get("fixed_income")
    assert equity is not None and bonds is not None
    assert equity.gap == D("2000.0")
    assert bonds.gap == D("-2000.0")
    assert equity.direction == "overweight"
    assert sum((g.gap for g in gaps.gaps), ZERO) == ZERO


def test_unclassified_value_is_excluded_and_reported() -> None:
    view = _two_account_view()
    view = _view(
        [*view.holdings, _holding(security_id="s-x", symbol=None, asset_class="unknown")],
        list(view.accounts),
    )
    gaps = target_gaps(view, _policy())
    assert gaps.unclassified_value == D("1000")
    assert gaps.classified_value == D("10000")
    assert gaps.get("unknown") is None


def test_proposed_targets_replace_the_policy_and_are_validated() -> None:
    proposed = {AssetClass.US_EQUITY: D("0.8"), AssetClass.FIXED_INCOME: D("0.2")}
    gaps = target_gaps(_two_account_view(), proposed, policy=_policy())
    assert gaps.targets_proposed
    assert all(g.gap == ZERO for g in gaps.gaps)

    with pytest.raises(ValidationError):
        target_gaps(_two_account_view(), {AssetClass.US_EQUITY: D("0.5")})
    with pytest.raises(ValueError, match="unknown"):
        target_gaps(_two_account_view(), {"unknown": D("1")})


def test_a_held_class_without_a_target_is_reported_not_hidden() -> None:
    view = _two_account_view()
    view = _view(
        [*view.holdings, _holding(security_id="s-re", symbol="REIT", asset_class="real_estate")],
        list(view.accounts),
    )
    gap = target_gaps(view, _policy()).get("real_estate")
    assert gap is not None and gap.untargeted and gap.gap == D("1000")


def test_cash_outside_holdings_is_reported_separately() -> None:
    gaps = target_gaps(_two_account_view(tax_cash=D("500")), _policy())
    assert gaps.uninvested_cash == D("500")
    assert gaps.classified_value == D("10000")
    assert any("outside holdings" in c for c in gaps.caveats)


# --- simulation: violations ------------------------------------------------


def test_oversell_is_flagged_and_capped_at_the_position() -> None:
    trades = [Trade("ira", TradeAction.SELL, "EQX", D("5000"))]
    result = simulate(_two_account_view(), trades, _policy())
    assert ViolationKind.OVERSELL in _kinds(result)
    assert result.trades[0].applied_amount == D("2000")
    assert not result.is_feasible


def test_buy_that_overdraws_the_account_is_flagged() -> None:
    trades = [Trade("ira", TradeAction.BUY, "BNDX", D("500"))]
    result = simulate(_two_account_view(), trades, _policy())
    assert ViolationKind.CASH_NEGATIVE in _kinds(result)
    (flow,) = result.cash
    assert flow.ending_cash == D("-500")


def test_trade_below_the_policy_minimum_is_flagged() -> None:
    trades = [
        Trade("ira", TradeAction.SELL, "EQX", D("50")),
        Trade("ira", TradeAction.BUY, "BNDX", D("50")),
    ]
    result = simulate(_two_account_view(), trades, _policy())
    below = [v for v in result.violations if v.kind is ViolationKind.BELOW_MIN_TRADE]
    assert [v.trade_index for v in below] == [0, 1]
    # Still applied: flagged, not silently dropped.
    assert result.trades[0].applied_amount == D("50")


def test_a_breached_constraint_is_a_violation() -> None:
    policy = _policy(
        constraints=[
            {"id": "eq-cap", "kind": "max_asset_class", "subject": "us_equity", "limit": "0.85"}
        ]
    )
    trades = [
        Trade("ira", TradeAction.SELL, "BNDX", D("2000")),
        Trade("ira", TradeAction.BUY, "EQX", D("2000")),
    ]
    result = simulate(_two_account_view(), trades, policy)
    assert ViolationKind.CONSTRAINT_BREACH in _kinds(result)
    assert result.is_feasible, "a policy breach is reported apart from an impossible trade"


def test_selling_what_the_account_does_not_hold_is_rejected() -> None:
    trades = [Trade("tax", TradeAction.SELL, "BNDX", D("500"))]
    result = simulate(_two_account_view(), trades, _policy())
    assert ViolationKind.NOT_HELD in _kinds(result)
    assert result.trades[0].applied_amount == ZERO


# --- simulation: results ---------------------------------------------------


def test_a_balanced_swap_reaches_target_and_reuses_assess() -> None:
    trades = [
        Trade("ira", TradeAction.SELL, "EQX", D("2000")),
        Trade("ira", TradeAction.BUY, "BNDX", D("2000")),
    ]
    result = simulate(_two_account_view(), trades, _policy())
    assert result.violations == ()
    assert result.assessment_after.is_compliant
    assert not result.assessment_before.is_compliant
    bonds = result.by_asset_class.get("fixed_income")
    assert bonds is not None and bonds.value == D("4000")
    assert result.cash[0].net_change == ZERO
    assert result.trades[0].tax is not None
    assert result.trades[0].tax.status is TaxStatus.NO_REALIZATION_EVENT
    assert result.trades[0].tax.estimated_gain is None


def test_taxable_sale_uses_proportional_basis() -> None:
    trades = [Trade("tax", TradeAction.SELL, "EQX", D("3000"))]
    result = simulate(_two_account_view(), trades, _policy())
    tax = result.trades[0].tax
    assert tax is not None
    assert tax.status is TaxStatus.ESTIMATED
    # Half the position, half of its 4000 basis.
    assert tax.estimated_gain == D("1000")
    assert tax.basis_coverage == D("1")
    assert result.tax.estimated_gain == D("1000")
    assert result.cash[0].ending_cash == D("3000")


def test_missing_basis_is_none_with_a_reason_never_zero() -> None:
    view = _view(
        [_holding(cost_basis=None, cost_basis_source="unknown", unrealized_gain_loss=None)],
        [_account()],
    )
    result = simulate(view, [Trade("tax", TradeAction.SELL, "EQX", D("500"))], _policy())
    tax = result.trades[0].tax
    assert tax is not None
    assert tax.status is TaxStatus.BASIS_MISSING
    assert tax.estimated_gain is None
    assert tax.basis_coverage == ZERO
    assert "no cost basis" in tax.reason
    assert result.tax.estimated_gain is None
    assert result.tax.caveat is not None


def test_unconfirmed_tax_treatment_is_unknown() -> None:
    view = _view(
        [_holding(tax_treatment="unknown")],
        [_account(tax_treatment="unknown")],
    )
    result = simulate(view, [Trade("tax", TradeAction.SELL, "EQX", D("500"))], _policy())
    tax = result.trades[0].tax
    assert tax is not None
    assert tax.status is TaxStatus.UNKNOWN
    assert tax.estimated_gain is None
    assert result.tax.unknown_treatment_sale_value == D("500")


def test_same_security_pre_and_post_split_never_sums_shares() -> None:
    """One ETF, pre-split in one account and post-split in another.

    The trade is dollars. The untouched account keeps its own share count; the
    traded one reports none rather than one derived from a possibly stale price.
    """
    view = _view(
        [
            _holding(quantity=D("1348.207"), price=D("117.51"), market_value=D("158427.80")),
            _holding(
                account_id="ira",
                account_name="IRA",
                tax_treatment="tax_deferred",
                quantity=D("141.637"),
                price=D("755.28"),
                market_value=D("106975.82"),
            ),
        ],
        [
            _account(holdings_value=D("158427.80"), cash_balance=D("5000")),
            _account(
                account_id="ira",
                display_name="IRA",
                tax_treatment="tax_deferred",
                holdings_value=D("106975.82"),
            ),
        ],
    )
    trades = [
        Trade("ira", TradeAction.SELL, "EQX", D("6975.82")),
        Trade("tax", TradeAction.BUY, "eqx", D("5000")),
    ]
    result = simulate(view, trades, _policy())
    rows = {r.account_id: r for r in result.projected_view.holdings}
    assert rows["ira"].market_value == D("100000.00")
    assert rows["ira"].quantity is None
    assert rows["tax"].market_value == D("163427.80")
    assert rows["tax"].quantity is None
    assert {r.security_id for r in rows.values()} == {"s-eq"}
    for outcome in result.trades:
        assert not hasattr(outcome, "quantity")
    assert not hasattr(result, "total_quantity")
    equity = result.by_asset_class.get("us_equity")
    assert equity is not None and equity.value == D("263427.80")


def test_buying_an_unheld_security_needs_its_asset_class() -> None:
    view = _two_account_view(ira_cash=D("1000"))
    missing = simulate(view, [Trade("ira", TradeAction.BUY, "NEWB", D("1000"))], _policy())
    assert ViolationKind.MISSING_ASSET_CLASS in _kinds(missing)
    assert missing.trades[0].applied_amount == ZERO

    result = simulate(
        view,
        [Trade("ira", TradeAction.BUY, "NEWB", D("1000"), AssetClass.FIXED_INCOME)],
        _policy(),
    )
    assert result.violations == ()
    bonds = result.by_asset_class.get("fixed_income")
    assert bonds is not None and bonds.value == D("3000")
    new = next(r for r in result.projected_view.holdings if r.security_name == "NEWB")
    assert new.price is None and new.quantity is None
    assert result.cash[0].ending_cash == ZERO


def test_a_security_without_a_ticker_is_matched_by_its_description() -> None:
    view = _view(
        [
            _holding(
                symbol=None, security_name="Stable Value Fund Class A", asset_class="fixed_income"
            )
        ],
        [_account()],
    )
    result = simulate(
        view, [Trade("tax", TradeAction.SELL, "STABLE VALUE FUND, CLASS A", D("200"))], _policy()
    )
    assert result.trades[0].applied_amount == D("200")


def test_proposed_targets_change_the_drift_but_not_the_trades() -> None:
    proposed = {AssetClass.US_EQUITY: D("0.8"), AssetClass.FIXED_INCOME: D("0.2")}
    result = simulate(_two_account_view(), [], _policy(), targets=proposed)
    assert result.assessment_after.is_compliant
    assert result.gaps_after.targets_proposed
    assert not simulate(_two_account_view(), [], _policy()).assessment_after.is_compliant


def test_money_is_never_a_float() -> None:
    with pytest.raises(TypeError):
        Trade("tax", TradeAction.SELL, "EQX", 100.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Trade.from_mapping({"account_id": "tax", "action": "sell", "security": "EQX", "amount": 5})
    trade = Trade.from_mapping(
        {
            "account_id": "tax",
            "action": "buy",
            "security": "X",
            "amount": "10.50",
            "asset_class": "us_equity",
        }
    )
    assert trade.amount == D("10.50") and trade.asset_class is AssetClass.US_EQUITY


# --- the baseline suggestion -----------------------------------------------


def test_suggestion_closes_gaps_inside_the_ira_before_touching_taxable() -> None:
    suggestion = suggest_rebalance(_two_account_view(), _policy())
    assert suggestion.needs_rebalance
    assert {m.account_id for m in suggestion.moves} == {"ira"}
    sell = next(m for m in suggestion.moves if m.action is TradeAction.SELL)
    buy = next(m for m in suggestion.moves if m.action is TradeAction.BUY)
    assert (sell.asset_class, sell.amount) == ("us_equity", D("2000.0"))
    assert (buy.asset_class, buy.amount) == ("fixed_income", D("2000.0"))
    assert sell.tax is not None and sell.tax.status is TaxStatus.NO_REALIZATION_EVENT
    assert suggestion.assessment_after.is_compliant
    assert all(g.gap == ZERO for g in suggestion.gaps_after.gaps)


def test_suggestion_spends_taxable_cash_before_selling() -> None:
    view = _two_account_view(tax_cash=D("10000"))
    suggestion = suggest_rebalance(view, _policy())
    assert all(m.action is TradeAction.BUY for m in suggestion.moves)
    assert suggestion.tax.estimated_gain is None
    assert suggestion.assessment_after.is_compliant


def test_suggestion_sells_taxable_only_when_it_must_and_estimates_the_gain() -> None:
    """All equity sits in the taxable account, so it has to be sold there."""
    view = _view(
        [
            _holding(market_value=D("8000"), cost_basis=D("6000")),
            _holding(
                account_id="ira",
                account_name="IRA",
                tax_treatment="tax_deferred",
                security_id="s-bd",
                symbol="BNDX",
                security_name="Bond Index Fund",
                asset_class="fixed_income",
                market_value=D("2000"),
                cost_basis=D("2000"),
            ),
        ],
        [
            _account(holdings_value=D("8000")),
            _account(
                account_id="ira",
                display_name="IRA",
                tax_treatment="tax_deferred",
                holdings_value=D("2000"),
            ),
        ],
    )
    suggestion = suggest_rebalance(view, _policy())
    sell = next(m for m in suggestion.moves if m.action is TradeAction.SELL)
    assert sell.account_id == "tax" and sell.amount == D("2000.0")
    assert sell.tax is not None and sell.tax.estimated_gain == D("500.00")
    assert suggestion.assessment_after.is_compliant


def test_suggestion_honors_tax_placement_for_buys() -> None:
    policy = _policy(
        targets={"us_equity": "0.4", "intl_equity": "0.3", "fixed_income": "0.3"},
        tax_placement={"prefer_tax_deferred": ["fixed_income"]},
    )
    suggestion = suggest_rebalance(_two_account_view(ira_cash=D("200")), policy)
    ira_buys = [
        m for m in suggestion.moves if m.account_id == "ira" and m.action is TradeAction.BUY
    ]
    assert ira_buys[0].asset_class == "fixed_income"


def test_suggestion_drops_moves_below_the_minimum() -> None:
    view = _two_account_view()
    policy = _policy(
        targets={"us_equity": "0.79", "fixed_income": "0.21"},
        drift_band="0.005",
        rebalancing={"min_trade_value": "1000"},
    )
    suggestion = suggest_rebalance(view, policy)
    assert suggestion.needs_rebalance
    assert suggestion.moves == ()
    assert suggestion.skipped


def test_suggestion_does_nothing_in_band_under_threshold() -> None:
    proposed = {AssetClass.US_EQUITY: D("0.78"), AssetClass.FIXED_INCOME: D("0.22")}
    suggestion = suggest_rebalance(_two_account_view(), _policy(), targets=proposed)
    assert not suggestion.needs_rebalance
    assert suggestion.moves == ()


def test_suggestion_follows_proposed_targets() -> None:
    proposed = {AssetClass.US_EQUITY: D("0.5"), AssetClass.FIXED_INCOME: D("0.5")}
    suggestion = suggest_rebalance(_two_account_view(), _policy(), targets=proposed)
    assert suggestion.gaps_before.targets_proposed
    moved = sum((m.amount for m in suggestion.moves if m.action is TradeAction.SELL), ZERO)
    # 2000 can move inside the IRA; the last 1000 must come from taxable.
    assert moved == D("3000.0")
    assert all(g.gap == ZERO for g in suggestion.gaps_after.gaps)


def test_suggestion_leaves_unconfirmed_accounts_alone() -> None:
    view = _view(
        [
            _holding(tax_treatment="unknown", market_value=D("8000")),
            _holding(
                account_id="ira",
                account_name="IRA",
                tax_treatment="tax_deferred",
                security_id="s-bd",
                symbol="BNDX",
                asset_class="fixed_income",
                market_value=D("2000"),
            ),
        ],
        [
            _account(tax_treatment="unknown", holdings_value=D("8000")),
            _account(
                account_id="ira",
                display_name="IRA",
                tax_treatment="tax_deferred",
                holdings_value=D("2000"),
            ),
        ],
    )
    suggestion = suggest_rebalance(view, _policy())
    assert all(m.account_id != "tax" for m in suggestion.moves)
    assert any("unconfirmed" in c for c in suggestion.caveats)


def test_cash_flow_method_never_sells() -> None:
    policy = _policy(rebalancing={"method": "cash_flow", "min_trade_value": "100"})
    suggestion = suggest_rebalance(_two_account_view(tax_cash=D("1000")), policy)
    assert suggestion.moves
    assert all(m.action is TradeAction.BUY for m in suggestion.moves)
