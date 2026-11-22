"""Adding a second document to an existing snapshot.

Where `snapshot.create_snapshot` builds portfolio state from one document, this
handles the case where another document also describes some of the same
accounts. It decides precedence per account, then lets the winner supply that
account's holdings wholesale.

Wholesale replacement is the point. A document that wins an account is trusted
for what that account *contains*, not merely what it is worth -- because the
two can differ. On the real pair the aggregation was missing an entire $383K
holding the direct source reported, while their totals still agreed to within
$469.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..extraction.canonical import ExtractedPortfolioDocument
from ..ingestion.matcher import AccountRef, match_accounts
from ..ingestion.reconciler import ReconciliationReport
from ..ingestion.validator import partition_accounts
from ..models.entities import Account, AccountMapping, PortfolioSnapshot, SourceFile
from .precedence import (
    PrecedenceReport,
    apply_precedence,
)
from .snapshot import tolerance_from, write_account_positions

ZERO = Decimal("0")


@dataclass
class MultiSourceResult:
    precedence: PrecedenceReport
    reconciliation: ReconciliationReport = field(default_factory=ReconciliationReport)
    #: Accounts this document reports that the portfolio does not contain.
    #: Reported prominently: ignoring one silently would hide real money.
    unmatched_accounts: list[str] = field(default_factory=list)
    #: Accounts whose identifier resolves to more than one candidate.
    ambiguous_accounts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [self.precedence.summary()]
        if self.unmatched_accounts:
            parts.append(
                f"{len(self.unmatched_accounts)} account(s) in this document are not in "
                "your portfolio and were not added"
            )
        if self.ambiguous_accounts:
            parts.append(f"{len(self.ambiguous_accounts)} account(s) could not be identified")
        return "; ".join(parts)


def _confirmed_mappings(session: Session, accounts: list[Account]) -> dict[str, str]:
    if not accounts:
        return {}
    rows = session.execute(
        select(AccountMapping, Account)
        .join(Account, Account.id == AccountMapping.account_id)
        .where(AccountMapping.account_id.in_([a.id for a in accounts]))
    ).all()
    return {mapping.enrichment_key: account.external_account_key for mapping, account in rows}


def add_document_to_snapshot(
    session: Session,
    snapshot: PortfolioSnapshot,
    document: ExtractedPortfolioDocument,
    *,
    source_file: SourceFile | None = None,
    as_of_date: dt.date | None = None,
    settings: Settings | None = None,
) -> MultiSourceResult:
    """Let a second document supply the accounts it wins precedence for."""
    settings = settings or get_settings()
    tolerance = tolerance_from(settings)

    accounts = [
        account
        for account in (session.get(Account, a.account_id) for a in snapshot.account_snapshots)
        if account is not None
    ]

    matching = match_accounts(
        [AccountRef(a.external_account_key, a.display_name) for a in accounts],
        [AccountRef(a.external_account_key, a.display_name) for a in document.accounts],
        confirmed=_confirmed_mappings(session, accounts),
    )

    result = MultiSourceResult(precedence=PrecedenceReport())
    result.unmatched_accounts = [
        f"{r.display_name or r.key}" for r in matching.unmatched_enrichment
    ]
    result.ambiguous_accounts = [ref.key for ref, _ in matching.ambiguous]

    if result.unmatched_accounts:
        snapshot.unmatched_enrichment_json = [
            {
                "key": r.key,
                "display_name": r.display_name,
                "note": (
                    "reported by this document but not covered by your portfolio; "
                    "it is NOT included"
                ),
            }
            for r in matching.unmatched_enrichment
        ]

    by_auth_key = {a.external_account_key: a for a in accounts}
    # Rewrite the document's account keys to the portfolio's own keys, so
    # precedence and writing both work in one namespace.
    extracted_by_auth_key = {}
    for match in matching.matched:
        extracted = next(
            (a for a in document.accounts if a.external_account_key == match.enrichment.key),
            None,
        )
        if extracted is not None:
            extracted_by_auth_key[match.authoritative.key] = extracted

    # Skip accounts this document reports with no value at all.
    keep_keys, skipped = partition_accounts(
        [
            (key, extracted.display_name, [p.market_value for p in extracted.positions])
            for key, extracted in extracted_by_auth_key.items()
        ]
    )
    for entry in skipped:
        result.warnings.append(
            f"{entry.display_name}: this document reports no value for the account, "
            "so the existing holdings were kept"
        )

    aligned = ExtractedPortfolioDocument(
        document_type=document.document_type,
        producing_institution=document.producing_institution,
        as_of_date=document.as_of_date,
        as_of_date_source=document.as_of_date_source,
        reported_total_value=document.reported_total_value,
        accounts=[
            extracted_by_auth_key[key].model_copy(update={"external_account_key": key})
            for key in keep_keys
        ],
        warnings=list(document.warnings),
    )

    result.precedence = apply_precedence(
        session, snapshot, aligned, by_auth_key, as_of_date=as_of_date or snapshot.as_of_date
    )

    supplied = {
        d.account_key
        for d in result.precedence.decisions
        if d.outcome.value in ("supplied", "sole_source")
    }
    for extracted_account in aligned.accounts:
        if extracted_account.external_account_key not in supplied:
            continue
        account = by_auth_key.get(extracted_account.external_account_key)
        if account is None:
            continue
        write_account_positions(
            session,
            snapshot,
            account,
            extracted_account,
            tolerance,
            result.reconciliation,
            result.warnings,
            source_file=source_file,
        )

    session.flush()
    return result
