"""Reconciliation arithmetic.

Several cases here are drawn directly from the real corpus, with the actual
figures, because the tolerances were calibrated from those measurements rather
than chosen by intuition.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from portfolio_manager.ingestion.reconciler import (
    CheckStatus,
    ReconciliationReport,
    Tolerance,
    check_account_total,
    check_portfolio_total,
    check_position_basis,
    check_position_value,
)

#: The shipped defaults.
TOL = Tolerance(absolute=Decimal("1.00"), percentage=Decimal("0.001"), maximum=Decimal("25.00"))


def test_the_headline_catch() -> None:
    """100 shares at $500 is $50,000, not $60,000."""
    result = check_position_value("ACME", Decimal("100"), Decimal("500"), Decimal("60000"), TOL)
    assert result.status is CheckStatus.FAILED
    assert result.expected == Decimal("50000")
    assert result.difference == Decimal("10000")


def test_real_rounding_drift_passes() -> None:
    """Measured worst case in the corpus: VGT, $0.23 off on a six-figure position.

    Only 10 of 68 real rows match exactly. An exact-equality check would reject
    good data on 58 of them.
    """
    result = check_position_value(
        "VGT", Decimal("141.637"), Decimal("755.28"), Decimal("106975.82"), TOL
    )
    assert result.status is CheckStatus.PASSED
    assert abs(result.difference or Decimal(0)) < Decimal("0.25")


def test_percentage_tolerance_is_capped() -> None:
    """0.1% of $5M would be $5,000 of silent slack. The cap forbids that."""
    portfolio = Decimal("1976601.90")
    assert TOL.for_value(portfolio) == Decimal("25.00")
    result = check_position_value("BIG", Decimal("1"), portfolio, portfolio + Decimal("4000"), TOL)
    assert result.status is CheckStatus.FAILED


def test_tolerance_scales_up_to_the_cap_but_not_past_it() -> None:
    assert TOL.for_value(Decimal("100")) == Decimal("1.00")  # absolute floor
    assert TOL.for_value(Decimal("10000")) == Decimal("10.00")  # proportional
    assert TOL.for_value(Decimal("1000000")) == Decimal("25.00")  # capped


def test_closed_positions_are_exempt() -> None:
    """The real 529 accounts carry six quantity-0 rows with no value."""
    result = check_position_value(
        "Sample Bond Portfolio", Decimal("0"), Decimal("10.76"), None, TOL, is_closed=True
    )
    assert result.status is CheckStatus.SKIPPED


def test_absent_inputs_skip_rather_than_fail() -> None:
    """Aggregated rows report no gain/loss at all; that is not an error."""
    assert (
        check_position_value("X", None, Decimal("10"), Decimal("100"), TOL).status
        is CheckStatus.SKIPPED
    )
    assert (
        check_position_basis("X", None, Decimal("5"), Decimal("100"), TOL).status
        is CheckStatus.SKIPPED
    )


def test_basis_plus_gain_equals_value() -> None:
    ok = check_position_basis(
        "VOO", Decimal("74210.55"), Decimal("44725.87"), Decimal("118936.42"), TOL
    )
    assert ok.status is CheckStatus.PASSED
    bad = check_position_basis(
        "VOO", Decimal("74210.55"), Decimal("44725.87"), Decimal("138936.42"), TOL
    )
    assert bad.status is CheckStatus.FAILED


def test_account_total_matches_sum_of_holdings() -> None:
    result, residual = check_account_total(
        "acct", [Decimal("100"), Decimal("200")], Decimal("300"), TOL
    )
    assert result.status is CheckStatus.PASSED
    assert residual is None


def test_settlement_cash_explains_a_shortfall_and_is_recorded() -> None:
    """The real Vanguard document reconciled this way, exact to the cent.

    The figures here are invented; the shape is the real one. Account
    19000011: holdings sum to $842,316.40 while the account total is
    $899,524.55. The $57,208.15 gap is precisely the settlement-fund balance,
    which Vanguard reports outside the holdings table.
    """
    holdings = Decimal("842316.40")
    total = Decimal("899524.55")
    cash = Decimal("57208.15")
    result, residual = check_account_total(
        "19000011", [holdings], total, TOL, stated_cash_balance=cash
    )
    assert result.status is CheckStatus.PASSED
    assert residual == cash
    assert "cash balance" in result.message


def test_an_unexplained_shortfall_still_fails() -> None:
    """A missing holding must never be waved through as 'probably cash'."""
    result, residual = check_account_total(
        "acct", [Decimal("842316.40")], Decimal("899524.55"), TOL
    )
    assert result.status is CheckStatus.FAILED
    assert residual is None
    assert "not explained" in result.message


def test_a_shortfall_that_misses_the_stated_cash_fails() -> None:
    """Cash must actually account for the gap, not merely be present."""
    result, residual = check_account_total(
        "acct",
        [Decimal("842316.40")],
        Decimal("899524.55"),
        TOL,
        stated_cash_balance=Decimal("5000.00"),
    )
    assert result.status is CheckStatus.FAILED
    assert residual is None


def test_absent_reported_total_skips() -> None:
    """The real Fidelity export states no account totals at all."""
    result, residual = check_account_total("acct", [Decimal("100")], None, TOL)
    assert result.status is CheckStatus.SKIPPED
    assert residual is None


def test_portfolio_total_includes_cash_residuals() -> None:
    """The real Vanguard total reconciled this way; these figures are invented."""
    holdings = [
        Decimal("842316.40"),
        Decimal("615204.88"),
        Decimal("73918.27"),
        Decimal("41275.63"),
        Decimal("52690.14"),
    ]
    cash = Decimal("57208.15") + Decimal("21567.92") + Decimal("3884.06")
    result = check_portfolio_total(holdings, Decimal("1708065.45"), TOL, cash_residuals=cash)
    assert result.status is CheckStatus.PASSED


def test_portfolio_total_fails_if_cash_is_forgotten() -> None:
    """Omitting the residual understates by the cash -- it must not pass."""
    holdings = [
        Decimal("842316.40"),
        Decimal("615204.88"),
        Decimal("73918.27"),
        Decimal("41275.63"),
        Decimal("52690.14"),
    ]
    result = check_portfolio_total(holdings, Decimal("1708065.45"), TOL)
    assert result.status is CheckStatus.FAILED


def test_report_collects_failures_for_the_recovery_prompt() -> None:
    report = ReconciliationReport()
    report.checks.append(
        check_position_value("ACME", Decimal("100"), Decimal("500"), Decimal("60000"), TOL)
    )
    report.checks.append(check_position_value("OK", Decimal("2"), Decimal("5"), Decimal("10"), TOL))
    assert not report.passed
    assert report.summary() == {"total": 2, "passed": 1, "failed": 1, "skipped": 0}
    described = report.failure_descriptions()
    assert len(described) == 1
    assert "ACME" in described[0] and "60000" in described[0]


def test_everything_is_decimal_not_float() -> None:
    result = check_position_value("X", Decimal("0.1"), Decimal("3"), Decimal("0.3"), TOL)
    assert result.status is CheckStatus.PASSED
    assert isinstance(result.expected, Decimal)


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-100")])
def test_tolerance_handles_zero_and_negative_amounts(value: Decimal) -> None:
    assert TOL.for_value(value) >= Decimal("1.00")
