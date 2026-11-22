"""Embedded gains, reported with the coverage they are based on.

29% of holdings in the real corpus report no cost basis at all, and 100% of
aggregated holdings report no gain/loss. A total unrealized gain computed over
whatever happens to have a basis is not wrong so much as *unlabelled* -- it
answers a narrower question than the reader assumes. So every figure here
carries the share of portfolio value it was computed from.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models.enums import CostBasisSource, TaxTreatment
from .view import ZERO, PortfolioView


@dataclass(frozen=True)
class GainsCoverage:
    """What fraction of the portfolio the gains figures actually describe."""

    covered_value: Decimal
    uncovered_value: Decimal
    reported_basis_value: Decimal
    derived_basis_value: Decimal

    @property
    def total(self) -> Decimal:
        return self.covered_value + self.uncovered_value

    @property
    def covered_fraction(self) -> Decimal:
        return ZERO if self.total == ZERO else self.covered_value / self.total

    def caveat(self) -> str | None:
        if self.uncovered_value == ZERO:
            return None
        return (
            f"computed from {self.covered_fraction:.1%} of portfolio value; the rest "
            "reports no cost basis"
        )


@dataclass(frozen=True)
class EmbeddedGains:
    market_value: Decimal
    cost_basis: Decimal
    unrealized_gain: Decimal
    coverage: GainsCoverage

    @property
    def return_fraction(self) -> Decimal | None:
        """Gain as a fraction of basis.

        Deliberately not called a 'return': it is a point-in-time comparison of
        value to basis, with no cash flows or time weighting behind it.
        """
        if self.cost_basis == ZERO:
            return None
        return self.unrealized_gain / self.cost_basis


@dataclass(frozen=True)
class TaxLotView:
    """Gains grouped by tax treatment, for placement decisions."""

    by_treatment: dict[str, EmbeddedGains]
    unclassified_value: Decimal


def _gains_for(rows: list[object]) -> EmbeddedGains:
    covered_value = ZERO
    uncovered_value = ZERO
    basis_total = ZERO
    reported = ZERO
    derived = ZERO

    for row in rows:
        value = row.value  # type: ignore[attr-defined]
        basis = row.cost_basis  # type: ignore[attr-defined]
        source = row.cost_basis_source  # type: ignore[attr-defined]
        if basis is None or source == CostBasisSource.UNKNOWN.value:
            uncovered_value += value
            continue
        covered_value += value
        basis_total += basis
        if source == CostBasisSource.REPORTED.value:
            reported += value
        else:
            derived += value

    return EmbeddedGains(
        market_value=covered_value,
        cost_basis=basis_total,
        unrealized_gain=covered_value - basis_total,
        coverage=GainsCoverage(
            covered_value=covered_value,
            uncovered_value=uncovered_value,
            reported_basis_value=reported,
            derived_basis_value=derived,
        ),
    )


def embedded_gains(view: PortfolioView) -> EmbeddedGains:
    """Unrealized gain across every holding that reports a basis."""
    return _gains_for([h for h in view.holdings if h.counts_toward_allocation])


def gains_by_tax_treatment(view: PortfolioView) -> TaxLotView:
    """Where the gains sit, which is what drives placement decisions.

    A large embedded gain in a taxable account is a very different fact from
    the same gain in a Roth, so these are never merged.
    """
    grouped: dict[str, list[object]] = {}
    unclassified = ZERO
    for holding in view.holdings:
        if not holding.counts_toward_allocation:
            continue
        if holding.tax_treatment == TaxTreatment.UNKNOWN.value:
            unclassified += holding.value
            continue
        grouped.setdefault(holding.tax_treatment, []).append(holding)

    return TaxLotView(
        by_treatment={treatment: _gains_for(rows) for treatment, rows in sorted(grouped.items())},
        unclassified_value=unclassified,
    )


def largest_embedded_gains(view: PortfolioView, limit: int = 10) -> list[tuple[str, Decimal]]:
    """Holdings with the biggest unrealized gain, for tax-aware decisions."""
    gains: list[tuple[str, Decimal]] = []
    for holding in view.holdings:
        if not holding.counts_toward_allocation:
            continue
        if holding.cost_basis is None:
            continue
        label = f"{holding.symbol or holding.security_name} in {holding.account_name}"
        gains.append((label, holding.value - holding.cost_basis))
    return sorted(gains, key=lambda g: -g[1])[:limit]
