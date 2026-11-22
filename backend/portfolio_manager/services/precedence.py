"""Deciding which source is authoritative for each account.

An account can be described by more than one document, and those descriptions
can disagree about more than valuation. Measured on the real pair, Fidelity's
aggregation and Vanguard's own statement disagree on 35 of 44 securities --
entire positions present in one and absent from the other -- while their totals
agree to within $469. That is a rebalance the aggregation had not yet picked
up, not a pricing lag.

So the rule is **precedence, not patching**: for each account, one document
wins outright and supplies that account's holdings wholesale. Patching fresh
prices onto a composition from another date produces a portfolio of mixed
vintage that *looks* current, which is worse than an openly stale one.

Native beats aggregated automatically, because an institution reporting its own
account is the better witness. Anything else -- two native sources, or two
aggregations -- is put to the user once and remembered.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..extraction.canonical import ExtractedAccount, ExtractedPortfolioDocument
from ..models.entities import Account, AccountSnapshot, PortfolioSnapshot, PositionSnapshot

ZERO = Decimal("0")


class PrecedenceOutcome(StrEnum):
    #: This document supplied the account's holdings.
    SUPPLIED = "supplied"
    #: An existing source already wins for this account.
    DEFERRED = "deferred"
    #: Two comparable sources disagree; the user must choose once.
    NEEDS_DECISION = "needs_decision"
    #: Nothing else covers this account, so this document supplies it.
    SOLE_SOURCE = "sole_source"


@dataclass(frozen=True)
class AccountDecision:
    account_key: str
    display_name: str
    outcome: PrecedenceOutcome
    winner: str | None
    incumbent: str | None
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "account": self.display_name,
            "outcome": self.outcome.value,
            "winner": self.winner,
            "incumbent": self.incumbent,
            "reason": self.reason,
        }


@dataclass
class PrecedenceReport:
    decisions: list[AccountDecision] = field(default_factory=list)
    replaced_accounts: int = 0
    replaced_positions: int = 0

    @property
    def needs_decision(self) -> list[AccountDecision]:
        return [d for d in self.decisions if d.outcome is PrecedenceOutcome.NEEDS_DECISION]

    def summary(self) -> str:
        parts = [f"{self.replaced_accounts} account(s) supplied by this document"]
        deferred = sum(1 for d in self.decisions if d.outcome is PrecedenceOutcome.DEFERRED)
        if deferred:
            parts.append(f"{deferred} left to an existing source")
        if self.needs_decision:
            parts.append(f"{len(self.needs_decision)} account(s) need you to choose a source")
        return "; ".join(parts)


def decide(
    account: Account,
    incumbent_source: str | None,
    challenger_institution: str,
    challenger_is_aggregated: bool,
    incumbent_is_aggregated: bool,
) -> AccountDecision:
    """Which source should supply this account.

    Pure and side-effect free so the reasoning can be tested directly and shown
    to the user verbatim.
    """
    common = {"account_key": account.external_account_key, "display_name": account.display_name}

    if account.preferred_source:
        if account.preferred_source == challenger_institution:
            return AccountDecision(
                **common,
                outcome=PrecedenceOutcome.SUPPLIED,
                winner=challenger_institution,
                incumbent=incumbent_source,
                reason=f"you chose {challenger_institution} as the source for this account",
            )
        return AccountDecision(
            **common,
            outcome=PrecedenceOutcome.DEFERRED,
            winner=account.preferred_source,
            incumbent=incumbent_source,
            reason=f"you chose {account.preferred_source} as the source for this account",
        )

    if incumbent_source is None:
        return AccountDecision(
            **common,
            outcome=PrecedenceOutcome.SOLE_SOURCE,
            winner=challenger_institution,
            incumbent=None,
            reason="no other document covers this account",
        )

    if incumbent_is_aggregated and not challenger_is_aggregated:
        return AccountDecision(
            **common,
            outcome=PrecedenceOutcome.SUPPLIED,
            winner=challenger_institution,
            incumbent=incumbent_source,
            reason=(
                f"{challenger_institution} reports this account directly, while it "
                "currently comes from an aggregation feed that reports it second-hand"
                + (f" (via {incumbent_source})" if incumbent_source != "unknown" else "")
            ),
        )

    if challenger_is_aggregated and not incumbent_is_aggregated:
        return AccountDecision(
            **common,
            outcome=PrecedenceOutcome.DEFERRED,
            winner=incumbent_source,
            incumbent=incumbent_source,
            reason=(
                f"{incumbent_source} reports this account directly, while "
                f"{challenger_institution} reports it second-hand"
            ),
        )

    kind = "aggregation feeds" if challenger_is_aggregated else "direct sources"
    return AccountDecision(
        **common,
        outcome=PrecedenceOutcome.NEEDS_DECISION,
        winner=None,
        incumbent=incumbent_source,
        reason=(
            f"{incumbent_source} and {challenger_institution} are both {kind} for this "
            "account, so neither automatically wins. Choose once and it will be remembered."
        ),
    )


def infer_staleness(extracted: ExtractedAccount) -> tuple[bool, str | None]:
    """Whether an account's data shows signs of not being current.

    An aggregation feed that has not synced recently reports no intraday
    movement. Measured on the real export, 0 of 30 native rows lacked that data
    against 48 of 56 aggregated rows -- so its absence across a whole account is
    a reliable signal, and the file's own date says nothing about it.
    """
    if not extracted.is_aggregated:
        return False, None
    missing_gain_loss = sum(1 for p in extracted.positions if p.unrealized_gain_loss is None)
    if extracted.positions and missing_gain_loss == len(extracted.positions):
        return True, (
            "reported second-hand by an aggregation feed with no gain/loss data, so its "
            "as-of date is unknown and may be considerably older than this file"
        )
    return True, "reported second-hand by an aggregation feed; its as-of date is unknown"


def apply_precedence(
    session: Session,
    snapshot: PortfolioSnapshot,
    document: ExtractedPortfolioDocument,
    accounts_by_key: dict[str, Account],
    *,
    as_of_date: dt.date | None = None,
) -> PrecedenceReport:
    """Let a document supply the accounts it wins, replacing them wholesale.

    Only decides and records; the caller writes the replacement holdings, so
    this module never has to know how a position is constructed.
    """
    report = PrecedenceReport()

    for extracted in document.accounts:
        account = accounts_by_key.get(extracted.external_account_key)
        if account is None:
            continue

        account_snapshot = session.execute(
            select(AccountSnapshot).where(
                AccountSnapshot.portfolio_snapshot_id == snapshot.id,
                AccountSnapshot.account_id == account.id,
            )
        ).scalar_one_or_none()

        incumbent = account_snapshot.supplied_by if account_snapshot else None
        incumbent_aggregated = bool(account.is_aggregated)

        decision = decide(
            account,
            incumbent,
            extracted.institution,
            extracted.is_aggregated,
            incumbent_aggregated,
        )
        report.decisions.append(decision)

        if decision.outcome in (PrecedenceOutcome.DEFERRED, PrecedenceOutcome.NEEDS_DECISION):
            continue

        # This document wins: clear the account's holdings so the caller can
        # write its own. Replacing wholesale is the point -- a partial refresh
        # would mix two dates in one account.
        removed = (
            session.execute(
                select(PositionSnapshot).where(
                    PositionSnapshot.portfolio_snapshot_id == snapshot.id,
                    PositionSnapshot.account_id == account.id,
                )
            )
            .scalars()
            .all()
        )
        for position in removed:
            session.delete(position)
        report.replaced_positions += len(removed)
        report.replaced_accounts += 1

        stale, reason = infer_staleness(extracted)
        if account_snapshot is not None:
            account_snapshot.supplied_by = extracted.institution
            account_snapshot.as_of_date = None if stale else as_of_date
            account_snapshot.as_of_confidence = "unknown" if stale else "stated"
            account_snapshot.is_stale = stale
            account_snapshot.staleness_reason = reason

        # Record the automatic choice so it is visible and not re-derived.
        if decision.outcome is PrecedenceOutcome.SUPPLIED and not account.preferred_source:
            account.preferred_source = extracted.institution

    session.flush()
    return report


def set_preferred_source(
    session: Session, account: Account, institution: str, *, reason: str = ""
) -> None:
    """Record the user's one-time choice of source for an account."""
    account.preferred_source = institution
    session.add(account)
    session.flush()
