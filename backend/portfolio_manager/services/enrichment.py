"""Refreshing an existing snapshot from an optional secondary document.

The authoritative source decides **which** accounts and holdings exist.
Enrichment may only refresh the *measurements* of holdings that already exist.
It cannot add, remove, or re-scope a position, which is what makes it
structurally incapable of inflating the portfolio: the worst case degrades from
"your total is wrong" to "some values were not refreshed".

Two rules that the real documents forced:

* **The measurement tuple is replaced atomically or not at all.** Overwriting
  price alone would break `quantity x price = value` wherever two sources
  disagree on share basis -- which happens: the same ETF appears pre-split in
  one feed and post-split in the other, each internally consistent.
* **Settlement cash is carried, not discarded.** Measured on the real Vanguard
  document, an account's stated total exceeds the sum of its holdings by
  exactly its money-market balance, on all three brokerage accounts. The
  aggregation feed omits that cash entirely, understating by six figures.

* **Enrichment is all-or-nothing per account.** Two sources can disagree about
  what an account *contains*, not merely what it is worth: the real Fidelity
  export lists 22 holdings in one Rollover IRA where Vanguard lists 12. When
  that happens, refreshing the holdings that do match leaves the rest at their
  old values and produces a portfolio of mixed vintage -- some rows current,
  some stale, and a total that is as-of no particular moment. Measured on the
  real pair, doing so moved the portfolio by $420,854, far more than price
  staleness alone can justify.

  So an account is enriched only when the refreshed holdings, plus any stated
  cash, reproduce the account total the enrichment document itself reports.
  If they do not, the account is left entirely on its primary values and
  flagged for review. A partially refreshed account is worse than an
  unrefreshed one, because it looks current.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..extraction.canonical import ExtractedPortfolioDocument
from ..ingestion.matcher import AccountRef, MatchReport, match_accounts
from ..ingestion.normalizer import (
    NormalizationError,
    normalize_security_key,
    parse_money,
    parse_quantity,
)
from ..ingestion.reconciler import (
    CheckStatus,
    ReconciliationReport,
    Tolerance,
    check_position_value,
)
from ..models.entities import (
    Account,
    AccountMapping,
    AccountSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
    Security,
    SourceFile,
)
from ..models.enums import CostBasisSource, ValueSource
from .snapshot import tolerance_from

ZERO = Decimal("0")

#: A shortfall larger than this fraction of an account's stated total is not
#: credible as a settlement balance. The real Vanguard money-market balances
#: were 3%-8% of account value; anything approaching half the account means the
#: two documents are describing different things.
MAX_CREDIBLE_CASH_FRACTION = Decimal("0.5")


@dataclass
class EnrichmentResult:
    matching: MatchReport
    refreshed_positions: int = 0
    #: Holdings the authoritative source lists that the enrichment document did
    #: not mention. They keep their primary values.
    not_refreshed_positions: int = 0
    #: Holdings the enrichment document lists that the authoritative source
    #: does not. Ignored by design, but counted and reported.
    ignored_enrichment_positions: int = 0
    cash_residuals: dict[str, Decimal] = field(default_factory=dict)
    #: Accounts left wholly on primary values because the two sources disagree
    #: about the account's composition, not merely its valuation.
    rejected_accounts: list[str] = field(default_factory=list)
    reconciliation: ReconciliationReport = field(default_factory=ReconciliationReport)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_cash_carried(self) -> Decimal:
        return sum(self.cash_residuals.values(), ZERO)

    def summary(self) -> str:
        parts = [f"{self.refreshed_positions} holding(s) refreshed"]
        if self.not_refreshed_positions:
            parts.append(f"{self.not_refreshed_positions} left unrefreshed")
        if self.ignored_enrichment_positions:
            parts.append(
                f"{self.ignored_enrichment_positions} holding(s) in the enrichment "
                "document are not covered by the authoritative source and were ignored"
            )
        if self.cash_residuals:
            parts.append(f"cash balances carried: {self.total_cash_carried}")
        if self.matching.ambiguous:
            parts.append(f"{len(self.matching.ambiguous)} account(s) need manual mapping")
        if self.rejected_accounts:
            parts.append(
                f"{len(self.rejected_accounts)} account(s) left unenriched because the "
                "two sources disagree on their composition"
            )
        if self.matching.unmatched_enrichment:
            parts.append(
                f"{len(self.matching.unmatched_enrichment)} account(s) reported by the "
                "enrichment document are not in your portfolio"
            )
        return "; ".join(parts)


def _confirmed_mappings(session: Session, snapshot: PortfolioSnapshot) -> dict[str, str]:
    """Human-approved enrichment-to-account links, keyed by enrichment key."""
    account_ids = [a.account_id for a in snapshot.account_snapshots]
    if not account_ids:
        return {}
    rows = session.execute(
        select(AccountMapping, Account)
        .join(Account, Account.id == AccountMapping.account_id)
        .where(AccountMapping.account_id.in_(account_ids))
    ).all()
    return {mapping.enrichment_key: account.external_account_key for mapping, account in rows}


def enrich_snapshot(
    session: Session,
    snapshot: PortfolioSnapshot,
    enrichment: ExtractedPortfolioDocument,
    *,
    source_file: SourceFile | None = None,
    settings: Settings | None = None,
) -> EnrichmentResult:
    """Refresh a snapshot's measurements from a secondary document."""
    settings = settings or get_settings()
    tolerance = tolerance_from(settings)

    accounts = {
        a.account_id: session.get(Account, a.account_id) for a in snapshot.account_snapshots
    }
    authoritative = [
        AccountRef(key=account.external_account_key, display_name=account.display_name)
        for account in accounts.values()
        if account is not None
    ]
    enrichment_refs = [
        AccountRef(key=a.external_account_key, display_name=a.display_name)
        for a in enrichment.accounts
    ]

    matching = match_accounts(
        authoritative, enrichment_refs, confirmed=_confirmed_mappings(session, snapshot)
    )
    result = EnrichmentResult(matching=matching)

    by_auth_key = {
        account.external_account_key: account
        for account in accounts.values()
        if account is not None
    }
    extracted_by_key = {a.external_account_key: a for a in enrichment.accounts}

    for match in matching.matched:
        account = by_auth_key.get(match.authoritative.key)
        extracted_account = extracted_by_key.get(match.enrichment.key)
        if account is None or extracted_account is None:
            continue

        positions = list(
            session.execute(
                select(PositionSnapshot).where(
                    PositionSnapshot.portfolio_snapshot_id == snapshot.id,
                    PositionSnapshot.account_id == account.id,
                )
            ).scalars()
        )
        by_security_key: dict[str, PositionSnapshot] = {}
        for position in positions:
            security = session.get(Security, position.security_id)
            if security is not None:
                by_security_key[security.normalized_key] = position

        # --- plan, without touching anything yet ---------------------------
        planned: list[tuple[PositionSnapshot, Measurements]] = []
        ignored = 0
        seen_keys: set[str] = set()
        for extracted_position in extracted_account.positions:
            try:
                key = normalize_security_key(
                    extracted_position.symbol, extracted_position.description
                )
            except NormalizationError as exc:
                result.warnings.append(f"{extracted_account.display_name}: {exc}")
                continue

            target = by_security_key.get(key)
            if target is None:
                # The authoritative source decides what exists. A holding only
                # the enrichment document knows about is reported, not added.
                ignored += 1
                continue

            measurements = _read_measurements(extracted_position, tolerance, result)
            if measurements is not None:
                seen_keys.add(key)
                planned.append((target, measurements))

        unmatched_keys = [k for k in by_security_key if k not in seen_keys]
        reported_total = parse_money(extracted_account.reported_value)

        # --- verify the account as a whole before applying anything --------
        rejection = _rejection_reason(
            account_name=account.display_name,
            planned=planned,
            unmatched_positions=[by_security_key[k] for k in unmatched_keys],
            reported_total=reported_total,
            tolerance=tolerance,
        )
        if rejection is not None:
            result.rejected_accounts.append(account.display_name)
            result.warnings.append(rejection)
            result.not_refreshed_positions += len(positions)
            continue

        # --- apply -------------------------------------------------------
        for target, measurements in planned:
            _apply_measurements(session, target, measurements, source_file)
            result.refreshed_positions += 1
        result.not_refreshed_positions += len(unmatched_keys)
        result.ignored_enrichment_positions += ignored

        if reported_total is not None and planned:
            # The residual is the enrichment document's own internal gap -- its
            # stated account total minus the holdings IT lists. That is the
            # settlement balance. Computing it against our matched subset
            # instead would misrecord unmatched holdings as cash.
            enrichment_holdings = sum(
                (parse_money(p.market_value) or ZERO for p in extracted_account.positions),
                ZERO,
            )
            residual = reported_total - enrichment_holdings
            if residual > tolerance.for_value(reported_total):
                result.cash_residuals[account.display_name] = residual

        _refresh_account_totals(session, snapshot, account, reported_total)

    if matching.unmatched_enrichment:
        snapshot.unmatched_enrichment_json = [
            {
                "key": ref.key,
                "display_name": ref.display_name,
                "note": (
                    "reported by the enrichment document but not covered by your "
                    "authoritative source; it is NOT included in your portfolio"
                ),
            }
            for ref in matching.unmatched_enrichment
        ]

    session.flush()
    return result


@dataclass(frozen=True)
class Measurements:
    """A holding's complete measurement tuple, read from an enrichment source."""

    quantity: Decimal | None
    price: Decimal | None
    market_value: Decimal
    cost_basis: Decimal | None
    unrealized_gain_loss: Decimal | None


def _read_measurements(
    extracted: object, tolerance: Tolerance, result: EnrichmentResult
) -> Measurements | None:
    """Parse and verify one holding's measurements, or decline it.

    Returns None when the values are unreadable or internally inconsistent --
    never a partial tuple, because two sources can disagree on share basis
    (one pre-split, one post) and each is coherent only as a complete set.
    """
    try:
        quantity = parse_quantity(getattr(extracted, "quantity", None))
        price = parse_money(getattr(extracted, "price", None), field="price")
        market_value = parse_money(getattr(extracted, "market_value", None), field="market_value")
        cost_basis = parse_money(getattr(extracted, "cost_basis", None), field="cost_basis")
        gain_loss = parse_money(
            getattr(extracted, "unrealized_gain_loss", None), field="unrealized_gain_loss"
        )
    except NormalizationError as exc:
        result.warnings.append(f"enrichment value unreadable, holding left unchanged: {exc}")
        return None

    if market_value is None:
        return None

    check = check_position_value("enrichment", quantity, price, market_value, tolerance)
    result.reconciliation.checks.append(check)
    if check.status is CheckStatus.FAILED:
        result.warnings.append(
            f"enrichment values for a holding did not reconcile ({check.describe()}); "
            "the holding was left unchanged"
        )
        return None

    return Measurements(quantity, price, market_value, cost_basis, gain_loss)


def _rejection_reason(
    *,
    account_name: str,
    planned: list[tuple[PositionSnapshot, Measurements]],
    unmatched_positions: list[PositionSnapshot],
    reported_total: Decimal | None,
    tolerance: Tolerance,
) -> str | None:
    """Whether this account may be enriched at all.

    The two sources must agree about the account's *composition*, not merely
    its valuation. If refreshing the holdings that match would leave the
    account's total far from what the enrichment document itself reports, the
    documents describe different realities and mixing them would produce a
    portfolio of mixed vintage that merely looks current.
    """
    if not planned:
        if unmatched_positions:
            return (
                f"{account_name}: none of the holdings in the enrichment document could be "
                f"matched to the {len(unmatched_positions)} holding(s) already recorded for "
                "this account. Holdings without a ticker are matched on their description, "
                "which differs between sources. The account needs a manual review."
            )
        return None
    if reported_total is None:
        # Nothing to verify against. Refuse only when the enrichment plainly
        # covers a fraction of the account, which is the dangerous case.
        if unmatched_positions:
            stale = sum((p.market_value or ZERO for p in unmatched_positions), ZERO)
            refreshed = sum((m.market_value for _, m in planned), ZERO)
            if stale > refreshed:
                return (
                    f"{account_name}: the enrichment document covers only part of this "
                    f"account ({len(planned)} of {len(planned) + len(unmatched_positions)} "
                    "holdings) and states no account total, so it cannot be verified. "
                    "The account was left on its original values."
                )
        return None

    projected = sum((m.market_value for _, m in planned), ZERO) + sum(
        (p.market_value or ZERO for p in unmatched_positions), ZERO
    )
    # The enrichment total may legitimately exceed its own holdings by a cash
    # balance, so only a projection ABOVE the reported total is suspicious,
    # along with a shortfall too large to be cash.
    difference = projected - reported_total
    if abs(difference) <= tolerance.for_value(reported_total):
        return None
    if difference < ZERO and unmatched_positions == []:
        # A shortfall with full coverage is consistent with separately stated
        # cash -- but only if it is a plausible size for a cash balance.
        shortfall = -difference
        if shortfall <= reported_total * MAX_CREDIBLE_CASH_FRACTION:
            return None
        return (
            f"{account_name}: the enrichment document reports a total of {reported_total} "
            f"but lists holdings worth only {projected}, a shortfall of {shortfall}. "
            "That is too large to be a settlement cash balance, so the two documents "
            "are describing different things. The account was left on its original values."
        )
    return (
        f"{account_name}: the two sources disagree about this account's contents. "
        f"Refreshing the {len(planned)} matching holding(s) while leaving "
        f"{len(unmatched_positions)} on their original values would imply a total of "
        f"{projected}, but the enrichment document reports {reported_total} "
        f"(difference {difference:+}). The account was left on its original values "
        "and needs review."
    )


def _apply_measurements(
    session: Session,
    position: PositionSnapshot,
    measurements: Measurements,
    source_file: SourceFile | None,
) -> None:
    """Write a verified measurement tuple onto a holding, as a unit."""
    position.quantity = measurements.quantity
    position.price = measurements.price
    position.market_value = measurements.market_value
    position.unrealized_gain_loss = measurements.unrealized_gain_loss
    if measurements.cost_basis is not None:
        position.cost_basis = measurements.cost_basis
        position.cost_basis_source = CostBasisSource.REPORTED.value
    elif measurements.unrealized_gain_loss is not None:
        position.cost_basis = measurements.market_value - measurements.unrealized_gain_loss
        position.cost_basis_source = CostBasisSource.DERIVED.value

    position.value_source = ValueSource.ENRICHED.value
    if source_file is not None:
        position.enriched_from_source_file_id = source_file.id
    session.add(position)


def _refresh_account_totals(
    session: Session,
    snapshot: PortfolioSnapshot,
    account: Account,
    reported_total: Decimal | None,
) -> None:
    account_snapshot = session.execute(
        select(AccountSnapshot).where(
            AccountSnapshot.portfolio_snapshot_id == snapshot.id,
            AccountSnapshot.account_id == account.id,
        )
    ).scalar_one_or_none()
    if account_snapshot is None:
        return
    positions = session.execute(
        select(PositionSnapshot).where(
            PositionSnapshot.portfolio_snapshot_id == snapshot.id,
            PositionSnapshot.account_id == account.id,
        )
    ).scalars()
    account_snapshot.computed_value = sum((p.market_value or ZERO for p in positions), ZERO)
    if reported_total is not None:
        account_snapshot.reported_value = reported_total
