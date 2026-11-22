"""Instrument classification.

Asset allocation is meaningless without this, but a wrong classification moves
money between buckets in a chart the user makes decisions from -- so the rules
fire only on strong evidence and everything else stays honestly unknown.

The abbreviations below are all real: fund names in brokerage exports are
heavily truncated ("INTERNATL", "DIV APP", "EMR MKT"), which is exactly where a
naive keyword matcher silently gives up.
"""

from __future__ import annotations

import pytest

from portfolio_manager.models.enums import AssetClass, SecurityType
from portfolio_manager.services.security_classification import classify_security


@pytest.mark.parametrize(
    ("symbol", "name", "asset_class"),
    [
        ("VOO", "VANGUARD INDEX FUNDS S&P 500 ETF USD", AssetClass.US_EQUITY),
        ("SPY", "STATE STREET SPDR S&P 500", AssetClass.US_EQUITY),
        ("VXUS", "Vanguard Total International Stock Index Fund;ETF", AssetClass.INTL_EQUITY),
        ("BND", "Vanguard Total Bond Market Index Fund", AssetClass.FIXED_INCOME),
        ("VNQ", "Vanguard Real Estate Index Fund", AssetClass.REAL_ESTATE),
        ("FZDXX", "FIDELITY MMKT PREMIUM CLASS", AssetClass.CASH_EQUIVALENT),
        ("FDLXX", "FIDELITY TREASURY ONLY MONEY MARKET FD", AssetClass.CASH_EQUIVALENT),
    ],
)
def test_common_funds_classify_confidently(symbol: str, name: str, asset_class: AssetClass) -> None:
    result = classify_security(symbol, name)
    assert result.asset_class is asset_class
    assert result.confidence >= __import__("decimal").Decimal("0.85")


@pytest.mark.parametrize(
    ("symbol", "name", "asset_class"),
    [
        # Every one of these appears truncated in the real corpus and was
        # missed by the first draft of the rules.
        ("FSPSX", "FIDELITY INTERNATL INDEX FUND", AssetClass.INTL_EQUITY),
        ("VWO", "VANGUARD INTL EQUITY INDEX FDS FTSE EMR MKT ETF", AssetClass.INTL_EQUITY),
        ("VIG", "VANGUARD SPECIALIZED FUNDS DIV APP ETF", AssetClass.US_EQUITY),
        ("VYM", "VANGUARD WHITEHALL FDS HIGH DIV YLD", AssetClass.US_EQUITY),
        ("SOXQ", "INVESCO EXCHANGE-TRADED FD TR INVESCO PHLX SM", AssetClass.US_EQUITY),
        ("VWSUX", "Vanguard Ultra-Short-Term Tax-Exempt Fund;Admiral", AssetClass.FIXED_INCOME),
        ("VFSTX", "Vanguard Short-Term Investment-Grade Fund;Investor", AssetClass.FIXED_INCOME),
    ],
)
def test_abbreviated_fund_names_still_classify(
    symbol: str, name: str, asset_class: AssetClass
) -> None:
    assert classify_security(symbol, name).asset_class is asset_class


def test_treasuries_are_cash_equivalents() -> None:
    result = classify_security(None, "UNITED STATES TREASURY 0.000% 05/27/25")
    assert result.asset_class is AssetClass.CASH_EQUIVALENT


def test_symbol_less_holdings_classify_from_their_description() -> None:
    """Both real 529 accounts hold funds with no ticker at all."""
    assert (
        classify_security(None, "Vanguard 500 Index Portfolio").asset_class is AssetClass.US_EQUITY
    )
    assert (
        classify_security(None, "Vanguard Short-Term Bond Index Portfolio").asset_class
        is AssetClass.FIXED_INCOME
    )


def test_individual_stocks_are_recognised_despite_share_class_noise() -> None:
    """Real names trail the company suffix: 'CORP COM', 'INC COM CL A'."""
    for symbol, name in (
        ("MSFT", "Microsoft Corp"),
        ("COST", "COSTCO WHOLESALE CORP COM"),
        ("BLZE", "BACKBLAZE INC COM CL A"),
    ):
        result = classify_security(symbol, name)
        assert result.security_type is SecurityType.EQUITY
        assert result.asset_class is AssetClass.US_EQUITY


def test_target_date_funds_are_multi_asset() -> None:
    result = classify_security("VTINX", "Vanguard Target Retirement Income Fund")
    assert result.asset_class is AssetClass.MULTI_ASSET
    assert result.security_type is SecurityType.TARGET_DATE


def test_international_beats_the_generic_stock_rule() -> None:
    """'Total International Stock Index' contains both signals."""
    result = classify_security(None, "Vanguard Total International Stock Index Portfolio")
    assert result.asset_class is AssetClass.INTL_EQUITY


def test_a_placeholder_row_stays_unknown() -> None:
    """One real export carries rows with symbol "**" and a blank description."""
    for symbol, name in (("", "**"), ("**", ""), (None, "  ")):
        result = classify_security(symbol or None, name)
        assert result.asset_class is AssetClass.UNKNOWN
        assert not result.is_known
        assert result.confidence == 0


def test_a_symbol_less_opaque_name_stays_unknown() -> None:
    """With no ticker and no recognisable words there is nothing to go on."""
    result = classify_security(None, "Something Entirely Opaque")
    assert result.asset_class is AssetClass.UNKNOWN
    assert result.confidence == 0


def test_a_truncated_company_name_is_still_a_stock_but_scored_lower() -> None:
    """Vanguard reports Costco as "COSTCO WHOLESALE…", cutting off "CORP".

    Requiring the suffix missed a real holding; inferring from the absence of
    fund vocabulary catches it. That is weaker evidence, so it scores lower —
    the confidence is the honest part.
    """
    suffixed = classify_security("COST", "COSTCO WHOLESALE CORP COM")
    truncated = classify_security("COST", "COSTCO WHOLESALE\u2026")

    assert suffixed.security_type is SecurityType.EQUITY
    assert truncated.security_type is SecurityType.EQUITY
    assert truncated.confidence < suffixed.confidence


def test_a_truncated_fund_name_is_not_mistaken_for_a_stock() -> None:
    """The negative test must survive truncation too: a cut-off fund name
    still carries its issuer."""
    result = classify_security("VWXYZ", "VANGUARD TREASURY\u2026")
    assert result.security_type is not SecurityType.EQUITY


def test_every_classification_explains_itself() -> None:
    assert classify_security("VOO", "Vanguard 500").rationale
    assert classify_security(None, "???").rationale


@pytest.mark.parametrize("symbol", ["VUSXX", "VMFXX", "FZDXX", "FDLXX"])
def test_money_market_funds_with_truncated_names_still_classify(symbol: str) -> None:
    """Real exports truncate these: 'VANGUARD TREASURY...' says nothing.

    Caught by looking at the rendered UI, where six figures of a
    treasury money-market fund was sitting in the "Unknown" allocation slice.
    """
    result = classify_security(symbol, "VANGUARD TREASURY...")
    assert result.asset_class is AssetClass.CASH_EQUIVALENT
    assert result.security_type is SecurityType.MONEY_MARKET
