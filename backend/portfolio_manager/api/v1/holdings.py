"""Holdings and snapshot endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...analytics import compare_snapshots, load_portfolio
from ...models.entities import PortfolioSnapshot
from ...schemas.responses import (
    AllocationDriftOut,
    HoldingOut,
    PositionChangeOut,
    SnapshotComparisonOut,
    SnapshotOut,
    SnapshotRemovalOut,
)
from ...services.snapshot_removal import RemovalPlan, plan_removal, remove_snapshot
from ..deps import SessionDep, resolve_snapshot

router = APIRouter(tags=["holdings"])


@router.get("/holdings", response_model=list[HoldingOut])
def list_holdings(
    session: SessionDep,
    snapshot_id: str | None = None,
    account_id: str | None = None,
    symbol: str | None = None,
    tax_treatment: str | None = None,
    asset_class: str | None = None,
    include_closed: Annotated[
        bool, Query(description="Include zero-quantity holdings that are no longer held")
    ] = False,
) -> list[HoldingOut]:
    view = load_portfolio(session, resolve_snapshot(session, snapshot_id))
    rows = list(view.holdings)
    if not include_closed:
        rows = [h for h in rows if not h.is_closed]
    if account_id:
        rows = [h for h in rows if h.account_id == account_id]
    if symbol:
        wanted = symbol.upper()
        rows = [h for h in rows if (h.symbol or "").upper() == wanted]
    if tax_treatment:
        rows = [h for h in rows if h.tax_treatment == tax_treatment]
    if asset_class:
        rows = [h for h in rows if h.asset_class == asset_class]
    rows.sort(key=lambda h: h.value, reverse=True)
    return [HoldingOut.model_validate(h) for h in rows]


class SnapshotVisibilityIn(BaseModel):
    hidden_from_history: bool = Field(
        description="Leave this snapshot off the value-over-time chart. A display "
        "preference only: the snapshot is kept and still served by id."
    )


def _snapshot_out(session: SessionDep, snapshot: PortfolioSnapshot) -> SnapshotOut:
    view = load_portfolio(session, snapshot)
    return SnapshotOut(
        id=str(snapshot.id),
        as_of_date=snapshot.as_of_date,
        completeness=snapshot.completeness,
        status=snapshot.status,
        total_value=view.total_value,
        account_count=len(view.accounts),
        position_count=len(view.holdings),
        superseded_by=str(snapshot.superseded_by) if snapshot.superseded_by else None,
        hidden_from_history=snapshot.hidden_from_history,
        skipped_accounts=list(snapshot.skipped_accounts_json or []),
        unmatched_enrichment=list(snapshot.unmatched_enrichment_json or []),
    )


@router.get("/snapshots", response_model=list[SnapshotOut])
def list_snapshots(session: SessionDep) -> list[SnapshotOut]:
    """Every snapshot, newest first, including superseded and hidden ones."""
    snapshots = session.execute(
        select(PortfolioSnapshot).order_by(
            PortfolioSnapshot.as_of_date.desc(), PortfolioSnapshot.created_at.desc()
        )
    ).scalars()
    return [_snapshot_out(session, snapshot) for snapshot in snapshots]


@router.patch("/snapshots/{snapshot_id}", response_model=SnapshotOut)
def set_snapshot_visibility(
    snapshot_id: str, payload: SnapshotVisibilityIn, session: SessionDep
) -> SnapshotOut:
    """Show or hide a snapshot on the value-over-time chart.

    For a snapshot whose total is only part of the portfolio -- one document
    imported on its own -- which would otherwise plot as a false dip. Nothing is
    deleted, and which snapshot is current is unchanged.
    """
    snapshot = _get_snapshot(session, snapshot_id)
    snapshot.hidden_from_history = payload.hidden_from_history
    session.flush()
    return _snapshot_out(session, snapshot)


def _get_snapshot(session: SessionDep, snapshot_id: str) -> PortfolioSnapshot:
    snapshot = session.get(PortfolioSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"snapshot {snapshot_id} not found")
    return snapshot


def _removal_out(session: SessionDep, plan: RemovalPlan) -> SnapshotRemovalOut:
    return SnapshotRemovalOut(
        snapshot=_snapshot_out(session, plan.snapshot),
        account_count=plan.account_count,
        position_count=plan.position_count,
        documents=[d.original_filename for d in plan.documents],
        shared_documents=[d.original_filename for d in plan.shared_documents],
        recommendation_count=plan.recommendation_count,
        resurrected=[s.as_of_date for s in plan.resurrected],
        objections=list(plan.objections()),
        needs_force=plan.needs_force,
    )


@router.get("/snapshots/{snapshot_id}/removal", response_model=SnapshotRemovalOut)
def preview_snapshot_removal(snapshot_id: str, session: SessionDep) -> SnapshotRemovalOut:
    """What deleting this snapshot would do. Changes nothing.

    Read this before `DELETE /snapshots/{id}`: it names the documents that
    become importable again and anything that depends on the snapshot.
    """
    return _removal_out(session, plan_removal(session, _get_snapshot(session, snapshot_id)))


@router.delete("/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_snapshot(snapshot_id: str, session: SessionDep, force: bool = False) -> None:
    """Delete a snapshot, so its documents can be imported again.

    Permanent: the snapshot, its account and position readings, and the
    records of documents only it used are deleted. Accounts, securities and
    the preserved document files are kept. Refused with 409 when something
    else depends on the snapshot -- a journal entry citing it, or an earlier
    snapshot it superseded -- unless `force` is passed.
    """
    plan = plan_removal(session, _get_snapshot(session, snapshot_id))
    if plan.needs_force and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"kind": "removal_needs_force", "objections": list(plan.objections())},
        )
    remove_snapshot(session, plan, force=force)


@router.get("/snapshots/compare", response_model=SnapshotComparisonOut)
def compare(before_id: str, after_id: str, session: SessionDep) -> SnapshotComparisonOut:
    """Compare two snapshots.

    The result reports a change in *value*, never a return, and lists the
    confounders that make it not a performance figure.
    """
    before = session.get(PortfolioSnapshot, before_id)
    after = session.get(PortfolioSnapshot, after_id)
    if before is None or after is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "one or both snapshots were not found")

    result = compare_snapshots(load_portfolio(session, before), load_portfolio(session, after))

    def changes(items: object) -> list[PositionChangeOut]:
        return [
            PositionChangeOut(
                label=c.label,
                account_name=c.account_name,
                symbol=c.symbol,
                before=c.before,
                after=c.after,
                delta=c.delta,
            )
            for c in items  # type: ignore[attr-defined]
        ]

    return SnapshotComparisonOut(
        before_date=result.before_date,
        after_date=result.after_date,
        before_value=result.before_value,
        after_value=result.after_value,
        value_change=result.value_change,
        new_positions=changes(result.new_positions),
        removed_positions=changes(result.removed_positions),
        changed_positions=changes(result.changed_positions),
        drift=[
            AllocationDriftOut(
                key=d.key,
                label=d.label,
                before_fraction=d.before_fraction,
                after_fraction=d.after_fraction,
                drift=d.drift,
            )
            for d in result.drift
        ],
        confounders=result.confounders,
    )
