"""Loading a snapshot into flat rows that the analytics can work on.

One query layer here, so every analytic below is a pure function over plain
data. That keeps them trivially testable and keeps SQL out of the arithmetic.

The row types carry provenance -- staleness, which source supplied the account,
whether a cost basis was reported or derived -- because the analytics have to
report on their own trustworthiness, not just their results. A number that is
60% stale should say so rather than look like any other number.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.entities import (
    Account,
    AccountSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
    Security,
)

ZERO = Decimal("0")


@dataclass(frozen=True)
class HoldingRow:
    """One holding, flattened with everything an analytic needs."""

    account_id: str
    account_name: str
    institution: str
    account_type: str
    tax_treatment: str
    is_aggregated: bool
    account_is_stale: bool
    supplied_by: str | None

    security_id: str
    symbol: str | None
    security_name: str
    security_type: str
    asset_class: str

    quantity: Decimal | None
    price: Decimal | None
    market_value: Decimal | None
    cost_basis: Decimal | None
    cost_basis_source: str
    unrealized_gain_loss: Decimal | None
    is_closed: bool
    value_source: str

    @property
    def value(self) -> Decimal:
        """Value for aggregation. Absent and closed both contribute nothing."""
        if self.is_closed or self.market_value is None:
            return ZERO
        return self.market_value

    @property
    def counts_toward_allocation(self) -> bool:
        """Closed and valueless holdings must not dilute a percentage."""
        return not self.is_closed and self.market_value is not None


@dataclass(frozen=True)
class AccountRow:
    """One account, including cash the source reports outside its holdings."""

    account_id: str
    display_name: str
    institution: str
    account_type: str
    tax_treatment: str
    is_aggregated: bool
    is_stale: bool
    staleness_reason: str | None
    supplied_by: str | None
    as_of_date: dt.date | None
    as_of_confidence: str
    holdings_value: Decimal
    cash_balance: Decimal
    position_count: int

    @property
    def total_value(self) -> Decimal:
        return self.holdings_value + self.cash_balance


@dataclass(frozen=True)
class PortfolioView:
    """Everything an analytic needs about one snapshot."""

    snapshot_id: str
    as_of_date: dt.date
    completeness: str
    status: str
    holdings: tuple[HoldingRow, ...]
    accounts: tuple[AccountRow, ...]
    skipped_accounts: tuple[dict[str, object], ...] = ()

    @property
    def total_value(self) -> Decimal:
        """Holdings plus separately reported cash, across every account."""
        return sum((a.total_value for a in self.accounts), ZERO)

    @property
    def holdings_value(self) -> Decimal:
        return sum((a.holdings_value for a in self.accounts), ZERO)

    @property
    def cash_value(self) -> Decimal:
        return sum((a.cash_balance for a in self.accounts), ZERO)


def load_portfolio(session: Session, snapshot: PortfolioSnapshot) -> PortfolioView:
    """Read one snapshot into flat rows."""
    account_snapshots = list(
        session.execute(
            select(AccountSnapshot).where(AccountSnapshot.portfolio_snapshot_id == snapshot.id)
        ).scalars()
    )
    accounts = {
        a.id: a
        for a in session.execute(
            select(Account).where(
                Account.id.in_([s.account_id for s in account_snapshots] or [None])
            )
        ).scalars()
    }

    account_rows: list[AccountRow] = []
    for account_snapshot in account_snapshots:
        account = accounts.get(account_snapshot.account_id)
        if account is None:
            continue
        account_rows.append(
            AccountRow(
                account_id=str(account.id),
                display_name=account.display_name,
                institution=account.institution,
                account_type=account.account_type,
                tax_treatment=account.tax_treatment,
                is_aggregated=bool(account.is_aggregated),
                is_stale=bool(account_snapshot.is_stale),
                staleness_reason=account_snapshot.staleness_reason,
                supplied_by=account_snapshot.supplied_by,
                as_of_date=account_snapshot.as_of_date,
                as_of_confidence=account_snapshot.as_of_confidence,
                holdings_value=account_snapshot.computed_value or ZERO,
                cash_balance=account_snapshot.cash_balance or ZERO,
                position_count=account_snapshot.position_count,
            )
        )

    positions = list(
        session.execute(
            select(PositionSnapshot).where(PositionSnapshot.portfolio_snapshot_id == snapshot.id)
        ).scalars()
    )
    securities = {
        s.id: s
        for s in session.execute(
            select(Security).where(Security.id.in_([p.security_id for p in positions] or [None]))
        ).scalars()
    }
    stale_by_account = {row.account_id: row.is_stale for row in account_rows}
    supplied_by_account = {row.account_id: row.supplied_by for row in account_rows}

    holdings: list[HoldingRow] = []
    for position in positions:
        account = accounts.get(position.account_id)
        security = securities.get(position.security_id)
        if account is None or security is None:
            continue
        holdings.append(
            HoldingRow(
                account_id=str(account.id),
                account_name=account.display_name,
                institution=account.institution,
                account_type=account.account_type,
                tax_treatment=account.tax_treatment,
                is_aggregated=bool(account.is_aggregated),
                account_is_stale=stale_by_account.get(str(account.id), False),
                supplied_by=supplied_by_account.get(str(account.id)),
                security_id=str(security.id),
                symbol=security.symbol,
                security_name=security.name,
                security_type=security.security_type,
                asset_class=security.asset_class,
                quantity=position.quantity,
                price=position.price,
                market_value=position.market_value,
                cost_basis=position.cost_basis,
                cost_basis_source=position.cost_basis_source,
                unrealized_gain_loss=position.unrealized_gain_loss,
                is_closed=bool(position.is_closed),
                value_source=position.value_source,
            )
        )

    return PortfolioView(
        snapshot_id=str(snapshot.id),
        as_of_date=snapshot.as_of_date,
        completeness=snapshot.completeness,
        status=snapshot.status,
        holdings=tuple(holdings),
        accounts=tuple(account_rows),
        skipped_accounts=tuple(snapshot.skipped_accounts_json or ()),
    )
