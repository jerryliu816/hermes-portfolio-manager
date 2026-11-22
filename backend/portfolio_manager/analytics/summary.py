"""Portfolio summary, including how much of it can be trusted.

Every figure here travels with its own caveats. That is not defensiveness: on
the real corpus 61% of value sat in accounts whose as-of date is unknown, and
29% of holdings report no cost basis. A summary that showed a total without
saying so would be presenting a number more confident than the data behind it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ..models.enums import CostBasisSource, TaxTreatment
from .view import ZERO, PortfolioView


#: Percentages are ratios in [0, 1]; formatting is the UI's business.
def _fraction(part: Decimal, whole: Decimal) -> Decimal:
    return ZERO if whole == ZERO else part / whole


@dataclass(frozen=True)
class DataQuality:
    """How far the numbers can be trusted, expressed as fractions of value."""

    stale_value: Decimal
    stale_fraction: Decimal
    unclassified_value: Decimal
    unclassified_fraction: Decimal
    missing_basis_value: Decimal
    missing_basis_fraction: Decimal
    aggregated_value: Decimal
    aggregated_fraction: Decimal
    stale_accounts: tuple[str, ...] = ()
    unclassified_accounts: tuple[str, ...] = ()

    @property
    def is_fully_trusted(self) -> bool:
        return (
            self.stale_fraction == ZERO
            and self.unclassified_fraction == ZERO
            and self.missing_basis_fraction == ZERO
        )

    def caveats(self) -> list[str]:
        """Plain-language warnings, ready to show beside the headline number."""
        notes: list[str] = []
        if self.stale_fraction > ZERO:
            notes.append(
                f"{self.stale_fraction:.1%} of value is in accounts whose as-of date is "
                "unknown, because they are reported second-hand by an aggregation feed"
            )
        if self.unclassified_fraction > ZERO:
            notes.append(
                f"{self.unclassified_fraction:.1%} of value is in accounts with no "
                "confirmed tax treatment and is excluded from tax analysis"
            )
        if self.missing_basis_fraction > ZERO:
            notes.append(
                f"{self.missing_basis_fraction:.1%} of value has no reported cost basis, "
                "so gains cannot be computed for it"
            )
        return notes


@dataclass(frozen=True)
class PortfolioSummary:
    as_of_date: dt.date
    completeness: str
    total_value: Decimal
    holdings_value: Decimal
    cash_value: Decimal
    account_count: int
    position_count: int
    #: Distinct instruments, which is smaller than position_count whenever the
    #: same security is held in more than one account.
    security_count: int
    closed_position_count: int
    skipped_account_count: int
    quality: DataQuality
    warnings: list[str] = field(default_factory=list)


def summarize(view: PortfolioView) -> PortfolioSummary:
    total = view.total_value

    stale_accounts = [a for a in view.accounts if a.is_stale]
    stale_value = sum((a.total_value for a in stale_accounts), ZERO)

    unclassified = [a for a in view.accounts if a.tax_treatment == TaxTreatment.UNKNOWN.value]
    unclassified_value = sum((a.total_value for a in unclassified), ZERO)

    aggregated_value = sum((a.total_value for a in view.accounts if a.is_aggregated), ZERO)

    open_holdings = [h for h in view.holdings if h.counts_toward_allocation]
    missing_basis_value = sum(
        (h.value for h in open_holdings if h.cost_basis_source == CostBasisSource.UNKNOWN.value),
        ZERO,
    )

    quality = DataQuality(
        stale_value=stale_value,
        stale_fraction=_fraction(stale_value, total),
        unclassified_value=unclassified_value,
        unclassified_fraction=_fraction(unclassified_value, total),
        missing_basis_value=missing_basis_value,
        missing_basis_fraction=_fraction(missing_basis_value, total),
        aggregated_value=aggregated_value,
        aggregated_fraction=_fraction(aggregated_value, total),
        stale_accounts=tuple(a.display_name for a in stale_accounts),
        unclassified_accounts=tuple(a.display_name for a in unclassified),
    )

    warnings: list[str] = []
    if view.completeness != "complete":
        warnings.append(
            "this snapshot is incomplete: at least one account identified in the "
            "source document was not extracted"
        )
    if view.skipped_accounts:
        names = ", ".join(str(s.get("display_name", "?")) for s in view.skipped_accounts)
        warnings.append(
            f"{len(view.skipped_accounts)} account(s) reported no value and were "
            f"not imported: {names}"
        )

    return PortfolioSummary(
        as_of_date=view.as_of_date,
        completeness=view.completeness,
        total_value=total,
        holdings_value=view.holdings_value,
        cash_value=view.cash_value,
        account_count=len(view.accounts),
        position_count=len(view.holdings),
        security_count=len({h.security_id for h in view.holdings}),
        closed_position_count=sum(1 for h in view.holdings if h.is_closed),
        skipped_account_count=len(view.skipped_accounts),
        quality=quality,
        warnings=warnings,
    )
