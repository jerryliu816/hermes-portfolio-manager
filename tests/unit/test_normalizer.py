"""Normalization, tested against both real number formats.

Fidelity prints "$118936.42" with no thousands separators; Vanguard prints
"$2,417.86" with them. One parser must handle both, and must never turn an
absent value into zero.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from portfolio_manager.ingestion.normalizer import (
    NormalizationError,
    is_null_sentinel,
    mask_account_key,
    normalize_security_key,
    parse_date,
    parse_money,
    parse_percent,
    parse_quantity,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$118936.42", Decimal("118936.42")),  # Fidelity: no separators
        ("$2,417.86", Decimal("2417.86")),  # Vanguard: separators
        ("+$44725.87", Decimal("44725.87")),
        ("-$0.28", Decimal("-0.28")),
        ("$0.00", Decimal("0")),
        ("(1,234.56)", Decimal("-1234.56")),  # accounting negative
        ("87341.26", Decimal("87341.26")),
        ("$704.02", Decimal("704.02")),
    ],
)
def test_parses_both_real_money_formats(raw: str, expected: Decimal) -> None:
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw", ["--", "", "  ", "N/A", "n/a", "none", None, "—"])
def test_null_sentinels_become_none_never_zero(raw: str | None) -> None:
    """A zero holding and an unreported one are different facts."""
    assert is_null_sentinel(raw)
    assert parse_money(raw) is None


def test_zero_is_not_a_null_sentinel() -> None:
    """The 529 accounts carry genuine quantity-0 rows."""
    assert not is_null_sentinel("0")
    assert parse_quantity("0") == Decimal("0")
    assert parse_quantity("0") is not None


def test_fractional_share_counts_are_exact() -> None:
    """Real quantities carry three decimals; rounding them loses money."""
    assert parse_quantity("1,348.207") == Decimal("1348.207")
    assert parse_quantity("318.457") == Decimal("318.457")
    assert parse_quantity("141.637") == Decimal("141.637")


def test_money_never_becomes_a_float() -> None:
    value = parse_money("$0.1")
    assert isinstance(value, Decimal)
    # The classic float failure: 0.1 + 0.2 != 0.3
    assert parse_money("$0.1") + parse_money("$0.2") == parse_money("$0.3")  # type: ignore[operator]


def test_percent_becomes_a_fraction() -> None:
    assert parse_percent("+60.27%") == Decimal("0.6027")
    assert parse_percent("-0.11%") == Decimal("-0.0011")
    assert parse_percent("--") is None


def test_unparseable_value_raises_rather_than_defaulting() -> None:
    """Silently returning zero for garbage would corrupt a portfolio."""
    with pytest.raises(NormalizationError):
        parse_money("about twelve dollars")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Aug-26-2026", dt.date(2026, 8, 26)),
        ("08/25/2026", dt.date(2026, 8, 25)),
        ("2026-08-26", dt.date(2026, 8, 26)),
        ('"Date downloaded Aug-26-2026 3:04 a.m ET"', dt.date(2026, 8, 26)),
        ("Value as of: August 25, 2026, 4:15 p.m., Eastern time", dt.date(2026, 8, 25)),
    ],
)
def test_parses_every_real_date_shape(raw: str, expected: dt.date) -> None:
    assert parse_date(raw) == expected


def test_absent_date_is_none_not_today() -> None:
    assert parse_date(None) is None
    assert parse_date("--") is None


def test_security_key_prefers_the_ticker() -> None:
    assert normalize_security_key("VOO", "Vanguard 500 Index Fund;ETF") == "VOO"
    assert normalize_security_key("voo", "anything") == "VOO"


def test_security_key_falls_back_to_the_description() -> None:
    """12 of 86 real holdings have no ticker at all."""
    key = normalize_security_key(None, "Vanguard 500 Index Portfolio")
    assert key == "vanguard-500-index-portfolio"


def test_security_key_is_stable_across_cosmetic_differences() -> None:
    a = normalize_security_key(None, "Vanguard 500 Index Portfolio")
    b = normalize_security_key(None, "  Vanguard  500   Index Portfolio  ")
    assert a == b


def test_security_key_requires_something_to_work_with() -> None:
    with pytest.raises(NormalizationError):
        normalize_security_key(None, "   ")


def test_account_keys_are_masked_for_display() -> None:
    assert mask_account_key("Z10000077") == "*****0077"


def test_masking_a_uuid_does_not_produce_a_wall_of_asterisks() -> None:
    """Caught by looking at the accounts page, where a 36-character key masked
    one-for-one was harder to read than the four characters identifying it."""
    masked = mask_account_key("c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
    assert masked == "********4e5f"
    assert len(masked) == 12
    assert mask_account_key("c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f").endswith("4e5f")
    assert "c1d2e3f4" not in mask_account_key("c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
    assert mask_account_key("") == "unknown"
