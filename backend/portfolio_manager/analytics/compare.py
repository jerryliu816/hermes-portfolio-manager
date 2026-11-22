"""Comparing two snapshots.

Deliberately never called a "return". The difference between two snapshots
mixes market movement with contributions, withdrawals, and -- in this system
especially -- changes in which source supplied an account. Presenting that as
performance would be misleading, so everything here is labelled as a change in
value and the confounders are reported alongside.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from .allocation import Breakdown, by_asset_class
from .view import ZERO, PortfolioView


@dataclass(frozen=True)
class PositionChange:
    label: str
    account_name: str
    symbol: str | None
    before: Decimal
    after: Decimal

    @property
    def delta(self) -> Decimal:
        return self.after - self.before

    @property
    def is_new(self) -> bool:
        return self.before == ZERO and self.after != ZERO

    @property
    def is_removed(self) -> bool:
        return self.after == ZERO and self.before != ZERO


@dataclass(frozen=True)
class AllocationDrift:
    key: str
    label: str
    before_fraction: Decimal
    after_fraction: Decimal

    @property
    def drift(self) -> Decimal:
        """Change in percentage points, as a fraction."""
        return self.after_fraction - self.before_fraction


@dataclass(frozen=True)
class SnapshotComparison:
    before_date: dt.date
    after_date: dt.date
    before_value: Decimal
    after_value: Decimal
    new_positions: tuple[PositionChange, ...]
    removed_positions: tuple[PositionChange, ...]
    changed_positions: tuple[PositionChange, ...]
    drift: tuple[AllocationDrift, ...]
    #: Reasons the headline change is not a performance figure.
    confounders: list[str] = field(default_factory=list)

    @property
    def value_change(self) -> Decimal:
        return self.after_value - self.before_value

    @property
    def value_change_fraction(self) -> Decimal | None:
        if self.before_value == ZERO:
            return None
        return self.value_change / self.before_value

    def headline(self) -> str:
        """Wording that does not imply performance."""
        direction = "increased" if self.value_change >= ZERO else "decreased"
        return f"portfolio value {direction} by {abs(self.value_change)}"


def _position_key(holding: object) -> tuple[str, str]:
    return (
        holding.account_id,  # type: ignore[attr-defined]
        holding.security_id,  # type: ignore[attr-defined]
    )


def compare_snapshots(before: PortfolioView, after: PortfolioView) -> SnapshotComparison:
    before_map = {_position_key(h): h for h in before.holdings if h.counts_toward_allocation}
    after_map = {_position_key(h): h for h in after.holdings if h.counts_toward_allocation}

    new: list[PositionChange] = []
    removed: list[PositionChange] = []
    changed: list[PositionChange] = []

    for key, holding in after_map.items():
        previous = before_map.get(key)
        change = PositionChange(
            label=holding.symbol or holding.security_name,
            account_name=holding.account_name,
            symbol=holding.symbol,
            before=previous.value if previous else ZERO,
            after=holding.value,
        )
        if previous is None:
            new.append(change)
        elif change.delta != ZERO:
            changed.append(change)

    for key, holding in before_map.items():
        if key not in after_map:
            removed.append(
                PositionChange(
                    label=holding.symbol or holding.security_name,
                    account_name=holding.account_name,
                    symbol=holding.symbol,
                    before=holding.value,
                    after=ZERO,
                )
            )

    drift = _allocation_drift(by_asset_class(before), by_asset_class(after))

    confounders: list[str] = []
    before_sources = {a.account_id: a.supplied_by for a in before.accounts}
    switched = [
        a.display_name
        for a in after.accounts
        if a.account_id in before_sources and before_sources[a.account_id] != a.supplied_by
    ]
    if switched:
        confounders.append(
            "the source supplying "
            + ", ".join(switched)
            + " changed between these snapshots, so part of the difference reflects "
            "better data rather than market movement"
        )
    if before.completeness != "complete" or after.completeness != "complete":
        confounders.append(
            "at least one of these snapshots is incomplete, so the comparison omits "
            "one or more accounts"
        )
    stale_before = sum((a.total_value for a in before.accounts if a.is_stale), ZERO)
    stale_after = sum((a.total_value for a in after.accounts if a.is_stale), ZERO)
    if stale_before > ZERO or stale_after > ZERO:
        confounders.append(
            "some accounts are reported second-hand with an unknown as-of date, so "
            "they may not have moved between these dates even if the market did"
        )

    return SnapshotComparison(
        before_date=before.as_of_date,
        after_date=after.as_of_date,
        before_value=before.total_value,
        after_value=after.total_value,
        new_positions=tuple(sorted(new, key=lambda c: -c.after)),
        removed_positions=tuple(sorted(removed, key=lambda c: -c.before)),
        changed_positions=tuple(sorted(changed, key=lambda c: -abs(c.delta))),
        drift=tuple(drift),
        confounders=confounders,
    )


def _allocation_drift(before: Breakdown, after: Breakdown) -> list[AllocationDrift]:
    keys = {s.key for s in before.slices} | {s.key for s in after.slices}
    out: list[AllocationDrift] = []
    for key in sorted(keys):
        previous = before.get(key)
        current = after.get(key)
        out.append(
            AllocationDrift(
                key=key,
                label=(current or previous).label,  # type: ignore[union-attr]
                before_fraction=previous.fraction if previous else ZERO,
                after_fraction=current.fraction if current else ZERO,
            )
        )
    return sorted(out, key=lambda d: -abs(d.drift))
