"""Deterministic arithmetic verification.

Pure functions over `Decimal`. No I/O, no model, no floats. This is the layer
that decides whether an extraction can be trusted, and it is deliberately the
one part of the pipeline that a language model has no influence over.

Two rules earned from the real corpus:

* **Tolerance is capped.** A percentage tolerance alone grants proportionally
  more slack the more money is at stake -- 0.1% of a $4.8M portfolio is $4,800.
  The effective tolerance is `min(max(abs, pct x value), cap)`.
* **An account total may legitimately exceed the sum of its holdings.**
  Settlement-fund cash is reported separately from the holdings table. Measured
  on the real Vanguard document, the gap equals the settlement balance to the
  cent on all three brokerage accounts. So the gap is reported as a *cash
  residual* rather than an error -- but only when it matches a stated cash
  balance, so a genuinely missing holding is never excused as "probably cash".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: Not applicable -- an input was absent, or the holding is closed.
    SKIPPED = "skipped"


class CheckKind(StrEnum):
    POSITION_VALUE = "position_value"
    POSITION_BASIS = "position_basis"
    ACCOUNT_TOTAL = "account_total"
    PORTFOLIO_TOTAL = "portfolio_total"


@dataclass(frozen=True)
class Tolerance:
    """How much difference is acceptable, and why."""

    absolute: Decimal
    percentage: Decimal
    maximum: Decimal

    def for_value(self, value: Decimal) -> Decimal:
        """Effective tolerance for an amount of this size.

        Whichever of the absolute and percentage allowances is larger, but
        never more than `maximum` -- otherwise a big account buys silence.
        """
        proportional = abs(value) * self.percentage
        allowed = max(self.absolute, proportional)
        return min(allowed, self.maximum)


@dataclass(frozen=True)
class CheckResult:
    kind: CheckKind
    status: CheckStatus
    subject: str
    expected: Decimal | None = None
    actual: Decimal | None = None
    tolerance: Decimal | None = None
    message: str = ""

    @property
    def difference(self) -> Decimal | None:
        if self.expected is None or self.actual is None:
            return None
        return self.actual - self.expected

    def describe(self) -> str:
        """A one-line human-readable form, used in the recovery prompt and UI."""
        if self.status is CheckStatus.SKIPPED:
            return f"{self.subject}: skipped ({self.message})"
        diff = self.difference
        if diff is None:
            return f"{self.subject}: {self.status}"
        return (
            f"{self.subject}: expected {self.expected}, got {self.actual} "
            f"(difference {diff:+}, tolerance {self.tolerance})"
        )


@dataclass
class ReconciliationReport:
    checks: list[CheckResult] = field(default_factory=list)
    #: Account subject -> unexplained-but-plausible cash residual.
    cash_residuals: dict[str, Decimal] = field(default_factory=dict)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.FAILED]

    @property
    def passed(self) -> bool:
        return not self.failures

    def failure_descriptions(self) -> tuple[str, ...]:
        return tuple(c.describe() for c in self.failures)

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c.status is CheckStatus.PASSED),
            "failed": len(self.failures),
            "skipped": sum(1 for c in self.checks if c.status is CheckStatus.SKIPPED),
        }


def within_tolerance(expected: Decimal, actual: Decimal, tolerance: Tolerance) -> bool:
    return abs(actual - expected) <= tolerance.for_value(expected)


def check_position_value(
    subject: str,
    quantity: Decimal | None,
    price: Decimal | None,
    market_value: Decimal | None,
    tolerance: Tolerance,
    *,
    is_closed: bool = False,
) -> CheckResult:
    """quantity x price should equal market value.

    Skipped for closed positions and whenever an input is absent. Both are
    common: the real 529 accounts carry quantity-0 rows with no value, and
    would otherwise inject six spurious failures from a single account.
    """
    if is_closed:
        return CheckResult(
            CheckKind.POSITION_VALUE, CheckStatus.SKIPPED, subject, message="closed position"
        )
    if quantity is None or price is None or market_value is None:
        return CheckResult(
            CheckKind.POSITION_VALUE,
            CheckStatus.SKIPPED,
            subject,
            message="quantity, price or value not reported",
        )
    expected = quantity * price
    ok = within_tolerance(expected, market_value, tolerance)
    return CheckResult(
        CheckKind.POSITION_VALUE,
        CheckStatus.PASSED if ok else CheckStatus.FAILED,
        subject,
        expected=expected,
        actual=market_value,
        tolerance=tolerance.for_value(expected),
    )


def check_position_basis(
    subject: str,
    cost_basis: Decimal | None,
    unrealized_gain_loss: Decimal | None,
    market_value: Decimal | None,
    tolerance: Tolerance,
) -> CheckResult:
    """cost basis + unrealized gain/loss should equal market value.

    Skipped whenever a term is absent -- 29% of real holdings report no cost
    basis, and 100% of aggregated holdings report no gain/loss.
    """
    if cost_basis is None or unrealized_gain_loss is None or market_value is None:
        return CheckResult(
            CheckKind.POSITION_BASIS,
            CheckStatus.SKIPPED,
            subject,
            message="cost basis or gain/loss not reported",
        )
    expected = cost_basis + unrealized_gain_loss
    ok = within_tolerance(expected, market_value, tolerance)
    return CheckResult(
        CheckKind.POSITION_BASIS,
        CheckStatus.PASSED if ok else CheckStatus.FAILED,
        subject,
        expected=expected,
        actual=market_value,
        tolerance=tolerance.for_value(expected),
    )


def check_account_total(
    subject: str,
    position_values: list[Decimal],
    reported_total: Decimal | None,
    tolerance: Tolerance,
    *,
    stated_cash_balance: Decimal | None = None,
) -> tuple[CheckResult, Decimal | None]:
    """The holdings should sum to the account total the document states.

    Returns the check plus any cash residual. When the document separately
    states a settlement/cash balance and the shortfall matches it, the check
    passes and the residual is recorded -- that is the real, verified shape of
    a Vanguard brokerage account. When there is no stated cash balance to
    explain the gap, it fails: a missing holding must never be waved through.
    """
    if reported_total is None:
        return (
            CheckResult(
                CheckKind.ACCOUNT_TOTAL,
                CheckStatus.SKIPPED,
                subject,
                message="document states no account total",
            ),
            None,
        )
    computed = sum(position_values, ZERO)

    if within_tolerance(reported_total, computed, tolerance):
        return (
            CheckResult(
                CheckKind.ACCOUNT_TOTAL,
                CheckStatus.PASSED,
                subject,
                expected=reported_total,
                actual=computed,
                tolerance=tolerance.for_value(reported_total),
            ),
            None,
        )

    shortfall = reported_total - computed
    if stated_cash_balance is not None and within_tolerance(
        stated_cash_balance, shortfall, tolerance
    ):
        return (
            CheckResult(
                CheckKind.ACCOUNT_TOTAL,
                CheckStatus.PASSED,
                subject,
                expected=reported_total,
                actual=computed + stated_cash_balance,
                tolerance=tolerance.for_value(reported_total),
                message=f"holdings plus stated cash balance {stated_cash_balance}",
            ),
            shortfall,
        )

    return (
        CheckResult(
            CheckKind.ACCOUNT_TOTAL,
            CheckStatus.FAILED,
            subject,
            expected=reported_total,
            actual=computed,
            tolerance=tolerance.for_value(reported_total),
            message=(
                "shortfall is not explained by a stated cash balance"
                if stated_cash_balance is None
                else f"shortfall does not match stated cash balance {stated_cash_balance}"
            ),
        ),
        None,
    )


def check_portfolio_total(
    account_values: list[Decimal],
    reported_total: Decimal | None,
    tolerance: Tolerance,
    *,
    cash_residuals: Decimal = ZERO,
) -> CheckResult:
    """The accounts should sum to any portfolio-wide total the document states."""
    if reported_total is None:
        return CheckResult(
            CheckKind.PORTFOLIO_TOTAL,
            CheckStatus.SKIPPED,
            "portfolio",
            message="document states no portfolio total",
        )
    computed = sum(account_values, ZERO) + cash_residuals
    ok = within_tolerance(reported_total, computed, tolerance)
    return CheckResult(
        CheckKind.PORTFOLIO_TOTAL,
        CheckStatus.PASSED if ok else CheckStatus.FAILED,
        "portfolio",
        expected=reported_total,
        actual=computed,
        tolerance=tolerance.for_value(reported_total),
    )
