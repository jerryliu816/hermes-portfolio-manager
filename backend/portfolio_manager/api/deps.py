"""Shared API dependencies."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import get_session_factory, session_scope
from ..models.entities import PortfolioSnapshot
from ..services.hermes_client import HermesClient


def get_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_session_opener() -> Callable[[], AbstractContextManager[Session]]:
    """How to open a session of one's own, for work that outlives the request.

    A streamed response runs after the request's session has closed. Injected
    rather than imported so tests can point it at their own transaction.
    """
    return session_scope


SessionOpenerDep = Annotated[
    Callable[[], AbstractContextManager[Session]], Depends(get_session_opener)
]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def latest_snapshot(session: Session) -> PortfolioSnapshot:
    """The most recent snapshot that has not been superseded.

    A superseded snapshot is a competing account of the same period, so serving
    one as "current" would show a portfolio the user has already corrected.
    """
    snapshot = (
        session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.superseded_by.is_(None))
            .order_by(PortfolioSnapshot.as_of_date.desc(), PortfolioSnapshot.created_at.desc())
        )
        .scalars()
        .first()
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no portfolio snapshot exists yet; import a document first",
        )
    return snapshot


def resolve_snapshot(session: Session, snapshot_id: str | None) -> PortfolioSnapshot:
    if snapshot_id is None:
        return latest_snapshot(session)
    snapshot = session.get(PortfolioSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"snapshot {snapshot_id} not found"
        )
    return snapshot


def get_hermes_client(settings: SettingsDep) -> HermesClient:
    return HermesClient(settings)


HermesDep = Annotated[HermesClient, Depends(get_hermes_client)]
