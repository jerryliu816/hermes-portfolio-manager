"""The investment policy document.

A policy is the user's own statement of intent -- target allocations, drift
bands, and constraints -- kept deliberately separate from portfolio truth. It
is the only place in the system that says what the portfolio *should* look
like; everything else describes what it *does* look like.

It round-trips through YAML so it can be edited, reviewed and version-controlled
outside the app, and is validated on the way in: a policy whose targets do not
sum to 100% would silently distort every drift figure computed from it.

All fractions are `Decimal` in [0, 1]. A "5% band" is `0.05`.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models.enums import AssetClass, TaxTreatment

#: Targets must sum to 1, but a hand-edited YAML file will not be exact.
#: One tenth of a percent is generous for rounding and far too small to hide a
#: real mistake.
TARGET_SUM_TOLERANCE = Decimal("0.001")

ONE = Decimal("1")
ZERO = Decimal("0")


class ConstraintKind(StrEnum):
    """The constraint types the evaluator understands.

    Deliberately a closed set: a policy that expresses a rule the system cannot
    check would give false assurance. Anything not listed here belongs in
    `notes`, where it is visible but not pretended to be enforced.
    """

    MAX_SINGLE_HOLDING = "max_single_holding"
    MAX_ASSET_CLASS = "max_asset_class"
    MIN_ASSET_CLASS = "min_asset_class"
    MIN_CASH = "min_cash"
    MAX_ACCOUNT_SHARE = "max_account_share"
    MAX_ILLIQUID = "max_illiquid"


class RebalanceMethod(StrEnum):
    #: Act only when a holding drifts outside its band.
    THRESHOLD = "threshold"
    #: Act on a fixed cadence regardless of drift.
    CALENDAR = "calendar"
    #: Direct new money to underweight positions instead of selling.
    CASH_FLOW = "cash_flow"


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Constraint(_Doc):
    id: str = Field(description="Stable identifier, so results can be tracked over time")
    kind: ConstraintKind
    limit: Decimal = Field(description="The threshold, as a fraction in [0, 1]")
    #: Which asset class or account the rule applies to, where relevant.
    subject: str | None = None
    description: str = ""

    @field_validator("limit")
    @classmethod
    def _within_unit_interval(cls, value: Decimal) -> Decimal:
        if not ZERO <= value <= ONE:
            raise ValueError(f"limit must be a fraction between 0 and 1, got {value}")
        return value

    @model_validator(mode="after")
    def _subject_required_where_meaningful(self) -> Constraint:
        needs_subject = {
            ConstraintKind.MAX_ASSET_CLASS,
            ConstraintKind.MIN_ASSET_CLASS,
            ConstraintKind.MAX_ACCOUNT_SHARE,
        }
        if self.kind in needs_subject and not self.subject:
            raise ValueError(f"constraint {self.id!r} of kind {self.kind} requires a subject")
        return self


class TaxPlacement(_Doc):
    """Which asset classes belong in which kind of account.

    Advisory: the system reports where holdings actually sit against this, and
    never moves anything.
    """

    prefer_tax_deferred: list[AssetClass] = Field(default_factory=list)
    prefer_tax_free: list[AssetClass] = Field(default_factory=list)
    prefer_taxable: list[AssetClass] = Field(default_factory=list)

    def preferred_for(self, asset_class: str) -> TaxTreatment | None:
        for treatment, classes in (
            (TaxTreatment.TAX_DEFERRED, self.prefer_tax_deferred),
            (TaxTreatment.TAX_FREE, self.prefer_tax_free),
            (TaxTreatment.TAXABLE, self.prefer_taxable),
        ):
            if any(c.value == asset_class for c in classes):
                return treatment
        return None


class Rebalancing(_Doc):
    method: RebalanceMethod = RebalanceMethod.THRESHOLD
    #: Trades below this are usually not worth the friction.
    min_trade_value: Decimal = Field(default=Decimal("1000"))
    #: Only meaningful for the calendar method.
    frequency_months: int | None = None


class PolicyDocument(_Doc):
    """A complete investment policy statement."""

    name: str = "default"
    version: int = 1
    notes: str = ""

    #: Target weights by asset class. Must sum to 1.
    targets: dict[AssetClass, Decimal] = Field(default_factory=dict)
    #: Default tolerance around each target, in absolute fraction terms.
    #: 0.05 means a 50% target is in band between 45% and 55%.
    drift_band: Decimal = Field(default=Decimal("0.05"))
    #: Per-class overrides of `drift_band`.
    drift_band_overrides: dict[AssetClass, Decimal] = Field(default_factory=dict)

    constraints: list[Constraint] = Field(default_factory=list)
    tax_placement: TaxPlacement = Field(default_factory=TaxPlacement)
    rebalancing: Rebalancing = Field(default_factory=Rebalancing)

    @field_validator("targets")
    @classmethod
    def _targets_are_fractions(cls, value: dict[AssetClass, Decimal]) -> dict[AssetClass, Decimal]:
        for asset_class, weight in value.items():
            if not ZERO <= weight <= ONE:
                raise ValueError(
                    f"target for {asset_class.value} must be a fraction between 0 and 1, "
                    f"got {weight}"
                )
        return value

    @field_validator("drift_band")
    @classmethod
    def _band_is_sensible(cls, value: Decimal) -> Decimal:
        if not ZERO < value <= ONE:
            raise ValueError(f"drift_band must be greater than 0 and at most 1, got {value}")
        return value

    @model_validator(mode="after")
    def _targets_sum_to_one(self) -> PolicyDocument:
        if not self.targets:
            return self
        total = sum(self.targets.values(), ZERO)
        if abs(total - ONE) > TARGET_SUM_TOLERANCE:
            raise ValueError(
                f"asset-class targets must sum to 1.0, but they sum to {total}. "
                "A policy that does not add up would distort every drift figure "
                "computed from it."
            )
        return self

    @model_validator(mode="after")
    def _constraint_ids_are_unique(self) -> PolicyDocument:
        seen = [c.id for c in self.constraints]
        duplicates = {i for i in seen if seen.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate constraint ids: {sorted(duplicates)}")
        return self

    def band_for(self, asset_class: AssetClass) -> Decimal:
        return self.drift_band_overrides.get(asset_class, self.drift_band)
