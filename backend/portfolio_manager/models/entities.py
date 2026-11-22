"""SQLAlchemy models for the portfolio system of record.

Design constraints that the real sample data forced, and which the schema
enforces rather than merely documents:

* ``Security`` carries **no price**. The same symbol legitimately holds two
  different prices in one document (VGT at $117.51 post-split and $755.28
  pre-split, from an aggregation feed), so price is a per-position fact.
* ``Security.symbol`` is **nullable**. 12 of 86 real rows have no ticker --
  529 portfolios carry only a description.
* ``Account.institution`` is per account, not per source file. The real
  "Fidelity" export aggregates four-plus institutions.
* Enum-valued columns are VARCHAR + CHECK, not native Postgres enums.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import MONEY, Base, TimestampMixin, enum_check
from .enums import (
    AccountType,
    AssetClass,
    Completeness,
    CostBasisSource,
    ImportStatus,
    SecurityType,
    SourceRole,
    TaxTreatment,
    ValueSource,
)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class SourceFile(Base, TimestampMixin):
    """An imported document. Identical re-imports are a no-op via `sha256`."""

    __tablename__ = "source_file"
    __table_args__ = (enum_check("role", SourceRole, "ck_source_file_role"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    #: authoritative = defines which accounts and positions exist.
    #: enrichment    = may only refresh measurements on existing positions.
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The date this document reports, which may differ per file.
    source_as_of_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: data/sources/<sha256[:2]>/<sha256>
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)

    extraction_runs: Mapped[list[ExtractionRun]] = relationship(back_populates="source_file")


class ExtractionRun(Base, TimestampMixin):
    """One attempt at extracting a source file. Never overwritten in place."""

    __tablename__ = "extraction_run"
    __table_args__ = (
        enum_check("status", ImportStatus, "ck_extraction_run_status"),
        Index("ix_extraction_run_active", "source_file_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_file.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    warnings_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    reconciliation_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    #: Pages the minimizer removed before submission, so a suspicious omission
    #: is always traceable. Truncation is never silent.
    dropped_pages_json: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    source_file: Mapped[SourceFile] = relationship(back_populates="extraction_runs")


class Account(Base, TimestampMixin):
    """A brokerage account. Institution is per account, not per source file."""

    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("institution", "external_account_key", name="uq_account_institution_key"),
        enum_check("account_type", AccountType, "ck_account_type"),
        enum_check("tax_treatment", TaxTreatment, "ck_account_tax_treatment"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    institution: Mapped[str] = mapped_column(String(128), nullable=False)
    #: True when this account reaches us via another institution's aggregation
    #: feed. Aggregated data is demonstrably staler and loses gain/loss.
    is_aggregated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_account_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_account_key_masked: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AccountType.UNKNOWN.value
    )
    tax_treatment: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaxTreatment.UNKNOWN.value
    )
    owner_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 0.0-1.0. Drives whether Account Setup pre-fills or asks.
    classification_confidence: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    classification_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Which institution's documents are authoritative for this account, once
    #: decided. Null means undecided. A native source beats an aggregated one
    #: automatically; anything else is put to the user, asked once, then reused.
    preferred_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AccountMapping(Base, TimestampMixin):
    """A user-confirmed link between an authoritative and an enrichment account.

    Needed because the embedded account identifier is not unique in the real
    data: both 529 accounts embed 900000002, and one account name is masked by
    the source with no number at all. An ambiguous identifier is never matched
    automatically.
    """

    __tablename__ = "account_mapping"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "enrichment_institution", "enrichment_key", name="uq_account_mapping"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    enrichment_institution: Mapped[str] = mapped_column(String(128), nullable=False)
    enrichment_key: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Security(Base, TimestampMixin):
    """An instrument. Carries no price and may have no symbol."""

    __tablename__ = "security"
    __table_args__ = (
        UniqueConstraint("normalized_key", name="uq_security_normalized_key"),
        enum_check("security_type", SecurityType, "ck_security_type"),
        enum_check("asset_class", AssetClass, "ck_security_asset_class"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Identity key: the symbol when present, else a normalized description.
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    security_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SecurityType.UNKNOWN.value
    )
    asset_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AssetClass.UNKNOWN.value
    )


class PortfolioSnapshot(Base, TimestampMixin):
    """Authoritative portfolio state at one point in time."""

    __tablename__ = "portfolio_snapshot"
    __table_args__ = (
        enum_check("completeness", Completeness, "ck_snapshot_completeness"),
        enum_check("status", ImportStatus, "ck_snapshot_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    completeness: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Completeness.UNKNOWN.value
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Accounts present in the source but omitted, with the reason. Always
    #: surfaced on the import summary so a skip is never silent.
    skipped_accounts_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    #: Accounts an enrichment document reported that the authoritative source
    #: does not cover. Reported, never silently dropped.
    unmatched_enrichment_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portfolio_snapshot.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Left off the value-over-time chart at the user's request -- typically a
    #: single-source snapshot whose total is only part of the portfolio. A
    #: display preference only: the snapshot still exists, is still served when
    #: asked for by id, and can still be the latest.
    hidden_from_history: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    account_snapshots: Mapped[list[AccountSnapshot]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )
    positions: Mapped[list[PositionSnapshot]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class AccountSnapshot(Base, TimestampMixin):
    """One account's state within a snapshot."""

    __tablename__ = "account_snapshot"
    __table_args__ = (
        UniqueConstraint("portfolio_snapshot_id", "account_id", name="uq_account_snapshot"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    portfolio_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL"), nullable=True
    )
    #: The total the document itself reported, when it stated one. Compared
    #: against the sum of positions during reconciliation.
    reported_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    computed_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    position_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: When THIS account's data is from, which is not necessarily the snapshot's
    #: date. An aggregation feed reports accounts it synced at some earlier,
    #: unstated time; the file's own date says nothing about them.
    as_of_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: stated | inferred | unknown -- how the date above was arrived at.
    as_of_confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    #: Set when the source shows signs of not being current, e.g. an aggregated
    #: account with no intraday data. Surfaced everywhere the value appears.
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    staleness_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The institution whose document supplied this account's holdings.
    supplied_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Settlement / money-market cash the source reports separately from its
    #: holdings table. Real and material -- six figures across three accounts
    #: in the source corpus -- but not a position, so it is held here rather than
    #: fabricated as one.
    cash_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))

    snapshot: Mapped[PortfolioSnapshot] = relationship(back_populates="account_snapshots")

    @property
    def total_value(self) -> Decimal:
        """Holdings plus separately reported cash."""
        return (self.computed_value or Decimal("0")) + (self.cash_balance or Decimal("0"))


class PositionSnapshot(Base, TimestampMixin):
    """A holding of one security in one account at one point in time.

    Identity is (snapshot, account, security): the same symbol held in three
    accounts is one Security row and three PositionSnapshot rows.
    """

    __tablename__ = "position_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_snapshot_id", "account_id", "security_id", name="uq_position_identity"
        ),
        enum_check("cost_basis_source", CostBasisSource, "ck_position_cost_basis_source"),
        enum_check("value_source", ValueSource, "ck_position_value_source"),
        Index("ix_position_snapshot_security", "security_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    portfolio_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("security.id", ondelete="RESTRICT"), nullable=False
    )

    # --- the measurement tuple: replaced atomically by enrichment, never in part
    quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    market_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    cost_basis: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    unrealized_gain_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    cost_basis_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CostBasisSource.UNKNOWN.value
    )
    #: Quantity zero with no reported value: a closed position. Excluded from
    #: allocation denominators and exempt from quantity*price reconciliation.
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    value_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ValueSource.PRIMARY.value
    )
    enriched_from_source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL"), nullable=True
    )
    warnings_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    snapshot: Mapped[PortfolioSnapshot] = relationship(back_populates="positions")


class SecurityAlias(Base, TimestampMixin):
    """Links two names for the same instrument across sources.

    Needed because holdings without a ticker are identified by their
    description, and descriptions differ between documents: both 529 accounts
    hold funds that neither source gives a symbol, and neither source describes
    them the same way. Confirmed once by the user, then reused.
    """

    __tablename__ = "security_alias"
    __table_args__ = (UniqueConstraint("alias_key", name="uq_security_alias_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("security.id", ondelete="CASCADE"), nullable=False
    )
    #: The normalized key as some other source would produce it.
    alias_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_institution: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AccountAuditLog(Base, TimestampMixin):
    """Records user overrides of account classification."""

    __tablename__ = "account_audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False, default="user")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
