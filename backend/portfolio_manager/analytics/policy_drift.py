"""Evaluating a portfolio against its investment policy.

The hardest thing to get right here is not the arithmetic -- it is refusing to
present drift as more meaningful than the data supports.

Drift is computed against classified value, and a portfolio with unclassified
holdings has a genuine ambiguity: those holdings might belong to any class, so
they could close a gap or widen it. Rather than pick an interpretation, the
evaluator reports the unclassified share alongside every figure and marks the
whole assessment provisional when it is large enough to change a conclusion.

The same applies to staleness. Rebalancing on figures whose as-of date is
unknown means acting on a portfolio you may no longer hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..models.enums import AssetClass, TaxTreatment
from ..schemas.policy import Constraint, ConstraintKind, PolicyDocument
from .allocation import by_account, by_asset_class
from .exposure import security_exposures
from .view import ZERO, PortfolioView

ONE = Decimal("1")

#: Above this share of unclassified value, drift figures can flip a conclusion,
#: so the assessment is marked provisional rather than presented as actionable.
PROVISIONAL_UNCLASSIFIED_THRESHOLD = Decimal("0.05")


@dataclass(frozen=True)
class ClassDrift:
    asset_class: str
    label: str
    target: Decimal
    actual: Decimal
    band: Decimal
    value: Decimal

    @property
    def drift(self) -> Decimal:
        return self.actual - self.target

    @property
    def is_in_band(self) -> bool:
        return abs(self.drift) <= self.band

    @property
    def direction(self) -> str:
        if self.is_in_band:
            return "in_band"
        return "overweight" if self.drift > ZERO else "underweight"

    def rebalance_value(self, total: Decimal) -> Decimal:
        """Value that would need to move to return to target. Signed.

        Positive means the class holds too much. This is arithmetic, not a
        recommendation: it ignores taxes, trading costs, and whether the money
        should move at all.
        """
        return (self.actual - self.target) * total


@dataclass(frozen=True)
class ConstraintResult:
    constraint: Constraint
    satisfied: bool
    actual: Decimal
    detail: str

    @property
    def breach(self) -> Decimal:
        """How far past the limit, as a fraction. Zero when satisfied."""
        if self.satisfied:
            return ZERO
        if self.constraint.kind in (ConstraintKind.MIN_CASH, ConstraintKind.MIN_ASSET_CLASS):
            return self.constraint.limit - self.actual
        return self.actual - self.constraint.limit


@dataclass(frozen=True)
class PlacementObservation:
    """Where an asset class actually sits versus where the policy prefers it."""

    asset_class: str
    preferred: str
    value_in_preferred: Decimal
    value_elsewhere: Decimal

    @property
    def total(self) -> Decimal:
        return self.value_in_preferred + self.value_elsewhere

    @property
    def fraction_in_preferred(self) -> Decimal:
        return ZERO if self.total == ZERO else self.value_in_preferred / self.total


@dataclass
class PolicyAssessment:
    policy_name: str
    policy_version: int
    total_value: Decimal
    classified_value: Decimal
    unclassified_value: Decimal
    drift: list[ClassDrift] = field(default_factory=list)
    constraints: list[ConstraintResult] = field(default_factory=list)
    placement: list[PlacementObservation] = field(default_factory=list)
    #: Reasons the underlying numbers may be unreliable: stale accounts,
    #: unclassified value, an incomplete snapshot.
    data_caveats: list[str] = field(default_factory=list)
    #: Gaps in the policy itself, e.g. holding an asset class it never mentions.
    #: Worth showing, but they say nothing about whether the data is sound.
    policy_notes: list[str] = field(default_factory=list)

    @property
    def caveats(self) -> list[str]:
        """Everything worth showing, data problems first."""
        return [*self.data_caveats, *self.policy_notes]

    @property
    def unclassified_fraction(self) -> Decimal:
        base = self.classified_value + self.unclassified_value
        return ZERO if base == ZERO else self.unclassified_value / base

    @property
    def out_of_band(self) -> list[ClassDrift]:
        return [d for d in self.drift if not d.is_in_band]

    @property
    def breaches(self) -> list[ConstraintResult]:
        return [c for c in self.constraints if not c.satisfied]

    @property
    def is_provisional(self) -> bool:
        """Whether the DATA is too uncertain to act on without checking.

        Deliberately narrow. It is not the same as "there is drift" -- a
        portfolio can be perfectly in band and still be assessed on stale data.
        It is also not the same as "there is something to say": a policy that
        omits an asset class the portfolio holds is a gap in the plan, not a
        reason to distrust the arithmetic, so it does not make the assessment
        provisional.
        """
        return bool(self.data_caveats)

    @property
    def is_compliant(self) -> bool:
        return not self.out_of_band and not self.breaches


def _evaluate_constraint(
    constraint: Constraint, view: PortfolioView, total: Decimal
) -> ConstraintResult:
    kind = constraint.kind

    if kind is ConstraintKind.MAX_SINGLE_HOLDING:
        exposures = security_exposures(view)
        worst = max(exposures, key=lambda e: e.fraction_of_portfolio, default=None)
        actual = worst.fraction_of_portfolio if worst else ZERO
        name = (worst.symbol or worst.name) if worst else "none"
        return ConstraintResult(
            constraint,
            satisfied=actual <= constraint.limit,
            actual=actual,
            detail=f"largest holding is {name} at {actual:.2%}, limit {constraint.limit:.2%}",
        )

    if kind in (ConstraintKind.MAX_ASSET_CLASS, ConstraintKind.MIN_ASSET_CLASS):
        breakdown = by_asset_class(view)
        found = breakdown.get(constraint.subject or "")
        actual = found.fraction if found else ZERO
        satisfied = (
            actual <= constraint.limit
            if kind is ConstraintKind.MAX_ASSET_CLASS
            else actual >= constraint.limit
        )
        comparator = "at most" if kind is ConstraintKind.MAX_ASSET_CLASS else "at least"
        return ConstraintResult(
            constraint,
            satisfied=satisfied,
            actual=actual,
            detail=f"{constraint.subject} is {actual:.2%}, policy says {comparator} "
            f"{constraint.limit:.2%}",
        )

    if kind is ConstraintKind.MIN_CASH:
        breakdown = by_asset_class(view)
        found = breakdown.get(AssetClass.CASH_EQUIVALENT.value)
        holdings_cash = found.value if found else ZERO
        # Cash reported outside a holdings table counts too.
        actual = ZERO if total == ZERO else (holdings_cash + view.cash_value) / total
        return ConstraintResult(
            constraint,
            satisfied=actual >= constraint.limit,
            actual=actual,
            detail=f"cash and equivalents are {actual:.2%}, policy floor {constraint.limit:.2%}",
        )

    if kind is ConstraintKind.MAX_ACCOUNT_SHARE:
        breakdown = by_account(view)
        found = next((s for s in breakdown.slices if s.label == constraint.subject), None)
        actual = found.fraction if found else ZERO
        return ConstraintResult(
            constraint,
            satisfied=actual <= constraint.limit,
            actual=actual,
            detail=f"{constraint.subject} holds {actual:.2%}, limit {constraint.limit:.2%}",
        )

    # MAX_ILLIQUID has no data behind it yet; say so rather than pass silently.
    return ConstraintResult(
        constraint,
        satisfied=True,
        actual=ZERO,
        detail="this constraint cannot be checked yet and was not evaluated",
    )


def assess(view: PortfolioView, policy: PolicyDocument) -> PolicyAssessment:
    """Compare a portfolio against its policy, with the caveats that apply."""
    breakdown = by_asset_class(view)
    unclassified = breakdown.get(AssetClass.UNKNOWN.value)
    unclassified_value = unclassified.value if unclassified else ZERO
    classified_value = breakdown.total - unclassified_value
    total = view.total_value

    assessment = PolicyAssessment(
        policy_name=policy.name,
        policy_version=policy.version,
        total_value=total,
        classified_value=classified_value,
        unclassified_value=unclassified_value,
    )

    # Drift is measured against classified value: an unclassified holding could
    # belong to any class, so including it in the denominator would understate
    # every weight by an unknown amount.
    for asset_class, target in sorted(policy.targets.items(), key=lambda kv: -kv[1]):
        found = breakdown.get(asset_class.value)
        value = found.value if found else ZERO
        actual = ZERO if classified_value == ZERO else value / classified_value
        assessment.drift.append(
            ClassDrift(
                asset_class=asset_class.value,
                label=asset_class.value.replace("_", " ").title(),
                target=target,
                actual=actual,
                band=policy.band_for(asset_class),
                value=value,
            )
        )

    # A class the portfolio holds but the policy never mentions is a real
    # finding: money sitting somewhere the plan does not account for.
    untargeted = [
        s
        for s in breakdown.slices
        if s.key not in {a.value for a in policy.targets} and s.key != AssetClass.UNKNOWN.value
    ]
    for slice_ in untargeted:
        assessment.policy_notes.append(
            f"{slice_.label} is {slice_.fraction:.1%} of classified value but has no "
            "target in the policy"
        )

    assessment.constraints = [_evaluate_constraint(c, view, total) for c in policy.constraints]
    assessment.placement = _placement(view, policy)

    if assessment.unclassified_fraction > PROVISIONAL_UNCLASSIFIED_THRESHOLD:
        assessment.data_caveats.append(
            f"{assessment.unclassified_fraction:.1%} of value is not classified by asset "
            "class, so these weights could shift once it is. Treat the drift figures as "
            "provisional."
        )

    stale_value = sum((a.total_value for a in view.accounts if a.is_stale), ZERO)
    if stale_value > ZERO and total > ZERO:
        assessment.data_caveats.append(
            f"{stale_value / total:.1%} of value is in accounts whose as-of date is "
            "unknown; rebalancing on these figures means acting on a portfolio you may "
            "no longer hold"
        )

    if view.completeness != "complete":
        assessment.data_caveats.append(
            "this snapshot is incomplete, so at least one account is missing from these "
            "figures entirely"
        )

    return assessment


def _placement(view: PortfolioView, policy: PolicyDocument) -> list[PlacementObservation]:
    """Where each preferred asset class actually sits."""
    observations: list[PlacementObservation] = []
    for asset_class in AssetClass:
        preferred = policy.tax_placement.preferred_for(asset_class.value)
        if preferred is None:
            continue
        in_preferred = ZERO
        elsewhere = ZERO
        for holding in view.holdings:
            if not holding.counts_toward_allocation or holding.asset_class != asset_class.value:
                continue
            # An account with unconfirmed tax treatment cannot be judged.
            if holding.tax_treatment == TaxTreatment.UNKNOWN.value:
                continue
            if holding.tax_treatment == preferred.value:
                in_preferred += holding.value
            else:
                elsewhere += holding.value
        if in_preferred + elsewhere > ZERO:
            observations.append(
                PlacementObservation(
                    asset_class=asset_class.value,
                    preferred=preferred.value,
                    value_in_preferred=in_preferred,
                    value_elsewhere=elsewhere,
                )
            )
    return observations
