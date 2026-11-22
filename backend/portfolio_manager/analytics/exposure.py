"""Security exposure across accounts, and concentration.

The rule that shapes this module: **share counts are never summed across
accounts.** The same ETF can appear pre-split in one source and post-split in
another, each row internally consistent, so 1348.207 shares in one account and
141.637 in another cannot be added -- the result would be a number of shares
that has never existed. Values add; quantities are reported per account.

That is also why `SecurityExposure.total_quantity` deliberately does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models.enums import TaxTreatment
from .view import ZERO, PortfolioView


@dataclass(frozen=True)
class AccountHolding:
    """One security's presence in one account, with its own share basis."""

    account_id: str
    account_name: str
    institution: str
    tax_treatment: str
    quantity: Decimal | None
    price: Decimal | None
    value: Decimal
    is_stale: bool


@dataclass(frozen=True)
class SecurityExposure:
    """Total exposure to one instrument, aggregated by value only."""

    security_id: str
    symbol: str | None
    name: str
    asset_class: str
    total_value: Decimal
    fraction_of_portfolio: Decimal
    holdings: tuple[AccountHolding, ...]

    @property
    def account_count(self) -> int:
        return len(self.holdings)

    @property
    def has_divergent_prices(self) -> bool:
        """Whether accounts disagree on this instrument's price.

        Legitimate: one source may be stale or pre-split. Worth surfacing so a
        reader does not mistake it for an error, and worth never silently
        averaging away.
        """
        prices = {h.price for h in self.holdings if h.price is not None}
        return len(prices) > 1

    @property
    def price_range(self) -> tuple[Decimal, Decimal] | None:
        prices = [h.price for h in self.holdings if h.price is not None]
        return (min(prices), max(prices)) if prices else None

    def by_tax_treatment(self) -> dict[str, Decimal]:
        """Where this exposure sits for tax purposes."""
        out: dict[str, Decimal] = {}
        for holding in self.holdings:
            out[holding.tax_treatment] = out.get(holding.tax_treatment, ZERO) + holding.value
        return out


def security_exposures(view: PortfolioView) -> list[SecurityExposure]:
    """Every instrument, largest exposure first."""
    total = view.total_value
    grouped: dict[str, list[object]] = {}
    for holding in view.holdings:
        if not holding.counts_toward_allocation:
            continue
        grouped.setdefault(holding.security_id, []).append(holding)

    exposures: list[SecurityExposure] = []
    for security_id, rows in grouped.items():
        first = rows[0]
        value = sum((r.value for r in rows), ZERO)  # type: ignore[attr-defined]
        exposures.append(
            SecurityExposure(
                security_id=security_id,
                symbol=first.symbol,  # type: ignore[attr-defined]
                name=first.security_name,  # type: ignore[attr-defined]
                asset_class=first.asset_class,  # type: ignore[attr-defined]
                total_value=value,
                fraction_of_portfolio=ZERO if total == ZERO else value / total,
                holdings=tuple(
                    AccountHolding(
                        account_id=r.account_id,  # type: ignore[attr-defined]
                        account_name=r.account_name,  # type: ignore[attr-defined]
                        institution=r.institution,  # type: ignore[attr-defined]
                        tax_treatment=r.tax_treatment,  # type: ignore[attr-defined]
                        quantity=r.quantity,  # type: ignore[attr-defined]
                        price=r.price,  # type: ignore[attr-defined]
                        value=r.value,  # type: ignore[attr-defined]
                        is_stale=r.account_is_stale,  # type: ignore[attr-defined]
                    )
                    for r in sorted(rows, key=lambda r: -r.value)  # type: ignore[attr-defined]
                ),
            )
        )
    return sorted(exposures, key=lambda e: (-e.total_value, e.symbol or e.name))


def find_exposure(view: PortfolioView, symbol: str) -> SecurityExposure | None:
    wanted = symbol.strip().upper()
    return next((e for e in security_exposures(view) if (e.symbol or "").upper() == wanted), None)


@dataclass(frozen=True)
class Concentration:
    """How concentrated the portfolio is, and where."""

    largest: tuple[SecurityExposure, ...]
    top_1_fraction: Decimal
    top_5_fraction: Decimal
    top_10_fraction: Decimal
    #: Herfindahl-Hirschman index over instrument weights, 0-1. Higher is more
    #: concentrated; 1 means a single holding.
    hhi: Decimal
    single_holdings_over_threshold: tuple[SecurityExposure, ...]
    threshold: Decimal


def concentration(
    view: PortfolioView, threshold: Decimal = Decimal("0.05"), top: int = 10
) -> Concentration:
    exposures = security_exposures(view)

    def cumulative(n: int) -> Decimal:
        return sum((e.fraction_of_portfolio for e in exposures[:n]), ZERO)

    hhi = sum((e.fraction_of_portfolio**2 for e in exposures), ZERO)
    return Concentration(
        largest=tuple(exposures[:top]),
        top_1_fraction=cumulative(1),
        top_5_fraction=cumulative(5),
        top_10_fraction=cumulative(10),
        hhi=hhi,
        single_holdings_over_threshold=tuple(
            e for e in exposures if e.fraction_of_portfolio >= threshold
        ),
        threshold=threshold,
    )


def divergent_price_securities(view: PortfolioView) -> list[SecurityExposure]:
    """Instruments whose accounts disagree on price.

    Surfaced deliberately rather than reconciled: on the real corpus six of the
    ten securities held in more than one place disagreed, because one source
    was stale or reported a pre-split basis.
    """
    return [e for e in security_exposures(view) if e.has_divergent_prices]


def unclassified_tax_exposure(view: PortfolioView) -> Decimal:
    """Value whose tax placement is unknown, so it can be shown, not hidden."""
    return sum(
        (
            h.value
            for h in view.holdings
            if h.counts_toward_allocation and h.tax_treatment == TaxTreatment.UNKNOWN.value
        ),
        ZERO,
    )
