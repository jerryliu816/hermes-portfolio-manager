"""Portfolio-level endpoints: summary, allocation, concentration, gains."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query

from ...analytics import (
    by_account,
    by_asset_class,
    by_institution,
    by_tax_treatment,
    cash_equivalents,
    concentration,
    embedded_gains,
    load_portfolio,
    security_exposures,
    summarize,
)
from ...analytics.allocation import Breakdown
from ...analytics.exposure import SecurityExposure
from ...schemas.responses import (
    BreakdownOut,
    CashBucketOut,
    CashEquivalentsOut,
    ConcentrationOut,
    DataQualityOut,
    EmbeddedGainsOut,
    GainsCoverageOut,
    PortfolioSummaryOut,
    SecurityExposureOut,
    SliceOut,
)
from ..deps import SessionDep, resolve_snapshot

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

ZERO = Decimal("0")


def _breakdown_out(breakdown: Breakdown) -> BreakdownOut:
    return BreakdownOut(
        slices=[
            SliceOut(
                key=s.key,
                label=s.label,
                value=s.value,
                fraction=s.fraction,
                position_count=s.position_count,
            )
            for s in breakdown.slices
        ],
        total=breakdown.total,
        excluded_value=breakdown.excluded_value,
        excluded_fraction=breakdown.excluded_fraction,
        excluded_label=breakdown.excluded_label,
    )


def _exposure_out(exposure: SecurityExposure) -> SecurityExposureOut:
    return SecurityExposureOut(
        security_id=exposure.security_id,
        symbol=exposure.symbol,
        name=exposure.name,
        asset_class=exposure.asset_class,
        total_value=exposure.total_value,
        fraction_of_portfolio=exposure.fraction_of_portfolio,
        account_count=exposure.account_count,
        has_divergent_prices=exposure.has_divergent_prices,
        holdings=[
            {
                "account_id": h.account_id,
                "account_name": h.account_name,
                "institution": h.institution,
                "tax_treatment": h.tax_treatment,
                "quantity": h.quantity,
                "price": h.price,
                "value": h.value,
                "is_stale": h.is_stale,
            }
            for h in exposure.holdings
        ],
    )


@router.get("/summary", response_model=PortfolioSummaryOut)
def get_summary(session: SessionDep, snapshot_id: str | None = None) -> PortfolioSummaryOut:
    """Headline figures, always accompanied by what qualifies them."""
    snapshot = resolve_snapshot(session, snapshot_id)
    view = load_portfolio(session, snapshot)
    summary = summarize(view)
    quality = summary.quality
    return PortfolioSummaryOut(
        snapshot_id=view.snapshot_id,
        as_of_date=view.as_of_date,
        completeness=summary.completeness,
        total_value=summary.total_value,
        holdings_value=summary.holdings_value,
        cash_value=summary.cash_value,
        account_count=summary.account_count,
        position_count=summary.position_count,
        security_count=summary.security_count,
        closed_position_count=summary.closed_position_count,
        skipped_account_count=summary.skipped_account_count,
        quality=DataQualityOut(
            stale_value=quality.stale_value,
            stale_fraction=quality.stale_fraction,
            unclassified_value=quality.unclassified_value,
            unclassified_fraction=quality.unclassified_fraction,
            missing_basis_value=quality.missing_basis_value,
            missing_basis_fraction=quality.missing_basis_fraction,
            aggregated_value=quality.aggregated_value,
            aggregated_fraction=quality.aggregated_fraction,
            stale_accounts=list(quality.stale_accounts),
            unclassified_accounts=list(quality.unclassified_accounts),
            is_fully_trusted=quality.is_fully_trusted,
            caveats=quality.caveats(),
        ),
        warnings=summary.warnings,
    )


@router.get("/allocation/asset-class", response_model=BreakdownOut)
def get_asset_allocation(session: SessionDep, snapshot_id: str | None = None) -> BreakdownOut:
    return _breakdown_out(
        by_asset_class(load_portfolio(session, resolve_snapshot(session, snapshot_id)))
    )


@router.get("/allocation/tax-treatment", response_model=BreakdownOut)
def get_tax_allocation(session: SessionDep, snapshot_id: str | None = None) -> BreakdownOut:
    """Tax placement, with unconfirmed accounts excluded and reported."""
    return _breakdown_out(
        by_tax_treatment(load_portfolio(session, resolve_snapshot(session, snapshot_id)))
    )


@router.get("/cash", response_model=CashEquivalentsOut)
def get_cash_equivalents(session: SessionDep, snapshot_id: str | None = None) -> CashEquivalentsOut:
    """All cash -- settlement balances and money-market holdings -- by tax side.

    The summary's `cash_value` is only the first of those two, because that is
    all some sources report outside their holdings table. This endpoint is the
    one to read for "how much cash do I hold".
    """
    cash = cash_equivalents(load_portfolio(session, resolve_snapshot(session, snapshot_id)))
    return CashEquivalentsOut(
        buckets=[
            CashBucketOut(
                key=b.key,
                label=b.label,
                settlement_value=b.settlement_value,
                holdings_value=b.holdings_value,
                total_value=b.total_value,
                position_count=b.position_count,
                account_count=b.account_count,
                fraction=b.fraction,
                treatments=list(b.treatments),
            )
            for b in cash.buckets
        ],
        total_value=cash.total_value,
        settlement_value=cash.settlement_value,
        holdings_value=cash.holdings_value,
        fraction_of_portfolio=cash.fraction_of_portfolio,
        taxable_value=cash.taxable_value,
        unconfirmed_value=cash.unconfirmed_value,
    )


@router.get("/allocation/institution", response_model=BreakdownOut)
def get_institution_allocation(session: SessionDep, snapshot_id: str | None = None) -> BreakdownOut:
    return _breakdown_out(
        by_institution(load_portfolio(session, resolve_snapshot(session, snapshot_id)))
    )


@router.get("/allocation/account", response_model=BreakdownOut)
def get_account_allocation(session: SessionDep, snapshot_id: str | None = None) -> BreakdownOut:
    return _breakdown_out(
        by_account(load_portfolio(session, resolve_snapshot(session, snapshot_id)))
    )


@router.get("/concentration", response_model=ConcentrationOut)
def get_concentration(
    session: SessionDep,
    snapshot_id: str | None = None,
    threshold: Annotated[
        float, Query(ge=0, le=1, description="Flag holdings at or above this weight")
    ] = 0.05,
) -> ConcentrationOut:
    view = load_portfolio(session, resolve_snapshot(session, snapshot_id))
    result = concentration(view, threshold=Decimal(str(threshold)))
    return ConcentrationOut(
        top_1_fraction=result.top_1_fraction,
        top_5_fraction=result.top_5_fraction,
        top_10_fraction=result.top_10_fraction,
        hhi=result.hhi,
        threshold=result.threshold,
        largest=[_exposure_out(e) for e in result.largest],
        over_threshold=[_exposure_out(e) for e in result.single_holdings_over_threshold],
    )


@router.get("/gains", response_model=EmbeddedGainsOut)
def get_embedded_gains(session: SessionDep, snapshot_id: str | None = None) -> EmbeddedGainsOut:
    """Unrealized gains, with the share of the portfolio they cover."""
    view = load_portfolio(session, resolve_snapshot(session, snapshot_id))
    gains = embedded_gains(view)
    return EmbeddedGainsOut(
        market_value=gains.market_value,
        cost_basis=gains.cost_basis,
        unrealized_gain=gains.unrealized_gain,
        coverage=GainsCoverageOut(
            covered_value=gains.coverage.covered_value,
            uncovered_value=gains.coverage.uncovered_value,
            covered_fraction=gains.coverage.covered_fraction,
            caveat=gains.coverage.caveat(),
        ),
    )


@router.get("/securities", response_model=list[SecurityExposureOut])
def list_security_exposures(
    session: SessionDep,
    snapshot_id: str | None = None,
    divergent_only: bool = Query(
        False, description="Only instruments whose accounts disagree on price"
    ),
) -> list[SecurityExposureOut]:
    view = load_portfolio(session, resolve_snapshot(session, snapshot_id))
    exposures = security_exposures(view)
    if divergent_only:
        exposures = [e for e in exposures if e.has_divergent_prices]
    return [_exposure_out(e) for e in exposures]
