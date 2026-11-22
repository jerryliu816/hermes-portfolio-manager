"""The investment policy document and its evaluation.

A policy is the only thing in the system that says what the portfolio *should*
look like, so it is validated strictly: an inconsistent policy would distort
every figure computed from it while looking authoritative.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_manager.analytics.policy_drift import assess
from portfolio_manager.analytics.view import AccountRow, HoldingRow, PortfolioView
from portfolio_manager.schemas.policy import ConstraintKind, PolicyDocument
from portfolio_manager.services.policy import (
    STARTER_POLICY_YAML,
    PolicyError,
    parse_yaml,
    to_yaml,
)

ZERO = Decimal("0")


def _holding(**kw: object) -> HoldingRow:
    base: dict[str, object] = {
        "account_id": "a1",
        "account_name": "Taxable",
        "institution": "Fidelity",
        "account_type": "brokerage",
        "tax_treatment": "taxable",
        "is_aggregated": False,
        "account_is_stale": False,
        "supplied_by": "Fidelity",
        "security_id": "s1",
        "symbol": "AAA",
        "security_name": "Alpha",
        "security_type": "etf",
        "asset_class": "us_equity",
        "quantity": Decimal("1"),
        "price": Decimal("1"),
        "market_value": Decimal("1000"),
        "cost_basis": Decimal("500"),
        "cost_basis_source": "reported",
        "unrealized_gain_loss": Decimal("500"),
        "is_closed": False,
        "value_source": "primary",
    }
    base.update(kw)
    return HoldingRow(**base)  # type: ignore[arg-type]


def _account(**kw: object) -> AccountRow:
    base: dict[str, object] = {
        "account_id": "a1",
        "display_name": "Taxable",
        "institution": "Fidelity",
        "account_type": "brokerage",
        "tax_treatment": "taxable",
        "is_aggregated": False,
        "is_stale": False,
        "staleness_reason": None,
        "supplied_by": "Fidelity",
        "as_of_date": dt.date(2026, 8, 26),
        "as_of_confidence": "stated",
        "holdings_value": Decimal("1000"),
        "cash_balance": ZERO,
        "position_count": 1,
    }
    base.update(kw)
    return AccountRow(**base)  # type: ignore[arg-type]


def _view(holdings: list[HoldingRow], accounts: list[AccountRow], **kw: object) -> PortfolioView:
    base: dict[str, object] = {
        "snapshot_id": "s",
        "as_of_date": dt.date(2026, 8, 26),
        "completeness": "complete",
        "status": "imported",
        "holdings": tuple(holdings),
        "accounts": tuple(accounts),
    }
    base.update(kw)
    return PortfolioView(**base)  # type: ignore[arg-type]


def _policy(**kw: object) -> PolicyDocument:
    base: dict[str, object] = {
        "targets": {"us_equity": Decimal("0.6"), "fixed_income": Decimal("0.4")},
        "drift_band": Decimal("0.05"),
    }
    base.update(kw)
    return PolicyDocument(**base)  # type: ignore[arg-type]


# --- validation -----------------------------------------------------------


def test_targets_that_do_not_sum_to_one_are_rejected() -> None:
    """A policy that does not add up distorts every drift figure from it."""
    with pytest.raises(ValidationError, match="must sum to 1.0"):
        PolicyDocument(targets={"us_equity": Decimal("0.6"), "fixed_income": Decimal("0.2")})


def test_rounding_in_a_hand_edited_file_is_tolerated() -> None:
    policy = PolicyDocument(
        targets={
            "us_equity": Decimal("0.333"),
            "intl_equity": Decimal("0.333"),
            "fixed_income": Decimal("0.334"),
        }
    )
    assert sum(policy.targets.values()) == Decimal("1.000")


def test_a_target_outside_the_unit_interval_is_rejected() -> None:
    with pytest.raises(ValidationError, match="between 0 and 1"):
        PolicyDocument(targets={"us_equity": Decimal("1.5")})


def test_a_zero_drift_band_is_rejected() -> None:
    """Every holding would be permanently out of band."""
    with pytest.raises(ValidationError, match="greater than 0"):
        PolicyDocument(drift_band=Decimal("0"))


def test_duplicate_constraint_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate constraint ids"):
        PolicyDocument(
            constraints=[
                {"id": "x", "kind": "min_cash", "limit": Decimal("0.02")},
                {"id": "x", "kind": "max_single_holding", "limit": Decimal("0.1")},
            ]
        )


def test_a_constraint_needing_a_subject_must_have_one() -> None:
    with pytest.raises(ValidationError, match="requires a subject"):
        PolicyDocument(
            constraints=[{"id": "x", "kind": "max_asset_class", "limit": Decimal("0.5")}]
        )


def test_unknown_fields_are_rejected() -> None:
    """A misspelled key would otherwise be silently ignored."""
    with pytest.raises(ValidationError):
        PolicyDocument(targts={"us_equity": Decimal("1")})  # type: ignore[call-arg]


# --- YAML round-trip ------------------------------------------------------


def test_the_starter_policy_is_valid_and_round_trips() -> None:
    document = parse_yaml(STARTER_POLICY_YAML)
    assert sum(document.targets.values()) == Decimal("1.00")
    assert parse_yaml(to_yaml(document)) == document


def test_yaml_numbers_become_decimal_not_float() -> None:
    """A float target of 0.1 is not 0.1, and these feed drift arithmetic."""
    document = parse_yaml("targets:\n  us_equity: 0.1\n  fixed_income: 0.9\ndrift_band: 0.05\n")
    assert all(isinstance(v, Decimal) for v in document.targets.values())
    assert sum(document.targets.values()) == Decimal("1.0")


def test_malformed_yaml_is_reported_clearly() -> None:
    with pytest.raises(PolicyError, match="not valid YAML"):
        parse_yaml("targets: [unclosed\n")


def test_a_non_mapping_policy_is_rejected() -> None:
    with pytest.raises(PolicyError, match="mapping"):
        parse_yaml("- just\n- a\n- list\n")


# --- drift ----------------------------------------------------------------


def test_drift_is_measured_and_banded() -> None:
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("700")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("300")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    result = assess(view, _policy())
    us = next(d for d in result.drift if d.asset_class == "us_equity")
    assert us.actual == Decimal("0.7")
    assert us.drift == Decimal("0.1")
    assert not us.is_in_band
    assert us.direction == "overweight"
    assert us.rebalance_value(result.total_value) == Decimal("100.0")


def test_a_portfolio_on_target_is_compliant() -> None:
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("400")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    result = assess(view, _policy())
    assert result.is_compliant
    assert not result.out_of_band
    assert not result.is_provisional


def test_drift_is_measured_against_classified_value_only() -> None:
    """An unclassified holding could belong to any class.

    Including it in the denominator would understate every weight by an
    unknown amount, which is worse than excluding it and saying so.
    """
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("400")),
            _holding(security_id="s3", asset_class="unknown", market_value=Decimal("500")),
        ],
        [_account(holdings_value=Decimal("1500"))],
    )
    result = assess(view, _policy())
    us = next(d for d in result.drift if d.asset_class == "us_equity")
    assert us.actual == Decimal("0.6"), "unclassified value must not dilute the weight"
    assert result.unclassified_value == Decimal("500")


def test_a_large_unclassified_share_makes_the_assessment_provisional() -> None:
    """Compliant-looking numbers can flip once the rest is classified."""
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("400")),
            _holding(security_id="s3", asset_class="unknown", market_value=Decimal("500")),
        ],
        [_account(holdings_value=Decimal("1500"))],
    )
    result = assess(view, _policy())
    assert result.is_provisional
    assert any("provisional" in c for c in result.data_caveats)


def test_stale_accounts_warn_against_acting_on_the_figures() -> None:
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("400")),
        ],
        [_account(holdings_value=Decimal("1000"), is_stale=True)],
    )
    result = assess(view, _policy())
    assert result.is_provisional
    assert any("no longer hold" in c for c in result.data_caveats)


def test_a_held_class_with_no_target_is_a_policy_gap_not_a_data_problem() -> None:
    """Money sitting somewhere the plan does not account for.

    Worth showing, but it says nothing about whether the arithmetic can be
    trusted -- so it must not make the assessment provisional.
    """
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("300")),
            _holding(security_id="s3", asset_class="real_estate", market_value=Decimal("100")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    result = assess(view, _policy())
    assert any("Real Estate" in c and "no target" in c for c in result.policy_notes)
    assert not result.data_caveats
    assert not result.is_provisional, "a policy gap is not a data-quality problem"


def test_being_in_band_does_not_make_uncertain_data_actionable() -> None:
    """Provisional and non-compliant are different things."""
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("400")),
        ],
        [_account(holdings_value=Decimal("1000"), is_stale=True)],
    )
    result = assess(view, _policy())
    assert result.is_compliant
    assert result.is_provisional


# --- constraints ----------------------------------------------------------


def test_max_single_holding_is_evaluated() -> None:
    view = _view(
        [
            _holding(
                security_id="s1", symbol="BIG", asset_class="us_equity", market_value=Decimal("600")
            ),
            _holding(
                security_id="s2",
                symbol="SM",
                asset_class="fixed_income",
                market_value=Decimal("400"),
            ),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    policy = _policy(
        constraints=[{"id": "cap", "kind": "max_single_holding", "limit": Decimal("0.10")}]
    )
    result = assess(view, policy)
    breach = result.breaches[0]
    assert breach.constraint.kind is ConstraintKind.MAX_SINGLE_HOLDING
    assert breach.actual == Decimal("0.6")
    assert breach.breach == Decimal("0.5")
    assert "BIG" in breach.detail


def test_min_cash_counts_cash_reported_outside_the_holdings_table() -> None:
    """Settlement balances are real money and must satisfy a cash floor."""
    view = _view(
        [
            _holding(asset_class="us_equity", market_value=Decimal("600")),
            _holding(security_id="s2", asset_class="fixed_income", market_value=Decimal("400")),
        ],
        [_account(holdings_value=Decimal("1000"), cash_balance=Decimal("100"))],
    )
    policy = _policy(constraints=[{"id": "cash", "kind": "min_cash", "limit": Decimal("0.05")}])
    result = assess(view, policy)
    assert result.constraints[0].satisfied
    assert result.constraints[0].actual > Decimal("0.05")


def test_an_uncheckable_constraint_says_so_rather_than_passing_silently() -> None:
    policy = _policy(
        constraints=[{"id": "illiquid", "kind": "max_illiquid", "limit": Decimal("0.1")}]
    )
    view = _view([_holding(asset_class="us_equity", market_value=Decimal("1000"))], [_account()])
    result = assess(view, policy)
    assert "cannot be checked yet" in result.constraints[0].detail


# --- tax placement --------------------------------------------------------


def test_placement_reports_where_a_class_actually_sits() -> None:
    view = _view(
        [
            _holding(
                security_id="s1",
                asset_class="fixed_income",
                tax_treatment="tax_deferred",
                market_value=Decimal("300"),
            ),
            _holding(
                security_id="s2",
                asset_class="fixed_income",
                tax_treatment="taxable",
                market_value=Decimal("100"),
            ),
            _holding(security_id="s3", asset_class="us_equity", market_value=Decimal("600")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    policy = _policy(tax_placement={"prefer_tax_deferred": ["fixed_income"]})
    result = assess(view, policy)
    placement = next(p for p in result.placement if p.asset_class == "fixed_income")
    assert placement.value_in_preferred == Decimal("300")
    assert placement.value_elsewhere == Decimal("100")
    assert placement.fraction_in_preferred == Decimal("0.75")


def test_placement_ignores_accounts_whose_tax_treatment_is_unconfirmed() -> None:
    """They cannot be judged, so judging them would be invention."""
    view = _view(
        [
            _holding(
                asset_class="fixed_income", tax_treatment="unknown", market_value=Decimal("400")
            ),
            _holding(security_id="s2", asset_class="us_equity", market_value=Decimal("600")),
        ],
        [_account(holdings_value=Decimal("1000"))],
    )
    policy = _policy(tax_placement={"prefer_tax_deferred": ["fixed_income"]})
    result = assess(view, policy)
    assert not [p for p in result.placement if p.asset_class == "fixed_income"]
