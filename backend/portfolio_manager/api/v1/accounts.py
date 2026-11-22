"""Account endpoints, including the classification and precedence workflows.

Account keys are masked on the way out. Clients address accounts by our own
UUID, so a real brokerage account number never reaches a browser, a log, or a
bug report.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...analytics import load_portfolio
from ...models.entities import Account, AccountAuditLog
from ...models.enums import AccountType, TaxTreatment
from ...schemas.responses import AccountOut, HoldingOut
from ...services.precedence import set_preferred_source
from ..deps import SessionDep, resolve_snapshot

router = APIRouter(prefix="/accounts", tags=["accounts"])


class ClassifyAccountIn(BaseModel):
    """A user's confirmation of an account's type and tax treatment."""

    account_type: AccountType
    tax_treatment: TaxTreatment
    owner_label: str | None = None
    reason: str | None = Field(default=None, description="Optional note recorded in the audit log")


class PreferredSourceIn(BaseModel):
    institution: str = Field(description="Which institution's documents win for this account")
    reason: str | None = None


@router.get("", response_model=list[AccountOut])
def list_accounts(session: SessionDep, snapshot_id: str | None = None) -> list[AccountOut]:
    snapshot = resolve_snapshot(session, snapshot_id)
    view = load_portfolio(session, snapshot)
    accounts = {
        str(a.id): a
        for a in session.execute(
            select(Account).where(Account.id.in_([r.account_id for r in view.accounts] or [None]))
        ).scalars()
    }
    out: list[AccountOut] = []
    for row in view.accounts:
        account = accounts.get(row.account_id)
        if account is None:
            continue
        out.append(
            AccountOut(
                id=row.account_id,
                account_key_masked=account.external_account_key_masked,
                display_name=row.display_name,
                institution=row.institution,
                account_type=row.account_type,
                tax_treatment=row.tax_treatment,
                is_aggregated=row.is_aggregated,
                holdings_value=row.holdings_value,
                cash_balance=row.cash_balance,
                total_value=row.total_value,
                position_count=row.position_count,
                is_stale=row.is_stale,
                staleness_reason=row.staleness_reason,
                supplied_by=row.supplied_by,
                as_of_date=row.as_of_date,
                as_of_confidence=row.as_of_confidence,
                needs_classification=row.tax_treatment == TaxTreatment.UNKNOWN.value,
                preferred_source=account.preferred_source,
            )
        )
    return sorted(out, key=lambda a: a.total_value, reverse=True)


@router.get("/needs-classification", response_model=list[AccountOut])
def list_unclassified(session: SessionDep, snapshot_id: str | None = None) -> list[AccountOut]:
    """The Account Setup worklist.

    Deliberately its own endpoint: on real data this is the normal path rather
    than an edge case, and the UI walks it once after the first import.
    """
    return [a for a in list_accounts(session, snapshot_id) if a.needs_classification]


@router.post("/{account_id}/classification", response_model=AccountOut)
def classify_account(
    account_id: str, payload: ClassifyAccountIn, session: SessionDep
) -> AccountOut:
    """Confirm an account's type and tax treatment.

    Every change is audited, and `classification_source` becomes `user` so a
    later import cannot silently overwrite the decision.
    """
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"account {account_id} not found")

    for field, new_value in (
        ("account_type", payload.account_type.value),
        ("tax_treatment", payload.tax_treatment.value),
        ("owner_label", payload.owner_label),
    ):
        old_value = getattr(account, field)
        if old_value == new_value:
            continue
        session.add(
            AccountAuditLog(
                account_id=account.id,
                field=field,
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(new_value) if new_value is not None else None,
                changed_by="user",
                reason=payload.reason,
            )
        )
        setattr(account, field, new_value)

    account.classification_source = "user"
    account.classification_confidence = None
    session.flush()

    return next(a for a in list_accounts(session, None) if a.id == account_id)


@router.post("/{account_id}/preferred-source", response_model=AccountOut)
def choose_source(account_id: str, payload: PreferredSourceIn, session: SessionDep) -> AccountOut:
    """Record which source is authoritative for this account.

    Asked once, when two comparable documents both describe the account, and
    reused silently thereafter.
    """
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"account {account_id} not found")
    session.add(
        AccountAuditLog(
            account_id=account.id,
            field="preferred_source",
            old_value=account.preferred_source,
            new_value=payload.institution,
            changed_by="user",
            reason=payload.reason,
        )
    )
    set_preferred_source(session, account, payload.institution)
    return next(a for a in list_accounts(session, None) if a.id == account_id)


@router.get("/{account_id}/holdings", response_model=list[HoldingOut])
def get_account_holdings(
    account_id: str, session: SessionDep, snapshot_id: str | None = None
) -> list[HoldingOut]:
    view = load_portfolio(session, resolve_snapshot(session, snapshot_id))
    rows = [h for h in view.holdings if h.account_id == account_id]
    if not rows and not any(a.account_id == account_id for a in view.accounts):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"account {account_id} not found")
    return [HoldingOut.model_validate(h) for h in rows]
