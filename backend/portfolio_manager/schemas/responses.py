"""API response models.

Two things are enforced here rather than left to each endpoint:

* **Money is a string** (see `money.py`).
* **Account identifiers are masked.** The raw key is a real brokerage account
  number; it never leaves the server, appears in a log, or reaches the browser.
  Clients address accounts by our own opaque UUID instead.

Response models also carry the provenance and caveats the analytics produce, so
a client cannot render a total without also having what qualifies it.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .money import Money, OptMoney, Ratio


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DataQualityOut(_Out):
    """How far the numbers can be trusted. Never optional in a response."""

    stale_value: Money
    stale_fraction: Ratio
    unclassified_value: Money
    unclassified_fraction: Ratio
    missing_basis_value: Money
    missing_basis_fraction: Ratio
    aggregated_value: Money
    aggregated_fraction: Ratio
    stale_accounts: list[str]
    unclassified_accounts: list[str]
    is_fully_trusted: bool
    caveats: list[str] = Field(
        description="Plain-language warnings to show beside the headline figure"
    )


class PortfolioSummaryOut(_Out):
    snapshot_id: str
    as_of_date: dt.date
    completeness: Literal["complete", "partial", "unknown"]
    total_value: Money
    holdings_value: Money
    cash_value: Money
    account_count: int
    position_count: int
    security_count: int
    closed_position_count: int
    skipped_account_count: int
    quality: DataQualityOut
    warnings: list[str]


class AccountOut(_Out):
    id: str
    #: Masked. The raw brokerage account number is never returned.
    account_key_masked: str
    display_name: str
    institution: str
    account_type: str
    tax_treatment: str
    is_aggregated: bool
    holdings_value: Money
    cash_balance: Money
    total_value: Money
    position_count: int
    is_stale: bool
    staleness_reason: str | None
    supplied_by: str | None
    as_of_date: dt.date | None
    as_of_confidence: str
    needs_classification: bool = Field(
        description="True when tax treatment is unconfirmed and the account is "
        "excluded from tax analysis"
    )
    preferred_source: str | None


class HoldingOut(_Out):
    account_id: str
    account_name: str
    institution: str
    tax_treatment: str
    security_id: str
    symbol: str | None
    security_name: str
    security_type: str
    asset_class: str
    quantity: OptMoney
    price: OptMoney
    market_value: OptMoney
    cost_basis: OptMoney
    cost_basis_source: str
    unrealized_gain_loss: OptMoney
    is_closed: bool
    value_source: str
    account_is_stale: bool


class AccountHoldingOut(_Out):
    account_id: str
    account_name: str
    institution: str
    tax_treatment: str
    quantity: OptMoney
    price: OptMoney
    value: Money
    is_stale: bool


class SecurityExposureOut(_Out):
    security_id: str
    symbol: str | None
    name: str
    asset_class: str
    total_value: Money
    fraction_of_portfolio: Ratio
    account_count: int
    has_divergent_prices: bool = Field(
        description="Accounts disagree on this instrument's price, usually because "
        "one source is stale or reports a pre-split basis. Surfaced, never averaged."
    )
    holdings: list[AccountHoldingOut] = Field(
        description="Per-account presence. Quantities are NOT summed across "
        "accounts: differing share bases make the sum meaningless."
    )


class SliceOut(_Out):
    key: str
    label: str
    value: Money
    fraction: Ratio
    position_count: int


class BreakdownOut(_Out):
    slices: list[SliceOut]
    total: Money
    excluded_value: Money
    excluded_fraction: Ratio
    excluded_label: str


class CashBucketOut(_Out):
    """Cash on one side of the tax line. Settlement and holdings stay apart."""

    key: Literal["taxable", "tax_sheltered", "unknown"]
    label: str
    settlement_value: Money
    holdings_value: Money
    total_value: Money
    position_count: int
    account_count: int
    fraction: Ratio
    treatments: list[str] = Field(description="The tax treatments folded into this bucket")


class CashEquivalentsOut(_Out):
    buckets: list[CashBucketOut]
    total_value: Money
    settlement_value: Money
    holdings_value: Money
    fraction_of_portfolio: Ratio
    taxable_value: Money
    unconfirmed_value: Money


class GainsCoverageOut(_Out):
    covered_value: Money
    uncovered_value: Money
    covered_fraction: Ratio
    caveat: str | None


class EmbeddedGainsOut(_Out):
    market_value: Money
    cost_basis: Money
    unrealized_gain: Money
    coverage: GainsCoverageOut


class ConcentrationOut(_Out):
    top_1_fraction: Ratio
    top_5_fraction: Ratio
    top_10_fraction: Ratio
    hhi: Ratio = Field(description="Herfindahl-Hirschman index over instrument weights")
    threshold: Ratio
    largest: list[SecurityExposureOut]
    over_threshold: list[SecurityExposureOut]


class SnapshotOut(_Out):
    id: str
    as_of_date: dt.date
    completeness: str
    status: str
    total_value: Money
    account_count: int
    position_count: int
    superseded_by: str | None
    hidden_from_history: bool
    skipped_accounts: list[dict[str, object]]
    unmatched_enrichment: list[dict[str, object]]


class SnapshotRemovalOut(_Out):
    """What deleting a snapshot would do, before anything is done."""

    snapshot: SnapshotOut
    account_count: int
    position_count: int
    #: Documents that fed only this snapshot. Their records go, so they can be
    #: imported again; the preserved bytes stay on disk.
    documents: list[str]
    #: Documents another snapshot also uses. They stay, and stay un-importable.
    shared_documents: list[str]
    recommendation_count: int
    #: As-of dates of snapshots this one superseded, which become live again.
    resurrected: list[dt.date]
    #: Consequences beyond the snapshot itself. Non-empty means the delete is
    #: refused unless `force` is passed.
    objections: list[str]
    needs_force: bool


class PositionChangeOut(_Out):
    label: str
    account_name: str
    symbol: str | None
    before: Money
    after: Money
    delta: Money


class AllocationDriftOut(_Out):
    key: str
    label: str
    before_fraction: Ratio
    after_fraction: Ratio
    drift: Ratio


class SnapshotComparisonOut(_Out):
    before_date: dt.date
    after_date: dt.date
    before_value: Money
    after_value: Money
    value_change: Money = Field(
        description="Change in portfolio value. NOT a return: it mixes market "
        "movement with contributions, withdrawals and changes of data source."
    )
    new_positions: list[PositionChangeOut]
    removed_positions: list[PositionChangeOut]
    changed_positions: list[PositionChangeOut]
    drift: list[AllocationDriftOut]
    confounders: list[str] = Field(
        description="Reasons the value change should not be read as performance"
    )


class ImportOut(_Out):
    outcome: str
    #: The name the user gave the file, so several results can be told apart.
    filename: str
    source_file_id: str
    sha256_prefix: str
    snapshot_id: str | None
    message: str
    reconciliation: dict[str, object] | None
    validation: dict[str, object] | None
    #: True when this document joined a snapshot another document opened.
    joined: bool = False
    #: Accounts this document reports that the snapshot it joined does not
    #: cover, and which were therefore not added. Never silently dropped.
    unmatched_accounts: list[str] = Field(default_factory=list)


class ImportBatchOut(_Out):
    """Several documents filed as one portfolio."""

    results: list[ImportOut]
    #: The order the documents were filed in: widest first, whatever order
    #: they were sent in, so a joining document never costs an account.
    order: list[str]
    reordered: bool
    as_of_date: dt.date | None


# --- investment policy ----------------------------------------------------


class ClassDriftOut(_Out):
    asset_class: str
    label: str
    target: Ratio
    actual: Ratio
    band: Ratio
    value: Money
    drift: Ratio
    is_in_band: bool
    direction: Literal["in_band", "overweight", "underweight"]
    rebalance_value: Money = Field(
        description="Signed value that would move to return to target. Arithmetic "
        "only: it ignores tax, trading cost, and whether the money should move."
    )


class ConstraintResultOut(_Out):
    id: str
    kind: str
    description: str
    limit: Ratio
    actual: Ratio
    satisfied: bool
    breach: Ratio
    detail: str


class PlacementObservationOut(_Out):
    asset_class: str
    preferred: str
    value_in_preferred: Money
    value_elsewhere: Money
    fraction_in_preferred: Ratio


class PolicyAssessmentOut(_Out):
    policy_name: str
    policy_version: int
    total_value: Money
    classified_value: Money
    unclassified_value: Money
    unclassified_fraction: Ratio
    is_compliant: bool
    is_provisional: bool = Field(
        description="The data is too uncertain to act on without checking. Distinct "
        "from having drift: a portfolio can be in band and still assessed on stale "
        "or unclassified data."
    )
    drift: list[ClassDriftOut]
    out_of_band: list[ClassDriftOut]
    constraints: list[ConstraintResultOut]
    breaches: list[ConstraintResultOut]
    placement: list[PlacementObservationOut]
    caveats: list[str] = Field(description="Everything worth showing, data problems first")
    data_caveats: list[str] = Field(
        description="Reasons the numbers may be unreliable: stale accounts, "
        "unclassified value, an incomplete snapshot"
    )
    policy_notes: list[str] = Field(
        description="Gaps in the policy itself, which say nothing about data quality"
    )


class PolicyOut(_Out):
    name: str
    version: int
    is_active: bool
    document: dict[str, object]
    raw_yaml: str | None


class PolicyVersionOut(_Out):
    name: str
    version: int
    is_active: bool
    created_at: dt.datetime


# --- recommendation journal -----------------------------------------------


class DecisionOut(_Out):
    id: str
    decision: Literal["accepted", "rejected", "deferred"]
    note: str | None
    decided_by: str
    decided_at: dt.datetime


class RecommendationOut(_Out):
    id: str
    title: str
    rationale: str
    source: str = Field(description="Who proposed it, e.g. 'hermes' or 'user'")
    status: Literal["pending", "accepted", "rejected", "deferred"]
    is_open: bool = Field(
        description="Whether this still awaits a decision. A deferred "
        "recommendation is open again: setting something aside is not the same "
        "as being finished with it."
    )
    proposal: dict[str, object]
    estimated_tax_impact: OptMoney
    snapshot_id: str | None
    created_at: dt.datetime
    kind: Literal["note", "trades", "target_change"] = "note"
    targets: dict[str, object] | None = Field(
        default=None, description="Proposed target weights as decimal strings"
    )
    simulation: dict[str, object] | None = Field(
        default=None,
        description="The engine's report, computed by the server when the recommendation "
        "was saved. Money and fractions are strings.",
    )
    context_ids: list[str] = Field(default_factory=list)
    advisor_session_id: str | None = None
    decisions: list[DecisionOut] = Field(
        description="Oldest first. Append-only: a decision never replaces an earlier one."
    )


class JournalStatsOut(_Out):
    total_recommendations: int
    pending: int
    accepted: int
    rejected: int
    deferred: int
    decided: int
    acceptance_rate: Ratio | None = Field(
        description="Share of decided recommendations that were accepted. Null when "
        "nothing has been decided, rather than a rate computed from no decisions."
    )


# --- advisor inputs ---------------------------------------------------------


class PhilosophyOut(_Out):
    version: int
    is_active: bool
    body_markdown: str
    created_at: dt.datetime


class AdvisorContextOut(_Out):
    id: str
    kind: Literal["view", "preference", "url", "document"]
    title: str
    body: str
    extraction: Literal["not_applicable", "extracted", "no_text", "not_captured"] = Field(
        description="Whether `body` holds the item's text. `no_text` is a PDF with no "
        "text layer; `not_captured` is a URL the advisor reads live."
    )
    source_url: str | None
    original_filename: str | None
    expires_on: dt.date | None
    is_expired: bool
    is_active: bool
    created_by: Literal["user", "hermes"]
    confirmed: bool = Field(
        description="False for an advisor's paraphrase the user has not yet confirmed"
    )
    created_at: dt.datetime


class AdvisorContextListOut(_Out):
    current: list[AdvisorContextOut]
    expired: list[AdvisorContextOut] = Field(
        description="Active but past their expiry date: still shown, so a lapsed view "
        "is reconsidered rather than silently dropped"
    )
