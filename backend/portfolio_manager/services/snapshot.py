"""Turning a verified extraction into authoritative portfolio state.

This is the boundary where a model's output stops being a suggestion and starts
being the system of record, so the ordering is deliberate:

    validate completeness -> normalize -> skip empty accounts -> reconcile
        -> only then write

An extraction that fails completeness or reconciliation still produces a
snapshot row, but marked `needs_review` rather than `imported`, so the evidence
is preserved for a human instead of being thrown away.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..extraction.canonical import (
    DocumentOverview,
    ExtractedAccount,
    ExtractedPortfolioDocument,
)
from ..ingestion.normalizer import (
    NormalizationError,
    mask_account_key,
    normalize_security_key,
    parse_date,
    parse_money,
    parse_quantity,
)
from ..ingestion.reconciler import (
    ReconciliationReport,
    Tolerance,
    check_account_total,
    check_position_basis,
    check_position_value,
)
from ..ingestion.validator import (
    SkippedAccount,
    ValidationReport,
    check_completeness,
    partition_accounts,
)
from ..models.entities import (
    Account,
    AccountSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
    Security,
    SourceFile,
)
from ..models.enums import (
    Completeness,
    CostBasisSource,
    ImportStatus,
    ValueSource,
)
from .classification import classify_account
from .precedence import infer_staleness
from .security_classification import classify_security

ZERO = Decimal("0")


@dataclass
class SnapshotResult:
    snapshot: PortfolioSnapshot
    validation: ValidationReport
    reconciliation: ReconciliationReport
    skipped: list[SkippedAccount] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> ImportStatus:
        return ImportStatus(self.snapshot.status)

    @property
    def holdings_value(self) -> Decimal:
        return sum((a.computed_value for a in self.snapshot.account_snapshots), ZERO)

    @property
    def cash_value(self) -> Decimal:
        """Cash reported in its own panel rather than as a holdings row."""
        return sum((a.cash_balance or ZERO for a in self.snapshot.account_snapshots), ZERO)

    @property
    def total_value(self) -> Decimal:
        """Holdings plus separately reported cash.

        The cash term is not optional. A settlement fund shown in its own panel
        is the usual reason an account's stated total exceeds the sum of its
        holdings rows, and on the real corpus it is 5% of the portfolio. An
        import that reported holdings alone and called the figure "total"
        contradicted both the document and `PortfolioView.total_value`, which
        has always included it.
        """
        return self.holdings_value + self.cash_value


def stated_as_of_date(extracted: ExtractedPortfolioDocument) -> dt.date | None:
    """The date the document itself claims, or None if it states none usable."""
    try:
        return parse_date(extracted.as_of_date)
    except NormalizationError:
        return None


def resolve_as_of_date(
    extracted: ExtractedPortfolioDocument, as_of_date: dt.date | None = None
) -> tuple[dt.date, list[str]]:
    """The date this document describes, and anything qualifying that choice.

    Separated out because the date has to be known *before* deciding whether a
    document opens a new snapshot or joins one that already exists.
    """
    warnings: list[str] = []
    if as_of_date is not None:
        # An override never silently replaces what the document says. The
        # difference is exactly the sort of qualification a figure has to carry:
        # a reader comparing this account against its statement needs to know
        # the two dates are not the same.
        stated = stated_as_of_date(extracted)
        if stated is not None and stated != as_of_date:
            days = abs((as_of_date - stated).days)
            warnings.append(
                f"the document states an as-of date of {stated.isoformat()}, but it was "
                f"placed on {as_of_date.isoformat()} at your instruction -- "
                f"{days} day(s) apart"
            )
        elif stated is None:
            warnings.append(
                f"the document stated no usable as-of date; it was placed on "
                f"{as_of_date.isoformat()} at your instruction"
            )
        return as_of_date, warnings

    resolved = None
    try:
        resolved = parse_date(extracted.as_of_date)
    except NormalizationError as exc:
        warnings.append(str(exc))
    if resolved is None:
        resolved = dt.date.today()
        warnings.append(
            "the document stated no usable as-of date; today's date was used and "
            "should be confirmed"
        )
    return resolved, warnings


def tolerance_from(settings: Settings) -> Tolerance:
    return Tolerance(
        absolute=settings.recon_abs_tolerance,
        percentage=settings.recon_pct_tolerance,
        maximum=settings.recon_max_abs_tolerance,
    )


def get_or_create_security(session: Session, symbol: str | None, description: str) -> Security:
    """Find or create the security, keyed by ticker or normalized description.

    Deliberately carries no price: the same instrument legitimately shows two
    different prices in one document when one source is stale or pre-split.
    """
    key = normalize_security_key(symbol, description)
    existing = session.execute(
        select(Security).where(Security.normalized_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    classification = classify_security(symbol, description)
    security = Security(
        normalized_key=key,
        symbol=symbol.strip().upper() if symbol and symbol.strip() else None,
        name=description or key,
        security_type=classification.security_type.value,
        asset_class=classification.asset_class.value,
    )
    session.add(security)
    session.flush()
    return security


def get_or_create_account(session: Session, extracted: ExtractedAccount) -> Account:
    """Find or create the account, classifying it only on first sight.

    A later import never silently reclassifies an account: once a human has
    confirmed the type through Account Setup, re-importing must not undo it.
    """
    existing = session.execute(
        select(Account).where(
            Account.institution == extracted.institution,
            Account.external_account_key == extracted.external_account_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    classification = classify_account(extracted.display_name, extracted.account_type_hint)
    account = Account(
        institution=extracted.institution,
        is_aggregated=extracted.is_aggregated,
        external_account_key=extracted.external_account_key,
        external_account_key_masked=mask_account_key(extracted.external_account_key),
        display_name=extracted.display_name,
        account_type=classification.account_type.value,
        tax_treatment=classification.tax_treatment.value,
        classification_confidence=classification.confidence,
        classification_source="inferred",
    )
    session.add(account)
    session.flush()
    return account


def _cost_basis_source(
    cost_basis: Decimal | None, market_value: Decimal | None, gain_loss: Decimal | None
) -> tuple[Decimal | None, CostBasisSource]:
    """Use a reported basis, else derive one, else say honestly that it is unknown."""
    if cost_basis is not None:
        return cost_basis, CostBasisSource.REPORTED
    if market_value is not None and gain_loss is not None:
        return market_value - gain_loss, CostBasisSource.DERIVED
    return None, CostBasisSource.UNKNOWN


def create_snapshot(
    session: Session,
    extracted: ExtractedPortfolioDocument,
    *,
    overview: DocumentOverview | None = None,
    source_file: SourceFile | None = None,
    as_of_date: dt.date | None = None,
    settings: Settings | None = None,
) -> SnapshotResult:
    """Build a portfolio snapshot from a verified extraction."""
    settings = settings or get_settings()
    tolerance = tolerance_from(settings)
    warnings: list[str] = list(extracted.warnings)

    # 1. Completeness. Checked first because no amount of clean arithmetic can
    #    compensate for an account that vanished between stages.
    if overview is not None:
        validation = check_completeness(
            [a.external_account_key for a in overview.accounts],
            [a.external_account_key for a in extracted.accounts],
            {a.external_account_key: a.expected_position_count for a in overview.accounts},
            {a.external_account_key: len(a.positions) for a in extracted.accounts},
        )
    else:
        validation = ValidationReport(completeness=Completeness.UNKNOWN)
        warnings.append(
            "no document-analysis pass was supplied, so completeness could not be verified"
        )

    # 2. Which accounts carry any reported value at all.
    keep_keys, skipped = partition_accounts(
        [
            (a.external_account_key, a.display_name, [p.market_value for p in a.positions])
            for a in extracted.accounts
        ]
    )
    validation.skipped_accounts = skipped
    kept = {k for k in keep_keys}

    # 3. Resolve the snapshot's date.
    resolved_date, date_warnings = resolve_as_of_date(extracted, as_of_date)
    warnings.extend(date_warnings)

    snapshot = PortfolioSnapshot(
        as_of_date=resolved_date,
        completeness=validation.completeness.value,
        status=ImportStatus.EXTRACTED.value,
        skipped_accounts_json=[s.to_json() for s in skipped],
    )
    session.add(snapshot)
    session.flush()

    # 4. Write accounts and positions, reconciling as we go.
    reconciliation = ReconciliationReport()
    for extracted_account in extracted.accounts:
        if extracted_account.external_account_key not in kept:
            continue
        account = get_or_create_account(session, extracted_account)
        written = write_account_positions(
            session,
            snapshot,
            account,
            extracted_account,
            tolerance,
            reconciliation,
            warnings,
            source_file=source_file,
        )
        if written.residual is not None:
            reconciliation.cash_residuals[extracted_account.display_name[:32]] = written.residual

    session.flush()

    # 5. Decide the outcome. Completeness can veto an otherwise clean import.
    if validation.requires_review or not reconciliation.passed:
        snapshot.status = ImportStatus.NEEDS_REVIEW.value
    elif settings.auto_approve_clean_imports:
        snapshot.status = ImportStatus.IMPORTED.value
    else:
        snapshot.status = ImportStatus.APPROVED.value
    session.flush()

    return SnapshotResult(
        snapshot=snapshot,
        validation=validation,
        reconciliation=reconciliation,
        skipped=skipped,
        warnings=warnings,
    )


def supersede_overlapping_snapshot(
    session: Session, snapshot: PortfolioSnapshot
) -> PortfolioSnapshot | None:
    """Mark an earlier snapshot for the same date as superseded.

    A corrected re-export has a different sha256, so content dedupe cannot
    catch it. Without this, the same period would end up with two competing
    truths and no indication of which one to believe.
    """
    prior = (
        session.execute(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.as_of_date == snapshot.as_of_date,
                PortfolioSnapshot.id != snapshot.id,
                PortfolioSnapshot.superseded_by.is_(None),
            )
            .order_by(PortfolioSnapshot.created_at.desc())
        )
        .scalars()
        .first()
    )
    if prior is None:
        return None
    prior.superseded_by = snapshot.id
    session.flush()
    return prior


@dataclass
class WrittenAccount:
    """What writing one account's holdings produced."""

    position_count: int
    computed_value: Decimal
    reported_value: Decimal | None
    residual: Decimal | None


def write_account_positions(
    session: Session,
    snapshot: PortfolioSnapshot,
    account: Account,
    extracted_account: ExtractedAccount,
    tolerance: Tolerance,
    reconciliation: ReconciliationReport,
    warnings: list[str],
    *,
    source_file: SourceFile | None = None,
    value_source: ValueSource = ValueSource.PRIMARY,
) -> WrittenAccount:
    """Write one account's holdings into a snapshot, reconciling as it goes.

    Shared by the first import and by a later document that wins precedence for
    an account, so both produce identical structure and identical checks.
    """
    account_total = ZERO
    position_values: list[Decimal] = []

    for extracted_position in extracted_account.positions:
        try:
            quantity = parse_quantity(extracted_position.quantity)
            price = parse_money(extracted_position.price, field="price")
            market_value = parse_money(extracted_position.market_value, field="market_value")
            raw_basis = parse_money(extracted_position.cost_basis, field="cost_basis")
            gain_loss = parse_money(
                extracted_position.unrealized_gain_loss, field="unrealized_gain_loss"
            )
        except NormalizationError as exc:
            warnings.append(
                f"{extracted_account.display_name}: {exc}; the holding was recorded "
                "with the unreadable field left empty"
            )
            quantity = price = market_value = raw_basis = gain_loss = None

        security = get_or_create_security(
            session, extracted_position.symbol, extracted_position.description
        )
        is_closed = quantity == ZERO and market_value is None
        cost_basis, basis_source = _cost_basis_source(raw_basis, market_value, gain_loss)

        subject = (
            f"{extracted_account.display_name[:24]}/"
            f"{extracted_position.symbol or extracted_position.description[:18]}"
        )
        reconciliation.checks.append(
            check_position_value(
                subject, quantity, price, market_value, tolerance, is_closed=is_closed
            )
        )
        reconciliation.checks.append(
            check_position_basis(subject, raw_basis, gain_loss, market_value, tolerance)
        )

        session.add(
            PositionSnapshot(
                portfolio_snapshot_id=snapshot.id,
                account_id=account.id,
                security_id=security.id,
                quantity=quantity,
                price=price,
                market_value=market_value,
                cost_basis=cost_basis,
                unrealized_gain_loss=gain_loss,
                cost_basis_source=basis_source.value,
                is_closed=is_closed,
                value_source=value_source.value,
                enriched_from_source_file_id=(
                    source_file.id if value_source is ValueSource.ENRICHED and source_file else None
                ),
            )
        )
        if market_value is not None:
            account_total += market_value
            position_values.append(market_value)

    reported = parse_money(extracted_account.reported_value, field="reported_value")
    check, residual = check_account_total(
        extracted_account.display_name[:32],
        position_values,
        reported,
        tolerance,
        stated_cash_balance=parse_money(extracted_account.cash_balance, field="cash_balance"),
    )
    reconciliation.checks.append(check)

    existing = session.execute(
        select(AccountSnapshot).where(
            AccountSnapshot.portfolio_snapshot_id == snapshot.id,
            AccountSnapshot.account_id == account.id,
        )
    ).scalar_one_or_none()
    stale, staleness_reason = infer_staleness(extracted_account)

    # Prefer what the document actually states over anything inferred: an
    # unexplained shortfall might be missing holdings, not cash.
    stated_cash = parse_money(extracted_account.cash_balance, field="cash_balance")
    if stated_cash is not None:
        cash = stated_cash
    elif residual is not None and residual > ZERO:
        cash = residual
    else:
        cash = ZERO

    if existing is None:
        session.add(
            AccountSnapshot(
                portfolio_snapshot_id=snapshot.id,
                account_id=account.id,
                source_file_id=source_file.id if source_file is not None else None,
                reported_value=reported,
                computed_value=account_total,
                cash_balance=cash,
                position_count=len(extracted_account.positions),
                supplied_by=extracted_account.institution,
                is_stale=stale,
                staleness_reason=staleness_reason,
                as_of_date=None if stale else snapshot.as_of_date,
                as_of_confidence="unknown" if stale else "stated",
            )
        )
    else:
        existing.reported_value = reported
        existing.computed_value = account_total
        existing.cash_balance = cash
        existing.position_count = len(extracted_account.positions)
        existing.supplied_by = extracted_account.institution
        existing.is_stale = stale
        existing.staleness_reason = staleness_reason
        existing.as_of_date = None if stale else snapshot.as_of_date
        existing.as_of_confidence = "unknown" if stale else "stated"
        if source_file is not None:
            existing.source_file_id = source_file.id

    session.flush()
    return WrittenAccount(
        position_count=len(extracted_account.positions),
        computed_value=account_total,
        reported_value=reported,
        residual=residual,
    )
