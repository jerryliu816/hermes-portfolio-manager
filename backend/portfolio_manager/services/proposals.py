"""Proposals on the wire: parsing trades and targets, and reporting the engine.

The rebalance engine (`analytics/rebalance.py`) works in `Decimal` and frozen
dataclasses. Three callers need its results as JSON -- the MCP tools Hermes
calls, the Advisor page's proposal panel, and a saved recommendation -- and all
three must show the same figures, so the translation lives here, once.

Money and fractions leave as strings, for the same reason as everywhere else in
this system. Money arrives as a string too: a trade amount sent as a JSON
number is refused, not rounded.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.allocation import Breakdown, by_asset_class
from ..analytics.policy_drift import PolicyAssessment
from ..analytics.rebalance import (
    AccountCash,
    RebalanceSuggestion,
    SimulationResult,
    TargetGaps,
    TaxEstimate,
    TaxSummary,
    Trade,
    simulate,
    suggest_rebalance,
    target_gaps,
)
from ..analytics.summary import summarize
from ..analytics.view import PortfolioView, load_portfolio
from ..models.advisory import Recommendation
from ..models.entities import PortfolioSnapshot
from ..models.enums import AssetClass, RecommendationKind
from ..schemas.policy import PolicyDocument
from .policy import active_policy

_RATIO_PLACES = Decimal("0.000001")
_CENTS = Decimal("0.01")


class ProposalError(ValueError):
    """A proposal could not be read, or there is nothing to measure it against."""


def money(value: Decimal | None) -> str | None:
    """Dollars to the cent. Engine figures carry more places than any decision needs."""
    if value is None:
        return None
    return format(value.quantize(_CENTS, rounding=ROUND_HALF_UP), "f")


def ratio(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(_RATIO_PLACES, rounding=ROUND_HALF_UP).normalize(), "f")


# --- parsing --------------------------------------------------------------


def parse_trades(raw: object) -> list[Trade]:
    """Trades from a wire payload. Every problem names the trade it is in."""
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ProposalError("trades must be a list")
    if not raw:
        raise ProposalError("trades is empty; there is nothing to simulate")
    trades: list[Trade] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ProposalError(f"trade {index} must be an object")
        missing = [k for k in ("account_id", "action", "security", "amount") if not item.get(k)]
        if missing:
            raise ProposalError(f"trade {index} is missing {', '.join(missing)}")
        try:
            trades.append(Trade.from_mapping(item))
        except (TypeError, ValueError, InvalidOperation, KeyError) as exc:
            raise ProposalError(f"trade {index}: {exc}") from exc
    return trades


def parse_targets(raw: object) -> dict[AssetClass, Decimal] | None:
    """Proposed targets as `{asset_class: fraction}`, fractions as strings."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or not raw:
        raise ProposalError("targets must be a non-empty {asset_class: fraction} object")
    parsed: dict[AssetClass, Decimal] = {}
    for key, value in raw.items():
        try:
            asset_class = AssetClass(str(key))
        except ValueError as exc:
            valid = ", ".join(a.value for a in AssetClass if a is not AssetClass.UNKNOWN)
            raise ProposalError(f"{key!r} is not an asset class; use one of: {valid}") from exc
        if isinstance(value, bool) or not isinstance(value, str | Decimal):
            raise ProposalError(
                f'target for {key} must be a decimal string such as "0.35", never a JSON number'
            )
        try:
            parsed[asset_class] = Decimal(value)
        except InvalidOperation as exc:
            raise ProposalError(f"target for {key} is not a number: {value!r}") from exc
    return parsed


def targets_json(targets: Mapping[AssetClass, Decimal] | None) -> dict[str, str] | None:
    if targets is None:
        return None
    return {k.value: ratio(v) or "0" for k, v in targets.items()}


# --- context -----------------------------------------------------------------


@dataclass(frozen=True)
class ProposalBasis:
    """What a proposal is measured against."""

    snapshot: PortfolioSnapshot
    view: PortfolioView
    policy: PolicyDocument
    #: False when there is no stored policy and the defaults stand in for its
    #: bands and constraints (only allowed with explicitly proposed targets).
    policy_is_stored: bool


def latest_snapshot(session: Session) -> PortfolioSnapshot | None:
    found: PortfolioSnapshot | None = (
        session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.superseded_by.is_(None))
            .order_by(PortfolioSnapshot.as_of_date.desc(), PortfolioSnapshot.created_at.desc())
        )
        .scalars()
        .first()
    )
    return found


def load_basis(session: Session, *, targets: object = None) -> ProposalBasis:
    """The current snapshot and policy. Never invents targets.

    With no stored policy, proposed targets are required: they are the only
    statement of intent there is. Bands and constraints then fall back to the
    defaults, and that is said in every result.
    """
    snapshot = latest_snapshot(session)
    if snapshot is None:
        raise ProposalError("no portfolio snapshot exists yet")
    stored = active_policy(session)
    if stored is None and targets is None:
        raise ProposalError(
            "the user has no investment policy, so there are no targets to measure against. "
            "Ask them, or pass proposed targets explicitly as a target change."
        )
    return ProposalBasis(
        snapshot=snapshot,
        view=load_portfolio(session, snapshot),
        policy=stored.document if stored else PolicyDocument(),
        policy_is_stored=stored is not None,
    )


def _basis_json(
    basis: ProposalBasis, targets: Mapping[AssetClass, Decimal] | None
) -> dict[str, Any]:
    notes: list[str] = []
    if not basis.policy_is_stored:
        notes.append(
            "no stored policy: drift bands, constraints and the minimum trade size are the "
            "defaults, not the user's"
        )
    return {
        "snapshot_id": str(basis.snapshot.id),
        "as_of_date": basis.snapshot.as_of_date.isoformat(),
        "policy_version": basis.policy.version if basis.policy_is_stored else None,
        "targets": "proposed" if targets is not None else "policy",
        "proposed_targets": targets_json(targets),
        "basis_notes": notes,
        **_quality(basis.view),
    }


def _quality(view: PortfolioView) -> dict[str, Any]:
    """How far the figures can be trusted, carried inside every report.

    Inside the report rather than added by each caller, so a stored simulation
    or the proposal panel keeps the same qualifications Hermes sees.
    """
    quality = summarize(view).quality
    return {
        "completeness": view.completeness,
        "stale_fraction": ratio(quality.stale_fraction),
        "unclassified_tax_fraction": ratio(quality.unclassified_fraction),
        "missing_cost_basis_fraction": ratio(quality.missing_basis_fraction),
        "data_caveats": quality.caveats(),
    }


def _caveats(view: PortfolioView, own: Sequence[str]) -> list[str]:
    """The portfolio's caveats followed by the engine's, each once."""
    return list(dict.fromkeys([*summarize(view).quality.caveats(), *own]))


# --- reporting ---------------------------------------------------------------


def _breakdown(b: Breakdown) -> dict[str, Any]:
    return {
        "total": money(b.total),
        "excluded_value": money(b.excluded_value),
        "excluded_label": b.excluded_label or None,
        "slices": [
            {"key": s.key, "label": s.label, "value": money(s.value), "fraction": ratio(s.fraction)}
            for s in b.slices
        ],
    }


def _gaps(g: TargetGaps) -> dict[str, Any]:
    return {
        "targets_proposed": g.targets_proposed,
        "classified_value": money(g.classified_value),
        "unclassified_value": money(g.unclassified_value),
        "uninvested_cash": money(g.uninvested_cash),
        "gaps": [
            {
                "asset_class": x.asset_class,
                "target": ratio(x.target),
                "actual": ratio(x.actual),
                "band": ratio(x.band),
                "current_value": money(x.current_value),
                "target_value": money(x.target_value),
                "gap": money(x.gap),
                "direction": x.direction,
                "untargeted": x.untargeted,
            }
            for x in g.gaps
        ],
        "caveats": list(g.caveats),
    }


def _tax(t: TaxEstimate | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {
        "status": t.status.value,
        "sale_value": money(t.sale_value),
        "estimated_gain": money(t.estimated_gain),
        "basis_coverage": ratio(t.basis_coverage),
        "reason": t.reason,
    }


def _tax_summary(t: TaxSummary) -> dict[str, Any]:
    return {
        "estimated_gain": money(t.estimated_gain),
        "taxable_sale_value": money(t.taxable_sale_value),
        "taxable_sale_value_with_basis": money(t.taxable_sale_value_with_basis),
        "sheltered_sale_value": money(t.sheltered_sale_value),
        "unknown_treatment_sale_value": money(t.unknown_treatment_sale_value),
        "caveat": t.caveat,
    }


def _cash(rows: Sequence[AccountCash]) -> list[dict[str, Any]]:
    return [
        {
            "account_id": c.account_id,
            "account": c.display_name,
            "starting_cash": money(c.starting_cash),
            "net_change": money(c.net_change),
            "ending_cash": money(c.ending_cash),
        }
        for c in rows
    ]


def _assessment(a: PolicyAssessment) -> dict[str, Any]:
    return {
        "is_compliant": a.is_compliant,
        "is_provisional": a.is_provisional,
        "drift": [
            {
                "asset_class": d.asset_class,
                "label": d.label,
                "target": ratio(d.target),
                "actual": ratio(d.actual),
                "drift": ratio(d.drift),
                "band": ratio(d.band),
                "direction": d.direction,
            }
            for d in a.drift
        ],
        "breaches": [{"id": c.constraint.id, "detail": c.detail} for c in a.breaches],
    }


def _account_names(view: PortfolioView) -> dict[str, str]:
    return {a.account_id: a.display_name for a in view.accounts}


def gaps_report(
    basis: ProposalBasis, targets: Mapping[AssetClass, Decimal] | None
) -> dict[str, Any]:
    gaps = target_gaps(basis.view, targets if targets is not None else basis.policy, basis.policy)
    report = {**_basis_json(basis, targets), **_gaps(gaps)}
    report["caveats"] = _caveats(basis.view, gaps.caveats)
    return report


def simulation_report(
    basis: ProposalBasis,
    trades: Sequence[Trade],
    targets: Mapping[AssetClass, Decimal] | None,
) -> dict[str, Any]:
    result: SimulationResult = simulate(basis.view, trades, basis.policy, targets)
    names = _account_names(basis.view)
    return {
        **_basis_json(basis, targets),
        "is_feasible": result.is_feasible,
        "trades": [
            {
                "index": o.index,
                "account_id": o.trade.account_id,
                "account": names.get(o.trade.account_id),
                "action": o.trade.action.value,
                "security": o.trade.security,
                "requested_amount": money(o.trade.amount),
                "applied_amount": money(o.applied_amount),
                "asset_class": o.asset_class,
                "tax": _tax(o.tax),
            }
            for o in result.trades
        ],
        "before": {
            "by_asset_class": _breakdown(by_asset_class(basis.view)),
            "assessment": _assessment(result.assessment_before),
        },
        "after": {
            "by_asset_class": _breakdown(result.by_asset_class),
            "by_tax_treatment": _breakdown(result.by_tax_treatment),
            "assessment": _assessment(result.assessment_after),
            "gaps": _gaps(result.gaps_after),
        },
        "cash": _cash(result.cash),
        "tax": _tax_summary(result.tax),
        "violations": [
            {
                "kind": v.kind.value,
                "detail": v.detail,
                "trade_index": v.trade_index,
                "account_id": v.account_id,
            }
            for v in result.violations
        ],
        "caveats": _caveats(basis.view, result.caveats),
    }


def suggestion_report(
    basis: ProposalBasis, targets: Mapping[AssetClass, Decimal] | None
) -> dict[str, Any]:
    s: RebalanceSuggestion = suggest_rebalance(basis.view, basis.policy, targets)
    return {
        **_basis_json(basis, targets),
        "method": s.method,
        "needs_rebalance": s.needs_rebalance,
        "moves": [
            {
                "account_id": m.account_id,
                "account": m.account_name,
                "tax_treatment": m.tax_treatment,
                "action": m.action.value,
                "asset_class": m.asset_class,
                "amount": money(m.amount),
                "reason": m.reason,
                "tax": _tax(m.tax),
            }
            for m in s.moves
        ],
        "skipped": list(s.skipped),
        "gaps_before": _gaps(s.gaps_before),
        "gaps_after": _gaps(s.gaps_after),
        "cash": _cash(s.cash),
        "tax": _tax_summary(s.tax),
        "caveats": _caveats(basis.view, s.caveats),
    }


def simulate_wire(
    session: Session, trades_raw: object, targets_raw: object = None
) -> dict[str, Any]:
    """Parse, load and simulate in one step. Raises `ProposalError`."""
    trades = parse_trades(trades_raw)
    targets = parse_targets(targets_raw)
    basis = load_basis(session, targets=targets)
    try:
        return simulation_report(basis, trades, targets)
    except ValueError as exc:
        # resolve_policy refuses proposed targets that do not sum to 100%.
        raise ProposalError(str(exc)) from exc


@dataclass(frozen=True)
class StructuredProposal:
    """What a structured recommendation stores, all of it recomputed here."""

    trades: list[dict[str, Any]] | None
    targets: dict[str, str] | None
    simulation: dict[str, Any] | None


def structure_proposal(
    session: Session, kind: str, trades_raw: object = None, targets_raw: object = None
) -> StructuredProposal:
    """Validate a proposal and compute the figures to store with it.

    The single place a saved recommendation gets its numbers, whoever saves
    it, so a journal entry can never carry figures the engine did not produce.
    Raises `ProposalError` (or `ValueError` for targets that do not sum to 1).
    """
    targets = parse_targets(targets_raw)
    if kind == "trades":
        if not trades_raw:
            raise ProposalError("a trades recommendation needs `trades`")
        parse_trades(trades_raw)
        simulation = simulate_wire(session, trades_raw, targets_raw)
        return StructuredProposal(
            trades=list(trades_raw),  # type: ignore[call-overload]
            targets=targets_json(targets),
            simulation=simulation,
        )
    if kind == "target_change":
        if targets is None:
            raise ProposalError("a target change needs `targets`")
        report = gaps_report(load_basis(session, targets=targets), targets)
        return StructuredProposal(trades=None, targets=targets_json(targets), simulation=report)
    if kind == "note":
        return StructuredProposal(trades=None, targets=targets_json(targets), simulation=None)
    raise ProposalError(f"kind must be note, trades or target_change, got {kind!r}")


@dataclass(frozen=True)
class RecordedProposal:
    recommendation: Recommendation
    notes: list[str]


def record_proposal(
    session: Session,
    *,
    title: str,
    rationale: str,
    kind: str,
    source: str,
    trades: object = None,
    targets: object = None,
    context_ids: Sequence[str] | None = None,
    advisor_session_id: uuid.UUID | None = None,
    proposal: Mapping[str, Any] | None = None,
    estimated_tax_impact: str | None = None,
) -> RecordedProposal:
    """Write a recommendation to the journal, with figures only the engine produced.

    Shared by Hermes's `save_recommendation` and the Advisor page's "Save to
    journal", so the two can never disagree about what a proposal costs. A
    model-supplied tax figure is kept only on a free-text note, where there is
    no simulation to contradict it.
    """
    structured = structure_proposal(session, kind, trades, targets)
    notes: list[str] = []
    is_note = kind == RecommendationKind.NOTE.value
    if not is_note and estimated_tax_impact:
        notes.append(
            "estimated_tax_impact was ignored: for a structured proposal the "
            "figures come from the engine's simulation"
        )
    tax: Decimal | None = None
    if is_note and estimated_tax_impact:
        try:
            tax = Decimal(estimated_tax_impact)
        except InvalidOperation as exc:
            raise ProposalError(
                f"estimated_tax_impact is not a number: {estimated_tax_impact!r}"
            ) from exc
    snapshot = latest_snapshot(session)
    record = Recommendation(
        portfolio_snapshot_id=snapshot.id if snapshot else None,
        source=source,
        title=title,
        rationale=rationale,
        kind=kind,
        proposal_json=(
            {"trades": structured.trades} if structured.trades is not None else dict(proposal or {})
        ),
        targets_json=structured.targets,
        simulation_json=structured.simulation,
        context_ids=[str(c) for c in context_ids] if context_ids else None,
        advisor_session_id=advisor_session_id,
        estimated_tax_impact=tax,
    )
    session.add(record)
    session.flush()
    return RecordedProposal(record, notes)
