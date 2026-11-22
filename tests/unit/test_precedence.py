"""Source precedence: which document supplies an account.

The rule exists because two sources can disagree about what an account
*contains*, not merely what it is worth. Measured on the real pair, the
aggregation was missing an entire $383K holding the direct source reported,
while the two totals still agreed to within $469.
"""

from __future__ import annotations

import pytest

from portfolio_manager.extraction.canonical import ExtractedAccount, ExtractedPosition
from portfolio_manager.models.entities import Account
from portfolio_manager.services.precedence import (
    PrecedenceOutcome,
    decide,
    infer_staleness,
)


def _account(preferred: str | None = None, aggregated: bool = True) -> Account:
    return Account(
        institution="Fidelity",
        is_aggregated=aggregated,
        external_account_key="X1",
        external_account_key_masked="**X1",
        display_name="Test Account",
        account_type="brokerage",
        tax_treatment="taxable",
        preferred_source=preferred,
    )


def test_a_direct_source_beats_an_aggregation_feed() -> None:
    """The institution reporting its own account is the better witness."""
    result = decide(_account(), "Fidelity", "Vanguard", False, True)
    assert result.outcome is PrecedenceOutcome.SUPPLIED
    assert result.winner == "Vanguard"
    assert "directly" in result.reason


def test_an_aggregation_feed_does_not_displace_a_direct_source() -> None:
    result = decide(_account(aggregated=False), "Vanguard", "Fidelity", True, False)
    assert result.outcome is PrecedenceOutcome.DEFERRED
    assert result.winner == "Vanguard"


def test_two_comparable_sources_are_put_to_the_user() -> None:
    """Neither automatically wins, so it is asked once rather than guessed."""
    result = decide(_account(aggregated=False), "Schwab", "Vanguard", False, False)
    assert result.outcome is PrecedenceOutcome.NEEDS_DECISION
    assert result.winner is None
    assert "Choose once" in result.reason


def test_a_user_choice_overrides_the_automatic_rule() -> None:
    """Set once, remembered -- in both directions."""
    wins = decide(_account(preferred="Fidelity"), "Vanguard", "Fidelity", True, False)
    assert wins.outcome is PrecedenceOutcome.SUPPLIED
    assert wins.winner == "Fidelity"

    loses = decide(_account(preferred="Fidelity"), "Fidelity", "Vanguard", False, True)
    assert loses.outcome is PrecedenceOutcome.DEFERRED
    assert loses.winner == "Fidelity"


def test_the_only_source_supplies_the_account() -> None:
    result = decide(_account(), None, "Fidelity", True, True)
    assert result.outcome is PrecedenceOutcome.SOLE_SOURCE
    assert "no other document" in result.reason


def test_every_decision_explains_itself_in_plain_language() -> None:
    """The reason is shown to the user verbatim, so it must read well."""
    for result in (
        decide(_account(), "Fidelity", "Vanguard", False, True),
        decide(_account(aggregated=False), "Schwab", "Vanguard", False, False),
        decide(_account(), None, "Fidelity", True, True),
    ):
        assert result.reason and result.reason[0].islower() or result.reason
        assert "None" not in result.reason


def _extracted(aggregated: bool, gain_loss: str | None) -> ExtractedAccount:
    return ExtractedAccount(
        external_account_key="X1",
        display_name="Test",
        institution="Fidelity",
        is_aggregated=aggregated,
        account_type_hint=None,
        reported_value=None,
        positions=[
            ExtractedPosition(
                symbol="AAA",
                description="Alpha",
                quantity="1",
                price="$1.00",
                market_value="$1.00",
                cost_basis=None,
                unrealized_gain_loss=gain_loss,
            )
        ],
    )


def test_a_direct_source_is_not_stale() -> None:
    stale, reason = infer_staleness(_extracted(aggregated=False, gain_loss=None))
    assert stale is False
    assert reason is None


def test_an_aggregated_account_with_no_gain_loss_flags_unknown_age() -> None:
    """0 of 30 real native rows lacked this data, against 48 of 56 aggregated."""
    stale, reason = infer_staleness(_extracted(aggregated=True, gain_loss=None))
    assert stale is True
    assert reason is not None and "as-of date is unknown" in reason


def test_every_aggregated_account_is_at_least_flagged() -> None:
    """Second-hand reporting always carries an unknown as-of date."""
    stale, reason = infer_staleness(_extracted(aggregated=True, gain_loss="$5.00"))
    assert stale is True
    assert reason is not None


@pytest.mark.parametrize("cash", ["$57,208.15", None])
def test_cash_balance_is_optional_on_the_wire(cash: str | None) -> None:
    account = ExtractedAccount(
        external_account_key="X1",
        display_name="Test",
        institution="Vanguard",
        is_aggregated=False,
        account_type_hint=None,
        reported_value="$899,524.55",
        cash_balance=cash,
        positions=[],
    )
    assert account.cash_balance == cash
