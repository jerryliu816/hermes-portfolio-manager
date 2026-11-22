"""Cash and cash equivalents, split by how the money is taxed.

Two facts about the real corpus make this its own analytic rather than a slice
of `by_asset_class`:

* **Cash arrives in two shapes.** Vanguard reports a settlement fund as an
  account balance outside the holdings table, so it lands in
  `AccountRow.cash_balance`; Fidelity lists the same kind of money as a
  money-market holding row, so it lands in the `cash_equivalent` asset class.
  Either alone understates the position, and the asset-class breakdown sees
  only the second.
* **Where cash sits changes what it costs.** A money-market yield is ordinary
  income in a taxable account and deferred or exempt in a sheltered one, so the
  taxable share is the number worth watching, not the total.

`tax_deferred` is deliberately *not* called non-taxable. It is sheltered now
and taxed on withdrawal, and a label that said otherwise would be more
confident than the truth. Accounts with no confirmed treatment get their own
bucket rather than being folded into either side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models.enums import AssetClass, TaxTreatment
from .view import ZERO, PortfolioView

#: Which side of the tax line each treatment falls on. `unknown` is absent on
#: purpose: it is answered by `_UNKNOWN`, not by a guess.
_BUCKETS: dict[str, str] = {
    TaxTreatment.TAXABLE.value: "taxable",
    TaxTreatment.TAX_DEFERRED.value: "tax_sheltered",
    TaxTreatment.TAX_FREE.value: "tax_sheltered",
    TaxTreatment.EDUCATION.value: "tax_sheltered",
}
_UNKNOWN = "unknown"

_LABELS: dict[str, str] = {
    "taxable": "Taxable",
    "tax_sheltered": "Tax-sheltered",
    _UNKNOWN: "Unconfirmed",
}

#: Ordering is meaning, not aesthetics: taxable first because it is the bucket
#: with a yearly tax bill attached, unknown last because it is a gap.
_ORDER = ("taxable", "tax_sheltered", _UNKNOWN)


@dataclass(frozen=True)
class CashBucket:
    """Cash on one side of the tax line, with the two shapes kept apart."""

    key: str
    label: str
    #: Balances the source reports outside its holdings table.
    settlement_value: Decimal
    #: `cash_equivalent` holdings -- money-market funds, T-bills, sweeps.
    holdings_value: Decimal
    position_count: int
    account_count: int
    fraction: Decimal
    #: The treatments folded into this bucket, so a UI can say which they were.
    treatments: tuple[str, ...]

    @property
    def total_value(self) -> Decimal:
        return self.settlement_value + self.holdings_value


@dataclass(frozen=True)
class CashEquivalents:
    """Every dollar of cash in the portfolio, and where it is taxed."""

    buckets: tuple[CashBucket, ...]
    total_value: Decimal
    settlement_value: Decimal
    holdings_value: Decimal
    #: Share of the whole portfolio -- holdings plus settlement cash.
    fraction_of_portfolio: Decimal

    def get(self, key: str) -> CashBucket | None:
        return next((b for b in self.buckets if b.key == key), None)

    @property
    def taxable_value(self) -> Decimal:
        bucket = self.get("taxable")
        return bucket.total_value if bucket else ZERO

    @property
    def unconfirmed_value(self) -> Decimal:
        """Cash whose tax treatment is unknown, so it belongs to neither side."""
        bucket = self.get(_UNKNOWN)
        return bucket.total_value if bucket else ZERO


def cash_equivalents(view: PortfolioView) -> CashEquivalents:
    """Settlement balances plus cash-equivalent holdings, bucketed by tax side."""
    settlement: dict[str, Decimal] = {}
    holdings: dict[str, Decimal] = {}
    positions: dict[str, int] = {}
    accounts: dict[str, set[str]] = {}
    treatments: dict[str, set[str]] = {}

    def bucket_for(tax_treatment: str) -> str:
        return _BUCKETS.get(tax_treatment, _UNKNOWN)

    for account in view.accounts:
        if account.cash_balance == ZERO:
            continue
        key = bucket_for(account.tax_treatment)
        settlement[key] = settlement.get(key, ZERO) + account.cash_balance
        accounts.setdefault(key, set()).add(account.account_id)
        treatments.setdefault(key, set()).add(account.tax_treatment)

    for holding in view.holdings:
        if holding.asset_class != AssetClass.CASH_EQUIVALENT.value:
            continue
        if not holding.counts_toward_allocation:
            continue
        key = bucket_for(holding.tax_treatment)
        holdings[key] = holdings.get(key, ZERO) + holding.value
        positions[key] = positions.get(key, 0) + 1
        accounts.setdefault(key, set()).add(holding.account_id)
        treatments.setdefault(key, set()).add(holding.tax_treatment)

    total = sum(settlement.values(), ZERO) + sum(holdings.values(), ZERO)
    buckets = tuple(
        CashBucket(
            key=key,
            label=_LABELS[key],
            settlement_value=settlement.get(key, ZERO),
            holdings_value=holdings.get(key, ZERO),
            position_count=positions.get(key, 0),
            account_count=len(accounts.get(key, ())),
            fraction=(
                ZERO
                if total == ZERO
                else (settlement.get(key, ZERO) + holdings.get(key, ZERO)) / total
            ),
            treatments=tuple(sorted(treatments.get(key, ()))),
        )
        for key in _ORDER
        # An empty bucket is not a finding. Rendering "Taxable: 0" beside a real
        # number invites reading it as a measured zero rather than an absence.
        if settlement.get(key, ZERO) + holdings.get(key, ZERO) != ZERO
    )

    portfolio_total = view.total_value
    return CashEquivalents(
        buckets=buckets,
        total_value=total,
        settlement_value=sum(settlement.values(), ZERO),
        holdings_value=sum(holdings.values(), ZERO),
        fraction_of_portfolio=ZERO if portfolio_total == ZERO else total / portfolio_total,
    )
