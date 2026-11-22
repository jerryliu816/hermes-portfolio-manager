"""Parsing proposals off the wire: refuse anything a float could have touched."""

from __future__ import annotations

from decimal import Decimal

import pytest

from portfolio_manager.models.enums import AssetClass
from portfolio_manager.services.proposals import (
    ProposalError,
    parse_targets,
    parse_trades,
    targets_json,
)

SELL = {"account_id": "a", "action": "sell", "security": "VTI", "amount": "100.50"}


def test_a_trade_parses_with_decimal_money() -> None:
    (trade,) = parse_trades([SELL])
    assert trade.amount == Decimal("100.50")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("VTI", "must be a list"),
        ([], "nothing to simulate"),
        ([SELL, "x"], "trade 1 must be an object"),
        ([{**SELL, "security": ""}], "trade 0 is missing security"),
        ([{**SELL, "amount": 100.5}], "never a JSON number"),
        ([{**SELL, "amount": "-5"}], "positive"),
        ([{**SELL, "action": "hold"}], "trade 0"),
    ],
)
def test_a_bad_trade_is_refused_and_named(raw: object, message: str) -> None:
    with pytest.raises(ProposalError, match=message):
        parse_trades(raw)


def test_targets_parse_as_decimals_and_round_trip_as_strings() -> None:
    parsed = parse_targets({"us_equity": "0.6", "fixed_income": "0.4"})
    assert parsed == {AssetClass.US_EQUITY: Decimal("0.6"), AssetClass.FIXED_INCOME: Decimal("0.4")}
    assert targets_json(parsed) == {"us_equity": "0.6", "fixed_income": "0.4"}
    assert parse_targets(None) is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "non-empty"),
        ({"stocks": "1"}, "not an asset class"),
        ({"us_equity": 1}, "never a JSON number"),
        ({"us_equity": True}, "never a JSON number"),
        ({"us_equity": "lots"}, "not a number"),
    ],
)
def test_bad_targets_are_refused(raw: object, message: str) -> None:
    with pytest.raises(ProposalError, match=message):
        parse_targets(raw)
