"""Allocation breakdowns: asset class, institution, tax treatment.

Two rules run through all of them:

* **Closed and valueless holdings never enter a denominator.** The real 529
  accounts carry six quantity-zero rows; counting them would dilute every
  percentage by a holding that no longer exists.
* **A bucket that means "we do not know" is reported separately from the ones
  that mean something.** Folding `unknown` into `taxable` would produce a
  tax-placement analysis that looks complete and is wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from ..models.enums import TaxTreatment
from .view import ZERO, HoldingRow, PortfolioView


@dataclass(frozen=True)
class Slice:
    key: str
    label: str
    value: Decimal
    fraction: Decimal
    position_count: int

    @property
    def is_unknown(self) -> bool:
        return self.key == "unknown"


@dataclass(frozen=True)
class Breakdown:
    """A set of slices plus what had to be left out of it."""

    slices: tuple[Slice, ...]
    total: Decimal
    #: Value that could not be classified, excluded from `slices` fractions.
    excluded_value: Decimal = ZERO
    excluded_label: str = ""

    @property
    def excluded_fraction(self) -> Decimal:
        base = self.total + self.excluded_value
        return ZERO if base == ZERO else self.excluded_value / base

    def get(self, key: str) -> Slice | None:
        return next((s for s in self.slices if s.key == key), None)


def _group(
    holdings: Iterable[HoldingRow],
    key: Callable[[HoldingRow], str],
    label: Callable[[str], str] | None = None,
) -> Breakdown:
    buckets: dict[str, tuple[Decimal, int]] = {}
    for holding in holdings:
        if not holding.counts_toward_allocation:
            continue
        bucket = key(holding)
        value, count = buckets.get(bucket, (ZERO, 0))
        buckets[bucket] = (value + holding.value, count + 1)

    total = sum((v for v, _ in buckets.values()), ZERO)
    slices = tuple(
        sorted(
            (
                Slice(
                    key=bucket,
                    label=label(bucket) if label else bucket.replace("_", " ").title(),
                    value=value,
                    fraction=ZERO if total == ZERO else value / total,
                    position_count=count,
                )
                for bucket, (value, count) in buckets.items()
                # A zero-value bucket is not an allocation. It renders as an
                # invisible slice with a legend entry saying 0%, which is noise.
                if value != ZERO
            ),
            key=lambda s: (-s.value, s.key),
        )
    )
    return Breakdown(slices=slices, total=total)


def by_asset_class(view: PortfolioView) -> Breakdown:
    return _group(view.holdings, lambda h: h.asset_class)


def by_institution(view: PortfolioView) -> Breakdown:
    """Where the money is held. Cash is included, since it is held somewhere."""
    buckets: dict[str, tuple[Decimal, int]] = {}
    for account in view.accounts:
        value, count = buckets.get(account.institution, (ZERO, 0))
        buckets[account.institution] = (value + account.total_value, count + 1)
    total = sum((v for v, _ in buckets.values()), ZERO)
    slices = tuple(
        sorted(
            (
                Slice(
                    key=name,
                    label=name.title() if name.islower() else name,
                    value=value,
                    fraction=ZERO if total == ZERO else value / total,
                    position_count=count,
                )
                for name, (value, count) in buckets.items()
            ),
            key=lambda s: (-s.value, s.key),
        )
    )
    return Breakdown(slices=slices, total=total)


def by_tax_treatment(view: PortfolioView) -> Breakdown:
    """Tax placement, with unclassified accounts held out rather than assumed.

    An account whose tax treatment we have not confirmed is excluded from the
    percentages and reported separately. Bucketing it as taxable would make the
    analysis look complete while quietly being wrong -- and on the real corpus
    that is not a rare edge case.
    """
    known: list[HoldingRow] = []
    excluded = ZERO
    for holding in view.holdings:
        if not holding.counts_toward_allocation:
            continue
        if holding.tax_treatment == TaxTreatment.UNKNOWN.value:
            excluded += holding.value
        else:
            known.append(holding)

    # Cash sits in an account, so it inherits that account's treatment.
    cash_by_treatment: dict[str, Decimal] = {}
    for account in view.accounts:
        if account.cash_balance == ZERO:
            continue
        if account.tax_treatment == TaxTreatment.UNKNOWN.value:
            excluded += account.cash_balance
        else:
            cash_by_treatment[account.tax_treatment] = (
                cash_by_treatment.get(account.tax_treatment, ZERO) + account.cash_balance
            )

    breakdown = _group(known, lambda h: h.tax_treatment)
    if cash_by_treatment:
        merged: dict[str, tuple[Decimal, int]] = {
            s.key: (s.value, s.position_count) for s in breakdown.slices
        }
        for treatment, cash in cash_by_treatment.items():
            value, count = merged.get(treatment, (ZERO, 0))
            merged[treatment] = (value + cash, count)
        total = sum((v for v, _ in merged.values()), ZERO)
        breakdown = Breakdown(
            slices=tuple(
                sorted(
                    (
                        Slice(
                            key=k,
                            label=k.replace("_", " ").title(),
                            value=v,
                            fraction=ZERO if total == ZERO else v / total,
                            position_count=c,
                        )
                        for k, (v, c) in merged.items()
                    ),
                    key=lambda s: (-s.value, s.key),
                )
            ),
            total=total,
        )

    return Breakdown(
        slices=breakdown.slices,
        total=breakdown.total,
        excluded_value=excluded,
        excluded_label=(
            "accounts with no confirmed tax treatment, excluded from these percentages"
            if excluded > ZERO
            else ""
        ),
    )


def by_account(view: PortfolioView) -> Breakdown:
    total = view.total_value
    slices = tuple(
        sorted(
            (
                Slice(
                    key=a.account_id,
                    label=a.display_name,
                    value=a.total_value,
                    fraction=ZERO if total == ZERO else a.total_value / total,
                    position_count=a.position_count,
                )
                for a in view.accounts
            ),
            key=lambda s: (-s.value, s.label),
        )
    )
    return Breakdown(slices=slices, total=total)
