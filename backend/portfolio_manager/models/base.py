"""Declarative base and shared column types."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Annotated

from sqlalchemy import CheckConstraint, DateTime, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Money and quantities. NUMERIC(20,6) everywhere -- never float. Six decimal
#: places because real share counts carry three decimals (shaped like 318.457)
#: and money-market balances are large two-decimal amounts (like 87341.26).
MONEY = Numeric(20, 6)

Money = Annotated[Decimal, mapped_column(MONEY)]
OptMoney = Annotated[Decimal | None, mapped_column(MONEY, nullable=True)]


def enum_check(column: str, enum_cls: type[Enum], name: str) -> CheckConstraint:
    """A CHECK constraint restricting `column` to the values of `enum_cls`.

    Used instead of native Postgres enum types: adding a value later is a
    one-line change here, whereas ALTER TYPE does not autogenerate in Alembic.
    """
    allowed = ", ".join(f"'{m.value}'" for m in enum_cls)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


ShortStr = Annotated[str, mapped_column(String(64))]
MedStr = Annotated[str, mapped_column(String(255))]
