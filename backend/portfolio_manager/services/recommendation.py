"""The recommendation and decision journal.

An analyst proposes; the user decides. This module owns that record, and it is
deliberately append-only: a recommendation deferred in March and accepted in
June has a history worth keeping, and overwriting the earlier decision would
erase the fact that it was once set aside.

The journal is also what stops an analyst from being annoying. Repeating a
suggestion the user has already rejected, without acknowledging it, is worse
than saying nothing -- so `get_recommendation_history` exists on the MCP surface
and the rejection rate is computed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models.advisory import Recommendation, RecommendationDecision


class DecisionType(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    #: Set aside for now. Deliberately distinct from rejection: an analyst may
    #: reasonably raise a deferred idea again, but not a rejected one.
    DEFERRED = "deferred"


class RecommendationStatus(StrEnum):
    #: No decision has been recorded yet.
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class JournalEntry:
    """A recommendation plus the decision history attached to it."""

    recommendation: Recommendation

    @property
    def decisions(self) -> list[RecommendationDecision]:
        """Oldest first, so the sequence reads as a narrative."""
        return sorted(self.recommendation.decisions, key=lambda d: d.created_at)

    @property
    def latest_decision(self) -> RecommendationDecision | None:
        decisions = self.decisions
        return decisions[-1] if decisions else None

    @property
    def status(self) -> RecommendationStatus:
        latest = self.latest_decision
        if latest is None:
            return RecommendationStatus.PENDING
        return RecommendationStatus(latest.decision)

    @property
    def is_open(self) -> bool:
        """Whether the user still owes this a decision.

        A deferred recommendation is open again: setting something aside is not
        the same as being finished with it.
        """
        return self.status in (RecommendationStatus.PENDING, RecommendationStatus.DEFERRED)


@dataclass(frozen=True)
class JournalStats:
    total: int
    pending: int
    accepted: int
    rejected: int
    deferred: int

    @property
    def decided(self) -> int:
        return self.accepted + self.rejected

    @property
    def acceptance_rate(self) -> Decimal | None:
        """Of the recommendations actually decided, what share was accepted.

        None when nothing has been decided -- a rate computed from no decisions
        would be a number pretending to be a signal.
        """
        if self.decided == 0:
            return None
        return Decimal(self.accepted) / Decimal(self.decided)


def list_recommendations(
    session: Session,
    *,
    status: RecommendationStatus | None = None,
    source: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[JournalEntry]:
    """The journal, newest first.

    Status is filtered in Python rather than SQL because it is derived from the
    latest decision, and expressing "the most recent row per group" in SQL here
    would cost more clarity than the query saves.
    """
    statement = select(Recommendation).options(selectinload(Recommendation.decisions))
    if source:
        statement = statement.where(Recommendation.source == source)
    if since:
        statement = statement.where(Recommendation.created_at >= since)
    statement = statement.order_by(Recommendation.created_at.desc())

    entries = [JournalEntry(r) for r in session.execute(statement).scalars()]
    if status is not None:
        entries = [e for e in entries if e.status is status]
    return entries[:limit]


def get_recommendation(session: Session, recommendation_id: str) -> JournalEntry | None:
    record = (
        session.execute(
            select(Recommendation)
            .options(selectinload(Recommendation.decisions))
            .where(Recommendation.id == recommendation_id)
        )
        .scalars()
        .first()
    )
    return JournalEntry(record) if record else None


def record_decision(
    session: Session,
    recommendation_id: str,
    decision: DecisionType,
    *,
    note: str | None = None,
    decided_by: str = "user",
) -> JournalEntry | None:
    """Append a decision. Never replaces an earlier one."""
    entry = get_recommendation(session, recommendation_id)
    if entry is None:
        return None
    session.add(
        RecommendationDecision(
            recommendation_id=entry.recommendation.id,
            decision=decision.value,
            note=note,
            decided_by=decided_by,
        )
    )
    session.flush()
    session.refresh(entry.recommendation)
    return JournalEntry(entry.recommendation)


def journal_stats(session: Session, *, days: int | None = None) -> JournalStats:
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    entries = list_recommendations(session, since=since, limit=10_000)
    counts = {status: 0 for status in RecommendationStatus}
    for entry in entries:
        counts[entry.status] += 1
    return JournalStats(
        total=len(entries),
        pending=counts[RecommendationStatus.PENDING],
        accepted=counts[RecommendationStatus.ACCEPTED],
        rejected=counts[RecommendationStatus.REJECTED],
        deferred=counts[RecommendationStatus.DEFERRED],
    )


def create_recommendation(
    session: Session,
    *,
    title: str,
    rationale: str,
    source: str = "user",
    snapshot_id: str | None = None,
    proposal: dict[str, object] | None = None,
    estimated_tax_impact: Decimal | None = None,
) -> JournalEntry:
    """Record a recommendation.

    Available to the user as well as to an analyst: writing down your own
    reasoning at the time is the point of a journal.
    """
    record = Recommendation(
        portfolio_snapshot_id=snapshot_id,
        source=source,
        title=title,
        rationale=rationale,
        proposal_json=proposal or {},
        estimated_tax_impact=estimated_tax_impact,
    )
    session.add(record)
    session.flush()
    return JournalEntry(record)
