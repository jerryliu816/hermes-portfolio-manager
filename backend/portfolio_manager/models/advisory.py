"""Advisory tables: policy, philosophy, context, recommendations, decisions.

Deliberately separate from portfolio truth. Nothing here is authoritative
holdings data. The Hermes MCP layer may write only to the recommendation,
decision and advisor-context tables.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import MONEY, Base, TimestampMixin, enum_check
from .enums import ContextCreator, ContextKind, RecommendationKind, TextExtraction


class InvestmentPolicy(Base, TimestampMixin):
    """The investment policy statement, editable as YAML or through the UI."""

    __tablename__ = "investment_policy"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Target allocations, drift bands, constraints. Free-form so the policy can
    #: evolve without a migration.
    document_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    raw_yaml: Mapped[str | None] = mapped_column(Text, nullable=True)


class InvestmentPhilosophy(Base, TimestampMixin):
    """The user's investment philosophy, in their own words.

    Prose, not a schema: risk tolerance, horizon, beliefs, what they will never
    hold. Kept apart from `InvestmentPolicy`, whose schema is closed so that
    every rule in it can be checked; nothing here is checked, only read.
    Versioned the same way -- saving inserts, never overwrites.
    """

    __tablename__ = "investment_philosophy"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)


class AdvisorContext(Base, TimestampMixin):
    """Something the user wants the advisor to take into account.

    A view, a preference, a URL or a document. Advisory input, never portfolio
    truth: an advisor may write these (when the user says "remember that I
    think ..."), and they are marked as its paraphrase until the user confirms.
    """

    __tablename__ = "advisor_context"
    __table_args__ = (
        enum_check("kind", ContextKind, "ck_advisor_context_kind"),
        enum_check("created_by", ContextCreator, "ck_advisor_context_created_by"),
        enum_check("extraction", TextExtraction, "ck_advisor_context_extraction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The user's words, or the text extracted from a document or page.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extraction: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: For documents: the preserved original, under data/context/.
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stored_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: A view about "the next Fed meeting" stops being true. Null = no expiry.
    expires_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    #: False for an advisor's paraphrase until the user confirms it.
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AdvisorSession(Base, TimestampMixin):
    """A conversation with the advisor, held by Hermes and indexed here.

    Hermes owns the messages; this row lets the Advisor page list past
    conversations and remember the last set of trades the advisor simulated, so
    the proposal panel can recompute it with the engine rather than trust a
    figure from the chat.
    """

    __tablename__ = "advisor_session"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hermes_session_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    portfolio_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portfolio_snapshot.id", ondelete="SET NULL"), nullable=True
    )
    #: The arguments of the advisor's latest `simulate_trades` call, verbatim.
    last_trades_json: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    #: Proposed targets that simulation ran against, when not the policy's own.
    last_targets_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    forked_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("advisor_session.id", ondelete="SET NULL"), nullable=True
    )


class Recommendation(Base, TimestampMixin):
    """An analyst suggestion. Never mutates holdings."""

    __tablename__ = "recommendation"
    __table_args__ = (enum_check("kind", RecommendationKind, "ck_recommendation_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portfolio_snapshot.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="hermes")
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: Structured proposal (account, security, action, amount) when the analyst
    #: provides one. Advisory only.
    proposal_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    estimated_tax_impact: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecommendationKind.NOTE.value, server_default="note"
    )
    #: Proposed target weights, as decimal strings: the change itself for a
    #: target change, or what the trades were measured against.
    targets_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    #: The engine's own report, computed by the server when saved -- never
    #: figures supplied by the analyst.
    simulation_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    #: Which advisor-context items the recommendation relied on.
    context_ids: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    advisor_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("advisor_session.id", ondelete="SET NULL"), nullable=True
    )

    decisions: Mapped[list[RecommendationDecision]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan"
    )


class RecommendationDecision(Base, TimestampMixin):
    """What the human decided about a recommendation."""

    __tablename__ = "recommendation_decision"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation.id", ondelete="CASCADE"), nullable=False
    )
    #: accepted | rejected | deferred
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False, default="user")

    recommendation: Mapped[Recommendation] = relationship(back_populates="decisions")
