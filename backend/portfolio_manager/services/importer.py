"""The end-to-end import: a file on disk becomes portfolio state.

Composition only -- every decision lives in the module that owns it. Reading
this function top to bottom should be the clearest available description of
what importing a document actually does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..extraction.contracts import (
    DocumentExtractionProvider,
    ExtractionContext,
    ExtractionResult,
    OverviewResult,
    SourceDocument,
)
from ..hermes.trigger import get_trigger
from ..ingestion.validator import ValidationReport
from ..models.entities import ExtractionRun, PortfolioSnapshot, SourceFile
from ..models.enums import Completeness, ImportStatus, SourceRole
from ..registry_types import ImportOutcome
from .extraction_run import record_extraction_run
from .multi_source import MultiSourceResult, add_document_to_snapshot
from .snapshot import (
    SnapshotResult,
    create_snapshot,
    resolve_as_of_date,
    stated_as_of_date,
)
from .source_file import store_source_document


class DateOverrideError(Exception):
    """An `--as-of` override moved a document further than is plausible.

    Placing two documents on one date is how a portfolio is assembled from an
    aggregation plus a direct source. Placing a document on a date it has
    nothing to do with silently rewrites history instead, and the arithmetic
    cannot tell the difference: a month-old statement reconciles perfectly
    against itself. So the distance is bounded, and going past the bound has to
    be deliberate.
    """

    def __init__(self, stated: dt.date, requested: dt.date, tolerance_days: int) -> None:
        self.stated = stated
        self.requested = requested
        self.tolerance_days = tolerance_days
        super().__init__(
            f"the document states {stated.isoformat()} but you asked to place it on "
            f"{requested.isoformat()}, {abs((requested - stated).days)} days apart -- "
            f"more than the {tolerance_days}-day limit"
        )


@dataclass
class ImportResult:
    outcome: ImportOutcome
    source_file: SourceFile
    snapshot: SnapshotResult | None = None
    extraction_run: ExtractionRun | None = None
    #: Set when this document joined an existing snapshot rather than opening
    #: one, i.e. a second source describing the same date.
    multi_source: MultiSourceResult | None = None
    message: str = ""
    #: Whether Hermes was told about the new snapshot. False is the normal
    #: case: the integration is optional and off by default.
    analysis_notified: bool = False

    @property
    def created_state(self) -> bool:
        return self.outcome is ImportOutcome.IMPORTED


def import_document(
    session: Session,
    path: Path,
    provider: DocumentExtractionProvider,
    *,
    role: SourceRole = SourceRole.AUTHORITATIVE,
    as_of_date: dt.date | None = None,
    force_as_of: bool = False,
    settings: Settings | None = None,
) -> ImportResult:
    """Import one document, or report that it was already imported.

    `as_of_date` places the document on a chosen date rather than the one it
    states. That is what lets an aggregation and a direct source exported a day
    apart describe a single portfolio, since a document joins an existing
    snapshot by date.
    """
    settings = settings or get_settings()
    prepared = prepare_document(session, path, provider, role=role, settings=settings)
    if prepared.duplicate is not None:
        return prepared.duplicate
    return file_document(
        session,
        prepared,
        as_of_date=as_of_date,
        force_as_of=force_as_of,
        settings=settings,
    )


@dataclass
class PreparedDocument:
    """A document read but not yet filed.

    Reading is the expensive, irreversible-in-cost half of an import, and
    filing is where the date and the precedence order are decided. Keeping them
    apart is what lets several documents be read first and then filed in an
    order chosen from what they turned out to contain.
    """

    path: Path
    source_file: SourceFile
    overview_result: OverviewResult | None = None
    extraction: ExtractionResult | None = None
    #: Set when the file had already been imported, in which case nothing else
    #: on this object is populated.
    duplicate: ImportResult | None = None

    @property
    def account_count(self) -> int:
        """How many accounts this document describes.

        The ordering key for a multi-document import, because a joining
        document may only supply accounts the snapshot already has.
        """
        if self.extraction is None:
            return 0
        return len(self.extraction.document.accounts)


def prepare_document(
    session: Session,
    path: Path,
    provider: DocumentExtractionProvider,
    *,
    role: SourceRole = SourceRole.AUTHORITATIVE,
    settings: Settings | None = None,
) -> PreparedDocument:
    """Store and extract a document, without deciding where it belongs."""
    settings = settings or get_settings()
    document = SourceDocument.from_path(path)

    stored = store_source_document(session, document, role=role, settings=settings)
    if not stored.is_new:
        return PreparedDocument(
            path=path,
            source_file=stored.source_file,
            duplicate=ImportResult(
                outcome=ImportOutcome.DUPLICATE,
                source_file=stored.source_file,
                message=(
                    f"this file was already imported on "
                    f"{stored.source_file.created_at:%Y-%m-%d}; no new state was created"
                ),
            ),
        )

    context = ExtractionContext()
    overview_result = provider.understand_document(document, context)

    # Chunk by account when the document is large enough that a single response
    # risks truncation -- and truncation would silently drop accounts.
    account_keys = tuple(a.external_account_key for a in overview_result.overview.accounts)
    if len(account_keys) > settings.extraction_accounts_per_call:
        context = ExtractionContext(account_keys=account_keys)

    extraction = provider.extract_positions(document, overview_result.overview, context)
    return PreparedDocument(
        path=path,
        source_file=stored.source_file,
        overview_result=overview_result,
        extraction=extraction,
    )


def file_document(
    session: Session,
    prepared: PreparedDocument,
    *,
    as_of_date: dt.date | None = None,
    force_as_of: bool = False,
    settings: Settings | None = None,
) -> ImportResult:
    """Place an already-read document: open a snapshot, or join one."""
    settings = settings or get_settings()
    stored_source_file = prepared.source_file
    overview_result = prepared.overview_result
    extraction = prepared.extraction
    assert overview_result is not None and extraction is not None

    # A second document for a date already covered does not open a competing
    # snapshot. It joins the existing one and supplies the accounts it wins
    # precedence for -- which is the whole point of tracking precedence per
    # account. Two isolated snapshots would leave the portfolio showing
    # whichever file happened to be imported last, and neither is the truth.
    if as_of_date is not None and not force_as_of:
        stated = stated_as_of_date(extraction.document)
        if stated is not None and abs((as_of_date - stated).days) > (
            settings.snapshot_date_tolerance_days
        ):
            # A refused import must leave nothing behind. The source file was
            # registered before extraction, so without this the retry -- the one
            # carrying force_as_of, the whole point of the refusal -- would be
            # dismissed as a duplicate and do nothing at all.
            session.delete(stored_source_file)
            session.flush()
            raise DateOverrideError(stated, as_of_date, settings.snapshot_date_tolerance_days)

    resolved_date, date_warnings = resolve_as_of_date(extraction.document, as_of_date)
    existing = find_live_snapshot(session, resolved_date)

    if existing is not None:
        multi = add_document_to_snapshot(
            session,
            existing,
            extraction.document,
            source_file=stored_source_file,
            as_of_date=resolved_date,
            settings=settings,
        )
        snapshot = SnapshotResult(
            snapshot=existing,
            validation=ValidationReport(completeness=Completeness(existing.completeness)),
            reconciliation=multi.reconciliation,
            # The document's own warnings travel with it. They are the most
            # valuable output of an extraction -- a loss shown only by a red
            # arrow, a truncated fund name -- and dropping them here would make
            # a second source quieter than a first for no defensible reason.
            warnings=date_warnings + list(extraction.document.warnings) + multi.warnings,
        )
        run = record_extraction_run(
            session,
            stored_source_file,
            extraction,
            overview=overview_result,
            validation=snapshot.validation,
            reconciliation=snapshot.reconciliation,
            status=snapshot.status,
        )
        stored_source_file.source_as_of_date = resolved_date
        return ImportResult(
            outcome=ImportOutcome.IMPORTED,
            source_file=stored_source_file,
            snapshot=snapshot,
            extraction_run=run,
            multi_source=multi,
            message=_describe_multi(snapshot, multi),
        )

    snapshot = create_snapshot(
        session,
        extraction.document,
        overview=overview_result.overview,
        source_file=stored_source_file,
        as_of_date=as_of_date,
        settings=settings,
    )
    # Joining is by exact date, so a pair exported a day apart parts company
    # here -- each half internally consistent, each calling itself complete,
    # and the portfolio showing whichever landed later. Nothing on screen said
    # the other half existed. Say it, and name the flag that files them as one.
    neighbour = find_nearby_snapshot(
        session,
        snapshot.snapshot.as_of_date,
        settings.snapshot_date_tolerance_days,
        excluding=snapshot.snapshot,
    )
    nearby_warnings = (
        [_describe_neighbour(neighbour, snapshot.snapshot.as_of_date)]
        if neighbour is not None
        else []
    )
    snapshot.warnings[:0] = date_warnings + nearby_warnings

    run = record_extraction_run(
        session,
        stored_source_file,
        extraction,
        overview=overview_result,
        validation=snapshot.validation,
        reconciliation=snapshot.reconciliation,
        status=snapshot.status,
    )

    stored_source_file.source_as_of_date = snapshot.snapshot.as_of_date

    # Tell Hermes a snapshot exists, if it is configured. Fire-and-forget by
    # design: the snapshot is already committed and valid, so an unreachable
    # analyst must never turn a successful import into a failed one.
    trigger_result = get_trigger(settings).snapshot_created(
        str(snapshot.snapshot.id), snapshot.snapshot.as_of_date.isoformat()
    )

    outcome = (
        ImportOutcome.NEEDS_REVIEW
        if snapshot.status is ImportStatus.NEEDS_REVIEW
        else ImportOutcome.IMPORTED
    )
    return ImportResult(
        outcome=outcome,
        source_file=stored_source_file,
        snapshot=snapshot,
        extraction_run=run,
        message=_describe(snapshot),
        analysis_notified=trigger_result.delivered,
    )


@dataclass
class MultiImportResult:
    """What importing several documents together did, in the order it did it."""

    results: tuple[ImportResult, ...]
    #: The order chosen, widest first, as filenames.
    order: tuple[str, ...]
    #: The date every document was filed on.
    as_of_date: dt.date | None
    reordered: bool

    @property
    def created_state(self) -> bool:
        return any(r.created_state for r in self.results)


def import_documents(
    session: Session,
    paths: Sequence[Path],
    provider: DocumentExtractionProvider,
    *,
    role: SourceRole = SourceRole.AUTHORITATIVE,
    as_of_date: dt.date | None = None,
    force_as_of: bool = False,
    settings: Settings | None = None,
) -> MultiImportResult:
    """Import several documents as one portfolio, in the order that works.

    Two documents describing one portfolio are exported on different days and
    state different dates, so filing them together previously meant knowing
    about `--as-of` and typing it. Given both files at once there is nothing to
    know: the date is resolved from the first document filed, and the rest join
    it.

    The order is chosen rather than taken from the command line, because it
    matters and is easy to get backwards. A joining document may only supply
    accounts the snapshot already has -- an aggregation that sees every account
    can be joined by a direct source that sees five, but not the other way
    round, and the reverse silently costs you every account only the wider
    document reports. So the widest document goes first. Command-line order
    breaks ties, which keeps a single-document import exactly as it was.
    """
    settings = settings or get_settings()

    # Read everything before filing anything: the account counts that decide
    # the order are only knowable after extraction, and extracting twice to
    # learn them would double the cost of the expensive half.
    prepared = [
        prepare_document(session, path, provider, role=role, settings=settings) for path in paths
    ]
    fresh = [p for p in prepared if p.duplicate is None]
    ordered = sorted(fresh, key=lambda p: -p.account_count)
    reordered = [p.path for p in ordered] != [p.path for p in fresh]

    results: list[ImportResult] = []
    filed_on = as_of_date
    for document in ordered:
        result = file_document(
            session,
            document,
            as_of_date=filed_on,
            force_as_of=force_as_of,
            settings=settings,
        )
        results.append(result)
        # Every later document joins the first one's date, which is what makes
        # --as-of unnecessary here.
        if filed_on is None and result.snapshot is not None:
            filed_on = result.snapshot.snapshot.as_of_date

    results.extend(p.duplicate for p in prepared if p.duplicate is not None)
    return MultiImportResult(
        results=tuple(results),
        order=tuple(p.path.name for p in ordered),
        as_of_date=filed_on,
        reordered=reordered,
    )


def _describe(result: SnapshotResult) -> str:
    parts = [
        f"{len(result.snapshot.account_snapshots)} account(s)",
        f"total {result.total_value}",
    ]
    if result.cash_value:
        parts.append(f"of which {result.cash_value} is separately reported cash")
    if result.skipped:
        parts.append(f"{len(result.skipped)} account(s) skipped with no reported value")
    failures = result.reconciliation.failures
    if failures:
        parts.append(f"{len(failures)} reconciliation failure(s)")
    if not result.validation.is_complete:
        parts.append("extraction was incomplete")
    return "; ".join(parts)


def find_live_snapshot(session: Session, as_of_date: dt.date) -> PortfolioSnapshot | None:
    """The snapshot already describing this date, if there is one."""
    return (
        session.execute(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.as_of_date == as_of_date,
                PortfolioSnapshot.superseded_by.is_(None),
            )
            .order_by(PortfolioSnapshot.created_at.desc())
        )
        .scalars()
        .first()
    )


def find_nearby_snapshot(
    session: Session,
    as_of_date: dt.date,
    tolerance_days: int,
    *,
    excluding: PortfolioSnapshot | None = None,
) -> PortfolioSnapshot | None:
    """The nearest live snapshot within `tolerance_days`, if there is one.

    Deliberately the same bound `--as-of` is allowed to move a document. Inside
    it, two snapshots are plausibly two halves of one portfolio; outside it,
    they are simply two different days and nothing needs saying.
    """
    window = dt.timedelta(days=tolerance_days)
    query = select(PortfolioSnapshot).where(
        PortfolioSnapshot.as_of_date != as_of_date,
        PortfolioSnapshot.as_of_date >= as_of_date - window,
        PortfolioSnapshot.as_of_date <= as_of_date + window,
        PortfolioSnapshot.superseded_by.is_(None),
    )
    if excluding is not None:
        query = query.where(PortfolioSnapshot.id != excluding.id)
    candidates = list(session.execute(query).scalars())
    if not candidates:
        return None

    def distance(candidate: PortfolioSnapshot) -> tuple[int, float]:
        # Nearest first; the more recently built one breaks a tie.
        return (
            abs((candidate.as_of_date - as_of_date).days),
            -candidate.created_at.timestamp(),
        )

    return min(candidates, key=distance)


def _describe_neighbour(neighbour: PortfolioSnapshot, as_of_date: dt.date) -> str:
    days = abs((neighbour.as_of_date - as_of_date).days)
    return (
        f"this document opened its own {as_of_date.isoformat()} snapshot, but a "
        f"snapshot for {neighbour.as_of_date.isoformat()} is already live, "
        f"{days} day(s) away. If the two documents describe one portfolio, "
        f"re-import this one with --as-of {neighbour.as_of_date.isoformat()} so "
        "each source supplies the accounts it wins. Left as they are, neither "
        "snapshot holds the accounts only the other reports."
    )


def _describe_multi(result: SnapshotResult, multi: MultiSourceResult) -> str:
    parts = [
        f"joined the existing {result.snapshot.as_of_date} snapshot",
        multi.precedence.summary(),
        f"portfolio now {result.total_value} across "
        f"{len(result.snapshot.account_snapshots)} account(s)",
    ]
    if multi.unmatched_accounts:
        parts.append(
            f"{len(multi.unmatched_accounts)} account(s) in this document are NOT in "
            "your portfolio and were not added"
        )
    if multi.ambiguous_accounts:
        parts.append(f"{len(multi.ambiguous_accounts)} account(s) could not be identified")
    failures = result.reconciliation.failures
    if failures:
        parts.append(f"{len(failures)} reconciliation failure(s)")
    return "; ".join(parts)
