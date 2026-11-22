"""Deterministic portfolio analytics.

Pure functions over a `PortfolioView`. No model, no floats, no SQL below the
view layer -- so every number here is reproducible and testable, and none of it
depends on an extraction having been interpreted correctly twice.
"""

from .allocation import (
    Breakdown,
    Slice,
    by_account,
    by_asset_class,
    by_institution,
    by_tax_treatment,
)
from .cash import CashBucket, CashEquivalents, cash_equivalents
from .compare import SnapshotComparison, compare_snapshots
from .exposure import (
    Concentration,
    SecurityExposure,
    concentration,
    divergent_price_securities,
    find_exposure,
    security_exposures,
)
from .gains import EmbeddedGains, embedded_gains, gains_by_tax_treatment, largest_embedded_gains
from .summary import DataQuality, PortfolioSummary, summarize
from .view import AccountRow, HoldingRow, PortfolioView, load_portfolio

__all__ = [
    "AccountRow",
    "Breakdown",
    "CashBucket",
    "CashEquivalents",
    "Concentration",
    "DataQuality",
    "EmbeddedGains",
    "HoldingRow",
    "PortfolioSummary",
    "PortfolioView",
    "SecurityExposure",
    "Slice",
    "SnapshotComparison",
    "by_account",
    "by_asset_class",
    "by_institution",
    "by_tax_treatment",
    "cash_equivalents",
    "compare_snapshots",
    "concentration",
    "divergent_price_securities",
    "embedded_gains",
    "find_exposure",
    "gains_by_tax_treatment",
    "largest_embedded_gains",
    "load_portfolio",
    "security_exposures",
    "summarize",
]
