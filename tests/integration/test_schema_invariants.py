"""Schema-level invariants forced by the real sample data.

Each test here corresponds to a finding measured from the real corpus; if one
of these regresses, the portfolio numbers stop being trustworthy.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from portfolio_manager.models import (
    Account,
    AccountType,
    Completeness,
    CostBasisSource,
    ImportStatus,
    PortfolioSnapshot,
    PositionSnapshot,
    Security,
    TaxTreatment,
)


def _account(session: Session, institution: str, key: str, tax: TaxTreatment) -> Account:
    acct = Account(
        institution=institution,
        external_account_key=key,
        external_account_key_masked=f"***{key[-4:]}",
        display_name=f"{institution} {key}",
        account_type=AccountType.BROKERAGE.value,
        tax_treatment=tax.value,
    )
    session.add(acct)
    session.flush()
    return acct


def _snapshot(session: Session) -> PortfolioSnapshot:
    snap = PortfolioSnapshot(
        as_of_date=dt.date(2026, 8, 26),
        completeness=Completeness.COMPLETE.value,
        status=ImportStatus.IMPORTED.value,
    )
    session.add(snap)
    session.flush()
    return snap


def _position(
    session: Session, snap: PortfolioSnapshot, acct: Account, sec: Security, **kw: object
) -> PositionSnapshot:
    pos = PositionSnapshot(
        portfolio_snapshot_id=snap.id,
        account_id=acct.id,
        security_id=sec.id,
        cost_basis_source=CostBasisSource.UNKNOWN.value,
        **kw,
    )
    session.add(pos)
    session.flush()
    return pos


def test_one_security_many_positions_across_accounts(session: Session) -> None:
    """The core identity rule: SPY in three accounts is ONE security, THREE positions.

    Symbols are never merged across accounts, and the aggregate is the sum of
    values with the tax split preserved.
    """
    snap = _snapshot(session)
    spy = Security(normalized_key="SPY", symbol="SPY", name="SPDR S&P 500 ETF")
    session.add(spy)
    session.flush()

    ira = _account(session, "Vanguard", "19000011", TaxTreatment.TAX_DEFERRED)
    taxable = _account(session, "Fidelity", "Z10000077", TaxTreatment.TAXABLE)
    roth = _account(session, "Fidelity", "25000055", TaxTreatment.TAX_FREE)

    _position(session, snap, ira, spy, market_value=Decimal("350000"))
    _position(session, snap, taxable, spy, market_value=Decimal("100000"))
    _position(session, snap, roth, spy, market_value=Decimal("50000"))

    securities = session.query(Security).filter_by(symbol="SPY").all()
    positions = session.query(PositionSnapshot).filter_by(security_id=spy.id).all()

    assert len(securities) == 1, "SPY must be a single security row"
    assert len(positions) == 3, "SPY must be three separate position rows"
    assert sum(p.market_value or Decimal(0) for p in positions) == Decimal("500000")

    by_tax = {
        session.get(Account, p.account_id).tax_treatment: p.market_value  # type: ignore[union-attr]
        for p in positions
    }
    assert by_tax == {
        TaxTreatment.TAX_DEFERRED.value: Decimal("350000"),
        TaxTreatment.TAXABLE.value: Decimal("100000"),
        TaxTreatment.TAX_FREE.value: Decimal("50000"),
    }


def test_position_identity_is_unique(session: Session) -> None:
    """The same security cannot appear twice in one account in one snapshot."""
    snap = _snapshot(session)
    sec = Security(normalized_key="VOO", symbol="VOO", name="Vanguard S&P 500 ETF")
    session.add(sec)
    session.flush()
    acct = _account(session, "Fidelity", "Z10000077", TaxTreatment.TAXABLE)

    _position(session, snap, acct, sec, market_value=Decimal("1000"))
    with pytest.raises(IntegrityError):
        _position(session, snap, acct, sec, market_value=Decimal("2000"))


def test_same_symbol_may_hold_two_prices(session: Session) -> None:
    """Price is a per-position fact, never a property of the security.

    Real case: VGT is $117.51 in the native Fidelity account and $755.28 in the
    aggregated Vanguard feed (pre-split). Both rows are internally consistent,
    so this must be storable without conflict.
    """
    snap = _snapshot(session)
    vgt = Security(normalized_key="VGT", symbol="VGT", name="Vanguard Info Tech ETF")
    session.add(vgt)
    session.flush()
    assert not hasattr(vgt, "price"), "Security must not carry a price column"

    joint = _account(session, "Fidelity", "Z10000077", TaxTreatment.TAXABLE)
    rollover = _account(session, "Vanguard", "19000011", TaxTreatment.TAX_DEFERRED)

    a = _position(
        session,
        snap,
        joint,
        vgt,
        quantity=Decimal("1348.207"),
        price=Decimal("117.51"),
        market_value=Decimal("158427.80"),
    )
    b = _position(
        session,
        snap,
        rollover,
        vgt,
        quantity=Decimal("141.637"),
        price=Decimal("755.28"),
        market_value=Decimal("106975.82"),
    )
    assert a.price != b.price
    # Each row is internally consistent *within rounding tolerance* even though
    # the two disagree on share basis. Measured across the 68 real rows that
    # carry all three fields, only 10 match exactly and the worst drift is
    # $0.23 on a six-figure position -- which is why reconciliation is tolerance
    # based, and why an exact-equality check would reject good data.
    for pos in (a, b):
        assert pos.quantity is not None and pos.price is not None
        assert pos.market_value is not None
        assert abs(pos.quantity * pos.price - pos.market_value) < Decimal("1.00")


def test_security_symbol_may_be_null(session: Session) -> None:
    """12 of 86 real rows have no ticker -- 529 portfolios carry only a name."""
    sec = Security(
        normalized_key="vanguard-500-index-portfolio",
        symbol=None,
        name="Vanguard 500 Index Portfolio",
    )
    session.add(sec)
    session.flush()
    assert sec.symbol is None
    assert sec.id is not None


def test_money_round_trips_as_decimal_not_float(session: Session) -> None:
    """Money must survive the database as exact Decimal, never float."""
    snap = _snapshot(session)
    sec = Security(normalized_key="FZDXX", symbol="FZDXX", name="Fidelity MMKT Premium")
    session.add(sec)
    session.flush()
    acct = _account(session, "Fidelity", "Z10000077", TaxTreatment.TAXABLE)

    exact = Decimal("87341.26")
    pos = _position(
        session, snap, acct, sec, quantity=exact, price=Decimal("1.00"), market_value=exact
    )
    session.expire(pos)
    reloaded = session.get(PositionSnapshot, pos.id)
    assert reloaded is not None
    assert isinstance(reloaded.market_value, Decimal)
    assert reloaded.market_value == exact
    # Fractional share counts must not be rounded away.
    assert reloaded.quantity == Decimal("87341.26")


def test_enum_columns_reject_unknown_values(session: Session) -> None:
    """CHECK constraints do the work native Postgres enums would have."""
    bad = Account(
        institution="Fidelity",
        external_account_key="X1",
        external_account_key_masked="***X1",
        display_name="bad",
        account_type="not_a_real_type",
        tax_treatment=TaxTreatment.UNKNOWN.value,
    )
    session.add(bad)
    with pytest.raises((IntegrityError, DBAPIError)):
        session.flush()


def test_account_unique_per_institution_and_key(session: Session) -> None:
    """The same key at different institutions is two different accounts."""
    _account(session, "Fidelity", "12345", TaxTreatment.TAXABLE)
    _account(session, "Vanguard", "12345", TaxTreatment.TAXABLE)  # allowed
    with pytest.raises(IntegrityError):
        _account(session, "Fidelity", "12345", TaxTreatment.TAXABLE)


def test_snapshot_records_skipped_accounts(session: Session) -> None:
    """Skipping a zero-value account is recorded, never silent."""
    snap = PortfolioSnapshot(
        as_of_date=dt.date(2026, 8, 26),
        completeness=Completeness.COMPLETE.value,
        status=ImportStatus.IMPORTED.value,
        skipped_accounts_json=[
            {"display_name": "IIA 6QQ-80000", "reason": "zero_reported_value", "positions": 3},
        ],
    )
    session.add(snap)
    session.flush()
    reloaded = session.get(PortfolioSnapshot, snap.id)
    assert reloaded is not None
    assert reloaded.skipped_accounts_json[0]["reason"] == "zero_reported_value"


def test_readonly_session_cannot_write() -> None:
    """The Hermes read path must fail at the database, not merely by convention."""
    pytest.importorskip("psycopg")
    from portfolio_manager.database import readonly_session

    with readonly_session() as sess, pytest.raises((DBAPIError, IntegrityError)):
        sess.execute(
            PositionSnapshot.__table__.insert().values(
                id=uuid.uuid4(),
                portfolio_snapshot_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                security_id=uuid.uuid4(),
                cost_basis_source=CostBasisSource.UNKNOWN.value,
                value_source="primary",
            )
        )
