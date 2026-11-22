"""Rebalancing arithmetic: gaps to target, trade simulation, a baseline plan.

This is the arithmetic an advisor needs so that no model ever computes a dollar
figure. Three functions, all pure over a `PortfolioView`:

* `target_gaps` -- how far each asset class is from its target, in dollars.
* `simulate` -- what a list of proposed trades would do: resulting allocation,
  drift against the policy, cash left in each account, an estimate of the gain
  each sale would realize, and everything about the proposal that is not
  possible or not allowed.
* `suggest_rebalance` -- a deterministic, tax-aware baseline at asset-class
  level per account. It never picks a security; that is the advisor's job, and
  `simulate` is how its choice gets checked.

Rules that run through all three:

* **Trades are dollars, never shares.** A share count after a dollar trade is
  not computed, so a traded position's quantity becomes `None` rather than a
  number derived from a price that may be stale. Share counts are never summed
  across accounts: the same security can be pre-split in one source and
  post-split in another.
* **Money never moves between accounts.** A sale's proceeds stay in the account
  that sold; a buy is funded from that account's cash.
* **The weights are the ones `policy_drift.assess` reports.** Drift is measured
  against classified holdings value, so cash reported outside a holdings table
  is not part of the denominator. It is reported separately as cash available to
  fund buys. Simulated drift is `assess` run on a derived view, never a second
  implementation of it.
* **An estimate carries what it is based on.** A sale with no cost basis has an
  unknown gain, not a zero one. A sale in an account whose tax treatment is not
  confirmed has an unknown tax outcome.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ..ingestion.normalizer import NormalizationError, normalize_security_key
from ..models.enums import AssetClass, CostBasisSource, TaxTreatment
from ..schemas.policy import ConstraintKind, PolicyDocument, RebalanceMethod
from .allocation import Breakdown, by_asset_class, by_tax_treatment
from .policy_drift import PolicyAssessment, assess
from .view import ZERO, AccountRow, HoldingRow, PortfolioView

#: Treatments under which a sale is not a realization event.
SHELTERED_TREATMENTS = frozenset(
    {TaxTreatment.TAX_DEFERRED.value, TaxTreatment.TAX_FREE.value, TaxTreatment.EDUCATION.value}
)

#: Caveats that apply to every simulation, because the arithmetic cannot see them.
_TAX_ESTIMATE_LIMITS = (
    "gain estimates use each position's proportional (average) cost basis; they do not "
    "reflect lot selection, holding period, wash-sale rules or trading costs"
)


# --- inputs ----------------------------------------------------------------


class TradeAction(StrEnum):
    SELL = "sell"
    BUY = "buy"


@dataclass(frozen=True)
class Trade:
    """One proposed trade, in dollars.

    `security` is a ticker or a description: `Security.symbol` is nullable, so a
    holding with no ticker is named by its description and matched on the same
    normalized key the importer uses. `asset_class` is required only to buy a
    security the portfolio does not hold anywhere.
    """

    account_id: str
    action: TradeAction
    security: str
    amount: Decimal
    asset_class: AssetClass | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(
                f"trade amount must be a Decimal, got {type(self.amount).__name__}; "
                "money is never a float"
            )
        if not self.amount.is_finite() or self.amount <= ZERO:
            raise ValueError(f"trade amount must be a positive dollar figure, got {self.amount}")
        if not self.security.strip():
            raise ValueError("a trade must name a security")

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Trade:
        """Parse a trade from a wire payload, where money arrives as a string."""
        amount = data.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, str | Decimal):
            raise TypeError("trade amount must be a decimal string, never a JSON number")
        asset_class = data.get("asset_class")
        return cls(
            account_id=str(data["account_id"]),
            action=TradeAction(str(data["action"])),
            security=str(data["security"]),
            amount=Decimal(amount),
            asset_class=AssetClass(str(asset_class)) if asset_class else None,
        )


TargetsInput = PolicyDocument | Mapping[AssetClass, Decimal] | Mapping[str, Decimal]


def resolve_policy(policy: PolicyDocument, targets: TargetsInput | None = None) -> PolicyDocument:
    """The policy with proposed targets swapped in, validated like any policy.

    A proposed allocation that does not sum to 100% is refused here rather than
    producing gaps that silently do not add up.
    """
    if targets is None:
        return policy
    if isinstance(targets, PolicyDocument):
        return targets
    if any(str(k) == AssetClass.UNKNOWN.value for k in targets):
        raise ValueError("'unknown' is not an asset class a target can be set for")
    data = policy.model_dump()
    data["targets"] = {str(k): v for k, v in targets.items()}
    return PolicyDocument.model_validate(data)


# --- target gaps -----------------------------------------------------------


@dataclass(frozen=True)
class ClassGap:
    asset_class: str
    target: Decimal
    #: Current fraction of classified holdings value, as `assess` reports it.
    actual: Decimal
    band: Decimal
    current_value: Decimal
    target_value: Decimal
    #: Held, but the targets never mention it. Its target is zero by omission.
    untargeted: bool = False

    @property
    def gap(self) -> Decimal:
        """Signed dollars. Positive is overweight."""
        return self.current_value - self.target_value

    @property
    def is_in_band(self) -> bool:
        return abs(self.actual - self.target) <= self.band

    @property
    def direction(self) -> str:
        if self.is_in_band:
            return "in_band"
        return "overweight" if self.gap > ZERO else "underweight"


@dataclass(frozen=True)
class TargetGaps:
    policy_name: str
    targets_proposed: bool
    classified_value: Decimal
    #: Excluded from every figure: it could belong to any class.
    unclassified_value: Decimal
    #: Cash reported outside holdings. Not in the weights; available for buys.
    uninvested_cash: Decimal
    gaps: tuple[ClassGap, ...]
    caveats: tuple[str, ...]

    def get(self, asset_class: str) -> ClassGap | None:
        return next((g for g in self.gaps if g.asset_class == asset_class), None)

    @property
    def out_of_band(self) -> tuple[ClassGap, ...]:
        return tuple(g for g in self.gaps if not g.is_in_band)


def _gaps_from(
    view: PortfolioView, policy: PolicyDocument, assessment: PolicyAssessment, proposed: bool
) -> TargetGaps:
    base = assessment.classified_value
    gaps = [
        ClassGap(
            asset_class=d.asset_class,
            target=d.target,
            actual=d.actual,
            band=d.band,
            current_value=d.value,
            target_value=d.target * base,
        )
        for d in assessment.drift
    ]
    targeted = {d.asset_class for d in assessment.drift}
    breakdown = by_asset_class(view)
    for slice_ in breakdown.slices:
        if slice_.key in targeted or slice_.key == AssetClass.UNKNOWN.value:
            continue
        gaps.append(
            ClassGap(
                asset_class=slice_.key,
                target=ZERO,
                actual=ZERO if base == ZERO else slice_.value / base,
                band=policy.band_for(AssetClass(slice_.key)),
                current_value=slice_.value,
                target_value=ZERO,
                untargeted=True,
            )
        )

    caveats = list(assessment.caveats)
    if view.cash_value != ZERO:
        caveats.append(
            f"{view.cash_value} of cash is reported outside holdings; like policy drift, "
            "these weights leave it out, and it is available to fund buys"
        )
    return TargetGaps(
        policy_name=policy.name,
        targets_proposed=proposed,
        classified_value=base,
        unclassified_value=assessment.unclassified_value,
        uninvested_cash=view.cash_value,
        gaps=tuple(gaps),
        caveats=tuple(caveats),
    )


def target_gaps(
    view: PortfolioView,
    targets: TargetsInput,
    policy: PolicyDocument | None = None,
) -> TargetGaps:
    """Dollar over- and underweight per asset class against `targets`.

    `targets` is the current policy or a proposed `{asset_class: fraction}`
    mapping. A proposed mapping borrows bands and constraints from `policy` when
    given, and the defaults otherwise.
    """
    proposed = not isinstance(targets, PolicyDocument)
    effective = resolve_policy(policy or PolicyDocument(), targets)
    return _gaps_from(view, effective, assess(view, effective), proposed)


# --- tax estimates ---------------------------------------------------------


class TaxStatus(StrEnum):
    ESTIMATED = "estimated"
    #: Every sold position lacks a cost basis.
    BASIS_MISSING = "basis_missing"
    #: Sold inside a tax-deferred, tax-free or education account.
    NO_REALIZATION_EVENT = "no_realization_event"
    #: The account's tax treatment has not been confirmed.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaxEstimate:
    status: TaxStatus
    sale_value: Decimal
    #: Realized gain (negative for a loss) over the part of the sale with a
    #: basis. `None` whenever it cannot be estimated -- never zero for unknown.
    estimated_gain: Decimal | None
    #: Fraction of the sale's value that has a cost basis. `None` where basis
    #: does not matter (sheltered) or the treatment is unknown.
    basis_coverage: Decimal | None
    reason: str


def _estimate_tax(
    account: AccountRow, portions: Sequence[tuple[HoldingRow, Decimal]]
) -> TaxEstimate:
    """Gain realized by selling `amount` out of each row, proportionally."""
    sale_value = sum((amount for _, amount in portions), ZERO)
    if account.tax_treatment == TaxTreatment.UNKNOWN.value:
        return TaxEstimate(
            TaxStatus.UNKNOWN,
            sale_value,
            None,
            None,
            "the account's tax treatment is not confirmed, so whether this sale is taxable "
            "is unknown",
        )
    if account.tax_treatment in SHELTERED_TREATMENTS:
        return TaxEstimate(
            TaxStatus.NO_REALIZATION_EVENT,
            sale_value,
            None,
            None,
            f"sold inside a {account.tax_treatment.replace('_', '-')} account: "
            "no realization event",
        )

    covered = ZERO
    gain = ZERO
    for row, amount in portions:
        if (
            row.cost_basis is None
            or row.cost_basis_source == CostBasisSource.UNKNOWN.value
            or row.value == ZERO
        ):
            continue
        covered += amount
        gain += amount - row.cost_basis * (amount / row.value)
    coverage = ZERO if sale_value == ZERO else covered / sale_value
    if covered == ZERO:
        return TaxEstimate(
            TaxStatus.BASIS_MISSING,
            sale_value,
            None,
            coverage,
            "no cost basis is reported for this position, so the realized gain is unknown",
        )
    reason = "proportional cost basis"
    if covered < sale_value:
        reason = (
            f"proportional cost basis, covering {coverage:.1%} of the sale; the rest "
            "reports no basis and is not in this figure"
        )
    return TaxEstimate(TaxStatus.ESTIMATED, sale_value, gain, coverage, reason)


@dataclass(frozen=True)
class TaxSummary:
    """Realized-gain estimate across every sale, with what it covers."""

    estimated_gain: Decimal | None
    taxable_sale_value: Decimal
    taxable_sale_value_with_basis: Decimal
    sheltered_sale_value: Decimal
    unknown_treatment_sale_value: Decimal

    @property
    def caveat(self) -> str | None:
        parts = []
        missing = self.taxable_sale_value - self.taxable_sale_value_with_basis
        if missing > ZERO:
            parts.append(f"{missing} of taxable sales has no cost basis")
        if self.unknown_treatment_sale_value > ZERO:
            parts.append(
                f"{self.unknown_treatment_sale_value} is sold in accounts with unconfirmed "
                "tax treatment"
            )
        if not parts:
            return None
        return "; ".join(parts) + ", so the gain estimate does not cover it"


def _summarize_tax(estimates: Iterable[TaxEstimate]) -> TaxSummary:
    gain: Decimal | None = None
    taxable = covered = sheltered = unknown = ZERO
    for e in estimates:
        if e.status is TaxStatus.UNKNOWN:
            unknown += e.sale_value
        elif e.status is TaxStatus.NO_REALIZATION_EVENT:
            sheltered += e.sale_value
        else:
            taxable += e.sale_value
            if e.estimated_gain is not None and e.basis_coverage is not None:
                covered += e.sale_value * e.basis_coverage
                gain = (gain or ZERO) + e.estimated_gain
    return TaxSummary(gain, taxable, covered, sheltered, unknown)


# --- the mutable book a simulation runs on ---------------------------------


class _Book:
    """Working copy of a view that trades are applied to."""

    def __init__(self, view: PortfolioView) -> None:
        self.view = view
        self.rows: list[HoldingRow] = list(view.holdings)
        self.accounts: dict[str, AccountRow] = {a.account_id: a for a in view.accounts}
        self.cash_delta: dict[str, Decimal] = {a.account_id: ZERO for a in view.accounts}
        self.holdings_delta: dict[str, Decimal] = {a.account_id: ZERO for a in view.accounts}
        #: Accounts any trade touched, even one that nets to zero.
        self.touched: set[str] = set()

    def cash(self, account_id: str) -> Decimal:
        return self.accounts[account_id].cash_balance + self.cash_delta[account_id]

    def live_rows(self, account_id: str) -> list[int]:
        return [
            i
            for i, r in enumerate(self.rows)
            if r.account_id == account_id and r.counts_toward_allocation and r.value > ZERO
        ]

    def sell(self, indices: Sequence[int], amount: Decimal) -> list[tuple[HoldingRow, Decimal]]:
        """Sell `amount` across `indices` in proportion to value. Returns portions."""
        total = sum((self.rows[i].value for i in indices), ZERO)
        portions: list[tuple[HoldingRow, Decimal]] = []
        if total == ZERO:
            return portions
        remaining = amount
        for n, i in enumerate(indices):
            row = self.rows[i]
            share = remaining if n == len(indices) - 1 else amount * row.value / total
            remaining -= share
            portions.append((row, share))
            fraction_left = (row.value - share) / row.value
            self.rows[i] = dataclasses.replace(
                row,
                market_value=row.value - share,
                cost_basis=None if row.cost_basis is None else row.cost_basis * fraction_left,
                unrealized_gain_loss=None,
                # A dollar trade does not say how many shares it was.
                quantity=None,
            )
            self.cash_delta[row.account_id] += share
            self.touched.add(row.account_id)
            self.holdings_delta[row.account_id] -= share
        return portions

    def add(
        self,
        account_id: str,
        amount: Decimal,
        *,
        existing: int | None,
        security_id: str,
        name: str,
        symbol: str | None,
        asset_class: str,
        security_type: str,
    ) -> None:
        if existing is not None:
            row = self.rows[existing]
            self.rows[existing] = dataclasses.replace(
                row,
                market_value=row.value + amount,
                cost_basis=None if row.cost_basis is None else row.cost_basis + amount,
                unrealized_gain_loss=None,
                quantity=None,
            )
        else:
            account = self.accounts[account_id]
            self.rows.append(
                HoldingRow(
                    account_id=account_id,
                    account_name=account.display_name,
                    institution=account.institution,
                    account_type=account.account_type,
                    tax_treatment=account.tax_treatment,
                    is_aggregated=account.is_aggregated,
                    account_is_stale=account.is_stale,
                    supplied_by=account.supplied_by,
                    security_id=security_id,
                    symbol=symbol,
                    security_name=name,
                    security_type=security_type,
                    asset_class=asset_class,
                    quantity=None,
                    price=None,
                    market_value=amount,
                    # A proposed buy's basis is what it costs.
                    cost_basis=amount,
                    cost_basis_source=CostBasisSource.REPORTED.value,
                    unrealized_gain_loss=None,
                    is_closed=False,
                    value_source="simulated",
                )
            )
        self.cash_delta[account_id] -= amount
        self.touched.add(account_id)
        self.holdings_delta[account_id] += amount

    def derived_view(self) -> PortfolioView:
        accounts = tuple(
            dataclasses.replace(
                a,
                holdings_value=a.holdings_value + self.holdings_delta[a.account_id],
                cash_balance=a.cash_balance + self.cash_delta[a.account_id],
            )
            for a in self.view.accounts
        )
        return dataclasses.replace(
            self.view, holdings=tuple(self.rows), accounts=accounts, status="simulated"
        )

    def cash_flows(self) -> tuple[AccountCash, ...]:
        return tuple(
            AccountCash(
                account_id=a.account_id,
                display_name=a.display_name,
                starting_cash=a.cash_balance,
                net_change=self.cash_delta[a.account_id],
            )
            for a in self.view.accounts
            if a.account_id in self.touched
        )


# --- simulation ------------------------------------------------------------


class ViolationKind(StrEnum):
    #: The sale is larger than the position. It was applied only up to its value.
    OVERSELL = "oversell"
    #: The account ends with less than zero cash.
    CASH_NEGATIVE = "cash_negative"
    BELOW_MIN_TRADE = "below_min_trade"
    #: A policy constraint is breached after the trades.
    CONSTRAINT_BREACH = "constraint_breach"
    #: Not applied: the account is not in this snapshot.
    UNKNOWN_ACCOUNT = "unknown_account"
    #: Not applied: the account does not hold what is being sold.
    NOT_HELD = "not_held"
    #: Not applied: the name matches more than one security in the account.
    AMBIGUOUS_SECURITY = "ambiguous_security"
    #: Not applied: buying a security held nowhere needs its asset class.
    MISSING_ASSET_CLASS = "missing_asset_class"


@dataclass(frozen=True)
class Violation:
    kind: ViolationKind
    detail: str
    trade_index: int | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class AccountCash:
    account_id: str
    display_name: str
    starting_cash: Decimal
    #: Sale proceeds in, minus buys out.
    net_change: Decimal

    @property
    def ending_cash(self) -> Decimal:
        return self.starting_cash + self.net_change


@dataclass(frozen=True)
class TradeOutcome:
    index: int
    trade: Trade
    #: What was actually applied: zero when the trade was rejected, capped at
    #: the position's value for an oversell.
    applied_amount: Decimal
    asset_class: str | None
    security_id: str | None
    #: For sells only.
    tax: TaxEstimate | None


@dataclass(frozen=True)
class SimulationResult:
    trades: tuple[TradeOutcome, ...]
    by_asset_class: Breakdown
    by_tax_treatment: Breakdown
    #: `assess` on the portfolio before and after the trades.
    assessment_before: PolicyAssessment
    assessment_after: PolicyAssessment
    gaps_after: TargetGaps
    cash: tuple[AccountCash, ...]
    tax: TaxSummary
    violations: tuple[Violation, ...]
    caveats: tuple[str, ...]
    projected_view: PortfolioView

    @property
    def is_feasible(self) -> bool:
        """No trade is impossible as stated. Policy breaches are reported apart."""
        return not any(v.kind is not ViolationKind.CONSTRAINT_BREACH for v in self.violations)


def _matches(row: HoldingRow, wanted: str) -> bool:
    text = wanted.strip()
    if row.security_id == text:
        return True
    if row.symbol and row.symbol.strip().upper() == text.upper():
        return True
    try:
        return normalize_security_key(None, row.security_name) == normalize_security_key(None, text)
    except NormalizationError:
        return False


def _find(rows: Sequence[HoldingRow], indices: Iterable[int], wanted: str) -> list[int]:
    return [i for i in indices if _matches(rows[i], wanted)]


def _breach_violations(before: PolicyAssessment, after: PolicyAssessment) -> list[Violation]:
    already = {c.constraint.id for c in before.breaches}
    return [
        Violation(
            ViolationKind.CONSTRAINT_BREACH,
            f"{c.constraint.id}: {c.detail}"
            + (" (already breached before these trades)" if c.constraint.id in already else ""),
        )
        for c in after.breaches
    ]


def _cash_violations(book: _Book) -> list[Violation]:
    return [
        Violation(
            ViolationKind.CASH_NEGATIVE,
            f"{flow.display_name} would end with {flow.ending_cash} in cash",
            account_id=flow.account_id,
        )
        for flow in book.cash_flows()
        if flow.ending_cash < ZERO and flow.net_change < ZERO
    ]


def simulate(
    view: PortfolioView,
    trades: Sequence[Trade],
    policy: PolicyDocument,
    targets: TargetsInput | None = None,
) -> SimulationResult:
    """Apply `trades` in order to a copy of the portfolio and report the result.

    Nothing about a proposal is silently corrected. A trade that cannot be
    applied is rejected with a violation; an oversell is applied only up to the
    position's value and flagged; everything else -- negative cash, trades
    below the policy minimum, breached constraints -- is applied and flagged.
    """
    effective = resolve_policy(policy, targets)
    book = _Book(view)
    outcomes: list[TradeOutcome] = []
    violations: list[Violation] = []
    minimum = effective.rebalancing.min_trade_value

    for index, trade in enumerate(trades):

        def reject(kind: ViolationKind, detail: str, trade: Trade = trade, i: int = index) -> None:
            violations.append(Violation(kind, detail, i, trade.account_id))
            outcomes.append(TradeOutcome(i, trade, ZERO, trade.asset_class, None, None))

        account = book.accounts.get(trade.account_id)
        if account is None:
            reject(ViolationKind.UNKNOWN_ACCOUNT, f"no account {trade.account_id} in this snapshot")
            continue

        found = _find(book.rows, book.live_rows(trade.account_id), trade.security)
        ids = {book.rows[i].security_id for i in found}
        if len(ids) > 1:
            reject(
                ViolationKind.AMBIGUOUS_SECURITY,
                f"{trade.security!r} matches {len(ids)} securities in {account.display_name}; "
                "name it by ticker or security id",
            )
            continue

        if trade.amount < minimum:
            violations.append(
                Violation(
                    ViolationKind.BELOW_MIN_TRADE,
                    f"{trade.action} of {trade.amount} is below the policy minimum of {minimum}",
                    index,
                    trade.account_id,
                )
            )

        if trade.action is TradeAction.SELL:
            if not found:
                reject(
                    ViolationKind.NOT_HELD,
                    f"{account.display_name} holds no {trade.security!r} to sell",
                )
                continue
            position_value = sum((book.rows[i].value for i in found), ZERO)
            applied = trade.amount
            if trade.amount > position_value:
                applied = position_value
                violations.append(
                    Violation(
                        ViolationKind.OVERSELL,
                        f"sells {trade.amount} of {trade.security} in {account.display_name}, "
                        f"which holds {position_value}; applied up to {position_value}",
                        index,
                        trade.account_id,
                    )
                )
            first = book.rows[found[0]]
            portions = book.sell(found, applied)
            outcomes.append(
                TradeOutcome(
                    index,
                    trade,
                    applied,
                    first.asset_class,
                    first.security_id,
                    _estimate_tax(account, portions),
                )
            )
            continue

        # A buy: of a security this account holds, one held elsewhere, or a new one.
        if found:
            template: HoldingRow | None = book.rows[found[0]]
        else:
            elsewhere = _find(book.rows, range(len(book.rows)), trade.security)
            elsewhere_ids = {book.rows[i].security_id for i in elsewhere}
            if len(elsewhere_ids) > 1:
                reject(
                    ViolationKind.AMBIGUOUS_SECURITY,
                    f"{trade.security!r} matches {len(elsewhere_ids)} securities in the "
                    "portfolio; name it by ticker or security id",
                )
                continue
            template = book.rows[elsewhere[0]] if elsewhere else None

        if template is not None:
            asset_class = template.asset_class
            security_id = template.security_id
            name, symbol, security_type = (
                template.security_name,
                template.symbol,
                template.security_type,
            )
        elif trade.asset_class is not None:
            asset_class = trade.asset_class.value
            security_id = f"proposed:{trade.security.strip()}"
            name, symbol, security_type = trade.security.strip(), None, "unknown"
        else:
            reject(
                ViolationKind.MISSING_ASSET_CLASS,
                f"{trade.security!r} is not held anywhere, so its asset class must be given",
            )
            continue

        book.add(
            trade.account_id,
            trade.amount,
            existing=found[0] if found else None,
            security_id=security_id,
            name=name,
            symbol=symbol,
            asset_class=asset_class,
            security_type=security_type,
        )
        outcomes.append(TradeOutcome(index, trade, trade.amount, asset_class, security_id, None))

    return _result(view, book, effective, targets is not None, outcomes, violations)


def _result(
    view: PortfolioView,
    book: _Book,
    policy: PolicyDocument,
    proposed: bool,
    outcomes: list[TradeOutcome],
    violations: list[Violation],
) -> SimulationResult:
    projected = book.derived_view()
    before = assess(view, policy)
    after = assess(projected, policy)
    violations = [*violations, *_cash_violations(book), *_breach_violations(before, after)]
    tax = _summarize_tax(o.tax for o in outcomes if o.tax is not None)

    caveats = list(after.caveats)
    if any(o.tax is not None for o in outcomes):
        caveats.append(_TAX_ESTIMATE_LIMITS)
    if tax.caveat:
        caveats.append(tax.caveat)

    return SimulationResult(
        trades=tuple(outcomes),
        by_asset_class=by_asset_class(projected),
        by_tax_treatment=by_tax_treatment(projected),
        assessment_before=before,
        assessment_after=after,
        gaps_after=_gaps_from(projected, policy, after, proposed),
        cash=book.cash_flows(),
        tax=tax,
        violations=tuple(violations),
        caveats=tuple(caveats),
        projected_view=projected,
    )


# --- the baseline suggestion -----------------------------------------------


@dataclass(frozen=True)
class SuggestedMove:
    """Buy or sell `amount` of an asset class in one account.

    Which security is the advisor's choice. A sell is spread across the
    account's holdings of the class in proportion to value, which is what the
    tax estimate assumes.
    """

    account_id: str
    account_name: str
    tax_treatment: str
    action: TradeAction
    asset_class: str
    amount: Decimal
    reason: str
    tax: TaxEstimate | None = None


@dataclass(frozen=True)
class RebalanceSuggestion:
    method: str
    #: Whether anything is out of band. Under the threshold method nothing is
    #: suggested when it is not.
    needs_rebalance: bool
    moves: tuple[SuggestedMove, ...]
    #: Moves worked out but dropped, e.g. below `min_trade_value`.
    skipped: tuple[str, ...]
    gaps_before: TargetGaps
    gaps_after: TargetGaps
    assessment_after: PolicyAssessment
    cash: tuple[AccountCash, ...]
    tax: TaxSummary
    caveats: tuple[str, ...]


_UNSPECIFIED = "unspecified:"


def _class_rows(book: _Book, account_id: str, asset_class: str) -> list[int]:
    return [i for i in book.live_rows(account_id) if book.rows[i].asset_class == asset_class]


def _class_value(book: _Book, account_id: str, asset_class: str) -> Decimal:
    return sum((book.rows[i].value for i in _class_rows(book, account_id, asset_class)), ZERO)


def _gain_ratio(book: _Book, account_id: str, asset_class: str) -> Decimal | None:
    """Embedded gain per dollar of value, over the rows with a basis."""
    value = basis = ZERO
    for i in _class_rows(book, account_id, asset_class):
        row = book.rows[i]
        if row.cost_basis is None or row.cost_basis_source == CostBasisSource.UNKNOWN.value:
            continue
        value += row.value
        basis += row.cost_basis
    return None if value == ZERO else (value - basis) / value


class _Planner:
    def __init__(self, view: PortfolioView, policy: PolicyDocument) -> None:
        self.policy = policy
        self.book = _Book(view)
        self.minimum = policy.rebalancing.min_trade_value
        self.moves: list[SuggestedMove] = []
        self.skipped: list[str] = []
        #: Signed dollars still to move per class; positive means buy.
        self.need: dict[str, Decimal] = {}

    def _preference(self, account: AccountRow, asset_class: str) -> int:
        preferred = self.policy.tax_placement.preferred_for(asset_class)
        if preferred is None:
            return 1
        return 0 if preferred.value == account.tax_treatment else 2

    def _buy_order(self, account: AccountRow) -> list[str]:
        return sorted(
            (c for c, n in self.need.items() if n > ZERO),
            key=lambda c: (self._preference(account, c), -self.need[c], c),
        )

    def _record_buy(self, account: AccountRow, asset_class: str, amount: Decimal, why: str) -> None:
        self.book.add(
            account.account_id,
            amount,
            existing=None,
            security_id=f"{_UNSPECIFIED}{asset_class}",
            name=f"unspecified {asset_class.replace('_', ' ')}",
            symbol=None,
            asset_class=asset_class,
            security_type="unknown",
        )
        self.need[asset_class] -= amount
        self.moves.append(
            SuggestedMove(
                account.account_id,
                account.display_name,
                account.tax_treatment,
                TradeAction.BUY,
                asset_class,
                amount,
                why,
            )
        )

    def fund_buys(self, account: AccountRow, budget: Decimal, why: str) -> Decimal:
        """Spend up to `budget` of the account's cash on underweights. Returns spent."""
        spent = ZERO
        for asset_class in self._buy_order(account):
            amount = min(budget - spent, self.need[asset_class])
            if amount <= ZERO:
                break
            if amount < self.minimum:
                self.skipped.append(
                    f"buy {amount} of {asset_class} in {account.display_name}: below the "
                    f"policy minimum of {self.minimum}; left as cash"
                )
                continue
            self._record_buy(account, asset_class, amount, why)
            spent += amount
        return spent

    def sell_excess(self, account: AccountRow, limit: Decimal, taxable: bool) -> Decimal:
        """Sell overweight classes in this account, up to `limit`. Returns proceeds."""
        candidates = [
            c
            for c, n in self.need.items()
            if n < ZERO and _class_value(self.book, account.account_id, c) > ZERO
        ]
        if taxable:
            # Losses first, then the smallest gains; classes with no basis last,
            # because their gain is unknown rather than small.
            def order(c: str) -> tuple[int, Decimal, str]:
                ratio = _gain_ratio(self.book, account.account_id, c)
                return (1, ZERO, c) if ratio is None else (0, ratio, c)

            candidates.sort(key=order)
        else:
            candidates.sort(key=lambda c: (self.need[c], c))

        proceeds = ZERO
        for asset_class in candidates:
            held = _class_value(self.book, account.account_id, asset_class)
            amount = min(-self.need[asset_class], held, limit - proceeds)
            if amount <= ZERO:
                break
            if amount < self.minimum:
                self.skipped.append(
                    f"sell {amount} of {asset_class} in {account.display_name}: below the "
                    f"policy minimum of {self.minimum}"
                )
                continue
            portions = self.book.sell(
                _class_rows(self.book, account.account_id, asset_class), amount
            )
            self.need[asset_class] += amount
            why = (
                "overweight; sold inside a tax-advantaged account, where it realizes no gain"
                if not taxable
                else "overweight; sold in a taxable account because the tax-advantaged "
                "accounts could not close the gap, lowest embedded gain first"
            )
            self.moves.append(
                SuggestedMove(
                    account.account_id,
                    account.display_name,
                    account.tax_treatment,
                    TradeAction.SELL,
                    asset_class,
                    amount,
                    why,
                    _estimate_tax(account, portions),
                )
            )
            proceeds += amount
        return proceeds

    def remaining_need(self) -> Decimal:
        return sum((n for n in self.need.values() if n > ZERO), ZERO)


def _cash_floor(view: PortfolioView, policy: PolicyDocument) -> Decimal:
    """Cash that must stay uninvested to keep a `min_cash` constraint satisfied."""
    floors = [c.limit for c in policy.constraints if c.kind is ConstraintKind.MIN_CASH]
    if not floors:
        return ZERO
    in_holdings = by_asset_class(view).get(AssetClass.CASH_EQUIVALENT.value)
    held = in_holdings.value if in_holdings else ZERO
    return max(ZERO, max(floors) * view.total_value - held)


def _cash_to_close(gaps: TargetGaps) -> Decimal:
    """New money that would bring every targeted class to target with no sale.

    The smallest base at which no class is overweight: max(value / target),
    over classes with a nonzero target. A class held against a zero target can
    never be fixed by adding money, so it does not set the base.
    """
    bases = [g.current_value / g.target for g in gaps.gaps if g.target > ZERO]
    return max(ZERO, max(bases, default=ZERO) - gaps.classified_value)


def suggest_rebalance(
    view: PortfolioView,
    policy: PolicyDocument,
    targets: TargetsInput | None = None,
) -> RebalanceSuggestion:
    """A deterministic, tax-aware baseline, at asset-class level per account.

    In order:

    1. Uninvested cash goes to underweight classes -- taxable cash first, since
       spending it realizes nothing -- keeping any `min_cash` floor.
    2. Remaining gaps are closed inside tax-deferred, tax-free and education
       accounts, where selling realizes no gain.
    3. Only then are taxable accounts sold, losses and smallest gains first.

    Buys prefer the classes the policy's `tax_placement` assigns to that
    account's treatment. Money never moves between accounts, and a move below
    `min_trade_value` is dropped and reported. Accounts with unconfirmed tax
    treatment or an unknown as-of date are left alone: their tax outcome or
    their holdings are not known well enough to trade on. Unclassified
    holdings and classes the policy has no target for are not sold.
    """
    effective = resolve_policy(policy, targets)
    before = assess(view, effective)
    gaps = _gaps_from(view, effective, before, targets is not None)
    method = effective.rebalancing.method
    planner = _Planner(view, effective)
    caveats: list[str] = list(gaps.caveats)

    tradeable = [
        a for a in view.accounts if a.tax_treatment != TaxTreatment.UNKNOWN.value and not a.is_stale
    ]
    held_out = [a for a in view.accounts if a not in tradeable and a.total_value != ZERO]
    if held_out:
        caveats.append(
            f"{len(held_out)} account(s) holding "
            f"{sum((a.total_value for a in held_out), ZERO)} are not traded because their "
            "tax treatment is unconfirmed or their as-of date is unknown"
        )
    untargeted = [g for g in gaps.gaps if g.untargeted]
    if untargeted:
        caveats.append(
            "holdings in classes the policy sets no target for ("
            + ", ".join(g.asset_class for g in untargeted)
            + ") are left in place rather than sold to zero"
        )

    needs_rebalance = bool(gaps.out_of_band)
    if method is RebalanceMethod.THRESHOLD and not needs_rebalance:
        return _suggestion(planner, view, effective, gaps, before, caveats, needs_rebalance)

    # How much uninvested cash can go in, and the base the targets apply to once it has.
    floor = _cash_floor(view, effective)
    spendable = {a.account_id: max(ZERO, a.cash_balance) for a in tradeable}
    available = max(ZERO, sum(spendable.values(), ZERO) - floor)
    deploy = min(available, _cash_to_close(gaps))
    base = gaps.classified_value + deploy
    planner.need = {
        g.asset_class: g.target * base - g.current_value for g in gaps.gaps if not g.untargeted
    }
    if floor > ZERO:
        caveats.append(f"{floor} of cash is kept uninvested to satisfy the policy's cash floor")

    def treatment_order(a: AccountRow) -> tuple[int, str]:
        return (0 if a.tax_treatment == TaxTreatment.TAXABLE.value else 1, a.account_id)

    # 1. Deploy cash.
    left = deploy
    for account in sorted(tradeable, key=treatment_order):
        if left <= ZERO:
            break
        budget = min(spendable[account.account_id], left)
        left -= planner.fund_buys(account, budget, "underweight; funded from uninvested cash")

    if method is not RebalanceMethod.CASH_FLOW:
        # 2. Close gaps where selling realizes nothing, then 3. in taxable accounts.
        for taxable in (False, True):
            for account in sorted(tradeable, key=lambda a: a.account_id):
                is_taxable = account.tax_treatment == TaxTreatment.TAXABLE.value
                if is_taxable is not taxable:
                    continue
                limit = planner.remaining_need()
                if limit <= ZERO:
                    break
                proceeds = planner.sell_excess(account, limit, taxable=is_taxable)
                if proceeds > ZERO:
                    planner.fund_buys(
                        account, proceeds, "underweight; funded by sales in the same account"
                    )
    else:
        caveats.append(
            "the policy rebalances by cash flow, so nothing is sold; gaps that new cash "
            "cannot close remain"
        )

    return _suggestion(planner, view, effective, gaps, before, caveats, needs_rebalance)


def _suggestion(
    planner: _Planner,
    view: PortfolioView,
    policy: PolicyDocument,
    gaps: TargetGaps,
    before: PolicyAssessment,
    caveats: list[str],
    needs_rebalance: bool,
) -> RebalanceSuggestion:
    projected = planner.book.derived_view()
    after = assess(projected, policy)
    tax = _summarize_tax(m.tax for m in planner.moves if m.tax is not None)
    if any(m.tax is not None for m in planner.moves):
        caveats.append(_TAX_ESTIMATE_LIMITS)
    if tax.caveat:
        caveats.append(tax.caveat)
    for violation in _breach_violations(before, after):
        caveats.append(f"after these moves, constraint {violation.detail}")
    return RebalanceSuggestion(
        method=policy.rebalancing.method.value,
        needs_rebalance=needs_rebalance,
        moves=tuple(planner.moves),
        skipped=tuple(planner.skipped),
        gaps_before=gaps,
        gaps_after=_gaps_from(projected, policy, after, gaps.targets_proposed),
        assessment_after=after,
        cash=planner.book.cash_flows(),
        tax=tax,
        caveats=tuple(dict.fromkeys(caveats)),
    )
