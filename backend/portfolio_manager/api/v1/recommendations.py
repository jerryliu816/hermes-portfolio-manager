"""The recommendation journal.

What an analyst proposes and what the user decided. Deliberately append-only:
a recommendation deferred once and accepted later has a history worth keeping.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ...schemas.responses import DecisionOut, JournalStatsOut, RecommendationOut
from ...services.recommendation import (
    DecisionType,
    JournalEntry,
    RecommendationStatus,
    create_recommendation,
    get_recommendation,
    journal_stats,
    list_recommendations,
    record_decision,
)
from ..deps import SessionDep

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


class DecisionIn(BaseModel):
    decision: DecisionType
    note: str | None = Field(
        default=None, description="Why. Worth writing: future-you will want the reasoning."
    )


class RecommendationIn(BaseModel):
    """A recommendation the user writes for themselves."""

    title: str
    rationale: str
    proposal: dict[str, object] = Field(default_factory=dict)
    estimated_tax_impact: Decimal | None = None
    snapshot_id: str | None = None


def recommendation_out(entry: JournalEntry) -> RecommendationOut:
    record = entry.recommendation
    return RecommendationOut(
        id=str(record.id),
        title=record.title,
        rationale=record.rationale,
        source=record.source,
        status=entry.status.value,
        is_open=entry.is_open,
        proposal=record.proposal_json,
        estimated_tax_impact=record.estimated_tax_impact,
        snapshot_id=str(record.portfolio_snapshot_id) if record.portfolio_snapshot_id else None,
        created_at=record.created_at,
        kind=record.kind,
        targets=record.targets_json,
        simulation=record.simulation_json,
        context_ids=[str(c) for c in (record.context_ids or [])],
        advisor_session_id=str(record.advisor_session_id) if record.advisor_session_id else None,
        decisions=[
            DecisionOut(
                id=str(d.id),
                decision=d.decision,
                note=d.note,
                decided_by=d.decided_by,
                decided_at=d.created_at,
            )
            for d in entry.decisions
        ],
    )


@router.get("", response_model=list[RecommendationOut])
def list_journal(
    session: SessionDep,
    status_filter: Annotated[
        RecommendationStatus | None,
        Query(alias="status", description="Filter by current status"),
    ] = None,
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[RecommendationOut]:
    return [
        recommendation_out(e)
        for e in list_recommendations(session, status=status_filter, source=source, limit=limit)
    ]


@router.get("/open", response_model=list[RecommendationOut])
def list_open(session: SessionDep) -> list[RecommendationOut]:
    """Recommendations still awaiting a decision, including deferred ones."""
    return [recommendation_out(e) for e in list_recommendations(session, limit=1000) if e.is_open]


@router.get("/stats", response_model=JournalStatsOut)
def get_stats(
    session: SessionDep,
    days: Annotated[int | None, Query(ge=1, description="Only count the last N days")] = None,
) -> JournalStatsOut:
    stats = journal_stats(session, days=days)
    return JournalStatsOut(
        total_recommendations=stats.total,
        pending=stats.pending,
        accepted=stats.accepted,
        rejected=stats.rejected,
        deferred=stats.deferred,
        decided=stats.decided,
        acceptance_rate=stats.acceptance_rate,
    )


@router.post("", response_model=RecommendationOut, status_code=status.HTTP_201_CREATED)
def add_recommendation(payload: RecommendationIn, session: SessionDep) -> RecommendationOut:
    """Record your own reasoning, not just an analyst's."""
    return recommendation_out(
        create_recommendation(
            session,
            title=payload.title,
            rationale=payload.rationale,
            source="user",
            snapshot_id=payload.snapshot_id,
            proposal=payload.proposal,
            estimated_tax_impact=payload.estimated_tax_impact,
        )
    )


@router.get("/{recommendation_id}", response_model=RecommendationOut)
def get_one(recommendation_id: str, session: SessionDep) -> RecommendationOut:
    entry = get_recommendation(session, recommendation_id)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"recommendation {recommendation_id} not found"
        )
    return recommendation_out(entry)


@router.post("/{recommendation_id}/decision", response_model=RecommendationOut)
def decide(recommendation_id: str, payload: DecisionIn, session: SessionDep) -> RecommendationOut:
    """Record a decision. Appends; it never replaces an earlier one."""
    entry = record_decision(session, recommendation_id, payload.decision, note=payload.note)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"recommendation {recommendation_id} not found"
        )
    return recommendation_out(entry)
