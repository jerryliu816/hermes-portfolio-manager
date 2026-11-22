"""Unfiling a snapshot, so a mis-filed document can be imported again.

This exists for one recurring situation. Two documents describing one portfolio
are exported on different days, the second states its own date, and it opens a
snapshot of its own instead of joining the first -- what `--as-of` is for, and
what the import now warns about. Putting that right means importing the second
document again, and imports deduplicate by content hash, so the retry is a
no-op until the first attempt is unfiled. Before this it took hand-written SQL.

Two deliberate limits:

* **The preserved bytes stay on disk.** A document is the only artefact here
  that cannot be reconstructed -- the database records what it was read to say,
  never the thing itself. Removal drops the record and leaves the file, which
  costs a little space and makes the operation recoverable. A re-import writes
  the same content-addressed path anyway.

* **It refuses rather than guesses.** A snapshot cited by the recommendation
  journal, or superseding an earlier one, has consequences beyond itself; those
  are reported and require an explicit override rather than being swept along.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.advisory import Recommendation
from ..models.entities import (
    AccountSnapshot,
    ExtractionRun,
    PortfolioSnapshot,
    PositionSnapshot,
    SourceFile,
)


class SnapshotRemovalError(Exception):
    """The snapshot could not be identified, or removing it would cost more
    than the caller has agreed to."""


@dataclass(frozen=True)
class RemovalPlan:
    """Everything removing this snapshot would do, before anything is done."""

    snapshot: PortfolioSnapshot
    account_count: int
    position_count: int
    #: Documents that fed only this snapshot. Their records go, so the files
    #: can be imported again; the bytes stay on disk.
    documents: tuple[SourceFile, ...]
    #: Documents that also fed another snapshot, so their records must stay --
    #: and those files therefore stay un-importable.
    shared_documents: tuple[SourceFile, ...]
    #: Journal entries citing this snapshot. They survive, without provenance.
    recommendation_count: int
    #: Snapshots this one superseded. Deleting it makes them live again.
    resurrected: tuple[PortfolioSnapshot, ...]

    @property
    def needs_force(self) -> bool:
        return bool(self.recommendation_count or self.resurrected)

    def objections(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.recommendation_count:
            reasons.append(
                f"{self.recommendation_count} recommendation(s) cite this snapshot; they "
                "will survive but lose the portfolio they were written against"
            )
        for earlier in self.resurrected:
            reasons.append(
                f"the {earlier.as_of_date.isoformat()} snapshot was superseded by this one "
                "and becomes live again"
            )
        return tuple(reasons)


def find_snapshot(session: Session, reference: str) -> PortfolioSnapshot:
    """Resolve a snapshot from an id, an id prefix, or an as-of date.

    A date is how people think about snapshots and an id prefix is what the
    listing prints, so both work. An ambiguous reference is an error rather
    than a guess: this deletes things.
    """
    reference = reference.strip()
    try:
        as_of = dt.date.fromisoformat(reference)
    except ValueError:
        pass
    else:
        matches = list(
            session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.as_of_date == as_of)
            ).scalars()
        )
        return _exactly_one(matches, reference)

    candidates = list(session.execute(select(PortfolioSnapshot)).scalars())
    matches = [s for s in candidates if str(s.id).startswith(reference.lower())]
    if not matches:
        try:
            wanted = uuid.UUID(reference)
        except ValueError:
            matches = []
        else:
            matches = [s for s in candidates if s.id == wanted]
    return _exactly_one(matches, reference)


def _exactly_one(matches: list[PortfolioSnapshot], reference: str) -> PortfolioSnapshot:
    if not matches:
        raise SnapshotRemovalError(f"no snapshot matches {reference!r}")
    if len(matches) > 1:
        dates = ", ".join(f"{s.as_of_date} ({str(s.id)[:8]})" for s in matches)
        raise SnapshotRemovalError(f"{reference!r} matches {len(matches)} snapshots: {dates}")
    return matches[0]


def plan_removal(session: Session, snapshot: PortfolioSnapshot) -> RemovalPlan:
    """Work out what removing this snapshot costs, without removing it."""
    accounts = list(
        session.execute(
            select(AccountSnapshot).where(AccountSnapshot.portfolio_snapshot_id == snapshot.id)
        ).scalars()
    )
    position_count = len(
        list(
            session.execute(
                select(PositionSnapshot.id).where(
                    PositionSnapshot.portfolio_snapshot_id == snapshot.id
                )
            ).scalars()
        )
    )

    # A document belongs to this snapshot if it supplied one of its accounts,
    # or if it was filed on this date and supplied nothing -- the losing half
    # of a precedence decision is still filed here, and is exactly the document
    # someone is most likely to be re-importing.
    supplied = {a.source_file_id for a in accounts if a.source_file_id is not None}
    filed_here = {
        f.id
        for f in session.execute(
            select(SourceFile).where(SourceFile.source_as_of_date == snapshot.as_of_date)
        ).scalars()
    }
    candidate_ids = supplied | filed_here

    elsewhere = {
        a.source_file_id
        for a in session.execute(
            select(AccountSnapshot).where(
                AccountSnapshot.portfolio_snapshot_id != snapshot.id,
                AccountSnapshot.source_file_id.is_not(None),
            )
        ).scalars()
    }

    documents: list[SourceFile] = []
    shared: list[SourceFile] = []
    for file_id in sorted(candidate_ids, key=str):
        record = session.get(SourceFile, file_id)
        if record is None:
            continue
        (shared if file_id in elsewhere else documents).append(record)

    recommendations = len(
        list(
            session.execute(
                select(Recommendation.id).where(Recommendation.portfolio_snapshot_id == snapshot.id)
            ).scalars()
        )
    )
    resurrected = tuple(
        session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.superseded_by == snapshot.id)
        ).scalars()
    )

    return RemovalPlan(
        snapshot=snapshot,
        account_count=len(accounts),
        position_count=position_count,
        documents=tuple(documents),
        shared_documents=tuple(shared),
        recommendation_count=recommendations,
        resurrected=resurrected,
    )


def remove_snapshot(session: Session, plan: RemovalPlan, *, force: bool = False) -> None:
    """Carry out a plan. Accounts and securities are never touched.

    They are shared across snapshots and outlive any one of them; the database
    enforces that with a restricting foreign key, so a mistake here fails loudly
    rather than deleting history.
    """
    if plan.needs_force and not force:
        raise SnapshotRemovalError("; ".join(plan.objections()))

    for document in plan.documents:
        # The runs go with the document explicitly. The database would cascade
        # them, but the ORM tries to orphan them first and hits a NOT NULL.
        for run in session.execute(
            select(ExtractionRun).where(ExtractionRun.source_file_id == document.id)
        ).scalars():
            session.delete(run)
        session.delete(document)
    session.delete(plan.snapshot)
    session.flush()
